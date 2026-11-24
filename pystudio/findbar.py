"""Find / Replace bar shown above the editor (Ctrl+F, Ctrl+H)."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QToolButton, QVBoxLayout, QWidget)


class FindBar(QWidget):
    closed = Signal()

    def __init__(self, get_editor, parent=None):
        super().__init__(parent)
        self.get_editor = get_editor  # callable returning the active CodeEditor

        self.find_edit = QLineEdit(placeholderText="Find")
        self.replace_edit = QLineEdit(placeholderText="Replace")
        self.case_box = QCheckBox("Match case")
        self.word_box = QCheckBox("Whole word")
        self.status = QLabel()

        btn_prev = QPushButton("Previous")
        btn_next = QPushButton("Next")
        btn_repl = QPushButton("Replace")
        btn_all = QPushButton("Replace All")
        btn_close = QToolButton(text="✕")
        btn_close.setAutoRaise(True)

        row1 = QHBoxLayout()
        row1.addWidget(self.find_edit, 1)
        row1.addWidget(btn_prev)
        row1.addWidget(btn_next)
        row1.addWidget(self.case_box)
        row1.addWidget(self.word_box)
        row1.addWidget(self.status)
        row1.addWidget(btn_close)

        self.replace_row = QWidget()
        row2 = QHBoxLayout(self.replace_row)
        row2.setContentsMargins(0, 0, 0, 0)
        row2.addWidget(self.replace_edit, 1)
        row2.addWidget(btn_repl)
        row2.addWidget(btn_all)
        row2.addStretch()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)
        lay.addLayout(row1)
        lay.addWidget(self.replace_row)

        self.find_edit.returnPressed.connect(self.find_next)
        btn_next.clicked.connect(self.find_next)
        btn_prev.clicked.connect(self.find_prev)
        btn_repl.clicked.connect(self.replace_one)
        btn_all.clicked.connect(self.replace_all)
        btn_close.clicked.connect(self.hide_bar)
        self.hide()

    def show_find(self, replace=False):
        ed = self.get_editor()
        if ed and ed.textCursor().hasSelection():
            self.find_edit.setText(ed.textCursor().selectedText())
        self.replace_row.setVisible(replace)
        self.show()
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def hide_bar(self):
        ed = self.get_editor()
        if ed is not None and hasattr(ed, "visuals"):
            ed.visuals.set_search(None)
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide_bar()
            return
        super().keyPressEvent(event)

    def _report(self, found):
        self.status.setText("" if found else "Not found")

    def find_next(self):
        ed = self.get_editor()
        if ed and self.find_edit.text():
            self._report(ed.find_text(self.find_edit.text(), False,
                                      self.case_box.isChecked(), self.word_box.isChecked()))

    def find_prev(self):
        ed = self.get_editor()
        if ed and self.find_edit.text():
            self._report(ed.find_text(self.find_edit.text(), True,
                                      self.case_box.isChecked(), self.word_box.isChecked()))

    def replace_one(self):
        ed = self.get_editor()
        if ed and self.find_edit.text():
            self._report(ed.replace_current(self.find_edit.text(), self.replace_edit.text(),
                                            self.case_box.isChecked()))

    def replace_all(self):
        ed = self.get_editor()
        if ed and self.find_edit.text():
            n = ed.replace_all(self.find_edit.text(), self.replace_edit.text(),
                               self.case_box.isChecked(), self.word_box.isChecked())
            self.status.setText(f"{n} replaced")
