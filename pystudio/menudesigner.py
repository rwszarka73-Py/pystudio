"""In-place MenuStrip designer, like VS: click "Type Here" and type.

The menu model is a list of dicts:
  {"name", "text", "shortcut", "items": [...], "checkable", "checked",
   "enabled", "visible", "tooltip", "icon"}   or   {"sep": True}
A path is a tuple of indexes: (0,) = first top menu, (0, 2) = its third item.
"""
import re

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLineEdit, QMenu, QWidget

from .model import MENU_H

ROW_H = 24
SEP_H = 9
TYPE_HERE = "Type Here"


def clean(text):
    return re.sub(r"[^0-9A-Za-z]", "", text.replace("&", "")) or "Item"


def plain(text):
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def new_item(name, text):
    return {"name": name, "text": text, "shortcut": "", "items": [], "checkable": False,
            "checked": False, "enabled": True, "visible": True, "tooltip": "", "icon": ""}


# ============================================================ model helpers
def children(menu, path):
    items = menu
    for i in path:
        items = items[i].setdefault("items", [])
    return items


def item_at(menu, path):
    if not path:
        return None
    try:
        return children(menu, path[:-1])[path[-1]]
    except (IndexError, KeyError, TypeError):
        return None


class InlineEdit(QLineEdit):
    """Edit box placed over a menu entry. Enter = commit, Esc = cancel."""

    def __init__(self, parent, rect, text, on_commit, on_cancel):
        super().__init__(parent)
        self.on_commit, self.on_cancel = on_commit, on_cancel
        self._done = False
        self.setText(text)
        self.setGeometry(rect)
        self.setStyleSheet("QLineEdit { background: #ffffff; color: #000000; border: 1px solid #3399ff;"
                           " padding: 0 4px; }")
        self.returnPressed.connect(self._commit)
        self.show()
        self.setFocus()
        self.selectAll()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._finish(False)
            return
        super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._finish(bool(self.text().strip()))

    def _commit(self):
        self._finish(True)

    def _finish(self, commit):
        if self._done:
            return
        self._done = True
        text = self.text().strip()
        self.hide()
        self.deleteLater()
        if commit and text:
            self.on_commit(text)
        else:
            self.on_cancel()


