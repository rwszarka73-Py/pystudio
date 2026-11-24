"""Convert a Qt Designer form that uses layouts into a PyStudio (WinForms-style) form.

Qt lays the form out at its designed size; every control gets the position and size Qt gave it, and the
layouts are removed. Then the form is laid out once more, bigger, to see which controls grew or moved
- those get Anchor (Left+Right / Top+Bottom / Right / Bottom) so the converted form still resizes the
way the layouts did. Control names, properties, items, menus and toolbars are kept, so code that uses
self.ui.<name> keeps working. The original file is kept as <form>.ui.bak."""
import copy
import os
import shutil
import xml.etree.ElementTree as ET

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtWidgets import QApplication, QLayout, QStackedWidget, QTabWidget, QToolBox, QWidget

GROW_W, GROW_H = 240, 180          # how much bigger the second layout pass makes the form


def uses_layouts(path):
    try:
        top = ET.parse(path).getroot().find("widget")
    except (ET.ParseError, OSError):
        return False
    return top is not None and top.find(".//layout") is not None


def _load(xml_bytes, workdir):
    from PySide6.QtUiTools import QUiLoader
    loader = QUiLoader()
    from PySide6.QtCore import QDir
    loader.setWorkingDirectory(QDir(workdir))
    buf = QBuffer()
    buf.setData(QByteArray(xml_bytes))
    buf.open(QIODevice.ReadOnly)
    w = loader.load(buf)
    buf.close()
    if w is None:
        raise ValueError(loader.errorString() or "Qt couldn't load the form")
    return w


def _settle(top):
    """Make Qt lay out everything, including tab pages that aren't showing."""
    app = QApplication.instance()
    for _ in range(3):
        app.processEvents()
    for stack in top.findChildren(QWidget):
        if isinstance(stack, (QTabWidget, QStackedWidget, QToolBox)):
            cur = stack.currentIndex()
            for i in range(stack.count()):
                stack.setCurrentIndex(i)
                app.processEvents()
            stack.setCurrentIndex(cur)
    for lay in top.findChildren(QLayout):
        lay.activate()
    app.processEvents()


def _measure(top):
    return {w.objectName(): (w.geometry(), w.parentWidget()) for w in top.findChildren(QWidget) if w.objectName()}


def _set_geometry(el, rect):
    for p in el.findall("property"):
        if p.get("name") == "geometry":
            el.remove(p)
    p = ET.Element("property", {"name": "geometry"})
    r = ET.SubElement(p, "rect")
    for k, v in zip(("x", "y", "width", "height"), (rect.x(), rect.y(), rect.width(), rect.height())):
        ET.SubElement(r, k).text = str(v)
    el.insert(0, p)


def _set_string(el, name, value):
    for p in el.findall("property"):
        if p.get("name") == name:
            el.remove(p)
    p = ET.SubElement(el, "property", {"name": name, "stdset": "0"})
    ET.SubElement(p, "string").text = value


def _anchor(g1, g2, grow_w, grow_h):
    """Anchor from how a control changed when its container grew by (grow_w, grow_h)."""
    sides = []
    dx, dw = g2.x() - g1.x(), g2.width() - g1.width()
    dy, dh = g2.y() - g1.y(), g2.height() - g1.height()
    if grow_h > 0 and dh >= grow_h * 0.4:
        sides += ["Top", "Bottom"]
    elif grow_h > 0 and dy >= grow_h * 0.6:
        sides += ["Bottom"]
    else:
        sides += ["Top"]
    if grow_w > 0 and dw >= grow_w * 0.4:
        sides += ["Left", "Right"]
    elif grow_w > 0 and dx >= grow_w * 0.6:
        sides += ["Right"]
    else:
        sides += ["Left"]
    return ", ".join(sides)


