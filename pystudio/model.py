"""Form model: controls, their properties, and .ui XML read/write.

Every control is a real Qt widget on the design surface. Extra designer data
(colors, anchor, tag, ...) lives in widget._ps (a dict). Files are standard
Qt .ui XML so pyside6-uic can generate ui_<form>.py from them.
"""
import copy
import os
import re
import xml.etree.ElementTree as ET

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDateTimeEdit, QFrame,
                               QGroupBox, QLabel, QLineEdit, QListWidget, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QSlider, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QWidget)

GRID = 8
MENU_H = 24
TOOL_H = 30
STATUS_H = 22

# Toolbox entry -> (Qt class, VS-style name prefix, default size)
TOOLS = {
    "Button":         ("QPushButton",    "button",         (90, 30)),
    "Label":          ("QLabel",         "label",          (60, 20)),
    "TextBox":        ("QLineEdit",      "textBox",        (140, 26)),
    "TextArea":       ("QPlainTextEdit", "textArea",       (200, 100)),
    "CheckBox":       ("QCheckBox",      "checkBox",       (110, 22)),
    "RadioButton":    ("QRadioButton",   "radioButton",    (110, 22)),
    "ComboBox":       ("QComboBox",      "comboBox",       (140, 26)),
    "ListBox":        ("QListWidget",    "listBox",        (140, 110)),
    "NumericUpDown":  ("QSpinBox",       "numericUpDown",  (90, 26)),
    "DateTimePicker": ("QDateTimeEdit",  "dateTimePicker", (140, 26)),
    "TrackBar":       ("QSlider",        "trackBar",       (150, 26)),
    "ProgressBar":    ("QProgressBar",   "progressBar",    (150, 24)),
    "PictureBox":     ("QLabel",         "pictureBox",     (120, 100)),
    "DataGridView":   ("QTableWidget",   "dataGridView",   (320, 160)),
    "BindingNavigator": ("QFrame",       "bindingNavigator", (320, 27)),
    "Chart":          ("PsChart",        "chart",          (360, 240)),
    "WebBrowser":     ("PsWebBrowser",   "webBrowser",     (420, 300)),
    "GroupBox":       ("QGroupBox",      "groupBox",       (200, 120)),
    "Panel":          ("QFrame",         "panel",          (200, 120)),
    "LinkLabel":      ("QLabel",         "linkLabel",      (80, 20)),
    "MaskedTextBox":  ("QLineEdit",      "maskedTextBox",  (140, 26)),
    "TreeView":       ("QTreeWidget",    "treeView",       (180, 200)),
    "ListView":       ("QTreeWidget",    "listView",       (300, 160)),
    "TabControl":     ("QTabWidget",     "tabControl",     (320, 220)),
    "SplitContainer": ("QSplitter",      "splitContainer", (320, 220)),
}
# tools that share a Qt class with another tool: saved with a psType marker
PSTYPE_TOOLS = {"PictureBox", "LinkLabel", "MaskedTextBox", "ListView", "BindingNavigator"}
BINDABLE = {"DataGridView", "ListView", "ComboBox", "ListBox", "Chart"}   # DataSource property
# Classes from pystudio_runtime (uic imports them via <customwidgets>)
CUSTOM_HEADERS = {"PsChart": "pystudio_runtime.h", "PsWebBrowser": "pystudio_runtime.h"}
# The open project's UserControls: class name -> file stem (form .ui / .py); filled by
# register_user_controls() so they work like built-in tools (Toolbox, naming, save/load)
USER_CONTROLS = {}
DIALOG_RESULTS = ["None", "OK", "Cancel", "Yes", "No", "Abort", "Retry", "Ignore"]
# controls with a DataBinding property (VB: DataBindings) - shows/edits a column of the current row
VALUE_BINDABLE = {"TextBox", "MaskedTextBox", "TextArea", "Label", "CheckBox", "NumericUpDown",
                  "DateTimePicker", "ComboBox", "TrackBar", "ProgressBar"}
SPECIAL_TOOLS = ["MenuStrip", "ToolStrip", "StatusStrip"]   # form-level strips

# Non-visual components (shown in the tray under the form, like VS):
# type -> (name prefix, default properties)
COMPONENTS = {
    "Timer": ("timer", {"enabled": False, "interval": 100}),
    "OpenFileDialog": ("openFileDialog", {"title": "", "filter": "All files (*)",
                                          "initialDirectory": "", "multiselect": False, "fileName": ""}),
    "SaveFileDialog": ("saveFileDialog", {"title": "", "filter": "All files (*)",
                                          "initialDirectory": "", "fileName": ""}),
    "FolderBrowserDialog": ("folderBrowserDialog", {"description": "", "selectedPath": ""}),
    "ColorDialog": ("colorDialog", {"color": "#000000"}),
    "FontDialog": ("fontDialog", {}),
    "ImageList": ("imageList", {"images": [], "imageSize": 16}),
    "PrintDocument": ("printDocument", {"documentName": "document", "landscape": False}),
    "PrintDialog": ("printDialog", {"document": ""}),
    "PrintPreviewDialog": ("printPreviewDialog", {"document": "", "title": "Print Preview"}),
    "PageSetupDialog": ("pageSetupDialog", {"document": ""}),
    "ContextMenuStrip": ("contextMenuStrip", {"items": []}),
    "NotifyIcon": ("notifyIcon", {"text": "", "icon": "", "visible": True, "menu": "",
                                  "balloonTitle": "", "balloonText": ""}),
    "ErrorProvider": ("errorProvider", {"icon": ""}),
    "BindingSource": ("bindingSource", {"dataSource": "", "filter": "", "sort": ""}),
}
COMPONENT_ICONS = {"Timer": "⏱", "OpenFileDialog": "📂", "SaveFileDialog": "💾",
                   "FolderBrowserDialog": "🗀", "ColorDialog": "🎨", "FontDialog": "A",
                   "ImageList": "🖼", "PrintDocument": "📄", "PrintDialog": "🖨", "PrintPreviewDialog": "🔍",
                   "PageSetupDialog": "📐", "ContextMenuStrip": "☰", "NotifyIcon": "🔔", "ErrorProvider": "❗",
                   "BindingSource": "⛁"}
# Controls that take part in Tab order
FOCUSABLE = {"Button", "TextBox", "TextArea", "CheckBox", "RadioButton", "ComboBox", "ListBox",
             "NumericUpDown", "DateTimePicker", "TrackBar", "DataGridView", "MaskedTextBox",
             "TreeView", "ListView", "TabControl", "LinkLabel"}
CONTAINERS = {"GroupBox", "Panel", "TabControl", "SplitContainer"}
PANELED = {"TabControl", "SplitContainer"}     # containers whose children live on pages/panels
DOCKS = ["None", "Top", "Bottom", "Left", "Right", "Fill"]
TOOL_GROUPS = [
    ("Common Controls", ["Button", "CheckBox", "ComboBox", "DateTimePicker", "Label", "LinkLabel",
                         "ListBox", "ListView", "MaskedTextBox", "NumericUpDown", "PictureBox",
                         "ProgressBar", "RadioButton", "TextArea", "TextBox", "TrackBar", "TreeView",
                         "WebBrowser"]),
    ("Containers", ["GroupBox", "Panel", "SplitContainer", "TabControl"]),
    ("Data", ["BindingNavigator", "BindingSource", "Chart", "DataGridView"]),
    ("Menus & Toolbars", SPECIAL_TOOLS + ["ContextMenuStrip"]),
    ("Components", ["ErrorProvider", "ImageList", "NotifyIcon", "Timer"]),
    ("Dialogs", ["ColorDialog", "FolderBrowserDialog", "FontDialog", "OpenFileDialog", "SaveFileDialog"]),
    ("Printing", ["PageSetupDialog", "PrintDialog", "PrintDocument", "PrintPreviewDialog"]),
]
CLASS_TO_TOOL = {cls: tool for tool, (cls, _p, _s) in TOOLS.items() if tool not in PSTYPE_TOOLS}
CLASS_TO_TOOL["QDateEdit"] = "DateTimePicker"     # forms saved before v2.11 (date-only box)
_BUILTIN_TOOLS = set(TOOLS)

