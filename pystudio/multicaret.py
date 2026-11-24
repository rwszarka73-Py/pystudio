"""Multiple carets for the code editor (VS 2022 multi-caret editing).

  Ctrl+Alt+Click        add a caret
  Shift+Alt+Click/drag  box (column) selection - Alt+drag alone moves windows in GNOME
  Shift+Alt+Up/Down     add a caret on the line above/below
  Shift+Alt+.           select the next occurrence of the selection/word (adds a caret)
  Shift+Alt+;           select every occurrence
  Esc / plain click     back to one caret

Typing, Backspace, Delete, Enter, Tab, arrows, Home/End (with or without Shift) and
cut/copy/paste act on every caret, as one undo step.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QTextCursor
from PySide6.QtWidgets import QTextEdit

from . import theme

_MOVES = {Qt.Key_Left: QTextCursor.Left, Qt.Key_Right: QTextCursor.Right,
          Qt.Key_Up: QTextCursor.Up, Qt.Key_Down: QTextCursor.Down,
          Qt.Key_Home: QTextCursor.StartOfLine, Qt.Key_End: QTextCursor.EndOfLine}


class MultiCaretMixin:
    _carets = ()
    _box_anchor = None

    # ---------------------------------------------------------------- state
    def carets(self):
        """All carets, the main one first."""
        return [self.textCursor()] + list(self._carets)

    def has_multi(self):
        return bool(self._carets)

    def clear_carets(self):
        if self._carets:
            self._carets = []
            self._mc_changed()

    def _set_all(self, cursors):
        """Store carets, dropping duplicates (carets that met), main one first."""
        seen, out = set(), []
        for c in cursors:
            key = (c.position(), c.anchor())
            if key not in seen:
                seen.add(key)
                out.append(QTextCursor(c))
        self.setTextCursor(out[0])
        self._carets = out[1:]
        self._mc_changed()

    def _mc_changed(self):
        self._refresh_extra()
        self.viewport().update()

    def add_caret(self, cursor):
        self._set_all(self.carets() + [cursor])

    def mc_selections(self):
        """Extra selections that show the other carets' selected text."""
        out = []
        for c in self._carets:
            if c.hasSelection():
                s = QTextEdit.ExtraSelection()
                s.format.setBackground(QColor(theme.c(theme.SELECTION)))
                s.cursor = QTextCursor(c)
                out.append(s)
        return out

    # ---------------------------------------------------------------- commands
    def add_caret_vertical(self, down):
        """Shift+Alt+Down/Up: a caret at the same column on the next/previous line."""
        cs = self.carets()
        edge = max(cs, key=lambda c: c.blockNumber()) if down else min(cs, key=lambda c: c.blockNumber())
        col = edge.positionInBlock()
        block = edge.block().next() if down else edge.block().previous()
        if not block.isValid():
            return
        c = QTextCursor(block)
        c.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, min(col, block.length() - 1))
        self.add_caret(c)

    def _word_or_selection(self, c):
        if c.hasSelection():
            return c.selectedText()
        c.select(QTextCursor.WordUnderCursor)
        return c.selectedText()

    def add_next_occurrence(self):
        """Shift+Alt+.: select the word (first time), then add the next match as a new caret."""
        main = self.textCursor()
        if not main.hasSelection():
            text = self._word_or_selection(main)
            if text:
                self.setTextCursor(main)
                self._mc_changed()
            return
        text = main.selectedText()
        last = max(self.carets(), key=lambda c: c.selectionEnd())
        doc = self.document()
        found = doc.find(text, last.selectionEnd(), doc.FindFlag.FindCaseSensitively)
        if found.isNull():
            found = doc.find(text, 0, doc.FindFlag.FindCaseSensitively)     # wrap around
        if found.isNull() or any(c.selectionStart() == found.selectionStart() for c in self.carets()):
            return
        self.add_caret(found)
        self.ensureCursorVisible()

    def add_all_occurrences(self):
        """Shift+Alt+;: a caret on every match of the selection/word."""
        main = self.textCursor()
        text = self._word_or_selection(QTextCursor(main))
        if not text:
            return
        doc = self.document()
        found, cs, pos = [], [], 0
        while True:
            c = doc.find(text, pos, doc.FindFlag.FindCaseSensitively)
            if c.isNull():
                break
            found.append(c)
            pos = c.selectionEnd()
        if not found:
            return
        # keep the match under the main caret as the main one
        i = next((n for n, c in enumerate(found) if c.selectionStart() <= main.position() <= c.selectionEnd()), 0)
        cs = [found[i]] + found[:i] + found[i + 1:]
        self._set_all(cs)

    def _box_select(self, anchor_pos, pos):
        """Carets from anchor to pos, one per line, same columns (box selection)."""
        a = self.cursorForPosition(anchor_pos)
        b = self.cursorForPosition(pos)
        fm = self.fontMetrics()
        x0 = min(anchor_pos.x(), pos.x()) - self.contentOffset().x() - self.document().documentMargin()
        x1 = max(anchor_pos.x(), pos.x()) - self.contentOffset().x() - self.document().documentMargin()
        w = max(1, fm.horizontalAdvance(" "))
        c0, c1 = max(0, round(x0 / w)), max(0, round(x1 / w))
        first, last = sorted((a.blockNumber(), b.blockNumber()))
        cs = []
        block = self.document().findBlockByNumber(first)
        while block.isValid() and block.blockNumber() <= last:
            n = block.length() - 1
            c = QTextCursor(block)
            c.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, min(c0, n))
            c.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, max(0, min(c1, n) - min(c0, n)))
            cs.append(c)
            block = block.next()
        if cs:
            # the caret you're dragging from stays the main one
            if b.blockNumber() < a.blockNumber():
                cs.reverse()
            self._set_all([cs[-1]] + cs[:-1])

    # ---------------------------------------------------------------- editing with several carets
    def mc_key(self, event):
        """Handle a key press; returns True when it was used."""
        key, mods, text = event.key(), event.modifiers(), event.text()
        sa = (mods & Qt.ShiftModifier) and (mods & Qt.AltModifier) and not (mods & Qt.ControlModifier)
        if sa and key in (Qt.Key_Up, Qt.Key_Down):
            self.add_caret_vertical(key == Qt.Key_Down)
            return True
        if sa and key in (Qt.Key_Period, Qt.Key_Greater):
            self.add_next_occurrence()
            return True
        if sa and key in (Qt.Key_Semicolon, Qt.Key_Colon):
            self.add_all_occurrences()
            return True
        if not self._carets:
            return False
        if key == Qt.Key_Escape:
            self.clear_carets()
            return True
        ctrl = bool(mods & Qt.ControlModifier)
        if ctrl and key in (Qt.Key_C, Qt.Key_X, Qt.Key_V):
            {Qt.Key_C: self.copy, Qt.Key_X: self.cut, Qt.Key_V: self.paste}[key]()
            return True
        if key in _MOVES and not ctrl:
            mode = QTextCursor.KeepAnchor if mods & Qt.ShiftModifier else QTextCursor.MoveAnchor
            cs = []
            for c in self.carets():
                if mode == QTextCursor.MoveAnchor and c.hasSelection() and key in (Qt.Key_Left, Qt.Key_Right):
                    p = c.selectionStart() if key == Qt.Key_Left else c.selectionEnd()
                    c.setPosition(p)
                else:
                    c.movePosition(_MOVES[key], mode)
                cs.append(c)
            self._set_all(cs)
            return True
        if key == Qt.Key_Backspace:
            self._edit(lambda c, i: c.removeSelectedText() if c.hasSelection() else c.deletePreviousChar())
            return True
        if key == Qt.Key_Delete:
            self._edit(lambda c, i: c.removeSelectedText() if c.hasSelection() else c.deleteChar())
            return True
        if key in (Qt.Key_Return, Qt.Key_Enter):
            def nl(c, i):
                line = c.block().text()
                c.insertText("\n" + line[:len(line) - len(line.lstrip())])
            self._edit(nl)
            return True
        if key == Qt.Key_Tab:
            self._edit(lambda c, i: c.insertText("    "))
            return True
        if text and text.isprintable() and not ctrl:
            self._edit(lambda c, i: c.insertText(text))
            return True
        if ctrl and key in (Qt.Key_Z, Qt.Key_Y, Qt.Key_A, Qt.Key_S):
            self.clear_carets()          # undo/redo/select all/save: back to one caret
        return False

    # clipboard and undo (Edit menu / Ctrl+C... call these)
    def copy(self):
        if not self._carets:
            return super().copy()
        parts = [c.selectedText().replace(" ", "\n") for c in sorted(self.carets(), key=lambda c: c.position())]
        QGuiApplication.clipboard().setText("\n".join(parts))

    def cut(self):
        if not self._carets:
            return super().cut()
        self.copy()
        self._edit(lambda c, i: c.removeSelectedText())

    def paste(self):
        if not self._carets:
            return super().paste()
        clip = QGuiApplication.clipboard().text()
        lines = clip.split("\n")
        cs = self.carets()
        order = sorted(range(len(cs)), key=lambda i: cs[i].position())
        per = {i: lines[k] for k, i in enumerate(order)} if len(lines) == len(cs) else None   # one line each
        self._edit(lambda c, i: c.insertText(per[i] if per else clip))

    def undo(self):
        self.clear_carets()
        super().undo()

    def redo(self):
        self.clear_carets()
        super().redo()

    def _edit(self, func):
        cs = self.carets()
        cs[0].beginEditBlock()
        for i, c in enumerate(cs):
            func(c, i)
        cs[0].endEditBlock()
        self._set_all(cs)

    # ---------------------------------------------------------------- mouse / painting
    def mousePressEvent(self, e):
        mods = e.modifiers()
        if e.button() == Qt.LeftButton and mods & Qt.ControlModifier and mods & Qt.AltModifier:
            self.add_caret(self.cursorForPosition(e.position().toPoint()))
            return
        if e.button() == Qt.LeftButton and mods & Qt.ShiftModifier and mods & Qt.AltModifier:
            self._box_anchor = e.position().toPoint()
            self._box_select(self._box_anchor, self._box_anchor)
            return
        self._box_anchor = None
        if e.button() == Qt.LeftButton:
            self.clear_carets()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._box_anchor is not None and e.buttons() & Qt.LeftButton:
            self._box_select(self._box_anchor, e.position().toPoint())
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._box_anchor is not None:
            self._box_anchor = None
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self._carets:
            return
        p = QPainter(self.viewport())
        p.setPen(QColor(theme.c(theme.EDITOR_FG)))
        for c in self._carets:
            r = self.cursorRect(c)
            p.drawLine(r.left(), r.top(), r.left(), r.bottom())
        p.end()
