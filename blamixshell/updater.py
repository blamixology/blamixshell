"""Update check + install, based on GitHub Releases (no update server needed).

Only talks to api.github.com / github.com, and only when enabled (on by default,
at most once a day, can be switched off in Settings). No telemetry is sent: the
request is a plain GET for the latest release.

How an update is applied depends on how BlamixShell was installed:
  msi       Windows installer   -> download the new MSI, run it (upgrades in place)
  portable  Windows zip folder  -> download zip, a small script swaps the files after
                                   BlamixShell exits (the data/ folder is kept), restarts
  other     macOS / Linux / pip / source -> open the release page
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .paths import INSTALLED_MARKER

from .links import REPO  # noqa: E402
API_URL = os.environ.get("BLAMIXSHELL_UPDATE_URL", f"https://api.github.com/repos/{REPO}/releases/latest")
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
USER_AGENT = f"BlamixShell/{__version__} (+https://github.com/{REPO})"


class UpdateError(Exception):
    pass


def parse_version(v: str) -> tuple[int, ...]:
    m = re.match(r"^v?(\d+(?:\.\d+)*)", (v or "").strip())
    if not m:
        return ()
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    c, cur = parse_version(candidate), parse_version(current)
    return bool(c) and c > cur


@dataclass
class Asset:
    name: str
    url: str
    size: int = 0
    sha256: str = ""          # GitHub publishes "digest": "sha256:<hex>" for release assets


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    page: str
    assets: list[Asset] = field(default_factory=list)


def install_kind() -> str:
    if not getattr(sys, "frozen", False):
        return "source"
    exe_dir = Path(sys.executable).resolve().parent
    if sys.platform == "win32":
        return "msi" if (exe_dir / INSTALLED_MARKER).exists() else "portable"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def _get(url: str, timeout: float = 10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (fixed https URL)


def fetch_latest(url: str = API_URL) -> Release:
    try:
        with _get(url) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise UpdateError(f"Could not check for updates: {e}") from None
    tag = data.get("tag_name") or ""
    assets = []
    for a in data.get("assets", []):
        digest = a.get("digest") or ""
        assets.append(Asset(a.get("name", ""), a.get("browser_download_url", ""), int(a.get("size") or 0),
                            digest.split(":", 1)[1] if digest.startswith("sha256:") else ""))
    return Release(version=".".join(map(str, parse_version(tag))) or tag, tag=tag,
                   notes=(data.get("body") or "").strip(), page=data.get("html_url") or RELEASES_PAGE,
                   assets=assets)


def check(current: str = __version__, url: str = API_URL) -> Release | None:
    """Latest release if it is newer than `current`, else None. Raises UpdateError."""
    rel = fetch_latest(url)
    return rel if is_newer(rel.tag, current) else None


def pick_asset(rel: Release, kind: str | None = None) -> Asset | None:
    kind = kind or install_kind()
    names = {a.name.lower(): a for a in rel.assets}

    def find(pred):
        return next((a for n, a in names.items() if pred(n)), None)

    if kind == "msi":
        return find(lambda n: n.endswith(".msi") and "x64" in n)
    if kind == "portable":
        return find(lambda n: n.startswith("blamixshell-windows") and n.endswith(".zip"))
    return None


def download(asset: Asset, dest_dir: Path, progress=lambda done, total: None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / asset.name
    h = hashlib.sha256()
    done = 0
    try:
        with _get(asset.url, timeout=30) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or asset.size or 0)
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                progress(done, total)
    except Exception as e:
        raise UpdateError(f"Download failed: {e}") from None
    if asset.sha256 and h.hexdigest().lower() != asset.sha256.lower():
        dest.unlink(missing_ok=True)
        raise UpdateError("Downloaded file failed its checksum check; the update was not installed.")
    if asset.size and dest.stat().st_size != asset.size:
        dest.unlink(missing_ok=True)
        raise UpdateError("Downloaded file is incomplete; the update was not installed.")
    return dest


# ---------------------------------------------------------------- the one-file command-line binary
def _machine() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(m, m)


def _is_musl() -> bool:
    for folder in ("/lib", "/usr/lib"):
        try:
            if any(Path(folder).glob("ld-musl-*")):
                return True
        except OSError:
            pass
    return False


def cli_asset_name() -> str:
    """The release file for this system's one-file binary ("" when there isn't one)."""
    m = _machine()
    if sys.platform.startswith("linux"):
        return f"blamixshell-linux-{'musl-' if _is_musl() else ''}{m}"
    if sys.platform == "darwin":
        return f"blamixshell-macos-{'arm64' if m in ('arm64', 'aarch64') else m}"
    return ""


def is_onefile_binary() -> bool:
    """Running as the single-file command-line program (not the folder build, the desktop app or pip)?"""
    if not getattr(sys, "frozen", False):
        return False
    unpacked = getattr(sys, "_MEIPASS", "")
    return bool(unpacked) and Path(unpacked).resolve().parent != Path(sys.executable).resolve().parent


def pick_cli_asset(rel: Release) -> Asset | None:
    want = cli_asset_name().lower()
    return next((a for a in rel.assets if a.name.lower() == want), None) if want else None


def check_binary_file(path: Path) -> str:
    """"" when `path` looks like a program this system can run (an ELF or Mach-O file), else why not."""
    try:
        head = Path(path).read_bytes()[:4]
    except OSError as e:
        return str(e)
    if sys.platform.startswith("linux"):
        ok = head == b"\x7fELF"
    else:
        ok = head in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf")
    return "" if ok else "The downloaded file isn't a program for this system, so it was not installed."


def install_cli_binary(asset: Asset, target: Path | None = None, progress=lambda done, total: None) -> Path:
    """Download `asset`, check its SHA-256, and swap it in for the running program (`target`). The new file is
    staged next to it so the swap is one atomic rename; nothing changes if any step fails."""
    target = Path(target or sys.executable).resolve()
    if not asset.sha256:
        raise UpdateError("This release doesn't publish a checksum for the file, so it won't be installed "
                          "automatically. Download it from the release page and compare its SHA-256 yourself.")
    try:
        staging = Path(tempfile.mkdtemp(prefix=".blamixshell-update-", dir=target.parent))
    except OSError as e:
        raise UpdateError(f"Can't write next to {target}: {e}. Run the update with enough rights (sudo), "
                          "or download the file by hand.") from None
    try:
        got = download(asset, staging, progress)
        problem = check_binary_file(got)
        if problem:
            raise UpdateError(problem)
        got.chmod(0o755)
        os.replace(got, target)
    except OSError as e:
        raise UpdateError(f"Could not replace {target}: {e}") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


# ---------------------------------------------------------------- admin policy (managed / offline)
POLICY_FILE = "policy.ini"


def _policy_files() -> list[Path]:
    files = []
    if getattr(sys, "frozen", False):
        files.append(Path(sys.executable).resolve().parent / POLICY_FILE)
    else:
        files.append(Path(__file__).resolve().parent.parent / POLICY_FILE)
    if sys.platform == "darwin":
        files.append(Path("/Library/Application Support/BlamixShell") / POLICY_FILE)
    elif sys.platform != "win32":
        files.append(Path("/etc/blamixshell") / POLICY_FILE)
    return files


def _registry_policy() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg
    places = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\BlamixShell"),   # Group Policy
              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\BlamixShell"),             # MSI, all users
              (winreg.HKEY_CURRENT_USER, r"Software\Policies\BlamixShell"),
              (winreg.HKEY_CURRENT_USER, r"Software\BlamixShell")]              # MSI, just me
    for root, key in places:
        try:
            with winreg.OpenKey(root, key) as k:
                value, _type = winreg.QueryValueEx(k, "UpdateCheck")
                return str(value)
        except OSError:
            continue
    return None


def _ini_policy() -> str | None:
    import configparser
    for f in _policy_files():
        if f.is_file():
            cp = configparser.ConfigParser()
            try:
                cp.read(f, encoding="utf-8")
            except configparser.Error:
                continue
            if cp.has_option("policy", "update_check"):
                return cp.get("policy", "update_check")
    return None


def update_check_policy() -> bool | None:
    """An administrator's choice, which the user can't change: False = never contact GitHub
    (managed or air-gapped networks), True = always on, None = no policy (user decides).
    Sources: BLAMIXSHELL_UPDATE_CHECK, the Windows registry (set by the MSI's UPDATECHECK
    property or Group Policy), or policy.ini next to the app / in /etc/blamixshell."""
    for value in (os.environ.get("BLAMIXSHELL_UPDATE_CHECK"), _registry_policy(), _ini_policy()):
        if value is None or str(value).strip() == "":
            continue
        v = str(value).strip().lower()
        if v in ("0", "false", "no", "off"):
            return False
        if v in ("1", "true", "yes", "on"):
            return True
    return None


def updates_allowed(settings: dict) -> bool:
    policy = update_check_policy()
    return policy if policy is not None else bool(settings.get("check_updates", True))


# ---------------------------------------------------------------- update from a file (offline)
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_version(path: Path) -> str:
    """Version from a release file name (BlamixShell-1.6.0-x64.msi), or ''."""
    m = re.search(r"(\d+\.\d+\.\d+)", Path(path).name)
    return m.group(1) if m else ""


def check_update_file(path: Path, kind: str | None = None, expected_sha256: str = "") -> str:
    """Problems with a local update package ('' = fine to install)."""
    kind = kind or install_kind()
    path = Path(path)
    if kind not in ("msi", "portable"):
        return ("On this system, install the new version by replacing the app (macOS / Linux) "
                "or with pip; there's nothing to run from here.")
    want = ".msi" if kind == "msi" else ".zip"
    if path.suffix.lower() != want:
        return ("This copy was installed with the installer: pick the .msi file (BlamixShell-x.y.z-x64.msi)."
                if kind == "msi" else "This is the portable version: pick BlamixShell-windows-x64.zip.")
    if kind == "portable":
        try:
            with zipfile.ZipFile(path) as z:
                if not any(n.endswith("BlamixShell.exe") for n in z.namelist()):
                    return "That zip doesn't contain BlamixShell.exe."
        except zipfile.BadZipFile:
            return "That file isn't a valid zip (incomplete copy?)."
    if expected_sha256.strip():
        if sha256_of(path).lower() != expected_sha256.strip().lower().removeprefix("sha256:"):
            return "The file's SHA-256 doesn't match the one you entered: don't install it."
    return ""


# ---------------------------------------------------------------- applying (Windows)
UPDATE_LOG = "blamixshell-update.log"   # in %TEMP%: what the update script did, for support


def _log(line: str) -> str:
    # redirection first: "code 0>>file" would be read as a redirect of handle 0
    return f'>>"%TEMP%\\{UPDATE_LOG}" echo %DATE% %TIME% {line}'


def _wait_and_kill_helpers(pid: int, app_dir: Path, max_wait: int = 20) -> str:
    """Batch lines: wait (at most max_wait seconds) for the app (pid) to exit, force-end
    it if it hangs, then end any leftover app / Chromium helper processes started from
    *this* install folder, so no file stays locked.
    Uses `ping` to sleep: `timeout` fails when the script runs without a console."""
    ps = (f"Get-Process QtWebEngineProcess,BlamixShell,ShellDeck -ErrorAction SilentlyContinue | "
          f"Where-Object {{ $_.Path -like '{app_dir}\\*' }} | Stop-Process -Force")
    return f"""{_log(f"update started, waiting for pid {pid}")}
