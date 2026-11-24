"""The layout designer: edits Qt Designer forms - with layouts, and dialogs/panels (QDialog, QWidget) - right
in PyStudio, keeping the .ui exactly as Qt Designer would.

The form is shown live (Qt itself lays it out); click to select, drag to move a control to another place in
a layout (a blue line or box shows where it goes), drag from the Toolbox to add one. Lay Out Vertically /
Horizontally / in a Grid / as a Form, Break Layout, spacers, the Object Inspector (every widget, layout and
spacer), properties, and double-click for event code (PyQt's uic.loadUi style or PyStudio's self.ui)."""
import os
import re

from PySide6.QtCore import QBuffer, QByteArray, QDir, QIODevice, QMimeData, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QAction, QColor, QDrag, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFontDialog, QFormLayout, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter, QTabBar,
                               QTabWidget, QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget, QBoxLayout, QSpacerItem)

from .uimodel import CONTAINERS, LAYOUT_CLASSES, PAGED, UiModel

MIME = "application/x-pystudio-qt-widget"
TOOLS = [("Push Button", "QPushButton", "PushButton"), ("Label", "QLabel", "TextLabel"),
         ("Line Edit", "QLineEdit", None), ("Plain Text Edit", "QPlainTextEdit", None),
         ("Check Box", "QCheckBox", "CheckBox"), ("Radio Button", "QRadioButton", "RadioButton"),
         ("Combo Box", "QComboBox", None), ("Spin Box", "QSpinBox", None), ("Double Spin Box", "QDoubleSpinBox", None),
         ("Date Edit", "QDateEdit", None), ("Slider", "QSlider", None), ("Progress Bar", "QProgressBar", None),
         ("List Widget", "QListWidget", None), ("Tree Widget", "QTreeWidget", None),
         ("Table Widget", "QTableWidget", None), ("Group Box", "QGroupBox", "GroupBox"), ("Frame", "QFrame", None),
         ("Tab Widget", "QTabWidget", None), ("Widget (container)", "QWidget", None),
         ("Horizontal Spacer", "spacer:h", None), ("Vertical Spacer", "spacer:v", None)]
DEFAULT_SIGNAL = {"QPushButton": ("clicked", ""), "QToolButton": ("clicked", ""), "QCommandLinkButton": ("clicked", ""),
                  "QCheckBox": ("toggled", "checked"), "QRadioButton": ("toggled", "checked"),
                  "QLineEdit": ("textChanged", "text"), "QPlainTextEdit": ("textChanged", ""),
                  "QTextEdit": ("textChanged", ""), "QComboBox": ("currentIndexChanged", "index"),
                  "QSpinBox": ("valueChanged", "value"), "QDoubleSpinBox": ("valueChanged", "value"),
                  "QSlider": ("valueChanged", "value"), "QDial": ("valueChanged", "value"),
                  "QScrollBar": ("valueChanged", "value"), "QListWidget": ("itemDoubleClicked", "item"),
                  "QTreeWidget": ("itemDoubleClicked", "item, column"),
                  "QTableWidget": ("cellDoubleClicked", "row, column"), "QDateEdit": ("dateChanged", "date"),
                  "QDateTimeEdit": ("dateTimeChanged", "value"), "QTimeEdit": ("timeChanged", "time"),
                  "QTabWidget": ("currentChanged", "index"), "QGroupBox": ("toggled", "checked"),
                  "QListView": ("doubleClicked", "index"), "QTableView": ("doubleClicked", "index"),
                  "QTreeView": ("doubleClicked", "index")}
FIRST_PROPS = ["objectName", "text", "title", "windowTitle", "placeholderText", "toolTip", "enabled", "checkable",
               "checked", "readOnly", "minimum", "maximum", "value", "alignment", "minimumSize", "maximumSize",
               "sizePolicy", "font", "styleSheet", "echoMode", "orientation", "currentIndex"]
SKIP_PROPS = {"geometry", "pos", "size", "x", "y", "width", "height", "frameGeometry", "normalGeometry", "rect",
              "childrenRect", "childrenRegion", "palette", "cursor", "windowIcon", "icon", "pixmap", "locale",
              "inputMethodHints", "windowModality", "windowFilePath", "focus", "visible", "updatesEnabled",
              "sizeHint", "minimumSizeHint", "frameSize", "isActiveWindow", "underMouse", "minimized", "maximized",
              "fullScreen", "modal", "acceptDrops", "autoFillBackground", "contextMenuPolicy", "layoutDirection",
              "mouseTracking", "tabletTracking", "windowOpacity", "accessibleName", "accessibleDescription",
              "baseSize", "sizeIncrement", "whatsThis", "statusTip", "windowIconText", "windowRole",
              "displayText", "selectedText", "hasSelectedText", "acceptableInput", "undoAvailable",
              "redoAvailable", "cursorPosition", "dragEnabled", "cursorMoveStyle", "tabletTracking",
              "documentTitle", "plainText", "html", "markdown", "cleanText", "currentText", "currentData", "count"}


def _rect_in(widget, ancestor):
    return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())


