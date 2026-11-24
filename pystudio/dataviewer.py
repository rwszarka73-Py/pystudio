"""Debugger data viewer: a list, dict, DataFrame, array or query result from Locals or Watch as a grid,
like Visual Studio's DataTable / IEnumerable visualizer.

Click the magnifier next to a value (or right-click > View as Table) while paused. The grid sorts by any
column (click its header), filters as you type, copies the selection (Ctrl+C, with headers) and exports
to Excel or CSV. It shows the first 100,000 rows; the title says when there are more."""
import csv
import os

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QTableView, QVBoxLayout)

ROW_LIMIT = 100000


class TableModel(QAbstractTableModel):
    def __init__(self, columns, rows, parent=None):
        super().__init__(parent)
        self.columns, self.rows = columns, rows

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def value(self, r, c):
        row = self.rows[r]
        return row[c] if c < len(row) else None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        v = self.value(index.row(), index.column())
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            if v is None:
                return "" if role == Qt.DisplayRole else None
            if isinstance(v, bool):
                return str(v)
            if isinstance(v, float) and role == Qt.DisplayRole:
                return f"{v:,.6g}" if abs(v) >= 1e15 or (v and abs(v) < 1e-4) else f"{v:,.6f}".rstrip("0").rstrip(".")
            if isinstance(v, int) and role == Qt.DisplayRole:
                return f"{v:,}" if abs(v) >= 10000 else str(v)
            text = str(v)
            if role == Qt.ToolTipRole:
                return text if len(text) > 40 else None
            return text.replace("\n", " ⏎ ")
        if role == Qt.TextAlignmentRole and isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and v is None:
            return QColor("#6a6a6a")
        if role == Qt.UserRole:
            return v
        return None

    def headerData(self, section, orient, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        return self.columns[section] if orient == Qt.Horizontal else str(section + 1)


class _Proxy(QSortFilterProxyModel):
    """Numbers sort as numbers, empty cells last; the filter matches any column."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.words = []

    def set_filter(self, text):
        self.words = text.lower().split()
        self.invalidateFilter()

    def filterAcceptsRow(self, r, parent):
        if not self.words:
            return True
        m = self.sourceModel()
        hay = " ".join("" if m.value(r, c) is None else str(m.value(r, c)) for c in range(m.columnCount())).lower()
        return all(w in hay for w in self.words)

    def lessThan(self, a, b):
        va, vb = a.data(Qt.UserRole), b.data(Qt.UserRole)
        if va is None or vb is None:
            return vb is None and va is not None
        num = (int, float)
        if isinstance(va, num) and isinstance(vb, num) and not isinstance(va, bool) and not isinstance(vb, bool):
            return va < vb
        return str(va).lower() < str(vb).lower()


class DataViewerDialog(QDialog):
    def __init__(self, title, table, parent=None):
        """table: {"columns", "rows", "total", "kind"} from the debugger."""
        super().__init__(parent)
        self.title = title
        self.columns, self.rows = table.get("columns", []), table.get("rows", [])
        total = table.get("total", len(self.rows))
        more = f" - showing the first {len(self.rows):,} of {total:,}" if total > len(self.rows) else ""
        self.setWindowTitle(f"{title}  ({table.get('kind', '')}){more}")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1000, 600)
        self.model = TableModel(self.columns, self.rows, self)
        self.proxy = _Proxy(self)
        self.proxy.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(-1, Qt.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setAlternatingRowColors(True)
        self.view.setWordWrap(False)
        self.view.verticalHeader().setDefaultSectionSize(22)
        self.view.horizontalHeader().setStretchLastSection(len(self.columns) < 4)
        self.view.resizeColumnsToContents()
        for c in range(len(self.columns)):
            if self.view.columnWidth(c) > 360:
                self.view.setColumnWidth(c, 360)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter rows (any column)...")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        self.count = QLabel()
        b_xlsx = QPushButton("Export to Excel...")
        b_xlsx.clicked.connect(self.export_excel)
        b_csv = QPushButton("Export to CSV...")
        b_csv.clicked.connect(self.export_csv)
        b_copy = QPushButton("Copy")
        b_copy.setToolTip("Copy the selected cells (or everything) with the column names - pastes into Excel")
        b_copy.clicked.connect(self.copy)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.count)
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("Click a column header to sort. The data is a snapshot from when you opened it."), 1)
        for b in (b_copy, b_csv, b_xlsx, b_close):
            bottom.addWidget(b)
        v = QVBoxLayout(self)
        v.addLayout(top)
        v.addWidget(self.view, 1)
        v.addLayout(bottom)
        QShortcut(QKeySequence.Copy, self.view, self.copy)
        self._filter("")

    def _filter(self, text):
        self.proxy.set_filter(text)
        n = self.proxy.rowCount()
        self.count.setText(f"{n:,} row{'s' * (n != 1)}" + (f" of {len(self.rows):,}" if n != len(self.rows) else ""))

    def visible_rows(self):
        """The rows as shown (sorted and filtered)."""
        return [self.rows[self.proxy.mapToSource(self.proxy.index(r, 0)).row()] for r in range(self.proxy.rowCount())]

    def copy(self):
        sel = self.view.selectionModel().selectedIndexes()
        if len(sel) <= 1:
            cols, rows = list(range(len(self.columns))), list(range(self.proxy.rowCount()))
        else:
            cols = sorted({i.column() for i in sel})
            rows = sorted({i.row() for i in sel})

        def text(r, c):
            v = self.proxy.index(r, c).data(Qt.UserRole)
            return "" if v is None else str(v).replace("\t", " ").replace("\n", " ")
        lines = ["\t".join(self.columns[c] for c in cols)]
        lines += ["\t".join(text(r, c) for c in cols) for r in rows]
        QApplication.clipboard().setText("\n".join(lines))

    def _ask(self, caption, ext, flt):
        base = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in self.title)[:60] or "data"
        path, _f = QFileDialog.getSaveFileName(self, caption, os.path.join(os.path.expanduser("~"), base + ext), flt)
        if path and not path.lower().endswith(ext):
            path += ext
        return path

    def export_excel(self, path=None):
        path = path or self._ask("Export to Excel", ".xlsx", "Excel workbooks (*.xlsx)")
        if not path:
            return None
        from . import xlsxwriter_min
        try:
            xlsxwriter_min.write(path, [(self.title[:31] or "Data", self.columns, self.visible_rows())])
        except OSError as e:
            QMessageBox.warning(self, "Export to Excel", f"Couldn't save it: {e}")
            return None
        return path

    def export_csv(self, path=None):
        path = path or self._ask("Export to CSV", ".csv", "CSV files (*.csv)")
        if not path:
            return None
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(self.columns)
                for r in self.visible_rows():
                    w.writerow(["" if v is None else v for v in r])
        except OSError as e:
            QMessageBox.warning(self, "Export to CSV", f"Couldn't save it: {e}")
            return None
        return path


def open_viewer(session, ref, title, parent=None):
    """Asks the paused program for the table and shows it."""
    if not session.paused:
        return

    def got(msg):
        if msg.get("error"):
            QMessageBox.information(parent, "View as Table", msg["error"])
            return
        dlg = DataViewerDialog(title, msg, parent)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()                     # not modal: keep it open next to the code (its parent keeps it alive)
    session.table(ref, got, ROW_LIMIT)
