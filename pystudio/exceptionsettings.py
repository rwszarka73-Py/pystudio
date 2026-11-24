"""Exception Settings (VS: Debug > Windows > Exception Settings, Ctrl+Alt+E): choose exceptions
that stop the debugger as soon as they're raised - even ones your code catches with try/except.
Unhandled exceptions always stop."""
import json

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

COMMON = [
    ("ArithmeticError", "base of ZeroDivisionError, OverflowError"),
    ("AssertionError", "assert failed (also failing pytest asserts)"),
    ("AttributeError", "x.name doesn't exist (often: x is None)"),
    ("ConnectionError", "network connection failed"),
    ("FileNotFoundError", "open() of a missing file"),
    ("ImportError", "import failed"),
    ("IndexError", "list index out of range"),
    ("KeyError", "dict key doesn't exist"),
    ("LookupError", "base of IndexError, KeyError"),
    ("ModuleNotFoundError", "no module with that name"),
    ("NameError", "a name isn't defined"),
    ("NotImplementedError", "not written yet"),
    ("OSError", "file / OS errors (base of FileNotFoundError...)"),
    ("PermissionError", "not allowed to read/write"),
    ("RecursionError", "function calls itself forever"),
    ("RuntimeError", "general runtime problem"),
    ("TimeoutError", "something took too long"),
    ("TypeError", "wrong type, e.g. '1' + 2"),
    ("UnicodeError", "text encoding problems"),
    ("ValueError", "right type, bad value, e.g. int('abc')"),
    ("ZeroDivisionError", "divided by zero"),
    ("DataError", "PyStudio data access (my.query, my.save...)"),
]


def load():
    """{"types": [names], "all": bool, "custom": [names]}"""
    try:
        d = json.loads(QSettings().value("exceptionSettings", "") or "{}")
    except ValueError:
        d = {}
    return {"types": list(d.get("types", [])), "all": bool(d.get("all")), "custom": list(d.get("custom", []))}


def save(d):
    QSettings().setValue("exceptionSettings", json.dumps(d))


class ExceptionSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Exception Settings")
        self.resize(560, 560)
        d = load()
        self.custom = list(d["custom"])
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.textChanged.connect(self._filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Break When Thrown", "What it means"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setRootIsDecorated(True)
        self.root = QTreeWidgetItem(["Python Exceptions (all)", "every exception your code raises"])
        self.root.setFlags(self.root.flags() | Qt.ItemIsUserCheckable)
        self.root.setCheckState(0, Qt.Checked if d["all"] else Qt.Unchecked)
        self.tree.addTopLevelItem(self.root)
        on = set(d["types"])
        for name, desc in COMMON + [(c, "(added by you)") for c in self.custom if c not in dict(COMMON)]:
            it = QTreeWidgetItem([name, desc])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked if name in on else Qt.Unchecked)
            self.root.addChild(it)
        self.root.setExpanded(True)
        b_add = QPushButton("Add...")
        b_add.setToolTip("Add an exception by name, e.g. your own class or json.JSONDecodeError's name JSONDecodeError")
        b_add.clicked.connect(self._add)
        b_none = QPushButton("Uncheck All")
        b_none.clicked.connect(self._uncheck)
        row = QHBoxLayout()
        row.addWidget(b_add)
        row.addWidget(b_none)
        row.addStretch()
        note = QLabel("Checked exceptions stop the debugger where they're raised in your code, even inside "
                      "try/except. Unhandled exceptions always stop. A class also matches its subclasses "
                      "(OSError catches FileNotFoundError).")
        note.setWordWrap(True)
        note.setStyleSheet("color: #9a9a9a;")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.search)
        lay.addWidget(self.tree, 1)
        lay.addLayout(row)
        lay.addWidget(note)
        lay.addWidget(bb)

    def _filter(self, text):
        t = text.lower()
        for i in range(self.root.childCount()):
            it = self.root.child(i)
            it.setHidden(bool(t) and t not in it.text(0).lower() and t not in it.text(1).lower())

    def _add(self):
        name, ok = QInputDialog.getText(self, "Add Exception", "Exception class name (e.g. MyAppError):")
        name = name.strip().split(".")[-1]
        if ok and name.isidentifier():
            if name not in self.custom:
                self.custom.append(name)
            it = QTreeWidgetItem([name, "(added by you)"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked)
            self.root.addChild(it)

    def _uncheck(self):
        self.root.setCheckState(0, Qt.Unchecked)
        for i in range(self.root.childCount()):
            self.root.child(i).setCheckState(0, Qt.Unchecked)

    def settings(self):
        types = [self.root.child(i).text(0) for i in range(self.root.childCount())
                 if self.root.child(i).checkState(0) == Qt.Checked]
        return {"types": types, "all": self.root.checkState(0) == Qt.Checked, "custom": self.custom}

    def _ok(self):
        save(self.settings())
        self.accept()
