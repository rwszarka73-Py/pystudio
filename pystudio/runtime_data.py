"""Data, settings and resources part of pystudio_runtime.py (appended to RUNTIME_SOURCE)."""
import os

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dbcore.py"), encoding="utf-8") as _f:
    _CORE = _f.read()

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rt_binding.py"), encoding="utf-8") as _f:
    _BINDING = _f.read()
_EXTRA = ""
for _name in ("rt_forms.py", "pschart.py", "psweb.py", "rt_hotreload.py", "rt_components.py",
              "rt_report.py"):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), _name), encoding="utf-8") as _f:
        _EXTRA += "\n\n" + _f.read()

DATA_RUNTIME = "\n\n" + _CORE + '''

# ------------------------------------------------------------------ data access
_PROJECT_CACHE = None
_OPEN = {}
_PASSWORDS = {}
_QUERY_CACHE = {}


def _proj():
    global _PROJECT_CACHE
    if _PROJECT_CACHE is None:
        _PROJECT_CACHE = _project()
    return _PROJECT_CACHE


def _connections():
    return {c.get("name"): c for c in _proj().get("connections", []) if c.get("name")}


def _prompt_password(cfg):
    from PySide6.QtWidgets import QInputDialog, QLineEdit
    who = cfg.get("user") or ""
    pw, ok = QInputDialog.getText(QApplication.activeWindow(), "Sign in",
                                  f"{secret_name(cfg)}{(' for ' + who) if who else ''}\\n{connection_summary(cfg)}",
                                  QLineEdit.Password)
    return pw if ok else None


def db(name=None):
    """The open DB-API connection for one of the project's data connections (opened once,
    then reused). name=None uses the first connection."""
    conns = _connections()
    if not conns:
        raise DataError("This project has no data connections (View > Server Explorer in PyStudio)")
    name = name or next(iter(conns))
    if name in _OPEN:
        return _OPEN[name]
    cfg = conns.get(name)
    if cfg is None:
        raise DataError(f"No data connection named {name!r}. Connections: {', '.join(conns)}")
    if name not in _PASSWORDS:
        _PASSWORDS[name] = resolve_password(cfg, _prompt_password)
    if cfg.get("provider") == "SQLite" and getattr(sys, "frozen", False):
        cfg = dict(cfg, file=_user_copy(cfg.get("file", "")))
    conn = connect(cfg, _PASSWORDS[name], _HERE)
    _OPEN[name] = conn
    return conn


def data_dir():
    """Per-user folder for the app's data (~/.local/share/<app>)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, _package_name(_proj().get("name") or "app"))


def _user_copy(path):
    """A published app's bundled SQLite file is read-only (or temporary), so the app works on a
    per-user copy in data_dir() - made from the bundled file the first time (like VS's
    'Copy to Output Directory')."""
    path = os.path.expanduser(path)
    if not path or path == ":memory:" or os.path.isabs(path):
        return path
    dest = os.path.join(data_dir(), path)
    if not os.path.exists(dest):
        src = os.path.join(_HERE, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(src):
            import shutil
            shutil.copy2(src, dest)
    return dest


def close_connections():
    for c in _OPEN.values():
        try:
            c.close()
        except Exception:
            pass
    _OPEN.clear()


def _query_def(name):
    if not isinstance(name, str) or not _re.fullmatch(r"[A-Za-z_]\\w*", name):
        return None
    if name in _QUERY_CACHE:
        return _QUERY_CACHE[name]
    for folder in (os.path.join(_HERE, "queries"), _HERE):
        path = os.path.join(folder, name + ".sql")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                q = parse_query_file(f.read())
            _QUERY_CACHE[name] = q
            return q
    return None


def _resolve(name_or_sql, connection, params):
    q = _query_def(name_or_sql)
    if q is None:
        if isinstance(name_or_sql, str) and _re.fullmatch(r"[A-Za-z_]\\w*", name_or_sql):
            raise DataError(f"No query named {name_or_sql!r} (queries/{name_or_sql}.sql)")
        return name_or_sql, connection, params
    merged = {k: coerce_param(v) for k, v in q["params"].items() if v is not None}
    merged.update(params)
    return q["sql"], connection or q["connection"] or None, merged


def query(name_or_sql, /, connection=None, **params):
    """Run a saved query (its name, e.g. "GetCustomers") or a SELECT statement and return a
    DataTable:  for row in query("GetCustomers", state="NE"): print(row.Name)"""
    sql, connection, params = _resolve(name_or_sql, connection, params)
    return run(db(connection), sql, params)


def execute(sql, /, connection=None, **params):
    """Run INSERT/UPDATE/DELETE (or a saved query) and return the number of rows changed:
    execute("UPDATE Customers SET Phone = :phone WHERE CustomerID = :id", phone=p, id=5)"""
    sql, connection, params = _resolve(sql, connection, params)
    return run(db(connection), sql, params).rowcount


def scalar(sql, /, connection=None, **params):
    """The first value of the first row (or None):  n = scalar("SELECT COUNT(*) FROM Orders")"""
    t = query(sql, connection, **params)
    return t[0][0] if t and t.columns else None


def _norm(s):
    return _re.sub(r"[\\s_]", "", str(s)).lower()


def _designed_columns(w, current):
    key = "_psDesignedColumns"
    v = w.property(key)
    if v is None:
        v = json.dumps(current)
        w.setProperty(key, v)
    return json.loads(v)


def _pick_columns(table, designed):
    """Designed columns that match result columns (by name, ignoring case/spaces/_) pick and
    order the columns shown; otherwise all result columns are shown."""
    lookup = {_norm(c): i for i, c in enumerate(table.columns)}
    picked = [(text, lookup[_norm(text)]) for text in designed if _norm(text) in lookup]
    return picked or [(c, i) for i, c in enumerate(table.columns)]


def fill(widget, source, /, connection=None, **params):
    """Fill a DataGridView (QTableWidget), ListView/TreeView (QTreeWidget), ComboBox or ListBox
    from a saved query name, a SELECT statement or a DataTable. Returns the DataTable."""
    table = source if isinstance(source, DataTable) else query(source, connection, **params)
    widget._ps_table = table
    widget._ps_busy = widget._ps_pushing = True    # filling isn't editing
    try:
        _fill_widget(widget, table)
    finally:
        widget._ps_busy = widget._ps_pushing = False
    _after_fill(widget, table, source, connection, params)
    return table


def _fill_widget(widget, table):
    if getattr(widget, "_ps_is_chart", False):
        widget.bind(table)
        return
    from PySide6.QtWidgets import QComboBox, QListWidget, QTableWidget, QTableWidgetItem, QTreeWidget, \\
        QTreeWidgetItem
    if isinstance(widget, QTableWidget):
        designed = _designed_columns(widget, [widget.horizontalHeaderItem(i).text() if
                                              widget.horizontalHeaderItem(i) else ""
                                              for i in range(widget.columnCount())])
        cols = _pick_columns(table, designed)
        widget._ps_cols = [i for _t, i in cols]
        sorting = widget.isSortingEnabled()
        widget.setSortingEnabled(False)
        widget.clear()
        widget.setColumnCount(len(cols))
        widget.setHorizontalHeaderLabels([c[0] for c in cols])
        widget.setRowCount(len(table))
        right = Qt.AlignRight | Qt.AlignVCenter
        for r, row in enumerate(table):
            for c, (_text, i) in enumerate(cols):
                v = tuple.__getitem__(row, i)
                item = QTableWidgetItem(to_text(v))
                if is_number(v):
                    item.setData(Qt.EditRole, v)
                    item.setTextAlignment(right)
                if c == 0:
                    item.setData(Qt.UserRole, r)
                widget.setItem(r, c, item)
        widget.setSortingEnabled(sorting)
        widget.resizeColumnsToContents()
    elif isinstance(widget, QTreeWidget):
        designed = _designed_columns(widget, [widget.headerItem().text(i)
                                              for i in range(widget.columnCount())]
                                     if not widget.isHeaderHidden() else [])
        cols = _pick_columns(table, designed)
        widget.clear()
        widget.setColumnCount(len(cols))
        widget.setHeaderLabels([c[0] for c in cols])
        widget.setRootIsDecorated(False)
        for r, row in enumerate(table):
            item = QTreeWidgetItem([to_text(tuple.__getitem__(row, i)) for _t, i in cols])
            item.setData(0, Qt.UserRole, r)
            widget.addTopLevelItem(item)
        for i in range(len(cols)):
            widget.resizeColumnToContents(i)
    elif isinstance(widget, (QComboBox, QListWidget)):
        disp = widget.property("displayMember") or (table.columns[0] if table.columns else "")
        val = widget.property("valueMember") or ""
        di = table._index.get(str(disp).lower(), 0)
        vi = table._index.get(str(val).lower()) if val else None
        widget.blockSignals(True)
        widget.clear()
        for r, row in enumerate(table):
            text = to_text(tuple.__getitem__(row, di)) if table.columns else ""
            if isinstance(widget, QComboBox):
                widget.addItem(text, r)
            else:
                widget.addItem(text)
                widget.item(widget.count() - 1).setData(Qt.UserRole, r)
        widget.blockSignals(False)
        widget._ps_value_index = vi
        if isinstance(widget, QComboBox) and not widget.isEditable():
            widget.setCurrentIndex(0 if len(table) else -1)
    else:
        raise DataError(f"fill() can't show data in a {type(widget).__name__}")


def selected_value(widget):
    """The ValueMember column of the selected item (or the whole row if no ValueMember)."""
    row = current_row(widget)
    if row is None:
        return None
    vi = getattr(widget, "_ps_value_index", None)
    return tuple.__getitem__(row, vi) if vi is not None else row


# ------------------------------------------------------------------ settings (My.Settings)
_SETTINGS_OBJECTS = []


def _config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, _package_name(_proj().get("name") or "app"))


def _convert(kind, v):
    if v is None:
        return None
    try:
        if kind == "Integer":
            return int(float(v)) if not isinstance(v, int) or isinstance(v, bool) else v
        if kind == "Decimal":
            return float(v)
        if kind == "Boolean":
            return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")
        if kind == "StringList":
            if isinstance(v, (list, tuple)):
                return [str(x) for x in v]
            return [line for line in str(v).splitlines() if line.strip()]
        if kind == "Date":
            if isinstance(v, _dt.date):
                return v
            return _dt.date.fromisoformat(str(v).strip()) if str(v).strip() else None
    except (TypeError, ValueError):
        return None
    return str(v)


class SettingsBase:
    """Project settings (Project Properties > Settings). User-scope values are saved per user
    in ~/.config/<app>/settings.json; Application-scope values are read-only."""

    def __init__(self):
        self._defs = {s["name"]: s for s in _proj().get("settings", []) if s.get("name")}
        self._values = {}
        self._dirty = False
        self.reload()
        _SETTINGS_OBJECTS.append(self)

    @property
    def path(self):
        return os.path.join(_config_dir(), "settings.json")

    def _get(self, name):
        d = self._defs[name]
        if d.get("scope") != "Application" and name in self._values:
            return _convert(d.get("type", "String"), self._values[name])
        return _convert(d.get("type", "String"), d.get("value"))

    def _set(self, name, value):
        d = self._defs[name]
        if d.get("scope") == "Application":
            raise AttributeError(f"{name} is an Application setting (read-only); make it a User "
                                 "setting in Project Properties > Settings to change it")
        value = _convert(d.get("type", "String"), value)
        if isinstance(value, _dt.date):
            value = value.isoformat()
        if self._values.get(name, object()) != value:
            self._values[name] = value
            self._dirty = True

    def __getitem__(self, name):
        return self._get(name)

    def __setitem__(self, name, value):
        self._set(name, value)

    def names(self):
        return list(self._defs)

    def save(self):
        """Write the user settings (done automatically when the app closes)."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._values, f, indent=2)
        os.replace(tmp, self.path)
        self._dirty = False

    def reload(self):
        """Throw away unsaved changes and read the saved user settings again."""
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self._values = {k: v for k, v in data.items() if k in self._defs} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._values = {}
        self._dirty = False

    def reset(self):
        """Back to the default values from Project Properties."""
        self._values = {}
        self._dirty = True


def _save_settings_on_exit():
    for s in _SETTINGS_OBJECTS:
        if s._dirty:
            try:
                s.save()
            except OSError:
                pass
    close_connections()


# ------------------------------------------------------------------ resources (My.Resources)
class ResourcesBase:
    """Project resources (Project Properties > Resources)."""

    def __init__(self):
        self._defs = {r["name"]: r for r in _proj().get("resources", []) if r.get("name")}
        self._cache = {}

    def path(self, name):
        """Full path of a file resource."""
        v = self._defs[name].get("value", "")
        return v if os.path.isabs(v) else os.path.join(_HERE, v)

    def _get(self, name):
        d = self._defs[name]
        kind = d.get("type", "String")
        if kind == "String":
            return d.get("value", "")
        if name in self._cache:
            return self._cache[name]
        if kind == "Image":
            from PySide6.QtGui import QPixmap
            v = QPixmap(self.path(name))
        elif kind == "Text":
            with open(self.path(name), encoding="utf-8", errors="replace") as f:
                v = f.read()
        else:
            v = self.path(name)
        self._cache[name] = v
        return v

    def icon(self, name):
        """An image resource as a QIcon (for buttons, menus, window icons)."""
        return QIcon(self.path(name))

    def __getitem__(self, name):
        return self._get(name)
''' + "\n\n" + _BINDING + _EXTRA
