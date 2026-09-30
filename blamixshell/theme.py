"""Look & feel: color tokens, Qt stylesheet, SVG icons, Windows title-bar tweaks."""
from __future__ import annotations

import re
import sys

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

C = {
    "bg": "#0b0d12",
    "sidebar": "#10131a",
    "surface": "#151923",
    "surface2": "#1b2030",
    "hover": "#222839",
    "border": "#232838",
    "text": "#e6e9f2",
    "muted": "#8a92a8",
    "faint": "#5b6378",
    "accent": "#7c8cff",
    "accent2": "#a78bfa",
    "ok": "#3ddc97",
    "warn": "#ffc857",
    "danger": "#ff5d73",
}

UI_FONT = "Segoe UI Variable Text"

QSS = f"""
* {{ font-family: "{UI_FONT}", "Segoe UI", "Inter", sans-serif; font-size: 10pt; color: {C['text']}; }}
QMainWindow, QDialog {{ background: {C['bg']}; }}
QToolTip {{ background: {C['surface2']}; color: {C['text']}; border: 1px solid {C['border']};
           border-radius: 6px; padding: 5px 8px; }}

#Sidebar {{ background: {C['sidebar']}; border-right: 1px solid {C['border']}; }}
#Brand {{ font-size: 13pt; font-weight: 600; letter-spacing: 0.3px; }}
#BrandSub {{ color: {C['faint']}; font-size: 8.5pt; }}
#SectionLabel {{ color: {C['faint']}; font-size: 8pt; font-weight: 600; letter-spacing: 1px; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
  background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 8px;
  padding: 7px 10px; selection-background-color: {C['accent']};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus, QDoubleSpinBox:focus {{
  border: 1px solid {C['accent']};
}}
QLineEdit#Search {{ background: {C['surface']}; padding: 8px 12px; border-radius: 10px; }}
QComboBox {{ padding-right: 30px; }}
QComboBox:hover {{ border: 1px solid {C['faint']}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 28px; border: none;
  border-left: 1px solid {C['border']}; }}
QComboBox::down-arrow {{ image: url(__ASSETS__/chevron-down.svg); width: 12px; height: 12px; }}
QComboBox::down-arrow:on {{ top: 1px; }}
QComboBox QAbstractItemView {{ background: {C['surface2']}; border: 1px solid {C['border']};
  selection-background-color: {C['hover']}; outline: none; padding: 4px; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; }}

QPushButton {{ background: {C['surface2']}; border: 1px solid {C['border']}; border-radius: 8px;
  padding: 7px 14px; }}
QPushButton:hover {{ background: {C['hover']}; }}
QPushButton:pressed {{ background: {C['surface']}; }}
QPushButton:disabled {{ color: {C['faint']}; }}
QPushButton#Primary {{ background: {C['accent']}; border: 1px solid {C['accent']}; color: #0b0d12; font-weight: 600; }}
QPushButton#Primary:hover {{ background: #95a2ff; }}
QPushButton#Danger {{ background: transparent; border: 1px solid {C['danger']}; color: {C['danger']}; }}
QPushButton#Ghost, QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 6px; }}
QPushButton#Ghost:hover, QToolButton:hover {{ background: {C['hover']}; }}
QToolButton:checked {{ background: {C['surface2']}; border: 1px solid {C['border']}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

QTreeWidget, QListWidget, QTableWidget {{ background: transparent; border: none; outline: none; }}
QTreeWidget::item, QListWidget::item {{ border-radius: 8px; padding: 2px; }}
QTreeWidget::item:hover, QListWidget::item:hover {{ background: {C['hover']}; }}
QTreeWidget::item:selected, QListWidget::item:selected {{ background: {C['surface2']}; color: {C['text']}; }}
#Palette QListWidget::item:selected {{ background: {C['hover']}; color: {C['text']}; }}
QTreeView::branch {{ background: transparent; }}
/* server list: rows are painted by the delegate; keep Qt from painting its own
   selection/focus block in the indentation area */
QTreeWidget#ServerTree {{ outline: 0; }}
QTreeWidget#ServerTree::item, QTreeWidget#ServerTree::item:selected, QTreeWidget#ServerTree::item:hover,
QTreeWidget#ServerTree::item:selected:active, QTreeWidget#ServerTree::item:selected:!active {{
  background: transparent; border: none; }}
QTreeWidget#ServerTree::branch, QTreeWidget#ServerTree::branch:selected, QTreeWidget#ServerTree::branch:hover {{
  background: transparent; border: none; }}
QHeaderView::section {{ background: transparent; color: {C['faint']}; border: none;
  border-bottom: 1px solid {C['border']}; padding: 6px 8px; font-size: 8.5pt; }}

QTabWidget::pane {{ border: none; }}
QTabBar {{ background: {C['bg']}; }}
QTabBar::tab {{ background: transparent; color: {C['muted']}; padding: 9px 10px 9px 14px; margin: 6px 2px 0 2px;
  border-top-left-radius: 10px; border-top-right-radius: 10px; min-width: 90px; }}
QTabBar::tab:selected {{ background: {C['surface']}; color: {C['text']}; }}
QTabBar::tab:hover:!selected {{ background: {C['sidebar']}; color: {C['text']}; }}
QTabBar::close-button {{ subcontrol-position: right; image: url(__ASSETS__/close.svg); width: 16px; height: 16px;
  border-radius: 5px; }}
QToolButton#TabClose {{ padding: 0; border-radius: 6px; }}
QToolButton#TabClose:hover {{ background: {C['hover']}; }}
QTabBar::close-button:hover {{ image: url(__ASSETS__/close-hover.svg); background: {C['hover']}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {C['border']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {C['faint']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {C['border']}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QSplitter::handle {{ background: {C['border']}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QMenu {{ background: {C['surface2']}; border: 1px solid {C['border']}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {C['hover']}; }}
QMenu::separator {{ height: 1px; background: {C['border']}; margin: 5px 8px; }}
QMenu::icon {{ padding-left: 8px; }}

QStatusBar {{ background: {C['sidebar']}; color: {C['muted']}; border-top: 1px solid {C['border']}; }}
QStatusBar QLabel {{ color: {C['muted']}; font-size: 8.5pt; padding: 0 6px; }}

QProgressBar {{ background: {C['surface']}; border: none; border-radius: 3px; height: 6px; text-align: center; }}
QProgressBar::chunk {{ background: {C['accent']}; border-radius: 3px; }}

QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
QCheckBox::indicator {{ border: 1px solid {C['faint']}; border-radius: 5px; background: {C['surface']}; }}
QCheckBox::indicator:checked {{ background: {C['accent']}; border-color: {C['accent']}; image: url(__ASSETS__/check.svg); }}
QRadioButton::indicator {{ border: 1px solid {C['faint']}; border-radius: 8px; background: {C['surface']}; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QRadioButton::indicator:checked {{ border: 1px solid {C['accent']};
  background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 {C['accent']}, stop:0.42 {C['accent']}, stop:0.52 {C['surface']}, stop:1 {C['surface']}); }}

#Card {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 14px; }}
#Card:hover {{ border: 1px solid {C['accent']}; }}
#Palette {{ background: {C['surface2']}; border: 1px solid {C['border']}; border-radius: 14px; }}
#Palette QLineEdit {{ background: transparent; border: none; font-size: 12pt; padding: 12px 14px; }}
#Palette QListWidget::item {{ padding: 8px 10px; }}
#PaneHeader {{ background: {C['surface']}; border-bottom: 1px solid {C['border']}; }}
#PaneHeader[active="true"] {{ border-bottom: 1px solid {C['accent']}; }}
#Overlay {{ background: rgba(11,13,18,215); }}
#Toolbar {{ background: {C['surface']}; border-bottom: 1px solid {C['border']}; }}
#Muted {{ color: {C['muted']}; }}
#Hint {{ color: {C['faint']}; font-size: 8.5pt; }}
#H1 {{ font-size: 22pt; font-weight: 600; }}
#H2 {{ font-size: 12pt; font-weight: 600; }}
#Pill {{ background: {C['surface2']}; border: 1px solid {C['border']}; border-radius: 9px; padding: 2px 8px;
  color: {C['muted']}; font-size: 8.5pt; }}
#SftpPanel {{ background: {C['sidebar']}; border-left: 1px solid {C['border']}; }}
#DropHint {{ border: 2px dashed {C['accent']}; border-radius: 12px; color: {C['accent']}; }}
"""

