"""Built-in form designer (like the VS Windows Forms designer).

Edits QMainWindow .ui files with absolutely positioned controls (WinForms-style
Location/Size), real containers (GroupBox, Panel), a MenuStrip and a StatusStrip.
"""
import copy
import json
import os
import re
import xml.etree.ElementTree as ET

from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QFontDatabase, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QScrollArea, QStyleFactory, QVBoxLayout, QWidget)

from . import model as M
from . import snaplines
from .menudesigner import MenuBarStrip, MenuDesignerMixin, item_at
from .model import GRID, MENU_H, STATUS_H, TOOL_H, TOOLS
from .toolstrip import ToolBarStrip, ToolStripMixin

HANDLE = 7
MIME_TOOL = "application/x-pystudio-tool"


class UnsupportedForm(Exception):
    pass


def snap(v, enabled=True):
    return int(round(v / GRID) * GRID) if enabled else int(v)


_clip_release_hooked = False


def _release_clipboard_at_exit():
    """Qt frees the clipboard's data in its own C++ shutdown, after Python has stopped; data made in
    Python then crashes PyStudio on exit (Ubuntu's "quit unexpectedly" dialog). So when PyStudio quits
    with copied controls on the clipboard, they're swapped for plain text that Qt owns."""
    global _clip_release_hooked
    if _clip_release_hooked:
        return
    _clip_release_hooked = True
    import atexit
    from PySide6.QtWidgets import QApplication

    def release():
        try:
            cb = QApplication.clipboard()
            md = cb.mimeData() if cb is not None else None
            if md is not None and md.hasFormat(FormDesigner.CLIP_MIME):
                cb.setText(md.text())
        except RuntimeError:
            pass
    QApplication.instance().aboutToQuit.connect(release)
    atexit.register(release)


def _destroy(w):
    """Remove a control from the form right away (deleteLater alone can leave ghosts)."""
    w.hide()
    w.setParent(None)
    w.deleteLater()


def default_form():
    return {"title": "Form1", "back": None, "fore": None, "font": None, "css": "",
            "icon": "", "minsize": (0, 0), "maxsize": (0, 0), "opacity": 100,
            "startpos": "WindowsDefaultLocation", "border": "Sizable", "maxbox": True,
            "minbox": True, "topmost": False, "state": "Normal", "accept": "", "cancel": "", "kind": "",
            "contextmenu": ""}


def form_css(form):
    rules = []
    if form.get("back"):
        rules.append(f"QMainWindow, #centralwidget {{ background-color: {form['back']}; }}")
    if form.get("fore"):
        rules.append("#centralwidget QLabel, #centralwidget QCheckBox, #centralwidget QRadioButton,"
                     f" #centralwidget QGroupBox {{ color: {form['fore']}; }}")
    user = (form.get("css") or "").strip()
    if rules:
        return (user + "\n" if user else "") + "/* PyStudio */\n" + "\n".join(rules)
    return user


