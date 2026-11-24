"""Error List and Find Results panels (like VS's)."""
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from .language import check_source, project_python_files
from . import theme


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.verticalHeader().setDefaultSectionSize(22)
    t.setStyleSheet("QTableWidget { background: #1e1e1e; color: #d4d4d4; border: 0; }"
                    "QTableWidget::item:selected { background: #094771; }"
                    "QHeaderView::section { background: #2d2d30; color: #cccccc; border: 0;"
                    " padding: 3px 6px; }")
    return t


class ErrorList(QWidget):
    activated = Signal(str, int, int)   # path, line, col
    counts_changed = Signal(int, int)   # errors, warnings

    def __init__(self, parent=None):
        super().__init__(parent)
        self.by_file = {}
        self.root = None

        def tb(text):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setChecked(True)
            b.setAutoRaise(True)
            b.setStyleSheet("QToolButton { padding: 2px 8px; color: #cccccc; }"
                            "QToolButton:checked { background: #3e3e42; }")
            b.toggled.connect(self.refresh)
            return b
        self.b_err = tb("✖ 0 Errors")
        self.b_warn = tb("⚠ 0 Warnings")
        self.scope = QLabel("")
        self.scope.setStyleSheet("color: #9a9a9a; padding-left: 8px;")
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        bar.addWidget(self.b_err)
        bar.addWidget(self.b_warn)
        bar.addWidget(self.scope)
        bar.addStretch()
        self.table = _table(["", "Description", "File", "Line"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 26)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._open)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.table)

    def set_file(self, path, diags):
        if not path:
            return
        path = os.path.abspath(path)
        if self.root and not path.startswith(self.root + os.sep):
            return   # only show the project's files (not library files opened via F12)
        self.by_file[path] = list(diags)
        self.refresh()

    def scan_project(self, root, open_texts=None):
        """Check every .py file in the project; open editors supply their unsaved text."""
        self.root = os.path.abspath(root) if root else None
        self.by_file = {}
        open_texts = open_texts or {}
        if self.root:
            for p in project_python_files(self.root):
                p = os.path.abspath(p)
                text = open_texts.get(p)
                if text is None:
                    try:
                        with open(p, encoding="utf-8", errors="replace") as f:
                            text = f.read()
                    except OSError:
                        continue
                self.by_file[p] = check_source(text, p)
            try:
                from .analysis import check_project_ruff
                for p, ds in check_project_ruff(self.root).items():
                    if p in self.by_file and p not in open_texts:      # open files get theirs from the editor
                        self.by_file[p] = self.by_file[p] + ds
            except Exception:  # noqa: BLE001
                pass
            self.scope.setText(f"Entire project: {os.path.basename(self.root)}")
        else:
            self.scope.setText("")
        self.refresh()

    def errors(self):
        return [d for ds in self.by_file.values() for d in ds if d.severity == "error"]

    def refresh(self):
        all_d = [d for ds in self.by_file.values() for d in ds]
        n_err = sum(1 for d in all_d if d.severity == "error")
        n_warn = len(all_d) - n_err
        self.b_err.setText(f"✖ {n_err} Error{'s' if n_err != 1 else ''}")
        self.b_warn.setText(f"⚠ {n_warn} Warning{'s' if n_warn != 1 else ''}")
        shown = [d for d in all_d if (d.severity == "error" and self.b_err.isChecked()) or
                 (d.severity != "error" and self.b_warn.isChecked())]
        shown.sort(key=lambda d: (d.severity != "error", d.path, d.line))
        self.table.setRowCount(0)
        for d in shown:
            r = self.table.rowCount()
            self.table.insertRow(r)
            icon = QTableWidgetItem("✖" if d.severity == "error" else "⚠")
            icon.setForeground(QColor(theme.c("#f14c4c")) if d.severity == "error" else QColor(theme.c("#cca700")))
            icon.setTextAlignment(Qt.AlignCenter)
            rel = os.path.relpath(d.path, self.root) if self.root else os.path.basename(d.path)
            items = [icon, QTableWidgetItem(d.message), QTableWidgetItem(rel), QTableWidgetItem(str(d.line))]
            items[1].setToolTip(f"{d.code}: {d.message}")
            for c, it in enumerate(items):
                it.setData(Qt.UserRole, (d.path, d.line, d.col))
                self.table.setItem(r, c, it)
        self.counts_changed.emit(n_err, n_warn)

    def _open(self, row, _col):
        it = self.table.item(row, 1)
        if it:
            path, line, col = it.data(Qt.UserRole)
            self.activated.emit(path, line, col)


class FindResults(QWidget):
    activated = Signal(str, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = QLabel("")
        self.title.setStyleSheet("color: #cccccc; padding: 3px 6px;")
        self.table = _table(["File", "Line", "Code"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self._open)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.title)
        lay.addWidget(self.table)

    def show_results(self, title, results, root=None):
        """results: [(path, line, col, code_text)]"""
        self.title.setText(f"{title}  -  {len(results)} result{'s' if len(results) != 1 else ''}")
        self.table.setRowCount(0)
        for path, line, col, text in results:
            r = self.table.rowCount()
            self.table.insertRow(r)
            rel = os.path.relpath(path, root) if root and path.startswith(root) else path
            items = [QTableWidgetItem(rel), QTableWidgetItem(str(line)), QTableWidgetItem(text)]
            for c, it in enumerate(items):
                it.setData(Qt.UserRole, (path, line, col))
                self.table.setItem(r, c, it)

    def _open(self, row, _col):
        it = self.table.item(row, 0)
        if it:
            path, line, col = it.data(Qt.UserRole)
            self.activated.emit(path, line, col)
