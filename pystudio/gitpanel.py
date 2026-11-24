"""Git Changes (VS: View > Git Changes): branch, fetch/pull/push/sync, commit, staged and
unstaged changes, diffs and history - git run in the background so the IDE never freezes."""
import os
import re
import shutil
import subprocess
import sys

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)
from . import theme

GITIGNORE = """# Made by PyStudio - files Git shouldn't track
__pycache__/
*.pyc
publish/
build/
dist/
.venv/
venv/
*.spec
"""

STATUS_NAMES = {"M": "Modified", "A": "Added", "D": "Deleted", "R": "Renamed", "C": "Copied",
                "?": "New (untracked)", "U": "Conflict"}
STATUS_COLORS = {"M": "#d7ba7d", "A": "#89d185", "D": "#f48771", "R": "#75beff", "C": "#75beff",
                 "?": "#89d185", "U": "#f14c4c"}


def git_available():
    return shutil.which("git") is not None


def _askpass_script():
    """A tiny executable that shows PyStudio's sign-in dialog (git needs a program path)."""
    folder = os.path.join(os.path.expanduser("~"), ".local", "share", "pystudio")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "git-askpass.sh")
    py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "askpass.py")
    text = f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n'
    try:
        old = open(path).read() if os.path.exists(path) else ""
        if old != text:
            with open(path, "w") as f:
                f.write(text)
        os.chmod(path, 0o755)
    except OSError:
        return None
    return path


