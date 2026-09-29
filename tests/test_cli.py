"""CLI + TUI tests. Live parts need SHELLDECK_TEST_SSH=host:port:user:password."""
import asyncio
import os
import sys

import pytest

from shelldeck import cli
from shelldeck.models import Server, Store
from shelldeck.vault import Vault

LIVE = os.environ.get("SHELLDECK_TEST_SSH")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELLDECK_HOME", str(tmp_path))
    st = Store(Vault.create(tmp_path / "vault.sdv", "masterpass", n_log2=12), {})
    st.upsert(Server(name="web-1", host="10.0.0.1", username="deploy", group="Prod/EU", tags=["prod", "web"]))
    st.upsert(Server(name="db-1", host="10.0.0.2", username="deploy", group="Prod/EU", tags=["prod", "db"]))
    st.upsert(Server(name="stage", host="stage.local", group="Staging", favorite=True))
    return st


def test_ls_and_find(store, capsys):
    ns = cli.build_parser().parse_args(["ls", "tag:prod"])
    assert cli.cmd_ls(store, ns) == 0
    out = capsys.readouterr().out
    assert "web-1" in out and "db-1" in out and "stage" not in out
    assert cli.find_server(store, "DB-1").host == "10.0.0.2"
    with pytest.raises(SystemExit):
        cli.find_server(store, "prod")          # ambiguous
    s = cli.adhoc_server("root@1.2.3.4:2200")
    assert (s.username, s.host, s.port) == ("root", "1.2.3.4", 2200)


def test_add_and_rm(store):
    ns = cli.build_parser().parse_args(["add", "--name", "cache", "--host", "ops@10.9.9.9:2201", "--auth", "agent",
                                        "--group", "Prod/US", "--tags", "redis", "--jump", "stage"])
    cli.cmd_add(store, ns)
    s = cli.find_server(store, "cache")
    assert (s.username, s.port, s.group, s.tags) == ("ops", 2201, "Prod/US", ["redis"])
    assert store.servers[s.jump_id].name == "stage"
    cli.cmd_rm(store, cli.build_parser().parse_args(["rm", "cache", "-y"]))
    assert cli.find_server(store, "cache") is None


def test_exec_parser_strips_separator():
    a = cli.build_parser().parse_args(["exec", "tag:prod", "--", "uptime", "-p"])
    assert [x for x in a.command if x != "--"] == ["uptime", "-p"]


def test_tui_renders_and_filters(store):
    from shelldeck.tui import ServerForm, ShellDeckTUI
    from textual.widgets import Tree

    async def go():
        app = ShellDeckTUI(store)
        async with app.run_test(size=(120, 36)) as pilot:
            tree = app.query_one(Tree)
            labels = []

            def walk(n):
                for ch in n.children:
                    labels.append(str(ch.label))
                    walk(ch)
            walk(tree.root)
            assert any("web-1" in l for l in labels) and any("Favorites" in l for l in labels)
            await pilot.press("slash")
            for ch in "db":
                await pilot.press(ch)
            await pilot.pause()
            labels.clear()
            walk(tree.root)
            assert any("db-1" in l for l in labels) and not any("web-1" in l for l in labels)
            await pilot.press("enter")          # jump to first match
            await pilot.pause()
            assert app.selected_server().name == "db-1"
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, ServerForm)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, ServerForm) and form.is_new
            form.query_one("#host").value = "me@new.example:2022"
            form.query_one("#name").value = "newbox"
            form.action_save()
            await pilot.pause()
            assert cli.find_server(store, "newbox").port == 2022
    asyncio.run(go())


@pytest.mark.skipif(not LIVE, reason="no live sshd")
def test_exec_many_live(store, capsys):
    host, port, user, pw = LIVE.split(":")
    a = Server(name="live-a", host=host, port=int(port), username=user, password=pw, group="Lab")
    b = Server(name="live-b", host=host, port=int(port), username=user, password=pw, group="Lab")
    bad = Server(name="dead", host="127.0.0.1", port=1, username="x", password="y", group="Lab")
    for s in (a, b, bad):
        store.upsert(s)
    # unknown host key without --accept-new -> clean error, not a hang
    code = cli.run_many(store, [a], "echo hi", accept_new=False)
    assert code == 2 and "Unknown host key" in capsys.readouterr().out
    code = cli.run_many(store, [a, b, bad], "echo out-$((1+1)); echo err >&2; exit 0", accept_new=True)
    out = capsys.readouterr().out
    assert code == 2                      # 'dead' fails
    assert out.count("out-2") == 2 and "unable to connect" in out.lower()
    assert "2/3 succeeded" in out


@pytest.mark.skipif(not LIVE or sys.platform == "win32", reason="no live sshd")
def test_interactive_connect_via_pty(store, tmp_path):
    import pexpect
    from shelldeck.ssh_core import trust_host_key
    import paramiko
    host, port, user, pw = LIVE.split(":")
    store.upsert(Server(name="live", host=host, port=int(port), username=user, password=pw))
    t = paramiko.Transport((host, int(port)))
    t.start_client()
    trust_host_key(f"[{host}]:{port}", t.get_remote_server_key())
    t.close()
    env = dict(os.environ, SHELLDECK_HOME=str(tmp_path), TERM="xterm-256color")
    child = pexpect.spawn(sys.executable, ["-m", "shelldeck", "connect", "live"], env=env,
                          cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          timeout=20, encoding="utf-8", dimensions=(30, 100))
    child.expect("Master password:")
    child.sendline("masterpass")
    child.expect(r"\$ ")
    child.sendline("stty size; echo MARK_$((40+2))")
    child.expect("30 100")
    child.expect("MARK_42")
    child.setwinsize(40, 120)
    child.sendline("stty size")
    child.expect("40 120")
    child.sendline("exit")
    child.expect("disconnected from live")
    child.expect(pexpect.EOF)
