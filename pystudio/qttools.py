"""Qt Designer launch + automatic .ui -> ui_*.py generation (pyside6-uic)."""
import os
import shutil
import subprocess
import sys

from PySide6.QtCore import QFileSystemWatcher, QObject, QProcess, QTimer, Signal

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv"}


def _tool(name):
    """Find a PySide6 tool next to the running Python first (PyStudio's venv)."""
    local = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return shutil.which(name)


def generated_path(ui_path):
    folder, fname = os.path.split(ui_path)
    return os.path.join(folder, "ui_" + os.path.splitext(fname)[0] + ".py")


def needs_compile(ui_path):
    out = generated_path(ui_path)
    return not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(ui_path)


def compile_ui(ui_path):
    """Generate ui_<name>.py. Returns (ok, message)."""
    uic = _tool("pyside6-uic")
    if not uic:
        return False, "pyside6-uic not found (is PySide6 installed?)"
    out = generated_path(ui_path)
    r = subprocess.run([uic, ui_path, "-o", out], capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip() or f"pyside6-uic failed on {ui_path}"
    try:
        _add_component_hints(ui_path, out)
    except Exception:  # noqa: BLE001 - hints are only for IntelliSense
        pass
    return True, f"Generated {os.path.basename(out)}"


_COMPONENT_TYPES = {"Timer": "_PsQTimer", "OpenFileDialog": "_ps_rt.OpenFileDialog",
                    "SaveFileDialog": "_ps_rt.SaveFileDialog",
                    "FolderBrowserDialog": "_ps_rt.FolderBrowserDialog",
                    "ColorDialog": "_ps_rt.ColorDialog", "FontDialog": "_ps_rt.FontDialog",
                    "ImageList": "_ps_rt.ImageList", "PrintDocument": "_ps_rt.PrintDocument",
                    "PrintDialog": "_ps_rt.PrintDialog", "PrintPreviewDialog": "_ps_rt.PrintPreviewDialog",
                    "PageSetupDialog": "_ps_rt.PageSetupDialog"}


def _add_component_hints(ui_path, out):
    """Components (Timer, dialogs) are created at runtime by pystudio_runtime, so
    declare them on the Ui class for IntelliSense (self.ui.timer1. -> start, stop...)."""
    import json
    import re
    import xml.etree.ElementTree as ET
    top = ET.parse(ui_path).getroot().find("widget")
    comps = []
    for p in top.findall("property") if top is not None else []:
        if p.get("name") == "components":
            comps = json.loads(p.findtext("string") or "[]")
    if not comps:
        return
    with open(out, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"^class Ui_\w+\(object\):\n", src, re.M)
    if not m:
        return
    hints = ["    # Components - created by pystudio_runtime.setup_form() (type hints for IntelliSense)"]
    for c in comps:
        t = _COMPONENT_TYPES.get(c.get("type"))
        if t:
            hints.append(f'    {c["name"]}: "{t}"')
    imports = ("from PySide6.QtCore import QTimer as _PsQTimer  # PyStudio: component hints\n"
               "import pystudio_runtime as _ps_rt  # PyStudio: component hints\n")
    i = src.rfind("\n", 0, m.start()) + 1
    src = src[:m.start()] + imports + "\n" + src[m.start():m.end()] + "\n".join(hints) + "\n\n" + src[m.end():]
    with open(out, "w", encoding="utf-8") as f:
        f.write(src)


def compile_all_stale(root):
    """Compile every out-of-date .ui file under root. Returns list of (ok, msg)."""
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".ui"):
                p = os.path.join(dirpath, f)
                if needs_compile(p):
                    results.append(compile_ui(p))
    return results


class DesignerBridge(QObject):
    """Opens .ui files in Qt Designer and regenerates ui_*.py whenever one is saved."""

    message = Signal(str, str)  # text, kind ("info" / "err")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._on_changed)
        self._pending = set()
        self._timer = QTimer(self, singleShot=True, interval=300)
        self._timer.timeout.connect(self._flush)

    def open(self, ui_path):
        designer = _tool("pyside6-designer")
        if not designer:
            self.message.emit("Qt Designer (pyside6-designer) not found.\n", "err")
            return False
        if ui_path not in self.watcher.files():
            self.watcher.addPath(ui_path)
        QProcess.startDetached(designer, [ui_path])
        return True

    def _on_changed(self, path):
        self._pending.add(path)
        self._timer.start()  # debounce: Designer may write the file in several steps

    def _flush(self):
        for path in self._pending:
            if os.path.exists(path):
                if path not in self.watcher.files():   # re-arm after save-by-rename
                    self.watcher.addPath(path)
                ok, msg = compile_ui(path)
                self.message.emit(msg + "\n", "info" if ok else "err")
        self._pending.clear()
