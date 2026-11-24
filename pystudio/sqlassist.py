"""SQL IntelliSense and query history for the Query Designer.

IntelliSense: as you type in a query, a list pops up with what fits where the cursor is -
  after FROM / JOIN / INTO / UPDATE: the connection's tables (and schemas)
  after  alias.  or  table.  : that table's columns, with their types
  anywhere else: the columns of the tables in the query, their aliases, tables, and SQL keywords.
Ctrl+Space opens it any time. Tab or Enter inserts the choice, Esc closes it.
Table and column names are read from the connection in the background and kept for this session
(Refresh in the History/IntelliSense menu reloads them). It never asks for a password while you type:
a connection that prompts for one gets IntelliSense after the first Execute.

History: every query you execute is saved (connection, when, rows, time, error) in
~/.local/share/pystudio/query_history.jsonl - the newest 5,000. The History button searches it."""
import datetime
import json
import os
import re

from PySide6.QtCore import QObject, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QCompleter, QDialog, QDialogButtonBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QSplitter, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

TABLE_WORDS = {"from", "join", "into", "update", "table", "describe", "desc", "truncate"}
NOT_ALIAS = {"where", "on", "join", "inner", "left", "right", "full", "outer", "cross", "group", "order", "having",
             "limit", "union", "set", "values", "using", "natural", "as", "with", "select", "and", "or", "fetch",
             "offset", "window", "qualify", "lateral", "pivot", "unpivot", "sample", "tablesample", "except",
             "intersect", "minus", "returning", "by", "into", "for", "top", "when", "then", "else", "end"}
IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[\w$#]+)'


def _bare(name):
    return name[1:-1] if name and name[0] in '"`[' else name


def statement_at(text, pos):
    """The statement around pos (split on ; outside quotes) and pos within it."""
    start, i, q = 0, 0, None
    bounds = []
    while i < len(text):
        ch = text[i]
        if q:
            if ch == q:
                q = None
        elif ch in "'\"`":
            q = ch
        elif ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        elif ch == ";":
            bounds.append((start, i))
            start = i + 1
        i += 1
    bounds.append((start, len(text)))
    for a, b in bounds:
        if a <= pos <= b:
            return text[a:b], pos - a
    return text, pos


def table_refs(sql):
    """{alias or table name (lowercase): (schema, table)} for the tables a statement uses."""
    refs = {}
    no_comments = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S)
    pat = re.compile(rf"\b(from|join|update|into)\s+({IDENT}(?:\.{IDENT}){{0,2}})(?:\s+(?:as\s+)?({IDENT}))?"
                     rf"((?:\s*,\s*{IDENT}(?:\.{IDENT}){{0,2}}(?:\s+(?:as\s+)?{IDENT})?)*)", re.I)
    one = re.compile(rf"({IDENT}(?:\.{IDENT}){{0,2}})(?:\s+(?:as\s+)?({IDENT}))?", re.I)

    def add(full, alias):
        parts = [_bare(p) for p in re.findall(IDENT, full)]
        if not parts or parts[-1].lower() in NOT_ALIAS or parts[-1].lower() == "select":
            return
        schema, table = (parts[-2] if len(parts) > 1 else ""), parts[-1]
        refs.setdefault(table.lower(), (schema, table))
        if alias and _bare(alias).lower() not in NOT_ALIAS:
            refs[_bare(alias).lower()] = (schema, table)
    for m in pat.finditer(no_comments):
        add(m.group(2), m.group(3))
        if m.group(1).lower() == "from" and m.group(4):
            for part in m.group(4).split(",")[1:]:
                mm = one.match(part.strip())
                if mm:
                    add(mm.group(1), mm.group(2))
    return refs


