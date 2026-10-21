"""What the dashboard needs from a terminal pane, for the machine a terminal's shell went on to (see relay.py)."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class RelayPane(QObject):
    state_changed = Signal(object)

    def __init__(self, term):
        super().__init__(term)
        self.term = term                       # the terminal whose shell is on that machine
        self.relay = term.relay
        self.server = self.relay.server
        term.state_changed.connect(lambda _p: self.state_changed.emit(self))
        term.hop_changed.connect(lambda _p: self.state_changed.emit(self))

    @property
    def session(self):
        return self.relay

    @property
    def state(self) -> str:
        """Connected while the terminal is connected and its shell is still on that machine."""
        if self.term.state != "connected" or self.term.relay is not self.relay or self.term.relay_state != "ok":
            return "disconnected"
        return "connected"

    def reconnect(self) -> None:
        if self.term.state != "connected":
            self.term.reconnect()              # the way there starts at the first server

    def log_command(self, command: str, source: str = "dashboard", prompt: str = "") -> None:
        self.term.log_command(f"[on {self.relay.chain[-1].label}] {command}", source, prompt)
