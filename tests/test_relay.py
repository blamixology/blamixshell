"""Files, editor and dashboard following `ssh other` typed in a terminal, through the first server's own ssh: the
commands it runs there, chains, why it can't, the terminal's switching, the Files panel and the dashboard."""
import io
import os
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import dashboard as d, relay  # noqa: E402
from blamixshell.hops import HopWatch  # noqa: E402


class FakeChannel:
    """Runs nothing: records the command and answers like the machine at the end would."""

    def __init__(self, log, answer):
        self.log, self.answer = log, answer
        self.out = io.BytesIO()
        self.err = io.BytesIO()

    def exec_command(self, command):
        self.log.append(command)
        out, err, self.code = self.answer(command)
        self.out, self.err = io.BytesIO(out.encode()), io.BytesIO(err.encode())

    def settimeout(self, t):
        pass

    def makefile_stdin(self, *a):
        return SimpleNamespace(write=lambda b: None, flush=lambda: None, channel=SimpleNamespace(shutdown_write=lambda: None))

    def makefile(self, *a):
        ch = self
        return SimpleNamespace(read=lambda *n: ch.out.read(), channel=SimpleNamespace(recv_exit_status=lambda: ch.code))

    def makefile_stderr(self, *a):
        return SimpleNamespace(read=lambda *n: self.err.read())


class FakeTransport:
    def __init__(self, answer):
        self.log = []
        self.answer = answer

    def open_session(self, *a, **k):
        return FakeChannel(self.log, self.answer)

    def is_active(self):
        return True


def home_with(answer):
    t = FakeTransport(answer)
    client = SimpleNamespace(get_transport=lambda: t)
    return SimpleNamespace(client=client, is_connected=True, server=SimpleNamespace(id="h1", label="!CompilerMaster",
                                                                                    group="Lab")), t


def chain(*steps):
    w = HopWatch()
    w.update("safemobile@CompilerMaster:~$", "ls")
    for typed, prompt in steps:
        w.update(f"x@{w.chain[-1].seen_as if w.chain else 'CompilerMaster'}:~$", typed)
        w.update(prompt, "ls")
    return w.chain


def inner(command: str) -> str:
    """The command the last machine runs, out of `mkdir …; exec ssh … dest 'command'`."""
    return shlex.split(command.split("; exec ", 1)[1])[-1]


def test_commands_go_through_the_first_servers_ssh():
    home, t = home_with(lambda c: ("bxs-relay-ok\nlinxstress\n", "", 0))
    rs = relay.RelaySession(home, chain(("ssh -p 2222 -i ~/.ssh/k bob@linx", "bob@LinxStress:~$")))
    assert rs.server.label == "LinxStress (via !CompilerMaster)" and rs.server.username == "bob"
    assert rs.probe() == (True, "") and rs.files_host == "linxstress"
    r = d.Runner(rs.client).run("echo hi")
    sent = t.log[-1]
    assert sent.startswith("mkdir -p ~/.ssh 2>/dev/null; exec ssh -T -o BatchMode=yes")
    assert "ControlMaster=auto" in sent and "-p 2222 -i '~/.ssh/k' bob@linx" in sent
    assert "sh -c 'echo hi'" in inner(sent) and r.code == 0                         # the Runner's wrapper, intact
    ch = rs.client.get_transport().open_session()
    ch.invoke_subsystem("sftp")
    assert t.log[-1].endswith("-s bob@linx sftp")                                   # SFTP is a subsystem there too
    ch.get_pty()                                                                    # asked for: ignored, no error


def test_a_chain_wraps_once_per_machine():
    home, t = home_with(lambda c: ("bxs-relay-ok\nc\n", "", 0))
    hops = chain(("ssh b", "u@B:~$"), ("ssh -l root c", "root@C:~#"))
    rs = relay.RelaySession(home, hops)
    assert rs.server.label == "C (via !CompilerMaster → B)"
    rs.client.exec_command("uptime")
    outer = t.log[-1]
    assert shlex.split(outer.split("; exec ", 1)[1])[-2] == "b"                    # the first server goes to b …
    second = inner(outer)
    assert "exec ssh" in second and shlex.split(second.split("; exec ", 1)[1])[-2] == "c"   # … b goes to c
    assert inner(second) == "uptime"


