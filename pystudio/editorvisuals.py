"""Visual Studio 2022's editor visuals:

Scrollbar map: the vertical scrollbar marks where errors (red), warnings (green), unsaved changes (yellow),
Find matches (orange), breakpoints (dark red) and the debugger's current line are in the whole file, plus a
line for the cursor - click there to jump.
Sticky scroll: the class / def lines you're inside stay pinned at the top while you scroll; click one to
go to it.
Bracket pair colors: matching ( ) [ ] { } get the same color by nesting depth (gold, orchid, blue), so
it's easy to see which closes which.
Tools > Options turns each on or off."""
import difflib
import re

from PySide6.QtCore import QRect, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QScrollBar, QStyle, QStyleOptionSlider, QTextEdit, QWidget

from . import theme


def enabled(name):
    return QSettings().value(name, True, type=bool)


# ============================================================ unsaved changes
def changed_lines(saved_text, text):
    """Line numbers (1-based) that differ from the saved file."""
    if saved_text is None or saved_text == text:
        return set()
    a, b = saved_text.split("\n"), text.split("\n")
    if len(a) > 20000 or len(b) > 20000:
        return set()
    out = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
        elif tag == "delete":
            out.add(max(1, j1))
    return out


# ============================================================ scrollbar map
class MapScrollBar(QScrollBar):
    def __init__(self, editor):
        super().__init__(Qt.Vertical, editor)
        self.ed = editor

    def marks(self):
        """[(line, color, kind)]; kind: 'full' (whole width) or 'side' (right half)."""
        ed = self.ed
        out = []
        for ln in getattr(ed, "_changed", set()):
            out.append((ln, "#c9a227", "left"))
        for ln in getattr(ed, "_search_lines", []):
            out.append((ln, "#d18616", "center"))
        for d in ed.diagnostics:
            out.append((d.line, "#f14c4c" if d.severity == "error" else "#4fb34f", "right"))
        try:
            for bp in ed.breakpoints() if ed._bp_count else []:
                out.append((bp["line"], "#c42b1c", "dot"))
        except Exception:  # noqa: BLE001
            pass
        if ed.exec_line:
            out.append((ed.exec_line, "#e5c400", "full"))
        return out

    def paintEvent(self, e):
        super().paintEvent(e)
        if not enabled("scrollMap"):
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, self)
        if groove.height() < 20:
            return
        total = max(1, self.ed.document().blockCount())
        p = QPainter(self)
        w = self.width()

        def y_of(line):
            return groove.top() + int((line - 1) / total * groove.height())
        for line, color, kind in self.marks():
            y = y_of(line)
            c = QColor(theme.c(color))
            if kind == "left":
                p.fillRect(QRect(1, y, max(2, w // 4), 3), c)
            elif kind == "center":
                p.fillRect(QRect(w // 4 + 1, y, max(2, w // 2 - 2), 3), c)
            elif kind == "right":
                p.fillRect(QRect(w - max(3, w // 4) - 1, y, max(3, w // 4), 3), c)
            elif kind == "dot":
                p.setBrush(c)
                p.setPen(Qt.NoPen)
                p.drawEllipse(QRect(1, y - 1, 5, 5))
            else:
                p.fillRect(QRect(1, y, w - 2, 3), c)
        cur = self.ed.textCursor().blockNumber() + 1
        p.fillRect(QRect(0, y_of(cur), w, 2), QColor(theme.c("#a0a0a0")))
        p.end()


# ============================================================ sticky scroll
SCOPE = re.compile(r"^(\s*)(async\s+def|def|class)\b")
MAX_STICKY = 4


class StickyHeader(QWidget):
    def __init__(self, editor):
        super().__init__(editor.viewport())
        self.ed = editor
        self.lines = []          # block numbers
        self.setCursor(Qt.PointingHandCursor)
        self.hide()

    def scopes(self):
        """Block numbers of the class/def lines around the first visible line (outermost first)."""
        ed = self.ed
        first = ed.firstVisibleBlock()
        if not first.isValid():
            return []
        out = []
        b = first
        text = b.text()
        indent = len(text) - len(text.lstrip()) if text.strip() else 10 ** 6
        # the first visible line itself is part of the scopes only if it's a def (then it's on screen already)
        b = b.previous()
        steps = 0
        while b.isValid() and steps < 5000:
            t = b.text()
            if t.strip():
                ind = len(t) - len(t.lstrip())
                if ind < indent:
                    if SCOPE.match(t):
                        out.append(b.blockNumber())
                    indent = ind
                    if ind == 0:
                        break
            b = b.previous()
            steps += 1
        out.reverse()
        return out[-MAX_STICKY:]

    def refresh(self):
        if not enabled("stickyScroll") or not (self.ed.path or "").endswith((".py", ".pyw")) and self.ed.path:
            self.hide()
            return
        self.lines = self.scopes()
        if not self.lines:
            self.hide()
            return
        lh = self.ed.fontMetrics().height() + 2
        self.setGeometry(0, 0, self.ed.viewport().width(), lh * len(self.lines) + 2)
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        bg = QColor(theme.c("#1e1e1e"))
        p.fillRect(self.rect(), bg)
        p.setFont(self.ed.font())
        fm = self.ed.fontMetrics()
        lh = fm.height() + 2
        x0 = int(self.ed.document().documentMargin()) - self.ed.horizontalScrollBar().value()
        tabw = self.ed.tabStopDistance() or fm.horizontalAdvance(" ") * 4
        kw = QColor(theme.c("#569cd6"))
        name_c = QColor(theme.c("#dcdcaa"))
        cls_c = QColor(theme.c("#4ec9b0"))
        txt = QColor(theme.c("#d4d4d4"))
        for i, bn in enumerate(self.lines):
            t = self.ed.document().findBlockByNumber(bn).text()
            lead = t[:len(t) - len(t.lstrip())]
            x = x0 + sum(tabw if ch == "\t" else fm.horizontalAdvance(" ") for ch in lead)
            y = i * lh + fm.ascent() + 1
            body = t.lstrip()
            m = re.match(r"(async\s+def|def|class)(\s+)(\w+)(.*)", body)
            parts = [(m.group(1), kw), (m.group(2), txt), (m.group(3), cls_c if m.group(1) == "class" else name_c),
                     (m.group(4), txt)] if m else [(body, txt)]
            for s, c in parts:
                p.setPen(c)
                p.drawText(x, y, s)
                x += fm.horizontalAdvance(s)
        p.setPen(QColor(theme.c("#3f3f46")))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()

    def mousePressEvent(self, e):
        lh = self.ed.fontMetrics().height() + 2
        i = int(e.position().y() // lh)
        if 0 <= i < len(self.lines):
            self.ed.go_to_line(self.lines[i] + 1)
            self.ed.setFocus()


# ============================================================ bracket pair colors
_TOKENS = re.compile(r"#[^\n]*|[rRbBuUfF]{0,2}(\"\"\"[\s\S]*?(?:\"\"\"|$)|'''[\s\S]*?(?:'''|$)|\"(?:\\.|[^\"\\\n])*\"?"
                     r"|'(?:\\.|[^'\\\n])*'?)|[()\[\]{}]")
PAIR_COLORS = ("#ffd700", "#da70d6", "#179fff")
LIGHT_COLORS = ("#0431fa", "#319331", "#7b3814")


def bracket_depths(text):
    """[(position, depth)] for every bracket outside strings and comments."""
    out, stack = [], []
    for m in _TOKENS.finditer(text):
        s = m.group(0)
        if len(s) != 1:
            continue
        if s in "([{":
            out.append((m.start(), len(stack)))
            stack.append(s)
        else:
            if stack:
                stack.pop()
            out.append((m.start(), len(stack)))
    return out


class Visuals:
    """Installs the three visuals on one CodeEditor."""

    def __init__(self, ed):
        self.ed = ed
        ed._changed = set()
        ed._search_lines = []
        ed._sel_pairs = []
        self.saved_text = None
        self.pairs = []
        if not isinstance(ed.verticalScrollBar(), MapScrollBar):
            ed.setVerticalScrollBar(MapScrollBar(ed))
        self.sticky = StickyHeader(ed)
        self.t_changes = QTimer(ed, singleShot=True, interval=400)
        self.t_changes.timeout.connect(self.update_changes)
        self.t_pairs = QTimer(ed, singleShot=True, interval=150)
        self.t_pairs.timeout.connect(self.update_pairs)
        self.t_view = QTimer(ed, singleShot=True, interval=30)
        self.t_view.timeout.connect(self.update_view)
        ed.document().contentsChanged.connect(self._changed)
        ed.updateRequest.connect(lambda _r, dy: self.t_view.start() if dy else None)
        ed.horizontalScrollBar().valueChanged.connect(lambda _v: self.sticky.update())
        ed.cursorPositionChanged.connect(lambda: ed.verticalScrollBar().update())

    def _changed(self):
        self.t_changes.start()
        self.t_pairs.start()

    def mark_saved(self):
        self.saved_text = self.ed.toPlainText()
        self.update_changes()

    def update_changes(self):
        try:
            self.ed._changed = changed_lines(self.saved_text, self.ed.toPlainText()) if self.saved_text is not None \
                else set()
            self.ed.verticalScrollBar().update()
        except RuntimeError:
            pass

    def set_search(self, text, case=False, whole=False):
        ed = self.ed
        lines = []
        if text:
            flags = re.IGNORECASE if not case else 0
            pat = re.compile((r"\b%s\b" if whole else "%s") % re.escape(text), flags)
            for i, line in enumerate(ed.toPlainText().split("\n"), 1):
                if pat.search(line):
                    lines.append(i)
                    if len(lines) > 5000:
                        break
        ed._search_lines = lines
        ed.verticalScrollBar().update()

    def update_pairs(self):
        try:
            ed = self.ed
            if not enabled("bracketPairs") or (ed.path and not ed.path.endswith((".py", ".pyw"))):
                self.pairs = []
            else:
                text = ed.toPlainText()
                self.pairs = bracket_depths(text) if len(text) < 2_000_000 else []
            self.update_view()
        except RuntimeError:
            pass

    def update_view(self):
        """Colors for the brackets on screen (only those, so big files stay fast), and the sticky lines."""
        try:
            ed = self.ed
            self.sticky.refresh()
            if not self.pairs:
                if ed._sel_pairs:
                    ed._sel_pairs = []
                    ed._refresh_extra()
                return
            first = ed.firstVisibleBlock()
            start = first.position()
            end_block = ed.cursorForPosition(ed.viewport().rect().bottomRight()).block()
            end = end_block.position() + end_block.length()
            import bisect
            positions = [p for p, _d in self.pairs]
            lo, hi = bisect.bisect_left(positions, start), bisect.bisect_right(positions, end)
            light = theme.saved_theme().lower().startswith("light") if hasattr(theme, "saved_theme") else False
            colors = LIGHT_COLORS if light else PAIR_COLORS
            sels = []
            doc = ed.document()
            for pos, depth in self.pairs[lo:hi]:
                s = QTextEdit.ExtraSelection()
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(colors[depth % 3]))
                s.format = fmt
                c = QTextCursor(doc)
                c.setPosition(pos)
                c.setPosition(pos + 1, QTextCursor.KeepAnchor)
                s.cursor = c
                sels.append(s)
            ed._sel_pairs = sels
            ed._refresh_extra()
        except RuntimeError:
            pass
