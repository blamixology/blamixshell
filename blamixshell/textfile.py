"""Decode/encode files for the built-in editor without changing what the user
didn't touch: encoding, BOM, line endings and the final newline round-trip."""
from __future__ import annotations

from dataclasses import dataclass

EDIT_LIMIT = 5 * 1024 * 1024          # editable up to this size
VIEW_LIMIT = 50 * 1024 * 1024         # read-only up to this size; bigger: download instead


class NotText(Exception):
    pass


@dataclass
class TextMeta:
    encoding: str = "utf-8"
    bom: bool = False
    eol: str = "\n"                   # "\n" | "\r\n" | "\r"
    mixed_eol: bool = False


def looks_binary(data: bytes) -> bool:
    head = data[:8192]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):   # UTF-16 text has NULs
        return False
    return b"\x00" in head


def detect_eol(text: str) -> tuple[str, bool]:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    counts = {"\r\n": crlf, "\n": lf, "\r": cr}
    best = max(counts, key=counts.get)
    if counts[best] == 0:
        return "\n", False
    mixed = sum(1 for v in counts.values() if v) > 1
    return best, mixed


def decode(data: bytes) -> tuple[str, TextMeta]:
    """bytes -> (text with '\\n' line endings, meta to re-encode it faithfully)."""
    if looks_binary(data):
        raise NotText("This looks like a binary file")
    meta = TextMeta()
    if data.startswith(b"\xef\xbb\xbf"):
        meta.encoding, meta.bom = "utf-8", True
        text = data[3:].decode("utf-8", errors="replace")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        meta.encoding, meta.bom = "utf-16", True
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            meta.encoding = "cp1252"
            try:
                text = data.decode("cp1252")
            except UnicodeDecodeError:
                meta.encoding = "latin-1"
                text = data.decode("latin-1")
    meta.eol, meta.mixed_eol = detect_eol(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, meta


def encode(text: str, meta: TextMeta) -> bytes:
    if meta.eol != "\n":
        text = text.replace("\n", meta.eol)
    if meta.encoding == "utf-16":
        return text.encode("utf-16")          # Python writes the BOM itself
    raw = text.encode(meta.encoding, errors="strict")
    if meta.bom and meta.encoding == "utf-8":
        raw = b"\xef\xbb\xbf" + raw
    return raw


EOL_NAMES = {"\n": "LF", "\r\n": "CRLF", "\r": "CR"}
