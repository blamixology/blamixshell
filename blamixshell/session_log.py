"""Command log (who ran what, where) and full session recordings. Qt-free.

Command log  one line per command typed in a terminal (or run by the dashboard / exec):
             logs/commands/2026-10-01.log, tab-separated so it greps and imports cleanly:
             time, local user, login@host, server name, source, prompt, command
Recording    everything a terminal shows, while you record (or always, per server):
             logs/sessions/<server>/2026-10-01_14-03-22.log
             "text": colors/cursor codes stripped (default) - "raw": as received, replay with
             `cat` or `less -R`. Optional time stamp at the start of every line.

Logs are plain files: they hold whatever the commands/terminal showed. Typed passwords
aren't echoed by servers, so they don't end up here.
"""
from __future__ import annotations

import codecs
import getpass
import os
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from .paths import data_dir

COMMAND_HEADER = "# time\tlocal user\tlogin\tserver\tsource\tprompt\tcommand\n"


def log_root(settings: dict | None = None) -> Path:
    folder = (settings or {}).get("log_dir") or ""
    return Path(folder).expanduser() if folder else data_dir() / "logs"


def local_user() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    return f"{user}@{socket.gethostname()}"


def _safe(name: str) -> str:
    return re.sub(r"[^\w.@-]+", "_", name).strip("_")[:80] or "server"


def _field(text: str) -> str:
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ⏎ ")


# ---------------------------------------------------------------- command log
_PROMPT = re.compile(r"^(.{0,240}?(?:[$#%>❯➜»λ]|\]\$|\]#))\s(.*)$")
_PASSWORD = re.compile(r"(password|passphrase|passcode|verification code|pin)[^:]*:\s*$", re.I)


def split_prompt(line: str) -> tuple[str, str]:
    """'deploy@web-1:/srv$ git pull' -> ('deploy@web-1:/srv$', 'git pull').
    The prompt ends at the first $ # % > ❯ followed by a space; no prompt -> ('', line)."""
    line = line.rstrip()
    m = _PROMPT.match(line)
    if not m:
        if line and line[-1] in "$#%>❯➜»λ":     # a bare prompt: Enter on an empty line
            return line, ""
        return "", line.strip()
    return m.group(1).strip(), m.group(2).strip()


def command_from_line(line: str) -> tuple[str, str] | None:
    """(prompt, command) for the terminal line the user pressed Enter on, or None when
    there's nothing worth logging (empty line, a password prompt)."""
    if _PASSWORD.search(line):
        return None
    prompt, cmd = split_prompt(line)
    if not cmd:
        return None
    return prompt, cmd


class CommandLog:
    """Appends to logs/commands/<date>.log (one file a day). Thread-safe."""

    _lock = threading.Lock()

    def __init__(self, root: Path):
        self.dir = Path(root) / "commands"

    def path_for(self, when: float | None = None) -> Path:
        return self.dir / f"{datetime.fromtimestamp(when or time.time()):%Y-%m-%d}.log"

    def add(self, server, command: str, source: str = "terminal", prompt: str = "",
            when: float | None = None) -> None:
        when = when or time.time()
        login = f"{server.username + '@' if server.username else ''}{server.host}"
        line = "\t".join([datetime.fromtimestamp(when).astimezone().isoformat(timespec="seconds"),
                          _field(local_user()), _field(login), _field(server.label), source,
                          _field(prompt), _field(command)]) + "\n"
        path = self.path_for(when)
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                new = not path.exists()
                with open(path, "a", encoding="utf-8") as f:
                    if new:
                        f.write(COMMAND_HEADER)
                    f.write(line)
            except OSError:
                pass        # logging must never break a session


# ---------------------------------------------------------------- recordings
# CSI (colors, cursor moves), OSC (titles, links), and single-char escapes
_ESC_SEQ = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[PX^_][^\x1b]*\x1b\\"
                      r"|\x1b[()][0-9A-Za-z]|\x1b[@-Z\\-_=>78]")
