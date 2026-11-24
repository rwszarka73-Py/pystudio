"""PyStudio's own toolbar/menu icons, drawn in code so they look the same on every Linux desktop.
(Qt's standard icons come from the desktop's icon theme - on Ubuntu's Yaru, "save" is a green
download arrow.) Visual Studio 2022 style: flat shapes, a few clear colors."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from . import theme

FOLDER = "#dcb67a"
FOLDER_DARK = "#b8914f"
PAGE = "#f0f0f0"
PAGE_EDGE = "#7a7a7a"
BLUE = "#3c8fd8"
GREEN = "#43a047"
STAR = "#e8b400"


def _icon(draw):
    icon = QIcon()
    for size in (16, 24, 32):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(size / 32, size / 32)
        draw(p)
        p.end()
        icon.addPixmap(pm, QIcon.Normal)
        # disabled: faded
        dis = QPixmap(pm.size())
        dis.fill(Qt.transparent)
        q = QPainter(dis)
        q.setOpacity(0.35)
        q.drawPixmap(0, 0, pm)
        q.end()
        icon.addPixmap(dis, QIcon.Disabled)
    return icon


def _pen(color, w=1.6):
    return QPen(QColor(color), w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)


def _page(p, x=6, y=3, w=18, h=25, fold=6):
    path = QPainterPath()
    path.moveTo(x, y)
    path.lineTo(x + w - fold, y)
    path.lineTo(x + w, y + fold)
    path.lineTo(x + w, y + h)
    path.lineTo(x, y + h)
    path.closeSubpath()
    p.setPen(_pen(PAGE_EDGE, 1.4))
    p.setBrush(QColor(PAGE))
    p.drawPath(path)
    p.drawPolyline([QPointF(x + w - fold, y), QPointF(x + w - fold, y + fold), QPointF(x + w, y + fold)])


def _plus(p, cx, cy, color=GREEN, r=5.5):
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QPointF(cx, cy), r + 1.5, r + 1.5)
    p.setPen(_pen("#ffffff", 2.2))
    p.drawLine(QPointF(cx - r + 2, cy), QPointF(cx + r - 2, cy))
    p.drawLine(QPointF(cx, cy - r + 2), QPointF(cx, cy + r - 2))


def _folder(p, open_=False):
    p.setPen(_pen(FOLDER_DARK, 1.3))
    p.setBrush(QColor(FOLDER_DARK))
    back = QPainterPath()
    back.moveTo(3, 7)
    back.lineTo(12, 7)
    back.lineTo(14.5, 10)
    back.lineTo(27, 10)
    back.lineTo(27, 26)
    back.lineTo(3, 26)
    back.closeSubpath()
    p.drawPath(back)
    p.setBrush(QColor(FOLDER))
    front = QPainterPath()
    if open_:
        front.moveTo(7, 14)
        front.lineTo(30, 14)
        front.lineTo(26, 26)
        front.lineTo(3, 26)
    else:
        front.moveTo(3, 13)
        front.lineTo(27, 13)
        front.lineTo(27, 26)
        front.lineTo(3, 26)
    front.closeSubpath()
    p.drawPath(front)


def new_project():
    def draw(p):
        _folder(p)
        _plus(p, 23, 23, STAR)
    return _icon(draw)


def new_file():
    def draw(p):
        _page(p)
        _plus(p, 23, 23)
    return _icon(draw)


def open_folder():
    return _icon(lambda p: _folder(p, open_=True))


def _floppy(p, x=4, y=4, s=24):
    k = s / 24
    body = QPainterPath()
    body.moveTo(x, y)
    body.lineTo(x + 19 * k, y)
    body.lineTo(x + s, y + 5 * k)
    body.lineTo(x + s, y + s)
    body.lineTo(x, y + s)
    body.closeSubpath()
    p.setPen(_pen("#1f5f9e", 1.3))
    p.setBrush(QColor(BLUE))
    p.drawPath(body)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#e8eef5"))                         # label
    p.drawRect(QRectF(x + 4 * k, y + 12 * k, 16 * k, 10 * k))
    p.setBrush(QColor("#d0d7e0"))                         # shutter
    p.drawRect(QRectF(x + 6 * k, y + 1.5 * k, 11 * k, 6.5 * k))
    p.setBrush(QColor("#1f5f9e"))
    p.drawRect(QRectF(x + 13 * k, y + 2.5 * k, 2.6 * k, 4.5 * k))


def save():
    return _icon(_floppy)


def save_all():
    def draw(p):
        _floppy(p, 8, 2, 20)
        _floppy(p, 3, 9, 20)
    return _icon(draw)


def refresh():
    def draw(p):
        p.setPen(_pen(theme.c("#c5c5c5"), 3))
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(6, 6, 20, 20), 60 * 16, 290 * 16)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.c("#c5c5c5")))
        tip = QPainterPath()
        tip.moveTo(21, 3)
        tip.lineTo(28, 9)
        tip.lineTo(19.5, 12)
        tip.closeSubpath()
        p.drawPath(tip)
    return _icon(draw)


def project():
    """Solution Explorer: a project folder (VS draws a project as a box with the language badge)."""
    def draw(p):
        p.setPen(_pen("#1f5f9e", 1.4))
        p.setBrush(QColor("#e8eef5"))
        p.drawRoundedRect(QRectF(3, 5, 22, 22), 3, 3)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#3c8fd8"))
        p.drawRoundedRect(QRectF(3, 5, 22, 6), 3, 3)
        p.setBrush(QColor(STAR))
        p.drawEllipse(QPointF(24, 23), 6.5, 6.5)
        p.setPen(_pen("#1f1f1f", 1.8))
        f = p.font()
        f.setBold(True)
        f.setPixelSize(9)
        p.setFont(f)
        p.drawText(QRectF(17.5, 16.5, 13, 13), Qt.AlignCenter, "Py")
    return _icon(draw)


def solution():
    """Solution Explorer: the solution (VS draws a purple box)."""
    def draw(p):
        p.setPen(_pen("#5c2d91", 1.4))
        p.setBrush(QColor("#ece4f5"))
        p.drawRoundedRect(QRectF(4, 6, 24, 20), 3, 3)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#68217a"))
        p.drawRoundedRect(QRectF(4, 6, 24, 6), 3, 3)
        p.setBrush(QColor("#9b4fc6"))
        p.drawRect(QRectF(9, 15, 14, 3))
        p.drawRect(QRectF(9, 20, 9, 3))
    return _icon(draw)


def _cube(p, color, dark):
    """VS's member glyph: an isometric box."""
    top = QPainterPath()
    top.moveTo(16, 4); top.lineTo(27, 9.5); top.lineTo(16, 15); top.lineTo(5, 9.5); top.closeSubpath()
    left = QPainterPath()
    left.moveTo(5, 9.5); left.lineTo(16, 15); left.lineTo(16, 28); left.lineTo(5, 22.5); left.closeSubpath()
    right = QPainterPath()
    right.moveTo(27, 9.5); right.lineTo(16, 15); right.lineTo(16, 28); right.lineTo(27, 22.5); right.closeSubpath()
    p.setPen(_pen(dark, 1.2))
    p.setBrush(QColor(color).lighter(130))
    p.drawPath(top)
    p.setBrush(QColor(color))
    p.drawPath(left)
    p.setBrush(QColor(dark))
    p.drawPath(right)


