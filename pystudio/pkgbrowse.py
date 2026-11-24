"""Package Manager Browse and Updates tabs (VS: NuGet Browse / Updates)."""
import json
import re
import webbrowser

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSplitter,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import pypi


class _Relay(QObject):
    done = Signal(int, object)     # request id, result (dict | None | Exception)


class _Lookup(QRunnable):
    def __init__(self, relay, req, name):
        super().__init__()
        self.relay, self.req, self.name = relay, req, name

    def run(self):
        try:
            res = pypi.lookup(self.name)
        except OSError as e:
            res = e
        try:
            self.relay.done.emit(self.req, res)
        except RuntimeError:       # the dialog closed meanwhile
            pass


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


class BrowseTab(QWidget):
    """Search box, results (catalog + exact PyPI match), details with version and Install."""

    def __init__(self, page):
        super().__init__()
        self.page = page               # ProjectPropertiesDialog: _run, _python, _log, installed()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.relay = _Relay(self)
        self.relay.done.connect(self._looked_up)
        self._req = 0
        self._pending = {}             # request id -> ("search" | "details", name)
        self.cache = {}
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search packages: a name (requests) or what you need (excel, pdf, serial port...)")
        self.timer = QTimer(self, singleShot=True, interval=350)
        self.timer.timeout.connect(self._search)
        self.search.textChanged.connect(lambda _t: self.timer.start())
        self.b_web = QPushButton("Search pypi.org...")
        self.b_web.setToolTip("Open PyPI's search in your web browser (finds every package)")
        self.b_web.clicked.connect(lambda: webbrowser.open("https://pypi.org/search/?q=" +
                                                           self.search.text().strip().replace(" ", "+")))
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.b_web)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Package", "Category", "Description"])
        self.list.setRootIsDecorated(False)
        self.list.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.list.setColumnWidth(0, 190)
        self.list.setColumnWidth(1, 110)
        self.list.currentItemChanged.connect(lambda it, _p: self._select(it))
        self.list.itemDoubleClicked.connect(lambda _it, _c: self.install())
        # details
        self.d_name = QLabel()
        self.d_name.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.d_info = QLabel()
        self.d_info.setWordWrap(True)
        self.d_info.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.d_info.setOpenExternalLinks(True)
        self.d_info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.version = QComboBox()
        self.version.setToolTip("Version to install (latest first)")
        self.b_install = QPushButton("Install")
        self.b_install.clicked.connect(self.install)
        self.b_install.setEnabled(False)
        self._have = None
        self.version.currentTextChanged.connect(self._version_changed)
        vrow = QHBoxLayout()
        vrow.addWidget(QLabel("Version:"))
        vrow.addWidget(self.version, 1)
        vrow.addWidget(self.b_install)
        det = QWidget()
        dl = QVBoxLayout(det)
        dl.addWidget(self.d_name)
        dl.addLayout(vrow)
        dl.addWidget(self.d_info, 1)
        sp = QSplitter(Qt.Horizontal)
        sp.addWidget(self.list)
        sp.addWidget(det)
        sp.setSizes([520, 300])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addLayout(top)
        lay.addWidget(sp, 1)
        self._search()

    # ---------------------------------------------------------- search
    def _ask(self, kind, name):
        self._req += 1
        self._pending[self._req] = (kind, name)
        self.pool.start(_Lookup(self.relay, self._req, name))

    def _search(self):
        text = self.search.text().strip()
        self.list.clear()
        installed = self.page.installed()
        results = pypi.search_catalog(text)
        names = {_norm(r[0]) for r in results}
        for name, cat, desc in results:
            self._add(name, cat, desc, installed)
        if text and " " not in text and _norm(text) not in names:
            if _norm(text) in self.cache:
                self._add_exact(self.cache[_norm(text)], installed)
            else:
                self._ask("search", text)
        if not results and not text:
            pass
        if self.list.topLevelItemCount():
            self.list.setCurrentItem(self.list.topLevelItem(0))

    def _add(self, name, cat, desc, installed, top=False):
        have = installed.get(_norm(name))
        it = QTreeWidgetItem([name + (f"   ✓ {have}" if have else ""), cat, desc])
        it.setData(0, Qt.UserRole, name)
        it.setToolTip(2, desc)
        if top:
            self.list.insertTopLevelItem(0, it)
        else:
            self.list.addTopLevelItem(it)
        return it

    def _add_exact(self, info, installed):
        if info is None:
            return
        it = self._add(info["name"], "PyPI", info["summary"], installed, top=True)
        self.list.setCurrentItem(it)

    def _looked_up(self, req, res):
        kind, name = self._pending.pop(req, (None, None))
        if kind is None:
            return
        if isinstance(res, Exception):
            if kind == "details":
                self.d_info.setText(f"<span style='color:#f48771'>{res}</span>")
            return
        self.cache[_norm(name)] = res
        if kind == "search":
            if res is not None and _norm(self.search.text().strip()) == _norm(name):
                self._add_exact(res, self.page.installed())
        elif kind == "details":
            cur = self.list.currentItem()
            if cur is not None and _norm(cur.data(0, Qt.UserRole)) == _norm(name):
                self._show(res, name)

    # ---------------------------------------------------------- details
    def _select(self, it):
        self.version.clear()
        self.b_install.setEnabled(False)
        if it is None:
            self.d_name.clear()
            self.d_info.clear()
            return
        name = it.data(0, Qt.UserRole)
        self.d_name.setText(name)
        if _norm(name) in self.cache:
            self._show(self.cache[_norm(name)], name)
        else:
            self.d_info.setText(f"{it.text(2)}<br><br><span style='color:#9a9a9a'>Getting details from PyPI...</span>")
            self._ask("details", name)

    def _show(self, info, name):
        if info is None:
            self.d_info.setText(f"There's no package named <b>{name}</b> on PyPI.")
            return
        have = self.page.installed().get(_norm(info["name"]))
        self._have = None
        self.version.clear()
        self.version.addItems(info["versions"] or [info["version"]])
        if info["version"] in info["versions"]:
            self.version.setCurrentText(info["version"])      # latest stable, not a beta
        self._have = have
        self._version_changed(self.version.currentText())
        rows = [info["summary"], ""]
        if have:
            rows.append(f"<b>Installed:</b> {have}")
        rows.append(f"<b>Latest:</b> {info['version']}")
        if info.get("requires_python"):
            rows.append(f"<b>Needs Python:</b> {info['requires_python']}")
        if info.get("license") and len(info["license"]) < 60:
            rows.append(f"<b>License:</b> {info['license']}")
        rows.append(f"<a href='https://pypi.org/project/{info['name']}/'>pypi.org/project/{info['name']}</a>")
        if info.get("home") and "pypi.org" not in info["home"]:
            rows.append(f"<a href='{info['home']}'>{info['home']}</a>")
        self.d_info.setText("<br>".join(rows))

    def _version_changed(self, v):
        have = self._have
        self.b_install.setText("Installed" if have == v else ("Update" if have else "Install"))
        self.b_install.setEnabled(bool(v) and have != v and bool(self.page._python()))

    def install(self):
        it = self.list.currentItem()
        py = self.page._python()
        if it is None or not py:
            return
        name, ver = it.data(0, Qt.UserRole), self.version.currentText()
        spec = f"{name}=={ver}" if ver else name
        self.b_install.setEnabled(False)
        self.page._run(py, ["-m", "pip", "install", "--disable-pip-version-check", spec],
                       lambda code, _o: (self.page._log("Done." if code == 0 else f"pip exited with code {code}"),
                                         self.page.refresh_packages(), self._search()))


