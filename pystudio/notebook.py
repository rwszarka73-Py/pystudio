"""Jupyter notebooks (.ipynb) in PyStudio: code cells you run one at a time, each with its output underneath (text,
tables, charts, images, errors), and Markdown cells for notes. Files are standard Jupyter notebooks (nbformat 4),
so they open in Jupyter, VS Code and GitHub too.

The kernel (nbkernel.py) runs in the project's Python in the notebook's folder, so the project's packages and
modules import. Shift+Enter runs a cell and moves on; Ctrl+Enter runs it and stays; Alt+Enter runs and adds a cell."""
import json
import os
import re
import signal
import uuid

from PySide6.QtCore import QByteArray, QProcess, QProcessEnvironment, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QKeySequence, QPixmap, QShortcut, QTextOption
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QTextBrowser, QToolButton, QVBoxLayout,
                               QWidget)

from . import theme
from .highlighter import PythonHighlighter

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
KERNEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nbkernel.py")
MAX_TEXT_LINES = 40


def _mono():
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setPointSize(10)
    return f


def _text(v):
    return "".join(v) if isinstance(v, list) else (v or "")


def _lines(text):
    """nbformat stores multi-line strings as a list of lines (each but the last ending in a newline)."""
    if not text:
        return []
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


# ============================================================ kernel process
class KernelClient(QProcess):
    message = Signal(dict)
    state_changed = Signal(str)          # "starting" | "idle" | "busy" | "dead"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.readyReadStandardOutput.connect(self._read_out)
        self.readyReadStandardError.connect(self._read_err)
        self.finished.connect(self._finished)
        self.pending = []                 # cell ids queued / running, in order
        self.version = ""
        self._buf = b""
        self._stopping = False

    def start_kernel(self, python, cwd, env):
        e = QProcessEnvironment.systemEnvironment()
        for k, v in (env or {}).items():
            e.insert(k, v)
        e.insert("PYTHONUNBUFFERED", "1")
        e.insert("PYTHONIOENCODING", "utf-8")
        self.setProcessEnvironment(e)
        self.setWorkingDirectory(cwd)
        self.pending = []
        self._stopping = False
        self.state_changed.emit("starting")
        self.start(python, ["-u", KERNEL])

    @property
    def alive(self):
        return self.state() != QProcess.NotRunning

    def execute(self, cell_id, code):
        self.pending.append(cell_id)
        self.state_changed.emit("busy")
        self.write((json.dumps({"id": cell_id, "code": code}) + "\n").encode("utf-8"))

    def send_input(self, text):
        self.write((json.dumps({"input": text}) + "\n").encode("utf-8"))

    def interrupt(self):
        if self.alive and self.pending:
            os.kill(self.processId(), signal.SIGINT)

    def stop(self):
        self._stopping = True
        if self.alive:
            self.closeWriteChannel()
            if not self.waitForFinished(1500):
                self.kill()
                self.waitForFinished(1500)

    def _read_out(self):
        self._buf += bytes(self.readAllStandardOutput())
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            try:
                m = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            t = m.get("type")
            if t == "ready":
                self.version = m.get("python", "")
                self.state_changed.emit("idle")
                continue
            if t == "done" and m.get("id") in self.pending:
                self.pending.remove(m["id"])
            self.message.emit(m)
            if t == "done" and not self.pending:
                self.state_changed.emit("idle")

    def _read_err(self):
        text = bytes(self.readAllStandardError()).decode("utf-8", "replace")
        if text:
            self.message.emit({"id": self.pending[0] if self.pending else None, "type": "stream", "name": "stderr",
                               "text": text})

    def _finished(self, code, _status):
        left = list(self.pending)
        self.pending = []
        self.state_changed.emit("dead")
        if not self._stopping:
            for cid in left:
                self.message.emit({"id": cid, "type": "error", "ename": "KernelDied",
                                   "evalue": f"The kernel stopped (exit code {code}).",
                                   "traceback": ["The Python process running the notebook stopped. Restart the "
                                                 "kernel to keep going.\n"]})
                self.message.emit({"id": cid, "type": "done", "count": None})


