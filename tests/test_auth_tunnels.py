"""2FA / keyboard-interactive auth and port forwarding, against an in-process SSH server."""
import os
import socket
import struct
import sys
import time
from pathlib import Path

import paramiko
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sshserver import CODE, PASSWORD, USER, TestSSHServer, echo_server, host_key  # noqa: E402

from shelldeck.models import Server, Tunnel  # noqa: E402
from shelldeck import ssh_core  # noqa: E402
from shelldeck.ssh_core import (AuthCancelled, NeedsInput, host_id, open_client,  # noqa: E402
                                trust_host_key)
from shelldeck.tunnels import TunnelManager  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELLDECK_HOME", str(tmp_path))
    return tmp_path


def _server(srv, **kw) -> Server:
    trust_host_key(host_id("127.0.0.1", srv.port), host_key())
    return Server(name="t", host="127.0.0.1", port=srv.port, username=USER, keepalive=0, **kw)


def _close(client, chain):
    client.close()
    for c in chain:
        c.close()


# ------------------------------------------------------------------ auth
def test_plain_password_still_works():
    with TestSSHServer("password") as srv:
        client, chain = open_client(_server(srv, password=PASSWORD), lambda _i: None)
        assert client.get_transport().is_authenticated()
        _close(client, chain)


def test_otp_fills_saved_password_and_asks_only_for_the_code():
    asked = []

    def ask(title, instructions, prompts):
        asked.append((title, instructions, prompts))
        return [CODE]
    with TestSSHServer("otp") as srv:
        client, chain = open_client(_server(srv, password=PASSWORD), lambda _i: None, interactive=ask)
        assert client.get_transport().is_authenticated()
        _close(client, chain)
    assert len(asked) == 1
    title, _instr, prompts = asked[0]
    assert title == "Login" and [p[0].strip() for p in prompts] == ["Verification code:"]
    assert prompts[0][1] is True          # echo flag passed through


def test_otp_without_saved_password_asks_for_both():
    with TestSSHServer("otp") as srv:
        answers = {"Password:": PASSWORD, "Verification code:": CODE}
        client, chain = open_client(_server(srv), lambda _i: None,
                                    interactive=lambda t, i, ps: [answers[p[0].strip()] for p in ps])
        assert client.get_transport().is_authenticated()
        _close(client, chain)


def test_key_then_code():
    key = paramiko.RSAKey.generate(2048)
    import io
    buf = io.StringIO()
    key.write_private_key(buf)
    with TestSSHServer("key+otp", client_key=key) as srv:
        s = _server(srv, auth="key", key_data=buf.getvalue())
        client, chain = open_client(s, lambda _i: None, interactive=lambda t, i, ps: [CODE])
        assert client.get_transport().is_authenticated()
        _close(client, chain)


def test_cancel_and_missing_handler():
    with TestSSHServer("otp") as srv:
        with pytest.raises(AuthCancelled):
            open_client(_server(srv, password=PASSWORD), lambda _i: None, interactive=lambda *a: None)
        with pytest.raises(NeedsInput):
            open_client(_server(srv, password=PASSWORD), lambda _i: None)
        msg = ssh_core.test_connection(_server(srv, password=PASSWORD), lambda _i: None)
        assert msg.startswith("OK.") and "Verification code" in msg


def test_wrong_code_fails():
    with TestSSHServer("otp") as srv:
        with pytest.raises(paramiko.AuthenticationException):
            open_client(_server(srv, password=PASSWORD), lambda _i: None, interactive=lambda *a: ["000000"])


def test_jump_host_prompts_too():
    with TestSSHServer("otp") as bastion, TestSSHServer("password") as target:
        b = _server(bastion, password=PASSWORD)
        t = _server(target, password=PASSWORD, jump_id=b.id)
        client, chain = open_client(t, {b.id: b}.get, interactive=lambda *a: [CODE])
        assert client.get_transport().is_authenticated() and len(chain) == 1
        _close(client, chain)


