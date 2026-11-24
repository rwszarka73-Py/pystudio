"""VS 2022-style Start Window and the recent-projects list."""
import datetime
import glob
import json
import os

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QVBoxLayout, QWidget)

from . import __version__


# ============================================================ recent store
def too_big_to_open(path):
    """The whole disk, a top-level system folder or the home folder itself: opening one as a
    project makes Solution Explorer, the Error List and IntelliSense read every file in it."""
    try:
        p = os.path.realpath(path) if path else ""
    except (OSError, ValueError):
        return True
    home = os.path.realpath(os.path.expanduser("~"))
    return (not p or p == os.sep or os.path.dirname(p) == os.sep or p == home
            or p == os.path.dirname(home))


def load_recent():
    """[{path, name, opened, pinned}] newest first, pinned on top; drops missing folders."""
    s = QSettings()
    raw = s.value("recent", "")
    items = []
    if raw:
        try:
            items = json.loads(raw)
        except ValueError:
            items = []
    else:   # migrate the old plain list
        for p in s.value("recentProjects", []) or []:
            items.append({"path": p, "name": os.path.basename(p), "opened": "", "pinned": False})
    items = [i for i in items if os.path.isdir(i.get("path", "")) and not too_big_to_open(i.get("path", ""))]
    items.sort(key=lambda i: (not i.get("pinned"), ""), reverse=False)
    pinned = [i for i in items if i.get("pinned")]
    rest = sorted([i for i in items if not i.get("pinned")], key=lambda i: i.get("opened", ""), reverse=True)
    return pinned + rest


def _save(items):
    QSettings().setValue("recent", json.dumps(items))


def add_recent(path, name=None):
    if too_big_to_open(path):
        return
    path = os.path.abspath(path)
    items = [i for i in load_recent() if i["path"] != path]
    old = next((i for i in load_recent() if i["path"] == path), {})
    items.insert(0, {"path": path, "name": name or os.path.basename(path) or path,
                     "opened": datetime.datetime.now().isoformat(timespec="minutes"),
                     "pinned": old.get("pinned", False)})
    _save(items[:30])


def remove_recent(path):
    _save([i for i in load_recent() if i["path"] != path])


def toggle_pin(path):
    items = load_recent()
    for i in items:
        if i["path"] == path:
            i["pinned"] = not i.get("pinned")
    _save(items)


def _when(iso):
    if not iso:
        return ""
    try:
        t = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return ""
    now = datetime.datetime.now()
    if t.date() == now.date():
        return "Today " + t.strftime("%-I:%M %p")
    if (now.date() - t.date()).days == 1:
        return "Yesterday"
    return t.strftime("%-m/%-d/%Y")


# ============================================================ dialog
class Tile(QPushButton):
    def __init__(self, icon, title, desc):
        super().__init__()
        self.setMinimumSize(QSize(360, 64))
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        ic = QLabel(icon)
        ic.setStyleSheet("font-size: 24px; color: #7cc0ff; background: transparent;")
        ic.setFixedWidth(40)
        text = QLabel(f"<span style='font-size:14px; color:#ffffff;'><b>{title}</b></span><br>"
                      f"<span style='color:#a8a8a8;'>{desc}</span>")
        text.setStyleSheet("background: transparent;")
        text.setWordWrap(True)
        lay.addWidget(ic)
        lay.addWidget(text, 1)
        for w in (ic, text):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet("QPushButton { background: #333337; border: 1px solid #3f3f46; text-align: left; }"
                           "QPushButton:hover { background: #3e3e42; border: 1px solid #007acc; }")


