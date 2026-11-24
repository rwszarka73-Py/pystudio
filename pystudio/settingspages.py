"""Project Properties > Settings and Resources pages (VS's Settings.settings / Resources.resx)."""
import os
import shutil

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QFileDialog,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from . import mygen

DEFAULTS = {"String": "", "Integer": "0", "Decimal": "0.0", "Boolean": "False", "Color": "#000000",
            "Date": "", "StringList": ""}


def _note(text):
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: #9a9a9a;")
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lbl


class SettingsPage(QWidget):
    """Name | Type | Scope | Value, like VS's settings grid."""

    def __init__(self, project):
        super().__init__()
        self.p = project
        self.grid = QTableWidget(0, 4)
        self.grid.setHorizontalHeaderLabels(["Name", "Type", "Scope", "Value"])
        self.grid.verticalHeader().hide()
        hh = self.grid.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.grid.setColumnWidth(0, 170)
        self.grid.setColumnWidth(1, 110)
        self.grid.setColumnWidth(2, 110)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.grid.cellDoubleClicked.connect(self._double_clicked)
        b_add = QPushButton("Add Setting")
        b_add.clicked.connect(lambda: self.add_row())
        b_del = QPushButton("Remove")
        b_del.clicked.connect(self._remove)
        self.ck_save = QCheckBox("Save user settings when the app closes (like VB's SaveMySettingsOnExit)")
        self.ck_save.setChecked(bool(project.data.get("saveSettingsOnExit", True)))
        row = QHBoxLayout()
        row.addWidget(b_add)
        row.addWidget(b_del)
        row.addStretch()
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Settings</b>  (VB: My.Settings)"))
        lay.addWidget(self.grid, 1)
        lay.addLayout(row)
        lay.addWidget(self.ck_save)
        lay.addWidget(_note(
            "In code:  <tt>import my</tt> then <tt>my.settings.UserName</tt> to read and "
            "<tt>my.settings.UserName = \"Rob\"</tt> to change. <b>User</b> settings are saved per user in "
            "~/.config/&lt;app&gt;/settings.json; <b>Application</b> settings are read-only. "
            "Boolean: True/False. Date: yyyy-mm-dd. StringList: one value per line (double-click to edit). "
            "Color: double-click to pick."))
        for s in project.data.get("settings", []):
            self.add_row(s)

    def add_row(self, s=None):
        new = s is None
        s = s or {"name": self._unique(), "type": "String", "scope": "User", "value": ""}
        r = self.grid.rowCount()
        self.grid.insertRow(r)
        self.grid.setItem(r, 0, QTableWidgetItem(s.get("name", "")))
        cb = QComboBox()
        cb.addItems(mygen.SETTING_TYPES)
        cb.setCurrentText(s.get("type", "String"))
        cb.currentTextChanged.connect(lambda t, cb=cb: self._type_changed(cb, t))
        self.grid.setCellWidget(r, 1, cb)
        sc = QComboBox()
        sc.addItems(["User", "Application"])
        sc.setCurrentText(s.get("scope", "User"))
        self.grid.setCellWidget(r, 2, sc)
        v = s.get("value", "")
        if isinstance(v, list):
            v = "\n".join(v)
        self.grid.setItem(r, 3, QTableWidgetItem(str(v)))
        self._decorate(r)
        if new:
            self.grid.setCurrentCell(r, 0)
            self.grid.editItem(self.grid.item(r, 0))
        return r

    def _unique(self):
        names = {self.grid.item(r, 0).text() for r in range(self.grid.rowCount()) if self.grid.item(r, 0)}
        n = 1
        while f"Setting{n}" in names:
            n += 1
        return f"Setting{n}"

    def _row_of(self, widget):
        for r in range(self.grid.rowCount()):
            if self.grid.cellWidget(r, 1) is widget:
                return r
        return -1

    def _type_changed(self, cb, t):
        r = self._row_of(cb)
        if r >= 0:
            cur = self.grid.item(r, 3).text()
            if not cur or t in ("Boolean", "Integer", "Decimal", "Color"):
                self.grid.item(r, 3).setText(DEFAULTS[t])
            self._decorate(r)

    def _decorate(self, r):
        t = self.grid.cellWidget(r, 1).currentText()
        item = self.grid.item(r, 3)
        if t == "Color" and QColor(item.text()).isValid():
            item.setData(Qt.DecorationRole, QColor(item.text()))
        else:
            item.setData(Qt.DecorationRole, None)

    def _double_clicked(self, r, c):
        if c != 3:
            return
        t = self.grid.cellWidget(r, 1).currentText()
        item = self.grid.item(r, 3)
        if t == "Color":
            col = QColorDialog.getColor(QColor(item.text() or "#000000"), self, "Setting color")
            if col.isValid():
                item.setText(col.name())
                self._decorate(r)
        elif t == "StringList":
            text, ok = QInputDialog.getMultiLineText(self, "StringList", "One value per line:", item.text())
            if ok:
                item.setText(text)
        elif t == "Boolean":
            item.setText("False" if item.text().strip().lower() == "true" else "True")
        elif t == "Date" and not item.text():
            item.setText(QDate.currentDate().toString("yyyy-MM-dd"))

    def _remove(self):
        for r in sorted({i.row() for i in self.grid.selectedIndexes()}, reverse=True):
            self.grid.removeRow(r)

    def settings(self):
        out = []
        for r in range(self.grid.rowCount()):
            name = self.grid.item(r, 0).text().strip()
            t = self.grid.cellWidget(r, 1).currentText()
            v = self.grid.item(r, 3).text()
            out.append({"name": name, "type": t, "scope": self.grid.cellWidget(r, 2).currentText(),
                        "value": [x for x in v.splitlines() if x.strip()] if t == "StringList" else v})
        return out

    def validate(self):
        seen = set()
        for s in self.settings():
            n = s["name"]
            if not mygen.valid_name(n):
                return f"Setting name {n!r} isn't a valid name (letters, digits and _; can't start with a digit)."
            if n.lower() in seen:
                return f"There are two settings named {n}."
            seen.add(n.lower())
            v, t = s["value"], s["type"]
            if t == "Integer" and v.strip():
                try:
                    int(v)
                except ValueError:
                    return f"{n}: {v!r} isn't a whole number."
            if t == "Decimal" and v.strip():
                try:
                    float(v)
                except ValueError:
                    return f"{n}: {v!r} isn't a number."
            if t == "Boolean" and v.strip().lower() not in ("true", "false", ""):
                return f"{n}: use True or False."
            if t == "Date" and v.strip() and not QDate.fromString(v.strip(), "yyyy-MM-dd").isValid():
                return f"{n}: dates are written yyyy-mm-dd, e.g. 2026-11-21."
        return None

    def store(self):
        self.p["settings"] = self.settings()
        self.p["saveSettingsOnExit"] = self.ck_save.isChecked()