# ------------------------------------------------------------------ tunnels
def _roundtrip(port: int, payload: bytes = b"hello tunnel") -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(payload)
        got = b""
        while len(got) < len(payload):
            got += s.recv(1024)
        return got


@pytest.fixture
def connected():
    with TestSSHServer("password") as srv:
        client, chain = open_client(_server(srv, password=PASSWORD), lambda _i: None)
        yield srv, client
        _close(client, chain)


def test_parse_openssh_syntax():
    t = Tunnel.parse("L", "5432:db.internal:5432")
    assert (t.kind, t.listen_host, t.listen_port, t.dest_host, t.dest_port) == ("L", "127.0.0.1", 5432, "db.internal", 5432)
    assert Tunnel.parse("-D", "1080").describe() == "SOCKS 127.0.0.1:1080"
    assert Tunnel.parse("R", "0.0.0.0:9000:localhost:3000").listen_host == "0.0.0.0"
    assert Tunnel.parse("L", "8080 web:80").dest_host == "web"
    with pytest.raises(ValueError):
        Tunnel.parse("L", "nonsense")
    assert Tunnel("L", listen_port=0).problem()
    s = Server(host="h", tunnels=[{"kind": "D", "listen_port": 1080}])
    assert isinstance(Server.from_dict(s.__dict__).tunnels[0], Tunnel)


def test_local_forward(connected):
    _srv, client = connected
    ls, eport = echo_server()
    mgr = TunnelManager(client.get_transport(), [Tunnel("L", "127.0.0.1", 0, "127.0.0.1", eport)])
    # port 0 isn't allowed for L in the UI (problem()), bind a free one ourselves
    free = socket.create_server(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    mgr.states[0].tunnel.listen_port = port
    (st,) = mgr.start()
    assert st.ok, st.error
    assert _roundtrip(port) == b"hello tunnel"
    assert _roundtrip(port, b"x" * 200_000) == b"x" * 200_000     # bigger than one window
    mgr.stop()
    ls.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).recv(1)


def _free_port() -> int:
    s = socket.create_server(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_dynamic_socks5_and_socks4a(connected):
    _srv, client = connected
    ls, eport = echo_server()
    port = _free_port()
    mgr = TunnelManager(client.get_transport(), [Tunnel("D", "127.0.0.1", port)])
    assert mgr.start()[0].ok
    # SOCKS5, domain name
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"\x05\x01\x00")
        assert s.recv(2) == b"\x05\x00"
        host = b"localhost"
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", eport))
        assert s.recv(10)[:2] == b"\x05\x00"
        s.sendall(b"ping5")
        assert s.recv(5) == b"ping5"
    # SOCKS4a
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"\x04\x01" + struct.pack("!H", eport) + b"\x00\x00\x00\x01" + b"me\x00127.0.0.1\x00")
        assert s.recv(8)[:2] == b"\x00\x5a"
        s.sendall(b"ping4")
        assert s.recv(5) == b"ping4"
    # closed destination port -> SOCKS failure reply, not a hang
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"\x05\x01\x00")
        s.recv(2)
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", _free_port()))
        assert s.recv(10)[:2] == b"\x05\x05"
    mgr.stop()
    ls.close()


def test_remote_forward(connected):
    srv, client = connected
    ls, eport = echo_server()
    mgr = TunnelManager(client.get_transport(), [Tunnel("R", "localhost", 0, "127.0.0.1", eport)])
    (st,) = mgr.start()
    assert st.ok and st.port, st.error
    # connect to the port the *server* opened; it comes back through the tunnel
    assert _roundtrip(st.port) == b"hello tunnel"
    mgr.stop()
    ls.close()
    time.sleep(0.2)
    assert st.port not in srv.remote_listeners


