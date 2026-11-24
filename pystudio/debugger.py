"""PyStudio side of the debugger: the debug session plus the Locals, Watch and
Call Stack windows (like VS's)."""
import json
import os
import sys
import time

from PySide6.QtCore import QTimer, QObject, QProcess, QProcessEnvironment, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)
from . import theme

DEBUGGEE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debuggee.py")


class DebugSession(QObject):
    stopped = Signal(dict)       # {"reason", "stack": [...], "exception"}
    continued = Signal()
    exited = Signal(int)
    trace_output = Signal(str)   # tracepoint messages
    attach_failed = Signal(str)

    def __init__(self, output, parent=None):
        super().__init__(parent)
        self.output = output
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._on_connect)
        self.sock = None
        self.proc = None
        self.buf = b""
        self.seq = 0
        self.callbacks = {}
        self.pending = []
        self.paused = False
        self.stack = []
        self.t0 = 0.0
        self.initial_breakpoints = {}
        self.exception_settings = {"types": [], "all": False}

    @property
    def active(self):
        if getattr(self, "attached_pid", None):
            return True
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    # ------------------------------------------------------------ attach to a running program
    def attach(self, proc_info, root, breakpoints):
        """Debug > Attach to Process: the program loads the debugger and connects here."""
        from . import attach as attach_mod
        self.path_map = None
        self.remote = None
        self.initial_breakpoints = breakpoints
        self.stop_on_entry = False
        self.server.close()
        self.server.listen(QHostAddress.LocalHost, 0)
        self.attached_pid = proc_info["pid"]
        self.attached_info = proc_info
        self.output.clear()
        self.output.write(f"> Attaching to {proc_info['title']} (process {proc_info['pid']})...\n", "info")
        self.t0 = time.monotonic()
        self._script = attach_mod.write_script(self.server.serverPort(), root)
        self.injector = attach_mod.Injector(self)
        self.injector.done.connect(self._injected)
        self.injector.run(proc_info, self._script)
        self._attach_timer = QTimer(self, singleShot=True, interval=30000)
        import shutil
        waits = (proc_info.get("version") or (0, 0)) >= (3, 14) and not shutil.which("gdb")
        self._attach_timer.timeout.connect(lambda: self.sock is None and self._attach_end(
            "The program didn't connect. Without gdb, Python 3.14 runs the attach code only when the program's "
            "main thread next runs Python code - click something in the program while attaching, or install gdb "
            "(Tools > Install Components), which attaches right away." if waits else
            "The program didn't connect. It may not be able to load PyStudio's debugger - see View > Output."))
        self._attach_timer.start()

    def _injected(self, ok, msg):
        if not ok and self.sock is None:
            self._attach_end("Couldn't attach:\n" + msg)

    def _attach_end(self, error=None):
        pid = getattr(self, "attached_pid", None)
        if pid is None:
            return
        self.attached_pid = None
        if getattr(self, "_attach_timer", None):
            self._attach_timer.stop()
        try:
            os.remove(self._script)
        except (OSError, AttributeError):
            pass
        self.paused = False
        self.stack = []
        if self.sock is not None:
            s, self.sock = self.sock, None
            try:
                s.close()
            except RuntimeError:              # Qt already deleted it (the program ended)
                pass
        self.server.close()
        if error:
            self.output.write(error + "\n", "err")
            self.attach_failed.emit(error)
        else:
            self.output.write(f"\nDetached from process {pid} (it keeps running).\n", "info")
        self.exited.emit(0)

    def detach(self):
        if getattr(self, "attached_pid", None):
            self.send({"cmd": "detach"})
            if self.sock is not None:
                self.sock.flush()
            self._attach_end()

    # ------------------------------------------------------------ start / stop
    def start(self, python, script, cwd, args, env, root, breakpoints, stop_on_entry=False):
        """breakpoints: {file: [{"line": n, "condition": str}]}"""
        self.initial_breakpoints = breakpoints
        self.stop_on_entry = stop_on_entry
        self.path_map = None
        self.remote = None
        self.server.close()
        self.server.listen(QHostAddress.LocalHost, 0)
        port = self.server.serverPort()
        from . import remote
        r = remote.remote_for(cwd)
        if r is not None:
            return self._start_remote(r, script, cwd, args, env, port)
        self.proc = QProcess(self)
        penv = QProcessEnvironment.systemEnvironment()
        penv.insert("PYTHONUNBUFFERED", "1")
        penv.insert("PYTHONIOENCODING", "utf-8")
        for k, v in (env or {}).items():
            penv.insert(k, v)
        self.proc.setProcessEnvironment(penv)
        self.proc.setWorkingDirectory(cwd)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.output.write(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")))
        self.proc.readyReadStandardError.connect(
            lambda: self.output.write(bytes(self.proc.readAllStandardError()).decode("utf-8", "replace"), "err"))
        self.proc.finished.connect(self._on_finished)
        self.output.clear()
        self.output.write(f"> Debugging {script} {' '.join(args)}\n\n", "info")
        self.t0 = time.monotonic()
        self.proc.start(python, ["-u", DEBUGGEE, str(port), root, script] + list(args))

    def _start_remote(self, r, script, cwd, args, env, port):
        """A remote project: the program and the debugger helper run on the server; the helper connects
        back here through the SSH connection (-R). Paths are translated both ways (see send / _on_read)."""
        import random
        from . import remote
        cfg, root = r
        self.remote = r
        self.path_map = (root, cfg["path"].rstrip("/") or "/")
        self.output.clear()
        self.output.write(f"> Debugging {os.path.basename(script)} on {cfg['host']}\n", "info")
        helper = remote.upload(cfg, DEBUGGEE, ".cache/pystudio/debuggee.py")
        if not helper:
            self.output.write(f"Couldn't copy the debugger helper to {cfg['host']} (is the SSH connection up?).\n",
                              "err")
            self.server.close()
            self.exited.emit(-1)
            return
        rport = random.randint(30000, 59000)
        py = remote.remote_python(cfg, root)
        argv = [py, "-u", helper, str(rport), cfg["path"], remote.map_arg(cfg, root, script)] + \
            [remote.map_arg(cfg, root, a) for a in (args or [])]
        cmd = remote.command_line(cfg, root, cwd, argv, env)
        self._pid = remote.PidFilter()
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(remote.qenv())
        self.proc.readyReadStandardOutput.connect(
            lambda: self.output.write(self._pid.feed(bytes(self.proc.readAllStandardOutput()).decode("utf-8",
                                                                                                    "replace"))))
        self.proc.readyReadStandardError.connect(
            lambda: self.output.write(bytes(self.proc.readAllStandardError()).decode("utf-8", "replace"), "err"))
        self.proc.finished.connect(self._on_finished)
        self.output.write("\n", "info")
        self.t0 = time.monotonic()
        a = remote.ssh_argv(cfg, cmd, x11=bool(cfg.get("x11")), forward=f"127.0.0.1:{rport}:127.0.0.1:{port}")
        self.proc.start(a[0], a[1:])

    # remote projects: local mount paths <-> server paths in every message
    def _map_paths(self, obj, outgoing):
        a, b = self.path_map if outgoing else self.path_map[::-1]
        sep_a = a.rstrip("/") + "/"

        def conv(v):
            if isinstance(v, str) and (v == a or v.startswith(sep_a)):
                return b.rstrip("/") + v[len(a.rstrip("/")):] if v != a else b
            return v

        def walk(o):
            if isinstance(o, dict):
                return {k: (conv(v) if k in ("file", "path", "program", "filename", "root", "cwd") else walk(v))
                        for k, v in o.items()}
            if isinstance(o, list):
                return [walk(x) for x in o]
            return o
        return walk(obj)

    def stop(self):
        if getattr(self, "attached_pid", None):
            self.detach()                       # attached: leave the program running (VS: Detach All)
            return
        if self.active:
            self.send({"cmd": "stop"})
            if getattr(self, "remote", None) is not None:
                from . import remote
                remote.kill_remote(self.remote[0], getattr(getattr(self, "_pid", None), "pid", None))
            self.proc.kill()

    def _on_finished(self, code, _status):
        elapsed = time.monotonic() - self.t0
        self.output.write(f"\nThe program has exited with code {code} ({elapsed:.2f}s).\n", "info")
        self.paused = False
        self.stack = []
        self.sock = None
        self.server.close()
        self.exited.emit(code)

    # ------------------------------------------------------------ protocol
    def _on_connect(self):
        self.sock = self.server.nextPendingConnection()
        self.sock.readyRead.connect(self._on_read)
        if getattr(self, "attached_pid", None):
            self.sock.disconnected.connect(lambda: getattr(self, "attached_pid", None) and self._attach_end(
                "The program ended (or closed the debugger connection)."
                if not self._pid_alive() else None))
        for f, bps in self.initial_breakpoints.items():
            self.send({"cmd": "setBreakpoints", "file": f, "breakpoints": bps})
        self.send({"cmd": "setExceptionBreakpoints", **self.exception_settings})
        self.send({"cmd": "start", "stopOnEntry": getattr(self, "stop_on_entry", False)})

    def send(self, msg):
        if getattr(self, "path_map", None):
            msg = self._map_paths(msg, True)
        if self.sock is not None:
            self.sock.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.sock.flush()

    def request(self, msg, callback):
        self.seq += 1
        msg["seq"] = self.seq
        self.callbacks[self.seq] = callback
        self.send(msg)

    def _on_read(self):
        # A readyRead can still arrive after _on_finished cleared self.sock - read the emitting socket.
        sock = self.sender() or self.sock
        if sock is None:
            return
        self.buf += bytes(sock.readAll())
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if getattr(self, "path_map", None):
                msg = self._map_paths(msg, False)
            if "resp" in msg:
                cb = self.callbacks.pop(msg["resp"], None)
                if cb:
                    cb(msg)
                continue
            ev = msg.get("event")
            if ev == "stopped":
                self.paused = True
                self.stack = msg.get("stack", [])
                self.stopped.emit(msg)
            elif ev == "continued":
                self.paused = False
                self.continued.emit()
            elif ev == "output":
                self.trace_output.emit(msg.get("text", ""))
            elif ev == "attached":
                if getattr(self, "_attach_timer", None):
                    self._attach_timer.stop()
                self.output.write(f"Attached to process {msg.get('pid')} (Python {msg.get('python')}): "
                                  f"{msg.get('program')}\nBreakpoints are active; Stop Debugging detaches.\n",
                                  "info")

    def _pid_alive(self):
        pid = getattr(self, "attached_info", {}).get("pid")
        return bool(pid) and os.path.exists(f"/proc/{pid}")

    # ------------------------------------------------------------ commands
    def set_breakpoints(self, file, bps):
        self.send({"cmd": "setBreakpoints", "file": file, "breakpoints": bps})

    def resume(self, how="continue"):
        if self.paused:
            hook = getattr(self, "before_resume", None)
            if hook is not None:
                try:
                    hook()                         # Edit and Continue: apply changes first (sent in order)
                except Exception:  # noqa: BLE001
                    pass
            self.send({"cmd": how})

    def pause(self):
        if self.active and not self.paused:
            self.send({"cmd": "pause"})

    def variables(self, callback, frame=0, ref=0):
        self.request({"cmd": "variables", "frame": frame, "ref": ref}, lambda m: callback(m.get("vars", [])))

    def evaluate(self, expr, frame, callback):
        self.request({"cmd": "evaluate", "expr": expr, "frame": frame}, lambda m: callback(m.get("result", {})))

    def table(self, ref, callback, limit=100000):
        """Data viewer: callback({"columns", "rows", "total", "kind"} or {"error"})."""
        self.request({"cmd": "table", "ref": ref, "limit": limit}, callback)

    def execute(self, code, frame, callback):
        """Immediate window: callback({"output", "result", "error"})."""
        self.request({"cmd": "execute", "code": code, "frame": frame}, callback)

    def select_thread(self, ident, callback):
        """Threads window: switch Call Stack / Locals / Watch to another thread. callback(stack)."""
        self.request({"cmd": "selectThread", "thread": ident}, lambda m: callback(m.get("stack", []), m.get("thread")))

    def set_next_statement(self, line, callback):
        self.request({"cmd": "jump", "line": line}, callback)

    def run_to_line(self, file, line):
        if self.paused:
            self.send({"cmd": "runToLine", "file": file, "line": line})

    def set_variable(self, frame, ref, name, value, callback):
        self.request({"cmd": "setVariable", "frame": frame, "ref": ref, "name": name, "value": value}, callback)

    def set_exception_settings(self, types, break_all=False):
        self.exception_settings = {"types": list(types), "all": bool(break_all)}
        if self.active:
            self.send({"cmd": "setExceptionBreakpoints", **self.exception_settings})


# ============================================================ windows
_TREE_CSS = ("QTreeWidget { background: #1e1e1e; color: #d4d4d4; border: 0; }"
             "QTreeWidget::item:selected { background: #094771; }"
             "QHeaderView::section { background: #2d2d30; color: #cccccc; border: 0; padding: 3px 6px; }")


class VariablesTree(QTreeWidget):
    """Name / Value / Type tree that loads children when expanded (Locals and Watch)."""

    def __init__(self, session, editable=False, parent=None):
        super().__init__(parent)
        self.session = session
        self.editable = editable
        self.setColumnCount(3)
        self.setHeaderLabels(["Name", "Value", "Type"])
        self.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.setColumnWidth(0, 180)
        self.setStyleSheet(_TREE_CSS)
        self.setUniformRowHeights(True)
        self.itemExpanded.connect(self._expand)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    TABLE_REF = Qt.UserRole + 3

    @staticmethod
    def _magnifier():
        if not hasattr(VariablesTree, "_mag"):
            from .icons import magnifier
            VariablesTree._mag = magnifier()
        return VariablesTree._mag

    def set_table(self, it, ref):
        """A value the data viewer can show: a magnifier in the Value column (click it: View as Table)."""
        it.setData(0, self.TABLE_REF, ref or None)
        if ref:
            it.setIcon(1, self._magnifier())
            it.setToolTip(1, (it.toolTip(1) or "")[:2000] + "\n\nClick the magnifier to view it as a table.")
        else:
            from PySide6.QtGui import QIcon
            it.setIcon(1, QIcon())

    def make_item(self, v, parent=None):
        it = QTreeWidgetItem([v["name"], v["value"], v["type"]])
        it.setToolTip(1, v["value"])
        if v.get("type") == "error":
            it.setForeground(1, QColor(theme.c("#f48771")))
        if v.get("ref"):
            it.setData(0, Qt.UserRole, v["ref"])
            it.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        self.set_table(it, v.get("table"))
        (parent.addChild(it) if parent is not None else self.addTopLevelItem(it))
        return it

    def _title(self, it):
        names = []
        while it is not None:
            names.append(it.text(0))
            it = it.parent()
        out = names[-1]
        for n in reversed(names[:-1]):
            out += n if n.startswith("[") else (f"[{n}]" if n[:1] in "'\"" or n[:1].isdigit() else f".{n}")
        return out

    def view_table(self, it):
        ref = it.data(0, self.TABLE_REF) if it is not None else None
        if ref and self.session.paused:
            from .dataviewer import open_viewer
            open_viewer(self.session, ref, self._title(it), self.window())

    def mousePressEvent(self, e):
        it = self.itemAt(e.position().toPoint())
        if it is not None and it.data(0, self.TABLE_REF) and e.button() == Qt.LeftButton:
            rect = self.visualItemRect(it)
            x0 = self.header().sectionViewportPosition(1)
            iw = self.iconSize().width()
            iw = iw if iw > 0 else self.style().pixelMetric(self.style().PixelMetric.PM_SmallIconSize)
            if x0 <= e.position().x() <= x0 + iw + 8 and \
                    rect.top() <= e.position().y() <= rect.bottom():
                self.view_table(it)
                return
        super().mousePressEvent(e)

    def _menu(self, pos):
        it = self.itemAt(pos)
        if it is None:
            return
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        a = m.addAction(self._magnifier(), "View as Table...", lambda: self.view_table(it))
        a.setEnabled(bool(it.data(0, self.TABLE_REF)) and self.session.paused)
        m.addAction("Copy Value", lambda: __import__("PySide6.QtWidgets", fromlist=["QApplication"])
                    .QApplication.clipboard().setText(it.text(1)))
        m.exec(self.viewport().mapToGlobal(pos))

    def _expand(self, item):
        ref = item.data(0, Qt.UserRole)
        if not ref or item.childCount() or not self.session.paused:
            return

        def fill(vars_):
            for v in vars_:
                self.make_item(v, item)
            if not vars_:
                item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicatorWhenChildless)
        self.session.variables(fill, ref=ref)


