"""The terminal dashboard: tabs load, tables filter and sort, actions ask first and then run."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("textual")

from blamixshell import collect, dashboard as d  # noqa: E402
from blamixshell.tui_dash import DashApp  # noqa: E402
from test_collect import FakeRunner  # noqa: E402

SERVER = SimpleNamespace(label="web-1", address="deploy@web-1:22")
SVCS = [d.Service("sshd.service", "loaded", "active", "running", "OpenSSH server", "enabled", "systemd"),
        d.Service("nginx.service", "loaded", "active", "running", "Web server", "enabled", "systemd"),
        d.Service("backup.service", "loaded", "failed", "failed", "Nightly backup", "enabled", "systemd")]
PROCS = [d.Process(10, "root", 0.5, 1.0, 900, "2-03:04:05", "/usr/sbin/sshd -D"),
         d.Process(2, "deploy", 55.0, 12.0, 1_200_000, "00:30", "node server.js"),
         d.Process(100, "root", 3.0, 0.1, 12_800, "1-00:00:00", "nginx: master")]


def cells(table, col=0):
    return [table.get_row_at(i)[col].plain for i in range(table.row_count)]


async def until(pilot, cond, tries=100):
    for _ in range(tries):
        await pilot.pause(0.05)
        if cond():
            return True
    return False


def run_dashboard_test(scenario, replies=None):
    from textual.widgets import DataTable
    runner = FakeRunner(replies)
    ctx = collect.Context(runner, username="deploy", root=True)           # root: no sudo prompts in this test
    ov = d.Overview(host="web-1", os="CentOS 7", kernel="3.10", cpus=2, cpu_percent=10, load=(0.1, 0.1, 0.1),
                    mem_total_kb=1000, mem_avail_kb=500)

    async def main():
        app = DashApp(SERVER, ctx)
        async with app.run_test(size=(150, 45)) as pilot:
            await until(pilot, lambda: hasattr(app.screen, "pane") and app.screen.pane("overview").loaded)
            await scenario(app, pilot, runner, DataTable)
    with mock.patch.object(d, "overview", lambda r: ov), mock.patch.object(d, "services", lambda r: (SVCS, "")), \
            mock.patch.object(d, "processes", lambda r, sort, limit=0: PROCS):
        asyncio.run(main())


def test_tabs_load_filter_and_sort():
    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        assert "web-1" in screen.title and screen.pane("overview").data.rows[0][0] == "CPU"
        screen.query_one("TabbedContent").active = "tab-services"
        assert await until(pilot, lambda: screen.pane("services").loaded)
        pane = screen.pane("services")
        table = pane.query_one(DataTable)
        assert cells(table) == ["backup.service", "nginx.service", "sshd.service"]        # failed first
        pane.query_one("Input").value = "nginx"
        await pilot.pause()
        assert cells(table) == ["nginx.service"] and "1 of 3" in str(pane.query_one(".note").render())
        pane.query_one("Input").value = ""
        await pilot.pause()
        pane.sort_by(0)
        await pilot.pause()
        assert cells(table) == ["backup.service", "nginx.service", "sshd.service"]
        pane.sort_by(0)                                                                    # again: reversed
        await pilot.pause()
        assert cells(table) == ["sshd.service", "nginx.service", "backup.service"]
        screen.query_one("TabbedContent").active = "tab-processes"
        assert await until(pilot, lambda: screen.pane("processes").loaded)
        proc = screen.pane("processes")
        proc.sort_by(0)                                                                    # PID: by value
        await pilot.pause()
        assert cells(proc.query_one(DataTable)) == ["2", "10", "100"]
        proc.sort_by(4)                                                                    # Memory: sizes
        await pilot.pause()
        assert cells(proc.query_one(DataTable), 4) == ["900 KB", "12.5 MB", "1.1 GB"]
        proc.sort_by(5)
        await pilot.pause()
        assert cells(proc.query_one(DataTable), 5) == ["00:30", "1-00:00:00", "2-03:04:05"]
    run_dashboard_test(scenario)


def test_actions_ask_first_and_then_run():
    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        screen.query_one("TabbedContent").active = "tab-services"
        assert await until(pilot, lambda: screen.pane("services").loaded)
        table = screen.pane("services").query_one(DataTable)
        table.focus()
        table.move_cursor(row=1)                                                           # nginx.service
        await pilot.pause()
        await pilot.press("a")                                                             # the choices for the row
        await pilot.pause()
        assert type(app.screen).__name__ == "ChoiceScreen"
        labels = list(app.screen.labels)
        assert any(label.startswith("Restart nginx.service") for label in labels)
        await pilot.press("down", "down", "enter") if labels[0].startswith("Start") else await pilot.press("enter")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen"                                # asks before it does anything
        assert not any(c.startswith("systemctl") for c in runner.calls)
        await pilot.click("#yes")
        assert await until(pilot, lambda: any("systemctl" in c for c in runner.calls))
        assert any("nginx.service" in c for c in runner.calls if "systemctl" in c)
    run_dashboard_test(scenario)


def test_nothing_runs_when_the_confirmation_is_declined():
    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        screen.query_one("TabbedContent").active = "tab-processes"
        assert await until(pilot, lambda: screen.pane("processes").loaded)
        table = screen.pane("processes").query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter")                                                         # "End process"
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen"
        await pilot.click("#no")
        await pilot.pause(0.3)
        assert not any(c.startswith("kill") for c in runner.calls)
    run_dashboard_test(scenario)


def test_system_actions_ask_for_a_value_then_confirm_and_checks_just_show_output():
    import test_system as ts
    from blamixshell import system as sy
    replies = {sy.SYSTEM_SCRIPT: ts.SYSTEM, sy.NETWORK_SCRIPT: ts.NETWORK, sy.MOUNTS_SCRIPT: ts.MOUNTS}

    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        screen.query_one("TabbedContent").active = "tab-system"
        assert await until(pilot, lambda: screen.pane("system").loaded)
        table = screen.pane("system").query_one(DataTable)
        assert "Reboot required" in cells(table) and "Europe/Bucharest" in " ".join(cells(table, 1))
        table.focus()
        await pilot.press("a")
        await pilot.pause()
        assert type(app.screen).__name__ == "ChoiceScreen"
        assert app.screen.labels[0] == "Reboot now" and app.screen.labels[1].startswith("Reboot in a while")
        await pilot.press("down", "enter")                                     # "Reboot in a while…" asks for minutes
        await pilot.pause()
        assert type(app.screen).__name__ == "PromptScreen"
        await pilot.press("x", "enter")                                        # not a number: a message, nothing runs
        await pilot.pause()
        assert type(app.screen).__name__ == "DashboardScreen"
        await pilot.press("a", "down", "enter")
        await pilot.pause()
        await pilot.press("7", "enter")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen"                    # the value is turned into a command, asked
        assert "shutdown -r +7" in str(app.screen.message)
        await pilot.click("#no")
        await pilot.pause(0.2)
        assert not any(c.startswith("shutdown") for c in runner.calls)
        # a read-only check runs at once and shows what it found
        screen.query_one("TabbedContent").active = "tab-network"
        assert await until(pilot, lambda: screen.pane("network").loaded)
        nt = screen.pane("network").query_one(DataTable)
        nt.focus()
        await pilot.press("a", "down", "enter")                                # "Check the internet"
        assert await until(pilot, lambda: type(app.screen).__name__ == "TextScreen")
        assert any("1.1.1.1" in c and "ping" in c for c in runner.calls)
    run_dashboard_test(scenario, replies)
