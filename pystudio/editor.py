"""Code editor: line numbers, folding, smart indent, auto-close, bracket matching,
and IntelliSense (completion, parameter info, hover info, live error squiggles)."""
import os
import re

from PySide6.QtCore import QPointF, QEvent, QPoint, QRect, QSize, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QIcon, QPainter, QPixmap, QPolygon,
                           QStandardItem, QStandardItemModel, QTextBlockUserData,
                           QTextCharFormat, QTextCursor, QTextDocument, QTextFormat)
from PySide6.QtWidgets import (QCompleter, QMessageBox, QPlainTextEdit, QTextEdit, QToolTip,
                               QWidget)

from . import theme
from .highlighter import PythonHighlighter
from .language import check_source
from . import quickfix, snippets
from .multicaret import MultiCaretMixin

INDENT = "    "  # PEP 8: four spaces
FOLD_W = 14
BP_W = 16          # breakpoint margin (left of the line numbers)
PAIRS = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}
CLOSERS = {")", "]", "}"}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")

KIND_COLORS = {"function": "#b180d7", "class": "#ee9d28", "module": "#75beff",
               "instance": "#75beff", "statement": "#75beff", "param": "#75beff",
               "property": "#d4d4d4", "keyword": "#c586c0", "path": "#cccccc"}
KIND_LETTER = {"function": "ƒ", "class": "C", "module": "M", "instance": "v", "statement": "v",
               "param": "p", "property": "P", "keyword": "K", "path": "/"}
_ICONS = {}


def kind_icon(kind):
    if kind not in _ICONS:
        pm = QPixmap(16, 16)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(KIND_COLORS.get(kind, "#9a9a9a")))
        p.drawRoundedRect(1, 1, 14, 14, 3, 3)
        p.setPen(QColor(theme.c("#1e1e1e")))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(11)
        p.setFont(f)
        p.drawText(QRect(0, 0, 16, 16), Qt.AlignCenter, KIND_LETTER.get(kind, "•"))
        p.end()
        _ICONS[kind] = QIcon(pm)
    return _ICONS[kind]


class FoldData(QTextBlockUserData):
    """Per-line data that moves with the line as you edit: fold state + breakpoint."""

    def __init__(self):
        super().__init__()
        self.folded = False
        self.bp = False
        self.cond = ""
        self.hit = ("", 0)        # hit count rule: ("=" | ">=" | "%", n)
        self.log = ""             # action: message to log (a tracepoint when cont is True)
        self.cont = False         # continue after logging (don't stop)
        self.bm = False           # bookmark (Ctrl+K, Ctrl+K)


class LineNumberArea(QWidget):
    """The gutter: line numbers + fold arrows."""

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)
        if getattr(self, "_drag_arrow", False) and getattr(self, "_drag_y", None) is not None:
            p = QPainter(self)
            p.setPen(QColor(theme.c("#ffcc00")))
            p.drawLine(0, self._drag_y, self.width(), self._drag_y)
            p.end()

    def mouseMoveEvent(self, e):
        if getattr(self, "_drag_arrow", False):
            b = self.editor._block_at_y(int(e.position().y()))
            if b is not None:
                self._drag_y = round(self.editor.blockBoundingGeometry(b).translated(
                    self.editor.contentOffset()).top())
                self.update()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if getattr(self, "_drag_arrow", False):
            self._drag_arrow, self._drag_y = False, None
            self.unsetCursor()
            self.update()
            b = self.editor._block_at_y(int(e.position().y()))
            if b is not None and b.blockNumber() + 1 != self.editor.exec_line:
                self.editor.set_next_requested.emit(b.blockNumber() + 1)
            return
        super().mouseReleaseEvent(e)

    def mousePressEvent(self, e):
        x, y = e.position().x(), int(e.position().y())
        ed = self.editor
        if e.button() == Qt.LeftButton and x < BP_W and ed.exec_line:
            b = ed._block_at_y(y)
            if b is not None and b.blockNumber() + 1 == ed.exec_line:
                self._drag_arrow = True                 # drag the yellow arrow = Set Next Statement
                self.setCursor(Qt.ClosedHandCursor)
                return
        if x < BP_W:
            if e.button() == Qt.RightButton:
                self.editor.breakpoint_menu_at_y(y, e.globalPosition().toPoint())
            else:
                self.editor.toggle_breakpoint_at_y(y)
        elif x >= self.width() - FOLD_W - 2:
            self.editor.toggle_fold_at_y(y)


