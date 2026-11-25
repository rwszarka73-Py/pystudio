"""Import a Visual Studio VB.NET WinForms project (.vbproj) into a PyStudio project.

For each form, the controls, positions and properties from <Form>.Designer.vb (InitializeComponent)
become a PyStudio form (.ui) - built through PyStudio's own designer, so it opens and saves like
any other form. Event handlers (Sub ... Handles Button1.Click) become Python handlers already
wired up, with the VB code inside as comments to translate. My.Settings, the startup form and
pictures stored in the forms' .resx files come along too. The original .vb files are copied to
vb_original/ for reference."""
import base64
import os
import re
import shutil
import xml.etree.ElementTree as ET

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor

from . import codegen
from . import model as M
from . import properties as P

# VB control type -> PyStudio tool
TYPE_MAP = {
    "Button": "Button", "Label": "Label", "TextBox": "TextBox", "RichTextBox": "TextArea",
    "CheckBox": "CheckBox", "RadioButton": "RadioButton", "ComboBox": "ComboBox", "ListBox": "ListBox",
    "CheckedListBox": "ListBox", "NumericUpDown": "NumericUpDown", "DateTimePicker": "DateTimePicker",
    "MonthCalendar": "DateTimePicker", "TrackBar": "TrackBar", "ProgressBar": "ProgressBar",
    "PictureBox": "PictureBox", "DataGridView": "DataGridView", "GroupBox": "GroupBox", "Panel": "Panel",
    "FlowLayoutPanel": "Panel", "TableLayoutPanel": "Panel", "LinkLabel": "LinkLabel",
    "MaskedTextBox": "MaskedTextBox", "TreeView": "TreeView", "ListView": "ListView",
    "TabControl": "TabControl", "SplitContainer": "SplitContainer", "WebBrowser": "WebBrowser",
    "WebView2": "WebBrowser", "Chart": "Chart", "BindingNavigator": "BindingNavigator",
    "DomainUpDown": "ComboBox", "HScrollBar": "TrackBar", "VScrollBar": "TrackBar",
}
COMPONENT_MAP = {"Timer": "Timer", "OpenFileDialog": "OpenFileDialog", "SaveFileDialog": "SaveFileDialog",
                 "FolderBrowserDialog": "FolderBrowserDialog", "ColorDialog": "ColorDialog",
                 "FontDialog": "FontDialog", "ImageList": "ImageList", "PrintDocument": "PrintDocument",
                 "PrintDialog": "PrintDialog", "PrintPreviewDialog": "PrintPreviewDialog",
                 "PageSetupDialog": "PageSetupDialog", "ContextMenuStrip": "ContextMenuStrip",
                 "NotifyIcon": "NotifyIcon", "ErrorProvider": "ErrorProvider", "BindingSource": "BindingSource"}
SKIPPED_TYPES = {"ToolTip": "tool tips are set on each control (ToolTip property)",
                 "BackgroundWorker": "use a QThread/QTimer in code", "HelpProvider": "not supported",
                 "SerialPort": "use pyserial in code (Package Manager)"}
STRIP_ITEMS = {"ToolStripMenuItem", "ToolStripSeparator", "ToolStripButton", "ToolStripStatusLabel",
               "ToolStripLabel", "ToolStripComboBox", "ToolStripTextBox", "ToolStripDropDownButton",
               "ToolStripSplitButton", "ToolStripProgressBar", "DataGridViewTextBoxColumn",
               "DataGridViewCheckBoxColumn", "DataGridViewComboBoxColumn", "DataGridViewButtonColumn",
               "DataGridViewImageColumn", "DataGridViewLinkColumn", "ColumnHeader", "TabPage",
               "SplitterPanel", "ToolStripContainer"}
VALUE_TYPES = {"Size", "SizeF", "Point", "PointF", "Font", "Padding", "Rectangle", "Container", "Icon",
               "Decimal", "Bitmap", "Cursor"}
WINDOWS_FONTS = {"microsoft sans serif", "segoe ui", "tahoma", "arial", "calibri", "verdana", "ms sans serif"}

# VB event -> Qt signal, by PyStudio tool ("*" = any)
EVENT_MAP = {
    ("Button", "Click"): "clicked", ("CheckBox", "CheckedChanged"): "toggled", ("CheckBox", "Click"): "clicked",
    ("RadioButton", "CheckedChanged"): "toggled", ("RadioButton", "Click"): "clicked",
    ("TextBox", "TextChanged"): "textChanged", ("TextBox", "Leave"): "editingFinished",
    ("TextBox", "Validated"): "editingFinished", ("TextBox", "Validating"): "editingFinished",
    ("MaskedTextBox", "Validating"): "editingFinished", ("MaskedTextBox", "Leave"): "editingFinished", ("MaskedTextBox", "TextChanged"): "textChanged",
    ("TextArea", "TextChanged"): "textChanged",
    ("ComboBox", "SelectedIndexChanged"): "currentIndexChanged",
    ("ComboBox", "SelectedValueChanged"): "currentIndexChanged", ("ComboBox", "TextChanged"): "currentTextChanged",
    ("ComboBox", "SelectionChangeCommitted"): "activated",
    ("ListBox", "SelectedIndexChanged"): "currentRowChanged", ("ListBox", "DoubleClick"): "itemDoubleClicked",
    ("ListBox", "Click"): "itemClicked",
    ("NumericUpDown", "ValueChanged"): "valueChanged", ("TrackBar", "ValueChanged"): "valueChanged",
    ("TrackBar", "Scroll"): "valueChanged", ("DateTimePicker", "ValueChanged"): "dateTimeChanged",
    ("DataGridView", "CellClick"): "cellClicked", ("DataGridView", "CellContentClick"): "cellClicked",
    ("DataGridView", "CellDoubleClick"): "cellDoubleClicked", ("DataGridView", "CellValueChanged"): "cellChanged",
    ("DataGridView", "SelectionChanged"): "itemSelectionChanged",
    ("DataGridView", "CellMouseDown"): "cellPressed", ("DataGridView", "CellMouseClick"): "cellPressed",
    ("TreeView", "AfterSelect"): "currentItemChanged", ("TreeView", "NodeMouseDoubleClick"): "itemDoubleClicked",
    ("TreeView", "NodeMouseClick"): "itemClicked", ("TreeView", "AfterExpand"): "itemExpanded",
    ("ListView", "SelectedIndexChanged"): "itemSelectionChanged", ("ListView", "DoubleClick"): "itemDoubleClicked",
    ("ListView", "ItemActivate"): "itemDoubleClicked",
    ("TabControl", "SelectedIndexChanged"): "currentChanged", ("LinkLabel", "LinkClicked"): "linkActivated",
    ("Timer", "Tick"): "timeout", ("PrintDocument", "PrintPage"): "printPage",
    ("PrintDocument", "BeginPrint"): "beginPrint", ("PrintDocument", "EndPrint"): "endPrint",
    ("WebBrowser", "DocumentCompleted"): "documentCompleted", ("WebBrowser", "Navigated"): "navigated",
    ("GroupBox", "CheckedChanged"): "toggled", ("SplitContainer", "SplitterMoved"): "splitterMoved",
    ("menu", "Click"): "triggered", ("toolbar", "Click"): "triggered",
    ("NotifyIcon", "DoubleClick"): "doubleClicked", ("NotifyIcon", "MouseDoubleClick"): "doubleClicked",
    ("NotifyIcon", "Click"): "clicked", ("NotifyIcon", "MouseClick"): "clicked",
    ("NotifyIcon", "BalloonTipClicked"): "messageClicked", ("ContextMenuStrip", "Opening"): "aboutToShow",
    ("ContextMenuStrip", "ItemClicked"): "triggered",
    ("BindingSource", "PositionChanged"): "positionChanged", ("BindingSource", "CurrentChanged"): "positionChanged",
    ("BindingSource", "ListChanged"): "listChanged",
}