# 24x24 stroke icons (hand-made, stroke = {c})
_ICONS = {
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
    "settings": '<path d="M19.28 10.70 L21.54 10.95 L21.54 13.05 L19.28 13.30 L19.23 13.59 L18.07 16.23 L19.49 18.00 L18.00 19.49 L16.23 18.07 L15.99 18.23 L13.30 19.28 L13.05 21.54 L10.95 21.54 L10.70 19.28 L10.41 19.23 L7.77 18.07 L6.00 19.49 L4.51 18.00 L5.93 16.23 L5.77 15.99 L4.72 13.30 L2.46 13.05 L2.46 10.95 L4.72 10.70 L4.77 10.41 L5.93 7.77 L4.51 6.00 L6.00 4.51 L7.77 5.93 L8.01 5.77 L10.70 4.72 L10.95 2.46 L13.05 2.46 L13.30 4.72 L13.59 4.77 L16.23 5.93 L18.00 4.51 L19.49 6.00 L18.07 7.77 L18.23 8.01 Z"/><circle cx="12" cy="12" r="3"/>',
    "folder": '<path d="M3 7.5a2 2 0 0 1 2-2h4l2 2.2h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "folder-open": '<path d="M3 8V7a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v1"/><path d="M3.5 19l2.3-8h15.7l-2.3 8z"/>',
    "file": '<path d="M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M14 3v5h5"/>',
    "server": '<rect x="3.5" y="4" width="17" height="7" rx="2"/><rect x="3.5" y="13" width="17" height="7" rx="2"/><path d="M7.5 7.5h.01M7.5 16.5h.01"/>',
    "terminal": '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M7 10l3 2.5L7 15M12.5 15.5H17"/>',
    "split-h": '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M12 4.5v15"/>',
    "split-v": '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M3 12h18"/>',
    "upload": '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
    "download": '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6"/>',
    "up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "home": '<path d="M4 11l8-7 8 7v8.5a1 1 0 0 1-1 1h-4.5V15h-5v5.5H5a1 1 0 0 1-1-1z"/>',
    "trash": '<path d="M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
    "edit": '<path d="M4 20h4l11-11-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
    "import": '<path d="M12 3v12M7 10l5 5 5-5"/><path d="M5 15v4a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-4"/>',
    "star": '<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.8l-5.2 2.8 1-5.8-4.3-4.1 5.9-.8z"/>',
    "bolt": '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>',
    "broadcast": '<circle cx="12" cy="12" r="2"/><path d="M8 8a5.5 5.5 0 0 0 0 8M16 8a5.5 5.5 0 0 1 0 8M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14"/>',
    "code": '<path d="M9 7l-5 5 5 5M15 7l5 5-5 5"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    "plug": '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>',
    "sidebar": '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M15 4.5v15"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .8-1 1.5v.4M12 16.8h.01"/>',
    "heart": '<path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.4 4.3 4.3 0 0 1 19.5 10c0 5.4-7.5 10-7.5 10z"/>',
    "coffee": '<path d="M5 9h11v6a4.5 4.5 0 0 1-4.5 4.5h-2A4.5 4.5 0 0 1 5 15z"/><path d="M16 10.5h1.5a2.5 2.5 0 0 1 0 5H16"/><path d="M8.5 3.5c-.8 1 .8 2-.1 3M12 3.5c-.8 1 .8 2-.1 3"/><path d="M4 21h13"/>',
    "tunnel": '<path d="M4 8h13M13 4l4 4-4 4M20 16H7M11 12l-4 4 4 4"/>',
    "shield": '<path d="M12 3l7 3v5.5c0 4.5-3 7.8-7 9.5-4-1.7-7-5-7-9.5V6z"/><path d="M9 12l2 2 4-4"/>',
    "folder-plus": '<path d="M3 7.5a2 2 0 0 1 2-2h4l2 2.2h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v5M9.5 13.5h5"/>',
}

