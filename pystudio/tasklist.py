"""Task List (View > Task List, Ctrl+\\, T): the TODO / HACK / FIXME / UNDONE / BUG comments in the
solution, like Visual Studio's. Double-click one to go to it."""
import os
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .findinfiles import project_files

TOKENS = ["TODO", "HACK", "FIXME", "UNDONE", "BUG"]
_RE = re.compile(r"(?:#|--)\s*(" + "|".join(TOKENS) + r")\b[:\s-]*(.*)$", re.IGNORECASE)
MAX_FILES = 3000


def scan_text(text):
    """[(line, token, description)]"""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if "#" not in line and "--" not in line:
            continue
        m = _RE.search(line)
        if m:
            # a # inside a string isn't a comment: check the part before it has balanced quotes
            before = line[:m.start()]
            if before.count('"') % 2 or before.count("'") % 2:
                continue
            out.append((i, m.group(1).upper(), m.group(2).strip()))
    return out


class TaskList(QWidget):
    open_location = Signal(str, int)

    def __init__(self):
        super().__init__()
        self.root = None
        self.open_texts = None        # callable -> {abs path: text} of open editors
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        self.cb_token = QComboBox()
        self.cb_token.addItems(["All"] + TOKENS)
        self.cb_token.currentIndexChanged.connect(self._fill)
        self.e_search = QLineEdit()
        self.e_search.setPlaceholderText("Search Task List")
        self.e_search.textChanged.connect(self._fill)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh)
        self.l_count = QLabel("")
        bar.addWidget(self.cb_token)
        bar.addWidget(self.e_search, 1)
        bar.addWidget(b)
        bar.addWidget(self.l_count)
        v.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Description", "Token", "File", "Line"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.setColumnWidth(0, 420)
        self.tree.setColumnWidth(1, 70)
        self.tree.setColumnWidth(2, 220)
        self.tree.itemActivated.connect(lambda it, _c: self.open_location.emit(*it.data(0, Qt.UserRole)))
        v.addWidget(self.tree, 1)
        self.tasks = []               # (path, line, token, text)

    def set_root(self, root):
        self.root = root
        self.refresh()

    def refresh(self):
        self.tasks = []
        if not self.root:
            self._fill()
            return
        opened = self.open_texts() if self.open_texts else {}
        for n, p in enumerate(project_files(self.root, "*.py;*.sql")):
            if n >= MAX_FILES:
                break
            p = os.path.abspath(p)
            text = opened.get(p)
            if text is None:
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
            for line, token, desc in scan_text(text):
                self.tasks.append((p, line, token, desc))
        self._fill()

    def update_file(self, path, text):
        """An editor saved/changed one file: rescan just it."""
        if not self.root or not path:
            return
        path = os.path.abspath(path)
        if not path.startswith(self.root + os.sep):
            return
        self.tasks = [t for t in self.tasks if t[0] != path]
        if path.endswith((".py", ".sql")):
            self.tasks += [(path, ln, tok, d) for ln, tok, d in scan_text(text)]
        self._fill()

    def _fill(self, *_):
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        want = self.cb_token.currentText()
        q = self.e_search.text().strip().lower()
        shown = 0
        for path, line, token, desc in sorted(self.tasks):
            if want != "All" and token != want:
                continue
            rel = os.path.relpath(path, self.root) if self.root else path
            if q and q not in desc.lower() and q not in rel.lower():
                continue
            it = QTreeWidgetItem([desc or "(no description)", token, rel, ""])
            it.setData(3, Qt.DisplayRole, line)
            it.setData(0, Qt.UserRole, (path, line))
            it.setToolTip(0, desc)
            self.tree.addTopLevelItem(it)
            shown += 1
        self.tree.setSortingEnabled(True)
        self.l_count.setText(f"{shown} task{'s' if shown != 1 else ''}")
