# ------------------------------------------------------------------ ContextMenuStrip / NotifyIcon / ErrorProvider / BindingSource
from PySide6.QtCore import QEvent as _CQEvent, QObject as _CQObject, QPoint as _CQPoint, Signal as _CSignal
from PySide6.QtGui import QIcon as _CQIcon, QKeySequence as _CQKeySequence, QPainter as _CQPainter, QPixmap as _CQPixmap
from PySide6.QtWidgets import (QAbstractScrollArea as _CQScroll, QLabel as _CQLabel, QMenu as _CQMenu,
                               QSystemTrayIcon as _CQTray)


def _project_file(rel):
    """A path inside the project (images/...), wherever the program runs from (also when published)."""
    if not rel or os.path.isabs(rel):
        return rel
    for base in (getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(sys.argv[0] or ".")), os.getcwd()):
        if base and os.path.exists(os.path.join(base, rel)):
            return os.path.join(base, rel)
    return rel


class ContextMenuStrip(_CQMenu):
    """The right-click menu of one or more controls (their ContextMenuStrip property).

        def on_contextMenuStrip1_triggered(self, action):
            if action is self.ui.actionCopy: ...
            print(self.ui.contextMenuStrip1.source_control.objectName())   # the control right-clicked

    Each item is also self.ui.<item name> (a QAction), so its own triggered signal works too."""

    def __init__(self, parent, items=None):
        super().__init__(parent)
        self.source_control = None
        self.items = {}
        self._build(self, items or [])

    def _build(self, menu, items):
        for it in items:
            if it.get("sep"):
                menu.addSeparator()
                continue
            if it.get("items"):
                sub = menu.addMenu(it.get("text", ""))
                sub.setObjectName(it.get("name", ""))
                self._build(sub, it["items"])
                if it.get("name"):
                    self.items[it["name"]] = sub.menuAction()
                continue
            a = menu.addAction(it.get("text", ""))
            a.setObjectName(it.get("name", ""))
            if it.get("shortcut"):
                a.setShortcut(_CQKeySequence(it["shortcut"]))
            if it.get("checkable"):
                a.setCheckable(True)
                a.setChecked(bool(it.get("checked")))
            if it.get("enabled") is False:
                a.setEnabled(False)
            if it.get("tooltip"):
                a.setToolTip(it["tooltip"])
            if it.get("name"):
                self.items[it["name"]] = a

    def item(self, name):
        return self.items.get(name)

    def show_for(self, widget, pos=None):
        """Open the menu for widget (at pos in widget coordinates, or under the mouse)."""
        self.source_control = widget
        if pos is None:
            from PySide6.QtGui import QCursor
            self.popup(QCursor.pos())
        else:
            area = widget.viewport() if isinstance(widget, _CQScroll) else widget
            self.popup(area.mapToGlobal(pos))


def _attach_context_menus(window, target):
    """Controls whose ContextMenuStrip property names a menu get it on right-click."""
    widgets = window.findChildren(QWidget)
    central = window.centralWidget() if hasattr(window, "centralWidget") else None
    if window.property("contextMenuStrip") and central is not None:
        central.setProperty("contextMenuStrip", window.property("contextMenuStrip"))
    for w in widgets:
        name = w.property("contextMenuStrip")
        if not name:
            continue
        menu = getattr(target, str(name), None)
        if not isinstance(menu, ContextMenuStrip):
            continue
        w.setContextMenuPolicy(Qt.CustomContextMenu)
        w.customContextMenuRequested.connect(lambda pos, w=w, m=menu: m.show_for(w, pos))


