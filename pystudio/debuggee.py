"""PyStudio debugger engine - runs YOUR program and talks to PyStudio.

PyStudio starts:  python -u debuggee.py <port> <project_root> <script.py> [args...]
It uses Python's built-in bdb module (what pdb is built on) plus a small JSON-lines
protocol over a localhost socket. Only the standard library is needed, so it works
with any interpreter the project uses.
"""
import bdb
import json
import os
import queue
import re
import socket
import sys
import threading
import traceback
import types

_THIS = os.path.normcase(os.path.abspath(__file__))
_LIB_DIRS = tuple({os.path.normcase(os.path.dirname(os.__file__)) + os.sep,
                   os.path.normcase(os.path.join(sys.base_prefix, "lib")) + os.sep})
_BDB = os.path.normcase(os.path.abspath(bdb.__file__))


class Channel:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port))
        self.lock = threading.Lock()
        self.rfile = self.sock.makefile("r", encoding="utf-8")
        self.on_lost = None          # attached to a running program: detach instead of ending it

    def send(self, obj):
        data = (json.dumps(obj, default=str) + "\n").encode("utf-8")
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                if self.on_lost is not None:
                    self.on_lost()
                    return
                os._exit(0)

    def messages(self):
        for line in self.rfile:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass


class _QtProps:
    """Groups a Qt object's properties under one expandable '[Qt properties]' row."""

    def __init__(self, obj):
        self.obj = obj

    def __repr__(self):
        return "{...}"


