"""Create GitHub Release: puts what Publish / Publish for Windows built (.deb, .tar.gz, .exe, Setup.exe) on the
project's GitHub Releases page, tagged with the project's version - so others (or your other computers) can
download it. Uses the gh command, like Publish to GitHub."""
import glob
import os
import re
import zipfile

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from . import ghpublish

KINDS = [("*.deb", "Ubuntu/Debian installer"), ("*.tar.gz", "Linux app (install.sh inside)"),
         ("*.AppImage", "Linux AppImage"), ("*-setup.exe", "Windows installer (Setup.exe)"),
         ("*.exe", "Windows program"), ("*.msi", "Windows installer"), ("*.zip", "zip")]


def _git(root, args):
    import subprocess
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, errors="replace", timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def github_repo(root):
    """owner/name of the project's GitHub repository, or "" (not on GitHub / gh missing / not signed in)."""
    if not ghpublish.gh_exe():
        return ""
    code, out, _ = ghpublish._gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=root,
                                 timeout=30)
    return out.strip() if code == 0 else ""


def find_assets(root, version=""):
    """[(path, description, checked)] - the files in publish/ worth attaching. The ones with this version in their
    name are checked; a downloaded Windows build (a folder) is offered as a zip."""
    pub = os.path.join(root, "publish")
    found, seen = [], set()
    for folder in (pub, os.path.join(pub, "windows"), os.path.join(pub, "windows-github")):
        for pattern, about in KINDS:
            for f in sorted(glob.glob(os.path.join(folder, pattern)), key=os.path.getmtime, reverse=True):
                if f in seen or not os.path.isfile(f):
                    continue
                seen.add(f)
                found.append([f, about, bool(version) and version in os.path.basename(f)])
    gh_dl = os.path.join(pub, "windows-github")
    if os.path.isdir(gh_dl) and not any(p.startswith(gh_dl + os.sep) for p, _a, _c in found):
        if any(files for _d, _s, files in os.walk(gh_dl)):
            found.append([gh_dl, "Windows build from Run on GitHub (zipped when uploaded)", True])
    # nothing has the version in its name: check the newest of each kind
    if found and not any(c for _p, _a, c in found):
        kinds = set()
        for item in found:
            if item[1] not in kinds:
                kinds.add(item[1])
                item[2] = True
    return [tuple(x) for x in found]


def notes_since_last_tag(root):
    code, last, _ = _git(root, ["describe", "--tags", "--abbrev=0"])
    rng = [f"{last}..HEAD"] if code == 0 and last else []
    _c, log, _e = _git(root, ["log", "--no-merges", "--pretty=format:- %s", *rng, "-n", "40"])
    lines = [ln for ln in log.splitlines() if ln.strip() and not re.match(r"- (Merge|Initial commit$)", ln)]
    head = f"Changes since {last}:\n" if rng else ""
    return (head + "\n".join(lines)) if lines else ""


