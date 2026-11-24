"""Class View (VS's View > Class View): every class in the solution with its methods, properties and fields,
plus module-level functions - read with Python's own parser (ast), so it's quick and needs nothing running.

Projects > files > classes > members. Base Types lists what a class inherits from; with "Show inherited
members" a class also lists the members it gets from its base classes in the project (grayed). Search
filters everything; double-click (or Enter) goes to the code."""
import ast
import glob
import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLineEdit, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import icons

SKIP_DIRS = {"__pycache__", "venv", ".venv", "build", "dist", "publish", ".git", "node_modules", ".mypy_cache",
             ".pytest_cache", ".idea"}
SKIP_FILES = {"pystudio_runtime.py", "my.py"}
MAX_FILES = 600


# ============================================================ parsing
class Member:
    __slots__ = ("name", "kind", "line", "sig", "owner")

    def __init__(self, name, kind, line, sig="", owner=None):
        self.name, self.kind, self.line, self.sig, self.owner = name, kind, line, sig, owner


class ClassInfo:
    def __init__(self, name, path, line, bases, doc):
        self.name, self.path, self.line, self.bases, self.doc = name, path, line, bases, doc
        self.members = []
        self.nested = []


def _sig(fn):
    a = fn.args
    names = [x.arg for x in a.posonlyargs + a.args]
    if names and names[0] in ("self", "cls"):
        names = names[1:]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        names.append("*")
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    ret = ""
    if fn.returns is not None:
        try:
            ret = " -> " + ast.unparse(fn.returns)
        except Exception:  # noqa: BLE001
            ret = ""
    return f"({', '.join(names)}){ret}"


def _base_name(node):
    try:
        text = ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "?"
    return text


def _class(node, path):
    doc = ast.get_docstring(node) or ""
    c = ClassInfo(node.name, path, node.lineno, [_base_name(b) for b in node.bases], doc.split("\n")[0])
    seen = set()
    fields = {}
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decos = {_base_name(d).split(".")[-1] for d in item.decorator_list}
            if "setter" in decos or "deleter" in decos:
                continue
            kind = "property" if decos & {"property", "cached_property"} else \
                ("static" if "staticmethod" in decos else ("classmethod" if "classmethod" in decos else "method"))
            c.members.append(Member(item.name, kind, item.lineno, "" if kind == "property" else _sig(item), c))
            seen.add(item.name)
            if item.name == "__init__" or item.name.startswith("setup") or item.name == "setupUi":
                for sub in ast.walk(item):
                    targets = sub.targets if isinstance(sub, ast.Assign) else \
                        [sub.target] if isinstance(sub, (ast.AnnAssign, ast.AugAssign)) else []
                    for t in targets:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                            fields.setdefault(t.attr, t.lineno)
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name):
                    kind = "signal" if isinstance(item.value, ast.Call) and \
                        _base_name(item.value.func).split(".")[-1] in ("Signal", "pyqtSignal") else "field"
                    c.members.append(Member(t.id, kind, item.lineno, "", c))
                    seen.add(t.id)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            c.members.append(Member(item.target.id, "field", item.lineno, "", c))
            seen.add(item.target.id)
        elif isinstance(item, ast.ClassDef):
            c.nested.append(_class(item, path))
    for name, line in fields.items():
        if name not in seen:
            c.members.append(Member(name, "field", line, "", c))
    return c


