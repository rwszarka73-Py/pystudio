"""Optional Ubuntu programs PyStudio uses for some features - Tools > Install Components lists them, shows which are
installed, and installs the checked ones (one password prompt). install.sh asks about the same list."""
import os
import shutil
import sys

from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QPlainTextEdit, QTableWidget, QTableWidgetItem, QVBoxLayout)

# (key, name, apt packages, commands that show it's installed, what it's for)
COMPONENTS = [
    ("git", "Git", ["git"], ["git"], "Source control: the Git menu, Git Changes, history, branches"),
    ("gh", "GitHub CLI", ["gh"], ["gh"], "Publish a repository to GitHub; Run on GitHub / Download in Publish for "
                                         "Windows"),
    ("wine", "Wine", ["wine"], ["wine", "wine64"], "Publish for Windows: build the Windows .exe on this computer"),
    ("nsis", "NSIS", ["nsis"], ["makensis"], "Publish for Windows: the Setup.exe installer"),
    ("odbc", "unixODBC", ["unixodbc"], ["isql"], "ODBC data connections in Server Explorer (with pyodbc)"),
    ("gdb", "gdb", ["gdb"], ["gdb"], "Debug > Attach to Process (attaches right away, even to an idle program)"),
    ("sshfs", "sshfs", ["sshfs"], ["sshfs"], "File > Open Remote Folder: projects on another machine over SSH"),
]


def _dpkg_installed(pkg):
    import subprocess
    try:
        r = subprocess.run(["dpkg-query", "-W", "-f=${Status}", pkg], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip().endswith("installed") and "not-installed" not in r.stdout


def is_installed(key):
    """Its command is on PATH, or Ubuntu's package database says its packages are installed."""
    c = next(c for c in COMPONENTS if c[0] == key)
    return any(shutil.which(cmd) for cmd in c[3]) or all(_dpkg_installed(p) for p in c[2])


def missing(keys=None):
    return [c[0] for c in COMPONENTS if (keys is None or c[0] in keys) and not is_installed(c[0])]


def packages_for(keys):
    out = []
    for c in COMPONENTS:
        if c[0] in keys:
            out += c[2]
    return out


def install_command(keys):
    """(program, args) that installs keys with one administrator prompt, or None."""
    pkgs = packages_for(keys)
    if not pkgs:
        return None
    script = ("export DEBIAN_FRONTEND=noninteractive; apt-get update && apt-get install -y "
              + " ".join(pkgs))
    pkexec = shutil.which("pkexec")
    if pkexec and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):     # graphical password prompt
        return pkexec, ["/bin/sh", "-c", script]
    return None


class InstallDialog(QDialog):
    """Tools > Install Components (and the Install buttons in Publish for Windows)."""
    installed = Signal(list)

    def __init__(self, preselect=None, parent=None, title="Install Components"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 480)
        self.proc = None
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Ubuntu programs PyStudio uses for some features. Check the ones to install - Ubuntu "
                           "asks for your password once."))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Component", "Status", "Used for"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        v.addWidget(self.table)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setStyleSheet("font-family: monospace;")
        self.log.hide()
        v.addWidget(self.log, 1)
        self.cmd = QLabel()
        self.cmd.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.cmd.setWordWrap(True)
        v.addWidget(self.cmd)
        self.bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.b_install = self.bb.addButton("Install Selected", QDialogButtonBox.AcceptRole)
        self.bb.accepted.connect(self.install)
        self.bb.rejected.connect(self.reject)
        h2 = QHBoxLayout()
        h2.addStretch()
        h2.addWidget(self.bb)
        v.addLayout(h2)
        self.preselect = set(preselect or [])
        self.fill()

    def fill(self):
        self.table.setRowCount(0)
        for key, name, _pkgs, _cmds, what in COMPONENTS:
            r = self.table.rowCount()
            self.table.insertRow(r)
            ok = is_installed(key)
            it = QTableWidgetItem(name)
            it.setData(Qt.UserRole, key)
            if ok:
                it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable)
            else:
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if (not self.preselect or key in self.preselect) else Qt.Unchecked)
            st = QTableWidgetItem("Installed" if ok else "Not installed")
            st.setForeground(Qt.darkGreen if ok else Qt.red)
            self.table.setItem(r, 0, it)
            self.table.setItem(r, 1, st)
            self.table.setItem(r, 2, QTableWidgetItem(what))
        todo = missing()
        self.b_install.setEnabled(bool(todo) and self.proc is None)
        pkgs = packages_for(todo)
        self.cmd.setText(("Or in a terminal:  <font face='monospace'>sudo apt install " + " ".join(pkgs) + "</font>")
                         if pkgs else "Everything is installed.")
        self.cmd.setTextFormat(Qt.RichText)

    def selected(self):
        out = []
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it.flags() & Qt.ItemIsUserCheckable and it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    def install(self):
        keys = self.selected()
        if not keys:
            return
        cmd = install_command(keys)
        if cmd is None:
            QMessageBox.information(self, "Install Components",
                                    "Run this in a terminal (Ctrl+` opens one):\n\n    sudo apt install "
                                    + " ".join(packages_for(keys)))
            return
        self.log.show()
        self.log.appendPlainText(f"Installing {', '.join(packages_for(keys))} - enter your password when Ubuntu "
                                 "asks...\n")
        self.b_install.setEnabled(False)
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.log.appendPlainText(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
                                             .rstrip()))
        self.proc.finished.connect(lambda code, _s: self._done(code, keys))
        self.proc.start(*cmd)

    def _done(self, code, keys):
        self.proc = None
        now = [k for k in keys if is_installed(k)]
        if code in (126, 127) and not now:
            self.log.appendPlainText("\nCanceled (no password given).")
        elif len(now) == len(keys):
            self.log.appendPlainText("\nDone.")
        else:
            self.log.appendPlainText(f"\nSomething didn't install (exit code {code}) - see above.")
        self.fill()
        if now:
            self.installed.emit(now)


def ask_install(keys, parent=None, why=""):
    """Offer to install missing components right now. True when they're all there afterwards."""
    todo = missing(keys)
    if not todo:
        return True
    names = ", ".join(c[1] for c in COMPONENTS if c[0] in todo)
    if QMessageBox.question(parent, "Install " + names, (why + "\n\n" if why else "") +
                            f"{names} isn't installed. Install it now? (Ubuntu asks for your password.)") \
            != QMessageBox.Yes:
        return False
    dlg = InstallDialog(todo, parent, "Install " + names)
    dlg.exec()
    return not missing(keys)


if __name__ == "__main__":        # install.sh: list what's missing (keys)
    print(" ".join(missing()))
    sys.exit(0)