class LocalsWindow(QWidget):
    """Double-click a value (or select it and press F2) to change it while paused, like VS: type any Python
    expression - 42, "text", [1, 2], self.count + 1."""

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.frame = 0
        self.tree = VariablesTree(session)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.itemDoubleClicked.connect(self._edit)
        self.tree.itemChanged.connect(self._changed)
        self.tree.setToolTip("Double-click a value to change it")
        self._editing = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tree)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_F2 and self.tree.currentItem() is not None:
            self._edit(self.tree.currentItem(), 1)
            return
        super().keyPressEvent(e)

    def _edit(self, item, col):
        if col != 1 or not self.session.paused or not item.text(0) or item.text(0) == "[Qt properties]":
            return
        self._editing = (item, item.text(1))
        self.tree.blockSignals(True)
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self.tree.blockSignals(False)
        self.tree.editItem(item, 1)

    def _changed(self, item, col):
        if col != 1 or self._editing is None or self._editing[0] is not item:
            return
        old = self._editing[1]
        self._editing = None
        text = item.text(1).strip()
        self.tree.blockSignals(True)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.tree.blockSignals(False)
        if not text or text == old:
            self._set_text(item, old)
            return
        parent = item.parent()
        ref = parent.data(0, Qt.UserRole) if parent is not None else 0

        def done(msg):
            if msg.get("error"):
                self._set_text(item, old)
                item.setToolTip(1, f"Couldn't change it: {msg['error']}")
                from PySide6.QtWidgets import QToolTip
                QToolTip.showText(self.tree.viewport().mapToGlobal(self.tree.visualItemRect(item).bottomLeft()),
                                  f"Couldn't change it: {msg['error']}", self.tree)
                return
            v = msg.get("var", {})
            self._set_text(item, v.get("value", text), v.get("type"))
            item.takeChildren()
            item.setData(0, Qt.UserRole, v.get("ref") or None)
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator if v.get("ref")
                                         else QTreeWidgetItem.DontShowIndicator)
            self.tree.set_table(item, v.get("table"))
            item.setForeground(1, QColor(theme.c("#e5c07b")))      # changed values show highlighted
        self.session.set_variable(self.frame, ref or 0, item.text(0), text, done)

    def _set_text(self, item, value, typ=None):
        self.tree.blockSignals(True)
        item.setText(1, value)
        item.setToolTip(1, value)
        if typ is not None:
            item.setText(2, typ)
        self.tree.blockSignals(False)

    def load(self, frame):
        self.frame = frame
        self.tree.clear()
        self.session.variables(lambda vs: [self.tree.make_item(v) for v in vs], frame=frame)

    def clear(self):
        self.tree.clear()


