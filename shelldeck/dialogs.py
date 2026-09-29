"""Dialogs: unlock/create vault, server editor, settings, snippets, command palette."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QRadioButton, QSpinBox,
                               QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from .models import COLORS, Server, Snippet, Store
from .platform_ui import MONO_DEFAULT
from .settings import TERMINAL_THEMES, Settings
from .ssh_core import test_connection
from .theme import C, icon, style_window


def _section(text: str) -> QLabel:
    return QLabel(text.upper(), objectName="SectionLabel")


class _Base(QDialog):
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)


# ======================================================================= unlock
class UnlockDialog(_Base):
    def __init__(self, create: bool, attempt, parent=None):
        """attempt(password) -> str error or '' on success."""
        super().__init__(parent)
        self.create = create
        self._attempt = attempt
        self.setWindowTitle("ShellDeck")
        self.setFixedWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 30, 32, 26)
        lay.setSpacing(10)

        badge = QLabel()
        badge.setPixmap(icon("lock", C["accent"], 34).pixmap(34, 34))
        lay.addWidget(badge)
        h = QLabel("Create your vault" if create else "Unlock ShellDeck", objectName="H1")
        h.setStyleSheet("font-size:17pt; font-weight:600;")
        lay.addWidget(h)
        sub = QLabel(
            "Servers, passwords and keys are encrypted with AES-256-GCM using a key derived from "
            "this master password. There's no way to recover it if you forget it."
            if create else "Enter your master password to decrypt your servers.")
        sub.setWordWrap(True)
        sub.setObjectName("Muted")
        lay.addWidget(sub)
        lay.addSpacing(8)

        self.pw = QLineEdit(echoMode=QLineEdit.Password, placeholderText="Master password")
        lay.addWidget(self.pw)
        self.pw2 = QLineEdit(echoMode=QLineEdit.Password, placeholderText="Confirm password")
        self.pw2.setVisible(create)
        lay.addWidget(self.pw2)
        self.err = QLabel()
        self.err.setStyleSheet(f"color:{C['danger']};")
        self.err.setWordWrap(True)
        self.err.hide()
        lay.addWidget(self.err)

        self.btn = QPushButton("Create vault" if create else "Unlock", objectName="Primary")
        self.btn.setDefault(True)
        self.btn.clicked.connect(self._go)
        lay.addSpacing(6)
        lay.addWidget(self.btn)
        self.pw.returnPressed.connect(self._go if not create else self.pw2.setFocus)
        self.pw2.returnPressed.connect(self._go)

    def _go(self) -> None:
        p = self.pw.text()
        if self.create:
            if len(p) < 8:
                return self._fail("Use at least 8 characters.")
            if p != self.pw2.text():
                return self._fail("Passwords don't match.")
        self.btn.setEnabled(False)
        self.btn.setText("Deriving key …")
        self.repaint()
        err = self._attempt(p)
        self.btn.setEnabled(True)
        self.btn.setText("Create vault" if self.create else "Unlock")
        if err:
            self._fail(err)
            self.pw.selectAll()
            self.pw.setFocus()
        else:
            self.accept()

    def _fail(self, msg: str) -> None:
        self.err.setText(msg)
        self.err.show()


# ======================================================================= server editor
class _Tester(QObject):
    done = Signal(str)


class ServerDialog(_Base):
    def __init__(self, store: Store, server: Server | None = None, parent=None, group: str = ""):
        super().__init__(parent)
        self.store = store
        self.server = server.copy() if server else Server(group=group)
        self.setWindowTitle("Edit server" if server else "New server")
        self.setMinimumWidth(560)
        s = self.server

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel(self.windowTitle(), objectName="H2")
        root.addWidget(title)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        # -- connection tab
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(4, 14, 4, 4)
        f.setVerticalSpacing(10)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.name = QLineEdit(s.name, placeholderText="e.g. api-prod-1")
        self.host = QLineEdit(s.host, placeholderText="hostname or IP  (user@host:port works too)")
        self.host.editingFinished.connect(self._split_host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(s.port or 22)
        self.port.setFixedWidth(90)
        hp = QHBoxLayout()
        hp.addWidget(self.host, 1)
        hp.addSpacing(8)
        hp.addWidget(QLabel("Port"))
        hp.addWidget(self.port)
        self.user = QLineEdit(s.username, placeholderText="root, ubuntu, deploy …")
        f.addRow("Name", self.name)
        f.addRow("Host", hp)
        f.addRow("Username", self.user)

        authrow = QHBoxLayout()
        self.auth_group = QButtonGroup(self)
        self.auth_stack = QStackedWidget()
        for i, (key, label) in enumerate([("password", "Password"), ("key", "Private key"),
                                          ("agent", "SSH agent / Pageant")]):
            rb = QRadioButton(label)
            rb.setProperty("key", key)
            self.auth_group.addButton(rb, i)
            authrow.addWidget(rb)
            if s.auth == key:
                rb.setChecked(True)
        authrow.addStretch(1)
        f.addRow("Auth", authrow)

        pw_page = QWidget()
        pl = QFormLayout(pw_page)
        pl.setContentsMargins(0, 0, 0, 0)
        self.password = QLineEdit(s.password, echoMode=QLineEdit.Password,
                                  placeholderText="leave empty to ask on every connect")
        pl.addRow("Password", self.password)

        key_page = QWidget()
        kl = QFormLayout(key_page)
        kl.setContentsMargins(0, 0, 0, 0)
        self.key_path = QLineEdit(s.key_path, placeholderText="C:\\Users\\you\\.ssh\\id_ed25519")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_key)
        kr = QHBoxLayout()
        kr.addWidget(self.key_path, 1)
        kr.addWidget(browse)
        self.key_data = QPlainTextEdit(s.key_data)
        self.key_data.setPlaceholderText("…or paste a private key here (stored encrypted in the vault)")
        self.key_data.setFixedHeight(70)
        self.passphrase = QLineEdit(s.passphrase, echoMode=QLineEdit.Password,
                                    placeholderText="only if the key is encrypted")
        kl.addRow("Key file", kr)
        kl.addRow("", self.key_data)
        kl.addRow("Passphrase", self.passphrase)

        agent_page = QLabel("Uses keys loaded in Pageant or the Windows OpenSSH agent, plus the "
                            "default keys in ~/.ssh.")
        agent_page.setWordWrap(True)
        agent_page.setObjectName("Muted")
        for p in (pw_page, key_page, agent_page):
            self.auth_stack.addWidget(p)
        f.addRow("", self.auth_stack)
        self.auth_group.idToggled.connect(lambda i, on: on and self.auth_stack.setCurrentIndex(i))
        if not self.auth_group.checkedButton():
            self.auth_group.button(0).setChecked(True)
        self.auth_stack.setCurrentIndex(self.auth_group.checkedId())
        tabs.addTab(w, "Connection")

        # -- organize tab
        w2 = QWidget()
        f2 = QFormLayout(w2)
        f2.setContentsMargins(4, 14, 4, 4)
        f2.setVerticalSpacing(10)
        f2.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.group = QComboBox()
        self.group.setEditable(True)
        self.group.addItems([""] + store.all_groups())
        self.group.setCurrentText(s.group)
        self.group.lineEdit().setPlaceholderText("Prod/EU  (use / for nesting)")
        self.tags = QLineEdit(", ".join(s.tags), placeholderText="prod, db, docker")
        self.fav = QCheckBox("Pin to favorites")
        self.fav.setChecked(s.favorite)
        f2.addRow("Group", self.group)
        f2.addRow("Tags", self.tags)
        colrow = QHBoxLayout()
        colrow.setSpacing(6)
        self._color = s.color
        self._swatches: list[QPushButton] = []
        for col in COLORS:
            b = QPushButton()
            b.setFixedSize(24, 24)
            b.setCheckable(True)
            b.setProperty("c", col)
            b.clicked.connect(lambda _=False, c=col: self._pick_color(c))
            self._swatches.append(b)
            colrow.addWidget(b)
        colrow.addStretch(1)
        self._pick_color(s.color)
        f2.addRow("Color", colrow)
        f2.addRow("", self.fav)
        self.notes = QPlainTextEdit(s.notes)
        self.notes.setPlaceholderText("Notes: what runs here, who owns it, runbook links …")
        self.notes.setFixedHeight(90)
        f2.addRow("Notes", self.notes)
        tabs.addTab(w2, "Organize")

        # -- advanced tab
        w3 = QWidget()
        f3 = QFormLayout(w3)
        f3.setContentsMargins(4, 14, 4, 4)
        f3.setVerticalSpacing(10)
        f3.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.jump = QComboBox()
        self.jump.addItem("None (direct)", "")
        for other in sorted(store.servers.values(), key=lambda x: x.label.lower()):
            if other.id != s.id:
                self.jump.addItem(f"{other.label}  ({other.address})", other.id)
        idx = self.jump.findData(s.jump_id)
        self.jump.setCurrentIndex(max(idx, 0))
        self.keepalive = QSpinBox()
        self.keepalive.setRange(0, 600)
        self.keepalive.setSuffix(" s")
        self.keepalive.setValue(s.keepalive)
        self.startup = QLineEdit(s.startup_cmd, placeholderText="e.g. cd /srv/app && tmux attach || tmux")
        f3.addRow("Jump host", self.jump)
        f3.addRow("Keepalive", self.keepalive)
        f3.addRow("Run on connect", self.startup)
        tabs.addTab(w3, "Advanced")

        # -- buttons
        self.test_lbl = QLabel(objectName="Hint")
        self.test_lbl.setWordWrap(True)
        root.addWidget(self.test_lbl)
        row = QHBoxLayout()
        test = QPushButton(icon("plug"), " Test connection")
        test.clicked.connect(self._test)
        self.test_btn = test
        row.addWidget(test)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save", objectName="Primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(save)
        root.addLayout(row)
        self._tester = _Tester()
        self._tester.done.connect(self._test_done)
        (self.name if not s.host else self.host).setFocus()

    def _pick_color(self, col: str) -> None:
        self._color = col
        for b in self._swatches:
            c = b.property("c")
            sel = c == col
            b.setChecked(sel)
            bg = c or C["surface"]
            border = C["text"] if sel else C["border"]
            b.setStyleSheet(f"background:{bg}; border:2px solid {border}; border-radius:12px; padding:0;"
                            + ("" if c else f"color:{C['faint']};"))
            b.setText("" if c else "∅")

    def _split_host(self) -> None:
        t = self.host.text().strip()
        if t.startswith("ssh "):
            t = t[4:].strip()
        if "@" in t:
            u, t = t.rsplit("@", 1)
            if not self.user.text():
                self.user.setText(u)
        if t.count(":") == 1:
            t, p = t.split(":")
            if p.isdigit():
                self.port.setValue(int(p))
        self.host.setText(t)

    def _browse_key(self) -> None:
        from pathlib import Path
        path, _ = QFileDialog.getOpenFileName(self, "Private key", str(Path.home() / ".ssh"))
        if path:
            self.key_path.setText(path)

    def _collect(self) -> Server:
        self._split_host()
        s = self.server
        s.name = self.name.text().strip()
        s.host = self.host.text().strip()
        s.port = self.port.value()
        s.username = self.user.text().strip()
        s.auth = self.auth_group.checkedButton().property("key")
        s.password = self.password.text()
        s.key_path = self.key_path.text().strip().strip('"')
        s.key_data = self.key_data.toPlainText().strip()
        s.passphrase = self.passphrase.text()
        s.group = self.group.currentText().strip().strip("/")
        s.tags = [t.strip() for t in self.tags.text().split(",") if t.strip()]
        s.color = self._color
        s.favorite = self.fav.isChecked()
        s.notes = self.notes.toPlainText()
        s.jump_id = self.jump.currentData() or ""
        s.keepalive = self.keepalive.value()
        s.startup_cmd = self.startup.text()
        return s

    def _test(self) -> None:
        s = self._collect().copy()
        if not s.host:
            self.test_lbl.setText("Enter a host first.")
            return
        self.test_btn.setEnabled(False)
        self.test_lbl.setText("Testing …")
        threading.Thread(target=lambda: self._tester.done.emit(
            test_connection(s, self.store.servers.get)), daemon=True).start()

    def _test_done(self, msg: str) -> None:
        self.test_btn.setEnabled(True)
        ok = not msg or msg.startswith("OK")
        color = C["ok"] if ok else C["danger"]
        self.test_lbl.setText(f"<span style='color:{color}'>{'✔ ' + (msg or 'Connected and authenticated.') if ok else '✖ ' + msg}</span>")

    def _save(self) -> None:
        s = self._collect()
        if not s.host:
            QMessageBox.warning(self, "Missing host", "Please enter a host name or IP.")
            return
        self.accept()

    def result_server(self) -> Server:
        return self.server


# ======================================================================= settings
class SettingsDialog(_Base):
    def __init__(self, settings: Settings, store: Store, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.store = store
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.addWidget(QLabel("Settings", objectName="H2"))

        f = QFormLayout()
        f.setVerticalSpacing(10)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(_section("Terminal"))
        self.font = QComboBox()
        self.font.setEditable(True)
        mono = [fam for fam in QFontDatabase.families() if QFontDatabase.isFixedPitch(fam)]
        prefs = ["Cascadia Code", "Cascadia Mono", "SF Mono", "Menlo", "JetBrains Mono", "Fira Code",
                 "Ubuntu Mono", "Consolas", "DejaVu Sans Mono"]
        self.font.addItems([p for p in prefs if p in mono] + [m for m in mono if m not in prefs])
        self.font.setCurrentText(settings["font_family"].split(",")[0].strip())
        self.size = QSpinBox()
        self.size.setRange(8, 32)
        self.size.setValue(int(settings["font_size"]))
        self.lh = QDoubleSpinBox()
        self.lh.setRange(1.0, 2.0)
        self.lh.setSingleStep(0.05)
        self.lh.setValue(float(settings["line_height"]))
        self.theme = QComboBox()
        self.theme.addItems(list(TERMINAL_THEMES))
        self.theme.setCurrentText(settings["theme"])
        self.cursor = QComboBox()
        self.cursor.addItems(["bar", "block", "underline"])
        self.cursor.setCurrentText(settings["cursor_style"])
        self.blink = QCheckBox("Blinking cursor")
        self.blink.setChecked(settings["cursor_blink"])
        self.scroll = QSpinBox()
        self.scroll.setRange(1000, 200000)
        self.scroll.setSingleStep(5000)
        self.scroll.setValue(int(settings["scrollback"]))
        f.addRow("Font", self.font)
        f.addRow("Size", self.size)
        f.addRow("Line height", self.lh)
        f.addRow("Color theme", self.theme)
        f.addRow("Cursor", self.cursor)
        f.addRow("", self.blink)
        f.addRow("Scrollback lines", self.scroll)
        lay.addLayout(f)

        lay.addWidget(_section("Clipboard"))
        self.cos = QCheckBox("Copy on select (like PuTTY)")
        self.cos.setChecked(settings["copy_on_select"])
        self.rcp = QCheckBox("Right-click pastes")
        self.rcp.setChecked(settings["right_click_paste"])
        self.cmp = QCheckBox("Confirm before pasting multiple lines")
        self.cmp.setChecked(settings["confirm_multiline_paste"])
        for cb in (self.cos, self.rcp, self.cmp):
            lay.addWidget(cb)

        lay.addWidget(_section("Security"))
        chg = QPushButton(icon("lock"), " Change master password…")
        chg.clicked.connect(self._change_pw)
        lay.addWidget(chg, 0, Qt.AlignLeft)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setObjectName("Primary")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addSpacing(8)
        lay.addWidget(bb)

    def _save(self) -> None:
        s = self.settings
        fam = self.font.currentText().strip()
        s["font_family"] = f"{fam}, {MONO_DEFAULT}"
        s["font_size"] = self.size.value()
        s["line_height"] = round(self.lh.value(), 2)
        s["theme"] = self.theme.currentText()
        s["cursor_style"] = self.cursor.currentText()
        s["cursor_blink"] = self.blink.isChecked()
        s["scrollback"] = self.scroll.value()
        s["copy_on_select"] = self.cos.isChecked()
        s["right_click_paste"] = self.rcp.isChecked()
        s["confirm_multiline_paste"] = self.cmp.isChecked()
        s.save()
        self.accept()

    def _change_pw(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Change master password")
        l = QFormLayout(dlg)
        a = QLineEdit(echoMode=QLineEdit.Password)
        b = QLineEdit(echoMode=QLineEdit.Password)
        l.addRow("New password", a)
        l.addRow("Confirm", b)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        if len(a.text()) < 8 or a.text() != b.text():
            QMessageBox.warning(self, "Not changed", "Passwords must match and be at least 8 characters.")
            return
        self.store.vault.change_password(a.text(), self.store.to_dict())
        QMessageBox.information(self, "Done", "Master password changed.")


# ======================================================================= snippets
class SnippetsDialog(_Base):
    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Snippets")
        self.resize(640, 420)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        left = QVBoxLayout()
        self.list = QListWidget()
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        add = QPushButton(icon("plus"), " Add")
        add.clicked.connect(self._add)
        rm = QPushButton(icon("trash"), " Delete")
        rm.clicked.connect(self._del)
        row.addWidget(add)
        row.addWidget(rm)
        left.addLayout(row)
        lay.addLayout(left, 2)
        right = QVBoxLayout()
        right.addWidget(_section("Name"))
        self.name = QLineEdit()
        right.addWidget(self.name)
        right.addWidget(_section("Command"))
        self.cmd = QPlainTextEdit()
        self.cmd.setStyleSheet(f"font-family: '{MONO_DEFAULT.split(',')[0]}';")
        right.addWidget(self.cmd, 1)
        hint = QLabel("Snippets are typed into the active terminal (not auto-executed unless they "
                      "end with a newline).", objectName="Hint")
        hint.setWordWrap(True)
        right.addWidget(hint)
        done = QPushButton("Done", objectName="Primary")
        done.clicked.connect(self.accept)
        right.addWidget(done, 0, Qt.AlignRight)
        lay.addLayout(right, 3)
        self.list.currentRowChanged.connect(self._load)
        self.name.textEdited.connect(self._store)
        self.cmd.textChanged.connect(self._store)
        self._loading = False
        self._refresh()

    def _refresh(self, select: int = 0) -> None:
        self.list.clear()
        for sn in self.store.snippets:
            self.list.addItem(QListWidgetItem(icon("code"), sn.name or "(unnamed)"))
        if self.store.snippets:
            self.list.setCurrentRow(min(select, len(self.store.snippets) - 1))

    def _load(self, row: int) -> None:
        self._loading = True
        sn = self.store.snippets[row] if 0 <= row < len(self.store.snippets) else None
        self.name.setText(sn.name if sn else "")
        self.cmd.setPlainText(sn.command if sn else "")
        self._loading = False

    def _store(self) -> None:
        if self._loading:
            return
        row = self.list.currentRow()
        if 0 <= row < len(self.store.snippets):
            sn = self.store.snippets[row]
            sn.name = self.name.text()
            sn.command = self.cmd.toPlainText()
            self.list.item(row).setText(sn.name or "(unnamed)")

    def _add(self) -> None:
        self.store.snippets.append(Snippet("New snippet", ""))
        self._refresh(len(self.store.snippets) - 1)
        self.name.setFocus()
        self.name.selectAll()

    def _del(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.store.snippets):
            del self.store.snippets[row]
            self._refresh(row)

    def done(self, r):  # noqa: D401 - persist on close
        self.store.save()
        super().done(r)


# ======================================================================= command palette
class CommandPalette(QDialog):
    """Ctrl+Shift+P: fuzzy search servers and actions; also accepts user@host:port."""

    def __init__(self, entries: list[tuple[str, str, object, str]], parent=None, placeholder=""):
        """entries: (title, subtitle, callback, icon_name)."""
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Popup)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.entries = entries
        self.quick_connect = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(objectName="Palette")
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(6, 6, 6, 6)
        self.q = QLineEdit(placeholderText=placeholder or "Search servers & actions, or type user@host:port")
        lay.addWidget(self.q)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{C['border']};")
        lay.addWidget(line)
        self.list = QListWidget()
        self.list.setMinimumHeight(340)
        lay.addWidget(self.list)
        self.q.textChanged.connect(self._filter)
        self.q.returnPressed.connect(self._run)
        self.list.itemActivated.connect(lambda _i: self._run())
        self.q.installEventFilter(self)
        self.setFixedWidth(620)
        self._filter("")

    def eventFilter(self, obj, ev):  # noqa: N802
        from PySide6.QtCore import QEvent
        if obj is self.q and ev.type() == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Down, Qt.Key_Up):
                r = self.list.currentRow() + (1 if k == Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(r, self.list.count() - 1)))
                return True
            if k == Qt.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(obj, ev)

    @staticmethod
    def _score(q: str, text: str) -> int:
        q, text = q.lower(), text.lower()
        if not q:
            return 1
        if q in text:
            return 100 - text.index(q)
        i = 0
        for ch in text:
            if i < len(q) and ch == q[i]:
                i += 1
        return 10 if i == len(q) else 0

    def _filter(self, q: str) -> None:
        self.list.clear()
        scored = []
        for e in self.entries:
            sc = max(self._score(q, e[0]), self._score(q, e[1]) - 5)
            if sc > 0:
                scored.append((sc, e))
        scored.sort(key=lambda x: -x[0])
        qs = q.strip()
        if qs and ("@" in qs or "." in qs or qs.count(":") == 1) and " " not in qs:
            it = QListWidgetItem(icon("bolt", C["accent"]), f"Quick connect  ›  {qs}")
            it.setData(Qt.UserRole, ("quick", qs))
            self.list.addItem(it)
        for _sc, (title, sub, cb, ico) in scored[:60]:
            it = QListWidgetItem(icon(ico), f"{title}   ·   {sub}" if sub else title)
            it.setData(Qt.UserRole, ("cb", cb))
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _run(self) -> None:
        it = self.list.currentItem()
        if not it:
            return
        kind, val = it.data(Qt.UserRole)
        self.accept()
        if kind == "quick" and self.quick_connect:
            self.quick_connect(val)
        elif kind == "cb":
            val()

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        p = self.parentWidget()
        if p:
            g = p.geometry()
            self.move(g.x() + (g.width() - self.width()) // 2, g.y() + 90)
        self.q.setFocus()
