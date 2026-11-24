"""Find in Files / Replace in Files (VS: Ctrl+Shift+F / Ctrl+Shift+H)."""
import fnmatch
import os
import re

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout)

from .language import SKIP_DIRS, is_generated

SCOPES = ["Entire solution", "Entire project", "All open documents", "Current document"]
DEFAULT_TYPES = "*.py;*.sql;*.txt;*.md;*.json;*.toml"
MAX_FILES = 5000


def make_pattern(text, case=False, whole=False, regex=False):
    """Compiled regex for the search options; raises re.error for a bad regular expression."""
    pat = text if regex else re.escape(text)
    if whole:
        pat = rf"(?<!\w){pat}(?!\w)"
    return re.compile(pat, 0 if case else re.IGNORECASE)


def project_files(root, types=DEFAULT_TYPES, include_generated=False, skip_nested=False):
    """Files under root. skip_nested: leave out projects nested inside it (one project of a solution)."""
    masks = [t.strip() for t in re.split(r"[;,]", types or "*") if t.strip()] or ["*"]
    n = 0
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if skip_nested and dirpath != root and any(f.endswith(".pyproj") for f in files):
            dirnames[:] = []
            continue
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            if not any(fnmatch.fnmatch(f, m) for m in masks):
                continue
            if not include_generated and (is_generated(p) or f in ("pystudio_runtime.py", "my.py")):
                continue
            yield p
            n += 1
            if n >= MAX_FILES:
                return


def search_text(text, pattern):
    """[(line, col, line_text)] (1-based line, 0-based col) for every match."""
    out = []
    for i, line in enumerate(text.split("\n"), 1):
        for m in pattern.finditer(line):
            out.append((i, m.start(), line))
            if m.end() == m.start():       # empty regex match: don't loop forever
                break
    return out


def replace_text(text, pattern, replacement, regex=False):
    """Returns (new_text, count)."""
    repl = replacement if regex else replacement.replace("\\", "\\\\")
    return pattern.subn(repl, text)


class FindInFilesDialog(QDialog):
    def __init__(self, replace=False, initial="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Replace in Files" if replace else "Find in Files")
        self.resize(520, 0)
        s = QSettings()
        self.e_find = QComboBox()
        self.e_find.setEditable(True)
        self.e_find.addItems(s.value("findHistory", []) or [])
        self.e_find.setCurrentText(initial)
        self.e_replace = QComboBox()
        self.e_replace.setEditable(True)
        self.e_replace.addItems(s.value("replaceHistory", []) or [])
        self.e_replace.setCurrentText("")
        self.cb_scope = QComboBox()
        self.cb_scope.addItems(SCOPES)
        self.cb_scope.setCurrentText(s.value("findScope", SCOPES[0]))
        self.e_types = QLineEdit(s.value("findTypes", DEFAULT_TYPES))
        self.e_types.setToolTip("File types to search, separated by ; (e.g. *.py;*.sql). Generated files "
                                "(ui_*.py, my.py, pystudio_runtime.py) are skipped.")
        self.ck_case = QCheckBox("Match case")
        self.ck_whole = QCheckBox("Match whole word")
        self.ck_regex = QCheckBox("Use regular expressions")
        for ck, key in ((self.ck_case, "findCase"), (self.ck_whole, "findWhole"), (self.ck_regex, "findRegex")):
            ck.setChecked(s.value(key, False) in (True, "true"))
        self.error = QLabel()
        self.error.setStyleSheet("color: #f48771;")
        f = QFormLayout()
        f.addRow("Find what:", self.e_find)
        if replace:
            f.addRow("Replace with:", self.e_replace)
        f.addRow("Look in:", self.cb_scope)
        f.addRow("File types:", self.e_types)
        opts = QHBoxLayout()
        for ck in (self.ck_case, self.ck_whole, self.ck_regex):
            opts.addWidget(ck)
        opts.addStretch()
        b_go = QPushButton("Replace All" if replace else "Find All")
        b_go.setDefault(True)
        b_go.clicked.connect(self._go)
        b_cancel = QPushButton("Close")
        b_cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(b_go)
        row.addWidget(b_cancel)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addLayout(opts)
        if replace:
            note = QLabel("Changed files open in tabs, unsaved, so you can review them and undo with Ctrl+Z.")
            note.setStyleSheet("color: #999999;")
            note.setWordWrap(True)
            lay.addWidget(note)
        lay.addWidget(self.error)
        lay.addLayout(row)
        self.e_find.setFocus()
        self.e_find.lineEdit().selectAll()

    def _go(self):
        text = self.e_find.currentText()
        if not text:
            self.error.setText("Enter something to find.")
            return
        try:
            make_pattern(text, self.ck_case.isChecked(), self.ck_whole.isChecked(), self.ck_regex.isChecked())
        except re.error as e:
            self.error.setText(f"Regular expression error: {e}")
            return
        s = QSettings()
        for key, combo in (("findHistory", self.e_find), ("replaceHistory", self.e_replace)):
            hist = [combo.currentText()] + [h for h in (s.value(key, []) or []) if h != combo.currentText()]
            if combo.currentText():
                s.setValue(key, hist[:15])
        s.setValue("findScope", self.cb_scope.currentText())
        s.setValue("findTypes", self.e_types.text())
        s.setValue("findCase", self.ck_case.isChecked())
        s.setValue("findWhole", self.ck_whole.isChecked())
        s.setValue("findRegex", self.ck_regex.isChecked())
        self.accept()

    def options(self):
        return {"find": self.e_find.currentText(), "replace": self.e_replace.currentText(),
                "scope": self.cb_scope.currentText(), "types": self.e_types.text(),
                "case": self.ck_case.isChecked(), "whole": self.ck_whole.isChecked(),
                "regex": self.ck_regex.isChecked()}
