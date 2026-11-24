"""Performance Profiler (VS: Debug > Performance Profiler, Alt+F2): runs the project with
Python's cProfile, then shows which functions took the time - a document tab like VS's report."""
import os
import pstats

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .language import is_generated


class _Num(QTableWidgetItem):
    """Sorts by number, shows formatted text."""

    def __init__(self, value, text):
        super().__init__(text)
        self.value = value
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other):
        return self.value < getattr(other, "value", 0)


def load_stats(path):
    """[{func, file, line, calls, own, total}] (seconds) from a cProfile output file."""
    st = pstats.Stats(path)
    rows = []
    for (file, line, func), (cc, nc, tt, ct, _callers) in st.stats.items():
        rows.append({"func": func, "file": file, "line": line, "calls": nc, "prim": cc, "own": tt, "total": ct})
    return rows, st.total_tt


class ProfileReport(QWidget):
    """The report tab."""
    dirty_changed = Signal(bool)
    open_location = Signal(str, int)

    def __init__(self, title, rows, total, root, parent=None):
        super().__init__(parent)
        self.path = None
        self.title = title
        self.rows, self.total, self.root = rows, max(total, 1e-9), os.path.realpath(root or "")
        self.ck_mine = QCheckBox("Just My Code")
        self.ck_mine.setToolTip("Only functions in your project (not Python's or Qt's code)")
        self.ck_mine.setChecked(True)
        self.ck_mine.toggled.connect(self._fill)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter functions")
        self.search.textChanged.connect(self._fill)
        self.head = QLabel()
        self.head.setStyleSheet("color: #cccccc; padding: 4px;")
        self.head.setWordWrap(True)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Function", "File", "Calls", "Own time (ms)", "Total time (ms)",
                                              "Total %"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSortingEnabled(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Interactive)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 260)
        for c in (2, 3, 4, 5):
            self.table.setColumnWidth(c, 110)
        self.table.cellDoubleClicked.connect(self._open)
        tip = QLabel("Own time: time spent in the function itself.  Total time: including the functions it "
                     "calls.  Double-click a row to go to the code.  Waiting for clicks (the event loop) counts "
                     "as time in main() / exec().")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #9a9a9a; padding: 2px 4px;")
        bar = QHBoxLayout()
        bar.addWidget(self.ck_mine)
        bar.addWidget(self.search, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.head)
        lay.addLayout(bar)
        lay.addWidget(self.table, 1)
        lay.addWidget(tip)
        self._fill()

    @property
    def is_dirty(self):
        return False

    def display_name(self):
        return self.title

    def is_mine(self, file):
        if not file or file.startswith("<") or file == "~":
            return False
        f = os.path.realpath(file)
        if not f.startswith(self.root + os.sep) or is_generated(f):
            return False
        rel = os.path.relpath(f, self.root).split(os.sep)
        return not rel[0] in (".venv", "venv", "publish", "build") and os.path.basename(f) != "pystudio_runtime.py"

    def _fill(self):
        mine = self.ck_mine.isChecked()
        t = self.search.text().lower().strip()
        rows = [r for r in self.rows if (not mine or self.is_mine(r["file"]))
                and (not t or t in r["func"].lower() or t in r["file"].lower())]
        rows.sort(key=lambda r: r["own"], reverse=True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            file = r["file"]
            short = os.path.relpath(file, self.root) if self.is_mine(file) else os.path.basename(file) or file
            name = QTableWidgetItem(r["func"])
            name.setData(Qt.UserRole, (file, r["line"]))
            where = QTableWidgetItem(f"{short}:{r['line']}" if r["line"] else short)
            where.setData(Qt.UserRole, (file, r["line"]))
            self.table.setItem(i, 0, name)
            self.table.setItem(i, 1, where)
            self.table.setItem(i, 2, _Num(r["calls"], f"{r['calls']:,}"))
            self.table.setItem(i, 3, _Num(r["own"], f"{r['own'] * 1000:,.1f}"))
            self.table.setItem(i, 4, _Num(r["total"], f"{r['total'] * 1000:,.1f}"))
            self.table.setItem(i, 5, _Num(r["total"] / self.total, f"{100 * r['total'] / self.total:.1f}"))
        self.table.setSortingEnabled(True)
        self.table.sortItems(3, Qt.DescendingOrder)
        mine_rows = [r for r in self.rows if self.is_mine(r["file"])]
        top = sorted(mine_rows, key=lambda r: r["own"], reverse=True)[:3]
        calls = sum(r["calls"] for r in self.rows)
        import html
        hot = ", ".join(f"{html.escape(r['func'])} ({r['own'] * 1000:,.0f} ms)" for r in top if r["own"] >= 0.0005)
        self.head.setText(f"<b>{self.total:.2f} s</b> of Python time, {calls:,} function calls."
                          + (f"  Slowest in your code (own time): {hot}" if hot else ""))

    def _open(self, row, _col):
        it = self.table.item(row, 0)
        file, line = it.data(Qt.UserRole) if it else (None, 0)
        if file and os.path.exists(file):
            self.open_location.emit(file, max(1, line))
