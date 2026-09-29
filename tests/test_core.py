"""Core tests. Live SSH tests run when SHELLDECK_TEST_SSH=host:port:user:password is set."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from shelldeck import importers  # noqa: E402
from shelldeck.models import Server, Store  # noqa: E402
from shelldeck.vault import Vault, VaultError, WrongPassword  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELLDECK_HOME", str(tmp_path))
    return tmp_path


def test_vault_roundtrip(tmp_path):
    p = tmp_path / "v.sdv"
    v = Vault.create(p, "correct horse", {"a": 1}, n_log2=12)
    v.save({"servers": [{"host": "x", "password": "hunter2"}]})
    raw = p.read_bytes()
    assert b"hunter2" not in raw and raw[:4] == b"SDV1"
    _v2, data = Vault.open(p, "correct horse")
    assert data["servers"][0]["password"] == "hunter2"
    with pytest.raises(WrongPassword):
        Vault.open(p, "wrong")


def test_vault_change_password(tmp_path):
    p = tmp_path / "v.sdv"
    v = Vault.create(p, "old-password", n_log2=12)
    v.change_password("new-password", {"k": 2})
    with pytest.raises(WrongPassword):
        Vault.open(p, "old-password")
    assert Vault.open(p, "new-password")[1] == {"k": 2}


def test_vault_rejects_garbage(tmp_path):
    p = tmp_path / "junk"
    p.write_bytes(b"hello world" * 10)
    with pytest.raises(VaultError):
        Vault.open(p, "x")


def test_store_groups_and_search(tmp_path):
    st = Store(Vault.create(tmp_path / "v", "pw", n_log2=12), {})
    a = Server(name="api-1", host="10.0.0.1", group="Prod/EU", tags=["prod", "api"])
    b = Server(name="db", host="10.0.0.2", group="Prod/US", tags=["db"])
    st.upsert(a)
    st.upsert(b)
    assert st.all_groups() == ["Prod", "Prod/EU", "Prod/US"]
    assert a.matches("tag:pro api") and not b.matches("tag:prod")
    st.rename_group("Prod", "Production")
    assert st.servers[a.id].group == "Production/EU"
    st.delete_group("Production/EU")
    assert st.servers[a.id].group == "Production"
    # persisted
    _v, data = Vault.open(tmp_path / "v", "pw")
    assert Store(_v, data).servers[a.id].group == "Production"
    # dedupe on import
    assert st.import_servers([Server(host="10.0.0.2"), Server(host="new")]) == 1  # first is a dup
    assert st.import_servers([Server(host="new")]) == 0


def test_ssh_config_parser():
    cfg = """
Host bastion
  HostName bastion.example.com
  User ops
Host web-*
  User www
Host app
  HostName 10.1.2.3
  Port 2200
  User deploy
  IdentityFile ~/.ssh/app_key
  ProxyJump bastion