# Events per control (Events tab): (signal, handler argument list)
EVENTS = {
    "Button": [("clicked", ""), ("pressed", ""), ("released", "")],
    "CheckBox": [("toggled", "checked"), ("stateChanged", "state"), ("clicked", "")],
    "RadioButton": [("toggled", "checked"), ("clicked", "")],
    "TextBox": [("textChanged", "text"), ("textEdited", "text"), ("returnPressed", ""),
                ("editingFinished", "")],
    "TextArea": [("textChanged", "")],
    "ComboBox": [("currentIndexChanged", "index"), ("currentTextChanged", "text"),
                 ("activated", "index")],
    "ListBox": [("currentRowChanged", "row"), ("currentTextChanged", "text"),
                ("itemClicked", "item"), ("itemDoubleClicked", "item")],
    "NumericUpDown": [("valueChanged", "value"), ("editingFinished", "")],
    "DateTimePicker": [("dateTimeChanged", "value"), ("dateChanged", "date"), ("editingFinished", "")],
    "TrackBar": [("valueChanged", "value"), ("sliderMoved", "position"), ("sliderReleased", "")],
    "ProgressBar": [("valueChanged", "value")],
    "Label": [("linkActivated", "link")],
    "PictureBox": [],
    "DataGridView": [("cellClicked", "row, column"), ("cellDoubleClicked", "row, column"),
                     ("cellPressed", "row, column"),
                     ("cellChanged", "row, column"), ("itemSelectionChanged", "")],
    "GroupBox": [("toggled", "checked")],
    "Panel": [],
    "Timer": [("timeout", "")],
    "LinkLabel": [("linkActivated", "link")],
    "MaskedTextBox": [("textChanged", "text"), ("returnPressed", ""), ("editingFinished", "")],
    "TreeView": [("currentItemChanged", "current, previous"), ("itemClicked", "item, column"),
                 ("itemDoubleClicked", "item, column"), ("itemExpanded", "item")],
    "ListView": [("itemSelectionChanged", ""), ("itemClicked", "item, column"),
                 ("itemDoubleClicked", "item, column"), ("currentItemChanged", "current, previous")],
    "TabControl": [("currentChanged", "index"), ("tabBarClicked", "index")],
    "SplitContainer": [("splitterMoved", "pos, index")],
    "ImageList": [],
    "BindingNavigator": [],
    "Chart": [],
    "WebBrowser": [("documentCompleted", "url"), ("navigated", "url"), ("titleChanged", "title"),
                   ("loadProgress", "progress")],
    "PrintDocument": [("printPage", "e"), ("beginPrint", "document"), ("endPrint", "document")],
    "PrintDialog": [], "PrintPreviewDialog": [], "PageSetupDialog": [],
    "ContextMenuStrip": [("triggered", "action"), ("aboutToShow", "")],
    "NotifyIcon": [("doubleClicked", ""), ("clicked", ""), ("messageClicked", ""), ("activated", "reason")],
    "ErrorProvider": [],
    "BindingSource": [("positionChanged", "position"), ("listChanged", "")],
}
NO_DEFAULT_EVENT = {"Label", "PictureBox", "GroupBox", "Panel", "ProgressBar", "SplitContainer", "ImageList",
                    "BindingNavigator", "Chart", "PrintDialog", "PrintPreviewDialog", "PageSetupDialog",
                    "ErrorProvider"}


def register_user_controls(project_dir):
    """Find the project's UserControls (.ui files marked formKind=UserControl) and make them
    tools. Returns the sorted class names."""
    import glob
    USER_CONTROLS.clear()
    for path in sorted(glob.glob(os.path.join(project_dir or "", "*.ui"))):
        try:
            top = ET.parse(path).getroot().find("widget")
        except (ET.ParseError, OSError):
            continue
        if top is None or read_props(top).get("formKind") != "UserControl":
            continue
        cls = top.get("name") or ""
        stem = os.path.splitext(os.path.basename(path))[0]
        if not cls.isidentifier() or not os.path.exists(os.path.join(project_dir, stem + ".py")):
            continue
        g = read_props(top).get("geometry", (0, 0, 300, 150))
        USER_CONTROLS[cls] = stem
        TOOLS[cls] = (cls, cls[0].lower() + cls[1:], (g[2], g[3]))
        CLASS_TO_TOOL[cls] = cls
        EVENTS.setdefault(cls, [])
        NO_DEFAULT_EVENT.add(cls)
    for cls in [t for t, v in list(TOOLS.items()) if v[0] == t and t not in USER_CONTROLS
                and t not in CUSTOM_HEADERS and t[0].isupper() and t not in _BUILTIN_TOOLS]:
        TOOLS.pop(cls, None)
        CLASS_TO_TOOL.pop(cls, None)
    return sorted(USER_CONTROLS)


def usercontrol_pixmap(cls, project_dir, _depth=[0]):
    """What a UserControl looks like (for its copies on a form in the designer)."""
    stem = USER_CONTROLS.get(cls)
    path = os.path.join(project_dir or "", f"{stem}.ui")
    if not stem or not os.path.exists(path) or _depth[0] > 2:
        return None
    _depth[0] += 1
    try:
        top = ET.parse(path).getroot().find("widget")
        p = read_props(top)
        g = p.get("geometry", (0, 0, 300, 150))
        host = QWidget()
        host.setObjectName("centralwidget")
        host.setAttribute(Qt.WA_DontShowOnScreen)
        host.resize(g[2], g[3])
        css = p.get("styleSheet", "")
        host.setStyleSheet(SURFACE_CSS + "\n#centralwidget { background: #f0f0f0; }\n" + css)
        central = next((e for e in top.findall("widget") if e.get("name") == "centralwidget"), None)
        for el in (central.findall("widget") if central is not None else []):
            for c in control_from_xml(el, host, project_dir):
                apply_visuals(c, project_dir)
        host.show()
        pm = host.grab()
        host.hide()
        host.deleteLater()
        return pm
    except Exception:  # noqa: BLE001 - a broken user control still shows as a box
        return None
    finally:
        _depth[0] -= 1


def default_event(tool):
    if tool in NO_DEFAULT_EVENT or not EVENTS.get(tool):
        return None
    return EVENTS[tool][0]


TAB_POSITIONS = {"Top": "North", "Bottom": "South", "Left": "West", "Right": "East"}

TEXT_PROP = {"QPushButton": "text", "QLabel": "text", "QCheckBox": "text",
             "QRadioButton": "text", "QLineEdit": "text", "QGroupBox": "title",
             "QPlainTextEdit": "plainText"}

CURSORS = {"Default": "ArrowCursor", "Hand": "PointingHandCursor", "IBeam": "IBeamCursor",
           "Wait": "WaitCursor", "Cross": "CrossCursor", "SizeAll": "SizeAllCursor",
           "No": "ForbiddenCursor", "Help": "WhatsThisCursor"}

ALIGN = {  # VS ContentAlignment -> (horizontal, vertical)
    "TopLeft": ("AlignLeft", "AlignTop"), "TopCenter": ("AlignHCenter", "AlignTop"),
    "TopRight": ("AlignRight", "AlignTop"), "MiddleLeft": ("AlignLeft", "AlignVCenter"),
    "MiddleCenter": ("AlignHCenter", "AlignVCenter"), "MiddleRight": ("AlignRight", "AlignVCenter"),
    "BottomLeft": ("AlignLeft", "AlignBottom"), "BottomCenter": ("AlignHCenter", "AlignBottom"),
    "BottomRight": ("AlignRight", "AlignBottom"),
}
HALIGN = {"Left": "AlignLeft", "Center": "AlignHCenter", "Right": "AlignRight"}

BORDER_STYLES = ["None", "FixedSingle", "Fixed3D"]
ANCHOR_DEFAULT = "Top, Left"

SURFACE_CSS = """
QLineEdit, QPlainTextEdit, QListWidget, QTableWidget, QDateEdit, QDateTimeEdit, QSpinBox, QTreeView, QTreeWidget
    { background: #ffffff; color: #000000; }
QLabel, QCheckBox, QRadioButton, QGroupBox { color: #000000; }
QTreeView::item:selected, QListWidget::item:selected { background: #cce4f7; color: #000000; }
QHeaderView { background: #ffffff; }
QTableCornerButton::section { background: #f3f3f3; border: 0; }
QHeaderView::section { background: #f3f3f3; color: #000000; border: 0;
    border-right: 1px solid #d5d5d5; border-bottom: 1px solid #d5d5d5; padding: 2px 4px; }
QTabWidget::pane { border: 1px solid #c4c4c4; background: #ffffff; top: -1px; }
QTabBar::tab { background: #ececec; color: #000000; padding: 4px 12px; border: 1px solid #c4c4c4;
    border-bottom: 0; margin-right: 1px; }
QTabBar::tab:selected { background: #ffffff; }
QSplitter::handle { background: #e0e0e0; }
"""


