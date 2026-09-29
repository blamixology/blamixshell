"""Per-OS details for the desktop UI (fonts, shortcut labels)."""
from __future__ import annotations

import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

UI_FONTS = (["Segoe UI Variable Text", "Segoe UI"] if IS_WIN else
            [".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue"] if IS_MAC else
            ["Ubuntu", "Cantarell", "Inter", "Noto Sans", "DejaVu Sans"])

MONO_DEFAULT = ("Cascadia Code, Cascadia Mono, Consolas, monospace" if IS_WIN else
                "SF Mono, Menlo, Monaco, monospace" if IS_MAC else
                "Ubuntu Mono, JetBrains Mono, DejaVu Sans Mono, Liberation Mono, monospace")

# macOS renders points at 72 dpi, so Windows-tuned pt sizes look tiny there
FONT_SCALE = 1.3 if IS_MAC else 1.05 if IS_LINUX else 1.0


def pick_ui_font() -> str:
    from PySide6.QtGui import QFontDatabase
    fams = set(QFontDatabase.families())
    for f in UI_FONTS:
        if f in fams or f.startswith("."):
            return f
    return QFontDatabase.systemFont(QFontDatabase.GeneralFont).family()


def kb(text: str) -> str:
    """Shortcut labels: 'Ctrl+Shift+P' -> '⌘P' on macOS (the app maps Cmd+key there)."""
    if IS_MAC:
        return text.replace("Ctrl+Shift+", "⌘").replace("Ctrl+", "⌘")
    return text
