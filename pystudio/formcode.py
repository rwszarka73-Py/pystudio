"""Writing event handlers for a form, in whichever style its code uses:

- PyStudio / pyside6-uic style:  self.ui = Ui_Form1(); self.ui.setupUi(self)   ->  self.ui.<name>.<signal>
- uic.loadUi style (PyQt5/6 or PySide's own loader):  uic.loadUi("mainwindow.ui", self)  ->  self.<name>.<signal>

The connect line goes right after the form is loaded (or after the other connects), the handler at the end of
the class. Returns (file, line of the handler body)."""
import os
import re

from . import codegen


def _py_files(root, limit=400):
    n = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "venv", ".venv", "build",
                                                                         "dist", "publish")]
        for f in files:
            if f.endswith(".py") and not f.startswith("ui_"):
                n += 1
                if n > limit:
                    return
                yield os.path.join(dirpath, f)


def find_code(ui_path, project_root):
    """(py_path, style) - style 'ui' (self.ui) or 'loadui' (self.<name>), or (None, None)."""
    stem = os.path.splitext(ui_path)[0]
    base = os.path.basename(ui_path)
    py = stem + ".py"
    if os.path.exists(py):
        text = open(py, encoding="utf-8", errors="replace").read()
        if "self.ui" in text and "setupUi" in text:
            return py, "ui"
        if "loadUi" in text and base in text:
            return py, "loadui"
    for p in _py_files(project_root or os.path.dirname(ui_path)):
        text = open(p, encoding="utf-8", errors="replace").read()
        if base in text and re.search(r"\bloadUi\s*\(", text):
            return p, "loadui"
        if f"ui_{os.path.basename(stem)}" in text and "self.ui" in text:
            return p, "ui"
    return None, None


def add_handler(ui_path, project_root, control, signal, arg):
    py, style = find_code(ui_path, project_root)
    handler = f"{control}_{signal}"
    if py is None:
        raise ValueError(
            f"PyStudio couldn't find the code that loads {os.path.basename(ui_path)}.\n\n"
            f"Add this where the form is loaded (after uic.loadUi(..., self) or setupUi):\n"
            f"    self.{control}.{signal}.connect(self.{handler})\n\nand this method in the same class:\n"
            f"    def {handler}(self{', ' + arg if arg else ''}):\n        pass")
    with open(py, encoding="utf-8", errors="replace") as f:
        source = f.read()
    if style == "ui":
        new, line = codegen.add_event_handler(source, control, signal, arg)
    else:
        new, line = _add_loadui_handler(source, os.path.basename(ui_path), control, signal, arg)
    if new != source:
        with open(py, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
    return py, line


def _add_loadui_handler(source, ui_base, control, signal, arg):
    handler = f"{control}_{signal}"
    m = re.search(rf"^[ \t]*def {re.escape(handler)}\(", source, re.M)
    if m:
        return source, source[:m.start()].count("\n") + 2
    lines = source.split("\n")
    # the loadUi call for this form, with self as the base instance
    load = None
    for i, ln in enumerate(lines):
        if re.search(r"\bloadUi\s*\(", ln) and (ui_base in ln or "loadUi" in ln) and re.search(r",\s*self\s*\)", ln):
            if ui_base in ln or load is None:
                load = i
    if load is None:
        raise ValueError(f"PyStudio found the code for {ui_base} but not a  uic.loadUi(..., self)  line in a "
                         f"class. Add  self.{control}.{signal}.connect(self.{handler})  after the form is loaded.")
    indent = re.match(r"[ \t]*", lines[load]).group(0)
    # after the last connect that follows the loadUi line (same block)
    at = load
    for j in range(load + 1, len(lines)):
        s = lines[j]
        if s.strip() and not s.startswith(indent):
            break
        if re.match(rf"{re.escape(indent)}self\.\w+\.\w+\.connect\(", s):
            at = j
    lines.insert(at + 1, f"{indent}self.{control}.{signal}.connect(self.{handler})")
    # the class that holds the loadUi call: its end is the next line indented like (or less than) 'class'
    cls = next((j for j in range(load, -1, -1) if re.match(r"[ \t]*class\s+\w+", lines[j])), None)
    if cls is None:
        raise ValueError("The loadUi call isn't inside a class.")
    cls_indent = len(re.match(r"[ \t]*", lines[cls]).group(0))
    end = len(lines)
    for j in range(cls + 1, len(lines)):
        s = lines[j]
        if s.strip() and len(s) - len(s.lstrip()) <= cls_indent and not s.lstrip().startswith(("#", ")", "]")):
            end = j
            break
    while end > cls + 1 and not lines[end - 1].strip():
        end -= 1
    member = " " * (cls_indent + 4)
    params = "self" + (", " + arg if arg else "")
    block = ["", f"{member}def {handler}({params}):", f"{member}    pass"]
    lines[end:end] = block
    return "\n".join(lines), end + 3