def convert(path, backup=True):
    """Rewrite the .ui at path without layouts. Returns a report (list of lines)."""
    with open(path, "rb") as f:
        original = f.read()
    tree = ET.ElementTree(ET.fromstring(original))
    root = tree.getroot()
    top_el = root.find("widget")
    if top_el is None or top_el.get("class") != "QMainWindow":
        raise ValueError("Only main-window forms (QMainWindow) can be converted. Dialogs and plain widgets "
                         "made in Qt Designer still open in Qt Designer.")
    workdir = os.path.dirname(os.path.abspath(path))

    # pass 1: the designed size; pass 2: bigger (to learn the anchors)
    w = _load(original, workdir)
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    size = w.size()
    if size.width() < 50 or size.height() < 50:
        size = w.sizeHint()
    w.resize(size)
    w.show()
    _settle(w)
    first = _measure(w)
    w.resize(size.width() + GROW_W, size.height() + GROW_H)
    _settle(w)
    second = _measure(w)
    w.hide()
    w.deleteLater()

    report = []
    removed_layouts, removed_spacers = [], 0

    def growth(parent_name):
        if parent_name in first and parent_name in second:
            a, b = first[parent_name][0], second[parent_name][0]
            return b.width() - a.width(), b.height() - a.height()
        return GROW_W, GROW_H

    def flatten(widget_el):
        """Children of widget_el that sit in layouts become direct children with a geometry."""
        nonlocal removed_spacers
        new_children = []
        for child in list(widget_el):
            if child.tag == "layout":
                removed_layouts.append(child.get("name") or "(layout)")
                for item_widget in _layout_widgets(child):
                    new_children.append(item_widget)
                removed_spacers += len(child.findall(".//spacer"))
                widget_el.remove(child)
        for c in new_children:
            widget_el.append(c)
        pname = widget_el.get("name")
        gw, gh = growth(pname)
        for c in widget_el.findall("widget"):
            name = c.get("name")
            if name in first and widget_el.get("class") not in ("QTabWidget", "QStackedWidget", "QToolBox"):
                g1 = first[name][0]
                _set_geometry(c, g1)
                if name in second:
                    _set_string(c, "anchor", _anchor(g1, second[name][0], gw, gh))
            flatten(c)

    for child in top_el.findall("widget"):
        if child.get("name") == "centralwidget" or child.get("class") == "QWidget":
            flatten(child)

    # the form's size: its central area as Qt laid it out, plus PyStudio's menu/tool/status strips
    from .model import MENU_H, STATUS_H, TOOL_H
    cw = first.get("centralwidget", (None,))[0]
    if cw is not None and cw.width() > 0:
        kids = {c.get("class") for c in top_el.findall("widget")}
        bars = (MENU_H if "QMenuBar" in kids else 0) + (STATUS_H if "QStatusBar" in kids else 0) + \
            (TOOL_H if "QToolBar" in kids else 0)
        _set_geometry(top_el, _rect(0, 0, cw.width(), cw.height() + bars))
    else:
        _set_geometry(top_el, _rect(0, 0, size.width(), size.height()))

    if backup:
        bak = path + ".bak"
        n = 2
        while os.path.exists(bak):
            bak = f"{path}.bak{n}"
            n += 1
        shutil.copy2(path, bak)
        report.append(f"The original (with layouts) is saved as {os.path.basename(bak)}.")
    ET.indent(tree, space=" ")
    tree.write(path, encoding="UTF-8", xml_declaration=True)
    report.append(f"Removed {len(removed_layouts)} layout(s)" + (f" and {removed_spacers} spacer(s)"
                                                                 if removed_spacers else "")
                  + "; every control keeps its place and name, and controls that stretched with the window "
                    "got an Anchor so they still do.")
    stem = os.path.splitext(path)[0]
    py = stem + ".py"
    used = []
    if os.path.exists(py) and removed_layouts:
        with open(py, encoding="utf-8", errors="replace") as f:
            code = f.read()
        used = [n for n in removed_layouts if n and f"ui.{n}" in code]
    if used:
        report.append("Your code uses these layouts, which no longer exist - change those lines: " + ", ".join(used))
    folder = os.path.dirname(os.path.abspath(path))
    calls_setup = False
    for f in os.listdir(folder):
        if f.endswith(".py") and not f.startswith("ui_") and f != "pystudio_runtime.py":
            try:
                with open(os.path.join(folder, f), encoding="utf-8", errors="replace") as fh:
                    if "setup_form(" in fh.read():
                        calls_setup = True
                        break
            except OSError:
                pass
    if not calls_setup:
        report.append("For the Anchors to resize controls when the program runs, call PyStudio's runtime after "
                      "setupUi:  from pystudio_runtime import setup_form   then   setup_form(window)")
    return report


def _rect(x, y, w, h):
    from PySide6.QtCore import QRect
    return QRect(x, y, w, h)


def _layout_widgets(layout_el):
    """Widgets inside a layout (and nested layouts), in order."""
    out = []
    for item in layout_el.findall("item"):
        for sub in item:
            if sub.tag == "widget":
                out.append(copy.deepcopy(sub))
            elif sub.tag == "layout":
                out += _layout_widgets(sub)
    return out