KEY_NAMES = {"Control": "Ctrl", "Shift": "Shift", "Alt": "Alt", "Delete": "Del", "Return": "Return",
             "Enter": "Return", "Escape": "Esc", "Oemplus": "+", "OemMinus": "-", "Insert": "Ins"}


# ============================================================ value parsing
def _string(raw):
    raw = raw.strip()
    parts = re.findall(r'"((?:[^"]|"")*)"', raw)
    if not parts:
        return None
    text = "".join(p.replace('""', '"') for p in parts)
    if "vbCrLf" in raw or "Global.Microsoft.VisualBasic.ChrW(13)" in raw or "ChrW(10)" in raw:
        segs = re.split(r"\s*&\s*", raw)
        out = []
        for seg in segs:
            if seg.startswith('"'):
                out.append(_string(seg) or "")
            elif "vbCrLf" in seg or "vbNewLine" in seg or "ChrW(10)" in seg:
                out.append("\n")
        text = "".join(out)
    return text


def _ints(raw):
    return [int(float(x)) for x in re.findall(r"-?\d+(?:\.\d+)?", raw)]


def _number(raw):
    raw = raw.strip()
    m = re.match(r"New (?:System\.)?Decimal\(New Integer\(\) \{(-?\d+), (-?\d+), (-?\d+), (-?\d+)\}\)", raw)
    if m:
        lo, scale = int(m.group(1)), (int(m.group(4)) >> 16) & 0xFF
        v = lo / (10 ** scale) if scale else lo
        return -v if int(m.group(4)) < 0 else v
    m = re.match(r"-?\d+(?:\.\d+)?", raw.rstrip("!DRFSIL@#"))
    return float(m.group(0)) if m and "." in m.group(0) else (int(m.group(0)) if m else None)


def _bool(raw):
    return raw.strip().lower() == "true"


def _enum(raw):
    return raw.strip().split(".")[-1].rstrip(")")


def _flags(raw):
    """CType((A.Top Or A.Left), AnchorStyles) -> ["Top", "Left"]"""
    inner = raw.split(",")[0] if raw.strip().startswith("CType") else raw
    return [p.strip().split(".")[-1].strip("()") for p in re.split(r"\bOr\b", inner)]


def _color(raw):
    raw = raw.strip()
    if "SystemColors" in raw or raw.endswith("Color.Empty") or raw.endswith("Color.Transparent"):
        return None
    if "FromArgb" in raw:
        nums = _ints(raw.split("FromArgb", 1)[1])
        nums = [n for n in nums if n not in (0,) or True]
        vals = [n for n in nums if 0 <= n <= 255]
        # CType(CType(255, Byte), Integer) repeats each number once - keep the ones that matter
        vals = re.findall(r"CType\(CType\((\d+), Byte\), Integer\)", raw) or vals
        vals = [int(v) for v in vals]
        if len(vals) >= 3:
            r, g, b = vals[-3:]
            return f"#{r:02x}{g:02x}{b:02x}"
        if len(vals) == 1:            # FromArgb(int argb)
            v = vals[0] & 0xFFFFFF
            return f"#{v:06x}"
        return None
    name = raw.split(".")[-1]
    c = QColor(name.lower())
    return c.name() if c.isValid() else None


def _font(raw, keep_size=False):
    m = re.search(r'Font\("([^"]+)",\s*([\d.]+)!?(.*)', raw)
    if not m:
        return None
    family, size, rest = m.group(1), float(m.group(2)), m.group(3)
    d = {}
    if family.lower() not in WINDOWS_FONTS:
        d["family"] = family
    # Windows' 8.25pt default looks right as the ambient font; keep explicit other sizes
    if keep_size or (abs(size - 8.25) > 0.1 and abs(size - 9.0) > 0.1):
        d["size"] = max(6, round(size))
    for style, key in (("Bold", "bold"), ("Italic", "italic"), ("Underline", "underline"), ("Strikeout", "strikeout")):
        if f"FontStyle.{style}" in rest:
            d[key] = True
    return d or None


def _shortcut(raw):
    keys = []
    for k in _flags(raw):
        k = KEY_NAMES.get(k, k)
        if re.fullmatch(r"D\d", k):
            k = k[1]
        keys.append(k)
    mods = [k for k in keys if k in ("Ctrl", "Shift", "Alt")]
    rest = [k for k in keys if k not in ("Ctrl", "Shift", "Alt")]
    return "+".join(mods + rest)


def _filter(raw):
    """WinForms 'Text files|*.txt|All files|*.*' -> Qt 'Text files (*.txt);;All files (*.*)'"""
    s = _string(raw) or ""
    parts = s.split("|")
    out = []
    for i in range(0, len(parts) - 1, 2):
        name, pats = parts[i].strip(), parts[i + 1].replace(";", " ").strip()
        out.append(name if pats in name else f"{name} ({pats})")
    return ";;".join(out) or "All files (*)"


def _qt_date_format(fmt):
    return (fmt or "").replace("tt", "AP").replace("dddd", "dddd").replace("hh", "hh")


# ============================================================ Designer.vb parser
class VbForm:
    def __init__(self, name):
        self.name = name
        self.types = {}          # control name -> VB type (short)
        self.props = {}          # name -> {prop: raw}
        self.form = {}           # Me.<prop>
        self.parent = {}         # child -> (parent, panel index or None)
        self.order = []          # Controls.Add order
        self.items = {}          # name -> [strings]  (Items.AddRange with text)
        self.children = {}       # name -> [names]    (menu DropDownItems / Items with Me.X)
        self.columns = {}        # grid/listview -> [column names]
        self.tooltips = {}
        self.tree_nodes = {}     # local TreeNode var -> (text, [child vars])
        self.nodes = {}          # TreeView -> [node vars]
        self.lv_items = {}       # ListView -> [[texts]]
        self.lv_vars = {}
        self.resources = {}      # name.Prop -> resource key


def _braced_names(raw):
    m = re.search(r"\{(.*)\}", raw)
    return [x.strip().replace("Me.", "") for x in m.group(1).split(",")] if m else []


def join_continuations(text):
    """VB's line continuation: a line ending in ' _' continues on the next line."""
    return re.sub(r"[ \t]+_[ \t]*\r?\n[ \t]*", " ", text)


