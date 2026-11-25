"""VS-style Properties window: categorized/alphabetical grid + Events view."""
import os
import shutil

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QFileDialog, QFontDialog, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSlider, QSpinBox, QTableWidget, QToolButton,
                               QVBoxLayout, QWidget, QAbstractItemView as AIV)

from . import model as M


class Prop:
    def __init__(self, cat, name, kind, get, set_, opts=None, tip="", rebuild=False):
        self.cat, self.name, self.kind = cat, name, kind
        self.get, self.set, self.opts, self.tip = get, set_, opts, tip
        self.rebuild = rebuild   # rebuild the grid after a change (other rows depend on it)


# ============================================================ data-binding helpers
def _bound_sources(d, exclude=None):
    """Controls on the form that have a DataSource (can be a Master / BindingSource)."""
    return [c for c in d.controls if c is not exclude and c._ps.get("tool") in M.BINDABLE
            and c._ps.get("datasource")]


def _query_columns(d, name):
    from . import dbcore, mygen
    path = mygen.query_files(d.project_dir).get(name) if d.project_dir else None
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return dbcore.select_columns(dbcore.parse_query_file(f.read())["sql"]) or []
    except (OSError, ValueError, KeyError):
        return []


def _binding_choices(d, w):
    out = []
    for c in _bound_sources(d, w):
        cols = _query_columns(d, c._ps["datasource"]) or list(c._ps.get("columns") or [])
        out += [f"{c.objectName()}.{col}" for col in cols]
    return out


def _set_datasource(d, w, name):
    """Like VS: binding a grid/list to a query shows the query's columns in the designer
    (unless the Columns were already customized)."""
    ps = w._ps
    ps["datasource"] = name
    if ps.get("tool") in ("DataGridView", "ListView") and name:
        cols = _query_columns(d, name)
        if cols and list(ps.get("columns") or []) in ([], ["Column1"], ["Column1", "Column2"]):
            if ps["tool"] == "DataGridView":
                M.set_columns(w, cols)
            else:
                ps["columns"] = cols
                w.setColumnCount(len(cols))
                w.setHeaderLabels(cols)


# ============================================================ property specs
def form_specs(d):
    f = d.form

    def fset(key):
        def s(v):
            f[key] = v
            d.apply_form_visuals()
        return s

    def set_menu(v):
        d.menu = ([] if d.menu is None else d.menu) if v else None
        d._update_strips()

    def set_status(v):
        d.status = bool(v)
        d._update_strips()

    buttons = sorted(c.objectName() for c in d.controls if c._ps.get("tool") == "Button")
    specs = [
        Prop("Design", "(Name)", "readonly", lambda: d.form_class, None,
             tip="The form's class name (used in your code)."),
        Prop("Appearance", "Text", "str", lambda: f["title"], fset("title"),
             tip="Text in the window's title bar."),
        Prop("Appearance", "BackColor", "color", lambda: f.get("back"), fset("back"),
             tip="Background color of the form. Labels, check boxes, etc. show it through."),
        Prop("Appearance", "ForeColor", "color", lambda: f.get("fore"), fset("fore"),
             tip="Default text color for labels, check boxes, radio buttons and group boxes."),
        Prop("Appearance", "Font", "font", lambda: f.get("font"), fset("font"),
             tip="Default font for the form. Controls inherit it unless they set their own."),
        Prop("Appearance", "FormBorderStyle", "enum", lambda: f["border"], fset("border"),
             ["Sizable", "FixedSingle", "Fixed3D", "FixedDialog", "None"],
             tip="Fixed styles stop the user resizing the window; None removes the title bar."),
        Prop("Appearance", "StyleSheet", "multiline", lambda: f.get("css", ""), fset("css"),
             tip="Advanced: extra Qt style sheet (CSS-like) for the whole form."),
        Prop("Window Style", "Icon", "file", lambda: f.get("icon", ""), fset("icon"),
             tip="Window icon (title bar / taskbar)."),
        Prop("Window Style", "Opacity", "int", lambda: f.get("opacity", 100), fset("opacity"), (10, 100),
             tip="Window opacity in percent."),
        Prop("Window Style", "MaximizeBox", "bool", lambda: f["maxbox"], fset("maxbox"),
             tip="Show the maximize button."),
        Prop("Window Style", "MinimizeBox", "bool", lambda: f["minbox"], fset("minbox"),
             tip="Show the minimize button."),
        Prop("Window Style", "TopMost", "bool", lambda: f["topmost"], fset("topmost"),
             tip="Keep the window above other windows."),
        Prop("Window Style", "MenuStrip", "bool", lambda: d.menu is not None, set_menu,
             tip="Show a menu bar. Double-click it on the form to edit the menu.", rebuild=True),
        Prop("Window Style", "StatusStrip", "bool", lambda: d.status, set_status,
             tip="Show a status bar at the bottom (self.statusBar().showMessage(...) in code)."),
        Prop("Layout", "Width", "geoW", lambda: d.form_size.width(),
             lambda v: d.set_form_size(v, d.form_size.height()), (120, 4000)),
        Prop("Layout", "Height", "geoH", lambda: d.form_size.height(),
             lambda v: d.set_form_size(d.form_size.width(), v), (80, 4000)),
        Prop("Layout", "MinimumSize", "size", lambda: f["minsize"], fset("minsize"),
             tip="Smallest size the user can resize the window to (0, 0 = no limit)."),
        Prop("Layout", "MaximumSize", "size", lambda: f["maxsize"], fset("maxsize"),
             tip="Largest size the user can resize the window to (0, 0 = no limit)."),
        Prop("Layout", "StartPosition", "enum", lambda: f["startpos"], fset("startpos"),
             ["WindowsDefaultLocation", "CenterScreen", "CenterParent"],
             tip="Where the window appears when shown. CenterParent: over the form that opened it "
                 "with show_dialog()."),
        Prop("Layout", "WindowState", "enum", lambda: f["state"], fset("state"),
             ["Normal", "Maximized", "Minimized"], tip="Initial window state."),
        Prop("Misc", "AcceptButton", "enum", lambda: f.get("accept") or "(none)",
             lambda v: f.__setitem__("accept", "" if v == "(none)" else v), ["(none)"] + buttons,
             tip="The button clicked when the user presses Enter (e.g. OK)."),
        Prop("Misc", "CancelButton", "enum", lambda: f.get("cancel") or "(none)",
             lambda v: f.__setitem__("cancel", "" if v == "(none)" else v), ["(none)"] + buttons,
             tip="The button clicked when the user presses Esc (e.g. Cancel)."),
        Prop("Behavior", "ContextMenuStrip", "enum", lambda: f.get("contextmenu") or "(none)",
             lambda v: f.__setitem__("contextmenu", "" if v == "(none)" else v),
             ["(none)"] + [c["name"] for c in d.components if c["type"] == "ContextMenuStrip"],
             tip="The menu that opens when the user right-clicks the form's empty area."),
    ]
    if f.get("kind") == "UserControl":     # a control, not a window
        keep = {"(Name)", "BackColor", "ForeColor", "Font", "StyleSheet", "Width", "Height"}
        specs = [sp for sp in specs if sp.name in keep]
    return specs