set /a waited=0
:wait
tasklist /FI "PID eq {pid}" /NH 2>nul | find "{pid}" >nul || goto gone
if %waited% GEQ {max_wait} goto kill
set /a waited+=1
ping -n 2 127.0.0.1 >nul
goto wait
:kill
{_log(f"pid {pid} still running after {max_wait}s: ending it")}
taskkill /PID {pid} /F >nul 2>&1
:gone
powershell -NoProfile -Command "{ps}" >nul 2>&1
ping -n 2 127.0.0.1 >nul
"""


def msi_scope_args(app_dir: Path) -> str:
    """msiexec properties that keep the upgrade in the SAME context as the current
    install. A per-user MSI can't see (or remove) a per-machine install and vice
    versa, so a mismatch leaves two copies and can fail with 1603/2349."""
    local = os.environ.get("LOCALAPPDATA", "")
    try:
        per_user = bool(local) and Path(app_dir).resolve().is_relative_to(Path(local).resolve())
    except (OSError, ValueError):
        per_user = False
    return "ALLUSERS=2 MSIINSTALLPERUSER=1" if per_user else "ALLUSERS=1"


def msi_update_script(msi: Path, app_dir: Path, pid: int) -> str:
    log = Path(tempfile.gettempdir()) / "blamixshell-update-msi.log"
    # after the upgrade the app may live in a new folder (ShellDeck -> BlamixShell rename)
    candidates = [app_dir / "BlamixShell.exe", app_dir.parent / "BlamixShell" / "BlamixShell.exe"]
    starts = " ".join(f'"{c}"' for c in candidates)
    return f"""@echo off
