"""Debug > Attach to Process: debug a Python program that's already running (started from a terminal, a
desktop icon, a service...). PyStudio makes the program load its debugger, which connects back:

- gdb (Tools > Install Components) runs the attach code in the program right away, even while it waits in C code
  (a Qt program idle in its event loop).
- Python 3.14+ without gdb: Python's own sys.remote_exec, which runs it the next time the main thread runs Python code.

Ubuntu only lets a program inspect its own child processes, so attaching asks for your password (pkexec) unless
the system allows it (kernel.yama.ptrace_scope = 0). Python 3.12 or newer is needed (sys.monitoring)."""
import os
import re
import shutil
import tempfile

from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

DEBUGGEE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debuggee.py")


def ptrace_scope():
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0                           # no Yama: normal permission rules (same user may attach)


def python_processes():
    """[{pid, exe, version (major, minor) or None, cmd, title}] - the current user's Python programs."""
    me, mine = os.getuid(), {os.getpid(), os.getppid()}
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) in mine:
            continue
        pid = int(name)
        try:
            if os.stat(f"/proc/{pid}").st_uid != me and me != 0:
                continue
            exe = os.readlink(f"/proc/{pid}/exe")
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        base = os.path.basename(exe)
        m = re.match(r"python(\d)\.(\d+)", base)
        if not m and not base.startswith("python"):
            continue
        if any(x.endswith(("nbkernel.py", "repl_server.py", "debuggee.py", "language_server.py")) or
               "jedi" in x for x in argv[1:3]):
            continue                       # PyStudio's own helpers
        version = (int(m.group(1)), int(m.group(2))) if m else None
        script = next((a for a in argv[1:] if not a.startswith("-")), "")
        out.append({"pid": pid, "exe": exe, "version": version, "cmd": " ".join(argv),
                    "title": os.path.basename(script) if script else base})
    return sorted(out, key=lambda p: -p["pid"])