def test_errors_are_reported_not_raised(connected):
    _srv, client = connected
    busy = socket.create_server(("127.0.0.1", 0))
    port = busy.getsockname()[1]
    mgr = TunnelManager(client.get_transport(), [
        Tunnel("L", "127.0.0.1", port, "127.0.0.1", 1),     # port in use
        Tunnel("L", "127.0.0.1", 0, "x", 1),                 # invalid
        Tunnel("D", "127.0.0.1", _free_port(), enabled=False),
    ])
    states = mgr.start()
    assert len(states) == 2 and not any(s.ok for s in states)
    assert "in use" in states[0].error and "listen port" in states[1].error
    busy.close()


def test_server_refusing_forwarding():
    with TestSSHServer("password", deny_forwarding=True) as srv:
        client, chain = open_client(_server(srv, password=PASSWORD), lambda _i: None)
        mgr = TunnelManager(client.get_transport(), [Tunnel("R", "localhost", 0, "127.0.0.1", 9)])
        (st,) = mgr.start()
        assert not st.ok and "refused" in st.error
        _close(client, chain)


# ------------------------------------------------------------------ against a real OpenSSH server
# SHELLDECK_TEST_SSH=host:port:user:password        (password auth, forwarding allowed)
# SHELLDECK_TEST_SSH_KBD=host:port:user:password    (keyboard-interactive only, like PAM/2FA setups)
LIVE = os.environ.get("SHELLDECK_TEST_SSH", "")
LIVE_KBD = os.environ.get("SHELLDECK_TEST_SSH_KBD", "")


def _live_server(spec: str) -> Server:
    host, port, user, pw = spec.split(":", 3)
    return Server(name="live", host=host, port=int(port), username=user, password=pw, keepalive=0)


def _open_trusting(s: Server, **kw):
    from shelldeck.ssh_core import UnknownHostKey
    try:
        return open_client(s, lambda _i: None, **kw)
    except UnknownHostKey as e:
        trust_host_key(e.host_id, e.key)
        return open_client(s, lambda _i: None, **kw)


@pytest.mark.skipif(not LIVE, reason="no live sshd")
def test_live_tunnels_all_kinds():
    client, chain = _open_trusting(_live_server(LIVE))
    ls, eport = echo_server()
    lport, dport = _free_port(), _free_port()
    mgr = TunnelManager(client.get_transport(), [
        Tunnel("L", "127.0.0.1", lport, "127.0.0.1", eport),
        Tunnel("D", "127.0.0.1", dport),
        Tunnel("R", "localhost", 0, "127.0.0.1", eport),
    ])
    states = mgr.start()
    assert all(s.ok for s in states), [s.error for s in states]
    assert _roundtrip(lport) == b"hello tunnel"
    with socket.create_connection(("127.0.0.1", dport), timeout=5) as s:
        s.sendall(b"\x05\x01\x00")
        assert s.recv(2) == b"\x05\x00"
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", eport))
        assert s.recv(10)[:2] == b"\x05\x00"
        s.sendall(b"via socks")
        assert s.recv(9) == b"via socks"
    rport = states[2].port
    # the server listens on rport (on its side, here the same machine) -> back to our echo server
    assert _roundtrip(rport) == b"hello tunnel"
    mgr.stop()
    ls.close()
    _close(client, chain)


@pytest.mark.skipif(not LIVE_KBD, reason="no keyboard-interactive sshd")
def test_live_keyboard_interactive_uses_saved_password():
    s = _live_server(LIVE_KBD)
    client, chain = _open_trusting(s)                       # no handler: saved password fills the prompt
    assert client.get_transport().is_authenticated()
    _close(client, chain)
    asked = []
    s.password = ""

    def ask(title, instructions, prompts):
        asked.append(prompts)
        return [LIVE_KBD.split(":", 3)[3]]
    client, chain = _open_trusting(s, interactive=ask)      # no saved password: asked once
    assert client.get_transport().is_authenticated() and len(asked) == 1
    assert "password" in asked[0][0][0].lower() and asked[0][0][1] is False
    _close(client, chain)