# ============================================================ cell pieces
class CellEdit(QPlainTextEdit):
    run = Signal(str)                    # "stay" | "next" | "insert"
    focused = Signal()

    def __init__(self, code=True, parent=None):
        super().__init__(parent)
        self.setFont(_mono())
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setLineWrapMode(QPlainTextEdit.NoWrap if code else QPlainTextEdit.WidgetWidth)
        self.setTabChangesFocus(False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(f"QPlainTextEdit {{ background: {theme.EDITOR_BG}; color: {theme.EDITOR_FG}; "
                           "border: 1px solid #3c3c3c; padding: 2px; }")
        self.hl = PythonHighlighter(self.document()) if code else None
        self.document().contentsChanged.connect(self._fit)
        self._fit()

    def _fit(self):
        lines = max(1, self.document().blockCount())
        h = self.fontMetrics().lineSpacing() * lines + 14
        if self.horizontalScrollBar().isVisible():
            h += self.horizontalScrollBar().height()
        self.setFixedHeight(min(h, 2000))

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.focused.emit()

    def keyPressEvent(self, e):
        k, mods = e.key(), e.modifiers()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            if mods & Qt.ShiftModifier:
                self.run.emit("next")
                return
            if mods & Qt.ControlModifier:
                self.run.emit("stay")
                return
            if mods & Qt.AltModifier:
                self.run.emit("insert")
                return
            if self.hl is not None:                       # keep the indent; one more after a colon
                line = self.textCursor().block().text()[:self.textCursor().positionInBlock()]
                ind = re.match(r"[ \t]*", line).group(0)
                if line.rstrip().endswith(":"):
                    ind += "    "
                self.textCursor().insertText("\n" + ind)
                return
        if k == Qt.Key_Tab and self.hl is not None and not (mods & Qt.ShiftModifier):
            self.textCursor().insertText("    ")
            return
        super().keyPressEvent(e)


class OutputText(QPlainTextEdit):
    """Printed text / errors: sized to fit up to MAX_TEXT_LINES, then scrolls."""

    def __init__(self, text="", kind="stdout", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(_mono())
        self.setFrameShape(QFrame.NoFrame)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapAnywhere)
        self.kind = kind
        bg = {"stderr": "#3a2a2a", "error": "#3a2424"}.get(kind, "transparent")
        fg = {"error": "#f48771"}.get(kind, theme.EDITOR_FG)
        self.setStyleSheet(f"QPlainTextEdit {{ background: {bg}; color: {fg}; }}")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.append_text(text)

    def append_text(self, text):
        text = ANSI.sub("", text)
        if "\r" in text:                                   # progress bars: \r rewrites the line
            cur = self.toPlainText() + text
            cur = "\n".join(line.rsplit("\r", 1)[-1] for line in cur.split("\n"))
            self.setPlainText(cur)
        else:
            self.moveCursor(self.textCursor().MoveOperation.End)
            self.insertPlainText(text)
        QTimer.singleShot(0, self._fit)
        self._fit()

    def _fit(self):
        """Tall enough for the text (wrapped lines too) up to MAX_TEXT_LINES; beyond that it scrolls."""
        doc = self.document()
        lines, b = 0, doc.firstBlock()
        while b.isValid():
            lines += max(1, b.layout().lineCount()) if b.layout() is not None else 1
            b = b.next()
        if doc.blockCount() > 1 and not doc.lastBlock().text():
            lines -= 1                       # the line after a final newline
        n = min(MAX_TEXT_LINES, max(1, lines))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if lines > MAX_TEXT_LINES else Qt.ScrollBarAlwaysOff)
        self.setFixedHeight(int(self.fontMetrics().lineSpacing() * n + 2 * doc.documentMargin() + 6))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._fit)

    def text(self):
        return self.toPlainText()


