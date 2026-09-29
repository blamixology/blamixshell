"""Non-secret UI preferences (plain JSON next to the vault)."""
from __future__ import annotations

import json

from .paths import settings_path
from .platform_ui import IS_MAC, MONO_DEFAULT

TERMINAL_THEMES: dict[str, dict] = {
    "Midnight": {
        "background": "#0d1017", "foreground": "#d8dee9", "cursor": "#7aa2ff",
        "cursorAccent": "#0d1017", "selectionBackground": "#2c3a5c",
        "black": "#1b1f2a", "red": "#ff6b7a", "green": "#7ee0a1", "yellow": "#f5d37a",
        "blue": "#7aa2ff", "magenta": "#c49bff", "cyan": "#6fd8e8", "white": "#d8dee9",
        "brightBlack": "#5a6378", "brightRed": "#ff8e9a", "brightGreen": "#9ff0bd",
        "brightYellow": "#ffe3a0", "brightBlue": "#a3c0ff", "brightMagenta": "#dabcff",
        "brightCyan": "#9ae9f3", "brightWhite": "#ffffff",
    },
    "Graphite": {
        "background": "#161616", "foreground": "#e4e4e4", "cursor": "#f0f0f0",
        "cursorAccent": "#161616", "selectionBackground": "#3a3a3a",
        "black": "#262626", "red": "#e5534b", "green": "#57ab5a", "yellow": "#c69026",
        "blue": "#539bf5", "magenta": "#b083f0", "cyan": "#39c5cf", "white": "#d0d0d0",
        "brightBlack": "#6e6e6e", "brightRed": "#ff7b72", "brightGreen": "#7ee787",
        "brightYellow": "#e3b341", "brightBlue": "#79c0ff", "brightMagenta": "#d2a8ff",
        "brightCyan": "#56d4dd", "brightWhite": "#ffffff",
    },
    "Deep Ocean": {
        "background": "#07141f", "foreground": "#c7e3f2", "cursor": "#48d1cc",
        "cursorAccent": "#07141f", "selectionBackground": "#16384f",
        "black": "#0f2433", "red": "#ef6f6c", "green": "#5fd3a0", "yellow": "#f0c674",
        "blue": "#4fa3e0", "magenta": "#a98ae6", "cyan": "#48d1cc", "white": "#c7e3f2",
        "brightBlack": "#4a6a80", "brightRed": "#ff9491", "brightGreen": "#8cf0c4",
        "brightYellow": "#ffdca0", "brightBlue": "#82c3ff", "brightMagenta": "#c9b1ff",
        "brightCyan": "#7eeeea", "brightWhite": "#ffffff",
    },
    "Paper (light)": {
        "background": "#fbfbf8", "foreground": "#2b2f36", "cursor": "#3a5bd9",
        "cursorAccent": "#fbfbf8", "selectionBackground": "#cfdcff",
        "black": "#2b2f36", "red": "#c9303b", "green": "#2b8a3e", "yellow": "#a86f00",
        "blue": "#3a5bd9", "magenta": "#8c3fc2", "cyan": "#0b7f8c", "white": "#d9d9d4",
        "brightBlack": "#6b7280", "brightRed": "#e03e49", "brightGreen": "#37a14c",
        "brightYellow": "#c98800", "brightBlue": "#5476ef", "brightMagenta": "#a557db",
        "brightCyan": "#1797a6", "brightWhite": "#ffffff",
    },
}

DEFAULTS = {
    "font_family": MONO_DEFAULT,
    "font_size": 13 if IS_MAC else 14,
    "line_height": 1.15,
    "theme": "Midnight",
    "cursor_style": "bar",          # bar | block | underline
    "cursor_blink": True,
    "scrollback": 10000,
    "copy_on_select": True,
    "right_click_paste": True,
    "confirm_multiline_paste": True,
    "sftp_visible": False,
    "window_geometry": "",
    "sidebar_width": 280,
    "restore_tabs": True,           # reopen last session's tabs (they connect when opened)
    "last_session": {},             # layout only: server ids / splits, never passwords
    "check_updates": True,          # daily check against GitHub Releases
    "skip_version": "",
    "last_update_check": 0,
}


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            self.update(json.loads(settings_path().read_text(encoding="utf-8")))
        except Exception:
            pass
        if self.get("theme") not in TERMINAL_THEMES:
            self["theme"] = DEFAULTS["theme"]

    def save(self) -> None:
        try:
            settings_path().write_text(json.dumps(self, indent=2), encoding="utf-8")
        except Exception:
            pass

    def terminal_options(self) -> dict:
        return {
            "fontFamily": self["font_family"],
            "fontSize": int(self["font_size"]),
            "lineHeight": float(self["line_height"]),
            "cursorStyle": self["cursor_style"],
            "cursorBlink": bool(self["cursor_blink"]),
            "scrollback": int(self["scrollback"]),
            "theme": TERMINAL_THEMES[self["theme"]],
            "copyOnSelect": bool(self["copy_on_select"]),
            "rightClickPaste": bool(self["right_click_paste"]),
        }
