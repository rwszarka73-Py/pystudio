"""Windows check: finds code that works on Ubuntu but not on Windows, before Publish for Windows builds it.

It reads the code (it doesn't run it) and reports, with file and line:
  problems  - things that fail on Windows (preexec_fn, os.fork, fcntl, libc.so.6...)
  warnings  - things that quietly do nothing there (xdg-open, dbus-send, /usr/bin/..., ~/.config/...)
  notes     - things that work but look wrong (a console window flashing, open() without an encoding)
Code that already checks the platform first (if sys.platform == "win32": ... / if WINDOWS: ... return,
or try/except around it) is left alone."""
import ast
import os
import re
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHeaderView, QLabel, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

PROBLEM, WARNING, NOTE = "Problem", "Warning", "Note"
SKIP_DIRS = {"__pycache__", "venv", ".venv", "env", "build", "dist", "publish", "packaging", "tests", "test",
             "docs", "scripts", "examples", "node_modules", "installer", "site-packages"}

LINUX_MODULES = {
    "fcntl": "file locking/flags", "termios": "terminal settings", "pty": "pseudo-terminals", "tty": "terminal modes",
    "pwd": "the user database", "grp": "the group database", "resource": "process limits", "posix": "POSIX calls",
    "crypt": "password hashing", "syslog": "the system log", "spwd": "the shadow password file",
    "readline": "line editing", "curses": "terminal screens", "pyinotify": "file watching",
    "dbus": "D-Bus", "gi": "GNOME libraries", "evdev": "input devices",
}
MODULE_FIX = {
    "fcntl": "On Windows use msvcrt.locking, or the filelock package for both.",
    "curses": "pip install windows-curses provides it on Windows.",
    "readline": "pyreadline3 provides it on Windows; or check for ImportError.",
    "pwd": "Use getpass.getuser() or os.path.expanduser('~') instead.",
}
OS_FUNCS = {
    "fork": "Use subprocess or multiprocessing instead.",
    "forkpty": "Use subprocess instead.",
    "setsid": "Use start_new_session=True in subprocess (ignored on Windows) instead.",
    "setpgid": "", "getpgid": "", "killpg": "Use proc.terminate() / proc.kill() instead.",
    "getuid": "Use getpass.getuser() for the user name.", "geteuid": "", "getgid": "", "getegid": "",
    "setuid": "", "setgid": "", "getgroups": "", "chown": "", "lchown": "", "mkfifo": "Use a socket or queue.",
    "statvfs": "Use shutil.disk_usage(path) for free space.", "uname": "Use platform.uname() instead.",
    "sync": "", "nice": "", "wait3": "", "wait4": "", "openpty": "", "ttyname": "", "getloadavg": "",
    "sched_setaffinity": "", "sched_getaffinity": "Use os.cpu_count() for the number of CPUs.",
}
LINUX_SIGNALS = {"SIGKILL", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2", "SIGALRM", "SIGCHLD", "SIGPIPE", "SIGSTOP",
                 "SIGCONT", "SIGTSTP", "SIGWINCH", "SIGTTIN", "SIGTTOU", "SIGXCPU", "SIGPROF", "SIGVTALRM"}
