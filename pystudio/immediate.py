"""Immediate Window (VS: Debug > Windows > Immediate, Ctrl+Alt+I).

While paused in the debugger it runs code in the current frame: type an expression to see
its value, or a statement (x = 5, self.ui.label1.setText("hi")) to run it. When not
debugging it's a Python Interactive window: a Python shell in the project folder (with the
project's Python), so you can try out code and import your own modules."""
import codeop
import json
import os

from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QToolButton, QVBoxLayout,
                               QWidget)
from . import theme

REPL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repl_server.py")


class _Input(QLineEdit):
    history_up = Signal()
    history_down = Signal()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Up:
            self.history_up.emit()
        elif e.key() == Qt.Key_Down:
            self.history_down.emit()
        else:
            super().keyPressEvent(e)


class ImmediateWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None          # DebugSession (set by MainWindow)
        self.frame = lambda: 0       # current frame id while paused
        self.python = None
        self.cwd = None
        self.proc = None
        self._buf = b""
        self._pending = []           # code sent to the interactive process, waiting for replies
        self._lines = []             # a multi-line block being typed (def/for/if ...)
        self.history, self._hpos = [], 0
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.mode = QLabel()
        self.mode.setStyleSheet("color: #9a9a9a; padding: 2px 4px;")
        b_clear = QToolButton()
        b_clear.setText("Clear")
        b_clear.setAutoRaise(True)
        b_clear.clicked.connect(lambda: self.out.clear())
        self.b_reset = QToolButton()
        self.b_reset.setText("Reset")
        self.b_reset.setAutoRaise(True)
        self.b_reset.setToolTip("Start a fresh Python Interactive session (forgets variables)")
        self.b_reset.clicked.connect(self.reset)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.mode, 1)
        top.addWidget(self.b_reset)
        top.addWidget(b_clear)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(font)
        self.out.setStyleSheet("QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; border: 0; }")
        self.prompt = QLabel(">>>")
        self.prompt.setFont(font)
        self.prompt.setStyleSheet("color: #569cd6; padding-left: 4px;")
        self.input = _Input()
        self.input.setFont(font)
        self.input.setPlaceholderText("Type Python and press Enter (Up/Down: history)")
        self.input.returnPressed.connect(self.submit)
        self.input.history_up.connect(lambda: self._history(-1))
        self.input.history_down.connect(lambda: self._history(1))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.prompt)
        row.addWidget(self.input, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(top)
        lay.addWidget(self.out, 1)
        lay.addLayout(row)
        self.update_mode()
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is not None:
            QApplication.instance().aboutToQuit.connect(self.shutdown)

    # ---------------------------------------------------------- mode
    def debugging(self):
        return self.session is not None and self.session.active and self.session.paused

    def set_project(self, python, cwd):
        if (python, cwd) != (self.python, self.cwd):
            self.python, self.cwd = python, cwd
            self._stop()
        self.update_mode()

    def update_mode(self):
        if self.debugging():
            self.mode.setText("Immediate - paused: runs in the current frame (variables, self, ...)")
            self.b_reset.hide()
        elif self.session is not None and self.session.active:
            self.mode.setText("Immediate - the program is running; pause it (Break All) or stop at a breakpoint")
            self.b_reset.hide()
        else:
            self.mode.setText("Python Interactive - " + (f"in {os.path.basename(self.cwd)} " if self.cwd else "") +
                              "(import your modules, try out code)")
            self.b_reset.show()

    # ---------------------------------------------------------- output
    def _write(self, text, color=None):
        if not text:
            return
        c = self.out.textCursor()
        c.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(theme.c(color)))
        c.insertText(text if text.endswith("\n") else text + "\n", fmt)
        self.out.setTextCursor(c)
        self.out.ensureCursorVisible()

    def _show_reply(self, r):
        self._write(r.get("output", ""), "#cccccc")
        if r.get("result"):
            self._write(r["result"], "#f48771" if r.get("error") else "#ffffff")

    # ---------------------------------------------------------- input
    def _history(self, step):
        if not self.history:
            return
        self._hpos = max(0, min(len(self.history), self._hpos + step))
        self.input.setText(self.history[self._hpos] if self._hpos < len(self.history) else "")

    def submit(self):
        line = self.input.text()
        self.input.clear()
        if line.strip():
            self.history.append(line)
        self._hpos = len(self.history)
        self._write(("... " if self._lines else ">>> ") + line, "#6a9955")
        self._lines.append(line)
        src = "\n".join(self._lines)
        if not src.strip():
            self._lines = []
            return
        try:
            complete = codeop.compile_command(src, "<input>", "single") is not None
        except (SyntaxError, ValueError, OverflowError):
            complete = True            # let Python report the error
        if not complete or (len(self._lines) > 1 and line.strip()):
            self.prompt.setText("...")  # inside a block: an empty line runs it
            return
        self._lines = []
        self.prompt.setText(">>>")
        self.run(src)

    def run(self, src):
        if self.debugging():
            self.session.execute(src, self.frame(), self._debug_reply)
        elif self.session is not None and self.session.active:
            self._write("The program is running - pause it first (Debug > Break All).", "#d7ba7d")
        else:
            self._send(src)

    def _debug_reply(self, r):
        self._show_reply(r)
        if self.on_changed:
            self.on_changed()            # Locals/Watch may have changed (x = 5)

    on_changed = None

    # ---------------------------------------------------------- Python Interactive process
    def _start(self):
        if self.proc is not None:
            return True
        if not self.python:
            self._write("No Python set (Tools > Options).", "#f48771")
            return False
        p = QProcess(self)
        p.setWorkingDirectory(self.cwd or os.path.expanduser("~"))
        p.readyReadStandardOutput.connect(self._read)
        p.readyReadStandardError.connect(
            lambda: self._write(bytes(p.readAllStandardError()).decode("utf-8", "replace"), "#f48771"))
        p.finished.connect(lambda *_a: self._ended(p))
        p.start(self.python, ["-u", REPL])
        if not p.waitForStarted(5000):
            self._write(f"Couldn't start {self.python}", "#f48771")
            return False
        self.proc = p
        self._buf = b""
        self._pending = []
        self._write(f"Python Interactive - {self.python}", "#808080")
        return True

    def _ended(self, p):
        if p is self.proc:
            self.proc = None
            if self._pending:
                self._write("(the Python session ended - it will restart with the next line)", "#808080")
            self._pending = []

    def _send(self, src):
        if not self._start():
            return
        self._pending.append(src)
        self.proc.write((json.dumps({"code": src}) + "\n").encode("utf-8"))

    def _read(self):
        if self.proc is None:
            return
        self._buf += bytes(self.proc.readAllStandardOutput())
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            try:
                r = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                self._write(line.decode("utf-8", "replace"), "#cccccc")   # stray print from a thread
                continue
            if self._pending:
                self._pending.pop(0)
            self._show_reply(r)

    def _stop(self):
        if self.proc is not None:
            p, self.proc = self.proc, None
            try:
                p.readyReadStandardOutput.disconnect()
                p.readyReadStandardError.disconnect()
                p.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            p.kill()
            p.waitForFinished(2000)

    def shutdown(self):
        self._stop()

    def reset(self):
        self._stop()
        self._lines = []
        self.prompt.setText(">>>")
        self._write("(session reset)", "#808080")