def parse_designer(text, class_name=None):
    text = join_continuations(text)
    m = re.search(r"Sub InitializeComponent\(\)(.*?)End Sub", text, re.S | re.I)
    body = m.group(1) if m else ""
    if not class_name:
        cm = re.search(r"Partial (?:Class|Public Class)\s+(\w+)", text) or re.search(r"Class\s+(\w+)", text)
        class_name = cm.group(1) if cm else "Form1"
    f = VbForm(class_name)
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("'"):
            continue
        mm = re.match(r"Dim (\w+) As [\w.]+ = New [\w.]*TreeNode\((.*)\)$", line)
        if mm:
            args = mm.group(2)
            f.tree_nodes[mm.group(1)] = (_string(args.split(",")[0]) or "", _braced_names(args))
            continue
        mm = re.match(r"Dim (\w+) As [\w.]+ = New [\w.]*ListViewItem\((.*)\)$", line)
        if mm:
            f.lv_vars[mm.group(1)] = [p.replace('""', '"') for p in re.findall(r'"((?:[^"]|"")*)"', mm.group(2))]
            continue
        mm = re.match(r"Me\.(\w+) = New ([\w.]+)\((.*)\)$", line)
        if mm and (mm.group(1) == "components" or mm.group(2).split(".")[-1] in VALUE_TYPES):
            if mm.group(1) != "components":
                f.form[mm.group(1)] = line.split(" = ", 1)[1]
            continue
        if mm:
            f.types[mm.group(1)] = mm.group(2).split(".")[-1]
            f.props.setdefault(mm.group(1), {})
            continue
        mm = re.match(r"Me\.(\w+)\.(Panel[12])\.Controls\.Add\(Me\.(\w+)\)", line)
        if mm:
            f.parent[mm.group(3)] = (mm.group(1), 0 if mm.group(2) == "Panel1" else 1)
            continue
        mm = re.match(r"Me\.(\w+)\.Controls\.Add\(Me\.(\w+)\)", line)
        if mm:
            f.parent[mm.group(2)] = (mm.group(1), None)
            continue
        mm = re.match(r"Me\.Controls\.Add\(Me\.(\w+)\)", line)
        if mm:
            f.order.append(mm.group(1))
            continue
        mm = re.match(r"Me\.(\w+)\.(Items|DropDownItems)\.AddRange\((.*)\)$", line)
        if mm:
            if "Me." in mm.group(3):
                f.children.setdefault(mm.group(1), []).extend(_braced_names(mm.group(3)))
            elif "ListViewItem" in mm.group(3):
                f.lv_items.setdefault(mm.group(1), []).extend(_braced_names(mm.group(3)))
            else:
                f.items.setdefault(mm.group(1), []).extend(
                    p.replace('""', '"') for p in re.findall(r'"((?:[^"]|"")*)"', mm.group(3)))
            continue
        mm = re.match(r"Me\.(\w+)\.Items\.Add\((.*)\)$", line)
        if mm and _string(mm.group(2)) is not None:
            f.items.setdefault(mm.group(1), []).append(_string(mm.group(2)))
            continue
        mm = re.match(r"Me\.(\w+)\.Columns\.AddRange\((.*)\)$", line)
        if mm:
            f.columns.setdefault(mm.group(1), []).extend(_braced_names(mm.group(2)))
            continue
        mm = re.match(r"Me\.(\w+)\.Nodes\.AddRange\((.*)\)$", line)
        if mm:
            f.nodes.setdefault(mm.group(1), []).extend(_braced_names(mm.group(2)))
            continue
        mm = re.match(r"Me\.(\w+)\.SetToolTip\(Me\.(\w+), (.*)\)$", line)
        if mm:
            f.tooltips[mm.group(2)] = _string(mm.group(3)) or ""
            continue
        mm = re.match(r"Me\.(\w+)\.(\w+) = (.*)$", line)
        if mm and mm.group(1) in f.types:
            val = mm.group(3)
            rm = re.search(r'resources\.GetObject\("([^"]+)"\)', val)
            if rm:
                f.resources[f"{mm.group(1)}.{mm.group(2)}"] = rm.group(1)
            f.props[mm.group(1)][mm.group(2)] = val
            continue
        mm = re.match(r"Me\.(\w+) = (.*)$", line)
        if mm:
            val = mm.group(2)
            rm = re.search(r'resources\.GetObject\("([^"]+)"\)', val)
            if rm:
                f.resources[f"$this.{mm.group(1)}"] = rm.group(1)
            f.form[mm.group(1)] = val
    return f


# ============================================================ .resx pictures
def resx_images(resx_path):
    """{key: (bytes, extension)} - pictures embedded in a form's .resx file."""
    out = {}
    try:
        root = ET.parse(resx_path).getroot()
    except (OSError, ET.ParseError):
        return out
    for d in root.findall("data"):
        key, val = d.get("name"), d.findtext("value") or ""
        if not key or "base64" not in (d.get("mimetype") or "") and "Bitmap" not in (d.get("type") or "") \
                and "Icon" not in (d.get("type") or ""):
            continue
        try:
            raw = base64.b64decode("".join(val.split()))
        except ValueError:
            continue
        for sig, ext in ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"),
                         (b"\x00\x00\x01\x00", ".ico"), (b"BM", ".bmp")):
            i = raw.find(sig)
            if i >= 0 and (ext != ".bmp" or i > 0):
                out[key] = (raw[i:], ext)
                break
    return out


# ============================================================ project
def parse_vbproj(path):
    folder = os.path.dirname(os.path.abspath(path))
    text = open(path, encoding="utf-8-sig", errors="replace").read()
    name = (re.search(r"<AssemblyName>([^<]+)</AssemblyName>", text) or
            re.search(r"<RootNamespace>([^<]+)</RootNamespace>", text))
    name = name.group(1) if name else os.path.splitext(os.path.basename(path))[0]
    vb_files = [m.replace("\\", os.sep) for m in re.findall(r'<Compile Include="([^"]+\.vb)"', text)]
    if not vb_files or "Microsoft.NET.Sdk" in text:       # SDK-style projects list no files
        vb_files = []
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d.lower() not in ("bin", "obj", ".vs")]
            vb_files += [os.path.relpath(os.path.join(dirpath, f), folder) for f in files if f.endswith(".vb")]
    forms = []
    for rel in vb_files:
        if rel.endswith(".Designer.vb") and not os.path.basename(rel).startswith(("Application.", "Resources.",
                                                                                   "Settings.")):
            code = rel[:-len(".Designer.vb")] + ".vb"
            full = os.path.join(folder, rel)
            try:
                t = open(full, encoding="utf-8-sig", errors="replace").read()
            except OSError:
                continue
            if "InitializeComponent" in t and ("Inherits System.Windows.Forms.Form" in t or
                                               "Inherits Form" in t or "Inherits System.Windows.Forms.UserControl" in t
                                               or "Inherits UserControl" in t):
                forms.append((rel, code if os.path.exists(os.path.join(folder, code)) else None))
    startup = None
    for cand in ("My Project/Application.myapp", "My Project\\Application.myapp"):
        p = os.path.join(folder, cand.replace("\\", os.sep).replace("/", os.sep))
        if os.path.exists(p):
            m = re.search(r"<MainForm>(\w+)</MainForm>", open(p, encoding="utf-8-sig", errors="replace").read())
            startup = m.group(1) if m else None
    m = re.search(r"<StartupObject>[\w.]*?(\w+)</StartupObject>", text)
    if not startup and m and m.group(1) != "Sub Main":
        startup = m.group(1)
    if not startup:                  # Sub Main (Program.vb / a Module): Application.Run(New MainForm())
        startup = _startup_from_main(folder, vb_files)
    settings = os.path.join(folder, "My Project", "Settings.settings")
    return {"name": name, "folder": folder, "vb_files": vb_files, "forms": forms, "startup": startup,
            "settings": settings if os.path.exists(settings) else None}


SETTING_TYPES = {"System.String": "String", "System.Int32": "Integer", "System.Int64": "Integer",
                 "System.Int16": "Integer", "System.Boolean": "Boolean", "System.Decimal": "Decimal",
                 "System.Double": "Decimal", "System.Single": "Decimal", "System.DateTime": "Date",
                 "System.Drawing.Color": "Color", "System.Collections.Specialized.StringCollection": "StringList"}