def parse_file(path):
    """(classes, functions, variables, error)"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), path)
    except SyntaxError as e:
        return [], [], [], f"line {e.lineno}: {e.msg}"
    except (OSError, ValueError) as e:
        return [], [], [], str(e)
    classes, funcs, variables = [], [], []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append(_class(node, path))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(Member(node.name, "function", node.lineno, _sig(node)))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():          # module constants
                    variables.append(Member(t.id, "constant", node.lineno))
    return classes, funcs, variables, None


def python_files(root, skip_roots=()):
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                         and os.path.join(dirpath, d) not in skip_roots
                         )
        for f in sorted(files):
            if f.endswith(".py") and not f.startswith("ui_") and f not in SKIP_FILES:
                out.append(os.path.join(dirpath, f))
                if len(out) >= MAX_FILES:
                    return out
    return out


# ============================================================ the panel
ICON_FOR = {"method": icons.method, "static": icons.method, "classmethod": icons.method, "function": icons.function,
            "property": icons.prop, "field": icons.field, "constant": icons.field, "signal": icons.field}


class ClassView(QWidget):
    open_location = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.projects = []                 # [(name, root)]
        self.solution_root = None
        self._icons = {}
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search classes and members (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self, singleShot=True, interval=200)
        self._search_timer.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        b_refresh = QToolButton()
        b_refresh.setIcon(icons.refresh())
        b_refresh.setToolTip("Refresh")
        b_refresh.setAutoRaise(True)
        b_refresh.clicked.connect(self.refresh)
        self.b_sort = QToolButton()
        self.b_sort.setText("A-Z")
        self.b_sort.setCheckable(True)
        self.b_sort.setChecked(True)
        self.b_sort.setToolTip("Sort members alphabetically (off: in the order they're written)")
        self.b_sort.setAutoRaise(True)
        self.b_sort.toggled.connect(lambda _v: self.refresh())
        self.ck_inherited = QCheckBox("Inherited")
        self.ck_inherited.setToolTip("Show inherited members - from base classes in your projects (grayed)")
        self.ck_inherited.toggled.connect(lambda _v: self.refresh())
        self.ck_private = QCheckBox("_private")
        self.ck_private.setToolTip("Show members whose names start with _ (dunder methods like __init__ always show)")
        self.ck_private.setChecked(True)
        self.ck_private.toggled.connect(lambda _v: self.refresh())
        top = QHBoxLayout()
        top.setContentsMargins(4, 4, 4, 0)
        top.addWidget(self.search, 1)
        top.addWidget(self.b_sort)
        top.addWidget(b_refresh)
        opts = QHBoxLayout()
        opts.setContentsMargins(6, 0, 4, 0)
        opts.addWidget(self.ck_inherited)
        opts.addWidget(self.ck_private)
        opts.addStretch()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemActivated.connect(self._activate)
        self.tree.itemDoubleClicked.connect(self._activate)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addLayout(top)
        lay.addLayout(opts)
        lay.addWidget(self.tree, 1)
        self.classes = {}

    def _icon(self, name):
        if name not in self._icons:
            self._icons[name] = getattr(icons, name)() if isinstance(name, str) else name()
        return self._icons[name]

    def set_projects(self, projects, solution_root=None):
        self.projects = list(projects)
        self.solution_root = solution_root
        if self.isVisible():
            self.refresh()
        else:
            self._stale = True

    def showEvent(self, e):
        super().showEvent(e)
        if getattr(self, "_stale", True):
            QTimer.singleShot(0, self.refresh)

    def file_saved(self, path):
        if path and path.endswith(".py") and self.isVisible():
            self._refresh_soon()
        elif path and path.endswith(".py"):
            self._stale = True

    def _refresh_soon(self):
        if not hasattr(self, "_rt"):
            self._rt = QTimer(self, singleShot=True, interval=400)
            self._rt.timeout.connect(self.refresh)
        self._rt.start()

    # ------------------------------------------------------------ build
    def refresh(self):
        self._stale = False
        expanded = self._expanded_keys()
        self.tree.clear()
        roots = [r for _n, r in self.projects]
        parsed = {}
        self.classes = {}
        for name, root in self.projects:
            files = python_files(root, skip_roots=[r for r in roots if r != root and r.startswith(root + os.sep)]
                                 + [d for d in glob.glob(os.path.join(root, "*", "")) if
                                    glob.glob(os.path.join(d, "*.pyproj")) and os.path.abspath(d) not in roots])
            parsed[root] = [(p, parse_file(p)) for p in files]
            for p, (classes, _f, _v, _e) in parsed[root]:
                for c in classes:
                    self.classes.setdefault(c.name, c)
        multi = len(self.projects) > 1
        for name, root in self.projects:
            parent = self.tree.invisibleRootItem()
            if multi:
                pi = QTreeWidgetItem([name])
                pi.setIcon(0, self._icon("project"))
                pi.setData(0, Qt.UserRole, ("project", root))
                f = pi.font(0)
                f.setBold(True)
                pi.setFont(0, f)
                self.tree.addTopLevelItem(pi)
                parent = pi
            for path, (classes, funcs, variables, err) in parsed[root]:
                if not (classes or funcs or variables or err):
                    continue
                rel = os.path.relpath(path, root)
                mi = QTreeWidgetItem([rel])
                mi.setIcon(0, self._icon("module"))
                mi.setData(0, Qt.UserRole, ("loc", path, 1))
                mi.setToolTip(0, path)
                parent.addChild(mi)
                if err:
                    e = QTreeWidgetItem([f"(can't read: {err})"])
                    e.setForeground(0, QColor("#e06c75"))
                    e.setData(0, Qt.UserRole, ("loc", path, 1))
                    mi.addChild(e)
                for c in classes:
                    self._add_class(mi, c)
                for m in self._order(funcs + variables):
                    self._add_member(mi, m, path)
        self._restore_expanded(expanded)
        self._apply_filter()

    def _order(self, members):
        if self.b_sort.isChecked():
            return sorted(members, key=lambda m: (m.name.startswith("_") and not m.name.startswith("__"),
                                                  m.name.lower()))
        return sorted(members, key=lambda m: m.line)

    def _visible(self, name):
        return self.ck_private.isChecked() or not name.startswith("_") or \
            (name.startswith("__") and name.endswith("__"))

    def _add_class(self, parent, c):
        bases = ", ".join(b for b in c.bases if b != "object")
        ci = QTreeWidgetItem([c.name + (f"  ({bases})" if bases else "")])
        ci.setIcon(0, self._icon("cls"))
        ci.setData(0, Qt.UserRole, ("loc", c.path, c.line))
        ci.setData(0, Qt.UserRole + 1, c.name)
        ci.setToolTip(0, f"class {c.name}" + (f"({bases})" if bases else "") + (f"\n{c.doc}" if c.doc else ""))
        parent.addChild(ci)
        if bases:
            bi = QTreeWidgetItem(["Base Types"])
            bi.setForeground(0, QColor("#8a8a8a"))
            ci.addChild(bi)
            for b in c.bases:
                if b == "object":
                    continue
                known = self.classes.get(b.split(".")[-1])
                it = QTreeWidgetItem([b])
                it.setIcon(0, self._icon("cls"))
                if known is not None:
                    it.setData(0, Qt.UserRole, ("loc", known.path, known.line))
                    it.setToolTip(0, f"{known.path}:{known.line}")
                else:
                    it.setForeground(0, QColor("#8a8a8a"))
                    it.setToolTip(0, "Not in your projects (a library class, e.g. from PySide6 / PyQt5)")
                bi.addChild(it)
        for n in c.nested:
            self._add_class(ci, n)
        own = {m.name for m in c.members}
        members = [m for m in c.members if self._visible(m.name)]
        if self.ck_inherited.isChecked():
            for m in self._inherited(c):
                if m.name not in own and self._visible(m.name):
                    own.add(m.name)
                    members.append(m)
        for m in self._order(members):
            self._add_member(ci, m, m.owner.path if m.owner else c.path, inherited=m.owner is not c)

    def _inherited(self, c, seen=None):
        seen = seen or {c.name}
        out = []
        for b in c.bases:
            base = self.classes.get(b.split(".")[-1])
            if base is None or base.name in seen:
                continue
            seen.add(base.name)
            out += base.members
            out += self._inherited(base, seen)
        return out

    def _add_member(self, parent, m, path, inherited=False):
        label = m.name + (m.sig if m.kind in ("method", "static", "classmethod", "function") else "")
        it = QTreeWidgetItem([label])
        it.setIcon(0, self._icon(ICON_FOR.get(m.kind, icons.field)))
        it.setData(0, Qt.UserRole, ("loc", path, m.line))
        kind = {"static": "static method", "classmethod": "class method"}.get(m.kind, m.kind)
        tip = f"{kind}  {m.name}{m.sig}"
        if inherited and m.owner is not None:
            tip += f"\ninherited from {m.owner.name}"
            it.setForeground(0, QColor("#8a8a8a"))
            f = QFont(it.font(0))
            f.setItalic(True)
            it.setFont(0, f)
        elif m.name.startswith("_") and not (m.name.startswith("__") and m.name.endswith("__")):
            it.setForeground(0, QColor("#9a9a9a"))
        it.setToolTip(0, tip)
        parent.addChild(it)

    # ------------------------------------------------------------ keep expansion across refreshes
    def _key(self, it):
        parts = []
        while it is not None:
            parts.append(it.text(0))
            it = it.parent()
        return "/".join(reversed(parts))

    def _expanded_keys(self):
        out = set()

        def walk(it):
            for i in range(it.childCount()):
                ch = it.child(i)
                if ch.isExpanded():
                    out.add(self._key(ch))
                walk(ch)
        walk(self.tree.invisibleRootItem())
        return out

    def _restore_expanded(self, keys):
        first = not keys

        def walk(it, depth):
            for i in range(it.childCount()):
                ch = it.child(i)
                if self._key(ch) in keys or (first and ch.data(0, Qt.UserRole) and
                                             ch.data(0, Qt.UserRole)[0] == "project"):
                    ch.setExpanded(True)
                walk(ch, depth + 1)
        walk(self.tree.invisibleRootItem(), 0)

    # ------------------------------------------------------------ search
    def _apply_filter(self):
        text = self.search.text().strip().lower()

        def walk(it):
            any_shown = False
            for i in range(it.childCount()):
                ch = it.child(i)
                name = ch.text(0).lower()
                child_match = walk(ch)
                match = not text or text in name.split("(")[0] or child_match
                ch.setHidden(not match)
                if text and child_match:
                    ch.setExpanded(True)
                any_shown |= match and (bool(text) and (text in name.split("(")[0] or child_match))
            return any_shown
        walk(self.tree.invisibleRootItem())

    def _activate(self, item, _col=0):
        d = item.data(0, Qt.UserRole)
        if d and d[0] == "loc":
            self.open_location.emit(d[1], d[2])

    def find_class(self, name):
        """The tree item of a class (tests / Go To)."""
        found = self.tree.findItems(name, Qt.MatchStartsWith | Qt.MatchRecursive)
        return next((f for f in found if f.data(0, Qt.UserRole + 1) == name), None)
