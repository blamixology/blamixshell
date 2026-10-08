"""The terminal dashboard's Docker tabs: Compose projects (actions, logs, editing a compose file) and the
Images / volumes / networks list; read-only views show readable output."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("textual")

from blamixshell import collect, docker  # noqa: E402
from test_compose import PS as COMPOSE_PS  # noqa: E402
from test_docker_tools import FULL, INSPECT  # noqa: E402
from test_tui_dash import run_dashboard_test, until  # noqa: E402


def compose_project_key():
    ctx = collect.Context(None, "me")
    ctx.read = lambda script, needs_root=None, timeout=30: COMPOSE_PS
    return collect.load_compose(ctx).keys[0]


def test_actions_for_compose_and_images_rows():
    key = compose_project_key()
    labels = [a.label for a in collect.actions_for("compose", key)]
    assert labels[0] == "Up shop: start it, creating what is missing" and "Edit the compose files of shop…" in labels
    edit = next(a for a in collect.actions_for("compose", key) if a.picker == "compose-edit")
    assert edit.target == key
    down = next(a for a in collect.actions_for("compose", key) if a.label.startswith("Down"))
    assert down.danger and "down 2>&1" in down.command
    svc = [a.label for a in collect.actions_for("compose", key[:5] + ("worker",))]
    assert svc == ["Restart shop / worker", "Re-create shop / worker", "Log of shop / worker"]
    assert collect.actions_for("compose", key[:1] + ("",) + key[2:]) == []           # no compose tool: nothing to run
    ctx = collect.Context(None, "me")
    ctx.read = lambda script, needs_root=None, timeout=30: FULL
    t = collect.load_images(ctx)
    used = next(k for k in t.keys if k[0] == "image" and k[2] == "nginx:1.25")
    acts = collect.actions_for("images", used, ctx)
    assert not any(a.label.startswith("Remove the image") for a in acts)              # in use by "web"
    details = acts[0]
    assert details.readonly and details.root_if_refused and details.shape is docker.summarize_inspect
    bridge = next(k for k in t.keys if k[0] == "network" and k[2] == "bridge")
    assert not any(a.label.startswith("Remove") for a in collect.actions_for("images", bridge, ctx))
    assert any(a.label.startswith("Clean up") for a in collect.actions_for("docker", None, ctx))   # engine-wide


def test_compose_tab_runs_actions_and_edits_a_file():
    files = ("@@/srv/shop/docker-compose.yml\nservices:\n  web:\n    image: nginx\n"
             "@@/srv/shop/docker-compose.prod.yml\nservices: {}\n")

    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        screen.query_one("TabbedContent").active = "tab-compose"
        assert await until(pilot, lambda: screen.pane("compose").loaded)
        table = screen.pane("compose").query_one(DataTable)
        table.focus()
        table.move_cursor(row=0)                                               # the "shop" project (running first)
        await pilot.press("a")
        assert type(app.screen).__name__ == "ChoiceScreen"
        await pilot.press("enter")                                             # "Up shop"
        assert type(app.screen).__name__ == "ConfirmScreen"
        await pilot.press("escape")
        await pilot.pause()
        runner.replies[collect.compose.read_files_command(collect.compose_project(table_key(screen, 0)))] = files
        screen.edit_compose(table_key(screen, 0))
        assert await until(pilot, lambda: type(app.screen).__name__ == "ChoiceScreen")   # two files: which one?
        await pilot.press("down", "enter")                                     # docker-compose.prod.yml
        assert await until(pilot, lambda: type(app.screen).__name__ == "EditScreen")
        app.screen.query_one("TextArea").text = "services:\n  web:\n    restart: always\n"
        await pilot.press("ctrl+s")
        assert await until(pilot, lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.click("#yes")                                              # Save
        assert await until(pilot, lambda: any("blamixshell-new" in c and "config -q" in c for c in runner.calls))
        cmd = next(c for c in runner.calls if "blamixshell-new" in c)
        assert cmd.startswith("sh -c ") and "docker-compose.prod.yml.bak-" in cmd and "up -d" in cmd

    def table_key(screen, row):
        return screen.pane("compose").selected_key()

    run_dashboard_test(scenario, replies={docker.READ_SCRIPT: COMPOSE_PS})


def test_read_only_views_are_shaped_and_images_tab_loads():
    async def scenario(app, pilot, runner, DataTable):
        screen = app.screen
        screen.query_one("TabbedContent").active = "tab-images"
        assert await until(pilot, lambda: screen.pane("images").loaded)
        rows = screen.pane("images").data.rows
        assert [r[0] for r in rows].count("image") == 4 and ["volume", "pgdata"] == rows[4][:2]
        act = collect.Action("Details of worker", "docker container inspect worker 2>&1", readonly=True,
                             root_if_refused=True, shape=docker.summarize_inspect)
        runner.replies[act.command] = INSPECT
        screen.show_output(act, act.command)
        assert await until(pilot, lambda: type(app.screen).__name__ == "TextScreen")
        shown = str(app.screen.text)
        assert "OOM-killed" in shown and "hunter2" not in shown

    run_dashboard_test(scenario, replies={docker.READ_SCRIPT: FULL})
