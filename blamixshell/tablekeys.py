"""How table cells sort (shared by the desktop dashboard and the terminal dashboard). No Qt here."""
from __future__ import annotations

import re

_SIZE = re.compile(r"^([\d.]+)\s*(B|KB|MB|GB|TB)$", re.I)
_ELAPSED = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")


def auto_key(text: str):
    """What a cell sorts by: numbers (also 12%, 1,024), sizes (12.5 MB), durations (2-03:04:05) by value,
    anything else as lower-case text. Numbers come before text."""
    t = str(text).strip()
    try:
        return (0, float(t.rstrip("%").replace(",", "")))
    except ValueError:
        pass
    m = _SIZE.match(t)
    if m:
        return (0, float(m.group(1)) * 1024 ** ["B", "KB", "MB", "GB", "TB"].index(m.group(2).upper()))
    m = _ELAPSED.match(t)
    if m:
        days, hours, mins, secs = (int(x or 0) for x in m.groups())
        return (0, days * 86400 + hours * 3600 + mins * 60 + secs)
    return (1, t.lower())
