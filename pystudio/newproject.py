"""New Project dialog (like VS 'Configure your new project')."""
import os
import re

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from .templates import TEMPLATE_INFO, TEMPLATES, default_repos_dir

VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


class TemplateList(QListWidget):
    """VS-style template list: name + description. currentText() = template name."""

    def __init__(self):
        super().__init__()
        self.setStyleSheet("QListWidget { background: #252526; border: 1px solid #3f3f46; }"
                           "QListWidget::item { padding: 6px; }"
                           "QListWidget::item:selected { background: #094771; }")
        for t in TEMPLATES:
            it = QListWidgetItem(f"{t}\n{TEMPLATE_INFO[t][0]}")
            it.setData(Qt.UserRole, t)
            self.addItem(it)
        self.setWordWrap(True)
        self.setMinimumHeight(250)
        self.setCurrentRow(0)

    def currentText(self):
        it = self.currentItem()
        return it.data(Qt.UserRole) if it else TEMPLATES[0]

    def setCurrentText(self, name):
        for i in range(self.count()):
            if self.item(i).data(Qt.UserRole) == name:
                self.setCurrentRow(i)


class NewProjectDialog(QDialog):
    def __init__(self, parent=None, solution_root=None, add_to_solution=False):
        """solution_root: the open solution/project folder - then you can add the new project to it."""
        super().__init__(parent)
        self.solution_root = solution_root
        self.setWindowTitle("Add a new project" if add_to_solution else "Create a new project")
        self.setMinimumWidth(620)
        settings = QSettings()

        self.template = TemplateList()
        self.template.setCurrentText(settings.value("lastTemplate", TEMPLATES[0]))
        self.name = QLineEdit(self._next_free_name(settings, self.template.currentText()))
        self._auto_name = self.name.text()
        self.template.currentRowChanged.connect(self._template_changed)
        self.template.itemDoubleClicked.connect(lambda _it: self.name.setFocus())
        self.location = QLineEdit(settings.value("projectsDir", default_repos_dir()))
        browse = QPushButton("...")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._browse)
        loc_row = QWidget()
        h = QHBoxLayout(loc_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.location, 1)
        h.addWidget(browse)

        self.preview = QLabel()
        self.preview.setStyleSheet("color: #9a9a9a;")
        self.error = QLabel()
        self.error.setStyleSheet("color: #f48771;")

        form = QFormLayout()
        form.addRow(QLabel("<b>Choose a template</b>"))
        form.addRow(self.template)
        form.addRow("Project name:", self.name)
        form.addRow("Location:", loc_row)
        self.cb_solution = QComboBox()
        self.cb_solution.addItems(["Create new solution", "Add to solution"])
        self.cb_solution.setToolTip("Add to solution: the new project goes in the open solution's folder, next to "
                                    "your other projects (for example a Class Library your app uses).")
        self._normal_location = self.location.text()
        if solution_root:
            form.addRow("Solution:", self.cb_solution)
            self.cb_solution.currentIndexChanged.connect(self._solution_mode)

        form.addRow("", self.preview)
        self.ck_github = QCheckBox("Publish to GitHub (a private repository named after the project)")
        self.ck_github.setToolTip("Creates a Git repository and puts it on your GitHub account right away, like "
                                  "Git Changes > Publish to GitHub. The first time, it signs you in to GitHub.")
        self.ck_github.setChecked(settings.value("newProjectGitHub", False, type=bool))
        form.addRow("", self.ck_github)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Create")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.error)
        lay.addWidget(self.buttons)

        self.name.textChanged.connect(self._validate)
        self.location.textChanged.connect(self._validate)
        if solution_root and add_to_solution:
            self.template.setCurrentText("Class Library")    # the usual reason to add a project
            self.cb_solution.setCurrentIndex(1)
        self._validate()
        self.name.setFocus()
        self.name.selectAll()

    @property
    def publish_github(self):
        return self.ck_github.isChecked() and not self.add_to_solution

    @property
    def add_to_solution(self):
        return bool(self.solution_root) and self.cb_solution.currentIndex() == 1

    def _solution_mode(self, _i=None):
        self.ck_github.setVisible(not self.add_to_solution)   # it goes in the solution's repository
        if self.add_to_solution:
            self._normal_location = self.location.text()
            self.location.setText(self.solution_root)
            self.location.setEnabled(False)
        else:
            self.location.setText(self._normal_location)
            self.location.setEnabled(True)
        self._auto_name = self._next_free_name(QSettings(), self.template.currentText(), self.location.text())
        self.name.setText(self._auto_name)
        self._validate()

    @staticmethod
    def _next_free_name(settings, template=TEMPLATES[0], base=None):
        base = base or settings.value("projectsDir", default_repos_dir())
        prefix = TEMPLATE_INFO.get(template, ("", "WinFormsApp"))[1]
        n = 1
        while os.path.exists(os.path.join(base, f"{prefix}{n}")):
            n += 1
        return f"{prefix}{n}"

    def _template_changed(self, _row):
        """Like VS: the suggested name follows the template until you type your own."""
        if self.name.text() == self._auto_name:
            self._auto_name = self._next_free_name(QSettings(), self.template.currentText(), self.location.text())
            self.name.setText(self._auto_name)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Project location", self.location.text())
        if d:
            self.location.setText(d)

    @property
    def project_root(self):
        return os.path.join(os.path.expanduser(self.location.text().strip()), self.name.text().strip())

    def _validate(self):
        name = self.name.text().strip()
        err = ""
        if not name:
            err = "Enter a project name."
        elif not VALID_NAME.match(name):
            err = "Use letters, numbers, _ or - (must start with a letter or _)."
        elif os.path.exists(self.project_root):
            err = "A folder with that name already exists."
        self.preview.setText(f"Project will be created in: {self.project_root}"
                             + ("  (and added to the solution)" if self.add_to_solution else ""))
        self.error.setText(err)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(not err)

    def _accept(self):
        self._validate()
        if self.buttons.button(QDialogButtonBox.Ok).isEnabled():
            if not self.add_to_solution:
                QSettings().setValue("projectsDir", self.location.text().strip())
            QSettings().setValue("lastTemplate", self.template.currentText())
            if not self.add_to_solution:
                QSettings().setValue("newProjectGitHub", self.ck_github.isChecked())
            self.accept()
