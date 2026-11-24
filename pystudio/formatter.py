"""Format Document / Format Selection with black (VS: Edit > Advanced > Format Document)."""
import subprocess
import sys

from PySide6.QtCore import QSettings


def line_length():
    try:
        return int(QSettings().value("formatLineLength", 100))
    except (TypeError, ValueError):
        return 100


def format_on_save():
    return QSettings().value("formatOnSave", False) in (True, "true", "1", 1)


def black_installed(python=None):
    try:
        return subprocess.run([python or sys.executable, "-c", "import black"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def format_source(text, first_line=None, last_line=None, python=None):
    """Returns (ok, formatted text or error message). first/last_line (1-based) limit
    formatting to those lines (Format Selection)."""
    args = [python or sys.executable, "-m", "black", "--quiet", "--line-length", str(line_length())]
    if first_line and last_line:
        args += ["--line-ranges", f"{first_line}-{last_line}"]
    args.append("-")
    try:
        r = subprocess.run(args, input=text, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        err = r.stderr.strip()
        if "Cannot parse" in err:
            m = err.split("Cannot parse", 1)[1]
            return False, "The code has a syntax error, so it can't be formatted yet" + \
                (f" (line {m.split(':')[1].strip()})" if m.count(":") >= 2 else "") + "."
        if "No module named black" in err:
            return False, "black isn't installed"
        return False, err or "black failed"
    return True, r.stdout
