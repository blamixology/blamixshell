"""Following the terminal to another machine through the first server's own ssh.

You typed `ssh linxstress` in a server's shell. BlamixShell can't use that connection (it lives inside the remote
shell), and often has no login for LinxStress itself: your key for it may be on the first server. So it asks the first
server to connect for it, the same way you did. SFTP runs `ssh … linxstress -s sftp` there, and each dashboard command
runs `ssh … linxstress '<command>'`. That uses that server's keys, ~/.ssh/config and known_hosts, and one reused
connection (OpenSSH ControlMaster), so it stays quick. It never asks for a password (BatchMode): when the machine
wants one, the relay isn't offered and the Files panel suggests opening the machine as a server of its own.

RelayTransport stands in for a paramiko Transport, so everything built on `client.get_transport().open_session()`
(the dashboard's Runner, SFTP, the editor) works unchanged. No Qt here."""
from __future__ import annotations

import shlex
import threading
from types import SimpleNamespace

from .hops import Hop

# never prompt (no terminal to answer in), give up quickly, and share one connection between all the commands
BATCH = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
         "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/.bxs-%C", "-o", "ControlPersist=180"]
MARK = "bxs-relay-ok"


def ssh_words(hop: Hop, subsystem: str = "", command: str | None = None) -> list[str]:
    words = (["sudo", "-n"] if hop.sudo else []) + ["ssh", *BATCH, *hop.options]
    if subsystem:
        words += ["-s", hop.dest or hop.host, subsystem]
    else:
        words += [hop.dest or hop.host]
        if command is not None:
            words.append(command)
    return words


def wrap(hop: Hop, command: str | None = None, subsystem: str = "") -> str:
    """The command the machine before `hop` runs to reach it (~/.ssh exists for the shared connection's socket)."""
    return "mkdir -p ~/.ssh 2>/dev/null; exec " + shlex.join(ssh_words(hop, subsystem, command))


class RelayChannel:
    """A real channel on the machine before; exec_command / invoke_subsystem go one machine further."""

    def __init__(self, chan, hop: Hop):
        self._chan = chan
        self._hop = hop

    def exec_command(self, command: str) -> None:
        self._chan.exec_command(wrap(self._hop, command))

    def invoke_subsystem(self, name: str) -> None:
        self._chan.exec_command(wrap(self._hop, subsystem=name))

    def invoke_shell(self) -> None:
        raise NotImplementedError("The terminal itself is already on that machine.")

    def get_pty(self, *a, **k) -> None:
        pass                                   # nothing here needs a terminal (and BatchMode can't answer prompts)

    def __getattr__(self, name):
        return getattr(self._chan, name)


class RelayTransport:
    def __init__(self, parent, hop: Hop):
        self._parent = parent                  # a paramiko Transport, or another RelayTransport (A -> B -> C)
        self.hop = hop

    def open_session(self, *a, **k):
        return RelayChannel(self._parent.open_session(*a, **k), self.hop)

    def is_active(self) -> bool:
        return self._parent.is_active()

    def __getattr__(self, name):
        return getattr(self._parent, name)


class RelayClient:
    """Enough of paramiko.SSHClient for BlamixShell's tools."""

    def __init__(self, transport: RelayTransport):
        self._transport = transport

    def get_transport(self) -> RelayTransport:
        return self._transport

    def exec_command(self, command: str, bufsize: int = -1, timeout: float | None = None, get_pty: bool = False,
                     environment=None):
        chan = self._transport.open_session(timeout=timeout)
        chan.settimeout(timeout)
        chan.exec_command(command)
        return chan.makefile_stdin("wb", bufsize), chan.makefile("r", bufsize), chan.makefile_stderr("r", bufsize)

    def close(self) -> None:
        pass                                   # the connection belongs to the first server's session