_cache: dict[tuple, QIcon] = {}


def blend(base: str, color: str, amount: float) -> str:
    """Mix `color` into `base` (0 = base, 1 = color). Used to tint production servers."""
    a, b = QColor(base), QColor(color)
    if not b.isValid():
        return base
    mix = lambda x, y: round(x + (y - x) * amount)  # noqa: E731
    return QColor(mix(a.red(), b.red()), mix(a.green(), b.green()), mix(a.blue(), b.blue())).name()


def icon(name: str, color: str | None = None, size: int = 18) -> QIcon:
    color = color or C["muted"]
    key = (name, color, size)
    if key in _cache:
        return _cache[key]
    body = _ICONS.get(name, _ICONS["file"])
    fill = color if name == "star-filled" else "none"
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="{fill}" '
           f'stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    ico = QIcon()
    for scale in (1, 2):
        pm = QPixmap(QSize(size * scale, size * scale))
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        pm.setDevicePixelRatio(scale)
        ico.addPixmap(pm)
    _cache[key] = ico
    return ico


def star_icon(filled: bool, size: int = 16) -> QIcon:
    key = ("star", filled, size)
    if key in _cache:
        return _cache[key]
    col = C["warn"] if filled else C["faint"]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="{col if filled else "none"}" '
           f'stroke="{col}" stroke-width="1.7" stroke-linejoin="round">{_ICONS["star"]}</svg>')
    r = QSvgRenderer(QByteArray(svg.encode()))
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    r.render(p)
    p.end()
    pm.setDevicePixelRatio(2)
    _cache[key] = QIcon(pm)
    return _cache[key]


