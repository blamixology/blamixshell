"""The terminal dashboard: coming back after a dropped connection, a reboot we asked for, firewall actions,
and the details of a security check."""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("textual")

from blamixshell import collect, dashboard as d, firewall as fw, security, system as sy  # noqa: E402
from test_collect import FakeRunner  # noqa: E402
from test_features3 import BAD  # noqa: E402
from test_firewall import NFT  # noqa: E402
from test_system import SYSTEM  # noqa: E402
from test_tui_dash import cells, run_dashboard_test, until  # noqa: E402


def banner_text(screen) -> str:
    return str(screen.query_one("#conn").render())


def test_a_dropped_connection_is_noticed_and_it_reconnects_by_itself():
    state = {"down": False}
    opened = []

    def setup(ctx):
        ctx.retry_delays = (0.1,)
        ctx.alive = lambda: not state["down"]
        ctx.opener = lambda interactive: (opened.append(interactive) or object(), [])

    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        assert not screen.query_one("#conn").display                      # all well: nothing to say
        state["down"] = True
        assert await until(pilot, lambda: screen._watch.state == "lost", tries=120)
        assert "Connection lost" in banner_text(screen) or "Reconnecting" in banner_text(screen)
        state["down"] = False
        with mock.patch.object(d, "Runner", lambda client: FakeRunner()):
            assert await until(pilot, lambda: screen._watch.state == "up", tries=200)
        assert opened and opened[0] is False                              # tried without asking anything first
        assert "Back online" in banner_text(screen)
        assert await until(pilot, lambda: screen.pane("overview").loaded)  # and it reloaded what it shows
    run_dashboard_test(scenario, setup=setup)


def test_a_reboot_we_asked_for_is_expected_and_waited_for():
    state = {"down": False}

    def setup(ctx):
        ctx.retry_delays = (0.1,)
        ctx.alive = lambda: not state["down"]
        ctx.opener = lambda interactive: (object(), [])
        ctx.run = lambda command, allow_plain=False, timeout=60: (_ for _ in ()).throw(EOFError("Socket is closed"))

    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        screen.query_one("TabbedContent").active = "tab-system"
        assert await until(pilot, lambda: screen.pane("system").loaded)
        screen.pane("system").query_one(DataTable).focus()
        await pilot.press("a")
        await pilot.pause()
        assert app.screen.labels[0] == "Reboot now"
        await pilot.press("enter")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen"
        await pilot.click("#yes")
        assert await until(pilot, lambda: screen._watch._expected is not None)   # the connection error is the reboot
        state["down"] = True
        assert await until(pilot, lambda: screen._watch.state == "rebooting", tries=120)
        assert "rebooting" in banner_text(screen)
    run_dashboard_test(scenario, {sy.SYSTEM_SCRIPT: SYSTEM}, setup)


def test_firewall_actions_follow_the_firewall_and_never_offer_the_ssh_rule():
    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        screen.query_one("TabbedContent").active = "tab-firewall"
        assert await until(pilot, lambda: screen.pane("firewall").loaded)
        table = screen.pane("firewall").query_one(DataTable)
        assert "nftables: active" in str(screen.pane("firewall").query_one(".note").render())
        rules = cells(table, 2)
        table.focus()
        table.move_cursor(row=rules.index("tcp dport 22 accept"))
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        labels = list(app.screen.labels)
        assert "Open a port…" in labels and "Save rules" in labels
        assert not any(label.startswith("Remove") for label in labels)       # the rule that keeps this connection open
        await pilot.press("escape")
        await pilot.pause()
        table.move_cursor(row=rules.index("tcp dport { 80, 443 } accept"))
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert any(label.startswith("Remove the rule tcp dport { 80, 443 }") for label in app.screen.labels)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("a", "enter")                                       # "Open a port…": asks for it, then confirms
        await pilot.pause()
        assert type(app.screen).__name__ == "PromptScreen"
        await pilot.press(*"8080/tcp", "enter")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmScreen"
        assert "nft insert rule inet filter input tcp dport 8080 accept" in str(app.screen.message)
        await pilot.click("#no")
        await pilot.pause(0.2)
        assert not any(c.startswith("nft insert") for c in runner.calls)
    run_dashboard_test(scenario, {fw.READ_SCRIPT: NFT})


def test_enter_on_a_security_check_explains_it():
    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        screen.query_one("TabbedContent").active = "tab-security"
        assert await until(pilot, lambda: screen.pane("security").loaded)
        table = screen.pane("security").query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)                                              # the worst one comes first
        await pilot.pause()
        await pilot.press("enter")                                            # no actions on this tab: details instead
        await pilot.pause()
        assert type(app.screen).__name__ == "TextScreen"
        assert "How to fix it" in app.screen.text and "Status: problem" in app.screen.text
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("d")                                                # the same with the d key
        await pilot.pause()
        assert type(app.screen).__name__ == "TextScreen"
    run_dashboard_test(scenario, {security.READ_SCRIPT: BAD})