SIGNAL_FUNCS = {"alarm", "pause", "setitimer", "getitimer", "siginterrupt", "pthread_kill", "sigwait"}
LINUX_COMMANDS = {
    "xdg-open": "On Windows use os.startfile(path), or QDesktopServices.openUrl for both.",
    "xdg-email": "", "gio": "", "nautilus": "On Windows: explorer /select,\"file\".",
    "dbus-send": "D-Bus doesn't exist on Windows.", "gdbus": "D-Bus doesn't exist on Windows.",
    "notify-send": "Use QSystemTrayIcon.showMessage for both.", "zenity": "Use a Qt dialog instead.",
    "kdialog": "Use a Qt dialog instead.", "pkexec": "", "sudo": "", "apt": "", "apt-get": "", "dpkg": "",
    "snap": "", "systemctl": "", "gsettings": "", "xdotool": "", "xclip": "Use QApplication.clipboard().",
    "xsel": "Use QApplication.clipboard().", "lsblk": "", "blkid": "", "udisksctl": "", "mount": "",
    "umount": "", "lsusb": "", "nmcli": "", "pactl": "", "amixer": "", "aplay": "", "paplay": "",
    "bash": "Windows has no bash; run Python code or a .bat instead.", "sh": "Windows has no sh.",
    "which": "Use shutil.which(name) instead.", "killall": "", "pkill": "", "pgrep": "", "ps": "",
    "open": "",
}
CONSOLE_PROGRAMS = ("ffmpeg", "ffprobe", "ffplay", "git", "python", "pip", "node", "npm", "curl", "wget", "7z",
                    "yt-dlp", "youtube-dl", "magick", "convert", "sqlite3", "java", "dotnet", "cmd", "powershell",
                    "makensis", "handbrakecli", "mkvmerge", "mkvextract", "mediainfo", "exiftool", "adb", "gh")
LINUX_PATHS = re.compile(r"^(/usr/|/bin/|/sbin/|/etc/|/opt/|/tmp(/|$)|/proc/|/sys/|/dev/|/run/|/var/|/home/|/media/|"
                         r"/mnt/|~/\.config|~/\.local|~/\.cache|~/Desktop|~/Downloads|~/Videos|~/Music)")
PATH_FIX = {
    "/tmp": "Use tempfile.gettempdir() / tempfile.mkdtemp().",
    "~/.config": "Use QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation).",
    "~/.local": "Use QStandardPaths.writableLocation(QStandardPaths.AppDataLocation).",
    "~/.cache": "Use QStandardPaths.writableLocation(QStandardPaths.CacheLocation).",
    "/dev/null": "Use os.devnull or subprocess.DEVNULL.",
}
SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}


@dataclass
class Finding:
    level: str
    file: str          # relative to the project
    line: int
    message: str
    fix: str = ""
    code: str = ""


def project_files(root):
    """The project's .py files that go into the program (not tests, packaging, venvs...)."""
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")
                         and not os.path.exists(os.path.join(dirpath, d, "pyvenv.cfg")))
        for f in sorted(files):
            if f.endswith(".py") and not f.startswith("test_") and not f.endswith("_test.py"):
                out.append(os.path.join(dirpath, f))
        if len(out) > 800:
            break
    return out


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


_PLATFORM_WORDS = re.compile(r"sys\.platform|os\.name|platform\.system|win32|\bwin(dows)?\b|_win(dows)?\b|is_?win|\bnt\b|"
                             r"linux|posix|darwin|startfile|msvcrt", re.I)


def _is_platform_test(expr):
    try:
        return bool(_PLATFORM_WORDS.search(ast.unparse(expr)))
    except Exception:  # noqa: BLE001
        return False


def _ends_flow(body):
    return bool(body) and isinstance(body[-1], (ast.Return, ast.Raise, ast.Continue, ast.Break))


