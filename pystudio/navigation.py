"""Visual Studio-style navigation for PyStudio:
- Navigate Backward / Forward (Ctrl+- / Ctrl+Shift+-, mouse back/forward buttons)
- Bookmarks (Ctrl+K Ctrl+K toggle, Ctrl+K Ctrl+N next, Ctrl+K Ctrl+P previous, Ctrl+K Ctrl+L clear,
  Ctrl+K Ctrl+W Bookmark Window)
- Go To All (Ctrl+T / Ctrl+,): files, classes, functions, methods, forms' controls; "f " files only,
  "t " types, "m " members, ":42" a line in the current file
- CodeLens-style reference counts after each def/class ("3 references" - click for the exact list)

NavigationMixin is mixed into MainWindow."""
import ast
import json
import os
import re
from collections import Counter

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import theme
from .findinfiles import project_files
from .language import is_generated

MAX_HISTORY = 100
JUMP_LINES = 10          # moves of at least this many lines are remembered (like VS)
IDENT = re.compile(r"[A-Za-z_]\w*")
DEFN = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.M)


def codelens_enabled():
    return QSettings().value("codeLens", True, type=bool)


# ============================================================ reference counts
class ReferenceIndex:
    """Fast, approximate usage counts per name across the project's .py files (text-based, so a name
    in a comment or string counts too). Clicking the count runs the exact Find All References."""

    def __init__(self):
        self.per_file = {}           # path -> (uses Counter, defs Counter)
        self.total = Counter()
        self.defs = Counter()

    def clear(self):
        self.per_file.clear()
        self.total.clear()
        self.defs.clear()

    def update(self, path, text):
        path = os.path.abspath(path)
        old = self.per_file.pop(path, None)
        if old:
            self.total.subtract(old[0])
            self.defs.subtract(old[1])
        if text is None:
            return
        uses, defs = Counter(IDENT.findall(text)), Counter(DEFN.findall(text))
        self.per_file[path] = (uses, defs)
        self.total.update(uses)
        self.defs.update(defs)

    def build(self, root):
        self.clear()
        if not root:
            return
        for p in project_files(root, "*.py"):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    self.update(p, fh.read())
            except OSError:
                pass

    def count(self, name):
        if name not in self.total:
            return None
        return max(0, self.total[name] - max(1, self.defs[name]))


# ============================================================ Go To All
KIND_ICON = {"file": "📄", "class": "🔶", "function": "🟪", "method": "🟣", "form": "🪟", "control": "▫"}