setlocal
{_wait_and_kill_helpers(pid, app_dir)}{_log("running msiexec")}
msiexec /i "{msi}" {msi_scope_args(app_dir)} /passive /norestart /l*v "{log}"
set rc=%ERRORLEVEL%
{_log("msiexec finished with code %rc%")}
if not "%rc%"=="0" if not "%rc%"=="3010" goto end
for %%E in ({starts}) do if exist %%E ( start "" %%E & goto end )
:end
(goto) 2>nul & del "%~f0"
"""


def portable_update_script(new_dir: Path, app_dir: Path, pid: int) -> str:
    """Batch script: wait for BlamixShell to exit, mirror the new files over the old
    ones (keeping data/), then start the new version."""
    return f"""@echo off
setlocal
{_wait_and_kill_helpers(pid, app_dir)}robocopy "{new_dir}" "{app_dir}" /MIR /XD data /R:5 /W:1 /NFL /NDL /NJH /NJS >nul
if %ERRORLEVEL% GEQ 8 (
  {_log("copying the new files failed; data folder untouched")}
  exit /b 1
)
start "" "{app_dir}\\BlamixShell.exe"
rmdir /s /q "{new_dir.parent}" 2>nul
(goto) 2>nul & del "%~f0"
"""


def apply_update(path: Path, kind: str | None = None) -> None:
    """Start installing the downloaded update. The caller must quit the app right after."""
    kind = kind or install_kind()
    # CREATE_NO_WINDOW gives the script a hidden console that its children (tasklist,
    # ping, powershell) share. (With DETACHED_PROCESS every child would pop up a window.)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    if kind == "msi":
        # wait until BlamixShell has fully exited (incl. helper processes), then run the
        # MSI: /passive = progress bar only; MajorUpgrade replaces the old version
        app_dir = Path(sys.executable).resolve().parent
        script = Path(tempfile.gettempdir()) / f"blamixshell-update-{os.getpid()}.bat"
        script.write_text(msi_update_script(path, app_dir, os.getpid()), encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True)
        return
    if kind == "portable":
        app_dir = Path(sys.executable).resolve().parent
        work = Path(tempfile.mkdtemp(prefix="blamixshell-update-"))
        with zipfile.ZipFile(path) as z:
            z.extractall(work)
        inner = work / "BlamixShell"
        new_dir = inner if (inner / "BlamixShell.exe").exists() else work
        if not (new_dir / "BlamixShell.exe").exists():
            raise UpdateError("The downloaded package doesn't contain BlamixShell.exe")
        script = work.parent / f"blamixshell-update-{os.getpid()}.bat"
        script.write_text(portable_update_script(new_dir, app_dir, os.getpid()), encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True)
        return
    raise UpdateError("Automatic install isn't available for this installation; use the release page.")
