"""Qt wrapper around the SSH core: interactive shell on a background thread,
results delivered to the UI through Qt signals (queued across threads)."""
from __future__ import annotations

import threading
import time
from typing import Callable

import paramiko
from PySide6.QtCore import QObject, Signal

from . import aws
from .models import Server
from .ssh_core import (AuthConfigError, ChangedHostKey, UnknownHostKey,  # noqa: F401 (re-exported)
                       fingerprint, friendly_error, host_id, load_known_hosts,
                       load_private_key, local_agent_keys, open_client, open_sftp, open_shell,
                       test_connection, trust_host_key)
from .tunnels import TunnelManager


class ShellSession(QObject):
    output = Signal(bytes)
    status = Signal(str)                 # human readable progress
    connected = Signal()
    disconnected = Signal(str)           # reason ("" = clean exit)
    failed = Signal(str)
    host_key_prompt = Signal(str, str, str, bool)   # host_id, key type, fingerprint, changed?
    auth_prompt = Signal(str, str, str, object)     # server label, title, instructions, [(prompt, echo)]
    tunnels_changed = Signal(object)                # list[TunnelState]
    tunnel_message = Signal(str, bool)              # text, is_error
    aws_login_required = Signal(str)                # AWS profile whose SSO session expired

    def __init__(self, server: Server, resolve: Callable[[str], Server | None], parent=None):
        super().__init__(parent)
        self.server = server
        self._resolve = resolve
        self.client: paramiko.SSHClient | None = None
        self._chain: list[paramiko.SSHClient] = []
        self.chan: paramiko.Channel | None = None
        self._size = (120, 32)
        self._closing = False
        self._pending_key: tuple[str, paramiko.PKey] | None = None
        self.sftp_client: paramiko.SFTPClient | None = None
        self.sftp_cwd: str = ""
        self.sftp_noise: str = ""   # text a login script printed on the SFTP channel
        self.connected_at = 0.0
        self._lock = threading.Lock()
        self.tunnels: TunnelManager | None = None
        self._prompt_evt = threading.Event()
        self._prompt_answer: list[str] | None = None

    # -- lifecycle
    @property
    def is_connected(self) -> bool:
        return bool(self.chan and not self.chan.closed and self.client
                    and self.client.get_transport() and self.client.get_transport().is_active())

    def start(self, cols: int = 0, rows: int = 0) -> None:
        if cols and rows:
            self._size = (cols, rows)
        self._closing = False
        threading.Thread(target=self._run, daemon=True, name=f"ssh-{self.server.host}").start()

    def accept_host_key(self, replace: bool = False) -> None:
        if self._pending_key:
            hid, key = self._pending_key
            trust_host_key(hid, key, replace=replace)
            self._pending_key = None
            self.start()

    # -- 2FA / keyboard-interactive: called on the SSH thread, answered by the UI
    def _ask(self, title: str, instructions: str, prompts: list) -> list[str] | None:
        self._prompt_answer = None
        self._prompt_evt.clear()
        label = self.server.label
        self.auth_prompt.emit(label, title, instructions, list(prompts))
        while not self._prompt_evt.wait(0.25):
            if self._closing:
                return None
        return self._prompt_answer

    def answer_prompt(self, answers: list[str] | None) -> None:
        self._prompt_answer = answers
        self._prompt_evt.set()

    # a server's tunnels run once, on its first connected session (panes/tabs share them)
    _tunnel_owners: dict[str, "ShellSession"] = {}
    _owners_lock = threading.Lock()

    def _start_tunnels(self) -> None:
        wanted = [t for t in self.server.tunnels if t.enabled]
        if not wanted or not self.client:
            return
        with ShellSession._owners_lock:
            owner = ShellSession._tunnel_owners.get(self.server.id)
            if owner is not None and owner is not self and owner.tunnels is not None:
                self.tunnel_message.emit(f"⇄ {len(wanted)} tunnel(s) for {self.server.label} "
                                         "already run in another pane", False)
                return
            ShellSession._tunnel_owners[self.server.id] = self
        mgr = TunnelManager(self.client.get_transport(), wanted,
                            log=lambda m: self.tunnel_message.emit(m, True),   # runtime problems
                            on_change=lambda: self.tunnels_changed.emit(list(mgr.states)))
        self.tunnels = mgr
        for st in mgr.start():
            if st.ok:
                self.tunnel_message.emit("⇄ " + st.summary(), False)
            else:
                self.tunnel_message.emit(f"✖ tunnel {st.tunnel.describe()}: {st.error}", True)

    def _release_tunnels(self) -> None:
        if self.tunnels:
            self.tunnels.stop()
            self.tunnels = None
            self.tunnels_changed.emit([])
        with ShellSession._owners_lock:
            if ShellSession._tunnel_owners.get(self.server.id) is self:
                del ShellSession._tunnel_owners[self.server.id]

    def _run(self) -> None:
        try:
            self.client, self._chain = open_client(self.server, self._resolve, self.status.emit,
                                                   interactive=self._ask)
            self.status.emit("opening shell …")
            cols, rows = self._size
            chan = open_shell(self.client, self.server, width=cols, height=rows)
            if self.server.agent_forward:
                n = local_agent_keys()
                self.tunnel_message.emit(
                    f"agent forwarding on ({n} key{'s' if n != 1 else ''} in your local agent)" if n else
                    "agent forwarding on, but your local SSH agent has no keys", not n)
            chan.settimeout(None)
            self.chan = chan
            self.connected_at = time.time()
            self.connected.emit()
            self._start_tunnels()
            if self.server.startup_cmd:
                chan.send((self.server.startup_cmd.rstrip("\n") + "\n").encode())
            self._read_loop(chan)
        except UnknownHostKey as e:
            self._pending_key = (e.host_id, e.key)
            self.host_key_prompt.emit(e.host_id, e.key.get_name(), fingerprint(e.key), False)
        except ChangedHostKey as e:
            self._pending_key = (e.host_id, e.key)
            self.host_key_prompt.emit(e.host_id, e.key.get_name(), fingerprint(e.key), True)
        except AuthConfigError as e:
            self.failed.emit(str(e))
        except aws.LoginRequired as e:
            self.failed.emit(str(e))
            if e.sso and not self._closing:
                self.aws_login_required.emit(e.profile)
        except Exception as e:
            if not self._closing:
                self.failed.emit(friendly_error(e))

    def _read_loop(self, chan: paramiko.Channel) -> None:
        reason = ""
        try:
            while True:
                data = chan.recv(65536)
                if not data:
                    break
                # coalesce bursts (e.g. `cat bigfile`) to keep the UI fluid
                while chan.recv_ready() and len(data) < 512 * 1024:
                    more = chan.recv(65536)
                    if not more:
                        break
                    data += more
                self.output.emit(data)
        except Exception as e:
            reason = friendly_error(e)
        if chan.exit_status_ready():
            code = chan.recv_exit_status()
            reason = reason or ("" if code == 0 else f"shell exited with code {code}")
        elif not reason and not self._closing:
            reason = "connection closed by remote host"
        self._teardown()
        self.disconnected.emit("" if self._closing else reason)

    # -- io
    def send(self, data: bytes) -> None:
        chan = self.chan
        if chan and not chan.closed:
            try:
                chan.sendall(data)
            except Exception:
                pass

    def resize(self, cols: int, rows: int) -> None:
        if cols < 2 or rows < 2:
            return
        self._size = (cols, rows)
        chan = self.chan
        if chan and not chan.closed:
            try:
                chan.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    def sftp(self) -> paramiko.SFTPClient:
        """Open (once) an SFTP channel on the same connection. Call off the UI thread."""
        with self._lock:
            if self.sftp_client is None or self.sftp_client.sock.closed:
                if not self.client:
                    raise RuntimeError("Not connected")
                self.sftp_client, self.sftp_noise = open_sftp(self.client)
                self.sftp_client.get_channel().settimeout(30)
                self.sftp_cwd = self.sftp_client.normalize(".")
            return self.sftp_client

    def reset_sftp(self) -> None:
        """Drop a broken SFTP channel so the next operation opens a fresh one."""
        with self._lock:
            if self.sftp_client is not None:
                try:
                    self.sftp_client.close()
                except Exception:
                    pass
            self.sftp_client = None

    def _teardown(self) -> None:
        self._release_tunnels()
        for obj in [self.sftp_client, self.chan, self.client, *reversed(self._chain)]:
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self.sftp_client = None
        self.chan = None
        self.client = None
        self._chain = []

    def close(self) -> None:
        self._closing = True
        self._prompt_evt.set()
        self._release_tunnels()   # now, so a reconnect can bind the same ports right away
        threading.Thread(target=self._teardown, daemon=True).start()



