"""Qt wrapper around the SSH core: interactive shell on a background thread,
results delivered to the UI through Qt signals (queued across threads)."""
from __future__ import annotations

import threading
import time
from typing import Callable

import paramiko
from PySide6.QtCore import QObject, Signal

from .models import Server
from .ssh_core import (AuthConfigError, ChangedHostKey, UnknownHostKey,  # noqa: F401 (re-exported)
                       fingerprint, friendly_error, host_id, load_known_hosts,
                       load_private_key, open_client, open_sftp, test_connection, trust_host_key)
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
            chan = self.client.invoke_shell(term="xterm-256color", width=cols, height=rows)
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

