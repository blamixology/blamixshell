"""Qt-free SSH core: host-key checks, auth, bastion hops.

Shared by the desktop GUI, the CLI and the TUI (so those run on headless Linux
without PySide6 installed).
"""
from __future__ import annotations

import io
import socket
import threading
from typing import Callable

import paramiko

from .models import Server
from .paths import known_hosts_path


# ---------------------------------------------------------------- host keys
class UnknownHostKey(Exception):
    def __init__(self, host_id: str, key: paramiko.PKey):
        super().__init__(f"Unknown host key for {host_id}")
        self.host_id = host_id
        self.key = key


class ChangedHostKey(Exception):
    def __init__(self, host_id: str, key: paramiko.PKey, expected: paramiko.PKey):
        super().__init__(f"HOST KEY CHANGED for {host_id}")
        self.host_id = host_id
        self.key = key
        self.expected = expected


class _AskPolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        raise UnknownHostKey(hostname, key)


def fingerprint(key: paramiko.PKey) -> str:
    return key.fingerprint  # "SHA256:..." in paramiko >= 3


def host_id(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


_kh_lock = threading.Lock()


def load_known_hosts() -> paramiko.HostKeys:
    hk = paramiko.HostKeys()
    p = known_hosts_path()
    if p.exists():
        try:
            hk.load(str(p))
        except Exception:
            pass
    return hk


def trust_host_key(hid: str, key: paramiko.PKey, replace: bool = False) -> None:
    with _kh_lock:
        hk = load_known_hosts()
        if replace and hid in hk:
            del hk[hid]
        hk.add(hid, key.get_name(), key)
        hk.save(str(known_hosts_path()))


# ---------------------------------------------------------------- auth
class AuthConfigError(Exception):
    pass


def load_private_key(path: str = "", data: str = "", passphrase: str = "") -> paramiko.PKey:
    pw = passphrase or None
    if path:
        if path.lower().endswith(".ppk"):
            raise AuthConfigError(
                "PuTTY .ppk keys aren't supported directly. Open the key in PuTTYgen and use "
                "Conversions → Export OpenSSH key, then point ShellDeck at the exported file.")
        try:
            return paramiko.PKey.from_path(path, password=pw.encode() if pw else None)
        except paramiko.PasswordRequiredException:
            raise AuthConfigError("This key is encrypted: enter its passphrase.") from None
        except FileNotFoundError:
            raise AuthConfigError(f"Key file not found: {path}") from None
        except (TypeError, ValueError) as e:
            msg = str(e).lower()
            if "password" in msg or "encrypted" in msg or "decrypt" in msg:
                raise AuthConfigError("This key is encrypted: enter its passphrase."
                                      if not pw else "Wrong passphrase for this key.") from None
            raise AuthConfigError(f"Could not read key: {e}") from None
    if data:
        if "PuTTY-User-Key-File" in data:
            raise AuthConfigError("That's a PuTTY .ppk key. Export it as OpenSSH from PuTTYgen first.")
        last: Exception | None = None
        for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return cls.from_private_key(io.StringIO(data.strip() + "\n"), password=pw)
            except paramiko.PasswordRequiredException:
                raise AuthConfigError("This key is encrypted: enter its passphrase.") from None
            except Exception as e:  # try the next type
                last = e
        raise AuthConfigError(f"Could not read the pasted private key ({last})")
    raise AuthConfigError("No private key configured")


def _connect_kwargs(server: Server) -> dict:
    kw: dict = dict(username=server.username or None, timeout=12, banner_timeout=20,
                    auth_timeout=25, allow_agent=False, look_for_keys=False)
    if server.auth == "password":
        kw["password"] = server.password
    elif server.auth == "key":
        kw["pkey"] = load_private_key(server.key_path, server.key_data, server.passphrase)
    elif server.auth == "agent":
        kw["allow_agent"] = True
        kw["look_for_keys"] = True
    return kw


def open_client(server: Server, resolve: Callable[[str], Server | None],
                log: Callable[[str], None] = lambda m: None,
                _depth: int = 0) -> tuple[paramiko.SSHClient, list[paramiko.SSHClient]]:
    """Connect (through bastions if configured). Returns (client, [bastion clients])."""
    if _depth > 4:
        raise AuthConfigError("Jump host chain is too long (loop?)")
    sock = None
    chain: list[paramiko.SSHClient] = []
    if server.jump_id:
        jump = resolve(server.jump_id)
        if not jump:
            raise AuthConfigError("The configured jump host no longer exists")
        log(f"via jump host {jump.label} …")
        jclient, jchain = open_client(jump, resolve, log, _depth + 1)
        chain = jchain + [jclient]
        sock = jclient.get_transport().open_channel(
            "direct-tcpip", (server.host, int(server.port)), ("127.0.0.1", 0), timeout=15)

    client = paramiko.SSHClient()
    client._host_keys = load_known_hosts()       # read-only view; we save via trust_host_key
    client.set_missing_host_key_policy(_AskPolicy())
    log(f"connecting to {server.host}:{server.port} …")
    try:
        client.connect(server.host, port=int(server.port), sock=sock, **_connect_kwargs(server))
    except paramiko.BadHostKeyException as e:
        client.close()
        for c in chain:
            c.close()
        raise ChangedHostKey(host_id(server.host, server.port), e.key, e.expected_key) from None
    except UnknownHostKey as e:
        client.close()
        for c in chain:
            c.close()
        # paramiko reports the bare host; normalise to known_hosts format
        raise UnknownHostKey(host_id(server.host, server.port), e.key) from None
    except Exception:
        client.close()
        for c in chain:
            c.close()
        raise
    tr = client.get_transport()
    if tr and server.keepalive:
        tr.set_keepalive(int(server.keepalive))
    return client, chain


def friendly_error(e: Exception) -> str:
    if isinstance(e, paramiko.AuthenticationException):
        return "Authentication failed: check username, password or key."
    if isinstance(e, socket.timeout) or isinstance(e, TimeoutError):
        return "Connection timed out."
    if isinstance(e, socket.gaierror):
        return "Host name could not be resolved."
    if isinstance(e, ConnectionRefusedError):
        return "Connection refused (is SSH running on that port?)."
    if isinstance(e, paramiko.SSHException):
        return f"SSH error: {e}"
    if isinstance(e, OSError) and e.strerror:
        return e.strerror
    return str(e) or e.__class__.__name__


def test_connection(server: Server, resolve) -> str:
    """Blocking connectivity check used by the server dialog. Returns '' on success."""
    try:
        client, chain = open_client(server, resolve)
        client.close()
        for c in chain:
            c.close()
        return ""
    except UnknownHostKey as e:
        return f"OK. Host key not yet trusted ({e.key.get_name()} {fingerprint(e.key)}); you'll be asked on first connect."
    except ChangedHostKey:
        return "WARNING: the server's host key has CHANGED since you last connected."
    except AuthConfigError as e:
        return str(e)
    except Exception as e:
        return friendly_error(e)
