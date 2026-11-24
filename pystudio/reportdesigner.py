"""Report designer (reports/<Name>.psrpt) - PyStudio's ReportViewer/RDLC.

Bands top to bottom: Report Header, Page Header, Group Header, Detail, Group Footer, Page Footer, Report
Footer. Drag fields from the Fields list (or tools from the Toolbox) onto a band; drag the line under a
band to change its height. Text can hold fields and totals in braces: {Driver}, {Sum(Score)}, {Count()},
{Avg(Score)}, {PageNumber} of {TotalPages}, {Today}. The Preview tab runs the report's query and shows the
pages exactly as the program will print them (show_report / export_report in your code)."""
import copy
import json
import os

from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QDrag, QFont, QKeySequence, QPainter, QPen
from PySide6.QtPrintSupport import QPrinter, QPrintPreviewWidget
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout, QGraphicsItem,
                               QGraphicsRectItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QSplitter,
                               QStackedWidget, QTabBar, QVBoxLayout, QWidget)

from . import dbcore, mygen, rt_report as R

GRID = 4
STRIP = 18             # band title strip height
MIME = "application/x-pystudio-report-tool"
TOOLS = [("Text", "text", "Fixed text, or fields in braces: Total: {Sum(Score)}"),
         ("Line", "line", "A horizontal line"),
         ("Box", "box", "A rectangle (border and fill)"),
         ("Image", "image", "A picture (e.g. your logo)"),
         ("Page Number", "pagenum", "Page {PageNumber} of {TotalPages}"),
         ("Date / Time", "date", "Printed {Now}"),
         ("Count", "count", "{Count()} rows - in a group footer: rows in the group"),
         ("Sum", "sum", "{Sum(Field)} - pick the field in Properties")]


def snap(v):
    return int(round(v / GRID) * GRID)