_PARTIAL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*|[PX^_][^\x1b]*|[()])?$")


class TextCleaner:
    """Turns a terminal byte stream into plain lines: drops escape codes (also when split
    across chunks), applies backspace, and keeps the last overwrite of a \\r line
    (progress bars). Feed bytes, get finished lines."""

    def __init__(self):
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self._line: list[str] = []
        self._cr = False

    def feed(self, data: bytes) -> list[str]:
        text = self._pending + self._dec.decode(data)
        m = _PARTIAL.search(text)
        if m and m.group(0):
            self._pending, text = m.group(0), text[:m.start()]
        else:
            self._pending = ""
        text = _ESC_SEQ.sub("", text)
        out = []
        for ch in text:
            if ch == "\n":
                out.append("".join(self._line))
                self._line, self._cr = [], False
            elif ch == "\r":
                self._cr = True
            else:
                if self._cr:            # \r not followed by \n: the line is being redrawn
                    self._line, self._cr = [], False
                if ch == "\b":
                    if self._line:
                        self._line.pop()
                elif ch == "\t" or ch >= " ":
                    self._line.append(ch)
        return out

    def flush(self) -> list[str]:
        rest = "".join(self._line)
        self._line = []
        return [rest] if rest.strip() else []


class Recorder:
    """Writes one terminal session to a file (see module docs)."""

    def __init__(self, root: Path, server, fmt: str = "text", timestamps: bool = False):
        self.fmt = "raw" if fmt == "raw" else "text"
        self.timestamps = timestamps
        self.started = time.time()
        folder = Path(root) / "sessions" / _safe(server.label)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(self.started).strftime("%Y-%m-%d_%H-%M-%S")
        self.path = folder / f"{stamp}.log"
        n = 2
        while self.path.exists():
            self.path = folder / f"{stamp}-{n}.log"
            n += 1
        self._f = open(self.path, "ab")
        self._clean = TextCleaner() if self.fmt == "text" else None
        self._at_line_start = True
        self._lock = threading.Lock()
        login = f"{server.username + '@' if server.username else ''}{server.host}"
        self._write_text(f"# BlamixShell recording · {server.label} ({login}) · by {local_user()} · "
                         f"started {datetime.fromtimestamp(self.started).astimezone().isoformat(timespec='seconds')}\n")
        self.bytes = 0

    def _stamp(self) -> str:
        return datetime.now().strftime("[%H:%M:%S] ")

    def _write_text(self, s: str) -> None:
        self._f.write(s.encode("utf-8"))

    def write(self, data: bytes) -> None:
        with self._lock:
            if self._f.closed:
                return
            if self._clean is not None:
                for line in self._clean.feed(data):
                    self._write_text((self._stamp() if self.timestamps else "") + line + "\n")
            elif self.timestamps:
                parts = data.split(b"\n")
                for i, part in enumerate(parts):
                    if self._at_line_start and (part or i < len(parts) - 1):
                        self._f.write(self._stamp().encode())
                        self._at_line_start = False
                    self._f.write(part)
                    if i < len(parts) - 1:
                        self._f.write(b"\n")
                        self._at_line_start = True
            else:
                self._f.write(data)
            self.bytes += len(data)
            self._f.flush()

    def close(self) -> Path:
        with self._lock:
            if not self._f.closed:
                if self._clean is not None:
                    for line in self._clean.flush():
                        self._write_text((self._stamp() if self.timestamps else "") + line + "\n")
                end = datetime.now().astimezone().isoformat(timespec="seconds")
                self._write_text(f"\n# recording ended {end}\n")
                self._f.close()
        return self.path


# ---------------------------------------------------------------- housekeeping
def prune(root: Path, days: int) -> int:
    """Delete log files older than `days` (0 = keep everything). Returns how many."""
    if days <= 0 or not Path(root).exists():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for sub in ("commands", "sessions"):
        for p in (Path(root) / sub).rglob("*.log"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    return n
