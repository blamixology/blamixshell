"""AWS dialogs: SSO sign-in and importing SSM-managed instances."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QRadioButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from . import aws
from .models import Store
from .theme import C, icon, style_window


class _Sig(QObject):
    output = Signal(str)
    done = Signal(int)
    result = Signal(object, str)        # value, error


class _Base(QDialog):
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)


# ======================================================================= SSO sign-in
class AwsLoginDialog(_Base):
    """Runs `aws sso login --profile X`; the browser opens, this shows the URL/code and waits."""

    def __init__(self, profile: str, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle("AWS sign-in")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)
        lay.addWidget(QLabel(f"Signing in to AWS SSO  ·  profile <b>{profile or 'default'}</b>", objectName="H2"))
        self.msg = QLabel("Starting <code>aws sso login</code> … your browser opens to approve the sign-in.",
                          wordWrap=True)
        self.msg.setTextFormat(Qt.RichText)
        self.msg.setOpenExternalLinks(True)
        lay.addWidget(self.msg)
        self.code = QLabel("", objectName="H2", alignment=Qt.AlignCenter)
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.code.hide()
        lay.addWidget(self.code)
        self.hint = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.hint)
        row = QHBoxLayout()
        self.copy_btn = QPushButton(icon("copy"), " Copy link")
        self.copy_btn.hide()
        self.copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._url))
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self._url = ""
        self.ok = False
        self._sig = _Sig()
        self._sig.output.connect(self._on_output)
        self._sig.done.connect(self._on_done)
        self.login = aws.SsoLogin(profile, self._sig.output.emit)
        QTimer.singleShot(0, self._start)

    def _start(self) -> None:
        try:
            self.login.start()
        except aws.AwsError as e:
            self._fail(str(e))
            return
        threading.Thread(target=lambda: self._sig.done.emit(self.login.wait()), daemon=True).start()

    def _on_output(self, text: str) -> None:
        info = aws.parse_login_output(text)
        if info.url and info.url != self._url:
            self._url = info.url
            self.msg.setText("Approve the sign-in in your browser. If it didn't open, use "
                             f"<a href='{info.url}' style='color:{C['accent']}'>this link</a>"
                             + (" and enter the code below." if info.code else "."))
            self.copy_btn.show()
        if info.code:
            self.code.setText(info.code)
            self.code.show()
        self.hint.setText("Waiting for approval …")

    def _on_done(self, code: int) -> None:
        if code == 0:
            self.ok = True
            self.accept()
            return
        self._fail(self.login.error() or f"aws sso login exited with code {code}")

    def _fail(self, msg: str) -> None:
        self.msg.setText(f"<span style='color:{C['danger']}'>✖ {msg}</span>")
        self.hint.setText("")
        self.cancel_btn.setText("Close")

    def reject(self) -> None:
        self.login.cancel()
        super().reject()


_logins: dict[str, list] = {}       # profile -> callbacks waiting for a sign-in already on screen


def ensure_login(parent, profile: str, then=lambda: None, ask: bool = True) -> None:
    """Sign in to AWS SSO for `profile`, then call `then()`. Several panes of the same profile
    share one sign-in (each reconnects when it's done)."""
    if profile in _logins:
        _logins[profile].append(then)
        return
    if ask:
        box = QMessageBox(QMessageBox.Question, "AWS sign-in needed",
                          f"Your AWS SSO session for profile “{profile or 'default'}” has expired "
                          "(or you haven't signed in yet).", parent=parent)
        box.setInformativeText("Sign in now? Your browser opens to approve it.")
        go = box.addButton("Sign in", QMessageBox.AcceptRole)
        box.addButton("Not now", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not go:
            return
    _logins[profile] = [then]
    dlg = AwsLoginDialog(profile, parent)
    dlg.exec()
    waiting = _logins.pop(profile, [])
    if dlg.ok:
        for fn in waiting:
            try:
                fn()
            except RuntimeError:     # a pane that was closed meanwhile
                pass


# ======================================================================= import instances
class AwsImportDialog(_Base):
    """List SSM-managed instances for a profile/region and add them to the server tree."""

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Import from AWS")
        self.resize(860, 620)
        self.added = 0
        self.updated = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(QLabel("Import EC2 instances (AWS Systems Manager)", objectName="H2"))
        lay.addWidget(QLabel("Lists the instances Session Manager can reach (SSM agent online), using your "
                             "AWS CLI profiles. No inbound SSH port is needed.", objectName="Hint", wordWrap=True))

        top = QHBoxLayout()
        self.profile = QComboBox()
        self.profile.setEditable(True)
        self.profile.setMinimumWidth(200)
        profs = aws.profiles()
        self.profile.addItems(profs or ["default"])
        self.region = QComboBox()
        self.region.setEditable(True)
        self.region.addItems([""] + aws.REGIONS)
        self.region.lineEdit().setPlaceholderText("profile's region")
        self.profile.currentTextChanged.connect(self._profile_changed)
        self.list_btn = QPushButton(icon("refresh"), " List instances")
        self.list_btn.clicked.connect(self._list)
        self.login_btn = QPushButton(icon("lock"), " SSO sign-in")
        self.login_btn.clicked.connect(lambda: ensure_login(self, self._profile(), self._list, ask=False))
        top.addWidget(QLabel("Profile"))
        top.addWidget(self.profile)
        top.addWidget(QLabel("Region"))
        top.addWidget(self.region)
        top.addWidget(self.list_btn)
        top.addWidget(self.login_btn)
        top.addStretch(1)
        lay.addLayout(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Instance", "Platform", "SSM agent", "Private IP"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setStretchLastSection(False)
        lay.addWidget(self.table, 1)
        self.note = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.note)

        opts = QWidget()
        f = QFormLayout(opts)
        f.setContentsMargins(0, 0, 0, 0)
        mode = QHBoxLayout()
        self.mode = QButtonGroup(self)
        for i, (key, text, tip) in enumerate([
                ("ssm-ssh", "SSH over SSM", "Full SSH through Session Manager: files, tunnels and the dashboard work."),
                ("ssm-shell", "SSM shell only", "Session Manager's own shell: no SSH user or key needed, terminal only.")]):
            rb = QRadioButton(text)
            rb.setToolTip(tip)
            rb.setProperty("key", key)
            self.mode.addButton(rb, i)
            mode.addWidget(rb)
        self.mode.button(0).setChecked(True)
        mode.addStretch(1)
        f.addRow("Connect with", mode)
        self.user = QLineEdit(placeholderText="automatic (ubuntu, ec2-user, admin … from the OS)")
        f.addRow("SSH user", self.user)
        self.eic = QCheckBox("Use EC2 Instance Connect (a one-time key per connection, nothing stored on the instance)")
        self.eic.setChecked(True)
        f.addRow("SSH key", self.eic)
        self.group = QLineEdit(placeholderText="AWS/<profile>/<region>")
        f.addRow("Group", self.group)
        self.mode.idToggled.connect(lambda _i, _on: self._mode_changed())
        lay.addWidget(opts)

        row = QHBoxLayout()
        self.sel_lbl = QLabel("", objectName="Hint")
        row.addWidget(self.sel_lbl)
        row.addStretch(1)
        cancel = QPushButton("Close")
        cancel.clicked.connect(self.reject)
        self.import_btn = QPushButton("Import selected", objectName="Primary")
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._import)
        row.addWidget(cancel)
        row.addWidget(self.import_btn)
        lay.addLayout(row)
        self.table.itemSelectionChanged.connect(self._sel_changed)

        self._instances: list[aws.Instance] = []
        self._sig = _Sig()
        self._sig.result.connect(self._listed)
        if not aws.cli():
            self.note.setText(f"<span style='color:{C['danger']}'>The AWS CLI isn't installed.</span> "
                              f"Install it from <a style='color:{C['accent']}' href='{aws.CLI_INSTALL_URL}'>"
                              "docs.aws.amazon.com</a>, then reopen this dialog.")
            self.note.setOpenExternalLinks(True)
            self.list_btn.setEnabled(False)
        self._profile_changed(self.profile.currentText())

    def _profile_changed(self, profile: str) -> None:
        self.region.setCurrentText(aws.profile_region(profile.strip()))
        self.login_btn.setVisible(aws.is_sso_profile(profile.strip()))

    def _mode_changed(self) -> None:
        ssh = self.mode.checkedButton().property("key") == "ssm-ssh"
        self.user.setEnabled(ssh)
        self.eic.setEnabled(ssh)

    def _list(self) -> None:
        profile, region = self._profile(), self._region()
        self.list_btn.setEnabled(False)
        self.note.setText("Asking AWS …")

        def work():
            try:
                inst, note = aws.list_instances(profile, region)
                self._sig.result.emit(inst, note)
            except Exception as e:
                self._sig.result.emit(e, "")
        threading.Thread(target=work, daemon=True).start()

    def _listed(self, res, note: str) -> None:
        self.list_btn.setEnabled(True)
        if isinstance(res, aws.LoginRequired) and res.sso:
            self.note.setText(f"<span style='color:{C['warn']}'>{res}</span>")
            ensure_login(self, res.profile, self._list)
            return
        if isinstance(res, Exception):
            self.note.setText(f"<span style='color:{C['danger']}'>✖ {res}</span>")
            return
        self._instances = res
        t = self.table
        t.setRowCount(len(res))
        known = {(s.host, s.aws_profile) for s in self.store.servers.values() if s.is_ssm}
        profile = self._profile()
        for r, i in enumerate(res):
            exists = (i.id, profile) in known
            cells = [i.label + ("  (in your list)" if exists else ""), i.id,
                     i.platform_name or i.platform, i.ping + (f" · {i.state}" if i.state else ""), i.ip]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c == 3:
                    it.setForeground(Qt.GlobalColor.green if i.online else Qt.GlobalColor.gray)
                if exists:
                    it.setForeground(Qt.GlobalColor.gray)
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        t.selectAll()
        n = len(res)
        self.note.setText((f"{n} instance{'s' if n != 1 else ''} managed by Systems Manager."
                           if n else "No instances are managed by Systems Manager in this account/region.")
                          + (f"  {note}" if note else ""))
        self.group.setPlaceholderText(aws.default_group(profile, self._region()))

    def _profile(self) -> str:
        p = self.profile.currentText().strip()
        return "" if p == "default" else p

    def _region(self) -> str:
        return self.region.currentText().strip()

    def _sel_changed(self) -> None:
        n = len(self.table.selectionModel().selectedRows())
        self.sel_lbl.setText(f"{n} selected" if n else "")
        self.import_btn.setEnabled(bool(n))

    def _import(self) -> None:
        rows = sorted(i.row() for i in self.table.selectionModel().selectedRows())
        mode = self.mode.checkedButton().property("key")
        profile, region = self._profile(), self._region()
        group = self.group.text().strip().strip("/") or aws.default_group(profile, region)
        added, updated = aws.import_instances(
            self.store, [self._instances[r] for r in rows], profile, region, mode,
            self.user.text().strip(), self.eic.isChecked(), group)
        self.added += added
        self.updated += updated
        self._listed(self._instances, "")
        self.table.clearSelection()
        self.note.setText(f"✔ Added {added}" + (f", updated {updated}" if updated else "")
                          + f" server{'s' if added + updated != 1 else ''} in “{group}”.")
