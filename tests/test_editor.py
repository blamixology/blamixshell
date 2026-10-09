"""The built-in editor: reading and saving server files (atomic replace, permissions kept, sudo for root's files),
encodings and line endings kept, a check for changes made on the server meanwhile, and the editor window."""
import base64
import os
import posixpath
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from blamixshell import dashboard as d, remote_file as rf  # noqa: E402


class FakeFile:
    def __init__(self, sftp, path, mode):
        self.sftp, self.path, self.mode = sftp, path, mode
        if "w" in mode:
            sftp.files[path] = b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.sftp.mtimes[self.path] = self.sftp.clock

    def read(self):
        return self.sftp.files[self.path]

    def write(self, data):
        self.sftp.files[self.path] += data

    def truncate(self, n):
        self.sftp.files[self.path] = self.sftp.files[self.path][:n]


class FakeSftp:
    """A server's files in memory. `locked`: only root may read/write; `ro_dirs`: folders you can't create in."""

    def __init__(self, files, locked=(), ro_dirs=(), ro_files=()):
        self.files = dict(files)
        self.modes = {p: 0o640 for p in files}
        self.mtimes = {p: 1000.0 for p in files}
        self.locked, self.ro_dirs, self.ro_files = set(locked), set(ro_dirs), set(ro_files)
        self.clock = 2000.0
        self.renames = []

    def _deny(self, path):
        raise PermissionError(13, "Permission denied", path)

    def stat(self, path):
        if path not in self.files:
            raise FileNotFoundError(2, "No such file", path)
        return SimpleNamespace(st_size=len(self.files[path]), st_mtime=self.mtimes[path],
                               st_mode=0o100000 | self.modes.get(path, 0o644))

    def open(self, path, mode):
        if path in self.locked or ("w" in mode and posixpath.dirname(path) in self.ro_dirs and path not in self.files):
            self._deny(path)
        if mode != "rb" and path in self.ro_files:
            self._deny(path)
        return FakeFile(self, path, mode)

    def chmod(self, path, mode):
        self.modes[path] = mode

    def posix_rename(self, a, b):
        self.files[b] = self.files.pop(a)
        self.modes[b] = self.modes.pop(a, 0o644)
        self.mtimes[b] = self.clock
        self.renames.append((a, b))

    def remove(self, path):
        self.files.pop(path, None)


def session(sftp, user="deploy"):
    return SimpleNamespace(sftp=lambda: sftp, client=None, server=SimpleNamespace(label="web-1", username=user))


def test_read_and_atomic_save_keep_permissions():
    sftp = FakeSftp({"/etc/app.conf": b"a = 1\n"})
    f = rf.RemoteFile(session(sftp), "/etc/app.conf")
    info, data = f.read(10_000)
    assert data == b"a = 1\n" and info.mode == 0o640
    sftp.clock = 3000.0
    out = f.write(b"a = 2\n", info.mode)
    assert sftp.files["/etc/app.conf"] == b"a = 2\n" and sftp.modes["/etc/app.conf"] == 0o640
    assert len(sftp.renames) == 1 and sftp.renames[0][0].startswith("/etc/.app.conf.blamixshell-")
    assert [p for p in sftp.files if "blamixshell" in p] == [] and out.mtime == 3000.0
    try:
        f.read(3)
        raise AssertionError("too big")
    except ValueError as e:
        assert "too big" in str(e)