class WatchWindow(QWidget):
    """Type an expression in the last row (or use Debug > Add Watch); re-evaluated at every stop."""

    PLACEHOLDER = "Add item to watch"

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.frame = 0
        self.tree = VariablesTree(session)
        self.tree.itemChanged.connect(self._edited)
        self.tree.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                                  QAbstractItemView.SelectedClicked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tree)
        self.exprs = []
        self._rebuild()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Delete:
            it = self.tree.currentItem()
            if it is not None and it.parent() is None and it.data(0, Qt.UserRole + 1) is not None:
                del self.exprs[it.data(0, Qt.UserRole + 1)]
                self._rebuild()
                return
        super().keyPressEvent(e)

    def add(self, expr):
        expr = expr.strip()
        if expr:
            self.exprs.append(expr)
            self._rebuild()

    def _rebuild(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        for i, e in enumerate(self.exprs):
            it = self.tree.make_item({"name": e, "value": "", "type": "", "ref": 0})
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            it.setData(0, Qt.UserRole + 1, i)
        add = QTreeWidgetItem([self.PLACEHOLDER, "", ""])
        add.setForeground(0, QColor(theme.c("#7a7a7a")))
        add.setFlags(add.flags() | Qt.ItemIsEditable)
        self.tree.addTopLevelItem(add)
        self.tree.blockSignals(False)
        self.refresh(self.frame)

    def _edited(self, item, col):
        if col != 0 or item.parent() is not None:
            return
        idx = item.data(0, Qt.UserRole + 1)
        text = item.text(0).strip()
        if idx is None:
            if text and text != self.PLACEHOLDER:
                self.exprs.append(text)
        elif text:
            self.exprs[idx] = text
        else:
            del self.exprs[idx]
        self._rebuild()

    def refresh(self, frame=0):
        self.frame = frame
        if not self.session.paused:
            return
        for i, expr in enumerate(self.exprs):
            def done(res, i=i):
                it = self.tree.topLevelItem(i)
                if it is None:
                    return
                self.tree.blockSignals(True)
                it.takeChildren()
                it.setText(1, res.get("value", ""))
                it.setText(2, res.get("type", ""))
                it.setToolTip(1, res.get("value", ""))
                it.setForeground(1, QColor(theme.c("#f48771")) if res.get("type") == "error" else QColor(theme.c("#d4d4d4")))
                it.setData(0, Qt.UserRole, res.get("ref") or None)
                it.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator if res.get("ref")
                                           else QTreeWidgetItem.DontShowIndicator)
                self.tree.set_table(it, res.get("table"))
                self.tree.blockSignals(False)
            self.session.evaluate(expr, frame, done)


