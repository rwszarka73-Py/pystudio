"""Color themes modeled on Visual Studio's: Dark (default), Light and Blue.

PyStudio's panels are styled with the Dark colors. For Light/Blue, select() builds a map from
each Dark color to the theme's color; style sheets set on PyStudio's own widgets and colors made
with c() are translated through it. Form designs (the controls you place) keep their real colors.
The theme is chosen once at startup (Tools > Options > Theme restarts PyStudio)."""
import re

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

THEMES = ["Dark", "Light", "Blue"]
NAME = "Dark"

# Editor colors (VS Dark+) - replaced by select()
EDITOR_BG = "#1e1e1e"
EDITOR_FG = "#d4d4d4"
CURRENT_LINE = "#2a2d2e"
GUTTER_BG = "#1e1e1e"
GUTTER_FG = "#858585"
GUTTER_FG_ACTIVE = "#c6c6c6"
SELECTION = "#264f78"

# Syntax colors (updated in place by select(), so `from .theme import SYNTAX` sees them)
SYNTAX = {
    "keyword": "#569cd6",
    "control": "#c586c0",
    "builtin": "#4ec9b0",
    "self": "#9cdcfe",
    "string": "#ce9178",
    "comment": "#6a9955",
    "number": "#b5cea8",
    "decorator": "#dcdcaa",
    "defname": "#dcdcaa",
    "classname": "#4ec9b0",
}
_DARK_SYNTAX = dict(SYNTAX)
_LIGHT_SYNTAX = {"keyword": "#0000ff", "control": "#af00db", "builtin": "#267f99", "self": "#001080",
                 "string": "#a31515", "comment": "#008000", "number": "#098658", "decorator": "#795e26",
                 "defname": "#795e26", "classname": "#267f99"}

# Dark color -> (Light, Blue). Covers the colors PyStudio's panels use.
_PAIRS = {
    # backgrounds
    "#1e1e1e": ("#ffffff", "#ffffff"),     # editor, output, trees
    "#252526": ("#f5f5f5", "#f5f7fb"),     # panels
    "#2d2d30": ("#eeeef2", "#d6dbe9"),     # chrome: menus, toolbars, dock titles
    "#333337": ("#cccedb", "#b5bed6"),     # borders
    "#1b1b1c": ("#f6f6f6", "#e9ecee"),     # menus
    "#333334": ("#c9def5", "#fdf4bf"),     # menu hover
    "#3e3e40": ("#c9def5", "#fdf4bf"),     # hover
    "#3e3e42": ("#e5e5e5", "#ccd5f0"),     # buttons
    "#3c3c3c": ("#ffffff", "#ffffff"),     # text boxes
    "#3a3d41": ("#e5e5e5", "#ccd5f0"),
    "#2a2d2e": ("#eef0f3", "#eef0f3"),     # current line / hover
    "#264f78": ("#add6ff", "#add6ff"),     # editor selection
    "#094771": ("#cce8ff", "#fff29d"),     # list selection
    "#04395e": ("#cce8ff", "#fff29d"),
    "#454545": ("#cccedb", "#b5bed6"),
    "#3f3f46": ("#cccedb", "#b5bed6"),
    "#5a1d1d": ("#f7d4d4", "#f7d4d4"),     # breakpoint line
    "#4b4b18": ("#fff3b0", "#fff3b0"),     # debugger current line
    "#1e3a1e": ("#e6ffec", "#e6ffec"),     # diff added
    "#4b1e1e": ("#ffebe9", "#ffebe9"),     # diff removed
    # text
    "#f1f1f1": ("#1e1e1e", "#1e1e1e"),
    "#d4d4d4": ("#1e1e1e", "#1e1e1e"),
    "#cccccc": ("#333333", "#333333"),
    "#c0c0c0": ("#444444", "#1e1e1e"),
    "#c6c6c6": ("#237893", "#237893"),     # active line number
    "#858585": ("#8a8a8a", "#8a8a8a"),
    "#9a9a9a": ("#6d6d6d", "#5a5f6e"),
    "#aaaaaa": ("#6d6d6d", "#5a5f6e"),
    "#999999": ("#6d6d6d", "#5a5f6e"),
    "#888888": ("#6d6d6d", "#5a5f6e"),
    "#808080": ("#6d6d6d", "#5a5f6e"),
    "#6d6d6d": ("#a0a0a0", "#a0a0a0"),     # disabled text
    "#dcdcdc": ("#1e1e1e", "#1e1e1e"),     # property names
    "#bbbbbb": ("#444444", "#444444"),
    "#dcdcaa": ("#795e26", "#795e26"),
    "#b5e8b0": ("#116329", "#116329"),
    "#f5b0b0": ("#82071e", "#82071e"),
    # status colors (readable on light backgrounds)
    "#89d185": ("#1a7f37", "#1a7f37"),
    "#f48771": ("#cf222e", "#cf222e"),
    "#f14c4c": ("#cf222e", "#cf222e"),
    "#d7ba7d": ("#9a6700", "#9a6700"),
    "#75beff": ("#0969da", "#0969da"),
    "#3794ff": ("#0969da", "#0969da"),
    "#569cd6": ("#0451a5", "#0451a5"),
    "#6a9955": ("#008000", "#008000"),
    "#4fb34f": ("#008000", "#008000"),
}
_MAP = {}
_HEX = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")