class Debugger(bdb.Bdb):
    def __init__(self, chan, root):
        super().__init__()
        self.chan = chan
        self.root = os.path.normcase(os.path.abspath(root)) + os.sep
        self.commands = queue.Queue()
        self.refs = {}
        self.frames = []
        self.reason_pause = False
        self._qobject = None
        self._started = False
        self.stop_on_entry = False
        self.break_on = set()        # Exception Settings: exception names to stop on when raised
        self.break_all = False
        # threads: one thread at a time is stopped (others wait if they stop too); the one being looked at
        self.main_ident = threading.main_thread().ident
        self.stop_lock = threading.Lock()
        self.stopped_ident = None
        self.stopped_frames = []
        self.thread_step = None      # (ident, "step" | "next" | "return", depth) - stepping in a non-main thread
        self.mon = None              # sys.monitoring tool id (Python 3.12+), see install_thread_breakpoints
        self.bp_extra = {}           # (file, line) -> {"hitMode", "hitCount", "log", "continue"}
        self.bp_hits = {}            # (file, line) -> times the breakpoint was reached with its condition true

    def do_clear(self, arg):
        """bdb calls this to delete a temporary breakpoint (Run To Cursor while paused)."""
        try:
            self.clear_bpbynumber(arg)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ Just My Code
    def is_user(self, filename):
        f = self.canonic(filename)
        if getattr(self, "attached", False):     # attached: the program's own files, wherever they are
            return not (f.startswith(_LIB_DIRS) or "site-packages" in f or "dist-packages" in f
                        or f in (_THIS, _BDB) or f.startswith("<"))
        if not f.startswith(self.root):
            return False
        base = os.path.basename(f)
        if (base.startswith("ui_") and base.endswith(".py")) or base == "pystudio_runtime.py":
            return False
        rel = f[len(self.root):]
        return not rel.startswith((".venv" + os.sep, "venv" + os.sep, "publish" + os.sep))

    def stop_here(self, frame):
        if not self.is_user(frame.f_code.co_filename):
            return False
        return super().stop_here(frame)

    def set_continue(self):
        # bdb removes tracing when there are no breakpoints; keep it so breakpoints
        # added later (F9 while running) still work.
        self._set_stopinfo(self.botframe, None, -1)

    # ------------------------------------------------------------ bdb hooks
    def user_call(self, frame, argument_list):
        pass   # stop on the first line instead (user_line)

    def user_return(self, frame, return_value):
        pass

    def user_exception(self, frame, exc_info):
        pass   # unhandled ones stop via excepthook; "thrown" ones via dispatch_exception

    # ------------------------------------------------------------ Exception Settings
    def dispatch_call(self, frame, arg):
        # bdb stops tracing functions in files without breakpoints; exceptions raised there
        # are only seen while their function is traced, so keep tracing your own code
        result = super().dispatch_call(frame, arg)    # also sets bdb's bottom frame the first time
        if result is None and (self.break_on or self.break_all) and self.is_user(frame.f_code.co_filename):
            return self.trace_dispatch
        return result

    def dispatch_exception(self, frame, arg):
        if (self.break_on or self.break_all) and self.is_user(frame.f_code.co_filename):
            et, ev, _tb = arg
            if not getattr(ev, "_ps_seen", False):      # stop where it's raised, not in every caller
                try:
                    ev._ps_seen = True
                except Exception:  # noqa: BLE001 - some exception objects take no attributes
                    pass
                names = {c.__name__ for c in getattr(et, "__mro__", ())}
                if et not in (StopIteration, GeneratorExit, StopAsyncIteration) and \
                        (self.break_all or names & self.break_on):
                    self.interaction(frame, None, "exception",
                                     f"{et.__name__}: {ev}  (thrown - stops because of Exception Settings)")
        return super().dispatch_exception(frame, arg)

    def user_line(self, frame):
        f = self.canonic(frame.f_code.co_filename)
        if not self._started:
            # bdb starts in "step" mode; like VS, run until a breakpoint instead
            # (unless started with F10/F11: then stop on the first line)
            self._started = True
            if frame.f_lineno not in self.breaks.get(f, []) and not self.stop_on_entry:
                self.set_continue()
                return
        reason = "step"
        if self.reason_pause:
            reason, self.reason_pause = "pause", False
        elif frame.f_lineno in self.breaks.get(f, []):
            reason = "breakpoint"
            if not self.stop_here(frame) and not self.breakpoint_action(f, frame.f_lineno, frame):
                return                        # hit count not reached, or a tracepoint (log and go on)
        self.interaction(frame, None, reason)

    # ------------------------------------------------------------ hit counts and actions (tracepoints)
    def breakpoint_action(self, f, line, frame):
        """The breakpoint at f:line was reached (its condition is true). Count the hit; log its message;
        True to stop here."""
        key = (f, line)
        extra = self.bp_extra.get(key)
        if not extra:
            return True
        n = self.bp_hits[key] = self.bp_hits.get(key, 0) + 1
        mode, want = extra.get("hitMode") or "", int(extra.get("hitCount") or 0)
        if mode and want > 0:
            if (mode == "=" and n != want) or (mode == ">=" and n < want) or (mode == "%" and n % want):
                return False
        if extra.get("log"):
            try:
                text = self.format_message(extra["log"], frame, n)
            except Exception as e:  # noqa: BLE001
                text = f"(tracepoint message failed: {e})"
            self.chan.send({"event": "output", "category": "tracepoint", "text": text,
                            "file": f, "line": line})
        return not extra.get("continue")

    def format_message(self, template, frame, hits=0):
        """VS tracepoint text: {expression} values plus $FUNCTION $CALLER $TID $TNAME $LINE $FILE $HITCOUNT."""
        t = threading.current_thread()
        caller = frame.f_back.f_code.co_name if frame.f_back is not None else ""
        repl = {"$FUNCTION": frame.f_code.co_name, "$CALLER": caller, "$TID": str(threading.get_ident()),
                "$TNAME": t.name, "$LINE": str(frame.f_lineno), "$FILE": os.path.basename(frame.f_code.co_filename),
                "$HITCOUNT": str(hits)}
        for k, v in repl.items():
            template = template.replace(k, v)

        def ev(m):
            expr = m.group(1)
            try:
                v = eval(expr, frame.f_globals, frame.f_locals)
            except Exception as e:  # noqa: BLE001
                return f"<{type(e).__name__}: {e}>"
            return v if isinstance(v, str) else repr(v)
        return re.sub(r"\{([^{}]+)\}", ev, template)

    # ------------------------------------------------------------ pausing
    def interaction(self, frame, tb, reason, exc_text=""):
        with self.stop_lock:                    # another thread stopped: wait until it goes on
            try:
                return self._interaction(frame, tb, reason, exc_text)
            finally:
                self.stopped_ident = None

    def _interaction(self, frame, tb, reason, exc_text=""):
        ident = threading.get_ident()
        secondary = ident != self.main_ident
        self.stopped_ident = ident
        self.refs.clear()
        stack = []
        if tb is not None:
            t = tb
            while t is not None:
                stack.append((t.tb_frame, t.tb_lineno))
                t = t.tb_next
            stack.reverse()
        else:
            f = frame
            while f is not None:
                stack.append((f, f.f_lineno))
                f = f.f_back
        stack = [(f, ln) for f, ln in stack
                 if self.canonic(f.f_code.co_filename) not in (_THIS, _BDB)
                 and f.f_code.co_filename != "<string>"]
        self.frames = stack
        self.stopped_frames = stack

        def send_stopped(why):
            self.chan.send({"event": "stopped", "reason": why, "exception": exc_text,
                            "stack": self.stack_json(stack, tb is None),
                            "thread": ident, "threads": self.thread_list()})
        send_stopped(reason)
        while True:
            msg = self.commands.get()
            cmd = msg.get("cmd")
            if cmd == "variables":
                self._reply(msg, vars=self.variables(msg))
            elif cmd == "evaluate":
                self._reply(msg, result=self.evaluate(msg.get("expr", ""), msg.get("frame", 0)))
            elif cmd == "table":
                obj = self.refs.get(msg.get("ref"), _MISSING)
                self._reply(msg, **(to_table(obj, msg.get("limit", 100000)) if obj is not _MISSING else
                                    {"error": "That value is gone - the program has moved on. Pause again and "
                                              "open the viewer from Locals or Watch."}))
            elif cmd == "execute":
                self._reply(msg, **self.execute(msg.get("code", ""), msg.get("frame", 0)))
            elif cmd == "snapshot":
                self._reply(msg, **heap_snapshot())
            elif cmd == "applyEdits":
                res = apply_edits(self.canonic(msg.get("file", "")), msg.get("source", ""))
                try:
                    self._breaks_changed()           # breakpoints in the new code
                except Exception:  # noqa: BLE001
                    pass
                self._reply(msg, **res)
            elif cmd == "setVariable":
                self._reply(msg, **self.set_variable(msg))
            elif cmd == "selectThread":
                # Threads window: look at another thread's stack (Locals, Watch, Immediate follow)
                self.refs.clear()
                t = int(msg.get("thread", ident))
                self.frames = stack if t == ident else self.thread_stack(t)
                self._reply(msg, stack=self.stack_json(self.frames, t != ident or tb is None), thread=t)
            elif cmd == "jump":
                # Set Next Statement: only the paused line of the top frame can move (Python's rule)
                if self.frames is not stack:
                    self._reply(msg, error="Set Next Statement works in the thread that stopped.")
                    continue
                if tb is not None or not stack or reason == "exception":
                    self._reply(msg, error="Set Next Statement isn't possible while stopped on an exception.")
                    continue
                top = stack[0][0]
                try:
                    top.f_lineno = int(msg.get("line", 0))
                except (ValueError, TypeError) as e:
                    self._reply(msg, error=str(e))
                    continue
                stack[0] = (top, top.f_lineno)
                self._reply(msg, line=top.f_lineno)
                send_stopped("step")
            elif cmd == "runToLine":
                self.set_break(self.canonic(msg["file"]), int(msg["line"]), temporary=True)
                self._breaks_changed()
                if secondary:
                    self.thread_step = None
                else:
                    self.set_continue()
                self.refs.clear()
                self.chan.send({"event": "continued"})
                return
            elif cmd in ("continue", "next", "step", "return") and secondary:
                top = stack[0][0] if stack else frame
                if tb is not None or top is None or cmd == "continue":
                    self.thread_step = None
                else:
                    self.thread_step = (ident, cmd, self._depth(top))
                self._stepping_changed()
                self.refs.clear()
                self.chan.send({"event": "continued"})
                return
            elif cmd in ("continue", "next", "step", "return"):
                top = stack[0][0] if stack else frame
                if tb is not None or top is None or cmd == "continue":
                    self.set_continue()
                elif cmd == "next":
                    self.set_next(top)
                elif cmd == "step":
                    self.set_step()
                else:
                    self.set_return(top)
                self.refs.clear()
                self.chan.send({"event": "continued"})
                return

    # ------------------------------------------------------------ threads
    def stack_json(self, stack, top_is_live=True):
        return [{"id": i, "name": f.f_code.co_name, "file": self.canonic(f.f_code.co_filename),
                 "line": f.f_lineno if i == 0 and top_is_live else ln, "user": self.is_user(f.f_code.co_filename)}
                for i, (f, ln) in enumerate(stack)]

    def _frames_of(self, top):
        out = []
        f = top
        while f is not None:
            out.append((f, f.f_lineno))
            f = f.f_back
        return [(f, ln) for f, ln in out if self.canonic(f.f_code.co_filename) not in (_THIS, _BDB)
                and f.f_code.co_filename != "<string>"]

    def thread_stack(self, ident):
        top = sys._current_frames().get(ident)
        return self._frames_of(top) if top is not None else []

    @staticmethod
    def _depth(frame):
        n = 0
        while frame is not None:
            n += 1
            frame = frame.f_back
        return n

    def thread_list(self):
        """[{id, name, file, line, func, main, current}] - every thread running Python code (QThreads too)."""
        by_ident = {t.ident: t for t in threading.enumerate()}
        out = []
        for ident, top in sys._current_frames().items():
            t = by_ident.get(ident)
            if t is not None and t.name == "pystudio-debugger":
                continue
            frames = self.stopped_frames if ident == self.stopped_ident else self._frames_of(top)
            user = next(((f, ln) for f, ln in frames if self.is_user(f.f_code.co_filename)), None)
            loc = user or (frames[0] if frames else None)
            name = t.name if t is not None and not isinstance(t, threading._DummyThread) else ""
            if not name or name.startswith("Dummy-"):
                name = self._qt_thread_name(top) or "Qt thread"
            out.append({"id": ident, "name": name, "main": ident == self.main_ident,
                        "current": ident == self.stopped_ident,
                        "file": self.canonic(loc[0].f_code.co_filename) if loc else "",
                        "line": (loc[0].f_lineno if loc and ident != self.stopped_ident else loc[1]) if loc else 0,
                        "func": loc[0].f_code.co_name if loc else ""})
        out.sort(key=lambda d: (not d["main"], d["name"].lower()))
        return out

    @staticmethod
    def _qt_thread_name(top):
        """A QThread subclass (its run) or a worker object moved to a thread: name it after the class."""
        f = top
        found = None
        while f is not None:
            obj = f.f_locals.get("self") if "self" in f.f_code.co_varnames[:1] else None
            if obj is not None:
                names = [c.__name__ for c in type(obj).__mro__]
                if "QThread" in names:
                    return f"{type(obj).__name__} (QThread)"
                if "QObject" in names or "QRunnable" in names:
                    found = f"{type(obj).__name__} (worker)"
            f = f.f_back
        return found

    # breakpoints in other threads: sys.monitoring (3.12+) sees every thread - threading.Thread, QThread,
    # moveToThread workers, QThreadPool. Only functions of files with breakpoints get line events.
    def install_thread_breakpoints(self):
        mon = getattr(sys, "monitoring", None)
        if mon is None:
            threading.settrace(self._thread_trace)          # older Pythons: threading.Thread only
            return
        for tool in (4, 3, 5):
            try:
                mon.use_tool_id(tool, "pystudio")
                self.mon = tool
                break
            except ValueError:
                continue
        if self.mon is None:
            threading.settrace(self._thread_trace)
            return
        E = mon.events
        mon.register_callback(self.mon, E.PY_START, self._mon_start)
        mon.register_callback(self.mon, E.LINE, self._mon_line)
        mon.set_events(self.mon, E.PY_START)

    def _breaks_changed(self):
        if self.mon is not None:
            sys.monitoring.restart_events()
            self._arm_running()

    def _arm_running(self):
        """Functions already running (a thread's endless loop, the attached program's main loop) never start
        again, so PY_START won't arm them: give line events to their code - and to every loaded function from
        a file with breakpoints - now."""
        files = set(self.breaks)
        if not files:
            return
        E = sys.monitoring.events
        done = set()

        def arm(code):
            if id(code) in done:
                return
            done.add(id(code))
            if self.canonic(code.co_filename) in files:
                try:
                    sys.monitoring.set_local_events(self.mon, code, E.LINE)
                except ValueError:
                    pass
            for c in code.co_consts:
                if isinstance(c, types.CodeType):
                    arm(c)
        for top in sys._current_frames().values():
            f = top
            while f is not None:
                arm(f.f_code)
                f = f.f_back
        import gc
        for o in gc.get_objects():
            if isinstance(o, types.FunctionType):
                arm(o.__code__)

    def _stepping_changed(self):
        if self.mon is None:
            return
        E = sys.monitoring.events
        line_all = self.thread_step or getattr(self, "pause_any", False)
        sys.monitoring.set_events(self.mon, E.PY_START | (E.LINE if line_all else 0))
        sys.monitoring.restart_events()

    def _mon_start(self, code, _offset):
        f = self.canonic(code.co_filename)
        if self.thread_step or getattr(self, "pause_any", False):
            return None
        if f in self.breaks and self.is_user(code.co_filename):
            sys.monitoring.set_local_events(self.mon, code, sys.monitoring.events.LINE)
            return None
        return sys.monitoring.DISABLE

    def _mon_line(self, code, line):
        ident = threading.get_ident()
        step = self.thread_step
        if ident == self.main_ident or ident == self.stopped_ident:
            return None                         # bdb handles the main thread
        f = self.canonic(code.co_filename)
        if getattr(self, "pause_any", False) and self.is_user(code.co_filename):
            self.pause_any = False              # Break All (attached): the next line of the program's code
            self._stepping_changed()
            self.interaction(sys._getframe(1), None, "pause")
            return None
        if step and step[0] == ident and self.is_user(code.co_filename):
            frame = sys._getframe(1)
            _t, mode, depth = step
            d = self._depth(frame)
            if mode == "step" or (mode == "next" and d <= depth) or (mode == "return" and d < depth):
                self.thread_step = None
                self._stepping_changed()
                self.interaction(frame, None, "step")
            return None
        lines = self.breaks.get(f)
        if not lines or line not in lines:
            return None if step else sys.monitoring.DISABLE
        frame = sys._getframe(1)
        bp, flag = bdb.effective(f, line, frame)
        if bp is None:
            return None
        if flag and bp.temporary:
            self.do_clear(str(bp.number))
        if self.breakpoint_action(f, line, frame):
            self.interaction(frame, None, "breakpoint")
        return None

    def _thread_trace(self, frame, event, arg):           # Python < 3.12
        if event == "call" and (self.thread_step or self.canonic(frame.f_code.co_filename) in self.breaks):
            return self._thread_local
        return None

    def _thread_local(self, frame, event, arg):
        if event != "line":
            return self._thread_local
        ident = threading.get_ident()
        step = self.thread_step
        if step and step[0] == ident and self.is_user(frame.f_code.co_filename):
            _t, mode, depth = step
            d = self._depth(frame)
            if mode == "step" or (mode == "next" and d <= depth) or (mode == "return" and d < depth):
                self.thread_step = None
                self.interaction(frame, None, "step")
            return self._thread_local
        f = self.canonic(frame.f_code.co_filename)
        if frame.f_lineno in self.breaks.get(f, []):
            bp, flag = bdb.effective(f, frame.f_lineno, frame)
            if bp is not None:
                if flag and bp.temporary:
                    self.do_clear(str(bp.number))
                if self.breakpoint_action(f, frame.f_lineno, frame):
                    self.interaction(frame, None, "breakpoint")
        return self._thread_local

    def _reply(self, msg, **data):
        data["resp"] = msg.get("seq")
        self.chan.send(data)

    # ------------------------------------------------------------ values
    def _is_qobject(self, value):
        if self._qobject is None:
            mod = sys.modules.get("PySide6.QtCore")
            self._qobject = getattr(mod, "QObject", False) if mod else False
            if not self._qobject:
                self._qobject = None
                return False
        return isinstance(value, self._qobject)

    def _expandable(self, v):
        if isinstance(v, (dict, list, tuple, set, frozenset)):
            return len(v) > 0
        if isinstance(v, (str, bytes, int, float, complex, bool, type(None), types.ModuleType,
                          types.FunctionType, types.BuiltinFunctionType, types.MethodType, type)):
            return False
        if isinstance(v, _QtProps) or self._is_qobject(v):
            return True
        return bool(getattr(v, "__dict__", None))

    def var(self, name, value):
        try:
            r = repr(value)
        except Exception as e:  # noqa: BLE001
            r = f"<repr failed: {e}>"
        if len(r) > 300:
            r = r[:300] + "..."
        ref = 0
        try:
            if self._expandable(value):
                ref = len(self.refs) + 1
                self.refs[ref] = value
        except Exception:  # noqa: BLE001
            ref = 0
        out = {"name": name, "value": r, "type": type(value).__name__, "ref": ref}
        try:
            if is_tabular(value):            # Locals/Watch show a magnifier: View as Table
                if not ref:
                    t = len(self.refs) + 1
                    self.refs[t] = value
                    out["table"] = t
                else:
                    out["table"] = ref
        except Exception:  # noqa: BLE001
            pass
        return out

    def children(self, obj):
        out = []
        if isinstance(obj, dict):
            for k, v in list(obj.items())[:300]:
                out.append(self.var(repr(k), v))
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj[:300]):
                out.append(self.var(f"[{i}]", v))
        elif isinstance(obj, (set, frozenset)):
            for v in list(obj)[:300]:
                out.append(self.var("", v))
        elif isinstance(obj, _QtProps):
            try:
                mo = obj.obj.metaObject()
                props = []
                for i in range(mo.propertyCount()):
                    p = mo.property(i)
                    try:
                        props.append(self.var(p.name(), p.read(obj.obj)))
                    except Exception:  # noqa: BLE001
                        pass
                out = sorted(props, key=lambda x: x["name"].lower())
            except Exception:  # noqa: BLE001
                pass
        else:
            # your own attributes first (self.ui, counters...), Qt's properties grouped below
            d = getattr(obj, "__dict__", None) or {}
            for k in sorted(d, key=str.lower):
                v = d[k]
                if k.startswith("__") or type(v).__name__ in ("SignalInstance", "Signal") \
                        or isinstance(v, (types.MethodType, types.BuiltinMethodType)):
                    continue   # PySide keeps connected signals on the object; not useful here
                out.append(self.var(k, v))
            if self._is_qobject(obj):
                v = self.var("[Qt properties]", _QtProps(obj))
                v["type"] = ""
                out.append(v)
        return out

    def variables(self, msg):
        if msg.get("ref"):
            obj = self.refs.get(msg["ref"])
            return self.children(obj) if obj is not None else []
        i = msg.get("frame", 0)
        if not (0 <= i < len(self.frames)):
            return []
        f = self.frames[i][0]
        loc = f.f_locals
        is_module = loc is f.f_globals
        out = []
        names = sorted(loc, key=lambda n: (n != "self", n.lower()))
        for n in names:
            v = loc[n]
            if n.startswith("__") or (is_module and isinstance(v, (types.ModuleType, types.FunctionType, type))):
                continue
            out.append(self.var(n, v))
        return out

    def set_variable(self, msg):
        """Locals: a new value for a variable, an attribute, a list/dict item or a Qt property."""
        i = msg.get("frame", 0)
        if not (0 <= i < len(self.frames)):
            return {"error": "Not paused"}
        f = self.frames[i][0]
        name, ref = msg.get("name", ""), msg.get("ref", 0)
        try:
            value = eval(msg.get("value", ""), f.f_globals, f.f_locals)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        try:
            if not ref:
                f.f_locals[name] = value
                if sys.version_info < (3, 13) and f.f_locals is not f.f_globals:
                    import ctypes
                    ctypes.pythonapi.PyFrame_LocalsToFast(ctypes.py_object(f), ctypes.c_int(0))
            else:
                obj = self.refs.get(ref)
                if isinstance(obj, _QtProps):
                    if not obj.obj.setProperty(name, value):
                        return {"error": f"Qt didn't accept that value for {name}"}
                elif isinstance(obj, dict):
                    obj[eval(name, {}, {})] = value
                elif isinstance(obj, list):
                    obj[int(name.strip("[]"))] = value
                elif isinstance(obj, (tuple, frozenset, set, str)):
                    return {"error": f"A {type(obj).__name__} can't be changed (it's immutable)"}
                else:
                    setattr(obj, name, value)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        return {"var": self.var(name, value)}

    def evaluate(self, expr, frame_index):
        if not (0 <= frame_index < len(self.frames)):
            return {"name": expr, "value": "(not paused)", "type": "", "ref": 0}
        f = self.frames[frame_index][0]
        try:
            value = eval(expr, f.f_globals, f.f_locals)
        except Exception as e:  # noqa: BLE001
            return {"name": expr, "value": f"{type(e).__name__}: {e}", "type": "error", "ref": 0}
        return self.var(expr, value)


    def execute(self, source, frame_index):
        """Immediate window: an expression shows its value; statements run (x = 5 changes x)."""
        import io
        if not (0 <= frame_index < len(self.frames)):
            return {"output": "", "result": "(not paused)", "error": True}
        f = self.frames[frame_index][0]
        g, loc = f.f_globals, f.f_locals
        buf = io.StringIO()
        old = sys.stdout, sys.stderr
        result, error = "", False
        try:
            try:
                code, is_expr = compile(source, "<immediate>", "eval"), True
            except SyntaxError:
                code, is_expr = compile(source, "<immediate>", "exec"), False
            sys.stdout = sys.stderr = buf
            if is_expr:
                value = eval(code, g, loc)
                result = "" if value is None else _short_repr(value)    # like the REPL: print() shows no None
            else:
                exec(code, g, loc)
                if sys.version_info < (3, 13):        # 3.13+: f_locals writes through by itself
                    import ctypes
                    ctypes.pythonapi.PyFrame_LocalsToFast(ctypes.py_object(f), ctypes.c_int(0))
        except BaseException as e:  # noqa: BLE001 - show the error like the REPL does
            result, error = "".join(traceback.format_exception_only(type(e), e)).strip(), True
        finally:
            sys.stdout, sys.stderr = old
        return {"output": buf.getvalue(), "result": result, "error": error}


