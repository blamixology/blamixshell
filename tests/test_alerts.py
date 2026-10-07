"""Alerts history: start and end of each alert per server, failed services from the health poll, the window."""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import alerts as al, dashboard as d  # noqa: E402


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_alerts_start_end_and_survive_a_restart():
    path = Path(tempfile.mkdtemp()) / "alerts.json"
    clock = Clock()
    log = al.AlertLog(path, clock)
    started = log.update("s1", "web", {"disk": "disk 91% full"})
    assert [x["key"] for x in started] == ["disk"] and log.state(started[0]) == "ongoing"
    clock.t += 60
    assert log.update("s1", "web", {"disk": "disk 96% full", "svc:nginx.service": "nginx.service failed"})[0]["key"] \
        == "svc:nginx.service"
    assert log.entries("s1")[1]["worst"] == "disk 96% full"                 # the worst value is kept
    clock.t += 60
    log.update("s1", "web", {"svc:nginx.service": "nginx.service failed"})  # disk cleared
    disk = next(x for x in log.entries() if x["key"] == "disk")
    assert log.state(disk) == "ended" and disk["end"] - disk["start"] == 120
    log.update("s2", "db", {"mem": "memory 93% used"})
    assert [x["label"] for x in log.entries()] == ["db", "web", "web"] and len(log.entries("s1")) == 2
    clock.t += al.STALE_AFTER + 1                                            # nobody polls web any more
    nginx = log.entries("s1")[0]
    assert log.state(nginx) == "last seen" and log.ongoing("s1") == []

    again = al.AlertLog(path, clock)                                       # the app was closed and opened
    assert len(again.entries()) == 3
    assert all(x["end"] is not None for x in again.entries())
    assert next(x for x in again.entries() if x["key"] == "mem").get("end_unknown")
    again.clear("s2")
    assert [x["server"] for x in again.entries()] == ["s1", "s1"]
    again.clear()
    assert al.AlertLog(path, clock).entries() == []


def test_old_alerts_are_dropped():
    path = Path(tempfile.mkdtemp()) / "alerts.json"
    clock = Clock()
    log = al.AlertLog(path, clock)
    log.update("s1", "web", {"load": "load 9"})
    log.update("s1", "web", {})
    clock.t += (al.KEEP_DAYS + 1) * 86400
    log.update("s1", "web", {"disk": "disk 95% full"})
    assert [x["key"] for x in al.AlertLog(path, clock).entries()] == ["disk"]


def test_health_reads_failed_services_for_the_history_only():
    text = ("@@stat\ncpu 1 0 1 10 0 0 0 0\n@@load\n0.1 0.1 0.1 1/100 5\n@@nproc\n2\n@@mem\nMemTotal: 1000 kB\n"
            "MemAvailable: 900 kB\n@@df\nFilesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/sda1 100 50 50 50% /\n@@failed\nnginx.service\nbackup.timer\n@@end\n")
    h = d.parse_health(text)
    assert h.failed == ["nginx.service", "backup.timer"]
    assert d.health_alerts(h) == {}                                          # the low-resource warnings don't change
    assert d.health_alerts(h, services=True) == {"svc:nginx.service": "nginx.service failed",
                                                 "svc:backup.timer": "backup.timer failed"}


def test_the_window_lists_filters_and_clears():
    from unittest import mock
    from PySide6.QtWidgets import QApplication, QMessageBox
    from blamixshell.alerts_ui import AlertsDialog
    QApplication.instance() or QApplication([])
    clock = Clock()
    log = al.AlertLog(Path(tempfile.mkdtemp()) / "a.json", clock)
    log.update("s1", "web", {"disk": "disk 91% full"})
    log.update("s2", "db", {"mem": "memory 93% used"})
    clock.t += 30
    log.update("s2", "db", {})
    dlg = AlertsDialog(None, "s1", log)
    assert dlg.table.rowCount() == 1 and dlg.table.item(0, 1).text() == "web" and dlg.table.item(0, 4).text() == "ongoing"
    dlg.server.setCurrentIndex(0)
    assert dlg.table.rowCount() == 2 and "1 ongoing" in dlg.count.text()
    db = next(i for i in range(2) if dlg.table.item(i, 1).text() == "db")
    assert dlg.table.item(db, 3).text() == "30 s" and dlg.table.item(db, 4).text().startswith("ended")
    with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes):
        dlg._clear()
    assert dlg.table.rowCount() == 0 and dlg.count.text() == "No alerts recorded."
