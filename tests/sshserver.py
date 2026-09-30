"""A tiny in-process SSH server for tests (paramiko). No sshd needed, runs on every OS.

Supports: password, keyboard-interactive with a second factor, publickey-then-code,
shell sessions (echo), direct-tcpip (-L/-D) and tcpip-forward (-R).
"""
from __future__ import annotations

import socket
import threading

import paramiko

USER, PASSWORD, CODE = "tester", "pw", "123456"
_HOST_KEY: paramiko.PKey | None = None


def host_key() -> paramiko.PKey:
    global _HOST_KEY
    if _HOST_KEY is None:
        _HOST_KEY = paramiko.RSAKey.generate(2048)
    return _HOST_KEY


def _forwarded_agent_keys(t) -> list[str]:
    """Ask the client's forwarded agent for its keys (SSH agent protocol:
    REQUEST_IDENTITIES -> IDENTITIES_ANSWER). Returns the key type names."""
    import struct
    try:
        ch = t.open_forward_agent_channel()
    except Exception as e:
        return [f"ERROR {e}"]
    ch.settimeout(5)
    ch.sendall(struct.pack(">IB", 1, 11))
    raw = b""
    while len(raw) < 4 or len(raw) < 4 + struct.unpack(">I", raw[:4])[0]:
        chunk = ch.recv(65536)
        if not chunk:
            break
        raw += chunk
    ch.close()
    body = raw[4:]
    if not body or body[0] != 12:
        return ["ERROR bad agent reply"]
    n = struct.unpack(">I", body[1:5])[0]
    pos, names = 5, []
    for _ in range(n):
        ln = struct.unpack(">I", body[pos:pos + 4])[0]
        blob = body[pos + 4:pos + 4 + ln]
        pos += 4 + ln
        ln2 = struct.unpack(">I", body[pos:pos + 4])[0]
        pos += 4 + ln2                                   # comment
        tl = struct.unpack(">I", blob[:4])[0]
        names.append(blob[4:4 + tl].decode())
    return names


def _pump(a, b) -> None:
    def one(src, dst):
        try:
            while True:
                d = src.recv(65536)
                if not d:
                    break
                dst.sendall(d)
        except Exception:
            pass
        for obj in (src, dst):
            try:
                obj.close()
            except Exception:
                pass
    threading.Thread(target=one, args=(a, b), daemon=True).start()
    threading.Thread(target=one, args=(b, a), daemon=True).start()


class _Iface(paramiko.ServerInterface):
    def __init__(self, srv: "TestSSHServer", transport):
        self.srv = srv
        self.t = transport
        self.key_ok = False
        self.direct: dict[int, tuple[str, int]] = {}

    # ---- auth
    def get_allowed_auths(self, username):
        m = self.srv.mode
        if m == "password":
            return "password"
        if m == "otp":
            return "keyboard-interactive"
        if m == "key+otp":
            return "keyboard-interactive" if self.key_ok else "publickey"
        return "password,keyboard-interactive"

    def check_auth_password(self, username, password):
        ok = self.srv.mode == "password" and username == USER and password == PASSWORD
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        if self.srv.mode == "key+otp" and self.srv.client_key and key == self.srv.client_key:
            self.key_ok = True
            return paramiko.AUTH_PARTIALLY_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        q = paramiko.InteractiveQuery("Login", "Two-step verification")
        if self.srv.mode == "otp":
            q.add_prompt("Password: ", False)
        q.add_prompt("Verification code: ", True)
        return q

    def check_auth_interactive_response(self, responses):
        want = [PASSWORD, CODE] if self.srv.mode == "otp" else [CODE]
        self.srv.seen_responses.append(list(responses))
        return paramiko.AUTH_SUCCESSFUL if list(responses) == want else paramiko.AUTH_FAILED

    # ---- channels
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *a):
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=self.srv._echo, args=(channel,), daemon=True).start()
        return True

    def check_channel_exec_request(self, channel, command):
        command = command.decode() if isinstance(command, bytes) else command
        self.srv.commands.append(command)
        threading.Thread(target=self.srv._run_command, args=(self.t, channel, command), daemon=True).start()
        return True

    def check_channel_forward_agent_request(self, channel):
        self.srv.agent_requests += 1
        return True

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        if self.srv.deny_forwarding:
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        try:   # like OpenSSH: connect first, refuse the channel if that fails
            s = socket.create_connection(destination, timeout=5)
            s.settimeout(None)
        except OSError:
            return paramiko.OPEN_FAILED_CONNECT_FAILED
        self.direct[chanid] = s
        return paramiko.OPEN_SUCCEEDED

    def check_port_forward_request(self, address, port):
        if self.srv.deny_forwarding:
            return False
        ls = socket.create_server(("127.0.0.1", port))
        bound = ls.getsockname()[1]
        self.srv.remote_listeners[bound] = ls

        def accept():
            while True:
                try:
                    s, peer = ls.accept()
                except OSError:
                    return
                ch = self.t.open_forwarded_tcpip_channel(peer, (address, bound))
                _pump(s, ch)
        threading.Thread(target=accept, daemon=True).start()
        return bound

    def cancel_port_forward_request(self, address, port):
        ls = self.srv.remote_listeners.pop(port, None)
        if ls:
            ls.close()


