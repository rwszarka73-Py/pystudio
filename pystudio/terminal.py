"""Terminal window (View > Terminal, Ctrl+`) - VS's Developer Terminal: your shell, in the project folder, with
the project's Python first on PATH (its .venv activated when it has one) and referenced projects on PYTHONPATH.

The shell runs on a pseudo-terminal (like any terminal program); pyte (a small pure-Python terminal emulator)
turns its output into a screen of characters, which this widget draws. Colors, cursor keys, Ctrl+C / Ctrl+D /
Ctrl+Z, tab completion, history, resize, scrollback, select + copy, paste all work. Full-screen programs
(nano, htop, less) work too; vim and others that use a second screen buffer get a basic version of it."""
import codecs
import fcntl
import os
import re
import shutil
import signal
import struct
import subprocess
import termios

from PySide6.QtCore import QEvent, QRect, QSocketNotifier, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QKeySequence, QPainter
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMenu, QPushButton, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

try:
    import pyte
except ImportError:          # install.sh installs it; the panel offers to when it's missing
    pyte = None

SCROLLBACK = 5000
DECCKM = 1 << 5              # pyte stores private modes shifted by 5: ?1 = application cursor keys
ALT_SCREENS = (47 << 5, 1047 << 5, 1049 << 5)
BRACKETED_PASTE = 2004 << 5

# VS Code / VS dark terminal palette
PALETTE = {"black": "#000000", "red": "#cd3131", "green": "#0dbc79", "brown": "#e5e510", "yellow": "#e5e510",
           "blue": "#2472c8", "magenta": "#bc3fbc", "cyan": "#11a8cd", "white": "#e5e5e5",
           "brightblack": "#666666", "brightred": "#f14c4c", "brightgreen": "#23d18b", "brightbrown": "#f5f543",
           "brightyellow": "#f5f543", "brightblue": "#3b8eea", "brightmagenta": "#d670d6", "brightcyan": "#29b8db",
           "brightwhite": "#ffffff"}
BRIGHT = {"black": "brightblack", "red": "brightred", "green": "brightgreen", "brown": "brightbrown",
          "blue": "brightblue", "magenta": "brightmagenta", "cyan": "brightcyan", "white": "brightwhite"}
DEFAULT_FG, DEFAULT_BG = "#cccccc", "#1e1e1e"

KEYS = {Qt.Key_Return: "\r", Qt.Key_Enter: "\r", Qt.Key_Backspace: "\x7f", Qt.Key_Tab: "\t", Qt.Key_Backtab: "\x1b[Z",
        Qt.Key_Escape: "\x1b", Qt.Key_Home: "\x1b[H", Qt.Key_End: "\x1b[F", Qt.Key_PageUp: "\x1b[5~",
        Qt.Key_PageDown: "\x1b[6~", Qt.Key_Delete: "\x1b[3~", Qt.Key_Insert: "\x1b[2~",
        Qt.Key_F1: "\x1bOP", Qt.Key_F2: "\x1bOQ", Qt.Key_F3: "\x1bOR", Qt.Key_F4: "\x1bOS", Qt.Key_F5: "\x1b[15~",
        Qt.Key_F6: "\x1b[17~", Qt.Key_F7: "\x1b[18~", Qt.Key_F8: "\x1b[19~", Qt.Key_F9: "\x1b[20~",
        Qt.Key_F10: "\x1b[21~", Qt.Key_F11: "\x1b[23~", Qt.Key_F12: "\x1b[24~"}
ARROWS = {Qt.Key_Up: "A", Qt.Key_Down: "B", Qt.Key_Right: "C", Qt.Key_Left: "D"}


def available():
    global pyte
    if pyte is None:
        try:
            import importlib
            importlib.invalidate_caches()
            pyte = importlib.import_module("pyte")
        except ImportError:
            return False
    return True


_SCREEN_CLASS = None


