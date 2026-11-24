"""Project and form templates (VS: File > New Project, Add > Windows Form)."""
import os

FORM_UI = '''<?xml version="1.0" encoding="UTF-8"?>
<ui version="4.0">
 <class>{cls}</class>
 <widget class="QMainWindow" name="{cls}">
  <property name="geometry">
   <rect>
    <x>0</x>
    <y>0</y>
    <width>800</width>
    <height>450</height>
   </rect>
  </property>
  <property name="windowTitle">
   <string>{cls}</string>
  </property>
  <widget class="QWidget" name="centralwidget"/>
 </widget>
 <resources/>
 <connections/>
</ui>
'''

FORM_PY = '''"""{cls} - your code for this form (like {cls}.vb).

The layout lives in {stem}.ui. PyStudio regenerates ui_{stem}.py from it
automatically - don't edit ui_{stem}.py by hand (like {cls}.Designer.vb).
"""
import pystudio_runtime
from pystudio_runtime import Form
from ui_{stem} import Ui_{cls}


class {cls}(Form):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = Ui_{cls}()
        self.ui.setupUi(self)   # builds the controls (like InitializeComponent)
        pystudio_runtime.setup_form(self)   # Anchor, StartPosition, FormBorderStyle...

        # Event wiring - double-click a control in the designer to add one here
'''

DIALOG_UI = '''<?xml version="1.0" encoding="UTF-8"?>
<ui version="4.0">
 <class>{cls}</class>
 <widget class="QMainWindow" name="{cls}">
  <property name="geometry">
   <rect><x>0</x><y>0</y><width>420</width><height>170</height></rect>
  </property>
  <property name="windowTitle">
   <string>{cls}</string>
  </property>
  <property name="startPosition" stdset="0"><string>CenterParent</string></property>
  <property name="borderStyle" stdset="0"><string>FixedDialog</string></property>
  <property name="maximizeBox" stdset="0"><bool>false</bool></property>
  <property name="minimizeBox" stdset="0"><bool>false</bool></property>
  <property name="acceptButton" stdset="0"><string>okButton</string></property>
  <property name="cancelButton" stdset="0"><string>cancelButton</string></property>
  <widget class="QWidget" name="centralwidget">
   <widget class="QLabel" name="label1">
    <property name="geometry"><rect><x>16</x><y>16</y><width>380</width><height>20</height></rect></property>
    <property name="text"><string>Message</string></property>
   </widget>
   <widget class="QLineEdit" name="textBox1">
    <property name="geometry"><rect><x>16</x><y>44</y><width>388</width><height>26</height></rect></property>
    <property name="anchor" stdset="0"><string>Top, Left, Right</string></property>
   </widget>
   <widget class="QPushButton" name="okButton">
    <property name="geometry"><rect><x>216</x><y>124</y><width>90</width><height>30</height></rect></property>
    <property name="text"><string>OK</string></property>
    <property name="anchor" stdset="0"><string>Bottom, Right</string></property>
    <property name="dialogResult" stdset="0"><string>OK</string></property>
   </widget>
   <widget class="QPushButton" name="cancelButton">
    <property name="geometry"><rect><x>314</x><y>124</y><width>90</width><height>30</height></rect></property>
    <property name="text"><string>Cancel</string></property>
    <property name="anchor" stdset="0"><string>Bottom, Right</string></property>
    <property name="dialogResult" stdset="0"><string>Cancel</string></property>
   </widget>
  </widget>
 </widget>
 <tabstops><tabstop>textBox1</tabstop><tabstop>okButton</tabstop><tabstop>cancelButton</tabstop></tabstops>
 <resources/>
 <connections/>
</ui>
'''

USERCONTROL_UI = '''<?xml version="1.0" encoding="UTF-8"?>
<ui version="4.0">
 <class>{cls}</class>
 <widget class="QMainWindow" name="{cls}">
  <property name="geometry">
   <rect><x>0</x><y>0</y><width>300</width><height>150</height></rect>
  </property>
  <property name="windowTitle">
   <string>{cls}</string>
  </property>
  <property name="formKind" stdset="0"><string>UserControl</string></property>
  <widget class="QWidget" name="centralwidget"/>
 </widget>
 <resources/>
 <connections/>
</ui>
'''

USERCONTROL_PY = '''"""{cls} - a UserControl: design it like a form, then place it on forms from the
Toolbox ("{project} Components"). Each copy is a separate {cls} object:
    self.ui.{inst}.ui.label1.setText("Hi")
"""
import pystudio_runtime
from pystudio_runtime import UserControl
from ui_{stem} import Ui_{cls}


class {cls}(UserControl):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ui = Ui_{cls}()
        self.ui.setupUi(self)
        pystudio_runtime.setup_form(self)

        # Event wiring - double-click a control in the designer to add one here
'''

CLASS_PY = '''"""{cls} - a class (VB: Add > Class)."""


class {cls}:
    def __init__(self):
        pass
'''