_MISSING = object()


def _code_map(code, out=None):
    """{qualname: code object} for every function/class body compiled from a file."""
    out = {} if out is None else out
    for c in code.co_consts:
        if isinstance(c, type(code)):
            out[getattr(c, "co_qualname", c.co_name)] = c
            _code_map(c, out)
    return out


def heap_snapshot(top=300):
    """Diagnostic Tools > Take Snapshot: live Python objects by type (count and size)."""
    import gc
    gc.collect()
    counts, sizes = {}, {}
    objs = gc.get_objects()
    for o in objs:
        t = type(o)
        mod = getattr(t, "__module__", "") or ""
        name = t.__qualname__ if mod in ("builtins", "") else f"{mod}.{t.__qualname__}"
        counts[name] = counts.get(name, 0) + 1
        try:
            sizes[name] = sizes.get(name, 0) + sys.getsizeof(o)
        except Exception:  # noqa: BLE001
            pass
    rows = sorted(counts, key=lambda k: -sizes.get(k, 0))[:top]
    return {"objects": len(objs), "bytes": sum(sizes.values()),
            "types": [[k, counts[k], sizes.get(k, 0)] for k in rows]}


def apply_edits(path, source):
    """Edit and Continue: put the edited functions and methods of one file into the running program.
    Existing functions get the new code (calls from now on use it); new functions and classes are added;
    module-level statements are not run again."""
    import ast
    import gc
    try:
        new_code = compile(source, path, "exec")
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"error": f"Syntax error on line {e.lineno}: {e.msg} - nothing was applied."}
    new = _code_map(new_code)

    def same_file(f):
        try:
            return f and os.path.samefile(f, path)
        except OSError:
            return False
    namespaces = []
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and same_file(f):
            namespaces.append(m.__dict__)
    if not namespaces:
        return {"error": "That file isn't loaded in the program yet - the change takes effect when it is."}
    applied, added, skipped = [], [], []
    seen = set()

    def swap(func, label):
        if id(func) in seen or not hasattr(func, "__code__"):
            return
        seen.add(id(func))
        old = func.__code__
        if not same_file(old.co_filename):
            return
        q = getattr(old, "co_qualname", func.__qualname__)
        nc = new.get(q)
        if nc is None:
            return
        if nc.co_code == old.co_code and nc.co_consts == old.co_consts and nc.co_names == old.co_names and \
                nc.co_firstlineno == old.co_firstlineno:
            return
        if len(nc.co_freevars) != len(old.co_freevars) or nc.co_freevars != old.co_freevars:
            skipped.append([label, "uses different outer variables - restart to apply"])
            return
        try:
            func.__code__ = nc
            applied.append(label)
        except Exception as e:  # noqa: BLE001
            skipped.append([label, str(e)])

    def members(cls):
        for name, v in list(vars(cls).items()):
            if isinstance(v, (staticmethod, classmethod)):
                v = v.__func__
            if isinstance(v, property):
                for f in (v.fget, v.fset, v.fdel):
                    if f is not None:
                        yield f"{cls.__name__}.{name}", f
            elif hasattr(v, "__code__"):
                yield f"{cls.__name__}.{name}", v
            elif isinstance(v, type) and v.__module__ == cls.__module__:
                yield from members(v)

    for ns in namespaces:
        for name, v in list(ns.items()):
            if hasattr(v, "__code__"):
                swap(v, name)
            elif isinstance(v, type) and v.__module__ == ns.get("__name__"):
                for label, f in members(v):
                    swap(f, label)
    # closures and nested functions living elsewhere (callbacks, lambdas kept by Qt...): any function object
    # whose code came from this file
    for obj in gc.get_objects():
        if type(obj).__name__ == "function" and id(obj) not in seen:
            try:
                if same_file(obj.__code__.co_filename) and obj.__code__.co_name != "<lambda>":
                    swap(obj, obj.__qualname__)
            except Exception:  # noqa: BLE001
                pass
    # new top-level functions/classes, and new methods in existing classes
    ns = namespaces[0]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name not in ns:
            try:
                exec(compile(ast.Module([node], type_ignores=[]), path, "exec"), ns)
                added.append(node.name)
            except Exception as e:  # noqa: BLE001
                skipped.append([node.name, f"couldn't add it: {e}"])
        elif isinstance(node, ast.ClassDef) and isinstance(ns.get(node.name), type):
            cls = ns[node.name]
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name not in vars(cls):
                    if any(isinstance(n, ast.Name) and n.id == "super" for n in ast.walk(item)):
                        skipped.append([f"{node.name}.{item.name}", "a new method that calls super() - restart to "
                                                                    "add it"])
                        continue
                    tmp = {}
                    try:
                        exec(compile(ast.Module([item], type_ignores=[]), path, "exec"), ns, tmp)
                        f = tmp[item.name]
                        f.__qualname__ = f"{node.name}.{item.name}"
                        setattr(cls, item.name, f)
                        added.append(f"{node.name}.{item.name}")
                    except Exception as e:  # noqa: BLE001
                        skipped.append([f"{node.name}.{item.name}", f"couldn't add it: {e}"])
    return {"applied": applied, "added": added, "skipped": skipped}


