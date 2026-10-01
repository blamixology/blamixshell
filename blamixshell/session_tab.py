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

    # ------------------------------------------------------------ save / restore
    def layout_tree(self, describe) -> dict | None:
        """Nested dict of splits and panes. describe(pane) -> dict or None (skip)."""
        def node(w):
            if isinstance(w, TerminalPane):
                d = describe(w)
                if d is not None and w is self.active:
                    d["active"] = True
                return d
            if isinstance(w, QSplitter):
                kids = [(k, w.sizes()[i]) for i in range(w.count()) if (k := node(w.widget(i))) is not None]
                if not kids:
                    return None
                if len(kids) == 1:
                    return kids[0][0]
                return {"split": "h" if w.orientation() == Qt.Horizontal else "v",
                        "sizes": [sz for _k, sz in kids], "children": [k for k, _sz in kids]}
            return None
        return node(self._root) if self._root else None

    def build(self, tree: dict, make_pane) -> bool:
        """Recreate panes/splits from layout_tree(). make_pane(node) -> TerminalPane | None."""
        active: list[TerminalPane] = []

        def build(n):
            if "children" in n:
                kids = [k for k in (build(c) for c in n["children"]) if k is not None]
                if len(kids) <= 1:
                    return kids[0] if kids else None
                sp = QSplitter(Qt.Horizontal if n.get("split") == "h" else Qt.Vertical)
                sp.setChildrenCollapsible(False)
                sp.setHandleWidth(1)
                for k in kids:
                    sp.addWidget(k)
                sizes = n.get("sizes") or []
                sp.setSizes(sizes if len(sizes) == len(kids) else [1000] * len(kids))
                return sp
            pane = make_pane(n)
            if pane is not None:
                self._wire(pane)
                if n.get("active"):
                    active.append(pane)
            return pane
        root = build(tree)
        if root is None:
            return False
        self._root = root
        self._lay.addWidget(root)
        panes = self.panes()
        self.set_active(active[0] if active else panes[0])
        if len(panes) > 1:   # set_active skips the highlight when it was already current
            for p in panes:
                p.set_active(p is self.active)
        return True

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
                # insertWidget, not replaceWidget: PySide doesn't hand a replaceWidget()'d
                # splitter to Qt, so Python deleted it (and both terminals in it) on return
                idx = parent.indexOf(pane)
                sizes = parent.sizes()
                parent.insertWidget(idx, sp)
                sp.addWidget(pane)          # moves the pane out of the outer splitter
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

    def rotate(self, pane: TerminalPane) -> bool:
        """Side by side <-> stacked, for the split that holds `pane` (keeps every session)."""
        parent = pane.parentWidget()
        if not isinstance(parent, QSplitter):
            return False
        parent.setOrientation(Qt.Vertical if parent.orientation() == Qt.Horizontal else Qt.Horizontal)
        parent.setSizes([1000] * parent.count())
        pane.focus_terminal()
        self.changed.emit(self)
        return True

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
                grand.insertWidget(grand.indexOf(parent), only)
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
