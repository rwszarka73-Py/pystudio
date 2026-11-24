"""Tools > Options."""
import sys

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout)

STARTUP = [("start", "Show Start Window"), ("last", "Open most recent project"),
           ("empty", "Show empty environment")]


def startup_mode():
    return QSettings().value("startup", "start")


def editor_font_size():
    try:
        return int(QSettings().value("editorFontSize", 11))
    except (TypeError, ValueError):
        return 11


class OptionsDialog(QDialog):
    def __init__(self, current_python, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Options")
        self.resize(660, 520)
        s = QSettings()
        self.cb_start = QComboBox()
        for key, text in STARTUP:
            self.cb_start.addItem(text, key)
        self.cb_start.setCurrentIndex(next(i for i, (k, _t) in enumerate(STARTUP) if k == startup_mode()))
        self.sp_font = QSpinBox()
        self.sp_font.setRange(7, 32)
        self.sp_font.setValue(editor_font_size())
        self.e_python = QLineEdit(current_python)
        b = QPushButton("Browse...")
        b.clicked.connect(self._browse)
        reset = QPushButton("Reset")
        reset.setToolTip("Use PyStudio's own Python (has PySide6)")
        reset.clicked.connect(lambda: self.e_python.setText(sys.executable))
        row = QHBoxLayout()
        row.addWidget(self.e_python, 1)
        row.addWidget(b)
        row.addWidget(reset)
        f = QFormLayout()
        f.addRow("At startup:", self.cb_start)
        from .theme import THEMES, saved_theme
        self.cb_theme = QComboBox()
        self.cb_theme.addItems(THEMES)
        self.cb_theme.setCurrentText(saved_theme())
        self.cb_theme.setToolTip("Color theme, like Visual Studio's Dark / Light / Blue. Changing it restarts PyStudio.")
        f.addRow("Color theme:", self.cb_theme)
        f.addRow("Editor font size:", self.sp_font)
        f.addRow("Default Python:", row)
        from .formatter import format_on_save, line_length
        self.sp_len = QSpinBox()
        self.sp_len.setRange(60, 200)
        self.sp_len.setValue(line_length())
        self.sp_len.setToolTip("Longest line Format Document (black) makes. Black's own default is 88.")
        from PySide6.QtWidgets import QCheckBox
        self.ck_fmt = QCheckBox("Format document on save")
        self.ck_fmt.setChecked(format_on_save())
        f.addRow("Format line length:", self.sp_len)
        f.addRow("", self.ck_fmt)
        from .navigation import codelens_enabled
        self.ck_lens = QCheckBox("Show reference counts after def/class lines (CodeLens)")
        self.ck_lens.setChecked(codelens_enabled())
        f.addRow("", self.ck_lens)
        self.ck_hot = QCheckBox("Hot Reload: show form changes in the running program")
        self.ck_hot.setChecked(hot_reload_enabled())
        f.addRow("", self.ck_hot)
        from .ghost import enabled as _ghost_on
        self.ck_ghost = QCheckBox("Inline suggestions from Claude as you type (Tab accepts; uses API credits)")
        self.ck_ghost.setChecked(_ghost_on())
        self.ck_ghost.setToolTip("Gray suggestions after the cursor when you pause typing. Alt+\\ asks for one "
                                 "any time, even with this off. Needs the Claude Assistant's API key.")
        f.addRow("", self.ck_ghost)
        from . import analysis
        self.ck_ruff = QCheckBox("Ruff: find likely bugs as you type (Error List, squiggles, Ctrl+. fixes)")
        self.ck_ruff.setChecked(analysis.ruff_enabled())
        self.e_rules = QLineEdit(analysis.ruff_rules())
        self.e_rules.setToolTip("Ruff rule codes, comma-separated. PyStudio's default: " + analysis.DEFAULT_RULES +
                                " (E7/E9 statement and runtime errors, W6 deprecated escapes, B likely bugs, PLE "
                                "errors). Add SIM for simplifications, UP for newer syntax, I for import order, "
                                "N for naming. A project's own ruff.toml / [tool.ruff] wins.")
        self.ck_types = QCheckBox("Type checking (mypy) when you save: wrong argument types, misspelled attributes")
        self.ck_types.setChecked(analysis.types_enabled())
        self.ck_untyped = QCheckBox("Also check functions without type hints")
        self.ck_untyped.setChecked(analysis.check_untyped())
        missing = [n for n, ok in (("Ruff", analysis.ruff_exe()), ("mypy", analysis.mypy_cmd())) if not ok]
        from .editorvisuals import enabled as _vis
        self.ck_map = QCheckBox("Scrollbar map: errors, changes, Find matches, breakpoints")
        self.ck_map.setChecked(_vis("scrollMap"))
        self.ck_sticky = QCheckBox("Sticky scroll: keep the class/def you're in at the top")
        self.ck_sticky.setChecked(_vis("stickyScroll"))
        self.ck_pairs = QCheckBox("Bracket pair colors")
        self.ck_pairs.setChecked(_vis("bracketPairs"))
        f.addRow("", self.ck_map)
        f.addRow("", self.ck_sticky)
        f.addRow("", self.ck_pairs)
        f.addRow("", self.ck_ruff)
        f.addRow("Ruff rules:", self.e_rules)
        f.addRow("", self.ck_types)
        f.addRow("", self.ck_untyped)
        if missing:
            m = QLabel(f"{' and '.join(missing)} not installed: run  bash install.sh --update  from the PyStudio "
                       "zip (or Help > Install Update from Zip).")
            m.setStyleSheet("color: #cca700;")
            m.setWordWrap(True)
            f.addRow(m)
        note = QLabel("A project can use its own Python (Project Properties > Debug).")
        note.setStyleSheet("color: #9a9a9a;")
        f.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addStretch()
        lay.addWidget(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Python interpreter", "/usr/bin", "Python (python*)")
        if path:
            self.e_python.setText(path)

    def apply(self):
        s = QSettings()
        s.setValue("startup", self.cb_start.currentData())
        s.setValue("theme", self.cb_theme.currentText())
        s.setValue("editorFontSize", self.sp_font.value())
        s.setValue("formatLineLength", self.sp_len.value())
        s.setValue("formatOnSave", self.ck_fmt.isChecked())
        s.setValue("codeLens", self.ck_lens.isChecked())
        s.setValue("hotReload", self.ck_hot.isChecked())
        s.setValue("inlineSuggestions", self.ck_ghost.isChecked())
        s.setValue("python", self.e_python.text().strip() or sys.executable)
        s.setValue("ruffEnabled", self.ck_ruff.isChecked())
        s.setValue("scrollMap", self.ck_map.isChecked())
        s.setValue("stickyScroll", self.ck_sticky.isChecked())
        s.setValue("bracketPairs", self.ck_pairs.isChecked())
        s.setValue("ruffRules", self.e_rules.text().strip() or "")
        s.setValue("typeCheckEnabled", self.ck_types.isChecked())
        s.setValue("typeCheckUntyped", self.ck_untyped.isChecked())


def hot_reload_enabled():
    return QSettings().value("hotReload", True, type=bool)