def write_script(port, root):
    """The few lines the program runs to load PyStudio's debugger and connect back."""
    fd, path = tempfile.mkstemp(prefix="pystudio-attach-", suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write("import importlib.util as _u, sys as _s\n"
                f"_spec = _u.spec_from_file_location('pystudio_attach_debuggee', {DEBUGGEE!r})\n"
                "_m = _u.module_from_spec(_spec)\n"
                "_s.modules['pystudio_attach_debuggee'] = _m\n"
                "_spec.loader.exec_module(_m)\n"
                f"_m.attach({int(port)}, {root!r})\n")
    os.chmod(path, 0o644)                  # readable by the program (pkexec runs gdb as root)
    return path


def injector(proc_info, script):
    """(program, args, needs_root) that makes the process run script."""
    pid, version = proc_info["pid"], proc_info["version"] or (0, 0)
    if version >= (3, 14) and not shutil.which("gdb"):
        # Python's own remote_exec runs the code only when the main thread next runs Python code, so an idle GUI
        # program (sitting in Qt's event loop) won't connect until you click something in it; gdb doesn't wait.
        cmd = [proc_info["exe"], "-c", f"import sys; sys.remote_exec({pid}, {script!r})"]
    else:
        gdb = shutil.which("gdb") or "gdb"
        code = f'exec(open("{script}").read())'.replace('"', '\\"')
        cmd = [gdb, "-p", str(pid), "-batch", "-nx", "-q",
               "-ex", "set confirm off",
               "-ex", "call (int)PyGILState_Ensure()",
               "-ex", f'call (int)PyRun_SimpleString("{code}")',
               "-ex", "call (void)PyGILState_Release($1)",
               "-ex", "detach"]
    needs_root = ptrace_scope() >= 1 and os.getuid() != 0
    if needs_root:
        cmd = [shutil.which("pkexec") or "pkexec"] + cmd
    return cmd[0], cmd[1:], needs_root


def check(proc_info):
    """None, or why PyStudio can't attach to this process."""
    v = proc_info.get("version")
    if v is not None and v < (3, 12):
        return f"This program runs Python {v[0]}.{v[1]}. Attaching needs Python 3.12 or newer."
    if ptrace_scope() >= 3:
        return "This computer doesn't allow attaching to programs (kernel.yama.ptrace_scope = 3)."
    if (v is None or v < (3, 14)) and not shutil.which("gdb"):
        return "gdb isn't installed: Tools > Install Components > gdb (or  sudo apt install gdb)."
    if ptrace_scope() >= 1 and os.getuid() != 0 and not shutil.which("pkexec"):
        return ("Ubuntu only lets a program inspect its own child processes, and pkexec (the password prompt) "
                "isn't available. In a terminal:  sudo sysctl kernel.yama.ptrace_scope=0  allows it until restart.")
    return None


class Injector(QProcess):
    """Runs the injector; done(ok, message)."""
    done = Signal(bool, str)

    def run(self, proc_info, script):
        prog, args, self.needs_root = injector(proc_info, script)
        self.setProcessChannelMode(QProcess.MergedChannels)
        self.finished.connect(self._finished)
        self.errorOccurred.connect(lambda e: e == QProcess.FailedToStart and self.done.emit(
            False, f"Couldn't start {os.path.basename(prog)}."))
        self.start(prog, args)

    def _finished(self, code, _status):
        out = bytes(self.readAllStandardOutput()).decode("utf-8", "replace")
        if code in (126, 127) and self.needs_root:
            self.done.emit(False, "Canceled (no password given).")
        elif code != 0 or "Operation not permitted" in out or "No symbol" in out:
            hint = ""
            if "No symbol" in out:
                hint = "\n\nThis Python doesn't expose the functions gdb needs."
            elif "not permitted" in out:
                hint = "\n\nAttaching wasn't allowed."
            self.done.emit(False, (out.strip()[-1200:] or f"exit code {code}") + hint)
        else:
            self.done.emit(True, out)


class AttachDialog(QDialog):
    """Pick the program: Python processes of yours, newest first."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Attach to Process")
        self.resize(820, 480)
        v = QVBoxLayout(self)
        intro = QLabel("Debug a Python program that's already running. Set breakpoints in its files first (or "
                       "after attaching); Stop Debugging detaches and leaves it running.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        h = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name or command line")
        self.filter.textChanged.connect(self.fill)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh)
        h.addWidget(self.filter, 1)
        h.addWidget(b)
        v.addLayout(h)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Process", "ID", "Python", "Command line"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.table.horizontalHeader()
        for c in range(3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self.accept())
        self.table.itemSelectionChanged.connect(self._selected)
        v.addWidget(self.table, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #d29922;")
        v.addWidget(self.note)
        scope = ptrace_scope()
        perm = QLabel("Ubuntu will ask for your password to attach (it only lets programs inspect their own "
                      "child processes)." if scope >= 1 and os.getuid() != 0 else "")
        perm.setStyleSheet("color: #9a9a9a;")
        perm.setWordWrap(True)
        v.addWidget(perm)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("Attach")
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        v.addWidget(self.bb)
        self.procs = []
        self.refresh()

    def refresh(self):
        self.procs = python_processes()
        self.fill()

    def fill(self):
        t = self.filter.text().strip().lower()
        self.table.setRowCount(0)
        self.shown = [p for p in self.procs if not t or t in p["cmd"].lower() or t in p["title"].lower()]
        for p in self.shown:
            r = self.table.rowCount()
            self.table.insertRow(r)
            v = p["version"]
            for c, val in enumerate([p["title"], str(p["pid"]), f"{v[0]}.{v[1]}" if v else "?", p["cmd"]]):
                it = QTableWidgetItem(val)
                if v is not None and v < (3, 12):
                    it.setForeground(Qt.gray)
                it.setToolTip(p["cmd"])
                self.table.setItem(r, c, it)
        if self.shown:
            self.table.selectRow(0)
        self._selected()

    def selected(self):
        r = self.table.currentRow()
        return self.shown[r] if 0 <= r < len(self.shown) else None

    def _selected(self):
        p = self.selected()
        why = check(p) if p else None
        self.note.setText(why or "")
        self.bb.button(QDialogButtonBox.Ok).setEnabled(p is not None and why is None)