MODULE_PY = '''"""{stem} - shared functions and values (VB: Add > Module). Use it with:
    import {stem}
    {stem}.say_hello()
"""


def say_hello():
    print("Hello from {stem}")
'''

MAIN_FORMS = '''"""{name} - startup (like Program.vb / Sub Main)."""
import sys

from PySide6.QtWidgets import QApplication

import pystudio_runtime
from form1 import Form1


def main():
    app = QApplication(sys.argv)
    pystudio_runtime.setup_app(app)   # name, version and icon from the .pyproj
    form = Form1()
    form.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
'''

MAIN_CONSOLE = '''"""{name} - entry point."""


def main():
    print("Hello from {name}!")


if __name__ == "__main__":
    main()
'''

TEMPLATES = ["Windows Forms App", "Data App (Forms + database)", "Console App", "Class Library"]
TEMPLATE_INFO = {
    "Windows Forms App": ("A desktop app with a form (Form1) you design by dragging controls, like VB WinForms.",
                          "WinFormsApp"),
    "Data App (Forms + database)": ("A working database app to learn from: a sample SQLite database, a "
                                    "customers grid you can edit and save, its orders below (master/detail) "
                                    "and a navigator bar.", "DataApp"),
    "Console App": ("A program without windows that prints text - good for scripts and learning Python.",
                    "ConsoleApp"),
    "Class Library": ("Reusable code (a Python package) with tests, to share between your apps.",
                      "ClassLibrary"),
}

LIB_INIT = '''"""{name} - a class library (a Python package). Use it from other code with:
    from {pkg} import Calculator
"""
from .core import Calculator

__all__ = ["Calculator"]
__version__ = "1.0.0"
'''

LIB_CORE = '''"""The library's code. Add classes and functions here (or in more .py files in this folder)."""


class Calculator:
    """A small example class. Replace it with your own."""

    def __init__(self):
        self.history = []

    def add(self, a, b):
        """a + b, remembered in history."""
        result = a + b
        self.history.append(f"{a} + {b} = {result}")
        return result

    def divide(self, a, b):
        """a / b. Raises ZeroDivisionError when b is 0."""
        result = a / b
        self.history.append(f"{a} / {b} = {result}")
        return result
'''

LIB_TEST = '''"""Tests for {pkg} - run them in Test > Test Explorer (Ctrl+E, T)."""
import pytest

from {pkg} import Calculator


def test_add():
    calc = Calculator()
    assert calc.add(2, 3) == 5
    assert calc.history == ["2 + 3 = 5"]


def test_divide_by_zero():
    with pytest.raises(ZeroDivisionError):
        Calculator().divide(1, 0)
'''

LIB_MAIN = '''"""Try out the library (F5 runs this). Apps use it with:  from {pkg} import Calculator"""
from {pkg} import Calculator


def main():
    calc = Calculator()
    print(calc.add(2, 3))
    print(calc.divide(10, 4))
    print(calc.history)


if __name__ == "__main__":
    main()
'''

DATA_FORM_UI = '''<?xml version="1.0" encoding="UTF-8"?>
<ui version="4.0">
 <class>Form1</class>
 <widget class="QMainWindow" name="Form1">
  <property name="geometry">
   <rect><x>0</x><y>0</y><width>820</width><height>560</height></rect>
  </property>
  <property name="windowTitle">
   <string>Customers and Orders</string>
  </property>
  <widget class="QWidget" name="centralwidget">
   <widget class="QFrame" name="bindingNavigator1">
    <property name="geometry"><rect><x>0</x><y>0</y><width>820</width><height>27</height></rect></property>
    <property name="psType" stdset="0"><string>BindingNavigator</string></property>
    <property name="dock" stdset="0"><string>Top</string></property>
    <property name="bindingSource" stdset="0"><string>customersGrid</string></property>
   </widget>
   <widget class="QLabel" name="ordersLabel">
    <property name="geometry"><rect><x>0</x><y>320</y><width>820</width><height>24</height></rect></property>
    <property name="text"><string>  Orders for the selected customer</string></property>
    <property name="dock" stdset="0"><string>Bottom</string></property>
   </widget>
   <widget class="QTableWidget" name="ordersGrid">
    <property name="geometry"><rect><x>0</x><y>344</y><width>820</width><height>216</height></rect></property>
    <property name="dock" stdset="0"><string>Bottom</string></property>
    <property name="dataSource" stdset="0"><string>CustomerOrders</string></property>
    <property name="masterControl" stdset="0"><string>customersGrid</string></property>
    <column><property name="text"><string>OrderID</string></property></column>
    <column><property name="text"><string>ProductID</string></property></column>
    <column><property name="text"><string>Quantity</string></property></column>
    <column><property name="text"><string>OrderDate</string></property></column>
   </widget>
   <widget class="QTableWidget" name="customersGrid">
    <property name="geometry"><rect><x>0</x><y>27</y><width>820</width><height>293</height></rect></property>
    <property name="dock" stdset="0"><string>Fill</string></property>
    <property name="dataSource" stdset="0"><string>Customers</string></property>
    <column><property name="text"><string>CustomerID</string></property></column>
    <column><property name="text"><string>Name</string></property></column>
    <column><property name="text"><string>City</string></property></column>
    <column><property name="text"><string>State</string></property></column>
    <column><property name="text"><string>Phone</string></property></column>
   </widget>
  </widget>
 </widget>
 <resources/>
 <connections/>
</ui>
'''