# ============================================================ git runner
class GitService(QObject):
    log = Signal(str)               # commands and their output (Output panel)
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.folder = None          # the open project folder
        self.root = None            # its repository's top folder (None: not a repo)
        self._queue = []
        self._proc = None

    # ---------------------------------------------------------- setup
    def set_folder(self, folder):
        self.folder = folder
        self.root = None
        if folder and git_available():
            code, out, _err = self.run_sync(["rev-parse", "--show-toplevel"], cwd=folder)
            if code == 0 and out.strip():
                self.root = os.path.realpath(out.strip())

    def env(self):
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_PAGER": "cat", "GIT_EDITOR": "true"})
        ask = _askpass_script()
        if ask:
            env.update({"GIT_ASKPASS": ask, "SSH_ASKPASS": ask, "SSH_ASKPASS_REQUIRE": "force"})
        return env

    def run_sync(self, args, cwd=None, timeout=20):
        """Quick read-only commands (status, diff, log). Returns (code, stdout, stderr)."""
        try:
            r = subprocess.run(["git", *args], cwd=cwd or self.root or self.folder, env=self.env(),
                               capture_output=True, text=True, errors="replace", timeout=timeout)
            return r.returncode, r.stdout, r.stderr
        except (OSError, subprocess.SubprocessError) as e:
            return 1, "", str(e)

    @property
    def busy(self):
        return self._proc is not None

    def run(self, args, done=None):
        """Commands that can take a while or ask for a password (push, pull, commit...)."""
        self._queue.append((args, done))
        if self._proc is None:
            self._next()

    def _next(self):
        if not self._queue:
            self.busy_changed.emit(False)
            return
        args, done = self._queue.pop(0)
        p = QProcess(self)
        env = QProcessEnvironment()
        for k, v in self.env().items():
            env.insert(k, v)
        p.setProcessEnvironment(env)
        p.setWorkingDirectory(self.root or self.folder or os.getcwd())
        self._proc = p
        self.busy_changed.emit(True)
        self.log.emit(f"> git {' '.join(_quote(a) for a in args)}\n")

        def finished(code, _status):
            out = bytes(p.readAllStandardOutput()).decode("utf-8", "replace")
            err = bytes(p.readAllStandardError()).decode("utf-8", "replace")
            if out.strip() or err.strip():
                self.log.emit((out + err).rstrip() + "\n")
            self._proc = None
            p.deleteLater()
            if done:
                done(code, out, err)
            self._next()
        p.finished.connect(finished)
        p.errorOccurred.connect(lambda _e: p.state() == QProcess.NotRunning and finished(1, None))
        p.start("git", args)

    # ---------------------------------------------------------- queries
    def status(self):
        """{'branch', 'upstream', 'ahead', 'behind', 'files': [(x, y, path, orig)]} or None."""
        if not self.root:
            return None
        code, out, err = self.run_sync(["status", "--porcelain=v1", "-b", "-z", "--untracked-files=all"])
        if code != 0:
            return {"error": err.strip() or "git status failed", "files": [], "branch": "", "upstream": "",
                    "ahead": 0, "behind": 0}
        parts = out.split("\0")
        info = {"branch": "", "upstream": "", "ahead": 0, "behind": 0, "files": []}
        i = 0
        if parts and parts[0].startswith("## "):
            head = parts[0][3:]
            i = 1
            m = re.match(r"No commits yet on (\S+)", head) or re.match(r"Initial commit on (\S+)", head)
            if m:
                info["branch"] = m.group(1)
            else:
                m = re.match(r"([^.\s]+(?:\.[^.\s]+)*?)(?:\.\.\.(\S+))?(?: \[(.*)\])?$", head)
                if m:
                    info["branch"] = m.group(1)
                    info["upstream"] = m.group(2) or ""
                    for k, n in re.findall(r"(ahead|behind) (\d+)", m.group(3) or ""):
                        info[k] = int(n)
                else:
                    info["branch"] = head
        while i < len(parts):
            e = parts[i]
            i += 1
            if len(e) < 4:
                continue
            x, y, path = e[0], e[1], e[3:]
            orig = ""
            if x in "RC":
                orig = parts[i] if i < len(parts) else ""
                i += 1
            info["files"].append((x, y, path, orig))
        return info

    def branches(self):
        code, out, _ = self.run_sync(["branch", "--format=%(refname:short)"])
        return [b.strip() for b in out.splitlines() if b.strip()] if code == 0 else []

    def remotes(self):
        code, out, _ = self.run_sync(["remote"])
        return [r.strip() for r in out.splitlines() if r.strip()] if code == 0 else []

    def identity(self):
        name = self.run_sync(["config", "user.name"])[1].strip()
        email = self.run_sync(["config", "user.email"])[1].strip()
        return name, email

    def has_commits(self):
        return self.run_sync(["rev-parse", "--verify", "HEAD"])[0] == 0

    def merging(self):
        """A merge is under way (pull or merge stopped on conflicts)."""
        if not self.root:
            return False
        code, out, _ = self.run_sync(["rev-parse", "--git-path", "MERGE_HEAD"])
        path = out.strip()
        if code != 0 or not path:
            return False
        return os.path.exists(path if os.path.isabs(path) else os.path.join(self.root, path))

    def diff(self, path, staged=False, untracked=False):
        if untracked:
            return self.run_sync(["diff", "--no-index", "--", "/dev/null", path])[1]
        if staged:
            return self.run_sync(["diff", "--cached", "--", path])[1]
        if self.has_commits():
            return self.run_sync(["diff", "HEAD", "--", path])[1]
        return self.run_sync(["diff", "--", path])[1]

    def log_entries(self, limit=300, path=None):
        args = ["log", f"-n{limit}", "--date=format:%Y-%m-%d %H:%M", "--pretty=format:%h%x1f%ad%x1f%an%x1f%s%x1f%D"]
        if path:
            args += ["--follow", "--", path]
        code, out, _ = self.run_sync(args)
        rows = []
        for line in out.splitlines() if code == 0 else []:
            f = line.split("\x1f")
            if len(f) >= 4:
                rows.append({"hash": f[0], "date": f[1], "author": f[2], "subject": f[3],
                             "refs": f[4] if len(f) > 4 else ""})
        return rows

    def show(self, commit):
        return self.run_sync(["show", "--stat", "--patch", "--format=commit %H%nAuthor: %an <%ae>%nDate:   %ad%n%n    %s%n%n%b", commit])[1]


def _quote(a):
    return a if re.fullmatch(r"[\w./:=@%+-]+", a) else '"' + a.replace('"', '\\"') + '"'


# ============================================================ diff view (a document tab)
class _DiffHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        fmt = QTextCharFormat()
        if text.startswith(("+++", "---")):
            fmt.setForeground(QColor(theme.c("#cccccc")))
            fmt.setFontWeight(QFont.Bold)
        elif text.startswith("+"):
            fmt.setBackground(QColor(theme.c("#1e3a1e")))
            fmt.setForeground(QColor(theme.c("#b5e8b0")))
        elif text.startswith("-"):
            fmt.setBackground(QColor(theme.c("#4b1e1e")))
            fmt.setForeground(QColor(theme.c("#f5b0b0")))
        elif text.startswith("@@"):
            fmt.setForeground(QColor(theme.c("#569cd6")))
        elif text.startswith(("commit ", "diff --git", "index ", "Author:", "Date:", "new file", "deleted file")):
            fmt.setForeground(QColor(theme.c("#808080")))
        else:
            return
        self.setFormat(0, len(text), fmt)


