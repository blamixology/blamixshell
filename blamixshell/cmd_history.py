"""Per-server command history for terminal autocomplete (opt-in, see Settings).

Kept in <data>/history.json: {server id: [oldest ... newest]}. Only commands typed at a
shell prompt end up here (password prompts are skipped).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .paths import data_dir

MAX_PER_SERVER = 1000
MAX_LEN = 500


def history_path() -> Path:
    return data_dir() / "history.json"


class CommandHistory:
    _lock = threading.Lock()

    def __init__(self, path: Path | None = None):
        self.path = path or history_path()
        self._data: dict[str, list[str]] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): [c for c in v if isinstance(c, str)]
                              for k, v in raw.items() if isinstance(v, list)}
        except (OSError, ValueError):
            pass

    def get(self, server_id: str) -> list[str]:
        return list(self._data.get(server_id, []))

    def add(self, server_id: str, command: str) -> bool:
        """Remember `command` (newest last, no duplicates). False when it was skipped."""
        raw = command.rstrip()
        if not raw.strip() or len(raw) > MAX_LEN:
            return False
        cmd = raw.strip()
        items = self._data.setdefault(server_id, [])
        if cmd in items:
            items.remove(cmd)
        items.append(cmd)
        del items[:-MAX_PER_SERVER]
        self._save()
        return True

    def clear(self, server_id: str | None = None) -> None:
        if server_id is None:
            self._data.clear()
        else:
            self._data.pop(server_id, None)
        self._save()

    def _save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass        # history must never break a session


_shared: CommandHistory | None = None


def shared() -> CommandHistory:
    global _shared
    if _shared is None:
        _shared = CommandHistory()
    return _shared