def default_repos_dir():
    return os.path.join(os.path.expanduser("~"), "repos")


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


ITEM_KINDS = ["Windows Form", "Dialog", "User Control", "Class", "Module"]
_ITEM_BASE = {"Windows Form": "Form", "Dialog": "Dialog", "User Control": "UserControl",
              "Class": "Class", "Module": "Module"}


def add_form(folder, cls, kind="Windows Form", project=""):
    """Create <stem>.ui and <stem>.py for a new form, dialog or user control.
    Returns (ui_path, py_path)."""
    stem = cls.lower()
    ui_path = os.path.join(folder, stem + ".ui")
    py_path = os.path.join(folder, stem + ".py")
    if os.path.exists(ui_path) or os.path.exists(py_path):
        raise FileExistsError(f"{stem}.ui or {stem}.py already exists")
    if kind == "User Control":
        _write(ui_path, USERCONTROL_UI.format(cls=cls))
        _write(py_path, USERCONTROL_PY.format(cls=cls, stem=stem, project=project or "Project",
                                              inst=cls[0].lower() + cls[1:] + "1"))
    else:
        _write(ui_path, (DIALOG_UI if kind == "Dialog" else FORM_UI).format(cls=cls))
        _write(py_path, FORM_PY.format(cls=cls, stem=stem))
    return ui_path, py_path


def add_code_file(folder, name, kind):
    """Class or Module: <stem>.py. Returns its path."""
    stem = name.lower() if kind == "Class" else name
    path = os.path.join(folder, stem + ".py")
    if os.path.exists(path):
        raise FileExistsError(f"{stem}.py already exists")
    _write(path, (CLASS_PY if kind == "Class" else MODULE_PY).format(cls=name, stem=stem))
    return path


def next_item_name(folder, kind="Windows Form"):
    base = _ITEM_BASE.get(kind, "Form")
    n = 1
    while any(os.path.exists(os.path.join(folder, f"{base.lower()}{n}{ext}")) or
              os.path.exists(os.path.join(folder, f"{base}{n}{ext}")) for ext in (".ui", ".py")):
        n += 1
    return f"{base}{n}" if kind != "Module" else f"module{n}"


def next_form_name(folder):
    return next_item_name(folder, "Windows Form")


def create_project(root, name, template):
    """Create the project folder and files. Returns list of files to open."""
    from .project import Project
    os.makedirs(root)
    proj = Project(root)
    proj["external"] = False          # a PyStudio project (even with no forms)
    proj["name"] = name
    proj.save()
    proj.ensure_runtime()
    main_py = os.path.join(root, "main.py")
    if template == "Console App":
        _write(main_py, MAIN_CONSOLE.format(name=name))
        return [main_py]
    if template == "Class Library":
        import re
        pkg = re.sub(r"\W", "_", name.lower()).strip("_") or "mylib"
        if pkg[0].isdigit():
            pkg = "lib_" + pkg
        os.makedirs(os.path.join(root, pkg))
        os.makedirs(os.path.join(root, "tests"))
        _write(os.path.join(root, pkg, "__init__.py"), LIB_INIT.format(name=name, pkg=pkg))
        core = os.path.join(root, pkg, "core.py")
        _write(core, LIB_CORE)
        _write(os.path.join(root, "tests", "test_core.py"), LIB_TEST.format(pkg=pkg))
        _write(main_py, LIB_MAIN.format(pkg=pkg))
        return [core, main_py]
    ui_path, py_path = add_form(root, "Form1")
    _write(main_py, MAIN_FORMS.format(name=name))
    if template.startswith("Data App"):
        from . import dbcore, mygen
        os.makedirs(os.path.join(root, "data"))
        os.makedirs(os.path.join(root, "queries"))
        dbcore.sample_database(os.path.join(root, "data", "sample.db"))
        _write(os.path.join(root, "queries", "Customers.sql"), dbcore.format_query_file(
            "Sample", "SELECT CustomerID, Name, City, State, Phone\nFROM Customers\nORDER BY Name",
            description="All customers (editable: one table with a primary key)"))
        _write(os.path.join(root, "queries", "CustomerOrders.sql"), dbcore.format_query_file(
            "Sample", "SELECT OrderID, ProductID, Quantity, OrderDate\nFROM Orders\n"
            "WHERE CustomerID = :CustomerID\nORDER BY OrderDate", {"CustomerID": 1},
            "One customer's orders (CustomerID comes from the selected customer)"))
        proj["connections"] = [{"name": "Sample", "provider": "SQLite", "file": "data/sample.db"}]
        proj.save()
        _write(ui_path, DATA_FORM_UI)
        mygen.write(proj)
    return [ui_path, py_path, main_py]
