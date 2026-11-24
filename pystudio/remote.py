"""Remote projects over SSH (File > Open Remote Folder): work on a project that lives on another Linux
machine - a server, a Raspberry Pi - as if it were here.

- The remote folder is mounted with sshfs under ~/.local/share/pystudio/remote/, so the editor, Solution
  Explorer, Find in Files, Git, IntelliSense and the Error List work on it unchanged; saving writes to
  the server.
- Run (Ctrl+F5) and Debug (F5) start the program ON THE SERVER, with the server's Python (the project's
  .venv there if it has one), and stream its output here. Stop ends it on the server. The debugger
  connects back through the SSH connection, so breakpoints, stepping, Locals and Watch work as usual.
- Terminal opens an SSH shell on the server, in the project folder.
- "Show the program's windows here" (X11 forwarding) lets a Qt program running on the server open its
  windows on this screen.

One SSH connection per server is shared by all of this (OpenSSH ControlMaster), so a password is asked
once (in PyStudio's sign-in dialog); with an SSH key there's no prompt at all. Host keys are checked:
the first connection to a new server shows its fingerprint and asks."""
import json
import os
import re
import shlex
import shutil
import subprocess

from PySide6.QtCore import QEventLoop, QProcess, QProcessEnvironment, QSettings, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox,
                               QVBoxLayout)

BASE = os.path.join(os.path.expanduser("~"), ".local", "share", "pystudio", "remote")
CTL_DIR = os.path.join(os.path.expanduser("~"), ".cache", "pystudio", "ssh")
PID_MARK = "__PYSTUDIO_REMOTE_PID="


# ============================================================ configuration
def _settings_mounts():
    try:
        return json.loads(QSettings().value("remoteMounts", "{}") or "{}")
    except ValueError:
        return {}


def _save_mounts(m):
    QSettings().setValue("remoteMounts", json.dumps(m))


def recent_hosts():
    try:
        return json.loads(QSettings().value("remoteRecent", "[]") or "[]")
    except ValueError:
        return []


def _remember(cfg):
    lst = [c for c in recent_hosts() if (c.get("host"), c.get("user"), c.get("path"), c.get("port")) !=
           (cfg.get("host"), cfg.get("user"), cfg.get("path"), cfg.get("port"))]
    lst.insert(0, {k: cfg.get(k) for k in ("host", "user", "port", "path", "key", "python", "x11")})
    QSettings().setValue("remoteRecent", json.dumps(lst[:12]))


def target(cfg):
    return f"{cfg['user']}@{cfg['host']}" if cfg.get("user") else cfg["host"]


def label(cfg):
    port = int(cfg.get("port") or 22)
    return f"{target(cfg)}{'' if port == 22 else f':{port}'}:{cfg.get('path', '')}"


def mount_point(cfg):
    slug = re.sub(r"[^\w.-]+", "_", f"{target(cfg)}-{cfg.get('port') or 22}{cfg.get('path', '')}").strip("_")
    name = os.path.basename(cfg.get("path", "").rstrip("/")) or cfg["host"]
    return os.path.join(BASE, slug, name)


def remote_for(path):
    """(cfg, local_root) when path is inside a mounted remote folder, else None."""
    if not path:
        return None
    p = os.path.abspath(path)
    if not p.startswith(BASE + os.sep):
        return None
    for local, cfg in _settings_mounts().items():
        if p == local or p.startswith(local + os.sep):
            return cfg, local
    return None


def to_remote(cfg, local_root, path):
    rel = os.path.relpath(os.path.abspath(path), local_root)
    base = cfg["path"].rstrip("/") or "/"
    return base if rel == "." else (base.rstrip("/") + "/" + rel.replace(os.sep, "/"))


def to_local(cfg, local_root, rpath):
    base = cfg["path"].rstrip("/")
    if rpath == base:
        return local_root
    if base and rpath.startswith(base + "/"):
        return os.path.join(local_root, rpath[len(base) + 1:])
    return rpath


# ============================================================ ssh
def askpass_script():
    from .gitpanel import _askpass_script
    return _askpass_script()


def ssh_env():
    env = dict(os.environ)
    ask = askpass_script()
    if ask:
        env["SSH_ASKPASS"] = ask
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["PYSTUDIO_ASKPASS_TITLE"] = "SSH sign-in"
        env.setdefault("DISPLAY", ":0")
    return env