def _expand(h):
    h = h.lower()
    return "#" + "".join(ch * 2 for ch in h[1:]) if len(h) == 4 else h


def c(color):
    """A Dark-theme color translated to the current theme."""
    if not isinstance(color, str) or not color.startswith("#"):
        return color
    return _MAP.get(_expand(color), color)


_WHITE = re.compile(r"(?<![-\w])color\s*:\s*(#fff\b|#ffffff\b|white\b)", re.I)
_BG = re.compile(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,6})\b", re.I)


def _white_text(css):
    """White text stays white on a colored background (VS blue buttons/tabs); on a dark panel
    background - which becomes light - or with no background, it becomes dark text."""
    def fix(m):
        start = max(css.rfind("{", 0, m.start()), css.rfind("}", 0, m.start()))
        end = css.find("}", m.end())
        block = css[start + 1:end if end >= 0 else len(css)]
        bg = _BG.search(block)
        if bg is None or _expand(bg.group(1)) in _MAP:
            return m.group(0)[:m.start(1) - m.start()] + "#1f1f1f"     # (not a key: won't be re-mapped)
        return m.group(0)
    return _WHITE.sub(fix, css)


def translate(css, whites=True):
    if not _MAP or not css:
        return css
    if whites:
        css = _white_text(css)
    return _HEX.sub(lambda m: _MAP.get(_expand(m.group(0)), m.group(0)), css)


def saved_theme():
    t = QSettings().value("theme", "Dark")
    return t if t in THEMES else "Dark"


def select(name):
    """Choose the theme before PyStudio's windows are built."""
    global NAME
    NAME = name if name in THEMES else "Dark"
    _MAP.clear()
    if NAME == "Dark":
        SYNTAX.update(_DARK_SYNTAX)
        return
    i = 0 if NAME == "Light" else 1
    _MAP.update({k: v[i] for k, v in _PAIRS.items()})
    SYNTAX.update(_LIGHT_SYNTAX)
    # EDITOR_BG etc. keep their Dark values: style sheets and c() translate them exactly once
    # (setting light values here would get translated again - #1e1e1e text would turn white)
    _install_hook()


_hooked = False


def _install_hook():
    """Translate style sheets PyStudio sets on its own widgets (not designer controls)."""
    global _hooked
    if _hooked:
        return
    _hooked = True
    original = QWidget.setStyleSheet

    def set_style_sheet(self, css):
        if _MAP and css and not hasattr(self, "_ps"):
            css = translate(css)
        original(self, css)
    QWidget.setStyleSheet = set_style_sheet


