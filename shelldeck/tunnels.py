"""Port forwarding over an open SSH connection (Qt-free: used by the GUI and the CLI).

  L  local    a socket here  -> "direct-tcpip" channel -> dest as seen by the server
  D  dynamic  a SOCKS4/4a/5 proxy here; each client picks its own destination
  R  remote   the server listens ("tcpip-forward") -> connection to dest as seen from here
"""
from __future__ import annotations

import ipaddress
import socket
import struct
import threading
from dataclasses import dataclass
from typing import Callable

import paramiko

from .models import Tunnel


@dataclass
class TunnelState:
    tunnel: Tunnel
    ok: bool = False
    error: str = ""
    port: int = 0            # the port actually bound (useful when 0 was requested for -R)
    connections: int = 0     # currently open connections through this tunnel
    total: int = 0

    def summary(self) -> str:
        t = self.tunnel
        if not self.ok:
            return f"{t.describe()}  ✖ {self.error}"
        live = f"  ·  {self.connections} open" if self.connections else ""
        if t.kind == "R" and not t.listen_port:
            return f"{t.describe()} (server port {self.port}){live}"
        return f"{t.describe()}{live}"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client closed the connection during the SOCKS handshake")
        buf += chunk
    return buf


def socks_handshake(sock: socket.socket) -> tuple[tuple[str, int], Callable[[bool], None]]:
    """Read a SOCKS4/4a/5 CONNECT request. Returns ((host, port), reply) where
    reply(ok) sends the success/failure answer to the client."""
    ver = _recv_exact(sock, 1)[0]
    if ver == 5:
        methods = _recv_exact(sock, _recv_exact(sock, 1)[0])
        if 0 not in methods:
            sock.sendall(b"\x05\xff")
            raise ConnectionError("SOCKS client wants authentication (not supported)")
        sock.sendall(b"\x05\x00")
        _v, cmd, _rsv, atyp = _recv_exact(sock, 4)
        if atyp == 1:
            host = socket.inet_ntoa(_recv_exact(sock, 4))
        elif atyp == 3:
            host = _recv_exact(sock, _recv_exact(sock, 1)[0]).decode("idna")
        elif atyp == 4:
            host = str(ipaddress.IPv6Address(_recv_exact(sock, 16)))
        else:
            sock.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
            raise ConnectionError("unsupported SOCKS address type")
        port = struct.unpack("!H", _recv_exact(sock, 2))[0]
        if cmd != 1:
            sock.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            raise ConnectionError("only SOCKS CONNECT is supported")

        def reply(ok: bool) -> None:
            sock.sendall((b"\x05\x00" if ok else b"\x05\x05") + b"\x00\x01" + b"\x00" * 6)
        return (host, port), reply
    if ver == 4:
        cmd = _recv_exact(sock, 1)[0]
        port = struct.unpack("!H", _recv_exact(sock, 2))[0]
        ip = _recv_exact(sock, 4)

        def read_cstr() -> bytes:
            out = b""
            while (ch := _recv_exact(sock, 1)) != b"\x00":
                out += ch
                if len(out) > 255:
                    raise ConnectionError("SOCKS4 field too long")
            return out
        read_cstr()                                      # user id (ignored)
        host = read_cstr().decode("idna") if ip[:3] == b"\x00\x00\x00" and ip[3] else socket.inet_ntoa(ip)

        def reply(ok: bool) -> None:
            sock.sendall(b"\x00" + (b"\x5a" if ok else b"\x5b") + b"\x00" * 6)
        if cmd != 1:
            reply(False)
            raise ConnectionError("only SOCKS CONNECT is supported")
        return (host, port), reply
    raise ConnectionError(f"not a SOCKS request (first byte {ver})")