class OutputHtml(QTextBrowser):
    """HTML (pandas tables) and Markdown output, sized to its content."""
    CSS = ("table { border-collapse: collapse; } th, td { border: 1px solid #5a5a5a; padding: 3px 6px; } "
           "th { background: #2d3a4a; } ")

    def __init__(self, html=None, markdown=None, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.document().setDefaultStyleSheet(self.CSS)
        self.setStyleSheet("QTextBrowser { background: transparent; }")
        if markdown is not None:
            self.setMarkdown(markdown)
        else:
            self.setHtml(html or "")
        self.document().contentsChanged.connect(self._fit)
        QTimer.singleShot(0, self._fit)

    def _fit(self):
        self.document().setTextWidth(max(200, self.viewport().width()))
        self.setFixedHeight(int(min(self.document().size().height() + 6, 1200)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


def image_label(data_b64, svg=None):
    lbl = QLabel()
    pm = QPixmap()
    if svg is not None:
        try:
            from PySide6.QtSvg import QSvgRenderer
            from PySide6.QtGui import QPainter
            r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            size = r.defaultSize() if r.defaultSize().isValid() else QSize(400, 300)
            pm = QPixmap(size)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            r.render(p)
            p.end()
        except ImportError:
            pm = QPixmap()
    else:
        pm.loadFromData(QByteArray.fromBase64(data_b64.replace("\n", "").encode("ascii")))
    if not pm.isNull() and pm.width() > 900:
        pm = pm.scaledToWidth(900, Qt.SmoothTransformation)
    lbl.setPixmap(pm)
    lbl.setAlignment(Qt.AlignLeft)
    return lbl


# ============================================================ a cell
class Cell(QFrame):
    run_requested = Signal(object, str)
    selected = Signal(object)
    changed = Signal()

    def __init__(self, kind="code", source="", outputs=None, count=None, cell_id=None, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.id = cell_id or uuid.uuid4().hex[:8]
        self.outputs = []
        self.count = count
        self.running = False
        self.setFrameShape(QFrame.NoFrame)
        self.setObjectName("nbcell")
        self.prompt = QLabel()
        self.prompt.setFixedWidth(64)
        self.prompt.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.prompt.setStyleSheet("color: #6a9955; font-family: monospace; padding-top: 4px;")
        self.b_run = QToolButton()
        self.b_run.setText("▶")
        self.b_run.setToolTip("Run this cell (Ctrl+Enter; Shift+Enter runs it and moves to the next)")
        self.b_run.setAutoRaise(True)
        self.b_run.clicked.connect(lambda: self.run_requested.emit(self, "stay"))
        self.edit = CellEdit(kind == "code")
        self.edit.setPlainText(source)
        self.edit.document().setModified(False)
        self.edit.textChanged.connect(self.changed)
        self.edit.focused.connect(lambda: self.selected.emit(self))
        self.edit.run.connect(lambda how: self.run_requested.emit(self, how))
        self.view = None                       # rendered Markdown
        self.out_box = QVBoxLayout()
        self.out_box.setContentsMargins(0, 2, 0, 0)
        self.out_box.setSpacing(2)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.addWidget(self.prompt)
        left.addWidget(self.b_run, 0, Qt.AlignRight)
        left.addStretch()
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(2)
        right.addWidget(self.edit)
        if kind == "markdown":
            self.view = OutputHtml(markdown=source or "*Double-click to write notes (Markdown).*")
            self.view.setToolTip("Double-click to edit; Shift+Enter shows it again")
            self.view.mouseDoubleClickEvent = lambda _e: self.edit_markdown()
            self.view.mousePressEvent = lambda _e: self.selected.emit(self)
            right.addWidget(self.view)
            self.prompt.setText("")
            self.b_run.hide()
            self.edit.setVisible(not source)
            self.view.setVisible(bool(source))
        right.addLayout(self.out_box)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 8, 4)
        lay.setSpacing(6)
        lay.addLayout(left)
        lay.addLayout(right, 1)
        for o in outputs or []:
            self.add_output(o)
        self.set_prompt()
        self.set_current(False)

    def set_current(self, on):
        self.setStyleSheet("#nbcell { border-left: 3px solid %s; }" % ("#3c8fd8" if on else "transparent"))

    def set_prompt(self):
        if self.kind != "code":
            return
        self.prompt.setText("[*]:" if self.running else f"[{self.count if self.count is not None else ' '}]:")

    def source(self):
        return self.edit.toPlainText()

    def edit_markdown(self):
        if self.kind == "markdown":
            self.view.hide()
            self.edit.show()
            self.edit.setFocus()

    def render_markdown(self):
        if self.kind == "markdown":
            src = self.source()
            self.view.setMarkdown(src or "*Double-click to write notes (Markdown).*")
            self.edit.setVisible(False)
            self.view.show()

    # ------------------------------------------------------------ outputs
    def clear_outputs(self):
        self.outputs = []
        while self.out_box.count():
            w = self.out_box.takeAt(0).widget()
            if w is not None:
                w.deleteLater()

    def add_output(self, o):
        """o: an nbformat output dict."""
        t = o.get("output_type")
        last = self.out_box.itemAt(self.out_box.count() - 1).widget() if self.out_box.count() else None
        if t == "stream":
            text = _text(o.get("text"))
            name = o.get("name", "stdout")
            if self.outputs and self.outputs[-1].get("output_type") == "stream" and \
                    self.outputs[-1].get("name") == name and isinstance(last, OutputText):
                self.outputs[-1]["text"] = _text(self.outputs[-1]["text"]) + text
                last.append_text(text)
                return
            self.outputs.append({"output_type": "stream", "name": name, "text": text})
            self.out_box.addWidget(OutputText(text, name))
            return
        self.outputs.append(o)
        if t == "error":
            tb = ANSI.sub("", "".join(o.get("traceback") or [])) or f"{o.get('ename')}: {o.get('evalue')}"
            self.out_box.addWidget(OutputText(tb.rstrip("\n"), "error"))
            return
        data = o.get("data", {})
        w = None
        if "image/png" in data or "image/jpeg" in data:
            w = image_label(_text(data.get("image/png") or data.get("image/jpeg")))
        elif "image/svg+xml" in data:
            w = image_label(None, _text(data["image/svg+xml"]))
        elif "text/html" in data:
            w = OutputHtml(html=_text(data["text/html"]))
        elif "text/markdown" in data:
            w = OutputHtml(markdown=_text(data["text/markdown"]))
        elif "text/plain" in data:
            w = OutputText(_text(data["text/plain"]), "result")
        if w is not None:
            if t == "execute_result":
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                out = QLabel(f"Out[{o.get('execution_count') or ''}]:")
                out.setStyleSheet("color: #d16969; font-family: monospace;")
                out.setAlignment(Qt.AlignTop)
                h.addWidget(out)
                h.addWidget(w, 1)
                w = row
            self.out_box.addWidget(w)

    def to_json(self):
        src = _lines(self.source())
        if self.kind == "markdown":
            return {"cell_type": "markdown", "id": self.id, "metadata": {}, "source": src}
        outs = []
        for o in self.outputs:
            o = dict(o)
            if o.get("output_type") == "stream":
                o["text"] = _lines(_text(o["text"]))
            elif "data" in o:
                o["data"] = {k: (_lines(v) if isinstance(v, str) and k.startswith("text/") else v)
                             for k, v in o["data"].items()}
                o.setdefault("metadata", {})
            outs.append(o)
        return {"cell_type": "code", "execution_count": self.count, "id": self.id, "metadata": {},
                "outputs": outs, "source": src}


# ============================================================ the document
class NotebookDocument(QWidget):
    dirty_changed = Signal(bool)
    saved = Signal(str)

    def __init__(self, path=None, kernel_context=None, parent=None):
        super().__init__(parent)
        self.path = os.path.abspath(path) if path else None
        self.kernel_context = kernel_context or (lambda: ("python3", {}, ""))
        self._dirty = False
        self.cells = []
        self.current = None
        self.meta = {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python"}}
        self.kernel = KernelClient(self)
        self.kernel.message.connect(self._message)
        self.kernel.state_changed.connect(self._kernel_state)
        self._queue_after = {}             # cell id -> how ("next" / "insert") once it's queued

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 4)

        def tb(text, tip, fn):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
            return b
        tb("+ Code", "Add a code cell below this one (B)", lambda: self.insert_cell("code"))
        tb("+ Markdown", "Add a notes cell (Markdown) below this one", lambda: self.insert_cell("markdown"))
        bar.addSpacing(8)
        tb("▶ Run", "Run this cell and go to the next (Shift+Enter)", lambda: self.current and
           self.run_cell(self.current, "next"))
        tb("▶▶ Run All", "Run every cell from the top", self.run_all)
        self.b_interrupt = tb("■ Interrupt", "Stop the running cell (KeyboardInterrupt)", self.kernel.interrupt)
        tb("⟳ Restart", "Start a fresh Python (variables are cleared; outputs stay)", self.restart)
        tb("Clear Outputs", "Remove every cell's output", self.clear_all_outputs)
        bar.addSpacing(8)
        self.cb_type = QComboBox()
        self.cb_type.addItems(["Code", "Markdown"])
        self.cb_type.setToolTip("Change this cell to code or notes")
        self.cb_type.activated.connect(lambda i: self.change_type(["code", "markdown"][i]))
        bar.addWidget(self.cb_type)
        for text, tip, fn in (("↑", "Move the cell up (Ctrl+Shift+Up)", lambda: self.move(-1)),
                              ("↓", "Move the cell down (Ctrl+Shift+Down)", lambda: self.move(1)),
                              ("✕", "Delete the cell", self.delete_cell)):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        self.lbl_kernel = QLabel("Python: not started")
        self.lbl_kernel.setStyleSheet("color: #9a9a9a;")
        bar.addWidget(self.lbl_kernel)

        self.box = QVBoxLayout()
        self.box.setContentsMargins(8, 8, 8, 8)
        self.box.setSpacing(4)
        self.box.addStretch()
        inner = QWidget()
        inner.setLayout(self.box)
        inner.setObjectName("nbinner")
        inner.setStyleSheet(f"#nbinner {{ background: {theme.EDITOR_BG}; }}")
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(inner)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.scroll, 1)
        for seq, fn in (("Ctrl+Shift+Up", lambda: self.move(-1)), ("Ctrl+Shift+Down", lambda: self.move(1))):
            QShortcut(QKeySequence(seq), self, fn, context=Qt.WidgetWithChildrenShortcut)
        if self.path and os.path.exists(self.path):
            self.load(self.path)
        if not self.cells:
            self.insert_cell("code", mark_dirty=False)

    # ------------------------------------------------------------ doc API
    @property
    def is_dirty(self):
        return self._dirty

    def _set_dirty(self, v=True):
        if v != self._dirty:
            self._dirty = v
            self.dirty_changed.emit(v)

    def display_name(self):
        return os.path.basename(self.path) if self.path else "Untitled.ipynb"

    def setFocus(self):
        if self.current is not None:
            self.current.edit.setFocus()

    def load(self, path):
        with open(path, encoding="utf-8") as f:
            nb = json.load(f)
        if nb.get("nbformat", 4) < 4:
            raise ValueError("This notebook uses an old format (nbformat 3). Open it in Jupyter and save it once to "
                             "update it.")
        self.meta = nb.get("metadata") or self.meta
        for c in nb.get("cells", []):
            kind = c.get("cell_type", "code")
            if kind == "raw":
                kind = "markdown"
            self._add(Cell(kind, _text(c.get("source")), c.get("outputs") if kind == "code" else None,
                           c.get("execution_count"), c.get("id")))
        self._set_dirty(False)

    def to_json(self):
        meta = dict(self.meta)
        if self.kernel.version:
            meta.setdefault("language_info", {})["version"] = self.kernel.version
        return {"cells": [c.to_json() for c in self.cells], "metadata": meta, "nbformat": 4, "nbformat_minor": 5}

    def save(self, path=None):
        path = path or self.path
        if not path:
            return False
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self.to_json(), f, indent=1, ensure_ascii=False)
                f.write("\n")
        except OSError as e:
            QMessageBox.critical(self, "Save failed", f"Could not save {path}:\n{e}")
            return False
        self.path = os.path.abspath(path)
        self._set_dirty(False)
        self.saved.emit(self.path)
        return True

    def shutdown(self):
        self.kernel.stop()

    # ------------------------------------------------------------ cells
    def _add(self, cell, index=None):
        index = len(self.cells) if index is None else index
        self.cells.insert(index, cell)
        self.box.insertWidget(index, cell)
        cell.run_requested.connect(self.run_cell)
        cell.selected.connect(self.select)
        cell.changed.connect(lambda: self._set_dirty(True))
        return cell

    def select(self, cell):
        if self.current is not None and self.current is not cell and self.current in self.cells:
            self.current.set_current(False)
            if self.current.kind == "markdown" and self.current.source():
                self.current.render_markdown()
        self.current = cell
        cell.set_current(True)
        self.cb_type.setCurrentIndex(0 if cell.kind == "code" else 1)

    def insert_cell(self, kind="code", source="", mark_dirty=True, after=None):
        after = after if after is not None else self.current
        i = self.cells.index(after) + 1 if after in self.cells else len(self.cells)
        c = self._add(Cell(kind, source), i)
        self.select(c)
        if kind == "markdown":
            c.edit_markdown()
        QTimer.singleShot(0, lambda: (c.edit.setFocus(), self.scroll.ensureWidgetVisible(c)))
        if mark_dirty:
            self._set_dirty(True)
        return c

    def delete_cell(self):
        c = self.current
        if c is None:
            return
        i = self.cells.index(c)
        self.cells.remove(c)
        c.setParent(None)
        c.deleteLater()
        self.current = None
        if not self.cells:
            self.insert_cell("code")
        else:
            self.select(self.cells[min(i, len(self.cells) - 1)])
        self._set_dirty(True)

    def move(self, delta):
        c = self.current
        if c is None:
            return
        i = self.cells.index(c)
        j = i + delta
        if not 0 <= j < len(self.cells):
            return
        self.cells.insert(j, self.cells.pop(i))
        self.box.removeWidget(c)
        self.box.insertWidget(j, c)
        c.edit.setFocus()
        self._set_dirty(True)

    def change_type(self, kind):
        c = self.current
        if c is None or c.kind == kind:
            return
        i = self.cells.index(c)
        src = c.source()
        self.cells.remove(c)
        c.setParent(None)
        c.deleteLater()
        self.current = None
        n = self._add(Cell(kind, src), i)
        self.select(n)
        if kind == "markdown":
            n.edit_markdown()
        self._set_dirty(True)

    def clear_all_outputs(self):
        for c in self.cells:
            if c.kind == "code":
                c.clear_outputs()
                c.count = None
                c.set_prompt()
        self._set_dirty(True)

    # ------------------------------------------------------------ running
    def _ensure_kernel(self):
        if not self.kernel.alive:
            python, env, label = self.kernel_context()
            cwd = os.path.dirname(self.path) if self.path else os.path.expanduser("~")
            self.kernel_label = label
            self.kernel.start_kernel(python, cwd, env)

    def run_cell(self, cell, how="stay"):
        if cell.kind == "markdown":
            cell.render_markdown()
        elif cell.source().strip():
            self._ensure_kernel()
            cell.clear_outputs()
            cell.running = True
            cell.set_prompt()
            self.kernel.execute(cell.id, cell.source())
            self._set_dirty(True)
        i = self.cells.index(cell)
        if how == "next":
            if i + 1 < len(self.cells):
                nxt = self.cells[i + 1]
                self.select(nxt)
                nxt.edit.setFocus() if nxt.kind == "code" or nxt.edit.isVisible() else nxt.setFocus()
                self.scroll.ensureWidgetVisible(nxt)
            else:
                self.insert_cell("code", after=cell)
        elif how == "insert":
            self.insert_cell("code", after=cell)

    def run_all(self):
        for c in self.cells:
            if c.kind == "code" and c.source().strip():
                self.run_cell(c, "none")
            elif c.kind == "markdown" and c.source().strip():
                c.render_markdown()

    def restart(self):
        self.kernel.stop()
        for c in self.cells:
            if c.running:
                c.running = False
                c.set_prompt()
        self._ensure_kernel()

    def _cell(self, cid):
        return next((c for c in self.cells if c.id == cid), None)

    def _message(self, m):
        c = self._cell(m.get("id"))
        if c is None:
            return
        t = m.get("type")
        if t == "stream":
            c.add_output({"output_type": "stream", "name": m.get("name", "stdout"), "text": m.get("text", "")})
        elif t == "display":
            c.add_output({"output_type": "display_data", "data": m.get("data", {}), "metadata": {}})
        elif t == "result":
            c.add_output({"output_type": "execute_result", "execution_count": m.get("count"),
                          "data": m.get("data", {}), "metadata": {}})
        elif t == "error":
            c.add_output({"output_type": "error", "ename": m.get("ename", ""), "evalue": m.get("evalue", ""),
                          "traceback": m.get("traceback", [])})
        elif t == "input":
            text, ok = QInputDialog.getText(self, "Input", m.get("prompt") or "Input:")
            self.kernel.send_input(text if ok else "")
        elif t == "done":
            c.running = False
            if m.get("count") is not None:
                c.count = m.get("count")
            c.set_prompt()
        self._set_dirty(True)

    def _kernel_state(self, state):
        label = getattr(self, "kernel_label", "") or "Python"
        v = f" {self.kernel.version}" if self.kernel.version else ""
        text = {"starting": "starting...", "idle": "idle", "busy": "busy", "dead": "stopped"}.get(state, state)
        dot = {"idle": "#2ea043", "busy": "#d29922", "starting": "#d29922", "dead": "#8a8a8a"}.get(state, "#8a8a8a")
        self.lbl_kernel.setText(f"<span style='color:{dot}'>●</span> Python{v} - {text}")
        self.lbl_kernel.setToolTip(label)
        self.b_interrupt.setEnabled(state == "busy")


def new_notebook(path):
    nb = {"cells": [{"cell_type": "markdown", "id": uuid.uuid4().hex[:8], "metadata": {},
                     "source": ["# " + os.path.splitext(os.path.basename(path))[0] + "\n", "\n",
                                "Shift+Enter runs a cell. Markdown cells like this one hold notes."]},
                    {"cell_type": "code", "execution_count": None, "id": uuid.uuid4().hex[:8], "metadata": {},
                     "outputs": [], "source": []}],
          "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                       "language_info": {"name": "python"}}, "nbformat": 4, "nbformat_minor": 5}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1)
        f.write("\n")
    return path