def test_folder_not_writable_writes_in_place_and_root_files_need_sudo():
    sftp = FakeSftp({"/srv/www/index.html": b"<p>hi</p>"}, ro_dirs={"/srv/www"})
    f = rf.RemoteFile(session(sftp), "/srv/www/index.html")
    f.write(b"<p>bye</p>")
    assert sftp.files["/srv/www/index.html"] == b"<p>bye</p>" and not sftp.renames       # in place, no temp file
    locked = FakeSftp({"/etc/nginx/nginx.conf": b"x"}, locked={"/etc/nginx/nginx.conf"})
    try:
        rf.RemoteFile(session(locked), "/etc/nginx/nginx.conf").read(100)
        raise AssertionError("must ask for sudo")
    except rf.NeedsSudo as e:
        assert "only be read by root" in str(e)
    ro = FakeSftp({"/etc/hosts": b"127.0.0.1 localhost\n"}, ro_dirs={"/etc"}, ro_files={"/etc/hosts"})
    try:
        rf.RemoteFile(session(ro), "/etc/hosts").write(b"x")
        raise AssertionError("must ask for sudo")
    except rf.NeedsSudo as e:
        assert "only be changed by root" in str(e)


def test_with_sudo_root_reads_it_and_copies_into_the_file():
    sftp = FakeSftp({}, ro_dirs={"/etc"})
    f = rf.RemoteFile(session(sftp), "/etc/nginx/nginx.conf")
    f.sudo = True
    ran = []
    content = b"worker_processes 4;\n"

    def privileged(command, timeout=60):
        ran.append(command)
        if command.startswith("stat "):
            return d.Result(0, f"{len(content)} 1700000000 644", "")
        if command.startswith("base64 "):
            return d.Result(0, base64.encodebytes(content).decode(), "")
        return d.Result(0, "", "")
    with mock.patch.object(f, "_privileged", privileged):
        info, data = f.read(10_000)
        assert data == content and info.mode == 0o644
        f.write(b"worker_processes 8;\n")
    copy = next(c for c in ran if c.startswith("sh -c"))
    assert "cat /tmp/.blamixshell-edit-" in copy and "> /etc/nginx/nginx.conf" in copy             # owner, mode stay
    assert [p for p in sftp.files if p.startswith("/tmp/")] == []                                 # temp file removed


def wait(app, cond, seconds=5):
    end = time.time() + seconds
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


def test_the_editor_window_opens_edits_saves_and_notices_changes_on_the_server():
    from PySide6.QtWidgets import QApplication, QMessageBox
    from blamixshell.editor import EditorWindow
    app = QApplication.instance() or QApplication([])
    text = b"server {\r\n    listen 80;\r\n}\r\n"
    sftp = FakeSftp({"/etc/nginx/sites/app.conf": text})
    sess = session(sftp)
    win = EditorWindow()
    messages = []
    win.message.connect(messages.append)
    tab = win.open(sess, "/etc/nginx/sites/app.conf")
    assert wait(app, lambda: tab.loaded)
    assert tab.ed.toPlainText() == "server {\n    listen 80;\n}\n" and "CRLF" in tab.status.text()
    assert tab.lexer_name.lower().startswith("nginx") and tab.ed.indent_unit == "    "
    assert win.open(sess, "/etc/nginx/sites/app.conf") is tab and win.tabs.count() == 1        # not opened twice

    tab.ed.selectAll()
    tab.ed.insertPlainText("server {\n    listen 8080;\n}\n")                                    # as if typed
    assert tab.dirty and win.tabs.tabText(0).startswith("• ")
    tab.save()
    assert wait(app, lambda: not tab.dirty)
    assert sftp.files["/etc/nginx/sites/app.conf"] == b"server {\r\n    listen 8080;\r\n}\r\n"     # CRLF kept
    assert messages[-1] == "Saved app.conf to web-1"

    sftp.files["/etc/nginx/sites/app.conf"] = b"changed by someone else\r\n"                     # meanwhile
    sftp.mtimes["/etc/nginx/sites/app.conf"] = 9999.0
    tab.ed.selectAll()
    tab.ed.insertPlainText("mine\n")
    with mock.patch.object(QMessageBox, "exec", lambda self: None), \
            mock.patch.object(QMessageBox, "clickedButton", lambda self: None):                  # "Cancel"
        tab.save()
        assert wait(app, lambda: not tab._saving)
    assert sftp.files["/etc/nginx/sites/app.conf"] == b"changed by someone else\r\n" and tab.dirty

    with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel):
        win.close_tab(0)
    assert win.tabs.count() == 1                                                                 # unsaved: kept
    with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard):
        win.close_tab(0)
    assert win.tabs.count() == 0