# ============================================================ small helpers
def qt_align(name):
    h, v = ALIGN.get(name, ALIGN["MiddleLeft"])
    return getattr(Qt, h) | getattr(Qt, v)


def align_name(flags):
    flags = Qt.AlignmentFlag(int(flags))
    for name, (h, v) in ALIGN.items():
        if (flags & getattr(Qt, h)) and (flags & getattr(Qt, v)):
            return name
    return "MiddleLeft"


def font_from_dict(d):
    """Partial QFont: only the set attributes override the parent's font (ambient, like WinForms)."""
    f = QFont()
    if not d:
        return f
    if d.get("family"):
        f.setFamily(d["family"])
    if d.get("size"):
        f.setPointSize(int(d["size"]))
    for key, setter in (("bold", f.setBold), ("italic", f.setItalic),
                        ("underline", f.setUnderline), ("strikeout", f.setStrikeOut)):
        if key in d:
            setter(bool(d[key]))
    return f


def font_from_qfont(f, base=None):
    """dict for a QFont chosen in QFontDialog (only what differs from base)."""
    base = base or QFont()
    d = {}
    if f.family() != base.family():
        d["family"] = f.family()
    if f.pointSize() > 0 and f.pointSize() != base.pointSize():
        d["size"] = f.pointSize()
    for key, val, bval in (("bold", f.bold(), base.bold()), ("italic", f.italic(), base.italic()),
                           ("underline", f.underline(), base.underline()),
                           ("strikeout", f.strikeOut(), base.strikeOut())):
        if val != bval:
            d[key] = val
    return d or None


def font_text(d):
    if not d:
        return "(inherited)"
    parts = [d.get("family") or "", f"{d['size']}pt" if d.get("size") else ""]
    parts += [k.capitalize() for k in ("bold", "italic", "underline", "strikeout") if d.get(k)]
    return ", ".join(p for p in parts if p) or "(inherited)"


# ============================================================ widget creation
def new_ps(tool):
    return {"tool": tool, "raw": None, "back": None, "fore": None, "css": "",
            "anchor": ANCHOR_DEFAULT, "tag": "", "locked": False, "padding": "",
            "autosize": False, "cursor": "Default", "image": "", "sizemode": "Normal",
            "border": "None", "font": None, "visible": True, "tabstop": True,
            "flat": False, "columns": [], "format": "Short", "dock": "None",
            "link": "", "linktext": "", "split": 0,
            "datasource": "", "displaymember": "", "valuemember": "", "autoload": True,
            "master": "", "binding": "", "bindsource": "", "allowadd": True, "allowdelete": True,
            "dialogresult": "None", "charttype": "Column", "charttitle": "", "legend": True,
            "xmember": "", "ymembers": "", "url": "", "contextmenu": ""}


def make_widget(tool, parent, empty=False):
    """empty=True: no default tab pages / split panels (they come from the .ui file)."""
    cls = TOOLS[tool][0]
    if tool == "TrackBar":
        w = QSlider(Qt.Horizontal, parent)
    elif tool == "TabControl":
        w = QTabWidget(parent)
        if not empty:
            for i in (1, 2):
                pg = QWidget()
                pg.setObjectName(f"tabPage{i}")
                w.addTab(pg, f"tabPage{i}")
    elif tool == "SplitContainer":
        w = QSplitter(Qt.Horizontal, parent)
        w.setChildrenCollapsible(False)
        if not empty:
            for i in (1, 2):
                pn = QWidget()
                pn.setObjectName(f"splitPanel{i}")
                w.addWidget(pn)
    elif tool in ("TreeView", "ListView"):
        w = QTreeWidget(parent)
        if tool == "TreeView":
            w.setHeaderHidden(True)
            w.setColumnCount(1)
        else:
            w.setRootIsDecorated(False)
            w.setColumnCount(1)
            w.setHeaderLabels(["Column1"])
    elif tool == "DateTimePicker":
        w = QDateTimeEdit(parent)          # date and/or time, by its format (VB's DateTimePicker)
        w.setCalendarPopup(True)
        w.setDate(QDate.currentDate())
    elif tool == "Chart":
        from .pschart import make_chart_class
        w = make_chart_class()(parent)
    elif tool == "WebBrowser" or tool in USER_CONTROLS:
        w = QLabel(parent)
        w.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    else:
        w = {"QPushButton": QPushButton, "QLabel": QLabel, "QLineEdit": QLineEdit,
             "QPlainTextEdit": QPlainTextEdit, "QCheckBox": QCheckBox,
             "QRadioButton": QRadioButton, "QComboBox": QComboBox,
             "QListWidget": QListWidget, "QSpinBox": QSpinBox,
             "QProgressBar": QProgressBar, "QGroupBox": QGroupBox, "QFrame": QFrame,
             "QTableWidget": QTableWidget}[cls](parent)
    w._ps = new_ps(tool)
    w._ps_cls = cls
    if tool in USER_CONTROLS:
        w._ps_cls = tool      # saved as <widget class="UserControl1">
    if tool == "BindingNavigator":
        _navigator_preview(w)
        if not empty:
            w._ps["dock"] = "Top"          # VS docks a new BindingNavigator to the top
    if tool == "ListView":
        w._ps["columns"] = ["Column1"]
    if tool == "ProgressBar":
        w.setValue(0)
    if tool == "DateTimePicker":
        w.setDisplayFormat("M/d/yyyy")
    _design_mode(w, parent)
    return w


def _navigator_preview(frame):
    """Design-time look of a BindingNavigator (the app builds the working buttons)."""
    from PySide6.QtWidgets import QHBoxLayout, QStyle, QToolButton
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(2, 1, 2, 1)
    lay.setSpacing(1)
    st = frame.style()
    for item in (QStyle.SP_MediaSkipBackward, QStyle.SP_ArrowLeft, "|", "0", "of {0}", "|",
                 QStyle.SP_ArrowRight, QStyle.SP_MediaSkipForward, "|", "+",
                 QStyle.SP_DialogDiscardButton, QStyle.SP_DialogSaveButton):
        if isinstance(item, str):
            lb = QLabel(item, frame)
            if item == "0":
                lb.setFixedWidth(34)
                lb.setAlignment(Qt.AlignCenter)
                lb.setStyleSheet("background: white; border: 1px solid #aaa;")
            elif item == "|":
                lb.setStyleSheet("color: #aaa;")
            lay.addWidget(lb)
        else:
            b = QToolButton(frame)
            b.setAutoRaise(True)
            if item == QStyle.SP_DialogSaveButton:
                from .icons import save as _save_icon
                b.setIcon(_save_icon())
            else:
                b.setIcon(st.standardIcon(item))
            lay.addWidget(b)
    lay.addStretch()
    frame.setFrameShape(QFrame.StyledPanel)


def _chart_preview(w):
    """Sample data so the designer shows what the chart type looks like."""
    ps = w._ps
    w.setProperty("chartType", ps.get("charttype") or "Column")
    w.setProperty("chartTitle", ps.get("charttitle") or "")
    w.setProperty("legendVisible", bool(ps.get("legend", True)))
    names = [s.strip() for s in (ps.get("ymembers") or "").split(",") if s.strip()] or ["Series1"]
    xs = ["A", "B", "C", "D", "E"]
    w._data = {n: [(x, (7 * (i + 2) + 11 * j * (k + 1)) % 23 + 4) for i, x in enumerate(xs)]
               for k, n in enumerate(names) for j in [k + 1]}
    w._rebuild()


def make_placeholder(raw, parent):
    """Unknown widget from another tool: shown as a box, saved back unchanged."""
    w = QLabel(f"{raw.get('name')}\n({raw.get('class')})", parent)
    w.setAlignment(Qt.AlignCenter)
    w.setStyleSheet("background: #e8e8e8; color: #555; border: 1px dashed #888;")
    w._ps = new_ps(None)
    w._ps["raw"] = raw
    w._ps_cls = raw.get("class")
    _design_mode(w, parent)
    return w


def panels_of(w):
    """Pages of a TabControl / panels of a SplitContainer (where their child controls live)."""
    t = tool_of(w)
    if t in PANELED:
        return [w.widget(i) for i in range(w.count())]
    return []


def interior_rect(w):
    """Area of a container that docked children fill (in the container's coordinates), less its Padding."""
    try:
        l, t_, r, b = [int(x) for x in str(getattr(w, "_ps", {}).get("padding") or "0,0,0,0").split(",")]
    except ValueError:
        l = t_ = r = b = 0
    return _interior_raw(w).adjusted(l, t_, -r, -b)


