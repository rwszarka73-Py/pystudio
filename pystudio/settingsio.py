"""Tools > Import and Export Settings: like Visual Studio's. Save PyStudio's settings to one file and load
them on another machine (or back them up, or start fresh).

What's in the file, by category: options (theme, editor, formatting, code analysis, Claude model, updates),
window layout (dock positions, saved layouts), recent projects, Find history, breakpoints and bookmarks,
remote servers (address, user, folder - never passwords), and optionally the query history.
Never in it: passwords, API keys, tokens (those stay in this machine's keyring), and things that only make
sense on this machine (mounted folders, the last update check).
Paths under your home folder are stored as ~ and become the other machine's home folder.

Importing (and Reset) take effect when PyStudio restarts: it restarts for you, and saves a backup of the
current settings first (~/.local/share/pystudio/settings-backups)."""
import base64
import datetime
import json
import os
import re

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox, QLabel, QMessageBox,
                               QRadioButton, QVBoxLayout)

FORMAT = "pystudio-settings"
VERSION = 1
EXT = ".pystudio-settings"
HOME = os.path.expanduser("~")
DATA = os.path.join(HOME, ".local", "share", "pystudio")
PENDING = os.path.join(DATA, "pending-settings.json")
BACKUPS = os.path.join(DATA, "settings-backups")
HISTORY = os.path.join(DATA, "query_history.jsonl")

CATEGORIES = [
    ("options", "Options: theme, editor, formatting, code analysis, Claude, updates", True),
    ("layout", "Window layout and saved layouts", True),
    ("recent", "Recent projects and the projects folder", True),
    ("find", "Find history", True),
    ("debug", "Breakpoints, bookmarks and exception settings", True),
    ("remote", "Remote servers (address, user, folder - no passwords)", True),
    ("queries", "Query history (the SQL you've run)", False),
]
KEY_CATEGORY = {
    "windowState": "layout", "geometry": "layout", "layouts": "layout",
    "recentProjects": "recent", "recent": "recent", "projectsDir": "recent", "lastTemplate": "recent",
    "findHistory": "find", "replaceHistory": "find", "findTypes": "find", "findScope": "find",
    "breakpoints": "debug", "bookmarks": "debug", "exceptionSettings": "debug",
    "remoteRecent": "remote",
}
NEVER = {"remoteMounts", "updateLastCheck"}
SECRET = re.compile(r"pass|secret|token|apikey|api_key|credential", re.I)


def category(key):
    return KEY_CATEGORY.get(key.split("/")[0], "options")


# ------------------------------------------------------------ values <-> JSON
def _home_out(v):
    if isinstance(v, str):
        return v.replace(HOME, "~HOME~") if HOME in v else v
    if isinstance(v, list):
        return [_home_out(x) for x in v]
    if isinstance(v, dict):
        return {_home_out(k): _home_out(x) for k, x in v.items()}
    return v


def _home_in(v):
    if isinstance(v, str):
        return v.replace("~HOME~", HOME)
    if isinstance(v, list):
        return [_home_in(x) for x in v]
    if isinstance(v, dict):
        return {_home_in(k): _home_in(x) for k, x in v.items()}
    return v


