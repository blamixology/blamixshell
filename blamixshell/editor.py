"""Built-in editor (from BlamixFiles): server files open in tabs, with Pygments syntax highlighting, line numbers,
find/replace, go to line and comment toggling; Ctrl+S saves straight back to the server (a check for changes made
by someone else meanwhile, an atomic replace, encoding / line endings / permissions kept, sudo for root's files)."""
from __future__ import annotations

import bisect
import difflib
import hashlib
import re

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QKeySequence, QPainter, QShortcut,
                           QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat)
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QSplitter, QTextBrowser,
                               QPlainTextEdit, QPushButton, QTextEdit, QToolButton, QVBoxLayout, QWidget)

from .platform_ui import MONO_DEFAULT
from .textfile import EDIT_LIMIT, EOL_NAMES, VIEW_LIMIT, NotText, decode, encode
from .theme import C, blend, icon


def is_dark() -> bool:
    return QColor(C.get("bg", "#000000")).lightness() < 128


def human_size(n: int) -> str:
    from .sftp_panel import human
    return human(n)

FULL_LEX_LIMIT = 1_000_000     # whole-document lexing (correct multi-line strings/comments) below this
COMPARE_LIMIT = 2_000_000      # before saving, compare the server copy byte-for-byte up to this size

# token type -> (color, bold, italic); looked up through the token's parents
_STYLE = {
    "Comment": ("#5f6b85", False, True),
    "Comment.Preproc": ("#c49bff", False, False),
    "Keyword": ("#c49bff", False, False),
    "Keyword.Constant": ("#ffb86b", False, False),
    "Keyword.Type": ("#6fd8e8", False, False),
    "Name.Builtin": ("#6fd8e8", False, False),
    "Name.Function": ("#7aa2ff", False, False),
    "Name.Class": ("#f5d37a", False, False),
    "Name.Decorator": ("#6fd8e8", False, False),
    "Name.Tag": ("#ff7b8a", False, False),
    "Name.Attribute": ("#f5d37a", False, False),
    "Name.Variable": ("#ffa3b1", False, False),
    "Name.Constant": ("#ffb86b", False, False),
    "Name.Namespace": ("#f5d37a", False, False),
    "Name.Label": ("#7aa2ff", False, False),
    "Literal.String": ("#7ee0a1", False, False),
    "Literal.String.Escape": ("#ffb86b", False, False),
    "Literal.String.Interpol": ("#ffb86b", False, False),
    "Literal.Number": ("#ffb86b", False, False),
    "Literal": ("#ffb86b", False, False),
    "Operator.Word": ("#c49bff", False, False),
    "Generic.Heading": ("#7aa2ff", True, False),
    "Generic.Subheading": ("#7aa2ff", True, False),
    "Generic.Inserted": ("#7ee0a1", False, False),
    "Generic.Deleted": ("#ff7b8a", False, False),
    "Generic.Emph": ("", False, True),
    "Generic.Strong": ("", True, False),
    "Error": ("#ff5d73", False, False),
}

_STYLE_LIGHT = {
    "Comment": ("#6a7490", False, True),
    "Comment.Preproc": ("#7c3aed", False, False),
    "Keyword": ("#7c3aed", False, False),
    "Keyword.Constant": ("#b45309", False, False),
    "Keyword.Type": ("#0e7490", False, False),
    "Name.Builtin": ("#0e7490", False, False),
    "Name.Function": ("#2952cc", False, False),
    "Name.Class": ("#92710a", False, False),
    "Name.Decorator": ("#0e7490", False, False),
    "Name.Tag": ("#c4243b", False, False),
    "Name.Attribute": ("#92710a", False, False),
    "Name.Variable": ("#b8324a", False, False),
    "Name.Constant": ("#b45309", False, False),
    "Name.Namespace": ("#92710a", False, False),
    "Name.Label": ("#2952cc", False, False),
    "Literal.String": ("#15803d", False, False),
    "Literal.String.Escape": ("#b45309", False, False),
    "Literal.String.Interpol": ("#b45309", False, False),
    "Literal.Number": ("#b45309", False, False),
    "Literal": ("#b45309", False, False),
    "Operator.Word": ("#7c3aed", False, False),
    "Generic.Heading": ("#2952cc", True, False),
    "Generic.Subheading": ("#2952cc", True, False),
    "Generic.Inserted": ("#15803d", False, False),
    "Generic.Deleted": ("#c4243b", False, False),
    "Generic.Emph": ("", False, True),
    "Generic.Strong": ("", True, False),
    "Error": ("#d6334c", False, False),
}