class DiffView(QPlainTextEdit):
    """Read-only diff in a tab (VS: Compare with Unmodified)."""
    dirty_changed = Signal(bool)

    def __init__(self, title, text, parent=None):
        super().__init__(parent)
        self.path = None
        self.title = title
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.setFont(f)
        self.setStyleSheet("QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; border: 0; }")
        self._hl = _DiffHighlighter(self.document())
        self.setPlainText(text or "(no differences)")

    @property
    def is_dirty(self):
        return False

    def display_name(self):
        return self.title


# ============================================================ history
class HistoryDialog(QDialog):
    """View History: commits on the left, the selected commit's changes on the right."""

    def __init__(self, git, path=None, parent=None):
        super().__init__(parent)
        self.git = git
        self.setWindowTitle("History - " + (os.path.basename(path) if path else os.path.basename(git.root or "")))
        self.resize(1100, 650)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["ID", "Date", "Author", "Message"])
        self.list.setRootIsDecorated(False)
        self.list.setColumnWidth(0, 80)
        self.list.setColumnWidth(1, 130)
        self.list.setColumnWidth(2, 120)
        self.view = DiffView("", "")
        sp = QSplitter(Qt.Vertical)
        sp.addWidget(self.list)
        sp.addWidget(self.view)
        sp.setSizes([260, 390])
        lay = QVBoxLayout(self)
        lay.addWidget(sp)
        for e in git.log_entries(path=path):
            subject = e["subject"] + (f"   [{e['refs']}]" if e["refs"] else "")
            it = QTreeWidgetItem([e["hash"], e["date"], e["author"], subject])
            it.setData(0, Qt.UserRole, e["hash"])
            self.list.addTopLevelItem(it)
        self.list.currentItemChanged.connect(self._show)
        if self.list.topLevelItemCount():
            self.list.setCurrentItem(self.list.topLevelItem(0))
        else:
            self.view.setPlainText("No commits yet.")

    def _show(self, it, _prev):
        if it is not None:
            self.view.setPlainText(self.git.show(it.data(0, Qt.UserRole)))


