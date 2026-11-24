"""Output panel and the process runner (F5 / Shift+F5)."""
import os
import re
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit
from . import theme

TRACEBACK_RE = re.compile(r'File "(.+?)", line (\d+)')


class OutputPanel(QPlainTextEdit):
    """Read-only console. Double-click a traceback line to jump to the code."""

    location_activated = Signal(str, int)  # path, line

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(20000)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSize(10)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.setStyleSheet("QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; border: 0; }")

        self.fmt_out = QTextCharFormat()
        self.fmt_out.setForeground(QColor(theme.c("#d4d4d4")))
        self.fmt_err = QTextCharFormat()
        self.fmt_err.setForeground(QColor(theme.c("#f48771")))
        self.fmt_info = QTextCharFormat()
        self.fmt_info.setForeground(QColor(theme.c("#569cd6")))

    def write(self, text, kind="out"):
        fmt = {"out": self.fmt_out, "err": self.fmt_err, "info": self.fmt_info}[kind]
        c = self.textCursor()
        c.movePosition(QTextCursor.End)
        c.insertText(text, fmt)
        self.setTextCursor(c)
        self.ensureCursorVisible()

    def mouseDoubleClickEvent(self, event):
        line = self.cursorForPosition(event.position().toPoint()).block().text()
        m = TRACEBACK_RE.search(line)
        if m and os.path.isfile(m.group(1)):
            self.location_activated.emit(m.group(1), int(m.group(2)))
            return
        super().mouseDoubleClickEvent(event)


class Runner(QObject):
    """Runs a Python script as a child process and streams its output."""

    started = Signal()
    finished = Signal(int)

    def __init__(self, output: OutputPanel, parent=None):
        super().__init__(parent)
        self.output = output
        self.proc = None
        self.t0 = 0.0

    @property
    def is_running(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def run(self, python, script, cwd, args=None, extra_env=None):
        if self.is_running:
            return
        from . import remote
        self._remote = remote.remote_for(cwd)
        if self._remote is not None:
            return self._run_remote(script, cwd, args, extra_env)
        self.output.clear()
        self.output.write(f"> {python} {script} {' '.join(args or [])}\n\n", "info")

        self.proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")   # print() shows up immediately
        env.insert("PYTHONIOENCODING", "utf-8")
        for k, v in (extra_env or {}).items():
            env.insert(k, v)
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(cwd)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.output.write(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")))
        self.proc.readyReadStandardError.connect(
            lambda: self.output.write(bytes(self.proc.readAllStandardError()).decode("utf-8", "replace"), "err"))
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)

        self.t0 = time.monotonic()
        self.proc.start(python, ["-u", script] + list(args or []))
        self.started.emit()

    def _run_remote(self, script, cwd, args, extra_env):
        """A project in a remote folder: run it on the server, with the server's Python."""
        from . import remote
        cfg, root = self._remote
        py = remote.remote_python(cfg, root)
        argv = [py, "-u", remote.map_arg(cfg, root, script)] + [remote.map_arg(cfg, root, a) for a in (args or [])]
        cmd = remote.command_line(cfg, root, cwd, argv, extra_env)
        self.output.clear()
        self.output.write(f"> on {cfg['host']}: {' '.join(argv)}\n\n", "info")
        self._pid = remote.PidFilter()
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(remote.qenv())
        self.proc.readyReadStandardOutput.connect(
            lambda: self.output.write(self._pid.feed(bytes(self.proc.readAllStandardOutput()).decode("utf-8",
                                                                                                    "replace"))))
        self.proc.readyReadStandardError.connect(
            lambda: self.output.write(bytes(self.proc.readAllStandardError()).decode("utf-8", "replace"), "err"))
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self.t0 = time.monotonic()
        a = remote.ssh_argv(cfg, cmd, x11=bool(cfg.get("x11")))
        self.proc.start(a[0], a[1:])
        self.started.emit()

    def stop(self):
        if self.is_running:
            if getattr(self, "_remote", None) is not None:
                from . import remote
                remote.kill_remote(self._remote[0], getattr(self._pid, "pid", None))
            self.proc.kill()

    def _on_finished(self, code, _status):
        elapsed = time.monotonic() - self.t0
        self.output.write(f"\nProcess exited with code {code} ({elapsed:.2f}s)\n", "info")
        self.finished.emit(code)

    def _on_error(self, err):
        if err == QProcess.FailedToStart:
            self.output.write("\nFailed to start the Python interpreter. "
                              "Check Tools > Select Python Interpreter.\n", "err")
            self.finished.emit(-1)