class _Checker(ast.NodeVisitor):
    def __init__(self, rel, lines):
        self.rel, self.lines, self.out = rel, lines, []
        self.parents = {}

    def run(self, tree):
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        self.visit(tree)
        return self.out

    # ---------------------------------------------------------------- is this code already platform-aware?
    def _guarded(self, node, for_import=False):
        cur = node
        while cur in self.parents:
            parent = self.parents[cur]
            if isinstance(parent, (ast.If, ast.IfExp, ast.While)) and _is_platform_test(parent.test):
                return True
            if isinstance(parent, ast.Try) and cur in parent.body and parent.handlers:
                return True
            if isinstance(parent, ast.FunctionDef) and _PLATFORM_WORDS.search(parent.name) and \
                    re.search(r"linux|posix|unix|die_with", parent.name, re.I):
                return True
            # an earlier  if WINDOWS: ... return  in the same block
            for field in ("body", "orelse", "finalbody"):
                block = getattr(parent, field, None)
                if isinstance(block, list) and cur in block:
                    for prev in block[:block.index(cur)]:
                        if isinstance(prev, ast.If) and _is_platform_test(prev.test) and \
                                (_ends_flow(prev.body) or _ends_flow(prev.orelse)):
                            return True
            cur = parent
        return False

    def add(self, node, level, message, fix=""):
        line = getattr(node, "lineno", 0)
        code = self.lines[line - 1].strip() if 0 < line <= len(self.lines) else ""
        if "wincheck: ignore" in code or "noqa: win" in code:
            return
        self.out.append(Finding(level, self.rel, line, message, fix, code))

    # ---------------------------------------------------------------- imports
    def _module(self, node, name):
        top = name.split(".")[0]
        if top in LINUX_MODULES and not self._guarded(node, True):
            self.add(node, PROBLEM, f"import {name}: Windows has no '{top}' module ({LINUX_MODULES[top]}), "
                                    "so the program stops right here on Windows.",
                     MODULE_FIX.get(top, "Import it only on Linux (if sys.platform != 'win32':) or inside "
                                         "try/except ImportError."))

    def visit_Import(self, node):
        for a in node.names:
            self._module(node, a.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.level == 0:
            self._module(node, node.module)
        self.generic_visit(node)

    # ---------------------------------------------------------------- calls
    def visit_Call(self, node):
        name = _dotted(node.func)
        short = name.rsplit(".", 1)[-1]
        for kw in node.keywords:
            if kw.arg == "preexec_fn" and not self._guarded(node):
                self.add(node, PROBLEM, "preexec_fn isn't supported on Windows: starting the program fails with "
                                        "'preexec_fn is not supported on Windows platforms'.",
                         "Pass it only on Linux, e.g. kw = {} if sys.platform == 'win32' else "
                         "{'preexec_fn': f}; Popen(..., **kw).")
        if name.startswith("os.") and short in OS_FUNCS and name.count(".") == 1 and not self._guarded(node):
            self.add(node, PROBLEM, f"os.{short}() doesn't exist on Windows (AttributeError).",
                     OS_FUNCS[short] or "Call it only on Linux (if sys.platform != 'win32':).")
        elif name.startswith("signal.") and short in SIGNAL_FUNCS and not self._guarded(node):
            self.add(node, PROBLEM, f"signal.{short}() doesn't exist on Windows.",
                     "Use a QTimer or threading.Timer instead." if short in ("alarm", "setitimer") else
                     "Call it only on Linux.")
        elif short in ("CDLL", "LoadLibrary", "cdll") and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str) and ".so" in node.args[0].value and not self._guarded(node):
            self.add(node, PROBLEM, f"Loads the Linux library {node.args[0].value!r}, which Windows doesn't have.",
                     "Windows libraries are DLLs (ctypes.WinDLL('kernel32')...): load the right one per platform.")
        elif short in SUBPROCESS_CALLS and (name.startswith("subprocess.") or name in SUBPROCESS_CALLS):
            self._subprocess(node, short)
        elif short == "startDetached" or (short in ("start", "execute") and "proc" in name.lower()):
            self._command_args(node, node.args[0] if node.args else None, qprocess=True)
        elif name in ("open", "io.open") or short in ("read_text", "write_text"):
            self._encoding(node, name, short)
        self.generic_visit(node)

    def _command_args(self, node, arg, qprocess=False):
        prog = None
        if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts and isinstance(arg.elts[0], ast.Constant):
            prog = arg.elts[0].value
        elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            prog = arg.value.split()[0] if arg.value.split() else None
        if not isinstance(prog, str):
            return None
        base = os.path.basename(prog).lower()
        if base in LINUX_COMMANDS and not self._guarded(node):
            self.add(node, WARNING, f"Runs '{base}', a Linux program Windows doesn't have: on Windows this fails "
                                    "(FileNotFoundError) or does nothing.",
                     LINUX_COMMANDS[base] or "Do this a different way on Windows, behind if sys.platform == 'win32':.")
        return base

    def _subprocess(self, node, short):
        arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "args"), None)
        base = self._command_args(node, arg)
        kws = {k.arg for k in node.keywords}
        if short in ("getoutput", "getstatusoutput") or "creationflags" in kws or None in kws or \
                "startupinfo" in kws or self._guarded(node):
            return
        # a console program started from a windowed program: Windows opens a console window for it
        text = ""
        try:
            text = ast.unparse(arg).lower() if arg is not None else ""
            if isinstance(arg, ast.Name):              # cmd = [ff.FFMPEG, ...]; Popen(cmd): look at how cmd is built
                text += " " + " ".join(ast.unparse(v).lower() for v in self._assigned(node, arg.id))
        except Exception:  # noqa: BLE001
            pass
        if (base and any(base.startswith(c) for c in CONSOLE_PROGRAMS)) or \
                any(re.search(rf"\b{re.escape(c)}\b", text) for c in CONSOLE_PROGRAMS):
            self.add(node, NOTE, "On Windows a console window pops up each time this runs (the published program "
                                 "has no console of its own).",
                     "Add creationflags=subprocess.CREATE_NO_WINDOW on Windows: "
                     "**({'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}).")

    def _assigned(self, node, var):
        """The values assigned to var in the function (or module) around node."""
        scope = node
        while scope in self.parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = self.parents[scope]
        out = []
        for n in ast.walk(scope):
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == var for t in n.targets):
                out.append(n.value)
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)) and isinstance(n.target, ast.Name) and \
                    n.target.id == var and n.value is not None:
                out.append(n.value)
        return out

    def _encoding(self, node, name, short):
        kws = {k.arg for k in node.keywords}
        if "encoding" in kws or None in kws:
            return
        if name in ("open", "io.open"):
            mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"),
                                                               None)
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
                return
            if mode is not None and not isinstance(mode, ast.Constant):
                return
            if len(node.args) > 3:
                return           # buffering, encoding given by position
        elif short == "read_text" and node.args or short == "write_text" and len(node.args) > 1:
            return
        self.add(node, NOTE, f"{short}() without encoding= uses Windows' own encoding (cp1252) there, so files with "
                             "accents, emoji or other non-English text can fail with UnicodeDecodeError or come "
                             "out garbled.", 'Add encoding="utf-8".')

    # ---------------------------------------------------------------- names and strings
    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and node.value.id == "signal" and node.attr in LINUX_SIGNALS \
                and not self._guarded(node):
            self.add(node, PROBLEM, f"signal.{node.attr} doesn't exist on Windows (AttributeError).",
                     "Use proc.kill() / proc.terminate() (both work on Windows), or signal.SIGTERM.")
        self.generic_visit(node)

    def visit_Constant(self, node):
        v = node.value
        if isinstance(v, str) and len(v) < 300 and LINUX_PATHS.match(v) and not self._guarded(node):
            parent = self.parents.get(node)
            if not isinstance(parent, ast.Expr):           # not a docstring
                key = next((k for k in PATH_FIX if v.startswith(k)), None)
                if isinstance(parent, (ast.Tuple, ast.List)) and len(parent.elts) > 1:
                    self.add(node, NOTE, f"Linux folder {v!r} in a list of places to look: fine on Windows as long "
                                         "as the code falls back to something else there (shutil.which...).",
                             "Nothing to do if there's a fallback.")
                    return self.generic_visit(node)
                self.add(node, WARNING if not v.startswith("~") else NOTE,
                         f"Linux folder {v!r}: it doesn't exist on Windows.",
                         PATH_FIX.get(key, "Build the path per platform, or use QStandardPaths / tempfile / "
                                           "os.path.expanduser('~')."))
        elif isinstance(v, str) and v == "preexec_fn" and isinstance(self.parents.get(node), ast.Dict) and \
                not self._guarded(node):
            self.add(node, PROBLEM, "preexec_fn isn't supported on Windows.",
                     "Add it only on Linux (if sys.platform != 'win32':).")
        self.generic_visit(node)


