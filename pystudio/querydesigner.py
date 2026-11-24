"""Query Designer - a document tab for .sql query files: SQL editor, results grid and a
visual Query Builder (tables, columns, joins, filters, sorting, grouping), like VS's."""
import csv
import os
import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QKeySequence, QShortcut, QSyntaxHighlighter, \
    QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QSplitter, QStyle, QTableView, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget, QCheckBox)

from . import dbcore
from .theme import EDITOR_BG, EDITOR_FG, SELECTION, SYNTAX
from . import theme

SQL_KEYWORDS = set("""
select from where and or not in is null like between exists join inner left right full outer cross on
group by order having asc desc distinct as union all insert into values update set delete create table
view index drop alter add primary key foreign references default case when then else end limit offset
fetch first rows only with recursive count sum avg min max cast coalesce nullif upper lower trim
substr substring length current date time timestamp integer int varchar char decimal numeric real
text blob float double boolean commit rollback begin transaction pragma call top over partition
row_number rank dense_rank lead lag escape any some intersect except returning if
""".split())


class SqlHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)

        def fmt(color, italic=False):
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            f.setFontItalic(italic)
            return f
        self.f_kw = fmt(SYNTAX["keyword"])
        self.f_str = fmt(SYNTAX["string"])
        self.f_num = fmt(SYNTAX["number"])
        self.f_com = fmt(SYNTAX["comment"])
        self.f_param = fmt("#9cdcfe", italic=True)
        self.f_head = fmt("#608b4e", italic=True)

    def highlightBlock(self, text):
        state = self.previousBlockState()
        i = 0
        if state == 1:   # inside /* */
            end = text.find("*/")
            if end < 0:
                self.setFormat(0, len(text), self.f_com)
                self.setCurrentBlockState(1)
                return
            self.setFormat(0, end + 2, self.f_com)
            i = end + 2
        self.setCurrentBlockState(0)
        for m in re.finditer(r"('(?:[^']|'')*'?)|(--.*$)|(/\*)|(\b\d+(?:\.\d+)?\b)|((?<![:\w]):[A-Za-z_]\w*)|"
                             r"(\b[A-Za-z_]\w*\b)", text[i:]):
            s, e = m.start() + i, m.end() + i
            if m.group(1):
                self.setFormat(s, e - s, self.f_str)
            elif m.group(2):
                head = re.match(r"--\s*(pystudio query|connection:|param:|description:)", m.group(2), re.I)
                self.setFormat(s, e - s, self.f_head if head else self.f_com)
            elif m.group(3):
                end = text.find("*/", e)
                if end < 0:
                    self.setFormat(s, len(text) - s, self.f_com)
                    self.setCurrentBlockState(1)
                    return
                self.setFormat(s, end + 2 - s, self.f_com)
            elif m.group(4):
                self.setFormat(s, e - s, self.f_num)
            elif m.group(5):
                self.setFormat(s, e - s, self.f_param)
            elif m.group(6) and m.group(6).lower() in SQL_KEYWORDS:
                self.setFormat(s, e - s, self.f_kw)


class SqlEdit(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSize(11)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setStyleSheet(f"QPlainTextEdit {{ background: {EDITOR_BG}; color: {EDITOR_FG}; border: 0;"
                           f" selection-background-color: {SELECTION}; }}")
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.hl = SqlHighlighter(self.document())
        self.assist = None                    # sqlassist.SqlCompleter, set by QueryDocument

    def keyPressEvent(self, e):
        if self.assist is not None and self.assist.key_before(e):
            return
        self._key(e)
        if self.assist is not None:
            self.assist.key_after(e)

    def _key(self, e):
        if e.key() == Qt.Key_Tab and not e.modifiers():
            self.insertPlainText("    ")
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers():
            line = self.textCursor().block().text()
            indent = re.match(r"\s*", line).group(0)
            super().keyPressEvent(e)
            self.insertPlainText(indent)
            return
        super().keyPressEvent(e)


# ============================================================ results grid
class ResultModel(QAbstractTableModel):
    def __init__(self, table, parent=None):
        super().__init__(parent)
        self.table = table
        self.italic = QFont()
        self.italic.setItalic(True)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.table)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.table.columns)

    def value(self, row, col):
        return tuple.__getitem__(self.table[row], col)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        v = self.value(index.row(), index.column())
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            if v is None:
                return "NULL"
            t = dbcore.to_text(v)
            return t if role == Qt.DisplayRole or len(t) > 60 else None
        if role == Qt.ForegroundRole and v is None:
            return QColor(theme.c("#808080"))
        if role == Qt.FontRole and v is None:
            return self.italic
        if role == Qt.TextAlignmentRole and dbcore.is_number(v):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def headerData(self, section, orient, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orient == Qt.Horizontal:
            return str(self.table.columns[section])
        return str(section + 1)


class _SortProxy(QSortFilterProxyModel):
    def lessThan(self, a, b):
        m = self.sourceModel()
        x, y = m.value(a.row(), a.column()), m.value(b.row(), b.column())
        if x is None or y is None:
            return x is None and y is not None
        try:
            return x < y
        except TypeError:
            return str(x) < str(y)


class ResultGrid(QTableView):
    def __init__(self, table, parent=None):
        super().__init__(parent)
        self.model_ = ResultModel(table, self)
        self.proxy = _SortProxy(self)
        self.proxy.setSourceModel(self.model_)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.sortByColumn(-1, Qt.AscendingOrder)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.verticalHeader().setDefaultSectionSize(22)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.resizeColumnsToContents()
        for i in range(self.model_.columnCount()):
            if self.columnWidth(i) > 320:
                self.setColumnWidth(i, 320)
        QShortcut(QKeySequence.Copy, self, self.copy_selection, context=Qt.WidgetShortcut)

    @property
    def table(self):
        return self.model_.table

    def copy_selection(self):
        idx = self.selectedIndexes()
        if not idx:
            return
        rows = sorted({i.row() for i in idx})
        cols = sorted({i.column() for i in idx})
        sel = {(i.row(), i.column()) for i in idx}
        lines = []
        if len(rows) > 1 or len(cols) > 1:
            lines.append("\t".join(self.model_.headerData(self.proxy.mapToSource(self.proxy.index(0, c)).column(),
                                                          Qt.Horizontal) for c in cols))
        for r in rows:
            cells = []
            for c in cols:
                if (r, c) in sel:
                    src = self.proxy.mapToSource(self.proxy.index(r, c))
                    cells.append(dbcore.to_text(self.model_.value(src.row(), src.column())))
                else:
                    cells.append("")
            lines.append("\t".join(cells))
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText("\n".join(lines))


class ParamsDialog(QDialog):
    def __init__(self, params, last, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Query Parameters")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Values for the query's parameters (type NULL for no value):"))
        f = QFormLayout()
        self.edits = {}
        for name, default in params.items():
            e = QLineEdit(str(last.get(name, default if default is not None else "")))
            self.edits[name] = e
            f.addRow(f":{name}", e)
        lay.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        if self.edits:
            next(iter(self.edits.values())).selectAll()

    def values(self):
        return {n: e.text() for n, e in self.edits.items()}


