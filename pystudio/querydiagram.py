"""The Query Builder's Diagram pane (VS's Query Designer diagram): a box per table with checkable columns, lines for
joins. Drag a column onto a column of another table to join them; right-click a line for the join type (Inner,
Left - all rows from the first table, Right, Full) or to remove it. Drag a box by its title; x removes the table."""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsPathItem, QGraphicsScene, QGraphicsView, QMenu

from . import theme

W, HEAD, ROW = 190, 22, 18
JOIN_TYPES = ["INNER", "LEFT", "RIGHT", "FULL"]
JOIN_LABELS = {"INNER": "Inner join - matching rows only", "LEFT": "Left join - all rows from {a}",
               "RIGHT": "Right join - all rows from {b}", "FULL": "Full join - all rows from both"}


class TableBox(QGraphicsObject):
    def __init__(self, diagram, key):
        super().__init__()
        self.d = diagram
        self.key = tuple(key)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self._drag_from = None
        self.setZValue(1)

    def columns(self):
        cols = self.d.builder.columns.get(self.key)
        return [("*", "", False)] + list(cols[0]) if cols else [("*", "", False), ("(loading...)", "", False)]

    def boundingRect(self):
        return QRectF(0, 0, W, HEAD + ROW * len(self.columns()) + 4)

    def row_at(self, pos):
        if pos.y() < HEAD:
            return None
        i = int((pos.y() - HEAD) // ROW)
        cols = self.columns()
        return cols[i][0] if 0 <= i < len(cols) and cols[i][0] != "(loading...)" else None

    def row_y(self, name):
        for i, c in enumerate(self.columns()):
            if c[0] == name:
                return HEAD + ROW * i + ROW / 2
        return HEAD / 2

    def anchor(self, name, toward_x):
        """Scene point on the left or right edge of a column's row, on the side facing toward_x."""
        y = self.row_y(name)
        right = toward_x > self.sceneBoundingRect().center().x()
        return self.mapToScene(QPointF(W if right else 0, y))

    def paint(self, p, _opt, _w=None):
        bg = QColor(theme.c("#2b2b2b"))
        fg = QColor(theme.c("#dddddd"))
        r = self.boundingRect()
        p.setPen(QPen(QColor("#3c8fd8") if self.isSelected() else QColor("#7a7a7a"), 1.2))
        p.setBrush(bg)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        p.fillRect(QRectF(1, 1, W - 2, HEAD - 1), QColor("#3c6e9f"))
        p.setPen(QColor("white"))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(12)
        p.setFont(f)
        p.drawText(QRectF(6, 0, W - 26, HEAD), Qt.AlignVCenter | Qt.AlignLeft, self.key[1])
        p.drawText(QRectF(W - 20, 0, 16, HEAD), Qt.AlignCenter, "×")
        f.setBold(False)
        p.setFont(f)
        for i, (name, typ, pk) in enumerate(self.columns()):
            y = HEAD + ROW * i
            if name == "(loading...)":
                p.setPen(QColor("#8a8a8a"))
                p.drawText(QRectF(24, y, W - 28, ROW), Qt.AlignVCenter, name)
                continue
            checked = self.d.builder.has_column(self.key, name)
            box = QRectF(6, y + 3, 12, 12)
            p.setPen(QPen(QColor("#8a8a8a"), 1))
            p.setBrush(QColor("#3c8fd8") if checked else Qt.NoBrush)
            p.drawRect(box)
            if checked:
                p.setPen(QPen(QColor("white"), 1.6))
                p.drawPolyline([QPointF(8.5, y + 9), QPointF(11, y + 12), QPointF(16, y + 6)])
            p.setPen(fg)
            label = ("★ " if pk else "") + ("* (All Columns)" if name == "*" else name)
            p.drawText(QRectF(24, y, W - 90, ROW), Qt.AlignVCenter, label)
            if typ:
                p.setPen(QColor("#8a8a8a"))
                p.drawText(QRectF(W - 72, y, 66, ROW), Qt.AlignVCenter | Qt.AlignRight, typ.split("(")[0][:10])

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.d.update_lines()
        return super().itemChange(change, value)

    def mousePressEvent(self, e):
        pos = e.pos()
        if pos.y() < HEAD:
            if pos.x() > W - 22:
                self.d.builder.remove_table(self.key)
                return
            return super().mousePressEvent(e)       # drag the box
        name = self.row_at(pos)
        if name is None:
            return
        if pos.x() < 22:
            self.d.builder.toggle_column(self.key, name)
            self.update()
            e.accept()
            return
        if name != "*":                              # start drawing a join
            self._drag_from = name
            self.d.start_rubber(self.anchor(name, e.scenePos().x()))
        e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_from:
            self.d.move_rubber(e.scenePos())
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag_from:
            src = self._drag_from
            self._drag_from = None
            self.d.end_rubber()
            for it in self.scene().items(e.scenePos()):
                if isinstance(it, TableBox) and it is not self:
                    name = it.row_at(it.mapFromScene(e.scenePos()))
                    if name and name != "*":
                        self.d.builder.add_join((self.key, src), (it.key, name))
                    break
            return
        super().mouseReleaseEvent(e)

    def hoverMoveEvent(self, e):
        pos = e.pos()
        if pos.y() < HEAD:
            self.setCursor(Qt.PointingHandCursor if pos.x() > W - 22 else Qt.SizeAllCursor)
            self.setToolTip("Drag to move - x removes the table from the query")
        else:
            name = self.row_at(pos)
            self.setCursor(Qt.ArrowCursor)
            self.setToolTip("" if not name else
                            ("Check to show this column" if pos.x() < 22 else
                             f"Drag {name} onto a column of another table to join them"))


class JoinLine(QGraphicsPathItem):
    def __init__(self, diagram, join, auto=False):
        super().__init__()
        self.d = diagram
        self.join = join
        self.auto = auto
        self.setZValue(0)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, not auto)
        color = QColor("#8a8a8a") if auto else QColor("#3c8fd8")
        pen = QPen(color, 2)
        if auto:
            pen.setStyle(Qt.DashLine)
        self.setPen(pen)
        (ka, ca), (kb, cb) = join["a"], join["b"]
        jt = join.get("type", "INNER")
        self.setToolTip((f"From a foreign key: {ka[1]}.{ca} = {kb[1]}.{cb} (inner join). Drag columns to draw "
                         "your own join instead." if auto else
                         f"{ka[1]}.{ca} = {kb[1]}.{cb}\n" + JOIN_LABELS[jt].format(a=ka[1], b=kb[1]) +
                         "\nRight-click to change or remove"))

    def shape(self):
        s = QPainterPathStroker()
        s.setWidth(10)
        return s.createStroke(self.path())

    def paint(self, p, opt, w=None):
        if self.isSelected():
            pen = QPen(self.pen())
            pen.setColor(QColor("#ffb000"))
            p.setPen(pen)
            p.setRenderHint(QPainter.Antialiasing)
            p.drawPath(self.path())
        else:
            super().paint(p, opt, w)
        if not self.auto:                       # the join-type diamond (VS): the filled half keeps all its rows
            mid = self.path().pointAtPercent(0.5)
            a_left = self.path().pointAtPercent(0.0).x() <= self.path().pointAtPercent(1.0).x()
            r = 7
            top, right = QPointF(mid.x(), mid.y() - r), QPointF(mid.x() + r, mid.y())
            bottom, left = QPointF(mid.x(), mid.y() + r), QPointF(mid.x() - r, mid.y())
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor("#ffffff")))
            p.drawPolygon([top, right, bottom, left])
            jt = self.join.get("type", "INNER")
            fill_left = jt == "FULL" or (jt == "LEFT" and a_left) or (jt == "RIGHT" and not a_left)
            fill_right = jt == "FULL" or (jt == "LEFT" and not a_left) or (jt == "RIGHT" and a_left)
            p.setBrush(QBrush(QColor("#3c8fd8")))
            if fill_left:
                p.drawPolygon([top, bottom, left])
            if fill_right:
                p.drawPolygon([top, right, bottom])
            p.setPen(QPen(QColor("#3c8fd8"), 1.4))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon([top, right, bottom, left])

    def contextMenuEvent(self, e):
        if self.auto:
            return
        self.d.join_menu(self.join, e.screenPos())

    def mouseDoubleClickEvent(self, e):
        if not self.auto:
            self.d.join_menu(self.join, e.screenPos())