def qenv():
    e = QProcessEnvironment()
    for k, v in ssh_env().items():
        e.insert(k, v)
    return e


def ssh_opts(cfg, multiplex=True):
    os.makedirs(CTL_DIR, mode=0o700, exist_ok=True)
    o = ["-p", str(int(cfg.get("port") or 22)), "-o", "ServerAliveInterval=15", "-o", "ConnectTimeout=15",
         "-o", "StrictHostKeyChecking=yes"]
    if multiplex:
        o += ["-o", "ControlMaster=auto", "-o", f"ControlPath={CTL_DIR}/%C", "-o", "ControlPersist=900"]
    if cfg.get("key"):
        o += ["-i", os.path.expanduser(cfg["key"]), "-o", "IdentitiesOnly=yes"]
    return o


def ssh_argv(cfg, command=None, tty=False, x11=False, forward=None):
    a = ["ssh"] + ssh_opts(cfg)
    if tty:
        a.append("-tt")
    if x11:
        a.append("-Y")
    if forward:
        a += ["-o", "ExitOnForwardFailure=yes", "-R", forward]
    a.append(target(cfg))
    if command is not None:
        a.append(command)
    return a


def run(cfg, command, timeout=60, data=None):
    """Runs command on the server (sync, through the shared connection). (code, stdout, stderr)."""
    try:
        r = subprocess.run(ssh_argv(cfg, command), input=data, capture_output=True, timeout=timeout,
                           env=ssh_env(), stdin=None if data is not None else subprocess.DEVNULL)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "The server didn't answer in time."
    except OSError as e:
        return 127, "", str(e)


def _wait(proc, timeout_ms):
    loop = QEventLoop()
    proc.finished.connect(loop.quit)
    proc.errorOccurred.connect(lambda *_: loop.quit())
    QTimer.singleShot(timeout_ms, loop.quit)
    if proc.state() != QProcess.NotRunning:
        loop.exec()


def run_async_wait(argv, timeout_ms=60000, data=None):
    """Like subprocess.run but keeps PyStudio responsive (a password dialog may be showing)."""
    p = QProcess()
    p.setProcessEnvironment(qenv())
    p.start(argv[0], argv[1:])
    if data is not None:
        p.write(data)
    p.closeWriteChannel()
    _wait(p, timeout_ms)
    if p.state() != QProcess.NotRunning:
        p.kill()
        p.waitForFinished(2000)
        return 124, "", "Timed out."
    return (p.exitCode(), bytes(p.readAllStandardOutput()).decode("utf-8", "replace"),
            bytes(p.readAllStandardError()).decode("utf-8", "replace"))