class TunnelManager:
    """Runs a server's tunnels on one connected transport until stop()."""

    def __init__(self, transport: paramiko.Transport, tunnels: list[Tunnel],
                 log: Callable[[str], None] = lambda m: None,
                 on_change: Callable[[], None] = lambda: None):
        self.transport = transport
        self.states = [TunnelState(t) for t in tunnels if t.enabled]
        self._log = log
        self._on_change = on_change
        self._listeners: list[socket.socket] = []
        self._remote: dict[int, TunnelState] = {}     # server port -> state
        self._open: set = set()                        # sockets/channels to close on stop()
        self._lock = threading.Lock()
        self._stopped = False

    # ------------------------------------------------------------------ control
    def start(self) -> list[TunnelState]:
        """Open every enabled tunnel. Failures don't raise: see each state's .ok/.error.
        `log` only receives problems that happen later (a refused connection etc.)."""
        for st in self.states:
            problem = st.tunnel.problem()
            if problem:
                st.error = problem
                continue
            try:
                if st.tunnel.kind == "R":
                    self._start_remote(st)
                else:
                    self._start_listener(st)
                st.ok = True
            except Exception as e:
                st.error = _friendly(e)
        self._on_change()
        return self.states

    def stop(self) -> None:
        self._stopped = True
        for s in self._listeners:
            _close(s)
        remote = [(st.tunnel.listen_host, port) for port, st in self._remote.items()]
        self._remote.clear()
        if remote:
            # waits for the server's reply: never block the caller (UI thread) on it
            def cancel():
                for host, port in remote:
                    try:
                        self.transport.cancel_port_forward(host, port)
                    except Exception:
                        pass
            threading.Thread(target=cancel, daemon=True).start()
        with self._lock:
            items = list(self._open)
            self._open.clear()
        for obj in items:
            _close(obj)
        for st in self.states:
            st.ok, st.connections = False, 0

    @property
    def active(self) -> list[TunnelState]:
        return [s for s in self.states if s.ok]

    # ------------------------------------------------------------------ L and D
    def _start_listener(self, st: TunnelState) -> None:
        t = st.tunnel
        host = t.listen_host or "127.0.0.1"
        if host == "localhost":
            host = "127.0.0.1"
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        srv = socket.create_server((host, int(t.listen_port)), family=fam, backlog=32)
        st.port = srv.getsockname()[1]
        self._listeners.append(srv)
        threading.Thread(target=self._accept_loop, args=(srv, st), daemon=True,
                         name=f"tunnel-{t.kind}-{st.port}").start()

    def _accept_loop(self, srv: socket.socket, st: TunnelState) -> None:
        while not self._stopped:
            try:
                sock, peer = srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle_local, args=(sock, peer, st), daemon=True).start()

    def _handle_local(self, sock: socket.socket, peer, st: TunnelState) -> None:
        t = st.tunnel
        reply = None
        try:
            if t.kind == "D":
                sock.settimeout(15)
                dest, reply = socks_handshake(sock)
                sock.settimeout(None)
            else:
                dest = (t.dest_host, int(t.dest_port))
            chan = self.transport.open_channel("direct-tcpip", dest, peer[:2], timeout=15)
        except Exception as e:
            if reply:
                try:
                    reply(False)
                except Exception:
                    pass
            if not self._stopped:
                self._log(f"tunnel {t.describe()}: {_friendly(e)}")
            _close(sock)
            return
        if reply:
            reply(True)
        self._pump(sock, chan, st)

    # ------------------------------------------------------------------ R
    def _start_remote(self, st: TunnelState) -> None:
        t = st.tunnel
        port = self.transport.request_port_forward(t.listen_host or "", int(t.listen_port),
                                                   handler=self._on_remote)
        st.port = port
        self._remote[port] = st

    def _on_remote(self, chan, origin, server) -> None:
        # called on paramiko's transport thread: don't block it
        st = self._remote.get(server[1]) or (next(iter(self._remote.values())) if len(self._remote) == 1 else None)
        if st is None:
            _close(chan)
            return
        threading.Thread(target=self._handle_remote, args=(chan, st), daemon=True).start()

    def _handle_remote(self, chan, st: TunnelState) -> None:
        t = st.tunnel
        try:
            sock = socket.create_connection((t.dest_host, int(t.dest_port)), timeout=15)
            sock.settimeout(None)
        except Exception as e:
            self._log(f"tunnel {t.describe()}: {_friendly(e)}")
            _close(chan)
            return
        self._pump(sock, chan, st)

    # ------------------------------------------------------------------ plumbing
    def _pump(self, sock: socket.socket, chan, st: TunnelState) -> None:
        with self._lock:
            self._open.update((sock, chan))
            st.connections += 1
            st.total += 1
        self._on_change()

        def pipe(recv, send, eof):
            try:
                while True:
                    data = recv(65536)
                    if not data:
                        break
                    send(data)
            except Exception:
                pass
            try:
                eof()
            except Exception:
                pass
        back = threading.Thread(target=pipe, args=(chan.recv, sock.sendall,
                                                   lambda: sock.shutdown(socket.SHUT_WR)), daemon=True)
        back.start()
        pipe(sock.recv, chan.sendall, chan.shutdown_write)
        back.join()
        _close(sock)
        _close(chan)
        with self._lock:
            self._open.discard(sock)
            self._open.discard(chan)
            st.connections = max(0, st.connections - 1)
        self._on_change()


def _close(obj) -> None:
    try:
        obj.close()
    except Exception:
        pass


def _friendly(e: Exception) -> str:
    if isinstance(e, OSError) and getattr(e, "errno", None) in (98, 48, 10048):
        return "port already in use"
    if isinstance(e, PermissionError) or getattr(e, "errno", None) in (13, 10013):
        return "not allowed to use that port (ports below 1024 need admin rights)"
    if isinstance(e, paramiko.ChannelException):
        return f"the server refused ({e.text or 'administratively prohibited'})"
    if isinstance(e, paramiko.SSHException) and "forward" in str(e).lower():
        return "the server refused the remote forward (AllowTcpForwarding / GatewayPorts?)"
    return str(e) or e.__class__.__name__