# ============================================================ query builder
SORT_TYPES = ["", "Ascending", "Descending"]
GROUP_TYPES = ["", "Group By", "Count", "Sum", "Avg", "Min", "Max"]


class _TableTree(QTreeWidget):
    """The table list: drag a table onto the diagram."""

    def mimeTypes(self):
        return ["application/x-pystudio-table"]

    def mimeData(self, items):
        from PySide6.QtCore import QMimeData
        md = QMimeData()
        key = items[0].data(0, Qt.UserRole) if items else None
        if key and items[0].parent() is None:
            md.setData("application/x-pystudio-table", f"{key[0] or ''}\x1f{key[1]}".encode())
        return md


class QueryBuilder(QWidget):
    """Tables with checkable columns (left), the Diagram pane (tables and joins) and a criteria grid that write
    the SQL."""
    changed = Signal()

    COLS = ["Column", "Alias", "Table", "Output", "Sort Type", "Sort Order", "Group By", "Filter"]

    def __init__(self, doc):
        super().__init__(doc)
        self.doc = doc
        self.tables = []         # [(schema, table)] in the order they were added
        self.columns = {}        # (schema, table) -> ([(name, type, key)], fks)
        self.joins = []          # [{"a": (key, column), "b": (key, column), "type": "INNER"|"LEFT"|"RIGHT"|"FULL"}]
        self.pinned = set()      # tables added to the diagram (stay without checked columns)
        self._loading = False

        self.tree = _TableTree()
        self.tree.setHeaderLabel("Tables and views - check columns")
        self.tree.setToolTip("Double-click or drag a table onto the diagram; expand it to check columns")
        self.tree.setDragEnabled(True)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(self._tree_double_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        from .querydiagram import QueryDiagram
        self.diagram = QueryDiagram(self)
        self.diagram.table_dropped.connect(self.add_table)
        self.changed.connect(self.diagram.sync)
        self.grid = QTableWidget(0, len(self.COLS))
        self.grid.setHorizontalHeaderLabels(self.COLS)
        self.grid.verticalHeader().hide()
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.grid.horizontalHeader().setStretchLastSection(True)
        self.grid.itemChanged.connect(lambda _i: not self._loading and self.changed.emit())
        self.grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.distinct = QCheckBox("Distinct")
        self.distinct.toggled.connect(lambda _v: self.changed.emit())
        b_up = QToolButton()
        b_up.setArrowType(Qt.UpArrow)
        b_up.clicked.connect(lambda: self._move(-1))
        b_down = QToolButton()
        b_down.setArrowType(Qt.DownArrow)
        b_down.clicked.connect(lambda: self._move(1))
        b_remove = QPushButton("Remove")
        b_remove.clicked.connect(self._remove_rows)
        tip = QLabel("<small>Filter examples:  = 'NE'   &gt; 100   LIKE 'A%'   :state  (a parameter)   "
                     "IS NULL</small>")
        tools = QHBoxLayout()
        tools.addWidget(self.distinct)
        tools.addWidget(tip, 1)
        for b in (b_up, b_down, b_remove):
            tools.addWidget(b)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.grid, 1)
        rl.addLayout(tools)
        panes = QSplitter(Qt.Vertical)
        panes.addWidget(self.diagram)
        panes.addWidget(right)
        panes.setSizes([220, 200])
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(panes)
        split.setSizes([240, 700])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split)

    # ------------------------------------------------------------ tables
    def load_tables(self):
        self.tree.clear()
        cfg = self.doc.cfg()
        if cfg is None:
            self.tree.addTopLevelItem(QTreeWidgetItem(["(choose a connection)"]))
            return
        pw = self.doc.password()
        if pw is None:
            return
        self.tree.addTopLevelItem(QTreeWidgetItem(["Loading..."]))
        self.doc.service.tables(cfg, pw, self._fill_tables, self._failed)

    def _failed(self, msg):
        self.tree.clear()
        self.tree.addTopLevelItem(QTreeWidgetItem([msg.splitlines()[0]]))

    def _fill_tables(self, rows):
        self._loading = True
        self.tree.clear()
        multi = len({r[0] for r in rows}) > 1
        icon = self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
        for schema, name, kind in rows:
            it = QTreeWidgetItem([(f"{schema}.{name}" if multi and schema else name) +
                                  ("  (view)" if kind == "VIEW" else "")])
            it.setIcon(0, icon)
            it.setData(0, Qt.UserRole, (schema, name))
            it.addChild(QTreeWidgetItem(["Loading..."]))
            self.tree.addTopLevelItem(it)
        self._loading = False

    def _expanded(self, it):
        key = it.data(0, Qt.UserRole)
        if not key or it.data(0, Qt.UserRole + 1):
            return
        it.setData(0, Qt.UserRole + 1, True)
        cfg, pw = self.doc.cfg(), self.doc.password()
        if cfg is None or pw is None:
            return
        self.doc.service.columns(cfg, pw, key[0], key[1], lambda res, it=it: self._fill_columns(it, res),
                                 lambda msg, it=it: (it.takeChildren(), it.addChild(QTreeWidgetItem([msg]))))

    def _fill_columns(self, it, res):
        key = tuple(it.data(0, Qt.UserRole))
        self.columns[key] = res
        self.diagram.sync()
        self._loading = True
        it.takeChildren()
        for name, typ, pk in [("*", "", False)] + list(res[0]):
            c = QTreeWidgetItem([name if name == "*" else f"{name}  {typ}" + ("  [PK]" if pk else "")])
            c.setData(0, Qt.UserRole + 2, name)
            c.setFlags(c.flags() | Qt.ItemIsUserCheckable)
            c.setCheckState(0, Qt.Checked if self._row_of(key, name) >= 0 else Qt.Unchecked)
            it.addChild(c)
        self._loading = False

    def _item_changed(self, it, _col):
        if self._loading or it.parent() is None:
            return
        key = tuple(it.parent().data(0, Qt.UserRole))
        name = it.data(0, Qt.UserRole + 2)
        if it.checkState(0) == Qt.Checked:
            if self._row_of(key, name) < 0:
                self.add_column(key, name)
        else:
            r = self._row_of(key, name)
            if r >= 0:
                self.grid.removeRow(r)
                self._prune_tables()
                self.changed.emit()

    # ------------------------------------------------------------ diagram: tables, columns, joins
    def _tree_double_clicked(self, it, _col):
        key = it.data(0, Qt.UserRole)
        if key and it.parent() is None:
            self.add_table(tuple(key))

    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        key = it.data(0, Qt.UserRole) if it is not None and it.parent() is None else None
        if not key:
            return
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        if tuple(key) in self.tables:
            m.addAction("Remove from Query", lambda: self.remove_table(tuple(key)))
        else:
            m.addAction("Add to Diagram", lambda: self.add_table(tuple(key)))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def ensure_columns(self, key):
        key = tuple(key)
        if key in self.columns:
            return
        cfg, pw = self.doc.cfg(), self.doc.password()
        if cfg is None or pw is None:
            return
        self.columns[key] = None                     # loading

        def got(res, key=key):
            self.columns[key] = res
            self.diagram.sync()

        def failed(_msg, key=key):
            self.columns.pop(key, None)
        self.doc.service.columns(cfg, pw, key[0], key[1], got, failed)

    def add_table(self, key):
        key = tuple(key)
        self.pinned.add(key)
        if key not in self.tables:
            self.tables.append(key)
        self.ensure_columns(key)
        self.changed.emit()

    def remove_table(self, key):
        key = tuple(key)
        for r in reversed(range(self.grid.rowCount())):
            d = self.grid.item(r, 0).data(Qt.UserRole)
            if tuple(d[0]) == key:
                self.grid.removeRow(r)
                self._uncheck(d[0], d[1])
        self.pinned.discard(key)
        self.joins = [j for j in self.joins if tuple(j["a"][0]) != key and tuple(j["b"][0]) != key]
        self.tables = [t for t in self.tables if t != key]
        self.changed.emit()

    def has_column(self, key, name):
        return self._row_of(key, name) >= 0

    def toggle_column(self, key, name):
        r = self._row_of(key, name)
        if r >= 0:
            self.grid.removeRow(r)
            self._uncheck(key, name)
            self.pinned.add(tuple(key))           # unchecking in the diagram keeps the box
            self._prune_tables()
            self.changed.emit()
        else:
            self.add_column(key, name)
            self._check(key, name)

    def _check(self, key, name):
        self._loading = True
        for i in range(self.tree.topLevelItemCount()):
            t = self.tree.topLevelItem(i)
            if tuple(t.data(0, Qt.UserRole) or ()) == tuple(key):
                for j in range(t.childCount()):
                    if t.child(j).data(0, Qt.UserRole + 2) == name:
                        t.child(j).setCheckState(0, Qt.Checked)
        self._loading = False

    def add_join(self, a, b):
        a = (tuple(a[0]), a[1])
        b = (tuple(b[0]), b[1])
        if a[0] == b[0]:
            return
        for j in self.joins:
            if {j["a"], j["b"]} == {a, b}:
                return
        for k in (a[0], b[0]):
            if k not in self.tables:
                self.tables.append(k)
            self.pinned.add(k)
        self.joins.append({"a": a, "b": b, "type": "INNER"})
        self.changed.emit()

    def set_join_type(self, join, jt):
        join["type"] = jt
        self.changed.emit()

    def remove_join(self, join):
        self.joins = [j for j in self.joins if j is not join]
        self.changed.emit()

    def auto_joins(self):
        """Joins the SQL takes from foreign keys (tables with no drawn join to the ones before them)."""
        out = []
        placed = self.tables[:1]
        for key in self.tables[1:]:
            if not self._explicit_for(key, placed):
                j = self._fk_join(key, placed)
                if j:
                    out.append(j)
            placed.append(key)
        return out

    def _explicit_for(self, key, placed):
        return [j for j in self.joins if (tuple(j["b"][0]) == key and tuple(j["a"][0]) in placed) or
                (tuple(j["a"][0]) == key and tuple(j["b"][0]) in placed)]

    def _fk_join(self, key, placed):
        cols, fks = self.columns.get(key) or ([], [])
        for fcol, _rs, rt, rcol in fks:          # this table references a placed one
            for p in placed:
                if p[1].lower() == rt.lower():
                    return {"a": (p, rcol), "b": (key, fcol), "type": "INNER"}
        for p in placed:                          # a placed table references this one
            for fcol, _rs, rt, rcol in (self.columns.get(p) or ([], []))[1]:
                if rt.lower() == key[1].lower():
                    return {"a": (p, fcol), "b": (key, rcol), "type": "INNER"}
        return None

    # ------------------------------------------------------------ criteria rows
    def _row_of(self, key, name):
        for r in range(self.grid.rowCount()):
            d = self.grid.item(r, 0).data(Qt.UserRole) if self.grid.item(r, 0) else None
            if d and tuple(d[0]) == tuple(key) and d[1] == name:
                return r
        return -1

    def add_column(self, key, name):
        if tuple(key) not in self.tables:
            self.tables.append(tuple(key))
        self.ensure_columns(key)
        self._loading = True
        r = self.grid.rowCount()
        self.grid.insertRow(r)
        c0 = QTableWidgetItem(name)
        c0.setFlags(c0.flags() & ~Qt.ItemIsEditable)
        c0.setData(Qt.UserRole, (tuple(key), name))
        self.grid.setItem(r, 0, c0)
        self.grid.setItem(r, 1, QTableWidgetItem(""))
        t = QTableWidgetItem(key[1])
        t.setFlags(t.flags() & ~Qt.ItemIsEditable)
        self.grid.setItem(r, 2, t)
        out = QTableWidgetItem()
        out.setFlags((out.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        out.setCheckState(Qt.Checked)
        self.grid.setItem(r, 3, out)
        for col, choices in ((4, SORT_TYPES), (6, GROUP_TYPES)):
            cb = QComboBox()
            cb.addItems(choices)
            cb.currentIndexChanged.connect(lambda _i: self.changed.emit())
            if name == "*" and col == 6:
                cb.setEnabled(False)
            self.grid.setCellWidget(r, col, cb)
        so = QSpinBox()
        so.setRange(0, 99)
        so.setSpecialValueText(" ")
        so.valueChanged.connect(lambda _v: self.changed.emit())
        self.grid.setCellWidget(r, 5, so)
        self.grid.setItem(r, 7, QTableWidgetItem(""))
        self._loading = False
        self.changed.emit()

    def _move(self, delta):
        r = self.grid.currentRow()
        if r < 0 or not (0 <= r + delta < self.grid.rowCount()):
            return
        rows = [self._row_state(i) for i in range(self.grid.rowCount())]
        rows[r], rows[r + delta] = rows[r + delta], rows[r]
        self._set_rows(rows)
        self.grid.selectRow(r + delta)
        self.changed.emit()

    def _remove_rows(self):
        rows = sorted({i.row() for i in self.grid.selectedIndexes()}, reverse=True)
        for r in rows:
            d = self.grid.item(r, 0).data(Qt.UserRole)
            self.grid.removeRow(r)
            self._uncheck(d[0], d[1])
        self._prune_tables()
        self.changed.emit()

    def _uncheck(self, key, name):
        self._loading = True
        for i in range(self.tree.topLevelItemCount()):
            t = self.tree.topLevelItem(i)
            if tuple(t.data(0, Qt.UserRole) or ()) == tuple(key):
                for j in range(t.childCount()):
                    if t.child(j).data(0, Qt.UserRole + 2) == name:
                        t.child(j).setCheckState(0, Qt.Unchecked)
        self._loading = False

    def _prune_tables(self):
        used = {tuple(self.grid.item(r, 0).data(Qt.UserRole)[0]) for r in range(self.grid.rowCount())}
        used |= self.pinned
        for j in self.joins:
            used |= {tuple(j["a"][0]), tuple(j["b"][0])}
        self.tables = [t for t in self.tables if t in used]

    def _row_state(self, r):
        g = self.grid
        return {"key": g.item(r, 0).data(Qt.UserRole), "alias": g.item(r, 1).text(),
                "output": g.item(r, 3).checkState() == Qt.Checked,
                "sort": g.cellWidget(r, 4).currentText(), "order": g.cellWidget(r, 5).value(),
                "group": g.cellWidget(r, 6).currentText(), "filter": g.item(r, 7).text()}

    def _set_rows(self, rows):
        self._loading = True
        self.grid.setRowCount(0)
        self._loading = False
        for s in rows:
            key, name = s["key"]
            self.add_column(key, name)
            r = self.grid.rowCount() - 1
            self._loading = True
            self.grid.item(r, 1).setText(s["alias"])
            self.grid.item(r, 3).setCheckState(Qt.Checked if s["output"] else Qt.Unchecked)
            self.grid.cellWidget(r, 4).setCurrentText(s["sort"])
            self.grid.cellWidget(r, 5).setValue(s["order"])
            self.grid.cellWidget(r, 6).setCurrentText(s["group"])
            self.grid.item(r, 7).setText(s["filter"])
            self._loading = False

    # ------------------------------------------------------------ SQL
    def sql(self):
        rows = [self._row_state(r) for r in range(self.grid.rowCount())]
        if not rows or not self.tables:
            return ""
        provider = (self.doc.cfg() or {}).get("provider", "SQLite")
        multi = len(self.tables) > 1

        def tref(key):
            return dbcore.quote_ident(key[1], provider)

        def col(key, name):
            if name == "*":
                return f"{tref(key)}.*" if multi else "*"
            c = dbcore.quote_ident(name, provider)
            return f"{tref(key)}.{c}" if multi else c

        grouping = any(s["group"] for s in rows)
        select, where, order, group = [], [], [], []
        for i, s in enumerate(rows):
            key, name = tuple(s["key"][0]), s["key"][1]
            expr = col(key, name)
            g = s["group"]
            if grouping and g and g != "Group By" and name != "*":
                agg = f"{g.upper()}({expr})"
            else:
                agg = expr
            if grouping and (g == "Group By" or (not g and name != "*")):
                group.append(expr)
            if s["output"]:
                select.append(agg + (f" AS {dbcore.quote_ident(s['alias'], provider)}" if s["alias"] else ""))
            f = s["filter"].strip()
            if f:
                if f.startswith(":"):
                    f = "= " + f
                elif not re.match(r"(?i)(=|<|>|!|like\b|not\b|in\b|is\b|between\b)", f):
                    f = "= " + f
                if grouping and g and g != "Group By":
                    s["_having"] = f"{agg} {f}"
                else:
                    where.append(f"{expr} {f}")
            if s["sort"]:
                order.append((s["order"] or 100 + i, (s["alias"] and dbcore.quote_ident(s["alias"], provider))
                              or agg, s["sort"] == "Descending"))
        having = [s["_having"] for s in rows if s.get("_having")]

        lines = ["SELECT " + ("DISTINCT " if self.distinct.isChecked() else "") + (",\n       ".join(select) or "*")]
        first = self.tables[0]
        lines.append(f"FROM {dbcore.qualified(first[0], first[1], provider)}" +
                     (f" {tref(first)}" if multi and first[0] else ""))
        placed = [first]
        q = dbcore.quote_ident
        words = {"INNER": "INNER JOIN", "LEFT": "LEFT OUTER JOIN", "RIGHT": "RIGHT OUTER JOIN",
                 "FULL": "FULL OUTER JOIN"}
        for key in self.tables[1:]:
            conds, jt = [], None
            for j in self._explicit_for(key, placed):
                (ka, ca), (kb, cb) = j["a"], j["b"]
                conds.append(f"{tref(tuple(ka))}.{q(ca, provider)} = {tref(tuple(kb))}.{q(cb, provider)}")
                if jt is None:      # "a LEFT JOIN b" keeps a's rows; written from the other side it's RIGHT
                    t = j.get("type", "INNER")
                    jt = t if tuple(kb) == key else {"LEFT": "RIGHT", "RIGHT": "LEFT"}.get(t, t)
            if conds:
                kw, cond = words[jt or "INNER"], " AND ".join(conds)
            else:
                kw, cond = "INNER JOIN", self._join_condition(key, placed, provider)
            lines.append(f"{kw} {dbcore.qualified(key[0], key[1], provider)}" +
                         (f" {tref(key)}" if key[0] else "") + f" ON {cond}")
            placed.append(key)
        if where:
            lines.append("WHERE " + "\n  AND ".join(where))
        if group:
            lines.append("GROUP BY " + ", ".join(dict.fromkeys(group)))
        if having:
            lines.append("HAVING " + "\n   AND ".join(having))
        if order:
            order.sort(key=lambda o: o[0])
            lines.append("ORDER BY " + ", ".join(e + (" DESC" if d else "") for _o, e, d in order))
        return "\n".join(lines)

    def _join_condition(self, key, placed, provider):
        q = dbcore.quote_ident
        cols, fks = self.columns.get(key) or ([], [])
        for fcol, _rs, rt, rcol in fks:          # this table references a placed one
            for p in placed:
                if p[1].lower() == rt.lower():
                    return f"{q(key[1], provider)}.{q(fcol, provider)} = {q(p[1], provider)}.{q(rcol, provider)}"
        for p in placed:                          # a placed table references this one
            for fcol, _rs, rt, rcol in (self.columns.get(p) or ([], []))[1]:
                if rt.lower() == key[1].lower():
                    return f"{q(p[1], provider)}.{q(fcol, provider)} = {q(key[1], provider)}.{q(rcol, provider)}"
        return "1 = 1  -- set the join condition"


# ============================================================ the document
class QueryDocument(QWidget):
    dirty_changed = Signal(bool)
    saved = Signal(str)

    def __init__(self, service, project, path=None, connection="", sql="", parent=None):
        super().__init__(parent)
        self.service = service
        self.project = project
        self.path = path
        self.untitled_name = "Query1.sql"
        self.last_params = {}
        self._generated = None
        self._running = False

        self.cb_conn = QComboBox()
        self.cb_conn.setMinimumWidth(180)
        self.reload_connections()
        self.cb_conn.currentTextChanged.connect(self._conn_changed)
        self.b_run = QPushButton(" Execute")
        self.b_run.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.b_run.setToolTip("Execute (F5 or Ctrl+Enter). Runs only the selected text when there's a selection.")
        self.b_run.clicked.connect(self.execute)
        self.sp_limit = QSpinBox()
        self.sp_limit.setRange(0, 1_000_000)
        self.sp_limit.setValue(1000)
        self.sp_limit.setSpecialValueText("All")
        self.sp_limit.setSingleStep(500)
        self.sp_limit.setToolTip("Most rows to show (0 = all). Apps always get all rows.")
        self.b_builder = QPushButton("Query Builder")
        self.b_builder.setCheckable(True)
        self.b_builder.toggled.connect(self._toggle_builder)
        self.b_csv = QToolButton()            # Export: CSV, Excel, all results to Excel
        self.b_csv.setText("Export")
        self.b_csv.setPopupMode(QToolButton.InstantPopup)
        self.b_csv.setToolButtonStyle(Qt.ToolButtonTextOnly)
        from PySide6.QtWidgets import QMenu
        em = QMenu(self.b_csv)
        em.addAction("Export to CSV...", self.export_csv)
        em.addAction("Export to Excel...", self.export_excel)
        self.a_export_all = em.addAction("Export All Results to Excel (one sheet each)...",
                                         lambda: self.export_excel(all_results=True))
        self.b_csv.setMenu(em)
        self.b_csv.setToolTip("Save the results as a CSV file or an Excel workbook")
        self.b_csv.setEnabled(False)
        self._last_run = None                 # (cfg, statements, params) - Export can run it again for all rows
        self.lbl = QLabel()
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 4)
        bar.addWidget(QLabel("Connection:"))
        bar.addWidget(self.cb_conn)
        bar.addWidget(self.b_run)
        bar.addWidget(QLabel("Max rows:"))
        bar.addWidget(self.sp_limit)
        bar.addWidget(self.b_builder)
        bar.addWidget(self.b_csv)
        self.b_history = QToolButton()
        self.b_history.setText("History")
        self.b_history.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.b_history.setPopupMode(QToolButton.MenuButtonPopup)
        self.b_history.setToolTip("Every query you've executed: search it, run one again (Ctrl+H)")
        self.b_history.clicked.connect(self.show_history)
        hm = QMenu(self.b_history)
        hm.addAction("Query History...", self.show_history)
        hm.addAction("Refresh Table and Column Names (IntelliSense)", self.refresh_schema)
        self.b_history.setMenu(hm)
        bar.addWidget(self.b_history)
        bar.addWidget(self.lbl, 1)

        self.editor = SqlEdit()
        self.editor.document().modificationChanged.connect(self.dirty_changed)
        from .sqlassist import SqlCompleter
        self.assist = self.editor.assist = SqlCompleter(self, self.editor)
        self.builder = QueryBuilder(self)
        self.builder.hide()
        self.builder.changed.connect(self._builder_changed)
        self.results = QTabWidget()
        self.results.setDocumentMode(True)
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.results.addTab(self.messages, "Messages")

        self.split = QSplitter(Qt.Vertical)
        self.split.addWidget(self.builder)
        self.split.addWidget(self.editor)
        self.split.addWidget(self.results)
        self.split.setSizes([0, 260, 300])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(self.split, 1)

        for keys in ("Ctrl+Return", "Ctrl+Enter", "Ctrl+Shift+E"):
            QShortcut(QKeySequence(keys), self, self.execute, context=Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence("Ctrl+H"), self, self.show_history, context=Qt.WidgetWithChildrenShortcut)
        QTimer.singleShot(0, self.assist.warm)

        if path:
            self.load(path)
        else:
            self.editor.setPlainText(dbcore.format_query_file(connection, sql or "SELECT \nFROM "))
            self.editor.document().setModified(False)
            self._sync_combo()
            if not sql:
                c = self.editor.textCursor()
                c.movePosition(QTextCursor.End)
                self.editor.setTextCursor(c)

    # ------------------------------------------------------------ doc API (like CodeEditor)
    @property
    def is_dirty(self):
        return self.editor.document().isModified()

    def display_name(self):
        return os.path.basename(self.path) if self.path else self.untitled_name

    def setFocus(self):
        self.editor.setFocus()

    def load(self, path):
        with open(path, encoding="utf-8", errors="replace") as f:
            self.editor.setPlainText(f.read())
        self.path = path
        self.editor.document().setModified(False)
        self._sync_combo()

    def save(self, path=None):
        path = path or self.path
        if not path:
            return False
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                text = self.editor.toPlainText()
                f.write(text if text.endswith("\n") else text + "\n")
        except OSError as e:
            QMessageBox.critical(self, "Save failed", f"Could not save {path}:\n{e}")
            return False
        self.path = path
        self.editor.document().setModified(False)
        self.saved.emit(path)
        return True

    # ------------------------------------------------------------ connection
    def reload_connections(self):
        current = self.cb_conn.currentText()
        self.cb_conn.blockSignals(True)
        self.cb_conn.clear()
        names = [c["name"] for c in (self.project.data.get("connections", []) if self.project else [])]
        self.cb_conn.addItems(names)
        if current in names:
            self.cb_conn.setCurrentText(current)
        self.cb_conn.blockSignals(False)

    def _header_connection(self):
        return dbcore.parse_query_file(self.editor.toPlainText())["connection"]

    def _sync_combo(self):
        name = self._header_connection()
        self.cb_conn.blockSignals(True)
        if name and self.cb_conn.findText(name) < 0:
            self.cb_conn.addItem(name)
        if name:
            self.cb_conn.setCurrentText(name)
        self.cb_conn.blockSignals(False)
        if not name and self.cb_conn.count():
            self._conn_changed(self.cb_conn.currentText())

    def _conn_changed(self, name):
        text = self.editor.toPlainText()
        if re.search(r"(?mi)^--\s*connection\s*:.*$", text):
            new = re.sub(r"(?mi)^--\s*connection\s*:.*$", f"-- connection: {name}", text, count=1)
        elif text.lstrip().lower().startswith("-- pystudio query"):
            new = re.sub(r"(?i)^(\s*-- pystudio query[^\n]*\n)", rf"\1-- connection: {name}\n", text, count=1)
        else:
            new = f"-- connection: {name}\n" + text
        if new != text:
            c = self.editor.textCursor()
            c.beginEditBlock()
            c.select(QTextCursor.Document)
            c.insertText(new)
            c.endEditBlock()
        if self.builder.isVisible():
            self.builder.load_tables()
        self.assist.warm()

    def cfg(self):
        name = self.cb_conn.currentText()
        for c in (self.project.data.get("connections", []) if self.project else []):
            if c.get("name") == name:
                return c
        return None

    def password(self):
        cfg = self.cfg()
        return None if cfg is None else self.service.password_for(cfg, self)

    # ------------------------------------------------------------ builder
    def _toggle_builder(self, on):
        self.builder.setVisible(on)
        if on:
            sizes = self.split.sizes()
            self.split.setSizes([300, max(150, sizes[1] - 150), max(150, sizes[2] - 150)])
            if self.builder.tree.topLevelItemCount() == 0:
                self.builder.load_tables()

    def _body(self):
        return dbcore.parse_query_file(self.editor.toPlainText())["sql"]

    def _builder_changed(self):
        sql = self.builder.sql()
        body = self._body()
        if self._generated is not None and body.strip() and body.strip() != self._generated.strip():
            r = QMessageBox.question(self, "Query Builder", "Replace the SQL you typed with the built query?")
            if r != QMessageBox.Yes:
                return
        elif self._generated is None and body.strip() and not re.fullmatch(r"(?is)select\s*from\s*", body.strip()):
            r = QMessageBox.question(self, "Query Builder", "Replace the SQL in the editor with the built query?")
            if r != QMessageBox.Yes:
                return
        self._generated = sql
        text = self.editor.toPlainText()
        # keep the header comment lines, replace the body
        head = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("--") and re.match(r"--\s*(pystudio query|connection:|param:|description:)", s, re.I):
                head.append(line)
            elif s:
                break
        new = "\n".join(head) + "\n\n" + sql + "\n"
        c = self.editor.textCursor()
        c.beginEditBlock()
        c.select(QTextCursor.Document)
        c.insertText(new)
        c.endEditBlock()

    # ------------------------------------------------------------ running
    def execute(self):
        if self._running:
            return
        cfg = self.cfg()
        if cfg is None:
            QMessageBox.information(self, "Execute", "Choose a connection first (or add one in Server Explorer).")
            return
        cur = self.editor.textCursor()
        text = cur.selectedText().replace(" ", "\n") if cur.hasSelection() else self.editor.toPlainText()
        q = dbcore.parse_query_file(text)
        statements = dbcore.split_statements(q["sql"])
        if not statements:
            return
        params = {}
        for s in statements:
            for n in dbcore.find_params(s):
                params.setdefault(n, q["params"].get(n))
        if params:
            dlg = ParamsDialog(params, self.last_params, self)
            if dlg.exec() != QDialog.Accepted:
                return
            self.last_params.update(dlg.values())
            params = {n: dbcore.coerce_param(v) for n, v in dlg.values().items()}
        pw = self.password()
        if pw is None:
            return
        self._running = True
        self.b_run.setEnabled(False)
        self.lbl.setText("Executing...")
        self._last_run = (cfg, statements, params)
        self.service.run_script(cfg, pw, statements, params, self.sp_limit.value() or None,
                                self._done, self._failed)

    def _clear_results(self):
        while self.results.count() > 1:
            w = self.results.widget(0)
            self.results.removeTab(0)
            w.deleteLater()

    def _record(self, ok, rows=None, seconds=0.0, error=""):
        from . import sqlassist
        run = self._last_run
        if not run:
            return
        sqlassist.record(self.cb_conn.currentText(), ";\n".join(s.strip().rstrip(";") for s in run[1]) +
                         (";" if len(run[1]) > 1 else ""), ok, rows, seconds, error,
                         getattr(self.service, "project_key", ""))

    def show_history(self):
        from .sqlassist import HistoryDialog
        HistoryDialog(self).exec()

    def refresh_schema(self):
        from .sqlassist import SchemaCache
        c = SchemaCache.for_doc(self)
        if c is not None:
            c.refresh()
            self.lbl.setText("  Reloading table and column names...")

    def use_history(self, entry, how):
        """From Query History: replace the query, insert at the cursor, or replace and run."""
        sql = entry.get("sql", "")
        conn = entry.get("connection", "")
        if conn and conn != self.cb_conn.currentText() and self.cb_conn.findText(conn) >= 0:
            self.cb_conn.setCurrentText(conn)
        c = self.editor.textCursor()
        c.beginEditBlock()
        if how == "insert":
            c.insertText(sql)
        else:
            text = self.editor.toPlainText()
            body_start = 0                    # keep the header comments (-- connection:, parameters)
            for line in text.splitlines(True):
                if not line.lstrip().startswith("--"):
                    break
                body_start += len(line)
            c.setPosition(body_start)
            c.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
            c.insertText(sql)
        c.endEditBlock()
        self.editor.setTextCursor(c)
        self.editor.setFocus()
        if how == "run":
            self.execute()

    def _done(self, results):
        self._running = False
        self.b_run.setEnabled(True)
        last = results[-1][1] if results else None
        self._record(True, (len(last) if last is not None and last.columns else
                            (last.rowcount if last is not None and last.rowcount >= 0 else None)),
                     sum(t.elapsed for _s, t in results))
        self.assist.warm()
        self._clear_results()
        msgs = []
        total = 0.0
        grids = 0
        for sql, table in results:
            total += table.elapsed
            first = " ".join(sql.split())[:70]
            if table.columns:
                g = ResultGrid(table)
                self.results.insertTab(grids, g, "Results" if grids == 0 else f"Results {grids + 1}")
                grids += 1
                more = f" (showing the first {len(table)} - raise Max rows to see more)" if table.more else ""
                msgs.append(f"{first}\n  {len(table)} row(s){more}  {table.elapsed:.3f} s")
            else:
                n = table.rowcount
                msgs.append(f"{first}\n  {n if n >= 0 else 'OK -'} row(s) affected  {table.elapsed:.3f} s")
        self.messages.setPlainText("\n\n".join(msgs))
        self.results.setCurrentIndex(0)
        self.b_csv.setEnabled(grids > 0)
        self.a_export_all.setVisible(grids > 1)
        last = results[-1][1] if results else None
        if last is not None and last.columns:
            self.lbl.setText(f"  {len(last)}{'+' if last.more else ''} rows   {total:.3f} s   "
                             f"{self.cb_conn.currentText()}")
        else:
            self.lbl.setText(f"  Done   {total:.3f} s")

    def _failed(self, msg):
        self._running = False
        self.b_run.setEnabled(True)
        self._record(False, error=msg)
        self._clear_results()
        self.messages.setPlainText(msg)
        self.results.setCurrentWidget(self.messages)
        self.lbl.setText("  <span style='color:#f48771'>Error</span>")
        self.b_csv.setEnabled(False)

    def current_table(self):
        w = self.results.currentWidget()
        if isinstance(w, ResultGrid):
            return w.table
        w = self.results.widget(0)
        return w.table if isinstance(w, ResultGrid) else None

    def result_tables(self):
        return [self.results.widget(i).table for i in range(self.results.count())
                if isinstance(self.results.widget(i), ResultGrid)]

    def _ask_path(self, title, ext, filt):
        base = os.path.splitext(self.display_name())[0] + ext
        start = os.path.join(os.path.dirname(self.path) if self.path else os.path.expanduser("~"), base)
        path, _ = QFileDialog.getSaveFileName(self, title, start, filt)
        if path and not path.lower().endswith(ext):
            path += ext
        return path

    def _with_all_rows(self, tables, then):
        """Only part of the rows are shown (Max rows): offer to run the query again for all of them."""
        if not any(t.more for t in tables) or self._last_run is None:
            return then(tables)
        n = sum(len(t) for t in tables)
        r = QMessageBox.question(self, "Export", f"Only the first {n} row(s) are shown (Max rows). Run the query "
                                 "again and export all rows?", QMessageBox.Yes | QMessageBox.No |
                                 QMessageBox.Cancel, QMessageBox.Yes)
        if r == QMessageBox.Cancel:
            return None
        if r == QMessageBox.No:
            return then(tables)
        cfg, statements, params = self._last_run
        pw = self.password()
        if pw is None:
            return None
        self.lbl.setText("  Getting all rows for the export...")
        self.b_csv.setEnabled(False)
        wanted = [i for i, t in enumerate(self.result_tables()) if t in tables]

        def done(results):
            self.b_csv.setEnabled(True)
            full = [t for _sql, t in results if t.columns]
            then([full[i] for i in wanted if i < len(full)] or full)

        def failed(msg):
            self.b_csv.setEnabled(True)
            QMessageBox.warning(self, "Export", "Couldn't run the query again:\n" + msg)
        self.service.run_script(cfg, pw, statements, params, None, done, failed)
        return None

    def export_csv(self):
        table = self.current_table()
        if table is None:
            return
        path = self._ask_path("Export to CSV", ".csv", "CSV files (*.csv)")
        if not path:
            return

        def write(tables):
            write_csv(path, tables[0])
            self.lbl.setText(f"  Exported {len(tables[0])} rows to {os.path.basename(path)}")
        self._with_all_rows([table], write)

    def export_excel(self, all_results=False):
        tables = self.result_tables() if all_results else [self.current_table()]
        tables = [t for t in tables if t is not None]
        if not tables:
            return
        path = self._ask_path("Export to Excel", ".xlsx", "Excel workbooks (*.xlsx)")
        if not path:
            return

        def write(tables):
            write_xlsx(path, tables, os.path.splitext(os.path.basename(path))[0])
            n = sum(len(t) for t in tables)
            self.lbl.setText(f"  Exported {n} rows to {os.path.basename(path)}" +
                             (f" ({len(tables)} sheets)" if len(tables) > 1 else ""))
        self._with_all_rows(tables, write)


def write_xlsx(path, tables, name="Results"):
    from . import xlsxwriter_min
    sheets = [(name if len(tables) == 1 else f"Results {i + 1}", t.columns, list(t)) for i, t in enumerate(tables)]
    xlsxwriter_min.write(path, sheets)


def write_csv(path, table):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(table.columns)
        for r in table:
            w.writerow(["" if v is None else dbcore.to_text(v) for v in r])