# ============================================================ overlay (mouse, selection, drop indicator)
class Overlay(QWidget):
    def __init__(self, designer):
        super().__init__(designer.canvas)
        self.d = designer
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._press = None
        self.indicator = None          # ("line", QRect) | ("box", QRect)

    # Edit-menu hooks (MainWindow sends Ctrl+Z/C/V/X/Del to the focused widget)
    def undo(self): self.d.undo()
    def redo(self): self.d.redo()
    def copy(self): self.d.copy()
    def cut(self): self.d.copy(); self.d.delete_selected()
    def paste(self): self.d.paste()

    def paintEvent(self, _e):
        p = QPainter(self)
        sel = self.d.selected_rect()
        if sel is not None:
            p.setPen(QPen(QColor("#3399ff"), 2))
            p.drawRect(sel.adjusted(0, 0, -1, -1))
            for x, y in ((sel.left(), sel.top()), (sel.right(), sel.top()), (sel.left(), sel.bottom()),
                         (sel.right(), sel.bottom())):
                p.fillRect(x - 3, y - 3, 6, 6, QColor("#3399ff"))
        for r in self.d.layout_outlines():
            p.setPen(QPen(QColor(200, 60, 60, 120), 1, Qt.DashLine))
            p.drawRect(r.adjusted(0, 0, -1, -1))
        if self.indicator:
            kind, r = self.indicator
            p.setPen(QPen(QColor("#1e88e5"), 3))
            if kind == "line":
                p.drawLine(r.topLeft(), r.bottomRight())
            else:
                p.setBrush(QColor(30, 136, 229, 40))
                p.drawRect(r)

    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() != Qt.LeftButton:
            return
        pos = e.position().toPoint()
        if self.d.click_tab(pos):
            return
        self.d.select_at(pos)
        self._press = pos

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.LeftButton and self.d.selected is not None \
                and (e.position().toPoint() - self._press).manhattanLength() > 8 \
                and self.d.selected is not self.d.model.top and self.d.selected is not self.d.model.central():
            name = self.d.selected.get("name", "")
            self._press = None
            md = QMimeData()
            md.setData(MIME, ("move:" + name).encode())
            drag = QDrag(self)
            drag.setMimeData(md)
            drag.exec(Qt.MoveAction)
            self.indicator = None
            self.update()

    def mouseReleaseEvent(self, _e):
        self._press = None

    def mouseDoubleClickEvent(self, e):
        self.d.select_at(e.position().toPoint())
        self.d.event_code()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Delete:
            self.d.delete_selected()
            return
        super().keyPressEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasFormat(MIME):
            return
        target = self.d.drop_target(e.position().toPoint(), bytes(e.mimeData().data(MIME)).decode())
        self.indicator = target["indicator"] if target else None
        self.update()
        e.acceptProposedAction() if target else e.ignore()

    def dragLeaveEvent(self, _e):
        self.indicator = None
        self.update()

    def dropEvent(self, e):
        what = bytes(e.mimeData().data(MIME)).decode()
        self.indicator = None
        self.d.drop(e.position().toPoint(), what)
        e.acceptProposedAction()


class ToolList(QListWidget):
    def startDrag(self, _actions):
        it = self.currentItem()
        if it is None:
            return
        md = QMimeData()
        md.setData(MIME, ("new:" + it.data(Qt.UserRole)).encode())
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)