def test_a_root_only_file_is_opened_with_sudo_after_asking():
    from PySide6.QtWidgets import QApplication, QMessageBox
    from blamixshell.editor import EditorWindow
    app = QApplication.instance() or QApplication([])
    sftp = FakeSftp({"/etc/sudoers.d/ops": b"%ops ALL=(ALL) ALL\n"}, locked={"/etc/sudoers.d/ops"})
    win = EditorWindow()
    content = b"%ops ALL=(ALL) ALL\n"

    def privileged(self, command, timeout=60):
        if command.startswith("stat "):
            return d.Result(0, f"{len(content)} 1700000000 440", "")
        return d.Result(0, base64.encodebytes(content).decode(), "")
    with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes), \
            mock.patch.object(rf.RemoteFile, "_privileged", privileged):
        tab = win.open(session(sftp), "/etc/sudoers.d/ops")
        assert wait(app, lambda: tab.loaded)
    assert tab.file.sudo and not tab.sudo_lbl.isHidden() and tab.ed.toPlainText() == "%ops ALL=(ALL) ALL\n"
    with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard):
        win.close()


def test_binary_files_are_not_opened():
    from PySide6.QtWidgets import QApplication
    from blamixshell.editor import EditorWindow
    app = QApplication.instance() or QApplication([])
    sftp = FakeSftp({"/bin/tool": b"\x7fELF\x00\x00binary"})
    win = EditorWindow()
    tab = win.open(session(sftp), "/bin/tool")
    assert wait(app, lambda: "Can't open this file" in tab.ed.toPlainText())
    assert tab.ed.isReadOnly() and not tab.loaded
    win.close()


def test_markdown_files_show_text_side_by_side_or_preview():
    from PySide6.QtWidgets import QApplication
    from blamixshell.editor import EditorTab, EditorWindow, is_markdown
    app = QApplication.instance() or QApplication([])
    assert is_markdown("/srv/app/README.md") and is_markdown("notes.MARKDOWN") and not is_markdown("/etc/hosts")
    doc = b"# Deploy\n\n| step | command |\n|---|---|\n| 1 | `git pull` |\n\n- [x] backup\n"
    sftp = FakeSftp({"/srv/app/README.md": doc, "/etc/hosts": b"127.0.0.1 localhost\n"})
    win = EditorWindow()
    win.resize(1000, 600)
    EditorTab.md_view = "split"
    tab = win.open(session(sftp), "/srv/app/README.md")
    assert wait(app, lambda: tab.loaded)
    assert not tab.md_buttons["split"].isHidden() and tab.md_buttons["split"].isChecked()
    assert not tab.ed.isHidden() and not tab.preview.isHidden()                          # side by side
    html = tab.preview.toHtml()
    assert "Deploy" in html and "<table" in html and "git pull" in html                   # formatted, not raw
    tab.set_view("preview")
    assert tab.ed.isHidden() and not tab.preview.isHidden()
    tab.set_view("split")
    tab.ed.selectAll()
    tab.ed.insertPlainText("# Rollback\n")                                                # follows what you type
    assert wait(app, lambda: "Rollback" in tab.preview.toPlainText())
    tab.set_view("text")
    assert tab.preview.isHidden() and not tab.ed.isHidden() and EditorTab.md_view == "text"
    other = win.open(session(sftp), "/etc/hosts")
    assert wait(app, lambda: other.loaded)
    assert all(b.isHidden() for b in other.md_buttons.values()) and other.preview.isHidden()   # not Markdown
    from unittest import mock
    from PySide6.QtWidgets import QMessageBox
    with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard):
        win.close()