class SchemaCache(QObject):
    """Table and column names for one connection, read in the background."""
    changed = Signal()
    CACHES = {}

    @classmethod
    def for_doc(cls, doc):
        cfg = doc.cfg()
        if cfg is None:
            return None
        key = (getattr(doc.service, "project_key", ""), cfg.get("name"))
        c = cls.CACHES.get(key)
        if c is None:
            c = cls.CACHES[key] = SchemaCache(doc.service, cfg)
        c.cfg = cfg
        return c

    def __init__(self, service, cfg):
        super().__init__()
        self.service, self.cfg = service, cfg
        self.tables = None           # [(schema, name, kind)]
        self.columns = {}            # (schema lower, table lower) -> [(name, type, pk)]
        self._loading = set()
        self.error = ""

    def quiet_password(self):
        """The password if it's known without asking (None = would have to ask)."""
        if self.cfg.get("passwordMode", "None") != "Prompt":
            return self.service.password_for(self.cfg)
        pw = self.service.known_password(self.cfg.get("name"))
        if pw is not None:
            return pw
        try:
            from . import keystore
            return keystore.get(self.service._settings_key(self.cfg)) or None
        except Exception:  # noqa: BLE001
            return None

    def refresh(self):
        self.tables, self.columns, self._loading, self.error = None, {}, set(), ""
        self.load_tables()

    def load_tables(self):
        if self.tables is not None or "tables" in self._loading:
            return
        pw = self.quiet_password()
        if pw is None:
            return
        self._loading.add("tables")

        def done(rows):
            self._loading.discard("tables")
            self.tables = list(rows)
            self.changed.emit()

        def failed(msg):
            self._loading.discard("tables")
            self.tables, self.error = [], msg.splitlines()[0] if msg else "failed"
        self.service.tables(self.cfg, pw, done, failed)

    def find_table(self, schema, name):
        """(schema, name) as the database spells it, or None."""
        for s, n, _k in self.tables or []:
            if n.lower() == name.lower() and (not schema or (s or "").lower() == schema.lower()):
                return s, n
        return None

    def cols(self, schema, name):
        """Columns if loaded; otherwise starts loading them and returns None."""
        found = self.find_table(schema, name) or (schema, name)
        key = ((found[0] or "").lower(), found[1].lower())
        if key in self.columns:
            return self.columns[key]
        if key not in self._loading and self.tables is not None:
            pw = self.quiet_password()
            if pw is not None:
                self._loading.add(key)

                def done(res, key=key):
                    self._loading.discard(key)
                    self.columns[key] = list(res[0])
                    self.changed.emit()

                def failed(_msg, key=key):
                    self._loading.discard(key)
                    self.columns[key] = []
                self.service.columns(self.cfg, pw, found[0], found[1], done, failed)
        return None