def parse_settings(path):
    out = []
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return out
    for s in root.iter():
        if not s.tag.endswith("Setting"):
            continue
        name, typ, scope = s.get("Name"), s.get("Type", "System.String"), s.get("Scope", "User")
        if not name or not name.isidentifier():
            continue
        v = next((x for x in s if x.tag.endswith("Value")), None)
        value = (v.text or "") if v is not None else ""
        t = SETTING_TYPES.get(typ, "String")
        if t == "StringList":
            value = re.findall(r"<string>([^<]*)</string>", value)
        elif t == "Color":
            value = _color("Color." + value) or "#000000" if value and "," not in value else "#000000"
        elif t == "Boolean":
            value = "True" if value.strip().lower() == "true" else "False"
        out.append({"name": name, "type": t, "scope": "Application" if scope == "Application" else "User",
                    "value": value})
    return out


# ============================================================ building the PyStudio form
class FormBuilder:
    def __init__(self, designer, vb, images_dir, resx_imgs, log):
        self.d, self.vb, self.images_dir, self.resx, self.log = designer, vb, images_dir, resx_imgs, log
        self.widgets = {}
        self.menu_names = set()
        self.toolbar_names = set()

    def specs(self, w):
        return {sp.name: sp for sp in P.control_specs(self.d, w)}

    def setp(self, w, specs, name, value):
        sp = specs.get(name)
        if sp is None or value is None:
            return False
        try:
            sp.set(value)
            return True
        except Exception as e:  # noqa: BLE001 - keep importing; report it
            self.log.append(f"  {w.objectName()}.{name}: couldn't set ({e})")
            return False

    def _save_image(self, key, base):
        img = self.resx.get(key)
        if not img:
            return None
        os.makedirs(self.images_dir, exist_ok=True)
        path = os.path.join(self.images_dir, base + img[1])
        with open(path, "wb") as fh:
            fh.write(img[0])
        return os.path.relpath(path, self.d.project_dir).replace(os.sep, "/")

    def build(self):
        vb, d = self.vb, self.d
        # strips
        for name, t in vb.types.items():
            if t == "MenuStrip":
                d.menu = self._menu_items(vb.children.get(name, []))
            elif t == "StatusStrip":
                d.status = True
            elif t == "ToolStrip" and d.toolbar is None:
                d.toolbar = {"name": name, "style": "ImageAndText", "items": self._tool_items(vb.children.get(name, []))}
        # WinForms positions include the menu/tool strips at the top; PyStudio's surface starts below them
        self.top_offset = 0
        for name, t in vb.types.items():
            if t in ("MenuStrip", "ToolStrip") and _enum(vb.props.get(name, {}).get("Dock", "Top")) == "Top":
                size = vb.props.get(name, {}).get("Size")
                self.top_offset += (_ints(size) + [0, 24 if t == "MenuStrip" else 25])[1] if size else \
                    (24 if t == "MenuStrip" else 25)
        # controls, parents first
        # WinForms docks the control added LAST first, and draws the first-added one on top. PyStudio docks and
        # draws in creation order, so create in reverse Controls.Add order (stacked Dock=Top labels then come out
        # in the same order as in Visual Studio).
        roots = [n for n in reversed(vb.order) if n in vb.types] + \
            [n for n in vb.types if n not in vb.parent and n not in vb.order]
        for n in roots:
            self._create(n, d.surface)
        self._components()
        self._form()
        # tab order from TabIndex
        # TabIndex is per container in WinForms: walk containers in order
        def ordered(names):
            return sorted(names, key=lambda n: _number(vb.props.get(n, {}).get("TabIndex", "9999")) or 0)

        def kids_of(n):
            direct = [c for c, (par, _i) in vb.parent.items() if par == n]
            out = []
            for c in direct:
                out += kids_of(c) if vb.types.get(c) == "TabPage" else [c]
            return out
        order = []

        def walk(names):
            for n in ordered(names):
                w = self.widgets.get(n)
                if w is not None and w._ps.get("tool") in M.FOCUSABLE:
                    order.append(n)
                walk(kids_of(n))
        walk([n for n in roots if n in self.widgets])
        d.tab_order = order
        d._update_strips()
        d.apply_docks_all()
        d._update_tray()

    # ---------------------------------------------------------- menus/toolbars
    def _menu_items(self, names):
        out = []
        for n in names:
            t, p = self.vb.types.get(n, ""), self.vb.props.get(n, {})
            if t == "ToolStripSeparator":
                out.append({"sep": True})
                continue
            if t not in ("ToolStripMenuItem", ""):
                self.log.append(f"  menu item {n} ({t}) was skipped - only menu items are supported in menus")
                continue
            it = {"name": n, "text": _string(p.get("Text", '""')) or n, "items": self._menu_items(self.vb.children.get(n, []))}
            if "ShortcutKeys" in p:
                it["shortcut"] = _shortcut(p["ShortcutKeys"])
            if "Checked" in p and _bool(p["Checked"]) or "CheckOnClick" in p and _bool(p["CheckOnClick"]):
                it["checkable"] = True
                it["checked"] = "Checked" in p and _bool(p["Checked"])
            if "Enabled" in p and not _bool(p["Enabled"]):
                it["enabled"] = False
            if "ToolTipText" in p:
                it["tooltip"] = _string(p["ToolTipText"]) or ""
            self.menu_names.add(n)
            out.append(it)
        return out

    def _tool_items(self, names):
        out = []
        for n in names:
            t, p = self.vb.types.get(n, ""), self.vb.props.get(n, {})
            if t == "ToolStripSeparator":
                out.append({"sep": True})
            elif t in ("ToolStripButton", "ToolStripMenuItem", "ToolStripDropDownButton", "ToolStripSplitButton"):
                it = {"name": n, "text": _string(p.get("Text", '""')) or n}
                if "ToolTipText" in p:
                    it["tooltip"] = _string(p["ToolTipText"]) or ""
                img = self._save_image(self.vb.resources.get(f"{n}.Image", ""), n)
                if img:
                    it["icon"] = img
                self.toolbar_names.add(n)
                out.append(it)
            else:
                self.log.append(f"  toolbar item {n} ({t}) was skipped")
        return out

    # ---------------------------------------------------------- controls
    def _create(self, name, parent):
        vb = self.vb
        t = vb.types.get(name)
        p = vb.props.get(name, {})
        if t in COMPONENT_MAP or t in SKIPPED_TYPES or t in STRIP_ITEMS or t in ("MenuStrip", "StatusStrip",
                                                                                  "ToolStrip"):
            return
        tool = TYPE_MAP.get(t)
        if t == "TextBox" and _bool(p.get("Multiline", "False")):
            tool = "TextArea"
        if tool is None:
            tool = "Panel"
            self.log.append(f"  {name}: {t} isn't supported - placed as a Panel")
        x, y = (_ints(p["Location"]) + [0, 0])[:2] if "Location" in p else (0, 0)
        w_, h_ = (_ints(p["Size"]) + [100, 30])[:2] if "Size" in p else M.TOOLS[tool][2]
        if tool == "Label" and "Size" not in p:
            w_, h_ = 60, 20
        if parent is self.d.surface:
            y = max(0, y - self.top_offset)
        w = M.make_widget(tool, parent, empty=False)
        w.setObjectName(name)
        if tool == "DataGridView":
            M.set_columns(w, [])
        w.setGeometry(QRect(x, y, max(1, w_), max(1, h_)))
        M.apply_visuals(w, self.d.project_dir)
        w.show()
        self.d.controls.append(w)
        self.widgets[name] = w
        self._props(w, tool, t, p, name)
        if tool in ("CheckBox", "RadioButton", "Label", "LinkLabel") and w.sizeHint().width() > w.width():
            w.resize(w.sizeHint().width(), w.height())     # Linux fonts run wider than Windows' 8.25pt
        # children: reverse Controls.Add order (see roots in build) - WinForms docks the last-added first
        kids = [c for c, (par, _i) in vb.parent.items() if par == name][::-1]
        if tool == "TabControl":
            pages = [c for c in kids[::-1] if vb.types.get(c) == "TabPage"]     # tabs keep Controls.Add order
            if pages:
                self.setp(w, self.specs(w), "TabPages", [_string(vb.props.get(pg, {}).get("Text", '""')) or pg
                                                          for pg in pages])
                for i, (pg, panel) in enumerate(zip(pages, M.panels_of(w))):
                    panel.setObjectName(pg)
                    panel.resize(max(1, w.width() - 4), max(1, w.height() - 26))
                    for c in [c for c, (par, _i) in vb.parent.items() if par == pg][::-1]:
                        self._create(c, panel)
            return
        if tool == "SplitContainer":
            panels = M.panels_of(w)
            for c in kids:
                idx = vb.parent[c][1] or 0
                self._create(c, panels[min(idx, len(panels) - 1)])
            return
        if tool in M.CONTAINERS:
            for c in kids:
                self._create(c, w)
        elif kids:
            self.log.append(f"  {name}: {len(kids)} control(s) inside a {t} were skipped")

    def _props(self, w, tool, vbtype, p, name):
        specs = self.specs(w)
        if "Padding" in p and tool in M.CONTAINERS and hasattr(w, "_ps"):
            v = _ints(p["Padding"])
            v = v * 4 if len(v) == 1 else v
            if len(v) == 4 and any(v):
                w._ps["padding"] = ",".join(str(x) for x in v)
        s = lambda prop, val: self.setp(w, specs, prop, val)  # noqa: E731
        if "Text" in p and "Text" in specs:
            s("Text", _string(p["Text"]) or "")
        if "Font" in p:
            s("Font", _font(p["Font"]))
        for vbp in ("BackColor", "ForeColor"):
            if vbp in p:
                c = _color(p[vbp])
                if c:
                    s(vbp, c)
        if "Enabled" in p:
            s("Enabled", _bool(p["Enabled"]))
        if "Visible" in p:
            s("Visible", _bool(p["Visible"]))
        if "Anchor" in p:
            sides = [x for x in ("Top", "Bottom", "Left", "Right") if x in _flags(p["Anchor"])]
            s("Anchor", ", ".join(sides) if sides else "None")
        if "Dock" in p:
            s("Dock", _enum(p["Dock"]))
        if name in self.vb.tooltips:
            s("ToolTip", self.vb.tooltips[name])
        for vbp in ("ReadOnly", "Checked", "AutoSize", "Sorted", "WordWrap"):
            if vbp in p and vbp in specs:
                s(vbp, _bool(p[vbp]))
        if "CheckState" in p and "Checked" in specs:
            s("Checked", _enum(p["CheckState"]) != "Unchecked")
        for vbp in ("Minimum", "Maximum", "Value", "Increment", "SmallChange", "LargeChange", "MaxLength",
                    "TickFrequency"):
            if vbp in p and vbp in specs:
                v = _number(p[vbp])
                if v is not None:
                    s(vbp, int(v))
        if name in self.vb.items and "Items" in specs:
            s("Items", self.vb.items[name])
        if "DropDownStyle" in p and "DropDownStyle" in specs:
            s("DropDownStyle", _enum(p["DropDownStyle"]))
        if ("PasswordChar" in p or "UseSystemPasswordChar" in p) and "UseSystemPasswordChar" in specs:
            s("UseSystemPasswordChar", True if "PasswordChar" in p else _bool(p["UseSystemPasswordChar"]))
        if tool == "Label" and not _bool(p.get("AutoSize", "False")):
            w.setWordWrap(True)                # a fixed-size WinForms Label wraps its text
        if tool == "Label" and "TextAlign" not in p and "TextAlign" in specs:
            s("TextAlign", "TopLeft")          # VB's Label default (PyStudio's own labels default to MiddleLeft)
        if "TextAlign" in p and "TextAlign" in specs:
            v = _enum(p["TextAlign"])
            opts = specs["TextAlign"].opts or []
            s("TextAlign", v if v in opts else next((o for o in opts if o.endswith(v)), None))
        if "BorderStyle" in p and "BorderStyle" in specs:
            s("BorderStyle", _enum(p["BorderStyle"]))
        if "DialogResult" in p:
            s("DialogResult", _enum(p["DialogResult"]))
        if "SizeMode" in p and "SizeMode" in specs:
            v = _enum(p["SizeMode"])
            s("SizeMode", {"Zoom": "StretchImage", "AutoSize": "Normal"}.get(v, v))
        if "Image" in p and "Image" in specs:
            img = self._save_image(self.vb.resources.get(f"{name}.Image", ""), name)
            if img:
                s("Image", img)
            else:
                self.log.append(f"  {name}.Image: picture comes from My.Resources - set Image again in Properties")
        if "Mask" in p and "Mask" in specs:
            s("Mask", _string(p["Mask"]))
        if vbtype in ("DateTimePicker", "MonthCalendar"):
            if "Format" in p:
                fmt = _enum(p["Format"])
                s("Format", fmt if fmt in ("Short", "Long", "Time", "Custom") else "Short")
            if "CustomFormat" in p and _enum(p.get("Format", "")) == "Custom":
                s("CustomFormat", _qt_date_format(_string(p["CustomFormat"])))
            if "ShowUpDown" in p and _bool(p["ShowUpDown"]):
                w.setCalendarPopup(False)
        if vbtype == "LinkLabel" and "Text" in p:
            s("Text", _string(p["Text"]))
        if vbtype in ("HScrollBar", "VScrollBar") or ("Orientation" in p and tool in ("TrackBar", "SplitContainer")):
            if "Orientation" in specs:
                v = "Vertical" if vbtype == "VScrollBar" else _enum(p.get("Orientation", "Horizontal"))
                s("Orientation", v)
        if tool == "SplitContainer" and "SplitterDistance" in p:
            s("SplitterDistance", int(_number(p["SplitterDistance"]) or 50))
        if tool == "DataGridView":
            cols = [_string(self.vb.props.get(c, {}).get("HeaderText", '""')) or c for c in self.vb.columns.get(name, [])]
            s("Columns", cols)
            for vbp, prop in (("AllowUserToAddRows", "AllowUserToAddRows"), ("AllowUserToDeleteRows",
                                                                            "AllowUserToDeleteRows"),
                              ("RowHeadersVisible", "RowHeadersVisible")):
                if vbp in p:
                    s(prop, _bool(p[vbp]))
            if "SelectionMode" in p:
                s("SelectionMode", "FullRowSelect" if "FullRowSelect" in p["SelectionMode"] else "CellSelect")
            if "AlternatingRowsDefaultCellStyle" in p:
                s("AlternatingRowColors", True)
        if tool == "ListView":
            cols = [_string(self.vb.props.get(c, {}).get("Text", '""')) or c for c in self.vb.columns.get(name, [])]
            if cols:
                s("Columns", cols)
            rows = [self.vb.lv_vars.get(v, []) for v in self.vb.lv_items.get(name, [])]
            if rows:
                s("Items", [" | ".join(r) for r in rows])
        if tool == "TreeView":
            lines = []

            def walk(var, depth):
                text, kids = self.vb.tree_nodes.get(var, (var, []))
                lines.append("    " * depth + text)
                for k in kids:
                    walk(k, depth + 1)
            for var in self.vb.nodes.get(name, []):
                walk(var, 0)
            s("Nodes", "\n".join(lines))
        if tool == "GroupBox" and "Text" in p:
            s("Text", _string(p["Text"]) or "")
        if "Cursor" in p and "Cursor" in specs:
            v = _enum(p["Cursor"])
            if v in (specs["Cursor"].opts or []):
                s("Cursor", v)
        if "Tag" in p and "Tag" in specs:
            s("Tag", _string(p["Tag"]) or "")
        if "ContextMenuStrip" in p:                  # set directly: the menu component is made after controls
            w._ps["contextmenu"] = p["ContextMenuStrip"].replace("Me.", "").strip()

    # ---------------------------------------------------------- components / form
    def _components(self):
        for name, t in self.vb.types.items():
            ct = COMPONENT_MAP.get(t)
            if ct is None:
                if t in SKIPPED_TYPES and t != "ToolTip":
                    self.log.append(f"  {name} ({t}) was skipped - {SKIPPED_TYPES[t]}")
                continue
            p = self.vb.props.get(name, {})
            props = dict(M.COMPONENTS[ct][1])
            if ct == "Timer":
                props["interval"] = int(_number(p.get("Interval", "100")) or 100)
                props["enabled"] = _bool(p.get("Enabled", "False"))
            elif ct in ("OpenFileDialog", "SaveFileDialog"):
                if "Filter" in p:
                    props["filter"] = _filter(p["Filter"])
                for vbp, key in (("Title", "title"), ("FileName", "fileName"), ("InitialDirectory", "initialDirectory")):
                    if vbp in p:
                        props[key] = _string(p[vbp]) or ""
                if ct == "OpenFileDialog" and "Multiselect" in p:
                    props["multiselect"] = _bool(p["Multiselect"])
            elif ct == "FolderBrowserDialog":
                for vbp, key in (("Description", "description"), ("SelectedPath", "selectedPath")):
                    if vbp in p:
                        props[key] = _string(p[vbp]) or ""
            elif ct == "ColorDialog" and "Color" in p:
                props["color"] = _color(p["Color"]) or "#000000"
            elif ct == "PrintDocument" and "DocumentName" in p:
                props["documentName"] = _string(p["DocumentName"]) or "document"
            elif ct in ("PrintDialog", "PrintPreviewDialog", "PageSetupDialog") and "Document" in p:
                props["document"] = p["Document"].replace("Me.", "").strip()
            elif ct == "ImageList":
                self.log.append(f"  {name}: ImageList pictures live in the .resx - add them again (Images)")
            elif ct == "ContextMenuStrip":
                props["items"] = self._menu_items(self.vb.children.get(name, []))
            elif ct == "NotifyIcon":
                for vbp, key in (("Text", "text"), ("BalloonTipTitle", "balloonTitle"), ("BalloonTipText", "balloonText")):
                    if vbp in p:
                        props[key] = _string(p[vbp]) or ""
                if "Visible" in p:
                    props["visible"] = _bool(p["Visible"])
                if "ContextMenuStrip" in p:
                    props["menu"] = p["ContextMenuStrip"].replace("Me.", "").strip()
                img = self._save_image(self.vb.resources.get(f"{name}.Icon", ""), name)
                if img:
                    props["icon"] = img
            elif ct == "BindingSource":
                self.log.append(f"  {name}: BindingSource - set its DataSource to a saved query "
                                "(VB's was a DataSet/table adapter)")
            self.d.components.append({"type": ct, "name": name, "props": props})

    def _form(self):
        f, fp, d = self.d.form, self.vb.form, self.d
        if "Text" in fp:
            f["title"] = _string(fp["Text"]) or self.vb.name
        size = fp.get("ClientSize") or fp.get("Size")
        if size:
            w_, h_ = (_ints(size) + [800, 450])[:2]
            d.set_form_size(w_, h_)
        if "StartPosition" in fp:
            v = _enum(fp["StartPosition"])
            f["startpos"] = v if v in ("CenterScreen", "CenterParent") else "WindowsDefaultLocation"
        if "FormBorderStyle" in fp:
            v = _enum(fp["FormBorderStyle"])
            f["border"] = {"FixedToolWindow": "FixedSingle", "SizableToolWindow": "Sizable"}.get(v, v)
        for vbp, key in (("MaximizeBox", "maxbox"), ("MinimizeBox", "minbox"), ("TopMost", "topmost")):
            if vbp in fp:
                f[key] = _bool(fp[vbp])
        if "WindowState" in fp:
            f["state"] = _enum(fp["WindowState"])
        for vbp, key in (("BackColor", "back"), ("ForeColor", "fore")):
            if vbp in fp:
                f[key] = _color(fp[vbp])
        if "Font" in fp:
            # the form's own font keeps its size: the layout was measured for it (a Windows font
            # family is left to the desktop's font, which has nearly the same size at the same points)
            f["font"] = _font(fp["Font"], keep_size=True)
        if "AutoScaleDimensions" in fp and _enum(fp.get("AutoScaleMode", "Font")) == "Font":
            m = re.search(r"SizeF\(\s*([\d.]+)!?\s*,\s*([\d.]+)", fp["AutoScaleDimensions"])
            if m:
                f["autoscale"] = f"{float(m.group(1)):g},{float(m.group(2)):g}"
        for vbp, key in (("AcceptButton", "accept"), ("CancelButton", "cancel")):
            if vbp in fp:
                f[key] = fp[vbp].replace("Me.", "").strip()
        if "ContextMenuStrip" in fp:
            f["contextmenu"] = fp["ContextMenuStrip"].replace("Me.", "").strip()
        if "Icon" in fp:
            img = self._save_image(self.vb.resources.get("$this.Icon", "$this.Icon"), self.vb.name + "_icon")
            if img:
                f["icon"] = img
        if "Opacity" in fp:
            v = _number(fp["Opacity"])
            if v:
                f["opacity"] = int(v * 100) if v <= 1 else int(v)


