"""Terminal pane: xterm.js inside QWebEngineView, wired to an SSH shell session."""
from __future__ import annotations

import base64
import json
import time

from PySide6.QtCore import QFile, QIODevice, QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu,
                               QMessageBox, QToolButton, QVBoxLayout, QWidget)

from .models import Server
from .paths import assets_dir
from .ssh_session import ShellSession, make_session
from .platform_ui import kb
from .theme import C, blend, icon

_QWEBCHANNEL_JS: str | None = None
_HTML_TEMPLATE: str | None = None


def _qwebchannel_js() -> str:
    global _QWEBCHANNEL_JS
    if _QWEBCHANNEL_JS is None:
        f = QFile(":/qtwebchannel/qwebchannel.js")
        if f.open(QIODevice.ReadOnly):
            _QWEBCHANNEL_JS = bytes(f.readAll()).decode("utf-8")
            f.close()
        else:
            _QWEBCHANNEL_JS = ""
    return _QWEBCHANNEL_JS


def _html(opts: dict) -> str:
    global _HTML_TEMPLATE
    if _HTML_TEMPLATE is None:
        _HTML_TEMPLATE = (assets_dir() / "terminal.html").read_text(encoding="utf-8")
    return (_HTML_TEMPLATE
            .replace("__QWEBCHANNEL__", _qwebchannel_js())
            .replace("__OPTS__", json.dumps(opts))
            .replace("__BG__", opts["theme"]["background"]))


class TerminalBridge(QObject):
    # Python -> JS
    output = Signal(str)       # base64 bytes
    text = Signal(str)         # plain text (status lines)
    pasteText = Signal(str)
    options = Signal(str)
    command = Signal(str)
    logCommands = Signal(bool)
    suggest = Signal(str)      # JSON {"on": bool, "history": [...]}: autocomplete from history
    histAdd = Signal(str)      # a command to remember for autocomplete
    # Python-side notifications
    sig_input = Signal(bytes)
    sig_resize = Signal(int, int)
    sig_ready = Signal(int, int)
    sig_shortcut = Signal(str)
    sig_paste = Signal()
    sig_focus = Signal()
    sig_title = Signal(str)
    sig_command = Signal(str, str)      # screen line where Enter was pressed, or a pasted command
    sig_context = Signal()              # right-click (when right-click doesn't paste)

    @Slot(str)
    def input(self, data: str) -> None:
        self.sig_input.emit(data.encode("utf-8", "surrogatepass"))

    @Slot(str)
    def inputBinary(self, data: str) -> None:
        self.sig_input.emit(data.encode("latin-1", "replace"))

    @Slot(str, str)
    def commandLine(self, line: str, explicit: str) -> None:
        self.sig_command.emit(line, explicit)

    @Slot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        self.sig_resize.emit(cols, rows)

    @Slot(int, int)
    def ready(self, cols: int, rows: int) -> None:
        self.sig_ready.emit(cols, rows)

    @Slot(str)
    def copy(self, s: str) -> None:
        QGuiApplication.clipboard().setText(s)

    @Slot()
    def requestPaste(self) -> None:
        self.sig_paste.emit()

    @Slot()
    def contextMenu(self) -> None:
        self.sig_context.emit()

    @Slot(str)
    def shortcut(self, name: str) -> None:
        self.sig_shortcut.emit(name)

    @Slot(str)
    def openUrl(self, url: str) -> None:
        if url.startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(url))

    @Slot(str)
    def title(self, t: str) -> None:
        self.sig_title.emit(t)

    @Slot()
    def bell(self) -> None:
        QGuiApplication.beep() if hasattr(QGuiApplication, "beep") else None

    @Slot()
    def focused(self) -> None:
        self.sig_focus.emit()


class _Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            print(f"[terminal js] {message} ({source}:{line})")


class TerminalView(QWebEngineView):
    def __init__(self, opts: dict, parent=None):
        super().__init__(parent)
        self.setPage(_Page(self))
        s = self.settings()
        s.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, True)
        s.setAttribute(QWebEngineSettings.ShowScrollBars, False)
        self.setContextMenuPolicy(Qt.NoContextMenu)
        self.page().setBackgroundColor(opts["theme"]["background"])
        self.bridge = TerminalBridge(self)
        self.channel = QWebChannel(self.page())
        self.channel.registerObject("bridge", self.bridge)
        self.page().setWebChannel(self.channel)
        base = QUrl.fromLocalFile(str(assets_dir()) + "/")
        self.setHtml(_html(opts), base)


