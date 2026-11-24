"""Ruff and type checking for the Error List and the editor's squiggles.

Ruff (as you type, in the background): likely bugs and mistakes pyflakes doesn't look for - a mutable
default argument (def f(x=[])), a bare except:, comparing to None with ==, a loop variable used in a
lambda after the loop, an invalid escape in a string... Its fixes show in the lightbulb (Ctrl+.).
pyflakes still reports what it always did; Ruff's own copies of those (the F rules) are left out so
nothing shows twice. A project with its own Ruff settings (ruff.toml, or [tool.ruff] in pyproject.toml)
uses them instead of PyStudio's rule list.

Type checking (mypy, when you save and when a file opens): calling a function with the wrong kind of
argument, a misspelled attribute or method, using something that may be None... Type findings are
warnings: they never stop F5. Forms loaded with uic.loadUi / QUiLoader create their controls while the
program runs, so in those files "has no attribute" findings are left out.

Both are optional: Tools > Options turns them on and off; install.sh installs them."""
import importlib.util
import json
import os
import re
import shutil
import sys

from PySide6.QtCore import QObject, QProcess, QSettings, QTimer

DEFAULT_RULES = "E7,E9,W6,B,PLE"
ERROR_RULES = ("E9", "PLE")
CONFIG_FILES = ("ruff.toml", ".ruff.toml")


class Diag:
    """Same shape as language.Diagnostic, plus a Ruff fix."""

    def __init__(self, path, line, col, severity, code, message, fix=None, source=""):
        self.path, self.line, self.col = path, line, col
        self.severity, self.code, self.message = severity, code, message
        self.fix, self.source = fix, source


# ------------------------------------------------------------ settings
def ruff_enabled():
    return QSettings().value("ruffEnabled", True, type=bool)


def types_enabled():
    return QSettings().value("typeCheckEnabled", True, type=bool)


def ruff_rules():
    return (QSettings().value("ruffRules", DEFAULT_RULES) or DEFAULT_RULES).replace(" ", "")


def check_untyped():
    return QSettings().value("typeCheckUntyped", True, type=bool)


_python_for_types = None


def set_python(path):
    """The project's interpreter: mypy looks up its installed packages."""
    global _python_for_types
    _python_for_types = path if path and os.path.exists(path) else None


# ------------------------------------------------------------ tools
def ruff_exe():
    here = os.path.join(os.path.dirname(sys.executable), "ruff")
    if os.access(here, os.X_OK):
        return here
    return shutil.which("ruff")


def mypy_cmd():
    if importlib.util.find_spec("mypy") is not None:
        return [sys.executable, "-m", "mypy"]
    exe = shutil.which("mypy")
    return [exe] if exe else None


def has_ruff_config(path, root=None):
    """A ruff.toml or [tool.ruff] between the file and the project root (or the filesystem root)."""
    d = os.path.dirname(os.path.abspath(path))
    root = os.path.abspath(root) if root else None
    while True:
        for name in CONFIG_FILES:
            if os.path.isfile(os.path.join(d, name)):
                return True
        pp = os.path.join(d, "pyproject.toml")
        if os.path.isfile(pp):
            try:
                with open(pp, encoding="utf-8", errors="replace") as f:
                    if "[tool.ruff" in f.read():
                        return True
            except OSError:
                pass
        if d == root or os.path.dirname(d) == d:
            return False
        d = os.path.dirname(d)


def ruff_args(path, root=None):
    args = ["check", "--output-format=json", "--no-cache", "--exit-zero", "--quiet"]
    if path:
        args += ["--stdin-filename", path]
    if not (path and has_ruff_config(path, root)):
        args += ["--isolated", "--select", ruff_rules()]
    args += ["--extend-ignore", "F", "-"]     # pyflakes already reports the F rules
    return args


def parse_ruff(output, path):
    try:
        data = json.loads(output or "[]")
    except ValueError:
        return []
    out = []
    for d in data:
        code = d.get("code") or "Ruff"
        if code.startswith("F") and code[1:].isdigit():
            continue
        loc = d.get("location") or {}
        fix = d.get("fix")
        if d.get("code") is None:            # Ruff's own syntax errors: pyflakes reports those
            continue
        sev = "error" if code.startswith(ERROR_RULES) else "warning"
        out.append(Diag(path, loc.get("row", 1), max(0, loc.get("column", 1) - 1), sev, f"Ruff {code}",
                        f"{d.get('message', '')} [{code}]", fix if fix and fix.get("edits") else None, "ruff"))
    return out


_MYPY_LINE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<kind>error|warning|note):\s*"
                        r"(?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$")


