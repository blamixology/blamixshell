"""The alerts history window: what went wrong on which server, when, and for how long."""
from __future__ import annotations

import time

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from . import alerts as al
from .dialogs import _Base
from .theme import C


class AlertsDialog(_Base):
    def __init__(self, parent=None, server: str = "", log: al.AlertLog | None = None):
        super().__init__(parent)
        self.log = log or al.shared()
        self.setWindowTitle("Alerts history")
        self.resize(860, 480)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.addWidget(QLabel("Alerts history", objectName="H2"))
        root.addWidget(QLabel(
            "Recorded while a server is the active terminal: disk or memory 90 %, swap 60 %, load 1.5 per CPU, and "
            f"failed services. Kept on this computer for {al.KEEP_DAYS} days.", objectName="Hint", wordWrap=True))
        bar = QHBoxLayout()
        self.server = QComboBox()
        self.server.addItem("All servers", "")
        labels: dict[str, str] = {}
        for x in self.log.entries():
            labels.setdefault(x["server"], x.get("label") or x["server"])
        for sid, label in sorted(labels.items(), key=lambda kv: kv[1].lower()):
            self.server.addItem(label, sid)
        self.server.setCurrentIndex(max(0, self.server.findData(server)))
        self.server.currentIndexChanged.connect(lambda _i: self.fill())
        self.count = QLabel("", objectName="Hint")
        bar.addWidget(self.server)
        bar.addWidget(self.count)
        bar.addStretch(1)
        clear = QPushButton("Clear…")
        clear.clicked.connect(self._clear)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bar.addWidget(clear)
        bar.addWidget(close)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Started", "Server", "Alert", "For", "State"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        root.addWidget(self.table, 1)
        root.addLayout(bar)
        self.fill()

    def fill(self) -> None:
        items = self.log.entries(self.server.currentData() or "")
        now = self.log.clock()
        t = self.table
        t.setRowCount(len(items))
        for i, x in enumerate(items):
            state = self.log.state(x)
            end = x.get("end") if x.get("end") is not None else x.get("seen", x["start"])
            length = al.duration(end - x["start"])
            if state == "ongoing":
                length = al.duration(now - x["start"]) + " so far"
            elif x.get("end_unknown") or state == "last seen":
                length = "≥ " + length
            text = x["message"] + (f"  (worst: {x['worst']})" if x.get("worst") else "")
            shown = {"ongoing": "ongoing", "ended": f"ended {time.strftime('%H:%M', time.localtime(end))}",
                     "last seen": f"last seen {time.strftime('%H:%M', time.localtime(end))}"}[state]
            cells = [time.strftime("%Y-%m-%d %H:%M", time.localtime(x["start"])), x.get("label", ""), text, length,
                     shown]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if state == "ongoing":
                    it.setForeground(QColor(C["danger"]))
                elif c in (0, 3, 4):
                    it.setForeground(QColor(C["muted"]))
                t.setItem(i, c, it)
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        ongoing = sum(1 for x in items if self.log.state(x) == "ongoing")
        self.count.setText(f"{len(items)} alert{'s' if len(items) != 1 else ''}"
                           + (f", {ongoing} ongoing" if ongoing else "") if items else "No alerts recorded.")

    def _clear(self) -> None:
        sid = self.server.currentData() or ""
        what = f"the alerts of {self.server.currentText()}" if sid else "all recorded alerts"
        if QMessageBox.question(self, "Clear alerts", f"Delete {what}?") != QMessageBox.Yes:
            return
        self.log.clear(sid)
        self.fill()

