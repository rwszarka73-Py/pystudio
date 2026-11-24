# ------------------------------------------------------------------ data binding and editing
# Appended to pystudio_runtime.py (not imported by the IDE). VB equivalents: BindingSource,
# BindingNavigator, DataBindings, master/detail relations and TableAdapter.Update.
import datetime as _dtm

from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDoubleSpinBox, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QProgressBar,
                               QSlider, QSpinBox, QStyle, QTableWidget, QTableWidgetItem, QTextEdit, QToolButton,
                               QTreeWidget)

_NEW = -1            # data index stored in the grid's "new row" line (the * row)
_UPD_CACHE = {}      # (connection, sql) -> save info or the reason a query can't be saved


def _floppy_icon():
    """A save (floppy disk) icon drawn here - the desktop's icon theme may draw "save" as something else."""
    from PySide6.QtCore import QRectF, Qt as _Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
    pm = QPixmap(32, 32)
    pm.fill(_Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    body = QPainterPath()
    for i, (x, y) in enumerate(((4, 4), (23, 4), (28, 9), (28, 28), (4, 28))):
        body.moveTo(x, y) if i == 0 else body.lineTo(x, y)
    body.closeSubpath()
    p.setPen(QColor("#1f5f9e"))
    p.setBrush(QColor("#3c8fd8"))
    p.drawPath(body)
    p.setPen(_Qt.NoPen)
    p.setBrush(QColor("#e8eef5"))
    p.drawRect(QRectF(8, 16, 16, 10))
    p.setBrush(QColor("#d0d7e0"))
    p.drawRect(QRectF(10, 5.5, 11, 6.5))
    p.end()
    return QIcon(pm)


def _same(a, b):
    return a == b or to_text(a) == to_text(b)


class _Edit:
    """Working copy of a filled control's rows: edits, new rows and deletions until save()."""

    def __init__(self, widget, table, source, connection, params):
        self.w = widget
        self.table = table
        self.source = source              # query name or SQL; None when filled from a DataTable
        self.connection = connection
        self.params = dict(params)
        self.rows = [list(r) for r in table]
        self.orig = [tuple(r) for r in table]
        self.deleted = set()

    def changed(self):
        for i, row in enumerate(self.rows):
            o = self.orig[i]
            if i in self.deleted:
                if o is not None:
                    return True
            elif o is None:
                if any(v not in (None, "") for v in row):
                    return True
            elif any(not _same(a, b) for a, b in zip(row, o)):
                return True
        return False

    def typed(self, i, value):
        """Convert an edited value to the column's type (taken from the loaded rows)."""
        sample = next((r[i] for r in self.orig if r is not None and r[i] is not None), None)
        if isinstance(value, bool):
            return int(value) if isinstance(sample, int) and not isinstance(sample, bool) else value
        if isinstance(value, _dtm.date) and isinstance(sample, str):
            return value.isoformat()
        if not isinstance(value, str):
            return value
        if value == "":
            return None
        try:
            if isinstance(sample, bool):
                return value.strip().lower() in ("1", "true", "yes", "y")
            if isinstance(sample, int):
                return int(value)
            if isinstance(sample, float):
                return float(value)
            if isinstance(sample, _dtm.datetime):
                return _dtm.datetime.fromisoformat(value)
            if isinstance(sample, _dtm.date):
                return _dtm.date.fromisoformat(value[:10])
        except ValueError:
            pass
        return value

    def row(self, idx):
        if idx is None or not 0 <= idx < len(self.rows) or idx in self.deleted:
            return None
        r = Row(self.rows[idx], self.table._index)
        r._names = self.table.columns
        return r

    def updatable(self):
        """Save info, or the reason this control's rows can't be saved."""
        if self.source is None:
            return "it was filled from a DataTable, not from a query"
        try:
            sql, connection, _p = _resolve(self.source, self.connection, self.params)
        except Exception as e:
            return str(e)
        key = (connection, sql)
        if key not in _UPD_CACHE:
            try:
                _UPD_CACHE[key] = _save_info(sql, connection)
            except Exception as e:
                _UPD_CACHE[key] = f"couldn't read the table's columns ({e})"
        info = _UPD_CACHE[key]
        if isinstance(info, dict):
            missing = [k for k in info["pk"] if k.lower() not in self.table._index]
            if missing:
                return "the primary key (" + ", ".join(missing) + ") must be in the query's SELECT list"
        return info


def _save_info(sql, connection):
    flat = _re.sub(r"'(?:[^']|'')*'", "''", _strip_comments(sql))
    if not _re.match(r"\s*select\b", flat, _re.I):
        return "its query isn't a SELECT"
    if _re.search(r"\b(join|group\s+by|union|distinct|having|intersect|except)\b", flat, _re.I):
        return "its query joins or groups tables - only single-table queries can be saved"
    m = _re.search(r'\bfrom\s+((?:"[^"]+"|[\w$#@]+)(?:\s*\.\s*(?:"[^"]+"|[\w$#@]+))?)\s*(,)?', flat, _re.I)
    if not m or m.group(2) or _re.search(r"\bfrom\b", flat[m.end():], _re.I):
        return "its query must read from exactly one table"
    parts = [p.strip().strip('"') for p in _re.split(r"\s*\.\s*", m.group(1))]
    schema, table = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
    conns = _connections()
    name = connection or next(iter(conns), None)
    cfg = conns.get(name) or {}
    conn = db(name)
    provider = cfg.get("provider", "SQLite")
    if provider == "DB2" and not schema:
        schema = str(scalar("SELECT CURRENT SCHEMA FROM SYSIBM.SYSDUMMY1", name) or "").strip()
    if provider == "Snowflake" and not schema:
        schema = str(scalar("SELECT CURRENT_SCHEMA()", name) or "").strip()
    cols = schema_columns(conn, cfg, schema, table)
    if not cols and provider != "SQLite":
        cols = schema_columns(conn, cfg, schema.upper(), table.upper())
        if cols:
            schema, table = schema.upper(), table.upper()
    if not cols:
        return f"couldn't find the table {table}"
    pk = [c[0] for c in cols if c[2]]
    if not pk:
        return f"the table {table} has no primary key"
    return {"base": qualified(schema, table, provider), "pk": pk, "cols": {c[0].lower(): c[0] for c in cols},
            "conn": name, "provider": provider}


def _edit_of(widget):
    e = getattr(widget, "_ps_edit", None)
    if e is None:
        raise DataError(f"{widget.objectName() or 'This control'} hasn't been filled with data yet")
    return e


def _current_index(w):
    v = None
    if isinstance(w, QTableWidget):
        r = w.currentRow()
        it = w.item(r, 0) if r >= 0 else None
        v = it.data(Qt.UserRole) if it else None
    elif isinstance(w, QTreeWidget):
        it = w.currentItem()
        v = it.data(0, Qt.UserRole) if it else None
    elif isinstance(w, QComboBox):
        v = w.currentData()
    elif isinstance(w, QListWidget):
        it = w.currentItem()
        v = it.data(Qt.UserRole) if it else None
    return v if isinstance(v, int) and v >= 0 else None


def current_row(widget):
    """The data row behind the selected grid row / list item / combo item (or None), including
    edits that haven't been saved yet."""
    e = getattr(widget, "_ps_edit", None)
    return e.row(_current_index(widget)) if e is not None else None


def has_changes(widget):
    """True when the control has added, changed or deleted rows that haven't been saved."""
    e = getattr(widget, "_ps_edit", None)
    return bool(e and e.changed())


def can_save(widget):
    """True when save() can write this control's rows back (one table with a primary key)."""
    e = getattr(widget, "_ps_edit", None)
    return bool(e) and isinstance(e.updatable(), dict)


# ---------------------------------------------------------------- grid editing
def _grid_editable(w):
    return w.editTriggers() != QAbstractItemView.NoEditTriggers


def _is_new_line(w, r):
    it = w.item(r, 0)
    return it is not None and it.data(Qt.UserRole) == _NEW


def _new_line(w):
    for r in range(w.rowCount() - 1, -1, -1):
        if _is_new_line(w, r):
            return r
    return None


def _ensure_new_line(w):
    """WinForms' AllowUserToAddRows: an empty * line at the bottom; typing in it adds a row."""
    if (_new_line(w) is not None or not _grid_editable(w) or w.property("allowUserToAddRows") is False
            or not can_save(w)):
        return
    w._ps_busy = True
    try:
        r = w.rowCount()
        w.insertRow(r)
        it = QTableWidgetItem("")
        it.setData(Qt.UserRole, _NEW)
        w.setItem(r, 0, it)
        w.setVerticalHeaderItem(r, QTableWidgetItem("*"))
    finally:
        w._ps_busy = False


def _materialize(w, r):
    """Turn the * line at grid row r into a real new row; returns its data index."""
    e = w._ps_edit
    idx = len(e.rows)
    e.rows.append([None] * len(e.table.columns))
    e.orig.append(None)
    w._ps_busy = True
    try:
        w.item(r, 0).setData(Qt.UserRole, idx)
        w.setVerticalHeaderItem(r, QTableWidgetItem(str(r + 1)))
    finally:
        w._ps_busy = False
    _ensure_new_line(w)
    return idx


def _grid_item_changed(w, item):
    e = getattr(w, "_ps_edit", None)
    if e is None or getattr(w, "_ps_busy", False):
        return
    r, c = item.row(), item.column()
    first = w.item(r, 0)
    idx = first.data(Qt.UserRole) if first is not None else None
    if idx == _NEW:
        idx = _materialize(w, r)
    cols = getattr(w, "_ps_cols", [])
    if not isinstance(idx, int) or idx < 0 or c >= len(cols):
        return
    i = cols[c]
    e.rows[idx][i] = e.typed(i, item.data(Qt.EditRole))
    if r == w.currentRow():
        _push_bound(w)
    _update_navs(w)


class _GridKeys(QObject):
    """Delete removes fully selected rows (AllowUserToDeleteRows), like a DataGridView."""

    def eventFilter(self, obj, ev):
        if (ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Delete and obj.state() != QAbstractItemView.EditingState
                and obj.property("allowUserToDeleteRows") is not False and _grid_editable(obj)):
            rows = [i.row() for i in obj.selectionModel().selectedRows()]
            if rows:
                delete_row(obj, rows)
                return True
        return False


def add_row(widget):
    """Go to the grid's new-row line and start editing it (BindingNavigator: Add New)."""
    if not isinstance(widget, QTableWidget):
        raise DataError("Rows can only be added to a DataGridView")
    _ensure_new_line(widget)
    r = _new_line(widget)
    if r is None:
        raise DataError(f"Can't add rows to {widget.objectName()}: " + (
            "it's ReadOnly" if not _grid_editable(widget) else "AllowUserToAddRows is off"
            if widget.property("allowUserToAddRows") is False else str(_edit_of(widget).updatable())))
    e = widget._ps_edit
    info = e.updatable()
    cols = getattr(widget, "_ps_cols", [])
    pk = {k.lower() for k in info["pk"]} if isinstance(info, dict) else set()
    c = next((n for n, i in enumerate(cols) if str(e.table.columns[i]).lower() not in pk), 0)
    widget.setFocus()
    widget.setCurrentCell(r, c)
    widget.editItem(widget.item(r, c) or _blank(widget, r, c))


def _blank(w, r, c):
    w._ps_busy = True
    try:
        w.setItem(r, c, QTableWidgetItem(""))
    finally:
        w._ps_busy = False
    return w.item(r, c)


def delete_row(widget, rows=None):
    """Delete the current grid row (or the given row numbers). save() removes them from the database."""
    e = _edit_of(widget)
    if not isinstance(widget, QTableWidget):
        raise DataError("Rows can only be deleted from a DataGridView")
    if rows is None:
        rows = [widget.currentRow()]
    elif isinstance(rows, int):
        rows = [rows]
    widget._ps_busy = True
    try:
        for r in sorted({r for r in rows if 0 <= r < widget.rowCount()}, reverse=True):
            it = widget.item(r, 0)
            idx = it.data(Qt.UserRole) if it else None
            if idx == _NEW:
                continue
            if isinstance(idx, int):
                e.deleted.add(idx)
            widget.removeRow(r)
    finally:
        widget._ps_busy = False
    _current_changed(widget)


# ---------------------------------------------------------------- save / refresh
def _commit_pending():
    """Finish an edit in progress (a grid cell or a bound text box) before saving."""
    fw = QApplication.focusWidget()
    if fw is not None:
        fw.clearFocus()
        QApplication.processEvents()


def save(widget):
    """Write the control's added, changed and deleted rows to the database in one transaction
    (VB: TableAdapter.Update), then reload it. Returns (inserted, updated, deleted).
    Works when its DataSource reads one table that has a primary key."""
    _commit_pending()
    e = _edit_of(widget)
    u = e.updatable()
    if isinstance(u, str):
        raise DataError(f"Can't save {widget.objectName()}: {u}.")
    conn = db(u["conn"])
    cols = e.table.columns

    def q(name):
        return quote_ident(str(name), u["provider"])
    writable = [i for i, c in enumerate(cols) if str(c).lower() in u["cols"]]
    key = [e.table._index[k.lower()] for k in u["pk"]]

    def where(o, p):
        for n, i in enumerate(key):
            p[f"k{n}"] = o[i]
        return " AND ".join(f"{q(cols[i])} = :k{n}" for n, i in enumerate(key))

    work = []
    for i in sorted(e.deleted):
        if e.orig[i] is not None:
            p = {}
            work.append(("d", f"DELETE FROM {u['base']} WHERE {where(e.orig[i], p)}", p))
    for i, row in enumerate(e.rows):
        if i in e.deleted:
            continue
        o = e.orig[i]
        if o is None:
            vals = {u["cols"][str(cols[c]).lower()]: row[c] for c in writable if row[c] not in (None, "")}
            if not vals:
                continue
            have = {k.lower() for k in vals}
            for pname, pv in e.params.items():   # detail rows get the master's key (e.g. CustomerID)
                if pname.lower() in u["cols"] and pname.lower() not in have and pv is not None:
                    vals[u["cols"][pname.lower()]] = pv
            names = list(vals)
            p = {f"v{n}": vals[k] for n, k in enumerate(names)}
            work.append(("i", f"INSERT INTO {u['base']} ({', '.join(q(k) for k in names)}) VALUES "
                              f"({', '.join(':v%d' % n for n in range(len(names)))})", p))
        else:
            diff = [c for c in writable if not _same(row[c], o[c])]
            if diff:
                p = {f"v{n}": row[c] for n, c in enumerate(diff)}
                sets = ", ".join(f"{q(cols[c])} = :v{n}" for n, c in enumerate(diff))
                work.append(("u", f"UPDATE {u['base']} SET {sets} WHERE {where(o, p)}", p))
    counts = {"i": 0, "u": 0, "d": 0}
    cur = conn.cursor()
    try:
        for kind, sql, p in work:
            if param_style(conn) == "named":
                cur.execute(sql, {n: p[n] for n in find_params(sql)})
            else:
                qs, args = to_qmark(sql, p)
                cur.execute(qs, args)
            counts[kind] += 1
        conn.commit()
    except Exception as ex:
        try:
            conn.rollback()
        except Exception:
            pass
        msg = _db2_message(ex) if type(conn).__module__.startswith("ibm_db") else str(ex)
        raise DataError(f"Couldn't save {widget.objectName()} - nothing was saved.\n{msg}") from None
    finally:
        try:
            cur.close()
        except Exception:
            pass
    refresh(widget)
    return counts["i"], counts["u"], counts["d"]


def refresh(widget):
    """Re-run the control's query (VB: TableAdapter.Fill again). Unsaved changes are dropped."""
    e = _edit_of(widget)
    if e.source is None:
        raise DataError(f"{widget.objectName()} was filled from a DataTable - fill it again instead")
    pos = widget.currentRow() if isinstance(widget, QTableWidget) else None
    fill(widget, e.source, e.connection, **e.params)
    if pos is not None and widget.rowCount():
        widget.setCurrentCell(min(pos, widget.rowCount() - 1), max(widget.currentColumn(), 0))


# ---------------------------------------------------------------- after fill / current row
def _after_fill(widget, table, source, connection, params):
    widget._ps_edit = _Edit(widget, table, None if isinstance(source, DataTable) else source,
                            connection, params)
    if not getattr(widget, "_ps_wired", False):
        widget._ps_wired = True
        if isinstance(widget, QTableWidget):
            widget.currentCellChanged.connect(lambda r, _c, pr, _pc: r != pr and _current_changed(widget))
            widget.itemChanged.connect(lambda it: _grid_item_changed(widget, it))
            widget._ps_keys = _GridKeys(widget)
            widget.installEventFilter(widget._ps_keys)
        elif isinstance(widget, QTreeWidget):
            widget.currentItemChanged.connect(lambda *_a: _current_changed(widget))
        elif isinstance(widget, QComboBox):
            widget.currentIndexChanged.connect(lambda _i: _current_changed(widget))
        elif isinstance(widget, QListWidget):
            widget.currentRowChanged.connect(lambda _r: _current_changed(widget))
    if isinstance(widget, QTableWidget):
        _ensure_new_line(widget)
        if widget.rowCount() and widget.currentRow() < 0 and (len(table) or not _grid_editable(widget)):
            widget._ps_busy = True
            try:
                widget.setCurrentCell(0, 0)
            finally:
                widget._ps_busy = False
    elif isinstance(widget, QTreeWidget) and widget.topLevelItemCount() and widget.currentItem() is None:
        widget._ps_busy = True
        try:
            widget.setCurrentItem(widget.topLevelItem(0))
        finally:
            widget._ps_busy = False
    _current_changed(widget)
    if hasattr(widget, "_ps_bind"):       # a lookup ComboBox that is also bound to a column
        _push_bound(widget._ps_bind[0])


def _current_changed(w):
    if getattr(w, "_ps_busy", False):
        return
    _push_bound(w)
    for d in getattr(w, "_ps_details", []):
        _refill_detail(w, d)
    _update_navs(w)


def _refill_detail(master, d):
    e = getattr(master, "_ps_edit", None)
    row = e.row(_current_index(master)) if e is not None else None
    if has_changes(d) and can_save(d):
        if msgbox(f"Save the changes in {d.objectName()} first?", "", "YesNo", "Question") == "Yes":
            try:
                save(d)
            except DataError as ex:
                msgbox(str(ex), icon="Warning")
    src = d.property("dataSource")
    if row is None:
        _clear(d)
        return
    try:
        sql = _resolve(src, None, {})[0]
        names = find_params(sql)
        params = {}
        vi = getattr(master, "_ps_value_index", None)
        for n in names:
            if n.lower() in e.table._index:
                params[n] = row[n]
            elif vi is not None and len(names) == 1:
                params[n] = tuple.__getitem__(row, vi)
        fill(d, src, **params)
    except Exception as ex:
        _clear(d)
        msgbox(f"Couldn't load {d.objectName()} ({src}):\n{ex}", icon="Warning")


def _clear(w):
    old = getattr(w, "_ps_edit", None)
    cols = old.table.columns if old else []
    w._ps_busy = True
    try:
        if isinstance(w, QTableWidget):
            w.setRowCount(0)
        else:
            w.clear()
    finally:
        w._ps_busy = False
    w._ps_table = DataTable(cols, [])
    w._ps_edit = _Edit(w, w._ps_table, old.source if old else None, old.connection if old else None, {})
    _current_changed(w)


# ---------------------------------------------------------------- simple controls (DataBindings)
def _set_value(ctrl, v):
    if isinstance(ctrl, QLineEdit):
        ctrl.setText(to_text(v))
    elif isinstance(ctrl, (QPlainTextEdit, QTextEdit)):
        ctrl.setPlainText(to_text(v))
    elif isinstance(ctrl, QLabel):
        ctrl.setText(to_text(v))
    elif isinstance(ctrl, QCheckBox):
        ctrl.setChecked(bool(v) and str(v).strip().lower() not in ("0", "false", "n", "no"))
    elif isinstance(ctrl, QDoubleSpinBox):
        ctrl.setValue(float(v or 0))
    elif isinstance(ctrl, (QSpinBox, QSlider, QProgressBar)):
        try:
            ctrl.setValue(int(float(v or 0)))
        except (TypeError, ValueError):
            ctrl.setValue(0)
    elif isinstance(ctrl, QDateTimeEdit):
        if isinstance(v, (_dtm.date, _dtm.datetime)):
            ctrl.setDate(QDate(v.year, v.month, v.day))
        elif v:
            d = QDate.fromString(str(v)[:10], "yyyy-MM-dd")
            if d.isValid():
                ctrl.setDate(d)
    elif isinstance(ctrl, QComboBox):
        vi = getattr(ctrl, "_ps_value_index", None)
        t = getattr(ctrl, "_ps_table", None)
        if vi is not None and t is not None:
            j = next((n for n, r in enumerate(t) if _same(tuple.__getitem__(r, vi), v)), None)
            ctrl.setCurrentIndex(ctrl.findData(j) if j is not None else -1)
        else:
            ctrl.setCurrentText(to_text(v))


def _push_bound(src):
    e = getattr(src, "_ps_edit", None)
    row = e.row(_current_index(src)) if e is not None else None
    for ctrl, col in getattr(src, "_ps_bindings", []):
        ci = e.table._index.get(col.lower()) if e is not None else None
        ctrl._ps_pushing = True
        try:
            _set_value(ctrl, tuple.__getitem__(row, ci) if row is not None and ci is not None else None)
        finally:
            ctrl._ps_pushing = False


def _pull(ctrl, value):
    """A bound control was edited: store the value in the source's current row (and its grid cell)."""
    if getattr(ctrl, "_ps_pushing", False):
        return
    src, col = ctrl._ps_bind
    e = getattr(src, "_ps_edit", None)
    if e is None:
        return
    ci = e.table._index.get(col.lower())
    if ci is None:
        return
    idx = _current_index(src)
    if idx is None and isinstance(src, QTableWidget):
        r = src.currentRow()
        if r >= 0 and _is_new_line(src, r):
            idx = _materialize(src, r)
    if idx is None or idx in e.deleted:
        return
    e.rows[idx][ci] = e.typed(ci, value)
    if isinstance(src, QTableWidget) and ci in getattr(src, "_ps_cols", []):
        r, c = src.currentRow(), src._ps_cols.index(ci)
        src._ps_busy = True
        try:
            item = src.item(r, c) or QTableWidgetItem()
            v = e.rows[idx][ci]
            if is_number(v):
                item.setData(Qt.EditRole, v)
            else:
                item.setText(to_text(v))
            if src.item(r, c) is None:
                src.setItem(r, c, item)
        finally:
            src._ps_busy = False
    _update_navs(src)


def _wire_bound(ctrl):
    if isinstance(ctrl, QLineEdit):
        ctrl.textEdited.connect(lambda t: _pull(ctrl, t))
    elif isinstance(ctrl, (QPlainTextEdit, QTextEdit)):
        ctrl.textChanged.connect(lambda: _pull(ctrl, ctrl.toPlainText()))
    elif isinstance(ctrl, QCheckBox):
        ctrl.toggled.connect(lambda b: _pull(ctrl, b))
    elif isinstance(ctrl, (QSpinBox, QDoubleSpinBox, QSlider)):
        ctrl.valueChanged.connect(lambda v: _pull(ctrl, v))
    elif isinstance(ctrl, QDateTimeEdit):
        ctrl.dateChanged.connect(lambda d: _pull(ctrl, _dtm.date(d.year(), d.month(), d.day())))
    elif isinstance(ctrl, QComboBox):
        ctrl.currentIndexChanged.connect(lambda _i: _pull(ctrl, selected_value(ctrl)
                                                          if getattr(ctrl, "_ps_value_index", None) is not None
                                                          else ctrl.currentText()))


# ---------------------------------------------------------------- BindingNavigator
class _Navigator(QObject):
    """|< < [3] of 10 > >|  + x  Save - moves through and edits its BindingSource control."""

    def __init__(self, frame, src):
        super().__init__(frame)
        self.frame, self.src = frame, src
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(1)
        st = frame.style()

        def btn(icon, tip, slot, text=""):
            b = QToolButton(frame)
            b.setAutoRaise(True)
            if icon == "save":
                b.setIcon(_floppy_icon())
            elif icon is not None:
                b.setIcon(st.standardIcon(icon))
            else:
                b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            lay.addWidget(b)
            return b

        def sep():
            line = QFrame(frame)
            line.setFrameShape(QFrame.VLine)
            line.setFrameShadow(QFrame.Sunken)
            lay.addWidget(line)
        self.b_first = btn(QStyle.SP_MediaSkipBackward, "Move first", lambda: self.go(0))
        self.b_prev = btn(QStyle.SP_ArrowLeft, "Move previous", lambda: self.go(self.pos() - 1))
        sep()
        self.e_pos = QLineEdit(frame)
        self.e_pos.setFixedWidth(42)
        self.e_pos.setAlignment(Qt.AlignCenter)
        self.e_pos.setToolTip("Current position")
        self.e_pos.returnPressed.connect(self._typed)
        lay.addWidget(self.e_pos)
        self.l_count = QLabel("of 0", frame)
        lay.addWidget(self.l_count)
        sep()
        self.b_next = btn(QStyle.SP_ArrowRight, "Move next", lambda: self.go(self.pos() + 1))
        self.b_last = btn(QStyle.SP_MediaSkipForward, "Move last", lambda: self.go(self.count() - 1))
        sep()
        self.b_add = btn(None, "Add new", self._add, "+")
        self.b_del = btn(QStyle.SP_DialogDiscardButton, "Delete", self._delete)
        self.b_save = btn("save", "Save Data", self._save)
        lay.addStretch()
        if not hasattr(src, "_ps_navs"):
            src._ps_navs = []
        src._ps_navs.append(self)
        frame._ps_navigator = self
        self.update()

    def rows(self):
        s = self.src
        if isinstance(s, QTableWidget):
            return [r for r in range(s.rowCount()) if not _is_new_line(s, r)]
        if isinstance(s, QTreeWidget):
            return list(range(s.topLevelItemCount()))
        return list(range(s.count()))

    def count(self):
        return len(self.rows())

    def pos(self):
        s = self.src
        if isinstance(s, QTableWidget):
            r = s.currentRow()
            return self.rows().index(r) if r in self.rows() else (self.count() if r >= 0 else -1)
        if isinstance(s, QTreeWidget):
            it = s.currentItem()
            return s.indexOfTopLevelItem(it) if it else -1
        if isinstance(s, QComboBox):
            return s.currentIndex()
        return s.currentRow()

    def go(self, n):
        rows = self.rows()
        if not rows:
            return
        n = max(0, min(n, len(rows) - 1))
        s = self.src
        if isinstance(s, QTableWidget):
            s.setCurrentCell(rows[n], max(s.currentColumn(), 0))
        elif isinstance(s, QTreeWidget):
            s.setCurrentItem(s.topLevelItem(n))
        elif isinstance(s, QComboBox):
            s.setCurrentIndex(n)
        else:
            s.setCurrentRow(n)

    def _typed(self):
        try:
            self.go(int(self.e_pos.text()) - 1)
        except ValueError:
            self.update()

    def _add(self):
        try:
            add_row(self.src)
        except DataError as ex:
            msgbox(str(ex), icon="Warning")

    def _delete(self):
        try:
            delete_row(self.src)
        except DataError as ex:
            msgbox(str(ex), icon="Warning")

    def _save(self):
        try:
            save(self.src)
        except DataError as ex:
            msgbox(str(ex), icon="Warning")

    def update(self):
        n, p = self.count(), self.pos()
        grid = isinstance(self.src, QTableWidget)
        self.e_pos.setText("" if p < 0 else str(p + 1))
        self.l_count.setText(f"of {n}" + (" (new)" if grid and p == n and p >= 0 else ""))
        self.b_first.setEnabled(p > 0)
        self.b_prev.setEnabled(p > 0)
        self.b_next.setEnabled(0 <= p < n - 1)
        self.b_last.setEnabled(0 <= p < n - 1)
        editable = grid and _grid_editable(self.src) and can_save(self.src)
        self.b_add.setEnabled(editable and self.src.property("allowUserToAddRows") is not False)
        self.b_del.setEnabled(editable and 0 <= p < n and self.src.property("allowUserToDeleteRows") is not False)
        self.b_save.setEnabled(can_save(self.src) and has_changes(self.src))


def _update_navs(src):
    for nav in getattr(src, "_ps_navs", []):
        nav.update()


# ---------------------------------------------------------------- form setup
def _bind_data(window):
    """Wire master/detail, DataBindings and BindingNavigators, then fill controls whose
    DataSource is set (and AutoLoad isn't off) - like a WinForms Form_Load with TableAdapters."""
    errors = []
    widgets = _own_widgets(window)
    by_name = {w.objectName(): w for w in widgets if w.objectName()}
    details = set()
    for w in widgets:
        m = w.property("masterControl")
        if m and w.property("dataSource"):
            master = by_name.get(m)
            if master is None:
                errors.append(f"{w.objectName()}: its Master control {m} isn't on the form")
                continue
            if not hasattr(master, "_ps_details"):
                master._ps_details = []
            master._ps_details.append(w)
            details.add(w)
    for w in widgets:
        b = w.property("dataBinding")
        if not b:
            continue
        src_name, _dot, col = str(b).partition(".")
        src = by_name.get(src_name)
        if src is None or not col:
            errors.append(f"{w.objectName()}: DataBinding {b!r} should look like dataGridView1.Name")
            continue
        if not hasattr(src, "_ps_bindings"):
            src._ps_bindings = []
        src._ps_bindings.append((w, col))
        w._ps_bind = (src, col)
        _wire_bound(w)
    for w in widgets:
        if w.property("psType") == "BindingNavigator":
            src = by_name.get(w.property("bindingSource") or "")
            if src is not None:
                _Navigator(w, src)
            elif w.property("bindingSource"):
                errors.append(f"{w.objectName()}: BindingSource {w.property('bindingSource')} isn't on the form")
    for w in widgets:
        src = w.property("dataSource")
        if not src or w in details or w.property("autoLoad") is False:
            continue
        try:
            fill(w, src)
        except Exception as e:
            errors.append(f"{w.objectName()} ({src}): {e}")
    for w, src in [(w, w._ps_bind[0]) for w in widgets if hasattr(w, "_ps_bind")]:
        e = getattr(src, "_ps_edit", None)
        if e is not None and e.table.columns and w._ps_bind[1].lower() not in e.table._index:
            errors.append(f"{w.objectName()}: {src.objectName()} has no column {w._ps_bind[1]} "
                          f"(columns: {', '.join(map(str, e.table.columns))})")
    for src in {w._ps_bind[0] for w in widgets if hasattr(w, "_ps_bind")}:
        _push_bound(src)                  # lookup lists are filled now too
    if errors:
        msgbox("Couldn't load data:\n\n" + "\n".join(errors), icon="Warning")
