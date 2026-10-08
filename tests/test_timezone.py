"""Changing the server's time zone: the zones it knows (with their offsets), the chooser on the System tab, and the
command (timedatectl, or the /etc/localtime link where there is none)."""
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import collect, system as sysinfo  # noqa: E402

ZONES = """Europe/Bucharest|+0300 EEST
Europe/London|+0100 BST
Asia/Kolkata|+0530 IST
America/St_Johns|-0230 NDT
Asia/Dubai|+0400 +04
UTC|+0000 UTC
US/Eastern|-0400 EDT
Etc/GMT+2|-0200 -02
bad name|+0000 X
../etc/passwd|+0000 X
"""


def test_zones_are_parsed_with_offsets_and_sorted():
    zones = sysinfo.parse_zones(ZONES)
    names = [z.name for z in zones]
    assert names == ["America/St_Johns", "Asia/Dubai", "Asia/Kolkata", "Europe/Bucharest", "Europe/London", "UTC",
                     "Etc/GMT+2", "US/Eastern"]                       # cities, then UTC, then the old aliases
    by = {z.name: z for z in zones}
    assert by["Asia/Kolkata"].label == "Asia/Kolkata   UTC+05:30 IST"
    assert by["America/St_Johns"].offset == "-02:30" and by["Asia/Dubai"].abbr == ""   # "+04" is no name
    assert sysinfo.parse_zones("Europe/Paris\n")[0].label == "Europe/Paris"            # no offset: just the name


def test_the_command_checks_the_zone_and_has_a_fallback():
    cmd = sysinfo.timezone_command("Europe/Bucharest")
    assert "timedatectl set-timezone Europe/Bucharest" in cmd and "ln -sf /usr/share/zoneinfo/Europe/Bucharest" in cmd
    assert "/etc/timezone" in cmd and "Unknown time zone" in cmd
    for bad in ("", "../etc/passwd", "Europe/Bucharest; reboot", "a b"):
        try:
            sysinfo.timezone_command(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_the_system_tab_row_and_action_lead_to_the_chooser():
    act = next(a for a in collect.actions_for("system", None) if a.label.startswith("Set the time zone"))
    assert act.picker == "timezone" and act.prompt                      # the terminal dashboard still asks in text


def test_the_chooser_searches_and_sets_the_zone():
    from PySide6.QtWidgets import QApplication, QDialog
    from blamixshell import dashboard_ui as ui
    from test_dashboard_fw_details import make
    QApplication.instance() or QApplication([])
    zones = sysinfo.parse_zones(ZONES)
    dlg = ui._TimezoneDialog("web", "Europe/Bucharest", "2026-10-08 14:05", zones)
    assert dlg.value() == "Europe/Bucharest" and not dlg.ok.isEnabled()      # starts on the current one
    assert "(now)" in dlg.list.currentItem().text()
    dlg.find.setText("kolk")
    assert dlg.list.count() == 1 and dlg.value() == "Asia/Kolkata" and dlg.ok.isEnabled()
    dlg.find.setText("+05:30")
    assert dlg.value() == "Asia/Kolkata"
    dlg.find.setText("-02:30")
    assert dlg.value() == "America/St_Johns"
    dlg.find.setText("st johns")                                              # spaces for underscores
    assert dlg.value() == "America/St_Johns"
    dlg._pick("UTC")
    assert dlg.value() == "UTC" and dlg.find.text() == ""
    typed = ui._TimezoneDialog("web", "", "", [])                            # no zone list from the server
    typed.find.setText("Europe/Paris")
    assert typed.value() == "Europe/Paris" and typed.ok.isEnabled()
    typed.find.setText("x; reboot")
    assert not typed.ok.isEnabled()

    w, calls = make()
    w._show_system(collect.Table(["", "Value", "Detail"], rows=[["Time zone", "Europe/Bucharest (EEST +0300)", ""],
                                                                ["Server time", "2026-10-08 14:05", ""]],
                                 styles=["", ""], keys=[("tz", "Europe/Bucharest"), None], details=["", ""]))
    assert w._tz_current() == ("Europe/Bucharest", "2026-10-08 14:05")
    jobs = []
    w._job = lambda key, fn: jobs.append(key)
    w._ct["system"].table.selectRow(0)
    w._ct_details("system")                                                  # double-click on the row
    assert jobs == ["tzzones"]                                               # reads the server's zones once
    with mock.patch.object(ui._TimezoneDialog, "exec", lambda self: (self._pick("Asia/Kolkata"), QDialog.Accepted)[1]):
        w._show_tzzones(zones)
        assert calls[-1][0] == "Set the time zone to Asia/Kolkata"
        assert "timedatectl set-timezone Asia/Kolkata" in calls[-1][1]
        w._tz_change()                                                       # next time: no second read
    assert jobs == ["tzzones"] and len(calls) == 2