# line comment prefix by Pygments lexer name (for Ctrl+/)
_COMMENTS = {"//": ("javascript", "typescript", "php", "c", "c++", "java", "go", "rust", "c#", "kotlin",
                    "scss", "swift", "dart", "json5", "groovy"),
             "--": ("sql", "lua", "haskell", "mysql", "postgresql", "plpgsql"),
             ";": ("ini", "asm", "lisp", "clojure", "scheme"),
             "%": ("tex", "latex", "erlang", "matlab")}


def comment_prefix(lexer_name: str) -> str:
    low = lexer_name.lower()
    for prefix, names in _COMMENTS.items():
        if any(low == n or low.startswith(n + " ") for n in names):
            return prefix
    return "#"


def mono_font() -> QFont:
    fams = set(QFontDatabase.families())
    for f in (x.strip() for x in MONO_DEFAULT.split(",")):
        if f in fams:
            font = QFont(f)
            break
    else:
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPointSizeF(10.5)
    font.setStyleHint(QFont.Monospace)
    return font


def guess_lexer(filename: str, text: str):
    try:
        from pygments.lexers import get_lexer_for_filename, guess_lexer_for_filename
        from pygments.lexers.special import TextLexer
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    opts = dict(stripnl=False, stripall=False, ensurenl=False)
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    special = {"nginx.conf": "nginx", "dockerfile": "docker", "makefile": "make", ".env": "bash",
               ".bashrc": "bash", ".profile": "bash", ".zshrc": "bash", ".htaccess": "apacheconf",
               "caddyfile": "text", "jenkinsfile": "groovy", "vagrantfile": "ruby"}
    from pygments.lexers import get_lexer_by_name
    low = base.lower()
    try:
        if low in special:
            return get_lexer_by_name(special[low], **opts)
        if low.endswith((".conf", ".vhost")) and ("server {" in text or "location " in text):
            return get_lexer_by_name("nginx", **opts)
        if low.endswith(".service") or low.endswith(".timer") or low.endswith(".socket"):
            return get_lexer_by_name("ini", **opts)
        if low.startswith(".env"):
            return get_lexer_by_name("bash", **opts)
        return get_lexer_for_filename(base, text[:4000], **opts)
    except ClassNotFound:
        pass
    first = text.split("\n", 1)[0]
    if first.startswith("#!"):
        for key, name in (("python", "python"), ("bash", "bash"), ("sh", "bash"), ("node", "javascript"),
                          ("perl", "perl"), ("ruby", "ruby"), ("php", "php")):
            if key in first:
                return get_lexer_by_name(name, **opts)
    try:
        return guess_lexer_for_filename(base, text[:4000], **opts)
    except ClassNotFound:
        return TextLexer(**opts)


class Highlighter(QSyntaxHighlighter):
    def __init__(self, doc: QTextDocument, lexer):
        super().__init__(doc)
        self.lexer = lexer
        self._cache: dict = {}
        self._blocks: dict[int, list] = {}
        self.full = False
        self._timer = QTimer(singleShot=True, interval=250)
        self._timer.timeout.connect(self.relex)

    def fmt(self, ttype):
        if ttype in self._cache:
            return self._cache[ttype]
        t = ttype
        spec = None
        while t is not None and len(t):
            spec = (_STYLE if is_dark() else _STYLE_LIGHT).get(".".join(t))
            if spec:
                break
            t = t.parent
        f = None
        if spec:
            f = QTextCharFormat()
            color, bold, italic = spec
            if color:
                f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Bold)
            if italic:
                f.setFontItalic(True)
        self._cache[ttype] = f
        return f

    def restyle(self) -> None:
        """After a theme change: forget the cached colors and paint the text again."""
        self._cache.clear()
        self.rehighlight()

    def start(self) -> None:
        doc = self.document()
        self.full = self.lexer is not None and doc.characterCount() < FULL_LEX_LIMIT
        if self.full:
            # only real edits (re-applying formats also reports a 0/0 change)
            doc.contentsChange.connect(lambda _pos, rem, add: (rem or add) and self._timer.start())
            self.relex()
        else:
            self.rehighlight()

    def relex(self) -> None:
        if self.lexer is None:
            return
        text = self.document().toPlainText()
        starts = [0] + [m.end() for m in re.finditer("\n", text)]
        blocks: dict[int, list] = {}
        for index, ttype, value in self.lexer.get_tokens_unprocessed(text):
            f = self.fmt(ttype)
            if f is None or not value:
                continue
            pos, end = index, index + len(value)
            line = bisect.bisect_right(starts, pos) - 1
            while pos < end and line < len(starts):
                line_end = starts[line + 1] - 1 if line + 1 < len(starts) else len(text)
                seg_end = min(end, line_end)
                if seg_end > pos:
                    blocks.setdefault(line, []).append((pos - starts[line], seg_end - pos, f))
                line += 1
                pos = starts[line] if line < len(starts) else end
        self._blocks = blocks
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        if self.lexer is None:
            return
        if self.full:
            for start, length, f in self._blocks.get(self.currentBlock().blockNumber(), ()):
                self.setFormat(start, length, f)
            return
        for index, ttype, value in self.lexer.get_tokens_unprocessed(text):
            f = self.fmt(ttype)
            if f is not None:
                self.setFormat(index, len(value), f)


