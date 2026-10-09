"""Server dashboard window: overview, services, processes, logs, ports, updates, users.

Bound to a terminal pane and reuses its SSH connection (extra exec channels, like
`ssh host cmd`). Everything runs on background threads; the UI only renders results.
"""
from __future__ import annotations

import re
import shlex
import threading
import time
from types import SimpleNamespace

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFontDatabase, QPainter, QPainterPath, QPen, QSyntaxHighlighter, QTextCharFormat,
                           QTextCursor)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QMenu, QToolButton,
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QSpinBox, QTimeEdit,
                               QProgressBar, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from . import collect, cron, loglines, reconnect
from . import system as sysinfo
from . import dashboard as d
from .tablekeys import auto_key
from . import firewall as fw
from . import compose, docker, packages, report, security, sshkeys, storage, timers, units
from .theme import C, blend, icon, style_window

REFRESH_MS = 5000
# Tabs that stay in the bar; everything else lives in the "More" menu, grouped.
MAIN_TABS = ("overview", "services", "processes", "logs", "updates")
MORE_GROUPS = (("System", ("users", "system", "storage", "mounts", "docker")),
               ("Network and security", ("ports", "network", "firewall", "security")),
               ("Scheduling", ("cron", "timers")))
TAB_KEYS = ["overview", "services", "processes", "logs", "ports", "updates", "users", "cron", "firewall",
            "storage", "docker", "timers", "security", "system", "network", "mounts"]


class _Signals(QObject):
    done = Signal(str, object, str)          # job key, result, error


class Sparkline(QWidget):
    """Tiny line chart of the last values (0..100)."""

    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self.values: list[float] = []
        self.color = color
        self.setFixedHeight(34)

    def push(self, v: float | None) -> None:
        if v is None:
            return
        self.values = (self.values + [max(0.0, min(100.0, v))])[-60:]
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        if len(self.values) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height() - 2
        step = w / 59
        x0 = w - step * (len(self.values) - 1)
        pts = [QPointF(x0 + i * step, 1 + h - h * v / 100) for i, v in enumerate(self.values)]
        area = QPainterPath(QPointF(pts[0].x(), h + 1))
        for pt in pts:
            area.lineTo(pt)
        area.lineTo(QPointF(pts[-1].x(), h + 1))
        fill = QColor(self.color)
        fill.setAlpha(40)
        p.fillPath(area, fill)
        line = QPainterPath(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)
        p.setPen(QPen(QColor(self.color), 1.6))
        p.drawPath(line)
        p.end()


class Tile(QFrame):
    def __init__(self, title: str, color: str, spark: bool = True):
        super().__init__(objectName="DashTile")
        self.setStyleSheet(f"#DashTile {{ background:{C['surface']}; border:1px solid {C['border']};"
                           " border-radius:12px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        t = QLabel(title.upper(), objectName="SectionLabel")
        self.value = QLabel("–")
        self.value.setStyleSheet("font-size:18pt; font-weight:600;")
        self.sub = QLabel("", objectName="Hint")
        lay.addWidget(t)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)
        self.spark = Sparkline(color) if spark else None
        if self.spark:
            lay.addWidget(self.spark)
        else:
            lay.addStretch(1)         # same layout as the tiles with a chart

    def set(self, value: str, sub: str = "", level: float | None = None, color_value: bool = True) -> None:
        self.value.setText(value)
        self.sub.setText(sub)
        col = C["text"]
        if level is not None and color_value:
            col = C["danger"] if level >= 90 else C["warn"] if level >= 75 else C["text"]
        self.value.setStyleSheet(f"font-size:18pt; font-weight:600; color:{col};")


_auto_key = auto_key        # (moved to tablekeys.py, shared with the terminal dashboard)


class _SortItem(QTableWidgetItem):
    """A table cell that sorts by value (see _auto_key), or by an explicit key stored with it."""

    def __lt__(self, other) -> bool:  # noqa: N802
        a, b = self.data(Qt.UserRole + 1), other.data(Qt.UserRole + 1)
        return (_auto_key(self.text()) if a is None else a) < (_auto_key(other.text()) if b is None else b)


class _Filling:
    """`with _Filling(table):` fill it without the rows jumping around; the sort chosen by clicking a
    header is applied again when the block ends."""

    def __init__(self, table: QTableWidget):
        self.t = table
        self.was = table.isSortingEnabled()

    def __enter__(self):
        self.t.setSortingEnabled(False)
        return self.t

    def __exit__(self, *exc):
        self.t.setSortingEnabled(self.was)


def _table(headers: list[str], stretch: int = -1, sortable: bool = False) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setShowGrid(False)
    t.setAlternatingRowColors(False)
    t.setWordWrap(False)
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.ResizeToContents)
    hh.setSectionResizeMode(len(headers) - 1 if stretch < 0 else stretch, QHeaderView.Stretch)
    hh.setHighlightSections(False)
    t.setStyleSheet(f"QTableWidget {{ background:{C['surface']}; border:1px solid {C['border']}; border-radius:10px; }}"
                    f"QHeaderView::section {{ background:{C['surface']}; color:{C['muted']}; border:none;"
                    f" border-bottom:1px solid {C['border']}; padding:6px 8px; }}"
                    "QTableWidget::item { padding: 4px 8px; }"
                    f"QTableWidget::item:selected {{ background:{C['surface2']}; color:{C['text']}; }}")
    if sortable:                                  # click a column header to sort, click again to reverse
        t.setSortingEnabled(True)
        hh.setSectionsClickable(True)
        hh.setSortIndicatorShown(True)
        hh.setSortIndicator(-1, Qt.AscendingOrder)       # (no column chosen yet: the order as it arrives)
    return t


def _item(text, color: str | None = None, align_right: bool = False, data=None, sort=None) -> QTableWidgetItem:
    it = _SortItem(str(text))
    if sort is not None:
        it.setData(Qt.UserRole + 1, sort)
    if color:
        it.setForeground(QColor(color))
    if align_right:
        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if data is not None:
        it.setData(Qt.UserRole, data)
    return it


def _toolbar(*widgets) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(6)
    for w in widgets:
        if w == "stretch":
            row.addStretch(1)
        else:
            row.addWidget(w)
    return row


def _btn(ic: str, text: str, fn, tip: str = "") -> QPushButton:
    b = QPushButton(icon(ic), (" " + text) if text else "")
    b.clicked.connect(fn)
    if tip:
        b.setToolTip(tip)
    return b


class _SshKeysDialog(QDialog):
    """The keys in an account's authorized_keys: add a public key, remove one, then save."""

    def __init__(self, name: str, home: str, text: str, is_me: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"SSH keys of {name}")
        self.resize(780, 430)
        self.original, self.text, self.is_me = text, text, is_me
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"{home}/.ssh/authorized_keys: who can log in as <b>{name}</b> with a key.",
                             wordWrap=True))
        self.table = _table(["Type", "Fingerprint", "Comment", "Options"], stretch=2)
        self.table.itemSelectionChanged.connect(self._sync)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.add_btn = _btn("plus", "Add key…", self._add, "Paste a public key or open a .pub file")
        self.rm_btn = _btn("trash", "Remove", self._remove, "Remove the selected key")
        row.addWidget(self.add_btn)
        row.addWidget(self.rm_btn)
        self.msg = QLabel("", objectName="Hint", wordWrap=True)
        row.addWidget(self.msg, 1)
        lay.addLayout(row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._fill()

    def _fill(self) -> None:
        self.keys = sshkeys.parse(self.text)
        t = self.table
        t.setRowCount(len(self.keys))
        for i, k in enumerate(self.keys):
            if k.valid:
                t.setItem(i, 0, _item(k.type))
                t.setItem(i, 1, _item(k.fingerprint, C["muted"]))
                t.setItem(i, 2, _item(k.comment or "–"))
                t.setItem(i, 3, _item(k.options or "–", C["warn"] if k.options else C["muted"]))
            else:
                t.setItem(i, 0, _item("?", C["warn"]))
                t.setItem(i, 1, _item("(not a key we can read: kept as it is)", C["warn"]))
                t.setItem(i, 2, _item(k.raw[:60], C["muted"]))
                t.setItem(i, 3, _item("–", C["muted"]))
        self.msg.setText("Changes are written when you press Save." if self.text != self.original else "")
        self.buttons.button(QDialogButtonBox.Save).setEnabled(self.text != self.original)
        self._sync()

    def _sync(self) -> None:
        self.rm_btn.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def _add(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Add a public key")
        dlg.resize(560, 220)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Paste the public key (one line, starts with ssh-ed25519 or ssh-rsa …):"))
        box = QPlainTextEdit()
        box.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        lay.addWidget(box, 1)
        err = QLabel("")
        err.setStyleSheet(f"color:{C['danger']};")
        lay.addWidget(err)
        row = QHBoxLayout()

        def open_file():
            path, _ = QFileDialog.getOpenFileName(dlg, "Public key", "", "Public keys (*.pub);;All files (*)")
            if path:
                try:
                    with open(path, encoding="utf-8") as f:
                        box.setPlainText(f.read().strip())
                except OSError as e:
                    err.setText(str(e))
        row.addWidget(_btn("folder", "Open .pub file…", open_file))
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        row.addWidget(bb)
        lay.addLayout(row)
        bb.rejected.connect(dlg.reject)

        def ok():
            msg = sshkeys.validate_public_key(box.toPlainText())
            err.setText(msg)
            if not msg:
                dlg.accept()
        bb.accepted.connect(ok)
        if dlg.exec() != QDialog.Accepted:
            return
        self.text, added = sshkeys.add_key(self.text, box.toPlainText().strip())
        self._fill()
        if not added:
            self.msg.setText("That key is already there.")

    def _remove(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self.keys):
            self.text = sshkeys.remove_key(self.text, self.keys[rows[0].row()].raw)
            self._fill()

    def _save(self) -> None:
        had = [k for k in sshkeys.parse(self.original) if k.valid]
        left = [k for k in sshkeys.parse(self.text) if k.valid]
        if self.is_me and had and not left:
            if QMessageBox.warning(self, "Remove every key?", "This is the account you are connected as. With no key "
                                   "left you may not be able to log in again (unless a password works).",
                                   QMessageBox.Ok | QMessageBox.Cancel) != QMessageBox.Ok:
                return
        self.accept()


class _ViewDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 460)
        lay = QVBoxLayout(self)
        view = QPlainTextEdit(readOnly=True)
        view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        view.setPlainText(text.rstrip() or "(empty)")
        lay.addWidget(view, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class _ReportDialog(QDialog):
    def __init__(self, markdown: str, label: str, parent=None):
        super().__init__(parent)
        from datetime import date
        from pathlib import Path
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in label).strip("-") or "server"
        self.default_path = str(Path.home() / f"report-{safe}-{date.today():%Y-%m-%d}.md")
        self.setWindowTitle(f"Server report: {label}")
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit(markdown)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.msg = QLabel("", objectName="Hint")
        row.addWidget(self.msg, 1)
        row.addWidget(_btn("copy", "Copy", self._copy, "Copy the report to the clipboard"))
        row.addWidget(_btn("download", "Save…", self._save, "Save as a Markdown file"))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.view.toPlainText())
        self.msg.setText("Copied.")

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save report", self.default_path,
                                              "Markdown (*.md);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.view.toPlainText())
            self.msg.setText(f"Saved {path}")
        except OSError as e:
            self.msg.setText(f"Could not save: {e}")


class _NewServiceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New systemd service")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(placeholderText="my-app  (becomes my-app.service)")
        self.desc = QLineEdit(placeholderText="what it does, e.g. My Node app")
        self.command = QLineEdit(placeholderText="full path + arguments, e.g. /usr/bin/node /srv/app/server.js")
        self.user = QLineEdit(placeholderText="optional, default root")
        self.workdir = QLineEdit(placeholderText="optional, e.g. /srv/app")
        self.restart = QComboBox()
        for label, key in (("Restart it if it fails", "on-failure"), ("Always restart it", "always"),
                           ("Don't restart it", "no")):
            self.restart.addItem(label, key)
        self.env = QPlainTextEdit(placeholderText="optional environment, one per line:\nPORT=8080\nNODE_ENV=production")
        self.env.setFixedHeight(70)
        self.enable = QCheckBox("Start at boot")
        self.enable.setChecked(True)
        self.start = QCheckBox("Start it now")
        self.start.setChecked(True)
        for label, w in (("Name", self.name), ("Description", self.desc), ("Command", self.command),
                         ("Run as user", self.user), ("Working folder", self.workdir), ("Restart", self.restart),
                         ("Environment", self.env)):
            form.addRow(label, w)
        lay.addLayout(form)
        opts = QHBoxLayout()
        opts.addWidget(self.enable)
        opts.addWidget(self.start)
        opts.addStretch(1)
        lay.addLayout(opts)
        lay.addWidget(QLabel("UNIT FILE", objectName="SectionLabel"))
        self.preview = QPlainTextEdit(readOnly=True)
        self.preview.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.preview.setFixedHeight(150)
        lay.addWidget(self.preview)
        self.err = QLabel("")
        self.err.setStyleSheet(f"color:{C['danger']};")
        lay.addWidget(self.err)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Create")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        for w in (self.name, self.desc, self.command, self.user, self.workdir):
            w.textChanged.connect(self._update)
        self.env.textChanged.connect(self._update)
        self.restart.currentIndexChanged.connect(self._update)
        self._update()

    def _text(self) -> str:
        return units.build_unit(self.desc.text(), self.command.text(), self.user.text(), self.workdir.text(),
                                self.restart.currentData(), self.env.toPlainText().splitlines())

    def _update(self) -> None:
        self.preview.setPlainText(self._text())
        name = self.name.text().strip()
        err = ""
        if name and not units.valid_name(name):
            err = "The name can use letters, digits, - _ . and @ (no spaces, no .service)."
        elif self.command.text().strip():
            err = units.exec_error(self.command.text())
        self.err.setText(err)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(
            bool(name) and units.valid_name(name) and bool(self.command.text().strip()) and not err)

    def values(self) -> tuple[str, str, bool, bool]:
        return self.name.text().strip(), self._text(), self.enable.isChecked(), self.start.isChecked()


class _ListPicker(QDialog):
    def __init__(self, title: str, text: str, items: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.index = -1
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(text))
        self.list = QListWidget()
        self.list.addItems(items)
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _i: self._ok())
        lay.addWidget(self.list)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _ok(self) -> None:
        self.index = self.list.currentRow()
        self.accept()


class _OutputDialog(QDialog):
    def __init__(self, title: str, command: str, code, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 420)
        lay = QVBoxLayout(self)
        ok = code == 0
        head = QLabel(f"<b>{'Finished' if ok else 'Failed'}</b>  ·  exit code {code}")
        head.setStyleSheet(f"color:{C['ok'] if ok else C['danger']};")
        lay.addWidget(head)
        lay.addWidget(QLabel(command, objectName="Hint", wordWrap=True))
        view = QPlainTextEdit(readOnly=True)
        view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        view.setPlainText((text[-20000:]).rstrip() or "(no output)")
        lay.addWidget(view, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        lay.addWidget(bb)


class _FirewallRuleDialog(QDialog):
    def __init__(self, manager: str, zones: list[str], default_zone: str, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("Add firewall rule")
        lay = QFormLayout(self)
        self.kind = QComboBox()
        self.kind.addItem("Open a port", "port")
        if manager == "firewalld":
            self.kind.addItem("Allow a service (like http)", "service")
        self.port = QLineEdit(placeholderText="e.g. 8080, or a range 8000-8100")
        self.proto = QComboBox()
        self.proto.addItems(["tcp", "udp"])
        self.service = QLineEdit(placeholderText="e.g. http, https, mysql")
        self.zone = QComboBox()
        self.zone.setEditable(True)
        self.zone.addItems(zones or ([default_zone] if default_zone else []))
        self.zone.setCurrentText(default_zone)
        lay.addRow("Rule", self.kind)
        self.r_port, self.r_proto = QWidget(), QWidget()
        lay.addRow("Port", self.port)
        lay.addRow("Protocol", self.proto)
        lay.addRow("Service", self.service)
        if manager == "firewalld":
            lay.addRow("Zone", self.zone)
        self._rows = {"port": [self.port, self.proto], "service": [self.service]}
        self.kind.currentIndexChanged.connect(self._sync)
        lay.addRow(QLabel("Opening a port makes whatever listens there reachable from outside. The change is "
                          "permanent and applied immediately.", objectName="Hint", wordWrap=True))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)
        self._form = lay
        self._sync()

    def _sync(self) -> None:
        k = self.kind.currentData()
        for key, widgets in self._rows.items():
            for w in widgets:
                w.setVisible(key == k)
                lab = self._form.labelForField(w)
                if lab:
                    lab.setVisible(key == k)

    def values(self) -> tuple[str, str, str, str]:
        k = self.kind.currentData()
        zone = self.zone.currentText().strip() if self.manager == "firewalld" else ""
        return k, (self.service.text() if k == "service" else self.port.text()).strip(), self.proto.currentText(), zone


class _CronTextDialog(QDialog):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit crontab as text")
        self.resize(720, 460)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("The whole crontab. The server checks the syntax when you save.", objectName="Hint"))
        self.edit = QPlainTextEdit(text)
        self.edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.edit, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def text(self) -> str:
        return self.edit.toPlainText()


class _Signals2(QObject):
    done = Signal(int, str)


class _ServerPicker(QDialog):
    """Browse the server's folders (over the existing connection) to pick a script or a folder."""

    def __init__(self, run, start: str, want_dir: bool, parent=None):
        super().__init__(parent)
        self.run, self.want_dir, self.chosen, self.cwd = run, want_dir, "", ""
        self.setWindowTitle("Choose a folder on the server" if want_dir else "Choose a script on the server")
        self.resize(520, 440)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.path = QLineEdit()
        self.path.returnPressed.connect(lambda: self._go(self.path.text().strip()))
        top.addWidget(self.path, 1)
        top.addWidget(_btn("up", "", lambda: self._go(self.cwd.rstrip("/").rsplit("/", 1)[0] or "/"), "Parent folder"))
        top.addWidget(_btn("home", "", lambda: self._go(""), "Home folder"))
        lay.addLayout(top)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._open)
        self.list.itemSelectionChanged.connect(self._sync)
        lay.addWidget(self.list, 1)
        self.msg = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.msg)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Ok).setText("Use this folder" if want_dir else "Choose")
        lay.addWidget(self.buttons)
        self._go(start)

    def _go(self, path: str) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            out = self.run(cron.list_dir_command(path))
        finally:
            QApplication.restoreOverrideCursor()
        try:
            if out is None:
                raise ValueError("Not connected.")
            cwd, dirs, files = cron.parse_listing(out)
        except ValueError as e:
            self.msg.setText(str(e))
            return
        self.cwd = cwd
        self.path.setText(cwd)
        self.msg.setText("")
        self.list.clear()
        for name in dirs:
            self.list.addItem(QListWidgetItem(icon("folder"), name + "/"))
        if not self.want_dir:
            for name in files:
                self.list.addItem(QListWidgetItem(icon("file"), name))
        self._sync()

    def _full(self, text: str) -> str:
        return self.cwd.rstrip("/") + "/" + text.rstrip("/")

    def _open(self, item) -> None:
        if item.text().endswith("/"):
            self._go(self._full(item.text()))
        elif not self.want_dir:
            self._accept()

    def _sync(self) -> None:
        sel = self.list.selectedItems()
        ok = self.want_dir or (bool(sel) and not sel[0].text().endswith("/"))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok and bool(self.cwd))

    def _accept(self) -> None:
        sel = self.list.selectedItems()
        if self.want_dir:
            self.chosen = self._full(sel[0].text()) if sel and sel[0].text().endswith("/") else self.cwd
        elif sel and not sel[0].text().endswith("/"):
            self.chosen = self._full(sel[0].text())
        else:
            return
        self.accept()


