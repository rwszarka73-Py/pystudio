"""Test Explorer (VS: Test > Test Explorer, Ctrl+E, T) for pytest: find the project's tests,
run all / selected / failed, see pass/fail with the message and traceback, debug a test."""
import os
import re
import tempfile
import xml.etree.ElementTree as ET

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)
from . import theme

SHIM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pytest_shim.py")
ICONS = {"passed": ("✔", "#89d185"), "failed": ("✖", "#f14c4c"), "error": ("✖", "#f14c4c"),
         "skipped": ("⊘", "#d7ba7d"), "notrun": ("○", "#9a9a9a"), "running": ("◷", "#75beff")}
TEST_TEMPLATE = '''"""Tests{about} - run them in Test > Test Explorer (Ctrl+E, T).

Every function named test_... is a test. Inside it, assert what should be true:
    assert add(2, 3) == 5
A test passes when no assert fails and nothing raises an error.
"""
{imports}

def test_example():
    assert 1 + 1 == 2


def test_text():
    name = "PyStudio"
    assert name.lower() == "pystudio"
    assert len(name) == 8
'''


def parse_collect(text):
    """Node ids from 'pytest --collect-only -q' output."""
    ids = []
    for line in text.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "<", "ERROR", "FAILED")) and " " not in line.split("::")[0]:
            ids.append(line)
    return ids


def parse_junit(path, root):
    """{node_id: {"outcome", "time", "message", "details", "file", "line"}} from a junit xml file."""
    out = {}
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return out
    for tc in tree.iter("testcase"):
        f = (tc.get("file") or "").replace("\\", "/")
        classname, name = tc.get("classname", ""), tc.get("name", "")
        module = f[:-3].replace("/", ".") if f.endswith(".py") else ""
        cls = classname[len(module) + 1:] if module and classname.startswith(module + ".") else ""
        if not f and classname:          # no file attribute: guess from classname
            parts = classname.split(".")
            f = "/".join(parts) + ".py"
        node = f + "::" + (cls + "::" if cls else "") + name
        r = {"outcome": "passed", "time": float(tc.get("time") or 0), "message": "", "details": "",
             "file": os.path.join(root, f) if f else "", "line": int(tc.get("line") or 0) + 1 if tc.get("line") else 0}
        for tag, outcome in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            e = tc.find(tag)
            if e is not None:
                r["outcome"] = outcome
                r["message"] = e.get("message", "")
                r["details"] = (e.text or "").strip()
                break
        so = tc.find("system-out")
        if so is not None and (so.text or "").strip():
            r["details"] += "\n\n--- output ---\n" + so.text.strip()
        out[node] = r
    return out


def find_test_line(path, name):
    name = name.split("[")[0]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if re.match(rf"\s*(async\s+)?def {re.escape(name)}\(", line) or re.match(rf"\s*class {re.escape(name)}\b", line):
                    return i
    except OSError:
        pass
    return 1