class _Gutter(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):  # noqa: N802
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, e):  # noqa: N802
        ed = self.editor
        p = QPainter(self)
        p.fillRect(e.rect(), QColor(C["bg"]))
        block = ed.firstVisibleBlock()
        num = block.blockNumber()
        top = round(ed.blockBoundingGeometry(block).translated(ed.contentOffset()).top())
        bottom = top + round(ed.blockBoundingRect(block).height())
        cur = ed.textCursor().blockNumber()
        h = ed.fontMetrics().height()
        while block.isValid() and top <= e.rect().bottom():
            if block.isVisible() and bottom >= e.rect().top():
                p.setPen(QColor(C["text"] if num == cur else C["faint"]))
                p.drawText(0, top, self.width() - 10, h, Qt.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(ed.blockBoundingRect(block).height())
            num += 1


class CodeEditor(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Code")
        self.setFont(mono_font())
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.indent_unit = "    "
        self.comment = "#"
        self.gutter = _Gutter(self)
        self.blockCountChanged.connect(self._update_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_line)
        self._update_width()
        self._highlight_line()

    def gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_width(self, *_a) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy) -> None:
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def _highlight_line(self) -> None:
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor(blend(C["bg"], C["accent"], 0.07)))
        sel.format.setProperty(QTextFormat.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])
        self.gutter.update()

    # ---- editing helpers
    def keyPressEvent(self, e):  # noqa: N802
        if self.isReadOnly():
            return super().keyPressEvent(e)
        cur = self.textCursor()
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers() & Qt.ShiftModifier:
            line = cur.block().text()
            indent = line[:len(line) - len(line.lstrip(" \t"))]
            if line.rstrip().endswith((":", "{", "[", "(")):
                indent += self.indent_unit
            super().keyPressEvent(e)
            self.insertPlainText(indent)
            return
        if e.key() == Qt.Key_Tab and cur.hasSelection():
            return self._shift_lines(+1)
        if e.key() == Qt.Key_Backtab:
            return self._shift_lines(-1)
        if e.key() == Qt.Key_Tab:
            self.insertPlainText(self.indent_unit)
            return
        super().keyPressEvent(e)

    def _selected_blocks(self):
        cur = self.textCursor()
        doc = self.document()
        first = doc.findBlock(cur.selectionStart())
        last = doc.findBlock(max(cur.selectionStart(), cur.selectionEnd() - (1 if cur.hasSelection() else 0)))
        b = first
        while True:
            yield b
            if b == last or not b.isValid():
                break
            b = b.next()

    def _shift_lines(self, direction: int) -> None:
        cur = self.textCursor()
        cur.beginEditBlock()
        for b in list(self._selected_blocks()):
            c = QTextCursor(b)
            if direction > 0:
                c.insertText(self.indent_unit)
            else:
                text = b.text()
                n = 1 if text.startswith("\t") else len(text) - len(text.lstrip(" "))
                n = min(n, len(self.indent_unit)) if not text.startswith("\t") else 1
                c.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, n)
                c.removeSelectedText()
        cur.endEditBlock()

    def toggle_comment(self) -> None:
        blocks = [b for b in self._selected_blocks() if b.text().strip()]
        if not blocks:
            return
        prefix = self.comment
        all_commented = all(b.text().lstrip().startswith(prefix) for b in blocks)
        cur = self.textCursor()
        cur.beginEditBlock()
        for b in blocks:
            text = b.text()
            lead = len(text) - len(text.lstrip())
            c = QTextCursor(b)
            c.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, lead)
            if all_commented:
                n = len(prefix) + (1 if text[lead + len(prefix):lead + len(prefix) + 1] == " " else 0)
                c.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, n)
                c.removeSelectedText()
            else:
                c.insertText(prefix + " ")
        cur.endEditBlock()


