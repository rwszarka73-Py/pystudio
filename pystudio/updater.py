"""Updating PyStudio from inside PyStudio.

Help > Install Update from Zip: the PyStudio-vX.zip you downloaded - PyStudio checks it, shows the version, runs its
install.sh in update mode (no questions; settings, layouts and projects are kept) and offers to restart.

Help > Check for Updates: PyStudio's releases on a GitHub repository (for example yourname/pystudio-releases: each
release tagged v2.5.0 with the PyStudio zip attached). It compares versions, shows the release notes, downloads the
zip and installs it the same way. It can check once a day at startup. A private repository works through your
GitHub CLI sign-in (gh auth login)."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from PySide6.QtCore import QProcess, QSettings, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QVBoxLayout)

from . import __version__

API = os.environ.get("PYSTUDIO_UPDATE_API", "https://api.github.com")
DEFAULT_REPO = "rwszarka73-py/pystudio"      # where PyStudio's releases are published
LAUNCHER = os.path.join(os.path.expanduser("~"), ".local", "bin", "pystudio")


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def newer(a, b):
    """a is a newer version than b."""
    ta, tb = version_tuple(a), version_tuple(b)
    n = max(len(ta), len(tb))
    return ta + (0,) * (n - len(ta)) > tb + (0,) * (n - len(tb))


def inspect_zip(path):
    """(version, top folder) of a PyStudio zip, or raises ValueError."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise ValueError(f"This isn't a zip file ({e}).") from None
    with z:
        names = z.namelist()
        init = next((n for n in names if re.fullmatch(r"([^/]+/)?pystudio/__init__\.py", n)), None)
        top = init.split("pystudio/__init__.py")[0] if init else None
        if init is None or (top + "install.sh") not in names or (top + "main.py") not in names:
            raise ValueError("This zip isn't a PyStudio release (it has no pystudio/ folder with install.sh).")
        bad = [n for n in names if n.startswith("/") or ".." in n.split("/")]
        if bad:
            raise ValueError("This zip has unsafe paths in it.")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)', z.read(init).decode("utf-8", "replace"))
        return (m.group(1) if m else "?"), top.rstrip("/")


