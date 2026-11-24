"""View Call Hierarchy (Ctrl+K, Ctrl+T, or right-click a function): like Visual Studio's window. For the
function at the cursor it shows "Calls To" - every function in the project that calls it - and "Calls
From" - every function it calls. Each of those expands the same way, so you can follow a chain up to
where it starts or down to where it ends. Double-click to open; the Lines column is where the calls are.
Uses Jedi (the same engine as Go To Definition and Find All References)."""
import ast
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHeaderView, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

ROLE = Qt.UserRole


# ============================================================ analysis (runs on the Jedi worker thread)
def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _defs(tree):
    """[(start, end, qualname, node)] for every function/class, outer first."""
    out = []

    def walk(node, prefix):
        for n in ast.iter_child_nodes(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}.{n.name}" if prefix else n.name
                out.append((n.lineno, getattr(n, "end_lineno", n.lineno), q, n))
                walk(n, q)
            else:
                walk(n, prefix)
    walk(tree, "")
    return out


def enclosing(source, line):
    """(qualname, def line, def col) of the innermost function containing line, or None (module level)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    best = None
    for start, end, q, n in _defs(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and start <= line <= end:
            if best is None or start >= best[1]:
                best = (q, start, n.col_offset + (6 if isinstance(n, ast.AsyncFunctionDef) else 0) + 4)
    return best


def target_at(svc, code, path, line, col, ctx):
    """The function the cursor is on (its name, or anywhere inside it)."""
    script = svc._script(code, path, ctx)
    for d in script.goto(line, col, follow_imports=True) or []:
        if d.type in ("function", "class") and d.line:
            mp = str(d.module_path) if d.module_path else path
            return {"name": d.name, "qual": d.full_name or d.name, "path": mp, "line": d.line, "col": d.column,
                    "builtin": d.in_builtin_module()}
    enc = enclosing(code, line)
    if enc:
        q, l, c = enc
        return {"name": q.rsplit(".", 1)[-1], "qual": q, "path": path, "line": l, "col": c, "builtin": False}
    return None


def callers(svc, item, ctx):
    """Functions that call item: [{name, qual, path, line, col, lines}]"""
    code = _read(item["path"])
    script = svc._script(code, item["path"], ctx)
    groups = {}
    for r in script.get_references(item["line"], item["col"], scope="project"):
        mp = str(r.module_path) if r.module_path else None
        if not mp or "site-packages" in mp or (mp == item["path"] and r.line == item["line"]):
            continue
        src = code if mp == item["path"] else _read(mp)
        line_text = src.splitlines()[r.line - 1] if 0 < r.line <= len(src.splitlines()) else ""
        if line_text.lstrip().startswith(("import ", "from ")):
            continue                               # an import isn't a call
        rest = line_text[r.column + len(item["name"]):].lstrip()
        call = rest.startswith("(")
        enc = enclosing(src, r.line)
        if enc:
            q, l, c = enc
        else:
            q, l, c = f"(module level) {os.path.basename(mp)}", 1, 0
        key = (mp, q)
        g = groups.setdefault(key, {"name": q.rsplit(".", 1)[-1] if enc else q, "qual": q, "path": mp, "line": l,
                                    "col": c, "lines": [], "module": not enc, "values": 0})
        g["lines"].append(r.line)
        if not call:
            g["values"] += 1
    return sorted(groups.values(), key=lambda g: (g["path"], g["line"]))


def callees(svc, item, ctx):
    """Functions item calls: [{name, qual, path, line, col, lines, builtin}]"""
    code = _read(item["path"])
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    fn = None
    for start, _end, _q, n in _defs(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and start == item["line"]:
            fn = n
            break
    if fn is None:
        return []
    script = svc._script(code, item["path"], ctx)
    found = {}

    def visit(node):
        for n in ast.iter_child_nodes(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue                           # calls inside nested functions belong to them
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Attribute):
                    ln, col = f.end_lineno, f.end_col_offset - len(f.attr)
                elif isinstance(f, ast.Name):
                    ln, col = f.lineno, f.col_offset
                else:
                    ln = None
                if ln:
                    try:
                        ds = script.goto(ln, col, follow_imports=True)
                    except Exception:  # noqa: BLE001
                        ds = []
                    for d in ds[:1]:
                        mp = str(d.module_path) if d.module_path else ""
                        key = (mp, d.line, d.full_name or d.name)
                        e = found.setdefault(key, {"name": d.name, "qual": d.full_name or d.name, "path": mp,
                                                   "line": d.line or 0, "col": d.column or 0, "lines": [],
                                                   "builtin": d.in_builtin_module() or not mp or
                                                   "site-packages" in mp or "/lib/python" in mp,
                                                   "kind": d.type})
                        e["lines"].append(ln)
            visit(n)
    visit(fn)
    return sorted(found.values(), key=lambda e: (e["builtin"], min(e["lines"])))


# ============================================================ the window
class CallHierarchyWindow(QWidget):
    open_location = Signal(str, int, int)

    def __init__(self, lang, context, parent=None):
        super().__init__(parent)
        self.lang, self.context = lang, context
        self.root = None
        self.title = QLabel("Right-click a function > View Call Hierarchy (Ctrl+K, Ctrl+T).")
        self.title.setStyleSheet("color: #9a9a9a; padding: 2px 4px;")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "File", "Lines"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.setColumnWidth(1, 220)
        self.tree.setColumnWidth(2, 160)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.itemDoubleClicked.connect(self._open)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.title)
        v.addWidget(self.tree)

    def _rel(self, path):
        root = self.context()[0] if self.context else None
        return os.path.relpath(path, root) if root and path and path.startswith(root) else os.path.basename(path or "")

    def show_for(self, code, path, line, col):
        """Start from the function at the cursor."""
        ctx = self.context()

        def got(item):
            self.tree.clear()
            if not item:
                self.title.setText("Put the cursor on a function (its name, or anywhere inside it).")
                return
            self.root = item
            self.title.setText(f"Call Hierarchy: {item['qual']}")
            top = QTreeWidgetItem([item["qual"], self._rel(item["path"]), str(item["line"])])
            top.setData(0, ROLE, ("item", item, None))
            self.tree.addTopLevelItem(top)
            for direction, label in (("to", f"Calls To '{item['name']}'"), ("from", f"Calls From '{item['name']}'")):
                g = QTreeWidgetItem([label, "", ""])
                g.setData(0, ROLE, ("group", item, direction))
                g.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                top.addChild(g)
            top.setExpanded(True)
            top.child(0).setExpanded(True)
        self._run(lambda svc: target_at(svc, code, path, line, col, ctx), got)

    def _run(self, fn, callback):
        """A job on the language service's (single) Jedi thread."""
        svc = self.lang
        if svc is None or not svc.available:
            self.title.setText("Call Hierarchy needs Jedi (installed with PyStudio).")
            return
        from .language import _Job
        req_id = next(svc._ids)
        kind = f"calls{req_id}"
        svc._latest[kind] = req_id
        svc._callbacks[req_id] = callback
        svc.pool.start(_Job(svc, req_id, kind, lambda: fn(svc)))

    def _expand(self, it):
        data = it.data(0, ROLE)
        if not data or it.childCount() or data[0] not in ("group", "node"):
            return
        _k, item, direction = data
        ctx = self.context()
        loading = QTreeWidgetItem(["(searching...)", "", ""])
        it.addChild(loading)

        def got(entries):
            it.takeChildren()
            if not entries:
                it.addChild(QTreeWidgetItem(["(none)", "", ""]))
                return
            for e in entries:
                lines = ", ".join(str(n) for n in sorted(set(e["lines"]))[:8]) + \
                    ("..." if len(set(e["lines"])) > 8 else "")
                name = e["qual"] if direction == "to" else e["name"]
                if direction == "to" and e.get("values"):
                    name += "   (passed as a value)" if e["values"] == len(e["lines"]) else ""
                c = QTreeWidgetItem([name, "" if e.get("builtin") and direction == "from" and not e["path"]
                                     else self._rel(e["path"]), lines])
                c.setToolTip(0, e["qual"])
                c.setData(0, ROLE, ("node", e, direction))
                expandable = not e.get("builtin") and not e.get("module") and e.get("kind", "function") == "function"
                c.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator if expandable
                                          else QTreeWidgetItem.DontShowIndicator)
                if e.get("builtin"):
                    c.setForeground(0, c.foreground(0).color().darker(140))
                it.addChild(c)
        fn = callers if direction == "to" else callees
        self._run(lambda svc: fn(svc, item, ctx), got)

    def _open(self, it, col):
        data = it.data(0, ROLE)
        if not data or data[0] == "group":
            return
        e = data[1]
        if not e.get("path") or not os.path.isfile(e["path"]):
            return
        if data[0] == "node" and col == 2 and e.get("lines"):
            # the Lines column: go to the first call, not the definition
            where = e["path"] if data[2] == "to" else (self._parent_path(it) or e["path"])
            self.open_location.emit(where, min(e["lines"]), 0)
            return
        self.open_location.emit(e["path"], e["line"], e.get("col", 0))

    def _parent_path(self, it):
        p = it.parent()
        while p is not None:
            d = p.data(0, ROLE)
            if d and d[0] in ("item", "node"):
                return d[1].get("path")
            p = p.parent()
        return None