def python_symbols(path, text=None):
    """[(kind, name, container, line)] for classes, functions and methods in one file."""
    try:
        if text is None:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return []
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.append(("class", node.name, "", node.lineno))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(("method", sub.name, node.name, sub.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(("function", node.name, "", node.lineno))
    return out


def ui_controls(path):
    """[(kind, name, container, 0)] for the controls on a form (.ui)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    names = re.findall(r'<widget class="(\w+)" name="(\w+)"', text)
    stem = os.path.basename(path)
    return [("control", n, stem, 0) for cls, n in names[1:]
            if not n.startswith(("centralwidget", "qt_", "menubar", "statusbar"))]


def fuzzy_score(query, text):
    """Higher is better; None = no match. Prefix > word start > substring > in-order letters."""
    q, t = query.lower(), text.lower()
    if not q:
        return 1
    if t == q:
        return 1000
    if t.startswith(q):
        return 800 - len(t)
    i = t.find(q)
    if i >= 0:
        word_start = i == 0 or not t[i - 1].isalnum() or (text[i].isupper() and text[i - 1].islower())
        return (600 if word_start else 400) - i - len(t) // 4
    # camelCase / snake_case initials and letters in order
    pos = 0
    for ch in q:
        pos = t.find(ch, pos)
        if pos < 0:
            return None
        pos += 1
    return 100 - len(t)


class GoToAllDialog(QDialog):
    """Ctrl+T: type part of a file, class, function, method or control name and press Enter."""
    picked = Signal(str, int)          # path, line (0 = just open)

    def __init__(self, parent, items, current_path=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.items = items             # [(kind, name, container, path, line)]
        self.current_path = current_path
        self.setObjectName("GoToAll")
        self.setStyleSheet(f"""
            QDialog#GoToAll {{ background: {theme.c('#252526')}; border: 1px solid {theme.c('#007acc')}; }}
            QLineEdit {{ background: {theme.c('#3c3c3c')}; color: {theme.c('#d4d4d4')};
                         border: 1px solid {theme.c('#007acc')}; padding: 4px; font-size: 11pt; }}
            QListWidget {{ background: {theme.c('#252526')}; color: {theme.c('#d4d4d4')}; border: none; }}
            QListWidget::item:selected {{ background: {theme.c('#094771')}; }}
            QLabel {{ color: {theme.c('#9d9d9d')}; }}""")
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Search files, types, members and controls   "
                                     "(f file  t type  m member  :line)")
        self.list = QListWidget()
        self.hint = QLabel("")
        v.addWidget(self.edit)
        v.addWidget(self.list)
        v.addWidget(self.hint)
        self.edit.textChanged.connect(self._filter)
        self.edit.returnPressed.connect(self._accept_current)
        self.list.itemActivated.connect(self._accept_item)
        self.edit.installEventFilter(self)
        self.resize(640, 420)
        self._filter("")

    def eventFilter(self, obj, e):
        if obj is self.edit and e.type() == QEvent.KeyPress and e.key() in (Qt.Key_Down, Qt.Key_Up,
                                                                             Qt.Key_PageDown, Qt.Key_PageUp):
            self.list.keyPressEvent(e)
            return True
        return super().eventFilter(obj, e)

    def _filter(self, text):
        self.list.clear()
        text = text.strip()
        m = re.fullmatch(r":\s*(\d+)", text)
        if m:
            it = QListWidgetItem(f"Go to line {m.group(1)}"
                                 + (f" in {os.path.basename(self.current_path)}" if self.current_path else ""))
            it.setData(Qt.UserRole, (self.current_path or "", int(m.group(1))))
            self.list.addItem(it)
            self.list.setCurrentRow(0)
            return
        kinds = None
        pm = re.match(r"([ftm])\s+(.*)", text)
        if pm:
            kinds = {"f": {"file", "form"}, "t": {"class", "form"}, "m": {"method", "function", "control"}}[pm.group(1)]
            text = pm.group(2)
        scored = []
        for kind, name, container, path, line in self.items:
            if kinds and kind not in kinds:
                continue
            sc = fuzzy_score(text, name)
            if sc is None:
                continue
            scored.append((-sc, name.lower(), kind, name, container, path, line))
        scored.sort()
        for _s, _n, kind, name, container, path, line in scored[:200]:
            where = f"{container}  -  " if container else ""
            rel = os.path.basename(path)
            it = QListWidgetItem(f"{KIND_ICON.get(kind, '')}  {name}        {where}{rel}"
                                 + (f" ({line})" if line else ""))
            it.setData(Qt.UserRole, (path, line))
            it.setToolTip(path)
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.hint.setText(f"{len(scored)} result(s)" + (" - showing 200" if len(scored) > 200 else ""))

    def _accept_current(self):
        it = self.list.currentItem()
        if it is not None:
            self._accept_item(it)

    def _accept_item(self, it):
        path, line = it.data(Qt.UserRole)
        self.close()
        if path:
            self.picked.emit(path, line)


# ============================================================ Bookmark Window
class BookmarkWindow(QWidget):
    open_location = Signal(str, int)
    request = Signal(str)              # "prev" | "next" | "delete" | "clear"

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        for text, key, tip in (("◀ Previous", "prev", "Previous Bookmark (Ctrl+K, Ctrl+P)"),
                               ("Next ▶", "next", "Next Bookmark (Ctrl+K, Ctrl+N)"),
                               ("Delete", "delete", "Delete the selected bookmark"),
                               ("Clear All", "clear", "Clear All Bookmarks (Ctrl+K, Ctrl+L)")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, k=key: self.request.emit(k))
            bar.addWidget(b)
        bar.addStretch()
        v.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Line", "Code"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 60)
        self.tree.itemActivated.connect(lambda it, _c: self.open_location.emit(*it.data(0, Qt.UserRole)))
        v.addWidget(self.tree)

    def set_bookmarks(self, marks, root=None, line_text=None):
        """marks: [(path, line)]"""
        self.tree.clear()
        for path, line in marks:
            rel = os.path.relpath(path, root) if root and path.startswith(root + os.sep) else path
            code = line_text(path, line) if line_text else ""
            it = QTreeWidgetItem([rel, str(line), code.strip()])
            it.setData(0, Qt.UserRole, (path, line))
            self.tree.addTopLevelItem(it)

    def selected(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.UserRole) if it else None


class _MouseNav(QObject):
    """Mouse back/forward buttons navigate, like in Visual Studio."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def eventFilter(self, obj, e):
        if e.type() == QEvent.MouseButtonPress and e.button() in (Qt.BackButton, Qt.ForwardButton):
            w = obj if isinstance(obj, QWidget) else None
            if w is not None and w.window() is self.win:
                (self.win.navigate_back if e.button() == Qt.BackButton else self.win.navigate_forward)()
                return True
        return False


def _arrow_icon(back):
    """A VS-style navigate arrow in the theme's text color (the stock icons vanish on dark themes)."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
    icon = QIcon()
    for mode, color in ((QIcon.Normal, theme.c("#c5c5c5")), (QIcon.Disabled, theme.c("#5a5a5a"))):
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(color), 3.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        sx = -1 if back else 1
        cx = 16
        p.drawLine(QPointF(cx - 9 * sx, 16), QPointF(cx + 9 * sx, 16))
        p.drawLine(QPointF(cx + 9 * sx, 16), QPointF(cx + 2 * sx, 9))
        p.drawLine(QPointF(cx + 9 * sx, 16), QPointF(cx + 2 * sx, 23))
        p.end()
        icon.addPixmap(pm, mode)
    return icon


# ============================================================ MainWindow mixin
class NavigationMixin:
    def _nav_init(self):
        from PySide6.QtWidgets import QApplication, QDockWidget
        self._nav_back, self._nav_fwd = [], []
        self._nav_cur = None
        self._nav_moving = False
        try:
            self.bookmarks = json.loads(self.settings.value("bookmarks", "{}") or "{}")
        except ValueError:
            self.bookmarks = {}
        self.ref_index = ReferenceIndex()
        self._ref_timer = QTimer(self)
        self._ref_timer.setSingleShot(True)
        self._ref_timer.setInterval(900)
        self._ref_timer.timeout.connect(self._refresh_current_refs)
        self.bookmark_window = BookmarkWindow()
        self.bookmark_window.open_location.connect(self.open_file_at)
        self.bookmark_window.request.connect(self._bookmark_request)
        self.bookmarks_dock = QDockWidget("Bookmarks", self)
        self.bookmarks_dock.setObjectName("Bookmarks")
        self.bookmarks_dock.setWidget(self.bookmark_window)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.bookmarks_dock)
        self.tabifyDockWidget(self.tests_dock, self.bookmarks_dock)
        self.bookmarks_dock.hide()
        self._mouse_nav = _MouseNav(self)
        QApplication.instance().installEventFilter(self._mouse_nav)

    def _nav_actions(self):
        self.a_nav_back = self._act("Navigate &Backward", self.navigate_back)
        self.a_nav_back.setShortcuts([QKeySequence("Ctrl+-")])
        self.a_nav_back.setIcon(_arrow_icon(back=True))
        self.a_nav_fwd = self._act("Navigate &Forward", self.navigate_forward)
        self.a_nav_fwd.setShortcuts([QKeySequence("Ctrl+Shift+-"), QKeySequence("Ctrl+_"),
                                     QKeySequence("Ctrl+Shift+_")])
        self.a_nav_fwd.setIcon(_arrow_icon(back=False))
        self.a_nav_back.setEnabled(False)
        self.a_nav_fwd.setEnabled(False)
        self.a_goto_all = self._act("Go To &All...", self.go_to_all)
        self.a_goto_all.setShortcuts([QKeySequence("Ctrl+T"), QKeySequence("Ctrl+,")])
        self.a_bm_toggle = self._act("&Toggle Bookmark", self.toggle_bookmark, "Ctrl+K, Ctrl+K")
        self.a_bm_next = self._act("&Next Bookmark", lambda: self.next_bookmark(+1), "Ctrl+K, Ctrl+N")
        self.a_bm_prev = self._act("&Previous Bookmark", lambda: self.next_bookmark(-1), "Ctrl+K, Ctrl+P")
        self.a_bm_clear = self._act("&Clear All Bookmarks", self.clear_bookmarks, "Ctrl+K, Ctrl+L")
        self.a_bm_window = self._act("Bookmark &Window", self.show_bookmark_window, "Ctrl+K, Ctrl+W")

    # ---------------------------------------------------------- history
    def _nav_position(self):
        doc = self.current_doc()
        path = getattr(doc, "path", None)
        if not path:
            return None
        if hasattr(doc, "textCursor"):
            return (os.path.abspath(path), doc.textCursor().blockNumber() + 1)
        return (os.path.abspath(path), 0)

    def _nav_track(self):
        pos = self._nav_position()
        if pos is None:
            return
        cur = self._nav_cur
        if cur is not None and pos != cur and not self._nav_moving:
            if pos[0] != cur[0] or abs(pos[1] - cur[1]) >= JUMP_LINES:
                if not self._nav_back or self._nav_back[-1] != cur:
                    self._nav_back.append(cur)
                    del self._nav_back[:-MAX_HISTORY]
                self._nav_fwd.clear()
        self._nav_cur = pos
        self._nav_update_actions()

    def _nav_update_actions(self):
        if hasattr(self, "a_nav_back"):
            self.a_nav_back.setEnabled(bool(self._nav_back))
            self.a_nav_fwd.setEnabled(bool(self._nav_fwd))

    def _nav_go(self, target):
        path, line = target
        self._nav_moving = True
        try:
            if line:
                self.open_file_at(path, line)
            else:
                self.open_file(path)
        finally:
            self._nav_moving = False
        self._nav_cur = self._nav_position() or target
        self._nav_update_actions()

    def navigate_back(self):
        while self._nav_back:
            target = self._nav_back.pop()
            if os.path.exists(target[0]) and target != self._nav_cur:
                if self._nav_cur:
                    self._nav_fwd.append(self._nav_cur)
                self._nav_go(target)
                return
        self._nav_update_actions()

    def navigate_forward(self):
        while self._nav_fwd:
            target = self._nav_fwd.pop()
            if os.path.exists(target[0]) and target != self._nav_cur:
                if self._nav_cur:
                    self._nav_back.append(self._nav_cur)
                self._nav_go(target)
                return
        self._nav_update_actions()

    # ---------------------------------------------------------- bookmarks
    def _nav_attach_editor(self, ed):
        """Called for every CodeEditor MainWindow opens."""
        key = os.path.abspath(ed.path) if ed.path else None
        if key and key in self.bookmarks:
            ed.set_bookmarks(self.bookmarks[key])
        ed.bookmarks_changed.connect(self._on_editor_bookmarks)
        ed.lens_clicked.connect(lambda line, col, e=ed: self._lens_clicked(e, line, col))
        ed.codelens = self.ref_index.count if codelens_enabled() else None
        ed.textChanged.connect(self._ref_timer.start)

    def _sync_bookmarks(self):
        from .editor import CodeEditor
        for ed in self.editors():
            if isinstance(ed, CodeEditor) and ed.path:
                key = os.path.abspath(ed.path)
                marks = ed.bookmarks()
                if marks:
                    self.bookmarks[key] = marks
                else:
                    self.bookmarks.pop(key, None)
        self.settings.setValue("bookmarks", json.dumps(self.bookmarks))

    def _on_editor_bookmarks(self, _path=""):
        self._sync_bookmarks()
        self._refresh_bookmark_window()

    def _all_bookmarks(self):
        return sorted((p, ln) for p, lines in self.bookmarks.items() if os.path.exists(p) for ln in lines)

    def _line_text(self, path, line):
        for ed in self.editors():
            if getattr(ed, "path", None) and os.path.abspath(ed.path) == path and hasattr(ed, "document"):
                return ed.document().findBlockByNumber(line - 1).text()
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for i, text in enumerate(fh, 1):
                    if i == line:
                        return text
        except OSError:
            pass
        return ""

    def _refresh_bookmark_window(self):
        self.bookmark_window.set_bookmarks(self._all_bookmarks(), self.solution_root or self.project_root,
                                           self._line_text)

    def toggle_bookmark(self):
        ed = self.current_editor()
        if ed is None:
            self.statusBar().showMessage("Bookmarks go on lines of code - open a code file.", 3000)
            return
        ed.toggle_bookmark()

    def next_bookmark(self, step=1):
        self._sync_bookmarks()
        marks = self._all_bookmarks()
        if not marks:
            self.statusBar().showMessage("There are no bookmarks (Ctrl+K, Ctrl+K adds one).", 3000)
            return
        cur = self._nav_position() or ("", 0)
        if step > 0:
            target = next((m for m in marks if m > cur), marks[0])
        else:
            target = next((m for m in reversed(marks) if m < cur), marks[-1])
        self.open_file_at(*target)

    def clear_bookmarks(self):
        from .editor import CodeEditor
        for ed in self.editors():
            if isinstance(ed, CodeEditor):
                ed.blockSignals(True)
                ed.clear_bookmarks()
                ed.blockSignals(False)
        self.bookmarks = {}
        self.settings.setValue("bookmarks", "{}")
        self._refresh_bookmark_window()

    def show_bookmark_window(self):
        self._sync_bookmarks()
        self._refresh_bookmark_window()
        self.bookmarks_dock.show()
        self.bookmarks_dock.raise_()

    def _bookmark_request(self, what):
        if what == "prev":
            self.next_bookmark(-1)
        elif what == "next":
            self.next_bookmark(+1)
        elif what == "clear":
            self.clear_bookmarks()
        elif what == "delete":
            sel = self.bookmark_window.selected()
            if not sel:
                return
            path, line = sel
            from .editor import CodeEditor
            ed = next((e for e in self.editors() if isinstance(e, CodeEditor) and e.path
                       and os.path.abspath(e.path) == path), None)
            if ed is not None:
                ed.toggle_bookmark(ed.document().findBlockByNumber(line - 1))
            else:
                lines = [ln for ln in self.bookmarks.get(path, []) if ln != line]
                if lines:
                    self.bookmarks[path] = lines
                else:
                    self.bookmarks.pop(path, None)
                self.settings.setValue("bookmarks", json.dumps(self.bookmarks))
                self._refresh_bookmark_window()

    # ---------------------------------------------------------- Go To All
    def _goto_items(self):
        root = self.solution_root or self.project_root
        items = []
        open_text = {}
        from .editor import CodeEditor
        for ed in self.editors():
            if isinstance(ed, CodeEditor) and ed.path:
                open_text[os.path.abspath(ed.path)] = ed.toPlainText()
        if root:
            cache = getattr(self, "_sym_cache", {})
            new_cache = {}
            for p in project_files(root, "*"):
                p = os.path.abspath(p)
                items.append(("form" if p.endswith(".ui") else "file", os.path.basename(p),
                              os.path.dirname(os.path.relpath(p, root)), p, 0))
                if p.endswith(".py") and not is_generated(p):
                    if p in open_text:
                        syms = python_symbols(p, open_text[p])
                    else:
                        try:
                            mt = os.path.getmtime(p)
                        except OSError:
                            continue
                        hit = cache.get(p)
                        syms = hit[1] if hit and hit[0] == mt else python_symbols(p)
                        new_cache[p] = (mt, syms)
                    items += [(k, n, c, p, ln) for k, n, c, ln in syms]
                elif p.endswith(".ui"):
                    items += [(k, n, c, p, ln) for k, n, c, ln in ui_controls(p)]
            self._sym_cache = new_cache
        else:
            for p, text in open_text.items():
                items.append(("file", os.path.basename(p), "", p, 0))
                items += [(k, n, c, p, ln) for k, n, c, ln in python_symbols(p, text)]
        return items

    def go_to_all(self):
        doc = self.current_doc()
        cur = os.path.abspath(doc.path) if getattr(doc, "path", None) and hasattr(doc, "textCursor") else None
        dlg = GoToAllDialog(self, self._goto_items(), cur)
        dlg.picked.connect(lambda path, line: self.open_file_at(path, line) if line else self.open_file(path))
        top_left = self.tabs.mapToGlobal(self.tabs.rect().topLeft())
        w = min(700, max(420, self.tabs.width() - 40))
        dlg.resize(w, 440)
        dlg.move(top_left.x() + (self.tabs.width() - w) // 2, top_left.y() + 30)
        dlg.show()
        dlg.edit.setFocus()
        self._goto_dialog = dlg
        return dlg

    # ---------------------------------------------------------- CodeLens
    def rebuild_reference_index(self):
        self.ref_index.build(self.solution_root or self.project_root)
        self._repaint_editors()

    def _refresh_current_refs(self):
        ed = self.current_editor()
        if ed is not None and ed.path and ed.path.endswith(".py") and not is_generated(ed.path):
            self.ref_index.update(ed.path, ed.toPlainText())
            ed.viewport().update()

    def _repaint_editors(self):
        from .editor import CodeEditor
        on = codelens_enabled()
        for ed in self.editors():
            if isinstance(ed, CodeEditor):
                ed.codelens = self.ref_index.count if on else None
                ed.viewport().update()

    def _lens_clicked(self, ed, line, col):
        ed.go_to_line(line, col + 1)
        self.find_references()