def _interior_raw(w):
    from PySide6.QtCore import QRect
    t = tool_of(w) if hasattr(w, "_ps") else None
    if t == "GroupBox":
        r = w.contentsRect()
        top = w.fontMetrics().height() + 2
        return QRect(r.x() + 2, r.y() + top, max(0, r.width() - 4), max(0, r.height() - top - 2))
    if t == "Panel":
        return w.contentsRect()
    return w.rect()


def apply_docks(container, children):
    """WinForms docking: in z-order, each docked control takes a strip of what's left;
    Fill takes the rest."""
    rem = interior_rect(container)
    fills = []
    for c in children:
        d = c._ps.get("dock", "None") if hasattr(c, "_ps") else "None"
        if d == "None":
            continue
        if d == "Fill":
            fills.append(c)
            continue
        h, w_ = c.height(), c.width()
        if d == "Top":
            c.setGeometry(rem.x(), rem.y(), rem.width(), h)
            rem.setTop(rem.top() + h)
        elif d == "Bottom":
            c.setGeometry(rem.x(), rem.bottom() - h + 1, rem.width(), h)
            rem.setBottom(rem.bottom() - h)
        elif d == "Left":
            c.setGeometry(rem.x(), rem.y(), w_, rem.height())
            rem.setLeft(rem.left() + w_)
        elif d == "Right":
            c.setGeometry(rem.right() - w_ + 1, rem.y(), w_, rem.height())
            rem.setRight(rem.right() - w_)
    for c in fills:
        c.setGeometry(rem)


def tree_to_text(w):
    lines = []

    def walk(item, depth):
        for i in range(item.childCount()):
            ch = item.child(i)
            lines.append("    " * depth + ch.text(0))
            walk(ch, depth + 1)
    walk(w.invisibleRootItem(), 0)
    return "\n".join(lines)


def set_tree_from_text(w, text):
    w.clear()
    stack = [(-1, w.invisibleRootItem())]
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        lead = raw[:len(raw) - len(raw.lstrip(" \t"))]
        depth = lead.count("\t") + lead.count(" ") // 4
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else w.invisibleRootItem()
        it = QTreeWidgetItem(parent, [raw.strip()])
        stack.append((depth, it))
    w.expandAll()


def listview_rows(w):
    rows = []
    for i in range(w.topLevelItemCount()):
        it = w.topLevelItem(i)
        rows.append(" | ".join(it.text(c) for c in range(w.columnCount())).rstrip(" |"))
    return rows


def set_listview_rows(w, rows):
    w.clear()
    for r in rows:
        QTreeWidgetItem(w, [c.strip() for c in r.split("|")])


def set_listview_columns(w, cols):
    w._ps["columns"] = list(cols) or ["Column1"]
    w.setColumnCount(len(w._ps["columns"]))
    w.setHeaderLabels(w._ps["columns"])


def _design_mode(w, parent):
    w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    w.setFocusPolicy(Qt.NoFocus)
    for child in w.findChildren(object):
        if hasattr(child, "setAttribute"):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    if parent is not None:
        w.setPalette(parent.palette())


def tool_of(w):
    return w._ps.get("tool")


def is_container(w):
    return tool_of(w) in CONTAINERS


# ============================================================ text / items
def get_text(w):
    t = tool_of(w)
    if t == "LinkLabel":
        return w._ps.get("linktext", "")
    prop = TEXT_PROP.get(w._ps_cls)
    if not prop or t == "PictureBox":
        return None
    if prop == "plainText":
        return w.toPlainText()
    if prop == "title":
        return w.title()
    return w.text()


def link_html(text, url):
    from html import escape
    return f'<a href="{escape(url or "#")}">{escape(text)}</a>'


def set_text(w, value):
    if tool_of(w) == "LinkLabel":
        w._ps["linktext"] = value
        w.setText(link_html(value, w._ps.get("link")))
        apply_autosize(w)
        return
    prop = TEXT_PROP.get(w._ps_cls)
    if prop == "plainText":
        w.setPlainText(value)
    elif prop == "title":
        w.setTitle(value)
    elif prop:
        w.setText(value)
    apply_autosize(w)


def get_items(w):
    if isinstance(w, QComboBox):
        return [w.itemText(i) for i in range(w.count())]
    return [w.item(i).text() for i in range(w.count())]


def set_items(w, items):
    w.clear()
    w.addItems(items)


def set_columns(w, cols):
    w._ps["columns"] = list(cols)
    w.setColumnCount(len(cols))
    w.setHorizontalHeaderLabels(cols)
    w.setRowCount(0)


# ============================================================ visuals
def control_css(w, name=None):
    """Style sheet saved to .ui (and used in the designer) for BackColor/ForeColor."""
    ps = w._ps
    name = name or w.objectName()
    rules = []
    if ps.get("back"):
        rules.append(f"background-color: {ps['back']};")
        if w._ps_cls == "QPushButton":   # colored buttons need a border to look like buttons
            rules.append("border: 1px solid #8f8f8f; border-radius: 3px; padding: 4px;")
    if ps.get("fore"):
        rules.append(f"color: {ps['fore']};")
    generated = f"#{name} {{ {' '.join(rules)} }}" if rules else ""
    user = (ps.get("css") or "").strip()
    if generated:
        return (user + "\n" if user else "") + "/* PyStudio */\n" + generated
    return user


def apply_visuals(w, project_dir=None):
    """Re-apply everything that affects how a control looks in the designer."""
    ps = w._ps
    if ps.get("raw") is not None:
        return
    w.setFont(font_from_dict(ps.get("font")))
    css = control_css(w)
    tool = ps["tool"]
    if tool == "Panel" and ps.get("border") == "None":
        pass  # overlay draws a dotted outline in the designer
    w.setStyleSheet(css)
    if tool == "Chart":
        _chart_preview(w)
    if tool == "WebBrowser":
        w.setText(f"  🌐  WebBrowser\n  {ps.get('url') or '(set Url, or call navigate() in code)'}")
        w.setStyleSheet(css + "QLabel { background: #ffffff; color: #555; border: 1px solid #aaa; }")
    if tool in USER_CONTROLS:
        pm = usercontrol_pixmap(tool, project_dir)
        if pm is not None:
            w.setPixmap(pm)
        else:
            w.setText(f"  {tool}")
        w.setStyleSheet(css + "QLabel { background: #f0f0f0; color: #555; border: 1px dashed #999; }")
    if tool in ("Label", "PictureBox", "Panel"):
        b = ps.get("border", "None")
        w.setFrameShape(QFrame.NoFrame if b == "None" else (QFrame.Box if b == "FixedSingle" else QFrame.Panel))
        w.setFrameShadow(QFrame.Sunken if b == "Fixed3D" else QFrame.Plain)
    if tool == "PictureBox":
        path = ps.get("image") or ""
        full = os.path.join(project_dir or "", path) if path and not os.path.isabs(path) else path
        if path and os.path.exists(full):
            w.setPixmap(QPixmap(full))
            w.setText("")
        else:
            w.setPixmap(QPixmap())
            w.setText("")
        mode = ps.get("sizemode", "Normal")
        w.setScaledContents(mode == "StretchImage")
        w.setAlignment(Qt.AlignCenter if mode == "CenterImage" else Qt.AlignLeft | Qt.AlignTop)
    if tool == "Button":
        w.setFlat(bool(ps.get("flat")))
    if tool == "SplitContainer" and ps.get("split"):
        total = (w.width() if w.orientation() == Qt.Horizontal else w.height()) - w.handleWidth()
        a = max(10, min(ps["split"], total - 10))
        w.setSizes([a, max(10, total - a)])
    apply_autosize(w)


def apply_autosize(w):
    if w._ps.get("autosize") and tool_of(w) in ("Label", "LinkLabel"):
        w.adjustSize()