class SqlCompleter(QObject):
    """Attaches IntelliSense to a SqlEdit inside a QueryDocument."""

    def __init__(self, doc, edit):
        super().__init__(edit)
        self.doc, self.edit = doc, edit
        self.model = QStringListModel()
        self.c = QCompleter(self.model, edit)
        self.c.setWidget(edit)
        self.c.setCaseSensitivity(Qt.CaseInsensitive)
        self.c.setFilterMode(Qt.MatchContains)
        self.c.setCompletionMode(QCompleter.PopupCompletion)
        self.c.setMaxVisibleItems(12)
        self.c.activated[str].connect(self.insert)
        self.popup = self.c.popup()
        self.popup.setStyleSheet("QListView { background: #252526; color: #d4d4d4; border: 1px solid #454545; }"
                                 "QListView::item:selected { background: #04395e; }")
        self.hints = {}
        self.cache = None
        self._prefix = ""
        self._wanted = False
        self._explicit = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.update)

    def _cache(self):
        c = SchemaCache.for_doc(self.doc)
        if c is not self.cache:
            if self.cache is not None:
                try:
                    self.cache.changed.disconnect(self._schema_changed)
                except (RuntimeError, TypeError):
                    pass
            self.cache = c
            if c is not None:
                c.changed.connect(self._schema_changed)
        if c is not None:
            c.load_tables()
        return c

    def _schema_changed(self):
        if self._wanted and self.edit.hasFocus():
            self.update(explicit=self._explicit)

    def warm(self):
        """Start reading the table names (called when the connection is chosen or after Execute)."""
        self._cache()

    # ------------------------------------------------------------ keys (called from SqlEdit.keyPressEvent)
    def key_before(self, e):
        """True = handled."""
        if self.popup.isVisible():
            if e.key() in (Qt.Key_Tab, Qt.Key_Return, Qt.Key_Enter):
                idx = self.popup.currentIndex()
                if not idx.isValid():
                    idx = self.c.completionModel().index(0, 0)
                if idx.isValid():
                    self.insert(idx.data())
                self.popup.hide()
                return True
            if e.key() == Qt.Key_Escape:
                self.popup.hide()
                self._wanted = False
                return True
        if e.key() == Qt.Key_Space and e.modifiers() & Qt.ControlModifier:
            self.update(explicit=True)
            return True
        return False

    def key_after(self, e):
        t = e.text()
        if t and (t.isalnum() or t in "_.$#"):
            self.timer.start()
        elif e.key() == Qt.Key_Backspace and self.popup.isVisible():
            self.timer.start()
        elif self.popup.isVisible() and t:
            self.popup.hide()
            self._wanted = False

    # ------------------------------------------------------------ choices
    def _context(self):
        cur = self.edit.textCursor()
        text = self.edit.toPlainText()
        stmt, pos = statement_at(text, cur.position())
        before = stmt[:pos]
        line = before.rsplit("\n", 1)[-1]
        if line.lstrip().startswith("--") or before.count("'") % 2:
            return None                                   # in a comment or a string
        m = re.search(rf"({IDENT})\.([\w$#]*)$", before)
        if m:
            return {"kind": "dot", "qual": _bare(m.group(1)), "prefix": m.group(2), "stmt": stmt}
        prefix = re.search(r"[\w$#]*$", before).group(0)
        rest = before[:len(before) - len(prefix)]
        prev = re.search(r"(\w+)\s*(?:,\s*[\w$#.\"]*\s*)*$", rest)
        prev_word = prev.group(1).lower() if prev else ""
        kind = "table" if prev_word in TABLE_WORDS else "any"
        return {"kind": kind, "prefix": prefix, "stmt": stmt}

    def _items(self, ctx):
        from .querydesigner import SQL_KEYWORDS
        cache = self._cache()
        tables = (cache.tables or []) if cache else []
        hints = {}
        items = []

        def add(name, hint):
            if name and name not in hints:
                hints[name] = hint
                items.append(name)
        refs = table_refs(ctx["stmt"])
        if ctx["kind"] == "dot":
            q = ctx["qual"].lower()
            target = refs.get(q) or ((ctx["qual"] and cache and cache.find_table("", ctx["qual"])) or None)
            if target and cache:
                cols = cache.cols(*target)
                for name, typ, pk in cols or []:
                    add(name, f"{typ}{' (key)' if pk else ''} - {target[1]}")
            # schema.<tables>
            for s, n, kind in tables:
                if (s or "").lower() == q:
                    add(n, "view" if kind == "VIEW" else "table")
        elif ctx["kind"] == "table":
            for s, n, kind in tables:
                add(n, ("view" if kind == "VIEW" else "table") + (f" - {s}" if s else ""))
            for s in sorted({s for s, _n, _k in tables if s}):
                add(s, "schema")
        else:
            for alias, (s, n) in refs.items():
                if cache:
                    for name, typ, pk in cache.cols(s, n) or []:
                        add(name, f"{typ}{' (key)' if pk else ''} - {n}")
            for alias, (s, n) in refs.items():
                if alias != n.lower():
                    add(alias, f"alias for {n}")
            for s, n, kind in tables:
                add(n, "view" if kind == "VIEW" else "table")
            lower = ctx["prefix"][:1].islower()
            for kw in sorted(SQL_KEYWORDS):
                add(kw if lower else kw.upper(), "keyword")
        return items, hints

    def update(self, explicit=False):
        ctx = self._context()
        if ctx is None:
            self.popup.hide()
            return
        prefix = ctx["prefix"]
        if not explicit and not self.popup.isVisible() and ctx["kind"] != "dot" and len(prefix) < 2:
            self.popup.hide()
            return
        self._wanted = True
        self._explicit = explicit or self.popup.isVisible()
        items, self.hints = self._items(ctx)
        if not items:
            self.popup.hide()
            return
        self.model.setStringList(items)
        for i, name in enumerate(items):
            self.model.setData(self.model.index(i), self.hints.get(name, ""), Qt.ToolTipRole)
        self._prefix = prefix
        self.c.setCompletionPrefix(prefix)
        if self.c.completionCount() == 0 or (self.c.completionCount() == 1 and
                                             self.c.currentCompletion().lower() == prefix.lower()):
            self.popup.hide()
            return
        self.popup.setCurrentIndex(self.c.completionModel().index(0, 0))
        r = self.edit.cursorRect()
        r.setWidth(max(260, self.popup.sizeHintForColumn(0) + self.popup.verticalScrollBar().sizeHint().width()
                       + 20))
        self.c.complete(r)

    def insert(self, text):
        if not text:
            return
        cur = self.edit.textCursor()
        before = self.edit.toPlainText()[:cur.position()]
        n = len(re.search(r"[\w$#]*$", before).group(0))
        cur.beginEditBlock()
        cur.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, n)
        needs_quotes = not re.fullmatch(r"[A-Za-z_][\w$#]*", text)
        cur.insertText(f'"{text}"' if needs_quotes else text)
        cur.endEditBlock()
        self.edit.setTextCursor(cur)
        self._wanted = False


