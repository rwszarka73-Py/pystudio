"""Refactoring helpers: the Preview Changes dialog (Rename, Extract Method) and renaming a form's control from
code - the .ui form and every self.ui.<name> / self.<name> (uic.loadUi forms) in the code that uses it.

Like Visual Studio, event handlers keep their names (goButton_clicked stays) - only references change."""
import difflib
import html
import os
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QSplitter, QVBoxLayout)

SKIP_DIRS = {"__pycache__", "venv", ".venv", "build", "dist", "publish", ".git", "node_modules"}


def _walk(root, ext, limit=800):
    n = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(ext):
                n += 1
                if n > limit:
                    return
                yield os.path.join(dirpath, f)


def _read(path, texts=None):
    if texts and os.path.abspath(path) in texts:
        return texts[os.path.abspath(path)]
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


# ============================================================ controls in .ui forms
_NAMED = r"(<(?:widget|action|layout|spacer|actiongroup)\b[^>]*?\bname=\"){old}(\")"


def ui_names(text):
    return set(re.findall(r"<(?:widget|action|layout|spacer|actiongroup)\b[^>]*?\bname=\"([^\"]+)\"", text))


def ui_files_with(root, name, texts=None):
    """The .ui forms in the project that have a widget / action / layout named name."""
    out = []
    for p in _walk(root, ".ui"):
        try:
            if name in ui_names(_read(p, texts)):
                out.append(p)
        except OSError:
            pass
    return out


def rename_in_ui(text, old, new):
    o = re.escape(old)
    text = re.sub(_NAMED.format(old=o), rf"\g<1>{new}\g<2>", text)
    text = re.sub(rf"(<addaction name=\"){o}(\")", rf"\g<1>{new}\g<2>", text)
    text = re.sub(rf"<(tabstop|zorder|sender|receiver)>{o}</\1>", rf"<\1>{new}</\1>", text)
    text = re.sub(rf"(<property name=\"buddy\">\s*<cstring>){o}(</cstring>)", rf"\g<1>{new}\g<2>", text)
    return text


def code_uses_form(text, ui_path):
    base = os.path.basename(ui_path)
    stem = os.path.splitext(base)[0]
    return base in text or f"ui_{stem}" in text


def rename_in_code(text, old, new):
    """self.ui.old -> self.ui.new and self.old -> self.new (uic.loadUi forms put controls on self)."""
    return re.sub(rf"\b(self(?:\.ui)?\.){re.escape(old)}\b", rf"\g<1>{new}", text)


def control_rename_changes(root, old, new, texts=None, ui_paths=None, code_only=False):
    """{path: (old text, new text)} for renaming control old -> new: its .ui forms and the code using them.
    texts: {abs path: text} of open editors (unsaved text wins)."""
    changes = {}
    uis = ui_paths if ui_paths is not None else ui_files_with(root, old, texts)
    for ui in uis:
        if code_only:
            continue
        t = _read(ui, texts)
        n = rename_in_ui(t, old, new)
        if n != t:
            changes[ui] = (t, n)
    for p in _walk(root, ".py"):
        b = os.path.basename(p)
        if b.startswith("ui_") or b in ("pystudio_runtime.py", "my.py"):
            continue
        try:
            t = _read(p, texts)
        except OSError:
            continue
        if not any(code_uses_form(t, ui) for ui in uis):
            continue
        n = rename_in_code(t, old, new)
        if n != t:
            changes[p] = (t, n)
    return changes


def is_control_reference(line_text, col_start):
    """The name at col_start is written as self.ui.<name> or self.<name>."""
    before = line_text[:col_start]
    return before.endswith("self.ui.") or before.endswith("self.")


# ============================================================ Preview Changes
class PreviewDialog(QDialog):
    """VS's Preview Changes: the files that change (uncheck to leave one out) and each file's changes."""

    def __init__(self, title, summary, changes, root=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Preview Changes - {title}")
        self.resize(900, 620)
        self.changes = changes
        v = QVBoxLayout(self)
        n_lines = sum(self._count(o, n) for o, n in changes.values())
        v.addWidget(QLabel(f"<b>{html.escape(summary)}</b> &nbsp; {len(changes)} file(s), {n_lines} line(s) change. "
                           "Uncheck a file to leave it as it is."))
        self.files = QListWidget()
        for path in sorted(changes, key=lambda p: (not p.endswith(".py"), p)):
            o, n = changes[path]
            rel = os.path.relpath(path, root) if root and path.startswith(root + os.sep) else path
            it = QListWidgetItem(f"{rel}   ({self._count(o, n)} line(s))")
            it.setData(Qt.UserRole, path)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.files.addItem(it)
        self.files.currentItemChanged.connect(lambda it, _p: it and self._show(it.data(Qt.UserRole)))
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        self.diff.setFont(f)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.files)
        split.addWidget(self.diff)
        split.setSizes([160, 440])
        v.addWidget(split, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Apply).clicked.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        if self.files.count():
            self.files.setCurrentRow(0)

    @staticmethod
    def _count(o, n):
        return sum(1 for line in difflib.ndiff(o.splitlines(), n.splitlines()) if line.startswith("+ "))

    def _show(self, path):
        o, n = self.changes[path]
        self.diff.clear()
        cur = self.diff.textCursor()
        fmt = {"+": QTextCharFormat(), "-": QTextCharFormat(), "@": QTextCharFormat(), " ": QTextCharFormat()}
        fmt["+"].setBackground(QColor(35, 134, 54, 70))
        fmt["-"].setBackground(QColor(218, 54, 51, 70))
        fmt["@"].setForeground(QColor("#3c8fd8"))
        for line in difflib.unified_diff(o.splitlines(), n.splitlines(), "before", "after", n=2, lineterm=""):
            if line.startswith(("---", "+++")):
                continue
            cur.insertText(line + "\n", fmt.get(line[:1], fmt[" "]))
        self.diff.moveCursor(QTextCursor.Start)

    def selected(self):
        return [self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())
                if self.files.item(i).checkState() == Qt.Checked]
