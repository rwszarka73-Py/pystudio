"""Package updates, like NuGet's Updates tab in Visual Studio: PyStudio checks the project's interpreter for
newer versions on PyPI in the background when a project opens (once a day), shows "N package updates" in the
status bar, and lists them in Project > Manage Packages > Updates.

Only packages you installed yourself count (pip list --not-required); packages another package pulled in
(dependencies) and pip's own tools are listed under "Show dependencies too"."""
import hashlib
import json
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QSettings, QTimer, Signal

TOOLS = {"pip", "setuptools", "wheel"}
MAX_AGE = 20 * 3600          # automatic checks: at most about once a day per interpreter


def _norm(name):
    import re
    return re.sub(r"[-_.]+", "-", name).lower()


def _json_list(out):
    try:
        return json.loads(out[out.index("["):out.rindex("]") + 1]) if "[" in out else []
    except ValueError:
        return []


def major(v):
    try:
        return int(str(v).split(".")[0])
    except ValueError:
        return None


def is_major(row):
    a, b = major(row.get("version")), major(row.get("latest"))
    return a is not None and b is not None and b > a


def counted(rows):
    """The updates that count (badge, tab title): packages you installed, not pip's own tools."""
    return [r for r in rows if r.get("top") and _norm(r["name"]) not in TOOLS]


def _key(python):
    return "pkgUpdates/" + hashlib.sha1(python.encode()).hexdigest()[:16]


def cached(python):
    """(rows, time checked) from the last check of this interpreter, or (None, 0)."""
    if not python:
        return None, 0
    try:
        d = json.loads(QSettings().value(_key(python), "") or "{}")
        return d.get("rows"), float(d.get("time", 0))
    except (ValueError, TypeError):
        return None, 0


def store(python, rows):
    QSettings().setValue(_key(python), json.dumps({"time": time.time(), "rows": rows}))


def auto_check_on():
    return QSettings().value("pkgUpdateAutoCheck", True, type=bool)


class UpdateCheck(QObject):
    """pip list --not-required (local, quick), then pip list --outdated (asks PyPI). done(rows) or failed(text).
    rows: [{name, version, latest, top}] - top = you installed it (not only a dependency)."""
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self.python = None
        self._top = set()
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.cancel)

    @property
    def busy(self):
        return self.proc is not None

    def start(self, python):
        if self.busy or not python:
            return False
        self.python = python
        self._run(["-m", "pip", "list", "--not-required", "--format", "json", "--disable-pip-version-check"],
                  self._got_top)
        return True

    def cancel(self):
        if self.proc is not None:
            p, self.proc = self.proc, None
            try:
                p.kill()
                p.waitForFinished(2000)
                p.deleteLater()
            except RuntimeError:
                pass

    def _run(self, args, then):
        p = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PIP_NO_INPUT", "1")
        p.setProcessEnvironment(env)
        self.proc = p

        def fin(code, _s):
            try:
                if self.proc is not p:          # canceled
                    return
                out = bytes(p.readAllStandardOutput()).decode("utf-8", "replace")
                err = bytes(p.readAllStandardError()).decode("utf-8", "replace")
                self.proc = None
                p.deleteLater()
                then(code, out, err)
            except RuntimeError:                # PyStudio is closing: the process is already gone
                pass
        p.finished.connect(fin)
        p.errorOccurred.connect(lambda e: e == QProcess.FailedToStart and fin(1, None))
        p.start(self.python, args)

    def _got_top(self, code, out, _err):
        self._top = {_norm(r["name"]) for r in _json_list(out)} if code == 0 else set()
        self._run(["-m", "pip", "list", "--outdated", "--format", "json", "--disable-pip-version-check"],
                  self._got_outdated)

    def _got_outdated(self, code, out, err):
        if code != 0:
            msg = (err or out).strip().splitlines()
            self.failed.emit((msg[-1] if msg else "pip failed") + " (an internet connection is needed)")
            return
        rows = [{"name": r["name"], "version": r.get("version", ""), "latest": r.get("latest_version", ""),
                 "top": (_norm(r["name"]) in self._top) if self._top else True}
                for r in sorted(_json_list(out), key=lambda r: r["name"].lower())]
        store(self.python, rows)
        self.done.emit(rows)


class StatusBadge(QObject):
    """The status-bar button "📦 3 package updates" and the Project menu's count, for the main window."""

    def __init__(self, window):
        super().__init__(window)
        from PySide6.QtWidgets import QToolButton
        self.win = window
        self.check = UpdateCheck(self)
        self.check.done.connect(self.show_rows)
        self.button = QToolButton()
        self.button.setAutoRaise(True)
        self.button.setStyleSheet("color: #ffd166;")
        self.button.setToolTip("Newer versions of packages this project uses are on PyPI - click to see them")
        self.button.clicked.connect(self.open_updates)
        self.button.hide()
        window.statusBar().addPermanentWidget(self.button)
        self.python = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._auto)

    def project_changed(self, python):
        """A project became active (python = its interpreter) or closed (None)."""
        self.check.cancel()
        from . import interp
        if python and interp.externally_managed(python):
            python = None              # Ubuntu's own Python: Ubuntu's updates handle its packages, not pip
        self.python = python
        rows, _t = cached(python)
        self.show_rows(rows or [])
        if python and auto_check_on():
            self._timer.start(8000)        # after the project has settled in
        else:
            self._timer.stop()

    def _auto(self):
        _rows, t = cached(self.python)
        if self.python and time.time() - t > MAX_AGE:
            self.check.start(self.python)

    def show_rows(self, rows):
        n = len(counted(rows))
        self.button.setText(f"📦 {n} package update{'s' if n != 1 else ''}")
        self.button.setVisible(n > 0 and self.python is not None)
        a = getattr(self.win, "a_packages", None)
        if a is not None:
            a.setText(f"Manage &Packages... ({n} update{'s' if n != 1 else ''})" if n and self.python
                      else "Manage &Packages...")

    def refresh_from_cache(self):
        rows, _t = cached(self.python)
        self.show_rows(rows or [])

    def open_updates(self):
        self.win.project_properties("Packages", tab="Updates")