# ============================================================ code-behind (Form1.vb)
def parse_subs(vb_code):
    """[(kind, name, params, handles, body_lines)] for Subs/Functions in a VB file."""
    out = []
    pat = re.compile(r"^\s*(?:(?:Private|Public|Protected|Friend|Shared|Overrides|Overloads|Async)\s+)*"
                     r"(Sub|Function)\s+(\w+)\s*\(([^)]*)\)(?:\s+As\s+[\w.()]+)?(?:\s+Handles\s+(.+))?\s*$", re.I)
    lines = join_continuations(vb_code).splitlines()
    i = 0
    while i < len(lines):
        m = pat.match(lines[i])
        if not m:
            i += 1
            continue
        kind, name, params, handles = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        body = []
        i += 1
        end = re.compile(rf"^\s*End\s+{kind}\b", re.I)
        while i < len(lines) and not end.match(lines[i]):
            body.append(lines[i])
            i += 1
        i += 1
        out.append((kind, name, params, [h.strip() for h in handles.split(",") if h.strip()], body))
    return out


def _dedent(lines):
    lines = [ln.rstrip() for ln in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    ind = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
    return [ln[ind:] for ln in lines]


def _py_params(params):
    names = []
    for p in params.split(","):
        p = re.sub(r"\b(ByVal|ByRef|Optional|ParamArray)\b", "", p).strip()
        m = re.match(r"(\w+)", p)
        if m:
            n = m.group(1)
            names.append(n + "_" if n in ("lambda", "print", "type", "id", "from", "class", "def") else n)
    return names


def _startup_from_main(folder, rel_files):
    """The form a Sub Main starts: Application.Run(New X()) or Dim f As New X ... Application.Run(f)."""
    for rel in rel_files:
        try:
            t = open(os.path.join(folder, rel.replace("\\", os.sep)), encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        if "Application.Run" not in t:
            continue
        m = re.search(r"Application\.Run\(\s*New\s+([\w.]+)", t)
        if m:
            return m.group(1).split(".")[-1]
        m = re.search(r"Application\.Run\(\s*(\w+)\s*\)", t)
        if m:
            v = re.search(rf"\b{m.group(1)}\s+As\s+New\s+(\w+)|\b{m.group(1)}\s*=\s*New\s+(\w+)", t)
            if v:
                return v.group(1) or v.group(2)
    return None


def pick_startup(info, classes):
    """The form the app opens first. <StartupObject> can name a form, but also "Sub Main" or a Program class /
    Module (PrismPVR.Program): then it's the form that code creates - Application.Run(New MainForm()), or
    Dim/Using f As New MainForm ... Application.Run(f) - qualified names (New UI.MainForm) included."""
    st = info.get("startup")
    if st in classes:
        return st
    folder = info.get("folder", "")
    run_files = []
    for rel in info.get("vb_files", []) + info.get("cs_files", []):
        try:
            t = open(os.path.join(folder, rel.replace("\\", os.sep)), encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        if "Application.Run" in t:
            # the Program file named as the startup object first
            run_files.insert(0 if st and re.search(rf"\b(Module|Class|class)\s+{re.escape(st)}\b", t) else
                             len(run_files), t)
    for t in run_files:
        m = re.search(r"Application\.Run\(\s*(?:New|new)\s+([\w.]+)", t)
        if m and m.group(1).split(".")[-1] in classes:
            return m.group(1).split(".")[-1]
        m = re.search(r"Application\.Run\(\s*(\w+)\s*\)", t)
        if m:
            v = re.search(rf"\b{m.group(1)}\s+As\s+New\s+([\w.]+)|\b{m.group(1)}\s*=\s*(?:New|new)\s+([\w.]+)", t)
            if v and (v.group(1) or v.group(2)).split(".")[-1] in classes:
                return (v.group(1) or v.group(2)).split(".")[-1]
        made = [n.split(".")[-1] for n in re.findall(r"\b(?:New|new)\s+([\w.]+)\s*\(", t)]
        made = [n for n in made if n in classes]
        if made:
            return made[-1]           # the form Sub Main creates last is the one it runs
    for name in ("MainForm", "FormMain", "frmMain", "Main", "Form1"):
        if name in classes:
            return name
    return classes[0] if classes else None


# Form events with no signal in Qt: the form's matching method (closeEvent...) gets the VB code
FORM_OVERRIDES = {
    "FormClosing": ("closeEvent", "e.Cancel = True  ->  event.ignore(); return"),
    "FormClosed": ("closeEvent", ""),
    "Resize": ("resizeEvent", "Me.Width / Me.Height  ->  self.width() / self.height()"),
    "SizeChanged": ("resizeEvent", ""),
    "KeyDown": ("keyPressEvent", "e.KeyCode = Keys.F5  ->  event.key() == Qt.Key_F5;  e.Control  ->  "
                "event.modifiers() & Qt.ControlModifier"),
    "KeyUp": ("keyReleaseEvent", "e.KeyCode  ->  event.key()"),
    "KeyPress": ("keyPressEvent", "e.KeyChar  ->  event.text()"),
    "Paint": ("paintEvent", "e.Graphics  ->  painter = QPainter(self)"),
    "MouseDown": ("mousePressEvent", "e.X, e.Y  ->  event.position().x(), event.position().y()"),
    "MouseUp": ("mouseReleaseEvent", ""),
    "MouseMove": ("mouseMoveEvent", ""),
}


def convert_code(py_source, vb_code, builder, log):
    """Wire handlers for 'Handles X.Event' subs (VB body as comments); other Subs/Functions become
    methods with the VB code as comments. Returns the new Python source."""
    return convert_subs(py_source, parse_subs(vb_code), builder, log, "VB")


def convert_subs(py_source, subs, builder, log, lang="VB"):
    """subs: [(kind, name, params, handles, body_lines)] from VB (parse_subs) or C# (csimport)."""
    how = "Handles" if lang == "VB" else "event"
    src = py_source
    tool_of = {n: w._ps.get("tool") for n, w in builder.widgets.items()}
    comp_of = {c["name"]: c["type"] for c in builder.d.components}
    load_bodies = []
    overrides = {}                     # closeEvent -> [(sub name, handles, vb lines)]
    extra = []
    for kind, name, params, handles, body in subs:
        vb_text = _dedent(body)
        wired = False
        for h in handles:
            ctrl, _, event = h.partition(".")
            if ctrl in ("Me", "MyBase") and event in ("Load", "Shown"):
                load_bodies.append((name, vb_text))
                wired = True
                continue
            if ctrl in ("Me", "MyBase", "this", "base") and event in FORM_OVERRIDES:
                overrides.setdefault(FORM_OVERRIDES[event][0], []).append((name, h, event, vb_text))
                wired = True
                continue
            if ctrl in builder.menu_names:
                tool = "menu"
            elif ctrl in builder.toolbar_names:
                tool = "toolbar"
            else:
                tool = tool_of.get(ctrl) or comp_of.get(ctrl)
            signal = EVENT_MAP.get((tool, event))
            if signal is None:
                log.append(f"  {name}: {how} {h} - no matching Qt event; kept as a method to wire up yourself")
                continue
            arg = ""
            for sig, a in M.EVENTS.get(tool, []):
                if sig == signal:
                    arg = a
            try:
                src, line = codegen.add_event_handler(src, ctrl, signal, arg)
            except ValueError as e:
                log.append(f"  {name}: {e}")
                continue
            hname = codegen.handler_name(ctrl, signal)
            src = _fill_body(src, hname, [f"{lang} ({name}, {how} {h}):"] + vb_text)
            wired = True
        if not wired and name != "InitializeComponent":
            extra.append((kind, name, params, vb_text))
    if load_bodies:
        lines = [f"# Form Load ({lang}: " + ", ".join(n for n, _b in load_bodies) + ") - runs when the form opens"]
        for n, b in load_bodies:
            lines += [f"{lang} {n}:"] + b
        src = src.replace("        # Event wiring", "        self.form_load()\n\n        # Event wiring", 1)
        src = src.rstrip() + "\n\n    def form_load(self):\n" + "".join(
            f"        # {ln}\n" if ln else "        #\n" for ln in lines[1:]) + "        pass\n"
    for method, items in overrides.items():
        lines = []
        for n, h, event, b in items:
            hint = FORM_OVERRIDES[event][1]
            lines += [f"{lang} {n} ({how} {h}):"] + ([f"  (in Python: {hint})"] if hint else []) + b + [""]
        src = src.rstrip() + f"\n\n    def {method}(self, event):\n" + "".join(
            f"        # {ln}\n" if ln else "        #\n" for ln in lines[:-1]) + \
            f"        super().{method}(event)\n"
        log.append(f"  {', '.join(n for n, *_r in items)}: form event -> {method} (the VB code is inside)")
    for kind, name, params, vb_text in extra:
        args = ", ".join(["self"] + _py_params(params))
        src = src.rstrip() + f"\n\n    def {name}({args}):\n" + "".join(
            f"        # {ln}\n" if ln else "        #\n" for ln in [(f"{lang} {kind} {name}({params}):" if lang == "VB"
                                                          else f"{lang} {name}({params}):")] + vb_text) + \
            "        pass\n"
    return src if src.endswith("\n") else src + "\n"


def convert_module(vb_code, rel):
    """A VB Module/Class file -> Python: Module Subs/Functions become functions, a Class becomes a class."""
    cm = re.search(r"^\s*(?:Public |Friend )?(Module|Class)\s+(\w+)", vb_code, re.M)
    kind, name = (cm.group(1), cm.group(2)) if cm else ("Module", "Module1")
    out = [f'"""{name} - imported from {rel} (VB {kind}). Translate the VB code in the comments."""', ""]
    subs = parse_subs(vb_code)
    ind = ""
    if kind == "Class":
        out += ["", f"class {name}:"]
        ind = "    "
        if not subs:
            out.append("    pass")
    for k, n, params, _h, body in subs:
        args = _py_params(params)
        if kind == "Class":
            args = ["self"] + args
        out += ["", f"{ind}def {n}({', '.join(args)}):"]
        out += [f"{ind}    # {ln}" if ln else f"{ind}    #" for ln in [f"VB {k} {n}({params}):"] + _dedent(body)]
        out.append(f"{ind}    pass")
    if not subs and kind != "Class":
        out += [f"# {ln}" for ln in vb_code.splitlines()]
    return "\n".join(out) + "\n"


def _fill_body(src, hname, comment_lines):
    m = re.search(rf"^(\s*)def {re.escape(hname)}\(.*\):\n(\s*)pass\n?", src, re.M)
    if not m:
        return src
    ind = m.group(2)
    block = "".join(f"{ind}# {ln}\n" if ln else f"{ind}#\n" for ln in comment_lines) + f"{ind}pass\n"
    return src[:m.start()] + src[m.start():m.end()].rsplit(f"{ind}pass", 1)[0] + block + src[m.end():]


# ============================================================ whole project
def import_project(vbproj, dest_root, project_name=None):
    """Create a PyStudio project at dest_root from a .vbproj. Returns (files_to_open, report_lines)."""
    from .designer import FormDesigner
    from .project import Project
    from .templates import FORM_PY, FORM_UI, MAIN_FORMS, _write
    cs = vbproj.lower().endswith(".csproj")
    if cs:
        from . import csimport
        info = csimport.parse_csproj(vbproj)
    else:
        info = parse_vbproj(vbproj)
    lang = "C#" if cs else "VB"
    dsuffix, ext = (".Designer.cs", ".cs") if cs else (".Designer.vb", ".vb")
    name = project_name or re.sub(r"[^A-Za-z0-9_-]", "", info["name"]) or "ImportedApp"
    os.makedirs(dest_root)
    proj = Project(dest_root)
    proj["name"] = name
    proj["external"] = False
    proj["description"] = f"Imported from {os.path.basename(vbproj)}"
    report = [f"Imported {vbproj}", f"into {dest_root}", ""]
    if info["settings"]:
        settings = parse_settings(info["settings"])
        if settings:
            proj["settings"] = settings
            report.append(f"{'Properties.Settings' if cs else 'My.Settings'}: {len(settings)} setting(s) -> "
                          f"Project Properties > Settings (use my.settings.Name in code)")
    proj.save()
    proj.ensure_runtime()
    # originals for reference
    orig = os.path.join(dest_root, "cs_original" if cs else "vb_original")
    for rel in info["vb_files"] + [r[:-3] + ".resx" for r in info["vb_files"]]:
        src = os.path.join(info["folder"], rel)
        if os.path.exists(src):
            dst = os.path.join(orig, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    opened, classes = [], []
    for rel, code_rel in info["forms"]:
        dtext = open(os.path.join(info["folder"], rel), encoding="utf-8-sig", errors="replace").read()
        events = []
        code_text = open(os.path.join(info["folder"], code_rel), encoding="utf-8-sig",
                         errors="replace").read() if code_rel else ""
        if cs:
            lines, events, cs_cls = csimport.designer_to_vb(dtext)
            if lines is None:
                report.append(f"{rel}: skipped - no InitializeComponent")
                continue
            dtext = csimport.build_vb_designer(lines, cs_cls, csimport.is_user_control(code_text))
        vb = parse_designer(dtext)
        cls = vb.name
        stem = cls.lower()
        ui_path = os.path.join(dest_root, stem + ".ui")
        py_path = os.path.join(dest_root, stem + ".py")
        if os.path.exists(ui_path):
            report.append(f"{cls}: skipped - {stem}.ui already exists")
            continue
        is_uc = "UserControl" in dtext.split("InitializeComponent")[0]
        _write(ui_path, FORM_UI.format(cls=cls))
        d = FormDesigner(ui_path)
        if is_uc:
            d.form["kind"] = "UserControl"
        log = []
        resx = os.path.join(info["folder"], rel[:-len(dsuffix)] + ".resx")
        b = FormBuilder(d, vb, os.path.join(dest_root, "images"), resx_images(resx), log)
        b.build()
        d.save(ui_path)
        from .qttools import compile_ui
        compile_ui(ui_path)
        if is_uc:
            from .templates import USERCONTROL_PY
            py = USERCONTROL_PY.format(cls=cls, stem=stem, project=name, inst=cls[0].lower() + cls[1:] + "1")
        else:
            py = FORM_PY.format(cls=cls, stem=stem)
        if code_rel:
            if cs:
                subs = csimport.parse_methods(code_text, cls, events)
            else:
                subs = parse_subs(code_text)
            py = convert_subs(py, subs, b, log, lang)
        _write(py_path, py)
        d.deleteLater()
        n_ctrl = len(b.widgets)
        report.append(f"{cls}: {n_ctrl} control(s), {len(d.components)} component(s)" +
                      (", menu" if d.menu else "") + (", toolbar" if d.toolbar else "") +
                      (f", {len(log)} note(s):" if log else ""))
        report += log
        classes.append(cls)
        opened += [ui_path, py_path]
    startup = pick_startup(info, classes)
    main_py = os.path.join(dest_root, "main.py")
    if startup:
        _write(main_py, MAIN_FORMS.format(name=name).replace("from form1 import Form1",
                                                               f"from {startup.lower()} import {startup}")
               .replace("form = Form1()", f"form = {startup}()"))
        report.append(f"Startup form: {startup}")
    else:
        _write(main_py, f'"""{name} - imported from {"C#" if cs else "VB.NET"} (no forms found)."""\n')
    other = [r for r in info["vb_files"] if not r.endswith(dsuffix) and not r.startswith(("My Project", "Properties"))
             and r[:-len(ext)] + dsuffix not in [f for f, _c in info["forms"]]
             and os.path.basename(r).lower() not in ("program.cs", "assemblyinfo.cs")]
    if other:
        report += ["", f"Other {lang} code (modules/classes) - Python files with the {lang} code as comments:"]
        for r in other:
            stem = re.sub(r"\W", "_", os.path.splitext(os.path.basename(r))[0]).lower()
            target = os.path.join(dest_root, stem + ".py")
            if os.path.exists(target):
                report.append(f"  {r}: skipped - {stem}.py already exists")
                continue
            code = open(os.path.join(info["folder"], r), encoding="utf-8-sig", errors="replace").read()
            _write(target, csimport.convert_module(code, r) if cs else convert_module(code, r))
            report.append(f"  {r} -> {stem}.py")
    report += ["", f"Event handlers are wired up; the {lang} code is inside each handler as comments.",
               "Translate it to Python (self.ui.Label1.setText(...) instead of Label1.Text = ...), or use "
               "the AI Assistant's Translate button."]
    _write(os.path.join(dest_root, "IMPORT_REPORT.txt"), "\n".join(report) + "\n")
    return [p for p in opened if p.endswith(".ui")][:1] + [p for p in opened if p.endswith(".py")][:1], report