class UpdateRunner(QDialog):
    """Extracts a PyStudio zip and runs its install.sh --update, showing what it does."""

    def __init__(self, zip_path, version, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Installing PyStudio {version}")
        self.resize(640, 380)
        self.zip_path, self.version, self.ok = zip_path, version, False
        v = QVBoxLayout(self)
        self.lbl = QLabel(f"Installing PyStudio {version} (you have {__version__})... your settings and projects are "
                          "kept. Updating the Python packages can take a minute.")
        self.lbl.setWordWrap(True)
        v.addWidget(self.lbl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        v.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family: monospace;")
        v.addWidget(self.log, 1)
        self.bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.bb.rejected.connect(self.reject)
        self.bb.setEnabled(False)
        v.addWidget(self.bb)
        self.tmp = tempfile.mkdtemp(prefix="pystudio-update-")
        QTimer.singleShot(0, self._start)

    def _write(self, text):
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.ensureCursorVisible()

    def _start(self):
        try:
            _ver, top = inspect_zip(self.zip_path)
            with zipfile.ZipFile(self.zip_path) as z:
                z.extractall(self.tmp)
        except (ValueError, OSError, zipfile.BadZipFile) as e:
            return self._done(1, str(e))
        src = os.path.join(self.tmp, top) if top else self.tmp
        self._write(f"Unpacked to {src}\n> bash install.sh --update\n")
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setWorkingDirectory(src)
        self.proc.readyReadStandardOutput.connect(
            lambda: self._write(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")))
        self.proc.finished.connect(lambda code, _s: self._done(code))
        self.proc.start("bash", [os.path.join(src, "install.sh"), "--update"])

    def _done(self, code, msg=""):
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.bb.setEnabled(True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        if msg:
            self._write(msg + "\n")
        if code == 0:
            self.ok = True
            self.lbl.setText(f"PyStudio {self.version} is installed. Restart PyStudio now to use it (until then, "
                             "some windows may already use the new files).")
            b = self.bb.addButton("Restart Now", QDialogButtonBox.AcceptRole)
            b.clicked.connect(self.accept)
            b.setDefault(True)
        elif code == 3:
            self.lbl.setText("The update needs Ubuntu packages first - see below. Run  bash install.sh  from the zip "
                             "in a terminal (it asks for your password), then restart PyStudio.")
        else:
            self.lbl.setText(f"The update didn't install (exit code {code}). PyStudio is unchanged - see below.")

    def reject(self):
        if getattr(self, "proc", None) is not None and self.proc.state() != QProcess.NotRunning:
            return                        # don't stop install.sh half way
        super().reject()


def restart(window):
    """Close PyStudio (asking about unsaved work) and start the installed one again."""
    exe = LAUNCHER if os.path.exists(LAUNCHER) else shutil.which("pystudio")
    if not window.close():
        return False
    if exe:
        QProcess.startDetached(exe, [])
    QApplication.instance().quit()
    return True


def install_zip(window, path=None):
    """Help > Install Update from Zip."""
    if path is None:
        start = os.path.join(os.path.expanduser("~"), "Downloads")
        path, _f = QFileDialog.getOpenFileName(window, "Install PyStudio Update", start if os.path.isdir(start) else
                                               os.path.expanduser("~"), "PyStudio zip (PyStudio*.zip *.zip)")
        if not path:
            return False
    try:
        version, _top = inspect_zip(path)
    except ValueError as e:
        QMessageBox.warning(window, "Install Update", str(e))
        return False
    if not newer(version, __version__):
        same = version_tuple(version) == version_tuple(__version__)
        if QMessageBox.question(window, "Install Update", f"This zip has PyStudio {version}; you have {__version__}"
                                + (" already" if same else " (newer)") + ". Install it anyway?") != QMessageBox.Yes:
            return False
    elif QMessageBox.question(window, "Install Update", f"Install PyStudio {version}? You have {__version__}.\n\n"
                              "Your settings, window layouts and projects are kept.") != QMessageBox.Yes:
        return False
    if hasattr(window, "save_all") and not window.save_all():
        return False
    dlg = UpdateRunner(path, version, window)
    if dlg.exec() == QDialog.Accepted and dlg.ok:
        restart(window)
        return True
    return dlg.ok


# ============================================================ GitHub releases
def _token():
    """A GitHub token for private repositories: the GitHub CLI's sign-in."""
    gh = shutil.which("gh")
    if not gh:
        return ""
    try:
        r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


class ReleaseChecker(QNetworkAccessManager):
    """latest(repo) -> found({version, notes, asset_url, asset_name, page}) or failed(message)."""
    found = Signal(dict)
    failed = Signal(str)
    progress = Signal(int, int)
    downloaded = Signal(str)

    def _request(self, url, accept="application/vnd.github+json"):
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"Accept", accept.encode())
        req.setRawHeader(b"User-Agent", b"PyStudio-updater")
        tok = _token()
        if tok:
            req.setRawHeader(b"Authorization", f"Bearer {tok}".encode())
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.NoLessSafeRedirectPolicy)
        return req

    def latest(self, repo):
        repo = repo.strip().strip("/")
        repo = re.sub(r"^https?://github\.com/", "", repo)
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            self.failed.emit("Set the GitHub repository as owner/name, e.g.  rob/pystudio-releases")
            return
        reply = self.get(self._request(f"{API}/repos/{repo}/releases/latest"))
        reply.finished.connect(lambda: self._got(reply, repo))

    def _got(self, reply, repo):
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        data = bytes(reply.readAll())
        reply.deleteLater()
        if reply.error() != QNetworkReply.NoError and status not in (404,):
            self.failed.emit(f"Couldn't reach GitHub: {reply.errorString()}")
            return
        if status == 404:
            self.failed.emit(f"No releases found for {repo} (or it's private and the GitHub CLI isn't signed in: "
                             "gh auth login in the Terminal).")
            return
        try:
            rel = json.loads(data)
        except ValueError:
            self.failed.emit("GitHub's answer couldn't be read.")
            return
        assets = [a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".zip")]
        asset = next((a for a in assets if a["name"].lower().startswith("pystudio")), assets[0] if assets else None)
        version = (rel.get("tag_name") or rel.get("name") or "").lstrip("vV")
        self.found.emit({"version": version, "notes": rel.get("body") or "", "page": rel.get("html_url", ""),
                         "asset_url": asset.get("url") if asset else "", "asset_name": asset.get("name") if asset
                         else "", "download_url": asset.get("browser_download_url") if asset else ""})

    def download(self, info, dest_dir):
        # the API asset URL works for private repositories too (with the token)
        url = info.get("asset_url") or info.get("download_url")
        reply = self.get(self._request(url, "application/octet-stream"))
        dest = os.path.join(dest_dir, info.get("asset_name") or "PyStudio-update.zip")
        reply.downloadProgress.connect(lambda a, b: self.progress.emit(a, b))

        def done():
            ok = reply.error() == QNetworkReply.NoError
            data = bytes(reply.readAll())
            reply.deleteLater()
            if not ok:
                self.failed.emit(f"The download failed: {reply.errorString()}")
                return
            with open(dest, "wb") as f:
                f.write(data)
            self.downloaded.emit(dest)
        reply.finished.connect(done)


class UpdatesDialog(QDialog):
    """Help > Check for Updates."""

    def __init__(self, window, auto_found=None):
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle("Check for Updates")
        self.resize(620, 440)
        s = QSettings()
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"You have <b>PyStudio {__version__}</b>."))
        h = QHBoxLayout()
        h.addWidget(QLabel("Releases on GitHub:"))
        self.repo = QLineEdit(s.value("updateRepo", "") or DEFAULT_REPO)
        self.repo.setPlaceholderText("owner/repository, e.g.  rob/pystudio-releases")
        h.addWidget(self.repo, 1)
        self.b_check = QPushButton("Check Now")
        self.b_check.clicked.connect(self.check)
        h.addWidget(self.b_check)
        v.addLayout(h)
        self.ck_auto = QCheckBox("Check once a day when PyStudio starts")
        self.ck_auto.setChecked(s.value("updateAutoCheck", "true") == "true")
        v.addWidget(self.ck_auto)
        self.status = QLabel("No repository yet? Use Install from Zip with the zip you downloaded. To get updates "
                             "here, put each PyStudio zip on a GitHub release (tag v2.5.0 and so on) and enter the "
                             "repository above." if not self.repo.text() else "")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.hide()
        v.addWidget(self.notes, 1)
        self.bar = QProgressBar()
        self.bar.hide()
        v.addWidget(self.bar)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.b_zip = bb.addButton("Install from Zip...", QDialogButtonBox.ActionRole)
        self.b_zip.clicked.connect(lambda: (self._save(), self.close(), install_zip(window)))
        self.b_install = bb.addButton("Download and Install", QDialogButtonBox.AcceptRole)
        self.b_install.hide()
        self.b_install.clicked.connect(self.download)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.checker = ReleaseChecker(self)
        self.checker.found.connect(self._found)
        self.checker.failed.connect(self._failed)
        self.checker.progress.connect(lambda a, b: (self.bar.setRange(0, max(b, 1)), self.bar.setValue(a)))
        self.checker.downloaded.connect(self._downloaded)
        self.info = None
        if auto_found:
            self._found(auto_found)

    def _save(self):
        s = QSettings()
        s.setValue("updateRepo", self.repo.text().strip())
        s.setValue("updateAutoCheck", "true" if self.ck_auto.isChecked() else "false")

    def done(self, r):
        self._save()
        super().done(r)

    def check(self):
        self._save()
        self.status.setText("Checking...")
        self.b_check.setEnabled(False)
        self.checker.latest(self.repo.text())

    def _found(self, info):
        self.b_check.setEnabled(True)
        self.info = info
        if newer(info["version"], __version__):
            if not info.get("asset_url") and not info.get("download_url"):
                self.status.setText(f"PyStudio {info['version']} is out, but its release has no zip attached.")
                return
            self.status.setText(f"<b>PyStudio {info['version']}</b> is available.")
            self.notes.setPlainText(info["notes"] or "(no release notes)")
            self.notes.show()
            self.b_install.show()
        else:
            self.status.setText(f"You're up to date (the latest release is {info['version'] or 'unknown'}).")

    def _failed(self, msg):
        self.b_check.setEnabled(True)
        self.bar.hide()
        self.status.setText(msg)

    def download(self):
        if not self.info:
            return
        self.b_install.setEnabled(False)
        self.bar.show()
        self.status.setText(f"Downloading {self.info.get('asset_name')}...")
        self.tmp = tempfile.mkdtemp(prefix="pystudio-download-")
        self.checker.download(self.info, self.tmp)

    def _downloaded(self, path):
        self.status.setText("Downloaded.")
        self.accept()
        install_zip(self.window_, path)


def auto_check(window):
    """Once a day at startup (when a repository is set): a quiet check; a newer release shows in the status bar."""
    import time
    s = QSettings()
    repo = s.value("updateRepo", "") or DEFAULT_REPO
    if not repo or s.value("updateAutoCheck", "true") != "true":
        return None
    last = float(s.value("updateLastCheck", 0) or 0)
    if time.time() - last < 20 * 3600:
        return None
    s.setValue("updateLastCheck", time.time())
    checker = ReleaseChecker(window)

    def found(info):
        if newer(info["version"], __version__):
            from PySide6.QtWidgets import QToolButton
            b = QToolButton()
            b.setText(f"⬆ PyStudio {info['version']} is available")
            b.setAutoRaise(True)
            b.setStyleSheet("color: #ffd166; font-weight: bold;")
            b.clicked.connect(lambda: (window.statusBar().removeWidget(b),
                                       UpdatesDialog(window, info).exec()))
            window.statusBar().addPermanentWidget(b)
    checker.found.connect(found)
    checker.latest(repo)
    return checker

