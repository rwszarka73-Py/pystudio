"""IntelliSense: Jedi (completion, signatures, hover, go to definition, references)
and pyflakes (live error checking). Jedi runs on one background thread so typing
never waits on it."""
import itertools
import os
import sys

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

try:
    import jedi
except ImportError:          # PyStudio still works, just without IntelliSense
    jedi = None
try:
    from pyflakes import api as _pyflakes_api
except ImportError:
    _pyflakes_api = None

ERROR_KINDS = {"UndefinedName", "UndefinedLocal", "UndefinedExport", "DuplicateArgument",
               "ReturnOutsideFunction", "YieldOutsideFunction", "ContinueOutsideLoop",
               "BreakOutsideLoop", "DefaultExceptNotLast", "TwoStarredExpressions",
               "TooManyExpressionsInStarredAssignment", "ForwardAnnotationSyntaxError",
               "SyntaxError"}
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "publish", "build", "dist", "node_modules", "site-packages"}


class Diagnostic:
    __slots__ = ("path", "line", "col", "severity", "code", "message")

    def __init__(self, path, line, col, severity, code, message):
        self.path, self.line, self.col = path, line, col
        self.severity, self.code, self.message = severity, code, message


# ============================================================ pyflakes
class _Reporter:
    def __init__(self, path):
        self.path = path
        self.items = []

    def unexpectedError(self, filename, msg):
        self.items.append(Diagnostic(self.path, 1, 0, "error", "Error", str(msg)))

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.items.append(Diagnostic(self.path, lineno or 1, max(0, (offset or 1) - 1),
                                     "error", "SyntaxError", f"Syntax error: {msg}"))

    def flake(self, m):
        kind = type(m).__name__
        sev = "error" if kind in ERROR_KINDS else "warning"
        self.items.append(Diagnostic(self.path, m.lineno, getattr(m, "col", 0) or 0, sev, kind,
                                     m.message % m.message_args))


def check_source(code, path):
    """Errors/warnings for one file's text (fast; safe on the UI thread)."""
    if _pyflakes_api is None:
        try:
            compile(code, path or "<editor>", "exec")
            return []
        except SyntaxError as e:
            return [Diagnostic(path, e.lineno or 1, max(0, (e.offset or 1) - 1), "error",
                               "SyntaxError", f"Syntax error: {e.msg}")]
    r = _Reporter(path)
    _pyflakes_api.check(code, path or "<editor>", r)
    return r.items


def is_generated(path):
    name = os.path.basename(path)
    return (name.startswith("ui_") and name.endswith(".py")) or name in ("pystudio_runtime.py", "my.py")


MAX_PROJECT_FILES = 2000     # a safety limit: a project folder is never this big


def project_python_files(root, limit=MAX_PROJECT_FILES, skip_nested=True):
    """A project's own .py files. Projects nested inside it (other projects of a solution) are skipped."""
    n = 0
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if skip_nested and dirpath != root:
            if any(f.endswith(".pyproj") for f in files):
                dirnames[:] = []
                continue
        for f in files:
            p = os.path.join(dirpath, f)
            if f.endswith(".py") and not is_generated(p):
                yield p
                n += 1
                if n >= limit:
                    return


# ============================================================ jedi worker
class _Relay(QObject):
    done = Signal(int, object)


class _Job(QRunnable):
    def __init__(self, svc, req_id, kind, fn):
        super().__init__()
        self.svc, self.req_id, self.kind, self.fn = svc, req_id, kind, fn

    def run(self):
        if self.svc._latest.get(self.kind, self.req_id) != self.req_id:
            self._emit(None)   # replaced by a newer request
            return
        try:
            result = self.fn()
        except Exception:  # noqa: BLE001 - Jedi can fail on half-typed code; just no result
            result = None
        self._emit(result)

    def _emit(self, result):
        try:
            self.svc._relay.done.emit(self.req_id, result)
        except RuntimeError:   # PyStudio closed while this was running
            pass


