"""Qt-free SSH core: host-key checks, auth (incl. 2FA prompts), bastion hops, tunnels.

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
                "Conversions → Export OpenSSH key, then point BlamixShell at the exported file.")
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


# A server prompt during keyboard-interactive login: (text, echo). The handler gets
# (title, instructions, prompts) and returns one answer per prompt, or None to cancel.
Prompt = tuple[str, bool]
InteractiveHandler = Callable[[str, str, list[Prompt]], "list[str] | None"]


class AuthCancelled(paramiko.AuthenticationException):
    pass


class NeedsInput(paramiko.AuthenticationException):
    """The server asked for something (e.g. a 2FA code) and no handler was given."""

    def __init__(self, prompts: list[str]):
        super().__init__("The server asks for: " + ", ".join(p.strip() for p in prompts))
        self.prompts = prompts


def _is_password_prompt(text: str) -> bool:
    t = text.strip().lower()
    return "password" in t and "new" not in t and "code" not in t and "otp" not in t


def _default_key_files() -> list[paramiko.PKey]:
    """Unencrypted ~/.ssh/id_* keys (like ssh does when no key is configured)."""
    import os
    keys = []
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        for d in (".ssh", "ssh"):
            path = os.path.expanduser(f"~/{d}/{name}")
            if os.path.isfile(path):
                try:
                    keys.append(paramiko.PKey.from_path(path))
                except Exception:
                    pass   # encrypted or unreadable: the agent may still have it
    return keys


class _Client(paramiko.SSHClient):
    """SSHClient with an auth sequence that supports 2FA / keyboard-interactive
    prompts through a callback (paramiko's own falls back to stdin input())."""

    interactive: InteractiveHandler | None = None

    def _auth(self, username, password, pkey, key_filenames, allow_agent,
              look_for_keys, passphrase):  # noqa: PLR0912 (it's a sequence of fallbacks)
        t = self._transport
        saved: Exception | None = None

        # what does the server accept? ("none" is refused by nearly every server)
        try:
            t.auth_none(username)
            if t.is_authenticated():
                return
            allowed = ["publickey", "password", "keyboard-interactive"]
        except paramiko.BadAuthenticationType as e:
            allowed = list(e.allowed_types)
        except paramiko.SSHException:
            allowed = ["publickey", "password", "keyboard-interactive"]

        # 1) keys: configured key, agent keys, default key files
        keys: list[paramiko.PKey] = [pkey] if pkey is not None else []
        if allow_agent:
            try:
                keys += list(paramiko.Agent().get_keys())
            except Exception:
                pass
        if look_for_keys:
            keys += _default_key_files()
        if "publickey" in allowed:
            for key in keys:
                try:
                    remaining = t.auth_publickey(username, key)
                except paramiko.AuthenticationException as e:
                    saved = e
                    continue
                except paramiko.SSHException as e:
                    saved = e
                    continue
                if t.is_authenticated():
                    return
                allowed = list(remaining)   # key accepted, a second factor is required
                break

        # 2) password
        if password and "password" in allowed:
            try:
                remaining = t.auth_password(username, password, fallback=False)
                if t.is_authenticated():
                    return
                allowed = list(remaining)
            except paramiko.BadAuthenticationType as e:
                allowed = list(e.allowed_types)
            except paramiko.AuthenticationException as e:
                saved = e

        # 3) keyboard-interactive: fill password prompts with the saved password
        #    (once, so a wrong one is asked again), ask the user for everything else
        if "keyboard-interactive" in allowed and (self.interactive or password):
            state = {"pw_used": False, "cancelled": False, "unanswered": []}
            ask = self.interactive

            def handler(title, instructions, prompts):
                answers: list[str | None] = []
                for text, _echo in prompts:
                    if password and not state["pw_used"] and _is_password_prompt(text):
                        answers.append(password)
                        state["pw_used"] = True
                    else:
                        answers.append(None)
                missing = [(p[0], p[1]) for p, a in zip(prompts, answers) if a is None]
                if missing:
                    if not ask:
                        state["unanswered"] += [m[0] for m in missing]
                        return [a or "" for a in answers]
                    got = ask(title or "", instructions or "", missing)
                    if got is None:
                        state["cancelled"] = True
                        return ["" for _ in prompts]
                    it = iter(got)
                    answers = [a if a is not None else next(it, "") for a in answers]
                return answers
            try:
                t.auth_interactive(username, handler)
                if t.is_authenticated():
                    return
            except paramiko.AuthenticationException as e:
                saved = e
            if state["cancelled"]:
                raise AuthCancelled("Login cancelled.")
            other = [q for q in state["unanswered"] if not _is_password_prompt(q)]
            if other:   # (a repeated password prompt just means the password was wrong)
                raise NeedsInput(other)

        if saved is not None:
            raise saved
        raise paramiko.AuthenticationException(
            "No supported authentication method (server offers: " + ", ".join(allowed) + ")")


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
                _depth: int = 0, interactive: InteractiveHandler | None = None,
                ) -> tuple[paramiko.SSHClient, list[paramiko.SSHClient]]:
    """Connect (through bastions if configured). Returns (client, [bastion clients]).
    `interactive` answers keyboard-interactive prompts (2FA codes etc.); it gets the
    server's prompts and returns the answers, or None to cancel."""
    if _depth > 4:
        raise AuthConfigError("Jump host chain is too long (loop?)")
    sock = None
    chain: list[paramiko.SSHClient] = []
    if server.jump_id:
        jump = resolve(server.jump_id)
        if not jump:
            raise AuthConfigError("The configured jump host no longer exists")
        log(f"via jump host {jump.label} …")
        jclient, jchain = open_client(jump, resolve, log, _depth + 1, interactive)
        chain = jchain + [jclient]
        sock = jclient.get_transport().open_channel(
            "direct-tcpip", (server.host, int(server.port)), ("127.0.0.1", 0), timeout=15)

    client = _Client()
    client.interactive = interactive
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


