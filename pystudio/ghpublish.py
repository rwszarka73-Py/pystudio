"""Publish to GitHub (like Visual Studio's): creates the repository from the project's name and pushes - no
trip to github.com, no URL to paste. Signs in to GitHub first if needed (shows the one-time code and opens
the browser)."""
import os
import re
import shutil
import subprocess

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QUrl
from PySide6.QtGui import QCursor, QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QVBoxLayout)

DEVICE_URL = "https://github.com/login/device"


def _env():
    return dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1", GH_NO_UPDATE_NOTIFIER="1", GH_SPINNER_DISABLED="1")


def gh_exe():
    return shutil.which("gh")


def _gh(args, cwd=None, timeout=30):
    try:
        r = subprocess.run([gh_exe() or "gh", *args], cwd=cwd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, env=_env())
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def signed_in_user():
    """The GitHub account gh is signed in to, or "" when it isn't."""
    if not gh_exe():
        return ""
    code, out, _ = _gh(["api", "user", "-q", ".login"], timeout=20)
    return out.strip() if code == 0 else ""


def safe_name(name):
    """GitHub repository names: letters, digits, . _ - (spaces become -)."""
    n = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    return n or "my-project"


class SignInDialog(QDialog):
    """gh auth login --web, without a terminal: shows the one-time code, copies it, opens GitHub's page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sign in to GitHub")
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        self.l_info = QLabel("Starting...")
        self.l_info.setWordWrap(True)
        self.l_code = QLabel("")
        f = QFont()
        f.setPointSize(22)
        f.setBold(True)
        self.l_code.setFont(f)
        self.l_code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.b_open = QPushButton("Copy Code and Open GitHub")
        self.b_open.setEnabled(False)
        self.b_open.clicked.connect(self.open_page)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        bb.rejected.connect(self.reject)
        for w in (self.l_info, self.l_code, self.b_open, bb):
            v.addWidget(w)
        self.code = ""
        self.url = DEVICE_URL
        self._buf = ""
        self.proc = QProcess(self)
        env = QProcessEnvironment()
        for k, val in _env().items():
            env.insert(k, val)
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyRead.connect(self._read)
        self.proc.finished.connect(self._finished)
        # repo: create repositories; workflow: push Run on GitHub's workflow file
        self.proc.start(gh_exe() or "gh", ["auth", "login", "--web", "--hostname", "github.com",
                                           "--git-protocol", "https", "--scopes", "repo,workflow"])
        self.proc.closeWriteChannel()

    def _read(self):
        self._buf += bytes(self.proc.readAll()).decode("utf-8", "replace")
        m = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b", self._buf)
        if m and not self.code:
            self.code = m.group(1)
            u = re.search(r"https://\S+/login/device\S*", self._buf)
            self.url = u.group(0) if u else DEVICE_URL
            self.l_code.setText(self.code)
            self.l_info.setText("1. Click <b>Copy Code and Open GitHub</b> (your browser opens GitHub's sign-in "
                                "page).<br>2. Paste the code there (Ctrl+V), then <b>Authorize</b>.<br>"
                                "This window closes by itself when GitHub says you're done.")
            self.b_open.setEnabled(True)
            self.open_page()

    def open_page(self):
        QGuiApplication.clipboard().setText(self.code)
        QDesktopServices.openUrl(QUrl(self.url))

    def _finished(self, code, _status):
        if code == 0:
            _gh(["auth", "setup-git"])              # git push uses this sign-in from now on
            self.accept()
        else:
            QMessageBox.warning(self, "Sign in to GitHub", "GitHub sign-in didn't finish:\n\n"
                                + (self._buf.strip()[-800:] or "gh stopped."))
            self.reject()

    def reject(self):
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(2000)
        super().reject()


def ensure_signed_in(parent):
    """The GitHub account name, signing in first if needed; "" if that didn't happen."""
    if not gh_exe():
        from . import components
        if not components.ask_install(["gh"], parent, "Publishing to GitHub uses the GitHub CLI (gh)."):
            return ""
    user = signed_in_user()
    if user:
        _gh(["auth", "setup-git"])
        return user
    if QMessageBox.question(parent, "Publish to GitHub", "First, sign in to GitHub (once). Your browser opens "
                            "GitHub's sign-in page and you paste a code PyStudio shows you. Continue?") \
            != QMessageBox.Yes:
        return ""
    if SignInDialog(parent).exec() != QDialog.Accepted:
        return ""
    return signed_in_user()


