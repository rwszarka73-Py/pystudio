"""PyStudio's own log (Help > View Log): its errors, Qt's warnings, and hard crashes, so a problem on your machine
can be found and fixed. ~/.local/share/pystudio/logs:

    pystudio.log     errors with their tracebacks, Qt warnings, start/stop lines (kept to ~3 MB)
    crash.log        Python's report when PyStudio stops suddenly (a crash in Qt or a library)
    running.lock     present while PyStudio runs - still there at the next start means it didn't close normally

An error shows a box once (Copy Details / Open Log); the same error again only goes to the log."""
import datetime
import faulthandler
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import traceback

LOG_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "pystudio", "logs")
LOG_FILE = os.path.join(LOG_DIR, "pystudio.log")
CRASH_FILE = os.path.join(LOG_DIR, "crash.log")
LOCK_FILE = os.path.join(LOG_DIR, "running.lock")

log = logging.getLogger("pystudio")
_shown = set()
_recent = []
_crash_fh = None
_previous_crash = None


def system_info():
    from PySide6 import __version__ as pyside
    from PySide6.QtCore import qVersion
    try:
        from . import __version__ as ver
    except ImportError:
        ver = "?"
    distro = ""
    try:
        with open("/etc/os-release") as f:
            distro = next((line.split("=", 1)[1].strip().strip('"') for line in f
                           if line.startswith("PRETTY_NAME=")), "")
    except OSError:
        pass
    return (f"PyStudio {ver} | Python {platform.python_version()} | PySide6 {pyside} (Qt {qVersion()}) | "
            f"{distro or platform.platform()} | {os.environ.get('XDG_SESSION_TYPE', '?')} "
            f"{os.environ.get('XDG_CURRENT_DESKTOP', '')}".strip())


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _log_message_boxes():
    """Every warning or error PyStudio shows (Couldn't attach, a failed build...) also goes to the log."""
    from PySide6.QtWidgets import QMessageBox
    for name, level in (("warning", logging.WARNING), ("critical", logging.ERROR)):
        orig = getattr(QMessageBox, name)
        if getattr(orig, "_pystudio_logged", False):
            continue

        def wrapper(parent, title, text, *a, _orig=orig, _level=level, **k):
            log.log(_level, "%s: %s", title, str(text).strip())
            return _orig(parent, title, text, *a, **k)
        wrapper._pystudio_logged = True
        setattr(QMessageBox, name, staticmethod(wrapper))


def install(app):
    """Call once, right after QApplication is created."""
    global _crash_fh, _previous_crash
    _log_message_boxes()
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False

    # did the last session end suddenly?
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if not pid or not _pid_alive(pid) or pid == os.getpid():
            crash = ""
            try:
                with open(CRASH_FILE, encoding="utf-8", errors="replace") as f:
                    crash = f.read()[-6000:]
            except OSError:
                pass
            _previous_crash = crash.strip() or "(no crash report - it may have been closed by the system)"
            log.warning("The previous session didn't close normally.%s", "\n" + crash if crash.strip() else "")
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    # hard crashes (segfaults in Qt or a library): Python writes the stack of every thread here
    try:
        _crash_fh = open(CRASH_FILE, "w", encoding="utf-8")
        _crash_fh.write(f"PyStudio started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n{system_info()}\n\n")
        _crash_fh.flush()
        faulthandler.enable(_crash_fh, all_threads=True)
    except OSError:
        pass
    log.info("Started: %s", system_info())

    sys.excepthook = _excepthook
    threading.excepthook = lambda a: _excepthook(a.exc_type, a.exc_value, a.exc_traceback,
                                                 thread=a.thread.name if a.thread else "")
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    def qt_messages(mode, ctx, msg):
        if mode == QtMsgType.QtDebugMsg or "propagateSizeHints" in msg or "does not support" in msg:
            return
        level = {QtMsgType.QtInfoMsg: logging.INFO, QtMsgType.QtWarningMsg: logging.WARNING}.get(mode,
                                                                                               logging.ERROR)
        where = f" ({ctx.file}:{ctx.line})" if getattr(ctx, "file", None) else ""
        log.log(level, "Qt: %s%s", msg, where)
        if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            sys.stderr.write(msg + "\n")
    qInstallMessageHandler(qt_messages)
    app.aboutToQuit.connect(_clean_exit)


def _clean_exit():
    log.info("Closed normally.")
    try:
        faulthandler.disable()
        if _crash_fh is not None:
            _crash_fh.close()
        os.remove(LOCK_FILE)
    except OSError:
        pass


def previous_crash():
    """The report from a session that ended suddenly (once), or None."""
    global _previous_crash
    c, _previous_crash = _previous_crash, None
    return c


def _excepthook(exc_type, exc, tb, thread=""):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    log.error("Unhandled error%s:\n%s", f" in thread {thread}" if thread else "", text.rstrip())
    sys.__stderr__.write(text)
    if issubclass(exc_type, KeyboardInterrupt):
        return
    # one box per distinct error, at most 3 a minute (an error in a paint event repeats endlessly)
    key = (exc_type.__name__, str(exc)[:200], tb.tb_frame.f_code.co_filename if tb else "",
           traceback.extract_tb(tb)[-1].lineno if tb else 0)
    now = time.monotonic()
    _recent[:] = [t for t in _recent if now - t < 60]
    if key in _shown or len(_recent) >= 3:
        return
    _shown.add(key)
    _recent.append(now)
    from PySide6.QtCore import QThread, QTimer
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    if QThread.currentThread() is not app.thread():
        QTimer.singleShot(0, app, lambda: show_error(exc_type.__name__, str(exc), text))
    else:
        show_error(exc_type.__name__, str(exc), text)


def show_error(name, message, details, title="PyStudio error"):
    from PySide6.QtWidgets import QApplication, QMessageBox
    box = QMessageBox(QMessageBox.Warning, title,
                      f"PyStudio hit an unexpected error. Your work is still open; saving now is a good idea.\n\n"
                      f"{name}: {message[:300]}")
    box.setDetailedText(f"{system_info()}\n\n{details}")
    b_copy = box.addButton("Copy Details", QMessageBox.ActionRole)
    b_log = box.addButton("Open Log", QMessageBox.ActionRole)
    box.addButton(QMessageBox.Close)
    box.exec()
    if box.clickedButton() is b_copy:
        QApplication.clipboard().setText(f"{system_info()}\n\n{details}")
    elif box.clickedButton() is b_log:
        open_log()


def open_log():
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl.fromLocalFile(LOG_FILE if os.path.exists(LOG_FILE) else LOG_DIR))


def tail(n=200):
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""
