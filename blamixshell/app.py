"""Main window."""
from __future__ import annotations

import os
import time
import threading
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
                               QSplitter, QStackedWidget, QTabBar, QTabWidget, QToolButton, QVBoxLayout,
                               QWidget)

from . import __version__, importers
from .dialogs import (CommandPalette, ServerDialog, SettingsDialog, SnippetsDialog, UnlockDialog,
                      UpdateDialog)
from .models import Server, Store
from .server_tree import ServerTree
from .session_tab import SessionTab
from .settings import Settings
from .sftp_panel import SftpPanel
from .terminal import TerminalPane
from .platform_ui import IS_MAC, kb
from .theme import C, icon, style_window
from .vault import Vault, WrongPassword

STATE_COLORS = {"connected": C["ok"], "connecting": C["warn"], "failed": C["danger"],
                "disconnected": C["faint"], "idle": C["faint"]}


class _HealthSignals(QObject):
    done = Signal(object, object)     # (pane, Health | None)


class _HealthLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, e):  # noqa: N802
        self.clicked.emit()


class _UpdateSignals(QObject):
    checked = Signal(object)          # (manual, Release|None, error)
    progress = Signal(int, int)
    downloaded = Signal(object)       # (path, error)


def tab_icon(state_color: str, tint: str = "") -> QIcon:
    """Tab icon: the server color as a small bar (production = red), then the state dot."""
    pm = QPixmap(36, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    if tint:
        p.setBrush(QColor(tint))
        p.drawRoundedRect(2, 2, 6, 16, 3, 3)
    p.setBrush(QColor(state_color))
    p.drawEllipse(16 if tint else 6, 6, 8, 8)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def dot_icon(color: str) -> QIcon:
    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(6, 6, 8, 8)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def parse_target(text: str) -> Server | None:
    t = text.strip()
    if t.startswith("ssh "):
        t = t[4:].strip()
    port = 22
    parts = t.split()
    if "-p" in parts:
        i = parts.index("-p")
        if i + 1 < len(parts) and parts[i + 1].isdigit():
            port = int(parts[i + 1])
            del parts[i:i + 2]
    t = parts[-1] if parts else ""
    user = ""
    if "@" in t:
        user, t = t.rsplit("@", 1)
    if t.count(":") == 1:
        t, p = t.split(":")
        if p.isdigit():
            port = int(p)
    if not t:
        return None
    return Server(host=t, port=port, username=user, auth="password")


class WelcomePage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"background:{C['bg']};")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        col = QWidget()
        col.setMaximumWidth(760)
        self.lay = QVBoxLayout(col)
        self.lay.setSpacing(14)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(col, 10)
        h.addStretch(1)
        outer.addLayout(h)
        outer.addStretch(2)

        t = QLabel("Where to?", objectName="H1")
        self.lay.addWidget(t)
        sub = QLabel("Pick a server, or type an address to connect right away.", objectName="Muted")
        self.lay.addWidget(sub)
        self.quick = QLineEdit(placeholderText="user@host:port     then Enter")
        self.quick.setStyleSheet("font-size:12pt; padding:12px 16px; border-radius:12px;")
        self.quick.returnPressed.connect(lambda: (win.quick_connect(self.quick.text()), self.quick.clear()))
        self.lay.addWidget(self.quick)
        row = QHBoxLayout()
        for ic, label, fn in [("plus", "New server", win.new_server),
                              ("import", "Import from PuTTY", win.import_putty),
                              ("import", "Import ~/.ssh/config", win.import_ssh_config),
                              ("cloud", "Import from AWS", win.import_aws)]:
            b = QPushButton(icon(ic), " " + label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        self.lay.addLayout(row)
        self.lay.addSpacing(12)
        self.recent_lbl = QLabel("RECENT", objectName="SectionLabel")
        self.lay.addWidget(self.recent_lbl)
        self.grid = QGridLayout()
        self.grid.setSpacing(10)
        self.lay.addLayout(self.grid)
        self.lay.addSpacing(10)
        keys = QLabel(kb(
            "<span style='color:%s'>Ctrl+Shift+P</span> command palette &nbsp;·&nbsp; "
            "<span style='color:%s'>Ctrl+Shift+D / E</span> split &nbsp;·&nbsp; "
            "<span style='color:%s'>Ctrl+Shift+F</span> find &nbsp;·&nbsp; "
            "<span style='color:%s'>Ctrl+Shift+S</span> files &nbsp;·&nbsp; "
            "<span style='color:%s'>Ctrl+Shift+B</span> broadcast" % ((C["muted"],) * 5)),
            objectName="Hint")
        keys.setWordWrap(True)
        self.lay.addWidget(keys)
        self.refresh()

    def refresh(self) -> None:
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        store = self.win.store
        items = store.recent(6) or [s for s in store.servers.values() if s.favorite][:6] \
            or sorted(store.servers.values(), key=lambda s: s.label.lower())[:6]
        self.recent_lbl.setText("RECENT" if store.recent(1) else ("SERVERS" if items else ""))
        for i, s in enumerate(items):
            card = QPushButton()
            card.setObjectName("Card")
            card.setCursor(Qt.PointingHandCursor)
            card.setMinimumHeight(74)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 10, 14, 10)
            top = QHBoxLayout()
            ic = QLabel()
            ic.setPixmap(icon("server", s.color or C["accent"], 18).pixmap(18, 18))
            name = QLabel(s.label)
            name.setStyleSheet("font-weight:600; background:transparent;")
            top.addWidget(ic)
            top.addWidget(name, 1)
            cl.addLayout(top)
            ago = ""
            if s.last_connected:
                d = time.time() - s.last_connected
                ago = ("just now" if d < 90 else f"{int(d // 60)} min ago" if d < 3600
                       else f"{int(d // 3600)} h ago" if d < 86400 else f"{int(d // 86400)} d ago")
            addr = QLabel(s.address + (f"  ·  {ago}" if ago else ""), objectName="Hint")
            addr.setStyleSheet("background:transparent;")
            cl.addWidget(addr)
            for w in (ic, name, addr):
                w.setAttribute(Qt.WA_TransparentForMouseEvents)
            card.clicked.connect(lambda _=False, sid=s.id: self.win.connect_server(sid))
            self.grid.addWidget(card, i // 3, i % 3)


class MainWindow(QMainWindow):
    def __init__(self, store: Store, settings: Settings):
        super().__init__()
        self.store = store
        self.settings = settings
        self._last_shortcut = ("", 0.0)
        self.setWindowTitle("BlamixShell")
        self.resize(1400, 860)
        self.setMinimumSize(900, 560)

        root = QSplitter(Qt.Horizontal)
        root.setHandleWidth(1)
        root.setChildrenCollapsible(False)
        self.setCentralWidget(root)
        self.root_split = root

        # ---------------- sidebar
        side = QWidget(objectName="Sidebar")
        self.side = side
        side.setAttribute(Qt.WA_StyledBackground, True)
        side.setMinimumWidth(230)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(12, 14, 10, 10)
        sl.setSpacing(10)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icon("terminal", C["accent"], 22).pixmap(22, 22))
        bt = QVBoxLayout()
        bt.setSpacing(0)
        bt.addWidget(QLabel("BlamixShell", objectName="Brand"))
        self.count_lbl = QLabel(objectName="BrandSub")
        bt.addWidget(self.count_lbl)
        brand.addWidget(logo)
        brand.addLayout(bt, 1)
        add = QToolButton()
        add.setIcon(icon("plus", C["text"], 18))
        add.setToolTip(kb("New server (Ctrl+Shift+N)"))
        add.setPopupMode(QToolButton.InstantPopup)
        addm = QMenu(add)
        addm.addAction(icon("server"), "New server…", self.new_server)
        addm.addAction(icon("folder-plus"), "New group…", lambda: self.new_group(""))
        addm.addSeparator()
        addm.addAction(icon("import"), "Import from PuTTY", self.import_putty)
        addm.addAction(icon("import"), "Import ~/.ssh/config", self.import_ssh_config)
        addm.addAction(icon("cloud"), "Import from AWS (SSM)…", self.import_aws)
        addm.addSeparator()
        addm.addAction(icon("lock"), "AWS SSO sign-in…", self.aws_sign_in)
        add.setMenu(addm)
        brand.addWidget(add)
        sl.addLayout(brand)

        self.search = QLineEdit(objectName="Search", placeholderText="Search  ·  tag:prod")
        self.search.addAction(icon("search", C["faint"], 16), QLineEdit.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda t: self.tree.rebuild(t))
        self.search.returnPressed.connect(self._search_enter)
        sl.addWidget(self.search)

        self.tree = ServerTree(store)
        self.tree.connect_requested.connect(self.connect_server)
        self.tree.server_moved.connect(self._move_server)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        sl.addWidget(self.tree, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(2)
        for ic, tip, fn in [("code", "Snippets", self.manage_snippets),
                            ("settings", "Settings", self.open_settings),
                            ("lock", "Lock vault", self.lock)]:
            b = QToolButton()
            b.setIcon(icon(ic, C["muted"], 18))
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bottom.addWidget(b)
        help_btn = QToolButton()
        help_btn.setIcon(icon("help", C["muted"], 18))
        help_btn.setToolTip("Help")
        help_btn.setPopupMode(QToolButton.InstantPopup)
        help_btn.setMenu(self._help_menu(help_btn))
        bottom.addWidget(help_btn)
        bottom.addStretch(1)
        ver = QLabel(f"v{__version__}", objectName="Hint")
        bottom.addWidget(ver)
        sl.addLayout(bottom)
        root.addWidget(side)

        # ---------------- center
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        tb = QWidget(objectName="Toolbar")
        tb.setAttribute(Qt.WA_StyledBackground, True)
        tb.setFixedHeight(46)
        tl = QHBoxLayout(tb)
        tl.setContentsMargins(10, 6, 10, 6)
        tl.setSpacing(4)
        pal = QPushButton(icon("search", C["faint"], 16), "  Connect or run a command…")
        pal.setStyleSheet(f"text-align:left; color:{C['faint']}; background:{C['bg']}; "
                          f"border:1px solid {C['border']}; border-radius:9px; padding:6px 12px;")
        pal.setFixedWidth(430)
        pal.clicked.connect(self.open_palette)
        self.pal_btn = pal
        kbl = QLabel(kb("Ctrl+Shift+P"), pal)
        kbl.setStyleSheet(f"color:{C['faint']}; font-size:8pt; background:transparent; border:none;")
        kbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        pal_l = QHBoxLayout(pal)
        pal_l.setContentsMargins(0, 0, 10, 0)
        pal_l.addStretch(1)
        pal_l.addWidget(kbl)
        self.btn_side = self._tool("sidebar", "Show / hide server list (Ctrl+Shift+L)", self.toggle_sidebar,
                                   checkable=True)
        self.btn_side.setIcon(icon("sidebar", C["muted"], 18))
        tl.addWidget(self.btn_side)
        tl.addWidget(pal, 1)
        tl.addStretch(1)
        self.btn_split_r = self._tool("split-h", "Split right (Ctrl+Shift+D): same server; "
                                      "the arrow picks another server", lambda: self.split(Qt.Horizontal))
        self.btn_split_d = self._tool("split-v", "Split down (Ctrl+Shift+E): same server; "
                                      "the arrow picks another server", lambda: self.split(Qt.Vertical))
        for b, where in ((self.btn_split_r, "right"), (self.btn_split_d, "down")):
            b.setObjectName("SplitBtn")
            b.setPopupMode(QToolButton.MenuButtonPopup)
            menu = QMenu(b)
            menu.aboutToShow.connect(lambda m=menu, w=where: self._fill_split_menu(m, w))
            b.setMenu(menu)
        self.btn_rotate = self._tool("rotate", "Rotate split: side by side ↔ stacked (Ctrl+Shift+O)",
                                     self.rotate_split)
        self.btn_bcast = self._tool("broadcast", "Broadcast input to all panes in this tab (Ctrl+Shift+B)",
                                    self.toggle_broadcast, checkable=True)
        self.btn_snip = self._tool("code", "Snippets", None)
        self.btn_snip.setPopupMode(QToolButton.InstantPopup)
        self.snip_menu = QMenu(self.btn_snip)
        self.snip_menu.aboutToShow.connect(self._fill_snippets)
        self.btn_snip.setMenu(self.snip_menu)
        self.btn_sftp = self._tool("folder", "Files / SFTP (Ctrl+Shift+S)", self.toggle_sftp, checkable=True)
        self.btn_dash = self._tool("gauge", "Server dashboard (Ctrl+Shift+I)", lambda: self.open_dashboard())
        for b in (self.btn_split_r, self.btn_split_d, self.btn_rotate, self.btn_bcast, self.btn_snip, self.btn_dash, self.btn_sftp):
            tl.addWidget(b)
        cl.addWidget(tb)

        self.stack = QStackedWidget()
        self.welcome = WelcomePage(self)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(False)   # we add our own close button with proper spacing
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.ElideRight)
        self.tabs.setIconSize(QSize(18, 10))
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        newtab = QToolButton()
        newtab.setIcon(icon("plus", C["muted"], 16))
        newtab.setToolTip(kb("New connection (Ctrl+Shift+T)"))
        newtab.clicked.connect(self.open_palette)
        self.tabs.setCornerWidget(newtab, Qt.TopRightCorner)
        self.stack.addWidget(self.welcome)
        self.stack.addWidget(self.tabs)
        cl.addWidget(self.stack, 1)
        root.addWidget(center)

        # ---------------- sftp
        self.sftp = SftpPanel()
        self.sftp.status_message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        root.addWidget(self.sftp)
        root.setStretchFactor(0, 0)
        root.setStretchFactor(1, 1)
        root.setStretchFactor(2, 0)
        root.setSizes([int(settings.get("sidebar_width", 280)), 900, 360])
        self.sftp.setVisible(bool(settings.get("sftp_visible")))
        side_on = bool(settings.get("sidebar_visible", True))
        side.setVisible(side_on)
        self.btn_side.setChecked(side_on)   # (isVisible() is False until the window is shown)
        self.btn_sftp.setChecked(bool(settings.get("sftp_visible")))

        # ---------------- status bar
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.status_left = QLabel()
        self.status_right = QLabel()
        sb.addWidget(self.status_left, 1)
        # health strip: CPU / memory / disk / load of the active terminal's server
        self.health_lbl = _HealthLabel()
        self.health_lbl.setCursor(Qt.PointingHandCursor)
        self.health_lbl.setToolTip("Click for the server dashboard")
        self.health_lbl.clicked.connect(lambda: self.open_dashboard())
        self.health_lbl.hide()
        sb.addPermanentWidget(self.health_lbl)
        sb.addPermanentWidget(self.status_right)
        self._health = _HealthSignals()
        self._health.done.connect(self._show_health)
        self._health_prev: dict[int, tuple] = {}      # id(pane) -> last CPU sample
        self._health_busy = False
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._poll_health)
        self._health_timer.start(self._health_ms())

        self._install_shortcuts()
        self.refresh_all()
        geo = settings.get("window_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start(1000)

        # updates: quiet background check a few seconds after start (at most daily)
        self._upd = _UpdateSignals()
        self._upd.checked.connect(self._on_update_checked)
        self._upd.downloaded.connect(self._on_update_downloaded)
        self._upd.progress.connect(self._on_update_progress)
        self.update_btn = QPushButton()
        self.update_btn.setObjectName("Primary")
        self.update_btn.setStyleSheet("padding: 1px 10px; border-radius: 7px; font-size: 8.5pt;")
        self.update_btn.hide()
        self.update_btn.clicked.connect(lambda: self._show_update(self._pending_release))
        self.statusBar().addPermanentWidget(self.update_btn)
        self._pending_release = None
        QTimer.singleShot(4000, lambda: self.check_updates(manual=False))
        QTimer.singleShot(8000, self._prune_logs)

        # shared vault (OneDrive / Syncthing / USB): pick up changes from other computers
        self.store.notify = lambda msg: self.statusBar().showMessage(msg, 10000)
        self._vault_timer = QTimer(self)
        self._vault_timer.timeout.connect(self.check_vault_changes)
        self._vault_timer.start(5000)
        QApplication.instance().applicationStateChanged.connect(
            lambda st: st == Qt.ApplicationActive and self.check_vault_changes())

        # session restore: the layout is saved shortly after every change and on exit
        self.persist_layout = False      # set by restore_session() (not in self-test runs)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(800)
        self._layout_timer.timeout.connect(self.save_session)
        self.tabs.tabBar().tabMoved.connect(lambda *_: self._layout_timer.start())

    # ================================================================ helpers
    def _tab_close_button(self, tab: "SessionTab") -> QWidget:
        """Close button with real breathing room from the tab edge (the style's own
        close-button ignores margins on some platforms/DPI settings)."""
        holder = QWidget()
        holder.setAttribute(Qt.WA_TranslucentBackground)
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(6, 0, 4, 0)
        btn = QToolButton(holder)
        btn.setObjectName("TabClose")
        btn.setIcon(icon("x", C["muted"], 12))
        btn.setIconSize(QSize(12, 12))
        btn.setFixedSize(20, 20)
        btn.setToolTip(kb("Close tab (Ctrl+Shift+W closes a pane)"))
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda: self.close_tab(self.tabs.indexOf(tab)))
        lay.addWidget(btn)
        return holder

    def _tool(self, ic, tip, fn, checkable=False) -> QToolButton:
        b = QToolButton()
        b.setIcon(icon(ic, C["muted"], 18))
        b.setToolTip(kb(tip))
        b.setCheckable(checkable)
        if fn:
            b.clicked.connect(fn)
        return b

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)

    def _install_shortcuts(self) -> None:
        mapping = {"P": "p", "T": "t", "W": "w", "D": "d", "E": "e", "B": "b", "S": "s",
                   "N": "n", "R": "r", "V": "v", "L": "l", "I": "i", "O": "o"}
        # On macOS Qt's "Ctrl" is the Cmd key: Cmd+P, Cmd+D … (Cmd+Shift+… works too)
        prefixes = ["Ctrl+Shift+", "Ctrl+"] if IS_MAC else ["Ctrl+Shift+"]
        for prefix in prefixes:
            for key, name in mapping.items():
                sc = QShortcut(QKeySequence(f"{prefix}{key}"), self)
                sc.setContext(Qt.WindowShortcut)
                sc.activated.connect(lambda n=name: self.handle_shortcut(f"ctrl+shift+{n}"))
            for i in range(1, 10):
                sc = QShortcut(QKeySequence(f"{prefix}{i}"), self)
                sc.activated.connect(lambda n=i: self.handle_shortcut(f"ctrl+shift+{n}"))
        tabkey = "Meta" if IS_MAC else "Ctrl"   # Meta = the physical Control key on macOS
        QShortcut(QKeySequence(f"{tabkey}+Tab"), self, activated=lambda: self.handle_shortcut("nexttab"))
        QShortcut(QKeySequence(f"{tabkey}+Shift+Tab"), self, activated=lambda: self.handle_shortcut("prevtab"))
        QShortcut(QKeySequence("Ctrl+F"), self.search, activated=self.search.setFocus)

    def handle_shortcut(self, name: str) -> None:
        # the terminal and the window can both see a key; run each action once
        last, t = self._last_shortcut
        now = time.monotonic()
        if last == name and now - t < 0.25:
            return
        self._last_shortcut = (name, now)
        pane = self.active_pane()
        acts = {
            "ctrl+shift+p": self.open_palette, "ctrl+shift+t": self.open_palette,
            "ctrl+shift+w": lambda: pane and self.close_pane(pane),
            "ctrl+shift+d": lambda: self.split(Qt.Horizontal),
            "ctrl+shift+e": lambda: self.split(Qt.Vertical),
            "ctrl+shift+o": self.rotate_split,
            "ctrl+shift+b": lambda: (self.btn_bcast.toggle(), self.toggle_broadcast()),
            "ctrl+shift+s": lambda: (self.btn_sftp.toggle(), self.toggle_sftp()),
            "ctrl+shift+i": lambda: self.open_dashboard(),
            "ctrl+shift+n": self.new_server,
            "ctrl+shift+r": lambda: pane and pane.reconnect(),
            "ctrl+shift+v": lambda: pane and pane.paste(),
            "ctrl+shift+k": lambda: self.btn_snip.showMenu(),
            "ctrl+shift+l": lambda: (self.btn_side.toggle(), self.toggle_sidebar()),
            "nexttab": lambda: self.tabs.count() and self.tabs.setCurrentIndex(
                (self.tabs.currentIndex() + 1) % self.tabs.count()),
            "prevtab": lambda: self.tabs.count() and self.tabs.setCurrentIndex(
                (self.tabs.currentIndex() - 1) % self.tabs.count()),
            "zoom+": lambda: self._zoom(1), "zoom-": lambda: self._zoom(-1), "zoom0": lambda: self._zoom(0),
        }
        for i in range(1, 10):
            acts[f"ctrl+shift+{i}"] = lambda i=i: i <= self.tabs.count() and self.tabs.setCurrentIndex(i - 1)
        fn = acts.get(name)
        if fn:
            fn()

    def _zoom(self, d: int) -> None:
        self.settings["font_size"] = 14 if d == 0 else max(8, min(32, int(self.settings["font_size"]) + d))
        self.settings.save()
        for p in self.all_panes():
            p.apply_settings()
        self.statusBar().showMessage(f"Font size {self.settings['font_size']}", 1500)

    # ================================================================ state
    def current_tab(self) -> SessionTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, SessionTab) else None

    def active_pane(self) -> TerminalPane | None:
        t = self.current_tab()
        return t.active if t else None

    def all_panes(self) -> list[TerminalPane]:
        out = []
        for i in range(self.tabs.count()):
            out += self.tabs.widget(i).panes()
        return out

    def refresh_all(self) -> None:
        self.tree.rebuild(self.search.text())
        for p in self.all_panes():     # colors may have changed (server edited, group color)
            p.set_tint(self.store.color_for(self.store.servers.get(p.server.id, p.server)))
        if hasattr(self, "tabs"):
            self._color_tabs()
        n = len(self.store.servers)
        self.count_lbl.setText(f"{n} server{'s' if n != 1 else ''}")
        self.welcome.refresh()
        self._update_status()

    def _update_status(self) -> None:
        pane = self.active_pane()
        if pane:
            st = pane.state
            extra = ""
            if st == "connected" and pane.session:
                tr = pane.session.client.get_transport() if pane.session.client else None
                if tr:
                    extra = f"  ·  {tr.remote_version.split('_')[-1] if tr.remote_version else ''}" \
                            f"  ·  {tr.remote_cipher}"
            t = self.current_tab()
            bc = "  ·  BROADCAST ON" if t and t.broadcast else ""
            self.status_left.setText(f"● {pane.server.label}  ·  {pane.server.address}  ·  {st}{extra}{bc}")
            self.status_left.setStyleSheet(f"color:{STATE_COLORS.get(st, C['muted'])};" if bc == "" else
                                           f"color:{C['warn']};")
        else:
            self.status_left.setText("No active session")
            self.status_left.setStyleSheet("")
        live = sum(1 for p in self.all_panes() if p.state == "connected")
        self.status_right.setText(f"🔒 vault encrypted  ·  {live} live session{'s' if live != 1 else ''}")

    # ================================================================ health strip
    def _health_ms(self) -> int:
        try:
            return max(1, int(self.settings.get("health_interval", 5))) * 1000
        except (TypeError, ValueError):
            return 5000

    def _health_pane(self):
        p = self.active_pane()
        if (p and p.state == "connected" and p.server.uses_ssh and p.session
                and getattr(p.session, "client", None)):
            return p
        return None

    def _poll_health(self) -> None:
        if not self.settings.get("health_strip", True) or self._health_busy:
            if not self.settings.get("health_strip", True):
                self.health_lbl.hide()
            return
        if not self.isVisible() or self.isMinimized():
            return
        pane = self._health_pane()
        if pane is None:
            self.health_lbl.hide()
            return
        self._health_busy = True
        from . import dashboard as d
        client, prev = pane.session.client, self._health_prev.get(id(pane))

        def run():
            try:
                h = d.health(d.Runner(client), prev)
            except Exception:
                h = None
            self._health.done.emit(pane, h)
        threading.Thread(target=run, daemon=True, name="health").start()

    def _health_kick(self) -> None:
        """The active terminal changed: show its server's numbers soon, not the old ones."""
        pane = self._health_pane()
        if pane is not getattr(self, "_health_for", None):
            self._health_for = pane
            self.health_lbl.hide()
            QTimer.singleShot(300, self._poll_health)

    def _show_health(self, pane, h) -> None:
        self._health_busy = False
        if h is None or pane is not self._health_pane():
            if self._health_pane() is None:
                self.health_lbl.hide()
            elif h is None and pane is self._health_pane() and self.health_lbl.isVisible():
                # poll failed: keep the last numbers but say they are old
                if "(stale)" not in self.health_lbl.text():
                    self.health_lbl.setText(self.health_lbl.text().replace(
                        "&nbsp;&nbsp;", f" <span style='color:{C['faint']}'>(stale)</span>&nbsp;&nbsp;"))
            return
        self._health_prev[id(pane)] = h.sample

        def part(label, v, warn=75, bad=90, fmt="{:.0f}%"):
            if v is None:
                return f"<span style='color:{C['faint']}'>{label} …</span>"
            col = C["danger"] if v >= bad else C["warn"] if v >= warn else C["muted"]
            return f"<span style='color:{C['faint']}'>{label}</span> <span style='color:{col}'>{fmt.format(v)}</span>"
        load_bad = (h.cpus or 1) * 1.5
        cpu = part("CPU", h.cpu) if h.sample else f"<span style='color:{C['faint']}'>CPU n/a</span>"
        parts = [cpu, part("RAM", h.mem), part("/", h.disk, 80, 90),
                 part("load", h.load, (h.cpus or 1), load_bad, "{:.2f}")]
        if h.swap is not None and h.swap >= 10:       # only when it matters
            parts.insert(2, part("swap", h.swap, 25, 60))
        self.health_lbl.setText("  ·  ".join(parts) + "&nbsp;&nbsp;")
        from .dashboard import human_kb
        tip = [f"{pane.server.label} ({h.cpus} CPU{'s' if h.cpus != 1 else ''})"]
        if h.cpu is not None:
            tip.append(f"CPU {h.cpu:.0f}%")
        if h.mem_kb:
            tip.append(f"RAM {human_kb(h.mem_kb[0])} / {human_kb(h.mem_kb[1])}")
        if h.swap_kb:
            tip.append(f"Swap {human_kb(h.swap_kb[0])} / {human_kb(h.swap_kb[1])}")
        if h.disk_kb:
            tip.append(f"/ {human_kb(h.disk_kb[0])} / {human_kb(h.disk_kb[1])} used")
        if h.loads:
            tip.append("Load " + " ".join(f"{x:.2f}" for x in h.loads) + " (1 / 5 / 15 min)")
        tip.append("Click for the dashboard.")
        self.health_lbl.setToolTip("\n".join(tip))
        self.health_lbl.show()

    def _on_tab_changed(self, _i: int) -> None:
        QTimer.singleShot(0, self._health_kick)
        t = self.current_tab()
        self.stack.setCurrentIndex(1 if self.tabs.count() else 0)
        if t:
            for p in t.panes():      # restored tabs connect when you look at them
                if p.deferred:
                    p.start()
        self._layout_timer.start()
        self.btn_bcast.setChecked(bool(t and t.broadcast))
        self._sync_sftp()
        if t and t.active:
            QTimer.singleShot(0, t.active.focus_terminal)

    def _on_pane_state(self, _pane=None) -> None:
        self._health_kick()
        live = {p.server.id for p in self.all_panes() if p.state == "connected"}
        if live != self.tree.live:
            self.tree.live = live
            self.tree.viewport().update()
        for i in range(self.tabs.count()):
            t = self.tabs.widget(i)
            self.tabs.setTabText(i, t.title())
            self.tabs.setTabIcon(i, tab_icon(STATE_COLORS.get(t.state(), C["faint"]), self._tab_tint(t)))
            ps = t.panes()
            self.tabs.setTabToolTip(i, "\n".join(f"{p.server.label} ({p.server.address}): {p.state}" for p in ps))
        self._color_tabs()
        self._sync_sftp()
        self._update_status()
        self._layout_timer.start()

    @staticmethod
    def _tab_tint(t) -> str:
        p = t.active or (t.panes() or [None])[0]
        return p.tint if p is not None else ""

    def _color_tabs(self) -> None:
        for i in range(self.tabs.count()):
            t = self.tabs.widget(i)
            self.tabs.setTabIcon(i, tab_icon(STATE_COLORS.get(t.state(), C["faint"]), self._tab_tint(t)))

    def _sync_sftp(self) -> None:
        if not self.sftp.isVisible():
            return
        p = self.active_pane()
        self.sftp.set_session(p.session if p and p.state == "connected" and p.server.uses_ssh else None)

    # ================================================================ connect
    def _make_pane(self, server: Server, touch: bool = True) -> TerminalPane:
        pane = TerminalPane(server, self.store.servers.get, self.settings, tint=self.store.color_for(server))
        pane.close_requested.connect(self.close_pane)
        pane.shortcut.connect(lambda _p, n: self.handle_shortcut(n))
        pane.state_changed.connect(self._on_pane_state)
        pane.dashboard_requested.connect(self.open_dashboard)
        if touch and server.id in self.store.servers:
            self.store.touch(server.id)
        return pane

    def _new_tab(self) -> SessionTab:
        tab = SessionTab()
        tab.emptied.connect(self._tab_emptied)
        tab.active_pane_changed.connect(lambda _p: (self._sync_sftp(), self._update_status(), self._health_kick()))
        tab.changed.connect(self._on_pane_state)
        return tab

    def _add_tab(self, tab: SessionTab) -> int:
        idx = self.tabs.addTab(tab, tab_icon(C["faint"], self._tab_tint(tab)), tab.title())
        self.tabs.tabBar().setTabButton(idx, QTabBar.RightSide, self._tab_close_button(tab))
        return idx

    # ================================================================ session restore
    def _describe_pane(self, pane: TerminalPane) -> dict:
        s = pane.server
        if s.id in self.store.servers:
            return {"server": s.id}
        return {"adhoc": {"host": s.host, "port": s.port, "username": s.username}}   # quick connect

    def save_session(self) -> None:
        if not self.persist_layout or not self.settings.get("restore_tabs", True):
            return
        tabs = []
        current = 0
        for i in range(self.tabs.count()):
            tree = self.tabs.widget(i).layout_tree(self._describe_pane)
            if tree:
                if i == self.tabs.currentIndex():
                    current = len(tabs)
                tabs.append(tree)
        data = {"tabs": tabs, "current": current} if tabs else {}
        if data != self.settings.get("last_session"):
            self.settings["last_session"] = data
            self.settings.save()

    def restore_session(self) -> int:
        """Reopen last session's tabs (called once after unlocking). Returns tabs opened."""
        self.persist_layout = True
        data = self.settings.get("last_session") or {}
        if not self.settings.get("restore_tabs", True) or not data.get("tabs"):
            return 0

        def make(node: dict):
            if "server" in node:
                srv = self.store.servers.get(node["server"])
            elif "adhoc" in node:
                a = node["adhoc"]
                srv = Server(host=a.get("host", ""), port=int(a.get("port") or 22),
                             username=a.get("username", ""), auth="password") if a.get("host") else None
            else:
                srv = None
            if srv is None:          # server was deleted since
                return None
            pane = self._make_pane(srv, touch=False)
            pane.defer()
            return pane

        opened = 0
        for tree in data["tabs"]:
            tab = self._new_tab()
            if tab.build(tree, make):
                self._add_tab(tab)
                opened += 1
            else:
                tab.deleteLater()
        if opened:
            self.tabs.setCurrentIndex(min(int(data.get("current", 0)), opened - 1))
            self._on_tab_changed(self.tabs.currentIndex())
            self._on_pane_state()
            self.statusBar().showMessage(
                f"Reopened {opened} tab{'s' if opened != 1 else ''} from your last session", 5000)
        return opened

    def connect_server(self, server_id: str, where: str = "tab") -> None:
        s = self.store.servers.get(server_id)
        if not s:
            return
        self._open(s, where)
        self.welcome.refresh()

    def _open(self, server: Server, where: str = "tab") -> None:
        pane = self._make_pane(server)
        tab = self.current_tab()
        if where in ("right", "down") and tab and tab.active:
            tab.split(tab.active, pane, Qt.Horizontal if where == "right" else Qt.Vertical)
        else:
            tab = self._new_tab()
            tab.add_first(pane)
            idx = self._add_tab(tab)
            self.tabs.setCurrentIndex(idx)
        self.stack.setCurrentIndex(1)
        pane.start()
        QTimer.singleShot(50, pane.focus_terminal)
        self._on_pane_state()

    def quick_connect(self, text: str) -> None:
        s = parse_target(text)
        if not s:
            return
        # reuse a saved server if one matches
        for saved in self.store.servers.values():
            if saved.host.lower() == s.host.lower() and saved.port == s.port and \
                    (not s.username or saved.username == s.username):
                self.connect_server(saved.id)
                return
        s.name = ""
        self._open(s)

    def split(self, orientation) -> None:
        tab = self.current_tab()
        if not tab or not tab.active:
            return
        self._open(tab.active.server, "right" if orientation == Qt.Horizontal else "down")

    def rotate_split(self) -> None:
        tab = self.current_tab()
        if not tab or not tab.active or not tab.rotate(tab.active):
            self.statusBar().showMessage("Nothing to rotate: split the terminal first.", 4000)

    def _fill_split_menu(self, menu: QMenu, where: str) -> None:
        """Split button arrow: the same server, a recent one, or pick any server."""
        menu.clear()
        tab = self.current_tab()
        cur = tab.active.server if tab and tab.active else None
        if cur:
            menu.addAction(icon("terminal"), f"{cur.label}  (same server)",
                           lambda: self.split(Qt.Horizontal if where == "right" else Qt.Vertical))
            menu.addSeparator()
        recent = [s for s in self.store.recent(10) if not cur or s.id != cur.id][:8]
        for s in recent:
            menu.addAction(dot_icon(self.store.color_for(s)) if self.store.color_for(s) else icon("server"),
                           s.label, lambda sid=s.id: self.connect_server(sid, where))
        if recent:
            menu.addSeparator()
        menu.addAction(icon("search"), "Other server…", lambda: self.split_with(where))

    def split_with(self, where: str) -> None:
        """Open any saved server next to the active terminal (right or down)."""
        entries = []
        for s in sorted(self.store.servers.values(), key=lambda x: (-x.last_connected, x.label.lower())):
            tags = "  ".join("#" + t for t in s.tags)
            entries.append((s.label, f"{s.address}  {s.group}  {tags}".strip(),
                            lambda sid=s.id: self.connect_server(sid, where), "server"))
        side = "on the right" if where == "right" else "below"
        pal = CommandPalette(entries, self, placeholder=f"Which server should open {side}?",
                             anchor=getattr(self, "pal_btn", None))
        pal.exec()

    def close_pane(self, pane: TerminalPane) -> None:
        for i in range(self.tabs.count()):
            t = self.tabs.widget(i)
            if pane in t.panes():
                t.remove(pane)
                break
        self._on_pane_state()

    def close_tab(self, index: int) -> None:
        t = self.tabs.widget(index)
        live = [p for p in t.panes() if p.state == "connected"]
        if live and QMessageBox.question(
                self, "Close tab?", f"Disconnect {len(live)} live session(s) in this tab?") != QMessageBox.Yes:
            return
        t.shutdown()
        self.tabs.removeTab(index)
        t.deleteLater()
        self._on_tab_changed(self.tabs.currentIndex())
        self._on_pane_state()

    def _tab_emptied(self, tab: SessionTab) -> None:
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.removeTab(idx)
            tab.deleteLater()
        self._on_tab_changed(self.tabs.currentIndex())

    def toggle_broadcast(self) -> None:
        t = self.current_tab()
        if not t:
            self.btn_bcast.setChecked(False)
            return
        t.broadcast = self.btn_bcast.isChecked()
        self.statusBar().showMessage(
            f"Broadcast {'ON: typing goes to all ' + str(len(t.panes())) + ' panes' if t.broadcast else 'off'}", 3000)
        self._update_status()

    def toggle_sidebar(self) -> None:
        vis = self.btn_side.isChecked()
        self.side.setVisible(vis)
        if vis:
            sizes = self.root_split.sizes()
            want = int(self.settings.get("sidebar_width", 280))
            if sizes[0] < 200:
                sizes[1] = max(300, sizes[1] - want)
                sizes[0] = want
                self.root_split.setSizes(sizes)
        self.settings["sidebar_visible"] = vis
        self.settings.save()

    def toggle_sftp(self) -> None:
        vis = self.btn_sftp.isChecked()
        self.sftp.setVisible(vis)
        self.settings["sftp_visible"] = vis
        self.settings.save()
        if vis:
            sizes = self.root_split.sizes()
            if sizes[2] < 250:
                sizes[1] -= 360 - sizes[2]
                sizes[2] = 360
                self.root_split.setSizes(sizes)
            self.sftp.session = None
            self._sync_sftp()

    # ================================================================ palette
    def open_palette(self) -> None:
        entries = []
        for s in sorted(self.store.servers.values(), key=lambda x: (-x.last_connected, x.label.lower())):
            tags = "  ".join("#" + t for t in s.tags)
            entries.append((s.label, f"{s.address}  {s.group}  {tags}".strip(),
                            lambda sid=s.id: self.connect_server(sid), "server"))
        acts = [
            ("New server…", "Ctrl+Shift+N", self.new_server, "plus"),
            ("Split right", "Ctrl+Shift+D", lambda: self.split(Qt.Horizontal), "split-h"),
            ("Split down", "Ctrl+Shift+E", lambda: self.split(Qt.Vertical), "split-v"),
            ("Split right with another server…", "", lambda: self.split_with("right"), "split-h"),
            ("Split down with another server…", "", lambda: self.split_with("down"), "split-v"),
            ("Rotate split (side by side ↔ stacked)", "Ctrl+Shift+O", self.rotate_split, "rotate"),
            ("Toggle server list", "Ctrl+Shift+L", lambda: (self.btn_side.toggle(), self.toggle_sidebar()), "sidebar"),
            ("Toggle files panel", "Ctrl+Shift+S", lambda: (self.btn_sftp.toggle(), self.toggle_sftp()), "folder"),
            ("Server dashboard", "Ctrl+Shift+I", lambda: self.open_dashboard(), "gauge"),
            ("Toggle broadcast", "Ctrl+Shift+B", lambda: (self.btn_bcast.toggle(), self.toggle_broadcast()), "broadcast"),
            ("Reconnect", "Ctrl+Shift+R", lambda: self.active_pane() and self.active_pane().reconnect(), "refresh"),
            ("Clear terminal", "", lambda: self.active_pane() and self.active_pane().view.bridge.command.emit("clear"), "terminal"),
            ("Find in terminal", "Ctrl+Shift+F", lambda: self.active_pane() and self.active_pane().view.bridge.command.emit("find"), "search"),
            ("Manage snippets…", "", self.manage_snippets, "code"),
            ("Import from PuTTY", "", self.import_putty, "import"),
            ("Import ~/.ssh/config", "", self.import_ssh_config, "import"),
            ("Import from AWS (SSM)…", "", self.import_aws, "cloud"),
            ("AWS SSO sign-in…", "", self.aws_sign_in, "lock"),
            ("Settings…", "", self.open_settings, "settings"),
            ("Lock vault", "", self.lock, "lock"),
            ("Check for updates", "", lambda: self.check_updates(manual=True), "refresh"),
            ("Install update from file…", "", self.update_from_file, "import"),
            ("Start / stop recording this session", "", self.toggle_recording, "record"),
            ("Open logs folder", "", self.open_logs_folder, "folder"),
            ("About BlamixShell", "", self.show_about, "help"),
        ]
        for sn in self.store.snippets:
            acts.append((f"Snippet: {sn.name}", sn.command.replace("\n", " ⏎ ")[:60],
                         lambda c=sn.command: self.send_snippet(c), "code"))
        acts = [(t, kb(sub), cb, ic) for t, sub, cb, ic in acts]
        pal = CommandPalette(entries + acts, self, anchor=getattr(self, "pal_btn", None))
        pal.quick_connect = self.quick_connect
        pal.exec()

    # ================================================================ servers
    def new_server(self, group: str = "") -> None:
        dlg = ServerDialog(self.store, None, self, group=group if isinstance(group, str) else "")
        if dlg.exec() == QDialog.Accepted:
            self.store.upsert(dlg.result_server())
            self.refresh_all()

    def edit_server(self, sid: str) -> None:
        s = self.store.servers.get(sid)
        if not s:
            return
        dlg = ServerDialog(self.store, s, self)
        if dlg.exec() == QDialog.Accepted:
            self.store.upsert(dlg.result_server())
            self.refresh_all()

    def duplicate_server(self, sid: str) -> None:
        s = self.store.servers.get(sid)
        if s:
            c = s.copy()
            from .models import _id
            c.id = _id()
            c.name = f"{s.label} (copy)"
            c.last_connected = 0
            c.connect_count = 0
            self.store.upsert(c)
            self.refresh_all()
            self.edit_server(c.id)

    def delete_server(self, sid: str) -> None:
        s = self.store.servers.get(sid)
        if s and QMessageBox.question(self, "Delete server?", f"Delete “{s.label}” from your vault?") == QMessageBox.Yes:
            self.store.delete(sid)
            self.refresh_all()

    def _move_server(self, sid: str, group: str) -> None:
        s = self.store.servers.get(sid)
        if s and s.group != group:
            s.group = group
            self.store.upsert(s)
            self.refresh_all()
            self.statusBar().showMessage(f"Moved {s.label} to {group or 'top level'}", 3000)

    def new_group(self, parent: str = "") -> None:
        name, ok = QInputDialog.getText(self, "New group", "Group name:" + (f"  (inside {parent})" if parent else ""))
        if ok and name.strip():
            path = f"{parent}/{name.strip()}" if parent else name.strip()
            self.store.groups.add(path.strip("/"))
            self.store.save()
            self.refresh_all()

    def rename_group(self, path: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename group", "New name:", text=path.rsplit("/", 1)[-1])
        if ok and name.strip():
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            new = f"{parent}/{name.strip()}" if parent else name.strip()
            self.tree.collapsed.discard(path)
            self.store.rename_group(path, new)
            self.refresh_all()

    def delete_group(self, path: str) -> None:
        if QMessageBox.question(self, "Delete group?",
                                f"Delete group “{path}”? Its servers move to the parent group.") == QMessageBox.Yes:
            self.store.delete_group(path)
            self.refresh_all()

    def connect_group(self, path: str) -> None:
        servers = sorted([s for s in self.store.servers.values()
                          if s.group == path or s.group.startswith(path + "/")], key=lambda s: s.label.lower())
        if not servers:
            return
        if len(servers) > 8:
            if QMessageBox.question(self, "Open many sessions?",
                                    f"Open {len(servers)} sessions? (first 8 are tiled in one tab)") != QMessageBox.Yes:
                return
        first, rest = servers[0], servers[1:8]
        self._open(first)
        for i, s in enumerate(rest):
            self._open(s, "right" if i % 2 == 0 else "down")
        for s in servers[8:]:
            self._open(s)

    def _tree_menu(self, pos) -> None:
        it = self.tree.itemAt(pos)
        m = QMenu(self)
        kind = it.data(0, Qt.UserRole) if it else None
        key = it.data(0, Qt.UserRole + 1) if it else None
        if kind == "server":
            s = self.store.servers[key]
            m.addAction(icon("terminal"), "Connect", lambda: self.connect_server(key))
            has_tab = self.current_tab() is not None
            a1 = m.addAction(icon("split-h"), "Connect in split right", lambda: self.connect_server(key, "right"))
            a2 = m.addAction(icon("split-v"), "Connect in split down", lambda: self.connect_server(key, "down"))
            a1.setEnabled(has_tab)
            a2.setEnabled(has_tab)
            m.addSeparator()
            if s.uses_ssh:
                m.addAction(icon("gauge"), "Dashboard", lambda: self.dashboard_for_server(key))
            m.addAction(icon("edit"), "Edit…", lambda: self.edit_server(key))
            m.addAction(icon("copy"), "Duplicate", lambda: self.duplicate_server(key))
            m.addAction(icon("star", C["warn"]), "Unpin from favorites" if s.favorite else "Pin to favorites",
                        lambda: self._toggle_fav(key))
            mv = m.addMenu(icon("folder"), "Move to group")
            mv.addAction("(top level)", lambda: self._move_server(key, ""))
            for g in self.store.all_groups():
                mv.addAction(g, lambda g=g: self._move_server(key, g))
            mv.addSeparator()
            mv.addAction(icon("folder-plus"), "New group…", lambda: self._move_to_new_group(key))
            m.addSeparator()
            m.addAction(icon("copy"), "Copy aws command" if s.connection == "ssm-shell" else "Copy ssh command",
                        lambda: QGuiApplication.clipboard().setText(s.ssh_command()))
            if s.is_ssm:
                m.addAction(icon("lock"), "AWS SSO sign-in", lambda: self.aws_sign_in(s.aws_profile))
            m.addAction(icon("copy"), "Copy host", lambda: QGuiApplication.clipboard().setText(s.host))
            m.addSeparator()
            m.addAction(icon("trash", C["danger"]), "Delete", lambda: self.delete_server(key))
        elif kind == "group":
            m.addAction(icon("terminal"), "Connect to all (tiled)", lambda: self.connect_group(key))
            m.addSeparator()
            m.addAction(icon("plus"), "New server here…", lambda: self.new_server(key))
            m.addAction(icon("folder-plus"), "New subgroup…", lambda: self.new_group(key))
            m.addAction(icon("edit"), "Rename…", lambda: self.rename_group(key))
            cm = m.addMenu(icon("folder", self.store.group_colors.get(key) or C["muted"]), "Color")
            from .models import COLORS
            names = {"": "No color", "#ff6b6b": "Red (production)", "#ffa94d": "Orange", "#ffd43b": "Yellow",
                     "#69db7c": "Green", "#38d9a9": "Teal", "#4dabf7": "Blue", "#748ffc": "Indigo",
                     "#b197fc": "Violet", "#f783ac": "Pink"}
            for col in COLORS:
                a = cm.addAction(dot_icon(col) if col else icon("x"), names.get(col, col),
                                 lambda c=col: self._set_group_color(key, c))
                a.setCheckable(True)
                a.setChecked(self.store.group_colors.get(key, "") == col)
            m.addSeparator()
            m.addAction(icon("trash", C["danger"]), "Delete group", lambda: self.delete_group(key))
        else:
            m.addAction(icon("plus"), "New server…", self.new_server)
            m.addAction(icon("folder-plus"), "New group…", lambda: self.new_group(""))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _set_group_color(self, path: str, color: str) -> None:
        self.store.set_group_color(path, color)
        self.refresh_all()

    def _move_to_new_group(self, sid: str) -> None:
        name, ok = QInputDialog.getText(self, "New group", "Group name (use / to nest):")
        if ok and name.strip():
            self._move_server(sid, name.strip().strip("/"))

    def _toggle_fav(self, sid: str) -> None:
        s = self.store.servers[sid]
        s.favorite = not s.favorite
        self.store.upsert(s)
        self.refresh_all()

    def _search_enter(self) -> None:
        sid = self.tree.selected_server_id()
        if not sid:
            # connect to the first match
            matches = [s for s in self.store.servers.values() if s.matches(self.search.text())]
            if len(matches) >= 1:
                sid = sorted(matches, key=lambda s: -s.last_connected)[0].id
        if sid:
            self.connect_server(sid)
        elif self.search.text().strip():
            self.quick_connect(self.search.text())

    # ================================================================ imports
    def _import(self, servers: list[Server], source: str) -> None:
        if not servers:
            QMessageBox.information(self, "Import", f"No sessions found in {source}.")
            return
        added = self.store.import_servers(servers)
        self.refresh_all()
        QMessageBox.information(self, "Import",
                                f"Imported {added} new server(s) from {source}"
                                f" ({len(servers) - added} already existed).")

    def import_putty(self) -> None:
        import sys
        if sys.platform != "win32":
            QMessageBox.information(self, "Import", "PuTTY sessions live in the Windows registry, so this "
                                    "import only works on Windows. Use Import ~/.ssh/config instead.")
            return
        self._import(importers.putty_sessions(), "PuTTY")

    def import_aws(self) -> None:
        from .aws_ui import AwsImportDialog
        dlg = AwsImportDialog(self.store, self)
        dlg.exec()
        if dlg.added or dlg.updated:
            self.refresh_all()

    def aws_sign_in(self, profile: str | None = None) -> None:
        from . import aws
        from .aws_ui import ensure_login
        if profile is None:
            profs = [p for p in aws.profiles() if aws.is_sso_profile(p)]
            if not aws.cli():
                QMessageBox.information(self, "AWS sign-in", "The AWS CLI v2 isn't installed. Get it from "
                                        f"{aws.CLI_INSTALL_URL}")
                return
            if not profs:
                QMessageBox.information(self, "AWS sign-in", "No AWS profiles use SSO yet. Set one up "
                                        "with `aws configure sso` in a terminal.")
                return
            if len(profs) == 1:
                profile = profs[0]
            else:
                profile, ok = QInputDialog.getItem(self, "AWS sign-in", "Profile:", profs, 0, False)
                if not ok:
                    return
        ensure_login(self, "" if profile == "default" else profile,
                     lambda: self.statusBar().showMessage(f"Signed in to AWS ({profile or 'default'})", 6000),
                     ask=False)

    def import_ssh_config(self) -> None:
        try:
            self._import(importers.ssh_config_servers(), "~/.ssh/config")
        except Exception as e:
            QMessageBox.warning(self, "Import", f"Could not read ~/.ssh/config:\n{e}")

    # ================================================================ snippets / settings / lock
    def _fill_snippets(self) -> None:
        self.snip_menu.clear()
        for sn in self.store.snippets:
            a = self.snip_menu.addAction(icon("code"), sn.name, lambda c=sn.command: self.send_snippet(c))
            a.setToolTip(sn.command)
        if self.store.snippets:
            self.snip_menu.addSeparator()
        self.snip_menu.addAction(icon("edit"), "Manage snippets…", self.manage_snippets)

    def send_snippet(self, cmd: str) -> None:
        t = self.current_tab()
        if not t or not t.active:
            self.statusBar().showMessage("Open a session first", 2500)
            return
        targets = t.panes() if t.broadcast else [t.active]
        for p in targets:
            p.send_text(cmd)
        t.active.focus_terminal()

    def manage_snippets(self) -> None:
        SnippetsDialog(self.store, self).exec()

    # ================================================================ updates
    def check_updates(self, manual: bool = False) -> None:
        import threading
        from . import updater
        if updater.update_check_policy() is False:
            if manual:
                QMessageBox.information(self, "Updates", "Update checks are turned off by your administrator. "
                                        "To update, use Help → Install update from file.")
            return
        if not manual:
            if not updater.updates_allowed(self.settings):
                return
            if time.time() - float(self.settings.get("last_update_check", 0)) < 20 * 3600:
                return
        if manual:
            self.statusBar().showMessage("Checking for updates …", 3000)

        def run():
            try:
                rel = updater.check()
                self._upd.checked.emit((manual, rel, ""))
            except updater.UpdateError as e:
                self._upd.checked.emit((manual, None, str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _on_update_checked(self, payload) -> None:
        from . import __version__
        manual, rel, err = payload
        if not err:
            self.settings["last_update_check"] = time.time()
            self.settings.save()
        if err:
            if manual:
                QMessageBox.warning(self, "Updates", err)
            return
        if not rel:
            if manual:
                QMessageBox.information(self, "Updates", f"You're up to date (BlamixShell {__version__}).")
            return
        self._pending_release = rel
        self.update_btn.setText(f"⬆  Update {rel.version}")
        self.update_btn.show()
        if manual or rel.version != self.settings.get("skip_version"):
            if manual:
                self._show_update(rel)
            else:
                self.statusBar().showMessage(f"BlamixShell {rel.version} is available: click the button on the right.", 8000)
        else:
            self.update_btn.hide()   # user skipped this version

    def _show_update(self, rel) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from . import __version__, updater
        if not rel:
            return
        asset = updater.pick_asset(rel)
        dlg = UpdateDialog(rel, __version__, can_install=asset is not None, parent=self)
        dlg.exec()
        if dlg.choice == "skip":
            self.settings["skip_version"] = rel.version
            self.settings.save()
            self.update_btn.hide()
        elif dlg.choice == "page":
            QDesktopServices.openUrl(QUrl(rel.page))
        elif dlg.choice == "install" and asset:
            live = [p for p in self.all_panes() if p.state == "connected"]
            if live and QMessageBox.question(
                    self, "Install update?",
                    f"BlamixShell will close to install the update, disconnecting {len(live)} session(s). Continue?"
            ) != QMessageBox.Yes:
                return
            self._download_update(asset)

    def _download_update(self, asset) -> None:
        import tempfile
        import threading
        from pathlib import Path
        from . import updater
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Downloading update …")

        def run():
            try:
                path = updater.download(asset, Path(tempfile.gettempdir()) / "blamixshell-update",
                                        lambda d, t: self._upd.progress.emit(d, t))
                self._upd.downloaded.emit((str(path), ""))
            except updater.UpdateError as e:
                self._upd.downloaded.emit(("", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _on_update_progress(self, done: int, total: int) -> None:
        if total:
            self.update_btn.setText(f"Downloading update … {done * 100 // total}%")

    def _on_update_downloaded(self, payload) -> None:
        from pathlib import Path
        from . import updater
        path, err = payload
        self.update_btn.setEnabled(True)
        if err:
            self.update_btn.setText(f"⬆  Update {self._pending_release.version}")
            QMessageBox.warning(self, "Update failed", err)
            return
        try:
            updater.apply_update(Path(path))
        except updater.UpdateError as e:
            QMessageBox.warning(self, "Update failed", str(e))
            return
        self._force_quit = True
        self.close()          # saves settings and the session layout (closeEvent)
        # The update script waits for this process to end. Don't rely on a clean Qt
        # shutdown (an open dialog or a hung web engine can keep the process alive).
        os._exit(0)

    def update_from_file(self) -> None:
        """Offline update: install an MSI / portable zip that was copied to this computer."""
        from . import __version__, updater
        kind = updater.install_kind()
        if kind not in ("msi", "portable"):
            QMessageBox.information(self, "Install update from file",
                                    updater.check_update_file(Path("x"), kind))
            return
        filt = "BlamixShell installer (*.msi)" if kind == "msi" else "BlamixShell portable (*.zip)"
        path, _ = QFileDialog.getOpenFileName(self, "Install update from file", str(Path.home() / "Downloads"), filt)
        if not path:
            return
        problem = updater.check_update_file(Path(path), kind)
        if problem:
            QMessageBox.warning(self, "Install update from file", problem)
            return
        digest = updater.sha256_of(Path(path))
        ver = updater.file_version(Path(path))
        if ver and not updater.is_newer(ver, __version__) and QMessageBox.question(
                self, "Install update from file",
                f"{Path(path).name} is version {ver}; you have {__version__}. Install it anyway?") != QMessageBox.Yes:
            return
        expected, ok = QInputDialog.getText(
            self, "Check the file",
            f"<b>{Path(path).name}</b>{f' (version {ver})' if ver else ''}<br><br>"
            f"SHA-256: <code>{digest}</code><br><br>"
            "Compare it with the checksum on the release page (each download lists its sha256). "
            "Paste the expected value to check automatically, or leave empty:")
        if not ok:
            return
        problem = updater.check_update_file(Path(path), kind, expected)
        if problem:
            QMessageBox.warning(self, "Install update from file", problem)
            return
        if QMessageBox.question(self, "Install update from file",
                                "BlamixShell closes, installs the update and starts again. Continue?") != QMessageBox.Yes:
            return
        try:
            updater.apply_update(Path(path), kind)
        except updater.UpdateError as e:
            QMessageBox.warning(self, "Install update from file", str(e))
            return
        self._force_quit = True
        self.close()
        os._exit(0)

    def _help_menu(self, parent) -> QMenu:
        from . import links
        from .dialogs import open_url
        m = QMenu(parent)
        m.addAction(icon("terminal"), "About BlamixShell", self.show_about)
        m.addAction(icon("code"), "Keyboard shortcuts", lambda: open_url(links.SHORTCUTS_URL))
        m.addAction(icon("link"), "Documentation", lambda: open_url(links.DOCS_URL))
        m.addAction(icon("refresh"), "Check for updates", lambda: self.check_updates(manual=True))
        m.addAction(icon("import"), "Install update from file…", self.update_from_file)
        m.addAction(icon("edit"), "Report a problem", lambda: open_url(links.ISSUES_URL))
        m.addSeparator()
        m.addAction(icon("coffee", C["warn"]), "Buy me a coffee", lambda: open_url(links.KOFI_URL))
        m.addAction(icon("link"), "Made by Blamixology", lambda: open_url(links.COMPANY_URL))
        return m

    def show_about(self) -> None:
        from .dialogs import AboutDialog
        AboutDialog(self).exec()

    # ================================================================ dashboard
    def open_dashboard(self, pane=None) -> None:
        """Dashboard for a terminal pane (default: the active one), reusing its connection."""
        pane = pane or self.active_pane()
        if pane is None:
            self.statusBar().showMessage("Connect to a server first (or right-click a server → Dashboard).", 5000)
            return
        if not pane.server.uses_ssh:
            self.statusBar().showMessage("The dashboard needs SSH: switch this server to “SSH over AWS SSM”.", 6000)
            return
        from .dashboard_ui import DashboardWindow
        wins = getattr(self, "_dashboards", {})
        self._dashboards = wins
        win = wins.get(id(pane))
        if win is None:
            color = self.store.color_for(self.store.servers.get(pane.server.id, pane.server))
            win = DashboardWindow(pane, color=color, send_to_terminal=lambda text, p=pane: self._type_into(p, text),
                                  parent=self)
            win.destroyed.connect(lambda _o=None, k=id(pane): wins.pop(k, None))
            wins[id(pane)] = win
        win.show()
        win.raise_()
        win.activateWindow()

    def _type_into(self, pane, text: str) -> None:
        pane.send_text(text)
        for i in range(self.tabs.count()):
            if pane in self.tabs.widget(i).panes():
                self.tabs.setCurrentIndex(i)
                self.tabs.widget(i).set_active(pane)
        self.raise_()
        self.activateWindow()
        pane.focus_terminal()

    def dashboard_for_server(self, sid: str) -> None:
        """From the server list: use an open connection, or connect in a new tab first."""
        for p in self.all_panes():
            if p.server.id == sid and p.state in ("connected", "connecting"):
                self.open_dashboard(p)
                return
        self.connect_server(sid)
        self.open_dashboard(self.active_pane())

    def check_vault_changes(self) -> None:
        from .vault import PasswordChangedElsewhere
        try:
            changed = self.store.reload_if_changed()
        except PasswordChangedElsewhere:
            self._vault_timer.stop()
            QMessageBox.information(
                self, "Vault changed",
                "Your vault was re-encrypted with a new master password on another computer.\n\n"
                "BlamixShell will restart so you can unlock it with the new password.")
            self.restart()
            return
        except Exception:
            return
        if changed:
            self.refresh_all()
            self.statusBar().showMessage("Servers updated from another computer", 6000)

    def restart(self) -> None:
        """Start a new BlamixShell and close this one (after switching vault files)."""
        import sys
        from PySide6.QtCore import QProcess
        if getattr(sys, "frozen", False):
            QProcess.startDetached(sys.executable, [])
        else:
            QProcess.startDetached(sys.executable, ["-m", "blamixshell.main"],
                                   str(Path(__file__).resolve().parent.parent))
        self._force_quit = True
        self.close()
        os._exit(0)

    def open_settings(self) -> None:
        if SettingsDialog(self.settings, self.store, self).exec() == QDialog.Accepted:
            for p in self.all_panes():
                p.apply_settings()
                if p.state == "connected":
                    p.apply_logging()
            self._prune_logs()
            self._health_timer.setInterval(self._health_ms())
            if not self.settings.get("health_strip", True):
                self.health_lbl.hide()

    def _prune_logs(self) -> None:
        days = int(self.settings.get("log_retention_days", 0) or 0)
        if days > 0:
            from .session_log import log_root, prune
            root = log_root(self.settings)
            threading.Thread(target=lambda: prune(root, days), daemon=True).start()

    def open_logs_folder(self) -> None:
        from .dialogs import open_url
        from .session_log import log_root
        root = log_root(self.settings)
        root.mkdir(parents=True, exist_ok=True)
        open_url(QUrl.fromLocalFile(str(root)).toString())

    def toggle_recording(self) -> None:
        pane = self.active_pane()
        if pane is None or (pane.state != "connected" and pane.recorder is None):
            self.statusBar().showMessage("Connect to a server first, then record.", 4000)
            return
        pane.toggle_recording()

    def lock(self) -> None:
        self.hide()
        path = self.store.vault.path

        def attempt(pw: str) -> str:
            try:
                Vault.open(path, pw)
                return ""
            except WrongPassword:
                return "Wrong master password."
            except Exception as e:
                return str(e)
        dlg = UnlockDialog(False, attempt)
        if dlg.exec() == QDialog.Accepted:
            self.show()
        else:
            self._force_quit = True
            self.close()

    def closeEvent(self, e):  # noqa: N802
        live = [p for p in self.all_panes() if p.state == "connected"]
        if live and not getattr(self, "_force_quit", False) and QMessageBox.question(
                self, "Quit BlamixShell?", f"Disconnect {len(live)} live session(s) and quit?") != QMessageBox.Yes:
            e.ignore()
            return
        self._layout_timer.stop()
        self.save_session()
        self.settings["window_geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        if self.side.isVisible():
            self.settings["sidebar_width"] = self.root_split.sizes()[0]
        self.settings.save()
        for p in self.all_panes():
            p.shutdown()
        self.sftp.shutdown()
        e.accept()
        QApplication.instance().quit()
