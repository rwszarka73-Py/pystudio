"""Adds event handlers to a form's .py file (VS: double-click a control)."""
import re

WIRING_MARKER = "# Event wiring"


def handler_name(control, signal):
    return f"{control}_{signal}"


def add_event_handler(source, control, signal, arg):
    """Return (new_source, line_number_of_body) - 1-based line of the 'pass'.

    Adds   self.ui.<control>.<signal>.connect(self.<handler>)   in __init__
    and    def <handler>(self[, arg]): pass                     at the end of the class.
    If the handler already exists, the source is unchanged and the line of its
    'def' body is returned.
    """
    name = handler_name(control, signal)
    lines = source.split("\n")

    m = re.search(rf"^[ \t]*def {re.escape(name)}\(", source, re.M)
    if m:
        return source, source[:m.start()].count("\n") + 2

    # 1) connect line: after the last existing connect, else after the marker,
    #    else after setupUi
    connect = f"        self.ui.{control}.{signal}.connect(self.{name})"
    idx = None
    for i, line in enumerate(lines):
        if re.match(r"\s*self\.ui\.\w+\.\w+\.connect\(", line):
            idx = i
    if idx is None:
        for i, line in enumerate(lines):
            if WIRING_MARKER in line:
                idx = i
                break
    if idx is None:
        for i, line in enumerate(lines):
            if "setupUi(self)" in line:
                idx = i
                break
    if idx is None:
        raise ValueError("Couldn't find 'self.ui.setupUi(self)' in the form's code file.")
    lines.insert(idx + 1, connect)

    # 2) handler method at the end of the file (end of the form class)
    while lines and not lines[-1].strip():
        lines.pop()
    params = "self" + (f", {arg}" if arg else "")
    lines += ["", f"    def {name}({params}):", "        pass", ""]
    return "\n".join(lines), len(lines) - 1


def ensure_runtime_call(source):
    """Older forms (before v0.5) lack the pystudio_runtime hookup; add it.
    Returns the (possibly) updated source."""
    if "pystudio_runtime.setup_form(self)" in source:
        return source
    lines = source.split("\n")
    idx = next((i for i, l in enumerate(lines) if "setupUi(self)" in l), None)
    if idx is None:
        return source
    indent = lines[idx][:len(lines[idx]) - len(lines[idx].lstrip())]
    lines.insert(idx + 1, f"{indent}pystudio_runtime.setup_form(self)   # Anchor, StartPosition, FormBorderStyle...")
    if not any(l.strip() == "import pystudio_runtime" for l in lines):
        imp = next((i for i, l in enumerate(lines) if re.match(r"from ui_\w+ import", l)), None)
        if imp is None:
            imp = next((i for i, l in enumerate(lines) if l.startswith(("import ", "from "))), 0)
        lines.insert(imp, "import pystudio_runtime")
    return "\n".join(lines)


def ensure_form_base(source):
    """Forms made before v1.2 derive from QMainWindow; switch them to pystudio_runtime.Form
    (adds show_dialog / dialog_result). Only touches a form class that calls setup_form(self).
    Returns the (possibly) updated source."""
    if "pystudio_runtime.setup_form(self)" not in source:
        return source
    m = re.search(r"^class (\w+)\(QMainWindow\):", source, re.M)
    if not m:
        return source
    source = source[:m.start()] + f"class {m.group(1)}(Form):" + source[m.end():]
    lines = source.split("\n")
    # QMainWindow no longer used? drop it from its import line
    rest = "\n".join(l for l in lines if not l.startswith("from PySide6.QtWidgets import"))
    if not re.search(r"\bQMainWindow\b", rest):
        for i, l in enumerate(lines):
            mm = re.match(r"from PySide6\.QtWidgets import (.+)$", l)
            if mm and "(" not in l and re.search(r"\bQMainWindow\b", mm.group(1)):
                names = [n.strip() for n in mm.group(1).split(",") if n.strip() and n.strip() != "QMainWindow"]
                if names:
                    lines[i] = "from PySide6.QtWidgets import " + ", ".join(names)
                else:
                    lines[i] = None
                break
        lines = [l for l in lines if l is not None]
    if not any(l.strip() == "from pystudio_runtime import Form" for l in lines):
        idx = next((i for i, l in enumerate(lines) if l.strip() == "import pystudio_runtime"), None)
        if idx is None:
            idx = next((i for i, l in enumerate(lines) if re.match(r"from ui_\w+ import", l)), 0) - 1
        lines.insert(idx + 1, "from pystudio_runtime import Form")
    return "\n".join(lines)
