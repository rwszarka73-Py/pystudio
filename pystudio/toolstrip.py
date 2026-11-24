"""In-place ToolStrip (toolbar) designer, like VS.

Model: {"name": "toolStrip1", "style": "ImageAndText", "items": [...]} where an item is
  {"name", "text", "icon", "tooltip", "enabled", "checkable", "checked"}   (own button)
  {"ref": "actionOpen"}                                                    (shares a menu item)
  {"sep": True}
"""
import os
import xml.etree.ElementTree as ET

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QWidget

from . import model as M
from .model import TOOL_H

STYLES = {"Text": "ToolButtonTextOnly", "Image": "ToolButtonIconOnly",
          "ImageAndText": "ToolButtonTextBesideIcon"}


def new_button(name):
    return {"name": name, "text": name, "icon": "", "tooltip": "", "enabled": True,
            "checkable": False, "checked": False}


class ToolBarStrip(QWidget):
    def __init__(self, designer):
        super().__init__()
        self.d = designer
        self.setFixedHeight(TOOL_H)
        self.hide()

    def _label(self, it):
        if it.get("ref"):
            m = self.d.find_menu_action(it["ref"])
            return (m or {}).get("text", it["ref"]).replace("&", ""), (m or {}).get("icon", "")
        return it.get("text", ""), it.get("icon", "")

    def _rects(self):
        fm = self.fontMetrics()
        style = (self.d.toolbar or {}).get("style", "ImageAndText")
        x = 4
        out = []
        for i, it in enumerate((self.d.toolbar or {}).get("items", [])):
            if it.get("sep"):
                w = 9
            else:
                text, icon = self._label(it)
                w = 8
                if style != "Text" and icon:
                    w += 20
                if style != "Image" or not icon:
                    w += fm.horizontalAdvance(text) + 4
                w = max(w, 26)
            out.append((QRect(x, 3, w, self.height() - 6), i))
            x += w + 2
        add = QRect(x + 4, 4, fm.horizontalAdvance("+ Add ▾") + 14, self.height() - 8)
        return out, add

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#eef0f2"))
        p.setPen(QColor("#cfcfcf"))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        style = (self.d.toolbar or {}).get("style", "ImageAndText")
        items = (self.d.toolbar or {}).get("items", [])
        rects, add = self._rects()
        for r, i in rects:
            it = items[i]
            if self.d.special == "toolitem" and self.d.tool_sel == i:
                p.fillRect(r, QColor("#cce4f7"))
                p.setPen(QColor("#3399ff"))
                p.drawRect(r.adjusted(0, 0, -1, -1))
            if it.get("sep"):
                p.setPen(QColor("#b8b8b8"))
                p.drawLine(r.center().x(), r.top() + 3, r.center().x(), r.bottom() - 3)
                continue
            text, icon = self._label(it)
            x = r.left() + 4
            full = os.path.join(self.d.project_dir, icon) if icon and not os.path.isabs(icon) else icon
            if style != "Text" and icon and os.path.exists(full):
                pm = QPixmap(full).scaled(16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                p.drawPixmap(x, r.center().y() - 8, pm)
                x += 20
            if style != "Image" or not icon:
                enabled = it.get("enabled", True) if not it.get("ref") else True
                p.setPen(QColor("#000000") if enabled else QColor("#a0a0a0"))
                p.drawText(QRect(x, r.top(), r.right() - x, r.height()), Qt.AlignVCenter, text)
            if it.get("ref"):
                p.setPen(QColor("#3399ff"))
                p.drawText(QRect(r.right() - 8, r.top() - 2, 8, 10), Qt.AlignCenter, "•")
        p.setPen(QPen(QColor("#a0a0a0"), 1, Qt.DashLine))
        p.setBrush(QColor("#ffffff"))
        p.drawRect(add)
        f = QFont(self.font())
        f.setItalic(True)
        p.setFont(f)
        p.setPen(QColor("#6a6a6a"))
        p.drawText(add, Qt.AlignCenter, "+ Add ▾")
        if self.d.special == "toolstrip":
            p.setPen(QPen(QColor("#3399ff"), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.rect().adjusted(1, 1, -1, -1))

    def _hit(self, pos):
        rects, add = self._rects()
        for r, i in rects:
            if r.contains(pos):
                return i
        return "add" if add.contains(pos) else None

    def mousePressEvent(self, e):
        hit = self._hit(e.position().toPoint())
        gp = e.globalPosition().toPoint()
        if hit == "add":
            self.d.select_special("toolstrip")
            self.d.toolstrip_add_menu(gp)
        elif isinstance(hit, int):
            self.d.select_tool_item(hit)
            if e.button() == Qt.RightButton:
                self.d.toolstrip_context_menu(hit, gp)
        else:
            self.d.select_special("toolstrip")
        self.d.overlay.setFocus()

    def mouseDoubleClickEvent(self, e):
        hit = self._hit(e.position().toPoint())
        if isinstance(hit, int):
            it = self.d.toolbar["items"][hit]
            if not it.get("sep"):
                self.d.handler_requested.emit(it.get("ref") or it["name"])


class ToolStripMixin:
    def _toolstrip_init(self):
        self.toolbar = None
        self.tool_sel = None

    def find_menu_action(self, name):
        def walk(items):
            for it in items or []:
                if it.get("sep"):
                    continue
                if it.get("name") == name and not it.get("items"):
                    return it
                r = walk(it.get("items"))
                if r:
                    return r
            return None
        return walk(self.menu)

    def tool_item(self):
        if self.toolbar is None or self.tool_sel is None:
            return None
        items = self.toolbar["items"]
        return items[self.tool_sel] if 0 <= self.tool_sel < len(items) else None

    def select_tool_item(self, i):
        self.selection = []
        self.selected = None
        self.menu_sel = self.menu_open = None
        self.refresh_menu_panels()
        self.special = "toolitem"
        self.tool_sel = i
        self.tool_strip.update()
        self.overlay.update()
        self.selection_changed.emit()

    def toolstrip_add_menu(self, gp):
        m = QMenu(self)
        m.addAction("Button", lambda: self.add_tool_item("button"))
        m.addAction("Separator", lambda: self.add_tool_item("sep"))
        acts = M.all_actions(self.menu)
        if acts:
            sub = m.addMenu("Menu item (shares its code)")
            for name, text in acts:
                sub.addAction(f"{text}   ({name})", lambda n=name: self.add_tool_item("ref", n))
        m.exec(gp)

    def add_tool_item(self, kind, ref=None, index=None):
        def run():
            items = self.toolbar["items"]
            pos = len(items) if index is None else index
            if kind == "sep":
                items.insert(pos, {"sep": True})
            elif kind == "ref":
                items.insert(pos, {"ref": ref})
            else:
                items.insert(pos, new_button(self.unique_name("toolStripButton")))
            self._new_tool_index = pos
        self.change(run)
        self.select_tool_item(self._new_tool_index)

    def delete_tool_item(self, i):
        def run():
            del self.toolbar["items"][i]
        self.change(run)
        n = len(self.toolbar["items"])
        if n:
            self.select_tool_item(min(i, n - 1))
        else:
            self.select_special("toolstrip")

    def move_tool_item(self, i, delta):
        items = self.toolbar["items"]
        j = i + delta
        if not (0 <= j < len(items)):
            return

        def run():
            items[i], items[j] = items[j], items[i]
        self.change(run)
        self.select_tool_item(j)

    def toolstrip_context_menu(self, i, gp):
        it = self.toolbar["items"][i]
        m = QMenu(self)
        if not it.get("sep"):
            m.addAction("View Code (Click handler)", lambda: self.handler_requested.emit(it.get("ref") or it["name"]))
            m.addSeparator()
        ins = m.addMenu("Insert (before)")
        ins.addAction("Button", lambda: self.add_tool_item("button", index=i))
        ins.addAction("Separator", lambda: self.add_tool_item("sep", index=i))
        m.addAction("Move Left", lambda: self.move_tool_item(i, -1))
        m.addAction("Move Right", lambda: self.move_tool_item(i, 1))
        m.addSeparator()
        m.addAction("Delete", lambda: self.delete_tool_item(i))
        m.exec(gp)

    def rename_tool_item(self, item, new):
        new = new.strip()
        if new == item.get("name"):
            return ""
        if not new.isidentifier():
            return "Names must be letters, digits and _ and can't start with a digit."
        if new in self.names():
            return f"'{new}' is already used on this form."
        old = item["name"]
        item["name"] = new
        self.control_renamed.emit(old, new)
        return ""

    # ------------------------------------------------------------ .ui XML
    def toolbar_to_xml(self, top):
        tb = self.toolbar
        if tb is None:
            return
        el = ET.SubElement(top, "widget", {"class": "QToolBar", "name": tb.get("name", "toolStrip1")})
        M._prop(el, "windowTitle", "string", tb.get("name", "toolStrip1"))
        M._prop(el, "movable", "bool", False)
        M._prop(el, "toolButtonStyle", "enum",
                f"Qt::ToolButtonStyle::{STYLES.get(tb.get('style', 'ImageAndText'))}")
        a = ET.SubElement(el, "attribute", name="toolBarArea")
        ET.SubElement(a, "enum").text = "TopToolBarArea"
        a = ET.SubElement(el, "attribute", name="toolBarBreak")
        ET.SubElement(a, "bool").text = "false"
        own = []
        for it in tb.get("items", []):
            if it.get("sep"):
                ET.SubElement(el, "addaction", name="separator")
            elif it.get("ref"):
                ET.SubElement(el, "addaction", name=it["ref"])
            else:
                ET.SubElement(el, "addaction", name=it["name"])
                own.append(it)
        for it in own:
            a = ET.SubElement(top, "action", name=it["name"])
            if it.get("checkable"):
                M._prop(a, "checkable", "bool", True)
                if it.get("checked"):
                    M._prop(a, "checked", "bool", True)
            if not it.get("enabled", True):
                M._prop(a, "enabled", "bool", False)
            if it.get("icon"):
                M._prop(a, "icon", "iconset", it["icon"])
            M._prop(a, "text", "string", it.get("text", ""))
            if it.get("tooltip"):
                M._prop(a, "toolTip", "string", it["tooltip"])

    def toolbar_from_xml(self, top, el):
        menu_actions = {n for n, _t in M.all_actions(self.menu)}
        actions = {a.get("name"): M.read_props(a) for a in top.findall("action")}
        p = M.read_props(el)
        style = next((k for k, v in STYLES.items() if v in (p.get("toolButtonStyle") or "")), "ImageAndText")
        items = []
        for add in el.findall("addaction"):
            n = add.get("name")
            if n == "separator":
                items.append({"sep": True})
            elif n in menu_actions:
                items.append({"ref": n})
            elif n in actions:
                a = actions[n]
                items.append({"name": n, "text": a.get("text", n), "icon": a.get("icon", ""),
                              "tooltip": a.get("toolTip", ""), "enabled": a.get("enabled", True),
                              "checkable": a.get("checkable", False), "checked": a.get("checked", False)})
        self.toolbar = {"name": el.get("name") or "toolStrip1", "style": style, "items": items}