class TestSSHServer:
    """with TestSSHServer(mode="otp") as srv: connect to 127.0.0.1:srv.port"""

    __test__ = False   # not a pytest test class

    def __init__(self, mode: str = "password", client_key: paramiko.PKey | None = None,
                 deny_forwarding: bool = False):
        self.mode = mode
        self.client_key = client_key
        self.deny_forwarding = deny_forwarding
        self.seen_responses: list[list[str]] = []
        self.commands: list[str] = []
        self.agent_requests = 0
        self.remote_listeners: dict[int, socket.socket] = {}
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._transports: list[paramiko.Transport] = []

    def __enter__(self):
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._sock.close()
        for t in self._transports:
            t.close()
        for ls in self.remote_listeners.values():
            ls.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(host_key())
            iface = _Iface(self, t)
            try:
                t.start_server(server=iface)
            except Exception:
                continue
            self._transports.append(t)
            threading.Thread(target=self._channels, args=(t, iface), daemon=True).start()

    def _channels(self, t: paramiko.Transport, iface: _Iface) -> None:
        while t.is_active():
            ch = t.accept(1)
            if ch is None:
                continue
            s = iface.direct.pop(ch.get_id(), None)
            if s:
                _pump(s, ch)
            # sessions: the shell / exec request handlers take over

    @staticmethod
    def _run_command(t, ch, command) -> None:
        """exec: `agent-keys` lists the keys of the client's forwarded agent (like
        `ssh-add -l` on a real server); anything else is echoed back."""
        import time
        time.sleep(0.1)          # let paramiko confirm the exec request before we answer
        try:
            if command == "agent-keys":
                ch.sendall(("\n".join(_forwarded_agent_keys(t)) or "NO-KEYS").encode() + b"\n")
            else:
                ch.sendall(command.encode() + b"\n")
            ch.send_exit_status(0)
        except Exception as e:
            ch.sendall(f"ERROR {e}\n".encode())
            ch.send_exit_status(1)
        ch.close()

    @staticmethod
    def _echo(ch) -> None:
        import time
        time.sleep(0.05)         # after paramiko confirmed the shell request
        try:
            ch.sendall(b"welcome\r\n$ ")
            while True:
                d = ch.recv(1024)
                if not d:
                    break
                ch.sendall(d)
        except Exception:
            pass


def echo_server() -> tuple[socket.socket, int]:
    """A local TCP echo server (target for tunnels). Returns (listening socket, port)."""
    ls = socket.create_server(("127.0.0.1", 0))

    def run():
        while True:
            try:
                s, _ = ls.accept()
            except OSError:
                return

            def one(s=s):
                try:
                    while True:
                        d = s.recv(65536)
                        if not d:
                            break
                        s.sendall(d)
                finally:
                    s.close()
            threading.Thread(target=one, daemon=True).start()
    threading.Thread(target=run, daemon=True).start()
    return ls, ls.getsockname()[1]