class LanguageService(QObject):
    """Usage: svc.request(kind, code, path, line, col, context, callback).
    line is 1-based, col 0-based (Jedi's convention). callback runs on the UI thread."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.available = jedi is not None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)      # Jedi isn't thread-safe; one worker, in order
        self._ids = itertools.count(1)
        self._callbacks = {}
        self._latest = {}
        self._projects = {}
        self._relay = _Relay()
        self._relay.done.connect(self._deliver)

    def _deliver(self, req_id, result):
        cb = self._callbacks.pop(req_id, None)
        if cb is not None and result is not None:
            cb(result)

    extra_paths = {}         # project root -> [referenced project roots] (set by MainWindow)

    def _project(self, root, python):
        extra = list(self.extra_paths.get(root, []))
        key = (root, python, tuple(extra))
        if key not in self._projects:
            kwargs = {"added_sys_path": [root] + extra} if root else {}
            if python and os.path.exists(python) and os.path.abspath(python) != os.path.abspath(sys.executable):
                kwargs["environment_path"] = python
            try:
                self._projects[key] = jedi.Project(root or os.getcwd(), **kwargs)
            except Exception:  # noqa: BLE001 - bad interpreter: fall back to PyStudio's
                self._projects[key] = jedi.Project(root or os.getcwd(), added_sys_path=[root] + extra if root else [])
        return self._projects[key]

    def _script(self, code, path, ctx):
        root, python = ctx
        return jedi.Script(code, path=path or None, project=self._project(root, python))

    def request(self, kind, code, path, line, col, ctx, callback):
        if not self.available:
            return
        req_id = next(self._ids)
        self._latest[kind] = req_id
        self._callbacks[req_id] = callback
        fn = getattr(self, "_do_" + kind)
        self.pool.start(_Job(self, req_id, kind, lambda: fn(code, path, line, col, ctx)))

    def request_rename(self, code, path, line, col, ctx, new_name, callback):
        if not self.available:
            return
        req_id = next(self._ids)
        self._latest["rename"] = req_id
        self._callbacks[req_id] = callback

        def fn():
            try:
                ref = self._script(code, path, ctx).rename(line, col, new_name=new_name)
                out = {}
                for p, changed in ref.get_changed_files().items():
                    out[str(p)] = changed.get_new_code()
                return out
            except Exception as e:  # noqa: BLE001
                return {"__error__": str(e)}
        self.pool.start(_Job(self, req_id, "rename", fn))

    def request_extract(self, kind, code, path, start, end, ctx, new_name, callback):
        """Extract Method ("function") / Extract Variable ("variable") for the selection
        start..end ((line, col) pairs, Jedi coordinates). callback({path: new_code}) or {"__error__"}."""
        if not self.available:
            return
        req_id = next(self._ids)
        self._latest["extract"] = req_id
        self._callbacks[req_id] = callback

        def fn():
            try:
                script = self._script(code, path, ctx)
                f = script.extract_function if kind == "function" else script.extract_variable
                ref = f(start[0], start[1], until_line=end[0], until_column=end[1], new_name=new_name)
                return {str(p): ch.get_new_code() for p, ch in ref.get_changed_files().items()}
            except Exception as e:  # noqa: BLE001 - Jedi explains what can't be extracted
                return {"__error__": str(e)}
        self.pool.start(_Job(self, req_id, "extract", fn))

    def warmup(self, ctx):
        """Load PySide6 type info in the background so the first completion is quick."""
        if not self.available:
            return
        code = ("from PySide6.QtWidgets import QLabel, QPushButton, QLineEdit, QMainWindow\n"
                "QLabel().setT")
        self.request("warmup", code, None, 2, len("QLabel().setT"), ctx, lambda _r: None)

    # ------------------------------------------------------------ jobs
    def _do_warmup(self, code, path, line, col, ctx):
        self._script(code, path, ctx).complete(line, col)
        return True

    def _do_complete(self, code, path, line, col, ctx):
        out = []
        for c in self._script(code, path, ctx).complete(line, col):
            out.append((c.name, c.type, c.name_with_symbols))
        return out

    def _do_signatures(self, code, path, line, col, ctx):
        out = []
        for s in self._script(code, path, ctx).get_signatures(line, col):
            params = [p.to_string() for p in s.params]
            out.append((s.name, params, s.index, (s.docstring(raw=True) or "").strip()))
        return out

    def _do_hover(self, code, path, line, col, ctx):
        script = self._script(code, path, ctx)
        names = script.help(line, col) or script.infer(line, col)
        if not names:
            return ""
        n = names[0]
        try:
            head = n.description
        except Exception:  # noqa: BLE001
            head = n.name
        try:
            types = script.infer(line, col)
            t = types[0].name if types and types[0].type not in ("function", "class") else ""
            if t == n.name:
                t = ""
        except Exception:  # noqa: BLE001
            t = ""
        doc = (n.docstring() or "").strip()
        return {"head": head, "type": t, "full": n.full_name or "", "doc": doc}

    def _do_goto(self, code, path, line, col, ctx):
        script = self._script(code, path, ctx)
        res = script.goto(line, col, follow_imports=True) or script.infer(line, col)
        out = []
        for d in res:
            out.append((str(d.module_path) if d.module_path else None, d.line, d.column,
                        d.full_name or d.name, d.in_builtin_module()))
        return out

    def _do_references(self, code, path, line, col, ctx):
        script = self._script(code, path, ctx)
        out = []
        for r in script.get_references(line, col, scope="project"):
            mp = str(r.module_path) if r.module_path else None
            if mp and (is_generated(mp) or "site-packages" in mp):
                continue
            try:
                text = r.get_line_code().strip()
            except Exception:  # noqa: BLE001
                text = ""
            out.append((mp, r.line, r.column, text))
        return out