def _tray_icon_pixmap():
    pm = _CQPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = _CQPainter(pm)
    p.setRenderHint(_CQPainter.Antialiasing)
    p.setBrush(QColor("#3c8fd8"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 28, 28)
    p.end()
    return pm


class NotifyIcon(_CQTray):
    """An icon in the system tray (top bar on Ubuntu), like VB's NotifyIcon.

        self.ui.notifyIcon1.show_balloon("Backup", "Finished copying 120 files")
        self.ui.notifyIcon1.text = "MyApp - idle"          # the tooltip
    Events: clicked, doubleClicked, messageClicked (the balloon was clicked).
    Ubuntu shows tray icons through its AppIndicator extension (on by default)."""
    clicked = _CSignal()
    doubleClicked = _CSignal()

    def __init__(self, parent, text="", icon="", visible=True, menu="", balloonTitle="", balloonText=""):
        super().__init__(parent)
        path = _project_file(icon)
        ic = _CQIcon(path) if path and os.path.exists(path) else QApplication.windowIcon()
        self.setIcon(ic if not ic.isNull() else _CQIcon(_tray_icon_pixmap()))
        self.setToolTip(text)
        self.menu = menu                       # a ContextMenuStrip's name; linked after all are made
        self.balloon_title, self.balloon_text = balloonTitle, balloonText
        self.activated.connect(self._on_activated)
        if visible:
            self.show()

    @property
    def text(self):
        return self.toolTip()

    @text.setter
    def text(self, value):
        self.setToolTip(value)

    def _on_activated(self, reason):
        if reason == _CQTray.DoubleClick:
            self.doubleClicked.emit()
        elif reason == _CQTray.Trigger:
            self.clicked.emit()

    def show_balloon(self, title=None, text=None, icon="Information", ms=4000):
        icons = {"None": _CQTray.NoIcon, "Information": _CQTray.Information, "Warning": _CQTray.Warning,
                 "Error": _CQTray.Critical}
        self.showMessage(title if title is not None else self.balloon_title,
                         text if text is not None else self.balloon_text, icons.get(icon, _CQTray.Information), ms)


def _error_pixmap(size=16):
    pm = _CQPixmap(size, size)
    pm.fill(Qt.transparent)
    p = _CQPainter(pm)
    p.setRenderHint(_CQPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#e51400"))
    p.drawEllipse(0, 0, size, size)
    p.setPen(QColor("#ffffff"))
    f = p.font()
    f.setBold(True)
    f.setPixelSize(int(size * 0.8))
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "!")
    p.end()
    return pm


class ErrorProvider(_CQObject):
    """A red ! next to a control with a message on hover, like VB's ErrorProvider:

        if not self.ui.textBox1.text():
            self.ui.errorProvider1.set_error(self.ui.textBox1, "Name is required")
        else:
            self.ui.errorProvider1.set_error(self.ui.textBox1, "")      # clears it
        if self.ui.errorProvider1.has_errors(): return"""

    def __init__(self, parent, icon=""):
        super().__init__(parent)
        path = _project_file(icon)
        self._pixmap = _CQPixmap(path).scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation) \
            if path and os.path.exists(path) else _error_pixmap()
        self._marks = {}                       # widget -> (QLabel, text)

    def set_error(self, widget, text):
        old = self._marks.pop(widget, None)
        if old is not None:
            old[0].deleteLater()
            widget.removeEventFilter(self)
        if not text:
            return
        parent = widget.parentWidget() or widget
        lab = _CQLabel(parent)
        lab.setPixmap(self._pixmap)
        lab.setToolTip(text)
        lab.resize(16, 16)
        self._marks[widget] = (lab, text)
        widget.installEventFilter(self)
        self._place(widget)
        lab.setVisible(widget.isVisible())
        lab.raise_()

    def get_error(self, widget):
        m = self._marks.get(widget)
        return m[1] if m else ""

    def has_errors(self):
        return bool(self._marks)

    def clear(self):
        for w in list(self._marks):
            self.set_error(w, "")

    def _place(self, widget):
        lab = self._marks[widget][0]
        g = widget.geometry()
        lab.move(g.right() + 4, g.top() + (g.height() - 16) // 2)

    def eventFilter(self, obj, ev):
        if obj in self._marks:
            t = ev.type()
            if t in (_CQEvent.Move, _CQEvent.Resize):
                self._place(obj)
            elif t == _CQEvent.Show:
                self._marks[obj][0].show()
            elif t == _CQEvent.Hide:
                self._marks[obj][0].hide()
        return False


class BindingSource(_CQObject):
    """Rows from a query that several controls follow, like VB's BindingSource:

        bs = self.ui.bindingSource1              # DataSource = "GetDrivers" (a saved query or SELECT)
        bs.bind(self.ui.textBox1, "Name")        # textBox1 shows the current row's Name
        bs.fill(self.ui.dataGridView1)           # the grid shows the rows; clicking a row moves position
        bs.move_next(); bs.current.Name; bs.position; bs.count
        bs.filter = "Status = 'Active' AND Score >= 80";  bs.sort = "Name ASC"
        bs.refresh(region="East")                 # run the query again (with parameters)"""
    positionChanged = _CSignal(int)
    listChanged = _CSignal()

    def __init__(self, parent, dataSource="", filter="", sort="", connection=""):  # noqa: A002
        super().__init__(parent)
        self.data_source, self.connection = dataSource, connection or None
        self._filter, self._sort = filter, sort
        self.table = None                      # everything the query returned
        self.rows = []                         # after filter and sort
        self._pos = -1
        self._bindings = []                    # (widget, column)
        self._grids = []
        if dataSource:
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001 - show it, don't stop the form opening
                print(f"BindingSource {self.objectName() or ''}: {e}")

    # ---------------------------------------------------------- data
    def refresh(self, **params):
        self.table = query(self.data_source, self.connection, **params) if self.data_source else DataTable()
        self._apply()

    def set_data(self, table):
        """Use a DataTable you already have (e.g. from my.query)."""
        self.table = table
        self._apply()

    @property
    def filter(self):
        return self._filter

    @filter.setter
    def filter(self, value):
        self._filter = value or ""
        self._apply()

    @property
    def sort(self):
        return self._sort

    @sort.setter
    def sort(self, value):
        self._sort = value or ""
        self._apply()

    def _apply(self):
        t = self.table if self.table is not None else DataTable()
        rows = list(t)
        if self._filter:
            test = _bs_compile_filter(self._filter, t.columns)
            rows = [r for r in rows if test(r)]
        for part in reversed([p.strip() for p in self._sort.split(",") if p.strip()]):
            bits = part.split()
            col, desc = bits[0], len(bits) > 1 and bits[1].upper() == "DESC"
            idx = _bs_col(t.columns, col)
            rows.sort(key=lambda r, i=idx: (r[i] is None, r[i]), reverse=desc)
        self.rows = rows
        self.listChanged.emit()
        for g in self._grids:
            fill(g, DataTable(t.columns, [tuple(r) for r in rows]))
        self.position = 0 if rows else -1

    # ---------------------------------------------------------- position
    @property
    def count(self):
        return len(self.rows)

    @property
    def position(self):
        return self._pos

    @position.setter
    def position(self, value):
        value = max(-1, min(int(value), len(self.rows) - 1))
        changed = value != self._pos
        self._pos = value
        self._push()
        for g in self._grids:
            if 0 <= value < g.rowCount() and g.currentRow() != value:
                g.blockSignals(True)
                g.selectRow(value)
                g.blockSignals(False)
        if changed:
            self.positionChanged.emit(value)

    @property
    def current(self):
        return self.rows[self._pos] if 0 <= self._pos < len(self.rows) else None

    def move_first(self):
        self.position = 0

    def move_previous(self):
        self.position = max(0, self._pos - 1)

    def move_next(self):
        self.position = min(len(self.rows) - 1, self._pos + 1)

    def move_last(self):
        self.position = len(self.rows) - 1

    def find(self, column, value):
        """Index of the first row whose column equals value (-1 if none)."""
        i = _bs_col(self.table.columns, column)
        return next((n for n, r in enumerate(self.rows) if r[i] == value), -1)

    # ---------------------------------------------------------- controls
    def bind(self, widget, column):
        self._bindings.append((widget, column))
        self._push()

    def fill(self, grid):
        """Show the rows in a DataGridView; selecting a row there moves the position."""
        self._grids.append(grid)
        t = self.table if self.table is not None else DataTable()
        fill(grid, DataTable(t.columns, [tuple(r) for r in self.rows]))
        grid.currentCellChanged.connect(lambda row, _c, _pr, _pc: setattr(self, "position", row)
                                        if 0 <= row < len(self.rows) else None)
        if self._pos >= 0:
            grid.selectRow(self._pos)

    def _push(self):
        row = self.current
        for w, col in self._bindings:
            v = None if row is None else row[_bs_col(self.table.columns, col)]
            _bs_show(w, v)


def _bs_col(columns, name):
    low = [str(c).lower() for c in columns]
    try:
        return low.index(str(name).lower())
    except ValueError:
        raise DataError(f"There's no column named {name} (columns: {', '.join(map(str, columns))})") from None


def _bs_show(w, v):
    from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox, QLineEdit, QPlainTextEdit,
                                   QSpinBox, QTextEdit)
    if isinstance(w, QCheckBox):
        w.setChecked(bool(v))
    elif isinstance(w, (QSpinBox,)):
        w.setValue(int(v or 0))
    elif isinstance(w, QDoubleSpinBox):
        w.setValue(float(v or 0))
    elif isinstance(w, QDateEdit):
        d = QDate.fromString(str(v)[:10], "yyyy-MM-dd") if v else QDate()
        if d.isValid():
            w.setDate(d)
    elif isinstance(w, QComboBox):
        w.setCurrentText(to_text(v))
    elif isinstance(w, (QPlainTextEdit, QTextEdit)):
        w.setPlainText(to_text(v))
    elif hasattr(w, "setText"):
        w.setText(to_text(v))


