"""Filesystem locations used by ShellDeck."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ShellDeck"


_DATA_DIR: Path | None = None
INSTALLED_MARKER = "installed.marker"


def app_dir() -> Path | None:
    """Portable location: the folder holding the executable (or ShellDeck.app on
    macOS), or the project root when running from a source checkout.
    None when installed as a package (pip/pipx) -> use the per-user folder."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        # the Windows MSI drops this marker: installed apps keep data per-user
        # (%APPDATA%), even when the install folder happens to be writable
        if (exe.parent / INSTALLED_MARKER).exists():
            return None
        for parent in exe.parents:           # .../ShellDeck.app/Contents/MacOS/ShellDeck
            if parent.suffix == ".app":
                # installed in /Applications -> behave like a normal Mac app
                if parent.parent.name == "Applications":
                    return None
                return parent.parent
        return exe.parent
    root = Path(__file__).resolve().parent.parent
    return root if (root / "run.py").exists() else None


def _legacy_dir() -> Path:
    """Per-user folder (the platform convention)."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shelldeck"


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
    override = os.environ.get("SHELLDECK_HOME")
    if override:
        base = Path(override)
        base.mkdir(parents=True, exist_ok=True)
        return base
    if _DATA_DIR is not None:
        return _DATA_DIR
    portable = app_dir()
    base = portable / "data" if portable else _legacy_dir()
    if portable and _writable(base):
        _migrate_legacy(base)
    else:
        base = _legacy_dir()
        base.mkdir(parents=True, exist_ok=True)
    _DATA_DIR = base
    return base


def _migrate_legacy(target: Path) -> None:
    """One-time copy of a vault created by an earlier build in %APPDATA%."""
    legacy = _legacy_dir()
    if (target / "vault.sdv").exists() or not (legacy / "vault.sdv").exists():
        return
    import shutil
    for name in ("vault.sdv", "known_hosts", "settings.json"):
        if (legacy / name).exists():
            shutil.copy2(legacy / name, target / name)


def vault_path() -> Path:
    return data_dir() / "vault.sdv"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def known_hosts_path() -> Path:
    return data_dir() / "known_hosts"


def assets_dir() -> Path:
    # Works both from source and from a PyInstaller bundle.
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "shelldeck" / "assets"
    return Path(__file__).resolve().parent / "assets"
