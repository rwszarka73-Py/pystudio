"""F1 Help: the documentation page for what's under the cursor (VS: F1 opens the docs for
the keyword, class or method at the caret)."""
import builtins
import keyword
import os
import sys

PY = "https://docs.python.org/3/"
QT = "https://doc.qt.io/qtforpython-6/"
README = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.pdf")

KEYWORDS = {
    "if": "reference/compound_stmts.html#the-if-statement", "elif": "reference/compound_stmts.html#the-if-statement",
    "else": "reference/compound_stmts.html#the-if-statement",
    "for": "reference/compound_stmts.html#the-for-statement", "while": "reference/compound_stmts.html#the-while-statement",
    "break": "reference/simple_stmts.html#the-break-statement",
    "continue": "reference/simple_stmts.html#the-continue-statement",
    "try": "reference/compound_stmts.html#the-try-statement", "except": "reference/compound_stmts.html#the-try-statement",
    "finally": "reference/compound_stmts.html#the-try-statement",
    "raise": "reference/simple_stmts.html#the-raise-statement",
    "with": "reference/compound_stmts.html#the-with-statement",
    "def": "reference/compound_stmts.html#function-definitions",
    "return": "reference/simple_stmts.html#the-return-statement",
    "class": "reference/compound_stmts.html#class-definitions",
    "import": "reference/simple_stmts.html#the-import-statement", "from": "reference/simple_stmts.html#the-import-statement",
    "as": "reference/simple_stmts.html#the-import-statement",
    "lambda": "reference/expressions.html#lambda", "pass": "reference/simple_stmts.html#the-pass-statement",
    "global": "reference/simple_stmts.html#the-global-statement",
    "nonlocal": "reference/simple_stmts.html#the-nonlocal-statement",
    "assert": "reference/simple_stmts.html#the-assert-statement", "del": "reference/simple_stmts.html#the-del-statement",
    "yield": "reference/simple_stmts.html#the-yield-statement",
    "async": "reference/compound_stmts.html#coroutines", "await": "reference/expressions.html#await",
    "and": "reference/expressions.html#boolean-operations", "or": "reference/expressions.html#boolean-operations",
    "not": "reference/expressions.html#boolean-operations",
    "in": "reference/expressions.html#membership-test-operations", "is": "reference/expressions.html#is-not",
    "True": "library/constants.html#True", "False": "library/constants.html#False",
    "None": "library/constants.html#None", "match": "reference/compound_stmts.html#the-match-statement",
    "case": "reference/compound_stmts.html#the-match-statement",
}
# modules whose docs page is a submodule (os.path -> os.path.html)
SUBPAGES = {"os.path", "urllib.request", "urllib.parse", "urllib.error", "xml.etree.ElementTree", "logging.handlers",
            "logging.config", "concurrent.futures", "email.message", "email.mime", "http.client", "http.server",
            "importlib.metadata", "importlib.resources", "unittest.mock", "collections.abc", "json.tool",
            "multiprocessing.pool", "xml.dom.minidom", "html.parser", "sqlite3.dbapi2"}
TYPES = {"str", "int", "float", "list", "dict", "set", "tuple", "bytes", "bytearray", "bool", "complex",
         "frozenset", "range", "memoryview"}
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {"os", "sys", "re", "json", "datetime", "math", "time",
                                                          "random", "csv", "sqlite3", "subprocess", "pathlib",
                                                          "shutil", "glob", "collections", "itertools", "typing"}
# Toolbox control -> Qt class (designer F1)
TOOL_CLASSES = {"Chart": ("QtCharts", "QChartView"), "WebBrowser": ("QtWebEngineWidgets", "QWebEngineView")}


def qt_url(module, cls, member=None):
    url = f"{QT}PySide6/{module}/{cls}.html"
    return url + (f"#PySide6.{module}.{cls}.{member}" if member else "")


def url_for(full_name, word="", project_modules=()):
    """Docs URL for a dotted name from Jedi (e.g. 'PySide6.QtWidgets.QLabel.setText',
    'builtins.str.upper', 'os.path.join'), or for a keyword. None = nothing specific."""
    if word in keyword.kwlist or word in ("match", "case"):
        return PY + KEYWORDS.get(word, "reference/lexical_analysis.html#keywords")
    full = (full_name or "").strip()
    if not full:
        return None
    parts = full.split(".")
    top = parts[0]
    if top in ("pystudio_runtime", "my") or top in project_modules:
        return "file://" + README if top in ("pystudio_runtime", "my") else None
    if top == "PySide6" and len(parts) >= 3:
        return qt_url(parts[1], parts[2], parts[3] if len(parts) > 3 else None)
    if top == "builtins":
        name = parts[1] if len(parts) > 1 else word
        if name in TYPES:
            return PY + "library/stdtypes.html#" + (".".join(parts[1:3]) if len(parts) > 2 else name)
        obj = getattr(builtins, name, None)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            return PY + "library/exceptions.html#" + name
        return PY + "library/functions.html#" + name
    if top in STDLIB or top in ("os", "sys"):
        page = top
        for n in (3, 2):
            cand = ".".join(parts[:n])
            if cand in SUBPAGES:
                page = cand
                break
        return PY + f"library/{page.lower() if page == 'xml.etree.ElementTree' else page}.html#{full}"
    return f"https://pypi.org/project/{top.replace('_', '-')}/"


def search_url(word):
    return PY + "search.html?q=" + word