def check_source(source, rel="file.py"):
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [Finding(NOTE, rel, e.lineno or 0, f"Couldn't read this file: {e.msg}")]
    return _Checker(rel, source.splitlines()).run(tree)


def check_project(root):
    out = []
    for path in project_files(root):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        out += check_source(src, os.path.relpath(path, root).replace(os.sep, "/"))
    order = {PROBLEM: 0, WARNING: 1, NOTE: 2}
    out.sort(key=lambda f: (order[f.level], f.file, f.line))
    return out


def summary(findings):
    n = {lvl: sum(1 for f in findings if f.level == lvl) for lvl in (PROBLEM, WARNING, NOTE)}
    if not findings:
        return "No Windows problems found."
    parts = [f"{n[PROBLEM]} problem{'s' * (n[PROBLEM] != 1)}" if n[PROBLEM] else "",
             f"{n[WARNING]} warning{'s' * (n[WARNING] != 1)}" if n[WARNING] else "",
             f"{n[NOTE]} note{'s' * (n[NOTE] != 1)}" if n[NOTE] else ""]
    return ", ".join(p for p in parts if p)


class WindowsCheckDialog(QDialog):
    """The findings, grouped by level. Double-click opens the line in the editor."""
    COLORS = {PROBLEM: "#f14c4c", WARNING: "#cca700", NOTE: "#75beff"}

    def __init__(self, root, findings=None, open_at=None, parent=None):
        super().__init__(parent)
        self.root, self.open_at = root, open_at
        self.findings = check_project(root) if findings is None else findings
        self.setWindowTitle("Windows Check")
        self.resize(980, 560)
        v = QVBoxLayout(self)
        head = QLabel(f"<b>{summary(self.findings)}</b> &nbsp; Problems stop the program on Windows; warnings do "
                      "nothing there; notes work but look wrong. Double-click a line to open it. To hide one you've "
                      "checked, add <i># wincheck: ignore</i> at the end of that line.")
        head.setWordWrap(True)
        v.addWidget(head)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "File", "Line", "What happens on Windows", "Fix"])
        self.tree.setRootIsDecorated(True)
        self.tree.setWordWrap(True)
        self.tree.setAlternatingRowColors(True)
        groups = {}
        for f in self.findings:
            if f.level not in groups:
                g = QTreeWidgetItem([f"{f.level}s", "", "", "", ""])
                g.setForeground(0, Qt.GlobalColor.white)
                g.setFirstColumnSpanned(False)
                self.tree.addTopLevelItem(g)
                g.setExpanded(f.level != NOTE or len(self.findings) < 40)
                groups[f.level] = g
            it = QTreeWidgetItem(["●", f.file, str(f.line), f.message, f.fix])
            from PySide6.QtGui import QColor
            it.setForeground(0, QColor(self.COLORS[f.level]))
            it.setToolTip(3, f"{f.message}\n\n{f.code}")
            it.setToolTip(4, f.fix)
            it.setData(0, Qt.UserRole, f)
            groups[f.level].addChild(it)
        for lvl, g in groups.items():
            g.setText(0, f"{lvl}s ({g.childCount()})")
        hdr = self.tree.header()
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.setColumnWidth(0, 120)
        self.tree.setColumnWidth(1, 200)
        self.tree.setColumnWidth(2, 50)
        self.tree.setColumnWidth(4, 300)
        self.tree.itemDoubleClicked.connect(self._open)
        v.addWidget(self.tree, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _open(self, item, _col):
        f = item.data(0, Qt.UserRole)
        if f and self.open_at:
            self.open_at(os.path.join(self.root, f.file), f.line)