# ============================================================ the panel
class GitChangesPanel(QWidget):
    open_diff = Signal(str, str)        # title, diff text
    open_file = Signal(str)
    files_changed = Signal()            # working files may have changed (pull, checkout, undo)
    status_changed = Signal(str)        # short text for the status bar ("main ↑1")
    open_merge = Signal(str)            # a conflicted file -> the Merge Editor

    def __init__(self, git, parent=None):
        super().__init__(parent)
        self.git = git
        self._filling = False
        git.busy_changed.connect(self._busy)

        # --- not a repository
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        self.empty_label = QLabel()
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet("color: #aaaaaa;")
        self.b_create = QPushButton("Create Git Repository...")
        self.b_create.clicked.connect(self.create_repository)
        el.addWidget(self.empty_label)
        el.addWidget(self.b_create)
        el.addStretch()

        # --- repository
        self.main = QWidget()
        self.branch = QComboBox()
        self.branch.setToolTip("Current branch - pick another to switch to it")
        self.branch.activated.connect(self._branch_picked)

        def tb(text, tip, slot):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            if slot is not None:
                b.clicked.connect(slot)
            return b
        self.b_fetch = tb("⟳", "Fetch (check the remote for new commits)", self.fetch)
        self.b_pull = tb("↓", "Pull (get the remote's commits)", self.pull)
        self.b_push = tb("↑", "Push (send your commits to the remote)", self.push)
        self.b_sync = tb("⇅", "Sync (pull, then push)", self.sync)
        self.b_more = tb("⋯", "More: branches, history, remotes, sign-in", None)
        self.b_more.setPopupMode(QToolButton.InstantPopup)
        self.b_more.setMenu(self._more_menu())
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.branch, 1)
        for b in (self.b_fetch, self.b_pull, self.b_push, self.b_sync, self.b_more):
            top.addWidget(b)
        self.info = QLabel()
        self.info.setStyleSheet("color: #aaaaaa; padding: 0 2px;")
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("Enter a message <Required>")
        self.message.setMaximumHeight(70)
        self.b_commit = QToolButton()
        self.b_commit.setText("Commit All")
        self.b_commit.setPopupMode(QToolButton.MenuButtonPopup)
        self.b_commit.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.b_commit.clicked.connect(lambda: self.commit())
        cm = QMenu(self)
        cm.addAction("Commit and Push", lambda: self.commit(then="push"))
        cm.addAction("Commit and Sync", lambda: self.commit(then="sync"))
        self.b_commit.setMenu(cm)
        self.b_commit.setStyleSheet("QToolButton { background: #0e639c; color: white; padding: 4px 10px; }"
                                    "QToolButton:disabled { background: #3a3d41; color: #888; }")
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setRootIsDecorated(True)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        ml = QVBoxLayout(self.main)
        ml.setContentsMargins(4, 4, 4, 4)
        ml.addLayout(top)
        ml.addWidget(self.info)
        ml.addWidget(self.message)
        ml.addWidget(self.b_commit, 0, Qt.AlignLeft)
        ml.addWidget(self.tree, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.empty)
        lay.addWidget(self.main)
        self.refresh()

    # ---------------------------------------------------------- refresh
    def set_folder(self, folder):
        self.git.set_folder(folder)
        self.refresh()

    def refresh(self):
        g = self.git
        repo = bool(g.root)
        self.empty.setVisible(not repo)
        self.main.setVisible(repo)
        if not repo:
            if not git_available():
                self.empty_label.setText("Git isn't installed. Install it with:\n  sudo apt install git\n"
                                         "then reopen the project.")
                self.b_create.hide()
            elif not g.folder:
                self.empty_label.setText("Open a project to use Git.")
                self.b_create.hide()
            else:
                self.empty_label.setText("This project isn't in a Git repository yet. Git keeps every "
                                         "version of your code so you can see what changed and undo mistakes, "
                                         "and can back it up to GitHub.")
                self.b_create.show()
            self.status_changed.emit("")
            return
        st = g.status() or {}
        if st.get("error"):
            self.info.setText(st["error"])
        self._filling = True
        self.branch.clear()
        names = g.branches() or [st.get("branch") or "main"]
        if st.get("branch") and st["branch"] not in names:
            names.insert(0, st["branch"])
        self.branch.addItems(names)
        self.branch.setCurrentText(st.get("branch", ""))
        self._filling = False
        up = st.get("upstream")
        parts = [f"↑{st.get('ahead', 0)} ↓{st.get('behind', 0)}  {up}" if up else
                 ("no remote - ⋯ > Add Remote" if not g.remotes() else "not pushed yet")]
        self.info.setText("  ".join(parts))
        conflict_codes = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
        conflicted = [f for f in st.get("files", []) if f[0] + f[1] in conflict_codes]
        others = [f for f in st.get("files", []) if f[0] + f[1] not in conflict_codes]
        staged = [f for f in others if f[0] not in " ?"]
        changed = [f for f in others if f[1] != " "]
        self.merging = g.merging()
        if self.merging:
            self.info.setText(("Merging - double-click each file under Merge Conflicts to resolve it, then Commit."
                               if conflicted else "Merging - conflicts resolved. Commit to finish the merge.")
                              + "  (⋯ > Abort Merge to cancel)")
            self.message.setPlaceholderText("Message (optional - Git's merge message is used if empty)")
        else:
            self.message.setPlaceholderText("Enter a message <Required>")
        self.tree.clear()
        for title, files, is_staged in (("Merge Conflicts", conflicted, None), ("Staged Changes", staged, True),
                                        ("Changes", changed, False)):
            if is_staged is not False and not files:
                continue
            head = QTreeWidgetItem([f"{title} ({len(files)})", ""])
            f = head.font(0)
            f.setBold(True)
            head.setFont(0, f)
            head.setData(0, Qt.UserRole, None)
            self.tree.addTopLevelItem(head)
            for x, y, path, orig in sorted(files, key=lambda t: t[2].lower()):
                code = "U" if is_staged is None else x if is_staged else ("?" if y == "?" else y)
                folder = os.path.dirname(path)
                it = QTreeWidgetItem([os.path.basename(path) + (f"   {folder}" if folder else ""), code])
                it.setForeground(1, QColor(theme.c(STATUS_COLORS.get(code, "#cccccc"))))
                it.setToolTip(0, f"{path} - {STATUS_NAMES.get(code, code)}" + (f" (from {orig})" if orig else ""))
                it.setData(0, Qt.UserRole, (path, bool(is_staged), code))
                if is_staged is None:
                    it.setToolTip(0, f"{path} - conflict: double-click to resolve it in the Merge Editor")
                    head.setForeground(0, QColor(theme.c(STATUS_COLORS.get("U", "#e5534b"))))
                head.addChild(it)
            head.setExpanded(True)
        self.tree.resizeColumnToContents(1)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        n = len(st.get("files", []))
        self.b_commit.setText("Commit Merge" if self.merging else "Commit Staged" if staged else "Commit All")
        self.b_commit.setEnabled((bool(n) or self.merging) and not g.busy and not conflicted)
        self.b_commit.setToolTip("Resolve every file under Merge Conflicts first" if conflicted else "")
        ahead, behind = st.get("ahead", 0), st.get("behind", 0)
        self.status_changed.emit(f"⎇ {st.get('branch', '')}" + (f"  ↑{ahead}" if ahead else "") +
                                 (f" ↓{behind}" if behind else "") + (f"  ✎{n}" if n else ""))

    def _busy(self, busy):
        for b in (self.b_fetch, self.b_pull, self.b_push, self.b_sync, self.b_commit, self.branch):
            b.setEnabled(not busy)
        if busy:
            self.info.setText("Working...")
        else:
            self.refresh()

    # ---------------------------------------------------------- helpers
    def _say(self, title, code, out, err, ok_text=None):
        if code == 0:
            if ok_text:
                self.window().statusBar().showMessage(ok_text, 4000)
            return True
        msg = (err or out).strip() or "Git failed."
        if "could not read Username" in msg or "Authentication failed" in msg or "terminal prompts disabled" in msg:
            msg += ("\n\nSign-in failed or was canceled. For GitHub over https, use a personal access "
                    "token as the password (github.com > Settings > Developer settings).")
        QMessageBox.warning(self, title, msg[-1500:])
        return False

    def ensure_identity(self):
        name, email = self.git.identity()
        if name and email:
            return True
        dlg = QDialog(self)
        dlg.setWindowTitle("Who are you? (for your commits)")
        f = QFormLayout(dlg)
        e_name, e_mail = QLineEdit(name), QLineEdit(email)
        e_name.setPlaceholderText("Rob Smith")
        e_mail.setPlaceholderText("you@example.com")
        f.addRow(QLabel("Git records a name and email with every commit. This is saved once for all "
                        "your projects (git config --global)."))
        f.addRow("Name:", e_name)
        f.addRow("Email:", e_mail)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() != QDialog.Accepted or not e_name.text().strip() or not e_mail.text().strip():
            return False
        self.git.run_sync(["config", "--global", "user.name", e_name.text().strip()])
        self.git.run_sync(["config", "--global", "user.email", e_mail.text().strip()])
        return True

    # ---------------------------------------------------------- actions
    def create_repository(self):
        folder = self.git.folder
        if not folder or not self.ensure_identity():
            return
        gi = os.path.join(folder, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write(GITIGNORE)
        code, out, err = self.git.run_sync(["init", "-b", "main"], cwd=folder)
        if not self._say("Create Git Repository", code, out, err):
            return
        self.git.set_folder(folder)
        self.git.run_sync(["add", "-A"])
        code, out, err = self.git.run_sync(["commit", "-m", "Initial commit"])
        self._say("Create Git Repository", code, out, err, "Created the repository with an initial commit.")
        self.refresh()

    def commit(self, then=None):
        msg = self.message.toPlainText().strip()
        if getattr(self, "merging", False):
            def done_merge(code, out, err):
                if self._say("Commit", code, out, err, "Merge committed."):
                    self.message.clear()
                    if then == "push":
                        self.push()
            self.window().save_all() if hasattr(self.window(), "save_all") else None
            self.git.run(["commit", "-m", msg] if msg else ["commit", "--no-edit"], done_merge)
            return
        if not msg:
            QMessageBox.information(self, "Commit", "Enter a commit message first (what did you change?).")
            self.message.setFocus()
            return
        if not self.ensure_identity():
            return
        self.window().save_all() if hasattr(self.window(), "save_all") else None
        st = self.git.status() or {}
        staged = any(f[0] not in " ?" for f in st.get("files", []))
        if not staged:
            self.git.run(["add", "-A"])

        def done(code, out, err):
            if self._say("Commit", code, out, err, "Committed."):
                self.message.clear()
                if then == "push":
                    self.push()
                elif then == "sync":
                    self.sync()
        self.git.run(["commit", "-m", msg], done)

    def fetch(self):
        if self._need_remote():
            self.git.run(["fetch", "--prune"], lambda c, o, e: self._say("Fetch", c, o, e, "Fetched."))

    def pull(self, then=None):
        if not self._need_remote():
            return

        def done(code, out, err):
            self.files_changed.emit()
            if self._say("Pull", code, out, err, "Pulled.") and then:
                then()
        self.git.run(["pull", "--no-rebase", "--no-edit"], done)

    def push(self):
        if not self._need_remote():
            return
        st = self.git.status() or {}
        args = ["push"] if st.get("upstream") else ["push", "-u", self.git.remotes()[0], st.get("branch", "main")]
        self.git.run(args, lambda c, o, e: self._say("Push", c, o, e, "Pushed."))

    def sync(self):
        st = self.git.status() or {}
        if not st.get("upstream"):
            return self.push()
        self.pull(then=self.push)

    def _need_remote(self):
        if self.git.remotes():
            return True
        return self.add_remote()

    def add_remote(self):
        url, ok = QInputDialog.getText(
            self, "Add Remote",
            "Where should this project be pushed? Paste the repository's URL.\n"
            "On GitHub: create a new, empty repository (no README), then copy its URL, e.g.\n"
            "https://github.com/you/MyProject.git", text="https://github.com/")
        url = url.strip()
        if not ok or not url or url.rstrip("/") == "https://github.com":
            return False
        code, out, err = self.git.run_sync(["remote", "add", "origin", url])
        self.refresh()
        return self._say("Add Remote", code, out, err, "Remote 'origin' added.")

    def _branch_picked(self, _i):
        if self._filling:
            return
        name = self.branch.currentText()
        st = self.git.status() or {}
        if name == st.get("branch"):
            return

        def done(code, out, err):
            self.files_changed.emit()
            self._say("Switch Branch", code, out, err, f"Switched to {name}.")
        self.git.run(["checkout", name], done)

    def new_branch(self):
        name, ok = QInputDialog.getText(self, "New Branch", "Branch name (e.g. add-printing):")
        name = name.strip().replace(" ", "-")
        if ok and name:
            self.git.run(["checkout", "-b", name], lambda c, o, e: self._say("New Branch", c, o, e,
                                                                             f"Created and switched to {name}."))

    def delete_branch(self):
        others = [b for b in self.git.branches() if b != (self.git.status() or {}).get("branch")]
        if not others:
            QMessageBox.information(self, "Delete Branch", "There are no other branches.")
            return
        name, ok = QInputDialog.getItem(self, "Delete Branch", "Branch to delete:", others, 0, False)
        if ok and name:
            self.git.run(["branch", "-d", name], lambda c, o, e: self._say("Delete Branch", c, o, e, f"Deleted {name}."))

    def show_history(self, path=None):
        if self.git.root:
            HistoryDialog(self.git, path, self).exec()

    def _set_credentials(self, helper):
        self.git.run_sync(["config", "--global", "credential.helper", helper])
        self.window().statusBar().showMessage("Git will remember your sign-in.", 4000)

    def _more_menu(self):
        m = QMenu(self)
        m.addAction("New Branch...", self.new_branch)
        m.addAction("Delete Branch...", self.delete_branch)
        m.addSeparator()
        m.addAction("View History", lambda: self.show_history())
        m.addAction("Refresh", self.refresh)
        m.addSeparator()
        m.addAction("Add Remote...", self.add_remote)
        m.addSeparator()
        a = m.addAction("Abort Merge", self.abort_merge)
        m.aboutToShow.connect(lambda a=a: a.setVisible(self.git.merging()))
        sub = m.addMenu("Remember Sign-in")
        sub.addAction("For 8 hours (in memory)", lambda: self._set_credentials("cache --timeout=28800"))
        sub.addAction("Always (saved in ~/.git-credentials, plain text)", lambda: self._set_credentials("store"))
        return m

    # ---------------------------------------------------------- file items
    def _items(self):
        return [it.data(0, Qt.UserRole) for it in self.tree.selectedItems() if it.data(0, Qt.UserRole)]

    def _double_clicked(self, it, _col):
        d = it.data(0, Qt.UserRole)
        if d and d[2] == "U":
            self.open_merge.emit(os.path.join(self.git.root, d[0]))
        elif d:
            self.compare(*d)

    def abort_merge(self):
        if QMessageBox.question(self, "Abort Merge", "Cancel the merge and go back to how your files were before "
                                "it? Changes you made while resolving are lost.") != QMessageBox.Yes:
            return
        code, out, err = self.git.run_sync(["merge", "--abort"])
        self._say("Abort Merge", code, out, err, "Merge canceled.")
        self.files_changed.emit()
        self.refresh()

    def take_side(self, path, side):
        """Resolve a whole conflicted file with one side: 'ours' (current) or 'theirs' (incoming)."""
        code, out, err = self.git.run_sync(["checkout", f"--{side}", "--", path])
        if code == 0:
            code, out, err = self.git.run_sync(["add", "--", path])
        self._say("Resolve", code, out, err, None)
        self.files_changed.emit()
        self.refresh()

    def compare(self, path, staged, code):
        full = os.path.join(self.git.root, path)
        if code == "D" and not staged:
            text = self.git.diff(path)
        else:
            text = self.git.diff(path, staged=staged, untracked=(code == "?"))
        self.open_diff.emit(f"{os.path.basename(path)} (diff)", text or f"(no differences in {full})")

    def _menu(self, pos):
        items = self._items()
        if not items:
            return
        m = QMenu(self)
        paths = [p for p, _s, _c in items]
        if len(items) == 1 and items[0][2] == "U":
            p = items[0][0]
            m.addAction("Resolve in Merge Editor", lambda: self.open_merge.emit(os.path.join(self.git.root, p)))
            m.addAction("Take Current (yours) for the Whole File", lambda: self.take_side(p, "ours"))
            m.addAction("Take Incoming (theirs) for the Whole File", lambda: self.take_side(p, "theirs"))
            m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if any(s for _p, s, _c in items):
            m.addAction("Unstage", lambda: self._git_files(["restore", "--staged", "--"], paths))
        if any(not s for _p, s, _c in items):
            m.addAction("Stage", lambda: self._git_files(["add", "--"], paths))
        m.addSeparator()
        if len(items) == 1:
            p, s, c = items[0]
            m.addAction("Compare with Unmodified", lambda: self.compare(p, s, c))
            if c != "D":
                m.addAction("Open", lambda: self.open_file.emit(os.path.join(self.git.root, p)))
            m.addAction("View History", lambda: self.show_history(p))
        m.addSeparator()
        m.addAction("Undo Changes...", lambda: self.undo_changes(items))
        m.addAction("Ignore This File", lambda: self._ignore(paths))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _git_files(self, args, paths):
        code, out, err = self.git.run_sync(args + paths)
        self._say("Git", code, out, err)
        self.refresh()

    def undo_changes(self, items):
        names = "\n".join(p for p, _s, _c in items[:15]) + ("\n..." if len(items) > 15 else "")
        if QMessageBox.question(self, "Undo Changes",
                                f"Throw away your changes to these files? This can't be undone.\n\n{names}") \
                != QMessageBox.Yes:
            return
        has_head = self.git.has_commits()
        for path, _staged, code in items:
            full = os.path.join(self.git.root, path)
            if code == "?" or (code == "A" and not has_head) or code == "A":
                self.git.run_sync(["rm", "--cached", "-q", "--ignore-unmatch", "--", path])
                if os.path.isfile(full):
                    os.remove(full)
            else:
                self.git.run_sync(["restore", "--source=HEAD", "--staged", "--worktree", "--", path])
        self.files_changed.emit()
        self.refresh()

    def _ignore(self, paths):
        gi = os.path.join(self.git.root, ".gitignore")
        with open(gi, "a", encoding="utf-8") as f:
            for p in paths:
                f.write("\n/" + p.replace(os.sep, "/"))
        self.git.run_sync(["rm", "--cached", "-q", "--ignore-unmatch", "--", *paths])
        self.refresh()