def test_why_it_cant_and_sudo_ssh():
    home, _t = home_with(lambda c: ("", "bob@linx: Permission denied (publickey,password).\n", 255))
    ok, why = relay.RelaySession(home, chain(("ssh bob@linx", "bob@LinxStress:~$"))).probe()
    assert not ok and "password" in why
    assert "host key" in relay.explain("Host key verification failed.")
    assert "resolve" in relay.explain("ssh: Could not resolve hostname x: Name or service not known")
    assert "reached" in relay.explain("ssh: connect to host x port 22: Connection timed out")
    hop = chain(("sudo ssh root@db", "root@db:~#"))[-1]
    assert relay.wrap(hop, "id").split("; exec ", 1)[1].startswith("sudo -n ssh ")   # never waits for a password


def test_ssh_G_fills_in_the_form():
    home, _t = home_with(lambda c: ("user safemobile\nhostname 10.20.0.7\nport 2201\nidentityfile ~/.ssh/id_ed25519\n"
                                    "identityfile ~/.ssh/id_rsa\n", "", 0))
    hop = chain(("ssh linx", "safemobile@LinxStress:~$"))[-1]
    got = relay.resolved(home.client, hop)
    assert got == {"user": "safemobile", "hostname": "10.20.0.7", "port": "2201", "identityfile": "~/.ssh/id_ed25519"}


def wait(app, cond, seconds=5):
    end = time.time() + seconds
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


def test_the_terminal_switches_files_to_the_machine_and_back():
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
    from blamixshell import terminal
    app = QApplication.instance() or QApplication([])
    home, _t = home_with(lambda c: ("bxs-relay-ok\nlinxstress\n", "", 0))

    class Pane(QObject):                       # TerminalPane's relay logic, without a browser engine
        hop_changed = Signal(object)
        _relay_checked = Signal(object, object, str)
        files_session = terminal.TerminalPane.files_session
        _drop_relay = terminal.TerminalPane._drop_relay
        _follow_hop = terminal.TerminalPane._follow_hop
        _on_relay_checked = terminal.TerminalPane._on_relay_checked

        def __init__(self):
            super().__init__()
            self.session, self.server, self.settings = home, SimpleNamespace(uses_ssh=True), {}
            self.hops, self.relay, self.relay_state, self.relay_error = HopWatch(), None, "", ""
            self._relay_checked.connect(self._on_relay_checked)
    p = Pane()
    p.hops.update("safemobile@CompilerMaster:~$", "ssh linxstress")
    p.hops.update("safemobile@LinxStress:~$", "ls")
    p._follow_hop()
    assert p.relay_state == "checking" and p.files_session is home                 # not before it's known to work
    assert wait(app, lambda: p.relay_state == "ok")
    assert p.files_session is p.relay and p.relay.files_host == "linxstress"
    p.hops.update("safemobile@CompilerMaster:~$", "ls")                            # exit: back home
    p._follow_hop()
    assert p.relay is None and p.files_session is home
    p.settings = {"follow_ssh": False}
    p.hops.update("safemobile@CompilerMaster:~$", "ssh linxstress")
    p.hops.update("safemobile@LinxStress:~$", "ls")
    p._follow_hop()
    assert p.relay is None                                                          # turned off in Settings


def test_files_panel_and_dashboard_follow():
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
    from blamixshell import sftp_panel
    from blamixshell.relay_pane import RelayPane
    QApplication.instance() or QApplication([])
    hop = chain(("ssh linxstress", "safemobile@LinxStress:~$"))[-1]
    panel = sftp_panel.SftpPanel()
    panel.session = SimpleNamespace(files_host="compilermaster")
    panel.show_hop(hop, "!CompilerMaster", "ok")
    assert "Following your terminal to LinxStress" in panel.hop_lbl.text() and "Save LinxStress" in panel.hop_open.text()
    panel.show_hop(hop, "!CompilerMaster", "failed", "it asks for a password there")
    assert "can't follow you there without a password (it asks for a password there)" in panel.hop_lbl.text()
    assert panel.hop_open.text().strip() == "Open LinxStress here (through !CompilerMaster)"
    panel.shutdown()

    home, _t = home_with(lambda c: ("bxs-relay-ok\n", "", 0))
    rs = relay.RelaySession(home, [hop])

    class Term(QObject):
        state_changed = Signal(object)
        hop_changed = Signal(object)
        state, relay, relay_state = "connected", rs, "ok"
        logged = []

        def log_command(self, *a):
            self.logged.append(a)
    term = Term()
    rp = RelayPane(term)
    assert rp.session is rs and rp.state == "connected" and rp.server.label.startswith("LinxStress")
    rp.log_command("systemctl restart nginx", "dashboard")
    assert term.logged[-1][0] == "[on LinxStress] systemctl restart nginx"
    term.relay = None                                                               # the shell left that machine
    assert rp.state == "disconnected"
