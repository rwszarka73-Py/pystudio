"""Git > GitHub Issues: the project's to-do / bug list on GitHub, next to the code (via gh). Create, read, comment,
close and reopen issues; "Fixes #N" puts a line in the commit message so pushing that commit closes the issue."""
import json

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QSplitter,
                               QTableWidget, QTableWidgetItem, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from .gittools import gh

FIELDS = "number,title,state,labels,author,updatedAt,url,comments"


class NewIssueDialog(QDialog):
    def __init__(self, labels, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Issue")
        self.setMinimumWidth(520)
        f = QFormLayout(self)
        self.title = QLineEdit()
        self.title.setPlaceholderText("Short summary, e.g. Crash when the CSV has no header")
        f.addRow("Title:", self.title)
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText("Details: what happens, what you expected, steps to reproduce... (Markdown)")
        f.addRow("Description:", self.body)
        self.labels = QComboBox()
        self.labels.setEditable(True)
        self.labels.addItem("")
        self.labels.addItems(labels)
        self.labels.setToolTip("Optional: bug, enhancement... (comma-separated for more than one)")
        f.addRow("Label:", self.labels)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Create")
        bb.accepted.connect(lambda: self.title.text().strip() and self.accept())
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class IssuesWindow(QWidget):
    log = Signal(str)
    fixes = Signal(str)          # "Fixes #12" - for the commit message

    COLS = ["#", "Title", "Labels", "Author", "Comments", "Updated"]

    def __init__(self, git, parent=None):
        super().__init__(parent)
        self.git = git
        self.issues = []
        self.state = QComboBox()
        self.state.addItems(["Open", "Closed", "All"])
        self.state.activated.connect(lambda _i: self.refresh())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search issues...")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.refresh)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 4, 4, 4)
        bar.addWidget(self.state)
        bar.addWidget(self.search, 1)

        def tb(text, tip, fn):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(fn)
            bar.addWidget(b)
            return b
        tb("⟳", "Refresh", self.refresh)
        tb("New", "Create an issue (a bug to fix or an idea to do)", self.create)
        self.b_comment = tb("Comment", "Add a comment to the issue", self.comment)
        self.b_close = tb("Close", "Close the issue (done) - or reopen a closed one", self.toggle)
        self.b_fixes = tb("Fixes #", "Put \"Fixes #N\" in the Git Changes commit message: when that commit reaches "
                          "GitHub, the issue closes by itself", self.fixes_line)
        self.b_browser = tb("Browser", "Open it on GitHub", self.open_browser)
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
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self.open_browser())
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setSizes([600, 400])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.msg)
        lay.addWidget(split, 1)
        self._enable()

    def cwd(self):
        return self.git.root or self.git.folder

    def current(self):
        r = self.table.currentRow()
        return self.issues[r] if 0 <= r < len(self.issues) else None

    def _enable(self):
        i = self.current()
        for b in (self.b_comment, self.b_close, self.b_fixes, self.b_browser):
            b.setEnabled(i is not None)
        if i is not None:
            closed = i.get("state", "").upper() == "CLOSED"
            self.b_close.setText("Reopen" if closed else "Close")
            self.b_fixes.setEnabled(not closed)

    def refresh(self, select=None):
        self.table.setRowCount(0)
        self.issues = []
        self.detail.clear()
        if not self.git.root:
            self.msg.setText("Open a project that's on GitHub (Git Changes > Publish to GitHub).")
            self._enable()
            return
        state = self.state.currentText().lower()
        args = ["issue", "list", "--state", state, "--limit", "100", "--json", FIELDS]
        if self.search.text().strip():
            args += ["--search", self.search.text().strip()]
        code, out, err = gh(args, self.cwd())
        if code != 0:
            text = (err or out).lower()
            hint = ("Sign in: Git Changes > Publish to GitHub signs you in." if "auth" in text or "login" in text else
                    "Is this project on GitHub? (Git Changes > Publish to GitHub)")
            self.msg.setText(((err or out).strip().splitlines() or ["gh failed"])[-1] + "  - " + hint)
            self._enable()
            return
        try:
            self.issues = json.loads(out or "[]")
        except ValueError:
            self.issues = []
        what = {"open": "open", "closed": "closed", "all": ""}[state]
        self.msg.setText(f"{len(self.issues)} {what} issue(s)." if self.issues else
                         f"No {what} issues." + (" Click New to add one - a bug to fix or something to do."
                                                 if state == "open" and not self.search.text() else ""))
        for i in self.issues:
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = [f"#{i.get('number')}", i.get("title", ""),
                    ", ".join(lb.get("name", "") for lb in i.get("labels") or []),
                    (i.get("author") or {}).get("login", ""), str(len(i.get("comments") or [])),
                    (i.get("updatedAt") or "")[:10]]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if i.get("state", "").upper() == "CLOSED":
                    it.setForeground(Qt.gray)
                self.table.setItem(r, c, it)
        if self.issues:
            row = next((n for n, i in enumerate(self.issues) if i.get("number") == select), 0)
            self.table.selectRow(row)
        self._enable()

    def _show_detail(self):
        self._enable()
        i = self.current()
        if i is None:
            self.detail.clear()
            return
        code, out, _err = gh(["issue", "view", str(i["number"]), "--json", "title,body,state,url,author,createdAt,"
                              "comments,labels"], self.cwd())
        try:
            d = json.loads(out) if code == 0 else i
        except ValueError:
            d = i
        md = [f"## #{i['number']} {d.get('title', '')}",
              f"*{d.get('state', '').title()} - opened by {(d.get('author') or {}).get('login', '?')} "
              f"{(d.get('createdAt') or '')[:10]}*", "", d.get("body") or "*(no description)*"]
        for c in d.get("comments") or []:
            md += ["", "---", f"**{(c.get('author') or {}).get('login', '?')}** "
                   f"*{(c.get('createdAt') or '')[:10]}*", "", c.get("body", "")]
        self.detail.setMarkdown("\n".join(md))

    def _say(self, title, code, out, err, ok_text):
        text = (out + err).strip()
        self.log.emit(f"> gh {title}\n{text}\n")
        if code != 0:
            QMessageBox.warning(self, title, text or f"{title} failed.")
            return False
        self.msg.setText(ok_text)
        return True

    def create(self):
        if not self.git.root:
            self.refresh()
            return
        code, out, _e = gh(["label", "list", "--json", "name", "-q", ".[].name"], self.cwd())
        labels = [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []
        dlg = NewIssueDialog(labels, self)
        if dlg.exec() != QDialog.Accepted:
            return
        args = ["issue", "create", "--title", dlg.title.text().strip(), "--body", dlg.body.toPlainText()]
        for lb in [x.strip() for x in dlg.labels.currentText().split(",") if x.strip()]:
            args += ["--label", lb]
        code, out, err = gh(args, self.cwd(), timeout=60)
        if self._say("issue create", code, out, err, "Created: " + (out.strip().splitlines() or [""])[-1]):
            num = None
            try:
                num = int(out.strip().rstrip("/").split("/")[-1])
            except ValueError:
                pass
            self.state.setCurrentText("Open")
            self.refresh(select=num)

    def comment(self):
        i = self.current()
        if i is None:
            return
        text, ok = QInputDialog.getMultiLineText(self, f"Comment on #{i['number']}", "Comment (Markdown):")
        if not ok or not text.strip():
            return
        code, out, err = gh(["issue", "comment", str(i["number"]), "--body", text], self.cwd(), timeout=60)
        if self._say("issue comment", code, out, err, f"Commented on #{i['number']}."):
            self._show_detail()

    def toggle(self):
        i = self.current()
        if i is None:
            return
        closed = i.get("state", "").upper() == "CLOSED"
        code, out, err = gh(["issue", "reopen" if closed else "close", str(i["number"])], self.cwd(), timeout=60)
        if self._say("issue " + ("reopen" if closed else "close"), code, out, err,
                     f"{'Reopened' if closed else 'Closed'} #{i['number']}."):
            self.refresh()

    def fixes_line(self):
        i = self.current()
        if i is not None:
            self.fixes.emit(f"Fixes #{i['number']}")

    def open_browser(self):
        i = self.current()
        if i and i.get("url"):
            QDesktopServices.openUrl(QUrl(i["url"]))
