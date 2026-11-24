"""Git tools beyond Git Changes: the Merge Editor (resolve conflicts), Blame (who changed each line, and when),
and Pull Requests on GitHub (through the gh command)."""
import datetime
import json
import os
import re
import shutil
import subprocess

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QTextCursor, QTextFormat
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTextEdit, QToolButton,
                               QVBoxLayout, QWidget)

from . import theme

CONFLICT = re.compile(r"^<<<<<<< ?(?P<ours_label>[^\n]*)\n(?P<ours>(?:(?!=======\n|\|\|\|\|\|\|\| ?)[^\n]*\n)*?)"
                      r"(?:\|\|\|\|\|\|\| ?[^\n]*\n(?P<base>(?:(?!=======\n)[^\n]*\n)*?))?"
                      r"=======\n(?P<theirs>(?:(?!>>>>>>> ?)[^\n]*\n)*?)>>>>>>> ?(?P<theirs_label>[^\n]*)(?:\n|$)",
                      re.M)


def _mono():
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setPointSize(10)
    return f


def conflicts(text):
    """[match] for each conflict block (<<<<<<< ... ======= ... >>>>>>>) in text."""
    return list(CONFLICT.finditer(text if text.endswith("\n") else text + "\n"))


def has_conflict_markers(text):
    return bool(conflicts(text))


def resolve(text, index, how):
    """text with conflict number index replaced by: 'current', 'incoming', 'both' (current first) or
    'both-incoming' (incoming first)."""
    trailing = text.endswith("\n")
    t = text if trailing else text + "\n"
    cs = conflicts(t)
    if not 0 <= index < len(cs):
        return text
    m = cs[index]
    ours, theirs = m.group("ours"), m.group("theirs")
    body = {"current": ours, "incoming": theirs, "both": ours + theirs, "both-incoming": theirs + ours}[how]
    out = t[:m.start()] + body + t[m.end():]
    return out if trailing else out[:-1] if out.endswith("\n") else out


