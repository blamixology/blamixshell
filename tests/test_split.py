"""Split panes: nested splits must keep every terminal alive; rotate flips a split."""
import gc
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QSplitter, QWidget  # noqa: E402
import shiboken6  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakePane(QWidget):
    """Stands in for a TerminalPane (no web engine needed)."""
    activated = Signal(object)
    user_input = Signal(object, bytes)
    state_changed = Signal(object)

    def __init__(self, name):
        super().__init__()
        self.name = name

    def focus_terminal(self):
        pass

    def set_active(self, _on):
        pass

    def shutdown(self):
        pass


def _layout(w):
    if isinstance(w, QSplitter):
        return ("h" if w.orientation() == Qt.Horizontal else "v", [_layout(w.widget(i)) for i in range(w.count())])
    return w.name


def test_nested_splits_keep_their_panes(qapp, monkeypatch):
    from blamixshell import session_tab
    monkeypatch.setattr(session_tab, "TerminalPane", FakePane)
    tab = session_tab.SessionTab()
    a, b, c, d = (FakePane(n) for n in "abcd")
    tab.add_first(a)
    tab.split(a, b, Qt.Horizontal)          # a | b
    tab.split(b, c, Qt.Vertical)            # a | (b / c)   <- this used to delete b and c
    tab.split(c, d, Qt.Horizontal)          # a | (b / (c | d))
    gc.collect()
    qapp.processEvents()
    assert all(shiboken6.isValid(p) for p in (a, b, c, d))
    assert _layout(tab._root) == ("h", ["a", ("v", ["b", ("h", ["c", "d"])])])
    assert tab.rotate(d) and _layout(tab._root) == ("h", ["a", ("v", ["b", ("v", ["c", "d"])])])
    assert tab.rotate(a) and _layout(tab._root)[0] == "v"
    tab.remove(c)
    gc.collect()
    qapp.processEvents()
    assert all(shiboken6.isValid(p) for p in (a, b, d))
    assert _layout(tab._root) == ("v", ["a", ("v", ["b", "d"])])
    single = session_tab.SessionTab()
    e = FakePane("e")
    single.add_first(e)
    assert not single.rotate(e)             # nothing to rotate