def _to_json(v):
    if isinstance(v, (QByteArray, bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, (list, tuple)):
        return [_to_json(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _to_json(x) for k, x in v.items()}
    if v is None or isinstance(v, (str, int, float, bool)):
        return _home_out(v)
    return _home_out(str(v))


def _from_json(v):
    if isinstance(v, dict) and set(v) == {"__bytes__"}:
        return QByteArray(base64.b64decode(v["__bytes__"]))
    if isinstance(v, list):
        return [_from_json(x) for x in v]
    if isinstance(v, dict):
        return {k: _from_json(x) for k, x in v.items()}
    return _home_in(v)


# ------------------------------------------------------------ export / import
def _user():
    try:
        import getpass
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def collect(categories=None):
    """{"format", "version", "exported", "settings": {key: value}, "queryHistory": [...]}"""
    cats = set(categories or [c for c, _l, on in CATEGORIES if on])
    s = QSettings()
    out = {}
    for key in s.allKeys():
        if key.split("/")[0] in NEVER or SECRET.search(key) or category(key) not in cats:
            continue
        out[key] = _to_json(s.value(key))
    data = {"format": FORMAT, "version": VERSION, "exported": datetime.datetime.now().isoformat(timespec="seconds"),
            "from": f"{_user()}@{os.uname().nodename}", "settings": out}
    if "queries" in cats and os.path.isfile(HISTORY):
        with open(HISTORY, encoding="utf-8") as f:
            data["queryHistory"] = [_home_out(line.rstrip("\n")) for line in f if line.strip()]
    return data


def export_file(path, categories=None):
    data = collect(categories)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.chmod(path, 0o600)
    return len(data["settings"])


def read_file(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != FORMAT:
        raise ValueError("This isn't a PyStudio settings file.")
    return data


def categories_in(data):
    found = {category(k) for k in data.get("settings", {})}
    if data.get("queryHistory"):
        found.add("queries")
    return [c for c, _l, _on in CATEGORIES if c in found]


def backup():
    os.makedirs(BACKUPS, exist_ok=True)
    path = os.path.join(BACKUPS, datetime.datetime.now().strftime("before-%Y%m%d-%H%M%S") + EXT)
    export_file(path, [c for c, _l, _on in CATEGORIES])
    return path


def schedule(action, data=None, categories=None):
    """Applied by apply_pending() the next time PyStudio starts (before its window opens)."""
    os.makedirs(DATA, exist_ok=True)
    with open(PENDING, "w", encoding="utf-8") as f:
        json.dump({"action": action, "data": data, "categories": list(categories or [])}, f)
    os.chmod(PENDING, 0o600)


def apply_pending():
    """Called by main.py at startup. Returns a short message for the status bar, or ""."""
    if not os.path.isfile(PENDING):
        return ""
    try:
        with open(PENDING, encoding="utf-8") as f:
            p = json.load(f)
    except (OSError, ValueError):
        os.remove(PENDING)
        return ""
    os.remove(PENDING)
    s = QSettings()
    if p.get("action") == "reset":
        keep = {k: s.value(k) for k in s.allKeys() if k.split("/")[0] in NEVER}
        s.clear()
        for k, v in keep.items():
            s.setValue(k, v)
        s.sync()
        return "Settings were reset to their defaults."
    data = p.get("data") or {}
    cats = set(p.get("categories") or categories_in(data))
    n = 0
    for key, v in data.get("settings", {}).items():
        if category(key) in cats and key.split("/")[0] not in NEVER and not SECRET.search(key):
            s.setValue(key, _from_json(v))
            n += 1
    if "queries" in cats and data.get("queryHistory"):
        mine = set()
        if os.path.isfile(HISTORY):
            with open(HISTORY, encoding="utf-8") as f:
                mine = {line.rstrip("\n") for line in f}
        os.makedirs(DATA, exist_ok=True)
        with open(HISTORY, "a", encoding="utf-8") as f:
            for line in data["queryHistory"]:
                line = _home_in(line)
                if line not in mine:
                    f.write(line + "\n")
    s.sync()
    return f"Imported {n} settings."


# ------------------------------------------------------------ the dialog
class SettingsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle("Import and Export Settings")
        self.resize(560, 470)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Move your PyStudio settings to another machine, back them up, or start fresh."))
        self.rb_export = QRadioButton("Export selected settings to a file")
        self.rb_import = QRadioButton("Import selected settings from a file")
        self.rb_reset = QRadioButton("Reset all settings to their defaults")
        self.rb_export.setChecked(True)
        for rb in (self.rb_export, self.rb_import, self.rb_reset):
            v.addWidget(rb)
            rb.toggled.connect(self._mode)
        self.group = QGroupBox("Settings")
        g = QVBoxLayout(self.group)
        self.checks = {}
        for cat, label, on in CATEGORIES:
            ck = QCheckBox(label)
            ck.setChecked(on)
            self.checks[cat] = ck
            g.addWidget(ck)
        v.addWidget(self.group)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #9a9a9a;")
        v.addWidget(self.note)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.b_go = bb.addButton("Export...", QDialogButtonBox.AcceptRole)
        self.b_go.clicked.connect(self.go)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.data = None
        self._mode()

    def _mode(self):
        if self.rb_export.isChecked():
            self.group.setEnabled(True)
            for cat, _l, _on in CATEGORIES:
                self.checks[cat].setEnabled(True)
            self.b_go.setText("Export...")
            self.note.setText("Passwords, API keys and tokens are never exported; they stay in this machine's "
                              "keyring. Database connections travel with each project, not here.")
        elif self.rb_import.isChecked():
            self.group.setEnabled(True)
            self.b_go.setText("Choose File...")
            self.note.setText("PyStudio backs up your current settings first, then restarts to use the imported "
                              "ones.")
        else:
            self.group.setEnabled(False)
            self.b_go.setText("Reset...")
            self.note.setText("Everything goes back to how it was when PyStudio was first installed (your "
                              "projects, files and saved passwords aren't touched). A backup is saved first.")

    def selected(self):
        return [c for c, ck in self.checks.items() if ck.isChecked() and ck.isEnabled()]

    def go(self):
        if self.rb_export.isChecked():
            return self.do_export()
        if self.rb_import.isChecked():
            return self.do_import()
        return self.do_reset()

    def do_export(self, path=None):
        if not self.selected():
            return
        if path is None:
            name = f"PyStudio-{os.uname().nodename}-{datetime.date.today():%Y-%m-%d}{EXT}"
            path, _f = QFileDialog.getSaveFileName(self, "Export Settings", os.path.join(HOME, name),
                                                   f"PyStudio settings (*{EXT})")
            if not path:
                return
            if not path.endswith(EXT):
                path += EXT
        n = export_file(path, self.selected())
        QMessageBox.information(self, "Export Settings", f"Saved {n} settings to\n{path}\n\nCopy it to the other "
                                "machine and use Import there.")
        self.accept()

    def do_import(self, path=None):
        if self.data is None:
            if path is None:
                path, _f = QFileDialog.getOpenFileName(self, "Import Settings", HOME,
                                                       f"PyStudio settings (*{EXT});;All files (*)")
                if not path:
                    return
            try:
                self.data = read_file(path)
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, "Import Settings", f"Couldn't read it: {e}")
                return
            present = set(categories_in(self.data))
            for cat, ck in self.checks.items():
                ck.setEnabled(cat in present)
                ck.setChecked(cat in present)          # everything the file has, ticked
            self.note.setText(f"From {self.data.get('from', '?')}, exported {self.data.get('exported', '?')}. "
                              "Choose what to bring in, then Import.")
            self.b_go.setText("Import")
            return
        cats = self.selected()
        if not cats:
            return
        b = backup()
        schedule("import", self.data, cats)
        self._restart(f"Your current settings were saved to {b}.")

    def do_reset(self):
        if QMessageBox.question(self, "Reset Settings", "Reset all PyStudio settings to their defaults? PyStudio "
                                "saves a backup first and restarts.") != QMessageBox.Yes:
            return
        b = backup()
        schedule("reset")
        self._restart(f"Your current settings were saved to {b}.")

    def _restart(self, note):
        self.accept()
        if QMessageBox.question(self.window_, "Settings", f"{note}\n\nRestart PyStudio now to use the new "
                                "settings? (Otherwise they're used the next time it starts.)") == QMessageBox.Yes:
            from .updater import restart
            restart(self.window_)