class QueryDiagram(QGraphicsView):
    table_dropped = Signal(object)

    def __init__(self, builder, parent=None):
        super().__init__(parent)
        self.builder = builder
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setAcceptDrops(True)
        self.boxes = {}
        self.lines = []
        self.rubber = None
        self.setMinimumHeight(120)
        self._hint = None

    def box_for(self, key):
        return self.boxes.get(tuple(key))

    def sync(self):
        """Boxes for the builder's tables, lines for its joins."""
        keys = [tuple(k) for k in self.builder.tables]
        for k in list(self.boxes):
            if k not in keys:
                self.scene().removeItem(self.boxes.pop(k))
        for i, k in enumerate(keys):
            if k not in self.boxes:
                b = TableBox(self, k)
                xs = [bx.pos().x() + W for bx in self.boxes.values()]
                b.setPos(QPointF((max(xs) + 50) if xs else 20, 20 + (i % 2) * 18))
                self.scene().addItem(b)
                self.boxes[k] = b
            self.boxes[k].prepareGeometryChange()
            self.boxes[k].update()
        if self._hint is None:
            self._hint = self.scene().addSimpleText("Double-click a table on the left (or drag it here) to add it. "
                                                    "Drag a column onto another table's column to join them.")
            self._hint.setBrush(QColor("#8a8a8a"))
            self._hint.setPos(10, 10)
        self._hint.setVisible(not keys)
        self.update_lines()
        r = self.scene().itemsBoundingRect().adjusted(-20, -20, 40, 40)
        self.setSceneRect(r.united(QRectF(0, 0, 600, 160)))

    def update_lines(self):
        for ln in self.lines:
            self.scene().removeItem(ln)
        self.lines = []
        joins = [(j, False) for j in self.builder.joins] + [(j, True) for j in self.builder.auto_joins()]
        for j, auto in joins:
            ba, bb = self.box_for(j["a"][0]), self.box_for(j["b"][0])
            if ba is None or bb is None:
                continue
            pa = ba.anchor(j["a"][1], bb.sceneBoundingRect().center().x())
            pb = bb.anchor(j["b"][1], ba.sceneBoundingRect().center().x())
            path = QPainterPath(pa)
            dx = 30 if pb.x() > pa.x() else -30
            path.cubicTo(QPointF(pa.x() + dx, pa.y()), QPointF(pb.x() - dx, pb.y()), pb)
            ln = JoinLine(self, j, auto)
            ln.setPath(path)
            self.scene().addItem(ln)
            self.lines.append(ln)

    # rubber line while drawing a join
    def start_rubber(self, p):
        self.rubber = QGraphicsPathItem()
        self.rubber.setPen(QPen(QColor("#3c8fd8"), 2, Qt.DashLine))
        self.rubber.setZValue(5)
        self._r0 = p
        self.scene().addItem(self.rubber)

    def move_rubber(self, p):
        if self.rubber is not None:
            path = QPainterPath(self._r0)
            path.lineTo(p)
            self.rubber.setPath(path)

    def end_rubber(self):
        if self.rubber is not None:
            self.scene().removeItem(self.rubber)
            self.rubber = None

    def join_menu(self, join, screen_pos):
        ka, kb = join["a"][0], join["b"][0]
        m = QMenu(self)
        for jt in JOIN_TYPES:
            a = m.addAction(JOIN_LABELS[jt].format(a=ka[1], b=kb[1]))
            a.setCheckable(True)
            a.setChecked(join.get("type", "INNER") == jt)
            a.triggered.connect(lambda _c=False, jt=jt: self.builder.set_join_type(join, jt))
        m.addSeparator()
        m.addAction("Remove Join", lambda: self.builder.remove_join(join))
        m.exec(screen_pos)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Delete:
            for it in self.scene().selectedItems():
                if isinstance(it, JoinLine) and not it.auto:
                    self.builder.remove_join(it.join)
                    return
                if isinstance(it, TableBox):
                    self.builder.remove_table(it.key)
                    return
        super().keyPressEvent(e)

    # tables dragged from the tree
    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat("application/x-pystudio-table"):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat("application/x-pystudio-table"):
            e.acceptProposedAction()

    def dropEvent(self, e):
        data = bytes(e.mimeData().data("application/x-pystudio-table")).decode()
        schema, _sep, name = data.partition("\x1f")
        self.table_dropped.emit((schema, name))
        b = self.box_for((schema, name))
        if b is not None:
            b.setPos(self.mapToScene(e.position().toPoint()))
        e.acceptProposedAction()
