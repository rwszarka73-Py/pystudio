"""Diagnostic Tools (Debug > Windows > Diagnostic Tools, Ctrl+Alt+F2): like Visual Studio's window of the
same name. While a program runs (F5 or Ctrl+F5) it graphs the program's memory and CPU every half second;
red marks show where it stopped at a breakpoint. While paused, Take Snapshot counts the live Python
objects by type; each snapshot shows how much it grew or shrank since the one before, so a list or
cache that keeps growing stands out. Double-click a snapshot to see every type."""
import os
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

SAMPLE_MS = 500
try:
    CLK = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError, AttributeError):
    CLK = 100
NCPU = os.cpu_count() or 1


def _mb(b):
    return f"{b / 1048576:,.1f} MB"


def read_proc(pid):
    """(cpu seconds, resident bytes) of a process, or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        ticks = int(parts[11]) + int(parts[12])          # utime + stime
        with open(f"/proc/{pid}/statm") as f:
            rss_pages = int(f.read().split()[1])
        return ticks / CLK, rss_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


class Graph(QWidget):
    def __init__(self, title, color, unit, parent=None):
        super().__init__(parent)
        self.title, self.color, self.unit = title, QColor(color), unit
        self.points = []          # (t, value)
        self.marks = []           # t of breakpoint stops
        self.window = 60.0        # seconds shown
        self.fixed_max = None
        self.setMinimumHeight(70)

    def clear(self):
        self.points, self.marks = [], []
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(4, 16, -4, -4)
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        p.setPen(QColor("#c8c8c8"))
        last = self.points[-1][1] if self.points else None
        cur = "" if last is None else (f"{last:,.1f} {self.unit}")
        p.drawText(QRectF(4, 0, self.width() - 8, 15), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        p.drawText(QRectF(4, 0, self.width() - 8, 15), Qt.AlignRight | Qt.AlignVCenter, cur)
        p.setPen(QPen(QColor("#3c3c3c"), 1))
        p.drawRect(r)
        if len(self.points) < 2:
            return
        t1 = self.points[-1][0]
        t0 = max(self.points[0][0], t1 - self.window)
        span = max(t1 - t0, 1.0)
        vis = [(t, v) for t, v in self.points if t >= t0]
        vmax = self.fixed_max or max(max(v for _t, v in vis) * 1.15, 1.0)

        def pt(t, v):
            return QPointF(r.left() + (t - t0) / span * r.width(), r.bottom() - min(v, vmax) / vmax * r.height())
        for m in self.marks:
            if m >= t0:
                x = r.left() + (m - t0) / span * r.width()
                p.setPen(QPen(QColor("#e51400"), 1.5))
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        path = QPainterPath(pt(*vis[0]))
        for t, v in vis[1:]:
            path.lineTo(pt(t, v))
        fill = QPainterPath(path)
        fill.lineTo(pt(vis[-1][0], 0))
        fill.lineTo(pt(vis[0][0], 0))
        c = QColor(self.color)
        c.setAlpha(50)
        p.fillPath(fill, c)
        p.setPen(QPen(self.color, 1.6))
        p.drawPath(path)
        p.setPen(QColor("#8a8a8a"))
        p.drawText(r.adjusted(3, 1, -3, -1), Qt.AlignTop | Qt.AlignLeft, f"{vmax:,.0f}")


class DiagnosticTools(QWidget):
    def __init__(self, session, runner, parent=None):
        super().__init__(parent)
        self.session, self.runner = session, runner
        self.pid = None
        self.t_start = 0.0
        self._last = None
        self.snapshots = []
        self.elapsed = QLabel("Starts with F5 or Ctrl+F5.")
        self.elapsed.setStyleSheet("color: #9a9a9a;")
        self.mem = Graph("Process Memory (MB)", "#a074c4", "MB")
        self.cpu = Graph("CPU (% of all processors)", "#3c9ad8", "%")
        self.cpu.fixed_max = 100
        self.b_snap = QPushButton("Take Snapshot")
        self.b_snap.setToolTip("Counts the program's live Python objects by type (while paused)")
        self.b_snap.clicked.connect(self.take_snapshot)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Time", "Objects (diff)", "Size (diff)"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.show_snapshot(r))
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.addWidget(self.elapsed)
        v.addWidget(self.mem, 1)
        v.addWidget(self.cpu, 1)
        h = QHBoxLayout()
        h.addWidget(QLabel("Memory Usage"), 1)
        h.addWidget(self.b_snap)
        v.addLayout(h)
        v.addWidget(self.table, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(SAMPLE_MS)
        self.timer.timeout.connect(self.sample)
        self.session.stopped.connect(self._stopped)
        self._update_buttons()

    # ------------------------------------------------------------ sampling
    def start(self, pid):
        self.pid = pid
        self.t_start = time.monotonic()
        self._last = None
        self.snapshots = []
        self.table.setRowCount(0)
        self.mem.clear()
        self.cpu.clear()
        if not pid:
            self.elapsed.setText("Graphs aren't available for programs running on another machine.")
            return
        self.timer.start()
        self.sample()

    def stop(self):
        self.timer.stop()
        if self.pid:
            self.elapsed.setText(self.elapsed.text().replace("Running", "Ended after"))
        self.pid = None
        self._update_buttons()

    def sample(self):
        if not self.pid:
            return
        r = read_proc(self.pid)
        now = time.monotonic()
        if r is None:
            self.stop()
            return
        cpu_s, rss = r
        t = now - self.t_start
        if self._last is not None:
            dt = now - self._last[0]
            pct = max(0.0, (cpu_s - self._last[1]) / dt / NCPU * 100) if dt > 0 else 0.0
            self.cpu.points.append((t, min(pct, 100.0)))
        self._last = (now, cpu_s)
        self.mem.points.append((t, rss / 1048576))
        for g in (self.mem, self.cpu):
            if len(g.points) > 7200:
                del g.points[:1200]
            g.update()
        state = "Paused" if self.session.paused else "Running"
        self.elapsed.setText(f"{state}: {t:,.0f} s  -  {_mb(rss)}")
        self._update_buttons()

    def _stopped(self, _msg):
        if self.pid:
            t = time.monotonic() - self.t_start
            self.mem.marks.append(t)
            self.cpu.marks.append(t)
        self._update_buttons()

    def _update_buttons(self):
        self.b_snap.setEnabled(bool(self.session.active and self.session.paused))

    # ------------------------------------------------------------ snapshots
    def take_snapshot(self):
        if not (self.session.active and self.session.paused):
            return

        def got(msg):
            if "types" not in msg:
                return
            snap = {"time": time.monotonic() - self.t_start, "objects": msg.get("objects", 0),
                    "bytes": msg.get("bytes", 0), "types": {k: (n, b) for k, n, b in msg.get("types", [])}}
            prev = self.snapshots[-1] if self.snapshots else None
            self.snapshots.append(snap)
            r = self.table.rowCount()
            self.table.insertRow(r)
            dn = f" ({snap['objects'] - prev['objects']:+,})" if prev else ""
            db = f" ({(snap['bytes'] - prev['bytes']) / 1024:+,.0f} KB)" if prev else ""
            for c, text in enumerate((str(r + 1), f"{snap['time']:,.1f} s", f"{snap['objects']:,}{dn}",
                                      f"{_mb(snap['bytes'])}{db}")):
                it = QTableWidgetItem(text)
                if c >= 2 and prev and "(+" in text:
                    it.setForeground(QColor("#f48771"))
                self.table.setItem(r, c, it)
        self.session.request({"cmd": "snapshot"}, got)

    def show_snapshot(self, row):
        if 0 <= row < len(self.snapshots):
            SnapshotDialog(self.snapshots[row], self.snapshots[row - 1] if row else None, row + 1, self).exec()


class SnapshotDialog(QDialog):
    def __init__(self, snap, prev, number, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Snapshot #{number}" + (f" (compared with #{number - 1})" if prev else ""))
        self.resize(760, 560)
        types = snap["types"]
        rows = []
        for name, (n, b) in types.items():
            pn, pb = prev["types"].get(name, (0, 0)) if prev else (0, 0)
            rows.append((name, n, n - pn if prev else None, b, b - pb if prev else None))
        if prev:
            rows.sort(key=lambda x: -(x[4] or 0))
        self.rows = rows
        v = QVBoxLayout(self)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter types...")
        self.filter.textChanged.connect(self.fill)
        v.addWidget(self.filter)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Object Type", "Count", "Count Diff.", "Size (bytes)", "Size Diff."])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSortingEnabled(False)
        v.addWidget(self.table, 1)
        note = QLabel("Sizes are each object's own size (sys.getsizeof), not what it refers to. Sorted by how "
                      "much each type grew since the snapshot before." if prev else
                      "Take another snapshot later to see what grew.")
        note.setStyleSheet("color: #9a9a9a;")
        v.addWidget(note)
        self.fill()

    def fill(self):
        words = self.filter.text().lower().split()
        shown = [r for r in self.rows if all(w in r[0].lower() for w in words)]
        self.table.setRowCount(len(shown))
        for i, (name, n, dn, b, db) in enumerate(shown):
            vals = [name, f"{n:,}", "" if dn is None else f"{dn:+,}", f"{b:,}", "" if db is None else f"{db:+,}"]
            for c, text in enumerate(vals):
                it = QTableWidgetItem(text)
                if c:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c in (2, 4) and text.startswith("+") and text != "+0":
                    it.setForeground(QColor("#f48771"))
                self.table.setItem(i, c, it)