class UpdatesTab(QWidget):
    """Installed packages with a newer version on PyPI (pip list --outdated)."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.status = QLabel("Click Check for Updates.")
        self.status.setStyleSheet("color: #9a9a9a;")
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Package", "Installed", "Latest"])
        self.list.setRootIsDecorated(False)
        self.list.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.list.setColumnWidth(0, 220)
        b_check = QPushButton("Check for Updates")
        b_check.clicked.connect(self.check)
        self.b_sel = QPushButton("Update Selected")
        self.b_sel.clicked.connect(lambda: self.update([it.text(0) for it in self.list.selectedItems()]))
        self.b_all = QPushButton("Update All")
        self.b_all.clicked.connect(lambda: self.update([self.list.topLevelItem(i).text(0)
                                                        for i in range(self.list.topLevelItemCount())]))
        row = QHBoxLayout()
        row.addWidget(b_check)
        row.addStretch()
        row.addWidget(self.b_sel)
        row.addWidget(self.b_all)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addWidget(self.status)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)

    def check(self):
        py = self.page._python()
        if not py:
            return
        self.status.setText("Checking PyPI for newer versions...")
        self.list.clear()

        def done(code, out):
            try:
                rows = json.loads(out[out.index("["):out.rindex("]") + 1]) if "[" in out else []
            except ValueError:
                rows = []
            for r in sorted(rows, key=lambda r: r["name"].lower()):
                self.list.addTopLevelItem(QTreeWidgetItem([r["name"], r.get("version", ""), r.get("latest_version", "")]))
            self.status.setText(f"{len(rows)} update(s) available." if rows else
                                ("Everything is up to date." if code == 0 else "Couldn't check (internet needed)."))
        self.page._run(py, ["-m", "pip", "list", "--outdated", "--format", "json", "--disable-pip-version-check"],
                       done, quiet=True)

    def update(self, names):
        py = self.page._python()
        if not names or not py:
            return
        self.page._run(py, ["-m", "pip", "install", "--upgrade", "--disable-pip-version-check", *names],
                       lambda code, _o: (self.page._log("Done." if code == 0 else f"pip exited with code {code}"),
                                         self.page.refresh_packages(), self.check()))