# ============================================================ menu bar
class MenuBarStrip(QWidget):
    """The MenuStrip across the top of the form, with a 'Type Here' box at the end."""

    def __init__(self, designer):
        super().__init__()
        self.d = designer
        self.setFixedHeight(MENU_H)
        self.setMouseTracking(True)
        self.hide()

    def _rects(self):
        fm = self.fontMetrics()
        x = 4
        out = []
        for i, it in enumerate(self.d.menu or []):
            w = fm.horizontalAdvance(plain(it.get("text", ""))) + 18
            out.append((QRect(x, 1, w, self.height() - 2), i))
            x += w
        th = QRect(x + 2, 3, fm.horizontalAdvance(TYPE_HERE) + 18, self.height() - 6)
        return out, th

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#f5f5f5"))
        p.setPen(QColor("#cfcfcf"))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        rects, th = self._rects()
        sel = self.d.menu_sel
        open_ = self.d.menu_open
        for r, i in rects:
            it = self.d.menu[i]
            if sel == (i,):
                p.fillRect(r, QColor("#cce4f7"))
                p.setPen(QColor("#3399ff"))
                p.drawRect(r.adjusted(0, 0, -1, -1))
            elif open_ and open_[0] == i:
                p.fillRect(r, QColor("#e5f1fb"))
            p.setPen(QColor("#000000") if it.get("enabled", True) else QColor("#a0a0a0"))
            p.drawText(r, Qt.AlignCenter, plain(it.get("text", "")))
        p.setPen(QPen(QColor("#a0a0a0"), 1, Qt.DashLine))
        p.setBrush(QColor("#ffffff"))
        p.drawRect(th)
        f = QFont(self.font())
        f.setItalic(True)
        p.setFont(f)
        p.setPen(QColor("#8a8a8a"))
        p.drawText(th, Qt.AlignCenter, TYPE_HERE)
        if self.d.special == "menu":
            p.setPen(QPen(QColor("#3399ff"), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self.rect().adjusted(1, 1, -1, -1))

    def _hit(self, pos):
        rects, th = self._rects()
        for r, i in rects:
            if r.contains(pos):
                return i
        return "typehere" if th.contains(pos) else None

    def mousePressEvent(self, e):
        hit = self._hit(e.position().toPoint())
        if e.button() == Qt.RightButton and isinstance(hit, int):
            self.d.select_menu_item((hit,))
            self.d.menu_context_menu((hit,), e.globalPosition().toPoint())
            return
        if hit == "typehere":
            self.d.select_special("menu")
            self.edit_type_here()
        elif isinstance(hit, int):
            self.d.select_menu_item((hit,))
        else:
            self.d.select_special("menu")
        self.d.overlay.setFocus() if hit != "typehere" else None

    def mouseDoubleClickEvent(self, e):
        hit = self._hit(e.position().toPoint())
        if isinstance(hit, int):
            self.d.begin_menu_edit((hit,))

    def edit_type_here(self):
        _r, th = self._rects()
        InlineEdit(self, th.adjusted(0, -1, 60, 1), "", lambda t: self.d.add_menu_item((), t, chain=True),
                   lambda: None)

    def edit_item(self, index):
        rects, _th = self._rects()
        r = next((r for r, i in rects if i == index), None)
        if r is not None:
            item = self.d.menu[index]
            InlineEdit(self, r.adjusted(0, 0, 60, 0), item.get("text", ""),
                       lambda t: self.d.set_menu_text((index,), t), lambda: None)


# ============================================================ drop-down panels
class MenuPanel(QWidget):
    """One open drop-down (or submenu) listing the children of `path` + 'Type Here'."""

    def __init__(self, designer, path, parent):
        super().__init__(parent)
        self.d = designer
        self.path = path
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: #ffffff;")
        self.setMouseTracking(True)
        self._layout_rows()

    def items(self):
        return children(self.d.menu, self.path)

    def _layout_rows(self):
        fm = self.fontMetrics()
        w_text = w_sc = 0
        for it in self.items():
            if it.get("sep"):
                continue
            w_text = max(w_text, fm.horizontalAdvance(plain(it.get("text", ""))))
            w_sc = max(w_sc, fm.horizontalAdvance(it.get("shortcut", "")))
        width = max(190, 28 + w_text + (30 + w_sc if w_sc else 0) + 30)
        self.rows = []
        y = 2
        for i, it in enumerate(self.items()):
            h = SEP_H if it.get("sep") else ROW_H
            self.rows.append((QRect(2, y, width - 4, h), i))
            y += h
        self.type_here = QRect(2, y + 1, width - 4, ROW_H - 2)
        self.resize(width, y + ROW_H + 4)

    def row_rect(self, index):
        return next((r for r, i in self.rows if i == index), None)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#ffffff"))
        p.setPen(QColor("#a0a0a0"))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        items = self.items()
        sel = self.d.menu_sel
        open_ = self.d.menu_open or ()
        for r, i in self.rows:
            it = items[i]
            here = self.path + (i,)
            if it.get("sep"):
                if sel == here:
                    p.fillRect(r, QColor("#cce4f7"))
                p.setPen(QColor("#c8c8c8"))
                p.drawLine(r.left() + 26, r.center().y(), r.right() - 4, r.center().y())
                continue
            if sel == here:
                p.fillRect(r, QColor("#cce4f7"))
                p.setPen(QColor("#3399ff"))
                p.drawRect(r.adjusted(0, 0, -1, -1))
            elif open_[:len(here)] == here:
                p.fillRect(r, QColor("#e5f1fb"))
            enabled = it.get("enabled", True)
            p.setPen(QColor("#000000") if enabled else QColor("#a0a0a0"))
            if it.get("checked"):
                p.drawText(QRect(r.left() + 4, r.top(), 20, r.height()), Qt.AlignCenter, "✓")
            p.drawText(r.adjusted(28, 0, -20, 0), Qt.AlignVCenter | Qt.AlignLeft, plain(it.get("text", "")))
            if it.get("shortcut"):
                p.setPen(QColor("#6a6a6a"))
                p.drawText(r.adjusted(28, 0, -22, 0), Qt.AlignVCenter | Qt.AlignRight, it["shortcut"])
            if it.get("items"):
                p.setPen(QColor("#404040"))
                p.drawText(r.adjusted(0, 0, -6, 0), Qt.AlignVCenter | Qt.AlignRight, "▸")
            if not it.get("visible", True):
                p.setPen(QColor("#b0b0b0"))
                p.drawText(QRect(r.left() + 4, r.top(), 20, r.height()), Qt.AlignCenter, "◌")
        th = self.type_here
        p.setPen(QPen(QColor("#a0a0a0"), 1, Qt.DashLine))
        p.setBrush(QColor("#fafafa"))
        p.drawRect(th.adjusted(2, 1, -3, -2))
        f = QFont(self.font())
        f.setItalic(True)
        p.setFont(f)
        p.setPen(QColor("#8a8a8a"))
        p.drawText(th.adjusted(28, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, TYPE_HERE)

    def _hit(self, pos):
        for r, i in self.rows:
            if r.contains(pos):
                return i
        return "typehere" if self.type_here.contains(pos) else None

    def mousePressEvent(self, e):
        hit = self._hit(e.position().toPoint())
        if hit == "typehere":
            self.edit_type_here()
            return
        if isinstance(hit, int):
            self.d.select_menu_item(self.path + (hit,))
            if e.button() == Qt.RightButton:
                self.d.menu_context_menu(self.path + (hit,), e.globalPosition().toPoint())
            else:
                self.d.overlay.setFocus()

    def mouseDoubleClickEvent(self, e):
        hit = self._hit(e.position().toPoint())
        if isinstance(hit, int):
            it = self.items()[hit]
            if not it.get("sep") and not it.get("items"):
                self.d.handler_requested.emit(it["name"])   # like VS: go to the Click handler

    def edit_type_here(self):
        InlineEdit(self, self.type_here, "", lambda t: self.d.add_menu_item(self.path, t, chain=True),
                   lambda: None)

    def edit_item(self, index):
        r = self.row_rect(index)
        it = self.items()[index]
        if r is not None and not it.get("sep"):
            InlineEdit(self, r, it.get("text", ""), lambda t: self.d.set_menu_text(self.path + (index,), t),
                       lambda: None)


class MenuDesignerMixin:
    """FormDesigner methods for the in-place menu designer."""

    def _menu_init(self):
        self.menu_sel = None      # selected item path
        self.menu_open = None     # deepest open path (panels shown along it)
        self.menu_panels = []

    # ---------------------------------------------------------------- panels
    def refresh_menu_panels(self):
        for p in self.menu_panels:
            p.hide()
            p.deleteLater()
        self.menu_panels = []
        self.menu_strip.update()
        if not self.menu or self.special not in ("menu", "menuitem") or not self.menu_open:
            return
        path = self.menu_open
        if item_at(self.menu, path[:1]) is None:
            return
        anchor_widget = self.menu_strip
        rects, _th = self.menu_strip._rects()
        top_rect = next((r for r, i in rects if i == path[0]), None)
        if top_rect is None:
            return
        pos = self.menu_strip.mapTo(self.holder, top_rect.bottomLeft() + QPoint(0, 1))
        for depth in range(1, len(path) + 1):
            sub = path[:depth]
            it = item_at(self.menu, sub)
            if it is None or it.get("sep"):
                break
            if depth > 1 and not (it.get("items") or self.menu_sel == sub):
                break
            panel = MenuPanel(self, sub, self.holder)
            panel.move(pos)
            panel.show()
            panel.raise_()
            self.menu_panels.append(panel)
            if depth < len(path):
                r = panel.row_rect(path[depth])
                if r is None:
                    break
                pos = panel.mapTo(self.holder, r.topRight() + QPoint(2, -2))

    def panel_for(self, parent_path):
        return next((p for p in self.menu_panels if p.path == parent_path), None)

    # ---------------------------------------------------------------- selection
    def select_menu_item(self, path):
        self.selection = []
        self.selected = None
        self.special = "menuitem"
        self.menu_sel = tuple(path)
        it = item_at(self.menu, self.menu_sel)
        # top menu: open its drop-down; item: show its submenu (or a 'Type Here' to start one)
        self.menu_open = self.menu_sel
        if it is not None and it.get("sep"):
            self.menu_open = self.menu_sel[:-1]
        self.refresh_menu_panels()
        self.overlay.update()
        self.selection_changed.emit()

    def begin_menu_edit(self, path=None):
        path = tuple(path or self.menu_sel or ())
        if not path:
            return
        if len(path) == 1:
            self.menu_strip.edit_item(path[0])
            return
        panel = self.panel_for(path[:-1])
        if panel is not None:
            panel.edit_item(path[-1])

    # ---------------------------------------------------------------- changes
    def _menu_name(self, text, top):
        base = ("menu" if top else "action") + clean(text)
        return self.unique_name(base) if base in self.names() else base

    def add_menu_item(self, parent_path, text, chain=False, index=None):
        parent_path = tuple(parent_path)
        new_path = None

        def run():
            nonlocal new_path
            items = children(self.menu, parent_path) if parent_path else self.menu
            pos = len(items) if index is None else index
            if text.strip() == "-" and parent_path:
                items.insert(pos, {"sep": True})
            else:
                items.insert(pos, new_item(self._menu_name(text, not parent_path), text))
            new_path = parent_path + (pos,)
        if self.menu is None:
            self.menu = []
            self._update_strips()
        self.change(run)
        self._update_strips()
        if chain:
            # like VS: keep typing - the next 'Type Here' is ready
            if not parent_path:
                self.select_menu_item(new_path)
                panel = self.panel_for(new_path)
            else:
                self.select_menu_item(new_path)
                self.special = "menuitem"
                panel = self.panel_for(parent_path)
            if panel is not None:
                panel.edit_type_here()
        else:
            self.select_menu_item(new_path)

    def set_menu_text(self, path, text):
        it = item_at(self.menu, path)
        if it is None:
            return

        def run():
            it["text"] = text
        self.change(run)
        self.select_menu_item(path)

    def delete_menu_item(self, path):
        path = tuple(path)

        def run():
            items = children(self.menu, path[:-1]) if len(path) > 1 else self.menu
            if 0 <= path[-1] < len(items):
                del items[path[-1]]
        self.change(run)
        if not self.menu:
            self.menu_sel = self.menu_open = None
            self.select_special("menu")
        else:
            items = children(self.menu, path[:-1]) if len(path) > 1 else self.menu
            if items:
                self.select_menu_item(path[:-1] + (min(path[-1], len(items) - 1),))
            else:
                self.select_menu_item(path[:-1]) if len(path) > 1 else self.select_special("menu")

    def move_menu_item(self, path, delta):
        path = tuple(path)
        items = children(self.menu, path[:-1]) if len(path) > 1 else self.menu
        j = path[-1] + delta
        if not (0 <= j < len(items)):
            return

        def run():
            items[path[-1]], items[j] = items[j], items[path[-1]]
        self.change(run)
        self.select_menu_item(path[:-1] + (j,))

    def insert_menu_item(self, path, sep=False):
        path = tuple(path)
        parent = path[:-1]
        if sep:
            if not parent:
                return
            self.change(lambda: children(self.menu, parent).insert(path[-1], {"sep": True}))
            self.select_menu_item(path)
        else:
            self.add_menu_item(parent, "New Item" if parent else "New Menu", index=path[-1])
            self.begin_menu_edit(path)

    def menu_context_menu(self, path, global_pos):
        it = item_at(self.menu, path)
        m = QMenu(self)
        if it is not None and not it.get("sep") and not it.get("items") and len(path) > 1:
            m.addAction("View Code (Click handler)", lambda: self.handler_requested.emit(it["name"]))
            m.addSeparator()
        if it is not None and not it.get("sep"):
            m.addAction("Edit Text\tF2", lambda: self.begin_menu_edit(path))
        ins = m.addMenu("Insert")
        ins.addAction("Menu Item (above)", lambda: self.insert_menu_item(path))
        if len(path) > 1:
            ins.addAction("Separator (above)", lambda: self.insert_menu_item(path, sep=True))
        m.addAction("Move Up", lambda: self.move_menu_item(path, -1))
        m.addAction("Move Down", lambda: self.move_menu_item(path, 1))
        m.addSeparator()
        m.addAction("Delete\tDel", lambda: self.delete_menu_item(path))
        m.exec(global_pos)

    def rename_menu_item(self, item, new):
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