class RelaySession:
    """The machine at the end of a terminal's ssh chain, for the Files panel, the editor and the dashboard."""

    def __init__(self, home_session, chain: list[Hop]):
        self.home = home_session
        self.chain = list(chain)
        transport = home_session.client.get_transport()
        for hop in self.chain:
            transport = RelayTransport(transport, hop)
        self.client = RelayClient(transport)
        hop = self.chain[-1]
        home = home_session.server
        through = " → ".join([home.label] + [h.label for h in self.chain[:-1]])
        self.server = SimpleNamespace(
            id=f"{home.id}>" + ">".join(h.label for h in self.chain), label=f"{hop.label} (via {through})",
            username=hop.user, host=hop.host, port=hop.port, address=f"{hop.user + '@' if hop.user else ''}{hop.host}",
            uses_ssh=True, group=getattr(home, "group", ""), jump_id="", color="", tunnels=[])
        self.sftp_client = None
        self.sftp_cwd = ""
        self.sftp_noise = ""
        self.files_host = ""
        self._lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return bool(getattr(self.home, "is_connected", False))

    def sftp(self):
        from .ssh_core import open_sftp
        with self._lock:
            if self.sftp_client is None or self.sftp_client.sock.closed:
                self.sftp_client, self.sftp_noise = open_sftp(self.client)
                self.sftp_client.get_channel().settimeout(30)
                self.sftp_cwd = self.sftp_client.normalize(".")
            return self.sftp_client

    def reset_sftp(self) -> None:
        with self._lock:
            if self.sftp_client is not None:
                try:
                    self.sftp_client.close()
                except Exception:
                    pass
            self.sftp_client = None

    def close(self) -> None:
        self.reset_sftp()

    # ---- can it get there without a password?
    def probe(self, timeout: float = 25) -> tuple[bool, str]:
        """(works, why not). Runs `echo` and `hostname` on the machine; quick when the shared connection is up."""
        try:
            _i, out, err = self.client.exec_command(f"echo {MARK}; hostname 2>/dev/null || uname -n", timeout=timeout)
            text = out.read().decode("utf-8", "replace")
            problem = err.read().decode("utf-8", "replace")
        except Exception as e:
            return False, str(e) or e.__class__.__name__
        if MARK in text:
            lines = [x.strip() for x in text.split(MARK, 1)[1].splitlines() if x.strip()]
            self.files_host = lines[0][:64] if lines else self.chain[-1].label
            return True, ""
        return False, explain(problem)


def explain(stderr: str) -> str:
    """ssh's complaint, in a sentence."""
    t = (stderr or "").strip()
    low = t.lower()
    if "permission denied" in low:
        return "it asks for a password there (or the key isn't accepted)"
    if "host key verification failed" in low:
        return "the machine's host key isn't known yet on the server you went through"
    if "a password is required" in low or "sudo:" in low:
        return "the ssh was run with sudo, and sudo asks for a password"
    if "could not resolve" in low or "name or service not known" in low:
        return "the name doesn't resolve on the server you went through"
    if "timed out" in low or "connection refused" in low or "no route" in low:
        return "that machine can't be reached from the server you went through"
    line = t.splitlines()[-1] if t else "no answer"
    return line[:160]


def resolved(home_client, hop: Hop, timeout: float = 15) -> dict:
    """What ssh on the machine before would really use for `hop` (`ssh -G`): hostname, port, user, identityfile …
    For filling in the New server form when the relay can't be used. {} when it can't tell."""
    words = (["sudo", "-n"] if hop.sudo else []) + ["ssh", "-G", *hop.options, hop.dest or hop.host]
    try:
        chan = home_client.get_transport().open_session(timeout=timeout)   # any connection, also a relayed one
        chan.settimeout(timeout)
        chan.exec_command(shlex.join(words) + " 2>/dev/null")
        text = chan.makefile("r", -1).read().decode("utf-8", "replace")
    except Exception:
        return {}
    got: dict = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        if key in ("hostname", "port", "user", "identityfile", "proxyjump") and value:
            got.setdefault(key, value.strip())
    return got