class TestExplorer(QWidget):
    open_location = Signal(str, int)      # file, line
    debug_requested = Signal(list)        # node ids
    log = Signal(str, str)                # text, kind (Output panel)
    install_requested = Signal()
    coverage_ready = Signal(dict)         # {abs path: {"covered": [...], "missing": [...], "percent": float}}
    install_coverage_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = None
        self.python = None
        self.env = {}
        self.proc = None
        self.results = {}         # node id -> result dict
        self.nodes = []           # discovered node ids
        self._junit = None
        self._running = set()

        def tb(text, tip, slot):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(slot)
            return b
        self.b_all = tb("▶▶ Run All", "Run All Tests (Ctrl+R, A)", self.run_all)
        self.b_sel = tb("▶ Run", "Run the selected tests (Ctrl+R, T)", self.run_selected)
        self.b_failed = tb("✖ Run Failed", "Run the tests that failed last time", self.run_failed)
        self.b_debug = tb("🐞 Debug", "Debug the selected tests (stops at breakpoints)", self.debug_selected)
        self.b_cover = tb("◑ Coverage", "Analyze Code Coverage for All Tests: which lines the tests run "
                          "(green) and which they don't (red)", self.run_coverage)
        self.b_cancel = tb("■", "Cancel the test run", self.cancel)
        self.b_refresh = tb("⟳", "Find tests again", self.discover)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search tests")
        self.filter.textChanged.connect(self._apply_filter)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        for b in (self.b_all, self.b_sel, self.b_failed, self.b_debug, self.b_cover, self.b_cancel, self.b_refresh):
            bar.addWidget(b)
        bar.addWidget(self.filter, 1)
        self.summary = QLabel("")
        self.summary.setStyleSheet("color: #aaaaaa; padding: 2px 4px;")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Test", "Duration"])
        self.tree.setColumnWidth(0, 360)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.currentItemChanged.connect(lambda it, _p: self._details(it))
        self.tree.itemDoubleClicked.connect(self._open)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setStyleSheet("QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; border: 0; }")
        self.empty = QLabel()
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: #aaaaaa; padding: 8px;")
        self.b_install = QPushButton("Install pytest")
        self.b_install.clicked.connect(self.install_requested)
        self.b_install.hide()
        self.b_install_cov = QPushButton("Install coverage")
        self.b_install_cov.clicked.connect(self.install_coverage_requested)
        self.b_install_cov.hide()
        sp = QSplitter(Qt.Horizontal)
        sp.addWidget(self.tree)
        sp.addWidget(self.details)
        sp.setSizes([500, 400])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.summary)
        lay.addWidget(self.empty)
        lay.addWidget(self.b_install, 0, Qt.AlignLeft)
        lay.addWidget(self.b_install_cov, 0, Qt.AlignLeft)
        lay.addWidget(sp, 1)
        self._update_buttons()
        self._show_empty("Open a project to see its tests.")
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is not None:
            QApplication.instance().aboutToQuit.connect(self.shutdown)

    # ---------------------------------------------------------- project
    def set_project(self, root, python, env=None):
        self.root, self.python, self.env = root, python, dict(env or {})
        self.results, self.nodes = {}, []
        self.tree.clear()
        if root:
            self.discover()
        else:
            self._show_empty("Open a project to see its tests.")

    def _show_empty(self, text, install=False):
        self.empty.setText(text)
        self.empty.setVisible(bool(text))
        self.b_install.setVisible(install)

    def _update_buttons(self):
        busy = self.proc is not None
        for b in (self.b_all, self.b_sel, self.b_failed, self.b_debug, self.b_cover, self.b_refresh):
            b.setEnabled(not busy and bool(self.root))
        self.b_cancel.setEnabled(busy)

    # ---------------------------------------------------------- running pytest
    def _start(self, args, done, coverage_file=None):
        p = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONDONTWRITEBYTECODE", "1")
        for k, v in self.env.items():
            env.insert(k, v)
        p.setProcessEnvironment(env)
        p.setWorkingDirectory(self.root)
        p.setProcessChannelMode(QProcess.MergedChannels)
        out = []

        def read():
            try:
                out.append(bytes(p.readAllStandardOutput()).decode("utf-8", "replace"))
            except RuntimeError:     # PyStudio is closing
                pass
        p.readyReadStandardOutput.connect(read)

        def finished(code, _s):
            read()
            if self.proc is not p:   # canceled by shutdown()
                return
            self.proc = None
            try:
                self._update_buttons()
                done(code, "".join(out))
                p.deleteLater()
            except RuntimeError:     # the window was already destroyed (PyStudio quitting)
                pass
        p.finished.connect(finished)
        self.proc = p
        self._update_buttons()
        # pythonpath=. : tests in a tests/ folder can import the project's modules
        from .solution import nested_projects    # other projects of a solution inside this one
        ignore = [f"--ignore={d}" for d in nested_projects(self.root)] if self.root else []
        pytest = ["-m", "pytest", "-p", "no:cacheprovider", "-o", "pythonpath=."] + ignore + args
        if coverage_file:
            omit = ",".join(["*/test_*.py", "*/*_test.py", "*/tests/*", "*/ui_*.py", "*/pystudio_runtime.py",
                             "*/my.py", "*/.venv/*", "*/venv/*", "*/publish/*", "*/conftest.py"])
            pytest = ["-m", "coverage", "run", f"--data-file={coverage_file}", f"--source={self.root}",
                      f"--omit={omit}"] + pytest
        p.start(self.python, pytest)

    def cancel(self):
        if self.proc is not None:
            self.proc.kill()

    def shutdown(self):
        """PyStudio is closing: stop pytest without reporting back."""
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.finished.disconnect()
                p.readyReadStandardOutput.disconnect()
            except (RuntimeError, TypeError):
                pass
            p.kill()
            p.waitForFinished(2000)

    def discover(self):
        if not self.root or self.proc is not None:
            return
        self.summary.setText("Finding tests...")

        def done(code, text):
            if "No module named pytest" in text:
                self.summary.setText("")
                self._show_empty("Test Explorer uses pytest, which isn't installed for this project's Python.",
                                 install=True)
                return
            self.nodes = parse_collect(text)
            if not self.nodes:
                self.tree.clear()
                errors = [line for line in text.splitlines() if line.startswith(("ERROR", "E "))]
                self._show_empty("No tests found. Add one with Project > Add Unit Test - a file named "
                                 "test_*.py with functions named test_*." +
                                 ("\n\nCollection errors:\n" + "\n".join(errors[:8]) if errors else ""))
                self.summary.setText("")
                return
            self._show_empty("")
            self._build_tree()
        self._start(["--collect-only", "-q"], done)

    def _run(self, node_ids, coverage=False):
        if not self.root or self.proc is not None:
            return
        cov_file = None
        if coverage:
            fd, cov_file = tempfile.mkstemp(suffix=".coverage", prefix="pystudio-")
            os.close(fd)
        fd, self._junit = tempfile.mkstemp(suffix=".xml", prefix="pystudio-tests-")
        os.close(fd)
        self._running = set(node_ids) if node_ids else set(self.nodes)
        for n in self._running:
            self.results[n] = {"outcome": "running", "time": 0, "message": "", "details": ""}
        self._refresh_icons()
        self.summary.setText(f"Running {len(self._running)} test(s)...")
        self.log.emit(f"> {'coverage run -m ' if coverage else ''}pytest "
                      f"{' '.join(node_ids) if node_ids else '(all tests)'}\n", "info")

        def done(code, text):
            self.log.emit(text, "info")
            if cov_file:
                if "No module named coverage" in text:
                    self.summary.setText("")
                    self._show_empty("Code coverage uses the coverage package, which isn't installed for this "
                                     "project's Python.")
                    self.b_install_cov.show()
                    for n in self._running:
                        self.results.pop(n, None)
                    self._running = set()
                    self._build_tree()
                    return
                self._coverage_report(cov_file)
            got = parse_junit(self._junit, self.root)
            try:
                os.remove(self._junit)
            except OSError:
                pass
            for n in self._running:
                if n in got:
                    self.results[n] = got[n]
                else:
                    self.results[n] = {"outcome": "error" if code not in (0, 1, 5) else "notrun", "time": 0,
                                       "message": "Didn't run" if code in (0, 1, 5) else "pytest failed - see Output",
                                       "details": text[-3000:] if code not in (0, 1, 5) else ""}
            for n, r in got.items():           # tests found only now (e.g. new file)
                if n not in self.nodes:
                    self.nodes.append(n)
                self.results[n] = r
            self._running = set()
            self._build_tree()
            self._details(self.tree.currentItem())
        args = ["-q", f"--junitxml={self._junit}", "-o", "junit_family=xunit1"]
        self._start(args + list(node_ids), done, cov_file)

    def run_coverage(self):
        """Test > Analyze Code Coverage for All Tests."""
        self.b_install_cov.hide()
        self._show_empty("")
        self._run([], coverage=True)

    def _coverage_report(self, cov_file):
        import json
        import subprocess
        out = cov_file + ".json"
        data = None
        try:
            r = subprocess.run([self.python, "-m", "coverage", "json", f"--data-file={cov_file}", "-o", out,
                                "--ignore-errors"], cwd=self.root, capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and os.path.exists(out):
                with open(out, encoding="utf-8") as f:
                    data = json.load(f)
            elif "No data" in (r.stdout + r.stderr):
                data = {"files": {}, "totals": {}}
        except (OSError, subprocess.SubprocessError, ValueError):
            data = None
        finally:
            for f in (cov_file, out):
                try:
                    os.remove(f)
                except OSError:
                    pass
        if data is None:
            self.log.emit("Couldn't read the coverage results.\n", "err")
            return
        files = {}
        for path, info in data.get("files", {}).items():
            full = os.path.abspath(os.path.join(self.root, path))
            files[full] = {"covered": info.get("executed_lines", []), "missing": info.get("missing_lines", []),
                           "percent": info.get("summary", {}).get("percent_covered", 0.0)}
        total = data.get("totals", {}).get("percent_covered", 0.0)
        self.log.emit(f"Code coverage: {total:.0f}% of the lines in {len(files)} file(s) ran during the tests.\n",
                      "info")
        self.coverage_ready.emit(files)

    def run_all(self):
        self._run([])

    def selected_nodes(self):
        out = []
        for it in self.tree.selectedItems() or ([self.tree.currentItem()] if self.tree.currentItem() else []):
            out += self._nodes_under(it)
        return list(dict.fromkeys(out))

    def run_selected(self):
        nodes = self.selected_nodes()
        if nodes:
            self._run(nodes)

    def run_failed(self):
        nodes = [n for n, r in self.results.items() if r["outcome"] in ("failed", "error")]
        if nodes:
            self._run(nodes)

    def debug_selected(self):
        nodes = self.selected_nodes() or list(self.nodes)
        if nodes:
            self.debug_requested.emit(nodes)

    # ---------------------------------------------------------- tree
    def _nodes_under(self, it):
        n = it.data(0, Qt.UserRole)
        if n:
            return [n]
        out = []
        for i in range(it.childCount()):
            out += self._nodes_under(it.child(i))
        return out

    def _build_tree(self):
        cur = self.tree.currentItem().data(0, Qt.UserRole) if self.tree.currentItem() else None
        self.tree.clear()
        groups = {}
        for n in self.nodes:
            parts = n.split("::")
            fitem = groups.get(parts[0])
            if fitem is None:
                fitem = QTreeWidgetItem([parts[0], ""])
                fitem.setData(0, Qt.UserRole + 1, os.path.join(self.root, parts[0]))
                self.tree.addTopLevelItem(fitem)
                groups[parts[0]] = fitem
            parent = fitem
            for cls in parts[1:-1]:
                key = parts[0] + "::" + cls
                citem = groups.get(key)
                if citem is None:
                    citem = QTreeWidgetItem([cls, ""])
                    citem.setData(0, Qt.UserRole + 1, os.path.join(self.root, parts[0]))
                    parent.addChild(citem)
                    groups[key] = citem
                parent = citem
            it = QTreeWidgetItem([parts[-1], ""])
            it.setData(0, Qt.UserRole, n)
            parent.addChild(it)
            if n == cur:
                self.tree.setCurrentItem(it)
        self.tree.expandAll()
        self._refresh_icons()
        self._apply_filter(self.filter.text())

    def _refresh_icons(self):
        def walk(it):
            n = it.data(0, Qt.UserRole)
            if n:
                r = self.results.get(n, {"outcome": "notrun", "time": 0})
                outcome = r["outcome"]
                t = r.get("time") or 0
                it.setText(1, f"{t * 1000:.0f} ms" if t and outcome not in ("running", "notrun") else "")
            else:
                kids = [walk(it.child(i)) for i in range(it.childCount())]
                outcome = ("running" if "running" in kids else "failed" if {"failed", "error"} & set(kids)
                           else "notrun" if "notrun" in kids or not kids else
                           "skipped" if all(k == "skipped" for k in kids) else "passed")
            icon, color = ICONS[outcome]
            name = it.text(0).split(" ", 1)[1] if it.text(0)[:1] in "✔✖⊘○◷" else it.text(0)
            it.setText(0, f"{icon} {name}")
            it.setForeground(0, QColor(theme.c(color)))
            return outcome
        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        rs = [self.results.get(n, {"outcome": "notrun"})["outcome"] for n in self.nodes]
        if not any(o == "running" for o in rs):
            total = sum((self.results.get(n) or {}).get("time", 0) or 0 for n in self.nodes)
            parts = [f"{len(rs)} tests"]
            for key, label in (("passed", "passed"), ("failed", "failed"), ("error", "errors"),
                               ("skipped", "skipped"), ("notrun", "not run")):
                c = rs.count(key)
                if c:
                    parts.append(f"{c} {label}")
            took = (f"{total:.2f} s" if total >= 1 else f"{total * 1000:.0f} ms") if total else ""
            self.summary.setText("  ·  ".join(parts) + (f"  ·  {took}" if took else ""))

    def _apply_filter(self, text):
        t = text.lower().strip()

        def walk(it):
            n = it.data(0, Qt.UserRole)
            if n:
                show = not t or t in n.lower()
            else:
                show = any([walk(it.child(i)) for i in range(it.childCount())])
            it.setHidden(not show)
            return show
        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    def _details(self, it):
        if it is None:
            self.details.clear()
            return
        n = it.data(0, Qt.UserRole)
        if not n:
            self.details.setPlainText(self.summary.text())
            return
        r = self.results.get(n, {"outcome": "notrun"})
        lines = [n, "", f"Outcome: {r['outcome'].replace('notrun', 'not run')}"]
        if r.get("time"):
            lines.append(f"Duration: {r['time'] * 1000:.0f} ms")
        if r.get("message"):
            lines += ["", "Message:", r["message"]]
        if r.get("details"):
            lines += ["", r["details"]]
        self.details.setPlainText("\n".join(lines))

    def location(self, node):
        r = self.results.get(node) or {}
        path = r.get("file") or os.path.join(self.root, node.split("::")[0])
        line = r.get("line") or find_test_line(path, node.split("::")[-1])
        return path, line

    def _open(self, it, _col):
        n = it.data(0, Qt.UserRole)
        if n:
            self.open_location.emit(*self.location(n))
        elif it.data(0, Qt.UserRole + 1):
            path = it.data(0, Qt.UserRole + 1)
            name = it.text(0).split(" ", 1)[-1]
            self.open_location.emit(path, find_test_line(path, name) if "::" not in name and not name.endswith(".py") else 1)


def new_test_file(root, module=None):
    """tests/test_<module>.py (or test_example.py). Returns its path."""
    folder = os.path.join(root, "tests")
    os.makedirs(folder, exist_ok=True)
    base = f"test_{module}" if module else "test_example"
    path, n = os.path.join(folder, base + ".py"), 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}{n}.py")
        n += 1
    about = f" for {module}.py" if module else ""
    imports = f"import {module}\n" if module else ""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(TEST_TEMPLATE.format(about=about, imports=imports))
    return path