class PublishDialog(QDialog):
    def __init__(self, user, name, parent=None, note=""):
        super().__init__(parent)
        self.setWindowTitle("Publish to GitHub")
        self.setMinimumWidth(460)
        f = QFormLayout(self)
        f.addRow("Account:", QLabel(f"<b>{user}</b>"))
        self.e_name = QLineEdit(safe_name(name))
        f.addRow("Repository name:", self.e_name)
        self.e_desc = QLineEdit()
        self.e_desc.setPlaceholderText("optional")
        f.addRow("Description:", self.e_desc)
        self.cb_private = QCheckBox("Private repository (only you can see it)")
        self.cb_private.setChecked(True)
        f.addRow("", self.cb_private)
        self.l_url = QLabel()
        self.l_url.setStyleSheet("color: #9d9d9d;")
        f.addRow("Will be:", self.l_url)
        self.e_name.textChanged.connect(lambda t: self.l_url.setText(f"github.com/{user}/{safe_name(t)}"))
        self.l_url.setText(f"github.com/{user}/{safe_name(name)}")
        if note:
            n = QLabel(note)
            n.setWordWrap(True)
            n.setStyleSheet("color: #e5c07b;")
            f.addRow(n)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Publish")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def name(self):
        return safe_name(self.e_name.text())


def publish(panel):
    """Git Changes > Publish to GitHub. panel is the GitPanel (its .git, ensure_identity, refresh)."""
    g = panel.git
    if not g.root:
        return
    if g.remotes():
        QMessageBox.information(panel, "Publish to GitHub", "This project already has a remote: use Push.")
        return
    if not panel.ensure_identity():
        return
    user = ensure_signed_in(panel)
    if not user:
        return
    win = panel.window()
    win.save_all() if hasattr(win, "save_all") else None
    st = g.status() or {}
    changed = len(st.get("files", []))
    first = not g.has_commits()
    note = ""
    if first:
        note = "Everything in the project is committed first (\"Initial commit\"), skipping what .gitignore lists."
    elif changed:
        note = f"{changed} changed file(s) aren't committed yet - they're committed first (\"Before publishing\")."
    name = os.path.basename(g.root.rstrip(os.sep))
    while True:
        dlg = PublishDialog(user, name, panel, note)
        if dlg.exec() != QDialog.Accepted:
            return
        name = dlg.name()
        if first or changed:
            g.run_sync(["add", "-A"], timeout=120)
            code, out, err = g.run_sync(["commit", "-m", "Initial commit" if first else "Before publishing"],
                                        timeout=120)
            if code != 0 and "nothing to commit" not in (out + err):
                QMessageBox.warning(panel, "Publish to GitHub", (err or out).strip()[-1200:])
                return
            first = changed = False
            note = ""
        args = ["repo", "create", name, "--private" if dlg.cb_private.isChecked() else "--public",
                "--source", g.root, "--remote", "origin", "--push"]
        if dlg.e_desc.text().strip():
            args += ["--description", dlg.e_desc.text().strip()]
        win.statusBar().showMessage(f"Publishing to github.com/{user}/{name}...")
        QGuiApplication.setOverrideCursor(QCursor(Qt.BusyCursor))
        try:
            code, out, err = _gh(args, cwd=g.root, timeout=600)
        finally:
            QGuiApplication.restoreOverrideCursor()
        panel.refresh()
        if code == 0:
            url = f"https://github.com/{user}/{name}"
            win.statusBar().showMessage(f"Published to {url}", 8000)
            box = QMessageBox(QMessageBox.Information, "Publish to GitHub",
                              f"Published to <a href='{url}'>{url}</a>.<br><br>From now on, <b>Commit and Push</b> "
                              "(or ↑) sends your changes there.", QMessageBox.Ok, panel)
            b = box.addButton("Open on GitHub", QMessageBox.ActionRole)
            box.exec()
            if box.clickedButton() is b:
                QDesktopServices.openUrl(QUrl(url))
            return
        msg = (err or out).strip()
        win.statusBar().clearMessage()
        if "already exists" in msg.lower():
            QMessageBox.information(panel, "Publish to GitHub", f"You already have a repository named {name}. "
                                    "Pick another name.")
            name = name + "-2"
            continue
        if g.remotes():                   # created, but the push failed (e.g. a file over 100 MB)
            msg += ("\n\nThe repository was created; fix the problem above, then use Push (↑).")
        QMessageBox.warning(panel, "Publish to GitHub", msg[-1500:] or "gh failed.")
        return