_DARK_CSS = """
    QMainWindow::separator { background: #2d2d30; width: 6px; height: 6px; }
    QMainWindow::separator:hover { background: #007acc; }
    QDockWidget { color: #f1f1f1; }
    QDockWidget::title { background: #2d2d30; padding: 4px 6px; }
    QTabWidget::pane { border: 0; }
    QTabBar::tab { background: #2d2d30; color: #c0c0c0; padding: 5px 12px;
                   border: 0; margin-right: 1px; }
    QTabBar::tab:selected { background: #007acc; color: #ffffff; }
    QTabBar::tab:hover:!selected { background: #3e3e40; }
    QToolBar { background: #2d2d30; border: 0; spacing: 2px; padding: 2px; }
    QStatusBar { background: #007acc; color: #ffffff; }
    QStatusBar QLabel { color: #ffffff; padding: 0 8px; }
    QStatusBar QToolButton { color: #ffffff; }
    QMenuBar { background: #2d2d30; }
    QMenuBar::item:selected { background: #3e3e40; }
    QMenu { background: #1b1b1c; border: 1px solid #333337; }
    QMenu::item { padding: 4px 24px 4px 20px; }
    QMenu::item:selected { background: #333334; }
    QMenu::separator { height: 1px; background: #333337; margin: 3px 8px; }
    QTreeView { background: #252526; border: 0; }
    QTreeView::item:selected { background: #094771; }
    QLineEdit { background: #3c3c3c; border: 1px solid #3c3c3c; padding: 3px; color: #f1f1f1; }
    QLineEdit:focus { border: 1px solid #007acc; }
"""


def apply_theme(app: QApplication):
    """Palette and app-wide style for the selected theme (call after select())."""
    app.setStyle("Fusion")
    dark = NAME == "Dark"
    p = QPalette()
    p.setColor(QPalette.Window, QColor(c("#2d2d30")))
    p.setColor(QPalette.WindowText, QColor(c("#f1f1f1")))
    p.setColor(QPalette.Base, QColor(c("#252526") if dark else "#ffffff"))
    p.setColor(QPalette.AlternateBase, QColor(c("#2d2d30")))
    p.setColor(QPalette.ToolTipBase, QColor("#252526" if dark else "#ffffe1"))
    p.setColor(QPalette.ToolTipText, QColor("#f1f1f1" if dark else "#1e1e1e"))
    p.setColor(QPalette.Text, QColor(c("#f1f1f1")))
    p.setColor(QPalette.Button, QColor(c("#3e3e42")))
    p.setColor(QPalette.ButtonText, QColor(c("#f1f1f1")))
    p.setColor(QPalette.BrightText, QColor("#ff5555"))
    p.setColor(QPalette.Highlight, QColor("#007acc" if dark else "#3399ff"))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.Link, QColor(c("#3794ff")))
    p.setColor(QPalette.PlaceholderText, QColor("#888888" if dark else "#8a8a8a"))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(c("#6d6d6d")))
    app.setPalette(p)
    css = translate(_DARK_CSS, whites=False)     # its white text is on VS blue (status bar, tabs)
    if NAME != "Dark":     # selected rows: light background, so dark text
        css += """
    QListWidget::item:selected, QTreeWidget::item:selected, QTreeView::item:selected,
    QTableWidget::item:selected, QTableView::item:selected { color: #1e1e1e; }"""
    if NAME == "Light":
        css += """
    QTabBar::tab:selected { background: #007acc; color: #ffffff; }
    QDockWidget::title { background: #eeeef2; }
    QToolTip { color: #1e1e1e; background: #ffffe1; border: 1px solid #cccedb; }"""
    elif NAME == "Blue":
        css += """
    QMainWindow, QDialog { background: #d6dbe9; }
    QDockWidget::title { background: #4d6082; color: #ffffff; }
    QDockWidget { color: #ffffff; }
    QMenuBar { background: #d6dbe9; }
    QToolBar { background: #cfd6e5; }
    QTabBar::tab { background: #4d6082; color: #ffffff; }
    QTabBar::tab:selected { background: #fff29d; color: #1e1e1e; }
    QTabBar::tab:hover:!selected { background: #5b7199; }
    QStatusBar { background: #0e70c0; }
    QToolTip { color: #1e1e1e; background: #ffffe1; border: 1px solid #b5bed6; }"""
    app.setStyleSheet(css)


def apply_dark_theme(app: QApplication):
    """Kept for older callers/tests: Dark theme."""
    select("Dark")
    apply_theme(app)