def known_host(cfg):
    port = int(cfg.get("port") or 22)
    name = cfg["host"] if port == 22 else f"[{cfg['host']}]:{port}"
    try:
        r = subprocess.run(["ssh-keygen", "-F", name], capture_output=True, timeout=10)
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def trust_host(cfg, parent=None):
    """First connection: show the server's key fingerprint and add it to ~/.ssh/known_hosts if accepted."""
    if known_host(cfg):
        return True
    port = int(cfg.get("port") or 22)
    try:
        r = subprocess.run(["ssh-keyscan", "-p", str(port), "-T", "10", cfg["host"]], capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        QMessageBox.warning(parent, "Open Remote Folder", f"Couldn't reach {cfg['host']}: {e}")
        return False
    keys = [ln for ln in r.stdout.splitlines() if ln and not ln.startswith("#")]
    if not keys:
        QMessageBox.warning(parent, "Open Remote Folder", f"Couldn't reach {cfg['host']} on port {port}.\n\n"
                            + (r.stderr.strip() or "Check the address, and that SSH is running on it."))
        return False
    fps = []
    for k in keys:
        try:
            f = subprocess.run(["ssh-keygen", "-lf", "-"], input=k, capture_output=True, text=True, timeout=10)
            fps.append(f.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    if parent is not None and QMessageBox.question(
            parent, "New server",
            f"This is the first connection to {cfg['host']}. Its key fingerprints are:\n\n" + "\n".join(fps) +
            "\n\nIf you set the server up yourself, you can check one with  ssh-keygen -lf /etc/ssh/"
            "ssh_host_ed25519_key.pub  on it. Trust this server?") != QMessageBox.Yes:
        return False
    os.makedirs(os.path.expanduser("~/.ssh"), mode=0o700, exist_ok=True)
    with open(os.path.expanduser("~/.ssh/known_hosts"), "a") as f:
        for k in keys:
            f.write(k + "\n")
    return True


def connect(cfg, parent=None):
    """Opens (or reuses) the shared connection. Returns "" or an error message."""
    if not shutil.which("ssh"):
        return "ssh isn't installed (sudo apt install openssh-client)."
    if not trust_host(cfg, parent):
        return "canceled"
    code, out, err = run_async_wait(ssh_argv(cfg, "echo PYSTUDIO_OK"), 120000)
    if code == 0 and "PYSTUDIO_OK" in out:
        return ""
    msg = (err or out).strip()
    if "Permission denied" in msg:
        return ("The server didn't accept the sign-in (wrong password, or the key isn't in the server's "
                "~/.ssh/authorized_keys).\n\n" + msg)
    return msg or f"ssh failed (exit code {code})."


def mount(cfg, parent=None):
    """Mounts cfg's folder; returns (local_root, error)."""
    if not shutil.which("sshfs"):
        return None, ("sshfs isn't installed. Tools > Install Components > sshfs (or: sudo apt install "
                      "sshfs), then try again.")
    err = connect(cfg, parent)
    if err:
        return None, err
    code, out, e2 = run_async_wait(ssh_argv(cfg, f"test -d {shlex.quote(cfg['path'])} && echo YES"), 30000)
    if "YES" not in out:
        return None, f"There's no folder {cfg['path']} on {cfg['host']}."
    mp = mount_point(cfg)
    os.makedirs(mp, exist_ok=True)
    if not os.path.ismount(mp):
        opts = ["reconnect", "ServerAliveInterval=15", "follow_symlinks", "idmap=user", "StrictHostKeyChecking=yes",
                "ControlMaster=auto", f"ControlPath={CTL_DIR}/%C", "ControlPersist=900"]
        if cfg.get("key"):
            opts += [f"IdentityFile={os.path.expanduser(cfg['key'])}", "IdentitiesOnly=yes"]
        argv = ["sshfs", "-p", str(int(cfg.get("port") or 22)), "-o", ",".join(opts),
                f"{target(cfg)}:{cfg['path']}", mp]
        code, out, e3 = run_async_wait(argv, 60000)
        if code != 0 or not os.path.ismount(mp):
            return None, f"Couldn't mount {label(cfg)}:\n{(e3 or out).strip()}"
    m = _settings_mounts()
    m[mp] = {k: cfg.get(k) for k in ("host", "user", "port", "path", "key", "python", "x11")}
    _save_mounts(m)
    _remember(cfg)
    return mp, ""


def ensure_mounted(local_path, parent=None):
    """A remote project opened from the recent list after a restart: mount it again. True if usable."""
    r = remote_for(local_path)
    if r is None:
        return True
    cfg, root = r
    if os.path.ismount(root):
        return True
    mp, err = mount(cfg, parent)
    if err and parent is not None:
        QMessageBox.warning(parent, "Remote folder", err)
    return bool(mp)


def unmount(local_root, forget=False):
    """Disconnect: the folder stays in Recent and mounts again when opened (forget=True drops it)."""
    tool = shutil.which("fusermount3") or shutil.which("fusermount")
    if tool and os.path.ismount(local_root):
        r = subprocess.run([tool, "-u", local_root], capture_output=True, timeout=20)
        if r.returncode != 0:                       # still in use (a terminal in it...): detach it anyway
            subprocess.run([tool, "-u", "-z", local_root], capture_output=True, timeout=20)
    if forget:
        m = _settings_mounts()
        m.pop(local_root, None)
        _save_mounts(m)


def disconnect_all():
    for root in list(_settings_mounts()):
        if os.path.ismount(root):
            tool = shutil.which("fusermount3") or shutil.which("fusermount")
            if tool:
                subprocess.run([tool, "-u", "-z", root], capture_output=True, timeout=20)


# ============================================================ running there
def remote_python(cfg, local_root):
    if cfg.get("python"):
        return cfg["python"]
    for venv in (".venv", "venv", "env"):
        if os.path.exists(os.path.join(local_root, venv, "bin", "python")):
            return to_remote(cfg, local_root, os.path.join(local_root, venv, "bin", "python"))
    return "python3"


def _env_prefix(cfg, local_root, env):
    parts = ["PYTHONUNBUFFERED=1", "PYTHONIOENCODING=utf-8"]
    for k, v in (env or {}).items():
        if k in ("PATH", "HOME", "DISPLAY", "XAUTHORITY", "QT_QPA_PLATFORM") or not re.fullmatch(r"\w+", k):
            continue
        v = v.replace(local_root, cfg["path"].rstrip("/")) if local_root in v else v
        parts.append(f"{k}={shlex.quote(v)}")
    return "env " + " ".join(parts)


def command_line(cfg, local_root, cwd, program_argv, env=None):
    """The shell command run on the server: report the process id, cd, exec the program."""
    rcwd = to_remote(cfg, local_root, cwd) if cwd and (cwd == local_root or cwd.startswith(local_root + os.sep)) \
        else cfg["path"]
    argv = " ".join(shlex.quote(a) for a in program_argv)
    return f"echo {PID_MARK}$$; cd {shlex.quote(rcwd)} && exec {_env_prefix(cfg, local_root, env)} {argv}"


def map_arg(cfg, local_root, a):
    if isinstance(a, str) and (a == local_root or a.startswith(local_root + os.sep)):
        return to_remote(cfg, local_root, a)
    return a


class PidFilter:
    """Takes the first output line with the remote process id out of the program's output."""

    def __init__(self):
        self.pid = None
        self._buf = ""

    def feed(self, text):
        if self.pid is not None:
            return text
        self._buf += text
        if "\n" not in self._buf and len(self._buf) < 200:
            return ""
        first, _, rest = self._buf.partition("\n")
        self._buf = ""
        m = re.search(re.escape(PID_MARK) + r"(\d+)", first)
        if m:
            self.pid = int(m.group(1))
            return rest
        self.pid = 0
        return first + "\n" + rest


def kill_remote(cfg, pid):
    if pid:
        subprocess.Popen(ssh_argv(cfg, f"kill -TERM {pid} 2>/dev/null; sleep 1; kill -KILL {pid} 2>/dev/null; true"),
                         env=ssh_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


def upload(cfg, local_file, remote_rel):
    """Copies a file to $HOME/<remote_rel> on the server (for the debugger). Returns the remote path or None."""
    with open(local_file, "rb") as f:
        data = f.read()
    d = os.path.dirname(remote_rel)
    code, out, _err = run(cfg, f'mkdir -p "$HOME/{d}" && cat > "$HOME/{remote_rel}" && echo "$HOME/{remote_rel}"',
                          timeout=60, data=data)
    return out.strip().splitlines()[-1] if code == 0 and out.strip() else None


def terminal_command(cfg, local_root, cwd=None):
    rcwd = to_remote(cfg, local_root, cwd or local_root)
    return ssh_argv(cfg, f"cd {shlex.quote(rcwd)} 2>/dev/null; exec \"$SHELL\" -l", tty=True)


# ============================================================ File > Open Remote Folder
class OpenRemoteDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open Remote Folder (SSH)")
        self.resize(560, 360)
        self.cfg = None
        v = QVBoxLayout(self)
        intro = QLabel("Work on a project that lives on another Linux machine. Its folder is opened here over "
                       "SSH; Run, Debug and the Terminal run on that machine.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.recent = QComboBox()
        self.recent.addItem("(new connection)", None)
        for c in recent_hosts():
            self.recent.addItem(label(c), c)
        self.recent.currentIndexChanged.connect(self._pick)
        f = QFormLayout()
        f.addRow("Recent:", self.recent)
        self.host = QLineEdit()
        self.host.setPlaceholderText("server name or address, e.g. 192.168.1.50 or proliant.local")
        self.user = QLineEdit(os.environ.get("USER", ""))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.path = QLineEdit()
        self.path.setPlaceholderText("/home/you/projects/MyApp")
        b_browse = QPushButton("Browse...")
        b_browse.clicked.connect(self.browse)
        hp = QHBoxLayout()
        hp.addWidget(self.path, 1)
        hp.addWidget(b_browse)
        self.key = QLineEdit()
        self.key.setPlaceholderText("(none: use ssh-agent / default keys, or a password)")
        b_key = QPushButton("...")
        b_key.clicked.connect(self._pick_key)
        hk = QHBoxLayout()
        hk.addWidget(self.key, 1)
        hk.addWidget(b_key)
        self.python = QLineEdit()
        self.python.setPlaceholderText("python3 (or the project's .venv there, if it has one)")
        self.x11 = QCheckBox("Show the program's windows here (X11 forwarding: the server needs X11Forwarding yes)")
        f.addRow("Server:", self.host)
        f.addRow("User:", self.user)
        f.addRow("Port:", self.port)
        f.addRow("Folder on the server:", hp)
        f.addRow("SSH key:", hk)
        f.addRow("Python there:", self.python)
        f.addRow("", self.x11)
        v.addLayout(f)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.b_test = bb.addButton("Test Connection", QDialogButtonBox.ActionRole)
        self.b_test.clicked.connect(self.test)
        self.b_open = bb.addButton("Open", QDialogButtonBox.AcceptRole)
        self.b_open.setDefault(True)
        self.b_open.clicked.connect(self.open_)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        if self.recent.count() > 1:
            self.recent.setCurrentIndex(1)

    def _pick(self, _i):
        c = self.recent.currentData()
        if not c:
            return
        self.host.setText(c.get("host") or "")
        self.user.setText(c.get("user") or "")
        self.port.setValue(int(c.get("port") or 22))
        self.path.setText(c.get("path") or "")
        self.key.setText(c.get("key") or "")
        self.python.setText(c.get("python") or "")
        self.x11.setChecked(bool(c.get("x11")))

    def _pick_key(self):
        p, _f = QFileDialog.getOpenFileName(self, "SSH private key", os.path.expanduser("~/.ssh"))
        if p:
            self.key.setText(p)

    def current(self):
        return {"host": self.host.text().strip(), "user": self.user.text().strip(), "port": self.port.value(),
                "path": self.path.text().strip().rstrip("/") or "/", "key": self.key.text().strip(),
                "python": self.python.text().strip(), "x11": self.x11.isChecked()}

    def _valid(self):
        c = self.current()
        if not c["host"]:
            self.status.setText("Enter the server's name or address.")
            return None
        return c

    def test(self):
        c = self._valid()
        if not c:
            return
        self.status.setText("Connecting...")
        err = connect(c, self)
        if err:
            self.status.setText(f"<span style='color:#f48771'>{err}</span>")
            return
        code, out, _e = run(c, "python3 --version 2>&1; uname -n")
        self.status.setText(f"<span style='color:#89d185'>Connected.</span> {' / '.join(out.split())}")

    def browse(self):
        c = self._valid()
        if not c:
            return
        if connect(c, self):
            self.status.setText("Couldn't connect - Test Connection shows why.")
            return
        start = c["path"] if c["path"] != "/" else ""
        dlg = RemoteFolderPicker(c, start, self)
        if dlg.exec() == QDialog.Accepted:
            self.path.setText(dlg.chosen)

    def open_(self):
        c = self._valid()
        if not c:
            return
        self.status.setText("Opening...")
        mp, err = mount(c, self)
        if err:
            self.status.setText(f"<span style='color:#f48771'>{err}</span>")
            return
        self.cfg, self.local_root = c, mp
        self.accept()


class RemoteFolderPicker(QDialog):
    """Browse folders on the server."""

    def __init__(self, cfg, start, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(f"Folders on {cfg['host']}")
        self.resize(460, 420)
        self.chosen = start
        v = QVBoxLayout(self)
        self.where = QLabel()
        v.addWidget(self.where)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda it: self.go(it.text()))
        v.addWidget(self.list, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Use This Folder")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.load(start or "")

    def load(self, path):
        cmd = f"cd {shlex.quote(path)} 2>/dev/null || cd; pwd; ls -1Ap | grep '/$'" if path else "cd; pwd; ls -1Ap | grep '/$'"
        code, out, _e = run(self.cfg, cmd, timeout=30)
        lines = out.splitlines()
        self.chosen = lines[0] if lines else path
        self.where.setText(f"<b>{self.chosen}</b>  (double-click to open a folder)")
        self.list.clear()
        self.list.addItem("../")
        for d in sorted(lines[1:], key=str.lower):
            self.list.addItem(d)

    def go(self, name):
        if name == "../":
            self.load(os.path.dirname(self.chosen.rstrip("/")) or "/")
        else:
            self.load(self.chosen.rstrip("/") + "/" + name.rstrip("/"))

