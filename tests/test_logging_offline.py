"""Command log, session recordings, health strip parsing, and offline / managed updates."""
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

from blamixshell import dashboard as d
from blamixshell import session_log as sl
from blamixshell import updater
from blamixshell.models import Server


@pytest.mark.parametrize("line,expected", [
    ("deploy@web-1:/srv$ git pull", ("deploy@web-1:/srv$", "git pull")),
    ("root@web:~# echo a > b", ("root@web:~#", "echo a > b")),
    ("[mike@c7 ~]$ ls -la", ("[mike@c7 ~]$", "ls -la")),
    ("PS C:\\Users> dir", ("PS C:\\Users>", "dir")),
    ("❯ kubectl get pods", ("❯", "kubectl get pods")),
    ("mysql> select 1;", ("mysql>", "select 1;")),
    ("user@h:~$ ", None),                         # Enter on an empty prompt
    ("[sudo] password for mike: ", None),         # never log password prompts
    ("Enter passphrase for key '/root/.ssh/id': ", None),
])
def test_command_from_line(line, expected):
    assert sl.command_from_line(line) == expected


def test_text_cleaner_handles_split_escapes_and_redraws():
    c = sl.TextCleaner()
    out = c.feed(b"\x1b[32mgreen\x1b[0m text\r\nprog 10%\rprog 100%\r\nab\x08c\r\n\x1b]0;title\x07x\x1b[")
    out += c.feed(b"1mbold\r\n\xc3")              # escape and a UTF-8 character split across chunks
    out += c.feed(b"\xa9t\xc3\xa9\r\n")
    assert out == ["green text", "prog 100%", "ac", "xbold", "été"]


def _server():
    return Server(name="api prod/1", host="10.0.0.5", username="deploy")


def test_command_log_file(tmp_path):
    log = sl.CommandLog(tmp_path)
    when = time.mktime((2026, 10, 1, 14, 3, 22, 0, 0, -1))
    log.add(_server(), "systemctl restart nginx", "terminal", "deploy@api:/etc$", when=when)
    log.add(_server(), "kill -TERM 42\tx", "dashboard", when=when)
    lines = (tmp_path / "commands" / "2026-10-01.log").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# time")
    f = lines[1].split("\t")
    assert f[0].startswith("2026-10-01T14:03:22") and f[2] == "deploy@10.0.0.5" and f[3] == "api prod/1"
    assert f[4:] == ["terminal", "deploy@api:/etc$", "systemctl restart nginx"]
    assert lines[2].split("\t")[-1] == "kill -TERM 42 x"          # tabs can't break the columns


@pytest.mark.parametrize("fmt,ts", [("text", False), ("text", True), ("raw", False), ("raw", True)])
def test_recorder(tmp_path, fmt, ts):
    r = sl.Recorder(tmp_path, _server(), fmt, ts)
    r.write(b"\x1b[1mhello\x1b[0m\r\nwor")
    r.write(b"ld\r\n")
    path = r.close()
    assert path.parent == tmp_path / "sessions" / "api_prod_1"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# BlamixShell recording · api prod/1 (deploy@10.0.0.5)") and "recording ended" in text
    body = text.splitlines()[1:3]
    if fmt == "text":
        assert [l.split("] ", 1)[-1] if ts else l for l in body] == ["hello", "world"]
    else:
        assert "\x1b[1mhello" in text
    if ts:
        assert body[0].startswith("[") and body[1].startswith("[")


def test_prune(tmp_path):
    old = tmp_path / "commands" / "2020-01-01.log"
    new = tmp_path / "sessions" / "x" / "now.log"
    for p in (old, new):
        p.parent.mkdir(parents=True)
        p.write_text("x")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    assert sl.prune(tmp_path, 0) == 0 and sl.prune(tmp_path, 30) == 1
    assert not old.exists() and new.exists()


def test_health_parser():
    t = ("@@stat\ncpu  10000 0 5000 80000 1000 0 100 0 0 0\n@@load\n0.52 0.40 0.31 2/301 1\n@@nproc\n4\n"
         "@@mem\nMemTotal: 1000 kB\nMemAvailable: 400 kB\n@@df\n"
         "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100 71 29 71% /\n@@end\n")
    first = d.parse_health(t)
    assert first.cpu is None and first.mem == 60 and first.disk == 71 and first.load == 0.52 and first.cpus == 4
    later = d.parse_health(t.replace("10000 0 5000 80000", "10030 0 5010 80050"), first.sample)
    assert round(later.cpu, 1) == 44.4
    assert d.parse_health("@@end\n").mem is None          # nothing readable: no crash


# ------------------------------------------------------------------ offline / managed updates
def test_update_policy(monkeypatch, tmp_path):
    monkeypatch.delenv("BLAMIXSHELL_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(updater, "_registry_policy", lambda: None)
    monkeypatch.setattr(updater, "_policy_files", lambda: [tmp_path / "policy.ini"])
    assert updater.update_check_policy() is None and updater.updates_allowed({"check_updates": True})
    assert not updater.updates_allowed({"check_updates": False})
    (tmp_path / "policy.ini").write_text("[policy]\nupdate_check = false\n")
    assert updater.update_check_policy() is False and not updater.updates_allowed({"check_updates": True})
    monkeypatch.setenv("BLAMIXSHELL_UPDATE_CHECK", "1")                # env wins over the file
    assert updater.update_check_policy() is True and updater.updates_allowed({"check_updates": False})
    monkeypatch.setattr(updater, "_registry_policy", lambda: "0")
    monkeypatch.delenv("BLAMIXSHELL_UPDATE_CHECK")
    assert updater.update_check_policy() is False                      # MSI UPDATECHECK=0


def test_update_from_file_checks(tmp_path):
    msi = tmp_path / "BlamixShell-1.6.0-x64.msi"
    msi.write_bytes(b"msi")
    assert updater.file_version(msi) == "1.6.0"
    assert updater.check_update_file(msi, "msi") == ""
    assert "pick the .msi" in updater.check_update_file(tmp_path / "x.zip", "msi")
    assert "doesn't match" in updater.check_update_file(msi, "msi", "00" * 32)
    assert updater.check_update_file(msi, "msi", "sha256:" + updater.sha256_of(msi)) == ""
    z = tmp_path / "BlamixShell-windows-x64.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("BlamixShell/readme.txt", "no exe")
    assert "doesn't contain BlamixShell.exe" in updater.check_update_file(z, "portable")
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("BlamixShell/BlamixShell.exe", "exe")
    assert updater.check_update_file(z, "portable") == ""
    assert "replacing the app" in updater.check_update_file(msi, "mac")


def test_cli_exec_logs_commands(tmp_path, monkeypatch, capsys):
    from blamixshell import cli
    from blamixshell.models import Store
    from blamixshell.vault import Vault
    monkeypatch.setenv("BLAMIXSHELL_HOME", str(tmp_path))
    st = Store(Vault.create(tmp_path / "v.sdv", "masterpass", n_log2=10), {})
    s = Server(name="web-1", host="127.0.0.1", port=1, username="x", password="x", log_commands=True)
    st.upsert(s)
    monkeypatch.setattr(cli, "run_many", lambda *a, **k: 0)
    a = cli.build_parser().parse_args(["exec", "-y", "web-1", "--", "uptime"])
    a.command = [x for x in a.command if x != "--"]
    cli.cmd_exec(st, a)
    files = list((tmp_path / "logs" / "commands").glob("*.log"))
    assert files and files[0].read_text(encoding="utf-8").splitlines()[1].endswith("\texec\t\tuptime")