def _bs_compile_filter(text, columns):
    """BindingSource.Filter, the VB way:  Name = 'Smith' AND (Score >= 80 OR Status <> 'X'),
    Name LIKE 'Sm%', Notes IS NULL, Region IN ('East', 'West')  ->  a function(row) -> bool."""
    import re as _r
    tok_re = _r.compile(r"\s*(?:('(?:[^']|'')*')|(-?\d+(?:\.\d+)?)|(<>|<=|>=|!=|=|<|>|\(|\)|,)"
                        r"|(\[[^\]]+\]|[A-Za-z_]\w*))", _r.S)
    pos, out, text = 0, [], text.strip()
    cols = {str(c).lower(): i for i, c in enumerate(columns)}
    while pos < len(text):
        m = tok_re.match(text, pos)
        if not m or m.end() == pos:
            raise DataError(f"Can't read the filter near: {text[pos:pos + 20]}")
        pos = m.end()
        s, n, op, w = m.groups()
        if s is not None:
            out.append(repr(s[1:-1].replace("''", "'")))
        elif n is not None:
            out.append(n)
        elif op is not None:
            out.append({"=": "==", "<>": "!="}.get(op, op))
        else:
            word = w.strip("[]")
            up = word.upper()
            if up in ("AND", "OR", "NOT"):
                out.append(up.lower())
            elif up == "NULL":
                out.append("None")
            elif up == "IS":
                out.append("is")
            elif up == "LIKE":
                out.append("|_like_|")
            elif up == "IN":
                out.append("in")
            elif up in ("TRUE", "FALSE"):
                out.append(up.capitalize())
            elif word.lower() in cols:
                out.append(f"r[{cols[word.lower()]}]")
            else:
                raise DataError(f"The filter uses {word}, which isn't a column")
    code = " ".join(out)
    # LIKE:  x |_like_| 'pat'  ->  _like(x, 'pat')
    code = _r.sub(r"(r\[\d+\])\s*\|_like_\|\s*('(?:[^'\\]|\\.)*')", r"_bs_like(\1, \2)", code)
    try:
        fn = eval("lambda r: " + code, {"_bs_like": _bs_like, "__builtins__": {}})  # noqa: S307 - our own tokens only
    except SyntaxError:
        raise DataError(f"Can't read the filter: {text}") from None

    def test(row):
        try:
            return bool(fn(row))
        except TypeError:          # comparing None with a number, etc.
            return False
    return test


def _bs_like(value, pattern):
    import re as _r
    if value is None:
        return False
    rx = "".join(".*" if ch == "%" else "." if ch == "_" else _r.escape(ch) for ch in pattern)
    return _r.fullmatch(rx, str(value), _r.I | _r.S) is not None


_FACTORIES.update({"ContextMenuStrip": ContextMenuStrip, "NotifyIcon": NotifyIcon,
                   "ErrorProvider": ErrorProvider, "BindingSource": BindingSource})


def _components_extra(window, target, made):
    """After all components exist: menu items as self.ui.<name>, NotifyIcon menus, right-click menus."""
    for obj in made:
        if isinstance(obj, ContextMenuStrip):
            for name, action in obj.items.items():
                if name and not hasattr(target, name):
                    setattr(target, name, action)
        if isinstance(obj, NotifyIcon) and isinstance(obj.menu, str) and obj.menu:
            m = getattr(target, obj.menu, None)
            if isinstance(m, _CQMenu):
                obj.setContextMenu(m)
                obj.menu = m
    _attach_context_menus(window, target)
