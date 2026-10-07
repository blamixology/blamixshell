"""Open a server from the command line: `blamixshell gui --connect <server or user@host:port> [--key FILE]
[--jump SERVER]` (also `BlamixShell.exe --connect …`). If BlamixShell is already open, the request goes to that window
over a local socket (only this user can reach it) instead of opening a second one. Never a password on the command
line: the server's saved login is used, or the New server dialog asks.

The parsing and matching here have no Qt; the socket parts import QtNetwork when used."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sys

from .models import Server


def parse_args(argv: list[str]) -> dict | None:
    """The GUI's own command line (Qt's options pass through). None when nothing is asked."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--connect", "-c")
    p.add_argument("--key")
    p.add_argument("--jump")
    a, _rest = p.parse_known_args(argv)
    if not a.connect or not a.connect.strip():
        return None
    return request(a.connect, a.key or "", a.jump or "")


def request(target: str, key: str = "", jump: str = "") -> dict:
    return {"action": "connect", "target": target.strip(), "key": key.strip(), "jump": jump.strip()}


def split_target(text: str) -> tuple[str, str, int | None]:
    """"user@host:port", "host:port", "user@host", "host", "[::1]:2222" -> (user, host, port or None)."""
    t = text.strip()
    if t.startswith("ssh://"):
        t = t[6:].rstrip("/")
    user = ""
    if "@" in t:
        user, t = t.rsplit("@", 1)
    port = None
    if t.startswith("[") and "]" in t:                      # [IPv6]:port
        host, _, rest = t[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
        return user, host, port
    if t.count(":") == 1:
        host, p = t.split(":")
        if p.isdigit():
            return user, host, int(p)
    return user, t, port


def _by_name(store, text: str):
    t = text.strip().lower()
    if not t:
        return None
    if text in store.servers:
        return store.servers[text]
    hits = [s for s in store.servers.values() if s.name.lower() == t] or \
           [s for s in store.servers.values() if s.label.lower() == t]
    return hits[0] if len(hits) == 1 else None


def resolve(store, req: dict) -> tuple[str, object, str]:
    """What to do with a request: ("open", server_id, note) for a saved server, ("new", Server, note) to show the New
    server dialog prefilled, or ("error", None, message)."""
    target = req.get("target", "")
    if not target:
        return "error", None, "No server given."
    jump_id, note = "", ""
    if req.get("jump"):
        j = _by_name(store, req["jump"])
        if j is None:
            user, host, port = split_target(req["jump"])
            j = next((s for s in store.servers.values() if s.host.lower() == host.lower()
                      and (port is None or s.port == port) and (not user or s.username == user)), None)
        if j is None:
            note = f"Jump host “{req['jump']}” isn't a saved server, so it wasn't set."
        else:
            jump_id = j.id
    saved = _by_name(store, target)
    if saved is not None:
        return "open", saved.id, note
    user, host, port = split_target(target)
    if not host or " " in host or "/" in host:
        return "error", None, f"“{target}” isn't a saved server or a user@host:port address."
    same = [s for s in store.servers.values() if s.host.lower() == host.lower() and s.port == (port or 22)
            and (not user or s.username == user) and (not jump_id or s.jump_id == jump_id)]
    if same:                                   # a saved server: its own login is used (no duplicate entries)
        pick = max(same, key=lambda s: s.last_connected)
        notes = [note] if note else []
        if len(same) > 1:
            notes.append(f"{len(same)} saved servers match; opened the one used last ({pick.label}).")
        if req.get("key") and req["key"] != pick.key_path:
            notes.append(f"Opened the saved server {pick.label} with its own login (--key was not used).")
        return "open", pick.id, " ".join(notes)
    s = Server(host=host, port=port or 22, username=user, auth="key" if req.get("key") else "password",
               key_path=req.get("key", ""), jump_id=jump_id)
    return "new", s, note


# ---------------------------------------------------------------- the local socket
def socket_name() -> str:
    """One name per user and data folder (a portable copy with its own data is its own instance)."""
    from .paths import data_dir
    tag = hashlib.sha256(f"{getpass.getuser()}|{data_dir()}".encode()).hexdigest()[:16]
    return f"blamixshell-{tag}"


def send(req: dict, timeout_ms: int = 1500) -> bool:
    """Give the request to a BlamixShell that is already open. False when none answers."""
    from PySide6.QtNetwork import QLocalSocket
    if sys.platform == "win32":                # let the open window come to the front
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
    sock = QLocalSocket()
    sock.connectToServer(socket_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(json.dumps(req).encode() + b"\n")
    sock.flush()
    ok = sock.waitForBytesWritten(timeout_ms) and sock.waitForReadyRead(timeout_ms * 2) and \
        bytes(sock.readAll()).startswith(b"ok")
    sock.disconnectFromServer()
    return ok


def listen(on_request):
    """Start taking requests from later launches; on_request(dict) runs in the GUI thread. Returns the server (keep
    a reference) or None if it couldn't listen."""
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    name = socket_name()
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(500):                         # another BlamixShell is open and keeps the name
        probe.disconnectFromServer()                        # (Windows would let both listen on one pipe name)
        return None
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(name):
        QLocalServer.removeServer(name)                     # left over from a crash
        if not server.listen(name):
            return None

    def accept():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            buf = bytearray()

            def read(conn=conn, buf=buf):
                buf.extend(bytes(conn.readAll()))
                if b"\n" not in buf:
                    if len(buf) > 65536:
                        conn.disconnectFromServer()
                    return
                line = bytes(buf).split(b"\n", 1)[0]
                try:
                    req = json.loads(line.decode("utf-8"))
                    if not isinstance(req, dict) or req.get("action") != "connect":
                        raise ValueError
                except ValueError:
                    conn.write(b"bad\n")
                    conn.flush()
                    conn.disconnectFromServer()
                    return
                conn.write(b"ok\n")
                conn.flush()
                conn.disconnectFromServer()
                on_request(req)
            conn.readyRead.connect(read)
            conn.disconnected.connect(conn.deleteLater)
            if conn.bytesAvailable():
                read()
    server.newConnection.connect(accept)
    return server
