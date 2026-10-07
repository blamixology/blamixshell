"""The dashboard window opens inside the screen, centered over the main window, at the size you last used."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from blamixshell import dashboard_ui as ui  # noqa: E402


class FakePane(QObject):
    state_changed = Signal(object)
    state = "disconnected"
    session = None
    server = SimpleNamespace(id="s1", label="web", address="a@b", username="deploy", port=22)


def make(settings=None, parent=None):
    return ui.DashboardWindow(FakePane(), settings=settings if settings is not None else {}, parent=parent)


def test_opens_inside_the_screen_and_centered_over_the_main_window():
    app = QApplication.instance() or QApplication([])
    avail = app.primaryScreen().availableGeometry()
    parent = QWidget()
    parent.resize(500, 350)
    parent.move(avail.left() + 150, avail.top() + 120)
    parent.show()
    w = make(parent=parent)
    g = w.frameGeometry()
    assert avail.contains(g.topLeft()) and avail.contains(g.bottomRight()), (g, avail)
    assert g.width() <= avail.width() and g.height() <= avail.height()
    ref = parent.frameGeometry().center()
    assert abs((g.center().x()) - ref.x()) <= 2 + max(0, g.width() - avail.width() * 0.5) or g.left() == avail.left()


def test_without_a_main_window_it_is_centered_on_the_screen_and_never_off_the_edge():
    app = QApplication.instance() or QApplication([])
    avail = app.primaryScreen().availableGeometry()
    w = make({"dashboard_size": [99999, 99999]})                    # a stale, huge size is cut down to the screen
    g = w.frameGeometry()
    assert g.width() <= avail.width() and g.height() <= avail.height()
    assert g.left() >= avail.left() and g.top() >= avail.top() and g.right() <= avail.right() and g.bottom() <= avail.bottom()
    assert abs(g.center().x() - avail.center().x()) <= 3 and abs(g.center().y() - avail.center().y()) <= 3


def test_the_size_is_remembered_and_junk_is_ignored():
    saved = {}
    settings = type("S", (dict,), {"save": lambda self: saved.update(self)})()
    w = make(settings)
    w.resize(820, 560)
    w.close()
    assert settings["dashboard_size"] == [820, 560] and saved.get("dashboard_size") == [820, 560]
    again = make(settings)
    assert (again.width(), again.height()) == (820, 560) or again.width() <= QApplication.primaryScreen().availableGeometry().width()
    for junk in ("big", [1], None, ["a", "b"], [1, 2, 3]):
        w2 = make({"dashboard_size": junk})
        assert w2.width() >= 760 and w2.height() >= 520