def cls():
    """Class View: a class (VS: orange tree of boxes)."""
    def draw(p):
        p.setPen(_pen("#9a5b00", 1.3))
        p.setBrush(QColor("#f0a030"))
        p.drawRect(QRectF(3, 4, 11, 9))
        p.drawRect(QRectF(18, 12, 11, 8))
        p.drawRect(QRectF(18, 23, 11, 7))
        p.setBrush(Qt.NoBrush)
        p.drawPolyline([QPointF(8.5, 13), QPointF(8.5, 26.5), QPointF(18, 26.5)])
        p.drawLine(QPointF(8.5, 16), QPointF(18, 16))
    return _icon(draw)


def method():
    return _icon(lambda p: _cube(p, "#9b59b6", "#5c2d91"))


def function():
    return _icon(lambda p: _cube(p, "#8e7cc3", "#4b3c8a"))


def field():
    return _icon(lambda p: _cube(p, "#3c8fd8", "#1f5f9e"))


def prop():
    """Class View: a property (VS: a wrench)."""
    def draw(p):
        p.setPen(_pen("#555555", 1.2))
        p.setBrush(QColor("#9aa4ae"))
        path = QPainterPath()
        path.addEllipse(QPointF(22, 10), 7, 7)
        p.drawPath(path)
        p.setCompositionMode(QPainter.CompositionMode_Clear)     # the wrench's notch
        p.setPen(Qt.NoPen)
        p.drawRect(QRectF(20, 2, 4, 7))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.setPen(_pen("#555555", 4.5))
        p.drawLine(QPointF(17, 15), QPointF(6, 26))
    return _icon(draw)


def module():
    """Class View: a module (.py file)."""
    def draw(p):
        _page(p)
        p.setPen(_pen(BLUE, 1.6))
        f = p.font()
        f.setBold(True)
        f.setPixelSize(10)
        p.setFont(f)
        p.drawText(QRectF(6, 12, 18, 14), Qt.AlignCenter, "py")
    return _icon(draw)


def magnifier():
    """Data viewer: View as Table (Locals / Watch)."""
    def draw(p):
        p.setPen(_pen("#75beff", 3.2))
        p.setBrush(QColor(117, 190, 255, 40))
        p.drawEllipse(QRectF(4, 4, 17, 17))
        p.setPen(_pen("#75beff", 4.2))
        p.drawLine(QPointF(19, 19), QPointF(28, 28))
    return _icon(draw)
