"""Reading log lines: how bad a line is (for coloring) and filtering with a few lines of context around each match,
like `grep -C`. Used by the desktop and the terminal dashboards. No Qt here."""
from __future__ import annotations

import re

# journalctl -o short-iso and syslog lines have no level field, so the words decide
_ERROR = re.compile(r"\b(emerg|emergency|alert|crit|critical|fatal|panic|err|error|errors|failed|failure|fail|"
                    r"segfault|oom-killer|out of memory|killed process|denied|refused|traceback|exception)\b", re.I)
_WARN = re.compile(r"\b(warn|warning|warnings|deprecated|timeout|timed out|retry|retrying|unable|could not|"
                   r"cannot|can't|not found|invalid|degraded)\b", re.I)
_LEVEL = re.compile(r"(?:\blevel=|\[|<)(?P<lvl>emerg|alert|crit|err|error|warn|warning|notice|info|debug)\b", re.I)


def severity(line: str) -> str:
    """"error", "warn" or "" for one log line. An explicit level (level=info, [INFO]) wins over the words in the
    message, so "INFO: 0 errors" stays plain."""
    m = _LEVEL.search(line)
    if m:
        lvl = m.group("lvl").lower()
        return "error" if lvl in ("emerg", "alert", "crit", "err", "error") else "warn" if lvl.startswith("warn") else ""
    if _ERROR.search(line):
        return "error"
    if _WARN.search(line):
        return "warn"
    return ""


SEPARATOR = "--"


def matching(lines: list[str], needle: str, around: int = 0, only: str = "") -> tuple[list[str], int]:
    """The lines to show and how many matched. `needle`: text (any case) to look for; `around`: lines of context
    before and after each match (gaps between groups become a "--" line, like grep -C); `only`: "error" (errors) or
    "warn" (warnings and errors) keeps just those lines."""
    needle = needle.strip().lower()

    def hit(line: str) -> bool:
        if needle and needle not in line.lower():
            return False
        if only == "error":
            return severity(line) == "error"
        if only == "warn":
            return severity(line) in ("error", "warn")
        return True
    if not needle and not only:
        return list(lines), len(lines)
    hits = [i for i, line in enumerate(lines) if hit(line)]
    if around <= 0:
        return [lines[i] for i in hits], len(hits)
    keep: list[int] = []
    for i in hits:
        for j in range(max(0, i - around), min(len(lines), i + around + 1)):
            if not keep or j > keep[-1]:
                keep.append(j)
    out: list[str] = []
    for n, j in enumerate(keep):
        if n and j != keep[n - 1] + 1:
            out.append(SEPARATOR)
        out.append(lines[j])
    return out, len(hits)
