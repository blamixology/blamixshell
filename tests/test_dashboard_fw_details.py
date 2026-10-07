"""Desktop dashboard: the firewall buttons (enabled when they can work, with the reason when not) and the
details window that opens on a double-click."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from blamixshell import collect, dashboard as d, dashboard_ui as ui, firewall as fw, security  # noqa: E402
from test_firewall import FIREWALLD, IPTABLES, NFT, UFW  # noqa: E402
from test_security2 import RICH  # noqa: E402


class FakePane(QObject):
    state_changed = Signal(object)
    state = "connected"
    session = None
    server = SimpleNamespace(id="s1", label="web", address="a@b", username="deploy", port=22)


def make():
    QApplication.instance() or QApplication([])
    w = ui.DashboardWindow(FakePane(), settings={})
    w.refresh = lambda force=False: None
    calls = []
    w._privileged = lambda what, cmd, then, **k: calls.append((what, cmd))
    return w, calls


def col(table, c):
    return [table.item(i, c).text() for i in range(table.rowCount())]


def test_iptables_and_nftables_buttons_work_and_say_what_save_means():
    w, calls = make()
    w._show_firewall(IPTABLES)
    b = w.fw_buttons
    assert b["add"].isEnabled() and "Save rules" in b["reload"].text() and b["reload"].isEnabled()
    t = w.fw_table
    t.selectRow(0)                                                       # a default policy: nothing to remove
    assert not b["remove"].isEnabled() and "can't be removed" in b["remove"].toolTip()
    t.selectRow(2)                                                       # the rule that allows SSH
    assert not b["remove"].isEnabled() and "keeps your SSH connection open" in b["remove"].toolTip()
    assert "survive a reboot" in w.fw_hint.text()
    w._show_firewall(NFT)
    t = w.fw_table
    rules = col(t, 2)
    t.selectRow(next(i for i, r in enumerate(rules) if "80, 443" in r))
    assert b["remove"].isEnabled()
    w._fw_remove()
    assert calls[-1][1] == "nft delete rule inet filter input handle 7"
    w._fw_reload()
    assert "nft list ruleset" in calls[-1][1] and calls[-1][0] == "Save rules"
    ui._FirewallRuleDialog.exec = lambda self: QDialog.Accepted
    ui._FirewallRuleDialog.values = lambda self: ("port", "8080", "tcp", "")
    w._fw_add()
    assert calls[-1][1] == "nft insert rule inet filter input tcp dport 8080 accept"


def test_a_firewall_that_is_not_running_can_be_started_and_the_reason_is_shown():
    w, calls = make()
    w._show_firewall("@@manager\nfirewalld\n@@state\nnot running\n@@default\npublic\n@@zones\n")
    b = w.fw_buttons
    assert not b["add"].isEnabled() and "isn't running" in b["add"].toolTip() and "isn't running" in w.fw_hint.text()
    assert not b["start"].isHidden()
    w._fw_start()
    assert calls[-1] == ("Start firewall", "systemctl enable --now firewalld")
    w._show_firewall(UFW)
    assert b["start"].isHidden() and b["add"].isEnabled()
    w.server = SimpleNamespace(port=64990)
    w._show_firewall("@@manager\nufw\n@@state\nStatus: inactive\n@@rules\n")
    w._fw_start()
    assert "ufw allow 64990/tcp" in calls[-1][1]                         # SSH is allowed before it is switched on
    w._show_firewall("@@manager\nufw\n@@state\nERROR: You need to be root\n@@rules\n")
    assert not b["add"].isEnabled() and "needs root" in b["add"].toolTip() and b["start"].isHidden()
    w._show_firewall("@@manager\nnone\n")
    assert not b["add"].isEnabled() and "No firewall tool" in b["add"].toolTip()


def test_double_click_opens_the_details_of_a_security_check_and_of_other_rows():
    w, _ = make()
    seen = []
    ui._ViewDialog.exec = lambda self: seen.append((self.windowTitle(), self.findChild(ui.QPlainTextEdit).toPlainText()))
    w._show_security(RICH)
    w.sec_table.selectRow(0)
    w._sec_details()
    title, text = seen[-1]
    assert title == w._sec_findings[0].title and "How to fix it" in text and "Why it matters" in text
    w.sec_table.selectRow(next(i for i, f in enumerate(w._sec_findings) if f.title == "Reachable from outside"))
    w._sec_details()
    assert "redis-server" in seen[-1][1]
    procs = [d.Process(7, "root", 55.0, 1.0, 2048, "01:00", "/usr/sbin/sshd -D")]
    w._show_processes(procs)
    w.ps_table.selectRow(0)
    w._row_details(w.ps_table, "Process")
    assert seen[-1][0] == "Process" and "PID: 7" in seen[-1][1] and "Command: /usr/sbin/sshd -D" in seen[-1][1]
    w._show_firewall(FIREWALLD)
    w.fw_table.selectRow(0)
    w._row_details(w.fw_table, "Firewall rule")
    assert "Type:" in seen[-1][1]
    # a collect-backed tab: the row's own explanation, found through the original row even when the view is sorted
    import test_system as ts
    from test_collect import FakeRunner
    from blamixshell import system as sy
    ctx = collect.Context(FakeRunner({sy.MOUNTS_SCRIPT: ts.MOUNTS}))
    w._ct_show("mounts", collect.load_mounts(ctx))
    st = w._ct["mounts"]
    st.table.sortByColumn(1, Qt.DescendingOrder)
    st.table.selectRow(0)
    w._ct_details("mounts")
    shown = [r for r in col(st.table, 1)]
    assert shown == sorted(shown, reverse=True) and f"Mounted on: {shown[0]}" in seen[-1][1]
