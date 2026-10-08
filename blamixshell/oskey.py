"""Unlock with your Windows account: the master password, encrypted with Windows' own per-user protection (DPAPI,
what browsers use for saved passwords), so BlamixShell can open the vault without asking on this PC.

Only your Windows account on this computer can decrypt it: the file is useless on another PC, for another Windows
user, or copied off the machine. Anything that runs as you on this PC could decrypt it too, which is why it is
opt-in. It is kept in the local data folder, never in the vault (so it is never synced), and it is tied to one vault
file: another vault, or a changed master password, simply asks again. No Qt here."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

FILE = "unlock-windows.json"
_ENTROPY = b"BlamixShell vault unlock v1"


def available() -> bool:
    return sys.platform == "win32"


def _path() -> Path:
    from .paths import data_dir
    return data_dir() / FILE


# ---------------------------------------------------------------- DPAPI (CryptProtectData), through ctypes
def _crypt(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> Blob:
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, ent, out = blob(data), blob(_ENTROPY), Blob()
    UI_FORBIDDEN = 0x1
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(src), "BlamixShell", ctypes.byref(ent), None, None, UI_FORBIDDEN,
                                      ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, UI_FORBIDDEN,
                                        ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.GetLastError(), "Windows couldn't " + ("protect" if protect else "unprotect") + " it")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------------- remember / recall / forget
def _vault_id(vault_path) -> str:
    return str(Path(vault_path).resolve()).lower()


def remember(password: str, vault_path) -> None:
    """Keep the master password for this vault, encrypted for this Windows account."""
    if not available():
        raise OSError("Only on Windows.")
    blob = _crypt(password.encode("utf-8"), protect=True)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"vault": _vault_id(vault_path), "blob": base64.b64encode(blob).decode()}),
                   encoding="utf-8")
    tmp.replace(p)


def remembered(vault_path=None) -> bool:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return vault_path is None or data.get("vault") == _vault_id(vault_path)


def recall(vault_path) -> str | None:
    """The master password for this vault, or None (not remembered, another vault, another Windows user or PC)."""
    if not available():
        return None
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if data.get("vault") != _vault_id(vault_path):
            return None
        return _crypt(base64.b64decode(data["blob"]), protect=False).decode("utf-8")
    except (OSError, ValueError, KeyError):
        return None


def forget() -> None:
    try:
        _path().unlink()
    except OSError:
        pass
