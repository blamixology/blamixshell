"""`blamixshell gui --connect`: reading the command line, finding the saved server, and handing the request to a
BlamixShell that is already open."""
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import handoff  # noqa: E402
from blamixshell.models import Server  # noqa: E402


def store_with(*servers):
    return SimpleNamespace(servers={s.id: s for s in servers})


WEB = Server(name="web-1", host="10.0.0.5", port=22, username="deploy", last_connected=5)
WEB_ROOT = Server(name="web-1 root", host="10.0.0.5", port=22, username="root", last_connected=9)
BASTION = Server(name="bastion", host="bastion.example.com", username="ops")


def test_command_line():
    assert handoff.parse_args([]) is None and handoff.parse_args(["-platform", "offscreen"]) is None
    assert handoff.parse_args(["--connect", " web-1 ", "--key", "~/.ssh/id", "-style", "fusion"]) == \
        {"action": "connect", "target": "web-1", "key": "~/.ssh/id", "jump": ""}
    assert handoff.parse_args(["gui", "-c", "bob@h:2222", "--jump", "bastion"])["jump"] == "bastion"


def test_targets():
    assert handoff.split_target("bob@host:2222") == ("bob", "host", 2222)
    assert handoff.split_target("host") == ("", "host", None)
    assert handoff.split_target("ssh://bob@host:22/") == ("bob", "host", 22)
    assert handoff.split_target("me@[fe80::1]:2200") == ("me", "fe80::1", 2200)
    assert handoff.split_target("fe80::1") == ("", "fe80::1", None)


def test_saved_servers_are_reused():
    st = store_with(WEB, WEB_ROOT, BASTION)
    assert handoff.resolve(st, handoff.request("web-1")) == ("open", WEB.id, "")            # by name
    assert handoff.resolve(st, handoff.request("WEB-1 ROOT"))[1] == WEB_ROOT.id
    assert handoff.resolve(st, handoff.request("deploy@10.0.0.5"))[1] == WEB.id            # by address
    kind, sid, note = handoff.resolve(st, handoff.request("10.0.0.5"))
    assert kind == "open" and sid == WEB_ROOT.id and "used last" in note                     # two match
    kind, sid, note = handoff.resolve(st, handoff.request("deploy@10.0.0.5:22", key="/k/other"))
    assert kind == "open" and sid == WEB.id and "--key was not used" in note                 # no duplicate entry


def test_new_addresses_prefill_the_new_server_dialog():
    st = store_with(WEB, BASTION)
    kind, s, note = handoff.resolve(st, handoff.request("bob@db.internal:2222", key="/k/id", jump="bastion"))
    assert kind == "new" and (s.host, s.port, s.username, s.auth, s.key_path, s.jump_id) == \
        ("db.internal", 2222, "bob", "key", "/k/id", BASTION.id) and note == ""
    kind, s, note = handoff.resolve(st, handoff.request("db2", jump="nowhere"))
    assert kind == "new" and s.username == "" and s.auth == "password" and "isn't a saved server" in note
    assert handoff.resolve(st, handoff.request("not a host"))[0] == "error"
    assert handoff.resolve(st, handoff.request(""))[0] == "error"


def test_a_running_window_takes_the_request():
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    home = tempfile.mkdtemp()
    with mock.patch.dict(os.environ, {"BLAMIXSHELL_HOME": home}):
        got = []
        server = handoff.listen(got.append)
        assert server is not None
        assert handoff.listen(lambda r: None) is None          # a second launch never takes the name away
        import subprocess
        other = subprocess.Popen(                               # a second launch: `blamixshell gui --connect web-1`
            [sys.executable, "-c", "from PySide6.QtCore import QCoreApplication; app = QCoreApplication([]); "
             "from blamixshell import handoff; print('SENT', handoff.send(handoff.request('web-1')))"],
            stdout=subprocess.PIPE, text=True, cwd=str(Path(__file__).parent.parent),
            env=dict(os.environ, BLAMIXSHELL_HOME=home, QT_QPA_PLATFORM="offscreen"))
        end = time.time() + 20
        while other.poll() is None and time.time() < end:
            app.processEvents()
            time.sleep(0.01)
        assert "SENT True" in other.stdout.read() and got == [handoff.request("web-1")]
        server.close()
        assert not handoff.send(handoff.request("web-1"), timeout_ms=300)   # nobody open: start normally


def test_the_window_opens_a_saved_server_or_asks_and_waits_while_locked():
    from PySide6.QtWidgets import QDialog
    from blamixshell import app as appmod
    opened, upserts = [], []
    win = SimpleNamespace(isMinimized=lambda: False, show=lambda: None, raise_=lambda: None,
                          activateWindow=lambda: None, store=store_with(WEB),
                          statusBar=lambda: SimpleNamespace(showMessage=lambda *a: None),
                          connect_server=opened.append, refresh_all=lambda: None)
    win.store.upsert = upserts.append
    handle = appmod.MainWindow.handle_request
    handle(win, handoff.request("web-1"))
    assert opened == [WEB.id]

    class FakeDialog:
        def __init__(self, store, server, parent, new=False):
            assert new and server.host == "db.internal"
            self.server = server

        def exec(self):
            return QDialog.Accepted

        def result_server(self):
            return self.server
    with mock.patch.object(appmod, "ServerDialog", FakeDialog):
        handle(win, handoff.request("bob@db.internal"))
    assert len(upserts) == 1 and opened[-1] == upserts[0].id
    win._locked = True
    handle(win, handoff.request("web-1"))
    assert len(opened) == 2 and win._pending_requests == [handoff.request("web-1")]
