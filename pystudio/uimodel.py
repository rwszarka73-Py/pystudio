"""A Qt Designer .ui file as an editable tree - the model behind the layout designer.

Everything is edited in the XML itself, so whatever PyStudio doesn't touch (custom widgets, resources,
connections, properties it has no editor for) is saved back exactly as it was."""
import copy
import re
import xml.etree.ElementTree as ET

LAYOUT_CLASSES = {"QVBoxLayout": "Vertical", "QHBoxLayout": "Horizontal", "QGridLayout": "Grid",
                  "QFormLayout": "Form"}
PAGED = ("QTabWidget", "QStackedWidget", "QToolBox")
CONTAINERS = ("QWidget", "QGroupBox", "QFrame", "QScrollArea", "QTabWidget", "QStackedWidget", "QToolBox",
              "QDockWidget", "QSplitter")


class UiModel:
    def __init__(self, data):
        self.root = ET.fromstring(data)
        self.top = self.root.find("widget")
        if self.top is None:
            raise ValueError("This .ui file has no form in it")
        self.reindex()

    # ---------------------------------------------------------- structure
    def reindex(self):
        self.parent = {c: p for p in self.root.iter() for c in p}
        self.by_name = {}
        for el in self.root.iter():
            if el.tag in ("widget", "layout", "spacer", "action") and el.get("name"):
                self.by_name[el.get("name")] = el

    def to_bytes(self):
        ET.indent(self.root, space=" ")
        return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(self.root, encoding="utf-8")

    def snapshot(self):
        return ET.tostring(self.root, encoding="unicode")

    def restore(self, text):
        self.root = ET.fromstring(text)
        self.top = self.root.find("widget")
        self.reindex()

    @property
    def form_class(self):
        return self.top.get("class", "QWidget")

    def central(self):
        """Where the form's controls live: the central widget of a main window, else the form itself."""
        if self.form_class == "QMainWindow":
            for w in self.top.findall("widget"):
                if w.get("class") == "QWidget":
                    return w
        return self.top

    def widget_parent(self, el):
        """The widget element that contains el (through layouts/items)."""
        p = self.parent.get(el)
        while p is not None and p.tag != "widget":
            p = self.parent.get(p)
        return p

    def own_layout(self, widget_el):
        return widget_el.find("layout")

    def item_of(self, el):
        p = self.parent.get(el)
        return p if p is not None and p.tag == "item" else None

    def layout_of(self, el):
        """The layout element el sits in (directly), or None."""
        item = self.item_of(el)
        return self.parent.get(item) if item is not None else None

    def names(self):
        return set(self.by_name)

    def unique(self, base):
        base = re.sub(r"\W", "", base) or "widget"
        base = base[0].lower() + base[1:]
        n = 1
        while f"{base}{n if n > 1 else ''}" in self.by_name:
            n += 1
        return f"{base}{n if n > 1 else ''}"

    # ---------------------------------------------------------- properties
    @staticmethod
    def prop_el(el, name):
        for p in el.findall("property"):
            if p.get("name") == name:
                return p
        return None

    def prop_text(self, el, name):
        p = self.prop_el(el, name)
        if p is None or not len(p):
            return None
        return p[0].text

    def set_prop(self, el, name, kind, value, stdset=True):
        """kind: string number double bool enum set size rect sizepolicy font cstring."""
        old = self.prop_el(el, name)
        p = ET.Element("property", {"name": name})
        if not stdset:
            p.set("stdset", "0")
        if kind in ("string", "number", "double", "enum", "set", "cstring"):
            v = ET.SubElement(p, kind)
            v.text = ("true" if value else "false") if isinstance(value, bool) and kind != "string" else str(value)
        elif kind == "bool":
            ET.SubElement(p, "bool").text = "true" if value else "false"
        elif kind == "size":
            s = ET.SubElement(p, "size")
            ET.SubElement(s, "width").text = str(int(value[0]))
            ET.SubElement(s, "height").text = str(int(value[1]))
        elif kind == "rect":
            r = ET.SubElement(p, "rect")
            for k, v in zip(("x", "y", "width", "height"), value):
                ET.SubElement(r, k).text = str(int(v))
        elif kind == "sizepolicy":
            s = ET.SubElement(p, "sizepolicy", {"hsizetype": value[0], "vsizetype": value[1]})
            ET.SubElement(s, "horstretch").text = str(value[2] if len(value) > 2 else 0)
            ET.SubElement(s, "verstretch").text = str(value[3] if len(value) > 3 else 0)
        elif kind == "font":
            f = ET.SubElement(p, "font")
            for k in ("family", "pointsize", "bold", "italic", "underline"):
                if k in value and value[k] not in (None, ""):
                    v = value[k]
                    ET.SubElement(f, k).text = ("true" if v else "false") if isinstance(v, bool) else str(v)
        else:
            raise ValueError(kind)
        if old is not None:
            idx = list(el).index(old)
            el.remove(old)
            el.insert(idx, p)
        else:
            props = [i for i, c in enumerate(el) if c.tag == "property"]
            el.insert(props[-1] + 1 if props else 0, p)
        return p

    def remove_prop(self, el, name):
        p = self.prop_el(el, name)
        if p is not None:
            el.remove(p)

    # ---------------------------------------------------------- items (combo/list), columns (table/tree)
    def items(self, el):
        out = []
        for it in el.findall("item"):
            t = it.find("property/string")
            out.append(t.text or "" if t is not None else "")
        return out

    def set_items(self, el, texts):
        for it in el.findall("item"):
            el.remove(it)
        for t in texts:
            it = ET.SubElement(el, "item")
            p = ET.SubElement(it, "property", {"name": "text"})
            ET.SubElement(p, "string").text = t

    def columns(self, el):
        return [(c.find("property/string").text or "") if c.find("property/string") is not None else ""
                for c in el.findall("column")]

    def set_columns(self, el, texts):
        for c in el.findall("column"):
            el.remove(c)
        for t in texts:
            c = ET.SubElement(el, "column")
            p = ET.SubElement(c, "property", {"name": "text"})
            ET.SubElement(p, "string").text = t

    # ---------------------------------------------------------- creating
    def new_widget(self, cls, text=None):
        el = ET.Element("widget", {"class": cls, "name": self.unique(cls[1:] if cls.startswith("Q") else cls)})
        if text is not None:
            prop = "title" if cls == "QGroupBox" else "text"
            self.set_prop(el, prop, "string", text)
        if cls == "QTabWidget":
            for i in (1, 2):
                page = ET.SubElement(el, "widget", {"class": "QWidget", "name": f"{el.get('name')}Page{i}"})
                a = ET.SubElement(page, "attribute", {"name": "title"})
                ET.SubElement(a, "string").text = f"Tab {i}"
        self.reindex()
        return el

    def new_spacer(self, orientation):
        el = ET.Element("spacer", {"name": self.unique("horizontalSpacer" if orientation == "h" else "verticalSpacer")})
        p = ET.SubElement(el, "property", {"name": "orientation"})
        ET.SubElement(p, "enum").text = "Qt::Horizontal" if orientation == "h" else "Qt::Vertical"
        p = ET.SubElement(el, "property", {"name": "sizeHint", "stdset": "0"})
        s = ET.SubElement(p, "size")
        ET.SubElement(s, "width").text = "40" if orientation == "h" else "20"
        ET.SubElement(s, "height").text = "20" if orientation == "h" else "40"
        return el

    # ---------------------------------------------------------- moving things around
    def detach(self, el):
        """Take el out of where it is (its layout cell is closed up for box layouts)."""
        p = self.parent.get(el)
        if p is None:
            return
        if p.tag == "item":
            lay = self.parent.get(p)
            lay.remove(p)
            if lay.get("class") in ("QGridLayout", "QFormLayout"):
                self._compact_rows(lay)
        else:
            p.remove(el)
        self.reindex()

    def _compact_rows(self, lay):
        rows = sorted({int(i.get("row", 0)) for i in lay.findall("item")})
        remap = {r: n for n, r in enumerate(rows)}
        for i in lay.findall("item"):
            i.set("row", str(remap[int(i.get("row", 0))]))

    def insert_in_box(self, lay, index, el):
        item = ET.Element("item")
        item.append(el)
        items = [i for i, c in enumerate(lay) if c.tag == "item"]
        pos = items[index] if index < len(items) else (items[-1] + 1 if items else len(lay))
        lay.insert(pos, item)
        self.reindex()

    def insert_in_grid(self, lay, row, col, el, new_row=False, colspan=None):
        """Put el in the cell; new_row shifts rows >= row down first."""
        if new_row:
            for i in lay.findall("item"):
                if int(i.get("row", 0)) >= row:
                    i.set("row", str(int(i.get("row", 0)) + 1))
        item = ET.Element("item", {"row": str(row), "column": str(col)})
        if colspan:
            item.set("colspan", str(colspan))
        item.append(el)
        lay.append(item)
        self.reindex()

    def cell_taken(self, lay, row, col):
        for i in lay.findall("item"):
            r, c = int(i.get("row", 0)), int(i.get("column", 0))
            rs, cs = int(i.get("rowspan", 1)), int(i.get("colspan", 1))
            if r <= row < r + rs and c <= col < c + cs:
                return True
        return False

    def place_absolute(self, container, el, rect):
        self.set_prop(el, "geometry", "rect", rect)
        container.append(el)
        self.reindex()

    # ---------------------------------------------------------- layouts
    def apply_layout(self, container, cls, geometries):
        """Lay out a container's (absolutely placed) children. geometries: {name: (x, y, w, h)} from the
        preview, so the order follows where things are on screen."""
        kids = [c for c in container.findall("widget")]
        if container.get("class") in PAGED or container.find("layout") is not None:
            return False
        lay = ET.Element("layout", {"class": cls, "name": self.unique({"QVBoxLayout": "verticalLayout",
                                                                       "QHBoxLayout": "horizontalLayout",
                                                                       "QGridLayout": "gridLayout",
                                                                       "QFormLayout": "formLayout"}[cls])})

        def g(c):
            return geometries.get(c.get("name"), (0, 0, 0, 0))
        for c in kids:
            container.remove(c)
            self.remove_prop(c, "geometry")
        if cls in ("QVBoxLayout", "QHBoxLayout"):
            key = (lambda c: (g(c)[1], g(c)[0])) if cls == "QVBoxLayout" else (lambda c: (g(c)[0], g(c)[1]))
            for c in sorted(kids, key=key):
                ET.SubElement(lay, "item").append(c)
        else:
            rows = []
            for c in sorted(kids, key=lambda c: (g(c)[1], g(c)[0])):
                cy = g(c)[1] + g(c)[3] / 2
                for row in rows:
                    if abs(row[0] - cy) <= max(12, g(c)[3] / 2):
                        row[1].append(c)
                        break
                else:
                    rows.append([cy, [c]])
            for r, (_cy, cells) in enumerate(rows):
                cells.sort(key=lambda c: g(c)[0])
                if cls == "QFormLayout" and len(cells) == 1:
                    ET.SubElement(lay, "item", {"row": str(r), "column": "0", "colspan": "2"}).append(cells[0])
                    continue
                for col, c in enumerate(cells[:2] if cls == "QFormLayout" else cells):
                    ET.SubElement(lay, "item", {"row": str(r), "column": str(col)}).append(c)
                for c in (cells[2:] if cls == "QFormLayout" else []):
                    ET.SubElement(lay, "item", {"row": str(r), "column": "1"}).append(c)
        pos = max([i + 1 for i, c in enumerate(container) if c.tag in ("property", "attribute")] or [0])
        container.insert(pos, lay)
        self.reindex()
        return True

    def break_layout(self, container, geometries):
        """Remove the container's layout; every widget in it (and nested layouts) keeps its place."""
        lay = container.find("layout")
        if lay is None:
            return False
        widgets = []

        def collect(l):
            for item in l.findall("item"):
                for sub in item:
                    if sub.tag == "widget":
                        widgets.append(sub)
                    elif sub.tag == "layout":
                        collect(sub)
        collect(lay)
        container.remove(lay)
        for w in widgets:
            g = geometries.get(w.get("name"))
            if g:
                self.set_prop(w, "geometry", "rect", g)
            container.append(w)
        self.reindex()
        return True

    def tree(self):
        """[(depth, element)] for the Object Inspector."""
        out = []

        def walk(el, depth):
            for c in el:
                if c.tag in ("widget", "spacer"):
                    out.append((depth, c))
                    walk(c, depth + 1)
                elif c.tag == "layout":
                    out.append((depth, c))
                    walk(c, depth + 1)
                elif c.tag == "item":
                    walk(c, depth)
        out.append((0, self.top))
        walk(self.top, 1)
        return out

    def copy_el(self, el):
        return copy.deepcopy(el)