# ============================================================ XML: low level
def _prop(parent, name, kind, value, stdset=True):
    attrs = {"name": name}
    if not stdset:
        attrs["stdset"] = "0"
    p = ET.SubElement(parent, "property", attrs)
    if kind == "rect":
        r = ET.SubElement(p, "rect")
        for k, v in zip(("x", "y", "width", "height"), value):
            ET.SubElement(r, k).text = str(int(v))
    elif kind == "size":
        s = ET.SubElement(p, "size")
        ET.SubElement(s, "width").text = str(int(value[0]))
        ET.SubElement(s, "height").text = str(int(value[1]))
    elif kind == "font":
        f = ET.SubElement(p, "font")
        if value.get("family"):
            ET.SubElement(f, "family").text = value["family"]
        if value.get("size"):
            ET.SubElement(f, "pointsize").text = str(int(value["size"]))
        for k in ("bold", "italic", "underline", "strikeout"):
            if k in value:
                ET.SubElement(f, k).text = "true" if value[k] else "false"
    elif kind == "bool":
        ET.SubElement(p, "bool").text = "true" if value else "false"
    elif kind == "number":
        ET.SubElement(p, "number").text = str(int(value))
    elif kind == "double":
        ET.SubElement(p, "double").text = str(float(value))
    elif kind == "enum":
        ET.SubElement(p, "enum").text = value
    elif kind == "set":
        ET.SubElement(p, "set").text = value
    elif kind == "cursor":
        ET.SubElement(p, "cursorShape").text = value
    elif kind == "pixmap":
        ET.SubElement(p, "pixmap").text = value
    elif kind == "iconset":
        i = ET.SubElement(p, "iconset")
        ET.SubElement(i, "normaloff").text = value
    else:
        ET.SubElement(p, "string").text = value
    return p


def read_props(el):
    props = {}
    for p in el.findall("property"):
        name = p.get("name")
        child = next(iter(p), None)
        if child is None:
            continue
        tag = child.tag
        if tag == "rect":
            props[name] = tuple(int(child.findtext(k, "0")) for k in ("x", "y", "width", "height"))
        elif tag == "size":
            props[name] = (int(child.findtext("width", "0")), int(child.findtext("height", "0")))
        elif tag == "font":
            d = {}
            if child.findtext("family"):
                d["family"] = child.findtext("family")
            if child.findtext("pointsize"):
                d["size"] = int(child.findtext("pointsize"))
            for k in ("bold", "italic", "underline", "strikeout"):
                if child.find(k) is not None:
                    d[k] = child.findtext(k) == "true"
            props[name] = d or None
        elif tag == "bool":
            props[name] = child.text == "true"
        elif tag == "number":
            props[name] = int(child.text or 0)
        elif tag == "double":
            props[name] = float(child.text or 0)
        elif tag == "iconset":
            props[name] = child.findtext("normaloff") or (child.text or "").strip()
        else:  # string, enum, set, cursorShape, pixmap
            props[name] = child.text or ""
    return props


def _enum_tail(v):
    return (v or "").split("::")[-1]


def split_css(css):
    """Return the user's part of a style sheet (drop PyStudio's generated block)."""
    return (css or "").split("/* PyStudio */")[0].strip()