# ============================================================ strips
class Strip(QWidget):
    """Design-time MenuStrip / StatusStrip shown above/below the form surface."""

    def __init__(self, designer, kind):
        super().__init__()
        self.d = designer
        self.kind = kind
        self.setFixedHeight(MENU_H if kind == "menu" else STATUS_H)
        self.hide()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#f5f5f5") if self.kind == "menu" else QColor("#ececec"))
        p.setPen(QColor("#cfcfcf"))
        if self.kind == "menu":
            p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
            p.setPen(QColor("#000000"))
            x = 8
            for it in self.d.menu or []:
                text = it.get("text", "").replace("&", "")
                w = self.fontMetrics().horizontalAdvance(text) + 16
                p.drawText(QRect(x, 0, w, self.height()), Qt.AlignCenter, text)
                x += w
            if not self.d.menu:
                p.setPen(QColor("#888888"))
                p.drawText(QRect(8, 0, 300, self.height()), Qt.AlignVCenter, "Double-click to edit the menu")
        else:
            p.drawLine(0, 0, self.width(), 0)
            p.setPen(QColor("#888888"))
            p.drawText(QRect(8, 0, 300, self.height()), Qt.AlignVCenter, "StatusStrip")
        if self.d.special == self.kind:
            p.setPen(QPen(QColor("#3399ff"), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.rect().adjusted(1, 1, -1, -1))

    def mousePressEvent(self, e):
        self.d.select_special(self.kind)
        self.d.overlay.setFocus()

    def mouseDoubleClickEvent(self, e):
        if self.kind == "menu":
            self.d.edit_menu()


class ComponentTray(QWidget):
    """Strip under the form for non-visual components (Timer, dialogs), like VS."""

    ITEM_W = 170

    def __init__(self, designer):
        super().__init__()
        self.d = designer
        self.setFixedHeight(40)
        self.hide()

    def _rects(self):
        out = []
        for i, c in enumerate(self.d.components):
            out.append((QRect(8 + i * (self.ITEM_W + 6), 6, self.ITEM_W, 28), c))
        return out

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#e9e9e9"))
        p.setPen(QColor("#c8c8c8"))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        for r, c in self._rects():
            sel = self.d.special == "comp:" + c["name"]
            p.setPen(QPen(QColor("#3399ff"), 2) if sel else QColor("#b0b0b0"))
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(r, 3, 3)
            p.setPen(QColor("#000000"))
            p.drawText(r.adjusted(8, 0, -4, 0), Qt.AlignVCenter,
                       f"{M.COMPONENT_ICONS.get(c['type'], '■')}  {c['name']}")

    def _at(self, pos):
        return next((c for r, c in self._rects() if r.contains(pos)), None)

    def mousePressEvent(self, e):
        c = self._at(e.position().toPoint())
        if c:
            self.d.select_special("comp:" + c["name"])
            self.d.overlay.setFocus()

    def mouseDoubleClickEvent(self, e):
        c = self._at(e.position().toPoint())
        if c:
            self.d.handler_requested.emit(c["name"])

    def sizeHint(self):
        return QSize(max(200, 8 + len(self.d.components) * (self.ITEM_W + 6)), 40)


class MenuEditor(QDialog):
    """Edit a MenuStrip as indented text (one item per line)."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Menu")
        self.resize(520, 480)
        help_ = QLabel(
            "One item per line. Indent 4 spaces (or Tab) for items under a menu.\n"
            "&amp; marks the Alt-key letter, a line with just - is a separator,\n"
            "and a shortcut goes in brackets at the end, e.g.   &Open...  [Ctrl+O]")
        help_.setTextFormat(Qt.PlainText)
        help_.setText(help_.text().replace("&amp;", "&"))
        help_.setStyleSheet("color: #bbbbbb;")
        self.edit = QPlainTextEdit(text or "&File\n    &New  [Ctrl+N]\n    &Open...  [Ctrl+O]\n"
                                   "    &Save  [Ctrl+S]\n    -\n    E&xit\n&Help\n    &About")
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        f.setPointSize(11)
        self.edit.setFont(f)
        self.edit.setTabStopDistance(self.edit.fontMetrics().horizontalAdvance(" ") * 4)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(help_)
        lay.addWidget(self.edit)
        lay.addWidget(buttons)


# ============================================================ selection overlay
class Overlay(QWidget):
    """Transparent layer over the form surface: all mouse/keyboard design actions."""

    def __init__(self, designer, surface):
        super().__init__(surface)
        self.d = designer
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.mode = None
        self.rubber = None
        self.snaplines = []       # [(QLine, color)] while dragging/resizing

    # Edit-menu hooks (MainWindow sends Ctrl+Z/X/C/V/A to the focused widget)
    def undo(self): self.d.undo()
    def redo(self): self.d.redo()
    def cut(self): self.d.cut()
    def copy(self): self.d.copy()
    def paste(self): self.d.paste()
    def selectAll(self): self.d.select_many([c for c in self.d.controls
                                             if c.parentWidget() is self.d.surface])

    # ---------------------------------------------------------------- paint
    def paintEvent(self, _):
        p = QPainter(self)
        d = self.d
        # outline borderless containers / empty picture boxes so they can be found
        p.setPen(QPen(QColor("#9a9a9a"), 1, Qt.DotLine))
        for c in d.controls:
            t = M.tool_of(c)
            if (t == "Panel" and c._ps.get("border") == "None") or \
                    (t == "PictureBox" and not c._ps.get("image") and c._ps.get("border") == "None"):
                p.drawRect(d.sgeo(c).adjusted(0, 0, -1, -1))
            if t == "SplitContainer":
                for pn in M.panels_of(c):
                    p.drawRect(d.sgeo(pn).adjusted(1, 1, -2, -2))
        sel = d.selection
        single = len(sel) == 1
        for c in sel:
            r = d.sgeo(c)
            p.setPen(QPen(QColor("#3399ff"), 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(r.adjusted(-1, -1, 0, 0))
            p.setPen(QColor("#000000"))
            p.setBrush(QColor("#ffffff") if c is d.selected else QColor("#000000"))
            if c._ps.get("locked"):
                p.setBrush(QColor("#c0c0c0"))
            for k, h in self._handles(r).items():
                if single or k in ("tl", "tr", "bl", "br"):
                    p.drawRect(h)
        if self.rubber is not None:
            p.setPen(QPen(QColor("#3399ff"), 1, Qt.DashLine))
            p.setBrush(QColor(51, 153, 255, 40))
            p.drawRect(self.rubber)
        for line, color in self.snaplines:
            p.setPen(QPen(QColor(color), 1))
            p.drawLine(line)
        if self.mode == "move" and getattr(self, "drop_target", None) is not None:
            p.setPen(QPen(QColor("#33aa33"), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(d.sgeo(self.drop_target).adjusted(-2, -2, 1, 1))
        if d.tab_mode:
            f = QFont(self.font())
            f.setBold(True)
            f.setPointSize(9)
            p.setFont(f)
            for i, c in enumerate(d.current_tab_order(), 1):
                r = d.sgeo(c)
                badge = QRect(r.left(), r.top(), 22, 18)
                done = c in d.tab_seq
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#007acc") if done else QColor("#5a5a5a"))
                p.drawRect(badge)
                p.setPen(QColor("#ffffff"))
                p.drawText(badge, Qt.AlignCenter, str(i))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#888888"))
        p.drawRect(self.width() - 7, self.height() - 7, 6, 6)  # form resize grip

    @staticmethod
    def _handles(r):
        cx, cy = r.center().x(), r.center().y()
        pts = {"tl": (r.left(), r.top()), "t": (cx, r.top()), "tr": (r.right(), r.top()),
               "l": (r.left(), cy), "r": (r.right(), cy),
               "bl": (r.left(), r.bottom()), "b": (cx, r.bottom()), "br": (r.right(), r.bottom())}
        return {k: QRect(x - HANDLE // 2, y - HANDLE // 2, HANDLE, HANDLE) for k, (x, y) in pts.items()}

    def _handle_at(self, pos):
        d = self.d
        if len(d.selection) != 1 or d.selected._ps.get("locked"):
            return None
        for k, r in self._handles(d.sgeo(d.selected)).items():
            if r.adjusted(-2, -2, 2, 2).contains(pos):
                return k
        return None

    def _form_edge_at(self, pos):
        right = pos.x() >= self.width() - 6
        bottom = pos.y() >= self.height() - 6
        return ("r" if right else "") + ("b" if bottom else "") or None

    CURSORS = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
               "bl": Qt.SizeBDiagCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
               "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "rb": Qt.SizeFDiagCursor}

    # ---------------------------------------------------------------- mouse
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self.setFocus()
        d = self.d
        pos = e.position().toPoint()
        multi_key = bool(e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier))
        self.start = pos
        self.snapshot = d.to_xml_string()
        if d.tab_mode:
            c = d.control_at(pos)
            if c is not None:
                d.tab_click(c)
            self.mode = None
            return
        if d.armed_tool:
            self.mode = "create"
            self.rubber = QRect(pos, QSize(0, 0))
            return
        h = self._handle_at(pos)
        if h:
            self.mode, self.handle, self.orig = "resize", h, QRect(d.sgeo(d.selected))
            return
        # click on a TabControl's tab headers: switch the page (design time), like VS
        for tc in reversed([c for c in d.controls if M.tool_of(c) == "TabControl"]):
            bar = tc.tabBar()
            r = QRect(bar.mapTo(d.surface, QPoint(0, 0)), bar.size())
            if r.contains(pos) and tc.isVisible():
                idx = bar.tabAt(bar.mapFrom(d.surface, pos))
                if idx >= 0 and idx != tc.currentIndex():
                    tc.setCurrentIndex(idx)
                d.select(tc)
                self.mode = None
                self.update()
                return
        ctrl = d.control_at(pos)
        if ctrl is None:
            edge = self._form_edge_at(pos)
            if edge and not multi_key:
                self.mode, self.handle, self.orig = "form", edge, QSize(d.form_size)
                return
            self.mode = "band"
            self.band_base = list(d.selection) if multi_key else []
            self.rubber = QRect(pos, QSize(0, 0))
            if not multi_key:
                d.select(None)
            return
        if multi_key:
            d.select(ctrl, toggle=True)
            self.mode = None
            return
        if ctrl in d.selection:
            d.set_primary(ctrl)
        else:
            d.select(ctrl)
        movers = [c for c in d.top_selection()
                  if not c._ps.get("locked") and c._ps.get("dock", "None") == "None"]
        if not movers:
            self.mode = None
            return
        self.mode = "move"
        self.drop_target = None
        self.origs = {c: d.sgeo(c).topLeft() for c in movers}

    def mouseMoveEvent(self, e):
        d = self.d
        pos = e.position().toPoint()
        use_snap = not (e.modifiers() & Qt.AltModifier)
        if self.mode is None:
            if d.armed_tool:
                self.setCursor(Qt.CrossCursor)
                return
            over = d.control_at(pos)
            h = self._handle_at(pos) or (None if over else self._form_edge_at(pos))
            self.setCursor(self.CURSORS.get(h, Qt.SizeAllCursor if over else Qt.ArrowCursor))
            return
        dx, dy = pos.x() - self.start.x(), pos.y() - self.start.y()
        if self.mode in ("create", "band"):
            self.rubber = QRect(self.start, pos).normalized()
            if self.mode == "band":
                hits = [c for c in d.controls if c.parentWidget() is d.surface
                        and d.sgeo(c).intersects(self.rubber)]
                base = [c for c in self.band_base if c not in hits]
                d.select_many(base + hits, emit=False)
            self.update()
        elif self.mode == "move":
            prim = d.selected if d.selected in self.origs else next(iter(self.origs))
            o = self.origs[prim]
            raw = QRect(QPoint(o.x() + dx, o.y() + dy), prim.size())
            sdx = sdy = None
            if use_snap:
                sdx, sdy, _l = snaplines.snap_move(d, prim, raw, list(self.origs))
            nx = max(0, raw.x() + sdx if sdx is not None else snap(raw.x(), use_snap))
            ny = max(0, raw.y() + sdy if sdy is not None else snap(raw.y(), use_snap))
            ddx, ddy = nx - o.x(), ny - o.y()
            ddx = max(ddx, -min(p.x() for p in self.origs.values()))
            ddy = max(ddy, -min(p.y() for p in self.origs.values()))
            for c, p0 in self.origs.items():
                d.set_sgeo(c, QRect(QPoint(p0.x() + ddx, p0.y() + ddy), c.size()))
            self.snaplines = snaplines.lines_for(d, prim, d.sgeo(prim), list(self.origs)) if use_snap else []
            # show which container the control would drop into
            moved = list(self.origs)
            center = d.sgeo(prim).center()
            target = d.container_at(center, exclude=moved)
            self.drop_target = target if target is not prim.parentWidget() else None
            d.geometry_changed.emit()
            self.update()
        elif self.mode == "resize":
            raw = QRect(self.orig)
            h = self.handle
            if "l" in h:
                raw.setLeft(self.orig.left() + dx)
            if "r" in h:
                raw.setRight(self.orig.right() + dx)
            if "t" in h:
                raw.setTop(self.orig.top() + dy)
            if "b" in h:
                raw.setBottom(self.orig.bottom() + dy)
            snapped, self.snaplines = set(), []
            if use_snap:
                raw, self.snaplines, snapped = snaplines.snap_resize(d, d.selected, raw, h)
            r = QRect(self.orig)
            if "l" in h:
                r.setLeft(min(raw.left() if "l" in snapped else snap(raw.left(), use_snap), r.right() - GRID))
            if "r" in h:
                r.setRight(max(raw.right() if "r" in snapped else snap(raw.right() + 1, use_snap) - 1,
                               r.left() + GRID))
            if "t" in h:
                r.setTop(min(raw.top() if "t" in snapped else snap(raw.top(), use_snap), r.bottom() - GRID))
            if "b" in h:
                r.setBottom(max(raw.bottom() if "b" in snapped else snap(raw.bottom() + 1, use_snap) - 1,
                                r.top() + GRID))
            if use_snap:
                self.snaplines = snaplines.lines_for(d, d.selected, r, [d.selected])
            d.set_sgeo(d.selected, r)
            if d.selected._ps.get("dock", "None") != "None":
                d.apply_docks_all()
            d.geometry_changed.emit()
            self.update()
        elif self.mode == "form":
            w = self.orig.width() + (dx if "r" in self.handle else 0)
            h = self.orig.height() + (dy if "b" in self.handle else 0)
            d.set_form_size(max(120, snap(w, use_snap)), max(80, snap(h, use_snap)))

    def mouseReleaseEvent(self, e):
        d = self.d
        if self.mode == "create":
            tool = d.armed_tool
            r = self.rubber
            if r.width() < GRID or r.height() < GRID:
                r = QRect(QPoint(snap(self.start.x()), snap(self.start.y())), QSize(*TOOLS[tool][2]))
            else:
                r = QRect(snap(r.x()), snap(r.y()), max(GRID, snap(r.width())), max(GRID, snap(r.height())))
            self.rubber = None
            d.disarm()
            d.add_control(tool, r, snapshot=self.snapshot)
        elif self.mode == "band":
            self.rubber = None
            d.selection_changed.emit()
        elif self.mode == "move":
            for c in list(self.origs):
                target = d.container_at(d.sgeo(c).center(), exclude=list(self.origs)) or d.surface
                if target is not c.parentWidget():
                    d.reparent(c, target)
            self.drop_target = None
            if d.to_xml_string() != self.snapshot:
                d.commit(self.snapshot)
        elif self.mode in ("resize", "form"):
            if d.to_xml_string() != self.snapshot:
                d.commit(self.snapshot)
        self.mode = None
        self.snaplines = []
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self.d.tab_mode:
            return
        ctrl = self.d.control_at(e.position().toPoint())
        self.d.handler_requested.emit(ctrl.objectName() if ctrl is not None else "")

    # ------------------------------------------------------------- keyboard
    def keyPressEvent(self, e):
        k = e.key()
        d = self.d
        if d.special == "menuitem" and d.menu_sel:
            if k in (Qt.Key_F2, Qt.Key_Return, Qt.Key_Enter):
                d.begin_menu_edit()
                return
            if k in (Qt.Key_Up, Qt.Key_Down) and e.modifiers() & Qt.ControlModifier:
                d.move_menu_item(d.menu_sel, -1 if k == Qt.Key_Up else 1)
                return
        if k == Qt.Key_Delete:
            d.delete_selected()
            return
        if k == Qt.Key_Escape:
            if d.tab_mode:
                d.toggle_tab_order(False)
                return
            d.disarm()
            d.select(None)
            return
        if d.special == "toolitem" and d.tool_sel is not None and e.modifiers() & Qt.ControlModifier \
                and k in (Qt.Key_Left, Qt.Key_Right):
            d.move_tool_item(d.tool_sel, -1 if k == Qt.Key_Left else 1)
            return
        sel = [c for c in d.top_selection() if not c._ps.get("locked") and c._ps.get("dock", "None") == "None"]
        if sel and k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            snapshot = d.to_xml_string()
            dx = {Qt.Key_Left: -1, Qt.Key_Right: 1}.get(k, 0)
            dy = {Qt.Key_Up: -1, Qt.Key_Down: 1}.get(k, 0)
            for c in sel:
                g = c.geometry()
                if e.modifiers() & Qt.ShiftModifier:   # Shift+arrows resize, like VS
                    c.resize(max(GRID, g.width() + dx), max(GRID, g.height() + dy))
                else:
                    c.move(max(0, g.x() + dx), max(0, g.y() + dy))
            d.commit(snapshot)
            d.geometry_changed.emit()
            self.update()
            return
        super().keyPressEvent(e)

    # ---------------------------------------------------------- drag & drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME_TOOL):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        tool = bytes(e.mimeData().data(MIME_TOOL)).decode()
        if tool in M.SPECIAL_TOOLS or tool in M.COMPONENTS:
            self.d.add_special(tool)
        elif tool in TOOLS:
            pos = e.position().toPoint()
            self.d.add_control(tool, QRect(QPoint(snap(pos.x()), snap(pos.y())), QSize(*TOOLS[tool][2])))
        e.acceptProposedAction()
        self.setFocus()


# ============================================================ form surface
class Surface(QWidget):
    """The form's client area (centralwidget), with grid dots."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("centralwidget")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        self.setPalette(QStyleFactory.create("Fusion").standardPalette())
        self.setStyleSheet(M.SURFACE_CSS)

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setPen(QColor(0, 0, 0, 60))
        for x in range(0, self.width(), GRID * 2):
            for y in range(0, self.height(), GRID * 2):
                p.drawPoint(x, y)


# ============================================================ designer tab
class FormDesigner(ToolStripMixin, MenuDesignerMixin, QWidget):
    dirty_changed = Signal(bool)
    selection_changed = Signal()
    geometry_changed = Signal()
    handler_requested = Signal(str)        # control name ("" = the form)
    armed_changed = Signal(str)
    control_renamed = Signal(str, str)     # old, new
    tab_mode_changed = Signal(bool)

    _clipboard = None  # shared by all designer tabs: list of XML strings

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.project_dir = os.path.dirname(os.path.abspath(path))
        self.controls = []        # all controls; per-parent order = z-order
        self.selection = []       # last = primary
        self.selected = None
        self.special = None       # "menu" / "status" when a strip is selected
        self.armed_tool = None
        self._dirty = False
        self._undo, self._redo = [], []
        self.form_class = "Form1"
        self.form = default_form()
        self.menu = None          # menu model (list) or None = no MenuStrip
        self.status = False       # StatusStrip present
        self.components = []      # [{type, name, props}] - Timer, dialogs
        self.tab_order = []       # control names; [] = creation order
        self.tab_mode = False
        self.tab_seq = []
        self._root_extra = []
        self.form_size = QSize(800, 450)
        self._extra = []
        self._other_top = []
        self._root_attrs = {}
        self._top_extra_props = []

        self.title_bar = QLabel()
        self.title_bar.setFixedHeight(30)
        self.title_bar.setStyleSheet(
            "background: #f0f0f0; color: #000; padding-left: 10px; border: 1px solid #b8b8b8;"
            " border-bottom: 0; font-weight: bold;")
        self._menu_init()
        self._toolstrip_init()
        self.menu_strip = MenuBarStrip(self)
        self.tool_strip = ToolBarStrip(self)
        self.status_strip = Strip(self, "status")
        self.surface = Surface()
        self.overlay = Overlay(self, self.surface)

        frame = QWidget()
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)
        for w in (self.title_bar, self.menu_strip, self.tool_strip, self.surface, self.status_strip):
            fl.addWidget(w)

        self.tray = ComponentTray(self)
        holder = QWidget()
        holder.setObjectName("designHolder")
        holder.setStyleSheet("#designHolder { background: #2d2d30; }")
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(24, 24, 24, 24)
        hl.addWidget(frame, 0, Qt.AlignLeft | Qt.AlignTop)
        hl.addSpacing(16)
        hl.addWidget(self.tray, 0, Qt.AlignLeft)
        hl.addStretch()
        self.holder = holder      # menu drop-downs float on this

        scroll = QScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: 0; background: #2d2d30; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)

        self.load(path)

    # ------------------------------------------------------------ doc API
    @property
    def is_dirty(self):
        return self._dirty

    def _set_dirty(self, v):
        if v != self._dirty:
            self._dirty = v
            self.dirty_changed.emit(v)

    def display_name(self):
        return os.path.basename(self.path) + " [Design]"

    def setFocus(self):
        self.overlay.setFocus()

    # ------------------------------------------------------- coordinates
    def sgeo(self, c):
        """Control rectangle in surface coordinates."""
        return QRect(c.mapTo(self.surface, QPoint(0, 0)), c.size())

    def set_sgeo(self, c, rect):
        p = c.parentWidget().mapFrom(self.surface, rect.topLeft())
        c.setGeometry(QRect(p, rect.size()))

    def children_of(self, parent):
        return [c for c in self.controls if c.parentWidget() is parent]

    def descendants(self, c):
        return [x for x in self.controls if x is not c and c.isAncestorOf(x)]

    def top_selection(self):
        """Selected controls whose container isn't also selected."""
        return [c for c in self.selection
                if not any(o is not c and o.isAncestorOf(c) for o in self.selection)]

    def _inner(self, c, pos):
        """The widget inside container c whose children are at pos (page/panel or c itself)."""
        if M.tool_of(c) == "TabControl":
            page = c.currentWidget()
            return page if page is not None and self.sgeo(page).contains(pos) else None
        if M.tool_of(c) == "SplitContainer":
            return next((pn for pn in M.panels_of(c) if self.sgeo(pn).contains(pos)), None)
        return c

    def control_at(self, pos, exclude=()):
        def search(parent):
            for c in reversed(self.children_of(parent)):
                if c in exclude:
                    continue
                if self.sgeo(c).contains(pos):
                    if M.is_container(c):
                        inner = self._inner(c, pos)
                        return (search(inner) if inner is not None else None) or c
                    return c
            return None
        return search(self.surface)

    def container_at(self, pos, exclude=()):
        excluded = set(exclude)
        for c in exclude:
            excluded.update(self.descendants(c))

        def search(parent):
            for c in reversed(self.children_of(parent)):
                if c in excluded or not M.is_container(c):
                    continue
                if self.sgeo(c).contains(pos):
                    inner = self._inner(c, pos)
                    if inner is None:
                        return None          # e.g. on a TabControl's tab headers
                    return search(inner) or inner
            return None
        return search(self.surface)

    def all_containers(self):
        """Every widget that holds controls directly: the form, panels, group boxes, pages."""
        out = [self.surface]
        for c in self.controls:
            t = M.tool_of(c)
            if t in M.PANELED:
                out += M.panels_of(c)
            elif t in M.CONTAINERS:
                out.append(c)
        return out

    def showEvent(self, e):
        super().showEvent(e)
        # tab pages/split panels only get their real size once shown - lay out docked controls then
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._docks_after_show)

    def _docks_after_show(self):
        try:
            self.apply_docks_all()
        except RuntimeError:
            pass

    def apply_docks_all(self):
        for cont in self.all_containers():
            kids = self.children_of(cont)
            if any(k._ps.get("dock", "None") != "None" for k in kids):
                M.apply_docks(cont, kids)
        self.overlay.update()

    def _write_children(self, parent_el, parent_widget):
        for c in self.children_of(parent_widget):
            el = M.control_to_xml(c)
            parent_el.append(el)
            t = M.tool_of(c)
            if t in M.PANELED:
                for i, page in enumerate(M.panels_of(c)):
                    pel = ET.SubElement(el, "widget", {"class": "QWidget", "name": page.objectName()})
                    if t == "TabControl":
                        a = ET.SubElement(pel, "attribute", name="title")
                        ET.SubElement(a, "string").text = c.tabText(i)
                    self._write_children(pel, page)
            elif M.is_container(c):
                self._write_children(el, c)

    def set_tab_pages(self, c, titles):
        """TabPages editor: one title per line (add, rename, remove pages)."""
        titles = [t for t in titles if t.strip()] or ["tabPage1"]
        while c.count() > len(titles):
            page = c.widget(c.count() - 1)
            for x in [k for k in self.controls if page.isAncestorOf(k)]:
                self.controls.remove(x)
            c.removeTab(c.count() - 1)
            page.setParent(None)
            page.deleteLater()
        taken = set()
        for i, t in enumerate(titles):
            if i < c.count():
                c.setTabText(i, t.strip())
            else:
                page = QWidget()
                page.setObjectName(self.unique_name("tabPage", taken))
                taken.add(page.objectName())
                page.setPalette(c.palette())
                c.addTab(page, t.strip())

    def reparent(self, c, target):
        r = self.sgeo(c)
        self.controls.remove(c)
        c.setParent(target)
        self.set_sgeo(c, r)
        c.setPalette(target.palette())
        c.show()
        c.raise_()
        self.controls.append(c)
        for x in self.descendants(c):       # keep children after their container
            self.controls.remove(x)
            self.controls.append(x)
        self.overlay.raise_()

    # --------------------------------------------------------- load / save
    def load(self, path):
        tree = ET.parse(path)
        self._apply_xml(tree.getroot())
        self._undo.clear()
        self._redo.clear()
        self._set_dirty(False)

    def _apply_xml(self, root):
        top = root.find("widget")
        if top is None or top.get("class") != "QMainWindow":
            raise UnsupportedForm("PyStudio's designer edits QMainWindow forms.")
        central = None
        self._extra = []
        self.status = False
        self.toolbar = None
        tb_el = None
        for child in top.findall("widget"):
            cls = child.get("class")
            if child.get("name") == "centralwidget" and central is None:
                central = child
            elif cls == "QMenuBar":
                pass
            elif cls == "QToolBar" and tb_el is None:
                tb_el = child
            elif cls == "QStatusBar":
                self.status = True
            else:
                self._extra.append(copy.deepcopy(child))
        if central is not None and central.find("layout") is not None:
            raise UnsupportedForm("This form uses Qt layouts. PyStudio's designer uses "
                                  "absolute positioning (like WinForms).")
        hidden = M.hidden_containers(central) if central is not None else []
        if hidden:
            raise UnsupportedForm("This form has containers the WinForms-style designer can't show inside ("
                                  + ", ".join(hidden[:4]) + ("..." if len(hidden) > 4 else "") + ").")
        self.menu = M.menu_from_xml(top)
        if tb_el is not None:
            self.toolbar_from_xml(top, tb_el)
        self.tool_sel = None
        self._other_top = [copy.deepcopy(e) for e in top
                           if e.tag not in ("property", "widget", "action")]
        self._root_attrs = dict(root.attrib)
        self._root_extra = [copy.deepcopy(e) for e in root
                            if e.tag not in ("class", "widget", "tabstops", "resources", "connections",
                                             "customwidgets")]
        cw = root.find("customwidgets")    # PyStudio writes its own entries; keep any others
        self._customwidgets = [copy.deepcopy(e) for e in (cw.findall("customwidget") if cw is not None else [])
                               if e.findtext("class") not in M.CUSTOM_HEADERS
                               and e.findtext("class") not in M.USER_CONTROLS]
        self._resources = copy.deepcopy(root.find("resources"))
        self._connections = copy.deepcopy(root.find("connections"))
        ts = root.find("tabstops")
        self.tab_order = [t.text for t in ts.findall("tabstop")] if ts is not None else []
        p = M.read_props(top)
        try:
            self.components = json.loads(p.get("components") or "[]")
        except ValueError:
            self.components = []
        self.form_class = top.get("name") or root.findtext("class") or "Form1"
        f = default_form()
        f["title"] = p.get("windowTitle", self.form_class)
        f["font"] = p.get("font")
        css = p.get("styleSheet", "")
        f["css"] = M.split_css(css)
        gen = css.split("/* PyStudio */")[-1] if "/* PyStudio */" in css else ""
        m = re.search(r"background-color:\s*([^;]+);", gen)
        f["back"] = m.group(1).strip() if m else None
        m = re.search(r"(?<![-\w])color:\s*([^;]+);", gen.replace("background-color", "bg"))
        f["fore"] = m.group(1).strip() if m else None
        f["icon"] = p.get("windowIcon", "")
        f["minsize"] = p.get("minimumSize", (0, 0))
        mx = p.get("maximumSize", (0, 0))
        f["maxsize"] = (0, 0) if mx[0] >= 16777215 else mx
        f["opacity"] = int(round(p.get("windowOpacity", 1.0) * 100))
        f["startpos"] = p.get("startPosition", "WindowsDefaultLocation")
        f["border"] = p.get("borderStyle", "Sizable")
        f["maxbox"] = p.get("maximizeBox", True)
        f["minbox"] = p.get("minimizeBox", True)
        f["topmost"] = p.get("topMost", False)
        f["state"] = p.get("windowState", "Normal")
        f["accept"] = p.get("acceptButton", "")
        f["cancel"] = p.get("cancelButton", "")
        f["kind"] = p.get("formKind", "")
        f["contextmenu"] = p.get("contextMenuStrip", "")
        self.form = f
        handled = {"geometry", "windowTitle", "font", "styleSheet", "windowIcon", "minimumSize",
                   "maximumSize", "windowOpacity", "startPosition", "borderStyle", "maximizeBox",
                   "minimizeBox", "topMost", "windowState", "components", "acceptButton", "cancelButton",
                   "formKind", "contextMenuStrip"}
        self._top_extra_props = [copy.deepcopy(x) for x in top.findall("property")
                                 if x.get("name") not in handled]
        g = p.get("geometry", (0, 0, 800, 450))

        for c in self.controls:
            _destroy(c)
        self.controls = []
        self.selection = []
        self.selected = None
        self.special = None
        if central is not None:
            for el in central.findall("widget"):
                created = M.control_from_xml(el, self.surface, self.project_dir)
                for c in created:
                    c.show()
                self.controls.extend(created)
        self.set_form_size(g[2], g[3])
        self.apply_form_visuals()
        self.apply_docks_all()
        self._update_tray()
        self.overlay.raise_()
        self.overlay.update()
        self.selection_changed.emit()

    def to_xml(self):
        root = ET.Element("ui", self._root_attrs or {"version": "4.0"})
        ET.SubElement(root, "class").text = self.form_class
        top = ET.SubElement(root, "widget", {"class": "QMainWindow", "name": self.form_class})
        f = self.form
        M._prop(top, "geometry", "rect", (0, 0, self.form_size.width(), self.form_size.height()))
        if any(f["minsize"]):
            M._prop(top, "minimumSize", "size", f["minsize"])
        if any(f["maxsize"]):
            M._prop(top, "maximumSize", "size", f["maxsize"])
        if f.get("font"):
            M._prop(top, "font", "font", f["font"])
        M._prop(top, "windowTitle", "string", f["title"])
        if f.get("icon"):
            M._prop(top, "windowIcon", "iconset", f["icon"])
        if f.get("opacity", 100) < 100:
            M._prop(top, "windowOpacity", "double", f["opacity"] / 100.0)
        css = form_css(f)
        if css:
            M._prop(top, "styleSheet", "string", css)
        for key, name, default in (("startpos", "startPosition", "WindowsDefaultLocation"),
                                   ("border", "borderStyle", "Sizable"),
                                   ("state", "windowState", "Normal")):
            if f.get(key, default) != default:
                M._prop(top, name, "string", f[key], stdset=False)
        for key, name in (("maxbox", "maximizeBox"), ("minbox", "minimizeBox")):
            if not f.get(key, True):
                M._prop(top, name, "bool", False, stdset=False)
        if f.get("topmost"):
            M._prop(top, "topMost", "bool", True, stdset=False)
        names = {c.objectName() for c in self.controls}
        for key, name in (("accept", "acceptButton"), ("cancel", "cancelButton")):
            if f.get(key) and f[key] in names:
                M._prop(top, name, "string", f[key], stdset=False)
        if f.get("kind"):
            M._prop(top, "formKind", "string", f["kind"], stdset=False)
        menus = {c["name"] for c in self.components if c["type"] == "ContextMenuStrip"}
        if f.get("contextmenu") and f["contextmenu"] in menus:
            M._prop(top, "contextMenuStrip", "string", f["contextmenu"], stdset=False)
        if self.components:
            M._prop(top, "components", "string", json.dumps(self.components), stdset=False)
        for p in self._top_extra_props:
            top.append(copy.deepcopy(p))

        central = ET.SubElement(top, "widget", {"class": "QWidget", "name": "centralwidget"})
        s = self.surface.size()
        M._prop(central, "designSize", "string", f"{s.width()},{s.height()}", stdset=False)

        self._write_children(central, self.surface)

        if self.menu is not None:
            M.menu_to_xml(top, self.menu)
        self.toolbar_to_xml(top)
        if self.status:
            ET.SubElement(top, "widget", {"class": "QStatusBar", "name": "statusbar"})
        for e in self._extra:
            top.append(copy.deepcopy(e))
        for e in self._other_top:
            top.append(copy.deepcopy(e))
        for e in self._root_extra:
            root.append(copy.deepcopy(e))
        self._write_customwidgets(root)
        names = {c.objectName() for c in self.controls}
        order = [n for n in self.tab_order if n in names]
        if order:
            ts = ET.SubElement(root, "tabstops")
            for n in order:
                ET.SubElement(ts, "tabstop").text = n
        res = getattr(self, "_resources", None)
        root.append(copy.deepcopy(res) if res is not None else ET.Element("resources"))
        con = getattr(self, "_connections", None)
        root.append(copy.deepcopy(con) if con is not None else ET.Element("connections"))
        ET.indent(root, " ")
        return root

    def _write_customwidgets(self, root):
        """<customwidgets>: tells uic which module provides Chart, WebBrowser and UserControl classes."""
        entries = [copy.deepcopy(e) for e in getattr(self, "_customwidgets", [])]
        done = {e.findtext("class") for e in entries}
        for c in self.controls:
            cls = getattr(c, "_ps_cls", None)
            if cls in done or c._ps.get("raw") is not None:
                continue
            header = M.CUSTOM_HEADERS.get(cls) or (f"{M.USER_CONTROLS[cls]}.h" if cls in M.USER_CONTROLS else None)
            if header:
                e = ET.Element("customwidget")
                ET.SubElement(e, "class").text = cls
                ET.SubElement(e, "extends").text = "QWidget"
                ET.SubElement(e, "header").text = header
                entries.append(e)
                done.add(cls)
        if entries:
            cw = ET.SubElement(root, "customwidgets")
            cw.extend(entries)

    def to_xml_string(self):
        return ET.tostring(self.to_xml(), encoding="unicode")

    def save(self, path=None):
        path = path or self.path
        try:
            data = '<?xml version="1.0" encoding="UTF-8"?>\n' + self.to_xml_string() + "\n"
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(data)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", f"Could not save {path}:\n{e}")
            return False
        self.path = path
        self._set_dirty(False)
        return True

    # ---------------------------------------------------------- undo/redo
    def commit(self, snapshot):
        """Record that a change happened; snapshot = state before it."""
        self.apply_docks_all()
        self._undo.append(snapshot)
        del self._undo[:-200]
        self._redo.clear()
        self._set_dirty(True)

    def change(self, func):
        """Run func as one undoable change."""
        snapshot = self.to_xml_string()
        func()
        self.apply_docks_all()
        if self.to_xml_string() != snapshot:
            self.commit(snapshot)
        self.overlay.update()
        self.menu_strip.update()
        self.tool_strip.update()
        if self.menu_panels:
            self.refresh_menu_panels()

    def _restore(self, xml, push_to):
        names = [c.objectName() for c in self.selection]
        special = self.special
        menu_sel = self.menu_sel
        tool_sel = self.tool_sel
        push_to.append(self.to_xml_string())
        self._apply_xml(ET.fromstring(xml))
        self.select_many([c for c in (self.find_control(n) for n in names) if c is not None])
        if special == "toolitem" and self.toolbar is not None and tool_sel is not None \
                and tool_sel < len(self.toolbar["items"]):
            self.select_tool_item(tool_sel)
        elif special == "toolstrip" and self.toolbar is not None:
            self.select_special("toolstrip")
        elif special == "menuitem":
            if menu_sel and item_at(self.menu, menu_sel) is not None:
                self.select_menu_item(menu_sel)
            elif self.menu is not None:
                self.select_special("menu")
        elif special and not names:
            if not special.startswith("comp:") or self.find_component(special[5:]):
                self.select_special(special)
        self._set_dirty(True)

    def undo(self):
        if self._undo:
            self._restore(self._undo.pop(), self._redo)

    def redo(self):
        if self._redo:
            self._restore(self._redo.pop(), self._undo)

    # ------------------------------------------------------------ queries
    def find_control(self, name):
        return next((c for c in self.controls if c.objectName() == name), None)

    def names(self):
        names = {c.objectName() for c in self.controls} | {self.form_class, "centralwidget"}
        def walk(items):
            for it in items or []:
                if not it.get("sep"):
                    names.add(it.get("name", ""))
                    walk(it.get("items"))
        walk(self.menu)
        for c in self.controls:
            for pn in M.panels_of(c):
                names.add(pn.objectName())
        if self.toolbar is not None:
            names.add(self.toolbar.get("name", ""))
            names |= {it["name"] for it in self.toolbar.get("items", []) if it.get("name")}
        names |= {c["name"] for c in self.components}
        return names

    def unique_name(self, prefix, taken=()):
        n = 1
        while f"{prefix}{n}" in self.names() or f"{prefix}{n}" in taken:
            n += 1
        return f"{prefix}{n}"

    # ---------------------------------------------------------- selection
    def select(self, ctrl, toggle=False):
        self.special = None
        if ctrl is None:
            if not toggle:
                self.selection = []
        elif toggle:
            if ctrl in self.selection:
                self.selection.remove(ctrl)
            else:
                self.selection.append(ctrl)
        else:
            self.selection = [ctrl]
        self._selection_updated()

    def select_many(self, ctrls, emit=True):
        self.special = None
        self.selection = list(ctrls)
        self._selection_updated(emit)

    def select_special(self, kind):
        self.selection = []
        self.selected = None
        self.special = kind
        if kind != "toolitem":
            self.tool_sel = None
        self.tool_strip.update()
        if kind != "menuitem":
            self.menu_sel = None
            self.menu_open = None
            self.refresh_menu_panels()
        self.overlay.update()
        self.menu_strip.update()
        self.status_strip.update()
        self.tray.update()
        self.selection_changed.emit()

    def set_primary(self, ctrl):
        if ctrl in self.selection:
            self.selection.remove(ctrl)
            self.selection.append(ctrl)
            self._selection_updated()

    def _selection_updated(self, emit=True):
        self.selected = self.selection[-1] if self.selection else None
        if self.tool_sel is not None:
            self.tool_sel = None
            self.tool_strip.update()
        if self.menu_panels or self.menu_sel:
            self.menu_sel = self.menu_open = None
            self.refresh_menu_panels()
        self.overlay.update()
        self.menu_strip.update()
        self.status_strip.update()
        self.tray.update()
        if emit:
            self.selection_changed.emit()

    # ----------------------------------------------------------- changes
    def arm(self, tool):
        self.armed_tool = tool if tool in TOOLS else None
        self.overlay.setCursor(Qt.CrossCursor if self.armed_tool else Qt.ArrowCursor)
        self.armed_changed.emit(self.armed_tool or "")

    def disarm(self):
        self.arm(None)

    def add_control(self, tool, rect, snapshot=None):
        snapshot = snapshot or self.to_xml_string()
        parent = self.container_at(rect.topLeft()) or self.surface
        w = M.make_widget(tool, parent)
        name = self.unique_name(TOOLS[tool][1])
        w.setObjectName(name)
        if tool in ("Button", "Label", "CheckBox", "RadioButton", "GroupBox"):
            M.set_text(w, name)
        if tool == "Label":
            w._ps["autosize"] = True
        if tool == "DataGridView":
            M.set_columns(w, ["Column1", "Column2"])
        if tool == "LinkLabel":
            w._ps["autosize"] = True
            M.set_text(w, name)
        if tool == "TreeView":
            M.set_tree_from_text(w, "Node0\n    Node1\n    Node2")
        if tool == "TabControl":
            taken = set()
            for i in range(w.count()):
                pn = self.unique_name("tabPage", taken)
                taken.add(pn)
                w.widget(i).setObjectName(pn)
                w.setTabText(i, pn)
                w.widget(i).setPalette(w.palette())
        if tool == "SplitContainer":
            for i, pn in enumerate(M.panels_of(w), 1):
                pn.setObjectName(f"{name}Panel{i}")
        self.set_sgeo(w, rect)
        M.apply_visuals(w, self.project_dir)
        w.show()
        if tool == "SplitContainer":
            half = (w.width() - w.handleWidth()) // 2
            w.setSizes([half, half])
        self.controls.append(w)
        self.overlay.raise_()
        self.commit(snapshot)
        self.select(w)
        self.overlay.setFocus()
        return w

    def find_component(self, name):
        return next((c for c in self.components if c["name"] == name), None)

    def _update_tray(self):
        self.tray.setVisible(bool(self.components))
        self.tray.setFixedWidth(self.tray.sizeHint().width())
        self.tray.update()

    def add_special(self, tool):
        if tool in M.COMPONENTS:
            prefix, props = M.COMPONENTS[tool]
            name = self.unique_name(prefix)

            def add():
                self.components.append({"type": tool, "name": name, "props": copy.deepcopy(props)})
                self._update_tray()
            self.change(add)
            self.select_special("comp:" + name)
            self.disarm()
            return

        def run():
            if tool == "MenuStrip" and self.menu is None:
                self.menu = []      # empty strip; click "Type Here" to add menus
            if tool == "StatusStrip":
                self.status = True
            if tool == "ToolStrip" and self.toolbar is None:
                self.toolbar = {"name": self.unique_name("toolStrip"), "style": "ImageAndText", "items": []}
            self._update_strips()
        self.change(run)
        self.select_special({"MenuStrip": "menu", "ToolStrip": "toolstrip"}.get(tool, "status"))
        self.disarm()

    def edit_menu(self):
        dlg = MenuEditor(M.menu_to_text(self.menu), self)
        if dlg.exec() == QDialog.Accepted:
            text = dlg.edit.toPlainText()

            def run():
                self.menu = M.menu_from_text(text, self.menu) or None
                self._update_strips()
            self.change(run)
            self.select_special("menu")

    def _update_strips(self):
        self.menu_strip.setVisible(self.menu is not None)
        self.tool_strip.setVisible(self.toolbar is not None)
        self.tool_strip.update()
        self.status_strip.setVisible(self.status)
        self.set_form_size(self.form_size.width(), self.form_size.height())
        self.menu_strip.update()

    def delete_selected(self):
        if self.special == "toolitem" and self.tool_sel is not None:
            self.delete_tool_item(self.tool_sel)
            return
        if self.special == "toolstrip":
            def drop_tb():
                self.toolbar = None
                self._update_strips()
            self.change(drop_tb)
            self.select(None)
            return
        if self.special == "menuitem" and self.menu_sel:
            self.delete_menu_item(self.menu_sel)
            return
        if self.special and self.special.startswith("comp:"):
            name = self.special[5:]

            def drop():
                self.components = [c for c in self.components if c["name"] != name]
                self._update_tray()
            self.change(drop)
            self.select(None)
            return
        if self.special:
            kind = self.special

            def run():
                if kind == "menu":
                    self.menu = None
                else:
                    self.status = False
                self._update_strips()
            self.change(run)
            self.select(None)
            return
        if not self.selection:
            return
        snapshot = self.to_xml_string()
        doomed = []
        for c in self.top_selection():
            doomed += [c] + self.descendants(c)
        for c in doomed:
            if c in self.controls:
                self.controls.remove(c)
        for c in self.top_selection():
            _destroy(c)
        self.commit(snapshot)
        self.select(None)

    def _subtree_xml(self, c):
        holder = ET.Element("widget")
        # write just this control (and everything inside it)
        el = M.control_to_xml(c)
        holder.append(el)
        t = M.tool_of(c)
        if t in M.PANELED:
            for i, page in enumerate(M.panels_of(c)):
                pel = ET.SubElement(el, "widget", {"class": "QWidget", "name": page.objectName()})
                if t == "TabControl":
                    a = ET.SubElement(pel, "attribute", name="title")
                    ET.SubElement(a, "string").text = c.tabText(i)
                self._write_children(pel, page)
        elif M.is_container(c):
            self._write_children(el, c)
        return el

    CLIP_MIME = "application/x-pystudio-controls"

    def copy(self):
        """Ctrl+C: the controls go on the system clipboard, so they paste into any form - in this
        project, another project, or another PyStudio window."""
        top = self.top_selection()
        if top:
            import json
            from PySide6.QtCore import QMimeData
            from PySide6.QtWidgets import QApplication
            ordered = [c for c in self.controls if c in top]
            items = [ET.tostring(self._subtree_xml(c), encoding="unicode") for c in ordered]
            FormDesigner._clipboard = items
            FormDesigner._clip_source = (self.project_dir, os.path.abspath(self.path or ""))
            md = QMimeData()
            md.setData(self.CLIP_MIME, json.dumps({"project": self.project_dir, "form": FormDesigner._clip_source[1],
                                                   "items": items}).encode("utf-8"))
            md.setText(", ".join(c.objectName() for c in ordered))
            QApplication.clipboard().setMimeData(md)
            _release_clipboard_at_exit()

    def _clip_items(self):
        """(items, source project dir, source form) from the system clipboard, else PyStudio's own."""
        import json
        from PySide6.QtWidgets import QApplication
        md = QApplication.clipboard().mimeData()
        if md is not None and md.hasFormat(self.CLIP_MIME):
            try:
                data = json.loads(bytes(md.data(self.CLIP_MIME)).decode("utf-8"))
                return data.get("items") or [], data.get("project"), data.get("form")
            except ValueError:
                pass
        src = getattr(FormDesigner, "_clip_source", (None, None))
        return FormDesigner._clipboard or [], src[0], src[1]

    def _bring_images(self, xml, src_project):
        """Pictures referenced by pasted controls from another project are copied into this one."""
        if not src_project or not self.project_dir or os.path.abspath(src_project) == os.path.abspath(self.project_dir):
            return
        import shutil
        for rel in re.findall(r"<pixmap>([^<]+)</pixmap>", xml) + re.findall(r'<string>([^<]+\.(?:png|jpg|jpeg|gif|bmp|ico|svg))</string>', xml, re.I):
            if os.path.isabs(rel):
                continue
            src, dst = os.path.join(src_project, rel), os.path.join(self.project_dir, rel)
            if os.path.isfile(src) and not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)

    def cut(self):
        self.copy()
        self.delete_selected()

    def paste(self):
        items, src_project, src_form = self._clip_items()
        if not items:
            return
        same_form = src_form and self.path and os.path.abspath(self.path) == src_form
        snapshot = self.to_xml_string()
        target = self.selected if (self.selected is not None and M.is_container(self.selected)
                                   and len(self.selection) == 1) else self.surface
        pasted = []
        taken = set()
        existing = set(self.names())
        for xml in items:
            self._bring_images(xml, src_project)
            created = M.control_from_xml(ET.fromstring(xml), target, self.project_dir)
            renamed = False
            for w in created:
                tool = M.tool_of(w)
                prefix = TOOLS[tool][1] if tool in TOOLS else "control"
                old = w.objectName()
                if old and old not in existing and old not in taken:
                    taken.add(old)                  # another form: keep the name, like VS
                else:
                    renamed = True
                    w.setObjectName(self.unique_name(prefix, taken))
                    taken.add(w.objectName())
                    if M.get_text(w) == old:
                        M.set_text(w, w.objectName())
                M.apply_visuals(w, self.project_dir)
                w.show()
                for i, pn in enumerate(M.panels_of(w), 1):
                    keep = pn.objectName()
                    if keep and keep not in existing and keep not in taken:
                        taken.add(keep)
                        continue
                    if M.tool_of(w) == "TabControl":
                        pn.setObjectName(self.unique_name("tabPage", taken))
                    else:
                        pn.setObjectName(f"{w.objectName()}Panel{i}")
                    taken.add(pn.objectName())
            top = created[0]
            g = top.geometry()
            if same_form or renamed:                # on the same form, offset so the copy is visible
                top.move(g.x() + GRID * 2, g.y() + GRID * 2)
            self.controls.extend(created)
            pasted.append(top)
        self.overlay.raise_()
        self.commit(snapshot)
        self.select_many(pasted)

    # ------------------------------------------------------- Format menu
    def _format(self, func):
        if not self.selection:
            return
        self.change(func)
        self.geometry_changed.emit()

    def align(self, how):
        """Align to the primary selection (white handles), like VS."""
        if len(self.selection) < 2:
            return
        ref = self.sgeo(self.selected)

        def run():
            for c in self.selection:
                g = self.sgeo(c)
                x, y = g.x(), g.y()
                if how == "lefts": x = ref.x()
                elif how == "rights": x = ref.x() + ref.width() - g.width()
                elif how == "centers": x = ref.x() + (ref.width() - g.width()) // 2
                elif how == "tops": y = ref.y()
                elif how == "bottoms": y = ref.y() + ref.height() - g.height()
                elif how == "middles": y = ref.y() + (ref.height() - g.height()) // 2
                self.set_sgeo(c, QRect(QPoint(max(0, x), max(0, y)), g.size()))
        self._format(run)

    def make_same(self, how):
        if len(self.selection) < 2:
            return
        ref = self.selected.size()

        def run():
            for c in self.selection:
                w = ref.width() if how in ("width", "both") else c.width()
                h = ref.height() if how in ("height", "both") else c.height()
                c.resize(w, h)
        self._format(run)

    def center_in_form(self, how):
        """Center the selection as a group within its container."""
        def run():
            top = self.top_selection()
            parent = top[0].parentWidget()
            left = min(c.x() for c in top)
            up = min(c.y() for c in top)
            right = max(c.x() + c.width() for c in top)
            bottom = max(c.y() + c.height() for c in top)
            dx = (parent.width() - (right - left)) // 2 - left if how == "h" else 0
            dy = (parent.height() - (bottom - up)) // 2 - up if how == "v" else 0
            for c in top:
                c.move(max(0, c.x() + dx), max(0, c.y() + dy))
        self._format(run)

    def z_order(self, front):
        def run():
            for c in [c for c in self.controls if c in self.top_selection()]:
                self.controls.remove(c)
                if front:
                    self.controls.append(c)
                    c.raise_()
                else:
                    self.controls.insert(0, c)
                    c.lower()
            self.overlay.raise_()
        self._format(run)

    # -------------------------------------------------------------- form
    def set_form_size(self, w, h):
        self.form_size = QSize(w, h)
        bars = (MENU_H if self.menu is not None else 0) + (STATUS_H if self.status else 0) + \
            (TOOL_H if getattr(self, "toolbar", None) is not None else 0)
        sh = max(40, h - bars)
        self.surface.setFixedSize(w, sh)
        self.title_bar.setFixedWidth(w)
        self.menu_strip.setFixedWidth(w)
        self.tool_strip.setFixedWidth(w)
        self.tool_strip.setVisible(getattr(self, "toolbar", None) is not None)
        self.status_strip.setFixedWidth(w)
        self.menu_strip.setVisible(self.menu is not None)
        self.status_strip.setVisible(self.status)
        self.overlay.setGeometry(0, 0, w, sh)
        self.geometry_changed.emit()
        if getattr(self, "menu_panels", None):
            self.refresh_menu_panels()
        if getattr(self, "controls", None):
            self.apply_docks_all()

    def apply_form_visuals(self):
        f = self.form
        self.title_bar.setText(f["title"])
        self.surface.setStyleSheet(M.SURFACE_CSS + "\n" + form_css(f))
        self.surface.setFont(M.font_from_dict(f.get("font")))
        for c in self.controls:
            M.apply_visuals(c, self.project_dir)
        self.surface.update()

    def rename_component(self, comp, new):
        new = new.strip()
        if new == comp["name"]:
            return ""
        if not new.isidentifier():
            return "Names must be letters, digits and _ and can't start with a digit."
        if new in self.names():
            return f"'{new}' is already used on this form."
        old = comp["name"]
        comp["name"] = new
        # references to it: controls' ContextMenuStrip, NotifyIcon.ContextMenuStrip, print Document
        for c in self.controls:
            if c._ps.get("contextmenu") == old:
                c._ps["contextmenu"] = new
        if self.form.get("contextmenu") == old:
            self.form["contextmenu"] = new
        for other in self.components:
            for key in ("menu", "document"):
                if other["props"].get(key) == old:
                    other["props"][key] = new
        self.special = "comp:" + new
        self.tray.update()
        self.control_renamed.emit(old, new)
        return ""

    # ------------------------------------------------------- Tab order
    def current_tab_order(self):
        focus = [c for c in self.controls if M.tool_of(c) in M.FOCUSABLE]
        by_name = {c.objectName(): c for c in focus}
        ordered = [by_name[n] for n in self.tab_order if n in by_name]
        return ordered + [c for c in focus if c not in ordered]

    def toggle_tab_order(self, on=None):
        self.tab_mode = (not self.tab_mode) if on is None else on
        self.tab_seq = []
        if self.tab_mode:
            self.disarm()
            self.select(None)
        self.overlay.update()
        self.tab_mode_changed.emit(self.tab_mode)

    def tab_click(self, ctrl):
        """VS Tab Order mode: each click gives the control the next number."""
        if M.tool_of(ctrl) not in M.FOCUSABLE:
            return

        def run():
            order = self.current_tab_order()
            if ctrl in self.tab_seq:
                self.tab_seq.remove(ctrl)
            order.remove(ctrl)
            order.insert(len(self.tab_seq), ctrl)
            self.tab_seq.append(ctrl)
            self.tab_order = [c.objectName() for c in order]
        self.change(run)

    def rename(self, ctrl, new):
        """Returns error text or ''."""
        new = new.strip()
        if ctrl is None or new == ctrl.objectName():
            return ""
        if not new.isidentifier():
            return "Names must be letters, digits and _ and can't start with a digit."
        if new in self.names():
            return f"'{new}' is already used on this form."
        old = ctrl.objectName()
        ctrl.setObjectName(new)
        self.tab_order = [new if n == old else n for n in self.tab_order]
        for c in self.controls:      # data links that name this control (Master, DataBinding, ...)
            ps = c._ps
            if ps.get("master") == old:
                ps["master"] = new
            if ps.get("bindsource") == old:
                ps["bindsource"] = new
            if ps.get("binding", "").startswith(old + "."):
                ps["binding"] = new + ps["binding"][len(old):]
        M.apply_visuals(ctrl, self.project_dir)   # style sheet uses #name
        self.control_renamed.emit(old, new)
        return ""


