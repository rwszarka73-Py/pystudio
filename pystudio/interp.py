"""Which Python runs a project. PyStudio's own Python (its private venv) has PySide6; a project made outside
PyStudio may need packages that only the system's Python has - PyQt5 from apt, for example. PyStudio looks
at what the project imports and picks a Python that has all of it."""
import ast
import json
import os
import subprocess
import sys

SYSTEM_PYTHONS = ["/usr/bin/python3", "/usr/local/bin/python3"]
PIP_NAMES = {"PyQt5": "PyQt5", "PyQt6": "PyQt6", "cv2": "opencv-python", "PIL": "Pillow", "yaml": "PyYAML",
             "sklearn": "scikit-learn", "bs4": "beautifulsoup4", "serial": "pyserial", "usb": "pyusb",
             "dateutil": "python-dateutil", "dotenv": "python-dotenv", "jwt": "PyJWT", "Crypto": "pycryptodome",
             "gi": "PyGObject", "dbus": "dbus-python", "magic": "python-magic", "docx": "python-docx",
             "pptx": "python-pptx", "snowflake": "snowflake-connector-python", "ibm_db_dbi": "ibm_db",
             "win32api": "pywin32", "fitz": "PyMuPDF", "vlc": "python-vlc", "mpv": "python-mpv"}
APT_NAMES = {"PyQt5": "python3-pyqt5", "PyQt6": "python3-pyqt6", "gi": "python3-gi", "dbus": "python3-dbus",
             "cv2": "python3-opencv", "PIL": "python3-pil", "vlc": "python3-vlc"}


def pip_name(module):
    return PIP_NAMES.get(module, module)


def _local_modules(root):
    names = set()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "venv", ".venv", "build",
                                                                         "dist", "publish", "node_modules")]
        for f in files:
            if f.endswith(".py"):
                names.add(f[:-3])
        for d in dirs:
            names.add(d)
    return names


def project_imports(root, limit=400):
    """Top-level modules the project's code imports that aren't the standard library or its own files."""
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    local = _local_modules(root)
    found = set()
    n = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "venv", ".venv", "build",
                                                                         "dist", "publish", "node_modules")]
        for f in files:
            if not f.endswith(".py") or f.startswith("ui_") or f in ("pystudio_runtime.py", "my.py"):
                continue
            n += 1
            if n > limit:
                break
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                    tree = ast.parse(fh.read())
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        found.add(a.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    found.add(node.module.split(".")[0])
    generated = {"pystudio_runtime", "my"}           # PyStudio writes these (and ui_<form>.py) itself
    return sorted(m for m in found if m not in stdlib and m not in local and m not in generated
                  and not m.startswith("ui_"))


def missing(python, modules, timeout=15):
    """The modules python can't import (None if python itself can't run)."""
    if not modules:
        return []
    code = ("import importlib.util, json, sys\n"
            "out = []\n"
            "for m in json.loads(sys.argv[1]):\n"
            "    try:\n"
            "        ok = importlib.util.find_spec(m) is not None\n"
            "    except Exception:\n"
            "        ok = False\n"
            "    if not ok: out.append(m)\n"
            "print(json.dumps(out))\n")
    try:
        r = subprocess.run([python, "-c", code, json.dumps(list(modules))], capture_output=True, text=True,
                           timeout=timeout)
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def candidates(root, current, pystudio_python=sys.executable):
    out = []
    venvs = (os.path.join(root, ".venv", "bin", "python"), os.path.join(root, "venv", "bin", "python"))
    try:
        from .remote import remote_for
        if remote_for(root) is not None:      # a server's venv: it runs there, never here
            venvs = ()
            if current and remote_for(current) is not None:
                current = None
    except Exception:  # noqa: BLE001
        pass
    for p in (current, *venvs, pystudio_python, *SYSTEM_PYTHONS):
        # compare paths, not link targets: a venv's python links to the system python but has other packages
        if p and os.path.exists(p) and os.path.abspath(p) not in [os.path.abspath(x) for x in out]:
            out.append(p)
    return out


def best_python(root, current, pystudio_python=sys.executable, modules=None):
    """(python, missing_in_it, missing_in_current) - the candidate that can import the most of the project."""
    modules = project_imports(root) if modules is None else modules
    if not modules:
        return current, [], []
    cur_missing = missing(current, modules) or []
    if not cur_missing:
        return current, [], []
    best, best_missing = current, cur_missing
    for p in candidates(root, current, pystudio_python):
        m = missing(p, modules)
        if m is not None and len(m) < len(best_missing):
            best, best_missing = p, m
            if not m:
                break
    return best, best_missing, cur_missing


def python_with(module, root, current, pystudio_python=sys.executable):
    """A Python (other than current) that has module, or None."""
    for p in candidates(root, current, pystudio_python):
        if os.path.abspath(p) != os.path.abspath(current) and missing(p, [module]) == []:
            return p
    return None


_MANAGED = {}


def externally_managed(python):
    """True for a distribution's own Python (Ubuntu 23.04+): pip refuses to install into it (PEP 668). A venv made
    from it is not managed."""
    if not python:
        return False
    if python not in _MANAGED:
        code = ("import sys, sysconfig, os; print(sys.prefix == sys.base_prefix and os.path.exists("
                "os.path.join(sysconfig.get_path('stdlib'), 'EXTERNALLY-MANAGED')))")
        try:
            r = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=15)
            _MANAGED[python] = r.stdout.strip() == "True"
        except (OSError, subprocess.SubprocessError):
            _MANAGED[python] = False
    return _MANAGED[python]


def build_env_dir(python):
    """Where Publish keeps a venv for a managed Python: outside the project, one per Python."""
    import hashlib
    h = hashlib.sha1(os.path.realpath(python).encode()).hexdigest()[:10]
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "pystudio", "buildenvs", f"{os.path.basename(python)}-{h}")