# ============================================================ XML: controls
def control_to_xml(w):
    """Element for a control (and, for containers, its children)."""
    g = w.geometry()
    rect = (g.x(), g.y(), g.width(), g.height())
    ps = w._ps
    if ps.get("raw") is not None:
        raw = copy.deepcopy(ps["raw"])
        raw.set("name", w.objectName())
        for p in raw.findall("property"):
            if p.get("name") == "geometry":
                raw.remove(p)
        p = _prop(raw, "geometry", "rect", rect)
        raw.remove(p)
        raw.insert(0, p)
        return raw

    tool, cls = ps["tool"], w._ps_cls
    el = ET.Element("widget", {"class": cls, "name": w.objectName()})
    _prop(el, "geometry", "rect", rect)
    if ps.get("font"):
        _prop(el, "font", "font", ps["font"])
    if ps.get("cursor", "Default") != "Default":
        _prop(el, "cursor", "cursor", CURSORS[ps["cursor"]])
    if w.toolTip():
        _prop(el, "toolTip", "string", w.toolTip())
    if not w.isEnabled():
        _prop(el, "enabled", "bool", False)
    if not ps.get("visible", True):
        _prop(el, "visible", "bool", False)
    if not ps.get("tabstop", True):
        _prop(el, "focusPolicy", "enum", "Qt::FocusPolicy::NoFocus")
    css = control_css(w)
    if css:
        _prop(el, "styleSheet", "string", css)

    text_prop = TEXT_PROP.get(cls)
    if tool == "Button":
        _prop(el, "text", "string", w.text())
        if ps.get("flat"):
            _prop(el, "flat", "bool", True)
    elif tool == "Label":
        _prop(el, "text", "string", w.text())
        if align_name(w.alignment()) != "MiddleLeft":
            _prop(el, "alignment", "set", _align_set(align_name(w.alignment())))
        if w.wordWrap():
            _prop(el, "wordWrap", "bool", True)
    elif tool == "PictureBox":
        if ps.get("image"):
            _prop(el, "pixmap", "pixmap", ps["image"])
        if ps.get("sizemode") == "StretchImage":
            _prop(el, "scaledContents", "bool", True)
        if ps.get("sizemode") == "CenterImage":
            _prop(el, "alignment", "set", _align_set("MiddleCenter"))
    elif tool == "TextBox":
        if w.text():
            _prop(el, "text", "string", w.text())
        if align_name(w.alignment()) != "MiddleLeft":
            _prop(el, "alignment", "set", _align_set(align_name(w.alignment())))
        if w.maxLength() != 32767:
            _prop(el, "maxLength", "number", w.maxLength())
        if w.isReadOnly():
            _prop(el, "readOnly", "bool", True)
        if w.echoMode() == QLineEdit.Password:
            _prop(el, "echoMode", "enum", "QLineEdit::EchoMode::Password")
        if w.placeholderText():
            _prop(el, "placeholderText", "string", w.placeholderText())
    elif tool == "TextArea":
        if w.toPlainText():
            _prop(el, "plainText", "string", w.toPlainText())
        if w.isReadOnly():
            _prop(el, "readOnly", "bool", True)
        if w.placeholderText():
            _prop(el, "placeholderText", "string", w.placeholderText())
        if w.lineWrapMode() == QPlainTextEdit.NoWrap:
            _prop(el, "lineWrapMode", "enum", "QPlainTextEdit::LineWrapMode::NoWrap")
    elif tool in ("CheckBox", "RadioButton"):
        _prop(el, "text", "string", w.text())
        if w.isChecked():
            _prop(el, "checked", "bool", True)
        if tool == "CheckBox" and w.isTristate():
            _prop(el, "tristate", "bool", True)
    elif tool == "ComboBox":
        if w.isEditable():
            _prop(el, "editable", "bool", True)
        if w.maxVisibleItems() != 10:
            _prop(el, "maxVisibleItems", "number", w.maxVisibleItems())
    elif tool == "ListBox":
        mode = w.selectionMode()
        if mode == QAbstractItemView.MultiSelection:
            _prop(el, "selectionMode", "enum", "QAbstractItemView::SelectionMode::MultiSelection")
        elif mode == QAbstractItemView.ExtendedSelection:
            _prop(el, "selectionMode", "enum", "QAbstractItemView::SelectionMode::ExtendedSelection")
        if w.isSortingEnabled():
            _prop(el, "sortingEnabled", "bool", True)
    elif tool == "NumericUpDown":
        for n, v in (("minimum", w.minimum()), ("maximum", w.maximum()), ("value", w.value()),
                     ("singleStep", w.singleStep())):
            _prop(el, n, "number", v)
        if w.prefix():
            _prop(el, "prefix", "string", w.prefix())
        if w.suffix():
            _prop(el, "suffix", "string", w.suffix())
    elif tool == "DateTimePicker":
        _prop(el, "calendarPopup", "bool", w.calendarPopup())
        _prop(el, "displayFormat", "string", w.displayFormat())
    elif tool == "TrackBar":
        orient = "Horizontal" if w.orientation() == Qt.Horizontal else "Vertical"
        _prop(el, "orientation", "enum", f"Qt::Orientation::{orient}")
        for n, v in (("minimum", w.minimum()), ("maximum", w.maximum()), ("value", w.value()),
                     ("singleStep", w.singleStep()), ("pageStep", w.pageStep())):
            _prop(el, n, "number", v)
        tp = {QSlider.TicksAbove: "TicksAbove", QSlider.TicksBelow: "TicksBelow",
              QSlider.TicksBothSides: "TicksBothSides"}.get(w.tickPosition())
        if tp:
            _prop(el, "tickPosition", "enum", f"QSlider::TickPosition::{tp}")
        if w.tickInterval():
            _prop(el, "tickInterval", "number", w.tickInterval())
    elif tool == "ProgressBar":
        for n, v in (("minimum", w.minimum()), ("maximum", w.maximum()), ("value", w.value())):
            _prop(el, n, "number", v)
        if not w.isTextVisible():
            _prop(el, "textVisible", "bool", False)
    elif tool == "DataGridView":
        if w.alternatingRowColors():
            _prop(el, "alternatingRowColors", "bool", True)
        if w.editTriggers() == QAbstractItemView.NoEditTriggers:
            _prop(el, "editTriggers", "set", "QAbstractItemView::EditTrigger::NoEditTriggers")
        if w.selectionBehavior() == QAbstractItemView.SelectRows:
            _prop(el, "selectionBehavior", "enum", "QAbstractItemView::SelectionBehavior::SelectRows")
        if w.verticalHeader().isHidden():
            a = ET.SubElement(el, "attribute", name="verticalHeaderVisible")
            ET.SubElement(a, "bool").text = "false"
    elif tool == "GroupBox":
        _prop(el, "title", "string", w.title())
        if w.isCheckable():
            _prop(el, "checkable", "bool", True)
            _prop(el, "checked", "bool", w.isChecked())
    elif tool == "Panel":
        pass
    elif tool == "LinkLabel":
        _prop(el, "text", "string", link_html(ps.get("linktext", ""), ps.get("link")))
        _prop(el, "textFormat", "enum", "Qt::TextFormat::RichText")
        if ps.get("link"):
            _prop(el, "openExternalLinks", "bool", True)
    elif tool == "MaskedTextBox":
        if w.inputMask():
            _prop(el, "inputMask", "string", w.inputMask())
        if w.text():
            _prop(el, "text", "string", w.text())
    elif tool == "TreeView":
        _write_tree_items(el, w.invisibleRootItem())
        a = ET.SubElement(el, "attribute", name="headerVisible")
        ET.SubElement(a, "bool").text = "false"
        a = ET.SubElement(el, "column")
        _prop(a, "text", "string", "1")
    elif tool == "ListView":
        _prop(el, "rootIsDecorated", "bool", False)
        _prop(el, "allColumnsShowFocus", "bool", True)
        if w.alternatingRowColors():
            _prop(el, "alternatingRowColors", "bool", True)
        for c in ps.get("columns", []) or ["Column1"]:
            col = ET.SubElement(el, "column")
            _prop(col, "text", "string", c)
        for i in range(w.topLevelItemCount()):
            it = w.topLevelItem(i)
            item = ET.SubElement(el, "item")
            for c in range(w.columnCount()):
                _prop(item, "text", "string", it.text(c))
    elif tool == "TabControl":
        _prop(el, "currentIndex", "number", max(0, w.currentIndex()))
        pos = next((k for k, v in TAB_POSITIONS.items()
                    if getattr(QTabWidget.TabPosition, v) == w.tabPosition()), "Top")
        if pos != "Top":
            _prop(el, "tabPosition", "enum", f"QTabWidget::TabPosition::{TAB_POSITIONS[pos]}")
    elif tool == "SplitContainer":
        orient = "Horizontal" if w.orientation() == Qt.Horizontal else "Vertical"
        _prop(el, "orientation", "enum", f"Qt::Orientation::{orient}")
        _prop(el, "childrenCollapsible", "bool", False)
        sizes = w.sizes()
        if sizes and sizes[0] > 0:
            _prop(el, "splitterDistance", "number", sizes[0], stdset=False)
    if tool in ("Label", "PictureBox", "Panel") and ps.get("border", "None") != "None":
        shape = "Box" if ps["border"] == "FixedSingle" else "Panel"
        _prop(el, "frameShape", "enum", f"QFrame::Shape::{shape}")
        if ps["border"] == "Fixed3D":
            _prop(el, "frameShadow", "enum", "QFrame::Shadow::Sunken")
    del text_prop

    # dynamic (designer/runtime) properties
    if tool in PSTYPE_TOOLS:
        _prop(el, "psType", "string", tool, stdset=False)
    if tool == "LinkLabel":
        _prop(el, "linkText", "string", ps.get("linktext", ""), stdset=False)
        if ps.get("link"):
            _prop(el, "linkUrl", "string", ps["link"], stdset=False)
    if ps.get("dock", "None") != "None":
        _prop(el, "dock", "string", ps["dock"], stdset=False)
    if ps.get("anchor", ANCHOR_DEFAULT) != ANCHOR_DEFAULT:
        _prop(el, "anchor", "string", ps["anchor"], stdset=False)
    if ps.get("tag"):
        _prop(el, "tag", "string", ps["tag"], stdset=False)
    if ps.get("padding"):
        _prop(el, "padding", "string", ps["padding"], stdset=False)
    if ps.get("locked"):
        _prop(el, "locked", "bool", True, stdset=False)
    if tool in BINDABLE and ps.get("datasource"):
        _prop(el, "dataSource", "string", ps["datasource"], stdset=False)
        if ps.get("master"):
            _prop(el, "masterControl", "string", ps["master"], stdset=False)
        if not ps.get("autoload", True):
            _prop(el, "autoLoad", "bool", False, stdset=False)
        if tool in ("ComboBox", "ListBox"):
            if ps.get("displaymember"):
                _prop(el, "displayMember", "string", ps["displaymember"], stdset=False)
            if ps.get("valuemember"):
                _prop(el, "valueMember", "string", ps["valuemember"], stdset=False)
    if tool == "DataGridView":
        if not ps.get("allowadd", True):
            _prop(el, "allowUserToAddRows", "bool", False, stdset=False)
        if not ps.get("allowdelete", True):
            _prop(el, "allowUserToDeleteRows", "bool", False, stdset=False)
    if tool in VALUE_BINDABLE and ps.get("binding"):
        _prop(el, "dataBinding", "string", ps["binding"], stdset=False)
    if tool == "BindingNavigator" and ps.get("bindsource"):
        _prop(el, "bindingSource", "string", ps["bindsource"], stdset=False)
    if tool == "Button" and ps.get("dialogresult", "None") != "None":
        _prop(el, "dialogResult", "string", ps["dialogresult"], stdset=False)
    if tool == "Chart":
        _prop(el, "chartType", "string", ps.get("charttype") or "Column", stdset=False)
        if ps.get("charttitle"):
            _prop(el, "chartTitle", "string", ps["charttitle"], stdset=False)
        if not ps.get("legend", True):
            _prop(el, "legendVisible", "bool", False, stdset=False)
        if ps.get("xmember"):
            _prop(el, "xMember", "string", ps["xmember"], stdset=False)
        if ps.get("ymembers"):
            _prop(el, "yMembers", "string", ps["ymembers"], stdset=False)
    if tool == "WebBrowser" and ps.get("url"):
        _prop(el, "startUrl", "string", ps["url"], stdset=False)
    if ps.get("contextmenu"):
        _prop(el, "contextMenuStrip", "string", ps["contextmenu"], stdset=False)
    if ps.get("autosize"):
        _prop(el, "autoSize", "bool", True, stdset=False)
    if tool == "DateTimePicker":
        _prop(el, "valueToday", "bool", True, stdset=False)   # runtime shows today, like VS
    if tool == "DateTimePicker" and ps.get("format") != "Short":
        _prop(el, "dateFormat", "string", ps["format"], stdset=False)
    if tool in CONTAINERS:
        _prop(el, "designSize", "string", f"{g.width()},{g.height()}", stdset=False)

    # items / columns
    if tool in ("ComboBox", "ListBox"):
        for text in get_items(w):
            item = ET.SubElement(el, "item")
            _prop(item, "text", "string", text)
    if tool == "DataGridView":
        for c in ps.get("columns", []):
            col = ET.SubElement(el, "column")
            _prop(col, "text", "string", c)
    return el


def _write_tree_items(parent_el, item):
    for i in range(item.childCount()):
        ch = item.child(i)
        e = ET.SubElement(parent_el, "item")
        _prop(e, "text", "string", ch.text(0))
        _write_tree_items(e, ch)


def _read_tree_items(el, parent_item):
    for e in el.findall("item"):
        texts = [p.findtext("string") or "" for p in e.findall("property") if p.get("name") == "text"]
        it = QTreeWidgetItem(parent_item, [texts[0] if texts else ""])
        _read_tree_items(e, it)


def _align_set(name):
    h, v = ALIGN[name]
    return f"Qt::AlignmentFlag::{h}|Qt::AlignmentFlag::{v}"


def _align_from_set(text):
    flags = {_enum_tail(x).strip() for x in (text or "").split("|")}
    h = next((x for x in ("AlignHCenter", "AlignRight", "AlignLeft") if x in flags or
              (x == "AlignLeft" and "AlignLeading" in flags)), "AlignLeft")
    if "AlignCenter" in flags:
        return "MiddleCenter"
    v = next((x for x in ("AlignTop", "AlignBottom", "AlignVCenter") if x in flags), "AlignVCenter")
    for name, (hh, vv) in ALIGN.items():
        if hh == h and vv == v:
            return name
    return "MiddleLeft"