class ReleaseDialog(QDialog):
    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.p = project
        self.root = project.root
        self.repo = github_repo(self.root)
        version = (project["version"] or "1.0.0").strip()
        self.setWindowTitle(f"Create GitHub Release - {project.name}")
        self.setMinimumSize(640, 560)
        v = QVBoxLayout(self)
        intro = QLabel(f"Puts what you published on <b>github.com/{self.repo}/releases</b>, where anyone who can "
                       "see the repository can download it. The version comes from Project Properties &gt; "
                       "Application.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        f = QFormLayout()
        self.e_tag = QLineEdit("v" + version.lstrip("v"))
        self.e_tag.setToolTip("The release's tag - a name for this exact version of the code")
        f.addRow("Version tag:", self.e_tag)
        self.e_title = QLineEdit(f"{project.name} {version}")
        f.addRow("Title:", self.e_title)
        self.e_notes = QPlainTextEdit(notes_since_last_tag(self.root))
        self.e_notes.setPlaceholderText("What's new in this version (shown on the release page)")
        self.e_notes.setMaximumHeight(130)
        f.addRow("Notes:", self.e_notes)
        self.ck_pre = QCheckBox("Pre-release (a test version - not shown as the latest)")
        f.addRow("", self.ck_pre)
        v.addLayout(f)
        v.addWidget(QLabel("<b>Files to attach</b> (from the project's publish folder):"))
        self.files = QListWidget()
        for path, about, checked in find_assets(self.root, version):
            self._add(path, about, checked)
        v.addWidget(self.files, 1)
        h = QHBoxLayout()
        b_add = QPushButton("Add File...")
        b_add.clicked.connect(self._add_file)
        h.addWidget(b_add)
        self.l_none = QLabel()
        self.l_none.setStyleSheet("color: #e5c07b;")
        self.l_none.setWordWrap(True)
        h.addWidget(self.l_none, 1)
        v.addLayout(h)
        names = [os.path.basename(self.files.item(i).data(Qt.UserRole)).lower() for i in range(self.files.count())]
        if not names:
            self.l_none.setText("Nothing published yet - use Build > Publish (Linux) or Publish for Windows first, "
                                "or add a file.")
        else:
            missing = []
            if not any(n.endswith((".deb", ".tar.gz", ".appimage")) for n in names):
                missing.append("no Linux build (.deb / .tar.gz) - Build > Publish makes it")
            if not any(n.endswith((".exe", ".msi")) or "windows" in n for n in names):
                missing.append("no Windows build - Build > Publish for Windows makes it")
            self.l_none.setText(("Not found: " + "; ".join(missing) + ".") if missing else "")
        self.l_state = QLabel()
        self.l_state.setWordWrap(True)
        self.l_state.setStyleSheet("color: #9d9d9d;")
        v.addWidget(self.l_state)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("Create Release")
        self.bb.accepted.connect(self.create)
        self.bb.rejected.connect(self.reject)
        v.addWidget(self.bb)
        self.proc = None
        self.url = ""
        self._explain_state()

    def _add(self, path, about, checked=True):
        size = ""
        if os.path.isfile(path):
            size = f"  ({os.path.getsize(path) / 1e6:.1f} MB)"
        it = QListWidgetItem(f"{os.path.relpath(path, self.root) if path.startswith(self.root) else path}"
                             f"   - {about}{size}")
        it.setData(Qt.UserRole, path)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.files.addItem(it)

    def _add_file(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach Files", os.path.join(self.root, "publish"))
        for path in paths:
            self._add(path, "added", True)
        if paths:
            self.l_none.setText("")

    def chosen(self):
        return [self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())
                if self.files.item(i).checkState() == Qt.Checked]

    def _explain_state(self):
        _c, st, _e = _git(self.root, ["status", "--porcelain", "-b"])
        lines = st.splitlines()
        notes = []
        if lines and "ahead" in lines[0]:
            notes.append("Your latest commits aren't on GitHub yet - they're pushed first.")
        if any(ln and not ln.startswith("##") for ln in lines):
            notes.append("Some changes aren't committed - the release is made from the last commit, without them.")
        self.l_state.setText("  ".join(notes))

    # ---------------------------------------------------------- create
    def create(self):
        tag = self.e_tag.text().strip()
        files = self.chosen()
        if not tag:
            QMessageBox.information(self, "Create GitHub Release", "Enter a version tag, like v1.0.0.")
            return
        if not files and QMessageBox.question(self, "Create GitHub Release", "No files are checked - create the "
                                              "release with only the source code?") != QMessageBox.Yes:
            return
        try:
            files = [self._zipped(f) if os.path.isdir(f) else f for f in files]
        except OSError as e:
            QMessageBox.warning(self, "Create GitHub Release", f"Couldn't zip the folder:\n{e}")
            return
        _c, branch, _e = _git(self.root, ["rev-parse", "--abbrev-ref", "HEAD"])
        _c, st, _e = _git(self.root, ["status", "--porcelain", "-b"])
        if st.splitlines() and ("ahead" in st.splitlines()[0] or "..." not in st.splitlines()[0]):
            code, _o, err = _git(self.root, ["push", "-u", "origin", branch or "HEAD"])
            if code != 0:
                QMessageBox.warning(self, "Create GitHub Release", "Couldn't push your commits first:\n\n" + err[-800:])
                return
        exists = ghpublish._gh(["release", "view", tag], cwd=self.root, timeout=30)[0] == 0
        if exists:
            if QMessageBox.question(
                    self, "Create GitHub Release", f"{tag} is already released. Replace its files with these?\n\n"
                    "(For a new version, change the version in Project Properties > Application, Publish again, "
                    "and use the new tag.)") != QMessageBox.Yes:
                return
            args = ["release", "upload", tag, *files, "--clobber"]
        else:
            args = ["release", "create", tag, *files, "--title", self.e_title.text().strip() or tag,
                    "--notes", self.e_notes.toPlainText().strip() or f"{self.p.name} {tag}"]
            if branch and branch != "HEAD":
                args += ["--target", branch]
            if self.ck_pre.isChecked():
                args.append("--prerelease")
        self._run(args, tag)

    def _zipped(self, folder):
        dest = os.path.join(self.root, "publish",
                            f"{ghpublish.safe_name(self.p.name)}-{self.e_tag.text().strip().lstrip('v')}-windows.zip")
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for d, _s, names in os.walk(folder):
                for n in names:
                    full = os.path.join(d, n)
                    z.write(full, os.path.relpath(full, folder))
        return dest

    def _run(self, args, tag):
        self.bb.setEnabled(False)
        self.l_state.setText("Uploading to GitHub... (large files take a while)")
        self.proc = QProcess(self)
        env = QProcessEnvironment()
        for k, val in ghpublish._env().items():
            env.insert(k, val)
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(self.root)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)

        def done(code, _s):
            out = bytes(self.proc.readAll()).decode("utf-8", "replace").strip()
            self.proc = None
            self.bb.setEnabled(True)
            if code != 0:
                self.l_state.setText("")
                QMessageBox.warning(self, "Create GitHub Release", (out or "gh failed.")[-1500:])
                return
            m = re.search(r"https://github\.com/\S+", out)
            self.url = m.group(0) if m else f"https://github.com/{self.repo}/releases/tag/{tag}"
            win = self.parent().window() if self.parent() is not None else None
            if win is not None and hasattr(win, "statusBar"):
                win.statusBar().showMessage(f"Released {tag}: {self.url}", 10000)
            box = QMessageBox(QMessageBox.Information, "Create GitHub Release",
                              f"Released <a href='{self.url}'>{tag}</a>.", QMessageBox.Ok, self)
            b = box.addButton("Open on GitHub", QMessageBox.ActionRole)
            box.exec()
            if box.clickedButton() is b:
                QDesktopServices.openUrl(QUrl(self.url))
            self.accept()
        self.proc.finished.connect(done)
        self.proc.start(ghpublish.gh_exe() or "gh", args)

    def reject(self):
        if self.proc is not None:
            return                     # uploading: wait
        super().reject()


def create_release(project, parent):
    """Git > Create GitHub Release (also on the Publish pages)."""
    if project is None:
        QMessageBox.information(parent, "Create GitHub Release", "Open a project first.")
        return
    if not ghpublish.ensure_signed_in(parent):
        return
    if not github_repo(project.root):
        win = parent.window() if parent is not None else None
        panel = getattr(win, "git_panel", None)
        if QMessageBox.question(parent, "Create GitHub Release", "This project isn't on GitHub yet. Publish it "
                                "to GitHub first? (A private repository named after the project.)") != QMessageBox.Yes \
                or panel is None:
            return
        if not panel.git.root:
            panel.create_repository()
        panel.publish_github()
        if not github_repo(project.root):
            return
    ReleaseDialog(project, parent).exec()

