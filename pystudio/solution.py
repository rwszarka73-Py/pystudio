"""Solutions: several projects opened together (VS's .sln), e.g. an app plus its class libraries.

<Name>.pysln (JSON) sits in the solution folder; each project is a folder with its own .pyproj, listed by
its path relative to the solution folder ("." = the solution folder itself is a project, the usual case
when a single project grows into a solution):
    {"name": "MyApp", "projects": [".", "MathLib"], "startup": "."}

A project references another with "references": ["../MathLib"] in its .pyproj (path relative to the
referencing project). Referenced projects are put on the Python path when the program runs, in
IntelliSense, in tests and in Publish, so `import mathlib` just works."""
import glob
import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout)


def find_solution_file(folder):
    found = sorted(glob.glob(os.path.join(folder, "*.pysln")))
    return found[0] if found else None


def is_project_dir(folder):
    return bool(glob.glob(os.path.join(folder, "*.pyproj")))


def nested_projects(root, max_depth=3):
    """Folders under root (not root itself) that are projects of their own. Scans of one project skip them."""
    out = []
    root = os.path.abspath(root)
    base_depth = root.count(os.sep)
    for dirpath, dirnames, files in os.walk(root):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in
                       ("__pycache__", "venv", ".venv", "publish", "build", "dist", "node_modules")]
        if dirpath != root and any(f.endswith(".pyproj") for f in files):
            out.append(os.path.abspath(dirpath))
            dirnames[:] = []
    return out


class Solution:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.root = os.path.dirname(self.path)
        self.data = {"name": os.path.splitext(os.path.basename(path))[0], "projects": ["."], "startup": "."}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except (OSError, ValueError):
                pass

    @classmethod
    def create(cls, folder, name, projects=(".",), startup="."):
        s = cls(os.path.join(folder, name + ".pysln"))
        s.data = {"name": name, "projects": list(projects), "startup": startup}
        s.save()
        return s

    @property
    def name(self):
        return self.data.get("name") or os.path.splitext(os.path.basename(self.path))[0]

    def save(self):
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, indent=2)
            f.write("\n")

    def rel(self, root):
        r = os.path.relpath(os.path.abspath(root), self.root)
        return "." if r == "." else r.replace(os.sep, "/")

    def abs(self, rel):
        return os.path.normpath(os.path.join(self.root, rel))

    def projects(self):
        """Absolute roots of the projects that exist, in solution order."""
        return [self.abs(r) for r in self.data.get("projects", []) if os.path.isdir(self.abs(r))]

    def contains(self, root):
        return os.path.abspath(root) in self.projects()

    def add(self, root):
        r = self.rel(root)
        if r not in self.data["projects"]:
            self.data["projects"].append(r)
            self.save()

    def remove(self, root):
        r = self.rel(root)
        self.data["projects"] = [p for p in self.data["projects"] if p != r]
        if self.data.get("startup") == r:
            self.data["startup"] = self.data["projects"][0] if self.data["projects"] else "."
        self.save()

    def startup_root(self):
        s = self.abs(self.data.get("startup", "."))
        projs = self.projects()
        return s if s in projs else (projs[0] if projs else self.root)

    def set_startup(self, root):
        self.data["startup"] = self.rel(root)
        self.save()

    def project_for(self, path):
        """The deepest project of the solution that contains path."""
        path = os.path.abspath(path)
        best = None
        for root in self.projects():
            if path == root or path.startswith(root + os.sep):
                if best is None or len(root) > len(best):
                    best = root
        return best


# ============================================================ references
def references(project):
    """Absolute roots this project references (directly)."""
    out = []
    for r in project.data.get("references", []) or []:
        p = os.path.normpath(os.path.join(project.root, r))
        if os.path.isdir(p):
            out.append(p)
    return out


def all_references(project):
    """This project's references and theirs (transitively), in order, without repeats."""
    from .project import Project
    seen, order = {project.root}, []
    stack = list(references(project))
    while stack:
        root = stack.pop(0)
        if root in seen:
            continue
        seen.add(root)
        order.append(root)
        stack += references(Project(root))
    return order


def set_references(project, roots):
    project["references"] = [os.path.relpath(r, project.root).replace(os.sep, "/") for r in roots]
    project.save()


def python_path_env(project, base_env=None):
    """PYTHONPATH with the referenced projects (for running, debugging and tests)."""
    refs = all_references(project)
    if not refs:
        return {}
    existing = (base_env or {}).get("PYTHONPATH") or os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": os.pathsep.join(refs + ([existing] if existing else []))}


class ReferenceManager(QDialog):
    """Project > Add Reference: check the solution's projects this one uses (VS's Reference Manager)."""

    def __init__(self, project, solution, parent=None):
        super().__init__(parent)
        from .project import Project
        self.setWindowTitle(f"Reference Manager - {project.name}")
        self.setMinimumSize(460, 360)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("<b>Projects</b> in this solution that <b>" + project.name + "</b> can import:"))
        self.list = QListWidget()
        current = set(references(project))
        for root in solution.projects():
            if root == project.root:
                continue
            p = Project(root)
            it = QListWidgetItem(f"{p.name}    ({solution.rel(root)})")
            it.setData(Qt.UserRole, root)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if root in current else Qt.Unchecked)
            if project.root in all_references(p):
                it.setToolTip(f"{p.name} already uses {project.name} - referencing it back would be circular.")
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
            self.list.addItem(it)
        if not self.list.count():
            v.addWidget(QLabel("There are no other projects in the solution yet. Add one with\n"
                               "File > Add > New Project (for example a Class Library)."))
        v.addWidget(self.list, 1)
        tip = QLabel("A referenced project's folder is added to the Python path, so its packages and modules "
                     "can be imported: <font face='monospace'>from mathlib import Calculator</font>.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #9a9a9a;")
        v.addWidget(tip)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def checked(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]