def _screen_class():
    """TermScreen, made when first needed (pyte may be installed while PyStudio runs)."""
    global _SCREEN_CLASS
    if _SCREEN_CLASS is not None:
        return _SCREEN_CLASS

    class TermScreen(pyte.Screen):
        """pyte's screen plus scrollback (lines scrolled off the top) and the alternate screen (vim, less...)."""

        def __init__(self, columns, lines):
            super().__init__(columns, lines)
            self.history = []
            self._saved = None
            self.responder = None

        def save_cursor(self):
            """xterm keeps ONE saved cursor (ESC 7), and ESC 8 can restore it again and again; pyte uses a
            stack that the first restore empties, so a second ESC 8 jumped to the top-left corner (gh's
            menus drew over themselves)."""
            self.savepoints.clear()
            super().save_cursor()

        def restore_cursor(self):
            keep = self.savepoints[-1] if self.savepoints else None
            super().restore_cursor()
            if keep is not None:
                self.savepoints.append(keep)

        def write_process_input(self, data):
            """Answers the program's questions (where's the cursor, what terminal is this): pyte works them
            out; without sending them back, prompts like gh auth login draw over themselves."""
            if self.responder is not None:
                self.responder(data)

        def index(self):
            top, bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)
            if self.cursor.y == bottom and top == 0 and self._saved is None:
                line = self.buffer[0]
                self.history.append([line[x] for x in range(self.columns)])
                if len(self.history) > SCROLLBACK:
                    del self.history[:len(self.history) - SCROLLBACK]
            super().index()

        def set_mode(self, *modes, **kwargs):
            if kwargs.get("private") and any((m << 5) in ALT_SCREENS for m in modes) and self._saved is None:
                self._saved = ({y: dict(self.buffer[y]) for y in range(self.lines)}, self.cursor.x, self.cursor.y)
                self.erase_in_display(2)
            super().set_mode(*modes, **kwargs)

        def reset_mode(self, *modes, **kwargs):
            super().reset_mode(*modes, **kwargs)
            if kwargs.get("private") and any((m << 5) in ALT_SCREENS for m in modes) and self._saved is not None:
                lines, x, y = self._saved
                self._saved = None
                self.buffer.clear()
                for row, cells in lines.items():
                    if row < self.lines:
                        self.buffer[row].update({c: v for c, v in cells.items() if c < self.columns})
                self.cursor.x, self.cursor.y = min(x, self.columns - 1), min(y, self.lines - 1)
                self.dirty.update(range(self.lines))

    _SCREEN_CLASS = TermScreen
    return TermScreen


def _color(name, default, bold=False):
    if not name or name == "default":
        return QColor(default)
    if bold and name in BRIGHT:
        name = BRIGHT[name]
    if name in PALETTE:
        return QColor(PALETTE[name])
    if len(name) == 6:
        try:
            int(name, 16)
            return QColor("#" + name)
        except ValueError:
            pass
    return QColor(default)


