"""The desktop dashboard when the connection drops: what it says, how it comes back, and a reboot we asked for."""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from blamixshell import dashboard_ui as ui, reconnect  # noqa: E402


class FakePane(QObject):
    state_changed = Signal(object)
    session = None
    server = SimpleNamespace(id="s1", label="web", address="a@b", username="deploy", port=22)

    def __init__(self):
        super().__init__()
        self.state = "connected"
        self.reconnects = 0

    def reconnect(self):
        self.reconnects += 1
        self.state = "connecting"
        self.state_changed.emit(self)

    def go(self, state):
        self.state = state
        self.state_changed.emit(self)


def make():
    QApplication.instance() or QApplication([])
    pane = FakePane()
    w = ui.DashboardWindow(pane, settings={})
    w.refresh = lambda force=False: None                         # no server behind this window
    return pane, w


def shown(w):
    return not w.banner_row.isHidden()


def test_a_dropped_connection_says_so_and_reconnects_on_request_then_reports_the_downtime():
    pane, w = make()
    assert not shown(w) and w._watch.state == "up"
    pane.go("disconnected")
    assert shown(w) and w._watch.state == "lost" and "Connection lost" in w.banner.text()
    assert not w.btn_reconnect.isHidden() and not w.auto_reconnect.isHidden()
    w._reconnect_clicked()
    assert pane.reconnects == 1 and "Connecting" in w.banner.text()
    pane.go("failed")                                                  # that try failed: still waiting, still offering it
    assert w._watch.state == "lost" and shown(w)
    w._reconnect_clicked()
    assert pane.reconnects == 2
    pane.go("connected")
    assert w._watch.state == "up" and "Back online after" in w.banner.text() and w.btn_reconnect.isHidden()
    assert not w._reconnect_timer.isActive()
    w._hide_ok_banner()
    assert not shown(w)


def test_it_retries_by_itself_when_it_is_time_and_not_when_that_is_switched_off():
    pane, w = make()
    pane.go("disconnected")
    w._retry_at = time.monotonic() - 1
    w._reconnect_tick()
    assert pane.reconnects == 1
    pane.go("failed")
    w.auto_reconnect.setChecked(False)
    w._retry_at = time.monotonic() - 1
    w._reconnect_tick()
    assert pane.reconnects == 1                                         # off: it waits for the button
    assert "Next try" not in w.banner.text()


def test_a_reboot_we_asked_for_is_expected_and_given_time():
    pane, w = make()
    w._pending_drops = "reboot"
    w._on_done("action", None, "Socket is closed")                      # the command's connection died: that is the reboot
    assert "going down as asked" in w.status.text()
    pane.go("disconnected")
    assert w._watch.state == "rebooting" and "rebooting" in w.banner.text()
    pane.go("connected")
    assert w._watch.state == "up" and "Back online" in w.banner.text()


def test_a_shutdown_is_not_retried_by_itself():
    pane, w = make()
    w._pending_action = ("Shut down now", "x", "system", False, 30, "")
    w._pending_drops = "shutdown"
    w._on_done("action", SimpleNamespace(ok=True), "")
    pane.go("disconnected")
    assert w._watch.state == "shutdown" and "won't come back by itself" in w.banner.text()
    w._retry_at = time.monotonic() - 1
    w._reconnect_tick()
    assert pane.reconnects == 0                                         # only the button tries
    w._reconnect_clicked()
    assert pane.reconnects == 1


def test_an_action_without_a_drop_does_not_set_an_expectation():
    pane, w = make()
    w._pending_action = ("Something", "x", "system", False, 30, "")
    w._pending_drops = ""
    w._on_done("action", SimpleNamespace(ok=True), "")
    pane.go("disconnected")
    assert w._watch.state == "lost" and reconnect.looks_dropped("Socket is closed")