# ---------------------------------------------------------------- shells / commands
def local_agent_keys() -> int:
    """Number of keys in the local SSH agent (Pageant / Windows OpenSSH agent / ssh-agent)."""
    try:
        agent = paramiko.Agent()
        n = len(agent.get_keys())
        agent.close()
        return n
    except Exception:
        return 0


def _session(client: paramiko.SSHClient, agent_forward: bool) -> paramiko.Channel:
    chan = client.get_transport().open_session(timeout=15)
    if agent_forward:
        from paramiko.agent import AgentRequestHandler
        # ssh -A: the server may use the local agent's keys while this session is open
        chan._agent_handler = AgentRequestHandler(chan)   # keep it alive with the channel
    return chan


def open_shell(client: paramiko.SSHClient, server: Server, term: str = "xterm-256color",
               width: int = 120, height: int = 32) -> paramiko.Channel:
    """Interactive shell (with agent forwarding when the server has it enabled)."""
    chan = _session(client, server.agent_forward)
    chan.get_pty(term=term, width=width, height=height)
    chan.invoke_shell()
    return chan


def exec_command(client: paramiko.SSHClient, command: str, agent_forward: bool = False,
                 timeout: float | None = None, get_pty: bool = False):
    """Like SSHClient.exec_command, plus optional agent forwarding.
    Returns (stdin, stdout, stderr) file objects."""
    chan = _session(client, agent_forward)
    if get_pty:
        chan.get_pty()
    chan.settimeout(timeout)
    chan.exec_command(command)
    return chan.makefile_stdin("wb", -1), chan.makefile("r", -1), chan.makefile_stderr("r", -1)


# ---------------------------------------------------------------- sftp
_VERSION_REPLY = b"\x02\x00\x00\x00\x03"   # SSH_FXP_VERSION, protocol version 3


class _SkipLoginNoise:
    """Wraps an SFTP channel and drops any text a login script (~/.bashrc, ~/.profile,
    motd hacks) printed before the SFTP handshake. Without this, paramiko fails with
    "Garbage packet received" (OpenSSH's sftp says "Received message too long")."""

    def __init__(self, chan):
        self._chan = chan
        self._buf = b""
        self._synced = False
        self.skipped = b""

    def __getattr__(self, name):
        return getattr(self._chan, name)

    def recv(self, n: int) -> bytes:
        if not self._synced:
            self._sync()
        if self._buf:
            out, self._buf = self._buf[:n], self._buf[n:]
            return out
        return self._chan.recv(n)

    def _sync(self) -> None:
        data = b""
        while True:
            i = data.find(_VERSION_REPLY)
            while i != -1:
                if i >= 4 and 5 <= int.from_bytes(data[i - 4:i], "big") <= 65536:
                    self.skipped, self._buf, self._synced = data[:i - 4], data[i - 4:], True
                    return
                i = data.find(_VERSION_REPLY, i + 1)
            if len(data) > 256 * 1024:
                raise paramiko.SFTPError("No SFTP handshake from the server (is the SFTP subsystem enabled?)")
            chunk = self._chan.recv(32768)
            if not chunk:
                raise EOFError("The server closed the SFTP channel (is the SFTP subsystem enabled?)")
            data += chunk


def open_sftp(client: paramiko.SSHClient) -> tuple[paramiko.SFTPClient, str]:
    """Open SFTP on an existing connection, tolerating login-script output.
    Returns (sftp client, text the server printed before the handshake)."""
    chan = client.get_transport().open_session(timeout=15)
    chan.invoke_subsystem("sftp")
    wrapped = _SkipLoginNoise(chan)
    sftp = paramiko.SFTPClient(wrapped)
    return sftp, wrapped.skipped.decode("utf-8", "replace").strip()


def friendly_error(e: Exception) -> str:
    if isinstance(e, (AuthCancelled, NeedsInput)):
        return str(e)
    if isinstance(e, paramiko.AuthenticationException):
        msg = str(e)
        if msg.startswith("No supported authentication method"):
            return msg + "."
        return "Authentication failed: check username, password, key or code."
    if isinstance(e, socket.timeout) or isinstance(e, TimeoutError):
        return "Connection timed out."
    if isinstance(e, socket.gaierror):
        return "Host name could not be resolved."
    if isinstance(e, ConnectionRefusedError):
        return "Connection refused (is SSH running on that port?)."
    if isinstance(e, paramiko.SSHException):
        return f"SSH error: {e}"
    if isinstance(e, EOFError):
        return str(e) or "The server closed the channel."
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
    except NeedsInput as e:
        return (f"OK. The server also asks for “{e.prompts[0].strip().rstrip(':')}”; "
                "you'll be prompted when connecting.")
    except AuthConfigError as e:
        return str(e)
    except Exception as e:
        return friendly_error(e)