class FindBar(QWidget):
    def __init__(self, editor: CodeEditor, parent=None):
        super().__init__(parent)
        self.setObjectName("FindBar")
        self.ed = editor
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        self.find = QLineEdit(placeholderText="Find")
        self.repl = QLineEdit(placeholderText="Replace")
        self.case = QCheckBox("Aa")
        self.case.setToolTip("Match case")
        self.regex = QCheckBox(".*")
        self.regex.setToolTip("Regular expression")
        self.info = QLabel("", objectName="Hint")
        lay.addWidget(self.find, 2)
        prev_b = QPushButton("↑")
        next_b = QPushButton("↓")
        lay.addWidget(prev_b)
        lay.addWidget(next_b)
        lay.addWidget(self.case)
        lay.addWidget(self.regex)
        lay.addWidget(self.repl, 2)
        one = QPushButton("Replace")
        every = QPushButton("All")
        lay.addWidget(one)
        lay.addWidget(every)
        lay.addWidget(self.info)
        close = QToolButton()
        close.setIcon(icon("x"))
        close.clicked.connect(self.hide)
        lay.addWidget(close)
        self.find.returnPressed.connect(lambda: self.go(False))
        next_b.clicked.connect(lambda: self.go(False))
        prev_b.clicked.connect(lambda: self.go(True))
        one.clicked.connect(self.replace_one)
        every.clicked.connect(self.replace_all)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.hide, context=Qt.WidgetWithChildrenShortcut)

    def open(self, replace: bool) -> None:
        self.show()
        sel = self.ed.textCursor().selectedText()
        if sel and " " not in sel:
            self.find.setText(sel)
        self.repl.setVisible(replace)
        self.find.setFocus()
        self.find.selectAll()

    def _pattern(self):
        from PySide6.QtCore import QRegularExpression
        text = self.find.text()
        if not self.regex.isChecked():
            text = QRegularExpression.escape(text)
        rx = QRegularExpression(text)
        if not self.case.isChecked():
            rx.setPatternOptions(QRegularExpression.CaseInsensitiveOption)
        return rx

    def go(self, backwards: bool) -> bool:
        if not self.find.text():
            return False
        flags = QTextDocument.FindBackward if backwards else QTextDocument.FindFlag(0)
        rx = self._pattern()
        found = self.ed.find(rx, flags)
        if not found:   # wrap around
            c = self.ed.textCursor()
            c.movePosition(QTextCursor.End if backwards else QTextCursor.Start)
            self.ed.setTextCursor(c)
            found = self.ed.find(rx, flags)
        self.info.setText("" if found else "No matches")
        return found

    def replace_one(self) -> None:
        c = self.ed.textCursor()
        if c.hasSelection() and self._pattern().match(c.selectedText()).hasMatch():
            c.insertText(self.repl.text())
        self.go(False)

    def replace_all(self) -> None:
        if not self.find.text():
            return
        rx = self._pattern()
        c = QTextCursor(self.ed.document())
        c.beginEditBlock()
        n = 0
        cur = self.ed.document().find(rx, 0)
        while not cur.isNull() and cur.hasSelection():
            cur.insertText(self.repl.text())
            n += 1
            cur = self.ed.document().find(rx, cur.position())
        c.endEditBlock()
        self.info.setText(f"{n} replaced")


class DiffDialog(QDialog):
    def __init__(self, name: str, mine: str, theirs: str, parent=None,
                 left: str = "on the server", right: str = "your version"):
        super().__init__(parent)
        self.setWindowTitle(f"Changes: {name}")
        self.resize(900, 600)
        lay = QVBoxLayout(self)
        view = QPlainTextEdit(readOnly=True)
        view.setFont(mono_font())
        diff = difflib.unified_diff(theirs.splitlines(), mine.splitlines(),
                                    left, right, lineterm="")
        view.setPlainText("\n".join(diff) or "No differences.")
        Highlighter(view.document(), _diff_lexer()).start()
        lay.addWidget(view)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        lay.addWidget(close, 0, Qt.AlignRight)