class CallStackWindow(QWidget):
    frame_selected = Signal(int, str, int)   # frame id, file, line

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["", "Name", "Location"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 22)
        h.setSectionResizeMode(1, QHeaderView.Interactive)
        self.table.setColumnWidth(1, 220)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setStyleSheet(_TREE_CSS.replace("QTreeWidget", "QTableWidget"))
        self.table.cellDoubleClicked.connect(self._activate)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.table)
        self.current = 0

    def load(self, stack, root=None):
        self.table.setRowCount(0)
        external = False
        for fr in stack:
            if not fr.get("user"):
                if external:
                    continue        # collapse runs of library frames, like VS
                external = True
                r = self.table.rowCount()
                self.table.insertRow(r)
                it = QTableWidgetItem("[External Code]")
                it.setForeground(QColor(theme.c("#7a7a7a")))
                self.table.setItem(r, 1, it)
                continue
            external = False
            r = self.table.rowCount()
            self.table.insertRow(r)
            mark = QTableWidgetItem("➜" if fr["id"] == self.current else "")
            mark.setForeground(QColor(theme.c("#ffcc00")))
            f = fr["file"]
            rel = os.path.relpath(f, root) if root and f.startswith(root) else f
            items = [mark, QTableWidgetItem(fr["name"]), QTableWidgetItem(f"{rel}, line {fr['line']}")]
            for c, it in enumerate(items):
                it.setData(Qt.UserRole, (fr["id"], f, fr["line"]))
                self.table.setItem(r, c, it)

    def clear(self):
        self.table.setRowCount(0)

    def _activate(self, row, _col):
        it = self.table.item(row, 1)
        data = it.data(Qt.UserRole) if it else None
        if data:
            self.current = data[0]
            for r in range(self.table.rowCount()):
                m = self.table.item(r, 0)
                d = m.data(Qt.UserRole) if m else None
                if m:
                    m.setText("➜" if d and d[0] == self.current else "")
            self.frame_selected.emit(*data)