def hidden_containers(central_el):
    """Containers in a fixed-position form that the WinForms-style designer would show as a gray box
    with their controls hidden inside (a QStackedWidget, a plain QWidget, anything still using a layout)."""
    out = []

    def walk(el):
        for c in el.findall("widget"):
            cls = c.get("class")
            p = read_props(c)
            tool = p.get("psType") or CLASS_TO_TOOL.get(cls)
            if tool is None and cls in USER_CONTROLS:
                tool = cls
            nested = c.find("layout") is not None and tool not in PANELED
            has_kids = c.find(".//widget") is not None
            if (tool is None or nested or (c.find("widget") is not None and tool not in CONTAINERS)) and has_kids:
                out.append(c.get("name") or cls)
                continue
            if tool in PANELED:                # its pages/panels are plain QWidgets: look inside them
                for page in c.findall("widget"):
                    walk(page)
            else:
                walk(c)
    walk(central_el)
    return out


def control_from_xml(el, parent, project_dir=None):
    """Create a control (and its children). Returns the list of created controls,
    the control itself first."""
    cls = el.get("class")
    p = read_props(el)
    tool = p.get("psType") or CLASS_TO_TOOL.get(cls)
    if tool is None and cls in USER_CONTROLS:
        tool = cls
    nested = el.find("layout") is not None
    if tool in PANELED and nested:
        nested = False
    if tool is None or nested or (el.find("widget") is not None and tool not in CONTAINERS):
        w = make_placeholder(copy.deepcopy(el), parent)
        w.setObjectName(el.get("name") or "widget")
        w.setGeometry(*p.get("geometry", (0, 0, 100, 30)))
        return [w]

    w = make_widget(tool, parent, empty=True)
    w.setObjectName(el.get("name") or "widget")
    ps = w._ps
    ps["font"] = p.get("font")
    ps["dock"] = p.get("dock", "None")
    cur = p.get("cursor")
    ps["cursor"] = next((k for k, v in CURSORS.items() if v == _enum_tail(cur)), "Default") if cur else "Default"
    w.setToolTip(p.get("toolTip", ""))
    w.setEnabled(p.get("enabled", True))
    ps["visible"] = p.get("visible", True)
    ps["tabstop"] = "NoFocus" not in (p.get("focusPolicy") or "")
    ps["css"] = split_css(p.get("styleSheet"))
    m = re.search(r"background-color:\s*([^;]+);", p.get("styleSheet", "").split("/* PyStudio */")[-1]) \
        if "/* PyStudio */" in p.get("styleSheet", "") else None
    ps["back"] = m.group(1).strip() if m else None
    m = re.search(r"(?<![-\w])color:\s*([^;]+);", p.get("styleSheet", "").split("/* PyStudio */")[-1]) \
        if "/* PyStudio */" in p.get("styleSheet", "") else None
    ps["fore"] = m.group(1).strip() if m else None
    ps["anchor"] = p.get("anchor", ANCHOR_DEFAULT)
    ps["tag"] = p.get("tag", "")
    ps["padding"] = p.get("padding", "")
    ps["locked"] = p.get("locked", False)
    ps["autosize"] = p.get("autoSize", False)
    ps["datasource"] = p.get("dataSource", "")
    ps["autoload"] = p.get("autoLoad", True)
    ps["displaymember"] = p.get("displayMember", "")
    ps["valuemember"] = p.get("valueMember", "")
    ps["master"] = p.get("masterControl", "")
    ps["allowadd"] = p.get("allowUserToAddRows", True)
    ps["allowdelete"] = p.get("allowUserToDeleteRows", True)
    ps["binding"] = p.get("dataBinding", "")
    ps["bindsource"] = p.get("bindingSource", "")
    ps["dialogresult"] = p.get("dialogResult", "None")
    ps["charttype"] = p.get("chartType", "Column")
    ps["charttitle"] = p.get("chartTitle", "")
    ps["legend"] = p.get("legendVisible", True)
    ps["xmember"] = p.get("xMember", "")
    ps["ymembers"] = p.get("yMembers", "")
    ps["url"] = p.get("startUrl", "")
    ps["contextmenu"] = p.get("contextMenuStrip", "")
    fs = _enum_tail(p.get("frameShape"))
    ps["border"] = "FixedSingle" if fs == "Box" else ("Fixed3D" if fs in ("Panel", "StyledPanel", "WinPanel") else "None")

    if tool == "Button":
        w.setText(p.get("text", ""))
        ps["flat"] = p.get("flat", False)
    elif tool == "Label":
        w.setText(p.get("text", ""))
        w.setAlignment(qt_align(_align_from_set(p.get("alignment"))) if p.get("alignment") else qt_align("MiddleLeft"))
        w.setWordWrap(p.get("wordWrap", False))
    elif tool == "PictureBox":
        ps["image"] = p.get("pixmap", "")
        ps["sizemode"] = "StretchImage" if p.get("scaledContents") else (
            "CenterImage" if p.get("alignment") and "Center" in p.get("alignment") else "Normal")
    elif tool == "TextBox":
        w.setText(p.get("text", ""))
        if p.get("alignment"):
            w.setAlignment(qt_align(_align_from_set(p["alignment"])))
        w.setMaxLength(p.get("maxLength", 32767))
        w.setReadOnly(p.get("readOnly", False))
        if "Password" in (p.get("echoMode") or ""):
            w.setEchoMode(QLineEdit.Password)
        w.setPlaceholderText(p.get("placeholderText", ""))
    elif tool == "TextArea":
        w.setPlainText(p.get("plainText", ""))
        w.setReadOnly(p.get("readOnly", False))
        w.setPlaceholderText(p.get("placeholderText", ""))
        if "NoWrap" in (p.get("lineWrapMode") or ""):
            w.setLineWrapMode(QPlainTextEdit.NoWrap)
    elif tool in ("CheckBox", "RadioButton"):
        w.setText(p.get("text", ""))
        if tool == "CheckBox":
            w.setTristate(p.get("tristate", False))
        w.setChecked(p.get("checked", False))
    elif tool == "ComboBox":
        w.setEditable(p.get("editable", False))
        w.setMaxVisibleItems(p.get("maxVisibleItems", 10))
        set_items(w, [read_props(i).get("text", "") for i in el.findall("item")])
    elif tool == "ListBox":
        mode = p.get("selectionMode") or ""
        w.setSelectionMode(QAbstractItemView.MultiSelection if "MultiSelection" in mode else
                           QAbstractItemView.ExtendedSelection if "Extended" in mode else
                           QAbstractItemView.SingleSelection)
        w.setSortingEnabled(p.get("sortingEnabled", False))
        set_items(w, [read_props(i).get("text", "") for i in el.findall("item")])
    elif tool == "NumericUpDown":
        w.setMinimum(p.get("minimum", 0))
        w.setMaximum(p.get("maximum", 99))
        w.setValue(p.get("value", 0))
        w.setSingleStep(p.get("singleStep", 1))
        w.setPrefix(p.get("prefix", ""))
        w.setSuffix(p.get("suffix", ""))
    elif tool == "DateTimePicker":
        w.setDisplayFormat(p.get("displayFormat", "M/d/yyyy"))
        w.setCalendarPopup(p.get("calendarPopup", True))
        ps["format"] = p.get("dateFormat", "Short")
    elif tool == "TrackBar":
        if "Vertical" in (p.get("orientation") or ""):
            w.setOrientation(Qt.Vertical)
        w.setMinimum(p.get("minimum", 0))
        w.setMaximum(p.get("maximum", 99))
        w.setValue(p.get("value", 0))
        w.setSingleStep(p.get("singleStep", 1))
        w.setPageStep(p.get("pageStep", 10))
        tp = _enum_tail(p.get("tickPosition"))
        w.setTickPosition({"TicksAbove": QSlider.TicksAbove, "TicksBelow": QSlider.TicksBelow,
                           "TicksBothSides": QSlider.TicksBothSides}.get(tp, QSlider.NoTicks))
        w.setTickInterval(p.get("tickInterval", 0))
    elif tool == "ProgressBar":
        w.setMinimum(p.get("minimum", 0))
        w.setMaximum(p.get("maximum", 100))
        w.setValue(p.get("value", 0))
        w.setTextVisible(p.get("textVisible", True))
    elif tool == "DataGridView":
        set_columns(w, [read_props(c).get("text", "") for c in el.findall("column")])
        w.setAlternatingRowColors(p.get("alternatingRowColors", False))
        if "NoEditTriggers" in (p.get("editTriggers") or ""):
            w.setEditTriggers(QAbstractItemView.NoEditTriggers)
        if "SelectRows" in (p.get("selectionBehavior") or ""):
            w.setSelectionBehavior(QAbstractItemView.SelectRows)
        for a in el.findall("attribute"):
            if a.get("name") == "verticalHeaderVisible" and a.findtext("bool") == "false":
                w.verticalHeader().setVisible(False)
    elif tool == "GroupBox":
        w.setTitle(p.get("title", ""))
        w.setCheckable(p.get("checkable", False))
        if w.isCheckable():
            w.setChecked(p.get("checked", True))
    elif tool == "LinkLabel":
        ps["link"] = p.get("linkUrl", "")
        set_text(w, p.get("linkText", re.sub(r"<[^>]+>", "", p.get("text", ""))))
    elif tool == "MaskedTextBox":
        w.setInputMask(p.get("inputMask", ""))
        w.setText(p.get("text", ""))
    elif tool == "TreeView":
        _read_tree_items(el, w.invisibleRootItem())
        w.expandAll()
    elif tool == "ListView":
        set_listview_columns(w, [read_props(c).get("text", "") for c in el.findall("column")])
        w.setAlternatingRowColors(p.get("alternatingRowColors", False))
        for e in el.findall("item"):
            texts = [x.findtext("string") or "" for x in e.findall("property") if x.get("name") == "text"]
            QTreeWidgetItem(w, texts)
    elif tool == "SplitContainer":
        if "Vertical" in (p.get("orientation") or ""):
            w.setOrientation(Qt.Vertical)
        ps["split"] = p.get("splitterDistance", 0)
    elif tool == "TabControl":
        tp = _enum_tail(p.get("tabPosition"))
        if tp:
            w.setTabPosition(getattr(QTabWidget.TabPosition, tp, QTabWidget.North))

    w.setGeometry(*p.get("geometry", (0, 0) + TOOLS[tool][2]))
    created = [w]
    if tool in PANELED:
        for page_el in el.findall("widget"):
            page = QWidget()
            page.setObjectName(page_el.get("name") or "page")
            if tool == "TabControl":
                title = next((a.findtext("string") for a in page_el.findall("attribute")
                              if a.get("name") == "title"), page.objectName()) or ""
                w.addTab(page, title)
            else:
                w.addWidget(page)
            page.setPalette(w.palette())
            for child in page_el.findall("widget"):
                for c in control_from_xml(child, page, project_dir):
                    c.show()
                    created.append(c)
        if tool == "TabControl":
            w.setCurrentIndex(min(p.get("currentIndex", 0), max(0, w.count() - 1)))
    elif tool in CONTAINERS:
        for child in el.findall("widget"):
            for c in control_from_xml(child, w, project_dir):
                c.show()
                created.append(c)
    apply_visuals(w, project_dir)
    return created