class StartWindow(QDialog):
    """Returns via .result_action: ("open", folder) | ("new", None) | ("none", None)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PyStudio")
        self.resize(1000, 620)
        self.result_action = ("none", None)
        self.setStyleSheet("QDialog { background: #252526; }")

        title = QLabel(f"<span style='font-size:26px; color:#ffffff;'>PyStudio</span>"
                       f"<span style='color:#888;'>&nbsp;&nbsp;{__version__}</span>")

        # ---- left: recent
        left = QVBoxLayout()
        left.addWidget(QLabel("<span style='font-size:16px; color:#ffffff;'>Open recent</span>"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search recent (Alt+S)")
        self.search.textChanged.connect(self._fill)
        left.addWidget(self.search)
        self.list = QListWidget()
        self.list.setStyleSheet("QListWidget { background: #252526; border: 0; }"
                                "QListWidget::item { padding: 6px; border-radius: 2px; }"
                                "QListWidget::item:selected { background: #094771; }"
                                "QListWidget::item:hover { background: #2a2d2e; }")
        self.list.itemActivated.connect(self._open_item)
        self.list.itemClicked.connect(self._open_item)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        left.addWidget(self.list, 1)
        self.empty = QLabel("No recent projects yet.\nCreate one on the right to get started.")
        self.empty.setStyleSheet("color: #888;")
        left.addWidget(self.empty)
        left.addStretch()      # keeps everything at the top when the list is hidden

        # ---- right: get started
        right = QVBoxLayout()
        right.addWidget(QLabel("<span style='font-size:16px; color:#ffffff;'>Get started</span>"))
        t_new = Tile("✚", "Create a new project", "Windows Forms App with Form1, saved in ~/repos")
        t_open = Tile("📂", "Open a project", "Open a PyStudio project or solution (.pyproj, .pysln)")
        t_folder = Tile("🗀", "Open a local folder", "Navigate and edit code in any folder")
        t_new.clicked.connect(lambda: self._done(("new", None)))
        t_open.clicked.connect(self._open_project)
        t_folder.clicked.connect(self._open_folder)
        for t in (t_new, t_open, t_folder):
            right.addWidget(t)
        right.addStretch()
        cont = QPushButton("Continue without code →")
        cont.setFlat(True)
        cont.setCursor(Qt.PointingHandCursor)
        cont.setStyleSheet("QPushButton { color: #3794ff; border: 0; text-align: right; }"
                           "QPushButton:hover { text-decoration: underline; }")
        cont.clicked.connect(lambda: self._done(("none", None)))
        right.addWidget(cont, 0, Qt.AlignRight)

        cols = QHBoxLayout()
        lw, rw = QWidget(), QWidget()
        lw.setLayout(left)
        rw.setLayout(right)
        cols.addWidget(lw, 3)
        cols.addSpacing(24)
        cols.addWidget(rw, 2)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 20, 28, 20)
        lay.addWidget(title)
        lay.addSpacing(10)
        lay.addLayout(cols, 1)
        self._fill()
        self.search.setFocus()

    def _fill(self):
        q = self.search.text().strip().lower()
        self.list.clear()
        items = [i for i in load_recent() if not q or q in i["name"].lower() or q in i["path"].lower()]
        pinned_header = unpinned_header = False
        for i in items:
            if i.get("pinned") and not pinned_header:
                self._header("Pinned")
                pinned_header = True
            if not i.get("pinned") and not unpinned_header:
                self._header("Recent")
                unpinned_header = True
            it = QListWidgetItem()
            it.setData(Qt.UserRole, i["path"])
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(6, 2, 6, 2)
            icon = "📌" if i.get("pinned") else "▣"
            lab = QLabel(f"<span style='color:#ffffff; font-size:13px;'>{icon}&nbsp; <b>{i['name']}</b></span><br>"
                         f"<span style='color:#9a9a9a;'>{i['path']}</span>")
            when = QLabel(_when(i.get("opened")))
            when.setStyleSheet("color: #9a9a9a;")
            h.addWidget(lab, 1)
            h.addWidget(when)
            it.setSizeHint(QSize(0, 48))
            self.list.addItem(it)
            self.list.setItemWidget(it, w)
        self.empty.setVisible(not items)
        self.list.setVisible(bool(items))

    def _header(self, text):
        it = QListWidgetItem(text)
        it.setFlags(Qt.NoItemFlags)
        it.setForeground(Qt.gray)
        self.list.addItem(it)

    def _open_item(self, item):
        path = item.data(Qt.UserRole)
        if path:
            self._done(("open", path))

    def _menu(self, pos):
        item = self.list.itemAt(pos)
        path = item.data(Qt.UserRole) if item else None
        if not path:
            return
        m = QMenu(self)
        pinned = any(i["path"] == path and i.get("pinned") for i in load_recent())
        m.addAction("Unpin" if pinned else "Pin to top", lambda: (toggle_pin(path), self._fill()))
        m.addAction("Remove from list", lambda: (remove_recent(path), self._fill()))
        m.exec(self.list.viewport().mapToGlobal(pos))

    def _open_project(self):
        start = QSettings().value("projectsDir", os.path.expanduser("~/repos"))
        path, _ = QFileDialog.getOpenFileName(self, "Open Project", start,
                                              "PyStudio projects (*.pyproj);;All files (*)")
        if path:
            self._done(("open", os.path.dirname(path)))

    def _open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Open Folder", os.path.expanduser("~"))
        if path:
            self._done(("open", path))

    def _done(self, action):
        self.result_action = action
        self.accept()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_S and e.modifiers() & Qt.AltModifier:
            self.search.setFocus()
            return
        super().keyPressEvent(e)