class CodeEditor(MultiCaretMixin, QPlainTextEdit):
    dirty_changed = Signal(bool)
    diagnostics_changed = Signal(str, object)   # path, [Diagnostic]
    breakpoints_changed = Signal(str)           # path
    bookmarks_changed = Signal(str)             # path
    lens_clicked = Signal(int, int)             # line, column of the name - CodeLens "N references"
    set_next_requested = Signal(int)            # the yellow arrow was dragged to this line

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        from .editorvisuals import MapScrollBar
        self.setVerticalScrollBar(MapScrollBar(self))    # first, so later connections go to this one
        self.path = path
        self.lang = None          # LanguageService (set by MainWindow)
        self.context = None       # callable -> (project_root, python)
        self.diagnostics = []
        self._own_diags = []      # pyflakes
        self._ext = {}            # "ruff" / "mypy" -> [analysis.Diag], filled in the background
        self._analyzer = None
        self._fold_count = 0
        self._sel_bracket = []
        self._hover_global = None
        self.exec_line = None     # line the debugger is stopped on (1-based)
        self.coverage = None      # (covered lines, not-covered lines) from Analyze Code Coverage
        self._bulb_line = None    # line with a Quick Fix available (lightbulb)
        self._snippet_fields = [] # remaining snippet fields (QTextCursors) for Tab
        self.context_actions = [] # QActions MainWindow adds to the right-click menu
        self.debug_eval = None    # callable(expr, callback) while debugging & paused
        self._bp_count = 0
        self.codelens = None      # callable(name) -> reference count or None (set by MainWindow)
        self.ghost_text = ""      # inline AI suggestion shown in gray at ghost_pos (Tab accepts)
        self.ghost_pos = -1
        self.ghost_kept = False
        self.ghost = None         # GhostCompleter (set by MainWindow)
        self.cursorPositionChanged.connect(self._ghost_cursor_moved)
        self._lens_rects = []     # [(QRect, line, col)]

        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        for family in ("Cascadia Code", "JetBrains Mono", "DejaVu Sans Mono", "Ubuntu Mono", "Monospace"):
            if family in QFontDatabase.families():
                font = QFont(family)
                break
        from .options import editor_font_size
        font.setPointSize(editor_font_size())
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)

        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme.EDITOR_BG}; color: {theme.EDITOR_FG};"
            f" border: 0; selection-background-color: {theme.SELECTION}; }}")
        self.setMouseTracking(True)

        self.highlighter = PythonHighlighter(self.document())

        self.gutter = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._on_cursor_moved)
        self.document().modificationChanged.connect(self.dirty_changed)
        self.document().contentsChange.connect(self._on_contents_change)

        # live error checking, half a second after typing stops
        self._check_timer = QTimer(self, singleShot=True, interval=500)
        self._check_timer.timeout.connect(self.run_diagnostics)
        self._bp_timer = QTimer(self, singleShot=True, interval=300)
        self._bp_timer.timeout.connect(lambda: self.breakpoints_changed.emit(self.path or ""))
        from .editorvisuals import Visuals
        self.visuals = Visuals(self)

        # completion popup
        self._model = QStandardItemModel(self)
        self.completer = QCompleter(self._model, self)
        self.completer.setWidget(self)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setModelSorting(QCompleter.UnsortedModel)
        self.completer.setMaxVisibleItems(12)
        self.completer.activated[str].connect(self.insert_completion)
        self.completer.popup().setStyleSheet(
            "QListView { background: #252526; color: #d4d4d4; border: 1px solid #454545; }"
            "QListView::item:selected { background: #04395e; color: #ffffff; }")
        self._complete_pos = None

        self._update_gutter_width()
        self._refresh_extra()

        if path:
            self.load(path)

    # ------------------------------------------------------------------ files
    @property
    def is_dirty(self):
        return self.document().isModified()

    def display_name(self):
        return os.path.basename(self.path) if self.path else "Untitled"

    def load(self, path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            self.setPlainText(f.read())
        self.path = path
        self.document().setModified(False)
        self._check_timer.start(50)
        self.visuals.mark_saved()
        self.visuals.update_pairs()
        QTimer.singleShot(300, self, lambda: self._analysis().run_mypy())    # dropped if the tab closes first

    def save(self, path=None):
        path = path or self.path
        if not path:
            return False
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.toPlainText())
        except OSError as e:
            QMessageBox.critical(self, "Save failed", f"Could not save {path}:\n{e}")
            return False
        self.path = path
        self.document().setModified(False)
        self.visuals.mark_saved()
        self._analysis().run_mypy()
        return True

    @property
    def project_root(self):
        try:
            return self.context()[0] if self.context else None
        except Exception:  # noqa: BLE001
            return None

    def _analysis(self):
        if self._analyzer is None:
            from .analysis import Analyzer
            self._analyzer = Analyzer(self)
        return self._analyzer

    def set_external_diagnostics(self, kind, diags):
        """Ruff / mypy results (from the background): merged with pyflakes' for squiggles and the Error List."""
        self._ext[kind] = list(diags)
        self._merge_diagnostics()

    def _merge_diagnostics(self):
        merged = list(self._own_diags)
        seen = {(d.line, d.message) for d in merged}
        for kind in ("ruff", "mypy"):
            for d in self._ext.get(kind, []):
                if (d.line, d.message) not in seen:
                    merged.append(d)
        self.diagnostics = merged
        self._refresh_extra()
        self._update_bulb()
        self.gutter.update()
        self.diagnostics_changed.emit(self.path or "", self.diagnostics)

    def go_to_line(self, line, column=0):
        block = self.document().findBlockByNumber(max(0, line - 1))
        if not block.isVisible():
            self.unfold_all()
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor,
                            min(column, max(0, block.length() - 1)))
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()

    def position(self):
        """(line 1-based, column 0-based) of the cursor - Jedi's convention."""
        c = self.textCursor()
        return c.blockNumber() + 1, c.positionInBlock()

    # ------------------------------------------------------------ gutter
    def line_number_width(self):
        digits = max(3, len(str(self.blockCount())))
        return BP_W + 6 + self.fontMetrics().horizontalAdvance("9") * digits + FOLD_W

    def _update_gutter_width(self, _=0):
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "visuals"):
            self.visuals.t_view.start()
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, event):
        painter = QPainter(self.gutter)
        painter.fillRect(event.rect(), QColor(theme.c(theme.GUTTER_BG)))
        current = self.textCursor().blockNumber()
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        h = self.fontMetrics().height()
        err_lines = {d.line for d in self.diagnostics if d.severity == "error"}
        warn_lines = {d.line for d in self.diagnostics if d.severity != "error"}
        num_w = self.gutter.width() - FOLD_W - 4
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                ln = number + 1
                if ln in err_lines or ln in warn_lines:
                    painter.fillRect(BP_W, top + 2, 3, h - 4,
                                     QColor(theme.c("#f14c4c")) if ln in err_lines else QColor(theme.c("#cca700")))
                d = block.userData()
                if isinstance(d, FoldData) and d.bm:
                    painter.setRenderHint(QPainter.Antialiasing)
                    painter.setPen(QColor(theme.c("#9cdcfe")))
                    painter.setBrush(QColor(theme.c("#3b6e9e")))
                    bh = min(12, h - 4)
                    painter.drawRoundedRect(3, top + (h - bh) // 2, 10, bh, 2, 2)
                if isinstance(d, FoldData) and d.bp:
                    painter.setRenderHint(QPainter.Antialiasing)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(theme.c("#e51400")))
                    r = min(12, h - 4)
                    y0 = top + (h - r) // 2
                    if d.log and d.cont:                 # tracepoint: a diamond (VS)
                        cx, cy = 2 + r / 2, y0 + r / 2
                        painter.drawPolygon([QPointF(cx, y0), QPointF(2 + r, cy), QPointF(cx, y0 + r), QPointF(2, cy)])
                    else:
                        painter.drawEllipse(2, y0, r, r)
                    if d.cond or d.hit[0] or (d.log and not d.cont):
                        painter.setPen(QColor(theme.c("#ffffff")))
                        painter.drawText(2, y0, r, r, Qt.AlignCenter, "+")
                if self._bulb_line == ln and not (isinstance(d, FoldData) and d.bp) and self.exec_line != ln:
                    painter.setRenderHint(QPainter.Antialiasing)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(theme.c("#ffcc00")))
                    cy = top + h // 2
                    painter.drawEllipse(4, cy - 6, 9, 9)
                    painter.setBrush(QColor(theme.c("#c8c8c8")))
                    painter.drawRect(6, cy + 3, 5, 3)
                if self.exec_line == ln:
                    painter.setRenderHint(QPainter.Antialiasing)
                    painter.setPen(QColor(theme.c("#1e1e1e")))
                    painter.setBrush(QColor(theme.c("#ffcc00")))
                    cy = top + h // 2
                    painter.drawPolygon(QPolygon([QPoint(3, cy - 4), QPoint(8, cy - 4), QPoint(8, cy - 7),
                                                  QPoint(15, cy), QPoint(8, cy + 7), QPoint(8, cy + 4),
                                                  QPoint(3, cy + 4)]))
                if self.coverage is not None and (ln in self.coverage[0] or ln in self.coverage[1]):
                    painter.fillRect(self.gutter.width() - 3, top, 3, h,
                                     QColor("#2ea043") if ln in self.coverage[0] else QColor("#e5534b"))
                painter.setPen(QColor(theme.c(theme.GUTTER_FG_ACTIVE if number == current else theme.GUTTER_FG)))
                painter.drawText(0, top, num_w, h, Qt.AlignRight, str(ln))
                if self.is_foldable(block):
                    folded = self._is_folded(block)
                    painter.setPen(QColor(theme.c("#c5c5c5")))
                    painter.drawText(num_w + 2, top, FOLD_W, h, Qt.AlignCenter, "▸" if folded else "▾")
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    # ------------------------------------------------------------ folding
    @staticmethod
    def _indent(text):
        return len(text) - len(text.lstrip(" \t"))

    def is_foldable(self, block):
        text = block.text()
        s = text.strip()
        if not s.endswith(":") or s.startswith("#"):
            return False
        ind = self._indent(text)
        nxt = block.next()
        while nxt.isValid() and not nxt.text().strip():
            nxt = nxt.next()
        return nxt.isValid() and self._indent(nxt.text()) > ind

    def _fold_end(self, block):
        """Last block (number) inside the region that starts after this header."""
        ind = self._indent(block.text())
        last = block.blockNumber()
        nxt = block.next()
        while nxt.isValid():
            t = nxt.text()
            if t.strip():
                if self._indent(t) <= ind:
                    break
                last = nxt.blockNumber()
            nxt = nxt.next()
        return last

    def _is_folded(self, block):
        d = block.userData()
        return isinstance(d, FoldData) and d.folded

    def _set_folded(self, block, folded):
        d = block.userData()
        if not isinstance(d, FoldData):
            d = FoldData()
            block.setUserData(d)
        if d.folded != folded:
            self._fold_count += 1 if folded else -1
        d.folded = folded

    def toggle_fold_at_y(self, y):
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid():
            h = round(self.blockBoundingRect(block).height())
            if block.isVisible() and top <= y < top + h:
                if self.is_foldable(block):
                    self._set_folded(block, not self._is_folded(block))
                    self.apply_folds()
                return
            top += h
            block = block.next()

    def toggle_fold(self):
        """Ctrl+M, Ctrl+M: fold/unfold the block the cursor is in."""
        block = self.textCursor().block()
        ind = self._indent(block.text())
        b = block
        while b.isValid():
            if self.is_foldable(b) and (b == block or self._indent(b.text()) < ind or not block.text().strip()):
                if b.blockNumber() <= block.blockNumber() <= self._fold_end(b):
                    self._set_folded(b, not self._is_folded(b))
                    self.apply_folds()
                    if not self.textCursor().block().isVisible():
                        c = QTextCursor(b)
                        c.movePosition(QTextCursor.EndOfBlock)
                        self.setTextCursor(c)
                    return
            b = b.previous()

    def fold_all(self):
        b = self.document().firstBlock()
        while b.isValid():
            if self.is_foldable(b) and self._indent(b.text()) <= 4:
                self._set_folded(b, True)
            b = b.next()
        self.apply_folds()
        c = self.textCursor()
        if not c.block().isVisible():
            self.moveCursor(QTextCursor.Start)

    def unfold_all(self):
        b = self.document().firstBlock()
        while b.isValid():
            if self._is_folded(b):
                self._set_folded(b, False)
            b = b.next()
        self._fold_count = 0
        self.apply_folds()

    def apply_folds(self):
        doc = self.document()
        b = doc.firstBlock()
        hide_until = -1
        changed = False
        while b.isValid():
            n = b.blockNumber()
            visible = n > hide_until
            if b.isVisible() != visible:
                b.setVisible(visible)
                changed = True
            if visible and self._is_folded(b):
                if self.is_foldable(b):
                    hide_until = max(hide_until, self._fold_end(b))
                else:
                    self._set_folded(b, False)
            b = b.next()
        if changed:
            doc.markContentsDirty(0, doc.characterCount())
            self.viewport().update()
        self.gutter.update()

    # ------------------------------------------------------------ breakpoints
    def _block_at_y(self, y):
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid():
            h = round(self.blockBoundingRect(block).height())
            if block.isVisible() and top <= y < top + h:
                return block
            top += h
            block = block.next()
        return None

    def _data(self, block):
        d = block.userData()
        if not isinstance(d, FoldData):
            d = FoldData()
            block.setUserData(d)
        return d

    def toggle_breakpoint(self, block=None, on=None):
        """F9 / click in the margin."""
        block = block or self.textCursor().block()
        d = self._data(block)
        d.bp = (not d.bp) if on is None else on
        if not d.bp:
            d.cond, d.hit, d.log, d.cont = "", ("", 0), "", False
        self._bp_count = len(self.breakpoints())
        self.gutter.update()
        self._refresh_extra()
        self.breakpoints_changed.emit(self.path or "")

    def toggle_breakpoint_at_y(self, y):
        b = self._block_at_y(y)
        if b is not None:
            d = b.userData()
            has_bp = isinstance(d, FoldData) and d.bp
            if self._bulb_line == b.blockNumber() + 1 and not has_bp:
                self.setTextCursor(QTextCursor(b))
                self.show_quick_fixes()          # click the lightbulb
                return
            self.toggle_breakpoint(b)

    def breakpoint_menu_at_y(self, y, global_pos):
        from PySide6.QtWidgets import QMenu
        b = self._block_at_y(y)
        if b is None:
            return
        d = self._data(b)
        m = QMenu(self)
        if d.bp:
            m.addAction("Delete Breakpoint", lambda: self.toggle_breakpoint(b, False))
            m.addSeparator()
            m.addAction("Conditions...", lambda: self.breakpoint_settings(b))
            m.addAction("Actions...", lambda: self.breakpoint_settings(b, focus="actions"))
        else:
            m.addAction("Insert Breakpoint", lambda: self.toggle_breakpoint(b, True))
            m.addAction("Insert Tracepoint", lambda: self.insert_tracepoint(b))
        m.exec(global_pos)

    def breakpoint_settings(self, block=None, focus="conditions"):
        """VS's Breakpoint Settings: condition, hit count, and actions (log a message, continue)."""
        from .bpsettings import BreakpointSettingsDialog
        block = block or self.textCursor().block()
        d = self._data(block)
        dlg = BreakpointSettingsDialog(d.cond, d.hit, d.log, d.cont, block.blockNumber() + 1, focus, self)
        if dlg.exec():
            d.bp = True
            d.cond, d.hit, d.log, d.cont = dlg.values()
            self._bp_count = len(self.breakpoints())
            self.gutter.update()
            self._refresh_extra()
            self.breakpoints_changed.emit(self.path or "")

    def insert_tracepoint(self, block=None):
        """A breakpoint that logs a message to Output and keeps going."""
        block = block or self.textCursor().block()
        d = self._data(block)
        if not d.bp:
            d.log, d.cont = "$FUNCTION: line $LINE", True
        self.breakpoint_settings(block, focus="actions")

    # ------------------------------------------------------------ bookmarks
    def toggle_bookmark(self, block=None):
        block = block or self.textCursor().block()
        d = self._data(block)
        d.bm = not d.bm
        self.gutter.update()
        self.bookmarks_changed.emit(self.path or "")

    def bookmarks(self):
        """[line, ...] - 1-based."""
        out = []
        b = self.document().firstBlock()
        while b.isValid():
            d = b.userData()
            if isinstance(d, FoldData) and d.bm:
                out.append(b.blockNumber() + 1)
            b = b.next()
        return out

    def set_bookmarks(self, lines):
        for ln in lines:
            b = self.document().findBlockByNumber(int(ln) - 1)
            if b.isValid():
                self._data(b).bm = True
        self.gutter.update()

    def clear_bookmarks(self):
        b = self.document().firstBlock()
        while b.isValid():
            d = b.userData()
            if isinstance(d, FoldData):
                d.bm = False
            b = b.next()
        self.gutter.update()
        self.bookmarks_changed.emit(self.path or "")

    def breakpoints(self):
        """[{"line": n, "condition": str}] - 1-based lines."""
        out = []
        b = self.document().firstBlock()
        while b.isValid():
            d = b.userData()
            if isinstance(d, FoldData) and d.bp:
                bp = {"line": b.blockNumber() + 1, "condition": d.cond}
                if d.hit[0] and d.hit[1]:
                    bp.update({"hitMode": d.hit[0], "hitCount": d.hit[1]})
                if d.log:
                    bp["log"] = d.log
                if d.cont:
                    bp["continue"] = True
                out.append(bp)
            b = b.next()
        return out

    def set_breakpoints(self, bps):
        for bp in bps:
            b = self.document().findBlockByNumber(int(bp["line"]) - 1)
            if b.isValid():
                d = self._data(b)
                d.bp = True
                d.cond = bp.get("condition", "")
                d.hit = (bp.get("hitMode", ""), int(bp.get("hitCount", 0) or 0))
                d.log = bp.get("log", "")
                d.cont = bool(bp.get("continue"))
        self._bp_count = len(self.breakpoints())
        self.gutter.update()
        self._refresh_extra()

    def clear_breakpoints(self):
        b = self.document().firstBlock()
        while b.isValid():
            d = b.userData()
            if isinstance(d, FoldData):
                d.bp = False
                d.cond, d.hit, d.log, d.cont = "", ("", 0), "", False
            b = b.next()
        self._bp_count = 0
        self.gutter.update()
        self._refresh_extra()
        self.breakpoints_changed.emit(self.path or "")

    def set_coverage(self, covered, missing):
        """Code coverage coloring (None, None clears it). Editing the file clears it - the lines move."""
        self.coverage = None if covered is None else (set(covered), set(missing or ()))
        if self.coverage is not None and not getattr(self, "_cov_hooked", False):
            self._cov_hooked = True
            self.document().contentsChange.connect(self._coverage_edit)
        self.gutter.update()
        self._refresh_extra()

    def _coverage_edit(self, _pos, removed, added):
        if self.coverage is not None and (removed or added):
            self.coverage = None
            self.gutter.update()
            self._refresh_extra()

    def set_exec_line(self, line):
        """Show (or clear with None) the yellow 'next statement' marker."""
        self.exec_line = line
        if line:
            block = self.document().findBlockByNumber(line - 1)
            if not block.isVisible():
                self.unfold_all()
            self.setTextCursor(QTextCursor(block))
            self.centerCursor()
        self.gutter.update()
        self._refresh_extra()

    # ------------------------------------------------------------ highlights
    def _on_cursor_moved(self):
        if self._fold_count and not self.textCursor().block().isVisible():
            self.unfold_all()
        self._update_bulb()
        self._match_brackets()
        self._refresh_extra()
        self.gutter.update()

    def _match_brackets(self):
        self._sel_bracket = []
        c = self.textCursor()
        text = c.block().text()
        pos = c.positionInBlock()
        openers, closers = "([{", ")]}"
        for idx in (pos - 1, pos):
            if 0 <= idx < len(text) and text[idx] in openers + closers:
                ch = text[idx]
                abs_pos = c.block().position() + idx
                other = self._find_match(abs_pos, ch)
                if other is not None:
                    fmt = QTextCharFormat()
                    fmt.setBackground(QColor(theme.c("#3a3d41")))
                    fmt.setForeground(QColor(theme.c("#ffd700")))
                    for p in (abs_pos, other):
                        s = QTextEdit.ExtraSelection()
                        s.format = fmt
                        s.cursor = QTextCursor(self.document())
                        s.cursor.setPosition(p)
                        s.cursor.setPosition(p + 1, QTextCursor.KeepAnchor)
                        self._sel_bracket.append(s)
                return

    def _find_match(self, pos, ch, limit=20000):
        doc = self.document()
        pairs = {"(": ")", "[": "]", "{": "}"}
        rev = {v: k for k, v in pairs.items()}
        if ch in pairs:
            want, step, depth, p = pairs[ch], 1, 0, pos
        else:
            want, step, depth, p = rev[ch], -1, 0, pos
        n = 0
        while 0 <= p < doc.characterCount() and n < limit:
            c = doc.characterAt(p)
            if c == ch:
                depth += 1
            elif c == want:
                depth -= 1
                if depth == 0:
                    return p
            p += step
            n += 1
        return None

    def _refresh_extra(self):
        sels = []
        doc = self.document()
        if self.coverage is not None:
            for lines, color in ((self.coverage[0], QColor(46, 160, 67, 38)), (self.coverage[1], QColor(229, 83, 75, 46))):
                for ln in lines:
                    b = doc.findBlockByNumber(ln - 1)
                    if b.isValid():
                        s = QTextEdit.ExtraSelection()
                        s.format.setBackground(color)
                        s.format.setProperty(QTextFormat.FullWidthSelection, True)
                        s.cursor = QTextCursor(b)
                        sels.append(s)
        line = QTextEdit.ExtraSelection()
        line.format.setBackground(QColor(theme.c(theme.CURRENT_LINE)))
        line.format.setProperty(QTextFormat.FullWidthSelection, True)
        line.cursor = self.textCursor()
        line.cursor.clearSelection()
        sels.append(line)
        doc = self.document()
        if self._bp_count:
            for bp in self.breakpoints():
                s = QTextEdit.ExtraSelection()
                s.format.setBackground(QColor(theme.c("#5a1d1d")))
                s.format.setProperty(QTextFormat.FullWidthSelection, True)
                s.cursor = QTextCursor(doc.findBlockByNumber(bp["line"] - 1))
                sels.append(s)
        if self.exec_line:
            s = QTextEdit.ExtraSelection()
            s.format.setBackground(QColor(theme.c("#4b4b18")))
            s.format.setProperty(QTextFormat.FullWidthSelection, True)
            s.cursor = QTextCursor(doc.findBlockByNumber(self.exec_line - 1))
            sels.append(s)
        for d in self.diagnostics:
            block = doc.findBlockByNumber(d.line - 1)
            if not block.isValid():
                continue
            text = block.text()
            col = min(d.col, max(0, len(text) - 1))
            m = re.match(r"[A-Za-z_0-9.]+", text[col:])
            length = len(m.group(0)) if m else 1
            if not text.strip():
                continue
            if d.code == "SyntaxError" and length <= 1:   # underline the rest of the line
                length = max(1, len(text.rstrip()) - col)
            s = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setUnderlineStyle(QTextCharFormat.WaveUnderline)
            fmt.setUnderlineColor(QColor(theme.c("#f14c4c")) if d.severity == "error" else QColor(theme.c("#4fb34f")))
            s.format = fmt
            s.cursor = QTextCursor(block)
            s.cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, col)
            s.cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, max(1, min(length, len(text) - col)))
            sels.append(s)
        sels += getattr(self, "_sel_pairs", [])
        sels += self._sel_bracket
        sels += self.mc_selections()       # the other carets' selections
        self.setExtraSelections(sels)
        self.verticalScrollBar().update()  # the scrollbar map

    # ------------------------------------------------------------ diagnostics
    def _on_contents_change(self, pos, removed, added):
        if self._fold_count:
            self.apply_folds()
        if self._bp_count:
            self._bp_count = len(self.breakpoints())
            self._bp_timer.start()       # lines moved: tell the debugger
        self._check_timer.start()

    def run_diagnostics(self):
        if self.path and not self.path.endswith((".py", ".pyw")):
            return
        try:
            self._own_diags = check_source(self.toPlainText(), self.path or "")
        except Exception:  # noqa: BLE001
            self._own_diags = []
        self._merge_diagnostics()
        try:
            self._analysis().run_ruff()          # adds its findings when it finishes
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ Quick Fix
    def _fixes_on_line(self, line):
        out = []
        src = self.toPlainText()
        from . import analysis
        for d in self.diagnostics:
            if d.line == line:
                try:
                    out += quickfix.fixes_for(d, src) + analysis.fixes_for(d, src)
                except Exception:  # noqa: BLE001
                    pass
        return out

    def _update_bulb(self):
        line = self.textCursor().blockNumber() + 1
        has = any(d.line == line for d in self.diagnostics) and bool(self._fixes_on_line(line))
        new = line if has else None
        if new != self._bulb_line:
            self._bulb_line = new
            self.gutter.update()

    def show_quick_fixes(self):
        """Ctrl+. - like VS's lightbulb menu."""
        from PySide6.QtWidgets import QMenu
        line = self.textCursor().blockNumber() + 1
        fixes = self._fixes_on_line(line)
        pos = self.mapToGlobal(self.cursorRect().bottomLeft() + QPoint(self.viewportMargins().left(), 2))
        if not fixes:
            QToolTip.showText(pos, "No quick fixes here.", self)
            return
        m = QMenu(self)
        for title, new_src in fixes:
            m.addAction(title, lambda s=new_src: self.apply_text(s))      # keeps breakpoints and folds
        m.exec(pos)

    def replace_all_text(self, text):
        """Replace the document (one undo step), keeping the cursor on the same line/column."""
        line, col = self.position()
        delta = text.count("\n") - self.toPlainText().count("\n")
        c = self.textCursor()
        c.beginEditBlock()
        c.select(QTextCursor.Document)
        c.insertText(text)
        c.endEditBlock()
        # keep the cursor on 'the same' line (imports added above shift it down)
        target = line + (delta if delta > 0 and line > 1 else 0)
        self.go_to_line(max(1, target), col)

    @staticmethod
    def _line_ops(old_lines, new_lines):
        """Line edits old -> new: ("equal"|"line"|"delete"|"insert"|"replace", i1, i2, j1, j2).
        Lines are matched ignoring spaces (and blank lines can't anchor a match), so a line that
        a formatter only re-spaced is edited in place ("line") and keeps its breakpoint/fold."""
        import difflib
        norm_old = ["".join(x.split()) for x in old_lines]
        norm_new = ["".join(x.split()) for x in new_lines]
        sm = difflib.SequenceMatcher(lambda x: x == "", norm_old, norm_new, autojunk=False)
        out = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
                for k in range(i2 - i1):
                    out.append(("line", i1 + k, i1 + k + 1, j1 + k, j1 + k + 1))
            else:
                out.append((tag, i1, i2, j1, j2))
        return out

    def apply_text(self, new_text):
        """Change the document to new_text by replacing only the lines that differ (one undo
        step), so breakpoints, folds and the caret stay with their lines."""
        old_lines = self.toPlainText().split("\n")
        new_lines = new_text.split("\n")
        if old_lines == new_lines:
            return False
        doc = self.document()
        c = QTextCursor(doc)
        c.beginEditBlock()
        for tag, i1, i2, j1, j2 in reversed(self._line_ops(old_lines, new_lines)):
            if tag == "equal" or (tag == "line" and old_lines[i1] == new_lines[j1]):
                continue
            repl = "\n".join(new_lines[j1:j2])
            if i1 < i2:          # replace / delete lines i1..i2-1 (a "line" edit keeps its block)
                start = doc.findBlockByNumber(i1)
                end = doc.findBlockByNumber(i2 - 1)
                c.setPosition(start.position())
                c.setPosition(end.position() + end.length() - 1, QTextCursor.KeepAnchor)
                if tag == "delete":   # take a line break with the deleted lines
                    if end.next().isValid():
                        c.setPosition(end.next().position(), QTextCursor.KeepAnchor)
                    elif start.previous().isValid():
                        c.setPosition(start.previous().position() + start.previous().length() - 1)
                        c.setPosition(end.position() + end.length() - 1, QTextCursor.KeepAnchor)
                    c.removeSelectedText()
                else:
                    c.insertText(repl)
            else:                # insert before line i1
                if i1 == 0:
                    # Qt keeps a block's data (breakpoint, fold) with the text before a new line
                    # break, so move line 0's data down to where its text ends up
                    first = doc.firstBlock()
                    data = first.userData()
                    c.setPosition(0)
                    c.insertText(repl + "\n")
                    if isinstance(data, FoldData):
                        moved = FoldData()
                        moved.folded, moved.bp, moved.cond, moved.bm = data.folded, data.bp, data.cond, data.bm
                        moved.hit, moved.log, moved.cont = data.hit, data.log, data.cont
                        doc.findBlockByNumber(j2 - j1).setUserData(moved)
                        doc.firstBlock().setUserData(FoldData())
                elif i1 <= doc.blockCount():
                    prev = doc.findBlockByNumber(i1 - 1)     # end of the line before: keeps blocks intact
                    c.setPosition(prev.position() + prev.length() - 1)
                    c.insertText("\n" + repl)
                else:
                    c.movePosition(QTextCursor.End)
                    c.insertText("\n" + repl)
        c.endEditBlock()
        self._bp_count = len(self.breakpoints())
        self.gutter.update()
        return True

    # ------------------------------------------------------------ snippets
    def try_expand_snippet(self):
        c = self.textCursor()
        if c.hasSelection():
            return False
        word = self._word_before_cursor()
        if word not in snippets.SNIPPETS:
            return False
        before = c.block().text()[:c.positionInBlock() - len(word)]
        if before.strip() and not before.rstrip().endswith(("=", "(", ",", ":")):
            if snippets.SNIPPETS[word][1].count("\n"):   # block snippets need their own line
                return False
        indent = before if not before.strip() else before[:len(before) - len(before.lstrip())]
        text, fields = snippets.expand(word, indent)
        c.beginEditBlock()
        c.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, len(word))
        start = c.selectionStart()
        c.insertText(text)
        c.endEditBlock()
        self._snippet_fields = []
        for _order, s0, e0 in fields:
            fc = QTextCursor(self.document())
            fc.setPosition(start + s0)
            fc.setPosition(start + e0, QTextCursor.KeepAnchor)
            self._snippet_fields.append(fc)
        self.next_snippet_field()
        return True

    def next_snippet_field(self):
        if not self._snippet_fields:
            return False
        fc = self._snippet_fields.pop(0)
        self.setTextCursor(fc)
        return True

    def contextMenuEvent(self, e):
        menu = self.createStandardContextMenu()
        first = menu.actions()[0] if menu.actions() else None
        cur = self.cursorForPosition(e.pos())
        if not self.textCursor().hasSelection():
            self.setTextCursor(cur)
        for a in self.context_actions:
            if a is None:
                menu.insertSeparator(first)
            else:
                menu.insertAction(first, a)
        if self.context_actions:
            menu.insertSeparator(first)
        menu.exec(e.globalPos())

    def diagnostic_at(self, line, col):
        best = None
        for d in self.diagnostics:
            if d.line == line:
                best = best or d
                if d.col <= col:
                    best = d
        return best

    # ------------------------------------------------------------ IntelliSense
    def _ctx(self):
        return self.context() if self.context else (os.path.dirname(self.path or ""), None)

    def _word_before_cursor(self):
        c = self.textCursor()
        text = c.block().text()[:c.positionInBlock()]
        m = IDENT.search(text)
        return m.group(0) if m else ""

    def _in_string_or_comment(self):
        c = self.textCursor()
        before = c.block().text()[:c.positionInBlock()]
        if "#" in before:
            # crude: a # not inside quotes starts a comment
            q = None
            for ch in before:
                if q:
                    if ch == q:
                        q = None
                elif ch in "\"'":
                    q = ch
                elif ch == "#":
                    return True
        return before.count('"') % 2 == 1 or before.count("'") % 2 == 1

    def request_completion(self, explicit=False):
        if self.lang is None or not self.lang.available:
            return
        if not explicit and self._in_string_or_comment():
            return
        line, col = self.position()
        self._complete_pos = (line, col - len(self._word_before_cursor()))
        self.lang.request("complete", self.toPlainText(), self.path, line, col, self._ctx(),
                          lambda res, req=(line, col): self._show_completions(res, req))

    def _show_completions(self, items, req):
        line, col = self.position()
        if line != req[0] or col < self._complete_pos[1] or not self.hasFocus():
            return
        prefix = self._word_before_cursor()
        show_private = prefix.startswith("_")
        self._model.clear()
        public, private = [], []
        for name, kind, _sym in items:
            if name.startswith("__") and not prefix.startswith("__"):
                continue
            (private if name.startswith("_") else public).append((name, kind))
        for name, kind in public + (private if show_private else []):
            it = QStandardItem(kind_icon(kind), name)
            it.setToolTip(kind)
            self._model.appendRow(it)
        if self._model.rowCount() == 0:
            self.completer.popup().hide()
            return
        self.completer.setCompletionPrefix(prefix)
        if self.completer.completionCount() == 0:
            self.completer.popup().hide()
            return
        rect = self.cursorRect()
        rect.moveLeft(rect.left() + self.viewportMargins().left() -
                      self.fontMetrics().horizontalAdvance(prefix))
        rect.setWidth(340)
        self.completer.popup().setCurrentIndex(self.completer.completionModel().index(0, 0))
        self.completer.complete(rect)

    def insert_completion(self, text):
        prefix = self._word_before_cursor()
        c = self.textCursor()
        c.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, len(prefix))
        c.insertText(text)
        self.setTextCursor(c)
        self.completer.popup().hide()

    def request_completion_explicit(self):
        self.request_completion(explicit=True)

    def request_signature(self):
        if self.lang is None or not self.lang.available or self._in_string_or_comment():
            return
        line, col = self.position()
        self.lang.request("signatures", self.toPlainText(), self.path, line, col, self._ctx(),
                          lambda res, req=(line, col): self._show_signature(res, req))

    def _show_signature(self, sigs, req):
        if not sigs or self.position()[0] != req[0] or not self.hasFocus():
            QToolTip.hideText()
            return
        name, params, index, doc = sigs[0]
        shown = []
        for i, p in enumerate(params):
            p = p.replace("&", "&amp;").replace("<", "&lt;")
            shown.append(f"<b><u>{p}</u></b>" if i == index else p)
        html = f"<code>{name}({', '.join(shown)})</code>"
        if doc:
            first = doc.split("\n\n")[0].replace("<", "&lt;")[:300]
            html += f"<br><span style='color:#9a9a9a'>{first}</span>"
        pos = self.mapToGlobal(self.cursorRect().topLeft() + QPoint(self.viewportMargins().left(), -42))
        QToolTip.showText(pos, html, self)

    # ------------------------------------------------------------ hover
    def event(self, e):
        if e.type() == QEvent.ToolTip:
            self._hover(e.pos(), e.globalPos())
            return True
        return super().event(e)

    def _hover(self, pos, global_pos):
        vp = self.viewport().mapFrom(self, pos)
        if vp.x() < 0:
            QToolTip.hideText()
            return
        c = self.cursorForPosition(vp)
        line, col = c.blockNumber() + 1, c.positionInBlock()
        text = c.block().text()
        if self.debug_eval is not None:
            expr = self._expr_at(text, col)
            if expr:
                self._hover_global = global_pos
                self.debug_eval(expr, lambda res, gp=global_pos, e=expr: self._show_value(res, gp, e))
                return
        d = self.diagnostic_at(line, col)
        if d and abs(d.col - col) <= max(3, len(text)):
            word_end = re.match(r"[A-Za-z_0-9.]*", text[d.col:]).end()
            if d.col <= col <= d.col + max(1, word_end) or d.code == "SyntaxError":
                color = "#f14c4c" if d.severity == "error" else "#4fb34f"
                QToolTip.showText(global_pos, f"<span style='color:{color}'>●</span> {d.message}", self)
                return
        if col >= len(text) or not (text[col].isalnum() or text[col] == "_"):
            QToolTip.hideText()
            return
        if self.lang is None or not self.lang.available:
            return
        self._hover_global = global_pos
        self.lang.request("hover", self.toPlainText(), self.path, line, col, self._ctx(),
                          lambda res, gp=global_pos: self._show_hover(res, gp))

    @staticmethod
    def _expr_at(text, col):
        """Dotted name under the mouse, e.g. self.ui.label1 when hovering 'label1'."""
        if col >= len(text) or not (text[col].isalnum() or text[col] == "_"):
            return ""
        start = col
        while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_."):
            start -= 1
        end = col
        while end < len(text) and (text[end].isalnum() or text[end] == "_"):
            end += 1
        expr = text[start:end].strip(".")
        return expr if expr and not expr[0].isdigit() else ""

    def _show_value(self, res, gp, expr):
        if self._hover_global != gp or not res or res.get("type") == "error":
            return
        val = res.get("value", "").replace("&", "&amp;").replace("<", "&lt;")
        QToolTip.showText(gp, f"<code><b>{expr}</b> = {val}</code>"
                              f"<br><span style='color:#4ec9b0'>{res.get('type', '')}</span>", self)

    def _show_hover(self, info, gp):
        if not info or self._hover_global != gp:
            return
        head = info["head"].replace("<", "&lt;")
        typ = f" &nbsp;<span style='color:#4ec9b0'>{info['type']}</span>" if info.get("type") else ""
        doc = info.get("doc", "")
        lines = doc.split("\n")[:14]
        doc_html = "<br>".join(l.replace("&", "&amp;").replace("<", "&lt;") for l in lines)
        if len(doc) > 900:
            doc_html = doc_html[:900] + "…"
        html = f"<code>{head}</code>{typ}"
        if doc_html:
            html += f"<hr><span style='color:#cccccc'>{doc_html}</span>"
        QToolTip.showText(gp, html, self)

    def mouseMoveEvent(self, e):
        self._hover_global = None
        if self._lens_rects and not e.buttons():
            pos = e.position().toPoint()
            on_lens = any(r.contains(pos) for r, _l, _c in self._lens_rects)
            self.viewport().setCursor(Qt.PointingHandCursor if on_lens else Qt.IBeamCursor)
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not e.modifiers():
            pos = e.position().toPoint()
            for r, line, col in self._lens_rects:
                if r.contains(pos):
                    self.lens_clicked.emit(line, col)
                    return
        super().mousePressEvent(e)

    _LENS_RE = re.compile(r"^(\s*)(?:async\s+)?(def|class)\s+(\w+)")

    # ------------------------------------------------------------ inline suggestions (ghost text)
    def show_ghost(self, text):
        self.ghost_text, self.ghost_pos, self.ghost_kept = text, self.textCursor().position(), False
        self.viewport().update()

    def clear_ghost(self):
        if self.ghost_text:
            self.ghost_text, self.ghost_pos = "", -1
            self.viewport().update()

    def _ghost_cursor_moved(self):
        if self.ghost_text and self.textCursor().position() != self.ghost_pos:
            self.clear_ghost()

    def _ghost_insert(self, text):
        rest = self.ghost_text[len(text):]
        c = self.textCursor()
        c.beginEditBlock()
        c.insertText(text)
        c.endEditBlock()
        self.setTextCursor(c)
        if rest:
            self.ghost_text, self.ghost_pos, self.ghost_kept = rest, c.position(), True
        else:
            self.clear_ghost()
        self.viewport().update()

    def ghost_key(self, event):
        """Tab accepts, Ctrl+Right accepts a word, Esc dismisses. Returns True if the key was used."""
        if not self.ghost_text or self.textCursor().position() != self.ghost_pos:
            return False
        key, mods = event.key(), event.modifiers()
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr, Qt.Key_CapsLock):
            return False                       # Ctrl on its own (on the way to Ctrl+Right) keeps it
        if key == Qt.Key_Tab and not (mods & (Qt.ControlModifier | Qt.ShiftModifier)):
            self._ghost_insert(self.ghost_text)
            return True
        if key == Qt.Key_Right and mods & Qt.ControlModifier:
            m = re.match(r"\s*(\w+|[^\w\s]+)|\s+", self.ghost_text)
            self._ghost_insert(m.group(0) if m else self.ghost_text[:1])
            return True
        if key == Qt.Key_Escape:
            self.clear_ghost()
            return True
        t = event.text()
        if t and len(t) == 1 and t.isprintable() and self.ghost_text.startswith(t) and not mods & Qt.ControlModifier:
            self.ghost_text, self.ghost_kept = self.ghost_text[1:], True     # typed what it suggested
            self.ghost_pos += 1
            if not self.ghost_text:
                self.ghost_kept = False
            return False
        self.clear_ghost()
        return False

    def _paint_ghost(self, p):
        c = QTextCursor(self.document())
        c.setPosition(min(self.ghost_pos, self.document().characterCount() - 1))
        r = self.cursorRect(c)
        f = QFont(self.font())
        f.setItalic(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        lines = self.ghost_text.split("\n")
        p.setPen(QColor(theme.c("#7f848e")))
        p.drawText(r.right() + 1, r.top() + fm.ascent(), lines[0])
        if len(lines) > 1:
            x0 = round(self.document().documentMargin() + self.contentOffset().x())
            lh = fm.height()
            top = r.top() + r.height()
            p.fillRect(0, top, self.viewport().width(), lh * (len(lines) - 1) + 2, QColor(theme.c(theme.EDITOR_BG)))
            for i, ln in enumerate(lines[1:]):
                p.drawText(x0, top + i * lh + fm.ascent(), ln)

    def _paint_ghost_now(self):
        if self.ghost_text:
            gp = QPainter(self.viewport())
            self._paint_ghost(gp)
            gp.end()

    def _paint_line_note(self):
        """Inline blame: gray text after the end of the current line."""
        note = getattr(self, "line_note", None)
        if not note or not note[1]:
            return
        block = self.document().findBlockByNumber(note[0] - 1)
        if not block.isValid() or not block.isVisible():
            return
        geo = self.blockBoundingGeometry(block).translated(self.contentOffset())
        if geo.bottom() < 0 or geo.top() > self.viewport().height():
            return
        end = QTextCursor(block)
        end.movePosition(QTextCursor.EndOfBlock)
        x = self.cursorRect(end).right() + 40
        p = QPainter(self.viewport())
        f = QFont(self.font())
        f.setItalic(True)
        p.setFont(f)
        p.setPen(QColor(theme.c("#6e6e6e")))
        lh = round(self.blockBoundingRect(block).height())
        p.drawText(QRect(int(x), int(geo.top()), max(10, self.viewport().width() - int(x)), lh),
                   Qt.AlignVCenter | Qt.AlignLeft, note[1])
        p.end()

    def paintEvent(self, e):
        super().paintEvent(e)
        self._paint_line_note()
        self._lens_rects = []
        if self.codelens is None:
            self._paint_ghost_now()
            return
        p = QPainter(self.viewport())
        f = QFont(self.font())
        f.setPointSizeF(max(6.0, f.pointSizeF() * 0.82))
        p.setFont(f)
        fm = QFontMetrics(f)
        p.setPen(QColor(theme.c("#8a8a8a")))
        block = self.firstVisibleBlock()
        offset = self.contentOffset()
        bottom = self.viewport().height()
        while block.isValid():
            geo = self.blockBoundingGeometry(block).translated(offset)
            if geo.top() > bottom:
                break
            if block.isVisible():
                m = self._LENS_RE.match(block.text())
                if m and not m.group(3).startswith("__"):
                    n = self.codelens(m.group(3))
                    if n is not None:
                        text = f"{n} reference" + ("" if n == 1 else "s")
                        end = QTextCursor(block)
                        end.movePosition(QTextCursor.EndOfBlock)
                        x = self.cursorRect(end).right() + 28
                        lh = round(self.blockBoundingRect(block).height())
                        r = QRect(int(x), int(geo.top()), fm.horizontalAdvance(text) + 4, lh)
                        p.drawText(r, Qt.AlignVCenter | Qt.AlignLeft, text)
                        self._lens_rects.append((r, block.blockNumber() + 1, m.start(3)))
            block = block.next()
        p.end()
        self._paint_ghost_now()

    # ------------------------------------------------------------ editing keys
    def keyPressEvent(self, event):
        if self.mc_key(event):         # several carets (or adding one)
            self.completer.popup().hide()
            return
        if self.ghost_text and not self.completer.popup().isVisible() and self.ghost_key(event):
            return
        key = event.key()
        mods = event.modifiers()
        text = event.text()
        popup = self.completer.popup()

        # --- completion popup has the keyboard
        if popup.isVisible():
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                idx = popup.currentIndex()
                if idx.isValid():
                    self.insert_completion(idx.data())
                    return
                popup.hide()
            elif key == Qt.Key_Escape:
                popup.hide()
                return
            elif key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
                event.ignore()
                return

        if key == Qt.Key_Space and mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
            self.request_signature()          # Ctrl+Shift+Space: Parameter Info
            return
        if key == Qt.Key_Space and mods & Qt.ControlModifier:
            self.request_completion(explicit=True)   # Ctrl+Space: Complete Word
            return
        if key == Qt.Key_Escape:
            QToolTip.hideText()

        cursor = self.textCursor()

        if key == Qt.Key_Escape and self._snippet_fields:
            self._snippet_fields = []
        if key == Qt.Key_Tab and not (mods & Qt.ControlModifier) and not popup.isVisible():
            if self.next_snippet_field():
                return
            if self.try_expand_snippet():
                return
        if key == Qt.Key_Tab and not (mods & Qt.ControlModifier):
            if cursor.hasSelection():
                self.indent_selection()
            else:
                cursor.insertText(INDENT)
            return

        if key == Qt.Key_Backtab:
            self.unindent_selection()
            return

        if key in (Qt.Key_Return, Qt.Key_Enter):
            QToolTip.hideText()
            self._snippet_fields = []
            line = cursor.block().text()[:cursor.positionInBlock()]
            after = cursor.block().text()[cursor.positionInBlock():]
            indent = line[:len(line) - len(line.lstrip())]
            stripped = line.rstrip()
            words = stripped.split()
            if (stripped.endswith(":") or stripped.endswith(("(", "[", "{"))) and not stripped.lstrip().startswith("#"):
                indent += INDENT
            elif words and words[0] in ("return", "pass", "break", "continue", "raise"):
                if indent.endswith(INDENT):
                    indent = indent[:-len(INDENT)]
            super().keyPressEvent(event)
            self.textCursor().insertText(indent)
            if stripped.endswith(("(", "[", "{")) and after[:1] in CLOSERS:
                # (|) + Enter  ->  (\n    |\n)
                c = self.textCursor()
                pos = c.position()
                c.insertText("\n" + indent[:-len(INDENT)])
                c.setPosition(pos)
                self.setTextCursor(c)
            return

        if key == Qt.Key_Backspace and not cursor.hasSelection():
            before = cursor.block().text()[:cursor.positionInBlock()]
            after = cursor.block().text()[cursor.positionInBlock():]
            if before and after and PAIRS.get(before[-1]) == after[0]:
                # delete an empty auto-closed pair: (|) -> |
                cursor.deletePreviousChar()
                cursor.deleteChar()
                return
            if before and before.strip() == "" and len(before) % 4 == 0:
                for _ in range(4):
                    cursor.deletePreviousChar()
                return

        if key == Qt.Key_Home and not (mods & Qt.ControlModifier):
            text_ = cursor.block().text()
            first = len(text_) - len(text_.lstrip())
            target = 0 if cursor.positionInBlock() == first else first
            mode = QTextCursor.KeepAnchor if mods & Qt.ShiftModifier else QTextCursor.MoveAnchor
            cursor.setPosition(cursor.block().position() + target, mode)
            self.setTextCursor(cursor)
            return

        # --- auto-close brackets and quotes; type over closers
        if text and len(text) == 1 and not (mods & (Qt.ControlModifier | Qt.AltModifier)):
            after = cursor.block().text()[cursor.positionInBlock():]
            before = cursor.block().text()[:cursor.positionInBlock()]
            ch = text
            if (ch in CLOSERS or ch in "\"'") and after[:1] == ch and not cursor.hasSelection():
                if ch in CLOSERS or before.count(ch) % 2 == 1:
                    cursor.movePosition(QTextCursor.Right)
                    self.setTextCursor(cursor)
                    if ch == ")":
                        QToolTip.hideText()
                    return
            if ch in PAIRS:
                if cursor.hasSelection():
                    sel = cursor.selectedText()
                    cursor.insertText(ch + sel + PAIRS[ch])
                    return
                next_ok = not after or after[0] in " \t)]}:,;=" or after[0] in "\"'" and ch not in "\"'"
                if ch in "\"'":
                    prev = before[-1:] if before else ""
                    next_ok = next_ok and not (prev.isalnum() or prev == "_" or prev == ch)
                if next_ok and not self._in_string_or_comment():
                    cursor.insertText(ch + PAIRS[ch])
                    cursor.movePosition(QTextCursor.Left)
                    self.setTextCursor(cursor)
                    if ch == "(":
                        self.request_signature()
                    return

        super().keyPressEvent(event)

        # --- after the key: IntelliSense triggers
        if text == ".":
            self.request_completion()
        elif text in ("(", ","):
            self.request_signature()
        elif text == ")":
            QToolTip.hideText()
        elif text and (text.isalnum() or text == "_"):
            word = self._word_before_cursor()
            if popup.isVisible():
                self.completer.setCompletionPrefix(word)
                if self.completer.completionCount() == 0:
                    popup.hide()
                else:
                    popup.setCurrentIndex(self.completer.completionModel().index(0, 0))
            elif len(word) >= 2 and not word[0].isdigit():
                self.request_completion()
        elif key == Qt.Key_Backspace and popup.isVisible():
            word = self._word_before_cursor()
            if not word:
                popup.hide()
            else:
                self.completer.setCompletionPrefix(word)
        elif text and popup.isVisible():
            popup.hide()

    def focusOutEvent(self, e):
        QToolTip.hideText()
        super().focusOutEvent(e)

    # ------------------------------------------------------------ block edits
    def _selected_blocks(self):
        c = self.textCursor()
        doc = self.document()
        start = doc.findBlock(c.selectionStart())
        end = doc.findBlock(c.selectionEnd())
        if c.hasSelection() and c.selectionEnd() == end.position() and end != start:
            end = end.previous()
        blocks = []
        b = start
        while b.isValid():
            blocks.append(b)
            if b == end:
                break
            b = b.next()
        return blocks

    def _edit_blocks(self, func):
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for b in self._selected_blocks():
            func(b)
        cursor.endEditBlock()

    def indent_selection(self):
        def f(b):
            c = QTextCursor(b)
            c.insertText(INDENT)
        self._edit_blocks(f)

    def unindent_selection(self):
        def f(b):
            text = b.text()
            n = min(4, len(text) - len(text.lstrip(" ")))
            if n:
                c = QTextCursor(b)
                c.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, n)
                c.removeSelectedText()
        self._edit_blocks(f)

    def toggle_comment(self):
        blocks = [b for b in self._selected_blocks() if b.text().strip()]
        if not blocks:
            return
        all_commented = all(b.text().lstrip().startswith("#") for b in blocks)
        col = min(len(b.text()) - len(b.text().lstrip()) for b in blocks)

        def f(b):
            text = b.text()
            if not text.strip():
                return
            c = QTextCursor(b)
            if all_commented:
                i = text.index("#")
                n = 2 if text[i:i + 2] == "# " else 1
                c.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, i)
                c.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, n)
                c.removeSelectedText()
            else:
                c.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, col)
                c.insertText("# ")
        self._edit_blocks(f)

    # ------------------------------------------------------------------- find
    def find_text(self, text, backward=False, case=False, whole=False):
        self.visuals.set_search(text, case, whole)      # Find matches on the scrollbar map
        flags = QTextDocument.FindFlag(0)
        if backward:
            flags |= QTextDocument.FindBackward
        if case:
            flags |= QTextDocument.FindCaseSensitively
        if whole:
            flags |= QTextDocument.FindWholeWords
        if self.find(text, flags):
            return True
        c = self.textCursor()
        c.movePosition(QTextCursor.End if backward else QTextCursor.Start)
        self.setTextCursor(c)
        return self.find(text, flags)

    def replace_current(self, find, replace, case=False):
        c = self.textCursor()
        sel = c.selectedText()
        if sel and (sel == find if case else sel.lower() == find.lower()):
            c.insertText(replace)
        return self.find_text(find, case=case)

    def replace_all(self, find, replace, case=False, whole=False):
        count = 0
        c = self.textCursor()
        c.beginEditBlock()
        c.movePosition(QTextCursor.Start)
        self.setTextCursor(c)
        flags = QTextDocument.FindFlag(0)
        if case:
            flags |= QTextDocument.FindCaseSensitively
        if whole:
            flags |= QTextDocument.FindWholeWords
        while self.find(find, flags):
            self.textCursor().insertText(replace)
            count += 1
        c.endEditBlock()
        return count

    # ------------------------------------------------------------------- zoom
    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self.zoomIn(1) if event.angleDelta().y() > 0 else self.zoomOut(1)
            self._update_gutter_width()
            return
        super().wheelEvent(event)