# ============================================================ canvas items
class RpItem(QGraphicsRectItem):
    def __init__(self, designer, band, data):
        super().__init__()
        self.d, self.band, self.data = designer, band, data
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self._resizing = None
        self.sync_from_data()

    def sync_from_data(self):
        d = self.data
        h = max(2, d.get("h", 20)) if d.get("type") != "line" else 6
        self.setRect(0, 0, max(4, d.get("w", 100)), h)
        top = self.d.band_tops[self.band]
        self.setPos(d.get("x", 0), top + d.get("y", 0) - (3 if d.get("type") == "line" else 0))

    def paint(self, p, option, widget=None):
        d = self.data
        r = self.rect()
        t = d.get("type", "text")
        color = QColor(d.get("color") or "#000000")
        if t == "line":
            p.setPen(QPen(color, max(1.0, float(d.get("width", 1)))))
            p.drawLine(QPointF(0, 3), QPointF(r.width(), 3))
        elif t == "box":
            if d.get("fill"):
                p.fillRect(r, QColor(d["fill"]))
            p.setPen(QPen(color, max(1.0, float(d.get("width", 1)))))
            p.drawRect(r)
        elif t == "image":
            from PySide6.QtGui import QImage
            img = QImage(os.path.join(self.d.project_dir or "", d.get("image", "")))
            if img.isNull():
                p.setPen(QPen(QColor("#999999"), 1, Qt.DashLine))
                p.drawRect(r)
                p.drawText(r, Qt.AlignCenter, "(picture)")
            else:
                p.drawImage(r, img.scaled(int(r.width()), int(r.height()), Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))
        else:
            if d.get("fill"):
                p.fillRect(r, QColor(d["fill"]))
            f = d.get("font") or {}
            font = QFont(f.get("family") or "Sans Serif")
            font.setPixelSize(max(4, round(float(f.get("size", 9)) * 96 / 72)))
            font.setBold(bool(f.get("bold")))
            font.setItalic(bool(f.get("italic")))
            p.setFont(font)
            text = d.get("text", "")
            p.setPen(QColor("#1a5fb4") if "{" in text else color)        # fields/totals show in blue
            align = {"left": Qt.AlignLeft, "center": Qt.AlignHCenter, "right": Qt.AlignRight}.get(
                d.get("align", "left"), Qt.AlignLeft)
            p.drawText(r, int(align) | int(Qt.AlignVCenter), text)
            p.setPen(QPen(QColor("#c8c8c8"), 1, Qt.DotLine))
            p.drawRect(r.adjusted(0, 0, -1, -1))
        if self.isSelected():
            p.setPen(QPen(QColor("#3399ff"), 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(r.adjusted(-1, -1, 0, 0))
            p.fillRect(QRectF(r.right() - 5, r.bottom() - 5, 6, 6), QColor("#3399ff"))

    def hoverMoveEvent(self, e):
        r = self.rect()
        near = e.pos().x() > r.right() - 7 and (e.pos().y() > r.bottom() - 7 or self.data.get("type") == "line")
        self.setCursor(Qt.SizeFDiagCursor if near and self.data.get("type") != "line" else
                       Qt.SizeHorCursor if near else Qt.SizeAllCursor)
        super().hoverMoveEvent(e)

    def mousePressEvent(self, e):
        r = self.rect()
        if e.button() == Qt.LeftButton and e.pos().x() > r.right() - 7 and \
                (e.pos().y() > r.bottom() - 7 or self.data.get("type") == "line"):
            self.d.snapshot()
            self._resizing = (e.scenePos(), QRectF(r))
            e.accept()
            return
        self.d.snapshot()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._resizing:
            start, r0 = self._resizing
            dx, dy = e.scenePos().x() - start.x(), e.scenePos().y() - start.y()
            self.data["w"] = max(GRID, snap(r0.width() + dx))
            if self.data.get("type") != "line":
                self.data["h"] = max(GRID, snap(r0.height() + dy))
            self.prepareGeometryChange()
            self.sync_from_data()
            self.d.item_changed(self)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._resizing:
            self._resizing = None
            self.d.commit()
            return
        super().mouseReleaseEvent(e)
        self.d.item_moved(self)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self.scene() is not None:
            return QPointF(snap(value.x()), snap(value.y()))
        return super().itemChange(change, value)


class BandHandle(QGraphicsRectItem):
    """The line under a band: drag it to change the band's height."""

    def __init__(self, designer, band):
        super().__init__()
        self.d, self.band = designer, band
        self.setCursor(Qt.SizeVerCursor)
        self.setPen(Qt.NoPen)
        self.setBrush(QBrush(QColor("#b8c4d4")))
        self.setZValue(5)
        self.setToolTip(f"Drag to change the {R.BAND_TITLES[band]} height")
        self._start = None

    def mousePressEvent(self, e):
        self.d.snapshot()
        self._start = (e.scenePos().y(), self.d.band_height(self.band))
        e.accept()

    def mouseMoveEvent(self, e):
        if self._start:
            y0, h0 = self._start
            self._new_h = max(0, snap(h0 + e.scenePos().y() - y0))
            r = self.rect()
            self.setRect(r.x(), self.d.band_tops[self.band] + self._new_h - 1, r.width(), 4)

    def mouseReleaseEvent(self, e):
        if self._start and getattr(self, "_new_h", None) is not None:
            self.d.set_band_height(self.band, self._new_h)
            self.d.commit()
            QTimer.singleShot(0, lambda: self.d.relayout(keep_selection=True))   # not while this item is busy
        self._start = self._new_h = None


class ReportView(QGraphicsView):
    def __init__(self, designer):
        super().__init__(designer.scene)
        self.d = designer
        self.setAcceptDrops(True)
        self.setBackgroundBrush(QColor("#6b6b6b"))
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.acceptProposedAction()

    def dropEvent(self, e):
        kind = bytes(e.mimeData().data(MIME)).decode()
        self.d.drop(kind, self.mapToScene(e.position().toPoint()))
        e.acceptProposedAction()

    def keyPressEvent(self, e):
        if self.d.key(e):
            return
        super().keyPressEvent(e)


class DragList(QListWidget):
    def __init__(self, prefix):
        super().__init__()
        self.prefix = prefix
        self.setDragEnabled(True)

    def startDrag(self, _actions):
        it = self.currentItem()
        if it is None:
            return
        md = QMimeData()
        md.setData(MIME, (self.prefix + it.data(Qt.UserRole)).encode())
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)


# ============================================================ the document
class ReportDesigner(QWidget):
    dirty_changed = Signal(bool)
    saved = Signal(str)

    def __init__(self, service, project, path, parent=None):
        super().__init__(parent)
        self.service, self.project, self.path = service, project, path
        self.project_dir = project.root if project else os.path.dirname(os.path.dirname(path))
        with open(path, encoding="utf-8") as f:
            self.report = json.load(f)
        for b in R.BAND_ORDER:
            self.report.setdefault("bands", {}).setdefault(b, {"height": 0, "items": []})
        self._dirty = False
        self._undo, self._redo = [], []
        self._pending = None
        self.columns, self.table = [], None
        self.band_tops = {}
        self.scene = QGraphicsScene(self)
        self.scene.selectionChanged.connect(self._selection_changed)

        # --- toolbar
        self.cb_source = QComboBox()
        self.cb_source.setEditable(True)
        self.cb_source.setMinimumWidth(170)
        self.cb_source.setToolTip("The saved query (queries/*.sql) that gives the report its rows")
        self.cb_source.addItems(sorted(mygen.query_files(self.project_dir)))
        self.cb_source.setCurrentText(self.report.get("dataSource", ""))
        self.cb_source.currentTextChanged.connect(self._source_changed)
        self.cb_group = QComboBox()
        self.cb_group.setToolTip("Group the rows by a field (Group Header/Footer print for each group)")
        self.cb_group.currentTextChanged.connect(lambda t: self._set_report("groupBy", "" if t == "(none)" else t))
        self.e_sort = QLineEdit(self.report.get("sortBy", ""))
        self.e_sort.setPlaceholderText("Sort, e.g. Score DESC, Driver")
        self.e_sort.editingFinished.connect(lambda: self._set_report("sortBy", self.e_sort.text().strip()))
        self.cb_size = QComboBox()
        self.cb_size.addItems(list(R.PAGE_SIZES))
        self.cb_size.setCurrentText(self.report.get("page", {}).get("size", "Letter"))
        self.cb_size.currentTextChanged.connect(lambda t: self._set_page("size", t))
        self.cb_orient = QComboBox()
        self.cb_orient.addItems(["Portrait", "Landscape"])
        self.cb_orient.setCurrentText(self.report.get("page", {}).get("orientation", "Portrait"))
        self.cb_orient.currentTextChanged.connect(lambda t: self._set_page("orientation", t))
        b_layout = QPushButton("Auto Layout")
        b_layout.setToolTip("Lay out every field as a table (title, column headings, one line per row, totals, "
                            "page numbers) - like VS's Report Wizard. Replaces the current layout.")
        b_layout.clicked.connect(self.auto_layout)
        b_pdf = QPushButton("Export PDF...")
        b_pdf.clicked.connect(self.export_pdf)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 2)
        for w in (QLabel("Data:"), self.cb_source, QLabel("Group by:"), self.cb_group, self.e_sort,
                  QLabel("Page:"), self.cb_size, self.cb_orient, b_layout, b_pdf):
            bar.addWidget(w)
        bar.addStretch()

        self.tabs = QTabBar()
        self.tabs.addTab("Design")
        self.tabs.addTab("Preview")
        self.tabs.currentChanged.connect(self._tab)
        self.view = ReportView(self)
        self.printer = QPrinter(QPrinter.HighResolution)
        self.preview = QPrintPreviewWidget(self.printer)
        self.preview.paintRequested.connect(self._paint_preview)
        self.preview_msg = QLabel("")
        self.preview_msg.setAlignment(Qt.AlignCenter)
        prev_box = QWidget()
        pv = QVBoxLayout(prev_box)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(self.preview_msg)
        pv.addWidget(self.preview, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.view)
        self.stack.addWidget(prev_box)

        # --- left: toolbox + fields
        self.toolbox = DragList("tool:")
        for text, key, tip in TOOLS:
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, key)
            it.setToolTip(tip + "  (drag onto a band, or double-click)")
            self.toolbox.addItem(it)
        self.toolbox.itemDoubleClicked.connect(lambda it: self.drop("tool:" + it.data(Qt.UserRole), None))
        self.fields = DragList("field:")
        self.fields.setToolTip("Drag a field onto a band (double-click adds it to Detail with a heading)")
        self.fields.itemDoubleClicked.connect(lambda it: self.add_field_column(it.data(Qt.UserRole)))
        self.l_fields = QLabel("Fields")
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        lv.addWidget(QLabel("<b>Toolbox</b>"))
        lv.addWidget(self.toolbox, 2)
        lv.addWidget(self.l_fields)
        lv.addWidget(self.fields, 3)

        # --- right: properties
        self.props = QWidget()
        self.pform = QFormLayout(self.props)
        self.props.setMinimumWidth(230)

        split = QSplitter()
        split.addWidget(left)
        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(0)
        mv.addWidget(self.tabs)
        mv.addWidget(self.stack, 1)
        split.addWidget(mid)
        split.addWidget(self.props)
        split.setSizes([170, 800, 250])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(split, 1)

        for seq, fn in (("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo), ("Ctrl+D", self.duplicate)):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            a.triggered.connect(fn)
            self.addAction(a)
        self.relayout()
        self._show_props()
        self.load_fields()

    # ------------------------------------------------------------ doc API (like the other documents)
    @property
    def is_dirty(self):
        return self._dirty

    def _set_dirty(self, v):
        if v != self._dirty:
            self._dirty = v
            self.dirty_changed.emit(v)

    def display_name(self):
        return os.path.basename(self.path) + " [Report]"

    def save(self, path=None):
        path = path or self.path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self.report, f, indent=1)
                f.write("\n")
        except OSError as e:
            QMessageBox.critical(self, "Save failed", f"Could not save {path}:\n{e}")
            return False
        self.path = path
        self._set_dirty(False)
        self.saved.emit(path)
        return True

    # ------------------------------------------------------------ undo
    def snapshot(self):
        self._pending = json.dumps(self.report)

    def commit(self):
        if self._pending is not None and self._pending != json.dumps(self.report):
            self._undo.append(self._pending)
            self._redo.clear()
            self._set_dirty(True)
        self._pending = None

    def change(self, fn):
        self.snapshot()
        fn()
        self.commit()
        self.relayout()

    def undo(self):
        if self._undo:
            self._redo.append(json.dumps(self.report))
            self.report = json.loads(self._undo.pop())
            self._set_dirty(True)
            self.relayout()

    def redo(self):
        if self._redo:
            self._undo.append(json.dumps(self.report))
            self.report = json.loads(self._redo.pop())
            self._set_dirty(True)
            self.relayout()

    # ------------------------------------------------------------ layout of the canvas
    def content_width(self):
        """The report's printable width (designer units: 96 per inch)."""
        return R.report_content_width(self.report)

    def band_height(self, band):
        return int(self.report["bands"][band].get("height", 0))

    def set_band_height(self, band, h, relayout=False):
        self.report["bands"][band]["height"] = h
        if relayout:
            self.relayout(keep_selection=True)

    def relayout(self, keep_selection=False):
        sel = [it.data for it in self.scene.selectedItems() if isinstance(it, RpItem)] if keep_selection else []
        self.scene.blockSignals(True)
        self.scene.clear()
        width = R.report_content_width(self.report)
        y = 0
        grouped = bool(self.report.get("groupBy"))
        for band in R.BAND_ORDER:
            if band in ("groupHeader", "groupFooter") and not grouped and not self.band_height(band):
                self.band_tops[band] = y + STRIP
                continue
            strip = self.scene.addRect(0, y, width, STRIP, QPen(Qt.NoPen), QBrush(QColor("#dfe6ee")))
            strip.setZValue(-2)
            label = self.scene.addText(f"▾ {R.BAND_TITLES[band]}" + (
                f"  (by {self.report.get('groupBy')})" if band.startswith("group") and grouped else ""))
            label.setDefaultTextColor(QColor("#3c4a5c"))
            f = label.font()
            f.setPixelSize(11)
            label.setFont(f)
            label.setPos(2, y - 1)
            label.setZValue(-1)
            y += STRIP
            self.band_tops[band] = y
            h = self.band_height(band)
            area = self.scene.addRect(0, y, width, h, QPen(QColor("#d0d0d0")), QBrush(QColor("#ffffff")))
            area.setZValue(-3)
            area.setData(0, band)
            for item in self.report["bands"][band].get("items", []):
                gi = RpItem(self, band, item)
                self.scene.addItem(gi)
                if item in sel:
                    gi.setSelected(True)
            y += h
            handle = BandHandle(self, band)
            handle.setRect(0, y - 1, width, 4)
            self.scene.addItem(handle)
            y += 3
        self.scene.setSceneRect(-20, -10, width + 60, y + 40)
        self.scene.blockSignals(False)
        self._selection_changed()

    def band_at(self, scene_y):
        best = None
        for band in R.BAND_ORDER:
            top = self.band_tops.get(band)
            if top is None:
                continue
            if scene_y >= top - STRIP:
                best = band
        return best or "detail"

    # ------------------------------------------------------------ editing
    def item_changed(self, gi):
        self._set_dirty(True)
        self._show_props()

    def item_moved(self, gi):
        """After a drag: the item may now be in another band."""
        pos = gi.pos()
        line = gi.data.get("type") == "line"
        center_y = pos.y() + (3 if line else gi.rect().height() / 2)
        band = self.band_at(center_y)
        top = self.band_tops[band]
        x = max(0, min(int(pos.x()), self.content_width() - 4))
        y = max(0, int(pos.y() + (3 if line else 0) - top))
        if band != gi.band:
            self.report["bands"][gi.band]["items"].remove(gi.data)
            self.report["bands"][band].setdefault("items", []).append(gi.data)
        gi.data["x"], gi.data["y"] = x, y
        need = y + (0 if line else gi.data.get("h", 20))
        if need > self.band_height(band):
            self.set_band_height(band, snap(need + GRID))
        self.commit()
        QTimer.singleShot(0, lambda: self.relayout(keep_selection=True))   # not inside the item's own event

    def drop(self, kind, scene_pos):
        """A tool or field dropped on the canvas (or double-clicked: scene_pos None)."""
        width = self.content_width()
        if scene_pos is None:
            band, x, y = "detail" if kind.startswith("field:") else "reportFooter", 0, 0
        else:
            band = self.band_at(scene_pos.y())
            x, y = max(0, snap(scene_pos.x())), max(0, snap(scene_pos.y() - self.band_tops[band]))
        key = kind.split(":", 1)[1]
        cols = self.columns
        num = next((c for c in cols if self._numeric(c)), cols[0] if cols else "Field")
        items = {"text": {"type": "text", "w": 160, "h": 20, "text": "Text", "font": {"size": 9}},
                 "line": {"type": "line", "w": width - x, "h": 0, "color": "#666666"},
                 "box": {"type": "box", "w": 160, "h": 60, "color": "#666666"},
                 "image": {"type": "image", "w": 120, "h": 60, "image": ""},
                 "pagenum": {"type": "text", "w": 200, "h": 18, "text": "Page {PageNumber} of {TotalPages}",
                             "font": {"size": 8}},
                 "date": {"type": "text", "w": 200, "h": 18, "text": "Printed {Now}", "format": "%m/%d/%Y %I:%M %p",
                          "font": {"size": 8}},
                 "count": {"type": "text", "w": 160, "h": 20, "text": "{Count()} rows", "font": {"size": 9,
                                                                                               "bold": True}},
                 "sum": {"type": "text", "w": 160, "h": 20, "text": "Total: {Sum(" + num + ")}",
                         "format": ",.2f", "font": {"size": 9, "bold": True}}}
        if kind.startswith("field:"):
            item = {"type": "text", "w": 140, "h": 20, "text": "{" + key + "}", "font": {"size": 9}}
        else:
            item = items.get(key)
            if item is None:
                return
            item = copy.deepcopy(item)
        item["x"], item["y"] = x, y

        def add():
            self.report["bands"][band].setdefault("items", []).append(item)
            need = y + max(item.get("h", 0), 4) + GRID
            if need > self.band_height(band):
                self.set_band_height(band, snap(need))
        self.change(add)
        self._select_data(item)
        if key == "image" and not kind.startswith("field:"):
            self._browse_image(item)

    def add_field_column(self, col):
        """Double-click a field: heading in the Page Header + the field in Detail, in the next free column."""
        def add():
            bands = self.report["bands"]
            used = [it.get("x", 0) + it.get("w", 0) for it in bands["detail"].get("items", [])]
            x = snap(max(used) + 8) if used else 0
            w = 120
            if x + w > self.content_width():
                x = 0
            bands["detail"].setdefault("items", []).append(
                {"type": "text", "x": x, "y": 2, "w": w, "h": 18, "text": "{" + col + "}", "font": {"size": 9},
                 "align": "right" if self._numeric(col) else "left"})
            bands["pageHeader"].setdefault("items", []).append(
                {"type": "text", "x": x, "y": 4, "w": w, "h": 18, "text": col, "font": {"size": 9, "bold": True},
                 "color": "#1f4e79", "align": "right" if self._numeric(col) else "left"})
            for b, h in (("detail", 22), ("pageHeader", 28)):
                if self.band_height(b) < h:
                    self.set_band_height(b, h)
        self.change(add)

    def auto_layout(self, force=False):
        if not self.columns:
            QMessageBox.information(self, "Auto Layout", "Choose the report's data (a saved query) first, so "
                                    "PyStudio knows its fields.")
            return
        if not force and any(self.report["bands"][b].get("items") for b in R.BAND_ORDER):
            if QMessageBox.question(self, "Auto Layout", "Replace the current layout with a table of all "
                                    "fields?") != QMessageBox.Yes:
                return

        def run():
            fresh = R.new_report(self.report.get("name") or os.path.splitext(os.path.basename(self.path))[0],
                                 self.report.get("dataSource", ""), self.columns)
            fresh["page"] = self.report.get("page", fresh["page"])
            fresh["groupBy"], fresh["sortBy"] = self.report.get("groupBy", ""), self.report.get("sortBy", "")
            for it in fresh["bands"]["detail"]["items"] + fresh["bands"]["pageHeader"]["items"]:
                col = it.get("text", "").strip("{}")
                if self._numeric(col):
                    it["align"] = "right"
            self.report.update(fresh)
        self.change(run)

    def _numeric(self, col):
        if not self.table or col not in self.columns:
            return False
        i = self.columns.index(col)
        vals = [r[i] for r in list(self.table)[:20] if r[i] is not None]
        return bool(vals) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)

    def selected(self):
        return [it for it in self.scene.selectedItems() if isinstance(it, RpItem)]

    def _select_data(self, data):
        for gi in self.scene.items():
            if isinstance(gi, RpItem) and gi.data is data:
                gi.setSelected(True)

    def key(self, e):
        sel = self.selected()
        if not sel:
            return False
        if e.key() == Qt.Key_Delete:
            def run():
                for gi in sel:
                    self.report["bands"][gi.band]["items"].remove(gi.data)
            self.change(run)
            return True
        moves = {Qt.Key_Left: (-GRID, 0), Qt.Key_Right: (GRID, 0), Qt.Key_Up: (0, -GRID), Qt.Key_Down: (0, GRID)}
        if e.key() in moves:
            dx, dy = moves[e.key()]
            fine = e.modifiers() & Qt.ControlModifier
            dx, dy = (dx // GRID, dy // GRID) if fine else (dx, dy)
            datas = [gi.data for gi in sel]

            def run():
                for d in datas:
                    d["x"] = max(0, d.get("x", 0) + dx)
                    d["y"] = max(0, d.get("y", 0) + dy)
            self.change(run)
            for d in datas:
                self._select_data(d)
            return True
        return False

    def duplicate(self):
        sel = self.selected()
        if not sel:
            return
        copies = []

        def run():
            for gi in sel:
                c = copy.deepcopy(gi.data)
                c["y"] = c.get("y", 0) + (c.get("h", 20) or 8) + GRID
                self.report["bands"][gi.band]["items"].append(c)
                copies.append(c)
                need = c["y"] + c.get("h", 0) + GRID
                if need > self.band_height(gi.band):
                    self.set_band_height(gi.band, snap(need))
        self.change(run)
        for c in copies:
            self._select_data(c)

    def _set_report(self, key, value):
        if self.report.get(key, "") != value:
            self.change(lambda: self.report.__setitem__(key, value))

    def _set_page(self, key, value):
        def run():
            self.report.setdefault("page", {})[key] = value
        if self.report.get("page", {}).get(key) != value:
            self.change(run)

    # ------------------------------------------------------------ properties panel
    def _clear_props(self):
        while self.pform.rowCount():
            self.pform.removeRow(0)

    def _selection_changed(self):
        self._show_props()

    def _show_props(self):
        self._clear_props()
        sel = self.selected()
        if len(sel) != 1:
            self._report_props(len(sel))
            return
        gi = sel[0]
        d = gi.data
        t = d.get("type", "text")
        self.pform.addRow(QLabel(f"<b>{t.capitalize()}</b> in {R.BAND_TITLES[gi.band]}"))

        def setter(key, cast=lambda v: v, sub=None):
            def apply(v):
                def run():
                    target = d.setdefault(sub, {}) if sub else d
                    target[key] = cast(v)
                self.change(run)
                self._select_data(d)
            return apply

        if t == "text":
            e = QLineEdit(d.get("text", ""))
            e.setToolTip("Text; fields and totals in braces: {Driver}  {Sum(Score)}  {Count()}  {Avg(Score)}  "
                          "{Min(Score)}  {Max(Score)}  {PageNumber}  {TotalPages}  {Today}  {Now}  {Param(name)}")
            e.editingFinished.connect(lambda: setter("text")(e.text()) if e.text() != d.get("text") else None)
            self.pform.addRow("Text:", e)
            fmt = QComboBox()
            fmt.setEditable(True)
            fmt.addItems(["", ",.2f", ",.0f", ".1f", ".1%", "$,.2f", "%m/%d/%Y", "%Y-%m-%d", "%m/%d/%Y %I:%M %p"])
            fmt.setCurrentText(d.get("format", ""))
            fmt.setToolTip("How values show: ,.2f = 1,234.50   .1% = 12.5%   %m/%d/%Y = 11/22/2026")
            fmt.lineEdit().editingFinished.connect(lambda: setter("format")(fmt.currentText().strip())
                                                   if fmt.currentText().strip() != d.get("format", "") else None)
            fmt.activated.connect(lambda _i: setter("format")(fmt.currentText().strip()))
            self.pform.addRow("Format:", fmt)
            f = d.get("font") or {}
            sz = QSpinBox()
            sz.setRange(5, 72)
            sz.setValue(int(f.get("size", 9)))
            sz.editingFinished.connect(lambda: setter("size", int, "font")(sz.value()))
            self.pform.addRow("Font size:", sz)
            row = QHBoxLayout()
            for key, label in (("bold", "Bold"), ("italic", "Italic"), ("underline", "Underline")):
                ck = QCheckBox(label)
                ck.setChecked(bool(f.get(key)))
                ck.toggled.connect(lambda v, key=key: setter(key, bool, "font")(v))
                row.addWidget(ck)
            self.pform.addRow("", row)
            al = QComboBox()
            al.addItems(["left", "center", "right"])
            al.setCurrentText(d.get("align", "left"))
            al.currentTextChanged.connect(setter("align"))
            self.pform.addRow("Align:", al)
            wr = QCheckBox("Wrap long text")
            wr.setChecked(bool(d.get("wrap")))
            wr.toggled.connect(setter("wrap", bool))
            self.pform.addRow("", wr)
            bd = QCheckBox("Border")
            bd.setChecked(bool(d.get("border")))
            bd.toggled.connect(setter("border", bool))
            self.pform.addRow("", bd)
        if t == "image":
            b = QPushButton(d.get("image") or "Choose picture...")
            b.clicked.connect(lambda: self._browse_image(d))
            self.pform.addRow("Picture:", b)
        if t in ("text", "line", "box"):
            self.pform.addRow("Color:", self._color_button(d, "color", setter, "#000000"))
        if t in ("text", "box"):
            self.pform.addRow("Fill:", self._color_button(d, "fill", setter, ""))
        if t in ("line", "box"):
            lw = QSpinBox()
            lw.setRange(1, 10)
            lw.setValue(int(d.get("width", 1)))
            lw.editingFinished.connect(lambda: setter("width", int)(lw.value()))
            self.pform.addRow("Line width:", lw)
        for key, label in (("x", "Left"), ("y", "Top"), ("w", "Width"), ("h", "Height")):
            if key == "h" and t == "line":
                continue
            sp = QSpinBox()
            sp.setRange(0, 3000)
            sp.setValue(int(d.get(key, 0)))
            sp.editingFinished.connect(lambda key=key, sp=sp: setter(key, int)(sp.value())
                                       if sp.value() != d.get(key) else None)
            self.pform.addRow(label + ":", sp)
        hint = QLabel("Delete removes it, arrows nudge it (Ctrl+arrow: 1 pixel), Ctrl+D duplicates.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8a8a8a;")
        self.pform.addRow(hint)

    def _color_button(self, d, key, setter, default):
        b = QPushButton(d.get(key) or "(none)")
        if d.get(key):
            b.setStyleSheet(f"background: {d[key]}; color: {'#000' if QColor(d[key]).lightness() > 128 else '#fff'};")

        def pick():
            c = QColorDialog.getColor(QColor(d.get(key) or default or "#ffffff"), self, "Color")
            if c.isValid():
                setter(key)(c.name())
        b.clicked.connect(pick)
        if key == "fill":
            wrap = QWidget()
            h = QHBoxLayout(wrap)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(b, 1)
            clear = QPushButton("x")
            clear.setFixedWidth(26)
            clear.setToolTip("No fill")
            clear.clicked.connect(lambda: setter(key)(""))
            h.addWidget(clear)
            return wrap
        return b

    def _report_props(self, n_selected):
        self.pform.addRow(QLabel("<b>Report</b>" if not n_selected else f"<b>{n_selected} items selected</b>"))
        if n_selected:
            return
        name = QLineEdit(self.report.get("name", ""))
        name.editingFinished.connect(lambda: self._set_report("name", name.text().strip()))
        self.pform.addRow("Title:", name)
        alt = QPushButton(self.report.get("alternateRows") or "(none)")
        alt.setToolTip("Shade every other Detail row")

        def pick_alt():
            c = QColorDialog.getColor(QColor(self.report.get("alternateRows") or "#f3f6fa"), self, "Alternate rows")
            if c.isValid():
                self._set_report("alternateRows", c.name())
        alt.clicked.connect(pick_alt)
        self.pform.addRow("Alternate rows:", alt)
        no_alt = QPushButton("No shading")
        no_alt.clicked.connect(lambda: self._set_report("alternateRows", ""))
        self.pform.addRow("", no_alt)
        m = R.report_margins(self.report)
        for i, label in enumerate(("Left margin", "Top margin", "Right margin", "Bottom margin")):
            sp = QSpinBox()
            sp.setRange(0, 300)
            sp.setValue(m[i])
            sp.setSuffix(" px")
            sp.setToolTip("96 px = 1 inch")

            def set_margin(i=i, sp=sp):
                mm = R.report_margins(self.report)
                if mm[i] != sp.value():
                    mm[i] = sp.value()
                    self._set_page("margins", mm)
            sp.editingFinished.connect(set_margin)
            self.pform.addRow(label + ":", sp)
        for band in R.BAND_ORDER:
            sp = QSpinBox()
            sp.setRange(0, 1000)
            sp.setValue(self.band_height(band))

            def set_h(band=band, sp=sp):
                if sp.value() != self.band_height(band):
                    self.change(lambda: self.set_band_height(band, sp.value()))
            sp.editingFinished.connect(set_h)
            self.pform.addRow(R.BAND_TITLES[band] + ":", sp)
        tip = QLabel("In code:  show_report(\"" + (os.path.splitext(os.path.basename(self.path))[0]) +
                     "\")  opens it in a preview window;  export_report(name, \"file.pdf\")  saves a PDF.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #8a8a8a;")
        self.pform.addRow(tip)

    def _browse_image(self, d):
        path, _f = QFileDialog.getOpenFileName(self, "Picture", os.path.join(self.project_dir or "", "images"),
                                               "Pictures (*.png *.jpg *.jpeg *.gif *.bmp *.svg)")
        if not path:
            return
        root = self.project_dir or os.path.dirname(path)
        if not path.startswith(root + os.sep):
            import shutil
            os.makedirs(os.path.join(root, "images"), exist_ok=True)
            dest = os.path.join(root, "images", os.path.basename(path))
            if not os.path.exists(dest):
                shutil.copy2(path, dest)
            path = dest
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        self.change(lambda: d.__setitem__("image", rel))

    # ------------------------------------------------------------ data
    def _source_changed(self, text):
        text = text.strip()
        if text != self.report.get("dataSource", ""):
            self._set_report("dataSource", text)
            self.load_fields()

    def _query(self):
        """(cfg, sql, params) for the report's data source."""
        src = (self.report.get("dataSource") or "").strip()
        if not src:
            return None, "", {}
        files = mygen.query_files(self.project_dir)
        conns = (self.project.data.get("connections", []) if self.project else [])
        if src in files:
            with open(files[src], encoding="utf-8", errors="replace") as f:
                q = dbcore.parse_query_file(f.read())
            sql, conn, params = q["sql"], q["connection"], dict(q["params"])
        else:
            sql, conn, params = src, self.report.get("connection", ""), {n: None for n in dbcore.find_params(src)}
        cfg = next((c for c in conns if c.get("name") == conn), conns[0] if conns else None)
        return cfg, sql, params

    def load_fields(self, then=None):
        cfg, sql, params = self._query()
        self.fields.clear()
        if cfg is None or not sql:
            self.l_fields.setText("Fields  (choose the report's Data)" if not sql else
                                  "Fields  (add a connection in Server Explorer)")
            return
        pw = self.service.password_for(cfg, self)
        if pw is None:
            return
        self.l_fields.setText("Fields  (loading...)")
        stmt = dbcore.split_statements(sql)[-1] if dbcore.split_statements(sql) else sql

        def done(results):
            table = results[-1][1] if results else None
            self.table = table
            self.columns = [str(c) for c in (table.columns if table is not None else [])]
            self.fields.clear()
            for c in self.columns:
                it = QListWidgetItem(c)
                it.setData(Qt.UserRole, c)
                self.fields.addItem(it)
            self.l_fields.setText(f"Fields ({len(self.columns)})")
            cur = self.report.get("groupBy", "")
            self.cb_group.blockSignals(True)
            self.cb_group.clear()
            self.cb_group.addItems(["(none)"] + self.columns)
            self.cb_group.setCurrentText(cur or "(none)")
            self.cb_group.blockSignals(False)
            if then:
                then()

        def failed(msg):
            self.l_fields.setText("Fields  (the query failed - see its tooltip)")
            self.l_fields.setToolTip(msg)
        self.service.run_script(cfg, pw, [stmt], {n: dbcore.coerce_param(v) for n, v in params.items()},
                                200, done, failed)

    # ------------------------------------------------------------ preview / PDF
    def _tab(self, i):
        self.stack.setCurrentIndex(i)
        if i == 1:
            self.refresh_preview()

    def refresh_preview(self):
        cfg, sql, params = self._query()
        if cfg is None or not sql:
            self._preview_table = None
            self.preview_msg.setText("Showing the layout only - choose the report's Data to see real rows.")
            self.preview.updatePreview()
            return
        if any(v is None for v in params.values()):
            from .querydesigner import ParamsDialog
            dlg = ParamsDialog(params, getattr(self, "_last_params", {}), self)
            if not dlg.exec():
                return
            self._last_params = dlg.values()
            params = dlg.values()
        pw = self.service.password_for(cfg, self)
        if pw is None:
            return
        self.preview_msg.setText("Running the query...")
        self._preview_params = params

        def done(results):
            self._preview_table = results[-1][1] if results else None
            n = len(self._preview_table) if self._preview_table is not None else 0
            self.preview_msg.setText(f"{n} row(s) from {self.report.get('dataSource')}")
            self.preview.updatePreview()

        def failed(msg):
            self.preview_msg.setText(f"The query failed: {msg.splitlines()[0]}")
        stmts = dbcore.split_statements(sql)
        self.service.run_script(cfg, pw, stmts[-1:] or [sql], {n: dbcore.coerce_param(v) for n, v in params.items()},
                                None, done, failed)

    def _renderer(self):
        t = getattr(self, "_preview_table", None)
        cols = t.columns if t is not None else self.columns
        rows = list(t) if t is not None else []
        return R.ReportRenderer(self.report, cols, rows, getattr(self, "_preview_params", {}), self.project_dir)

    def _paint_preview(self, printer):
        try:
            self._renderer().render(printer)
        except Exception as e:  # noqa: BLE001 - show it in the preview, don't crash
            self.preview_msg.setText(f"Couldn't draw the report: {e}")

    def export_pdf(self):
        default = os.path.join(self.project_dir or os.path.expanduser("~"),
                               (self.report.get("name") or "report").replace(" ", "_") + ".pdf")
        path, _f = QFileDialog.getSaveFileName(self, "Export PDF", default, "PDF files (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        pr = QPrinter(QPrinter.HighResolution)
        pr.setOutputFormat(QPrinter.PdfFormat)
        pr.setOutputFileName(path)
        n = self._renderer().render(pr)
        QMessageBox.information(self, "Export PDF", f"Saved {n} page(s) to {path}"
                                + ("" if getattr(self, "_preview_table", None) is not None else
                                   "\n\n(Open the Preview tab first to include the query's rows.)"))


def create_report(project_dir, name, data_source=""):
    """reports/<name>.psrpt with an empty layout (Auto Layout fills it once the fields are known)."""
    folder = os.path.join(project_dir, "reports")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".psrpt")
    rep = R.new_report(name, data_source, [])
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, indent=1)
        f.write("\n")
    return path