def is_markdown(path: str, lexer_name: str = "") -> bool:
    return path.lower().endswith((".md", ".markdown", ".mdown", ".mkd")) or lexer_name.lower() == "markdown"


def _diff_lexer():
    try:
        from pygments.lexers import DiffLexer
        return DiffLexer(stripnl=False, ensurenl=False)
    except ImportError:
        return None


# ======================================================================= server files in tabs
class _Jobs(QObject):
    """Runs file work off the UI thread and hands the result back to it."""
    done = Signal(object, object, object)      # callback, result, error (an exception or None)

    def __init__(self, parent=None):
        super().__init__(parent)
        from concurrent.futures import ThreadPoolExecutor
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="editor")
        self.done.connect(lambda cb, res, err: cb(res, err))

    def run(self, fn, callback) -> None:
        def job():
            try:
                res, err = fn(), None
            except Exception as e:              # every failure goes back to the editor, which says what happened
                res, err = None, e
            self.done.emit(callback, res, err)
        self.pool.submit(job)


class EditorTab(QWidget):
    title_changed = Signal()
    message = Signal(str)

    def __init__(self, session, path: str, parent=None):
        super().__init__(parent)
        from .remote_file import RemoteFile
        self.session = session
        self.path = path
        self.file = RemoteFile(session, path)
        self.jobs = _Jobs(self)
        self.meta = None
        self.base_mtime = 0.0
        self.base_size = 0
        self.base_hash = ""
        self.base_mode = None
        self.loaded = False
        self._saving = False
        self.lexer_name = "Plain text"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 6, 10, 6)
        self.where = QLabel(f"{session.server.label}:  {path}", objectName="Muted")
        bl.addWidget(self.where, 1)
        self.sudo_lbl = QLabel("sudo", objectName="Hint")
        self.sudo_lbl.setStyleSheet(f"color:{C['warn']}; font-weight:600;")
        self.sudo_lbl.setToolTip("Opened with sudo: saving uses sudo too (the file keeps its owner and mode)")
        self.sudo_lbl.hide()
        bl.addWidget(self.sudo_lbl)
        self.md_buttons = {}
        self.md_group = QButtonGroup(self)
        self.md_group.setExclusive(True)
        for mode, text, tip in (("text", "Text", "Only the text"),
                                ("split", "Side by side", "The text and the formatted page next to each other"),
                                ("preview", "Preview", "Only the formatted page")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.clicked.connect(lambda _c=False, m=mode: self.set_view(m))
            self.md_group.addButton(b)
            self.md_buttons[mode] = b
            bl.addWidget(b)
            b.hide()
        self.save_btn = QPushButton(icon("upload", C["on_accent"]), " Save", objectName="Primary")
        self.save_btn.setToolTip("Save to the server (Ctrl+S)")
        self.save_btn.clicked.connect(self.save)
        self.save_btn.setEnabled(False)
        bl.addWidget(self.save_btn)
        reload_b = QToolButton()
        reload_b.setIcon(icon("refresh"))
        reload_b.setToolTip("Reload from the server")
        reload_b.clicked.connect(self.reload)
        bl.addWidget(reload_b)
        lay.addWidget(bar)
        self.ed = CodeEditor()
        self.ed.setReadOnly(True)
        self.ed.setPlainText("Loading …")
        # Markdown files: the formatted page next to the text (Qt draws it: headings, tables, lists, code, links)
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        self.preview.document().setDefaultStyleSheet(
            f"code, pre {{ font-family: '{mono_font().family()}'; background: {blend(C['bg'], C['accent'], 0.10)}; }}"
            f"a {{ color: {C['accent']}; }} th {{ background: {blend(C['bg'], C['accent'], 0.14)}; }}")
        self.preview.hide()
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self.ed)
        self.split.addWidget(self.preview)
        self.split.setChildrenCollapsible(False)
        lay.addWidget(self.split, 1)
        self.markdown = False
        self._render_timer = QTimer(self, singleShot=True, interval=300)
        self._render_timer.timeout.connect(self._render)
        self.ed.textChanged.connect(lambda: self.markdown and self.preview.isVisible() and self._render_timer.start())
        self.ed.verticalScrollBar().valueChanged.connect(self._follow_scroll)
        self.findbar = FindBar(self.ed)
        self.findbar.hide()
        lay.addWidget(self.findbar)
        self.status = QLabel("", objectName="Hint")
        self.status.setContentsMargins(10, 4, 10, 4)
        lay.addWidget(self.status)
        self.ed.document().modificationChanged.connect(self._modified)
        self.ed.cursorPositionChanged.connect(self._update_status)
        for seq, fn in (("Ctrl+S", self.save), ("Ctrl+F", lambda: self.findbar.open(False)),
                        ("Ctrl+H", lambda: self.findbar.open(True)), ("Ctrl+G", self.goto_line),
                        ("Ctrl+/", self.ed.toggle_comment), ("F3", lambda: self.findbar.go(False)),
                        ("Shift+F3", lambda: self.findbar.go(True))):
            QShortcut(QKeySequence(seq), self, activated=fn, context=Qt.WidgetWithChildrenShortcut)
        self.load()

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def dirty(self) -> bool:
        return self.loaded and self.ed.document().isModified()

    def tab_title(self) -> str:
        return ("• " if self.dirty else "") + self.name

    # ---- sudo: asked once per file, only when the login can't do it
    def _ask_sudo(self, why: str, retry) -> None:
        from PySide6.QtWidgets import QInputDialog
        if not self.file.sudo:
            if QMessageBox.question(self, "Needs root", f"{why}.\n\nUse sudo for this file? (Saving will use sudo "
                                    "too; the file keeps its owner and permissions.)") != QMessageBox.Yes:
                self.status.setText(why + ".")
                return
            self.file.sudo = True
            self.sudo_lbl.show()
            retry()
            return
        pw, ok = QInputDialog.getText(self, "sudo password", f"{why}.\nPassword for sudo on "
                                      f"{self.session.server.label} (for this file only, never saved):",
                                      QLineEdit.Password)
        if ok:
            self.file.sudo_pw = pw
            retry()
        else:
            self.status.setText("Cancelled: sudo needs your password.")

    # ---- load
    def load(self) -> None:
        self.jobs.run(lambda: self.file.read(VIEW_LIMIT), self._loaded)

    def _loaded(self, result, err) -> None:
        from .remote_file import NeedsSudo
        if isinstance(err, NeedsSudo):
            return self._ask_sudo(str(err), self.load)
        if err is not None:
            return self._load_failed(str(err))
        info, data = result
        try:
            text, meta = decode(data)
        except NotText as e:
            return self._load_failed(str(e))
        self.meta = meta
        self.base_mtime, self.base_size, self.base_mode = info.mtime, len(data), info.mode
        self.base_hash = hashlib.sha256(data).hexdigest()
        lexer = guess_lexer(self.path, text)
        self.lexer_name = lexer.name if lexer is not None else "Plain text"
        self.ed.comment = comment_prefix(self.lexer_name)
        lines = text.split("\n", 2000)[:2000]
        tabs = sum(1 for ln in lines if ln.startswith("\t"))
        spaces = sum(1 for ln in lines if ln.startswith("  "))
        self.ed.indent_unit = "\t" if tabs > spaces else ("  " if self._two_space(text) else "    ")
        self.ed.setPlainText(text)
        self.markdown = is_markdown(self.path, self.lexer_name)
        for b in self.md_buttons.values():
            b.setVisible(self.markdown)
        self.hl = Highlighter(self.ed.document(), lexer)
        self.hl.start()
        read_only = len(data) > EDIT_LIMIT
        self.ed.setReadOnly(read_only)
        self.ed.document().setModified(False)
        self.loaded = True
        self.ed.moveCursor(QTextCursor.Start)
        self._update_status()
        if read_only:
            self.message.emit(f"{self.name} is larger than {human_size(EDIT_LIMIT)}: opened read-only")
        if self.markdown:
            self.set_view(EditorTab.md_view)
        self.title_changed.emit()

    def _load_failed(self, msg: str) -> None:
        self.ed.setReadOnly(True)
        self.ed.setPlainText(f"Can't open this file:\n\n{msg}")
        self.status.setText(msg)
        self.message.emit(f"Can't open {self.name}: {msg}")

    @staticmethod
    def _two_space(text: str) -> bool:
        widths = [len(ln) - len(ln.lstrip(" ")) for ln in text.split("\n", 500)[:500] if ln.startswith(" ")]
        return bool(widths) and min(widths) == 2

    def reload(self) -> None:
        if self.dirty and QMessageBox.question(self, "Reload", "Discard your changes and reload?") != QMessageBox.Yes:
            return
        self.loaded = False
        self.load()

    # ---- save: is the server copy still the one we opened? then an atomic replace
    def save(self) -> None:
        if not self.loaded or self.ed.isReadOnly() or self._saving:
            return
        text = self.ed.toPlainText()
        try:
            data = encode(text, self.meta)
        except UnicodeEncodeError:
            if QMessageBox.question(self, "Encoding", f"Some characters can't be saved as {self.meta.encoding}. "
                                    "Save the file as UTF-8 instead?") != QMessageBox.Yes:
                return
            self.meta.encoding, self.meta.bom = "utf-8", False
            data = encode(text, self.meta)
        self._saving = True
        self.save_btn.setEnabled(False)
        self.status.setText("Checking for changes on the server …")

        def check():
            info = self.file.info()
            if info is None:                         # deleted meanwhile: saving creates it again
                return False, None
            changed = info.size != self.base_size or abs(info.mtime - self.base_mtime) > 1
            theirs = None
            if changed or info.size <= COMPARE_LIMIT:    # same size and time (1 s resolution): compare the content
                theirs = self.file.read(VIEW_LIMIT)[1] if info.size <= VIEW_LIMIT else None
                if theirs is not None:
                    changed = hashlib.sha256(theirs).hexdigest() != self.base_hash
            return changed, theirs
        self.jobs.run(check, lambda r, e: self._checked(r, e, text, data))

    def _checked(self, result, err, text: str, data: bytes) -> None:
        from .remote_file import NeedsSudo
        if isinstance(err, NeedsSudo):
            self._saving = False
            return self._ask_sudo(str(err), self.save)
        if err is not None:
            return self._save_failed(str(err))
        changed, theirs = result
        if changed:
            theirs_text = decode(theirs)[0] if theirs is not None else ""
            box = QMessageBox(QMessageBox.Warning, "Changed on the server",
                              f"<b>{self.name}</b> was changed on the server since you opened it.", parent=self)
            overwrite = box.addButton("Overwrite", QMessageBox.DestructiveRole)
            diff = box.addButton("Show differences", QMessageBox.ActionRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            while True:
                box.exec()
                if box.clickedButton() is diff:
                    DiffDialog(self.name, text, theirs_text, self).exec()
                    continue
                break
            if box.clickedButton() is not overwrite:
                self._saving = False
                self.save_btn.setEnabled(True)
                self._update_status()
                return
        self.status.setText("Saving …")
        self.jobs.run(lambda: self.file.write(data, self.base_mode), lambda r, e: self._saved(r, e, data))

    def _saved(self, info, err, data: bytes) -> None:
        from .remote_file import NeedsSudo
        if isinstance(err, NeedsSudo):
            self._saving = False
            return self._ask_sudo(str(err), self.save)
        if err is not None:
            return self._save_failed(str(err))
        self._saving = False
        self.base_mtime = info.mtime if info else self.base_mtime
        self.base_size = info.size if info else len(data)
        self.base_hash = hashlib.sha256(data).hexdigest()
        self.ed.document().setModified(False)
        self._update_status()
        self.message.emit(f"Saved {self.name} to {self.session.server.label}"
                          + (" (with sudo)" if self.file.sudo else ""))

    def _save_failed(self, msg: str) -> None:
        self._saving = False
        self.save_btn.setEnabled(self.dirty)
        self.status.setText(f"Save failed: {msg}")
        self.message.emit(f"Could not save {self.name}: {msg}")

    # ---- Markdown: text, side by side, or only the formatted page
    md_view = "split"                       # the last choice, for the next Markdown file

    def set_view(self, mode: str) -> None:
        if not self.markdown:
            return
        EditorTab.md_view = mode
        self.md_buttons[mode].setChecked(True)
        self.ed.setVisible(mode != "preview")
        self.preview.setVisible(mode != "text")
        if mode == "split":
            w = max(self.split.width(), 2)
            self.split.setSizes([w // 2, w - w // 2])
        if mode != "text":
            self._render()
        (self.preview if mode == "preview" else self.ed).setFocus()

    def _render(self) -> None:
        bar = self.preview.verticalScrollBar()
        keep = bar.value() / bar.maximum() if bar.maximum() else 0.0
        self.preview.setMarkdown(self.ed.toPlainText())
        bar.setValue(round(keep * bar.maximum()))
        self._follow_scroll()

    def _follow_scroll(self, *_a) -> None:
        """Side by side: the page follows the text's scroll position (in proportion)."""
        if not (self.markdown and self.preview.isVisible() and self.ed.isVisible()):
            return
        src, dst = self.ed.verticalScrollBar(), self.preview.verticalScrollBar()
        if src.maximum():
            dst.setValue(round(src.value() / src.maximum() * dst.maximum()))

    # ---- misc
    def goto_line(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        n, ok = QInputDialog.getInt(self, "Go to line", "Line:", self.ed.textCursor().blockNumber() + 1,
                                    1, self.ed.blockCount())
        if ok:
            self.ed.setTextCursor(QTextCursor(self.ed.document().findBlockByNumber(n - 1)))
            self.ed.centerCursor()

    def _modified(self, _m: bool) -> None:
        self.save_btn.setEnabled(self.dirty and not self.ed.isReadOnly())
        self.title_changed.emit()

    def _update_status(self) -> None:
        if not self.loaded:
            return
        c = self.ed.textCursor()
        m = self.meta
        enc = m.encoding.upper() + (" BOM" if m.bom else "")
        eol = EOL_NAMES.get(m.eol, "LF") + (" (mixed)" if m.mixed_eol else "")
        ro = " · read-only" if self.ed.isReadOnly() else ""
        self.status.setText(f"Ln {c.blockNumber() + 1}, Col {c.positionInBlock() + 1} · {enc} · {eol} · "
                            f"{self.lexer_name}{ro}  ·  Ctrl+S save · Ctrl+F find · Ctrl+H replace · Ctrl+G line · "
                            "Ctrl+/ comment")


class EditorWindow(QWidget):
    """One window with a tab per open file (a file already open is brought forward, not opened twice)."""
    message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        from PySide6.QtWidgets import QTabWidget
        self.setWindowTitle("BlamixShell editor")
        self.resize(1000, 720)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(lambda _i: self._retitle())
        lay.addWidget(self.tabs)

    def open(self, session, path: str) -> EditorTab:
        for i in range(self.tabs.count()):
            t = self.tabs.widget(i)
            if t.session is session and t.path == path:
                self.tabs.setCurrentIndex(i)
                self._front()
                return t
        tab = EditorTab(session, path)
        tab.title_changed.connect(lambda t=tab: self._tab_changed(t))
        tab.message.connect(self.message.emit)
        i = self.tabs.addTab(tab, icon("file"), tab.tab_title())
        self.tabs.setTabToolTip(i, f"{session.server.label}: {path}")
        self.tabs.setCurrentIndex(i)
        self._front()
        return tab

    def _front(self) -> None:
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tab_changed(self, tab: EditorTab) -> None:
        i = self.tabs.indexOf(tab)
        if i >= 0:
            self.tabs.setTabText(i, tab.tab_title())
        self._retitle()

    def _retitle(self) -> None:
        t = self.tabs.currentWidget()
        self.setWindowTitle(f"{t.tab_title()} · {t.session.server.label} · BlamixShell" if t else "BlamixShell editor")

    def _can_drop(self, tab: EditorTab) -> bool:
        if not tab.dirty:
            return True
        r = QMessageBox.question(self, "Unsaved changes", f"{tab.name} has changes that aren't saved. Close it anyway?",
                                 QMessageBox.Discard | QMessageBox.Cancel)
        return r == QMessageBox.Discard

    def close_tab(self, i: int) -> None:
        tab = self.tabs.widget(i)
        if tab is not None and self._can_drop(tab):
            self.tabs.removeTab(i)
            tab.deleteLater()
            if not self.tabs.count():
                self.hide()

    def closeEvent(self, e):  # noqa: N802
        for i in range(self.tabs.count()):
            if not self._can_drop(self.tabs.widget(i)):
                e.ignore()
                return
        while self.tabs.count():
            w = self.tabs.widget(0)
            self.tabs.removeTab(0)
            w.deleteLater()
        e.accept()