# ============================================================ history
def history_path():
    return os.path.join(os.path.expanduser("~"), ".local", "share", "pystudio", "query_history.jsonl")


MAX_HISTORY = 5000


def record(connection, sql, ok, rows=None, seconds=0.0, error="", project=""):
    entry = {"time": datetime.datetime.now().isoformat(timespec="seconds"), "connection": connection or "",
             "project": project or "", "sql": sql.strip(), "ok": bool(ok), "rows": rows,
             "seconds": round(seconds or 0.0, 3), "error": (error or "").strip()[:2000]}
    path = history_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        if os.path.getsize(path) > 8_000_000:       # keep the newest MAX_HISTORY
            entries = load()
            save(entries[-MAX_HISTORY:])
    except OSError:
        pass
    return entry


def load():
    out = []
    try:
        with open(history_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def save(entries):
    path = history_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    os.replace(tmp, path)


class HistoryDialog(QDialog):
    """Query history: search, then Insert / Replace / Run, Copy or Delete."""

    def __init__(self, doc, parent=None):
        super().__init__(parent or doc)
        self.doc = doc
        self.setWindowTitle("Query History")
        self.resize(980, 620)
        self.entries = load()
        v = QVBoxLayout(self)
        h = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search the SQL, connection or error...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.fill)
        h.addWidget(self.search, 1)
        self.ck_conn = QCheckBox(f"Only {doc.cb_conn.currentText() or 'this connection'}")
        self.ck_conn.setChecked(bool(doc.cb_conn.currentText()))
        self.ck_conn.toggled.connect(self.fill)
        h.addWidget(self.ck_conn)
        self.ck_errors = QCheckBox("Hide errors")
        self.ck_errors.toggled.connect(self.fill)
        h.addWidget(self.ck_errors)
        v.addLayout(h)
        split = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["When", "Connection", "Result", "Time", "Query"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setSectionResizeMode(4, QHeaderView.Stretch)
        for i, w in enumerate((140, 140, 110, 70)):
            self.tree.setColumnWidth(i, w)
        self.tree.currentItemChanged.connect(self._show)
        self.tree.itemDoubleClicked.connect(lambda *_: self.use("replace"))
        split.addWidget(self.tree)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setStyleSheet("font-family: monospace;")
        split.addWidget(self.preview)
        split.setSizes([380, 180])
        v.addWidget(split, 1)
        self.count = QLabel()
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.b_run = bb.addButton("Run", QDialogButtonBox.ActionRole)
        self.b_run.setToolTip("Put it in the editor (replacing the query) and execute it")
        self.b_run.clicked.connect(lambda: self.use("run"))
        b = bb.addButton("Replace Query", QDialogButtonBox.ActionRole)
        b.setToolTip("Replace the editor's query with this one (double-click does the same)")
        b.clicked.connect(lambda: self.use("replace"))
        b = bb.addButton("Insert at Cursor", QDialogButtonBox.ActionRole)
        b.clicked.connect(lambda: self.use("insert"))
        b = bb.addButton("Copy", QDialogButtonBox.ActionRole)
        b.clicked.connect(self.copy)
        b = bb.addButton("Delete", QDialogButtonBox.ActionRole)
        b.clicked.connect(self.delete)
        b = bb.addButton("Clear All...", QDialogButtonBox.ActionRole)
        b.clicked.connect(self.clear_all)
        bb.rejected.connect(self.reject)
        hb = QHBoxLayout()
        hb.addWidget(self.count, 1)
        hb.addWidget(bb)
        v.addLayout(hb)
        self.fill()

    def fill(self):
        words = self.search.text().lower().split()
        conn = self.doc.cb_conn.currentText() if self.ck_conn.isChecked() else None
        self.tree.clear()
        shown = 0
        for i in range(len(self.entries) - 1, -1, -1):
            e = self.entries[i]
            if conn is not None and e.get("connection") != conn:
                continue
            if self.ck_errors.isChecked() and not e.get("ok"):
                continue
            hay = f"{e.get('sql', '')} {e.get('connection', '')} {e.get('error', '')}".lower()
            if any(w not in hay for w in words):
                continue
            when = e.get("time", "").replace("T", " ")
            result = (f"{e['rows']} row(s)" if e.get("rows") is not None else "OK") if e.get("ok") else "Error"
            first = " ".join(e.get("sql", "").split())[:200]
            it = QTreeWidgetItem([when, e.get("connection", ""), result, f"{e.get('seconds', 0):.2f} s", first])
            if not e.get("ok"):
                from PySide6.QtGui import QColor
                it.setForeground(2, QColor("#f48771"))
            it.setData(0, Qt.UserRole, i)
            self.tree.addTopLevelItem(it)
            shown += 1
            if shown >= 2000:
                break
        self.count.setText(f"{shown} of {len(self.entries)} queries")
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self.preview.clear()

    def _selected(self):
        it = self.tree.currentItem()
        return None if it is None else self.entries[it.data(0, Qt.UserRole)]

    def _show(self, it, _prev=None):
        e = self._selected()
        if e is None:
            self.preview.clear()
            return
        text = e.get("sql", "")
        if e.get("error"):
            text += "\n\n-- Error:\n-- " + e["error"].replace("\n", "\n-- ")
        self.preview.setPlainText(text)

    def use(self, how):
        e = self._selected()
        if e is None:
            return
        self.doc.use_history(e, how)
        self.accept()

    def copy(self):
        from PySide6.QtWidgets import QApplication
        e = self._selected()
        if e:
            QApplication.clipboard().setText(e.get("sql", ""))

    def delete(self):
        idx = {it.data(0, Qt.UserRole) for it in self.tree.selectedItems()}
        if not idx:
            return
        self.entries = [e for i, e in enumerate(self.entries) if i not in idx]
        save(self.entries)
        self.fill()

    def clear_all(self):
        if QMessageBox.question(self, "Query History", "Delete the whole query history?") != QMessageBox.Yes:
            return
        self.entries = []
        save([])
        self.fill()
