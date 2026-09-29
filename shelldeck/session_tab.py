"""A tab that holds one or more terminal panes in nested splitters."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

from .terminal import TerminalPane


class SessionTab(QWidget):
    emptied = Signal(object)
    active_pane_changed = Signal(object)
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._root: QWidget | None = None
        self.active: TerminalPane | None = None
        self.broadcast = False

    # ------------------------------------------------------------ panes
    def panes(self) -> list[TerminalPane]:
        return self.findChildren(TerminalPane)

    def _wire(self, pane: TerminalPane) -> None:
        pane.activated.connect(self.set_active)
        pane.user_input.connect(self._route_input)
        pane.state_changed.connect(lambda _p: self.changed.emit(self))

    def add_first(self, pane: TerminalPane) -> None:
        self._wire(pane)
        self._root = pane
        self._lay.addWidget(pane)
        self.set_active(pane)

    def split(self, pane: TerminalPane, new: TerminalPane, orientation=Qt.Horizontal) -> None:
        self._wire(new)
        parent = pane.parentWidget()
        if isinstance(parent, QSplitter) and parent.orientation() == orientation:
            parent.insertWidget(parent.indexOf(pane) + 1, new)
            n = parent.count()
            parent.setSizes([1000] * n)
        else:
            sp = QSplitter(orientation)
            sp.setChildrenCollapsible(False)
            sp.setHandleWidth(1)
            if isinstance(parent, QSplitter):
                idx = parent.indexOf(pane)
                sizes = parent.sizes()
                parent.replaceWidget(idx, sp)
                parent.setSizes(sizes)
            else:
                self._lay.replaceWidget(pane, sp)
                self._root = sp
            sp.addWidget(pane)
            sp.addWidget(new)
            sp.setSizes([1000, 1000])
        self.set_active(new)
        new.focus_terminal()
        self.changed.emit(self)

    def remove(self, pane: TerminalPane) -> None:
        pane.shutdown()
        parent = pane.parentWidget()
        pane.setParent(None)
        pane.deleteLater()
        if isinstance(parent, QSplitter) and parent.count() == 1:
            only = parent.widget(0)
            grand = parent.parentWidget()
            if isinstance(grand, QSplitter):
                sizes = grand.sizes()
                grand.replaceWidget(grand.indexOf(parent), only)
                grand.setSizes(sizes)
            else:
                self._lay.replaceWidget(parent, only)
                self._root = only
            parent.setParent(None)
            parent.deleteLater()
        remaining = [p for p in self.panes() if p is not pane]
        if not remaining:
            self.emptied.emit(self)
            return
        if self.active is pane or self.active not in remaining:
            self.set_active(remaining[0])
            remaining[0].focus_terminal()
        self.changed.emit(self)

    def set_active(self, pane: TerminalPane) -> None:
        if self.active is pane:
            return
        self.active = pane
        for p in self.panes():
            p.set_active(p is pane and len(self.panes()) > 1)
        self.active_pane_changed.emit(pane)

    def _route_input(self, pane: TerminalPane, data: bytes) -> None:
        if self.broadcast:
            for p in self.panes():
                p.send(data)
        else:
            pane.send(data)

    # ------------------------------------------------------------ info
    def title(self) -> str:
        ps = self.panes()
        if not ps:
            return "—"
        first = (self.active or ps[0]).server.label
        return first if len(ps) == 1 else f"{first} +{len(ps) - 1}"

    def state(self) -> str:
        states = {p.state for p in self.panes()}
        for s in ("failed", "disconnected", "connecting", "connected"):
            if s in states:
                return s
        return "idle"

    def shutdown(self) -> None:
        for p in self.panes():
            p.shutdown()