def is_tabular(v):
    """Values the data viewer can show as a grid."""
    t = type(v)
    mod = getattr(t, "__module__", "") or ""
    if mod.startswith("pandas") and t.__name__ in ("DataFrame", "Series"):
        return True
    if mod.startswith("numpy") and t.__name__ == "ndarray":
        return getattr(v, "ndim", 0) in (1, 2) and getattr(v, "size", 0) > 0
    if isinstance(v, (list, tuple)):
        return len(v) > 1 or (len(v) == 1 and isinstance(v[0], (dict, list, tuple)))
    if isinstance(v, dict):
        return len(v) > 1
    if isinstance(v, (set, frozenset)):
        return len(v) > 1
    return hasattr(v, "columns") and hasattr(v, "rows") and not isinstance(v, type)


def _cell(v):
    """A grid cell: numbers and text as they are, everything else as its repr (cut at 1,000 characters)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        if isinstance(v, float) and v != v:
            return None                      # NaN shows empty
        if isinstance(v, int) and not isinstance(v, bool) and abs(v) > 2 ** 53:
            return str(v)
        return v[:1000] if isinstance(v, str) else v
    try:
        import numbers
        if isinstance(v, numbers.Integral):
            return int(v)
        if isinstance(v, numbers.Real):
            f = float(v)
            return None if f != f else f
    except Exception:  # noqa: BLE001
        pass
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat(sep=" ") if hasattr(v, "hour") and hasattr(v, "date") else v.isoformat()
        except Exception:  # noqa: BLE001
            pass
    try:
        r = str(v) if type(v).__name__ in ("Decimal", "UUID", "Timestamp", "Path", "PosixPath") else repr(v)
    except Exception as e:  # noqa: BLE001
        r = f"<repr failed: {e}>"
    return r[:1000]


def to_table(v, limit=100000):
    """{"columns", "rows", "total", "kind"} for the data viewer (rows cut at limit)."""
    try:
        t = type(v)
        mod = getattr(t, "__module__", "") or ""
        kind = t.__name__
        if mod.startswith("pandas") and kind == "Series":
            v = v.to_frame(name=v.name if v.name is not None else "value")
            mod, kind = "pandas", "Series"
        if mod.startswith("pandas"):
            idx_names = [n if n is not None else "" for n in v.index.names]
            show_index = not (len(idx_names) == 1 and not idx_names[0] and
                              type(v.index).__name__ == "RangeIndex" and v.index.start == 0 and v.index.step == 1)
            cols = ([str(n) or "index" for n in idx_names] if show_index else []) + \
                [" / ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in v.columns]
            rows = []
            for row in v.head(limit).itertuples(index=show_index, name=None):
                if show_index and len(idx_names) > 1:
                    row = tuple(row[0]) + tuple(row[1:])
                rows.append([_cell(x) for x in row])
            return {"columns": cols, "rows": rows, "total": len(v), "kind": f"{kind} {v.shape[0]} x {v.shape[1]}"}
        if mod.startswith("numpy") and kind == "ndarray":
            if v.ndim == 1:
                cols = ["index"] + (list(v.dtype.names) if v.dtype.names else ["value"])
                rows = [[i] + ([_cell(x) for x in r] if v.dtype.names else [_cell(r)]) for i, r in
                        enumerate(v[:limit].tolist())]
            else:
                cols = ["index"] + [str(i) for i in range(v.shape[1])]
                rows = [[i] + [_cell(x) for x in r] for i, r in enumerate(v[:limit].tolist())]
            return {"columns": cols, "rows": rows, "total": len(v), "kind": f"ndarray {v.shape} {v.dtype}"}
        if isinstance(v, (set, frozenset)):
            items = sorted(v, key=lambda x: (str(type(x)), str(x)))
            return {"columns": ["value"], "rows": [[_cell(x)] for x in items[:limit]], "total": len(v),
                    "kind": f"{kind} of {len(v)}"}
        if isinstance(v, dict):
            vals = list(v.values())
            if vals and all(isinstance(x, (list, tuple)) for x in vals) and len({len(x) for x in vals}) == 1:
                cols = [str(k) for k in v]
                n = len(vals[0])
                rows = [[_cell(col[i]) for col in vals] for i in range(min(n, limit))]
                return {"columns": cols, "rows": rows, "total": n, "kind": f"dict of {len(cols)} columns"}
            if vals and all(isinstance(x, dict) for x in vals):
                keys = []
                for x in vals[:2000]:
                    for k in x:
                        if k not in keys:
                            keys.append(k)
                rows = [[_cell(k)] + [_cell(x.get(c)) for c in keys] for k, x in list(v.items())[:limit]]
                return {"columns": ["key"] + [str(c) for c in keys], "rows": rows, "total": len(v),
                        "kind": f"dict of {len(v)} records"}
            rows = [[_cell(k), _cell(x), type(x).__name__] for k, x in list(v.items())[:limit]]
            return {"columns": ["key", "value", "type"], "rows": rows, "total": len(v), "kind": f"dict of {len(v)}"}
        if hasattr(v, "columns") and hasattr(v, "rows") and not isinstance(v, (list, tuple)):
            cols = [str(c[0] if isinstance(c, (tuple, list)) else c) for c in v.columns]
            src = v.rows
            rows = [[_cell(x) for x in r] for r in list(src)[:limit]]
            return {"columns": cols, "rows": rows, "total": len(src), "kind": kind}
        if isinstance(v, (list, tuple)):
            items = list(v)
            sample = items[:2000]
            if sample and all(isinstance(x, dict) for x in sample):
                keys = []
                for x in sample:
                    for k in x:
                        if k not in keys:
                            keys.append(k)
                rows = [[_cell(x.get(k)) if isinstance(x, dict) else _cell(x) for k in keys] for x in items[:limit]]
                return {"columns": [str(k) for k in keys], "rows": rows, "total": len(items),
                        "kind": f"{kind} of {len(items)} dicts"}
            if sample and all(hasattr(x, "keys") and hasattr(x, "__getitem__") and not isinstance(x, dict)
                              for x in sample):                        # sqlite3.Row and the like
                keys = list(sample[0].keys())
                rows = [[_cell(x[k]) for k in keys] for x in items[:limit]]
                return {"columns": [str(k) for k in keys], "rows": rows, "total": len(items),
                        "kind": f"{kind} of {len(items)} rows"}
            if sample and all(isinstance(x, (list, tuple)) for x in sample):
                fields = getattr(type(sample[0]), "_fields", None)       # namedtuples
                width = max(len(x) for x in sample)
                cols = list(fields) if fields and len(fields) == width else [f"Column{i + 1}" for i in range(width)]
                rows = [[_cell(x[i]) if i < len(x) else None for i in range(width)] if isinstance(x, (list, tuple))
                        else [_cell(x)] + [None] * (width - 1) for x in items[:limit]]
                return {"columns": cols, "rows": rows, "total": len(items), "kind": f"{kind} of {len(items)} rows"}
            if sample and all(hasattr(x, "__dict__") and not isinstance(x, type) for x in sample) and \
                    not all(isinstance(x, (str, int, float)) for x in sample):
                import dataclasses
                keys = []
                for x in sample:
                    d = dataclasses.asdict(x) if dataclasses.is_dataclass(x) else vars(x)
                    for k in d:
                        if not k.startswith("_") and k not in keys:
                            keys.append(k)
                rows = [[_cell(getattr(x, k, None)) for k in keys] for x in items[:limit]]
                return {"columns": keys, "rows": rows, "total": len(items),
                        "kind": f"{kind} of {len(items)} {type(sample[0]).__name__}"}
            rows = [[i, _cell(x), type(x).__name__] for i, x in enumerate(items[:limit])]
            return {"columns": ["index", "value", "type"], "rows": rows, "total": len(items),
                    "kind": f"{kind} of {len(items)}"}
        return {"error": f"A {kind} can't be shown as a table."}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Couldn't make a table from it: {type(e).__name__}: {e}"}


def _short_repr(v, limit=4000):
    try:
        r = repr(v)
    except Exception as e:  # noqa: BLE001
        r = f"<repr failed: {e}>"
    return r if len(r) <= limit else r[:limit] + " ..."


def main():
    port, root, script = int(sys.argv[1]), sys.argv[2], os.path.abspath(sys.argv[3])
    args = sys.argv[4:]
    chan = Channel(port)
    dbg = Debugger(chan, root)
    msgs = chan.messages()

    def apply_breakpoints(msg):
        fname = dbg.canonic(msg["file"])
        dbg.clear_all_file_breaks(fname)
        for k in [k for k in dbg.bp_extra if k[0] == fname]:
            del dbg.bp_extra[k]
        for bp in msg.get("breakpoints", []):
            dbg.set_break(fname, int(bp["line"]), cond=bp.get("condition") or None)
            extra = {k: bp.get(k) for k in ("hitMode", "hitCount", "log", "continue") if bp.get(k)}
            if extra:
                old = dbg.bp_extra.get((fname, int(bp["line"])))
                if old != extra:
                    dbg.bp_hits.pop((fname, int(bp["line"])), None)   # new settings: count from zero
                dbg.bp_extra[(fname, int(bp["line"]))] = extra
        dbg._breaks_changed()

    # Breakpoints arrive before the program starts
    for msg in msgs:
        if msg.get("cmd") == "setBreakpoints":
            apply_breakpoints(msg)
        elif msg.get("cmd") == "setExceptionBreakpoints":
            dbg.break_on, dbg.break_all = set(msg.get("types", [])), bool(msg.get("all"))
        elif msg.get("cmd") == "start":
            dbg.stop_on_entry = bool(msg.get("stopOnEntry"))
            break

    def reader():
        for msg in msgs:
            cmd = msg.get("cmd")
            if cmd == "setBreakpoints":
                apply_breakpoints(msg)
            elif cmd == "setExceptionBreakpoints":
                dbg.break_on, dbg.break_all = set(msg.get("types", [])), bool(msg.get("all"))
            elif cmd == "pause":
                dbg.reason_pause = True
                dbg.set_step()
            elif cmd == "stop":
                os._exit(0)
            else:
                dbg.commands.put(msg)
        os._exit(0)    # PyStudio went away
    threading.Thread(target=reader, daemon=True, name="pystudio-debugger").start()

    # Unhandled exceptions (including ones in Qt event handlers) stop like in VS
    def hook(et, ev, tb):
        traceback.print_exception(et, ev, tb)
        sys.stderr.flush()
        if tb is not None:
            dbg.interaction(None, tb, "exception", f"{et.__name__}: {ev}")
    sys.excepthook = hook

    def thread_hook(args):                    # unhandled exception in a threading.Thread: stop there too
        if args.exc_type is SystemExit:
            return
        hook(args.exc_type, args.exc_value, args.exc_traceback)
    threading.excepthook = thread_hook
    dbg.install_thread_breakpoints()

    main_mod = types.ModuleType("__main__")
    main_mod.__file__ = script
    main_mod.__builtins__ = __builtins__
    sys.modules["__main__"] = main_mod
    sys.argv = [script] + args
    sys.path[0] = os.path.dirname(script)
    with open(script, encoding="utf-8") as f:
        code = compile(f.read(), script, "exec")
    chan.send({"event": "started"})
    exit_code = 0
    try:
        dbg.run(code, main_mod.__dict__)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:  # noqa: BLE001
        et, ev, tb = sys.exc_info()
        hook(et, ev, tb.tb_next or tb)
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    chan.send({"event": "exited", "code": exit_code})
    os._exit(exit_code if isinstance(exit_code, int) else 0)


def attach(port, root):
    """Attach to this (already running) program: called by the code PyStudio injects (gdb, or sys.remote_exec on
    Python 3.14+). Everything runs in a background thread; the program keeps running until a breakpoint."""
    if getattr(sys, "monitoring", None) is None:
        sys.stderr.write("PyStudio can attach to Python 3.12 or newer only.\n")
        return

    def run():
        chan = Channel(port)
        dbg = Debugger(chan, os.path.normcase(os.path.abspath(root)) + os.sep if root else "/")
        dbg.attached = True
        dbg.main_ident = None                  # no bdb: every thread (main too) uses sys.monitoring
        old_hooks = (sys.excepthook, threading.excepthook)
        state = {"done": False}

        def detach():
            if state["done"]:
                return
            state["done"] = True
            dbg.breaks.clear()
            dbg.thread_step = None
            dbg.pause_any = False
            if dbg.mon is not None:
                E = sys.monitoring.events
                sys.monitoring.set_events(dbg.mon, 0)
                for ev in (E.PY_START, E.LINE):
                    sys.monitoring.register_callback(dbg.mon, ev, None)
                sys.monitoring.restart_events()
                sys.monitoring.free_tool_id(dbg.mon)
            sys.excepthook, threading.excepthook = old_hooks
            dbg.commands.put({"cmd": "continue"})          # a thread stopped at a breakpoint goes on
            try:
                chan.sock.close()
            except OSError:
                pass
        chan.on_lost = detach

        def hook(et, ev, tb):
            old_hooks[0](et, ev, tb)
            if tb is not None and not state["done"]:
                dbg.interaction(None, tb, "exception", f"{et.__name__}: {ev}")

        def thread_hook(args):
            if args.exc_type is not SystemExit:
                hook(args.exc_type, args.exc_value, args.exc_traceback)
        sys.excepthook, threading.excepthook = hook, thread_hook
        dbg.install_thread_breakpoints()

        def apply_breakpoints(msg):
            fname = dbg.canonic(msg["file"])
            dbg.clear_all_file_breaks(fname)
            for k in [k for k in dbg.bp_extra if k[0] == fname]:
                del dbg.bp_extra[k]
            for bp in msg.get("breakpoints", []):
                dbg.set_break(fname, int(bp["line"]), cond=bp.get("condition") or None)
                extra = {k: bp.get(k) for k in ("hitMode", "hitCount", "log", "continue") if bp.get(k)}
                if extra:
                    dbg.bp_extra[(fname, int(bp["line"]))] = extra
            dbg._breaks_changed()
        chan.send({"event": "attached", "pid": os.getpid(), "python": sys.version.split()[0],
                   "program": " ".join(sys.argv)})
        try:
            for msg in chan.messages():
                cmd = msg.get("cmd")
                if cmd == "setBreakpoints":
                    apply_breakpoints(msg)
                elif cmd in ("setExceptionBreakpoints", "start"):
                    continue
                elif cmd == "pause":
                    dbg.pause_any = True
                    dbg._stepping_changed()
                elif cmd in ("stop", "detach"):
                    break
                elif cmd == "terminate":
                    detach()
                    os._exit(1)
                else:
                    dbg.commands.put(msg)
        except OSError:
            pass
        detach()
    threading.Thread(target=run, daemon=True, name="pystudio-debugger").start()


if __name__ == "__main__":
    main()
