"""Alerts history: when a server crossed a limit (disk, memory, swap, load) or a service failed, and when that ended.
Recorded from the health strip's polls of the active terminal; kept in a small JSON file next to the settings
(nothing leaves this computer). No Qt here."""
from __future__ import annotations

import json
import time
from pathlib import Path

KEEP_DAYS = 30
LIMIT = 2000
# an alert of a server that is no longer polled (another tab is active, the app was closed) can't be called
# "ongoing": after this long without a poll it shows "last seen" instead
STALE_AFTER = 120


def default_path() -> Path:
    from .paths import data_dir
    return data_dir() / "alerts.json"


class AlertLog:
    def __init__(self, path: Path | None = None, clock=time.time, keep_days: int = KEEP_DAYS, limit: int = LIMIT):
        self.path = Path(path) if path else None
        self.clock = clock
        self.keep_days = keep_days
        self.limit = limit
        self.items: list[dict] = []          # oldest first: {start, end, seen, server, label, key, message}
        self._load()

    # ---- storage
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [x for x in data.get("alerts", []) if isinstance(x, dict) and "start" in x]
        except (OSError, ValueError, AttributeError):
            self.items = []
        for x in self.items:                 # open when the app closed: it ended some time after it was last seen
            if x.get("end") is None:
                x["end"] = x.get("seen") or x["start"]
                x["end_unknown"] = True

    def save(self) -> None:
        if not self.path:
            return
        cutoff = self.clock() - self.keep_days * 86400
        self.items = [x for x in self.items if (x.get("end") or x.get("seen") or x["start"]) >= cutoff][-self.limit:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"alerts": self.items}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # ---- recording
    def update(self, server: str, label: str, current: dict[str, str]) -> list[dict]:
        """The alerts that are true now for this server ({key: message}). Starts the new ones, ends the ones that
        cleared, and returns the ones that just started."""
        now = self.clock()
        changed = False
        open_ = {x["key"]: x for x in self.items if x["server"] == server and x.get("end") is None}
        started = []
        for key, message in current.items():
            if key in open_:
                open_[key]["seen"] = now
                worst = open_[key].get("worst") or open_[key]["message"]
                if _num(message) > _num(worst):               # "disk 91% full" -> "disk 95% full": keep the worst
                    open_[key]["worst"] = message
                continue
            item = {"start": now, "end": None, "seen": now, "server": server, "label": label, "key": key,
                    "message": message}
            self.items.append(item)
            started.append(item)
            changed = True
        for key, item in open_.items():
            if key not in current:
                item["end"] = now
                changed = True
        if changed:
            self.save()
        return started

    def clear(self, server: str = "") -> None:
        self.items = [x for x in self.items if server and x["server"] != server]
        self.save()

    # ---- reading
    def entries(self, server: str = "") -> list[dict]:
        """Newest first, optionally for one server."""
        return [x for x in reversed(self.items) if not server or x["server"] == server]

    def state(self, item: dict) -> str:
        """"ongoing", "ended" or "last seen" (still open, but this server isn't polled any more)."""
        if item.get("end") is not None:
            return "ended"
        return "ongoing" if self.clock() - item.get("seen", item["start"]) <= STALE_AFTER else "last seen"

    def ongoing(self, server: str) -> list[dict]:
        return [x for x in self.entries(server) if self.state(x) == "ongoing"]


def _num(message: str) -> float:
    import re
    m = re.search(r"(\d+(?:\.\d+)?)", message or "")
    return float(m.group(1)) if m else 0.0


def duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


_shared: AlertLog | None = None


def shared() -> AlertLog:
    """The one log of this app (the main window records, the dashboards read)."""
    global _shared
    if _shared is None:
        _shared = AlertLog(default_path())
    return _shared
