"""SFTP side panel: browse, upload (drag & drop), download, edit-in-place."""
from __future__ import annotations

import os
import posixpath
import stat
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
                               QProgressBar, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .ssh_session import ShellSession, friendly_error
from .theme import C, icon


def human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return str(n)


class _Signals(QObject):
    listed = Signal(object, str, list)      # session, path, entries
    error = Signal(str)
    progress = Signal(str, int, int)        # label, done, total
    finished = Signal(str)
    edit_ready = Signal(str, str)           # local, remote


class _Item(QTreeWidgetItem):
    def __lt__(self, other):  # folders first, then by column
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        a, b = self.data(0, Qt.UserRole + 1), other.data(0, Qt.UserRole + 1)
        if a != b:
            return bool(a) and not b if self.treeWidget().header().sortIndicatorOrder() == Qt.AscendingOrder \
                else bool(b) and not a
        if col == 1:
            return (self.data(1, Qt.UserRole) or 0) < (other.data(1, Qt.UserRole) or 0)
        if col == 2:
            return (self.data(2, Qt.UserRole) or 0) < (other.data(2, Qt.UserRole) or 0)
        return self.text(col).lower() < other.text(col).lower()


class RemoteTree(QTreeWidget):
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            e.acceptProposedAction()


class SftpPanel(QWidget):
    status_message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SftpPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(300)
        self.session: ShellSession | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sftp")
        self.sig = _Signals()
        self.sig.listed.connect(self._on_listed)
        self.sig.error.connect(self._on_error)
        self.sig.progress.connect(self._on_progress)
        self.sig.finished.connect(self._on_finished)
        self.sig.edit_ready.connect(self._open_for_edit)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_local_changed)
        self._edits: dict[str, tuple[ShellSession, str, float]] = {}
        self._debounce: dict[str, QTimer] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("FILES", objectName="SectionLabel")
        self.host_lbl = QLabel(objectName="Hint")
        top.addWidget(title)
        top.addWidget(self.host_lbl, 1)
        lay.addLayout(top)

        bar = QHBoxLayout()
        bar.setSpacing(2)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("/remote/path")
        self.path_edit.returnPressed.connect(lambda: self.navigate(self.path_edit.text().strip()))
        for name, tip, fn in [("up", "Parent folder", self.go_up), ("home", "Home folder", self.go_home),
                              ("refresh", "Refresh", self.refresh)]:
            bar.addWidget(self._btn(name, tip, fn))
        bar.addWidget(self.path_edit, 1)
        lay.addLayout(bar)

        bar2 = QHBoxLayout()
        bar2.setSpacing(2)
        for name, tip, fn in [("upload", "Upload files…", self.upload_dialog),
                              ("download", "Download selected…", self.download_selected),
                              ("folder-plus", "New folder", self.new_folder),
                              ("trash", "Delete selected", self.delete_selected)]:
            bar2.addWidget(self._btn(name, tip, fn))
        bar2.addStretch(1)
        self.count_lbl = QLabel(objectName="Hint")
        bar2.addWidget(self.count_lbl)
        lay.addLayout(bar2)

        self.tree = RemoteTree()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Name", "Size", "Modified", "Mode"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setStretchLastSection(False)
        for i in (1, 2, 3):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.tree.setColumnHidden(3, True)   # mode shown in tooltip; right-click header to show
        hdr.setContextMenuPolicy(Qt.CustomContextMenu)
        hdr.customContextMenuRequested.connect(
            lambda _p: self.tree.setColumnHidden(3, not self.tree.isColumnHidden(3)))
        self.tree.itemDoubleClicked.connect(self._on_double)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.files_dropped.connect(self.upload_paths)
        self.err_lbl = QLabel()
        self.err_lbl.setWordWrap(True)
        self.err_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.err_lbl.setStyleSheet(f"color:{C['danger']}; background:{C['surface']}; border:1px solid {C['border']};"
                                   "border-radius:8px; padding:8px;")
        self.err_lbl.hide()
        lay.addWidget(self.err_lbl)
        lay.addWidget(self.tree, 1)

        self.placeholder = QLabel("Open an SSH session to browse its files.\n\nDrop files here to upload.")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setObjectName("Muted")
        self.placeholder.setWordWrap(True)
        lay.addWidget(self.placeholder, 1)

        self.progress_lbl = QLabel(objectName="Hint")
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        lay.addWidget(self.progress_lbl)
        lay.addWidget(self.progress)
        self._show_progress(False)
        self._set_enabled(False)

    def _btn(self, name, tip, fn):
        b = QToolButton()
        b.setIcon(icon(name, C["muted"], 16))
        b.setToolTip(tip)
        b.clicked.connect(fn)
        return b

    def _set_enabled(self, on: bool) -> None:
        self.tree.setVisible(on)
        self.placeholder.setVisible(not on)
        self.path_edit.setEnabled(on)

    def _show_progress(self, on: bool) -> None:
        self.progress.setVisible(on)
        self.progress_lbl.setVisible(on)

    # ------------------------------------------------------------ binding
    def set_session(self, session: ShellSession | None) -> None:
        if session is self.session and session and self.tree.topLevelItemCount():
            return
        self.session = session
        if not session or not session.is_connected:
            self.host_lbl.setText("")
            self.tree.clear()
            self._set_enabled(False)
            return
        self.host_lbl.setText(session.server.label)
        self._set_enabled(True)
        self.navigate(session.sftp_cwd or "")

    def _run(self, fn, *args) -> None:
        sess = self.session
        if not sess:
            return

        def job():
            try:
                fn(sess, *args)
            except Exception as e:  # report every failure to the UI
                if not isinstance(e, IOError):
                    sess.reset_sftp()      # protocol-level failure: reopen next time
                msg = friendly_error(e)
                fname = getattr(e, "filename", None)
                self.sig.error.emit(f"{msg} ({fname})" if fname else msg)
        self._pool.submit(job)

    # ------------------------------------------------------------ listing
    def navigate(self, path: str) -> None:
        self._run(self._job_list, path)

    def refresh(self) -> None:
        if self.session:
            self.navigate(self.session.sftp_cwd or "")

    def go_up(self) -> None:
        if self.session and self.session.sftp_cwd:
            self.navigate(posixpath.dirname(self.session.sftp_cwd.rstrip("/")) or "/")

    def go_home(self) -> None:
        self._run(self._job_home)

    def _job_home(self, sess: ShellSession) -> None:
        sftp = sess.sftp()
        self._job_list(sess, sftp.normalize("."))

    def _job_list(self, sess: ShellSession, path: str) -> None:
        sftp = sess.sftp()
        path = sftp.normalize(path or sess.sftp_cwd or ".")
        entries = []
        for a in sftp.listdir_attr(path):
            is_dir = stat.S_ISDIR(a.st_mode or 0)
            if stat.S_ISLNK(a.st_mode or 0):
                try:
                    is_dir = stat.S_ISDIR(sftp.stat(posixpath.join(path, a.filename)).st_mode)
                except OSError:
                    pass
            entries.append((a.filename, is_dir, a.st_size or 0, a.st_mtime or 0,
                            stat.filemode(a.st_mode or 0)))
        sess.sftp_cwd = path
        self.sig.listed.emit(sess, path, entries)

    def _on_listed(self, sess, path: str, entries: list) -> None:
        if sess is not self.session:
            return
        self.err_lbl.hide()
        if sess.sftp_noise and not getattr(sess, "_noise_reported", False):
            sess._noise_reported = True
            first = sess.sftp_noise.splitlines()[0][:80]
            self.status_message.emit(
                f"SFTP: ignored text printed by a login script on the server (\"{first}\"). "
                "Tip: make ~/.bashrc quiet for non-interactive shells.")
        self.path_edit.setText(path)
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for name, is_dir, size, mtime, mode in entries:
            it = _Item([name, "" if is_dir else human(size),
                        self._fmt_time(mtime), mode])
            it.setToolTip(0, f"{name}\n{mode}  {human(size)}\n"
                             f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime)) if mtime else ''}")
            it.setIcon(0, icon("folder" if is_dir else "file", C["accent"] if is_dir else C["muted"], 16))
            it.setData(0, Qt.UserRole, posixpath.join(path, name))
            it.setData(0, Qt.UserRole + 1, is_dir)
            it.setData(1, Qt.UserRole, size)
            it.setData(2, Qt.UserRole, mtime)
            self.tree.addTopLevelItem(it)
        self.tree.setSortingEnabled(True)
        dirs = sum(1 for e in entries if e[1])
        self.count_lbl.setText(f"{dirs} folders · {len(entries) - dirs} files")

    @staticmethod
    def _fmt_time(mtime: float) -> str:
        if not mtime:
            return ""
        lt = time.localtime(mtime)
        if lt.tm_year == time.localtime().tm_year:
            return time.strftime("%b %d %H:%M", lt)
        return time.strftime("%b %d %Y", lt)

    def _on_error(self, msg: str) -> None:
        # inline banner (no modal popups: a broken server would spam them on every refresh)
        self._show_progress(False)
        self.status_message.emit(f"SFTP: {msg}")
        self.err_lbl.setText(f"⚠ {msg}")
        self.err_lbl.show()

    def _on_progress(self, label: str, done: int, total: int) -> None:
        self._show_progress(True)
        self.progress_lbl.setText(label)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(min(done, total))

    def _on_finished(self, msg: str) -> None:
        self._show_progress(False)
        if msg:
            self.status_message.emit(msg)
        self.refresh()

    # ------------------------------------------------------------ actions
    def _selected(self) -> list[tuple[str, bool]]:
        return [(i.data(0, Qt.UserRole), bool(i.data(0, Qt.UserRole + 1)))
                for i in self.tree.selectedItems()]

    def _on_double(self, item: QTreeWidgetItem) -> None:
        path, is_dir = item.data(0, Qt.UserRole), item.data(0, Qt.UserRole + 1)
        if is_dir:
            self.navigate(path)
        else:
            self.edit_remote(path)

    def _menu(self, pos) -> None:
        sel = self._selected()
        m = QMenu(self)
        if len(sel) == 1 and not sel[0][1]:
            m.addAction(icon("edit"), "Open / edit (auto-upload on save)", lambda: self.edit_remote(sel[0][0]))
        if sel:
            m.addAction(icon("download"), "Download…", self.download_selected)
        if len(sel) == 1:
            m.addAction(icon("edit"), "Rename…", self.rename_selected)
            m.addAction(icon("copy"), "Copy path", lambda: self._copy(sel[0][0]))
        m.addSeparator()
        m.addAction(icon("upload"), "Upload files…", self.upload_dialog)
        m.addAction(icon("folder-plus"), "New folder…", self.new_folder)
        m.addAction(icon("refresh"), "Refresh", self.refresh)
        if sel:
            m.addSeparator()
            m.addAction(icon("trash", C["danger"]), "Delete", self.delete_selected)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _copy(self, text: str) -> None:
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(text)

    def new_folder(self) -> None:
        if not self.session:
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name.strip():
            path = posixpath.join(self.session.sftp_cwd, name.strip())
            self._run(lambda s: (s.sftp().mkdir(path), self.sig.finished.emit(f"Created {path}")))

    def rename_selected(self) -> None:
        sel = self._selected()
        if len(sel) != 1:
            return
        old = sel[0][0]
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=posixpath.basename(old))
        if ok and name.strip() and name != posixpath.basename(old):
            new = posixpath.join(posixpath.dirname(old), name.strip())
            self._run(lambda s: (s.sftp().rename(old, new), self.sig.finished.emit(f"Renamed to {name}")))

    def delete_selected(self) -> None:
        sel = self._selected()
        if not sel:
            return
        names = "\n".join(posixpath.basename(p) + ("/" if d else "") for p, d in sel[:10])
        more = f"\n… and {len(sel) - 10} more" if len(sel) > 10 else ""
        if QMessageBox.warning(self, "Delete on server?",
                               f"Permanently delete these items (folders recursively)?\n\n{names}{more}",
                               QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self._run(self._job_delete, sel)

    def _job_delete(self, sess: ShellSession, sel) -> None:
        sftp = sess.sftp()

        def rm(path, is_dir):
            if is_dir:
                for a in sftp.listdir_attr(path):
                    child = posixpath.join(path, a.filename)
                    rm(child, stat.S_ISDIR(a.st_mode or 0))
                sftp.rmdir(path)
            else:
                sftp.remove(path)
        for i, (p, d) in enumerate(sel):
            self.sig.progress.emit(f"Deleting {posixpath.basename(p)}", i, len(sel))
            rm(p, d)
        self.sig.finished.emit(f"Deleted {len(sel)} item(s)")

    # uploads
    def upload_dialog(self) -> None:
        if not self.session:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Upload files")
        if files:
            self.upload_paths(files)

    def upload_paths(self, paths: list[str]) -> None:
        if not self.session or not self.session.is_connected:
            return
        self._run(self._job_upload, paths, self.session.sftp_cwd)

    def _job_upload(self, sess: ShellSession, paths: list[str], remote_dir: str) -> None:
        sftp = sess.sftp()
        plan: list[tuple[str, str]] = []
        dirs: list[str] = []
        for p in paths:
            p = os.path.normpath(p)
            base = os.path.basename(p)
            if os.path.isdir(p):
                for root, _dnames, fnames in os.walk(p):
                    rel = os.path.relpath(root, os.path.dirname(p)).replace(os.sep, "/")
                    dirs.append(posixpath.join(remote_dir, rel))
                    for f in fnames:
                        plan.append((os.path.join(root, f), posixpath.join(remote_dir, rel, f)))
            else:
                plan.append((p, posixpath.join(remote_dir, base)))
        for d in dirs:
            try:
                sftp.mkdir(d)
            except OSError:
                pass  # already exists
        total = sum(os.path.getsize(l) for l, _ in plan) or 1
        done = 0
        for local, remote in plan:
            name = os.path.basename(local)
            start = done

            def cb(x, _t, name=name, start=start):
                self.sig.progress.emit(f"↑ {name}", start + x, total)
            sftp.put(local, remote, callback=cb)
            done += os.path.getsize(local)
        self.sig.finished.emit(f"Uploaded {len(plan)} file(s) ({human(total)})")

    # downloads
    def download_selected(self) -> None:
        sel = self._selected()
        if not sel:
            return
        dest = QFileDialog.getExistingDirectory(self, "Download to…",
                                                str(Path.home() / "Downloads"))
        if dest:
            self._run(self._job_download, sel, dest)

    def _job_download(self, sess: ShellSession, sel, dest: str) -> None:
        sftp = sess.sftp()
        plan: list[tuple[str, str, int]] = []

        def walk(rpath, lpath, is_dir):
            if is_dir:
                os.makedirs(lpath, exist_ok=True)
                for a in sftp.listdir_attr(rpath):
                    walk(posixpath.join(rpath, a.filename), os.path.join(lpath, a.filename),
                         stat.S_ISDIR(a.st_mode or 0))
            else:
                plan.append((rpath, lpath, sftp.stat(rpath).st_size or 0))
        for rpath, is_dir in sel:
            walk(rpath, os.path.join(dest, posixpath.basename(rpath)), is_dir)
        total = sum(s for *_x, s in plan) or 1
        done = 0
        for rpath, lpath, size in plan:
            start = done
            name = posixpath.basename(rpath)
            sftp.get(rpath, lpath, callback=lambda x, _t, start=start, name=name:
                     self.sig.progress.emit(f"↓ {name}", start + x, total))
            done += size
        self.sig.finished.emit(f"Downloaded {len(plan)} file(s) to {dest}")

    # edit in place
    def edit_remote(self, remote: str) -> None:
        self._run(self._job_fetch_for_edit, remote)

    def _job_fetch_for_edit(self, sess: ShellSession, remote: str) -> None:
        folder = Path(tempfile.gettempdir()) / "blamixshell-edit" / uuid.uuid4().hex[:8]
        folder.mkdir(parents=True, exist_ok=True)
        local = folder / posixpath.basename(remote)
        size = sess.sftp().stat(remote).st_size or 0
        if size > 50 * 1024 * 1024:
            raise OSError(0, "File is larger than 50 MB; download it instead", remote)
        sess.sftp().get(remote, str(local))
        self._edits[str(local)] = (sess, remote, os.path.getmtime(local))
        self.sig.edit_ready.emit(str(local), remote)

    def _open_for_edit(self, local: str, remote: str) -> None:
        self._watcher.addPath(local)
        self.status_message.emit(f"Editing {remote}: changes upload automatically when you save")
        QDesktopServices.openUrl(QUrl.fromLocalFile(local))

    def _on_local_changed(self, local: str) -> None:
        # editors often replace the file; re-watch and debounce
        t = self._debounce.get(local)
        if not t:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(lambda l=local: self._push_edit(l))
            self._debounce[local] = t
        t.start(400)

    def _push_edit(self, local: str) -> None:
        if os.path.exists(local) and local not in self._watcher.files():
            self._watcher.addPath(local)
        info = self._edits.get(local)
        if not info or not os.path.exists(local):
            return
        sess, remote, last = info
        mtime = os.path.getmtime(local)
        if mtime == last:
            return
        self._edits[local] = (sess, remote, mtime)

        def job():
            try:
                sess.sftp().put(local, remote)
                self.sig.finished.emit(f"Saved {remote} to {sess.server.label}")
            except Exception as e:
                self.sig.error.emit(f"Upload of {remote} failed: {friendly_error(e)}")
        self._pool.submit(job)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