def parse_mypy(output, path, skip_codes=(), cwd=None):
    """mypy's lines for this file (it prints names relative to the folder it ran in: cwd)."""
    target = os.path.realpath(path)
    out = []
    for line in (output or "").splitlines():
        m = _MYPY_LINE.match(line.strip())
        if not m or m.group("kind") == "note":
            continue
        f = m.group("file")
        if not os.path.isabs(f) and cwd:
            f = os.path.join(cwd, f)
        if os.path.realpath(f) != target:
            continue
        code = m.group("code") or "type"
        if code in skip_codes:
            continue
        out.append(Diag(path, int(m.group("line")), max(0, int(m.group("col") or 1) - 1), "warning",
                        f"Type {code}", f"{m.group('msg')} [type: {code}]", None, "mypy"))
    return out


def mypy_args(path, text="", python=None):
    cache = os.path.join(os.path.expanduser("~"), ".cache", "pystudio", "mypy")
    args = ["--show-column-numbers", "--show-error-codes", "--no-error-summary", "--no-color-output",
            "--hide-error-context", "--no-pretty", "--ignore-missing-imports", "--follow-imports=silent",
            "--cache-dir", cache, "--incremental"]
    if check_untyped():
        args.append("--check-untyped-defs")
    py = python or _python_for_types
    if py and os.path.exists(py) and os.path.realpath(py) != os.path.realpath(sys.executable):
        args += ["--python-executable", py]
    return args + [path]


def loads_ui_at_runtime(text):
    return bool(re.search(r"\bloadUi\s*\(|QUiLoader\b", text or ""))


# ------------------------------------------------------------ fixes (lightbulb)
def apply_fix(source, fix):
    """Source with a Ruff fix's edits applied (rows/columns are 1-based)."""
    lines = source.split("\n")
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln) + 1)

    def offset(loc):
        r = max(1, loc.get("row", 1))
        if r > len(lines):
            return len(source)
        return min(starts[r - 1] + max(0, loc.get("column", 1) - 1), len(source))
    edits = sorted(fix.get("edits", []), key=lambda e: offset(e["location"]), reverse=True)
    for e in edits:
        a, b = offset(e["location"]), offset(e["end_location"])
        source = source[:a] + (e.get("content") or "") + source[b:]
    return source