# ============================================================ toolbox
class Toolbox(QListWidget):
    """VS-style toolbox. Click a tool then draw on the form, double-click to add,
    or drag it onto the form."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.designer = None
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setStyleSheet("QListWidget { background: #252526; border: 0; }"
                           "QListWidget::item { padding: 3px 8px; }"
                           "QListWidget::item:selected { background: #094771; }")
        self.project_group = None
        self._populate()
        self.itemClicked.connect(self._clicked)
        self.itemDoubleClicked.connect(self._double_clicked)
        self.setEnabled(False)

    def set_project_controls(self, project, classes):
        """VS's '<project> Components' group: the project's UserControls."""
        self.project_group = (f"{project} Components", list(classes)) if classes else None
        self.clear()
        self._populate()
        if self.designer is not None:
            self._sync(self.designer.armed_tool or "")

    def _populate(self):
        self.addItem(QListWidgetItem("Pointer"))
        groups = ([self.project_group] if self.project_group else []) + list(M.TOOL_GROUPS)
        for group, tools in groups:
            header = QListWidgetItem("▾ " + group)
            header.setFlags(Qt.NoItemFlags)
            header.setForeground(QColor("#9a9a9a"))
            f = header.font()
            f.setBold(True)
            header.setFont(f)
            self.addItem(header)
            for tool in tools:
                it = QListWidgetItem("    " + tool)
                it.setData(Qt.UserRole, tool)
                cls = TOOLS[tool][0] if tool in TOOLS else {"MenuStrip": "QMenuBar", "StatusStrip": "QStatusBar",
                                                            "ToolStrip": "QToolBar", "Timer": "QTimer",
                                                            "ImageList": "icons"}.get(tool, "component")
                it.setToolTip(f"{tool}  (UserControl in {M.USER_CONTROLS[tool]}.py)" if tool in M.USER_CONTROLS
                              else f"{tool}  ({cls})")
                self.addItem(it)
        self.setCurrentRow(0)

    @staticmethod
    def _tool(item):
        return item.data(Qt.UserRole) or item.text().strip()

    def set_designer(self, d):
        if self.designer is not None:
            try:
                self.designer.armed_changed.disconnect(self._sync)
            except (RuntimeError, TypeError):
                pass
        self.designer = d
        self.setEnabled(d is not None)
        if d is not None:
            d.armed_changed.connect(self._sync)
            self._sync(d.armed_tool or "")

    def _sync(self, tool):
        for i in range(self.count()):
            if self._tool(self.item(i)) == (tool or "Pointer"):
                self.setCurrentRow(i)

    def _clicked(self, item):
        d = self.designer
        if d is None:
            return
        tool = self._tool(item)
        if tool in M.SPECIAL_TOOLS or tool in M.COMPONENTS:
            d.add_special(tool)
            self._sync("")
        else:
            d.arm(tool)

    def _double_clicked(self, item):
        d = self.designer
        tool = self._tool(item)
        if d is not None and tool in TOOLS:
            d.disarm()
            n = len(d.children_of(d.surface))
            pos = QPoint(GRID * 2 + (n % 10) * GRID * 2, GRID * 2 + (n % 10) * GRID * 2)
            d.add_control(tool, QRect(pos, QSize(*TOOLS[tool][2])))

    def startDrag(self, _actions):
        item = self.currentItem()
        tool = self._tool(item) if item else None
        if tool not in TOOLS and tool not in M.SPECIAL_TOOLS and tool not in M.COMPONENTS:
            return
        mime = QMimeData()
        mime.setData(MIME_TOOL, tool.encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)
