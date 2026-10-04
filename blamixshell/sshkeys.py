"""authorized_keys editing for the Users tab: parse, fingerprint, add / remove, and the command
that writes the file back. No Qt here."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import shlex
from dataclasses import dataclass

KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ssh-dss", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
             "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
_KEY = re.compile(r"(?:^|\s)(" + "|".join(re.escape(t) for t in KEY_TYPES) + r")\s+([A-Za-z0-9+/=]+)(?:\s+(.*))?$")


@dataclass
class Key:
    raw: str                  # the line as it is in the file
    options: str = ""
    type: str = ""
    blob: str = ""
    comment: str = ""
    fingerprint: str = ""
    valid: bool = True        # False for lines we can't read (kept as they are)

    @property
    def bits_hint(self) -> str:
        return {"ssh-ed25519": "256", "ecdsa-sha2-nistp256": "256", "ecdsa-sha2-nistp384": "384",
                "ecdsa-sha2-nistp521": "521"}.get(self.type, "")


def fingerprint(blob_b64: str) -> str:
    """SHA256 fingerprint the way `ssh-keygen -l` prints it, or "" when the key isn't base64."""
    try:
        raw = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError):
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def parse_line(line: str) -> Key | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    m = _KEY.search(s)
    if not m:
        return Key(line, valid=False)
    ktype, blob, comment = m.group(1), m.group(2), (m.group(3) or "").strip()
    options = s[:m.start(1)].strip() if m.start(1) > 0 else ""
    fp = fingerprint(blob)
    return Key(line, options, ktype, blob, comment, fp, valid=bool(fp))


def parse(text: str) -> list[Key]:
    return [k for k in (parse_line(l) for l in text.splitlines()) if k]


def validate_public_key(line: str) -> str:
    """"" when `line` is one usable public key line, else what is wrong."""
    s = line.strip()
    if not s:
        return "Paste a public key (the contents of a .pub file)."
    if "\n" in s:
        return "One key per line."
    if "PRIVATE KEY" in s:
        return "That is a PRIVATE key. Never share it: paste the public key (.pub) instead."
    k = parse_line(s)
    if k is None or not k.valid:
        return "That doesn't look like a public key (ssh-ed25519 AAAA… comment)."
    return ""


def add_key(text: str, line: str) -> tuple[str, bool]:
    """(new file text, added): a key already present (same key material) is not added twice."""
    new = parse_line(line.strip())
    if new is None:
        return text, False
    if any(k.valid and k.blob == new.blob for k in parse(text)):
        return text, False
    base = text if not text or text.endswith("\n") else text + "\n"
    return base + line.strip() + "\n", True


def remove_key(text: str, raw_line: str) -> str:
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l == raw_line:
            del lines[i]
            break
    return "\n".join(lines) + "\n" if lines else ""


def read_command(home: str) -> str:
    return f"cat {_p(home)}/.ssh/authorized_keys 2>&1"


def _p(home: str) -> str:
    return shlex.quote(home.rstrip("/") or "/")


def save_command(home: str, user: str, text: str) -> str:
    """Write the file (base64, so quoting can't break it) with the right owner and permissions."""
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    d = f"{_p(home)}/.ssh"
    steps = [f"mkdir -p {d}", f"chmod 700 {d}", f"echo {b64} | base64 -d > {d}/authorized_keys",
             f"chmod 600 {d}/authorized_keys", f"chown -R {shlex.quote(user)}: {d}",
             f"{{ restorecon -R {d} 2>/dev/null || true; }}"]
    return f"sh -c {shlex.quote(' && '.join(steps))}"


def clean_read(out: str) -> str:
    """The file text from read_command's output ("" when the file or folder doesn't exist yet)."""
    s = out.strip("\n")
    return "" if s.lstrip().startswith("cat:") or "Permission denied" in s else (s + "\n" if s else "")