def fixes_for(diag, source):
    fix = getattr(diag, "fix", None)
    if not fix:
        return []
    title = fix.get("message") or "Apply Ruff's fix"
    unsafe = fix.get("applicability") == "unsafe"
    try:
        return [(f"{title}{'  (check the result)' if unsafe else ''}  [{diag.code.replace('Ruff ', '')}]",
                 apply_fix(source, fix))]
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------ the background runner (one per editor)
class Analyzer(QObject):
    """Runs Ruff (debounced, on each change) and mypy (on save/open) for one editor; calls
    editor.set_external_diagnostics(kind, [Diag])."""

    def __init__(self, editor):
        super().__init__(editor)
        self.ed = editor
        self.ruff_proc = None
        self.mypy_proc = None
        self._ruff_again = False
        self._mypy_again = False
        self._ruff_rev = -1
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

    def _root(self):
        return getattr(self.ed, "project_root", None)

    # Ruff
    def run_ruff(self):
        exe = ruff_exe()
        path = self.ed.path or ""
        if not exe or not ruff_enabled() or (path and not path.endswith((".py", ".pyw"))):
            if self.ed._ext.get("ruff"):
                self.ed.set_external_diagnostics("ruff", [])
            return
        if self.ruff_proc is not None and self.ruff_proc.state() != QProcess.NotRunning:
            self._ruff_again = True
            return
        self._ruff_rev = self.ed.document().revision()
        p = QProcess(self)
        self.ruff_proc = p
        if path:
            p.setWorkingDirectory(os.path.dirname(os.path.abspath(path)))
        p.finished.connect(lambda *_a, p=p, rev=self._ruff_rev: self._ruff_done(p, rev))
        p.errorOccurred.connect(lambda _e, p=p: self._ruff_failed(p))
        p.start(exe, ruff_args(path or None, self._root()))
        p.write(self.ed.toPlainText().encode("utf-8"))
        p.closeWriteChannel()

    def _ruff_failed(self, p):
        if p is self.ruff_proc:
            self.ruff_proc = None

    def _ruff_done(self, p, rev):
        try:
            self._ruff_finish(p, rev)
        except RuntimeError:            # PyStudio or the editor is closing
            pass

    def _ruff_finish(self, p, rev):
        out = bytes(p.readAllStandardOutput()).decode("utf-8", "replace")
        if p is self.ruff_proc:
            self.ruff_proc = None
        p.deleteLater()
        if rev == self.ed.document().revision():
            self.ed.set_external_diagnostics("ruff", parse_ruff(out, self.ed.path or ""))
        if self._ruff_again or rev != self.ed.document().revision():
            self._ruff_again = False
            QTimer.singleShot(0, self, self.run_ruff)

    # mypy
    def run_mypy(self):
        cmd = mypy_cmd()
        path = self.ed.path
        if not cmd or not types_enabled() or not path or not path.endswith((".py", ".pyw")) or \
                not os.path.isfile(path):
            if self.ed._ext.get("mypy"):
                self.ed.set_external_diagnostics("mypy", [])
            return
        if self.mypy_proc is not None and self.mypy_proc.state() != QProcess.NotRunning:
            self._mypy_again = True
            return
        text = self.ed.toPlainText()
        skip = ("attr-defined", "union-attr") if loads_ui_at_runtime(text) else ()
        p = QProcess(self)
        self.mypy_proc = p
        root = self._root() or os.path.dirname(os.path.abspath(path))
        p.setWorkingDirectory(root)
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.finished.connect(lambda *_a, p=p, skip=skip, root=root: self._mypy_done(p, skip, root))
        p.errorOccurred.connect(lambda _e, p=p: self._mypy_failed(p))
        try:
            python = self.ed.context()[1] if self.ed.context else None
        except Exception:  # noqa: BLE001
            python = None
        p.start(cmd[0], cmd[1:] + mypy_args(os.path.abspath(path), text, python))

    def _mypy_failed(self, p):
        if p is self.mypy_proc:
            self.mypy_proc = None

    def _mypy_done(self, p, skip, cwd=None):
        try:
            self._mypy_finish(p, skip, cwd)
        except RuntimeError:            # PyStudio or the editor is closing
            pass

    def _mypy_finish(self, p, skip, cwd=None):
        out = bytes(p.readAllStandardOutput()).decode("utf-8", "replace")
        if p is self.mypy_proc:
            self.mypy_proc = None
        p.deleteLater()
        self.ed.set_external_diagnostics("mypy", parse_mypy(out, self.ed.path or "", skip, cwd))
        if self._mypy_again:
            self._mypy_again = False
            QTimer.singleShot(0, self, self.run_mypy)

    def stop(self):
        for p in (self.ruff_proc, self.mypy_proc):
            try:
                if p is not None and p.state() != QProcess.NotRunning:
                    p.blockSignals(True)
                    p.kill()
                    p.waitForFinished(500)
            except RuntimeError:
                pass
        self.ruff_proc = self.mypy_proc = None


def check_file(path, text, root=None, with_types=False, timeout=30):
    """Ruff (and mypy) for a file that isn't open - the Error List's whole-project scan. Synchronous."""
    import subprocess
    out = []
    exe = ruff_exe()
    if exe and ruff_enabled():
        try:
            r = subprocess.run([exe] + ruff_args(path, root), input=text, capture_output=True, text=True,
                               timeout=timeout, cwd=os.path.dirname(os.path.abspath(path)))
            out += parse_ruff(r.stdout, path)
        except (OSError, subprocess.SubprocessError):
            pass
    if with_types:
        cmd = mypy_cmd()
        if cmd and types_enabled():
            try:
                cwd = root or os.path.dirname(os.path.abspath(path))
                r = subprocess.run(cmd + mypy_args(os.path.abspath(path), text), capture_output=True, text=True,
                                   timeout=timeout * 4, cwd=cwd)
                skip = ("attr-defined", "union-attr") if loads_ui_at_runtime(text) else ()
                out += parse_mypy(r.stdout, path, skip, cwd)
            except (OSError, subprocess.SubprocessError):
                pass
    return out


def check_project_ruff(root, timeout=30):
    """{path: [Diag]} for a whole project in one Ruff run (the Error List's project scan)."""
    import subprocess
    exe = ruff_exe()
    if not exe or not ruff_enabled() or not root:
        return {}
    args = ["check", root, "--output-format=json", "--no-cache", "--exit-zero", "--quiet"]
    if not has_ruff_config(os.path.join(root, "x.py"), root):
        args += ["--isolated", "--select", ruff_rules()]
    args += ["--extend-ignore", "F"]
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout, cwd=root)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    try:
        data = json.loads(r.stdout or "[]")
    except ValueError:
        return {}
    for d in data:
        path = os.path.abspath(d.get("filename", ""))
        out.setdefault(path, [])
        out[path] += parse_ruff(json.dumps([d]), path)
    return out