def apply_palette(app) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(C["bg"]))
    pal.setColor(QPalette.WindowText, QColor(C["text"]))
    pal.setColor(QPalette.Base, QColor(C["surface"]))
    pal.setColor(QPalette.AlternateBase, QColor(C["sidebar"]))
    pal.setColor(QPalette.Text, QColor(C["text"]))
    pal.setColor(QPalette.Button, QColor(C["surface2"]))
    pal.setColor(QPalette.ButtonText, QColor(C["text"]))
    pal.setColor(QPalette.Highlight, QColor(C["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor("#0b0d12"))
    pal.setColor(QPalette.PlaceholderText, QColor(C["faint"]))
    pal.setColor(QPalette.ToolTipBase, QColor(C["surface2"]))
    pal.setColor(QPalette.ToolTipText, QColor(C["text"]))
    app.setPalette(pal)
    from .paths import assets_dir
    from .platform_ui import FONT_SCALE, pick_ui_font
    fam = pick_ui_font()
    qss = QSS.replace("__ASSETS__", assets_dir().as_posix()).replace(UI_FONT, fam)
    if FONT_SCALE != 1.0:
        qss = re.sub(r"(\d+(?:\.\d+)?)pt", lambda m: f"{float(m.group(1)) * FONT_SCALE:.1f}pt", qss)
    app.setStyleSheet(qss)


def style_window(widget) -> None:
    """Dark title bar + matching caption color on Windows 10/11."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = wintypes.HWND(int(widget.winId()))
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))   # dark mode
        col = QColor(C["sidebar"])
        colorref = ctypes.c_int(col.red() | (col.green() << 8) | (col.blue() << 16))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(colorref), ctypes.sizeof(colorref))  # caption
        border = ctypes.c_int(0xFFFFFFFE)  # DWMWA_COLOR_NONE
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
        corner = ctypes.c_int(2)  # round corners (Win11)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
    except Exception:
        pass