# ============================================================ one terminal
class TerminalWidget(QWidget):
    title_changed = Signal(str)
    exited = Signal(int)

    def __init__(self, cwd, env=None, shell=None, parent=None, command=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.setMouseTracking(False)
        self.setMinimumSize(200, 80)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSize(11)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self._metrics()
        self.cwd = cwd
        self.scroll = 0                     # lines scrolled back into history
        self.sel = None                     # ((row, col), (row, col)) in "virtual" rows (history + screen)
        self._press = None
        self.exit_code = None
        self.screen = _screen_class()(80, 24)
        self.screen.responder = self._respond
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.master, slave = os.openpty()
        os.set_blocking(self.master, False)
        shell = shell or os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
        full_env = dict(os.environ)
        full_env.update(env or {})
        full_env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "PyStudio"})
        args = [shell, "-i"] if os.path.basename(shell) in ("bash", "zsh", "sh", "dash", "fish", "ksh") else [shell]
        if command:                      # a remote project: an SSH shell on the server
            args = list(command)
            from . import remote
            full_env.update({k: v for k, v in remote.ssh_env().items() if k.startswith(("SSH_ASKPASS", "PYSTUDIO"))})
        setsid = shutil.which("setsid")
        if setsid:                       # a new session with the pty as its controlling terminal (job control)
            args = [setsid, "-c"] + args
        self.proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=full_env,
                                     close_fds=True)
        os.close(slave)
        self.notifier = QSocketNotifier(self.master, QSocketNotifier.Read, self)
        self.notifier.activated.connect(self._read)
        self._repaint = QTimer(self, singleShot=True, interval=10)
        self._repaint.timeout.connect(self.update)
        self._blink = True
        self._blink_timer = QTimer(self, interval=550)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._blink_timer.start()
        QTimer.singleShot(0, self._resize_pty)

    def _metrics(self):
        fm = QFontMetrics(self.font())
        self.cw = max(1, fm.horizontalAdvance("M"))
        self.ch = max(1, fm.height())
        self.ascent = fm.ascent()

    # ------------------------------------------------------------ the shell
    def _read(self):
        chunks = []
        while True:
            try:
                data = os.read(self.master, 65536)
            except BlockingIOError:
                break
            except OSError:                  # EIO: the shell exited
                data = b""
            if not data:
                self._ended()
                break
            chunks.append(data)
            if len(chunks) > 64:
                break
        if chunks:
            text = self.decoder.decode(b"".join(chunks))
            if "\x1b]1" in text:
                self._answer_color_queries(text)
            self.stream.feed(text)
            if self.screen.title:
                self.title_changed.emit(self.screen.title)
            self._repaint.start()

    def _respond(self, data):
        try:
            os.write(self.master, data.encode("utf-8"))
        except OSError:
            pass

    _COLOR_QUERY = re.compile(r"\x1b\](1[012]);\?(\x07|\x1b\\)")

    def _answer_color_queries(self, text):
        """OSC 10/11/12 "what are your colors?" (gh, vim, fzf ask to pick light or dark colors)."""
        colors = {"10": "d4d4/d4d4/d4d4", "11": "1e1e/1e1e/1e1e", "12": "aeae/afaf/adad"}
        for m in self._COLOR_QUERY.finditer(text):
            self._respond(f"\x1b]{m.group(1)};rgb:{colors[m.group(1)]}{m.group(2)}")

    def _ended(self):
        if self.exit_code is not None:
            return
        self.notifier.setEnabled(False)
        try:
            self.exit_code = self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.exit_code = -1
        self.stream.feed("\r\n\x1b[90m[Process exited - press Enter to start a new shell]\x1b[0m\r\n")
        self.update()
        self.exited.emit(self.exit_code)

    def write(self, text):
        if self.exit_code is not None:
            return
        data = text.encode("utf-8") if isinstance(text, str) else text
        try:
            while data:
                n = os.write(self.master, data)
                data = data[n:]
        except BlockingIOError:
            QTimer.singleShot(10, lambda d=data: self.write(d))
        except OSError:
            pass

    def run(self, command):
        """Type a command and Enter (Run in Terminal)."""
        self.write(command + "\r")

    def close_shell(self):
        if self.exit_code is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGHUP)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self.proc.terminate()
                except OSError:
                    pass
            self.notifier.setEnabled(False)
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.exit_code = -1
        try:
            os.close(self.master)
        except OSError:
            pass

    def cwd_now(self):
        """The shell's current folder (for New Terminal Here / tests)."""
        try:
            return os.readlink(f"/proc/{self.proc.pid}/cwd")
        except OSError:
            return self.cwd

    def text(self):
        """Everything shown (history + screen) as plain text."""
        lines = ["".join(c.data for c in row).rstrip() for row in self.screen.history]
        lines += [self.screen.buffer[y] and "".join(self.screen.buffer[y][x].data for x in range(self.screen.columns))
                  .rstrip() or "" for y in range(self.screen.lines)]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    # ------------------------------------------------------------ size
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._resize_pty()

    def _resize_pty(self):
        cols = max(20, (self.width() - 8) // self.cw)
        rows = max(4, (self.height() - 4) // self.ch)
        if (cols, rows) != (self.screen.columns, self.screen.lines):
            self.screen.resize(rows, cols)
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass
        self.update()

    # ------------------------------------------------------------ drawing
    def _toggle_blink(self):
        self._blink = not self._blink
        if self.hasFocus():
            self.update(self._cursor_rect())

    def _cursor_rect(self):
        c = self.screen.cursor
        return QRect(4 + c.x * self.cw, 2 + (c.y + self.scroll) * self.ch, self.cw, self.ch)

    def _row(self, v):
        """Cells of virtual row v (history rows first, then the screen)."""
        h = self.screen.history
        if v < len(h):
            return h[v], True
        y = v - len(h)
        line = self.screen.buffer[y]
        return [line[x] for x in range(self.screen.columns)], False

    def _selected(self, v, col):
        if not self.sel:
            return False
        (r1, c1), (r2, c2) = sorted(self.sel)
        if v < r1 or v > r2:
            return False
        if r1 == r2:
            return c1 <= col < c2
        return (v > r1 or col >= c1) and (v < r2 or col < c2)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(DEFAULT_BG))
        font = self.font()
        bold_font = QFont(font)
        bold_font.setBold(True)
        h = len(self.screen.history)
        first = h - self.scroll
        reverse_screen = (pyte.modes.DECSCNM in self.screen.mode)
        for row in range(self.screen.lines):
            v = first + row
            if v < 0:
                continue
            cells, _hist = self._row(v)
            y = 2 + row * self.ch
            x = 0
            while x < len(cells):
                c = cells[x]
                fg = _color(c.fg, DEFAULT_FG, c.bold)
                bg = _color(c.bg, DEFAULT_BG)
                if c.reverse ^ reverse_screen:
                    fg, bg = bg, fg
                if self._selected(v, x):
                    bg = QColor("#264f78")
                # group cells with the same look into one run
                run = c.data or " "
                x2 = x + 1
                while x2 < len(cells):
                    d = cells[x2]
                    if (d.fg, d.bg, d.bold, d.reverse, d.underscore, d.italics) != \
                            (c.fg, c.bg, c.bold, c.reverse, c.underscore, c.italics) or \
                            self._selected(v, x2) != self._selected(v, x):
                        break
                    run += d.data or " "
                    x2 += 1
                rect = QRect(4 + x * self.cw, y, (x2 - x) * self.cw, self.ch)
                if bg != QColor(DEFAULT_BG):
                    p.fillRect(rect, bg)
                if run.strip():
                    f = bold_font if c.bold else font
                    f.setItalic(c.italics)
                    f.setUnderline(c.underscore)
                    p.setFont(f)
                    p.setPen(fg)
                    # draw cell by cell when the font isn't perfectly monospace for these characters
                    if QFontMetrics(f).horizontalAdvance(run) == len(run) * self.cw:
                        p.drawText(4 + x * self.cw, y + self.ascent, run)
                    else:
                        for i, ch in enumerate(run):
                            p.drawText(4 + (x + i) * self.cw, y + self.ascent, ch)
                x = x2
        # cursor
        cur = self.screen.cursor
        if not cur.hidden and self.scroll == 0 and self.exit_code is None:
            r = self._cursor_rect()
            if self.hasFocus():
                if self._blink:
                    p.fillRect(r, QColor("#aeafad"))
                    ch = self.screen.buffer[cur.y][cur.x].data
                    if ch.strip():
                        p.setFont(font)
                        p.setPen(QColor(DEFAULT_BG))
                        p.drawText(r.x(), r.y() + self.ascent, ch)
            else:
                p.setPen(QColor("#aeafad"))
                p.drawRect(r.adjusted(0, 0, -1, -1))
        if self.scroll:
            p.setPen(QColor("#8a8a8a"))
            p.drawText(self.rect().adjusted(0, 2, -8, 0), Qt.AlignRight | Qt.AlignTop,
                       f"scrolled back {self.scroll} lines - type or press End to return")

    # ------------------------------------------------------------ keyboard
    def event(self, e):
        # take Ctrl+C, Ctrl+D, Ctrl+Z, Tab... before PyStudio's menus do (except a few window-level ones)
        if e.type() == QEvent.ShortcutOverride:
            seq = QKeySequence(e.keyCombination())
            keep_for_window = {"Ctrl+`", "Ctrl+Shift+C", "Ctrl+Shift+V", "F5", "Shift+F5", "Ctrl+F5", "Ctrl+Tab",
                               "Ctrl+Shift+Tab", "Ctrl+PgUp", "Ctrl+PgDown"}
            if seq.toString() not in keep_for_window:
                e.accept()
                return True
        if e.type() == QEvent.KeyPress and e.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            self.keyPressEvent(e)
            return True
        return super().event(e)

    def keyPressEvent(self, e):
        if self.exit_code is not None:
            if e.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.restart_requested()
            return
        mods = e.modifiers()
        key = e.key()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        alt = bool(mods & Qt.AltModifier)
        if ctrl and shift and key == Qt.Key_C:
            self.copy()
            return
        if ctrl and shift and key == Qt.Key_V:
            self.paste()
            return
        if shift and key in (Qt.Key_PageUp, Qt.Key_PageDown):
            self.scroll_by((self.screen.lines - 1) * (1 if key == Qt.Key_PageUp else -1))
            return
        seq = None
        if key in ARROWS:
            letter = ARROWS[key]
            if ctrl or alt or shift:
                m = 1 + (shift and 1) + (alt and 2) + (ctrl and 4)
                seq = f"\x1b[1;{m}{letter}"
            else:
                seq = ("\x1bO" if DECCKM in self.screen.mode else "\x1b[") + letter
        elif key in KEYS:
            seq = KEYS[key]
            if key == Qt.Key_Backspace and ctrl:
                seq = "\x08"
        elif ctrl and Qt.Key_A <= key <= Qt.Key_Z:
            seq = chr(key - Qt.Key_A + 1)
        elif ctrl and key in (Qt.Key_BracketLeft, Qt.Key_Backslash, Qt.Key_BracketRight, Qt.Key_Space,
                              Qt.Key_At, Qt.Key_Underscore):
            seq = {Qt.Key_BracketLeft: "\x1b", Qt.Key_Backslash: "\x1c", Qt.Key_BracketRight: "\x1d",
                   Qt.Key_Space: "\x00", Qt.Key_At: "\x00", Qt.Key_Underscore: "\x1f"}[key]
        else:
            seq = e.text()
            if seq and alt:
                seq = "\x1b" + seq
        if seq:
            self.sel = None
            if self.scroll:
                self.scroll = 0
            self.write(seq)
            self._blink = True

    def restart_requested(self):
        pass                                 # the panel replaces it

    def focusNextPrevChild(self, _next):
        return False                         # Tab goes to the shell

    def copy(self):
        t = self.selected_text()
        if t:
            QApplication.clipboard().setText(t)

    def paste(self):
        t = QApplication.clipboard().text()
        if not t:
            return
        t = t.replace("\r\n", "\n").replace("\n", "\r")
        if BRACKETED_PASTE in self.screen.mode:
            t = "\x1b[200~" + t + "\x1b[201~"
        self.scroll = 0
        self.write(t)

    def selected_text(self):
        if not self.sel:
            return ""
        (r1, c1), (r2, c2) = sorted(self.sel)
        out = []
        for v in range(r1, r2 + 1):
            cells, _h = self._row(v)
            a = c1 if v == r1 else 0
            b = c2 if v == r2 else len(cells)
            out.append("".join(c.data for c in cells[a:b]).rstrip())
        return "\n".join(out)

    # ------------------------------------------------------------ mouse: scroll, select, menu
    def scroll_by(self, n):
        self.scroll = max(0, min(len(self.screen.history), self.scroll + n))
        self.update()

    def wheelEvent(self, e):
        self.scroll_by(3 if e.angleDelta().y() > 0 else -3)

    def _cell_at(self, pos):
        col = max(0, min(self.screen.columns, round((pos.x() - 4) / self.cw)))
        row = max(0, min(self.screen.lines - 1, (pos.y() - 2) // self.ch))
        return (len(self.screen.history) - self.scroll + row, col)

    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() == Qt.LeftButton:
            self._press = self._cell_at(e.position().toPoint())
            self.sel = None
            self.update()
        elif e.button() == Qt.MiddleButton:
            t = QApplication.clipboard().text(QApplication.clipboard().Mode.Selection)
            if t:
                self.write(t)

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.LeftButton:
            self.sel = (self._press, self._cell_at(e.position().toPoint()))
            self.update()

    def mouseReleaseEvent(self, _e):
        self._press = None
        if self.sel and self.sel[0] == self.sel[1]:
            self.sel = None
        if self.sel:
            QApplication.clipboard().setText(self.selected_text(), QApplication.clipboard().Mode.Selection)

    def mouseDoubleClickEvent(self, e):
        v, col = self._cell_at(e.position().toPoint())
        cells, _h = self._row(v)
        if col >= len(cells):
            return
        word = lambda c: c.data.strip() and (c.data.isalnum() or c.data in "_-./~:")  # noqa: E731
        a = col
        while a > 0 and word(cells[a - 1]):
            a -= 1
        b = col
        while b < len(cells) and word(cells[b]):
            b += 1
        if b > a:
            self.sel = ((v, a), (v, b))
            self.update()

    def contextMenuEvent(self, e):
        m = QMenu(self)
        a = m.addAction("Copy\tCtrl+Shift+C", self.copy)
        a.setEnabled(bool(self.sel))
        m.addAction("Paste\tCtrl+Shift+V", self.paste)
        m.addSeparator()
        m.addAction("Select All", self.select_all)
        m.addAction("Clear", self.clear)
        m.exec(e.globalPos())

    def select_all(self):
        total = len(self.screen.history) + self.screen.lines
        self.sel = ((0, 0), (total - 1, self.screen.columns))
        self.update()

    def clear(self):
        self.screen.history.clear()
        self.scroll = 0
        self.write("\x0c")                   # Ctrl+L: the shell redraws its prompt at the top


# ============================================================ the panel (tabs)
class TerminalPanel(QWidget):
    """The Terminal dock: tabs of shells. new_terminal() opens one in the project folder."""
    install_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.context = lambda: (os.path.expanduser("~"), {}, "")      # (cwd, env, label) - set by MainWindow
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        b_new = QToolButton()
        b_new.setText("+")
        b_new.setToolTip("New Terminal (Ctrl+Shift+`)")
        b_new.setAutoRaise(True)
        b_new.clicked.connect(lambda: self.new_terminal())
        b_kill = QToolButton()
        b_kill.setText("🗑")
        b_kill.setToolTip("Close this terminal")
        b_kill.setAutoRaise(True)
        b_kill.clicked.connect(lambda: self.close_tab(self.tabs.currentIndex()))
        corner = QWidget()
        cl = QHBoxLayout(corner)
        cl.setContentsMargins(0, 0, 4, 0)
        cl.addWidget(b_new)
        cl.addWidget(b_kill)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)
        self.missing = QWidget()
        mv = QVBoxLayout(self.missing)
        msg = QLabel("The Terminal needs the pyte package (a small terminal emulator) in PyStudio's Python.")
        msg.setWordWrap(True)
        b = QPushButton("Install pyte")
        b.clicked.connect(self.install_requested.emit)
        mv.addWidget(msg)
        mv.addWidget(b, 0, Qt.AlignLeft)
        mv.addStretch()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tabs)
        lay.addWidget(self.missing)
        self.tabs.setVisible(available())
        self.missing.setVisible(not available())

    def current(self):
        return self.tabs.currentWidget()

    def showEvent(self, e):
        """Clicking the Terminal tab opens the first terminal by itself (Ctrl+` is only needed for more)."""
        super().showEvent(e)
        QTimer.singleShot(0, self._ensure_one)

    def _ensure_one(self):
        try:
            if self.isVisible() and available() and self.tabs.count() == 0:
                self.new_terminal()
                t = self.current()
                t is not None and t.setFocus()
        except RuntimeError:          # closing
            pass

    def terminals(self):
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def new_terminal(self, cwd=None):
        if not available():
            return None
        base_cwd, env, label = self.context()
        cwd = cwd or base_cwd
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        from . import remote
        r = remote.remote_for(cwd)
        if r is not None:                # a remote project: the shell runs on the server
            t = TerminalWidget(os.path.expanduser("~"), env, command=remote.terminal_command(r[0], r[1], cwd))
            t.remote_cwd = cwd
            label = f"SSH: {remote.label(r[0])}"
        else:
            t = TerminalWidget(cwd, env)
        t.restart_requested = lambda t=t: self._restart(t)
        name = os.path.basename(cwd.rstrip("/")) or "/"
        n = sum(1 for x in self.terminals() if self.tabs.tabText(self.tabs.indexOf(x)).startswith(name))
        title = name + (f" ({n + 1})" if n else "")
        i = self.tabs.addTab(t, title)
        self.tabs.setTabToolTip(i, cwd + (f"\n{label}" if label else ""))
        self.tabs.setCurrentIndex(i)
        t.setFocus()
        return t

    def _restart(self, old):
        i = self.tabs.indexOf(old)
        cwd = getattr(old, "remote_cwd", None) or (old.cwd_now() if os.path.isdir(old.cwd_now()) else old.cwd)
        t = self.new_terminal(cwd)
        if t is not None and i >= 0:
            self.close_tab(self.tabs.indexOf(old))
            self.tabs.setCurrentWidget(t)

    def close_tab(self, i):
        w = self.tabs.widget(i)
        if w is None:
            return
        self.tabs.removeTab(i)
        w.close_shell()
        w.deleteLater()

    def close_all(self):
        while self.tabs.count():
            self.close_tab(0)

    def ensure_terminal(self):
        return self.current() or self.new_terminal()

    def set_available(self):
        self.tabs.setVisible(available())
        self.missing.setVisible(not available())