"""
    servers = {s.name: s for s in importers.parse_ssh_config(cfg)}
    assert set(servers) == {"bastion", "app"}
    app = servers["app"]
    assert (app.host, app.port, app.username, app.auth) == ("10.1.2.3", 2200, "deploy", "key")
    assert app.jump_id == servers["bastion"].id


def test_parse_target():
    from shelldeck.app import parse_target
    s = parse_target("ssh -p 2201 root@example.org")
    assert (s.username, s.host, s.port) == ("root", "example.org", 2201)
    s = parse_target("me@1.2.3.4:2222")
    assert (s.username, s.host, s.port) == ("me", "1.2.3.4", 2222)


def test_ppk_friendly_error():
    from shelldeck.ssh_session import AuthConfigError, load_private_key
    with pytest.raises(AuthConfigError, match="PuTTYgen"):
        load_private_key(path="C:/keys/mine.ppk")


LIVE = os.environ.get("SHELLDECK_TEST_SSH")


def _live_server(**kw):
    host, port, user, pw = LIVE.split(":")
    return Server(name="live", host=host, port=int(port), username=user, password=pw, **kw)


@pytest.mark.skipif(not LIVE, reason="no live sshd")
def test_live_shell_hostkey_and_sftp(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from shelldeck.ssh_session import ShellSession
    app = QCoreApplication.instance() or QCoreApplication([])
    srv = _live_server()
    s = ShellSession(srv, lambda _i: None)
    ev = {"out": b"", "prompt": None, "connected": False, "failed": None}
    s.output.connect(lambda b: ev.__setitem__("out", ev["out"] + b))
    s.host_key_prompt.connect(lambda *a: ev.__setitem__("prompt", a))
    s.connected.connect(lambda: ev.__setitem__("connected", True))
    s.failed.connect(lambda m: ev.__setitem__("failed", m))

    def pump(cond, t=15):
        end = time.time() + t
        while time.time() < end and not cond():
            app.processEvents()
            time.sleep(0.02)
        return cond()

    s.start(100, 30)
    assert pump(lambda: ev["prompt"] or ev["failed"]), "no host key prompt"
    assert ev["prompt"][3] is False and ev["prompt"][2].startswith("SHA256:")
    s.accept_host_key()
    assert pump(lambda: ev["connected"] or ev["failed"]) and ev["connected"], ev["failed"]
    s.send(b"echo SHELL_$((6*7))\n")
    assert pump(lambda: b"SHELL_42" in ev["out"])
    s.resize(140, 40)
    s.send(b"stty size\n")
    assert pump(lambda: b"40 140" in ev["out"])

    sftp = s.sftp()
    local = tmp_path / "up.txt"
    local.write_text("hello sftp")
    sftp.put(str(local), "shelldeck_up.txt")
    assert "shelldeck_up.txt" in sftp.listdir(".")
    sftp.remove("shelldeck_up.txt")

    # second connection: host key now known -> no prompt
    s2 = ShellSession(_live_server(), lambda _i: None)
    ok = {"c": False, "p": False}
    s2.connected.connect(lambda: ok.__setitem__("c", True))
    s2.host_key_prompt.connect(lambda *a: ok.__setitem__("p", True))
    s2.start()
    assert pump(lambda: ok["c"] or ok["p"]) and ok["c"] and not ok["p"]
    s.close()
    s2.close()


@pytest.mark.skipif(not LIVE or not os.environ.get("SHELLDECK_TEST_KEY"), reason="no key")
def test_live_key_auth_and_jump():
    from shelldeck.ssh_session import open_client, trust_host_key, load_known_hosts
    import paramiko
    key_path, passphrase = os.environ["SHELLDECK_TEST_KEY"].split(":")
    bastion = _live_server()
    target = _live_server(auth="key", key_path=key_path, passphrase=passphrase, jump_id=bastion.id)
    target.password = ""
    # pre-trust via a raw transport so the test doesn't prompt
    t = paramiko.Transport((bastion.host, bastion.port))
    t.start_client()
    trust_host_key(f"[{bastion.host}]:{bastion.port}", t.get_remote_server_key())
    t.close()
    assert load_known_hosts()
    client, chain = open_client(target, {bastion.id: bastion}.get)
    _i, out, _e = client.exec_command("echo via-jump")
    assert out.read().strip() == b"via-jump"
    assert len(chain) == 1
    client.close()
    chain[0].close()
    # wrong passphrase gives a readable error
    from shelldeck.ssh_session import AuthConfigError, load_private_key
    with pytest.raises(Exception):
        load_private_key(path=key_path, passphrase="nope")
    with pytest.raises(AuthConfigError, match="passphrase"):
        load_private_key(path=key_path)


def test_data_dir_portable_vs_installed(tmp_path, monkeypatch):
    import sys
    from shelldeck import paths
    monkeypatch.delenv("SHELLDECK_HOME", raising=False)
    monkeypatch.setattr(paths, "_DATA_DIR", None)
    appdir = tmp_path / "ShellDeck"
    appdir.mkdir()
    exe = appdir / "ShellDeck.exe"
    exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(paths, "_legacy_dir", lambda: tmp_path / "peruser")
    assert paths.data_dir() == appdir / "data"            # portable zip
    (appdir / paths.INSTALLED_MARKER).write_text("")
    monkeypatch.setattr(paths, "_DATA_DIR", None)
    assert paths.data_dir() == tmp_path / "peruser"       # MSI install


def test_sftp_skips_login_script_noise():
    """Text printed by ~/.bashrc before the SFTP handshake must not break SFTP."""
    from shelldeck.ssh_core import _SkipLoginNoise

    version = (5).to_bytes(4, "big") + b"\x02\x00\x00\x00\x03"   # SSH_FXP_VERSION v3
    after = b"NEXT-PACKET"

    class FakeChan:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def recv(self, n):
            return self.chunks.pop(0) if self.chunks else b""

    # noise split across reads, version packet split too
    ch = _SkipLoginNoise(FakeChan([b"Welcome to prod!\r\nLast", b" login: today\n" + version[:3],
                                   version[3:] + after]))
    got = b""
    while len(got) < len(version) + len(after):
        got += ch.recv(4)
    assert got == version + after
    assert b"Welcome to prod!" in ch.skipped
    # clean server: nothing skipped
    ch = _SkipLoginNoise(FakeChan([version]))
    assert ch.recv(100) == version and ch.skipped == b""