class SsmShellSession(QObject):
    """A plain AWS Session Manager shell (`aws ssm start-session`) in a local terminal.
    Same signals as ShellSession; there's no SSH connection, so no SFTP, tunnels or dashboard."""

    output = Signal(bytes)
    status = Signal(str)
    connected = Signal()
    disconnected = Signal(str)
    failed = Signal(str)
    host_key_prompt = Signal(str, str, str, bool)
    auth_prompt = Signal(str, str, str, object)
    tunnels_changed = Signal(object)
    tunnel_message = Signal(str, bool)
    aws_login_required = Signal(str)

    client = None
    chain: list = []
    tunnels = None
    sftp_cwd = ""

    def __init__(self, server: Server, resolve=None, parent=None):
        super().__init__(parent)
        self.server = server
        self.pty = None
        self._size = (120, 32)
        self._closing = False
        self.connected_at = 0.0

    @property
    def is_connected(self) -> bool:
        return bool(self.pty and self.pty.alive())

    def start(self, cols: int = 0, rows: int = 0) -> None:
        if cols and rows:
            self._size = (cols, rows)
        self._closing = False
        threading.Thread(target=self._run, daemon=True, name=f"ssm-{self.server.host}").start()

    def _run(self) -> None:
        from .pty_process import PtyProcess
        try:
            argv = aws.shell_argv(self.server)
            if not aws.plugin() and "BLAMIXSHELL_AWS" not in __import__("os").environ:
                raise aws.PluginMissing()
            self.status.emit(f"starting AWS SSM session to {self.server.host} …")
            pty = self.pty = PtyProcess(argv, aws.env(), *self._size)
        except aws.AwsError as e:
            self.failed.emit(str(e))
            return
        except Exception as e:
            self.failed.emit(f"Could not start the AWS CLI: {e}")
            return
        self.connected_at = time.time()
        self.connected.emit()
        tail = b""
        while True:
            data = pty.read()
            if not data:
                break
            tail = (tail + data)[-8192:]
            self.output.emit(data)
        code = pty.exit_code
        if self._closing:
            self.disconnected.emit("")
            return
        text = tail.decode("utf-8", "replace")
        if code and not self.connected_at_session(text):
            err = aws.classify(text, self.server.aws_profile)
            self.failed.emit(str(err))
            if isinstance(err, aws.LoginRequired) and err.sso:
                self.aws_login_required.emit(err.profile)
            return
        self.disconnected.emit("" if not code else f"session ended with code {code}")

    @staticmethod
    def connected_at_session(text: str) -> bool:
        """Did the SSM session itself start? (then a non-zero exit is the remote shell's)"""
        return "Starting session with SessionId" in text

    def send(self, data: bytes) -> None:
        if self.pty:
            try:
                self.pty.write(data)
            except Exception:
                pass

    def resize(self, cols: int, rows: int) -> None:
        if cols < 2 or rows < 2:
            return
        self._size = (cols, rows)
        if self.pty:
            self.pty.resize(cols, rows)

    def sftp(self):
        raise RuntimeError("Files need SSH: switch this server to “SSH over AWS SSM” to browse files.")

    def reset_sftp(self) -> None:
        pass

    def accept_host_key(self, replace: bool = False) -> None:
        pass

    def answer_prompt(self, answers) -> None:
        pass

    def close(self) -> None:
        self._closing = True
        pty, self.pty = self.pty, None
        if pty:
            threading.Thread(target=pty.close, daemon=True).start()


def make_session(server: Server, resolve, parent=None):
    return (SsmShellSession if server.connection == "ssm-shell" else ShellSession)(server, resolve, parent)