# ============================================================ the document
class LayoutDesigner(QWidget):
    dirty_changed = Signal(bool)
    saved = Signal(str)
    open_code = Signal(str, int)          # path, line - after writing an event handler
    control_renamed = Signal(str, str)    # old, new - MainWindow updates the code that uses the form

    def __init__(self, path, project_root=None, external=False, note="", parent=None):
        super().__init__(parent)
        self.path = os.path.abspath(path)
        self.project_root = project_root or os.path.dirname(self.path)
        self.external = external
        with open(path, "rb") as f:
            self.model = UiModel(f.read())
        self.selected = self.model.central()
        self._dirty = False
        self._undo, self._redo = [], []
        self._tab_index = {}
        self.preview = None
        self.by_name = {}
        self.clipboard = None

        # --- info bar
        self.info = QLabel(note or self._default_note())
        self.info.setWordWrap(True)
        self.info.setStyleSheet("background: #2d3a4a; color: #d8e4f0; padding: 5px 8px;")
        self.info_buttons = QHBoxLayout()
        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.addWidget(self.info, 1)
        info_row.addLayout(self.info_buttons)

        # --- layout toolbar
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 2, 6, 2)
        for text, cls, tip in (("Lay Out Vertically", "QVBoxLayout", "Stack the selected container's controls"),
                               ("Horizontally", "QHBoxLayout", "Put them side by side"),
                               ("In a Grid", "QGridLayout", "Rows and columns from where they are now"),
                               ("As a Form", "QFormLayout", "Label / field rows")):
            b = QPushButton(text)
            b.setToolTip(tip + " (select the container - a group box, frame, or the form - first)")
            b.clicked.connect(lambda _c=False, cls=cls: self.lay_out(cls))
            bar.addWidget(b)
        b = QPushButton("Break Layout")
        b.setToolTip("Remove the selected container's layout; controls keep their places")
        b.clicked.connect(self.break_layout)
        bar.addWidget(b)
        self.ck_outlines = QCheckBox("Show layouts")
        self.ck_outlines.setChecked(True)
        self.ck_outlines.toggled.connect(lambda _v: self.overlay.update())
        bar.addWidget(self.ck_outlines)
        b = QPushButton("Preview")
        b.setToolTip("Open the form in its own window to try it (resize it to see the layouts work)")
        b.clicked.connect(self.preview_window)
        bar.addWidget(b)
        bar.addStretch()

        # --- canvas
        self.canvas = QWidget()
        self.canvas.setObjectName("layoutCanvas")
        self.canvas.setStyleSheet("#layoutCanvas { background: #5a5a5a; }")
        self.overlay = Overlay(self)
        scroll = QScrollArea()
        scroll.setWidget(self.canvas)
        scroll.setWidgetResizable(False)
        self.scroll = scroll

        # --- left: toolbox ; right: object inspector + properties
        self.toolbox = ToolList()
        self.toolbox.setDragEnabled(True)
        for text, cls, _t in TOOLS:
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, cls)
            it.setToolTip(f"{cls}  - drag onto the form (into a layout or a container), or double-click to add "
                          "it to the selected container")
            self.toolbox.addItem(it)
        self.toolbox.itemDoubleClicked.connect(lambda it: self.add_to_selected(it.data(Qt.UserRole)))
        self.inspector = QTreeWidget()
        self.inspector.setHeaderLabels(["Object", "Class"])
        self.inspector.setColumnWidth(0, 170)
        self.inspector.itemClicked.connect(lambda it, _c: self.select(it.data(0, Qt.UserRole)))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter properties")
        self.filter.textChanged.connect(lambda _t: self._show_props())
        self.props_box = QWidget()
        self.pform = QFormLayout(self.props_box)
        self.pform.setLabelAlignment(Qt.AlignRight)
        props_scroll = QScrollArea()
        props_scroll.setWidgetResizable(True)
        props_scroll.setWidget(self.props_box)
        right = QSplitter(Qt.Vertical)
        insp = QWidget()
        iv = QVBoxLayout(insp)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.addWidget(QLabel("<b>Object Inspector</b>"))
        iv.addWidget(self.inspector)
        pw = QWidget()
        pv = QVBoxLayout(pw)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(QLabel("<b>Properties</b>"))
        pv.addWidget(self.filter)
        pv.addWidget(props_scroll, 1)
        right.addWidget(insp)
        right.addWidget(pw)
        right.setSizes([220, 420])
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        lv.addWidget(QLabel("<b>Toolbox</b>"))
        lv.addWidget(self.toolbox)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(scroll)
        split.addWidget(right)
        split.setSizes([150, 800, 300])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(info_row)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        for seq, fn in (("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo), ("Ctrl+C", self.copy), ("Ctrl+V", self.paste),
                        ("Ctrl+D", self.duplicate)):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            a.triggered.connect(fn)
            self.addAction(a)
        self.reload()

    def _default_note(self):
        kind = {"QMainWindow": "main window", "QDialog": "dialog"}.get(self.model.form_class, "panel (QWidget)")
        has = self.model.top.find(".//layout") is not None
        return (f"Qt Designer {kind}" + (" with layouts" if has else "") + " - edited as it is, so it keeps working "
                "with Qt Designer and with code that loads it (uic.loadUi). Drag controls into the layouts; "
                "select a container to lay it out.")

    def add_info_button(self, text, fn, tip=""):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.clicked.connect(fn)
        self.info_buttons.addWidget(b)
        return b

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

    def save(self, path=None):
        path = path or self.path
        try:
            with open(path, "wb") as f:
                f.write(self.model.to_bytes())
        except OSError as e:
            QMessageBox.critical(self, "Save failed", f"Could not save {path}:\n{e}")
            return False
        self.path = path
        self._set_dirty(False)
        self.saved.emit(path)
        return True

    # ------------------------------------------------------------ undo
    def change(self, fn):
        before = self.model.snapshot()
        sel = self.selected.get("name") if self.selected is not None else None
        result = fn()
        if self.model.snapshot() != before:
            self._undo.append(before)
            self._redo.clear()
            self._set_dirty(True)
        keep = self.selected if self.selected is not None and self.selected.get("name") in self.model.by_name \
            else self.model.by_name.get(sel)
        self.reload(keep)
        return result

    def undo(self):
        if self._undo:
            self._redo.append(self.model.snapshot())
            self.model.restore(self._undo.pop())
            self._set_dirty(True)
            self.reload()

    def redo(self):
        if self._redo:
            self._undo.append(self.model.snapshot())
            self.model.restore(self._redo.pop())
            self._set_dirty(True)
            self.reload()

    # ------------------------------------------------------------ preview
    def _load_preview(self):
        from PySide6.QtUiTools import QUiLoader
        loader = QUiLoader()
        loader.setWorkingDirectory(QDir(os.path.dirname(self.path)))
        buf = QBuffer()
        buf.setData(QByteArray(self.model.to_bytes()))
        buf.open(QIODevice.ReadOnly)
        w = loader.load(buf, None)
        buf.close()
        if w is None:
            raise ValueError(loader.errorString() or "Qt couldn't build the form")
        return w

    def reload(self, keep=None):
        """Rebuild the live preview from the XML (after every change)."""
        if self.preview is not None:
            for tw in self.preview.findChildren(QTabWidget) + self.preview.findChildren(QStackedWidget):
                if tw.objectName():
                    self._tab_index[tw.objectName()] = tw.currentIndex()
            self.preview.hide()
            self.preview.setParent(None)
            self.preview.deleteLater()
            self.preview = None
        try:
            w = self._load_preview()
        except Exception as e:  # noqa: BLE001 - show it instead of an empty canvas
            w = QLabel(f"Qt couldn't show this form:\n{e}")
            w.setStyleSheet("background: white; padding: 20px;")
        w.setParent(self.canvas)
        w.setWindowFlags(Qt.Widget)
        g = self.model.prop_el(self.model.top, "geometry")
        size = None
        if g is not None and g.find("rect") is not None:
            r = g.find("rect")
            size = (int(r.findtext("width", "400")), int(r.findtext("height", "300")))
        w.resize(*(size or (max(200, w.sizeHint().width()), max(150, w.sizeHint().height()))))
        w.move(12, 12)
        for tw in w.findChildren(QTabWidget) + w.findChildren(QStackedWidget):
            i = self._tab_index.get(tw.objectName())
            if i is not None and i < tw.count():
                tw.setCurrentIndex(i)
        w.show()
        self.preview = w
        self.by_name = {c.objectName(): c for c in w.findChildren(QWidget) if c.objectName()}
        self.by_name[self.model.top.get("name")] = w
        self.layouts = {l.objectName(): l for l in w.findChildren(QLayout) if l.objectName()}
        self.canvas.resize(w.width() + 24, w.height() + 24)
        self.overlay.setGeometry(w.geometry())
        self.overlay.raise_()
        self.selected = keep if keep is not None and (keep is self.model.top or keep.get("name") in self.model.by_name) \
            else self.model.central()
        self._fill_inspector()
        self._show_props()
        self.overlay.update()

    def _settle(self):
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        for l in self.preview.findChildren(QLayout):
            l.activate()

    # ------------------------------------------------------------ geometry helpers
    def widget_for(self, el):
        return self.by_name.get(el.get("name")) if el is not None else None

    def selected_rect(self):
        el = self.selected
        if el is None or self.preview is None:
            return None
        if el.tag == "widget":
            w = self.widget_for(el)
            if w is None or not w.isVisibleTo(self.preview):
                return None
            return _rect_in(w, self.preview)
        if el.tag == "layout":
            l = self.layouts.get(el.get("name"))
            if l is not None and l.parentWidget() is not None:
                g = l.geometry()
                return QRect(l.parentWidget().mapTo(self.preview, g.topLeft()), g.size())
        if el.tag == "spacer":
            return self._spacer_rect(el)
        return None

    def _spacer_rect(self, el):
        lay_el = self.model.layout_of(el)
        l = self.layouts.get(lay_el.get("name")) if lay_el is not None else None
        if l is None:
            return None
        idx = [i for i, it in enumerate(lay_el.findall("item")) if it.find("spacer") is el]
        if not idx or l.parentWidget() is None:
            return None
        item = l.itemAt(idx[0]) if isinstance(l, QBoxLayout) else None
        if item is None:
            for i in range(l.count()):
                if isinstance(l.itemAt(i), QSpacerItem):
                    item = l.itemAt(i)
                    break
        if item is None:
            return None
        g = item.geometry()
        return QRect(l.parentWidget().mapTo(self.preview, g.topLeft()), g.size())

    def layout_outlines(self):
        if not self.ck_outlines.isChecked() or self.preview is None:
            return []
        out = []
        for l in self.layouts.values():
            pw = l.parentWidget()
            if pw is not None and pw.isVisibleTo(self.preview):
                g = l.geometry()
                out.append(QRect(pw.mapTo(self.preview, g.topLeft()), g.size()))
        return out

    def element_at(self, pos):
        """The innermost designer element (named widget) under a point of the preview."""
        if self.preview is None:
            return None
        w = self.preview.childAt(pos)
        while w is not None and w is not self.preview:
            name = w.objectName()
            if name and name in self.model.by_name and self.model.by_name[name].tag == "widget":
                return self.model.by_name[name]
            w = w.parentWidget()
        return self.model.central()

    def click_tab(self, pos):
        """A click on a tab of a QTabWidget switches its page (design time), like Qt Designer."""
        w = self.preview.childAt(pos) if self.preview is not None else None
        if isinstance(w, QTabBar):
            tw = w.parentWidget()
            idx = w.tabAt(w.mapFrom(self.preview, pos))
            if isinstance(tw, QTabWidget) and idx >= 0:
                tw.setCurrentIndex(idx)
                self._tab_index[tw.objectName()] = idx
                if tw.objectName() in self.model.by_name:
                    self.select(self.model.by_name[tw.objectName()])
                self.overlay.update()
                return True
        return False

    def select_at(self, pos):
        self.select(self.element_at(pos))

    def select(self, el):
        if el is None:
            return
        self.selected = el
        self._sync_inspector()
        self._show_props()
        self.overlay.update()

    # ------------------------------------------------------------ drop targets
    def _container_layout_target(self, cont_el, pos):
        """Where a drop into container cont_el (at pos) goes: its layout, or an absolute spot."""
        lay_el = self.model.own_layout(cont_el)
        w = self.widget_for(cont_el)
        if w is None:
            return None
        if lay_el is None:
            if cont_el.get("class") in PAGED:
                return None
            local = w.mapFrom(self.preview, pos)
            r = QRect(pos.x() - 6, pos.y() - 6, 12, 12)
            return {"kind": "abs", "container": cont_el, "point": local, "indicator": ("box", r)}
        return self._layout_target(lay_el, pos)

    def _layout_target(self, lay_el, pos):
        l = self.layouts.get(lay_el.get("name"))
        if l is None or l.parentWidget() is None:
            return None
        pw = l.parentWidget()
        origin = pw.mapTo(self.preview, QPoint(0, 0))
        local = pos - origin
        cls = lay_el.get("class")
        lg = l.geometry()
        if cls in ("QVBoxLayout", "QHBoxLayout"):
            vertical = cls == "QVBoxLayout"
            n = l.count()
            idx = n
            for i in range(n):
                g = l.itemAt(i).geometry()
                mid = g.center().y() if vertical else g.center().x()
                if (local.y() if vertical else local.x()) < mid:
                    idx = i
                    break
            if n == 0:
                line = QRect(lg.left() + 4, lg.top() + 4, lg.width() - 8, 0) if vertical else \
                    QRect(lg.left() + 4, lg.top() + 4, 0, lg.height() - 8)
            elif idx < n:
                g = l.itemAt(idx).geometry()
                line = QRect(g.left(), g.top() - 2, g.width(), 0) if vertical else QRect(g.left() - 2, g.top(), 0, g.height())
            else:
                g = l.itemAt(n - 1).geometry()
                line = QRect(g.left(), g.bottom() + 2, g.width(), 0) if vertical else QRect(g.right() + 2, g.top(), 0, g.height())
            return {"kind": "box", "layout": lay_el, "index": idx, "indicator": ("line", line.translated(origin))}
        if cls in ("QGridLayout", "QFormLayout"):
            if isinstance(l, QGridLayout):
                rows, cols = max(1, l.rowCount()), max(1, l.columnCount())
                cell = None
                for r in range(rows):
                    for c in range(cols):
                        cr = l.cellRect(r, c)
                        if cr.isValid() and cr.contains(local):
                            cell = (r, c, cr)
                if cell is None:                             # below the last row: a new row
                    last = l.cellRect(rows - 1, 0)
                    r = QRect(lg.left(), (last.bottom() if last.isValid() else lg.top()) + 2, lg.width(), 4)
                    return {"kind": "grid", "layout": lay_el, "row": rows, "col": 0, "new_row": False,
                            "indicator": ("line", r.translated(origin))}
                r, c, cr = cell
                if not self.model.cell_taken(lay_el, r, c):
                    return {"kind": "grid", "layout": lay_el, "row": r, "col": c, "new_row": False,
                            "indicator": ("box", cr.translated(origin))}
                above = local.y() < cr.center().y()
                rr = r if above else r + 1
                y = cr.top() - 2 if above else cr.bottom() + 2
                return {"kind": "grid", "layout": lay_el, "row": rr, "col": c, "new_row": True,
                        "indicator": ("line", QRect(lg.left(), y, lg.width(), 0).translated(origin))}
            # QFormLayout: insert a full row
            rows = l.rowCount()
            row = rows
            for r in range(rows):
                for role in (QFormLayout.LabelRole, QFormLayout.FieldRole, QFormLayout.SpanningRole):
                    it = l.itemAt(r, role)
                    if it is not None and local.y() < it.geometry().center().y():
                        row = min(row, r)
            y = lg.bottom()
            for role in (QFormLayout.LabelRole, QFormLayout.FieldRole, QFormLayout.SpanningRole):
                it = l.itemAt(row, role) if row < rows else None
                if it is not None:
                    y = it.geometry().top() - 2
            return {"kind": "form", "layout": lay_el, "row": row,
                    "indicator": ("line", QRect(lg.left(), y, lg.width(), 0).translated(origin))}
        return None

    def drop_target(self, pos, what):
        el = self.element_at(pos)
        if el is None:
            return None
        moving = self.model.by_name.get(what[5:]) if what.startswith("move:") else None
        if moving is not None and (el is moving or self._is_inside(el, moving)):
            return None
        spacer = what.startswith("new:spacer")
        cls = el.get("class")
        # a container (or the form): drop inside it
        is_container = (cls in CONTAINERS and cls not in PAGED) or el is self.model.central() or \
            (self.model.parent.get(el) is not None and self.model.parent[el].get("class") in PAGED)
        if is_container:
            t = self._container_layout_target(el, pos)
            if t and not (spacer and t["kind"] == "abs"):
                return t
        # on a control in a layout: before/after it in that layout
        lay_el = self.model.layout_of(el)
        if lay_el is not None:
            return self._layout_target(lay_el, pos)
        parent = self.model.widget_parent(el)
        if parent is not None and not spacer:
            return self._container_layout_target(parent, pos)
        return None

    def _is_inside(self, el, ancestor):
        p = self.model.parent.get(el)
        while p is not None:
            if p is ancestor:
                return True
            p = self.model.parent.get(p)
        return False

    def drop(self, pos, what):
        target = self.drop_target(pos, what)
        if target is None:
            return
        if what.startswith("move:"):
            el = self.model.by_name.get(what[5:])
            if el is None:
                return
        else:
            el = None

        def run():
            nonlocal el
            m = self.model
            if el is None:
                kind = what[4:]
                if kind.startswith("spacer:"):
                    el = m.new_spacer(kind[-1])
                else:
                    text = next((t[2] for t in TOOLS if t[1] == kind), None)
                    el = m.new_widget(kind, text)
            else:
                # moving within the same box layout: account for the item leaving its old index
                old_lay = m.layout_of(el)
                if target["kind"] == "box" and old_lay is target["layout"]:
                    items = old_lay.findall("item")
                    old = next(i for i, it in enumerate(items) if el in list(it))
                    if old < target["index"]:
                        target["index"] -= 1
                m.detach(el)
            if target["kind"] == "box":
                m.insert_in_box(target["layout"], target["index"], el)
            elif target["kind"] == "grid":
                m.insert_in_grid(target["layout"], target["row"], target["col"], el, target["new_row"])
            elif target["kind"] == "form":
                m.insert_in_grid(target["layout"], target["row"], 0, el, new_row=True, colspan=2)
            else:
                p = target["point"]
                sh = (120, 24) if el.get("class") not in ("QListWidget", "QTableWidget", "QTreeWidget", "QPlainTextEdit",
                                                          "QGroupBox", "QTabWidget", "QFrame", "QWidget") else (200, 120)
                g = m.prop_el(el, "geometry")
                if g is not None and g.find("rect") is not None:
                    r = g.find("rect")
                    sh = (int(r.findtext("width", sh[0])), int(r.findtext("height", sh[1])))
                m.place_absolute(target["container"], el, (max(0, p.x()), max(0, p.y()), sh[0], sh[1]))
        self.change(run)
        if el is not None:
            self.select(el)

    def add_to_selected(self, kind):
        """Toolbox double-click: add at the end of the selected container's layout (or at its top left)."""
        cont = self.selected
        while cont is not None and not (cont.tag == "widget" and (cont.get("class") in CONTAINERS
                                                                    or cont is self.model.central())):
            cont = self.model.widget_parent(cont)
        cont = cont if cont is not None and cont.get("class") not in PAGED else self.model.central()
        w = self.widget_for(cont)
        if w is None:
            return
        lay = self.model.own_layout(cont)
        if lay is not None:
            l = self.layouts.get(lay.get("name"))
            g = l.geometry() if l is not None else QRect()
            pos = w.mapTo(self.preview, QPoint(g.right() - 2, g.bottom() - 2))
        else:
            pos = w.mapTo(self.preview, QPoint(12, 12))
        self.drop(pos, "new:" + kind)

    def delete_selected(self):
        el = self.selected
        if el is None or el is self.model.top or el is self.model.central():
            return
        if el.tag == "layout":
            self.break_layout()
            return
        self.change(lambda: self.model.detach(el))

    def copy(self):
        el = self.selected
        if el is not None and el.tag in ("widget", "spacer") and el is not self.model.top and el is not self.model.central():
            self.clipboard = self.model.copy_el(el)

    def paste(self):
        if self.clipboard is None:
            return
        import copy as _copy
        el = _copy.deepcopy(self.clipboard)
        for sub in el.iter():
            if sub.tag in ("widget", "layout", "spacer") and sub.get("name"):
                sub.set("name", self.model.unique(sub.get("name").rstrip("0123456789") or "widget"))
                self.model.by_name[sub.get("name")] = sub
        sel = self.selected or self.model.central()
        lay = self.model.layout_of(sel) if sel is not self.model.central() else self.model.own_layout(sel)

        def run():
            m = self.model
            if lay is not None and lay.get("class") in ("QVBoxLayout", "QHBoxLayout"):
                items = lay.findall("item")
                idx = next((i + 1 for i, it in enumerate(items) if sel in list(it)), len(items))
                m.insert_in_box(lay, idx, el)
            elif lay is not None:
                rows = [int(i.get("row", 0)) for i in lay.findall("item")]
                m.insert_in_grid(lay, (max(rows) + 1) if rows else 0, 0, el)
            else:
                cont = sel if sel.get("class") in CONTAINERS or sel is self.model.central() else m.widget_parent(sel)
                g = m.prop_el(el, "geometry")
                r = g.find("rect") if g is not None else None
                rect = (int(r.findtext("x", 0)) + 16, int(r.findtext("y", 0)) + 16, int(r.findtext("width", 120)),
                        int(r.findtext("height", 24))) if r is not None else (16, 16, 120, 24)
                m.place_absolute(cont, el, rect)
        self.change(run)
        self.select(el)

    def duplicate(self):
        self.copy()
        self.paste()

    # ------------------------------------------------------------ layouts
    def _container_for_layout_cmd(self):
        el = self.selected or self.model.central()
        if el.tag == "layout":
            el = self.model.widget_parent(el) if self.model.parent.get(el) is not None and \
                self.model.parent[el].tag == "widget" else None
        while el is not None and el.tag != "widget":
            el = self.model.parent.get(el)
        if el is not None and el.get("class") not in CONTAINERS and el is not self.model.central() and \
                not (self.model.parent.get(el) is not None and self.model.parent[el].get("class") in PAGED):
            el = self.model.widget_parent(el)
        return el

    def _geometries(self, container):
        self._settle()
        out = {}
        cw = self.widget_for(container)
        for el in container.iter("widget"):
            w = self.widget_for(el)
            if w is not None and w is not cw and w.parentWidget() is cw:
                g = w.geometry()
                out[el.get("name")] = (g.x(), g.y(), g.width(), g.height())
        return out

    def lay_out(self, cls):
        cont = self._container_for_layout_cmd()
        if cont is None:
            return
        if self.model.own_layout(cont) is not None:
            QMessageBox.information(self, "Lay Out", f"{cont.get('name')} already has a layout. Break Layout first "
                                    "to choose another kind.")
            return
        if not cont.findall("widget"):
            QMessageBox.information(self, "Lay Out", f"{cont.get('name')} has no controls to lay out yet.")
            return
        geos = self._geometries(cont)
        self.change(lambda: self.model.apply_layout(cont, cls, geos))
        self.select(cont)

    def break_layout(self):
        cont = self._container_for_layout_cmd()
        if cont is None or self.model.own_layout(cont) is None:
            return
        geos = {}
        self._settle()
        cw = self.widget_for(cont)
        for el in cont.iter("widget"):
            w = self.widget_for(el)
            if w is not None and w.parentWidget() is cw:
                g = w.geometry()
                geos[el.get("name")] = (g.x(), g.y(), g.width(), g.height())
        self.change(lambda: self.model.break_layout(cont, geos))
        self.select(cont)

    # ------------------------------------------------------------ object inspector
    def _fill_inspector(self):
        self.inspector.clear()
        stack = []
        self._tree_items = {}
        for depth, el in self.model.tree():
            if el.tag == "layout":
                label = el.get("name") or "(layout)"
                cls = el.get("class")
            elif el.tag == "spacer":
                label, cls = el.get("name") or "(spacer)", "Spacer"
            else:
                label, cls = el.get("name") or "(widget)", el.get("class")
            it = QTreeWidgetItem([label, cls])
            it.setData(0, Qt.UserRole, el)
            if el.tag != "widget":
                it.setForeground(0, QColor("#8a8a8a"))
            while len(stack) > depth:
                stack.pop()
            (stack[-1].addChild(it) if stack else self.inspector.addTopLevelItem(it))
            stack.append(it)
            self._tree_items[id(el)] = it
        self.inspector.expandAll()
        self._sync_inspector()

    def _sync_inspector(self):
        it = self._tree_items.get(id(self.selected)) if hasattr(self, "_tree_items") else None
        if it is not None:
            self.inspector.blockSignals(True)
            self.inspector.setCurrentItem(it)
            self.inspector.blockSignals(False)
        # show the page a selected control is on
        el = self.selected
        while el is not None:
            p = self.model.parent.get(el)
            if p is not None and p.get("class") in ("QTabWidget", "QStackedWidget"):
                tw = self.widget_for(p)
                page = self.widget_for(el)
                if tw is not None and page is not None and tw.indexOf(page) >= 0 and tw.currentWidget() is not page:
                    tw.setCurrentWidget(page)
                    self._tab_index[tw.objectName()] = tw.currentIndex()
            el = p

    # ------------------------------------------------------------ properties
    def _clear_props(self):
        while self.pform.rowCount():
            self.pform.removeRow(0)

    def _show_props(self):
        self._clear_props()
        el = self.selected
        if el is None:
            return
        flt = self.filter.text().strip().lower()
        self.pform.addRow(QLabel(f"<b>{el.get('name', '')}</b>  <span style='color:#8a8a8a'>"
                                 f"{el.get('class', el.tag)}</span>"))
        if el.tag == "widget":
            name = QLineEdit(el.get("name", ""))
            name.editingFinished.connect(lambda: self.rename(el, name.text().strip()))
            self.pform.addRow("objectName:", name)
            if el.get("class") in ("QComboBox", "QListWidget"):
                self.pform.addRow("Items:", self._list_button(el, "items"))
            if el.get("class") in ("QTableWidget", "QTreeWidget"):
                self.pform.addRow("Columns:", self._list_button(el, "columns"))
            p = self.model.parent.get(el)
            if p is not None and p.get("class") in PAGED:
                a = el.find("attribute[@name='title']") or el.find("attribute[@name='label']")
                title = QLineEdit(a.findtext("string", "") if a is not None else "")
                title.editingFinished.connect(lambda: self._set_page_title(el, title.text()))
                self.pform.addRow("Page title:", title)
            if el.get("class") in DEFAULT_SIGNAL:
                b = QPushButton(f"⚡ {DEFAULT_SIGNAL[el.get('class')][0]} handler")
                b.setToolTip("Write the event code (double-click the control does the same)")
                b.clicked.connect(self.event_code)
                self.pform.addRow("Event:", b)
            self._widget_props(el, flt)
        elif el.tag == "layout":
            self._layout_props(el)
        elif el.tag == "spacer":
            self._spacer_props(el)

    def _list_button(self, el, what):
        values = self.model.items(el) if what == "items" else self.model.columns(el)
        b = QPushButton(f"(Collection) {len(values)}")

        def edit():
            t, ok = QInputDialog.getMultiLineText(self, what.capitalize(), "One per line:", "\n".join(values))
            if ok:
                lines = [x for x in t.split("\n") if x.strip()]
                self.change(lambda: self.model.set_items(el, lines) if what == "items" else
                            self.model.set_columns(el, lines))
        b.clicked.connect(edit)
        return b

    def _set_page_title(self, el, text):
        def run():
            a = el.find("attribute[@name='title']")
            if a is None:
                import xml.etree.ElementTree as ET
                a = ET.Element("attribute", {"name": "title"})
                ET.SubElement(a, "string")
                el.insert(0, a)
            a.find("string").text = text
        self.change(run)

    def rename(self, el, new):
        old = el.get("name")
        if not new or new == old:
            return
        if not re.fullmatch(r"[A-Za-z_]\w*", new) or new in self.model.by_name:
            QMessageBox.warning(self, "Rename", f"'{new}' isn't a valid, unused name.")
            self._show_props()
            return
        is_widget = el.tag == "widget"
        from .refactor import rename_in_ui
        # the element and every reference to it in the form (tab order, buddies, connections)
        self.change(lambda: self.model.restore(rename_in_ui(self.model.snapshot(), old, new)))
        self.select(self.model.by_name.get(new))
        if is_widget:
            self.save()                       # the form and its code change together
            self.control_renamed.emit(old, new)

    def _widget_props(self, el, flt):
        w = self.widget_for(el)
        if w is None:
            return
        mo = w.metaObject()
        props = []
        for i in range(mo.propertyCount()):
            p = mo.property(i)
            n = p.name()
            if n in SKIP_PROPS or n == "objectName" or not p.isWritable() or not p.isDesignable():
                continue
            if flt and flt not in n.lower():
                continue
            props.append(p)
        props.sort(key=lambda p: (FIRST_PROPS.index(p.name()) if p.name() in FIRST_PROPS else 99, p.name().lower()))
        for p in props:
            editor = self._editor_for(el, w, p)
            if editor is None:
                continue
            label = QLabel(p.name() + ":")
            if self.model.prop_el(el, p.name()) is not None:
                label.setStyleSheet("font-weight: bold;")         # set in the .ui (not a default)
                label.setToolTip("Set in the .ui. Right-click to reset it to the default.")
                label.setContextMenuPolicy(Qt.CustomContextMenu)
                label.customContextMenuRequested.connect(
                    lambda _pos, n=p.name(): self.change(lambda: self.model.remove_prop(el, n)))
            self.pform.addRow(label, editor)

    def _editor_for(self, el, w, p):
        name = p.name()
        v = p.read(w)
        t = p.typeName()
        if p.isEnumType():
            en = p.enumerator()
            scope = en.scope()
            if en.isFlag():
                current = int(v.value) if hasattr(v, "value") else int(v)
                flags = {en.key(i): en.value(i) for i in range(en.keyCount()) if en.value(i)}
                on = [k for k, val in flags.items() if (current & val) == val]
                b = QPushButton(" | ".join(on) or "(none)")

                def edit_flags():
                    t2, ok = QInputDialog.getText(self, name, "Flags, separated by |  (e.g. AlignLeft|AlignVCenter):",
                                                  text="|".join(on))
                    if ok:
                        parts = [x.strip() for x in t2.split("|") if x.strip() in flags]
                        if parts:
                            val = "|".join(f"{scope}::{x}" for x in parts)
                            self.change(lambda: self.model.set_prop(el, name, "set", val))
                b.clicked.connect(edit_flags)
                return b
            cb = QComboBox()
            keys = [en.key(i) for i in range(en.keyCount())]
            cb.addItems(keys)
            current = int(v.value) if hasattr(v, "value") else int(v)
            for i in range(en.keyCount()):
                if en.value(i) == current:
                    cb.setCurrentIndex(i)
                    break
            cb.activated.connect(lambda i: self.change(
                lambda: self.model.set_prop(el, name, "enum", f"{scope}::{keys[i]}")))
            return cb
        if t == "QString":
            e = QLineEdit(v or "")
            e.editingFinished.connect(lambda: self.change(lambda: self.model.set_prop(el, name, "string", e.text()))
                                      if e.text() != (v or "") else None)
            return e
        if t == "bool":
            c = QCheckBox()
            c.setChecked(bool(v))
            c.toggled.connect(lambda on: self.change(lambda: self.model.set_prop(el, name, "bool", on)))
            return c
        if t == "int":
            s = QSpinBox()
            s.setRange(-2_000_000_000, 2_000_000_000)
            s.setValue(int(v))
            s.editingFinished.connect(lambda: self.change(lambda: self.model.set_prop(el, name, "number", s.value()))
                                      if s.value() != int(v) else None)
            return s
        if t == "double":
            s = QDoubleSpinBox()
            s.setRange(-1e12, 1e12)
            s.setDecimals(4)
            s.setValue(float(v))
            s.editingFinished.connect(lambda: self.change(lambda: self.model.set_prop(el, name, "double", s.value()))
                                      if s.value() != float(v) else None)
            return s
        if t == "QSize":
            e = QLineEdit(f"{v.width()} x {v.height()}")
            e.setToolTip("width x height")

            def set_size():
                m = re.findall(r"\d+", e.text())
                if len(m) >= 2:
                    self.change(lambda: self.model.set_prop(el, name, "size", (int(m[0]), int(m[1]))))
            e.editingFinished.connect(set_size)
            return e
        if t == "QSizePolicy":
            from PySide6.QtWidgets import QSizePolicy
            pols = ["Fixed", "Minimum", "Maximum", "Preferred", "Expanding", "MinimumExpanding", "Ignored"]
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            combos = []
            for getter in (v.horizontalPolicy, v.verticalPolicy):
                cb = QComboBox()
                cb.addItems(pols)
                cur = getter()
                cb.setCurrentText(next((n for n in pols if getattr(QSizePolicy, n) == cur), "Preferred"))
                combos.append(cb)
                h.addWidget(cb)
            for cb in combos:
                cb.activated.connect(lambda _i: self.change(lambda: self.model.set_prop(
                    el, name, "sizepolicy", (combos[0].currentText(), combos[1].currentText(),
                                             v.horizontalStretch(), v.verticalStretch()))))
            return box
        if t == "QFont":
            b = QPushButton(f"{v.family()}, {v.pointSize()}pt" + (", bold" if v.bold() else ""))

            def pick():
                ok, f = QFontDialog.getFont(v, self)
                if isinstance(ok, bool) is False:          # PySide returns (font, ok) or (ok, font)
                    ok, f = f, ok
                if ok:
                    self.change(lambda: self.model.set_prop(el, name, "font", {
                        "family": f.family(), "pointsize": f.pointSize(), "bold": f.bold(), "italic": f.italic(),
                        "underline": f.underline()}))
            b.clicked.connect(pick)
            return b
        return None

    def _layout_props(self, el):
        self.pform.addRow(QLabel("Kind: " + LAYOUT_CLASSES.get(el.get("class"), el.get("class"))))
        for name, label in (("spacing", "Spacing"), ("leftMargin", "Left margin"), ("topMargin", "Top margin"),
                            ("rightMargin", "Right margin"), ("bottomMargin", "Bottom margin")):
            s = QSpinBox()
            s.setRange(-1, 200)
            cur = self.model.prop_text(el, name)
            l = self.layouts.get(el.get("name"))
            if cur is None and l is not None:
                m = l.contentsMargins()
                cur = {"spacing": l.spacing(), "leftMargin": m.left(), "topMargin": m.top(),
                       "rightMargin": m.right(), "bottomMargin": m.bottom()}[name]
            s.setValue(int(cur or 0))
            s.editingFinished.connect(lambda name=name, s=s: self.change(
                lambda: self.model.set_prop(el, name, "number", s.value())))
            self.pform.addRow(label + ":", s)
        tip = QLabel("Break Layout (toolbar) removes it; controls keep their places.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #8a8a8a;")
        self.pform.addRow(tip)

    def _spacer_props(self, el):
        cur = self.model.prop_el(el, "sizeHint")
        wv, hv = 40, 20
        if cur is not None and cur.find("size") is not None:
            wv, hv = int(cur.find("size").findtext("width", 40)), int(cur.find("size").findtext("height", 20))
        e = QLineEdit(f"{wv} x {hv}")

        def set_hint():
            m = re.findall(r"\d+", e.text())
            if len(m) >= 2:
                self.change(lambda: self.model.set_prop(el, "sizeHint", "size", (int(m[0]), int(m[1])), stdset=False))
        e.editingFinished.connect(set_hint)
        self.pform.addRow("Size:", e)
        st = QComboBox()
        st.addItems(["Expanding", "Fixed", "Minimum", "Maximum", "Preferred", "MinimumExpanding"])
        st.setCurrentText((self.model.prop_text(el, "sizeType") or "QSizePolicy::Expanding").split("::")[-1])
        st.activated.connect(lambda _i: self.change(lambda: self.model.set_prop(
            el, "sizeType", "enum", "QSizePolicy::" + st.currentText())))
        self.pform.addRow("Size type:", st)

    # ------------------------------------------------------------ event code
    def event_code(self):
        el = self.selected
        if el is None or el.tag != "widget" or el.get("class") not in DEFAULT_SIGNAL:
            return
        if self.is_dirty:
            self.save()
        signal, arg = DEFAULT_SIGNAL[el.get("class")]
        from . import formcode
        try:
            path, line = formcode.add_handler(self.path, self.project_root, el.get("name"), signal, arg)
        except ValueError as e:
            QMessageBox.information(self, "Event code", str(e))
            return
        self.open_code.emit(path, line)

    # ------------------------------------------------------------ try it
    def preview_window(self):
        try:
            w = self._load_preview()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Preview", str(e))
            return
        w.setAttribute(Qt.WA_DeleteOnClose)
        w.setWindowTitle((w.windowTitle() or self.model.top.get("name")) + "  [Preview]")
        w.show()
        self._preview_win = w