class TerminalPane(QWidget):
    """One terminal (header + xterm) bound to one SSH session."""

    activated = Signal(object)
    close_requested = Signal(object)
    shortcut = Signal(object, str)
    state_changed = Signal(object)
    user_input = Signal(object, bytes)     # the tab decides where input goes (broadcast)
    dashboard_requested = Signal(object)
    context_requested = Signal(object)

    def __init__(self, server: Server, resolve, settings, parent=None, tint: str = ""):
        super().__init__(parent)
        self.server = server
        self.tint = tint                 # server / group color (production guard)
        self._resolve = resolve
        self.settings = settings
        self.session: ShellSession | None = None
        self.state = "idle"               # idle | connecting | connected | disconnected | failed
        self._ready = False
        self._size = (0, 0)
        self._buffer: list[str] = []
        self._pending_start = False
        self._session_password = ""
        self.remote_title = ""
        self.deferred = False            # restored from the last session, not connected yet
        self.recorder = None             # session_log.Recorder while recording
        self._command_log = None         # session_log.CommandLog when commands are logged

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.header = QWidget(objectName="PaneHeader")
        self.header.setFixedHeight(30)
        h = QHBoxLayout(self.header)
        h.setContentsMargins(10, 0, 4, 0)
        h.setSpacing(6)
        self.dot = QLabel("●")
        self.title_lbl = QLabel()
        self.title_lbl.setStyleSheet("font-weight:600; font-size:9pt;")
        self.addr_lbl = QLabel(objectName="Hint")
        self.status_lbl = QLabel(objectName="Hint")
        h.addWidget(self.dot)
        h.addWidget(self.title_lbl)
        h.addWidget(self.addr_lbl)
        h.addStretch(1)
        # tunnels: shows "⇄ 2" when tunnels run on this connection; click for details
        self.tun_btn = QToolButton()
        self.tun_btn.setAutoRaise(True)
        self.tun_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.tun_btn.setPopupMode(QToolButton.InstantPopup)
        self.tun_btn.setMenu(QMenu(self.tun_btn))
        self.tun_btn.hide()
        self._tunnel_states: list = []
        h.addWidget(self.tun_btn)
        h.addWidget(self.status_lbl)
        self.rec_lbl = QLabel()
        self.rec_lbl.setStyleSheet(f"color:{C['danger']}; font-size:8.5pt; font-weight:600;")
        self.rec_lbl.hide()
        h.addWidget(self.rec_lbl)
        self.btn_rec = self._tbtn("record", "Record this session to a file", self.toggle_recording)
        h.addWidget(self.btn_rec)
        self.btn_dash = self._tbtn("gauge", kb("Server dashboard (Ctrl+Shift+I)"),
                                   lambda: self.dashboard_requested.emit(self))
        h.addWidget(self.btn_dash)
        self.btn_reconnect = self._tbtn("refresh", "Reconnect (R)", self.reconnect)
        self.btn_close = self._tbtn("x", kb("Close pane (Ctrl+Shift+W)"), lambda: self.close_requested.emit(self))
        h.addWidget(self.btn_reconnect)
        h.addWidget(self.btn_close)
        lay.addWidget(self.header)

        self.view = TerminalView(self._term_options(), self)
        lay.addWidget(self.view, 1)
        b = self.view.bridge
        b.sig_ready.connect(self._on_ready)
        b.sig_input.connect(lambda d: self.user_input.emit(self, d))
        b.sig_resize.connect(self._on_resize)
        b.sig_shortcut.connect(lambda n: self.shortcut.emit(self, n))
        b.sig_paste.connect(self.paste)
        b.sig_context.connect(lambda: self.context_requested.emit(self))
        b.sig_focus.connect(lambda: self.activated.emit(self))
        b.sig_title.connect(self._on_title)
        b.sig_command.connect(self._on_command_line)

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        self._refresh_header()

    def _tbtn(self, name, tip, fn):
        b = QToolButton()
        b.setIcon(icon(name, C["muted"], 14))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(fn)
        return b

    # ---------------------------------------------------------- header
    def set_active(self, active: bool) -> None:
        self.header.setProperty("active", "true" if active else "false")
        self.header.style().unpolish(self.header)
        self.header.style().polish(self.header)

    def _refresh_header(self) -> None:
        colors = {"connected": C["ok"], "connecting": C["warn"], "failed": C["danger"],
                  "disconnected": C["faint"], "idle": C["faint"]}
        self.dot.setStyleSheet(f"color:{colors.get(self.state, C['faint'])}; font-size:9pt;")
        self.title_lbl.setText(self.server.label)
        color = self.tint or self.server.color
        self.title_lbl.setStyleSheet("font-weight:600; font-size:9pt;" + (f" color:{color};" if color else ""))
        if color:   # a colored strip + tinted header: production looks different at a glance
            self.header.setStyleSheet(f"#PaneHeader {{ background: {blend(C['surface'], color, 0.16)};"
                                      f" border-left: 3px solid {color}; }}")
        else:
            self.header.setStyleSheet("")
        self.addr_lbl.setText(self.server.address if self.server.name else "")
        self.btn_reconnect.setVisible(self.state in ("disconnected", "failed"))
        self.btn_dash.setVisible(self.state == "connected" and self.server.uses_ssh)
        self.btn_rec.setVisible(self.state == "connected" or self.recorder is not None)
        self._tick()

    def _tick(self) -> None:
        if self.recorder is not None:
            secs = int(time.time() - self.recorder.started)
            self.rec_lbl.setText(f"● REC {secs // 60:02d}:{secs % 60:02d}")
        if self.state == "connected" and self.session:
            secs = int(time.time() - self.session.connected_at)
            self.status_lbl.setText(f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}")
        elif self.state == "connecting":
            pass
        else:
            self.status_lbl.setText(self.state if self.state != "idle" else "")

    def _set_state(self, st: str, msg: str = "") -> None:
        self.state = st
        if msg:
            self.status_lbl.setText(msg)
        self._refresh_header()
        self.state_changed.emit(self)

    # ---------------------------------------------------------- terminal io
    def _on_ready(self, cols: int, rows: int) -> None:
        self._ready = True
        self.apply_suggest()
        self._size = (cols, rows)
        for chunk in self._buffer:
            self.view.bridge.output.emit(chunk)
        self._buffer.clear()
        if self._pending_start:
            self._pending_start = False
            self._start_session()

    def _on_resize(self, cols: int, rows: int) -> None:
        self._size = (cols, rows)
        if self.session:
            self.session.resize(cols, rows)

    def _on_title(self, t: str) -> None:
        self.remote_title = t
        self.state_changed.emit(self)

    def write_output(self, data: bytes) -> None:
        b64 = base64.b64encode(data).decode("ascii")
        if self._ready:
            self.view.bridge.output.emit(b64)
        else:
            self._buffer.append(b64)

    def write_status(self, msg: str, color: str = "90") -> None:
        self.write_output(f"\x1b[{color}m{msg}\x1b[0m\r\n".encode())

    def send(self, data: bytes) -> None:
        """Deliver keyboard input to this pane's session (after broadcast routing)."""
        if self.state in ("disconnected", "failed") or self.deferred:
            if data in (b"r", b"R", b"\r"):
                self.reconnect()
            return
        if self.session:
            self.session.send(data)

    def send_text(self, text: str) -> None:
        self.view.bridge.pasteText.emit(text)

    def paste(self) -> None:
        text = QGuiApplication.clipboard().text()
        if not text:
            return
        lines = text.rstrip("\n").count("\n") + 1
        if lines > 1 and self.settings.get("confirm_multiline_paste", True):
            preview = "\n".join(text.splitlines()[:8])
            if QMessageBox.question(
                    self, "Paste multiple lines?",
                    f"You're about to paste {lines} lines into {self.server.label}:\n\n{preview}"
                    f"{chr(10) + '…' if lines > 8 else ''}") != QMessageBox.Yes:
                return
        self.view.bridge.pasteText.emit(text)

    def focus_terminal(self) -> None:
        self.view.setFocus()
        self.view.bridge.command.emit("focus")

    def _term_options(self) -> dict:
        opts = self.settings.terminal_options()
        if self.tint and self.settings.get("tint_terminals", True):
            theme = dict(opts["theme"])
            theme["background"] = blend(theme["background"], self.tint, 0.09)
            theme["cursorAccent"] = theme["background"]
            opts["theme"] = theme
        return opts

    def set_tint(self, color: str) -> None:
        if color == self.tint:
            return
        self.tint = color
        self._refresh_header()
        self.apply_settings()

    def apply_settings(self) -> None:
        opts = self._term_options()
        self.view.page().setBackgroundColor(opts["theme"]["background"])
        self.view.bridge.options.emit(json.dumps(opts))
        self._refresh_header()

    # ---------------------------------------------------------- session
    def defer(self) -> None:
        """Restored pane: show where it was, connect when its tab is opened."""
        self.deferred = True
        self.write_status(f"Restored from your last session: {self.server.label} ({self.server.address}).")
        self.status_lbl.setText("opens when you view this tab")

    def start(self) -> None:
        self.deferred = False
        if self.server.needs_password and not self.server.password and not self._session_password:
            pw, ok = QInputDialog.getText(self, f"Password for {self.server.label}",
                                          f"Password for {self.server.address}:", QLineEdit.Password)
            if not ok:
                self._set_state("failed", "cancelled")
                self.write_status("Connection cancelled. Press R to try again.", "33")
                return
            self._session_password = pw
        if self._ready:
            self._start_session()
        else:
            self._pending_start = True
            self._set_state("connecting", "starting …")

    def _start_session(self) -> None:
        if self.session:
            self.session.close()
            self.session.deleteLater()
        srv = self.server.copy()
        if self._session_password:
            srv.password = self._session_password
        s = make_session(srv, self._resolve, self)
        s.output.connect(self.write_output)
        s.output.connect(self._record)
        s.status.connect(lambda m: (self.write_status(m), self.status_lbl.setText(m)))
        s.connected.connect(self._on_connected)
        s.disconnected.connect(self._on_disconnected)
        s.failed.connect(self._on_failed)
        s.host_key_prompt.connect(self._on_host_key)
        s.auth_prompt.connect(self._on_auth_prompt)
        s.tunnels_changed.connect(self._on_tunnels)
        s.tunnel_message.connect(lambda m, err: self.write_status(m, "33" if err else "90"))
        s.aws_login_required.connect(self._on_aws_login)
        self.session = s
        self._set_state("connecting", "connecting …")
        cols, rows = self._size
        s.start(cols, rows)

    def reconnect(self) -> None:
        self.write_status("\r\nReconnecting …", "36")
        self.start()

    def _on_connected(self) -> None:
        self._set_state("connected")
        self.apply_logging()
        if self.server.record_sessions and self.recorder is None:
            self.start_recording()
        if self._size[0]:
            self.session.resize(*self._size)

    def _on_disconnected(self, reason: str) -> None:
        self.stop_recording()
        self._set_state("disconnected")
        msg = f"Session ended: {reason}." if reason else "Session ended."
        self.write_status(f"\r\n{msg}  Press R to reconnect.", "33")

    def _on_failed(self, reason: str) -> None:
        self.stop_recording()
        self._set_state("failed")
        self.write_status(f"\r\n✖ {reason}", "31")
        self.write_status("Press R to retry.", "90")
        if "Authentication" in reason and self.server.auth == "password":
            self._session_password = ""   # ask again next time

    def _on_aws_login(self, profile: str) -> None:
        from .aws_ui import ensure_login
        if time.time() - getattr(self, "_aws_login_at", 0) < 90:
            return      # just signed in and it still fails: don't loop, the error is shown
        self._aws_login_at = time.time()
        self.write_status("Sign in to AWS to continue …", "36")

        def again():
            if self.state in ("failed", "disconnected"):
                self.reconnect()
        QTimer.singleShot(0, lambda: ensure_login(self, profile, again))

    def _on_auth_prompt(self, label: str, title: str, instructions: str, prompts: list) -> None:
        from .dialogs import AuthPromptDialog
        sess = self.sender()
        self.status_lbl.setText("waiting for verification …")
        dlg = AuthPromptDialog(label, title, instructions, prompts, self)
        ok = dlg.exec() == QDialog.Accepted
        if sess:
            sess.answer_prompt(dlg.answers() if ok else None)

    def _on_tunnels(self, states: list) -> None:
        self._tunnel_states = states
        if not states:
            self.tun_btn.hide()
            return
        ok = [s for s in states if s.ok]
        bad = len(states) - len(ok)
        color = C["danger"] if bad and not ok else C["warn"] if bad else C["ok"]
        self.tun_btn.setIcon(icon("tunnel", color, 14))
        live = sum(s.connections for s in states)
        self.tun_btn.setText((f"{len(ok)}/{len(states)}" if bad else f"{len(ok)}") + (f" · {live} open" if live else ""))
        tip = "\n".join(("✔ " if s.ok else "✖ ") + s.summary() for s in states)
        self.tun_btn.setToolTip("Tunnels on this connection\n" + tip)
        m = self.tun_btn.menu()
        m.clear()
        for st in states:
            act = m.addAction(icon("tunnel", C["ok"] if st.ok else C["danger"], 14), st.summary())
            if st.ok and st.tunnel.kind != "R":
                addr = f"{st.tunnel.listen_host}:{st.port}"
                act.setToolTip(f"Click to copy {addr}")
                act.triggered.connect(lambda _=False, a=addr: QGuiApplication.clipboard().setText(a))
            else:
                act.setEnabled(False)
        self.tun_btn.show()

    def _on_host_key(self, hid: str, ktype: str, fp: str, changed: bool) -> None:
        if changed:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle("HOST KEY CHANGED")
            box.setText(f"<b>The host key for {hid} has changed.</b>")
            box.setInformativeText(
                "This can mean someone is intercepting the connection (man-in-the-middle), "
                "or the server was reinstalled.<br><br>"
                f"New key: <code>{ktype}</code><br><code>{fp}</code><br><br>"
                "Only replace the stored key if you know why it changed.")
            replace = box.addButton("Replace key and connect", QMessageBox.DestructiveRole)
            box.addButton("Abort", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is replace:
                self.session.accept_host_key(replace=True)
                return
            self._on_failed("Host key mismatch: connection aborted")
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Trust this host?")
        box.setText(f"<b>First connection to {hid}</b>")
        box.setInformativeText(
            f"The server presented this {ktype} key:<br><code>{fp}</code><br><br>"
            "Verify the fingerprint with the server admin if you can. Trust and remember it?")
        ok = box.addButton("Trust && connect", QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is ok:
            self.write_status(f"Trusted {ktype} {fp}", "90")
            self.session.accept_host_key()
        else:
            self._on_failed("Host key not trusted: connection cancelled")

    # ---------------------------------------------------------- command log / recording
    def apply_logging(self) -> None:
        """Command logging on or off for this pane (global setting or per server)."""
        from .session_log import CommandLog, log_root
        on = bool(self.settings.get("command_log")) or self.server.log_commands
        self._command_log = CommandLog(log_root(self.settings)) if on else None
        self.apply_suggest()

    def _suggest_on(self) -> bool:
        return bool(self.settings.get("history_autocomplete"))

    def apply_suggest(self) -> None:
        """Tell the terminal page whether to report command lines and show suggestions."""
        if not self._ready:
            return
        import json
        from . import cmd_history
        bridge = self.view.bridge
        on = self._suggest_on()
        bridge.logCommands.emit(self._command_log is not None or on)
        hist = cmd_history.shared().get(self.server.id) if on else []
        bridge.suggest.emit(json.dumps({"on": on, "history": hist[-500:]}))

    def log_command(self, command: str, source: str = "terminal", prompt: str = "") -> None:
        if self._command_log is not None and command.strip():
            self._command_log.add(self.server, command.strip(), source, prompt)

    def _on_command_line(self, line: str, explicit: str) -> None:
        if self.state != "connected":
            return
        if explicit:
            self.log_command(explicit, "paste")
            return
        from .session_log import command_from_line
        got = command_from_line(line)
        if got:
            self.log_command(got[1], "terminal", got[0])
            if self._suggest_on():
                from . import cmd_history
                if cmd_history.shared().add(self.server.id, got[1]):
                    self.view.bridge.histAdd.emit(got[1])

    def _record(self, data: bytes) -> None:
        if self.recorder is not None:
            try:
                self.recorder.write(data)
            except Exception as e:
                self.write_status(f"Recording stopped: {e}", "31")
                self.stop_recording()

    def toggle_recording(self) -> None:
        if self.recorder is None:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self) -> None:
        from .session_log import Recorder, log_root
        try:
            self.recorder = Recorder(log_root(self.settings), self.server,
                                     self.settings.get("record_format", "text"),
                                     bool(self.settings.get("record_timestamps")))
        except OSError as e:
            self.write_status(f"Could not start recording: {e}", "31")
            return
        self.write_status(f"● Recording to {self.recorder.path}", "31")
        self.btn_rec.setIcon(icon("record", C["danger"], 14))
        self.btn_rec.setToolTip("Stop recording")
        self.rec_lbl.show()
        self._tick()
        self.state_changed.emit(self)

    def stop_recording(self) -> None:
        if self.recorder is None:
            return
        rec, self.recorder = self.recorder, None
        path = rec.close()
        self.btn_rec.setIcon(icon("record", C["muted"], 14))
        self.btn_rec.setToolTip("Record this session to a file")
        self.rec_lbl.hide()
        if self.state == "connected":
            self.write_status(f"■ Recording saved: {path}", "90")
        self.state_changed.emit(self)

    def shutdown(self) -> None:
        self.stop_recording()
        if self.session:
            self.session.close()
        self._clock.stop()