class CoverageResults(QWidget):
    """VS's Code Coverage Results: each file's covered percentage and lines. Double-click: the first line the
    tests don't run."""
    open_location = Signal(str, int)
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.summary = QLabel("Run Test > Analyze Code Coverage (or ◑ Coverage in Test Explorer).")
        self.summary.setStyleSheet("color: #aaaaaa; padding: 3px 6px;")
        b_clear = QToolButton()
        b_clear.setText("Hide coloring")
        b_clear.setToolTip("Remove the green / red coverage coloring from the editor")
        b_clear.setAutoRaise(True)
        b_clear.clicked.connect(self.clear_requested)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.summary, 1)
        top.addWidget(b_clear)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["File", "Covered %", "Lines run", "Lines not run"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._open)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        self.files = {}

    def load(self, files, root=None):
        self.files = files
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        run = sum(len(f["covered"]) for f in files.values())
        not_run = sum(len(f["missing"]) for f in files.values())
        pct = 100.0 * run / (run + not_run) if run + not_run else 100.0
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setText(f"<b>{pct:.0f}%</b> covered - {run} line(s) ran during the tests, {not_run} didn't "
                             f"({len(files)} file(s)). Double-click a file for its first line that didn't run.")
        for path, f in sorted(files.items()):
            r = self.table.rowCount()
            self.table.insertRow(r)
            rel = os.path.relpath(path, root) if root and path.startswith(root + os.sep) else path
            pct_item = QTableWidgetItem()
            pct_item.setData(Qt.DisplayRole, round(f["percent"], 1))
            pct_item.setForeground(QColor("#2ea043") if f["percent"] >= 80 else
                                   QColor("#d29922") if f["percent"] >= 50 else QColor("#e5534b"))
            items = [QTableWidgetItem(rel), pct_item, QTableWidgetItem(), QTableWidgetItem()]
            items[2].setData(Qt.DisplayRole, len(f["covered"]))
            items[3].setData(Qt.DisplayRole, len(f["missing"]))
            for c, it in enumerate(items):
                it.setData(Qt.UserRole, path)
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.AscendingOrder)

    def clear(self):
        self.files = {}
        self.table.setRowCount(0)
        self.summary.setText("Run Test > Analyze Code Coverage (or ◑ Coverage in Test Explorer).")

    def _open(self, row, _col):
        it = self.table.item(row, 0)
        path = it.data(Qt.UserRole) if it else None
        if path:
            missing = self.files.get(path, {}).get("missing") or [1]
            self.open_location.emit(path, min(missing))
