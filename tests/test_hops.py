"""A terminal that went on to another machine with ssh: noticed from what you typed and the prompt, the Files panel
says its files are still the first server's, and offers to open that machine through the first one."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell.hops import HopWatch, parse_ssh, prompt_host  # noqa: E402


def test_which_commands_are_a_hop():
    assert parse_ssh("ssh linxstress").host == "linxstress"
    h = parse_ssh("ssh -p 2222 -l bob 10.0.0.5")
    assert (h.host, h.user, h.port) == ("10.0.0.5", "bob", 2222)
    h = parse_ssh("sudo ssh -tt -J bastion -i ~/.ssh/id root@db.internal")
    assert (h.host, h.user) == ("db.internal", "root")
    assert parse_ssh("ssh ssh://bob@h:2200").port == 2200
    assert parse_ssh("ssh -o StrictHostKeyChecking=no h").host == "h"
    for not_a_hop in ("ssh host uptime", "ssh", "ls -la", "sshd -t", "git push", "ssh 'unclosed", "ssh -p 22"):
        assert parse_ssh(not_a_hop) is None, not_a_hop
    assert prompt_host("safemobile@LinxStress:~$") == "LinxStress"
    assert prompt_host("[root@web-1 nginx]#") == "web-1" and prompt_host("$") == ""


def test_going_there_and_coming_back():
    w = HopWatch()
    assert not w.update("safemobile@CompilerMaster:~$", "ls")
    assert not w.update("safemobile@CompilerMaster:~$", "ssh linxstress")       # typed, not there yet
    assert w.update("safemobile@LinxStress:~$", "sudo nano /etc/nginx/conf.d/safemobile.conf")
    assert (w.hop.host, w.hop.seen_as, w.hop.user, w.hop.via) == ("linxstress", "LinxStress", "safemobile",
                                                                  "CompilerMaster")
    assert not w.update("safemobile@LinxStress:~$", "exit")                    # still there until the prompt says
    assert w.update("safemobile@CompilerMaster:~$", "ls") and w.hop is None   # back home
    w2 = HopWatch()
    w2.update("root@a:~#", "sudo -i")                                          # same machine, other user: no hop
    assert not w2.update("root@a:~#", "ls") and w2.hop is None


def test_the_files_panel_says_so_and_offers_to_open_it():
    from PySide6.QtWidgets import QApplication
    from blamixshell import sftp_panel
    QApplication.instance() or QApplication([])
    panel = sftp_panel.SftpPanel()
    panel.session = SimpleNamespace(files_host="compilermaster")
    w = HopWatch()
    w.update("safemobile@CompilerMaster:~$", "ssh linxstress")
    w.update("safemobile@LinxStress:~$", "ls")
    got = []
    panel.open_hop.connect(got.append)
    panel.show_hop(w.hop, "!CompilerMaster")
    assert not panel.hop_box.isHidden()
    assert "on LinxStress now" in panel.hop_lbl.text() and "still on <b>compilermaster</b>" in panel.hop_lbl.text()
    assert panel.hop_open.text().strip() == "Open LinxStress here (through !CompilerMaster)"
    panel.hop_open.click()
    assert got == [w.hop]
    panel.show_hop(None)
    assert panel.hop_box.isHidden()
    panel.shutdown()


def test_open_here_reuses_a_saved_server_or_fills_in_the_form():
    from PySide6.QtWidgets import QDialog
    from blamixshell import app as appmod
    from blamixshell.models import Server
    home = Server(name="!CompilerMaster", host="git.safemobile.org", port=64990, username="safemobile", group="Lab")
    opened, saved = [], []
    store = SimpleNamespace(servers={home.id: home}, upsert=saved.append)
    win = SimpleNamespace(active_pane=lambda: SimpleNamespace(server=home, session=None, hops=SimpleNamespace(chain=[])),
                          store=store, refresh_all=lambda: None, connect_server=opened.append)
    w = HopWatch()
    w.update("safemobile@CompilerMaster:~$", "ssh linxstress")
    w.update("safemobile@LinxStress:~$", "ls")

    class Form:
        def __init__(self, store, server, parent, new=False):
            assert new and (server.host, server.jump_id, server.username, server.group) == (
                "linxstress", home.id, "safemobile", "Lab") and server.name == "LinxStress"
            self.server = server

        def exec(self):
            return QDialog.Accepted

        def result_server(self):
            return self.server
    with mock.patch.object(appmod, "ServerDialog", Form):
        appmod.MainWindow.open_hop(win, w.hop)
    assert len(saved) == 1 and opened == [saved[0].id]
    store.servers[saved[0].id] = saved[0]                                       # next time: no form, just connect
    with mock.patch.object(appmod, "ServerDialog", lambda *a, **k: (_ for _ in ()).throw(AssertionError)):
        appmod.MainWindow.open_hop(win, w.hop)
    assert opened == [saved[0].id, saved[0].id]