class _CronJobDialog(QDialog):
    """Pick when a command runs from a simple form; the cron expression is built for you."""

    KINDS = [("Every few minutes", "minutes"), ("Every hour", "hourly"), ("Every day", "daily"),
             ("Every week", "weekly"), ("Every month", "monthly"), ("When the server starts", "reboot"),
             ("Custom (cron expression)", "custom")]

    def __init__(self, entry: cron.Entry | None, now, tz: str, parent=None, run=None, note: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Edit scheduled job" if entry else "Add scheduled job")
        self.setMinimumWidth(520)
        self.now, self.tz, self.run, self.note = now, tz, run, note
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        def row(label: str, widget, *extra, into=None) -> QWidget:
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(label)
            lab.setMinimumWidth(90)
            h.addWidget(lab)
            h.addWidget(widget, 1)
            for x in extra:
                h.addWidget(x)
            (into or lay).addWidget(w)
            return w

        self.command = QLineEdit(placeholderText="a script path or any command, e.g. /usr/local/bin/backup.sh --full")
        self.browse_cmd = QPushButton("Browse…")
        self.browse_cmd.setToolTip("Pick the script from the server's files")
        self.browse_cmd.setVisible(run is not None)
        self.browse_cmd.clicked.connect(self._browse_script)
        row("Command", self.command, self.browse_cmd)
        self.check_lbl = QLabel("", objectName="Hint", wordWrap=True)
        self.check_lbl.setContentsMargins(94, 0, 0, 0)
        lay.addWidget(self.check_lbl)
        self._check_sig = _Signals2()
        self._check_sig.done.connect(self._checked)
        self._check_id = 0
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(600)
        self._check_timer.timeout.connect(self._run_check)
        self.kind = QComboBox()
        for label, key in self.KINDS:
            self.kind.addItem(label, key)
        row("Run", self.kind)
        self.every = QSpinBox()
        self.every.setRange(1, 59)
        self.every.setValue(5)
        self.every.setSuffix(" minutes")
        self.minute = QSpinBox()
        self.minute.setRange(0, 59)
        self.minute.setPrefix("minute ")
        self.time = QTimeEdit()
        self.time.setDisplayFormat("HH:mm")
        self.weekday = QComboBox()
        for n in (1, 2, 3, 4, 5, 6, 0):
            self.weekday.addItem(cron.WEEKDAYS[n], n)
        self.day = QSpinBox()
        self.day.setRange(1, 31)
        self.custom = QLineEdit(placeholderText="minute hour day-of-month month day-of-week, e.g. */10 9-17 * * 1-5")
        self.r_every, self.r_minute = row("Every", self.every), row("At", self.minute)
        self.r_weekday, self.r_day = row("On", self.weekday), row("On day", self.day)
        self.r_time, self.r_custom = row("At", self.time), row("Schedule", self.custom)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(True)
        lay.addWidget(self.enabled)

        # more options: output, folder, login shell
        self.more = QPushButton("More options")
        self.more.setCheckable(True)
        self.more.setFlat(True)
        self.more.setStyleSheet("text-align:left; padding:4px 0;")
        lay.addWidget(self.more)
        self.opts = QWidget()
        ol = QVBoxLayout(self.opts)
        ol.setContentsMargins(0, 0, 0, 0)
        self.out_mode = QComboBox()
        self.out_mode.addItem("Default (cron emails it, if mail is set up)", cron.OUT_DEFAULT)
        self.out_mode.addItem("Discard it", cron.OUT_DISCARD)
        self.out_mode.addItem("Save it to a log file", cron.OUT_LOG)
        row("Output", self.out_mode, into=ol)
        self.log = QLineEdit(placeholderText="$HOME/cron-job.log")
        self.r_log = row("Log file", self.log, into=ol)
        self.folder = QLineEdit(placeholderText="optional: the job starts here (default: the home folder)")
        self.browse_dir = QPushButton("Browse…")
        self.browse_dir.setVisible(run is not None)
        self.browse_dir.clicked.connect(self._browse_folder)
        row("Run in", self.folder, self.browse_dir, into=ol)
        self.login = QCheckBox("Login shell (loads the user's PATH and environment)")
        ol.addWidget(self.login)
        lay.addWidget(self.opts)
        self.opts.setVisible(False)
        self.more.toggled.connect(self.opts.setVisible)
        self.more.toggled.connect(lambda on: self.more.setText("Fewer options" if on else "More options"))

        self.expr_lbl = QLabel("")
        self.expr_lbl.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.desc_lbl = QLabel("")
        self.next_lbl = QLabel("", objectName="Hint", wordWrap=True)
        self.err_lbl = QLabel("")
        self.err_lbl.setStyleSheet(f"color:{C['danger']};")
        box = QFrame()
        box.setStyleSheet(f"QFrame {{ background:{C['surface']}; border:1px solid {C['border']}; border-radius:10px; }}"
                          "QLabel { border:none; background:transparent; }")
        bl = QVBoxLayout(box)
        for w in (self.desc_lbl, self.expr_lbl, self.next_lbl, self.err_lbl):
            bl.addWidget(w)
        lay.addWidget(box)
        lay.addWidget(QLabel("Jobs run on the server's clock and time zone.", objectName="Hint"))
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        if entry:
            parts = cron.decompose(entry.command)
            self.command.setText(parts.command)
            self.out_mode.setCurrentIndex(max(0, self.out_mode.findData(parts.out)))
            self.log.setText(parts.log)
            self.folder.setText(parts.folder)
            self.login.setChecked(parts.login)
            if parts.out != cron.OUT_DEFAULT or parts.folder or parts.login:
                self.more.setChecked(True)
            self.enabled.setChecked(entry.enabled)
            kind, p = cron.classify(entry.schedule)
            self.kind.setCurrentIndex(self.kind.findData(kind))
            self.every.setValue(p.get("every", 5))
            self.minute.setValue(p.get("minute", 0))
            from PySide6.QtCore import QTime
            self.time.setTime(QTime(p.get("hour", 0), p.get("minute", 0)))
            self.weekday.setCurrentIndex(max(0, self.weekday.findData(p.get("weekday", 1))))
            self.day.setValue(p.get("day", 1))
            self.custom.setText(entry.schedule)
        else:
            from PySide6.QtCore import QTime
            self.kind.setCurrentIndex(self.kind.findData("daily"))
            self.time.setTime(QTime(2, 0))
        for w in (self.command, self.custom, self.log, self.folder):
            w.textChanged.connect(self._update)
        self.out_mode.currentIndexChanged.connect(self._out_changed)
        self.command.textChanged.connect(lambda _t: self._check_timer.start())
        self.folder.textChanged.connect(lambda _t: self._check_timer.start())
        for w in (self.kind, self.weekday):
            w.currentIndexChanged.connect(self._update)
        for w in (self.every, self.minute, self.day):
            w.valueChanged.connect(self._update)
        self.time.timeChanged.connect(self._update)
        self._update()
        self._check_timer.start()

    # ---- the command part ----
    def _out_changed(self) -> None:
        if self.out_mode.currentData() == cron.OUT_LOG and not self.log.text().strip():
            hit = cron.script_path(self.command.text())
            stem = hit[0].rsplit("/", 1)[-1].rsplit(".", 1)[0] if hit else "job"
            self.log.setText(f"$HOME/cron-{stem or 'job'}.log")
        self._update()

    def _parts(self) -> cron.Parts:
        return cron.Parts(self.command.text().strip(), self.folder.text().strip(), self.login.isChecked(),
                          self.out_mode.currentData(), self.log.text().strip())

    def _browse_script(self) -> None:
        hit = cron.script_path(self.command.text())
        start = hit[0].rsplit("/", 1)[0] if hit and "/" in hit[0] else ""
        dlg = _ServerPicker(self.run, start, False, self)
        if dlg.exec() != QDialog.Accepted:
            return
        text = self.command.text().strip()
        if not text:
            self.command.setText(dlg.chosen)
        elif hit and hit[0] in text:
            self.command.setText(text.replace(hit[0], dlg.chosen, 1))
        else:
            self.command.setText(f"{text} {dlg.chosen}")

    def _browse_folder(self) -> None:
        dlg = _ServerPicker(self.run, self.folder.text().strip(), True, self)
        if dlg.exec() == QDialog.Accepted:
            self.folder.setText(dlg.chosen)

    def _run_check(self) -> None:
        """Does the script named in the command exist and can it run? (asked in the background)"""
        hit = cron.script_path(self.command.text())
        self._check_id += 1
        if not hit or self.run is None:
            self.check_lbl.setText("")
            return
        path, need_exec = hit
        rid = self._check_id

        def work():
            try:
                out = (self.run(cron.check_script(path, need_exec)) or "").strip()
            except Exception:
                out = ""
            self._check_sig.done.emit(rid, f"{out}|{path}")
        threading.Thread(target=work, daemon=True, name="cron-check").start()

    def _checked(self, rid: int, res: str) -> None:
        if rid != self._check_id:
            return
        state, _, path = res.partition("|")
        who = f" ({self.note})" if self.note else ""
        msgs = {"ok": (f"✔ {path} exists on the server{who}", C["ok"]),
                "missing": (f"⚠ {path} wasn't found on the server{who}", C["warn"]),
                "dir": (f"⚠ {path} is a folder, not a script{who}", C["warn"]),
                "noexec": (f"⚠ {path} isn't executable{who}. Run  chmod +x {path}  on the server, or start it "
                           f"with its interpreter, e.g.  bash {path}", C["warn"])}
        text, color = msgs.get(state, ("", C["muted"]))
        self.check_lbl.setText(text)
        self.check_lbl.setStyleSheet(f"color:{color};")

    def schedule(self) -> str:
        k = self.kind.currentData()
        if k == "custom":
            return self.custom.text().strip()
        t = self.time.time()
        return cron.build_schedule(k, minute=self.minute.value() if k == "hourly" else t.minute(),
                                   hour=t.hour(), weekday=self.weekday.currentData(), day=self.day.value(),
                                   every=self.every.value())

    def values(self) -> tuple[str, str, bool]:
        return self.schedule(), cron.compose(self._parts()), self.enabled.isChecked()
    # (the plain command for other uses, like a systemd timer: cron.compose(self._parts(), cron_escape=False))

    def _update(self) -> None:
        k = self.kind.currentData()
        show = {"minutes": [self.r_every], "hourly": [self.r_minute], "daily": [self.r_time],
                "weekly": [self.r_weekday, self.r_time], "monthly": [self.r_day, self.r_time],
                "reboot": [], "custom": [self.r_custom]}[k]
        for w in (self.r_every, self.r_minute, self.r_weekday, self.r_day, self.r_time, self.r_custom):
            w.setVisible(any(w is x for x in show))
        self.r_log.setVisible(self.out_mode.currentData() == cron.OUT_LOG)
        expr = self.schedule()
        err = cron.validate_schedule(expr) if expr else "Enter a schedule."
        err_cmd = not self.command.text().strip() or (self.out_mode.currentData() == cron.OUT_LOG
                                                      and not self.log.text().strip())
        self.err_lbl.setText(err)
        self.err_lbl.setVisible(bool(err))
        self.expr_lbl.setText(expr)
        self.desc_lbl.setText(f"<b>{cron.describe(expr)}</b>" if not err else "")
        runs = cron.next_runs(expr, self.now or __import__("datetime").datetime.now(), 3) if not err else []
        self.next_lbl.setText("Next runs: " + "  ·  ".join(f"{r:%a %d %b %H:%M}" for r in runs)
                              if runs else ("Runs once each time the server starts." if expr == "@reboot" else ""))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(not err and not err_cmd)


class _AddUserDialog(QDialog):
    def __init__(self, groups: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add user")
        lay = QFormLayout(self)
        self.name = QLineEdit(placeholderText="e.g. deploy")
        self.shell = QComboBox()
        self.shell.addItems(["/bin/bash", "/bin/sh", "/usr/sbin/nologin"])
        self.shell.setEditable(True)
        self.admin_grp = d.admin_group(groups)
        self.admin = QCheckBox(f"Administrator (adds to “{self.admin_grp}”)" if self.admin_grp
                               else "Administrator (no sudo / wheel group found)")
        self.admin.setEnabled(bool(self.admin_grp))
        self.extra = QLineEdit(placeholderText="optional, comma separated, e.g. docker,www-data")
        self.home = QLineEdit(placeholderText="default: /home/<username>")
        self.home.setToolTip("Leave empty for the system default, or give an absolute path such as /srv/deploy")
        self.make_home = QCheckBox("Create the home folder")
        self.make_home.setChecked(True)
        lay.addRow("Username", self.name)
        lay.addRow("Home folder", self.home)
        lay.addRow("", self.make_home)
        lay.addRow("Shell", self.shell)
        lay.addRow("", self.admin)
        lay.addRow("Other groups", self.extra)
        lay.addRow(QLabel("The account starts without a password (key login only). Use “Set password” "
                          "afterwards if it needs one.", objectName="Hint", wordWrap=True))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def values(self) -> tuple[str, str, list[str], str, bool]:
        groups = [g.strip() for g in self.extra.text().split(",") if g.strip()]
        if self.admin.isChecked() and self.admin_grp and self.admin_grp not in groups:
            groups.append(self.admin_grp)
        return (self.name.text().strip(), self.shell.currentText().strip() or "/bin/bash", groups,
                self.home.text().strip(), self.make_home.isChecked())


class _GroupsDialog(QDialog):
    def __init__(self, account: d.Account, groups: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Groups of {account.name}")
        self.account = account
        self.primary = account.groups[0] if account.groups else ""
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Tick the groups {account.name} should belong to."
                             + (f" The primary group ({self.primary}) can't be changed here." if self.primary else ""),
                             wordWrap=True))
        self.list = QListWidget()
        names = sorted(set(groups) | set(account.groups))
        for g in names:
            if g == self.primary:
                continue
            it = QListWidgetItem(g)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if g in account.groups else Qt.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def changes(self) -> tuple[list[str], list[str]]:
        add, remove = [], []
        for i in range(self.list.count()):
            it = self.list.item(i)
            had, now = it.text() in self.account.groups, it.checkState() == Qt.Checked
            if now and not had:
                add.append(it.text())
            elif had and not now:
                remove.append(it.text())
        return add, remove


class _LogHighlighter(QSyntaxHighlighter):
    """Errors red, warnings amber, the "--" between context groups faint, and the filter text marked."""

    def __init__(self, doc):
        super().__init__(doc)
        self.needle = ""

    def highlightBlock(self, text: str) -> None:            # noqa: N802 (Qt's name)
        if text == loglines.SEPARATOR:
            f = QTextCharFormat()
            f.setForeground(QColor(C["faint"]))
            self.setFormat(0, len(text), f)
            return
        sev = loglines.severity(text)
        if sev:
            f = QTextCharFormat()
            f.setForeground(QColor(C["danger"] if sev == "error" else C["warn"]))
            self.setFormat(0, len(text), f)
        if self.needle:
            mark = QTextCharFormat()
            mark.setBackground(QColor(blend(C["bg"], C["accent"], 0.45)))
            mark.setForeground(QColor(C["text"]))
            low, n, i = text.lower(), len(self.needle), 0
            while (i := low.find(self.needle, i)) >= 0:
                self.setFormat(i, n, mark)
                i += n


class _TimezoneDialog(QDialog):
    """Pick the server's time zone from the zones it knows, with each one's offset right now."""

    def __init__(self, server: str, current: str, now: str, zones: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Time zone")
        self.resize(560, 560)
        self.zones = zones
        self.current = current
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>Time zone of {server}</b>"))
        lay.addWidget(QLabel(f"Now: <b>{current or 'unknown'}</b>" + (f"  ·  server time {now}" if now else ""),
                             objectName="Muted"))
        self.find = QLineEdit(placeholderText="Search: a city, a region, or an offset like +03 or -05:30")
        self.find.textChanged.connect(self._fill)
        lay.addWidget(self.find)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._ok())
        self.list.currentItemChanged.connect(lambda *_: self._changed())
        lay.addWidget(self.list, 1)
        self.typed = None
        if not zones:                    # no tz database listed (minimal containers): type it, the server checks it
            lay.addWidget(QLabel("The server didn't list its time zones (is the tzdata package installed?). "
                                 "Type a name like Europe/Bucharest; the server checks that it knows it.",
                                 objectName="Hint", wordWrap=True))
            self.list.hide()
            self.find.setPlaceholderText("Europe/Bucharest")
            self.find.textChanged.connect(lambda _t: self._changed())
        self.hint = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.hint)
        bar = QHBoxLayout()
        utc = QPushButton("UTC")
        utc.setToolTip("Use UTC (common for servers: logs and cron jobs don't shift with summer time)")
        utc.clicked.connect(lambda: self._pick("UTC"))
        bar.addWidget(utc)
        bar.addStretch(1)
        self.ok = QPushButton("Set time zone")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self._ok)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bar.addWidget(self.ok)
        bar.addWidget(cancel)
        lay.addLayout(bar)
        self._fill()
        self._pick(current, select_only=True)
        self.find.setFocus()

    def _fill(self) -> None:
        if not self.zones:
            return
        q = self.find.text().strip().lower().replace("utc", "").replace(" ", "")
        keep = self.list.currentItem().data(Qt.UserRole) if self.list.currentItem() else self.current
        self.list.clear()
        for z in self.zones:
            text = z.label
            hay = (z.name.lower().replace("_", " ") + " " + z.name.lower() + " " + z.offset + " " + z.abbr.lower()
                   + " " + z.offset.replace(":00", ""))
            if q and q not in hay.replace(" ", "") and q not in hay:
                continue
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, z.name)
            if z.name == self.current:
                it.setText(text + "   (now)")
                it.setForeground(QColor(C["accent"]))
            self.list.addItem(it)
            if z.name == keep:
                self.list.setCurrentItem(it)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self._changed()

    def _pick(self, name: str, select_only: bool = False) -> None:
        if not self.zones:
            self.find.setText(name)
            return
        if not select_only:
            self.find.clear()
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == name:
                self.list.setCurrentRow(i)
                self.list.scrollToItem(self.list.item(i), QAbstractItemView.PositionAtCenter)
                return

    def value(self) -> str:
        if not self.zones:
            return self.find.text().strip()
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else ""

    def _changed(self) -> None:
        v = self.value()
        same = bool(v) and v == self.current
        self.ok.setEnabled(bool(v) and not same and sysinfo.valid_zone(v))
        if self.zones:
            self.hint.setText("That's the time zone it has now." if same else
                              f"{self.list.count()} zones" + (" match" if self.find.text().strip() else "")
                              + ". Changes the clock the server shows, its logs and when cron jobs run; the time "
                              "itself (UTC) doesn't change.")

    def _ok(self) -> None:
        if self.ok.isEnabled():
            self.accept()


class _ComposeFileDialog(QDialog):
    """A project's compose files, one tab each; saved only after compose accepts the new version."""

    def __init__(self, project: str, files: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Compose files: {project}")
        self.resize(820, 600)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Saving checks the file with compose first (with the project's other files and its .env); "
                             "the previous version is kept next to it as <file>.bak-<time>.", objectName="Hint",
                             wordWrap=True))
        self.tabs = QTabWidget()
        self.edits: dict = {}
        self.original = dict(files)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        for path, text in files.items():
            ed = QPlainTextEdit(text)
            ed.setFont(mono)
            ed.setLineWrapMode(QPlainTextEdit.NoWrap)
            ed.setTabChangesFocus(False)
            self.edits[path] = ed
            self.tabs.addTab(ed, path.rsplit("/", 1)[-1])
            self.tabs.setTabToolTip(self.tabs.count() - 1, path)
        lay.addWidget(self.tabs, 1)
        self.apply = QCheckBox("Apply after saving (up -d: re-creates the services whose settings changed)")
        self.apply.setChecked(True)
        lay.addWidget(self.apply)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def changed(self) -> dict:
        return {p: e.toPlainText() for p, e in self.edits.items() if e.toPlainText() != self.original[p]}

    def _save(self) -> None:
        if not self.changed():
            QMessageBox.information(self, "Compose files", "Nothing changed.")
            return
        self.accept()