def control_specs(d, w):
    ps = w._ps
    tool = ps.get("tool")
    pd = d.project_dir

    def vis(func):
        """Setter wrapper that re-applies the control's look afterwards."""
        def s(v):
            func(v)
            M.apply_visuals(w, pd)
        return s

    def psset(key):
        return vis(lambda v: ps.__setitem__(key, v))

    def geo(key):
        def s(v):
            r = w.geometry()
            if key == "X": r.moveLeft(v)
            if key == "Y": r.moveTop(v)
            if key == "W": r.setWidth(v)
            if key == "H": r.setHeight(v)
            w.setGeometry(r)
        return s

    specs = [
        Prop("Design", "(Name)", "name", w.objectName, None,
             tip="The name used in code: self.ui.<name>"),
        Prop("Layout", "Left", "geoX", lambda: w.x(), geo("X"), (0, 4000)),
        Prop("Layout", "Top", "geoY", lambda: w.y(), geo("Y"), (0, 4000)),
        Prop("Layout", "Width", "geoW", lambda: w.width(), geo("W"), (1, 4000)),
        Prop("Layout", "Height", "geoH", lambda: w.height(), geo("H"), (1, 4000)),
    ]
    if tool is None:   # placeholder from another tool
        return specs

    specs += [
        Prop("Design", "Locked", "bool", lambda: ps["locked"], psset("locked"),
             tip="Locked controls can't be moved or resized in the designer."),
        Prop("Appearance", "BackColor", "color", lambda: ps.get("back"), psset("back"),
             tip="Background color."),
        Prop("Appearance", "ForeColor", "color", lambda: ps.get("fore"), psset("fore"),
             tip="Text color."),
        Prop("Appearance", "Font", "font", lambda: ps.get("font"), psset("font"),
             tip="Font (inherits the form's font unless set)."),
        Prop("Appearance", "Cursor", "enum", lambda: ps.get("cursor", "Default"),
             lambda v: ps.__setitem__("cursor", v), list(M.CURSORS),
             tip="Mouse pointer shown over the control."),
        Prop("Appearance", "StyleSheet", "multiline", lambda: ps.get("css", ""), psset("css"),
             tip="Advanced: extra Qt style sheet rules for this control."),
        Prop("Behavior", "Enabled", "bool", w.isEnabled, w.setEnabled,
             tip="Whether the control responds to the user."),
        Prop("Behavior", "Visible", "bool", lambda: ps.get("visible", True),
             lambda v: ps.__setitem__("visible", v),
             tip="Shown at runtime. (It stays visible in the designer, like VS.)"),
        Prop("Behavior", "TabStop", "bool", lambda: ps.get("tabstop", True),
             lambda v: ps.__setitem__("tabstop", v), tip="Can receive focus with the Tab key."),
        Prop("Behavior", "ToolTip", "str", w.toolTip, w.setToolTip, tip="Text shown when hovering."),
        Prop("Layout", "Anchor", "anchor", lambda: ps.get("anchor", M.ANCHOR_DEFAULT),
             lambda v: ps.__setitem__("anchor", v),
             tip="Edges of the container this control stays attached to when the window resizes."),
        Prop("Layout", "Dock", "enum", lambda: ps.get("dock", "None"), lambda v: ps.__setitem__("dock", v),
             M.DOCKS, tip="Stick to an edge of the container (Top/Bottom/Left/Right) or fill what's "
                          "left (Fill). Docked controls resize with the window."),
        Prop("Data", "Tag", "str", lambda: ps.get("tag", ""), lambda v: ps.__setitem__("tag", v),
             tip="Any text you want to keep with the control: self.ui.<name>.property('tag')"),
        Prop("Behavior", "ContextMenuStrip", "enum", lambda: ps.get("contextmenu") or "(none)",
             lambda v: ps.__setitem__("contextmenu", "" if v == "(none)" else v),
             ["(none)"] + [c["name"] for c in d.components if c["type"] == "ContextMenuStrip"],
             tip="The menu that opens when the user right-clicks this control (add a ContextMenuStrip "
                 "from the Toolbox first)."),
    ]

    def text_prop(multiline=False):
        return Prop("Appearance", "Text", "multiline" if multiline else "str",
                    lambda: M.get_text(w) or "", lambda v: M.set_text(w, v),
                    tip="Text shown on the control.")

    if tool in ("Button", "Label", "CheckBox", "RadioButton", "TextBox", "GroupBox"):
        specs.append(text_prop())
    if tool == "TextArea":
        specs.append(text_prop(True))
    if tool in ("Label", "PictureBox", "Panel"):
        specs.append(Prop("Appearance", "BorderStyle", "enum", lambda: ps.get("border", "None"),
                          psset("border"), M.BORDER_STYLES))
    if tool == "Button":
        specs.append(Prop("Appearance", "FlatStyle", "enum",
                          lambda: "Flat" if ps.get("flat") else "Standard",
                          vis(lambda v: ps.__setitem__("flat", v == "Flat")), ["Standard", "Flat"]))
    if tool == "Label":
        specs += [
            Prop("Layout", "AutoSize", "bool", lambda: ps.get("autosize", False), psset("autosize"),
                 tip="Size the label to fit its text."),
            Prop("Appearance", "TextAlign", "enum", lambda: M.align_name(w.alignment()),
                 lambda v: w.setAlignment(M.qt_align(v)), list(M.ALIGN)),
            Prop("Behavior", "WordWrap", "bool", w.wordWrap, vis(w.setWordWrap)),
        ]
    if tool == "TextBox":
        specs += [
            Prop("Appearance", "TextAlign", "enum",
                 lambda: {"MiddleCenter": "Center", "MiddleRight": "Right"}.get(M.align_name(w.alignment()), "Left"),
                 lambda v: w.setAlignment(M.qt_align({"Left": "MiddleLeft", "Center": "MiddleCenter",
                                                      "Right": "MiddleRight"}[v])), list(M.HALIGN)),
            Prop("Behavior", "MaxLength", "int", w.maxLength, w.setMaxLength, (1, 32767)),
            Prop("Behavior", "ReadOnly", "bool", w.isReadOnly, w.setReadOnly),
            Prop("Behavior", "UseSystemPasswordChar", "bool",
                 lambda: w.echoMode() == QLineEdit.Password,
                 lambda v: w.setEchoMode(QLineEdit.Password if v else QLineEdit.Normal),
                 tip="Hide the text with password dots."),
            Prop("Appearance", "PlaceholderText", "str", w.placeholderText, w.setPlaceholderText,
                 tip="Gray hint text shown when empty."),
        ]
    if tool == "TextArea":
        specs += [
            Prop("Behavior", "ReadOnly", "bool", w.isReadOnly, w.setReadOnly),
            Prop("Behavior", "WordWrap", "bool", lambda: w.lineWrapMode() != QPlainTextEdit.NoWrap,
                 lambda v: w.setLineWrapMode(QPlainTextEdit.WidgetWidth if v else QPlainTextEdit.NoWrap)),
            Prop("Appearance", "PlaceholderText", "str", w.placeholderText, w.setPlaceholderText),
        ]
    if tool in ("CheckBox", "RadioButton"):
        specs.append(Prop("Appearance", "Checked", "bool", w.isChecked, w.setChecked))
    if tool == "CheckBox":
        specs.append(Prop("Behavior", "ThreeState", "bool", w.isTristate, w.setTristate))
    if tool in ("ComboBox", "ListBox"):
        specs.append(Prop("Data", "Items", "items", lambda: M.get_items(w),
                          lambda v: M.set_items(w, v), tip="One item per line."))
    if tool == "ComboBox":
        specs += [
            Prop("Appearance", "DropDownStyle", "enum",
                 lambda: "DropDown" if w.isEditable() else "DropDownList",
                 lambda v: w.setEditable(v == "DropDown"), ["DropDownList", "DropDown"],
                 tip="DropDown lets the user type; DropDownList only allows picking an item."),
            Prop("Behavior", "MaxDropDownItems", "int", w.maxVisibleItems, w.setMaxVisibleItems, (1, 100)),
        ]
    if tool == "ListBox":
        modes = {"One": AIV.SingleSelection, "MultiSimple": AIV.MultiSelection,
                 "MultiExtended": AIV.ExtendedSelection}
        specs += [
            Prop("Behavior", "SelectionMode", "enum",
                 lambda: next(k for k, v in modes.items() if v == w.selectionMode()),
                 lambda v: w.setSelectionMode(modes[v]), list(modes)),
            Prop("Behavior", "Sorted", "bool", w.isSortingEnabled, w.setSortingEnabled),
        ]
    if tool in ("NumericUpDown", "TrackBar", "ProgressBar"):
        specs += [
            Prop("Data", "Minimum", "int", w.minimum, w.setMinimum),
            Prop("Data", "Maximum", "int", w.maximum, w.setMaximum),
            Prop("Data", "Value", "int", w.value, w.setValue),
        ]
    if tool == "NumericUpDown":
        specs += [
            Prop("Data", "Increment", "int", w.singleStep, w.setSingleStep, (1, 1000000)),
            Prop("Appearance", "Prefix", "str", w.prefix, w.setPrefix, tip="Text before the number, e.g. $"),
            Prop("Appearance", "Suffix", "str", w.suffix, w.setSuffix, tip="Text after the number, e.g. %"),
        ]
    if tool == "TrackBar":
        ticks = {"None": QSlider.NoTicks, "TopLeft": QSlider.TicksAbove,
                 "BottomRight": QSlider.TicksBelow, "Both": QSlider.TicksBothSides}

        def set_orient(v):
            o = Qt.Horizontal if v == "Horizontal" else Qt.Vertical
            if o != w.orientation():
                w.setOrientation(o)
                w.resize(w.height(), w.width())
        specs += [
            Prop("Behavior", "SmallChange", "int", w.singleStep, w.setSingleStep, (1, 100000)),
            Prop("Behavior", "LargeChange", "int", w.pageStep, w.setPageStep, (1, 100000)),
            Prop("Appearance", "TickStyle", "enum",
                 lambda: next(k for k, v in ticks.items() if v == w.tickPosition()),
                 lambda v: w.setTickPosition(ticks[v]), list(ticks)),
            Prop("Appearance", "TickFrequency", "int", w.tickInterval, w.setTickInterval, (0, 100000)),
            Prop("Appearance", "Orientation", "enum",
                 lambda: "Horizontal" if w.orientation() == Qt.Horizontal else "Vertical",
                 set_orient, ["Horizontal", "Vertical"]),
        ]
    if tool == "ProgressBar":
        specs.append(Prop("Appearance", "ShowText", "bool", w.isTextVisible, w.setTextVisible,
                          tip="Show the percentage text."))
    if tool == "DateTimePicker":
        fmts = {"Short": "M/d/yyyy", "Long": "dddd, MMMM d, yyyy", "Time": "h:mm AP"}

        def set_fmt(v):
            ps["format"] = v
            if v in fmts:
                w.setDisplayFormat(fmts[v])
                w.setCalendarPopup(v != "Time")      # a time is picked with the arrows (VB: ShowUpDown)
        specs += [
            Prop("Appearance", "Format", "enum", lambda: ps.get("format", "Short"), set_fmt,
                 ["Short", "Long", "Time", "Custom"], rebuild=True),
            Prop("Appearance", "CustomFormat", "str", w.displayFormat,
                 lambda v: (w.setDisplayFormat(v), ps.__setitem__("format", "Custom")),
                 tip="Qt date/time format, e.g. yyyy-MM-dd, MMM d, yyyy or h:mm AP"),
        ]
    if tool == "PictureBox":
        specs += [
            Prop("Appearance", "Image", "file", lambda: ps.get("image", ""), psset("image"),
                 tip="Picture file (copied into the project's images folder)."),
            Prop("Behavior", "SizeMode", "enum", lambda: ps.get("sizemode", "Normal"), psset("sizemode"),
                 ["Normal", "StretchImage", "CenterImage"]),
        ]
    if tool == "DataGridView":
        specs += [
            Prop("Data", "Columns", "items", lambda: ps.get("columns", []),
                 lambda v: M.set_columns(w, v), tip="Column header texts, one per line."),
            Prop("Appearance", "AlternatingRowColors", "bool", w.alternatingRowColors,
                 w.setAlternatingRowColors),
            Prop("Behavior", "ReadOnly", "bool",
                 lambda: w.editTriggers() == QAbstractItemView.NoEditTriggers,
                 lambda v: w.setEditTriggers(QAbstractItemView.NoEditTriggers if v else
                                             QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                             | QAbstractItemView.AnyKeyPressed)),
            Prop("Behavior", "SelectionMode", "enum",
                 lambda: "FullRowSelect" if w.selectionBehavior() == QAbstractItemView.SelectRows else "CellSelect",
                 lambda v: w.setSelectionBehavior(QAbstractItemView.SelectRows if v == "FullRowSelect"
                                                  else QAbstractItemView.SelectItems),
                 ["CellSelect", "FullRowSelect"]),
            Prop("Appearance", "RowHeadersVisible", "bool", lambda: not w.verticalHeader().isHidden(),
                 lambda v: w.verticalHeader().setVisible(v)),
            Prop("Behavior", "AllowUserToAddRows", "bool", lambda: ps.get("allowadd", True), psset("allowadd"),
                 tip="Show the * line for new rows (when ReadOnly is off and the DataSource can be saved)."),
            Prop("Behavior", "AllowUserToDeleteRows", "bool", lambda: ps.get("allowdelete", True),
                 psset("allowdelete"), tip="Select whole rows and press Delete to delete them."),
        ]
    if tool in M.BINDABLE:
        from . import mygen
        queries = list(mygen.query_files(pd)) if pd else []
        cur = ps.get("datasource", "")
        if cur and cur not in queries:
            queries.append(cur)
        specs += [
            Prop("Data", "DataSource", "enum", lambda: ps.get("datasource") or "(none)",
                 lambda v: _set_datasource(d, w, "" if v == "(none)" else v), ["(none)"] + queries,
                 tip="A saved query (queries/*.sql) that fills this control when the form opens. "
                     "Add queries in Server Explorer or Project > Add New Query.", rebuild=True),
            Prop("Data", "Master", "enum", lambda: ps.get("master") or "(none)",
                 lambda v: ps.__setitem__("master", "" if v == "(none)" else v),
                 ["(none)"] + [c.objectName() for c in _bound_sources(d, w)],
                 tip="Master/detail: when the selected row of this control changes, the DataSource query "
                     "is run again with its :parameters taken from the master's row (same column names), "
                     "e.g. Orders WHERE CustomerID = :CustomerID under a Customers grid."),
            Prop("Data", "AutoLoad", "bool", lambda: ps.get("autoload", True), psset("autoload"),
                 tip="Fill from DataSource when the form opens. Turn off to fill it yourself, e.g. "
                     f"my.fill(self.ui.{w.objectName()}, my.queries.GetCustomers(state=\"NE\"))"),
        ]
        if tool in ("ComboBox", "ListBox"):
            specs += [
                Prop("Data", "DisplayMember", "str", lambda: ps.get("displaymember", ""), psset("displaymember"),
                     tip="Column shown in the list (default: the first column)."),
                Prop("Data", "ValueMember", "str", lambda: ps.get("valuemember", ""), psset("valuemember"),
                     tip="Column returned by my.selected_value(self.ui.<name>), e.g. CustomerID."),
            ]
    if tool == "Button":
        specs.append(Prop("Behavior", "DialogResult", "enum", lambda: ps.get("dialogresult", "None"),
                          psset("dialogresult"), M.DIALOG_RESULTS,
                          tip="Clicking the button sets the form's DialogResult; on a form shown with "
                              "show_dialog() that also closes it (e.g. OK / Cancel buttons)."))
    if tool == "Chart":
        from .pschart import CHART_TYPES
        cols = _query_columns(d, ps.get("datasource")) if ps.get("datasource") else []
        specs += [
            Prop("Chart", "ChartType", "enum", lambda: ps.get("charttype") or "Column", psset("charttype"),
                 CHART_TYPES, tip="Column, Bar, Line, Spline, Area, Point, Pie or Doughnut."),
            Prop("Chart", "Title", "str", lambda: ps.get("charttitle", ""), psset("charttitle"),
                 tip="Text above the chart."),
            Prop("Chart", "LegendVisible", "bool", lambda: ps.get("legend", True), psset("legend"),
                 tip="Show the series names (when there's more than one series, or for Pie)."),
            Prop("Data", "XMember", "combo", lambda: ps.get("xmember") or "", psset("xmember"), [""] + cols,
                 tip="DataSource column for the labels along the bottom (default: the first column)."),
            Prop("Data", "YMembers", "combo", lambda: ps.get("ymembers") or "", psset("ymembers"), [""] + cols,
                 tip="Column(s) with the values, separated by commas - one series each "
                     "(default: every number column)."),
        ]
    if tool == "WebBrowser":
        specs.append(Prop("Behavior", "Url", "str", lambda: ps.get("url", ""), psset("url"),
                          tip="Web page shown when the form opens, e.g. https://www.weather.gov - "
                              "or call self.ui.webBrowser1.navigate(url) in code."))
    if tool in M.VALUE_BINDABLE:
        specs.append(Prop("Data", "DataBinding", "combo", lambda: ps.get("binding") or "(none)",
                          lambda v: ps.__setitem__("binding", "" if v.strip() in ("(none)", "") else v.strip()),
                          ["(none)"] + _binding_choices(d, w),
                          tip="Show and edit a column of another control's current row, like VB's "
                              "DataBindings: e.g. dataGridView1.Name. Edits are saved with that control "
                              "(BindingNavigator Save or my.save)."))
    if tool == "BindingNavigator":
        specs.append(Prop("Data", "BindingSource", "enum", lambda: ps.get("bindsource") or "(none)",
                          lambda v: ps.__setitem__("bindsource", "" if v == "(none)" else v),
                          ["(none)"] + [c.objectName() for c in _bound_sources(d)],
                          tip="The data-bound control (usually a DataGridView) that the navigator moves "
                              "through, adds to, deletes from and saves."))
    if tool == "GroupBox":
        specs.append(Prop("Behavior", "CheckBox", "bool", w.isCheckable, w.setCheckable,
                          tip="Show a check box in the title that enables/disables the contents."))
    if tool == "LinkLabel":
        specs.append(Prop("Appearance", "Text", "str", lambda: ps.get("linktext", ""),
                          lambda v: M.set_text(w, v), tip="Link text."))
        specs.append(Prop("Behavior", "LinkUrl", "str", lambda: ps.get("link", ""),
                          lambda v: (ps.__setitem__("link", v), M.set_text(w, ps.get("linktext", ""))),
                          tip="Web address opened when clicked (leave empty to handle linkActivated "
                              "in code instead)."))
        specs.append(Prop("Layout", "AutoSize", "bool", lambda: ps.get("autosize", False), psset("autosize")))
    if tool == "MaskedTextBox":
        specs += [
            Prop("Behavior", "Mask", "str", w.inputMask, w.setInputMask,
                 tip="9 = digit, A = letter or digit, L = letter. Examples:  (999) 000-0000  phone,"
                     "  00/00/0000  date,  000-00-0000  SSN,  >LLL-999  plate (> = uppercase)."),
            Prop("Appearance", "Text", "str", w.text, w.setText),
        ]
    if tool == "TreeView":
        specs.append(Prop("Data", "Nodes", "multiline", lambda: M.tree_to_text(w),
                          lambda v: M.set_tree_from_text(w, v),
                          tip="One node per line; indent 4 spaces to put a node under the one above."))
    if tool == "ListView":
        specs += [
            Prop("Data", "Columns", "items", lambda: ps.get("columns", []),
                 lambda v: M.set_listview_columns(w, v), tip="Column headers, one per line."),
            Prop("Data", "Items", "items", lambda: M.listview_rows(w), lambda v: M.set_listview_rows(w, v),
                 tip="One row per line; separate the columns with |   e.g.  Rob | Omaha | NE"),
            Prop("Appearance", "AlternatingRowColors", "bool", w.alternatingRowColors, w.setAlternatingRowColors),
        ]
    if tool == "TabControl":
        specs += [
            Prop("Data", "TabPages", "items", lambda: [w.tabText(i) for i in range(w.count())],
                 lambda v: d.set_tab_pages(w, v),
                 tip="Page titles, one per line. Add a line to add a page; remove one to delete that "
                     "page (and its controls)."),
            Prop("Behavior", "SelectedIndex", "int", w.currentIndex, w.setCurrentIndex, (0, 99),
                 tip="Page shown first. In the designer, click a tab to switch pages."),
            Prop("Appearance", "Alignment", "enum",
                 lambda: next((k for k, v in M.TAB_POSITIONS.items()
                               if getattr(w.TabPosition, v) == w.tabPosition()), "Top"),
                 lambda v: w.setTabPosition(getattr(w.TabPosition, M.TAB_POSITIONS[v])),
                 list(M.TAB_POSITIONS)),
        ]
    if tool == "SplitContainer":
        def set_orient(v):
            w.setOrientation(Qt.Horizontal if v == "Vertical" else Qt.Vertical)
            total = (w.width() if w.orientation() == Qt.Horizontal else w.height()) - w.handleWidth()
            w.setSizes([total // 2, total - total // 2])

        def set_dist(v):
            ps["split"] = v
            M.apply_visuals(w, pd)
        specs += [
            Prop("Appearance", "Orientation", "enum",
                 lambda: "Vertical" if w.orientation() == Qt.Horizontal else "Horizontal", set_orient,
                 ["Vertical", "Horizontal"],
                 tip="Vertical = panels side by side (a vertical splitter bar), like WinForms."),
            Prop("Layout", "SplitterDistance", "int", lambda: (w.sizes() or [0])[0], set_dist, (10, 4000),
                 tip="Size of the first panel in pixels."),
        ]
    return specs


def toolstrip_specs(d):
    tb = d.toolbar

    def set_style(v):
        tb["style"] = v
    return [
        Prop("Design", "(Name)", "readonly", lambda: tb.get("name", ""), None),
        Prop("Appearance", "DisplayStyle", "enum", lambda: tb.get("style", "ImageAndText"), set_style,
             ["ImageAndText", "Image", "Text"], tip="How the buttons show: picture, text, or both."),
    ]


def toolitem_specs(d, item):
    def setp(k):
        return lambda v: item.__setitem__(k, v)
    return [
        Prop("Design", "(Name)", "toolname", lambda: item.get("name", ""), None,
             tip=f"Use it in code as self.ui.{item.get('name', '')}"),
        Prop("Appearance", "Text", "str", lambda: item.get("text", ""), setp("text")),
        Prop("Appearance", "Image", "file", lambda: item.get("icon", ""), setp("icon"),
             tip="Button picture (16x16 looks best)."),
        Prop("Behavior", "ToolTipText", "str", lambda: item.get("tooltip", ""), setp("tooltip")),
        Prop("Behavior", "Enabled", "bool", lambda: item.get("enabled", True), setp("enabled")),
        Prop("Behavior", "CheckOnClick", "bool", lambda: item.get("checkable", False), setp("checkable"),
             rebuild=True),
        Prop("Appearance", "Checked", "bool", lambda: item.get("checked", False), setp("checked")),
    ]


def menu_item_specs(d, item, top):
    def setp(k):
        return lambda v: item.__setitem__(k, v)
    specs = [
        Prop("Design", "(Name)", "menuname", lambda: item.get("name", ""), None,
             tip=f"Use it in code as self.ui.{item.get('name', '')}"),
        Prop("Appearance", "Text", "str", lambda: item.get("text", ""), setp("text"),
             tip="Menu text. & before a letter makes it the Alt-key shortcut (&File)."),
    ]
    if not top and not item.get("items"):
        specs += [
            Prop("Misc", "ShortcutKeys", "shortcut", lambda: item.get("shortcut", ""), setp("shortcut"),
                 tip="Click, then press the key combination (e.g. Ctrl+S). Esc clears it."),
            Prop("Behavior", "CheckOnClick", "bool", lambda: item.get("checkable", False), setp("checkable"),
                 tip="The item toggles a check mark when clicked (use the toggled event).", rebuild=True),
            Prop("Appearance", "Checked", "bool", lambda: item.get("checked", False), setp("checked")),
            Prop("Behavior", "Enabled", "bool", lambda: item.get("enabled", True), setp("enabled")),
            Prop("Behavior", "Visible", "bool", lambda: item.get("visible", True), setp("visible"),
                 tip="Shown at runtime (stays visible in the designer, marked ◌)."),
            Prop("Behavior", "ToolTipText", "str", lambda: item.get("tooltip", ""), setp("tooltip")),
            Prop("Appearance", "Image", "file", lambda: item.get("icon", ""), setp("icon"),
                 tip="Icon shown next to the item."),
        ]
    return specs


def component_specs(d, comp):
    p = comp["props"]
    t = comp["type"]

    def setp(k):
        return lambda v: p.__setitem__(k, v)
    specs = [Prop("Design", "(Name)", "compname", lambda: comp["name"], None,
                  tip=f"Use it in code as self.ui.{comp['name']}")]
    if t == "Timer":
        specs += [
            Prop("Behavior", "Enabled", "bool", lambda: p.get("enabled", False), setp("enabled"),
                 tip="Start ticking as soon as the form opens. In code: self.ui.timer1.start() / .stop()"),
            Prop("Behavior", "Interval", "int", lambda: p.get("interval", 100), setp("interval"),
                 (1, 86400000), tip="Milliseconds between ticks (1000 = 1 second)."),
        ]
    if t in ("OpenFileDialog", "SaveFileDialog"):
        specs += [
            Prop("Appearance", "Title", "str", lambda: p.get("title", ""), setp("title"),
                 tip="Dialog window title."),
            Prop("Behavior", "Filter", "str", lambda: p.get("filter", ""), setp("filter"),
                 tip="File types, e.g.  Text files (*.txt);;All files (*)"),
            Prop("Behavior", "InitialDirectory", "str", lambda: p.get("initialDirectory", ""),
                 setp("initialDirectory"), tip="Folder the dialog starts in."),
            Prop("Behavior", "FileName", "str", lambda: p.get("fileName", ""), setp("fileName"),
                 tip="Default file name; after show_dialog() it holds the chosen file."),
        ]
    if t == "OpenFileDialog":
        specs.append(Prop("Behavior", "Multiselect", "bool", lambda: p.get("multiselect", False),
                          setp("multiselect"), tip="Allow picking several files (see .file_names)."))
    if t == "FolderBrowserDialog":
        specs += [
            Prop("Appearance", "Description", "str", lambda: p.get("description", ""), setp("description")),
            Prop("Behavior", "SelectedPath", "str", lambda: p.get("selectedPath", ""), setp("selectedPath")),
        ]
    if t == "ColorDialog":
        specs.append(Prop("Data", "Color", "color", lambda: p.get("color"), setp("color"),
                          tip="Initial color; after show_dialog() .color holds the chosen color."))
    if t == "PrintDocument":
        specs += [
            Prop("Behavior", "DocumentName", "str", lambda: p.get("documentName", "document"),
                 setp("documentName"), tip="Name shown in the printer queue."),
            Prop("Behavior", "Landscape", "bool", lambda: p.get("landscape", False), setp("landscape"),
                 tip="Print sideways."),
        ]
    if t in ("PrintDialog", "PrintPreviewDialog", "PageSetupDialog"):
        docs = [c["name"] for c in d.components if c["type"] == "PrintDocument"]
        specs.append(Prop("Data", "Document", "enum", lambda: p.get("document") or "(none)",
                          lambda v: p.__setitem__("document", "" if v == "(none)" else v), ["(none)"] + docs,
                          tip="The PrintDocument it prints / previews / sets up."))
    if t == "PrintPreviewDialog":
        specs.append(Prop("Appearance", "Text", "str", lambda: p.get("title", "Print Preview"), setp("title"),
                          tip="Window title."))
    if t == "ContextMenuStrip":
        specs.append(Prop("Data", "Items", "menuitems", lambda: p.get("items") or [],
                          lambda v: p.__setitem__("items", M.context_menu_items(v)),
                          tip="The menu's items: one per line, indent for a submenu, - for a separator, "
                              "a shortcut in brackets: &Copy  [Ctrl+C]"))
    if t == "NotifyIcon":
        menus = [c["name"] for c in d.components if c["type"] == "ContextMenuStrip"]
        specs += [
            Prop("Appearance", "Text", "str", lambda: p.get("text", ""), setp("text"),
                 tip="Tooltip shown when the mouse is over the tray icon."),
            Prop("Appearance", "Icon", "file", lambda: p.get("icon", ""), setp("icon"),
                 tip="The tray icon (a .png or .ico in the project). Empty = the app's icon."),
            Prop("Behavior", "Visible", "bool", lambda: p.get("visible", True), setp("visible"),
                 tip=f"Show the icon as soon as the form opens. In code: self.ui.{comp['name']}.show() / .hide()"),
            Prop("Behavior", "ContextMenuStrip", "enum", lambda: p.get("menu") or "(none)",
                 lambda v: p.__setitem__("menu", "" if v == "(none)" else v), ["(none)"] + menus,
                 tip="The menu shown when the tray icon is right-clicked."),
            Prop("Appearance", "BalloonTipTitle", "str", lambda: p.get("balloonTitle", ""), setp("balloonTitle"),
                 tip="Default title for show_balloon()."),
            Prop("Appearance", "BalloonTipText", "str", lambda: p.get("balloonText", ""), setp("balloonText"),
                 tip="Default text for show_balloon()."),
        ]
    if t == "ErrorProvider":
        specs.append(Prop("Appearance", "Icon", "file", lambda: p.get("icon", ""), setp("icon"),
                          tip="Picture shown next to a control with an error. Empty = a red !"))
    if t == "BindingSource":
        from . import mygen
        queries = list(mygen.query_files(d.project_dir)) if d.project_dir else []
        specs += [
            Prop("Data", "DataSource", "combo", lambda: p.get("dataSource", ""), setp("dataSource"), queries,
                 tip="A saved query (or a SELECT) that gives the rows. Loaded when the form opens."),
            Prop("Data", "Filter", "str", lambda: p.get("filter", ""), setp("filter"),
                 tip="Only rows that match, like VB:  Status = 'Active' AND Score >= 80  (LIKE, IN, IS NULL work)"),
            Prop("Data", "Sort", "str", lambda: p.get("sort", ""), setp("sort"),
                 tip="Order, like VB:  Name ASC, Score DESC"),
        ]
    if t == "ImageList":
        specs += [
            Prop("Data", "Images", "filelist", lambda: list(p.get("images", [])), setp("images"),
                 tip="Pictures in the list (copied into the project's images folder)."),
            Prop("Appearance", "ImageSize", "int", lambda: p.get("imageSize", 16), setp("imageSize"), (8, 256),
                 tip="Width and height of the images, in pixels."),
        ]
    return specs


USAGE = {
    "Timer": "Code: self.ui.{n}.start(), self.ui.{n}.stop(). Use ⚡ Events for its tick (timeout).",
    "OpenFileDialog": "Code: if self.ui.{n}.show_dialog():  path = self.ui.{n}.file_name",
    "SaveFileDialog": "Code: if self.ui.{n}.show_dialog():  path = self.ui.{n}.file_name",
    "FolderBrowserDialog": "Code: if self.ui.{n}.show_dialog():  folder = self.ui.{n}.selected_path",
    "ColorDialog": "Code: if self.ui.{n}.show_dialog():  color = self.ui.{n}.color   (e.g. '#ff0000')",
    "FontDialog": "Code: if self.ui.{n}.show_dialog():  self.ui.label1.setFont(self.ui.{n}.font)",
    "ImageList": "Code: self.ui.{n}.icon(0) gives the first image as a QIcon, e.g. "
                 "item.setIcon(0, self.ui.{n}.icon(0)) for a TreeView node.",
    "ContextMenuStrip": "Set a control's ContextMenuStrip property to {n}. Code: ⚡ Events > triggered(action): "
                        "if action is self.ui.actionCopy: ...  ({n}.source_control = the control right-clicked)",
    "NotifyIcon": "Code: self.ui.{n}.show_balloon('Title', 'Message'); self.ui.{n}.text = 'tooltip'. "
                  "⚡ Events: doubleClicked, clicked.",
    "ErrorProvider": "Code: self.ui.{n}.set_error(self.ui.textBox1, 'Required')  ('' clears it); "
                     "self.ui.{n}.has_errors()",
    "BindingSource": "Code: self.ui.{n}.bind(self.ui.textBox1, 'Name'); self.ui.{n}.fill(self.ui.dataGridView1); "
                     ".move_next(), .current.Name, .position, .filter, .sort, .refresh()",
}


SHARED_EXCLUDE = {"(Name)", "Text", "Left", "Top", "Items", "Columns", "Image", "Tag"}


# ============================================================ panel
class PropertiesPanel(QWidget):
    event_requested = Signal(str, str, str)   # control, signal, handler arg

    def __init__(self, parent=None):
        super().__init__(parent)
        self.designer = None
        self.mode = "props"
        self.sort = "cat"
        self.handler_lookup = None   # MainWindow: designer -> {(ctrl, signal): handler}
        self._pending = {}           # QLineEdit -> undo snapshot while typing
        self.geo_editors = {}
        self.descs = {}

        self.header = QLabel("")
        self.header.setStyleSheet("padding: 4px 6px; background: #3f3f46; color: #fff;")

        def tbtn(text, tip):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setStyleSheet("QToolButton { padding: 3px 8px; color: #ccc; }"
                            "QToolButton:checked { background: #007acc; color: #fff; }")
            return b
        self.b_cat = tbtn("Categorized", "Group properties by category")
        self.b_alpha = tbtn("A-Z", "Sort properties alphabetically")
        self.b_props = tbtn("Properties", "Show properties")
        self.b_events = tbtn("⚡ Events", "Show events")
        self.b_cat.setChecked(True)
        self.b_props.setChecked(True)
        self.b_cat.clicked.connect(lambda: self.set_sort("cat"))
        self.b_alpha.clicked.connect(lambda: self.set_sort("alpha"))
        self.b_props.clicked.connect(lambda: self.set_mode("props"))
        self.b_events.clicked.connect(lambda: self.set_mode("events"))
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(2, 2, 2, 2)
        bl.setSpacing(2)
        for b in (self.b_cat, self.b_alpha):
            bl.addWidget(b)
        bl.addStretch()
        for b in (self.b_props, self.b_events):
            bl.addWidget(b)

        self.table = QTableWidget(0, 2)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 140)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setStyleSheet("QTableWidget { background: #252526; gridline-color: #3f3f46; border: 0; }")

        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setMinimumHeight(54)
        self.desc.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.desc.setStyleSheet("color: #bbbbbb; padding: 6px; border-top: 1px solid #3f3f46;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(bar)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.desc)

    # ------------------------------------------------------------ wiring
    def set_mode(self, mode):
        self.mode = mode
        self.b_props.setChecked(mode == "props")
        self.b_events.setChecked(mode == "events")
        self.rebuild()

    def set_sort(self, sort):
        self.sort = sort
        self.b_cat.setChecked(sort == "cat")
        self.b_alpha.setChecked(sort == "alpha")
        self.rebuild()

    def set_designer(self, d):
        self.flush()
        if self.designer is not None:
            for sig, slot in ((self.designer.selection_changed, self.rebuild),
                              (self.designer.geometry_changed, self.sync_geometry)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.designer = d
        if d is not None:
            d.selection_changed.connect(self.rebuild)
            d.geometry_changed.connect(self.sync_geometry)
        self.rebuild()

    def flush(self):
        """Commit a field that is still being typed in (e.g. before F5 / Save)."""
        for e in list(self._pending):
            try:
                e.editingFinished.emit()
            except RuntimeError:
                self._pending.pop(e, None)

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.FocusIn, QEvent.Enter) and obj in self.descs:
            name, tip = self.descs[obj]
            self.desc.setText(f"<b>{name}</b><br>{tip}" if tip else f"<b>{name}</b>")
        return False

    # ---------------------------------------------------------- change
    def _change(self, prop_setters, value, rebuild=False):
        d = self.designer
        d.change(lambda: [s(value) for s in prop_setters])
        if rebuild:
            self.rebuild()

    # ---------------------------------------------------------- editors
    def _editor(self, prop, setters):
        kind = prop.kind
        value = prop.get()
        change = lambda v: self._change(setters, v, prop.rebuild)   # noqa: E731

        if kind == "readonly":
            e = QLabel(" " + str(value))
            e.setStyleSheet("color: #9a9a9a;")
            return e
        if kind == "name":
            return self._name_editor(prop)
        if kind == "compname":
            return self._comp_name_editor(prop)
        if kind == "menuname":
            return self._menu_name_editor(prop)
        if kind == "shortcut":
            from PySide6.QtGui import QKeySequence
            from PySide6.QtWidgets import QKeySequenceEdit
            ks = QKeySequenceEdit(QKeySequence(value or ""))
            try:
                ks.setClearButtonEnabled(True)
                ks.setMaximumSequenceLength(1)
            except AttributeError:
                pass
            ks.editingFinished.connect(lambda: change(ks.keySequence().toString(QKeySequence.PortableText)))
            return ks
        if kind == "str":
            e = QLineEdit(value or "")
            d = self.designer

            def typed(text):
                if e not in self._pending:
                    self._pending[e] = d.to_xml_string()
                for s in setters:
                    s(text)
                d._set_dirty(True)
                d.overlay.update()

            def finished():
                snap0 = self._pending.pop(e, None)
                if snap0 is not None and d.to_xml_string() != snap0:
                    d.commit(snap0)
            e.textEdited.connect(typed)
            e.editingFinished.connect(finished)
            return e
        if kind == "menuitems":                  # ContextMenuStrip items: the menu editor
            def count(items):
                return sum(0 if it.get("sep") else 1 + count(it.get("items") or []) for it in items)
            b = QPushButton(f"(Collection) {count(value or [])} item(s)")
            b.setStyleSheet("text-align: left; padding: 2px 6px;")

            def edit_menu():
                from .designer import MenuEditor
                dlg = MenuEditor(M.menu_to_text(value) or "&Copy  [Ctrl+C]\n&Paste  [Ctrl+V]\n-\n&Delete", self)
                dlg.setWindowTitle(f"Edit {prop.name}")
                if dlg.exec() == QDialog.Accepted:
                    self._change(setters, M.menu_from_text(dlg.edit.toPlainText(), value) or [], True)
            b.clicked.connect(edit_menu)
            return b
        if kind in ("multiline", "items"):
            text = "\n".join(value) if kind == "items" else (value or "")
            shown = (f"(Collection) {len(value)} item(s)" if kind == "items"
                     else (text.replace("\n", " ⏎ ")[:40] or "(none)"))
            b = QPushButton(shown)
            b.setStyleSheet("text-align: left; padding: 2px 6px;")

            def edit():
                t, ok = QInputDialog.getMultiLineText(self, prop.name, prop.tip or prop.name, text)
                if ok:
                    v = [s for s in t.split("\n") if s.strip()] if kind == "items" else t
                    self._change(setters, v, True)
            b.clicked.connect(edit)
            return b
        if kind in ("int", "geoX", "geoY", "geoW", "geoH"):
            lo, hi = prop.opts or (-999999, 999999)
            s = QSpinBox()
            s.setRange(lo, hi)
            s.setValue(int(value))
            s.setKeyboardTracking(False)
            s.valueChanged.connect(change)
            if kind.startswith("geo"):
                self.geo_editors[kind[3:]] = s
            return s
        if kind == "bool":
            c = QCheckBox("True" if value else "False")
            c.setChecked(bool(value))
            c.setStyleSheet("padding-left: 6px;")
            c.toggled.connect(lambda v: (c.setText("True" if v else "False"), change(v)))
            return c
        if kind == "enum":
            cb = QComboBox()
            cb.addItems(prop.opts)
            cb.setCurrentText(value)
            cb.currentTextChanged.connect(change)
            return cb
        if kind == "combo":     # a list to pick from, or type your own
            cb = QComboBox()
            cb.setEditable(True)
            cb.addItems(prop.opts)
            cb.setCurrentText(value)
            cb.lineEdit().editingFinished.connect(lambda: change(cb.currentText()))
            cb.activated.connect(lambda _i: change(cb.currentText()))
            return cb
        if kind == "color":
            return self._color_editor(value, change)
        if kind == "font":
            return self._font_editor(value, change)
        if kind == "file":
            return self._file_editor(value, change)
        if kind == "filelist":
            return self._filelist_editor(value, change)
        if kind == "toolname":
            return self._tool_name_editor(prop)
        if kind == "anchor":
            return self._anchor_editor(value, change)
        if kind == "size":
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(2)
            sw, sh = QSpinBox(), QSpinBox()
            for s, v in ((sw, value[0]), (sh, value[1])):
                s.setRange(0, 10000)
                s.setValue(v)
                s.setKeyboardTracking(False)
                h.addWidget(s)
            sw.valueChanged.connect(lambda _v: change((sw.value(), sh.value())))
            sh.valueChanged.connect(lambda _v: change((sw.value(), sh.value())))
            return box
        return QLabel(str(value))

    def _name_editor(self, prop):
        d = self.designer
        w = d.selected
        e = QLineEdit(prop.get())

        def do_rename():
            self._pending.pop(e, None)
            if e.text().strip() == w.objectName():
                return
            snapshot = d.to_xml_string()
            err = d.rename(w, e.text())
            if err:
                QMessageBox.warning(self, "Rename", err)
                e.setText(w.objectName())
            else:
                self.header.setText(f"  {w.objectName()}   {M.tool_of(w) or w._ps_cls}")
                d.commit(snapshot)
        e.textEdited.connect(lambda _t: self._pending.setdefault(e, None))
        e.editingFinished.connect(do_rename)
        return e

    def _menu_name_editor(self, prop):
        from .menudesigner import item_at
        d = self.designer
        item = item_at(d.menu, d.menu_sel)
        e = QLineEdit(prop.get())

        def do_rename():
            self._pending.pop(e, None)
            if item is None or e.text().strip() == item.get("name"):
                return
            snapshot = d.to_xml_string()
            err = d.rename_menu_item(item, e.text())
            if err:
                QMessageBox.warning(self, "Rename", err)
                e.setText(item.get("name", ""))
            else:
                self.header.setText(f"  {item['name']}   ToolStripMenuItem")
                d.commit(snapshot)
        e.textEdited.connect(lambda _t: self._pending.setdefault(e, None))
        e.editingFinished.connect(do_rename)
        return e

    def _comp_name_editor(self, prop):
        d = self.designer
        comp = d.find_component(d.special[5:])
        e = QLineEdit(prop.get())

        def do_rename():
            self._pending.pop(e, None)
            if e.text().strip() == comp["name"]:
                return
            snapshot = d.to_xml_string()
            err = d.rename_component(comp, e.text())
            if err:
                QMessageBox.warning(self, "Rename", err)
                e.setText(comp["name"])
            else:
                self.header.setText(f"  {comp['name']}   {comp['type']}")
                d.commit(snapshot)
        e.textEdited.connect(lambda _t: self._pending.setdefault(e, None))
        e.editingFinished.connect(do_rename)
        return e

    def _color_editor(self, value, change):
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)
        b = QPushButton(value or "(default)")
        sw = value or "transparent"
        b.setStyleSheet(f"text-align: left; padding: 2px 6px; border-left: 14px solid {sw};")

        def pick():
            c = QColorDialog.getColor(QColor(value) if value else QColor("#ffffff"), self, "Choose color")
            if c.isValid():
                change(c.name())
                self.rebuild()
        b.clicked.connect(pick)
        r = QToolButton()
        r.setText("✕")
        r.setToolTip("Reset to default")
        r.clicked.connect(lambda: (change(None), self.rebuild()))
        h.addWidget(b, 1)
        h.addWidget(r)
        return box

    def _font_editor(self, value, change):
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)
        b = QPushButton(M.font_text(value))
        b.setStyleSheet("text-align: left; padding: 2px 6px;")
        d = self.designer

        def pick():
            base = QFont(d.surface.font()) if d.selected is not None else QFont()
            cur = QFont(base)
            cur = cur.resolve(base)
            if value:
                f = M.font_from_dict(value)
                cur = f.resolve(base)
            ok, f = QFontDialog.getFont(cur, self, "Font")
            if isinstance(ok, QFont):   # PySide returns (font, ok) or (ok, font) by version
                ok, f = f, ok
            if ok:
                change(M.font_from_qfont(f, base))
                self.rebuild()
        b.clicked.connect(pick)
        r = QToolButton()
        r.setText("✕")
        r.setToolTip("Inherit the parent's font")
        r.clicked.connect(lambda: (change(None), self.rebuild()))
        h.addWidget(b, 1)
        h.addWidget(r)
        return box

    def _file_editor(self, value, change):
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)
        b = QPushButton(os.path.basename(value) if value else "(none)")
        b.setStyleSheet("text-align: left; padding: 2px 6px;")
        d = self.designer

        def pick():
            path, _ = QFileDialog.getOpenFileName(self, "Choose image", d.project_dir,
                                                  "Images (*.png *.jpg *.jpeg *.bmp *.gif *.svg *.ico)")
            if not path:
                return
            rel = os.path.relpath(path, d.project_dir)
            if rel.startswith(".."):
                # Copy outside images into the project, like VS's Resources
                os.makedirs(os.path.join(d.project_dir, "images"), exist_ok=True)
                dest = os.path.join(d.project_dir, "images", os.path.basename(path))
                if not os.path.exists(dest):
                    shutil.copy2(path, dest)
                rel = os.path.relpath(dest, d.project_dir)
            change(rel.replace(os.sep, "/"))
            self.rebuild()
        b.clicked.connect(pick)
        r = QToolButton()
        r.setText("✕")
        r.setToolTip("No image")
        r.clicked.connect(lambda: (change(""), self.rebuild()))
        h.addWidget(b, 1)
        h.addWidget(r)
        return box

    def _filelist_editor(self, value, change):
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout as HB, QListWidget,
                                       QVBoxLayout as VB)
        b = QPushButton(f"(Collection) {len(value)} image(s)")
        b.setStyleSheet("text-align: left; padding: 2px 6px;")
        d = self.designer

        def edit():
            dlg = QDialog(self)
            dlg.setWindowTitle("Images Collection Editor")
            dlg.resize(460, 360)
            lst = QListWidget()
            lst.addItems(value)
            add, rem, up, dn = (QPushButton(t) for t in ("Add...", "Remove", "Up", "Down"))

            def do_add():
                paths, _ = QFileDialog.getOpenFileNames(dlg, "Add images", d.project_dir,
                                                        "Images (*.png *.jpg *.jpeg *.bmp *.gif *.svg *.ico)")
                for path in paths:
                    rel = os.path.relpath(path, d.project_dir)
                    if rel.startswith(".."):
                        os.makedirs(os.path.join(d.project_dir, "images"), exist_ok=True)
                        dest = os.path.join(d.project_dir, "images", os.path.basename(path))
                        if not os.path.exists(dest):
                            shutil.copy2(path, dest)
                        rel = os.path.relpath(dest, d.project_dir)
                    lst.addItem(rel.replace(os.sep, "/"))

            def move(delta):
                r = lst.currentRow()
                j = r + delta
                if 0 <= r < lst.count() and 0 <= j < lst.count():
                    it = lst.takeItem(r)
                    lst.insertItem(j, it)
                    lst.setCurrentRow(j)
            add.clicked.connect(do_add)
            rem.clicked.connect(lambda: lst.takeItem(lst.currentRow()))
            up.clicked.connect(lambda: move(-1))
            dn.clicked.connect(lambda: move(1))
            side = VB()
            for x in (add, rem, up, dn):
                side.addWidget(x)
            side.addStretch()
            row = HB()
            row.addWidget(lst, 1)
            row.addLayout(side)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(dlg.accept)
            bb.rejected.connect(dlg.reject)
            lay = VB(dlg)
            lay.addLayout(row)
            lay.addWidget(bb)
            if dlg.exec():
                change([lst.item(i).text() for i in range(lst.count())])
                self.rebuild()
        b.clicked.connect(edit)
        return b

    def _tool_name_editor(self, prop):
        d = self.designer
        item = d.tool_item()
        e = QLineEdit(prop.get())

        def do_rename():
            self._pending.pop(e, None)
            if item is None or e.text().strip() == item.get("name"):
                return
            snapshot = d.to_xml_string()
            err = d.rename_tool_item(item, e.text())
            if err:
                QMessageBox.warning(self, "Rename", err)
                e.setText(item.get("name", ""))
            else:
                d.commit(snapshot)
                d.tool_strip.update()
        e.textEdited.connect(lambda _t: self._pending.setdefault(e, None))
        e.editingFinished.connect(do_rename)
        return e

    def _anchor_editor(self, value, change):
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(2, 0, 0, 0)
        h.setSpacing(2)
        sides = {s.strip() for s in (value or "").split(",") if s.strip()}
        btns = {}
        for side in ("Top", "Bottom", "Left", "Right"):
            t = QToolButton()
            t.setText(side)
            t.setCheckable(True)
            t.setChecked(side in sides)
            t.setStyleSheet("QToolButton { padding: 1px 4px; color: #aaa; }"
                            "QToolButton:checked { background: #007acc; color: #fff; }")
            btns[side] = t
            h.addWidget(t)
        h.addStretch()

        def changed():
            chosen = [s for s in ("Top", "Bottom", "Left", "Right") if btns[s].isChecked()]
            change(", ".join(chosen) if chosen else "None")
        for t in btns.values():
            t.toggled.connect(lambda _v: changed())
        return box

    def _add_row(self, label, editor, tip=""):
        r = self.table.rowCount()
        self.table.insertRow(r)
        item = QLabel(" " + label)
        item.setStyleSheet("color: #dcdcdc;")
        item.setToolTip(tip)
        self.table.setCellWidget(r, 0, item)
        self.table.setCellWidget(r, 1, editor)
        for w in [editor] + editor.findChildren(QWidget):
            self.descs[w] = (label, tip)
            w.installEventFilter(self)
        self.descs[item] = (label, tip)
        item.installEventFilter(self)

    def _add_category(self, name):
        r = self.table.rowCount()
        self.table.insertRow(r)
        lab = QLabel("  ▾ " + name)
        lab.setStyleSheet("background: #333337; color: #ffffff; font-weight: bold;")
        self.table.setCellWidget(r, 0, lab)
        filler = QLabel("")
        filler.setStyleSheet("background: #333337;")
        self.table.setCellWidget(r, 1, filler)

    # ---------------------------------------------------------- build
    def rebuild(self):
        self.flush()
        self.table.setRowCount(0)
        self.geo_editors = {}
        self.descs = {}
        self.desc.setText("")
        d = self.designer
        if d is None:
            self.header.setText("  (open a form to see properties)")
            return
        if self.mode == "events":
            self._build_events()
            return
        if d.special == "menu":
            self.header.setText("  menubar   MenuStrip")
            b = QPushButton("Edit as text...")
            b.clicked.connect(d.edit_menu)
            self._add_row("Items", b, "Advanced: edit the whole menu as indented text.")
            self.desc.setText("Click 'Type Here' on the menu bar to add menus and items. Select an "
                              "item to set its properties; double-click an item to write its code. "
                              "Delete removes the whole MenuStrip.")
            return
        if d.special == "menuitem":
            from .menudesigner import item_at
            item = item_at(d.menu, d.menu_sel)
            if item is None:
                return
            if item.get("sep"):
                self.header.setText("  separator   ToolStripSeparator")
                self.desc.setText("Delete removes the separator; right-click for Move Up/Down.")
                return
            top = len(d.menu_sel) == 1
            self.header.setText(f"  {item.get('name', '')}   {'ToolStripMenuItem'}")
            specs = [(p, [p.set]) for p in menu_item_specs(d, item, top or bool(item.get("items")))]
            for cat in sorted({p.cat for p, _ in specs}):
                if self.sort == "cat":
                    self._add_category(cat)
                for p, setters in sorted((x for x in specs if x[0].cat == cat), key=lambda x: x[0].name.lstrip("(").lower()):
                    self._add_row(p.name, self._editor(p, setters), p.tip)
            self.desc.setText("F2 edits the text in place; Del deletes; Ctrl+Up/Down moves; "
                              "double-click the item to write its code.")
            return
        if d.special and d.special.startswith("comp:"):
            comp = d.find_component(d.special[5:])
            if comp is None:
                return
            self.header.setText(f"  {comp['name']}   {comp['type']}")
            specs = [(p, [p.set]) for p in component_specs(d, comp)]
            for p, setters in sorted(specs, key=lambda x: (x[0].cat, x[0].name.lstrip("("))):
                self._add_row(p.name, self._editor(p, setters), p.tip)
            self.desc.setText(USAGE.get(comp["type"], "").format(n=comp["name"]))
            return
        if d.special in ("toolstrip", "toolitem"):
            if d.special == "toolstrip":
                self.header.setText(f"  {d.toolbar.get('name', '')}   ToolStrip")
                specs = toolstrip_specs(d)
                self.desc.setText("Click '+ Add' on the toolbar for buttons, separators, or a menu item's "
                                  "button. Delete removes the whole ToolStrip.")
            else:
                item = d.tool_item()
                if item is None:
                    return
                if item.get("sep"):
                    self.header.setText("  separator   ToolStripSeparator")
                    self.desc.setText("Delete removes it; Ctrl+Left/Right moves it.")
                    return
                if item.get("ref"):
                    self.header.setText(f"  {item['ref']}   (menu item)")
                    self.desc.setText(f"This button shares the menu item {item['ref']}: same text, picture "
                                      "and code. Change them on the menu item.")
                    return
                self.header.setText(f"  {item.get('name', '')}   ToolStripButton")
                specs = toolitem_specs(d, item)
                self.desc.setText("Double-click the button to write its code. Ctrl+Left/Right moves it.")
            for p in sorted(specs, key=lambda x: (x.cat, x.name.lstrip("("))):
                self._add_row(p.name, self._editor(p, [p.set]), p.tip)
            return
        if d.special == "status":
            self.header.setText("  statusbar   StatusStrip")
            self.desc.setText("Show text from code with self.statusBar().showMessage('Ready'). "
                              "Delete removes the StatusStrip.")
            return

        if not d.selection:
            self.header.setText(f"  {d.form_class}   Form")
            specs = [(p, [p.set]) for p in form_specs(d)]
        elif len(d.selection) == 1:
            w = d.selected
            self.header.setText(f"  {w.objectName()}   {M.tool_of(w) or w._ps_cls}")
            specs = [(p, [p.set]) for p in control_specs(d, w)]
        else:
            self.header.setText(f"  {len(d.selection)} controls selected")
            per = [control_specs(d, c) for c in d.selection]
            prim = {p.name: p for p in control_specs(d, d.selected)}
            specs = []
            for name, p in prim.items():
                if name in SHARED_EXCLUDE:
                    continue
                matches = [next((q for q in lst if q.name == name and q.kind == p.kind), None) for lst in per]
                if all(matches):
                    specs.append((p, [q.set for q in matches]))
        if self.sort == "cat":
            cats = sorted({p.cat for p, _ in specs})
            for cat in cats:
                self._add_category(cat)
                for p, setters in sorted((x for x in specs if x[0].cat == cat), key=lambda x: x[0].name.lower()):
                    self._add_row(p.name, self._editor(p, setters), p.tip)
        else:
            for p, setters in sorted(specs, key=lambda x: x[0].name.lstrip("(").lower()):
                self._add_row(p.name, self._editor(p, setters), p.tip)

    def _events_button(self, handler, control, sig, arg):
        btn = QPushButton(handler or "")
        btn.setToolTip(f"Click to {'go to' if handler else 'create'} the handler")
        btn.setStyleSheet("QPushButton { text-align: left; padding: 2px 6px; border: 0;"
                          " background: #333337; color: #dcdcaa; }"
                          "QPushButton:hover { background: #3e3e42; }")
        btn.clicked.connect(lambda _=False: self.event_requested.emit(control, sig, arg))
        return btn

    def _build_events(self):
        d = self.designer
        existing = self.handler_lookup(d) if self.handler_lookup else {}
        if d.special == "toolitem":
            item = d.tool_item()
            if item is None or item.get("sep"):
                return
            name = item.get("ref") or item["name"]
            self.header.setText(f"  {name}   ToolStripButton")
            evs = [("triggered", "")] + ([("toggled", "checked")] if item.get("checkable") else [])
            for sig, arg in evs:
                h = existing.get((name, sig), "")
                self._add_row(sig, self._events_button(h, name, sig, arg), "Runs when the button is clicked.")
            if item.get("ref"):
                self.desc.setText("Shared with the menu item - one handler runs for both.")
            return
        if d.special == "menuitem":
            from .menudesigner import item_at
            item = item_at(d.menu, d.menu_sel)
            if item is None or item.get("sep") or item.get("items") or len(d.menu_sel) == 1:
                self.desc.setText("Top menus and submenus have no events - select a menu item.")
                return
            self.header.setText(f"  {item['name']}   ToolStripMenuItem")
            evs = [("triggered", "")] + ([("toggled", "checked")] if item.get("checkable") else [])
            for sig, arg in evs:
                h = existing.get((item["name"], sig), "")
                self._add_row(sig, self._events_button(h, item["name"], sig, arg),
                              "Runs when the item is clicked (VS: Click)." if sig == "triggered" else
                              "Runs when the check mark changes.")
            self.desc.setText("Click an event to create its handler, or to jump to it if it exists.")
            return
        if d.special == "menu":
            self.header.setText("  menubar   MenuStrip")
            acts = M.all_actions(d.menu)
            for name, text in acts:
                h = existing.get((name, "triggered"), "")
                self._add_row(f"{text}", self._events_button(h, name, "triggered", ""),
                              f"{name}.triggered - runs when the menu item is clicked")
            self.desc.setText("Click a menu item to create or go to its handler.")
            return
        if d.special and d.special.startswith("comp:"):
            comp = d.find_component(d.special[5:])
            if comp is None:
                return
            self.header.setText(f"  {comp['name']}   {comp['type']}")
            evs = M.EVENTS.get(comp["type"], [])
            for sig, arg in evs:
                h = existing.get((comp["name"], sig), "")
                self._add_row(sig, self._events_button(h, comp["name"], sig, arg),
                              "Runs every Interval milliseconds while the timer is running."
                              if sig == "timeout" else "")
            self.desc.setText("Click an event to create its handler." if evs else
                              USAGE.get(comp["type"], "").format(n=comp["name"]))
            return
        if len(d.selection) != 1:
            self.header.setText(f"  {d.form_class}   Form" if not d.selection else
                                f"  {len(d.selection)} controls selected")
            self.desc.setText("Form startup code goes in __init__ in the form's .py file "
                              "(it runs like Form_Load). Press F7 to view the code."
                              if not d.selection else "Select one control to see its events.")
            return
        w = d.selected
        tool = M.tool_of(w)
        self.header.setText(f"  {w.objectName()}   {tool or w._ps_cls}")
        events = M.EVENTS.get(tool, [])
        if not events:
            self.desc.setText("This control has no events.")
            return
        for sig, arg in events:
            h = existing.get((w.objectName(), sig), "")
            self._add_row(sig, self._events_button(h, w.objectName(), sig, arg),
                          f"Handler arguments: {arg}" if arg else "")
        self.desc.setText("Click an event to create its handler, or to jump to it if it exists.")

    def sync_geometry(self):
        """Live-update Left/Top/Width/Height while dragging, without undo steps."""
        d = self.designer
        if d is None:
            return
        if not d.selection:
            vals = {"W": d.form_size.width(), "H": d.form_size.height()}
        elif len(d.selection) == 1:
            g = d.selected.geometry()
            vals = {"X": g.x(), "Y": g.y(), "W": g.width(), "H": g.height()}
        else:
            return
        for k, v in vals.items():
            ed = self.geo_editors.get(k)
            if ed is not None:
                try:
                    if ed.value() != v:
                        ed.blockSignals(True)
                        ed.setValue(v)
                        ed.blockSignals(False)
                except RuntimeError:
                    pass