# ============================================================ menus
def _clean(text):
    return re.sub(r"[^0-9A-Za-z]", "", text.replace("&", "")) or "Item"


def menu_to_text(menu):
    """Menu model -> indented text for the menu editor."""
    lines = []

    def walk(items, depth):
        for it in items:
            if it.get("sep"):
                lines.append("    " * depth + "-")
                continue
            line = "    " * depth + it["text"]
            if it.get("shortcut"):
                line += f"  [{it['shortcut']}]"
            lines.append(line)
            walk(it.get("items", []), depth + 1)
    walk(menu or [], 0)
    return "\n".join(lines)


def context_menu_items(items):
    """Items of a ContextMenuStrip: commands are actions (actionCopy) even at the top level, where a
    menu bar would have menus (menuFile)."""
    for it in items or []:
        if it.get("sep"):
            continue
        if not it.get("items") and str(it.get("name", "")).startswith("menu"):
            it["name"] = "action" + it["name"][4:]
        context_menu_items(it.get("items"))
    return items


def menu_from_text(text, old=None):
    """Indented text -> menu model. Keeps names of items whose text didn't change."""
    old_names = {}

    def collect(items):
        for it in items or []:
            if not it.get("sep"):
                old_names.setdefault(it["text"], it["name"])
                collect(it.get("items"))
    collect(old)

    used = set()

    def unique(base):
        n, name = 1, base
        while name in used:
            n += 1
            name = f"{base}{n}"
        used.add(name)
        return name

    root = []
    stack = [(-1, {"items": root})]
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        lead = raw[:len(raw) - len(raw.lstrip(" \t"))]
        depth = lead.count("\t") + lead.count(" ") // 4
        body = raw.strip()
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else {"items": root}
        if body == "-":
            parent.setdefault("items", []).append({"sep": True})
            continue
        m = re.match(r"^(.*?)\s*\[([^\]]+)\]\s*$", body)
        label, shortcut = (m.group(1).strip(), m.group(2).strip()) if m else (body, "")
        node = {"text": label, "shortcut": shortcut, "items": []}
        parent.setdefault("items", []).append(node)
        stack.append((depth, node))

    def assign(items, top):
        for it in items:
            if it.get("sep"):
                continue
            is_menu = top or bool(it["items"])
            want = old_names.get(it["text"])
            if want and want.startswith("menu" if is_menu else "action") and want not in used:
                used.add(want)
                it["name"] = want
            else:
                it["name"] = unique(("menu" if is_menu else "action") + _clean(it["text"]))
            assign(it["items"], False)
    assign(root, True)
    return root


def all_actions(menu):
    """(name, text) of every clickable menu item."""
    out = []

    def walk(items, top):
        for it in items or []:
            if it.get("sep"):
                continue
            if it.get("items") or top:
                walk(it.get("items"), False)
            else:
                out.append((it["name"], it["text"].replace("&", "")))
    walk(menu, True)
    return out


def menu_to_xml(top, menu):
    """Add QMenuBar + QAction elements to the QMainWindow element."""
    bar = ET.SubElement(top, "widget", {"class": "QMenuBar", "name": "menubar"})
    actions = []

    def build(parent_el, items):
        for it in items:
            if it.get("sep"):
                ET.SubElement(parent_el, "addaction", name="separator")
            elif it.get("items") or parent_el is bar:
                m = ET.SubElement(parent_el, "widget", {"class": "QMenu", "name": it["name"]})
                _prop(m, "title", "string", it["text"])
                build(m, it.get("items", []))
                ET.SubElement(parent_el, "addaction", name=it["name"])
            else:
                actions.append(it)
                ET.SubElement(parent_el, "addaction", name=it["name"])
    build(bar, menu)
    for it in actions:
        a = ET.SubElement(top, "action", name=it["name"])
        if it.get("checkable"):
            _prop(a, "checkable", "bool", True)
            if it.get("checked"):
                _prop(a, "checked", "bool", True)
        if not it.get("enabled", True):
            _prop(a, "enabled", "bool", False)
        if it.get("icon"):
            _prop(a, "icon", "iconset", it["icon"])
        _prop(a, "text", "string", it["text"])
        if it.get("tooltip"):
            _prop(a, "toolTip", "string", it["tooltip"])
        if it.get("shortcut"):
            _prop(a, "shortcut", "string", it["shortcut"])
        if not it.get("visible", True):
            _prop(a, "visible", "bool", False)


def menu_from_xml(top):
    bar = next((w for w in top.findall("widget") if w.get("class") == "QMenuBar"), None)
    if bar is None:
        return None
    actions = {a.get("name"): read_props(a) for a in top.findall("action")}

    def parse(el):
        items = []
        menus = {m.get("name"): m for m in el.findall("widget") if m.get("class") == "QMenu"}
        for add in el.findall("addaction"):
            n = add.get("name")
            if n == "separator":
                items.append({"sep": True})
            elif n in menus:
                m = menus[n]
                items.append({"name": n, "text": read_props(m).get("title", n), "shortcut": "",
                              "items": parse(m)})
            elif n in actions:
                a = actions[n]
                items.append({"name": n, "text": a.get("text", n), "shortcut": a.get("shortcut", ""),
                              "items": [], "checkable": a.get("checkable", False),
                              "checked": a.get("checked", False), "enabled": a.get("enabled", True),
                              "visible": a.get("visible", True), "tooltip": a.get("toolTip", ""),
                              "icon": a.get("icon", "")})
        return items
    return parse(bar)
