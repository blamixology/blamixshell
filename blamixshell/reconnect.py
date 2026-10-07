"""Keeping a dashboard alive when the connection drops: knowing why (a reboot we asked for, a shutdown, or
something else), retrying with a growing delay, giving up politely, and saying what is going on.
No Qt here: the desktop dashboard and the terminal dashboard share it."""
from __future__ import annotations

import time

DELAYS = (2, 3, 5, 8, 10, 15, 20, 30)       # seconds between attempts; the last one repeats
PATIENCE = {"lost": 900, "reboot": 900, "shutdown": 0}      # how long to keep trying on our own (seconds)
EXPECT_WINDOW = 180                          # a reboot we asked for is expected to cut the connection within this

_DROP_HINTS = ("socket is closed", "connection reset", "broken pipe", "eof", "timed out", "timeout",
               "ssh session not active", "session is closed", "channel closed", "connection lost", "not connected",
               "no existing session", "transport", "connection aborted", "network is unreachable")


def looks_dropped(message) -> bool:
    """Does this error text mean the connection itself is gone (and not, say, a command that failed)?"""
    text = str(message).lower()
    return any(h in text for h in _DROP_HINTS)


def human(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


class Watch:
    """up -> lost / rebooting / shutdown -> up again. The clock is a parameter so tests need no waiting."""

    def __init__(self, delays=DELAYS, clock=time.monotonic):
        self.delays = tuple(delays) or (5,)
        self.clock = clock
        self.state = "up"                    # up | lost | rebooting | shutdown
        self.since: float | None = None      # when the connection went away
        self.attempts = 0
        self.gave_up = False
        self._expected: tuple[str, float] | None = None

    # ---- what happened
    def expect(self, kind: str) -> None:
        """We asked the server to "reboot" or "shutdown": the connection is about to drop, on purpose."""
        self._expected = (kind, self.clock())

    def lost(self) -> None:
        """The connection is gone (calling it again while down changes nothing)."""
        if self.state != "up":
            return
        now = self.clock()
        kind = "lost"
        if self._expected and now - self._expected[1] <= EXPECT_WINDOW:
            kind = {"reboot": "rebooting", "shutdown": "shutdown"}.get(self._expected[0], "lost")
        self._expected = None
        self.state, self.since, self.attempts, self.gave_up = kind, now, 0, False

    def recovered(self) -> float | None:
        """The connection is back: how long it was away (None if it never was)."""
        if self.state == "up":
            return None
        away = self.clock() - (self.since or self.clock())
        self.state, self.since, self.attempts, self.gave_up = "up", None, 0, False
        return away

    # ---- what to do about it
    def elapsed(self) -> float:
        return 0.0 if self.since is None else self.clock() - self.since

    def patience(self) -> float:
        return PATIENCE["reboot" if self.state == "rebooting" else "shutdown" if self.state == "shutdown" else "lost"]

    def should_retry(self) -> bool:
        """Keep trying on our own? (A shutdown doesn't come back by itself; every wait ends at some point.)"""
        if self.state == "up" or self.gave_up or self.patience() <= 0:
            return False
        if self.elapsed() > self.patience():
            self.gave_up = True
            return False
        return self.patience() > 0

    def next_delay(self) -> float:
        return float(self.delays[min(self.attempts, len(self.delays) - 1)])

    def tried(self) -> None:
        self.attempts += 1

    def manual(self) -> None:
        """The user pressed Reconnect: try again, and start the patience over."""
        self.gave_up = False
        if self.since is not None:
            self.since = self.clock()

    def message(self, retry_in: float | None = None) -> str:
        wait = f" Next try in {int(round(retry_in))} s." if retry_in is not None and retry_in > 0 else ""
        if self.state == "up":
            return ""
        if self.gave_up:
            return (f"Couldn't reconnect after {human(self.elapsed())}. Press Reconnect to try again, "
                    "or check that the server is up.")
        if self.state == "shutdown":
            return "The server is shutting down. It won't come back by itself: press Reconnect once it has been started."
        if self.state == "rebooting":
            return (f"The server is rebooting … waiting for it to come back ({human(self.elapsed())}, attempt "
                    f"{self.attempts + 1}).{wait}")
        return f"Connection lost {human(self.elapsed())} ago. Reconnecting … (attempt {self.attempts + 1}).{wait}"
