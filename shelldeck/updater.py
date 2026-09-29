"""Update check + install, based on GitHub Releases (no update server needed).

Only talks to api.github.com / github.com, and only when enabled (on by default,
at most once a day, can be switched off in Settings). No telemetry is sent: the
request is a plain GET for the latest release.

How an update is applied depends on how ShellDeck was installed:
  msi       Windows installer   -> download the new MSI, run it (upgrades in place)
  portable  Windows zip folder  -> download zip, a small script swaps the files after
                                   ShellDeck exits (the data/ folder is kept), restarts
  other     macOS / Linux / pip / source -> open the release page
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .paths import INSTALLED_MARKER

REPO = "blamixology/shelldeck"
API_URL = os.environ.get("SHELLDECK_UPDATE_URL", f"https://api.github.com/repos/{REPO}/releases/latest")
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
USER_AGENT = f"ShellDeck/{__version__} (+https://github.com/{REPO})"


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
        return find(lambda n: n.startswith("shelldeck-windows") and n.endswith(".zip"))
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


# ---------------------------------------------------------------- applying (Windows)
def portable_update_script(new_dir: Path, app_dir: Path, pid: int) -> str:
    """Batch script: wait for ShellDeck to exit, mirror the new files over the old
    ones (keeping data/), then start the new version."""
    return f"""@echo off
setlocal
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto wait)
robocopy "{new_dir}" "{app_dir}" /MIR /XD data /R:5 /W:1 /NFL /NDL /NJH /NJS >nul
if %ERRORLEVEL% GEQ 8 (
  echo Update failed while copying files. Your data folder was not touched.
  pause
  exit /b 1
)
start "" "{app_dir}\\ShellDeck.exe"
rmdir /s /q "{new_dir.parent}" 2>nul
(goto) 2>nul & del "%~f0"
"""


def apply_update(path: Path, kind: str | None = None) -> None:
    """Start installing the downloaded update. The caller must quit the app right after."""
    kind = kind or install_kind()
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if kind == "msi":
        # /passive = progress bar only; MajorUpgrade replaces the old version in place
        subprocess.Popen(["msiexec", "/i", str(path), "/passive"], creationflags=flags, close_fds=True)
        return
    if kind == "portable":
        app_dir = Path(sys.executable).resolve().parent
        work = Path(tempfile.mkdtemp(prefix="shelldeck-update-"))
        with zipfile.ZipFile(path) as z:
            z.extractall(work)
        inner = work / "ShellDeck"
        new_dir = inner if (inner / "ShellDeck.exe").exists() else work
        if not (new_dir / "ShellDeck.exe").exists():
            raise UpdateError("The downloaded package doesn't contain ShellDeck.exe")
        script = work.parent / f"shelldeck-update-{os.getpid()}.bat"
        script.write_text(portable_update_script(new_dir, app_dir, os.getpid()), encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags | 0x08000000,  # CREATE_NO_WINDOW
                         close_fds=True)
        return
    raise UpdateError("Automatic install isn't available for this installation; use the release page.")
