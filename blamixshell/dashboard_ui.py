"""Server dashboard window: overview, services, processes, logs, ports, updates, users.

Bound to a terminal pane and reuses its SSH connection (extra exec channels, like
`ssh host cmd`). Everything runs on background threads; the UI only renders results.
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from . import dashboard as d
from .theme import C, blend, icon, style_window

REFRESH_MS = 5000
TAB_KEYS = ["overview", "services", "processes", "logs", "ports", "updates", "users"]


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


def _table(headers: list[str], stretch: int = -1) -> QTableWidget:
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
    return t


def _item(text, color: str | None = None, align_right: bool = False, data=None) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
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


class DashboardWindow(QWidget):
    def __init__(self, pane, color: str = "", send_to_terminal=None, parent=None):
        super().__init__(parent, Qt.Window)
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
        self._timer.start(REFRESH_MS)
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
        self.auto.setToolTip("Refresh the open tab every 5 seconds (Overview, Processes, Logs in follow mode)")
        head.addWidget(self.updated_lbl)
        head.addSpacing(8)
        head.addWidget(self.auto)
        head.addWidget(_btn("refresh", "Refresh", lambda: self.refresh(force=True)))
        root.addLayout(head)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(f"background:{blend(C['surface'], C['warn'], 0.18)}; border-radius:8px; padding:8px 12px;")
        self.banner.hide()
        root.addWidget(self.banner)

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

        self.status = QLabel("", objectName="Hint")
        root.addWidget(self.status)

    def _page(self, title: str) -> QVBoxLayout:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(2, 12, 2, 2)
        lay.setSpacing(10)
        self.tabs.addTab(w, title)
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
        acts = [_btn("refresh", "", lambda: self.refresh(force=True), "Reload the list")]
        self.svc_buttons: dict[str, QPushButton] = {}
        for a in ("start", "stop", "restart", "enable", "disable"):
            b = _btn({"start": "bolt", "stop": "x", "restart": "refresh", "enable": "plus",
                      "disable": "x"}[a], a.capitalize(), lambda _=False, a=a: self._service_action(a),
                     {"enable": "Start at boot", "disable": "Don't start at boot"}.get(a, ""))
            self.svc_buttons[a] = b
            acts.append(b)
        lay.addLayout(_toolbar(self.svc_filter, self.svc_failed, "stretch", *acts,
                               _btn("file", "Status", self._service_status),
                               _btn("terminal", "Logs", self._service_logs)))
        self.svc_table = _table(["Service", "State", "Startup", "Description"])
        self.svc_table.doubleClicked.connect(lambda _i: self._service_status())
        self.svc_table.itemSelectionChanged.connect(self._update_service_buttons)
        lay.addWidget(self.svc_table, 1)
        self.svc_msg = QLabel("", objectName="Hint", wordWrap=True)
        lay.addWidget(self.svc_msg)

    def _build_processes(self) -> None:
        lay = self._page("Processes")
        self.ps_sort = QComboBox()
        self.ps_sort.addItems(["Sort by CPU", "Sort by memory"])
        self.ps_sort.currentIndexChanged.connect(lambda _i: self.refresh(force=True))
        self.ps_filter = QLineEdit(placeholderText="Filter by command, user or PID…")
        self.ps_filter.setMinimumWidth(260)
        self.ps_filter.textChanged.connect(lambda _t: self._fill_processes())
        self._procs: list = []
        lay.addLayout(_toolbar(self.ps_sort, self.ps_filter, "stretch",
                               _btn("x", "End process…", lambda: self._kill(False)),
                               _btn("x", "Force kill…", lambda: self._kill(True))))
        self.ps_table = _table(["PID", "User", "CPU %", "Mem %", "Memory", "Running for", "Command"])
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
        lay.addLayout(_toolbar(self.log_unit, self.log_prio, self.log_lines, _btn("download", "Load",
                               lambda: self.refresh(force=True)), self.log_follow, "stretch"))
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSizeF(max(8.5, mono.pointSizeF() * 0.95))
        self.log_view.setFont(mono)
        lay.addWidget(self.log_view, 1)

    def _build_ports(self) -> None:
        lay = self._page("Ports")
        lay.addWidget(QLabel("Ports this server listens on. Process names need root (or sudo) to be visible.",
                             objectName="Hint"))
        self.port_table = _table(["Protocol", "Address", "Port", "Process"])
        lay.addWidget(self.port_table, 1)

    def _build_updates(self) -> None:
        lay = self._page("Updates")
        self.upd_lbl = QLabel("")
        self.upd_btn = _btn("terminal", "Type the upgrade command in the terminal", self._type_upgrade,
                            "Types it into this server's terminal without pressing Enter, so you can review it")
        self.upd_btn.setEnabled(False)
        lay.addLayout(_toolbar(self.upd_lbl, "stretch", self.upd_btn))
        self.upd_table = _table(["Package", "New version"])
        lay.addWidget(self.upd_table, 1)
        lay.addWidget(QLabel("Read-only: this uses the server's cached package lists and never installs anything.",
                             objectName="Hint"))

    def _build_users(self) -> None:
        lay = self._page("Users")
        self.usr_table = _table(["Account", "UID", "Logged in", "Shell", "Home"])
        lay.addWidget(self.usr_table, 1)
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
            self.banner.hide()
            self.refresh(force=True)
        elif st == "connecting":
            self._banner("Connecting…")
        else:
            self._banner("Not connected: reconnect the terminal (press R in it) to update the dashboard.")

    def _banner(self, text: str) -> None:
        self.banner.setText(text)
        self.banner.show()

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
            self._job("processes", lambda r: d.processes(r, sort, limit=200))
        elif tab == "logs":
            unit = self.log_unit.currentText().strip()
            prio = d.PRIORITIES[self.log_prio.currentText()]
            lines = int(self.log_lines.currentText().split()[0])
            init = next((s.init for s in self._services if s.unit == unit), "")
            self._job("logs", lambda r: d.logs(r, unit, prio, lines, init))
        elif tab == "ports":
            self._job("ports", d.ports)
        elif tab == "updates":
            self.upd_lbl.setText("Checking for updates…")
            self._job("updates", d.updates)
        elif tab == "users":
            self._job("users", d.users)

    def _on_done(self, key: str, res, err: str) -> None:
        self._busy.discard(key)
        if self._closed:
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
        rows = [s for s in self._services if (not q or q in s.unit.lower() or q in s.description.lower())
                and (not self.svc_failed.isChecked() or s.failed)]
        rows.sort(key=lambda s: (not s.failed, s.unit))
        mixed = len({s.init for s in self._services}) > 1
        t = self.svc_table
        t.setRowCount(len(rows))
        for i, s in enumerate(rows):
            col = C["danger"] if s.failed else C["ok"] if s.active == "active" else C["muted"]
            label = f"{s.unit}  [supervisor]" if mixed and s.init == "supervisor" else s.unit
            t.setItem(i, 0, _item(label, data=(s.init, s.unit)))
            t.setItem(i, 1, _item(f"● {s.active} ({s.sub})", col))
            t.setItem(i, 2, _item(s.enabled or "–", C["muted"]))
            t.setItem(i, 3, _item(s.description, C["muted"]))
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
        self._fill_processes()

    def _fill_processes(self) -> None:
        q = self.ps_filter.text().strip().lower()
        procs = [p for p in self._procs if not q or q in p.command.lower() or q == p.user.lower()
                 or q == str(p.pid)]
        keep = self._selected(self.ps_table)
        t = self.ps_table
        t.setRowCount(len(procs))
        for i, p in enumerate(procs):
            t.setItem(i, 0, _item(p.pid, align_right=True, data=p.pid))
            t.setItem(i, 1, _item(p.user, C["muted"]))
            t.setItem(i, 2, _item(f"{p.cpu:.1f}", C["warn"] if p.cpu >= 50 else None, True))
            t.setItem(i, 3, _item(f"{p.mem:.1f}", None, True))
            t.setItem(i, 4, _item(d.human_kb(p.rss_kb), C["muted"], True))
            t.setItem(i, 5, _item(p.elapsed, C["muted"], True))
            t.setItem(i, 6, _item(p.command))
            if p.pid == keep:
                t.selectRow(i)

    def _show_logs(self, text: str) -> None:
        bar = self.log_view.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.log_view.setPlainText(text.rstrip() or "(no log lines)")
        if at_end or self.log_follow.isChecked() or "logs" not in self._loaded:
            bar.setValue(bar.maximum())

    def _show_ports(self, ports: list) -> None:
        t = self.port_table
        t.setRowCount(len(ports))
        for i, p in enumerate(ports):
            public = p.address in ("*", "0.0.0.0", "::")
            t.setItem(i, 0, _item(p.proto.upper(), C["muted"]))
            t.setItem(i, 1, _item("all interfaces" if public else p.address, C["warn"] if public else None))
            t.setItem(i, 2, _item(p.port, None, True))
            t.setItem(i, 3, _item(p.process or "–", C["muted"]))

    def _show_updates(self, res) -> None:
        mgr, ups = res
        self._update_mgr = mgr
        self.upd_btn.setEnabled(bool(mgr and ups and self.send_to_terminal))
        if not mgr:
            self.upd_lbl.setText("No supported package manager found (apt, dnf, yum, zypper, pacman, apk).")
        elif not ups:
            self.upd_lbl.setText(f"<span style='color:{C['ok']}'>● Up to date</span> ({mgr})")
        else:
            self.upd_lbl.setText(f"<b>{len(ups)}</b> update{'s' if len(ups) != 1 else ''} available ({mgr})")
        t = self.upd_table
        t.setRowCount(len(ups))
        for i, u in enumerate(ups):
            t.setItem(i, 0, _item(u.package))
            t.setItem(i, 1, _item(u.version or "–", C["muted"]))
        self.tabs.setTabText(5, f"Updates ({len(ups)})" if ups else "Updates")

    def _show_users(self, res) -> None:
        accounts, sessions = res
        t = self.usr_table
        t.setRowCount(len(accounts))
        for i, a in enumerate(accounts):
            t.setItem(i, 0, _item(a.name, C["warn"] if a.uid == 0 else None))
            t.setItem(i, 1, _item(a.uid, C["muted"], True))
            t.setItem(i, 2, _item(a.logged_in or "–", C["ok"] if a.logged_in else C["muted"], True))
            t.setItem(i, 3, _item(a.shell, C["muted"]))
            t.setItem(i, 4, _item(a.home, C["muted"]))
        self.who_view.setPlainText("\n".join(sessions) or "Nobody is logged in (besides non-interactive sessions).")

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

    def _privileged(self, what: str, command: str, then: str, allow_plain: bool = False) -> None:
        """Confirm, then run as root (directly, or via sudo when needed)."""
        runner = self._runner()
        if runner is None:
            return
        via = "" if self._root else "sudo "
        box = QMessageBox(QMessageBox.Warning if self.color else QMessageBox.Question,
                          "Confirm", f"<b>{what}</b> on <b>{self.server.label}</b>?", parent=self)
        box.setInformativeText(f"Runs:  {via}{command}")
        ok = box.addButton(what.split()[0], QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not ok:
            return

        def work(r: d.Runner):
            if allow_plain and not self._root:
                plain = r.run(command)            # own processes don't need sudo
                if plain.ok:
                    return plain
            if not self._root and self._sudo_pw is None and d.needs_password(r, self._root):
                return "need-password"
            return d.run_privileged(r, command, self._root, self._sudo_pw)
        self._pending_action = (what, command, then, allow_plain)
        self._job("action", work)

    def _show_action(self, res) -> None:
        what, command, then, allow_plain = self._pending_action
        if res == "need-password":
            pw, ok = QInputDialog.getText(self, "sudo password",
                                          f"Password for sudo on {self.server.label} "
                                          f"(used for this dashboard only, never saved):", QLineEdit.Password)
            if not ok:
                return
            self._sudo_pw = pw
            self._job("action", lambda r: d.run_privileged(r, command, self._root, self._sudo_pw))
            return
        if hasattr(self.pane, "log_command"):        # command log: the dashboard's actions too
            self.pane.log_command(command + ("" if res.ok else f"   # failed (exit {res.code})"), "dashboard")
        if res.ok:
            self.status.setText(f"✔ {what}: done")
        else:
            msg = (res.err or res.out).strip() or f"exit code {res.code}"
            if "incorrect password" in msg.lower() or "sorry, try again" in msg.lower():
                self._sudo_pw = None
                msg = "Wrong sudo password."
            QMessageBox.warning(self, what, msg[:1500])
        self._loaded.discard(then)
        if self._tab_key() == then:
            self.refresh(force=True)

    def _type_upgrade(self) -> None:
        cmd = d.UPGRADE_COMMANDS.get(self._update_mgr)
        if cmd and self.send_to_terminal:
            self.send_to_terminal(cmd)
            self.status.setText("Typed into the terminal: review it there and press Enter to run.")

    # ================================================================ window
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        style_window(self)

    def closeEvent(self, e):  # noqa: N802
        self._closed = True
        self._sudo_pw = None
        self._timer.stop()
        super().closeEvent(e)