# ============================================================ Merge Editor
class MergeEditor(QWidget):
    """VS's merge editor: Current (yours) and Incoming (theirs) above, the Result below. Take Current /
    Incoming / Both for each conflict, then Accept Merge (saves the file and marks it resolved: git add)."""
    dirty_changed = Signal(bool)
    saved = Signal(str)
    resolved = Signal(str)

    def __init__(self, git, path, parent=None):
        super().__init__(parent)
        self.git = git
        self.path = os.path.abspath(path)
        self.rel = os.path.relpath(self.path, git.root).replace(os.sep, "/")
        with open(self.path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        ours = git.run_sync(["show", f":2:{self.rel}"])
        theirs = git.run_sync(["show", f":3:{self.rel}"])
        cs = conflicts(text)
        branch = git.run_sync(["rev-parse", "--abbrev-ref", "HEAD"])[1].strip() or "HEAD"
        incoming = (cs[0].group("theirs_label").strip() if cs else "") or \
            git.run_sync(["name-rev", "--name-only", "MERGE_HEAD"])[1].strip() or "incoming"
        self.current_i = 0

        def pane(title, content):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(title)
            lbl.setStyleSheet("font-weight: bold; padding: 3px;")
            e = QPlainTextEdit()
            e.setReadOnly(True)
            e.setFont(_mono())
            e.setLineWrapMode(QPlainTextEdit.NoWrap)
            e.setPlainText(content)
            v.addWidget(lbl)
            v.addWidget(e)
            return w, e
        w_ours, self.ours = pane(f"Current (yours) - {branch}", ours[1] if ours[0] == 0 else "(not in your branch)")
        w_theirs, self.theirs = pane(f"Incoming (theirs) - {incoming}",
                                     theirs[1] if theirs[0] == 0 else "(not in the incoming branch)")
        top = QSplitter(Qt.Horizontal)
        top.addWidget(w_ours)
        top.addWidget(w_theirs)
        self.result = QPlainTextEdit()
        self.result.setFont(_mono())
        self.result.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.result.setPlainText(text)
        self.result.document().setModified(False)
        self.result.document().modificationChanged.connect(self.dirty_changed)
        self.result.textChanged.connect(self._refresh)
        res = QWidget()
        rv = QVBoxLayout(res)
        rv.setContentsMargins(0, 0, 0, 0)
        rl = QLabel("Result - edit it freely; conflicts are highlighted")
        rl.setStyleSheet("font-weight: bold; padding: 3px;")
        rv.addWidget(rl)
        rv.addWidget(self.result)
        split = QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(res)
        split.setSizes([300, 360])

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 4)

        def btn(text, tip, fn, primary=False):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            if primary:
                b.setStyleSheet("QPushButton { background: #0e639c; color: white; padding: 3px 12px; }"
                                "QPushButton:disabled { background: #3a3d41; color: #888; }")
            bar.addWidget(b)
            return b
        btn("◀", "Previous conflict", lambda: self.go(-1))
        self.lbl = QLabel()
        bar.addWidget(self.lbl)
        btn("▶", "Next conflict", lambda: self.go(1))
        bar.addSpacing(10)
        self.b_cur = btn("Take Current", "Keep your version of this conflict", lambda: self.take("current"))
        self.b_inc = btn("Take Incoming", "Use the incoming version of this conflict", lambda: self.take("incoming"))
        self.b_both = QToolButton()
        self.b_both.setText("Take Both")
        self.b_both.setPopupMode(QToolButton.MenuButtonPopup)
        self.b_both.clicked.connect(lambda: self.take("both"))
        from PySide6.QtWidgets import QMenu
        mb = QMenu(self.b_both)
        mb.addAction("Current first, then Incoming", lambda: self.take("both"))
        mb.addAction("Incoming first, then Current", lambda: self.take("both-incoming"))
        self.b_both.setMenu(mb)
        bar.addWidget(self.b_both)
        bar.addSpacing(10)
        btn("Take All Current", "Keep your version of every conflict", lambda: self.take_all("current"))
        btn("Take All Incoming", "Use the incoming version of every conflict", lambda: self.take_all("incoming"))
        bar.addStretch()
        self.b_accept = btn("Accept Merge", "Save the result and mark the file resolved (git add)", self.accept,
                            True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(bar)
        lay.addWidget(split, 1)
        self._refresh()
        QTimer.singleShot(0, lambda: self.go(0))

    # doc API
    @property
    def is_dirty(self):
        return self.result.document().isModified()

    def display_name(self):
        return os.path.basename(self.path) + " (Merge)"

    def setFocus(self):
        self.result.setFocus()

    def save(self, path=None):
        try:
            with open(path or self.path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.result.toPlainText())
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return False
        self.result.document().setModified(False)
        self.saved.emit(self.path)
        return True

    # conflicts
    def conflicts(self):
        return conflicts(self.result.toPlainText())

    def _refresh(self):
        cs = self.conflicts()
        n = len(cs)
        if self.current_i >= n:
            self.current_i = max(0, n - 1)
        self.lbl.setText(f" Conflict {self.current_i + 1} of {n} " if n else " No conflicts left ")
        for b in (self.b_cur, self.b_inc, self.b_both):
            b.setEnabled(bool(n))
        self.b_accept.setText("Accept Merge" if not n else f"Accept Merge ({n} left)")
        sels = []
        for i, m in enumerate(cs):
            s = QTextEdit.ExtraSelection()
            s.format.setBackground(QColor(210, 153, 34, 70 if i == self.current_i else 35))
            s.format.setProperty(QTextFormat.FullWidthSelection, True)
            c = QTextCursor(self.result.document())
            c.setPosition(m.start())
            c.setPosition(min(m.end(), len(self.result.toPlainText())), QTextCursor.KeepAnchor)
            s.cursor = c
            sels.append(s)
        self.result.setExtraSelections(sels)

    def go(self, delta):
        cs = self.conflicts()
        if not cs:
            return
        self.current_i = (self.current_i + delta) % len(cs)
        m = cs[self.current_i]
        c = self.result.textCursor()
        c.setPosition(m.start())
        self.result.setTextCursor(c)
        self.result.centerCursor()
        for pane, part in ((self.ours, m.group("ours")), (self.theirs, m.group("theirs"))):
            self._show_in(pane, part)
        self._refresh()

    def _show_in(self, pane, part):
        text = pane.toPlainText()
        i = text.find(part) if part else -1
        sels = []
        if i >= 0:
            c = QTextCursor(pane.document())
            c.setPosition(i)
            pane.setTextCursor(c)
            pane.centerCursor()
            s = QTextEdit.ExtraSelection()
            s.format.setBackground(QColor(60, 143, 216, 60))
            s.format.setProperty(QTextFormat.FullWidthSelection, True)
            s.cursor = QTextCursor(pane.document())
            s.cursor.setPosition(i)
            s.cursor.setPosition(i + len(part.rstrip("\n")), QTextCursor.KeepAnchor)
            sels.append(s)
        pane.setExtraSelections(sels)

    def _set_text(self, text):
        c = self.result.textCursor()
        c.beginEditBlock()
        c.select(QTextCursor.Document)
        c.insertText(text)
        c.endEditBlock()

    def take(self, how):
        text = resolve(self.result.toPlainText(), self.current_i, how)
        self._set_text(text)
        if self.conflicts():
            self.go(0)

    def take_all(self, how):
        text = self.result.toPlainText()
        while conflicts(text):
            text = resolve(text, 0, how)
        self._set_text(text)
        self._refresh()

    def accept(self):
        left = len(self.conflicts())
        if left and QMessageBox.question(self, "Accept Merge", f"{left} conflict(s) still have <<<<<<< markers. "
                                         "Accept anyway?") != QMessageBox.Yes:
            return
        if not self.save():
            return
        code, _out, err = self.git.run_sync(["add", "--", self.rel])
        if code != 0:
            QMessageBox.warning(self, "Accept Merge", err or "git add failed")
            return
        self.resolved.emit(self.path)


# ============================================================ Blame
def blame(git, path, text=None):
    """[{line, sha, author, time, summary, uncommitted}] for each line - with text (the editor's unsaved text),
    line numbers match the editor."""
    rel = os.path.relpath(os.path.abspath(path), git.root)
    args = ["git", "blame", "--line-porcelain"] + (["--contents", "-"] if text is not None else []) + ["--", rel]
    try:
        r = subprocess.run(args, cwd=git.root, env=git.env(), input=text, capture_output=True, text=True,
                           errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(str(e)) from None
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "git blame failed")
    out, cur = [], {}
    for line in r.stdout.split("\n"):
        if line.startswith("\t"):
            cur["code"] = line[1:]
            out.append(cur)
            cur = {}
        elif re.match(r"^[0-9a-f]{40} \d+ \d+", line):
            sha, _orig, final = line.split()[:3]
            cur = {"sha": sha, "line": int(final), "uncommitted": set(sha) == {"0"}}
        elif " " in line and cur is not None:
            k, v = line.split(" ", 1)
            if k == "author":
                cur["author"] = "You (not committed)" if v == "Not Committed Yet" else v
            elif k == "author-time":
                cur["time"] = int(v)
            elif k == "summary":
                cur["summary"] = v
    return out


def ago(ts):
    if not ts:
        return ""
    d = datetime.datetime.now() - datetime.datetime.fromtimestamp(ts)
    s = d.total_seconds()
    for n, unit in ((365 * 86400, "year"), (30 * 86400, "month"), (7 * 86400, "week"), (86400, "day"),
                    (3600, "hour"), (60, "minute")):
        if s >= n:
            k = int(s // n)
            return f"{k} {unit}{'s' if k > 1 else ''} ago"
    return "just now"


def inline_note(entry):
    """The current line's blame for the editor (GitLens style)."""
    if entry is None:
        return ""
    if entry.get("uncommitted"):
        return "You, not committed yet"
    return f"{entry.get('author', '')}, {ago(entry.get('time'))} • {entry.get('summary', '')}"[:120]


class BlameView(QWidget):
    """Git > Blame: the file with who last changed each line, when, and in which commit."""
    open_commit = Signal(str)
    open_line = Signal(str, int)
    dirty_changed = Signal(bool)

    def __init__(self, git, path, parent=None):
        super().__init__(parent)
        self.source_path = os.path.abspath(path)
        self.path = None                 # not the file itself: opening the file opens the editor, not this
        self.git = git
        rows = blame(git, path)
        self.table = QTableWidget(len(rows), 5)
        self.table.setHorizontalHeaderLabels(["Commit", "Author", "Date", "Line", "Code"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(18)
        h = self.table.horizontalHeader()
        for c in range(4):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.Stretch)
        mono = _mono()
        shade = [QColor(theme.c("#252526")), QColor(theme.c("#2d2d30"))]
        prev, k = None, 0
        for r, e in enumerate(rows):
            new = e["sha"] != prev
            if new:
                k ^= 1
                prev = e["sha"]
            date = datetime.datetime.fromtimestamp(e["time"]).strftime("%Y-%m-%d") if e.get("time") else ""
            cells = [("not committed" if e["uncommitted"] else e["sha"][:8]) if new else "",
                     e.get("author", "") if new else "", date if new else "", str(e["line"]), e.get("code", "")]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, e["sha"])
                it.setBackground(shade[k])
                if c in (3, 4):
                    it.setFont(mono)
                if c == 3:
                    it.setForeground(QColor("#858585"))
                if new and c < 3:
                    it.setToolTip(f"{e['sha'][:12]}  {e.get('author', '')}  {ago(e.get('time'))}\n"
                                  f"{e.get('summary', '')}\n\nDouble-click to see the commit")
                self.table.setItem(r, c, it)
        self.table.cellDoubleClicked.connect(self._double)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        info = QLabel(f"  {len({e['sha'] for e in rows})} commit(s) shaped this file. Double-click a commit to "
                      "see it, or a code line to go there.")
        info.setStyleSheet("color: #9a9a9a; padding: 3px;")
        lay.addWidget(info)
        lay.addWidget(self.table)

    is_dirty = False

    def display_name(self):
        return os.path.basename(self.source_path) + " (Blame)"

    def _double(self, row, col):
        sha = self.table.item(row, 0).data(Qt.UserRole)
        if col >= 3:
            self.open_line.emit(self.source_path, int(self.table.item(row, 3).text()))
        elif sha and set(sha) != {"0"}:
            self.open_commit.emit(sha)


# ============================================================ Pull Requests (gh)
def gh(args, cwd, timeout=60, input_text=None):
    exe = shutil.which("gh")
    if not exe:
        return 127, "", "The GitHub CLI isn't installed (Tools > Install Components > GitHub CLI)."
    try:
        r = subprocess.run([exe, *args], cwd=cwd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, input=input_text, env=dict(os.environ, GH_PROMPT_DISABLED="1",
                                                                       NO_COLOR="1", GH_NO_UPDATE_NOTIFIER="1"))
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout, r.stderr


class CreatePRDialog(QDialog):
    def __init__(self, branch, bases, title="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Pull Request")
        self.resize(560, 420)
        f = QFormLayout(self)
        self.title = QLineEdit(title)
        self.base = QComboBox()
        self.base.addItems(bases)
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText("What does this change, and why?")
        self.draft = QCheckBox("Draft (not ready for review yet)")
        f.addRow(QLabel(f"From <b>{branch}</b> into:"), self.base)
        f.addRow("Title:", self.title)
        f.addRow("Description:", self.body)
        f.addRow("", self.draft)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Create")
        bb.accepted.connect(lambda: self.title.text().strip() and self.accept())
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class PullRequestsWindow(QWidget):
    """Git > Pull Requests: this repository's pull requests on GitHub (via gh)."""
    open_diff = Signal(str, str)
    files_changed = Signal()
    log = Signal(str)

    COLS = ["#", "Title", "Branch", "Author", "Status", "Updated"]

    def __init__(self, git, parent=None):
        super().__init__(parent)
        self.git = git
        self.prs = []
        self.state = QComboBox()
        self.state.addItems(["Open", "Closed", "Merged", "All"])
        self.state.activated.connect(lambda _i: self.refresh())
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 4, 4, 4)
        bar.addWidget(self.state)

        def tb(text, tip, fn):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(fn)
            bar.addWidget(b)
            return b
        tb("⟳", "Refresh", self.refresh)
        tb("New", "Create a pull request from your current branch", self.create)
        self.b_view = tb("Changes", "See the pull request's changes (diff)", self.view_diff)
        self.b_checkout = tb("Check Out", "Switch to the pull request's branch to try it", self.checkout)
        self.b_browser = tb("Browser", "Open it on GitHub", self.open_browser)
        self.b_merge = tb("Merge...", "Merge the pull request on GitHub", self.merge)
        bar.addStretch()
        self.msg = QLabel()
        self.msg.setStyleSheet("color: #9a9a9a; padding: 0 4px;")
        self.msg.setWordWrap(True)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self.view_diff())
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(140)
        self.table.itemSelectionChanged.connect(self._show_detail)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.msg)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.detail)
        self._enable()

    def cwd(self):
        return self.git.root or self.git.folder

    def _enable(self):
        on = self.current() is not None
        for b in (self.b_view, self.b_checkout, self.b_browser, self.b_merge):
            b.setEnabled(on)

    def current(self):
        r = self.table.currentRow()
        return self.prs[r] if 0 <= r < len(self.prs) else None

    def refresh(self):
        self.table.setRowCount(0)
        self.prs = []
        self.detail.clear()
        if not self.git.root:
            self.msg.setText("Open a project that's in a Git repository on GitHub.")
            self._enable()
            return
        state = self.state.currentText().lower()
        code, out, err = gh(["pr", "list", "--state", state, "--limit", "50", "--json",
                             "number,title,headRefName,baseRefName,author,state,isDraft,updatedAt,url,body,"
                             "reviewDecision"], self.cwd())
        if code != 0:
            hint = ("Sign in once in the Terminal:  gh auth login" if "auth" in (err + out).lower() else
                    "Is this repository on GitHub? (Git Changes > ⋯ > Add Remote)")
            self.msg.setText(((err or out).strip().splitlines() or ["gh failed"])[-1] + "  - " + hint)
            self._enable()
            return
        try:
            self.prs = json.loads(out or "[]")
        except ValueError:
            self.prs = []
        self.msg.setText(f"{len(self.prs)} {state} pull request(s)." if self.prs else f"No {state} pull requests.")
        for p in self.prs:
            r = self.table.rowCount()
            self.table.insertRow(r)
            status = "Draft" if p.get("isDraft") else p.get("state", "").title()
            if p.get("reviewDecision") == "APPROVED":
                status += " ✓ approved"
            elif p.get("reviewDecision") == "CHANGES_REQUESTED":
                status += " - changes requested"
            vals = [f"#{p.get('number')}", p.get("title", ""), f"{p.get('headRefName')} → {p.get('baseRefName')}",
                    (p.get("author") or {}).get("login", ""), status, (p.get("updatedAt") or "")[:10]]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
        if self.prs:
            self.table.selectRow(0)
        self._enable()

    def _show_detail(self):
        p = self.current()
        self._enable()
        self.detail.setPlainText("" if p is None else f"{p.get('title')}\n{p.get('url')}\n\n{p.get('body') or ''}")

    def _say(self, title, code, out, err, ok_text):
        text = (out + err).strip()
        self.log.emit(f"> gh {title}\n{text}\n")
        if code != 0:
            QMessageBox.warning(self, title, text or f"{title} failed.")
            return False
        self.msg.setText(ok_text)
        return True

    def create(self):
        st = self.git.status() or {}
        branch = st.get("branch", "")
        default = gh(["repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"], self.cwd())
        base = default[1].strip() if default[0] == 0 else "main"
        if branch == base:
            QMessageBox.information(self, "Create Pull Request", f"You're on {base}. Make a branch for your change "
                                    "first (Git Changes > ⋯ > New Branch), commit to it, then create the pull "
                                    "request.")
            return
        bases = [base] + [b for b in self.git.branches() if b not in (base, branch)]
        last = self.git.run_sync(["log", "-1", "--format=%s"])[1].strip()
        dlg = CreatePRDialog(branch, bases, last, self)
        if dlg.exec() != QDialog.Accepted:
            return
        if not st.get("upstream") or st.get("ahead"):          # the branch must be on GitHub first
            remote = (self.git.remotes() or ["origin"])[0]
            code, out, err = self.git.run_sync(["push", "-u", remote, branch], timeout=120)
            self.log.emit(f"> git push -u {remote} {branch}\n{(out + err).strip()}\n")
            if code != 0:
                QMessageBox.warning(self, "Create Pull Request", "Couldn't push your branch:\n" + (err or out))
                return
        args = ["pr", "create", "--title", dlg.title.text().strip(), "--body", dlg.body.toPlainText(),
                "--base", dlg.base.currentText(), "--head", branch]
        if dlg.draft.isChecked():
            args.append("--draft")
        code, out, err = gh(args, self.cwd(), timeout=120)
        if self._say("pr create", code, out, err, "Created: " + out.strip().splitlines()[-1] if out.strip() else
                     "Created."):
            self.refresh()

    def view_diff(self):
        p = self.current()
        if p is None:
            return
        code, out, err = gh(["pr", "diff", str(p["number"])], self.cwd(), timeout=120)
        if code == 0:
            self.open_diff.emit(f"PR #{p['number']} (changes)", out or "(no changes)")
        else:
            QMessageBox.warning(self, "Pull Request", err or out)

    def checkout(self):
        p = self.current()
        if p is None:
            return
        if (self.git.status() or {}).get("files") and QMessageBox.question(
                self, "Check Out", "You have uncommitted changes. Git keeps them if it can; otherwise it stops. "
                "Continue?") != QMessageBox.Yes:
            return
        code, out, err = gh(["pr", "checkout", str(p["number"])], self.cwd(), timeout=120)
        if self._say("pr checkout", code, out, err, f"Switched to {p.get('headRefName')} (PR #{p['number']})."):
            self.files_changed.emit()

    def open_browser(self):
        p = self.current()
        if p and p.get("url"):
            QDesktopServices.openUrl(QUrl(p["url"]))

    def merge(self):
        p = self.current()
        if p is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Merge Pull Request")
        box.setText(f"Merge #{p['number']} \"{p.get('title')}\" into {p.get('baseRefName')} on GitHub?")
        b_merge = box.addButton("Merge commit", QMessageBox.AcceptRole)
        b_squash = box.addButton("Squash and merge", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        how = {b_merge: "--merge", b_squash: "--squash"}.get(box.clickedButton())
        if not how:
            return
        code, out, err = gh(["pr", "merge", str(p["number"]), how], self.cwd(), timeout=120)
        if self._say("pr merge", code, out, err, f"Merged #{p['number']}."):
            self.refresh()