class DashboardWindow(QWidget):
    def __init__(self, pane, color: str = "", send_to_terminal=None, settings=None, parent=None):
        super().__init__(parent, Qt.Window)
        self.settings = settings if settings is not None else {}
        self.pane = pane
        self.server = pane.server
        self.color = color
        self.send_to_terminal = send_to_terminal
        self._sig = _Signals()
        self._sig.done.connect(self._on_done)
        self._busy: set[str] = set()
        self._sudo_pw: str | None = None      # kept only while this window is open
        self._root = False
        self._services: list[d.Service] = []
        self._update_mgr = ""
        self._loaded: set[str] = set()
        self._closed = False
        self.setWindowTitle(f"Dashboard: {self.server.label}")
        self.resize(1060, 720)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._build()
        pane.state_changed.connect(lambda _p: self._connection_changed())
        pane.destroyed.connect(self.close)          # terminal closed: nothing left to show
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(self._interval_ms())
        self._place()
        self._connection_changed()

    # ================================================================ layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)
        head = QHBoxLayout()
        if self.color:
            strip = QFrame()
            strip.setFixedSize(4, 34)
            strip.setStyleSheet(f"background:{self.color}; border-radius:2px;")
            head.addWidget(strip)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        name = QLabel(self.server.label, objectName="H2")
        if self.color:
            name.setStyleSheet(f"color:{self.color};")
        self.subtitle = QLabel(self.server.address, objectName="Muted")
        titles.addWidget(name)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        self.updated_lbl = QLabel("", objectName="Hint")
        self.auto = QCheckBox("Auto-refresh")
        self.auto.setChecked(True)
        self.auto.setToolTip("Refresh the open tab regularly (Overview, Processes, Logs in follow mode)")
        self.interval = QComboBox()
        for sec in (2, 5, 10, 30, 60):
            self.interval.addItem(f"{sec} s", sec)
        self.interval.setCurrentIndex(max(0, self.interval.findData(self._interval_ms() // 1000)))
        self.interval.setToolTip("How often the open tab refreshes (remembered)")
        self.interval.currentIndexChanged.connect(self._interval_changed)
        head.addWidget(self.updated_lbl)
        head.addSpacing(8)
        head.addWidget(self.auto)
        head.addWidget(self.interval)
        head.addWidget(_btn("bolt", "Alerts", self._show_alerts,
                            "When this server crossed a limit (disk, memory, swap, load) or a service failed, "
                            "and for how long (recorded while it is the active terminal)"))
        head.addWidget(_btn("file", "Report…", self._make_report,
                            "Collect a Markdown report of this server (overview, services, updates, ports, accounts, "
                            "cron, firewall) to save or paste into a ticket"))
        head.addWidget(_btn("refresh", "Refresh", lambda: self.refresh(force=True)))
        root.addLayout(head)

        self.banner_row = QWidget()
        brl = QHBoxLayout(self.banner_row)
        brl.setContentsMargins(0, 0, 0, 0)
        brl.setSpacing(8)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        brl.addWidget(self.banner, 1)
        self.btn_reconnect = QPushButton("Reconnect now")
        self.btn_reconnect.clicked.connect(self._reconnect_clicked)
        brl.addWidget(self.btn_reconnect)
        self.auto_reconnect = QCheckBox("Reconnect automatically")
        self.auto_reconnect.setChecked(True)
        brl.addWidget(self.auto_reconnect)
        self.banner_row.hide()
        root.addWidget(self.banner_row)
        self._watch = reconnect.Watch()
        self._retry_at = 0.0
        self._pending_drops = ""
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(1000)
        self._reconnect_timer.timeout.connect(self._reconnect_tick)

        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(lambda _i: self.refresh())
        root.addWidget(self.tabs, 1)
        self._build_overview()
        self._build_services()
        self._build_processes()
        self._build_logs()
        self._build_ports()
        self._build_updates()
        self._build_users()
        self._build_cron()
        self._build_firewall()
        self._build_storage()
        self._build_docker()
        self._build_timers()
        self._build_security()
        self._build_collect_tabs()
        self._wire_details()
        self._group_tabs()

        self.status = QLabel("", objectName="Hint")
        root.addWidget(self.status)

    def _group_tabs(self) -> None:
        """Keep the main tabs in the bar and put the others in a grouped "More" menu at its right end
        (a bar with a dozen tabs overflows into scroll arrows)."""
        for i, key in enumerate(TAB_KEYS):
            self.tabs.setTabVisible(i, key in MAIN_TABS)
        self.more_btn = QToolButton()
        self.more_btn.setObjectName("MoreTabs")
        self.more_btn.setPopupMode(QToolButton.InstantPopup)
        self.more_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.more_btn.setCursor(Qt.PointingHandCursor)
        menu = QMenu(self.more_btn)
        self._more_actions: dict[str, object] = {}
        for title, keys in MORE_GROUPS:
            menu.addSection(title)
            for key in keys:
                i = TAB_KEYS.index(key)
                act = menu.addAction(self.tabs.tabText(i))
                act.triggered.connect(lambda _c=False, i=i: self.tabs.setCurrentIndex(i))
                self._more_actions[key] = act
        self.more_btn.setMenu(menu)
        self.tabs.setCornerWidget(self.more_btn, Qt.TopRightCorner)
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.currentChanged.connect(lambda _i: self._sync_more())
        self._sync_more()

    def _sync_more(self) -> None:
        """The button names the open tab when it is one from the menu, so you can see where you are."""
        key = TAB_KEYS[self.tabs.currentIndex()]
        active = key not in MAIN_TABS
        self.more_btn.setText((self.tabs.tabText(self.tabs.currentIndex()) if active else "More") + "  ▾")
        self.more_btn.setStyleSheet(
            "QToolButton#MoreTabs { padding: 9px 14px; margin: 6px 2px 0 2px; border-radius: 8px; "
            f"color: {C['text'] if active else C['muted']}; background: {C['surface'] if active else 'transparent'}; }}"
            f"QToolButton#MoreTabs:hover {{ color: {C['text']}; background: {C['surface'] if active else C['sidebar']}; }}")
        for k, act in self._more_actions.items():
            act.setCheckable(True)
            act.setChecked(k == key)

    def _page(self, title: str) -> QVBoxLayout:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 12, 2, 2)
        lay.setSpacing(10)
        # in a scroll area: a page with a wide toolbar scrolls sideways on a narrow window instead of making
        # the whole dashboard wider than the screen
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(w)
        self.tabs.addTab(scroll, title)
        return lay

    def _build_overview(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 12, 2, 2)
        lay.setSpacing(12)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.t_cpu = Tile("CPU", C["accent"])
        self.t_mem = Tile("Memory", C["ok"])
        self.t_load = Tile("Load", C["warn"], spark=False)
        self.t_swap = Tile("Swap", C["muted"], spark=False)
        for i, t in enumerate((self.t_cpu, self.t_mem, self.t_load, self.t_swap)):
            grid.addWidget(t, 0, i)
        lay.addLayout(grid)
        lay.addWidget(QLabel("DISKS", objectName="SectionLabel"))
        self.disks_box = QVBoxLayout()
        self.disks_box.setSpacing(6)
        lay.addLayout(self.disks_box)
        self.failed_title = QLabel("SERVICES", objectName="SectionLabel")
        lay.addWidget(self.failed_title)
        self.failed_lbl = QLabel("", wordWrap=True)
        self.failed_lbl.setTextFormat(Qt.RichText)
        self.failed_lbl.linkActivated.connect(lambda _l: self._show_failed_services())
        lay.addWidget(self.failed_lbl)
        lay.addStretch(1)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, "Overview")

    def _build_services(self) -> None:
        lay = self._page("Services")
        self.svc_filter = QLineEdit(placeholderText="Filter services…")
        self.svc_filter.setMinimumWidth(220)
        self.svc_filter.textChanged.connect(self._fill_services)
        self.svc_failed = QCheckBox("Failed only")
        self.svc_failed.toggled.connect(self._fill_services)
        self.svc_state = QComboBox()
        for label, key in (("All services", ""), ("Running", "running"), ("Not running", "stopped"),
                           ("Start at boot", "enabled"), ("Don't start at boot", "disabled")):
            self.svc_state.addItem(label, key)
        self.svc_state.currentIndexChanged.connect(lambda _i: self._fill_services())
        self.svc_count = QLabel("", objectName="Hint")
        acts = [_btn("refresh", "", lambda: self.refresh(force=True), "Reload the list")]
        self.svc_buttons: dict[str, QPushButton] = {}
        for a in ("start", "stop", "restart", "enable", "disable"):
            b = _btn({"start": "bolt", "stop": "x", "restart": "refresh", "enable": "plus",
                      "disable": "x"}[a], a.capitalize(), lambda _=False, a=a: self._service_action(a),
                     {"enable": "Start at boot", "disable": "Don't start at boot"}.get(a, ""))
            self.svc_buttons[a] = b
            acts.append(b)
        lay.addLayout(_toolbar(self.svc_filter, self.svc_state, self.svc_failed, self.svc_count, "stretch", *acts,
                               _btn("file", "Status", self._service_status),
                               _btn("code", "Unit file", self._view_unit, "Show the selected service's unit file"),
                               _btn("terminal", "Logs", self._service_logs),
                               _btn("plus", "New service…", self._new_service,
                                    "Create a systemd service from a simple form")))
        self.svc_table = _table(["Service", "State", "Startup", "Description"], sortable=True)
        self.svc_table.doubleClicked.connect(lambda _i: self._service_status())
        self.svc_table.itemSelectionChanged.connect(self._update_service_buttons)
        lay.addWidget(self.svc_table, 1)
        self.svc_msg = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.svc_msg)

    def _build_processes(self) -> None:
        lay = self._page("Processes")
        self.ps_sort = QComboBox()
        self.ps_sort.addItems(["Top by CPU", "Top by memory"])
        self.ps_sort.setToolTip("Which 500 processes to load. Click a column header to sort what is shown.")
        self.ps_sort.currentIndexChanged.connect(lambda _i: self.refresh(force=True))
        self.ps_filter = QLineEdit(placeholderText="Filter by command, user or PID…")
        self.ps_filter.setMinimumWidth(260)
        self.ps_filter.textChanged.connect(lambda _t: self._fill_processes())
        self.ps_user = QComboBox()
        self.ps_user.addItem("All users", "")
        self.ps_user.currentIndexChanged.connect(lambda _i: self._fill_processes())
        self.ps_count = QLabel("", objectName="Hint")
        self._procs: list = []
        lay.addLayout(_toolbar(self.ps_sort, self.ps_filter, self.ps_user, self.ps_count, "stretch",
                               _btn("x", "End process…", lambda: self._kill(False)),
                               _btn("x", "Force kill…", lambda: self._kill(True))))
        self.ps_table = _table(["PID", "User", "CPU %", "Mem %", "Memory", "Running for", "Command"], sortable=True)
        lay.addWidget(self.ps_table, 1)

    def _build_logs(self) -> None:
        lay = self._page("Logs")
        self.log_unit = QComboBox()
        self.log_unit.setEditable(True)
        self.log_unit.setMinimumWidth(240)
        self.log_unit.lineEdit().setPlaceholderText("All services (or type a unit)")
        self.log_prio = QComboBox()
        self.log_prio.addItems(list(d.PRIORITIES))
        self.log_lines = QComboBox()
        self.log_lines.addItems(["200 lines", "1000 lines", "5000 lines"])
        self.log_follow = QCheckBox("Follow")
        self.log_follow.setToolTip("Reload every 5 seconds and keep the end in view")
        top = _toolbar(self.log_unit, self.log_prio, self.log_lines, _btn("download", "Load",
                       lambda: self.refresh(force=True)), self.log_follow, "stretch")
        lay.addLayout(top)
        self._log_raw = ""
        self.log_find = QLineEdit(placeholderText="Filter the lines shown (text, any case)")
        self.log_find.setMinimumWidth(230)
        self.log_find.textChanged.connect(lambda _t: self._apply_log_filter())
        self.log_count = QLabel("", objectName="Hint")
        self.log_around = QComboBox()
        for n in (0, 2, 5, 10, 25):
            self.log_around.addItem("Matching lines only" if n == 0 else f"± {n} lines around", n)
        self.log_around.setToolTip("With a filter: also show the lines just before and after each match (like grep -C)")
        self.log_around.currentIndexChanged.connect(lambda _i: self._apply_log_filter())
        self.log_only = QComboBox()
        self.log_only.addItem("Every line", "")
        self.log_only.addItem("Warnings and errors", "warn")
        self.log_only.addItem("Errors only", "error")
        self.log_only.setToolTip("Keep only the lines that look like warnings or errors (by their level or wording); "
                                 "works with any log, also without journald")
        self.log_only.currentIndexChanged.connect(lambda _i: self._apply_log_filter())
        self.log_views = QComboBox()
        self.log_views.setMinimumWidth(170)
        self.log_views.activated.connect(self._apply_log_view)
        self._fill_log_views()
        top.insertWidget(top.count() - 1, self.log_only)       # before the stretch: the second row is full
        lay.addLayout(_toolbar(self.log_find, self.log_around, self.log_count,
                               _btn("search", "Next error", self._next_log_error,
                                    "Jump to the next line that looks like an error"), "stretch", self.log_views,
                               _btn("plus", "Save view", self._save_log_view,
                                    "Remember this service, level, size and filter for this server"),
                               _btn("trash", "", self._delete_log_view, "Delete the selected saved view")))
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSizeF(max(8.5, mono.pointSizeF() * 0.95))
        self.log_view.setFont(mono)
        self._log_hl = _LogHighlighter(self.log_view.document())
        lay.addWidget(self.log_view, 1)

    def _build_ports(self) -> None:
        lay = self._page("Ports")
        lay.addWidget(QLabel("Ports this server listens on. Process names need root (or sudo) to be visible.",
                             objectName="Hint"))
        self.port_filter = QLineEdit(placeholderText="Filter by port, address or process…")
        self.port_filter.setMinimumWidth(280)
        self.port_filter.textChanged.connect(lambda _t: self._fill_ports())
        self.port_count = QLabel("", objectName="Hint")
        self._ports: list = []
        lay.addLayout(_toolbar(self.port_filter, self.port_count, "stretch"))
        self.port_table = _table(["Protocol", "Address", "Port", "Process"], sortable=True)
        lay.addWidget(self.port_table, 1)

    def _build_updates(self) -> None:
        lay = self._page("Updates")
        self.upd_lbl = QLabel("")
        self.upd_btn = _btn("terminal", "Type the upgrade command in the terminal", self._type_upgrade,
                            "Types it into this server's terminal without pressing Enter, so you can review it")
        self.upd_btn.setEnabled(False)
        self.upd_install = _btn("download", "Install updates", self._install_updates,
                                "Installs all pending updates on the server (asks to confirm first)")
        self.upd_install.setEnabled(False)
        self.upd_filter = QLineEdit(placeholderText="Filter packages…")
        self.upd_filter.setMinimumWidth(220)
        self.upd_filter.textChanged.connect(lambda _t: self._fill_updates())
        self._upd_list: list = []
        lay.addLayout(_toolbar(self.upd_lbl, self.upd_filter, "stretch", self.upd_btn, self.upd_install))
        self.upd_table = _table(["Package", "New version"], sortable=True)
        lay.addWidget(self.upd_table, 1)
        self.upd_hint = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.upd_hint)

        # find a package, then install or remove it
        lay.addWidget(QLabel("<b>Find a package</b>"))
        self.pkg_query = QLineEdit(placeholderText="Package name or a word, e.g. nginx, htop, python3")
        self.pkg_query.setMinimumWidth(300)
        self.pkg_query.returnPressed.connect(self._pkg_search)
        self.pkg_count = QLabel("", objectName="Hint")
        self.pkg_install = _btn("download", "Install…", lambda: self._pkg_change("install"),
                                "Install the selected package (asks first)")
        self.pkg_remove = _btn("trash", "Remove…", lambda: self._pkg_change("remove"),
                               "Remove the selected package (shows what else goes with it, asks first)")
        self._pkg_list: list = []
        self._pkg_last = ""
        lay.addLayout(_toolbar(self.pkg_query, _btn("search", "Search", self._pkg_search), self.pkg_count, "stretch",
                               self.pkg_install, self.pkg_remove))
        self.pkg_table = _table(["Package", "Installed", "Version", "Description"], sortable=True)
        self.pkg_table.itemSelectionChanged.connect(self._update_pkg_buttons)
        lay.addWidget(self.pkg_table, 1)
        self._update_pkg_buttons()

    def _build_users(self) -> None:
        lay = self._page("Users")
        self._accounts: list[d.Account] = []          # what the table shows
        self._every_account: list[d.Account] = []     # including system accounts
        self._all_groups: list[str] = []
        self.usr_buttons = {
            "add": _btn("plus", "Add user", self._add_user, "Create an account with a home folder"),
            "groups": _btn("edit", "Groups…", self._edit_groups, "Change the groups of the selected account"),
            "lock": _btn("lock", "Lock", self._toggle_lock, "Block / allow password login for the selected account"),
            "passwd": _btn("terminal", "Set password", self._set_password,
                           "Types the passwd command in the terminal: the password is never sent from here"),
            "keys": _btn("shield", "SSH keys…", self._ssh_keys, "See and edit the selected account's authorized_keys"),
            "delete": _btn("trash", "Delete", self._delete_user, "Remove the selected account"),
        }
        self.usr_system = QCheckBox("Show system accounts")
        self.usr_system.toggled.connect(lambda _on: self._fill_users())
        lay.addLayout(_toolbar(self.usr_system, "stretch", *self.usr_buttons.values()))
        self.usr_table = _table(["Account", "UID", "Logged in", "Groups", "Locked", "Shell", "Home"])
        self.usr_table.itemSelectionChanged.connect(self._update_user_buttons)
        lay.addWidget(self.usr_table, 1)
        self.usr_note = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.usr_note)
        lay.addWidget(QLabel("LOGGED IN NOW", objectName="SectionLabel"))
        self.who_view = QPlainTextEdit(readOnly=True)
        self.who_view.setMaximumHeight(110)
        self.who_view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        lay.addWidget(self.who_view)

    # ================================================================ connection & jobs
    def _runner(self) -> d.Runner | None:
        try:
            s = self.pane.session
            if self.pane.state != "connected" or not s or not s.client:
                return None
        except RuntimeError:                          # the pane was deleted
            return None
        return d.Runner(s.client)

    def _connection_changed(self) -> None:
        if self._closed:
            return
        try:
            st = self.pane.state
        except RuntimeError:
            return
        if st == "connected":
            away = self._watch.recovered()
            self._reconnect_timer.stop()
            if away is None:
                self.banner_row.hide()
            else:
                self._banner(f"Back online after {reconnect.human(away)}.", ok=True)
                QTimer.singleShot(6000, self._hide_ok_banner)
                self._loaded.clear()
            self.refresh(force=True)
        elif st == "connecting":
            self._banner("Connecting…", buttons=not self._watch.state == "up")
        else:
            if self._watch.state == "up":
                self._watch.lost()
                self._retry_at = time.monotonic() + self._watch.next_delay()
            if not self._reconnect_timer.isActive():
                self._reconnect_timer.start()
            self._reconnect_tick(show_only=True)

    def _hide_ok_banner(self) -> None:
        if self._watch.state == "up" and not self._closed:
            self.banner_row.hide()

    def _banner(self, text: str, ok: bool = False, buttons: bool = False) -> None:
        tint = C["ok"] if ok else C["warn"]
        self.banner.setStyleSheet(f"background:{blend(C['surface'], tint, 0.18)}; border-radius:8px; padding:8px 12px;")
        self.banner.setText(text)
        self.btn_reconnect.setVisible(buttons)
        self.auto_reconnect.setVisible(buttons)
        self.banner_row.show()

    def _reconnect_tick(self, show_only: bool = False) -> None:
        """While the connection is gone: say what is happening and try again when it is time."""
        w = self._watch
        if w.state == "up":
            self._reconnect_timer.stop()
            return
        left = self._retry_at - time.monotonic()
        auto = self.auto_reconnect.isChecked() and w.should_retry()
        if auto and left <= 0 and not show_only:
            self._try_reconnect()
            return
        self._banner(w.message(left if auto else None), buttons=True)

    def _try_reconnect(self) -> None:
        w = self._watch
        w.tried()
        self._retry_at = time.monotonic() + w.next_delay()
        try:
            self.pane.reconnect()
        except RuntimeError:                           # the terminal was closed
            self._reconnect_timer.stop()

    def _reconnect_clicked(self) -> None:
        self._watch.manual()
        if self._watch.state != "up" and not self._reconnect_timer.isActive():
            self._reconnect_timer.start()
        self._try_reconnect()

    def _job(self, key: str, fn) -> None:
        if key in self._busy:
            return
        runner = self._runner()
        if runner is None:
            return
        self._busy.add(key)

        def work():
            try:
                res, err = fn(runner), ""
            except Exception as e:
                res, err = None, str(e) or e.__class__.__name__
            self._sig.done.emit(key, res, err)
        threading.Thread(target=work, daemon=True, name=f"dash-{key}").start()

    def _tab_key(self) -> str:
        return TAB_KEYS[self.tabs.currentIndex()]

    def _interval_ms(self) -> int:
        try:
            return max(1, int(self.settings.get("dashboard_interval", REFRESH_MS // 1000))) * 1000
        except (TypeError, ValueError):
            return REFRESH_MS

    def _interval_changed(self) -> None:
        sec = self.interval.currentData()
        self._timer.setInterval(sec * 1000)
        self.settings["dashboard_interval"] = sec
        if hasattr(self.settings, "save"):
            self.settings.save()

    def _tick(self) -> None:
        if not self.auto.isChecked() or not self.isVisible():
            return
        key = self._tab_key()
        if key in ("overview", "processes") or (key == "logs" and self.log_follow.isChecked()):
            self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        tab = self._tab_key()
        if not force and tab in self._loaded and tab != "overview":
            return
        if tab == "overview":
            self._job("overview", d.overview)
        elif tab == "services":
            self._job("services", d.services)
        elif tab == "processes":
            sort = "cpu" if self.ps_sort.currentIndex() == 0 else "mem"
            self._job("processes", lambda r: d.processes(r, sort, limit=500))
        elif tab == "logs":
            unit = self.log_unit.currentText().strip()
            prio = d.PRIORITIES[self.log_prio.currentText()]
            lines = int(self.log_lines.currentText().split()[0])
            init = next((s.init for s in self._services if s.unit == unit), "")
            self._job("logs", lambda r: d.logs(r, unit, prio, lines, init))
        elif tab == "ports":
            self._job("ports", d.ports)
        elif tab == "cron":
            self._cron_load()
        elif tab == "firewall":
            self._fw_load()
        elif tab == "docker":
            self._dk_load()
        elif tab == "timers":
            self._job("timers", lambda r: timers.parse(r.run(timers.READ_SCRIPT, timeout=30).out))
        elif tab == "security":
            self._sec_load(False)
        elif tab in ("system", "network", "mounts"):
            self._ct_load(tab)
        elif tab == "storage":
            self._job("storage", lambda r: storage.parse_filesystems(r.run(storage.FS_SCRIPT, timeout=20).out))
        elif tab == "updates":
            self.upd_lbl.setText("Checking for updates…")
            self._job("updates", d.updates)
        elif tab == "users":
            self._job("users", d.users)

    def _on_done(self, key: str, res, err: str) -> None:
        self._busy.discard(key)
        if self._closed:
            return
        if key == "action" and self._pending_drops:
            kind, self._pending_drops = self._pending_drops, ""
            if err or getattr(res, "ok", False):
                self._watch.expect(kind)                    # a reboot / shutdown we asked for: the drop is expected
            if err and reconnect.looks_dropped(err):
                self.status.setText(f"The server is going down as asked ({kind}).")
                self.status.setProperty("error", False)
                return
        if err:
            self.status.setText(f"{key}: {err}")
            self.status.setProperty("error", True)
            return
        handler = getattr(self, f"_show_{key}", None)
        if handler:
            handler(res)
            self._loaded.add(key)
            self.updated_lbl.setText("Updated " + time.strftime("%H:%M:%S"))
            if self.status.property("error"):          # keep success messages, drop stale errors
                self.status.setText("")
                self.status.setProperty("error", False)

    # ================================================================ renderers
    def _show_overview(self, ov: d.Overview) -> None:
        self._root = ov.root
        bits = [b for b in (ov.os, f"kernel {ov.kernel}" if ov.kernel else "", ov.arch,
                            f"up {d.human_uptime(ov.uptime_s)}" if ov.uptime_s else "") if b]
        self.subtitle.setText(f"{self.server.address}  ·  " + "  ·  ".join(bits))
        if ov.cpu_percent is not None:
            self.t_cpu.set(f"{ov.cpu_percent:.0f}%", f"{ov.cpus} CPU{'s' if ov.cpus != 1 else ''}", ov.cpu_percent)
            self.t_cpu.spark.push(ov.cpu_percent)
        else:
            self.t_cpu.set("n/a", "no /proc/stat on this system")
        if ov.mem_total_kb:
            self.t_mem.set(f"{ov.mem_percent:.0f}%", f"{d.human_kb(ov.mem_used_kb)} of {d.human_kb(ov.mem_total_kb)}",
                           ov.mem_percent)
            self.t_mem.spark.push(ov.mem_percent)
        else:
            self.t_mem.set("n/a")
        per_cpu = ov.load[0] / ov.cpus * 100 if ov.cpus else None
        self.t_load.set(f"{ov.load[0]:.2f}", f"5 min {ov.load[1]:.2f}  ·  15 min {ov.load[2]:.2f}", per_cpu)
        if ov.swap_total_kb:
            self.t_swap.set(f"{ov.swap_percent:.0f}%", f"of {d.human_kb(ov.swap_total_kb)}  ·  "
                            f"{ov.sessions} session{'s' if ov.sessions != 1 else ''}", ov.swap_percent)
        else:
            self.t_swap.set("none", f"{ov.sessions} login session{'s' if ov.sessions != 1 else ''}",
                            color_value=False)
        # disks
        while self.disks_box.count():
            w = self.disks_box.takeAt(0).widget()
            if w:
                w.setParent(None)
        for dk in ov.disks:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            name = QLabel(dk.mount)
            name.setMinimumWidth(200)
            name.setToolTip(dk.fstype)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(round(dk.percent))
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            col = C["danger"] if dk.percent >= 90 else C["warn"] if dk.percent >= 80 else C["accent"]
            bar.setStyleSheet(f"QProgressBar {{ background:{C['surface2']}; border:none; border-radius:4px; }}"
                              f"QProgressBar::chunk {{ background:{col}; border-radius:4px; }}")
            info = QLabel(f"{dk.percent:.0f}%  ·  {d.human_kb(dk.used_kb)} of {d.human_kb(dk.size_kb)}", objectName="Hint")
            info.setMinimumWidth(190)
            info.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            rl.addWidget(name)
            rl.addWidget(bar, 1)
            rl.addWidget(info)
            self.disks_box.addWidget(row)
        if not ov.disks:
            self.disks_box.addWidget(QLabel("No disk information (df not available).", objectName="Hint"))
        # services summary
        if ov.init and ov.init != "systemd":
            self.failed_lbl.setText(f"<span style='color:{C['muted']}'>Services are managed by "
                                    f"{d.INIT_NAMES.get(ov.init, ov.init)} (see the Services tab).</span>")
        elif ov.systemd in ("", "offline", "unknown"):
            what = (f"no service manager (first process: {ov.pid1})" if ov.pid1 and ov.pid1 != "systemd"
                    else "no systemd")
            self.failed_lbl.setText(f"<span style='color:{C['muted']}'>This server has {what}.</span>")
        elif ov.failed_units:
            names = ", ".join(ov.failed_units[:8]) + (" …" if len(ov.failed_units) > 8 else "")
            self.failed_lbl.setText(f"<span style='color:{C['danger']}'>● {len(ov.failed_units)} failed: {names}</span>"
                                    f" &nbsp; <a style='color:{C['accent']}' href='#'>Show</a>")
        else:
            self.failed_lbl.setText(f"<span style='color:{C['ok']}'>● All services OK</span>"
                                    f" <span style='color:{C['muted']}'>(systemd: {ov.systemd})</span>")

    def _show_services(self, res) -> None:
        svcs, problem = res
        self._services = svcs
        self.svc_msg.setText(problem)
        current = self.log_unit.currentText()
        self.log_unit.clear()
        self.log_unit.addItem("")
        self.log_unit.addItems([s.unit for s in svcs])
        self.log_unit.setCurrentText(current)
        self._fill_services()

    def _fill_services(self) -> None:
        q = self.svc_filter.text().strip().lower()
        state = self.svc_state.currentData() or ""
        want = {"": lambda s: True, "running": lambda s: s.active == "active",
                "stopped": lambda s: s.active != "active",
                "enabled": lambda s: s.enabled == "enabled", "disabled": lambda s: s.enabled == "disabled"}[state]
        rows = [s for s in self._services if (not q or q in s.unit.lower() or q in s.description.lower())
                and (not self.svc_failed.isChecked() or s.failed) and want(s)]
        rows.sort(key=lambda s: (not s.failed, s.unit))
        mixed = len({s.init for s in self._services}) > 1
        keep = self._selected(self.svc_table)
        t = self.svc_table
        with _Filling(t):
            t.setRowCount(len(rows))
            for i, s in enumerate(rows):
                col = C["danger"] if s.failed else C["ok"] if s.active == "active" else C["muted"]
                label = f"{s.unit}  [supervisor]" if mixed and s.init == "supervisor" else s.unit
                t.setItem(i, 0, _item(label, data=(s.init, s.unit)))
                t.setItem(i, 1, _item(f"● {s.active} ({s.sub})", col, sort=(0 if s.failed else 1, s.active, s.sub)))
                t.setItem(i, 2, _item(s.enabled or "–", C["muted"]))
                t.setItem(i, 3, _item(s.description, C["muted"]))
        self._reselect(t, keep)
        filtered = len(rows) != len(self._services)
        self.svc_count.setText(f"{len(rows)} of {len(self._services)}" if filtered else f"{len(rows)} services")
        n_failed = sum(s.failed for s in self._services)
        self.tabs.setTabText(1, f"Services ({n_failed} failed)" if n_failed else "Services")
        self._update_service_buttons()

    def _selected_service(self):
        """(init, unit) of the selected row, or None."""
        sel = self._selected(self.svc_table)
        return tuple(sel) if sel else None

    def _update_service_buttons(self) -> None:
        sel = self._selected_service()
        allowed = d.supported_actions(sel[0]) if sel else d.SERVICE_ACTIONS
        for a, b in self.svc_buttons.items():
            b.setEnabled(a in allowed)
            if a in ("enable", "disable"):
                b.setToolTip({"enable": "Start at boot", "disable": "Don't start at boot"}[a] if a in allowed
                             else "supervisord programs start at boot through autostart= in their config")

    def _show_processes(self, procs: list) -> None:
        self._procs = procs
        current = self.ps_user.currentData() or ""
        self.ps_user.blockSignals(True)
        self.ps_user.clear()
        self.ps_user.addItem("All users", "")
        for u in sorted({p.user for p in procs}):
            self.ps_user.addItem(u, u)
        self.ps_user.setCurrentIndex(max(0, self.ps_user.findData(current)))
        self.ps_user.blockSignals(False)
        self._fill_processes()

    def _fill_processes(self) -> None:
        q = self.ps_filter.text().strip().lower()
        user = self.ps_user.currentData() or ""
        procs = [p for p in self._procs if (not q or q in p.command.lower() or q in p.user.lower()
                                            or q in str(p.pid)) and (not user or p.user == user)]
        keep = self._selected(self.ps_table)
        t = self.ps_table
        with _Filling(t):
            t.setRowCount(len(procs))
            for i, p in enumerate(procs):
                t.setItem(i, 0, _item(p.pid, align_right=True, data=p.pid))
                t.setItem(i, 1, _item(p.user, C["muted"]))
                t.setItem(i, 2, _item(f"{p.cpu:.1f}", C["warn"] if p.cpu >= 50 else None, True))
                t.setItem(i, 3, _item(f"{p.mem:.1f}", None, True))
                t.setItem(i, 4, _item(d.human_kb(p.rss_kb), C["muted"], True, sort=(0, p.rss_kb * 1024.0)))
                t.setItem(i, 5, _item(p.elapsed, C["muted"], True))
                t.setItem(i, 6, _item(p.command))
        self._reselect(t, keep)
        self.ps_count.setText(f"{len(procs)} of {len(self._procs)}" if len(procs) != len(self._procs)
                              else f"{len(procs)} processes")

    def _show_alerts(self) -> None:
        from .alerts_ui import AlertsDialog
        AlertsDialog(self, self.server.id).exec()

    def _show_logs(self, text: str) -> None:
        bar = self.log_view.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self._log_raw = text.rstrip()
        self._apply_log_filter()
        if at_end or self.log_follow.isChecked() or "logs" not in self._loaded:
            bar.setValue(bar.maximum())

    def _apply_log_filter(self) -> None:
        lines = self._log_raw.splitlines()
        needle = self.log_find.text().strip().lower()
        only = self.log_only.currentData() or ""
        shown, hits = loglines.matching(lines, needle, int(self.log_around.currentData() or 0), only)
        self.log_around.setEnabled(bool(needle or only))
        self.log_count.setText(f"{hits} of {len(lines)} lines" if needle or only else "")
        self._log_hl.needle = needle
        self.log_view.setPlainText("\n".join(shown) or ("(no lines match the filter)" if lines else "(no log lines)"))

    def _next_log_error(self) -> None:
        """Move to the next line that looks like an error (from the cursor, wrapping around once)."""
        doc = self.log_view.document()
        start = self.log_view.textCursor().blockNumber() + 1
        count = doc.blockCount()
        for k in range(count):
            block = doc.findBlockByNumber((start + k) % count)
            if loglines.severity(block.text()) == "error":
                cur = QTextCursor(block)
                cur.select(QTextCursor.LineUnderCursor)
                self.log_view.setTextCursor(cur)
                self.log_view.centerCursor()
                self.log_count.setText(self.log_count.text().split("  ·  ")[0] +
                                       f"  ·  error at line {block.blockNumber() + 1}")
                return
        self.status.setText("No error lines in what is shown.")

    def _log_view_list(self) -> list:
        return list((self.settings.get("log_views") or {}).get(self.server.id, []))

    def _store_log_views(self, views: list) -> None:
        every = dict(self.settings.get("log_views") or {})
        every[self.server.id] = views
        self.settings["log_views"] = every
        if hasattr(self.settings, "save"):
            self.settings.save()

    def _fill_log_views(self, select: str = "") -> None:
        self.log_views.clear()
        self.log_views.addItem("Saved views…", None)
        for v in self._log_view_list():
            self.log_views.addItem(v["name"], v)
        if select:
            self.log_views.setCurrentIndex(max(0, self.log_views.findText(select)))

    def _save_log_view(self) -> None:
        unit = self.log_unit.currentText().strip()
        name, ok = QInputDialog.getText(self, "Save view", "Name for this view:", text=unit or "All logs")
        name = name.strip()
        if not ok or not name:
            return
        view = {"name": name, "unit": unit, "prio": self.log_prio.currentText(),
                "lines": self.log_lines.currentText(), "find": self.log_find.text(),
                "only": self.log_only.currentData() or "", "around": int(self.log_around.currentData() or 0)}
        views = [v for v in self._log_view_list() if v["name"] != name] + [view]
        self._store_log_views(views)
        self._fill_log_views(name)

    def _apply_log_view(self, _i: int) -> None:
        v = self.log_views.currentData()
        if not v:
            return
        self.log_unit.setCurrentText(v.get("unit", ""))
        self.log_prio.setCurrentIndex(max(0, self.log_prio.findText(v.get("prio", ""))))
        self.log_lines.setCurrentIndex(max(0, self.log_lines.findText(v.get("lines", ""))))
        self.log_find.setText(v.get("find", ""))
        self.log_only.setCurrentIndex(max(0, self.log_only.findData(v.get("only", ""))))
        self.log_around.setCurrentIndex(max(0, self.log_around.findData(int(v.get("around", 0)))))
        self.refresh(force=True)

    def _delete_log_view(self) -> None:
        v = self.log_views.currentData()
        if v:
            self._store_log_views([x for x in self._log_view_list() if x["name"] != v["name"]])
            self._fill_log_views()

    def _show_ports(self, ports: list) -> None:
        self._ports = ports
        self._fill_ports()

    def _fill_ports(self) -> None:
        q = self.port_filter.text().strip().lower()
        ports = [p for p in self._ports if not q or q in str(p.port) or q in p.address.lower()
                 or q in (p.process or "").lower() or q in p.proto.lower()
                 or (q in "all interfaces" and p.address in ("*", "0.0.0.0", "::"))]
        t = self.port_table
        with _Filling(t):
            t.setRowCount(len(ports))
            for i, p in enumerate(ports):
                public = p.address in ("*", "0.0.0.0", "::")
                t.setItem(i, 0, _item(p.proto.upper(), C["muted"]))
                t.setItem(i, 1, _item("all interfaces" if public else p.address, C["warn"] if public else None))
                t.setItem(i, 2, _item(p.port, None, True))
                t.setItem(i, 3, _item(p.process or "–", C["muted"]))
        self.port_count.setText(f"{len(ports)} of {len(self._ports)}" if len(ports) != len(self._ports)
                                else f"{len(ports)} listening")

    def _show_updates(self, res) -> None:
        mgr, ups = res
        self._update_mgr = mgr
        self.upd_btn.setEnabled(bool(mgr and ups and self.send_to_terminal))
        can_install = bool(self.settings.get("dashboard_install_updates")) and bool(d.install_command(mgr))
        self.upd_install.setVisible(bool(self.settings.get("dashboard_install_updates")))
        self.upd_install.setEnabled(bool(can_install and ups))
        self.upd_hint.setText("Uses the server's cached package lists. “Install updates” runs the upgrade for you "
                              "after a confirmation." if self.settings.get("dashboard_install_updates") else
                              "Read-only: this uses the server's cached package lists and never installs anything. "
                              "Turn on “Allow installing updates” in Settings to install from here.")
        if not mgr:
            self.upd_lbl.setText("No supported package manager found (apt, dnf, yum, zypper, pacman, apk).")
        elif not ups:
            self.upd_lbl.setText(f"<span style='color:{C['ok']}'>● Up to date</span> ({mgr})")
        else:
            self.upd_lbl.setText(f"<b>{len(ups)}</b> update{'s' if len(ups) != 1 else ''} available ({mgr})")
        self._upd_list = ups
        self._fill_updates()
        self.tabs.setTabText(5, f"Updates ({len(ups)})" if ups else "Updates")

    def _fill_updates(self) -> None:
        q = self.upd_filter.text().strip().lower()
        ups = [u for u in self._upd_list if not q or q in u.package.lower()]
        t = self.upd_table
        with _Filling(t):
            t.setRowCount(len(ups))
            for i, u in enumerate(ups):
                t.setItem(i, 0, _item(u.package))
                t.setItem(i, 1, _item(u.version or "–", C["muted"]))

    def _reselect(self, table: QTableWidget, key) -> None:
        """Select the row whose first cell carries `key` again (after the table was refilled or re-sorted)."""
        if key is None:
            return
        for i in range(table.rowCount()):
            it = table.item(i, 0)
            have = it.data(Qt.UserRole) if it else None
            if have == key or (isinstance(have, (list, tuple)) and isinstance(key, (list, tuple))
                               and tuple(have) == tuple(key)):
                table.selectRow(i)
                return

    def _show_users(self, res) -> None:
        accounts, sessions, groups = res
        self._every_account, self._all_groups = accounts, groups
        self._fill_users()
        self.who_view.setPlainText("\n".join(sessions) or "Nobody is logged in (besides non-interactive sessions).")

    def _fill_users(self) -> None:
        every = self._every_account
        accounts = every if self.usr_system.isChecked() else [a for a in every if not a.system]
        self._accounts = accounts
        t = self.usr_table
        t.setRowCount(len(accounts))
        for i, a in enumerate(accounts):
            t.setItem(i, 0, _item(a.name, C["warn"] if a.uid == 0 else None))
            t.setItem(i, 1, _item(a.uid, C["muted"], True))
            t.setItem(i, 2, _item(a.logged_in or "–", C["ok"] if a.logged_in else C["muted"], True))
            t.setItem(i, 3, _item(", ".join(a.groups) or "–", C["muted"]))
            t.setItem(i, 4, _item("locked" if a.locked else ("–" if a.locked is False else "?"),
                                  C["warn"] if a.locked else C["muted"]))
            t.setItem(i, 5, _item(a.shell, C["muted"]))
            t.setItem(i, 6, _item(a.home, C["muted"]))
        hidden = len(every) - len(accounts)
        if not every:
            note = ("The server returned no account list (getent passwd and /etc/passwd were empty or "
                    "unreadable for this login).")
        elif hidden:
            note = f"{hidden} system account{'s' if hidden != 1 else ''} hidden."
        else:
            note = ""
        self.usr_note.setText(note)
        self._update_user_buttons()

    # ================================================================ actions
    def _selected(self, table: QTableWidget):
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        return table.item(rows[0].row(), 0).data(Qt.UserRole)

    def _show_failed_services(self) -> None:
        self.tabs.setCurrentIndex(1)
        self.svc_failed.setChecked(True)

    def _service_status(self) -> None:
        sel = self._selected_service()
        if not sel:
            return
        init, unit = sel
        self._job("status", lambda r: (unit, r.run(d.status_command(unit, init)).out))

    def _show_status(self, res) -> None:
        unit, text = res
        box = QMessageBox(self)
        box.setWindowTitle(unit)
        box.setText(f"<b>{unit}</b>")
        box.setDetailedText(text)
        box.setInformativeText("\n".join(text.splitlines()[:8]))
        box.exec()

    def _service_logs(self) -> None:
        sel = self._selected_service()
        if not sel:
            return
        unit = sel[1]
        self.log_unit.setCurrentText(unit)
        self.tabs.setCurrentIndex(3)
        self.refresh(force=True)

    def _service_action(self, action: str) -> None:
        sel = self._selected_service()
        if not sel:
            self.status.setText("Select a service first.")
            return
        init, unit = sel
        if action not in d.supported_actions(init):
            self.status.setText(f"{action.capitalize()} isn't available for {d.INIT_NAMES.get(init, init)}.")
            return
        cmd = d.service_action_command(action, unit, init)
        self._privileged(f"{action.capitalize()} {unit}", cmd, then="services")

    def _kill(self, force: bool) -> None:
        pid = self._selected(self.ps_table)
        if not pid:
            self.status.setText("Select a process first.")
            return
        row = self.ps_table.currentRow()
        name = self.ps_table.item(row, 6).text()[:80] if row >= 0 else ""
        self._privileged(f"{'Force-kill' if force else 'End'} process {pid} ({name})",
                         d.kill_command(pid, force), then="processes", allow_plain=True)

    # ---- packages: search, install, remove ----
    def _pkg_allowed(self) -> bool:
        return bool(self.settings.get("dashboard_install_updates"))

    def _pkg_search(self) -> None:
        q = self.pkg_query.text().strip()
        mgr = getattr(self, "_update_mgr", "")
        if not q:
            return
        if not packages.valid_query(q):
            self.pkg_count.setText("Use letters, digits and . + _ - only.")
            return
        if mgr not in packages.MANAGERS:
            self.pkg_count.setText("Open the Updates tab first (it finds the package manager)." if not mgr else
                                   f"Searching isn't supported for {mgr}.")
            return
        self.pkg_count.setText("Searching …")
        self._pkg_last = q
        self._job("packages", lambda r: (q, packages.parse_search(
            mgr, r.run(packages.search_script(mgr, q), timeout=90).out, q)))

    def _show_packages(self, res) -> None:
        q, found = res
        self._pkg_list = found
        t = self.pkg_table
        with _Filling(t):
            t.setRowCount(len(found))
            for i, p in enumerate(found):
                t.setItem(i, 0, _item(p.name, C["ok"] if p.installed else None, data=p.name))
                t.setItem(i, 1, _item("installed" if p.installed else "–", C["ok"] if p.installed else C["muted"]))
                t.setItem(i, 2, _item(p.version or "–", C["muted"]))
                t.setItem(i, 3, _item(p.summary or "", C["muted"]))
        have = sum(1 for p in found if p.installed)
        more = " (the first ones only: be more specific)" if len(found) >= packages.MAX_RESULTS // 2 else ""
        self.pkg_count.setText(f"No package matches “{q}” in the server's package lists." if not found else
                               f"{len(found)} found, {have} installed{more}")
        self._update_pkg_buttons()

    def _selected_package(self):
        name = self._selected(self.pkg_table)
        return next((p for p in self._pkg_list if p.name == name), None) if name else None

    def _update_pkg_buttons(self) -> None:
        p = self._selected_package()
        allowed = self._pkg_allowed()
        for b in (self.pkg_install, self.pkg_remove):
            b.setVisible(allowed)
        self.pkg_install.setEnabled(bool(allowed and p and not p.installed))
        self.pkg_remove.setEnabled(bool(allowed and p and p.installed and not packages.protected(p.name)))
        self.pkg_remove.setToolTip(packages.why_protected(p.name) if p and p.installed and packages.protected(p.name)
                                   else "Remove the selected package (shows what else goes with it, asks first)")

    def _pkg_change(self, how: str) -> None:
        p = self._selected_package()
        mgr = getattr(self, "_update_mgr", "")
        if not p or not self._pkg_allowed():
            return
        try:
            cmd, show = (packages.install_command if how == "install" else packages.remove_command)(mgr, p.name)
        except ValueError as e:
            QMessageBox.information(self, "Packages", str(e))
            return
        if how == "remove":
            preview = packages.removal_preview_command(mgr, p.name)
            if preview:                                  # say what else would go, before asking
                self.pkg_count.setText("Checking what the removal would take with it …")
                self._pkg_pending = (p.name, cmd, show)
                self._job("pkg_preview", lambda r: packages.parse_preview(r.run(preview, timeout=60).out, p.name))
                return
        self._pkg_run(how, p.name, cmd, show)

    def _show_pkg_preview(self, others: list) -> None:
        name, cmd, show = self._pkg_pending
        self.pkg_count.setText("")
        if others:
            listed = ", ".join(others[:15]) + (f" and {len(others) - 15} more" if len(others) > 15 else "")
            if any(packages.protected(o) for o in others):
                QMessageBox.warning(self, "Remove package",
                                    f"Removing {name} would also remove {listed}, including packages the system "
                                    "needs. Not done: use the terminal if you really mean it.")
                return
            if QMessageBox.question(self, "Remove package", f"Removing {name} also removes:\n\n{listed}\n\n"
                                    "Continue?") != QMessageBox.Yes:
                return
        self._pkg_run("remove", name, cmd, show)

    def _pkg_run(self, how: str, name: str, cmd: str, show: str) -> None:
        self._privileged(f"{how.capitalize()} {name}", cmd, then="updates", timeout=900, show=show)

    def _install_updates(self) -> None:
        cmd = d.install_command(self._update_mgr)
        if not cmd or not self.settings.get("dashboard_install_updates"):
            return
        self._privileged("Install all updates", cmd, then="updates", timeout=1800)

    def _privileged(self, what: str, command: str, then: str, allow_plain: bool = False,
                    timeout: float = 30, show: str = "", plain_only: bool = False, drops: str = "") -> None:
        """Confirm, then run as root (directly, or via sudo when needed). `show` is what the
        confirmation and the command log display instead of a long / encoded command;
        `plain_only` never falls back to sudo (the plain attempt's error is shown)."""
        runner = self._runner()
        if runner is None:
            return
        via = "" if self._root else "sudo "
        box = QMessageBox(QMessageBox.Warning if self.color else QMessageBox.Question,
                          "Confirm", f"<b>{what}</b> on <b>{self.server.label}</b>?", parent=self)
        box.setInformativeText(f"Runs:  {via}{show or command}")
        ok = box.addButton(what.split()[0], QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not ok:
            return

        def work(r: d.Runner):
            if allow_plain and not self._root:
                plain = r.run(command)            # own processes don't need sudo
                if plain.ok or plain_only:
                    return plain
            if not self._root and self._sudo_pw is None and d.needs_password(r, self._root):
                return "need-password"
            return d.run_privileged(r, command, self._root, self._sudo_pw, timeout)
        self._pending_action = (what, command, then, allow_plain, timeout, show)
        self._pending_drops = drops
        self._job("action", work)

    def _show_action(self, res) -> None:
        what, command, then, allow_plain, timeout, show = self._pending_action
        if res == "need-password":
            pw, ok = QInputDialog.getText(self, "sudo password",
                                          f"Password for sudo on {self.server.label} "
                                          f"(used for this dashboard only, never saved):", QLineEdit.Password)
            if not ok:
                return
            self._sudo_pw = pw
            self._job("action", lambda r: d.run_privileged(r, command, self._root, self._sudo_pw, timeout))
            return
        if hasattr(self.pane, "log_command"):        # command log: the dashboard's actions too
            self.pane.log_command((show or command) + ("" if res.ok else f"   # failed (exit {res.code})"),
                                  "dashboard")
        if res.ok:
            self.status.setText(f"✔ {what}: done")
            backup = getattr(self, "_cron_backup", None)
            if then == "cron" and backup:
                kept = cron.save_backup(self.server.id, backup[0], backup[1])
                self._cron_backup = None
                if kept:
                    self.status.setText(f"✔ {what}: done (the previous crontab is kept under Restore…)")
        else:
            msg = (res.err or res.out).strip() or f"exit code {res.code}"
            if "incorrect password" in msg.lower() or "sorry, try again" in msg.lower():
                self._sudo_pw = None
                msg = "Wrong sudo password."
            QMessageBox.warning(self, what, msg[:1500])
        self._loaded.discard(then)
        if self._tab_key() == then:
            self.refresh(force=True)
        if then == "updates" and what.startswith(("Install ", "Remove ")) and self._pkg_last:
            self.pkg_query.setText(self._pkg_last)
            self._pkg_search()

    # ---- services: unit file and creating one ----
    def _view_unit(self) -> None:
        sel = self._selected_service()
        if not sel:
            self.status.setText("Select a service first.")
            return
        init, unit = sel
        if init != "systemd":
            self.status.setText("Unit files exist on systemd servers only.")
            return
        self._job("unitcat", lambda r: (unit, r.run(f"systemctl cat {shlex.quote(unit)} --no-pager 2>&1").out))

    def _show_unitcat(self, res) -> None:
        unit, text = res
        _ViewDialog(f"Unit file: {unit}", text, self).exec()

    def _new_service(self) -> None:
        if not self._services or self._services[0].init != "systemd":
            QMessageBox.information(self, "New service", "Creating services from here works on servers that run "
                                    "systemd. Open the Services tab first so the server's init system is known.")
            return
        dlg = _NewServiceDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        name, text, enable, start = dlg.values()
        if any(s.unit == f"{name}.service" for s in self._services):
            QMessageBox.warning(self, "New service", f"{name}.service already exists on this server.")
            return
        todo = "write the unit file" + (", enable it at boot" if enable else "") + (", start it" if start else "")
        self._privileged(f"Create service {name}", units.create_command(name, text, enable, start),
                         then="services", timeout=60,
                         show=f"{todo}: /etc/systemd/system/{name}.service")

    # ---- ssh keys (Users tab) ----
    def _priv_read(self, r: d.Runner, command: str) -> str:
        """Read something as the connected user, or as root when that is refused."""
        out = r.run(command, timeout=20).out
        if "Permission denied" not in out or self._root:
            return out
        if self._sudo_pw is None and d.needs_password(r, self._root):
            return "need-password"
        return d.run_privileged(r, command, self._root, self._sudo_pw, 20).out

    def _ssh_keys(self) -> None:
        a = self._selected_account()
        if a is None or not a.home:
            return
        home, name = a.home, a.name
        self._job("sshkeys", lambda r: (name, home, self._priv_read(r, sshkeys.read_command(home))))

    def _show_sshkeys(self, res) -> None:
        name, home, out = res
        if out == "need-password":
            pw, ok = QInputDialog.getText(self, "sudo password", f"Password for sudo on {self.server.label} "
                                          "(used for this dashboard only, never saved):", QLineEdit.Password)
            if ok:
                self._sudo_pw = pw
                self._ssh_keys()
            return
        text = sshkeys.clean_read(out)
        dlg = _SshKeysDialog(name, home, text, name == (self.server.username or ""), self)
        if dlg.exec() != QDialog.Accepted or dlg.text == text:
            return
        cron.save_backup(self.server.id, f"ssh-{name}", text)           # keep what was there
        n = len([k for k in sshkeys.parse(dlg.text) if k.valid])
        self._privileged(f"Update SSH keys of {name}", sshkeys.save_command(home, name, dlg.text), then="users",
                         allow_plain=True, show=f"write {home}/.ssh/authorized_keys ({n} key{'s' if n != 1 else ''})")

    # ---- docker ----
    def _build_docker(self) -> None:
        lay = self._page("Docker")
        self._dk = docker.Containers()
        self._dk_sudo = False
        self.dk_lbl = QLabel("")
        self.dk_clean = QToolButton()
        self.dk_clean.setText("Clean up  ▾")
        self.dk_clean.setPopupMode(QToolButton.InstantPopup)
        self.dk_clean.setStyleSheet(f"QToolButton {{ border:1px solid {C['border']}; padding:6px 12px; }}")
        self.dk_clean.setToolTip("Free disk space: stopped containers, unused images, volumes, networks, build cache "
                                 "(each one asks first)")
        clean_menu = QMenu(self.dk_clean)
        clean_menu.aboutToShow.connect(lambda: self._dk_fill_clean(clean_menu))
        self.dk_clean.setMenu(clean_menu)
        self.dk_tools = [
            _btn("gauge", "Disk use", lambda: self._dk_engine_view("Disk use", docker.disk_usage_command),
                 "What images, containers, volumes and the build cache take, and what could be freed"),
            _btn("bolt", "Events", lambda: self._dk_engine_view("Events (last hour)", docker.events_command),
                 "What happened in the last hour: containers that died or were OOM-killed, restarts, pulls"),
            _btn("help", "Engine info", lambda: self._dk_engine_view("Engine info", docker.info_command),
                 "Version, storage driver, root folder, warnings"),
            self.dk_clean]
        lay.addLayout(_toolbar(self.dk_lbl, "stretch", *self.dk_tools,
                               _btn("refresh", "", lambda: self.refresh(force=True), "Reload the lists")))
        self.dk_tabs = QTabWidget()
        lay.addWidget(self.dk_tabs, 1)

        # containers
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 8, 0, 0)
        self.dk_buttons = {
            "start": _btn("bolt", "Start", lambda: self._dk_action("start")),
            "stop": _btn("x", "Stop", lambda: self._dk_action("stop")),
            "restart": _btn("refresh", "Restart", lambda: self._dk_action("restart")),
            "logs": _btn("file", "Logs", self._dk_logs, "The last 300 log lines of the selected container "
                                                         "(also: double-click)"),
            "details": _btn("search", "Details", self._dk_details,
                            "Why it stopped (exit code, out of memory), restarts, health, limits, ports, mounts, "
                            "environment (secrets hidden) and the full inspect output"),
            "top": _btn("gauge", "Processes", self._dk_top, "The processes running inside the selected container"),
            "shell": _btn("terminal", "Shell", self._dk_shell,
                          "Types a command into this server's terminal that opens a shell inside the container"),
            "remove": _btn("trash", "Remove", self._dk_remove, "Delete the selected container"),
        }
        pl.addLayout(_toolbar("stretch", *self.dk_buttons.values()))
        self.dk_table = _table(["State", "Name", "Image", "Status", "Ports", "CPU", "Memory"], stretch=4)
        self.dk_table.itemSelectionChanged.connect(self._update_dk_buttons)
        self.dk_table.doubleClicked.connect(lambda _i: self._dk_logs())
        pl.addWidget(self.dk_table, 1)
        self.dk_tabs.addTab(page, "Containers")

        # compose projects
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 8, 0, 0)
        self._dk_projects: list = []
        self.dk_cmp_buttons = {
            "up": _btn("bolt", "Up", lambda: self._cmp_action("up"), "Start the project (creates what is missing)"),
            "stop": _btn("x", "Stop", lambda: self._cmp_action("stop")),
            "restart": _btn("refresh", "Restart", lambda: self._cmp_action("restart")),
            "update": _btn("download", "Update", lambda: self._cmp_action("update"),
                           "Pull newer images and re-create the services that changed (pull, then up -d)"),
            "down": _btn("trash", "Down", lambda: self._cmp_action("down"),
                         "Remove the project's containers and networks (volumes and their data are kept)"),
            "logs": _btn("file", "Logs", lambda: self._cmp_view("logs"), "The last 300 log lines of every service"),
            "ps": _btn("search", "Status", lambda: self._cmp_view("ps"), "compose ps: every container of the project"),
            "config": _btn("code", "Check config", lambda: self._cmp_view("config"),
                           "The configuration as compose reads it (variables filled in, files merged), or its errors"),
            "files": _btn("edit", "Files…", self._cmp_files, "View or edit the compose files (checked before saving)"),
        }
        pl.addLayout(_toolbar("stretch", *self.dk_cmp_buttons.values()))
        self.dk_cmp_table = _table(["Project", "State", "Services", "Folder", "Compose files"], stretch=4)
        self.dk_cmp_table.itemSelectionChanged.connect(self._cmp_selected_changed)
        self.dk_cmp_table.doubleClicked.connect(lambda _i: self._cmp_view("ps"))
        pl.addWidget(self.dk_cmp_table, 2)
        self.dk_svc_buttons = {
            "logs": _btn("file", "Logs", lambda: self._cmp_view("logs", service=True), "This service's log"),
            "restart": _btn("refresh", "Restart", lambda: self._cmp_action("restart", service=True)),
            "recreate": _btn("bolt", "Re-create", self._cmp_recreate,
                             "Re-create this service's containers (after its image or settings changed)"),
        }
        pl.addLayout(_toolbar(QLabel("<b>Services</b>"), "stretch", *self.dk_svc_buttons.values()))
        self.dk_svc_table = _table(["Service", "State", "Containers", "Image"], stretch=3)
        self.dk_svc_table.itemSelectionChanged.connect(self._update_dk_buttons)
        self.dk_svc_table.doubleClicked.connect(lambda _i: self._cmp_view("logs", service=True))
        pl.addWidget(self.dk_svc_table, 1)
        self.dk_cmp_note = QLabel("", objectName="Hint", wordWrap=True)
        pl.addWidget(self.dk_cmp_note)
        self.dk_tabs.addTab(page, "Compose")

        # images
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 8, 0, 0)
        self.dk_img_buttons = {
            "details": _btn("search", "Details", lambda: self._dk_inspect("image"), "The image's inspect output"),
            "history": _btn("code", "Layers", self._dk_history, "How the image was built, layer by layer, with sizes"),
            "pull": _btn("download", "Pull again", self._dk_pull,
                         "Download this tag again (gets a newer version if there is one; running containers keep "
                         "the old one until they are re-created)"),
            "remove": _btn("trash", "Remove", self._dk_remove_image, "Delete the image (not while a container uses it)"),
        }
        pl.addLayout(_toolbar("stretch", *self.dk_img_buttons.values()))
        self.dk_img_table = _table(["Repository", "Tag", "ID", "Size", "Created", "Used by"], stretch=5, sortable=True)
        self.dk_img_table.itemSelectionChanged.connect(self._update_dk_buttons)
        self.dk_img_table.doubleClicked.connect(lambda _i: self._dk_inspect("image"))
        pl.addWidget(self.dk_img_table, 1)
        self.dk_tabs.addTab(page, "Images")

        # volumes
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 8, 0, 0)
        self.dk_vol_buttons = {
            "details": _btn("search", "Details", lambda: self._dk_inspect("volume"), "Where its data is, labels"),
            "remove": _btn("trash", "Remove", self._dk_remove_volume, "Delete the volume and its data"),
        }
        pl.addLayout(_toolbar("stretch", *self.dk_vol_buttons.values()))
        self.dk_vol_table = _table(["Volume", "Driver", "Mount point"], stretch=2, sortable=True)
        self.dk_vol_table.itemSelectionChanged.connect(self._update_dk_buttons)
        self.dk_vol_table.doubleClicked.connect(lambda _i: self._dk_inspect("volume"))
        pl.addWidget(self.dk_vol_table, 1)
        self.dk_tabs.addTab(page, "Volumes")

        # networks
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 8, 0, 0)
        self.dk_net_buttons = {
            "details": _btn("search", "Details", lambda: self._dk_inspect("network"),
                            "Subnet, gateway and the containers attached to it"),
            "remove": _btn("trash", "Remove", self._dk_remove_network, "Delete the network"),
        }
        pl.addLayout(_toolbar("stretch", *self.dk_net_buttons.values()))
        self.dk_net_table = _table(["Network", "Driver", "Scope", "ID"], stretch=0, sortable=True)
        self.dk_net_table.itemSelectionChanged.connect(self._update_dk_buttons)
        self.dk_net_table.doubleClicked.connect(lambda _i: self._dk_inspect("network"))
        pl.addWidget(self.dk_net_table, 1)
        self.dk_tabs.addTab(page, "Networks")
        for table, buttons in ((self.dk_table, self.dk_buttons), (self.dk_cmp_table, self.dk_cmp_buttons),
                               (self.dk_svc_table, self.dk_svc_buttons), (self.dk_img_table, self.dk_img_buttons),
                               (self.dk_vol_table, self.dk_vol_buttons), (self.dk_net_table, self.dk_net_buttons)):
            table.setContextMenuPolicy(Qt.CustomContextMenu)
            table.customContextMenuRequested.connect(lambda pos, t=table, b=buttons: self._dk_menu(t, b, pos))

        self.dk_hint = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.dk_hint)
        self._update_dk_buttons()

    def _dk_load(self) -> None:
        def work(r: d.Runner):
            out = r.run(docker.READ_SCRIPT, timeout=45).out
            used = False
            if docker.parse(out).needs_access and not self._root:
                if self._sudo_pw is None and d.needs_password(r, self._root):
                    return "need-password", False
                res = d.run_privileged(r, f"sh -c {shlex.quote(docker.READ_SCRIPT)}", self._root, self._sudo_pw, 60)
                if res.ok:
                    out, used = res.out, True
            return out, used
        self._job("docker", work)

    def _show_docker(self, res) -> None:
        out, used = res
        if out == "need-password":
            pw, ok = QInputDialog.getText(self, "sudo password", f"Password for sudo on {self.server.label} "
                                          "(used for this dashboard only, never saved):", QLineEdit.Password)
            if ok:
                self._sudo_pw = pw
                self._dk_load()
            else:
                self.dk_hint.setText("Docker needs root or membership of the docker group.")
            return
        c = docker.parse(out)
        self._dk, self._dk_sudo = c, used
        t = self.dk_table
        t.setRowCount(len(c.items))
        colors = {"running": C["ok"], "paused": C["warn"], "restarting": C["warn"], "dead": C["danger"]}
        for i, x in enumerate(c.items):
            t.setItem(i, 0, _item("● " + x.state, colors.get(x.state, C["muted"])))
            t.setItem(i, 1, _item(x.name))
            t.setItem(i, 2, _item(x.image, C["muted"]))
            t.setItem(i, 3, _item(x.status, C["muted"]))
            t.setItem(i, 4, _item(x.ports or "–", C["muted"]))
            t.setItem(i, 5, _item(x.cpu or "–", None, True))
            t.setItem(i, 6, _item(x.mem or "–", None, True))
        self._dk_fill_others(c)
        if c.engine == "none":
            self.dk_lbl.setText("No Docker or Podman found")
            self.dk_hint.setText("Looked for the docker and podman commands.")
        elif c.needs_access:
            self.dk_lbl.setText(f"<b>{c.engine}</b>  ·  no access")
            self.dk_hint.setText("This account can't talk to the daemon. Use root, add it to the docker group, or "
                                 "use an account with sudo." + (f"  ({c.error})" if c.error else "")
                                 + (" The daemon may not be running: sudo systemctl start docker."
                                    if "daemon running" in c.error.lower() else ""))
        else:
            run = sum(1 for x in c.items if x.state == "running")
            totals = c.totals_text()
            self.dk_lbl.setText(f"<b>{c.engine}</b>  ·  {run} running, {len(c.items) - run} not running"
                                + (f"  ·  <b>{totals}</b>" if totals else ""))
            self.dk_lbl.setToolTip("Summed over the running containers. Docker counts CPU per core: 100 % is one "
                                   "full CPU." if totals else "")
            self.dk_hint.setText("Via sudo. " if used else "")
        self._update_dk_buttons()

    def _selected_container(self) -> docker.Container | None:
        rows = self.dk_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._dk.items):
            return None
        return self._dk.items[rows[0].row()]

    def _update_dk_buttons(self) -> None:
        x = self._selected_container()
        for k, b in self.dk_buttons.items():
            on = x is not None
            if k == "start":
                on = on and not x.running or (on and x.state == "paused")
            elif k in ("stop", "restart", "top"):
                on = on and x.running
            elif k == "shell":
                on = on and x.state == "running" and bool(self.send_to_terminal)
            b.setEnabled(bool(on))
        engine_ok = self._dk.engine in ("docker", "podman") and not self._dk.needs_access
        for b in self.dk_tools:
            b.setEnabled(engine_ok)
        img = self._dk_selected_image()
        for k, b in self.dk_img_buttons.items():
            b.setEnabled(img is not None and not (k == "pull" and img.dangling))
        if img is not None and img.used_by:
            self.dk_img_buttons["remove"].setEnabled(False)
            self.dk_img_buttons["remove"].setToolTip(f"Used by {', '.join(img.used_by[:5])}: remove those containers "
                                                     "first")
        else:
            self.dk_img_buttons["remove"].setToolTip("Delete the image (not while a container uses it)")
        for b in self.dk_vol_buttons.values():
            b.setEnabled(self._dk_selected("vol") is not None)
        p = self._cmp_selected()
        ok = p is not None and p.manageable and bool(self._dk.compose)
        for k, b in self.dk_cmp_buttons.items():
            b.setEnabled(ok)
        svc = self._cmp_service()
        for b in self.dk_svc_buttons.values():
            b.setEnabled(ok and svc is not None)
        net = self._dk_selected("net")
        self.dk_net_buttons["details"].setEnabled(net is not None)
        self.dk_net_buttons["remove"].setEnabled(net is not None and not net.builtin)
        self.dk_net_buttons["remove"].setToolTip("The engine's own network can't be removed." if net is not None and
                                                 net.builtin else "Delete the network")
        if x is not None and x.state == "paused":
            self.dk_buttons["start"].setText(" Resume")
        else:
            self.dk_buttons["start"].setText(" Start")

    def _dk_action(self, action: str) -> None:
        x = self._selected_container()
        if not x:
            return
        if action == "start" and x.state == "paused":
            action = "unpause"
        cmd = docker.action_command(self._dk.engine, action, x.id)
        self._privileged(f"{action.capitalize()} container {x.name}", cmd, then="docker", allow_plain=True)

    def _dk_remove(self) -> None:
        x = self._selected_container()
        if not x:
            return
        cmd = docker.action_command(self._dk.engine, "remove", x.id, force=x.running)
        self._privileged(f"Remove container {x.name}" + (" (it is running: forced)" if x.running else ""), cmd,
                         then="docker", allow_plain=True)

    def _dk_menu(self, table: QTableWidget, buttons: dict, pos) -> None:
        """Right-click: the row under the mouse, and the same actions as the toolbar (greyed out the same way)."""
        m = self._dk_menu_for(table, buttons, pos)
        if m is not None:
            m.exec(table.viewport().mapToGlobal(pos))

    def _dk_menu_for(self, table: QTableWidget, buttons: dict, pos):
        row = table.rowAt(pos.y())
        if row < 0:
            return None
        table.selectRow(row)
        m = QMenu(table)
        for b in buttons.values():
            if b.isHidden():
                continue
            act = m.addAction(b.icon(), b.text().strip(), b.click)
            act.setEnabled(b.isEnabled())
            if b.toolTip() and not b.isEnabled():
                act.setToolTip(b.toolTip())
        m.setToolTipsVisible(True)
        if table is self.dk_table:
            m.addSeparator()
            for b in self.dk_tools[:3]:                      # disk use, events, engine info
                m.addAction(b.icon(), b.text().strip(), b.click).setEnabled(b.isEnabled())
        return m

    def _dk_logs(self) -> None:
        x = self._selected_container()
        if x:
            self._dk_view(f"Logs: {x.name}", docker.logs_command(self._dk.engine, x.id))

    def _dk_view(self, title: str, cmd: str, summarize: bool = False, timeout: float = 60) -> None:
        """Run a read-only command (as yourself, or with sudo when the lists needed it) and show what it printed."""
        sudo = self._dk_sudo

        def work(r: d.Runner):
            if sudo and not self._root:
                res = d.run_privileged(r, f"sh -c {shlex.quote(cmd)}", self._root, self._sudo_pw, timeout)
            else:
                res = r.run(cmd, timeout=timeout)
            text = (res.out + ("\n" + res.err if res.err.strip() else "")).strip()
            return title, (docker.summarize_inspect(text) if summarize else text) or "(no output)"
        self._job("dkview", work)

    def _show_dkview(self, res) -> None:
        title, text = res
        _ViewDialog(title, text, self).exec()

    def _dk_engine_view(self, title: str, make) -> None:
        if self._dk.engine in ("docker", "podman"):
            self._dk_view(title, make(self._dk.engine), timeout=120)

    def _dk_details(self) -> None:
        x = self._selected_container()
        if x:
            self._dk_view(f"Container: {x.name}", docker.inspect_command(self._dk.engine, "container", x.id), True)

    def _dk_top(self) -> None:
        x = self._selected_container()
        if x:
            self._dk_view(f"Processes in {x.name}", docker.top_command(self._dk.engine, x.id))

    def _dk_shell(self) -> None:
        x = self._selected_container()
        if not x or not self.send_to_terminal:
            return
        self.send_to_terminal(docker.shell_command(self._dk.engine, x.name or x.id, sudo=self._dk_sudo and not self._root))
        self.status.setText(f"Typed into the terminal: press Enter there for a shell inside {x.name} (exit leaves it).")

    # ---- images, volumes, networks ----
    def _dk_fill_others(self, c) -> None:
        t = self.dk_img_table
        with _Filling(t):
            t.setRowCount(len(c.images))
            for i, img in enumerate(c.images):
                faint = C["muted"] if img.dangling else None
                t.setItem(i, 0, _item(img.repository, faint, data=img.id))
                t.setItem(i, 1, _item(img.tag, C["muted"]))
                t.setItem(i, 2, _item(img.id, C["muted"]))
                t.setItem(i, 3, _item(img.size, None, True))
                t.setItem(i, 4, _item(img.created, C["muted"]))
                t.setItem(i, 5, _item(", ".join(img.used_by) or "–", C["ok"] if img.used_by else C["muted"]))
        t = self.dk_vol_table
        with _Filling(t):
            t.setRowCount(len(c.volumes))
            for i, v in enumerate(c.volumes):
                t.setItem(i, 0, _item(v.name, None, data=v.name))
                t.setItem(i, 1, _item(v.driver, C["muted"]))
                t.setItem(i, 2, _item(v.mountpoint or "–", C["muted"]))
        t = self.dk_net_table
        with _Filling(t):
            t.setRowCount(len(c.networks))
            for i, n in enumerate(c.networks):
                t.setItem(i, 0, _item(n.name, C["muted"] if n.builtin else None, data=n.name))
                t.setItem(i, 1, _item(n.driver, C["muted"]))
                t.setItem(i, 2, _item(n.scope, C["muted"]))
                t.setItem(i, 3, _item(n.id, C["muted"]))
        dangling = sum(1 for i in c.images if i.dangling)
        self._cmp_fill(c)
        self.dk_tabs.setTabText(0, f"Containers ({len(c.items)})")
        self.dk_tabs.setTabText(1, f"Compose ({len(self._dk_projects)})")
        self.dk_tabs.setTabText(2, f"Images ({len(c.images)})" + (f", {dangling} dangling" if dangling else ""))
        self.dk_tabs.setTabText(3, f"Volumes ({len(c.volumes)})")
        self.dk_tabs.setTabText(4, f"Networks ({len(c.networks)})")

    # ---- compose projects ----
    def _cmp_fill(self, c) -> None:
        keep = self._selected(self.dk_cmp_table)
        self._dk_projects = compose.projects(c.items)
        t = self.dk_cmp_table
        with _Filling(t):
            t.setRowCount(len(self._dk_projects))
            for i, p in enumerate(self._dk_projects):
                color = C["ok"] if p.state == "running" else C["warn"] if p.running else C["muted"]
                t.setItem(i, 0, _item(p.name, None, data=p.name))
                t.setItem(i, 1, _item("● " + p.state, color))
                t.setItem(i, 2, _item(", ".join(s.name for s in p.services), C["muted"]))
                t.setItem(i, 3, _item(p.folder or "?", C["muted"]))
                t.setItem(i, 4, _item(", ".join(f.rsplit("/", 1)[-1] for f in p.files) or "?", C["muted"]))
        self._reselect(t, keep)
        if c.engine == "none":
            note = ""
        elif not self._dk_projects:
            note = "No compose projects: none of the containers was started by docker compose / docker-compose."
        elif not c.compose:
            note = ("Projects are shown from their containers, but no compose command was found on the server "
                    "(docker compose, docker-compose, podman-compose), so they can't be managed from here.")
        else:
            note = (f"Using “{c.compose}”. Commands run in the project's folder with its own compose files and .env, "
                    "like you would by hand. Projects that are fully down aren't listed (compose keeps no record of "
                    "them besides their containers).")
        self.dk_cmp_note.setText(note)
        self._cmp_selected_changed()

    def _cmp_selected(self):
        name = self._selected(self.dk_cmp_table)
        return next((p for p in self._dk_projects if p.name == name), None) if name else None

    def _cmp_service(self):
        p = self._cmp_selected()
        name = self._selected(self.dk_svc_table)
        return next((s for s in p.services if s.name == name), None) if p and name else None

    def _cmp_selected_changed(self) -> None:
        p = self._cmp_selected()
        keep = self._selected(self.dk_svc_table)
        t = self.dk_svc_table
        services = p.services if p else []
        with _Filling(t):
            t.setRowCount(len(services))
            for i, s in enumerate(services):
                st = s.state
                t.setItem(i, 0, _item(s.name, None, data=s.name))
                t.setItem(i, 1, _item("● " + st, C["ok"] if st.startswith("running") else
                                      C["warn"] if "running" in st else C["muted"]))
                t.setItem(i, 2, _item(", ".join(c.name for c in s.containers), C["muted"]))
                t.setItem(i, 3, _item(s.image, C["muted"]))
        self._reselect(t, keep)
        self._update_dk_buttons()

    def _cmp_action(self, action: str, service: bool = False) -> None:
        p = self._cmp_selected()
        svc = self._cmp_service() if service else None
        if p is None or (service and svc is None):
            return
        try:
            cmd = compose.action_command(self._dk.compose, p, action, svc.name if svc else "")
        except ValueError as e:
            self.status.setText(str(e))
            return
        what = compose.ACTIONS[action][1]
        target = f"{p.name} / {svc.name}" if svc else p.name
        self._privileged(f"{action.capitalize()} {target}", cmd, then="docker", allow_plain=True,
                         timeout=1800 if action in ("update", "pull", "up") else 300,
                         show=f"{self._dk.compose} {compose.ACTIONS[action][0].replace('{base}', '…')}"
                              + (f" {svc.name}" if svc else "") + f"   ({what}, in {p.folder})")

    def _cmp_recreate(self) -> None:
        p, svc = self._cmp_selected(), self._cmp_service()
        if p and svc:
            self._privileged(f"Re-create {p.name} / {svc.name}", compose.recreate_command(self._dk.compose, p, svc.name),
                             then="docker", allow_plain=True, timeout=900,
                             show=f"{self._dk.compose} up -d --force-recreate {svc.name}   (in {p.folder})")

    def _cmp_view(self, what: str, service: bool = False) -> None:
        p = self._cmp_selected()
        svc = self._cmp_service() if service else None
        if p is None or not self._dk.compose or (service and svc is None):
            return
        try:
            cmd = {"logs": lambda: compose.logs_command(self._dk.compose, p, svc.name if svc else ""),
                   "ps": lambda: compose.ps_command(self._dk.compose, p),
                   "config": lambda: compose.config_command(self._dk.compose, p)}[what]()
        except ValueError as e:
            self.status.setText(str(e))
            return
        title = {"logs": "Logs", "ps": "Status", "config": "Configuration"}[what]
        self._dk_view(f"{title}: {p.name}" + (f" / {svc.name}" if svc else ""), cmd, timeout=120)

    def _cmp_files(self) -> None:
        p = self._cmp_selected()
        if p is None:
            return
        try:
            cmd = compose.read_files_command(p)
        except ValueError as e:
            self.status.setText(str(e))
            return
        sudo = self._dk_sudo

        def work(r: d.Runner):
            if sudo and not self._root:
                return p.name, d.run_privileged(r, cmd, self._root, self._sudo_pw, 30).out
            return p.name, r.run(cmd, timeout=30).out
        self._job("cmpfiles", work)

    def _show_cmpfiles(self, res) -> None:
        name, text = res
        p = next((x for x in self._dk_projects if x.name == name), None)
        files = compose.parse_files(text)
        if p is None or not files:
            QMessageBox.warning(self, "Compose files", text.strip()[:800] or "Couldn't read the compose files.")
            return
        dlg = _ComposeFileDialog(p.name, files, self)
        if dlg.exec() != QDialog.Accepted:
            return
        changed = dlg.changed()
        try:
            steps = [compose.save_file_command(self._dk.compose, p, path, text) for path, text in changed.items()]
            if dlg.apply.isChecked():
                steps.append(compose.action_command(self._dk.compose, p, "up"))
        except ValueError as e:
            QMessageBox.warning(self, "Compose files", str(e))
            return
        names = ", ".join(path.rsplit("/", 1)[-1] for path in changed)
        self._privileged(f"Save {names}" + (" and apply" if dlg.apply.isChecked() else ""),
                         "sh -c " + shlex.quote(" && ".join(steps)),          # one command: sudo covers every step
                         then="docker", allow_plain=True, timeout=1800,
                         show=f"check with compose, keep a .bak copy, save {names}"
                              + (f", then {self._dk.compose} up -d" if dlg.apply.isChecked() else "")
                              + f"   (in {p.folder})")

    def _dk_selected(self, which: str):
        table, items, key = {"img": (self.dk_img_table, self._dk.images, "id"),
                             "vol": (self.dk_vol_table, self._dk.volumes, "name"),
                             "net": (self.dk_net_table, self._dk.networks, "name")}[which]
        value = self._selected(table)
        return next((x for x in items if getattr(x, key) == value), None) if value else None

    def _dk_selected_image(self):
        return self._dk_selected("img")

    def _dk_inspect(self, kind: str) -> None:
        x = self._dk_selected({"image": "img", "volume": "vol", "network": "net"}[kind])
        if x is None:
            return
        target = x.ref if kind == "image" else x.name
        self._dk_view(f"{kind.capitalize()}: {target}", docker.inspect_command(self._dk.engine, kind, target), True)

    def _dk_history(self) -> None:
        img = self._dk_selected_image()
        if img:
            self._dk_view(f"Layers of {img.ref}", docker.history_command(self._dk.engine, img.ref))

    def _dk_pull(self) -> None:
        img = self._dk_selected_image()
        if not img:
            return
        try:
            cmd = docker.pull_command(self._dk.engine, img.ref)
        except ValueError as e:
            self.status.setText(str(e))
            return
        self._privileged(f"Pull {img.ref}", cmd, then="docker", allow_plain=True, timeout=900)

    def _dk_remove_image(self) -> None:
        img = self._dk_selected_image()
        if not img or img.used_by:
            return
        self._privileged(f"Remove image {img.ref}", docker.remove_image_command(self._dk.engine, img.ref),
                         then="docker", allow_plain=True)

    def _dk_remove_volume(self) -> None:
        v = self._dk_selected("vol")
        if v:
            self._privileged(f"Remove volume {v.name} (its data is deleted)",
                             docker.remove_volume_command(self._dk.engine, v.name), then="docker", allow_plain=True)

    def _dk_remove_network(self) -> None:
        n = self._dk_selected("net")
        if not n:
            return
        try:
            cmd = docker.remove_network_command(self._dk.engine, n.name)
        except ValueError as e:
            self.status.setText(str(e))
            return
        self._privileged(f"Remove network {n.name}", cmd, then="docker", allow_plain=True)

    def _dk_fill_clean(self, menu: QMenu) -> None:
        menu.clear()
        engine = self._dk.engine
        for what, (_cmd, label) in docker.PRUNE.items():
            if what == "build-cache" and engine != "docker":
                continue
            menu.addAction(label + "…", lambda w=what, lb=label: self._dk_prune(w, lb))

    def _dk_prune(self, what: str, label: str) -> None:
        try:
            cmd = docker.prune_command(self._dk.engine, what)
        except ValueError:
            return
        self._privileged(label, cmd, then="docker", allow_plain=True, timeout=600)

    # ---- timers ----
    def _build_timers(self) -> None:
        lay = self._page("Timers")
        self._timers: list[timers.Timer] = []
        self.tm_buttons = {
            "add": _btn("plus", "New timer…", self._tm_add, "Schedule a command with a systemd timer (a simple form)"),
            "toggle": _btn("bolt", "Disable", self._tm_toggle, "Turn the selected timer off or on"),
            "run": _btn("terminal", "Run now", self._tm_run, "Start what the selected timer starts, once, now"),
            "unit": _btn("code", "Unit file", self._tm_unit, "Show the timer's unit file"),
        }
        lay.addLayout(_toolbar("stretch", *self.tm_buttons.values(),
                               _btn("refresh", "", lambda: self.refresh(force=True), "Reload")))
        self.tm_table = _table(["On", "Timer", "Runs", "Schedule", "Next", "Last"], stretch=3)
        self.tm_table.itemSelectionChanged.connect(self._update_tm_buttons)
        lay.addWidget(self.tm_table, 1)
        self.tm_hint = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.tm_hint)
        self._update_tm_buttons()

    def _show_timers(self, items: list) -> None:
        self._timers = items
        t = self.tm_table
        t.setRowCount(len(items))
        for i, x in enumerate(items):
            on = x.active == "active"
            t.setItem(i, 0, _item("●" if on else "○", C["ok"] if on else C["faint"]))
            t.setItem(i, 1, _item(x.name))
            t.setItem(i, 2, _item(x.unit or "–", C["muted"]))
            t.setItem(i, 3, _item(x.schedule or "–", C["muted"]))
            t.setItem(i, 4, _item(x.next))
            t.setItem(i, 5, _item(x.last, C["muted"]))
        self.tm_hint.setText("" if items else "No systemd timers found (or this server doesn't run systemd). "
                             "Use “New timer…” to schedule a command; cron jobs are in the Cron tab.")
        self._update_tm_buttons()

    def _selected_timer(self) -> timers.Timer | None:
        rows = self.tm_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._timers):
            return None
        return self._timers[rows[0].row()]

    def _update_tm_buttons(self) -> None:
        x = self._selected_timer()
        for k in ("toggle", "run", "unit"):
            self.tm_buttons[k].setEnabled(x is not None)
        if x is not None:
            self.tm_buttons["toggle"].setText(" Disable" if x.active == "active" else " Enable")

    def _tm_toggle(self) -> None:
        x = self._selected_timer()
        if x:
            on = x.active != "active"
            self._privileged(f"{'Enable' if on else 'Disable'} timer {x.name}", timers.toggle_command(x.name, on),
                             then="timers")

    def _tm_run(self) -> None:
        x = self._selected_timer()
        if x and x.unit:
            self._privileged(f"Run {x.unit} now", timers.run_now_command(x.unit), then="timers")

    def _tm_unit(self) -> None:
        x = self._selected_timer()
        if x:
            self._job("unitcat", lambda r: (x.name, r.run(f"systemctl cat {shlex.quote(x.name)} --no-pager 2>&1").out))

    def _tm_add(self) -> None:
        dlg = _CronJobDialog(None, self._cron_now, self._cron_tz, self, self._cron_run, "")
        dlg.setWindowTitle("New systemd timer")
        if dlg.exec() != QDialog.Accepted:
            return
        sched, _cronline, _enabled = dlg.values()
        command = cron.compose(dlg._parts(), cron_escape=False)
        boot = sched == "@reboot"
        calendar = timers.calendar_from_cron(sched)
        if not boot and calendar is None:
            QMessageBox.warning(self, "New timer", "That schedule can't be written as a systemd timer (it uses both a "
                                "day of the month and a weekday). Use a simpler schedule, or the Cron tab.")
            return
        hit = cron.script_path(command)
        default = (hit[0].rsplit("/", 1)[-1].rsplit(".", 1)[0] if hit else "job").lower().replace("_", "-")
        name, ok = QInputDialog.getText(self, "New timer", "Name (becomes <name>.timer and <name>.service):",
                                        text=default or "job")
        name = name.strip()
        if not ok or not units.valid_name(name):
            if ok:
                QMessageBox.warning(self, "New timer", "The name can use letters, digits, - _ . and @.")
            return
        svc, tm = timers.build_units(name, command, "", calendar or "", boot)
        self._privileged(f"Create timer {name}", timers.create_command(name, svc, tm), then="timers", timeout=60,
                         show=f"write {name}.service and {name}.timer, then enable the timer"
                              f" ({calendar or 'at startup'})")

    # ---- system, network and mounts: tables from collect.py, with the actions it offers ----
    CT_TITLES = {"system": "System", "network": "Network", "mounts": "Mounts"}
    CT_HINTS = {"system": "Time and clock, reboot, swap. Everything that changes the system is in Actions and asks first.",
                "network": "Interfaces, addresses, routes and name servers, as the server sees them. Actions: connection checks.",
                "mounts": "/etc/fstab compared with what is mounted now. Select a row for Mount / Unmount."}

    def _build_collect_tabs(self) -> None:
        self._ct: dict[str, object] = {}
        colors = {"ok": C["ok"], "warn": C["warn"], "bad": C["danger"], "dim": C["faint"], "": None}
        self._ct_colors = colors
        for key, title in self.CT_TITLES.items():
            lay = self._page(title)
            flt = QLineEdit(placeholderText="Filter…")
            flt.setMinimumWidth(240)
            count = QLabel("", objectName="Hint")
            btn = QToolButton()
            btn.setText("Actions  ▾")
            btn.setPopupMode(QToolButton.InstantPopup)
            btn.setStyleSheet(f"QToolButton {{ border:1px solid {C['border']}; padding:6px 12px; }}")
            menu = QMenu(btn)
            menu.aboutToShow.connect(lambda k=key: self._ct_fill_menu(k))
            btn.setMenu(menu)
            extra = [_btn("edit", "Time zone…", self._tz_change, "Change the server's time zone")] \
                if key == "system" else []
            lay.addLayout(_toolbar(flt, count, "stretch", *extra, btn,
                                   _btn("refresh", "", lambda _=False, k=key: self.refresh(force=True), "Reload")))
            table = _table([""], sortable=True)
            lay.addWidget(table, 1)
            note = QLabel(self.CT_HINTS[key], objectName="Hint", wordWrap=True)
            lay.addWidget(note)
            st = SimpleNamespace(table=table, filter=flt, count=count, note=note, data=None, menu=menu, button=btn)
            flt.textChanged.connect(lambda _t, k=key: self._ct_fill(k))
            table.doubleClicked.connect(lambda _i, k=key: self._ct_details(k))
            self._ct[key] = st

    def _ct_load(self, key: str) -> None:
        user = self.server.username or ""
        self._job(key, lambda r: collect.LOADERS[key](collect.Context(r, user, self._root)))

    def _show_system(self, table) -> None:
        self._ct_show("system", table)

    def _show_network(self, table) -> None:
        self._ct_show("network", table)

    def _show_mounts(self, table) -> None:
        self._ct_show("mounts", table)

    def _ct_show(self, key: str, table) -> None:
        self._ct[key].data = table
        self._ct_fill(key)

    def _ct_selected(self, key: str):
        sel = self._selected(self._ct[key].table)
        return tuple(sel) if isinstance(sel, (list, tuple)) else sel

    def _ct_fill(self, key: str) -> None:
        st = self._ct[key]
        data = st.data
        if data is None:
            return
        q = st.filter.text().strip().lower()
        idx = [i for i, row in enumerate(data.rows) if not q or q in " ".join(row).lower()]
        keep = self._selected(st.table)
        t = st.table
        with _Filling(t):
            t.setColumnCount(len(data.columns))
            t.setHorizontalHeaderLabels(data.columns)
            hh = t.horizontalHeader()
            for c in range(len(data.columns)):
                hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(len(data.columns) - 1, QHeaderView.Stretch)
            t.setRowCount(len(idx))
            for r, i in enumerate(idx):
                color = self._ct_colors.get(data.styles[i])
                for c, cell in enumerate(data.rows[i]):
                    t.setItem(r, c, _item(cell, color, data=data.keys[i] if c == 0 and data.keys[i] is not None else None))
                first = t.item(r, 0)
                if first is not None:
                    first.setData(Qt.UserRole + 2, i)                  # which row of the data this is (the view may be sorted)
        self._reselect(t, keep)
        st.count.setText(f"{len(idx)} of {len(data.rows)}" if len(idx) != len(data.rows) else f"{len(idx)} rows")
        st.note.setText(data.note or self.CT_HINTS[key])

    def _ct_fill_menu(self, key: str) -> None:
        st = self._ct[key]
        st.menu.clear()
        acts = collect.actions_for(key, self._ct_selected(key))
        for act in acts:
            st.menu.addAction(act.label, lambda a=act: self._collect_action(a, key))
        if not acts:
            st.menu.addAction("Nothing to do with this row").setEnabled(False)

    def _collect_action(self, act: collect.Action, key: str) -> None:
        if act.picker == "timezone":
            self._tz_change()
            return
        value = ""
        if act.prompt:
            value, ok = QInputDialog.getText(self, act.label.rstrip("…"), act.prompt, text=act.placeholder)
            if not ok or not value.strip():
                return
        try:
            cmd = act.command_for(value.strip())
        except ValueError as e:
            QMessageBox.warning(self, act.label.rstrip("…"), str(e))
            return
        if act.readonly:                              # only looks: no confirmation, show what it printed

            def work(r: d.Runner):
                res = r.run(cmd, timeout=60)
                return act.label.rstrip("…"), (res.out + ("\n" + res.err if res.err.strip() else "")).strip()
            self._job("ctview", work)
            return
        self._privileged(act.label.rstrip("…"), cmd, then=key, allow_plain=act.allow_plain, timeout=120,
                         show=cmd if len(cmd) < 150 else act.label, drops=act.drops)

    def _show_ctview(self, res) -> None:
        title, text = res
        _ViewDialog(title, text, self).exec()

    # ---- double-click: the details of a row ----
    def _cells_text(self, table: QTableWidget, row: int) -> str:
        lines = []
        for c in range(table.columnCount()):
            head = table.horizontalHeaderItem(c)
            item = table.item(row, c)
            lines.append(f"{(head.text() if head and head.text() else 'Item')}: {item.text() if item else ''}")
        return "\n".join(lines)

    def _row_details(self, table: QTableWidget, title: str) -> None:
        rows = table.selectionModel().selectedRows()
        if rows:
            _ViewDialog(title, self._cells_text(table, rows[0].row()), self).exec()

    def _sec_details(self) -> None:
        rows = self.sec_table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self._sec_findings):
            f = self._sec_findings[rows[0].row()]
            _ViewDialog(f.title, f.text(), self).exec()

    def _ct_details(self, key: str) -> None:
        st = self._ct[key]
        rows = st.table.selectionModel().selectedRows()
        if not rows or st.data is None:
            return
        first = st.table.item(rows[0].row(), 0)
        orig = first.data(Qt.UserRole + 2) if first else None
        if key == "system" and isinstance(self._ct_selected(key), tuple) and self._ct_selected(key)[:1] == ("tz",):
            self._tz_change()
            return
        if orig is not None:
            _ViewDialog(self.CT_TITLES[key], st.data.describe(int(orig)), self).exec()

    # ---- the time zone: a chooser with the zones the server knows ----
    def _tz_current(self) -> tuple[str, str]:
        """(zone name, server time) from the System tab, as last loaded."""
        data = self._ct["system"].data
        name = now = ""
        if data is not None:
            for row, k in zip(data.rows, data.keys):
                if isinstance(k, (list, tuple)) and k[:1] == ("tz",):
                    name = k[1] or ""
                if row and row[0] == "Server time":
                    now = row[1] if row[1] != "–" else ""
        return name, now

    def _tz_change(self) -> None:
        zones = getattr(self, "_tz_zones", None)
        if zones is not None:
            self._show_tzzones(zones)
            return
        self.status.setText("Reading the server's time zones …")
        self._job("tzzones", lambda r: sysinfo.parse_zones(r.run(sysinfo.ZONES_SCRIPT, timeout=60).out))

    def _show_tzzones(self, zones: list) -> None:
        self._tz_zones = zones
        if self.status.text().startswith("Reading the server's time zones"):
            self.status.setText("")
        current, now = self._tz_current()
        dlg = _TimezoneDialog(self.server.label, current, now, zones, self)
        if dlg.exec() != QDialog.Accepted:
            return
        zone = dlg.value()
        try:
            cmd = sysinfo.timezone_command(zone)
        except ValueError as e:
            QMessageBox.warning(self, "Time zone", str(e))
            return
        self._privileged(f"Set the time zone to {zone}", cmd, then="system", show=f"timedatectl set-timezone {zone}")

    def _wire_details(self) -> None:
        for table, title in ((self.ps_table, "Process"), (self.port_table, "Listening port"),
                             (self.upd_table, "Pending update"), (self.usr_table, "Account"),
                             (self.fw_table, "Firewall rule"), (self.tm_table, "Timer")):
            table.doubleClicked.connect(lambda _i, t=table, ti=title: self._row_details(t, ti))

    # ---- security ----
    def _build_security(self) -> None:
        lay = self._page("Security")
        self.sec_lbl = QLabel("")
        self.sec_root = _btn("shield", "Run the checks with sudo", lambda: self._sec_load(True),
                             "Some checks (shadow file, logs, sudoers) need root to read")
        self.sec_root.hide()
        lay.addLayout(_toolbar(self.sec_lbl, "stretch", self.sec_root,
                               _btn("refresh", "", lambda: self.refresh(force=True), "Check again")))
        self.sec_table = _table(["", "Check", "Result", "Advice"], stretch=3)
        self.sec_table.doubleClicked.connect(lambda _i: self._sec_details())
        self._sec_findings: list = []
        lay.addWidget(self.sec_table, 1)
        lay.addWidget(QLabel("Quick checks from read-only commands: a starting point, not a full audit. "
                             "Double-click a row for why it matters, how to fix it and the facts behind it.",
                             objectName="Hint", wordWrap=True))

    def _sec_load(self, privileged: bool) -> None:
        def work(r: d.Runner):
            if not privileged or self._root:
                return r.run(security.READ_SCRIPT, timeout=40).out
            if self._sudo_pw is None and d.needs_password(r, self._root):
                return "need-password"
            res = d.run_privileged(r, f"sh -c {shlex.quote(security.READ_SCRIPT)}", self._root, self._sudo_pw, 60)
            return res.out if res.ok else r.run(security.READ_SCRIPT, timeout=40).out
        self._job("security", work)

    def _show_security(self, out: str) -> None:
        if out == "need-password":
            pw, ok = QInputDialog.getText(self, "sudo password", f"Password for sudo on {self.server.label} "
                                          "(used for this dashboard only, never saved):", QLineEdit.Password)
            if ok:
                self._sudo_pw = pw
                self._sec_load(True)
            return
        found = security.parse(out)
        self._sec_findings = found
        t = self.sec_table
        t.setRowCount(len(found))
        colors = {security.OK: C["ok"], security.WARN: C["warn"], security.BAD: C["danger"],
                  security.UNKNOWN: C["faint"]}
        for i, x in enumerate(found):
            t.setItem(i, 0, _item("?" if x.level == security.UNKNOWN else "●", colors[x.level]))
            t.setItem(i, 1, _item(x.title))
            t.setItem(i, 2, _item(x.result, colors[x.level] if x.level != security.OK else None))
            t.setItem(i, 3, _item(x.advice or "–", C["muted"]))
        count = lambda lvl: sum(1 for x in found if x.level == lvl)          # noqa: E731
        bits = [f"<span style='color:{C['danger']}'>{count(security.BAD)} problem{'s' if count(security.BAD) != 1 else ''}</span>",
                f"<span style='color:{C['warn']}'>{count(security.WARN)} warning{'s' if count(security.WARN) != 1 else ''}</span>",
                f"{count(security.OK)} fine"]
        if count(security.UNKNOWN):
            bits.append(f"{count(security.UNKNOWN)} unknown")
        self.sec_lbl.setText("  ·  ".join(bits))
        self.sec_root.setVisible(security.needs_root(found) and not self._root)

    # ---- storage ----
    def _build_storage(self) -> None:
        lay = self._page("Storage")
        self._filesystems: list[storage.Filesystem] = []
        self.sto_virtual = QCheckBox("Show virtual filesystems (tmpfs, overlay, …)")
        self.sto_virtual.toggled.connect(lambda _on: self._fill_storage())
        lay.addLayout(_toolbar(self.sto_virtual, "stretch",
                               _btn("refresh", "", lambda: self.refresh(force=True), "Read the disks again")))
        self.sto_table = _table(["Mounted on", "Type", "Size", "Used", "Free", "Use", "Inodes"], stretch=0,
                                sortable=True)
        self.sto_table.doubleClicked.connect(self._sto_drill)
        lay.addWidget(self.sto_table, 2)
        lay.addWidget(QLabel("BIGGEST FOLDERS", objectName="SectionLabel"))
        self.du_path = QLineEdit("/")
        self.du_path.returnPressed.connect(self._du_run)
        lay.addLayout(_toolbar(self.du_path, _btn("up", "", self._du_up, "Parent folder"),
                               _btn("search", "Analyze", self._du_run,
                                    "Measure the folders inside this one (stays on one filesystem)")))
        self.du_table = _table(["Size", "Folder"], stretch=1, sortable=True)
        self.du_table.doubleClicked.connect(self._du_drill)
        lay.addWidget(self.du_table, 3)
        self.du_msg = QLabel("Double-click a filesystem above, or type a folder and press Analyze. Big folders "
                             "can take a minute.", objectName="Hint", wordWrap=True)
        lay.addWidget(self.du_msg)

    def _show_storage(self, fs: list) -> None:
        self._filesystems = fs
        self._fill_storage()

    def _fill_storage(self) -> None:
        rows = [f for f in self._filesystems if self.sto_virtual.isChecked() or not f.virtual]
        t = self.sto_table

        def pct(v):
            return None if v is None else C["danger"] if v >= 90 else C["warn"] if v >= 80 else None
        with _Filling(t):
            t.setRowCount(len(rows))
            for i, f in enumerate(rows):
                t.setItem(i, 0, _item(f.mount))
                t.setItem(i, 1, _item(f.fstype, C["muted"]))
                t.setItem(i, 2, _item(d.human_kb(f.size_kb), None, True))
                t.setItem(i, 3, _item(d.human_kb(f.used_kb), None, True))
                t.setItem(i, 4, _item(d.human_kb(f.avail_kb), None, True))
                t.setItem(i, 5, _item(f"{f.percent:.0f}%", pct(f.percent), True))
                t.setItem(i, 6, _item("–" if f.inode_percent is None else f"{f.inode_percent:.0f}%",
                                      pct(f.inode_percent), True))

    def _sto_drill(self, index) -> None:
        item = self.sto_table.item(index.row(), 0)
        if item:
            self.du_path.setText(item.text())
            self._du_run()

    def _du_run(self) -> None:
        path = self.du_path.text().strip() or "/"
        if not path.startswith(("/", "~")):
            self.du_msg.setText("Give a full path, like /var or /srv/data.")
            return
        self.du_msg.setText(f"Measuring {path} …")
        self._job("du", lambda r: (path, *storage.parse_du(r.run(storage.du_command(path), timeout=120).out, path)))

    def _du_up(self) -> None:
        self.du_path.setText(storage.parent(self.du_path.text().strip() or "/"))
        self._du_run()

    def _du_drill(self, index) -> None:
        item = self.du_table.item(index.row(), 1)
        if item:
            self.du_path.setText(item.text())
            self._du_run()

    def _show_du(self, res) -> None:
        path, total, inside = res
        t = self.du_table
        with _Filling(t):
            t.setRowCount(len(inside))
            for i, (kb, p) in enumerate(inside):
                share = kb / total if total else 0
                t.setItem(i, 0, _item(d.human_kb(kb), C["warn"] if share >= 0.5 else None, True, sort=(0, kb * 1024.0)))
                t.setItem(i, 1, _item(p))
        self.du_msg.setText(f"{path}: {d.human_kb(total)} in total, {len(inside)} entries shown. Double-click a "
                            "folder to go into it." if total else
                            f"Nothing readable in {path} (or it is empty / on another filesystem).")

    # ---- report ----
    def _make_report(self) -> None:
        if self._runner() is None:
            self.status.setText("Connect first: the report is collected from the server.")
            return
        self.status.setText("Collecting the report … (a few seconds)")

        def work(r: d.Runner):
            data, errs = {}, {}

            def grab(key, fn):
                try:
                    data[key] = fn()
                except Exception as e:
                    errs[key] = str(e) or e.__class__.__name__

            def jobs():
                body, _now, _tz = cron.parse_read(r.run(cron.read_script("")).out)
                return [e for e in cron.parse_crontab(body) if e.kind == "job"]
            grab("overview", lambda: d.overview(r))
            grab("services", lambda: d.services(r))
            grab("updates", lambda: d.updates(r))
            grab("ports", lambda: d.ports(r))
            grab("users", lambda: d.users(r))
            grab("cron", jobs)
            grab("firewall", lambda: fw.parse(r.run(fw.READ_SCRIPT, timeout=20).out))
            return data, errs
        self._job("report", work)

    def _show_report(self, res) -> None:
        from datetime import datetime
        data, errs = res
        self.status.setText("")
        md = report.build(self.server.label, self.server.address, datetime.now(), data, errs)
        _ReportDialog(md, self.server.label, self).exec()

    # ---- firewall ----
    def _build_firewall(self) -> None:
        lay = self._page("Firewall")
        self._fw = fw.Firewall()
        self._fw_rows: list[fw.Rule] = []
        self.fw_lbl = QLabel("")
        self.fw_buttons = {
            "add": _btn("plus", "Add rule", self._fw_add, "Open a port or allow a service"),
            "remove": _btn("trash", "Remove", self._fw_remove, "Remove the selected rule"),
            "reload": _btn("refresh", "Reload rules", self._fw_reload, "Apply the saved rules again"),
            "start": _btn("bolt", "Start firewall", self._fw_start, "Turn the firewall on (SSH is allowed first)"),
        }
        self.fw_buttons["start"].hide()
        lay.addLayout(_toolbar(self.fw_lbl, "stretch", *self.fw_buttons.values(),
                               _btn("refresh", "", lambda: self.refresh(force=True), "Read the rules again")))
        self.fw_table = _table(["Where", "Type", "Rule", "Detail"], stretch=3)
        self.fw_table.itemSelectionChanged.connect(self._update_fw_buttons)
        lay.addWidget(self.fw_table, 1)
        self.fw_hint = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.fw_hint)
        self._update_fw_buttons()

    def _fw_load(self) -> None:
        def work(r: d.Runner):
            out = r.run(fw.READ_SCRIPT, timeout=20).out
            if fw.needs_root(out) and not self._root:
                if self._sudo_pw is None and d.needs_password(r, self._root):
                    return "need-password"
                res = d.run_privileged(r, f"sh -c {shlex.quote(fw.READ_SCRIPT)}", self._root, self._sudo_pw, 30)
                if res.ok:
                    out = res.out
            return out
        self._job("firewall", work)

    def _show_firewall(self, res: str) -> None:
        if res == "need-password":
            pw, ok = QInputDialog.getText(self, "sudo password", f"Password for sudo on {self.server.label} "
                                          "(used for this dashboard only, never saved):", QLineEdit.Password)
            if ok:
                self._sudo_pw = pw
                self._fw_load()
            else:
                self.fw_hint.setText("Reading the firewall rules needs root or sudo.")
            return
        f = fw.parse(res)
        self._fw, self._fw_rows = f, f.rules
        t = self.fw_table
        t.setRowCount(len(f.rules))
        for i, r in enumerate(f.rules):
            col = C["ok"] if r.kind in ("allow", "service", "port") else C["danger"] if r.kind in ("deny", "reject") \
                else C["warn"] if r.kind == "limit" else None
            t.setItem(i, 0, _item(r.scope or "–", C["muted"]))
            t.setItem(i, 1, _item(r.kind, col))
            t.setItem(i, 2, _item(r.value))
            t.setItem(i, 3, _item(r.detail or "–", C["muted"]))
        names = {"firewalld": "firewalld", "ufw": "ufw", "iptables": "iptables", "nftables": "nftables"}
        if f.manager == "none":
            self.fw_lbl.setText("No firewall tool found")
            hint = "Looked for firewalld, ufw, iptables and nftables."
        else:
            state = {"running": "● running", "active": "● active", "inactive": "○ not running"}.get(f.state, f.state or "?")
            self.fw_lbl.setText(f"<b>{names[f.manager]}</b>  ·  {state}"
                                + (f"  ·  default zone {f.default_zone}" if f.default_zone else ""))
            if f.why_not_editable:
                hint = f.why_not_editable
            elif f.manager in ("iptables", "nftables"):
                hint = (f"Rules are changed in the running {f.manager} at once. “Save rules” makes them survive a reboot. "
                        "Rules for the SSH port can't be removed from here (they would cut this connection).")
            else:
                hint = ("Changes are saved permanently and applied at once. Rules for the SSH port can't be "
                        "removed from here (they would cut this connection). Double-click a rule for details.")
        self.fw_hint.setText(hint)
        self._update_fw_buttons()

    def _selected_fw_rule(self) -> fw.Rule | None:
        rows = self.fw_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._fw_rows):
            return None
        return self._fw_rows[rows[0].row()]

    def _update_fw_buttons(self) -> None:
        f = self._fw
        ok = f.editable
        r = self._selected_fw_rule()
        why = f.why_not_editable or "Not available here."
        b = self.fw_buttons
        b["add"].setEnabled(ok)
        b["add"].setToolTip("Open a port" + (" or allow a service" if f.manager == "firewalld" else "") if ok else why)
        persist = fw.reload_command(f.manager) or fw.save_command(f.manager)
        b["reload"].setText(" " + fw.action_label(f.manager))
        b["reload"].setEnabled(bool(ok and persist))
        b["reload"].setToolTip(("Make the rules survive a reboot (iptables-save / nft list ruleset)"
                                if f.manager in ("iptables", "nftables") else "Apply the saved rules again")
                               if ok and persist else why)
        cmd = fw.remove_command(f.manager, r) if r else None
        protected = bool(r and fw.protects_ssh(r, int(getattr(self.server, "port", 22) or 22)))
        b["remove"].setEnabled(bool(ok and cmd and not protected))
        b["remove"].setToolTip("Remove the selected rule" if ok and cmd and not protected else why if not ok else
                               "Select a rule first" if r is None else
                               "This rule keeps your SSH connection open, so it can't be removed from here" if protected else
                               "This kind of rule can't be removed from here")
        b["start"].setVisible(f.can_start)

    def _fw_add(self) -> None:
        zones = sorted({r.scope for r in self._fw.rules if r.scope} | ({self._fw.default_zone} - {""}))
        dlg = _FirewallRuleDialog(self._fw.manager, zones, self._fw.default_zone, self)
        if dlg.exec() != QDialog.Accepted:
            return
        kind, value, proto, zone = dlg.values()
        if kind == "service":
            if not re.fullmatch(r"[a-z0-9_-]+", value):
                QMessageBox.warning(self, "Add rule", "A service name uses letters, digits, - and _ (like http).")
                return
            self._privileged(f"Allow service {value}", fw.add_service_command(value, zone), then="firewall")
            return
        port = fw.normalize_port(value, self._fw.manager)
        if not port:
            QMessageBox.warning(self, "Add rule", "Enter a port (80) or a range (8000-8100), from 1 to 65535.")
            return
        try:
            cmd = fw.add_port_command(self._fw.manager, port, proto, zone, fw.nft_input_chain(self._fw.rules))
        except ValueError as e:
            QMessageBox.warning(self, "Add rule", str(e))
            return
        self._privileged(f"Open port {port}/{proto}", cmd, then="firewall")

    def _fw_remove(self) -> None:
        r = self._selected_fw_rule()
        cmd = fw.remove_command(self._fw.manager, r) if r else None
        if not r or not cmd:
            return
        if fw.protects_ssh(r, int(getattr(self.server, "port", 22) or 22)):
            QMessageBox.warning(self, "Remove rule", "This rule allows SSH. Removing it would cut this connection, "
                                "so it isn't offered here. Use the terminal if you really mean to.")
            return
        self._privileged(f"Remove rule {r.value}", cmd, then="firewall")

    def _fw_reload(self) -> None:
        cmd = fw.reload_command(self._fw.manager) or fw.save_command(self._fw.manager)
        if cmd:
            self._privileged(fw.action_label(self._fw.manager), cmd, then="firewall")

    def _fw_start(self) -> None:
        cmd = fw.start_command(self._fw.manager, int(getattr(self.server, "port", 22) or 22))
        if cmd:
            self._privileged("Start firewall", cmd, then="firewall")

    # ---- cron ----
    def _build_cron(self) -> None:
        lay = self._page("Cron")
        self._cron_entries: list[cron.Entry] = []
        self._cron_rows: list[int] = []
        self._cron_text = ""
        self._cron_sys = (0, 0)
        self._cron_diag = ("", "", "", "")
        self._cron_details = ""
        self._cron_now = None
        self._cron_tz = ""
        self.cron_user = QComboBox()
        me = self.server.username or "connected user"
        self.cron_user.addItem(f"Jobs of {me}", "")
        if self.server.username != "root":
            self.cron_user.addItem("Jobs of root", "root")
        self.cron_user.addItem("Another user…", "@other")
        self.cron_user.addItem("System jobs (read-only)", "@system")
        self.cron_user.activated.connect(self._cron_user_picked)
        self.cron_buttons = {
            "add": _btn("plus", "Add job", self._cron_add, "Schedule a new command (a form, no cron syntax needed)"),
            "edit": _btn("edit", "Edit", self._cron_edit, "Change the selected job"),
            "run": _btn("terminal", "Run now", self._cron_run_now, "Run the selected job once, now, and see its output"),
            "toggle": _btn("bolt", "Disable", self._cron_toggle, "Turn the selected job off or on without deleting it"),
            "delete": _btn("trash", "Delete", self._cron_delete, "Remove the selected job"),
            "text": _btn("file", "Edit as text", self._cron_edit_text, "Edit the whole crontab as plain text"),
            "restore": _btn("import", "Restore…", self._cron_restore,
                            "Go back to an earlier copy (a copy is kept before every change)"),
        }
        lay.addLayout(_toolbar(self.cron_user, "stretch", *self.cron_buttons.values(),
                               _btn("refresh", "", lambda: self.refresh(force=True), "Reload")))
        self.cron_table = _table(["On", "When", "Schedule", "Runs as", "Command"])
        self.cron_table.itemSelectionChanged.connect(self._update_cron_buttons)
        self.cron_table.doubleClicked.connect(lambda _i: self._cron_edit())
        lay.addWidget(self.cron_table, 1)
        self.cron_info = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.cron_info)
        self.cron_why = QPushButton("Why is this empty? Show details")
        self.cron_why.setFlat(True)
        self.cron_why.setStyleSheet("text-align:left; padding:2px 0;")
        self.cron_why.clicked.connect(self._cron_show_details)
        self.cron_why.hide()
        lay.addWidget(self.cron_why)
        self._update_cron_buttons()

    @property
    def _cron_who(self) -> str:
        return self.cron_user.currentData() or ""

    def _cron_user_picked(self, _i: int) -> None:
        if self.cron_user.currentData() == "@other":
            name, ok = QInputDialog.getText(self, "Another user", "Account name:")
            name = name.strip()
            if not ok or not d.valid_username(name):
                if ok:
                    QMessageBox.warning(self, "Another user", "That isn't a valid account name.")
                self.cron_user.setCurrentIndex(0)
                return
            idx = self.cron_user.findData(name)
            if idx < 0:
                self.cron_user.insertItem(self.cron_user.findData("@other"), f"Jobs of {name}", name)
                idx = self.cron_user.findData(name)
            self.cron_user.setCurrentIndex(idx)
        self.refresh(force=True)

    def _cron_load(self) -> None:
        who = self._cron_who

        def work(r: d.Runner):
            script = cron.read_script(who)
            if who in ("", "@system"):
                return who, r.run(script).out
            if not self._root and self._sudo_pw is None and d.needs_password(r, self._root):
                return who, "need-password"
            res = d.run_privileged(r, f"sh -c {shlex.quote(script)}", self._root, self._sudo_pw)
            if not res.ok and "password" in (res.err + res.out).lower():
                self._sudo_pw = None
            return who, res.out if res.ok else f"error:{(res.err or res.out).strip()}"
        self._job("cron", work)

    def _show_cron(self, res) -> None:
        who, out = res
        if who != self._cron_who:                   # the user picker moved on meanwhile
            return
        if out == "need-password":
            pw, ok = QInputDialog.getText(self, "sudo password",
                                          f"Password for sudo on {self.server.label} "
                                          f"(used for this dashboard only, never saved):", QLineEdit.Password)
            if ok:
                self._sudo_pw = pw
                self._cron_load()
            else:
                self._cron_fill([], "Reading another user's jobs needs sudo.")
            return
        if out.startswith("error:"):
            self._cron_fill([], out[6:][:300] or "Could not read the crontab.")
            return
        text, now, tz = cron.parse_read(out)
        self._cron_text, self._cron_now, self._cron_tz = text, now, tz
        self._cron_sys = cron.parse_sys(out)
        self._cron_diag = cron.parse_diag(out)
        self._cron_details = cron.parse_details(out)
        self._cron_fill(cron.parse_crontab(text, system=who == "@system"))

    def _cron_fill(self, entries: list[cron.Entry], error: str = "") -> None:
        self._cron_entries = entries
        system = self._cron_who == "@system"
        jobs = [(i, e) for i, e in enumerate(entries) if e.kind == "job"]
        self._cron_rows = [i for i, _ in jobs]
        t = self.cron_table
        t.setRowCount(len(jobs))
        for row, (_i, e) in enumerate(jobs):
            dim = None if e.enabled else C["faint"]
            t.setItem(row, 0, _item("●" if e.enabled else "○", C["ok"] if e.enabled else C["faint"]))
            period = e.source.rsplit("/", 1)[-1] if system else ""
            if period in ("cron.hourly", "cron.daily", "cron.weekly", "cron.monthly"):
                t.setItem(row, 1, _item(f"{period[5:].capitalize()}, time set by the system", dim))
                t.setItem(row, 2, _item(period, dim or C["muted"]))
            else:
                t.setItem(row, 1, _item(cron.describe(e.schedule), dim))
                t.setItem(row, 2, _item(e.schedule, dim or C["muted"]))
            who = f"{e.user}  ({e.source.rsplit('/', 1)[-1]})" if system and e.user else "–"
            t.setItem(row, 3, _item(who, C["muted"]))
            t.setItem(row, 4, _item(e.command, dim))
        env = [e.raw.strip() for e in entries if e.kind == "env"]
        bits = []
        if error:
            bits.append(error)
        elif not jobs:
            bits.append("No scheduled jobs." + ("" if system else " Use “Add job” to create one."))
            said, me, home, how = self._cron_diag
            if not system and (said or me):
                bits.append(f"Read as {me or '?'} ({home or 'no home'}); crontab said: "
                            f"“{said[:160] or 'nothing'}”" + (f" [{how}]" if how else ""))
            jobs_sys, periodic = self._cron_sys
            if not system and (jobs_sys or periodic):
                bits.append(f"The server also has {jobs_sys} system job{'s' if jobs_sys != 1 else ''} and "
                            f"{periodic} periodic script{'s' if periodic != 1 else ''}: pick “System jobs” "
                            "above to see them, or “Jobs of root” for root's own")
        if env:
            bits.append("Settings in this crontab: " + ", ".join(env[:4]) + (" …" if len(env) > 4 else ""))
        if self._cron_now:
            bits.append(f"Server time: {self._cron_now:%Y-%m-%d %H:%M} {self._cron_tz} (jobs run on server time)")
        self.cron_info.setText("  ·  ".join(bits))
        self.cron_why.setVisible(not jobs and not system and bool(self._cron_details))
        self._update_cron_buttons()

    def _selected_job(self) -> tuple[int, cron.Entry] | None:
        rows = self.cron_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._cron_rows):
            return None
        i = self._cron_rows[rows[0].row()]
        return i, self._cron_entries[i]

    def _update_cron_buttons(self) -> None:
        sel = self._selected_job()
        editable = self._cron_who != "@system"
        self.cron_buttons["add"].setEnabled(editable)
        self.cron_buttons["text"].setEnabled(editable)
        self.cron_buttons["restore"].setEnabled(editable)
        for k in ("edit", "toggle", "delete", "run"):
            self.cron_buttons[k].setEnabled(editable and sel is not None)
        if sel:
            self.cron_buttons["toggle"].setText(" Disable" if sel[1].enabled else " Enable")

    def _cron_show_details(self) -> None:
        said, me, home, how = self._cron_diag
        head = (f"Read as {me or '?'} (home {home or '?'}); crontab printed: {said or 'nothing'}"
                + (f" [{how}]" if how else ""))
        _ViewDialog("Cron: why no jobs?", f"{head}\n\n{self._cron_details}", self).exec()

    def _cron_run(self, command: str) -> str | None:
        """Run a short read-only command on the server for the job form (browse, script check)."""
        runner = self._runner()
        return runner.run(command, timeout=10).out if runner else None

    def _cron_note(self) -> str:
        who = self._cron_who
        return f"checked as {self.server.username or 'the connected user'}, the job runs as {who}" \
            if who and who != self.server.username else ""

    def _cron_save(self, text: str, what: str) -> None:
        who = self._cron_who
        if who == "@system":
            return
        shown = f"crontab {'-u ' + who + ' ' if who else ''}-   (replaces the crontab with the edited text)"
        self._cron_backup = (who, self._cron_text)          # kept once the change went through
        self._privileged(what, cron.save_command(who, text), then="cron", allow_plain=True,
                         show=shown, plain_only=not who)

    def _cron_replace(self, index: int | None, entry_raw: str | None, what: str) -> None:
        """Save the crontab with line `index` replaced by `entry_raw` (None: removed; no index: appended)."""
        lines = [e.raw for e in self._cron_entries]
        if index is None:
            lines.append(entry_raw)
        elif entry_raw is None:
            del lines[index]
        else:
            lines[index] = entry_raw
        self._cron_save("\n".join(lines) + "\n", what)

    def _cron_add(self) -> None:
        dlg = _CronJobDialog(None, self._cron_now, self._cron_tz, self, self._cron_run, self._cron_note())
        if dlg.exec() == QDialog.Accepted:
            sched, command, enabled = dlg.values()
            self._cron_replace(None, cron.format_job(sched, command, enabled), "Add scheduled job")

    def _cron_edit(self) -> None:
        sel = self._selected_job()
        if not sel or self._cron_who == "@system":
            return
        dlg = _CronJobDialog(sel[1], self._cron_now, self._cron_tz, self, self._cron_run, self._cron_note())
        if dlg.exec() == QDialog.Accepted:
            sched, command, enabled = dlg.values()
            self._cron_replace(sel[0], cron.format_job(sched, command, enabled), "Save scheduled job")

    def _cron_toggle(self) -> None:
        sel = self._selected_job()
        if sel:
            e = sel[1]
            self._cron_replace(sel[0], cron.format_job(e.schedule, e.command, not e.enabled),
                               f"{'Disable' if e.enabled else 'Enable'} scheduled job")

    def _cron_restore(self) -> None:
        who = self._cron_who
        files = cron.list_backups(self.server.id, who)
        if not files:
            QMessageBox.information(self, "Restore", "No earlier copies yet. A copy is kept every time you "
                                    "change this crontab from here.")
            return
        pick = _ListPicker("Restore an earlier crontab", "Choose a copy to review:",
                           [cron.backup_label(p) for p in files], self)
        if pick.exec() != QDialog.Accepted or pick.index < 0:
            return
        text = files[pick.index].read_text(encoding="utf-8")
        dlg = _CronTextDialog(text, self)
        dlg.setWindowTitle("Restore crontab: review, then save")
        if dlg.exec() == QDialog.Accepted:
            t = dlg.text()
            self._cron_save(t if t.endswith("\n") or not t else t + "\n", "Restore crontab")

    def _cron_run_now(self, _checked: bool = False, confirmed: bool = False) -> None:
        sel = self._selected_job()
        if not sel or self._cron_who == "@system":
            return
        e = sel[1]
        if not confirmed:
            box = QMessageBox(QMessageBox.Question, "Run now", f"Run this job now on <b>{self.server.label}</b>?",
                              parent=self)
            box.setInformativeText(e.command + "\n\nRuns once, outside its schedule, in a plain shell (not exactly "
                                   "cron's environment). The output is shown when it ends; it waits up to 2 minutes.")
            run = box.addButton("Run now", QMessageBox.AcceptRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not run:
                return
        who, cmd = self._cron_who, cron.run_now_command(e.command)
        self._cron_run_args = (e.command, who)

        def work(r: d.Runner):
            if not who:
                res = r.run(cmd, timeout=120)
            else:
                if not self._root and self._sudo_pw is None and d.needs_password(r, self._root):
                    return e.command, "need-password", ""
                inner = cmd if who == "root" else f"su -s /bin/sh {shlex.quote(who)} -c {shlex.quote(cmd)}"
                res = d.run_privileged(r, inner, self._root, self._sudo_pw, 120)
            return e.command, res.code, res.out + ("" if res.ok or res.out else res.err)
        self._job("cronrun", work)

    def _show_cronrun(self, res) -> None:
        command, code, out = res
        if code == "need-password":
            pw, ok = QInputDialog.getText(self, "sudo password", f"Password for sudo on {self.server.label} "
                                          "(used for this dashboard only, never saved):", QLineEdit.Password)
            if ok:
                self._sudo_pw = pw
                self._cron_run_now(confirmed=True)
            return
        if hasattr(self.pane, "log_command"):
            self.pane.log_command(f"{command}   # run now from the dashboard (exit {code})", "dashboard")
        _OutputDialog("Job output", command, code, out, self).exec()

    def _cron_delete(self) -> None:
        sel = self._selected_job()
        if sel:
            self._cron_replace(sel[0], None, "Delete scheduled job")

    def _cron_edit_text(self) -> None:
        dlg = _CronTextDialog(self._cron_text, self)
        if dlg.exec() == QDialog.Accepted:
            text = dlg.text()
            self._cron_save(text if text.endswith("\n") or not text else text + "\n", "Save crontab")

    # ---- users and groups ----
    def _selected_account(self) -> d.Account | None:
        rows = self.usr_table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._accounts):
            return None
        return self._accounts[rows[0].row()]

    def _update_user_buttons(self) -> None:
        a = self._selected_account()
        for k in ("groups", "lock", "passwd", "delete", "keys"):
            self.usr_buttons[k].setEnabled(a is not None)
        if a is not None:
            self.usr_buttons["lock"].setText(" Unlock" if a.locked else " Lock")
        self.usr_buttons["passwd"].setVisible(bool(self.send_to_terminal))

    def _is_me(self, a: d.Account) -> bool:
        return a.name == (self.server.username or "")

    def _add_user(self) -> None:
        dlg = _AddUserDialog(self._all_groups, self)
        if dlg.exec() != QDialog.Accepted:
            return
        name, shell, groups, home, make_home = dlg.values()
        if home and not home.startswith("/"):
            QMessageBox.warning(self, "Add user", "The home folder must be an absolute path, like /srv/deploy.")
            return
        if not d.valid_username(name):
            QMessageBox.warning(self, "Add user", "Use lowercase letters, digits, - and _ (max 32, not starting "
                                "with a digit).")
            return
        if any(a.name == name for a in self._accounts):
            QMessageBox.warning(self, "Add user", f"{name} already exists.")
            return
        self._privileged(f"Add user {name}", d.useradd_command(name, shell, groups, home, make_home),
                         then="users")

    def _edit_groups(self) -> None:
        a = self._selected_account()
        if a is None:
            return
        dlg = _GroupsDialog(a, self._all_groups, self)
        if dlg.exec() != QDialog.Accepted:
            return
        add, remove = dlg.changes()
        cmd = d.groups_command(a.name, add, remove)
        if not cmd:
            return
        self._privileged(f"Change groups of {a.name}", cmd, then="users")

    def _toggle_lock(self) -> None:
        a = self._selected_account()
        if a is None:
            return
        lock = not a.locked
        if lock and (self._is_me(a) or a.uid == 0):
            QMessageBox.warning(self, "Lock", "Locking this account could lock you out of the server, so it "
                                "isn't offered here.")
            return
        self._privileged(f"{'Lock' if lock else 'Unlock'} {a.name}", d.lock_command(a.name, lock), then="users")

    def _set_password(self) -> None:
        a = self._selected_account()
        if a is not None and self.send_to_terminal:
            self.send_to_terminal(f"sudo passwd {a.name}")
            self.status.setText("Typed into the terminal: press Enter, then enter the new password there.")

    def _delete_user(self) -> None:
        a = self._selected_account()
        if a is None:
            return
        if a.uid == 0 or self._is_me(a):
            QMessageBox.warning(self, "Delete", "This is root or the account you are connected as: it can't be "
                                "deleted from here.")
            return
        if a.logged_in:
            QMessageBox.warning(self, "Delete", f"{a.name} is logged in right now ({a.logged_in} session"
                                f"{'s' if a.logged_in != 1 else ''}). End their sessions first.")
            return
        box = QMessageBox(QMessageBox.Warning, "Delete user", f"Delete <b>{a.name}</b> on <b>{self.server.label}</b>?",
                          parent=self)
        keep = box.addButton("Delete, keep home folder", QMessageBox.AcceptRole)
        wipe = box.addButton(f"Delete and remove {a.home or 'home'}", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() not in (keep, wipe):
            return
        self._privileged(f"Delete user {a.name}", d.userdel_command(a.name, box.clickedButton() is wipe),
                         then="users")

    def _type_upgrade(self) -> None:
        cmd = d.UPGRADE_COMMANDS.get(self._update_mgr)
        if cmd and self.send_to_terminal:
            self.send_to_terminal(cmd)
            self.status.setText("Typed into the terminal: review it there and press Enter to run.")

    # ================================================================ window
    def _place(self) -> None:
        """A sensible size (the last one you used, never bigger than the screen) and a position centered over
        the main window, or on the screen when that isn't visible: never off the edge."""
        parent = self.parentWidget()
        screen = (parent.screen() if parent is not None else None) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        saved = self.settings.get("dashboard_size")
        w, h = (saved if isinstance(saved, (list, tuple)) and len(saved) == 2 else (1100, 720))
        try:
            w, h = int(w), int(h)
        except (TypeError, ValueError):
            w, h = 1100, 720
        self.resize(max(760, min(w, int(avail.width() * 0.94))), max(520, min(h, int(avail.height() * 0.92))))
        w, h = self.width(), self.height()
        if parent is not None and parent.isVisible() and not parent.isMinimized():
            center = parent.frameGeometry().center()
        else:
            center = avail.center()
        x = max(avail.left(), min(center.x() - w // 2, avail.right() - w + 1))
        y = max(avail.top(), min(center.y() - h // 2, avail.bottom() - h + 1))
        self.move(x, y)

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)

    def closeEvent(self, e):  # noqa: N802
        if not self.isMaximized() and not self.isFullScreen():          # remember the size for next time
            self.settings["dashboard_size"] = [self.width(), self.height()]
            if hasattr(self.settings, "save"):
                self.settings.save()
        self._closed = True
        self._sudo_pw = None
        self._timer.stop()
        super().closeEvent(e)
