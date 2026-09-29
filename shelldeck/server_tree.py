"""Sidebar server tree with groups, favorites, tags and live-session markers."""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QStyle, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem

from .models import Store
from .theme import C, icon

KIND = Qt.UserRole
KEY = Qt.UserRole + 1


class _Delegate(QStyledItemDelegate):
    def __init__(self, tree: "ServerTree"):
        super().__init__(tree)
        self.tree = tree

    def sizeHint(self, option, index):  # noqa: N802
        kind = index.data(KIND)
        return QSize(option.rect.width(), 46 if kind == "server" else 30)

    def paint(self, p: QPainter, option, index):
        kind = index.data(KIND)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(2, 1, -4, -1)
        depth, parent = 0, index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        indent = depth * 14
        hovered = option.state & QStyle.State_MouseOver
        selected = option.state & QStyle.State_Selected
        if selected or hovered:
            # highlight the whole row, including the indentation area on the left
            full = QRect(2, r.top(), self.tree.viewport().width() - 6, r.height())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(C["surface2"] if selected else C["hover"]))
            p.drawRoundedRect(full, 8, 8)
        base = QFont(option.font)

        if kind == "server":
            s = self.tree.store.servers.get(index.data(KEY))
            if not s:
                p.restore()
                return
            # color bar
            if s.color:
                p.setBrush(QColor(s.color))
                p.drawRoundedRect(QRect(r.left() + 6 + indent, r.top() + 9, 3, r.height() - 18), 1.5, 1.5)
            x = r.left() + 16 + indent
            ic = icon("server", s.color or C["faint"], 18)
            ic.paint(p, QRect(x, r.top() + (r.height() - 18) // 2, 18, 18))
            x += 28
            # live dot
            if s.id in self.tree.live:
                p.setPen(QPen(QColor(C["sidebar"]), 2))
                p.setBrush(QColor(C["ok"]))
                p.drawEllipse(QRect(x - 14, r.top() + r.height() // 2 + 2, 9, 9))
            # tags on the right
            tf = QFont(base)
            tf.setPointSizeF(base.pointSizeF() * 0.78)
            fm_t = QFontMetrics(tf)
            right = r.right() - 8
            p.setFont(tf)
            for tag in reversed(s.tags[:2]):
                w = fm_t.horizontalAdvance(tag) + 12
                pill = QRect(right - w, r.top() + (r.height() - 18) // 2, w, 18)
                if pill.left() < x + 90:
                    break
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(C["surface2"] if not selected else C["hover"]))
                p.drawRoundedRect(pill, 9, 9)
                p.setPen(QColor(C["muted"]))
                p.drawText(pill, Qt.AlignCenter, tag)
                right = pill.left() - 4
            if s.favorite and index.parent().data(KIND) != "favs":
                star = icon("star", C["warn"], 12)
                star.paint(p, QRect(right - 14, r.top() + (r.height() - 12) // 2, 12, 12))
                right -= 18
            avail = right - x - 6
            nf = QFont(base)
            nf.setWeight(QFont.DemiBold)
            p.setFont(nf)
            p.setPen(QColor(C["text"]))
            name = QFontMetrics(nf).elidedText(s.label, Qt.ElideRight, avail)
            p.drawText(QRect(x, r.top() + 5, avail, 20), Qt.AlignLeft | Qt.AlignVCenter, name)
            sf = QFont(base)
            sf.setPointSizeF(base.pointSizeF() * 0.85)
            p.setFont(sf)
            p.setPen(QColor(C["faint"]))
            sub = s.address + ("  ⇢ via " + self.tree.store.servers[s.jump_id].label
                               if s.jump_id in self.tree.store.servers else "")
            p.drawText(QRect(x, r.top() + 23, avail, 18), Qt.AlignLeft | Qt.AlignVCenter,
                       QFontMetrics(sf).elidedText(sub, Qt.ElideRight, avail))
        else:
            expanded = self.tree.isExpanded(index)
            x = r.left() + 4 + indent
            chev = "▾" if expanded else "▸"
            p.setPen(QColor(C["faint"]))
            p.drawText(QRect(x, r.top(), 12, r.height()), Qt.AlignCenter, chev)
            x += 14
            if kind == "favs":
                ic = icon("star", C["warn"], 15)
            else:
                ic = icon("folder-open" if expanded else "folder", C["muted"], 15)
            ic.paint(p, QRect(x, r.top() + (r.height() - 15) // 2, 15, 15))
            x += 22
            gf = QFont(base)
            gf.setWeight(QFont.DemiBold)
            gf.setPointSizeF(base.pointSizeF() * 0.92)
            p.setFont(gf)
            p.setPen(QColor(C["muted"]))
            name = index.data(Qt.DisplayRole)
            p.drawText(QRect(x, r.top(), r.width() - x - 40, r.height()), Qt.AlignLeft | Qt.AlignVCenter, name)
            cnt = index.data(Qt.UserRole + 2)
            if cnt:
                cf = QFont(base)
                cf.setPointSizeF(base.pointSizeF() * 0.8)
                p.setFont(cf)
                p.setPen(QColor(C["faint"]))
                p.drawText(QRect(r.right() - 40, r.top(), 32, r.height()), Qt.AlignRight | Qt.AlignVCenter, str(cnt))
        p.restore()


class ServerTree(QTreeWidget):
    connect_requested = Signal(str)
    server_moved = Signal(str, str)     # server id, new group

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setObjectName("ServerTree")
        self.live: set[str] = set()
        self.collapsed: set[str] = set()
        self.setHeaderHidden(True)
        self.setIndentation(0)   # nesting is drawn by the delegate (no Qt branch area)
        self.setRootIsDecorated(False)
        self.setItemDelegate(_Delegate(self))
        self.setMouseTracking(True)
        self.setUniformRowHeights(False)
        self.setAnimated(True)
        self.setExpandsOnDoubleClick(False)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.itemDoubleClicked.connect(self._double)
        self.itemClicked.connect(self._click)
        self.itemExpanded.connect(lambda it: self.collapsed.discard(it.data(0, KEY)))
        self.itemCollapsed.connect(lambda it: self.collapsed.add(it.data(0, KEY)))
        self._query = ""

    def drawBranches(self, painter, rect, index):  # noqa: N802
        # rows draw their own chevrons; the default branch area would paint the
        # selection in the accent color as a separate block on the left
        pass

    def _click(self, item: QTreeWidgetItem, _col: int) -> None:
        if item.data(0, KIND) in ("group", "favs"):
            item.setExpanded(not item.isExpanded())

    def _double(self, item: QTreeWidgetItem, _col: int) -> None:
        if item.data(0, KIND) == "server":
            self.connect_requested.emit(item.data(0, KEY))

    def keyPressEvent(self, e):  # noqa: N802
        it = self.currentItem()
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and it and it.data(0, KIND) == "server":
            self.connect_requested.emit(it.data(0, KEY))
            return
        super().keyPressEvent(e)

    def rebuild(self, query: str | None = None) -> None:
        if query is not None:
            self._query = query
        q = self._query
        sel = self.currentItem().data(0, KEY) if self.currentItem() else None
        self.clear()
        servers = [s for s in self.store.servers.values() if s.matches(q)]
        servers.sort(key=lambda s: s.label.lower())

        def server_item(parent, s):
            it = QTreeWidgetItem(parent, [s.label])
            it.setData(0, KIND, "server")
            it.setData(0, KEY, s.id)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
            it.setToolTip(0, f"{s.address}\n{s.notes}".strip())
            return it

        favs = [s for s in servers if s.favorite]
        if favs:
            fi = QTreeWidgetItem(self, ["Favorites"])
            fi.setData(0, KIND, "favs")
            fi.setData(0, KEY, "__favs__")
            fi.setData(0, Qt.UserRole + 2, len(favs))
            fi.setFlags(Qt.ItemIsEnabled)
            for s in favs:
                server_item(fi, s)
            fi.setExpanded("__favs__" not in self.collapsed or bool(q))

        groups: dict[str, QTreeWidgetItem] = {}

        def group_item(path: str) -> QTreeWidgetItem | None:
            if not path:
                return None
            if path in groups:
                return groups[path]
            parent_path, _, name = path.rpartition("/")
            parent = group_item(parent_path) if parent_path else None
            it = QTreeWidgetItem(parent or self, [name])
            it.setData(0, KIND, "group")
            it.setData(0, KEY, path)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsDropEnabled)
            groups[path] = it
            return it

        if not q:
            for g in self.store.all_groups():
                group_item(g)
        for s in servers:
            server_item(group_item(s.group) or self, s)
        for path, it in groups.items():
            n = sum(1 for s in servers if s.group == path or s.group.startswith(path + "/"))
            it.setData(0, Qt.UserRole + 2, n)
            it.setExpanded(path not in self.collapsed or bool(q))
        # order: favorites, groups (alpha), then ungrouped servers
        self._reorder_top()
        if sel:
            for it in self.findItems("*", Qt.MatchWildcard | Qt.MatchRecursive):
                if it.data(0, KIND) == "server" and it.data(0, KEY) == sel:
                    self.setCurrentItem(it)
                    break

    def _reorder_top(self) -> None:
        items = [self.takeTopLevelItem(0) for _ in range(self.topLevelItemCount())]
        rank = {"favs": 0, "group": 1, "server": 2}
        items.sort(key=lambda it: (rank.get(it.data(0, KIND), 3), it.text(0).lower()))
        for it in items:
            self.addTopLevelItem(it)
            self._restore_expansion(it)

    def _restore_expansion(self, it: QTreeWidgetItem) -> None:
        if it.data(0, KIND) in ("group", "favs"):
            it.setExpanded(it.data(0, KEY) not in self.collapsed or bool(self._query))
            for i in range(it.childCount()):
                self._restore_expansion(it.child(i))

    def dropEvent(self, e):  # noqa: N802
        src = self.currentItem()
        if not src or src.data(0, KIND) != "server":
            e.ignore()
            return
        target = self.itemAt(e.position().toPoint())
        if target is None:
            group = ""
        elif target.data(0, KIND) == "group":
            group = target.data(0, KEY)
        elif target.data(0, KIND) == "server":
            s = self.store.servers.get(target.data(0, KEY))
            group = s.group if s else ""
        else:
            e.ignore()
            return
        e.setDropAction(Qt.IgnoreAction)
        e.accept()
        self.server_moved.emit(src.data(0, KEY), group)

    def selected_server_id(self) -> str | None:
        it = self.currentItem()
        return it.data(0, KEY) if it and it.data(0, KIND) == "server" else None
