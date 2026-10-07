"""Finding, installing and removing packages: parsing each manager's search, the commands, what is protected, and the
Updates tab's buttons."""
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import packages as pk  # noqa: E402

APT = """@@results
htop - interactive processes viewer
aha - ANSI color to HTML converter
bashtop - Resource monitor that shows usage and stats
@@installed
htop|2.2.0-2build1
"""

DNF = """@@results
======================== Name Exactly Matched: htop ========================
htop.x86_64 : Interactive process viewer
======================== Name & Summary Matched: htop ======================
htop.i686 : Interactive process viewer
pcp-pmda-htop.x86_64 : Performance Co-Pilot metrics for htop
@@installed
bash|4.2.46-35.el7_9
htop|2.2.0-3.el7
"""

ZYPPER = """@@results
S  | Name          | Type       | Version     | Arch   | Repository
---+---------------+------------+-------------+--------+-----------
i+ | htop          | package    | 3.2.1-150500 | x86_64 | Main
   | htop          | srcpackage | 3.2.1-150500 | noarch | Source
   | bashtop       | package    | 0.9.25-bp155 | noarch | Backports
"""

PACMAN = """@@results
extra/htop 3.3.0-1 [installed]
    Interactive process viewer
extra/bashtop 0.9.25-3
    Linux resource monitor
"""

APK = """@@results
htop-3.3.0-r0 - Interactive process viewer
htop-doc-3.3.0-r0 - Interactive process viewer (documentation)
@@installed
htop-3.3.0-r0
musl-1.2.5-r0
"""


def test_search_results_per_manager_with_the_exact_name_first():
    apt = pk.parse_search("apt", APT, "htop")
    assert [p.name for p in apt] == ["htop", "aha", "bashtop"]
    assert apt[0].installed and apt[0].version == "2.2.0-2build1" and not apt[1].installed
    dnf = pk.parse_search("dnf", DNF, "htop")
    assert [p.name for p in dnf] == ["htop", "pcp-pmda-htop"] and dnf[0].installed and not dnf[1].installed
    zy = pk.parse_search("zypper", ZYPPER, "htop")
    assert [(p.name, p.installed) for p in zy] == [("htop", True), ("bashtop", False)]
    pac = pk.parse_search("pacman", PACMAN, "htop")
    assert [(p.name, p.installed, p.summary) for p in pac] == [("htop", True, "Interactive process viewer"),
                                                               ("bashtop", False, "Linux resource monitor")]
    apk = pk.parse_search("apk", APK, "htop")
    assert [(p.name, p.installed) for p in apk] == [("htop", True), ("htop-doc", False)]


def test_search_script_checks_the_query():
    assert "apt-cache search -- htop" in pk.search_script("apt", "htop")
    assert "-C search -- htop" in pk.search_script("yum", "htop")
    for bad in ("", "a b", "x;reboot", "$(id)", "-rf"):
        try:
            pk.search_script("apt", bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    try:
        pk.search_script("brew", "htop")
        raise AssertionError("unknown manager")
    except ValueError:
        pass


def test_install_and_remove_commands_and_protection():
    cmd, show = pk.install_command("apt", "htop")
    assert cmd.startswith("sh -c ") and "apt-get install -y" in cmd and show.startswith("apt-get install -y")
    assert "DEBIAN_FRONTEND=noninteractive" in cmd
    assert pk.install_command("dnf", "htop")[1] == "dnf -y install htop"
    assert pk.remove_command("apk", "htop")[1] == "apk del htop"
    for name in ("openssh-server", "sudo", "systemd", "glibc.x86_64", "libc6:amd64", "linux-image-6.1.0-13-amd64",
                 "kernel-3.10.0", "dpkg", "rpm"):
        assert pk.protected(name), name
        try:
            pk.remove_command("apt", name)
            raise AssertionError(name)
        except ValueError as e:
            assert "can't be removed" in str(e)
    assert not pk.protected("htop") and not pk.protected("nginx")
    for bad in ("a b", "x;y", "$(id)"):
        try:
            pk.install_command("apt", bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_removal_preview_lists_what_else_goes():
    assert "apt-get -s remove" in pk.removal_preview_command("apt", "htop")
    assert pk.removal_preview_command("dnf", "htop") is None                # rpm managers: just the confirmation
    assert pk.parse_preview("ubuntu-server\nhtop\n", "htop") == ["ubuntu-server"]
    assert pk.parse_preview("", "htop") == []


def test_updates_tab_search_install_remove():
    from test_dashboard_fw_details import make
    w, calls = make()
    w.settings = {"dashboard_install_updates": False}
    w._show_updates(("apt", []))
    w._show_packages(("htop", pk.parse_search("apt", APT, "htop")))
    assert w.pkg_table.rowCount() == 3 and "3 found, 1 installed" in w.pkg_count.text()
    assert w.pkg_install.isHidden() and w.pkg_remove.isHidden()                  # off in Settings
    w.settings = {"dashboard_install_updates": True}
    names = [w.pkg_table.item(i, 0).text() for i in range(3)]
    w.pkg_table.selectRow(names.index("aha"))
    assert w.pkg_install.isEnabled() and not w.pkg_remove.isEnabled() and not w.pkg_install.isHidden()
    w._pkg_change("install")
    assert calls[-1][0] == "Install aha" and "apt-get install -y" in calls[-1][1]
    w.pkg_table.selectRow(names.index("htop"))
    assert w.pkg_remove.isEnabled() and not w.pkg_install.isEnabled()
    jobs = []
    w._job = lambda key, fn: jobs.append(key)
    w._pkg_change("remove")
    assert jobs == ["pkg_preview"]                                                # asks the server what else goes
    from PySide6.QtWidgets import QMessageBox
    with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.No):
        w._show_pkg_preview(["ubuntu-server"])
    assert calls[-1][0] == "Install aha"                                          # declined: nothing ran
    with mock.patch.object(QMessageBox, "warning", lambda *a, **k: None):
        w._show_pkg_preview(["openssh-server"])                                   # would take SSH with it
    assert calls[-1][0] == "Install aha"
    with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes):
        w._show_pkg_preview(["ubuntu-server"])
    assert calls[-1][0] == "Remove htop" and "apt-get remove -y" in calls[-1][1]
    w._show_packages(("ssh", [pk.Package("openssh-server", installed=True)]))
    w.pkg_table.selectRow(0)
    assert not w.pkg_remove.isEnabled() and "can't be removed" in w.pkg_remove.toolTip()
