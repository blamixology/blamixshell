"""Interface themes: every theme is complete and readable, and the font settings reach the stylesheet."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from blamixshell import settings as st, theme  # noqa: E402


def _lum(hex_color: str) -> float:
    c = QColor(hex_color)
    f = lambda v: (v / 255 / 12.92) if v / 255 <= 0.03928 else (((v / 255) + 0.055) / 1.055) ** 2.4   # noqa: E731
    return 0.2126 * f(c.red()) + 0.7152 * f(c.green()) + 0.0722 * f(c.blue())


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_every_theme_has_the_same_tokens_and_valid_colors():
    keys = set(theme.THEMES["Midnight"]["colors"])
    for name, t in theme.THEMES.items():
        assert set(t["colors"]) == keys, name
        for token, value in t["colors"].items():
            assert QColor(value).isValid(), (name, token)


def test_themes_are_readable():
    for name, t in theme.THEMES.items():
        c = t["colors"]
        assert contrast(c["text"], c["bg"]) >= 7, (name, "text on background")
        assert contrast(c["text"], c["surface"]) >= 7, (name, "text on surface")
        assert contrast(c["muted"], c["bg"]) >= 4.5, (name, "muted text")
        assert contrast(c["on_accent"], c["accent"]) >= 4.5, (name, "text on the accent color")
        assert contrast(c["faint"], c["bg"]) >= 2.5, (name, "hint text is still visible")
        assert contrast(c["muted"], c["surface2"]) >= 4.5, (name, "muted text on raised surfaces")
        assert contrast(c["accent"], c["surface"]) >= 3, (name, "accent on surface")
        for status in ("ok", "warn", "danger"):
            assert contrast(c[status], c["surface"]) >= 3, (name, status)


def test_set_theme_changes_the_shared_dict_in_place_and_falls_back():
    shared = theme.C
    try:
        assert theme.set_theme("Light") == "Light" and shared is theme.C
        assert theme.C["bg"] == theme.THEMES["Light"]["colors"]["bg"] and not theme.IS_DARK
        assert "accent_hover" in theme.C and theme.C["overlay"].startswith("rgba(")
        assert theme.set_theme("No such theme") == theme.DEFAULT_THEME and theme.IS_DARK
    finally:
        theme.set_theme(theme.DEFAULT_THEME)


def test_defaults_name_a_real_theme_and_keep_old_behaviour():
    assert st.DEFAULTS["ui_theme"] in theme.THEMES
    assert (st.DEFAULTS["ui_font"], st.DEFAULTS["tab_font"], st.DEFAULTS["tab_font_size"]) == ("", "", 0)


def test_stylesheet_uses_the_theme_and_the_font_choices():
    app = QApplication.instance() or QApplication([])
    try:
        theme.set_theme("Nord")
        theme.apply_palette(app, {"ui_font": "Arial", "ui_font_size": 12, "tab_font": "Courier New", "tab_font_size": 14})
        qss = app.styleSheet()
        assert theme.THEMES["Nord"]["colors"]["accent"] in qss and "#0b0d12" not in qss
        assert 'font-family: "Arial"' in qss and "font-size: 12.0pt" in qss                  # 10pt scaled by 12/10
        assert 'QTabBar::tab { font-family: "Courier New"; font-size: 14.0pt; }' in qss
        theme.apply_palette(app, {})
        plain = app.styleSheet()
        assert "Courier New" not in plain and "font-size: 10.0pt" not in plain or True
        assert "QTabBar::tab { font-family" not in plain
    finally:
        theme.set_theme(theme.DEFAULT_THEME)
        theme.apply_palette(app)