class ThreadsWindow(QWidget):
    """VS's Threads window: every thread running Python code (QThreads and worker threads too). The yellow arrow
    is the thread that stopped; double-click another to see its Call Stack, Locals and Watch."""
    thread_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "ID", "Name", "Location", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 22)
        self.table.setColumnWidth(1, 120)
        self.table.setColumnWidth(2, 200)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.hideColumn(4)
        self.table.setStyleSheet(_TREE_CSS.replace("QTreeWidget", "QTableWidget"))
        self.table.cellDoubleClicked.connect(self._activate)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.table)
        self.stopped = None
        self.viewing = None
        self.threads = []

    def load(self, threads, stopped, root=None):
        self.threads = threads or []
        self.stopped = self.viewing = stopped
        self.root = root
        self._fill()

    def _fill(self):
        self.table.setRowCount(0)
        for t in self.threads:
            r = self.table.rowCount()
            self.table.insertRow(r)
            mark = "➜" if t["id"] == self.stopped else ("▷" if t["id"] == self.viewing else "")
            f = t.get("file") or ""
            rel = os.path.relpath(f, self.root) if self.root and f.startswith(self.root) else f
            loc = f"{t.get('func', '')}  ({rel}, line {t.get('line')})" if f else "[External Code]"
            name = t["name"] + ("  [Main Thread]" if t.get("main") and "Main" not in t["name"] else "")
            items = [QTableWidgetItem(mark), QTableWidgetItem(str(t["id"])), QTableWidgetItem(name),
                     QTableWidgetItem(loc)]
            items[0].setForeground(QColor(theme.c("#ffcc00")))
            for c, it in enumerate(items):
                it.setData(Qt.UserRole, t["id"])
                if t["id"] == self.stopped:
                    fnt = it.font()
                    fnt.setBold(True)
                    it.setFont(fnt)
                self.table.setItem(r, c, it)

    def show_viewing(self, ident):
        self.viewing = ident
        self._fill()

    def clear(self):
        self.threads = []
        self.table.setRowCount(0)

    def _activate(self, row, _col):
        it = self.table.item(row, 1)
        if it is not None:
            self.thread_selected.emit(int(it.data(Qt.UserRole)))