class ResourcesPage(QWidget):
    """Name | Type | Value - strings and files (images, text, other) the app uses."""

    def __init__(self, project):
        super().__init__()
        self.p = project
        self.grid = QTableWidget(0, 3)
        self.grid.setHorizontalHeaderLabels(["Name", "Type", "Value / File"])
        self.grid.verticalHeader().hide()
        self.grid.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.grid.setColumnWidth(0, 170)
        self.grid.setColumnWidth(1, 90)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.grid.currentCellChanged.connect(lambda r, *_: self._preview(r))
        self.preview = QLabel()
        self.preview.setFixedSize(140, 140)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("border: 1px solid #3f3f46;")
        b_str = QPushButton("Add String")
        b_str.clicked.connect(lambda: self.add_row({"name": self._unique("String"), "type": "String", "value": ""},
                                                   edit=True))
        b_file = QPushButton("Add Existing File...")
        b_file.clicked.connect(self._add_files)
        b_del = QPushButton("Remove")
        b_del.clicked.connect(self._remove)
        row = QHBoxLayout()
        for b in (b_str, b_file, b_del):
            row.addWidget(b)
        row.addStretch()
        body = QHBoxLayout()
        body.addWidget(self.grid, 1)
        side = QVBoxLayout()
        side.addWidget(self.preview)
        side.addStretch()
        body.addLayout(side)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Resources</b>  (VB: My.Resources)"))
        lay.addLayout(body, 1)
        lay.addLayout(row)
        lay.addWidget(_note(
            "Files are copied into the project's <b>resources</b> folder (Publish includes it). In code: "
            "<tt>import my</tt> then <tt>my.resources.Logo</tt> (an Image is a QPixmap, a Text file is its "
            "contents, any other file is its path), <tt>my.resources.icon(\"Logo\")</tt> for a QIcon."))
        for r in project.data.get("resources", []):
            self.add_row(r)

    def _unique(self, base):
        names = {self.grid.item(r, 0).text() for r in range(self.grid.rowCount())}
        n, name = 1, base + "1"
        while name in names:
            n += 1
            name = f"{base}{n}"
        return name

    def add_row(self, res, edit=False):
        r = self.grid.rowCount()
        self.grid.insertRow(r)
        self.grid.setItem(r, 0, QTableWidgetItem(res.get("name", "")))
        t = QTableWidgetItem(res.get("type", "String"))
        t.setFlags(t.flags() & ~Qt.ItemIsEditable)
        self.grid.setItem(r, 1, t)
        v = QTableWidgetItem(res.get("value", ""))
        if res.get("type", "String") != "String":
            v.setFlags(v.flags() & ~Qt.ItemIsEditable)
        self.grid.setItem(r, 2, v)
        if edit:
            self.grid.setCurrentCell(r, 2)
            self.grid.editItem(v)

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add resource files", os.path.expanduser("~"),
                                                "All files (*);;Images (*.png *.jpg *.jpeg *.gif *.svg *.ico *.bmp)")
        for path in paths:
            self.add_file(path)

    def add_file(self, path):
        folder = os.path.join(self.p.root, "resources")
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, os.path.basename(path))
        if os.path.abspath(path) != os.path.abspath(dest):
            shutil.copy2(path, dest)
        rel = os.path.relpath(dest, self.p.root).replace(os.sep, "/")
        name = mygen.ident(os.path.splitext(os.path.basename(path))[0])
        names = {self.grid.item(r, 0).text() for r in range(self.grid.rowCount())}
        base, n = name, 2
        while name in names:
            name = f"{base}{n}"
            n += 1
        self.add_row({"name": name, "type": mygen.resource_type_for(path), "value": rel})
        self.grid.setCurrentCell(self.grid.rowCount() - 1, 0)

    def _remove(self):
        for r in sorted({i.row() for i in self.grid.selectedIndexes()}, reverse=True):
            self.grid.removeRow(r)
        self.preview.clear()

    def _preview(self, r):
        self.preview.clear()
        if r < 0 or not self.grid.item(r, 1) or self.grid.item(r, 1).text() != "Image":
            return
        path = os.path.join(self.p.root, self.grid.item(r, 2).text())
        pm = QPixmap(path)
        if not pm.isNull():
            self.preview.setPixmap(pm.scaled(136, 136, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resources(self):
        return [{"name": self.grid.item(r, 0).text().strip(), "type": self.grid.item(r, 1).text(),
                 "value": self.grid.item(r, 2).text()} for r in range(self.grid.rowCount())]

    def validate(self):
        seen = set()
        for res in self.resources():
            n = res["name"]
            if not mygen.valid_name(n):
                return f"Resource name {n!r} isn't a valid name (letters, digits and _)."
            if n.lower() in seen:
                return f"There are two resources named {n}."
            seen.add(n.lower())
        return None

    def store(self):
        self.p["resources"] = self.resources()
