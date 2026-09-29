"""Filesystem locations used by BlamixShell."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "BlamixShell"
OLD_APP_NAME = "ShellDeck"          # the name before 1.2: its data is migrated on first start
DATA_FILES = ("vault.sdv", "known_hosts", "settings.json")


_DATA_DIR: Path | None = None
INSTALLED_MARKER = "installed.marker"


def app_dir() -> Path | None:
    """Portable location: the folder holding the executable (or BlamixShell.app on
    macOS), or the project root when running from a source checkout.
    None when installed as a package (pip/pipx) -> use the per-user folder."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        # the Windows MSI drops this marker: installed apps keep data per-user
        # (%APPDATA%), even when the install folder happens to be writable
        if (exe.parent / INSTALLED_MARKER).exists():
            return None
        for parent in exe.parents:           # .../BlamixShell.app/Contents/MacOS/BlamixShell
            if parent.suffix == ".app":
                # installed in /Applications -> behave like a normal Mac app
                if parent.parent.name == "Applications":
                    return None
                return parent.parent
        return exe.parent
    root = Path(__file__).resolve().parent.parent
    return root if (root / "run.py").exists() else None


def _user_dir(name: str = APP_NAME) -> Path:
    """Per-user folder (the platform convention)."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / name
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / name.lower()


_legacy_dir = _user_dir   # (kept name: the per-user folder used by installed / pip copies)


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """Portable: data lives in a 'data' folder next to the exe.

    Falls back to the per-user folder (%APPDATA%, ~/Library/Application Support,
    ~/.config) when that isn't writable (Program Files, a translocated macOS app,
    an AppImage) or when installed with pip/pipx."""
    global _DATA_DIR
    override = os.environ.get("BLAMIXSHELL_HOME")
    if override:
        base = Path(override)
        base.mkdir(parents=True, exist_ok=True)
        return base
    if _DATA_DIR is not None:
        return _DATA_DIR
    portable = app_dir()
    base = portable / "data" if portable else _user_dir()
    if portable and _writable(base):
        migrate_data(base, [_user_dir(), _user_dir(OLD_APP_NAME)])
    else:
        base = _user_dir()
        base.mkdir(parents=True, exist_ok=True)
        migrate_data(base, [_user_dir(OLD_APP_NAME)])
    _DATA_DIR = base
    return base


def migrate_data(target: Path, sources: list[Path]) -> Path | None:
    """One-time copy of an existing vault (+ settings, known_hosts) into an empty
    data folder: from the per-user folder, or from the old ShellDeck folder after the
    rename. Copies, never moves, so the old folder stays as a backup.
    Returns the folder copied from, or None."""
    if (target / "vault.sdv").exists():
        return None
    import shutil
    for src in sources:
        if src != target and (src / "vault.sdv").is_file():
            target.mkdir(parents=True, exist_ok=True)
            for name in DATA_FILES:
                if (src / name).is_file():
                    shutil.copy2(src / name, target / name)
            return src
    return None


def vault_path() -> Path:
    return data_dir() / "vault.sdv"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def known_hosts_path() -> Path:
    return data_dir() / "known_hosts"


def assets_dir() -> Path:
    # Works both from source and from a PyInstaller bundle.
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "blamixshell" / "assets"
    return Path(__file__).resolve().parent / "assets"
