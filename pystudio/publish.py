"""Publish (like VS Build > Publish): standalone app + install.sh + optional .deb."""
import glob
import os
import re
import shutil
import subprocess
import threading

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from .qttools import compile_all_stale

DEFAULT_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
<rect x="4" y="8" width="56" height="48" rx="6" fill="#2d2d30"/>
<rect x="4" y="8" width="56" height="12" rx="6" fill="#007acc"/>
<rect x="12" y="28" width="24" height="8" rx="2" fill="#dcdcdc"/>
<rect x="12" y="40" width="40" height="8" rx="2" fill="#4ec9b0"/></svg>
"""


def package_name(name):
    """Debian/desktop-safe id; must match pystudio_runtime._package_name()."""
    pkg = re.sub(r"[^a-z0-9+.-]+", "-", (name or "app").lower()).strip("-.+")
    return pkg if len(pkg) >= 2 else (pkg + "-app").lstrip("-")


def exe_name(name):
    return re.sub(r"[^A-Za-z0-9_-]", "", name or "") or "App"


def deb_version(v):
    v = re.sub(r"[^0-9A-Za-z.+~-]", "", v or "")
    return v if v[:1].isdigit() else "1.0.0"


def prune_qt(folder, uses_qml=False):
    """PyInstaller copies whole groups of Qt libraries. Keep only the Qt libraries something in
    the app actually links to (followed with ldd from every program, Python module and plugin),
    drop QML files for widget apps, and keep only the English QtWebEngine translations.
    Returns the bytes saved (0 when ldd isn't available)."""
    import subprocess
    internal = os.path.join(folder, "_internal")
    qt = os.path.join(internal, "PySide6", "Qt")
    qtlib, qml = os.path.join(qt, "lib"), os.path.join(qt, "qml")
    if not os.path.isdir(qtlib) or not shutil.which("ldd"):
        return 0

    def is_binary(path):
        name = os.path.basename(path)
        return ".so" in name or (os.access(path, os.X_OK) and "." not in name)
    roots = []
    for dirpath, _dirs, names in os.walk(folder):
        if dirpath.startswith(qtlib) or dirpath.startswith(qml):
            continue
        roots += [os.path.join(dirpath, n) for n in names if is_binary(os.path.join(dirpath, n))
                  and not os.path.islink(os.path.join(dirpath, n))]
    if uses_qml and os.path.isdir(qml):
        roots += [os.path.join(d, n) for d, _x, ns in os.walk(qml) for n in ns if ".so" in n]
    need, seen, todo = set(), set(), list(roots)
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        try:
            out = subprocess.run(["ldd", f], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return 0      # can't tell what's needed - keep everything
        for line in out.splitlines():
            if "=>" not in line:
                continue
            path = line.split("=>")[1].split("(")[0].strip()
            if path.startswith(internal):
                real = os.path.realpath(path)
                if real not in need:
                    need.add(real)
                    todo.append(real)
    saved = 0
    for name in os.listdir(qtlib):
        path = os.path.join(qtlib, name)
        real = os.path.realpath(path)
        if name.startswith("libQt6") and ".so" in name and real not in need:
            if not os.path.islink(path):
                saved += os.path.getsize(path)
            os.remove(path)
    if not uses_qml and os.path.isdir(qml):
        saved += sum(os.path.getsize(os.path.join(d, n)) for d, _x, ns in os.walk(qml) for n in ns
                     if not os.path.islink(os.path.join(d, n)))
        shutil.rmtree(qml)
    loc = os.path.join(qt, "translations", "qtwebengine_locales")
    if os.path.isdir(loc):
        for name in os.listdir(loc):
            if name not in ("en-US.pak", "en-GB.pak"):
                saved += os.path.getsize(os.path.join(loc, name))
                os.remove(os.path.join(loc, name))
    return saved


class Publisher(QObject):
    log = Signal(str)
    finished = Signal(bool, str)   # ok, message (release folder or error)
    _packaged = Signal(str)         # from the packaging thread: "" = done, else the error

    def __init__(self, project, python, single_file=False, make_deb=True, parent=None):
        super().__init__(parent)
        self.p = project
        self.python = python
        self.single_file = single_file
        self.make_deb = make_deb
        self.proc = None
        root = project.root
        self.display = project["title"] or project.name
        self.app = exe_name(project.name)
        self.pkg = package_name(project.name)
        self.version = project["version"] or "1.0.0"
        self.out = os.path.join(root, "publish")
        self.release = os.path.join(self.out, f"{self.app}-{self.version}-linux")
        self._canceled = False
        self._packaged.connect(self._package_done)

    # ------------------------------------------------------------ process chain
    def _run(self, args, then):
        self.proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setWorkingDirectory(self.p.root)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.log.emit(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")))
        self.proc.finished.connect(lambda code, _s: then(code))
        self.proc.errorOccurred.connect(lambda e: e == QProcess.FailedToStart and
                                        self._fail(f"Couldn't start {self.python}"))
        self.log.emit(f"> {self.python} {' '.join(args)}\n")
        self.proc.start(self.python, args)

    def _fail(self, msg):
        self.log.emit("\n" + msg + "\n")
        self.finished.emit(False, msg)

    def cancel(self):
        """Stop publishing (dialog closed). No signals afterwards."""
        self._canceled = True
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            try:
                self.proc.finished.disconnect()
                self.proc.readyReadStandardOutput.disconnect()
            except (RuntimeError, TypeError):
                pass
            self.proc.kill()
            self.proc.waitForFinished(3000)

    # ------------------------------------------------------------ steps
    def start(self):
        root = self.p.root
        start = self.p["startup"] or "main.py"
        if not os.path.exists(os.path.join(root, start)):
            return self._fail(f"{start} not found - set the startup file in Project Properties.")
        self.log.emit(f"Publishing {self.display} {self.version}...\n")
        if not self.p.external:         # PyStudio projects: forms and runtime up to date (others: as they are)
            for ok, msg in compile_all_stale(root):
                self.log.emit(msg + "\n")
                if not ok:
                    return self._fail("A form failed to generate; fix it and try again.")
            self.p.ensure_runtime()
        self._run(["-c", "import PyInstaller"], self._have_pyinstaller)

    def _have_pyinstaller(self, code):
        if code == 0:
            return self._build()
        self.log.emit("PyInstaller isn't installed for this Python - installing it (one time)...\n")
        self._run(["-m", "pip", "install", "--disable-pip-version-check", "pyinstaller"],
                  lambda c: self._build() if c == 0 else
                  self._fail("Couldn't install PyInstaller (internet needed). "
                             "Try: Project Properties > Packages > install 'pyinstaller'."))

    def _build(self):
        root = self.p.root
        sep = os.pathsep
        args = ["-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--log-level", "WARN",
                "--name", self.app,
                "--distpath", os.path.join(self.out, "dist"),
                "--workpath", os.path.join(self.out, "build"),
                "--specpath", self.out,
                "--paths", root,
                *[x for ref in _solution_refs(self.p) for x in ("--paths", ref)],
                "--add-data", f"{self.p.file}{sep}."]
        for folder in ("images", "resources", "data", "assets", "queries", "reports"):
            if os.path.isdir(os.path.join(root, folder)):
                args += ["--add-data", f"{os.path.join(root, folder)}{sep}{folder}"]
        for sql in sorted(glob.glob(os.path.join(root, "*.sql"))):
            args += ["--add-data", f"{sql}{sep}."]
        icon = self._icon_path()
        if icon and not os.path.relpath(icon, root).split(os.sep)[0] in ("images", "resources", "data", "assets"):
            args += ["--add-data", f"{icon}{sep}."]
        # Leave out big Qt modules the project never imports (saves tens of MB)
        used = self._project_source()
        if "PsChart" in used:                    # Chart control
            used += " QtCharts"
        if "PsWebBrowser" in used:               # WebBrowser control: QtWebEngine and what it needs
            used += " QtWebEngineCore QtWebEngineWidgets QtQuick QtQml QtQuickWidgets QtWebChannel QtPositioning"
        for mod in ("QtQuick", "QtQml", "QtPdf", "QtQuickWidgets", "QtWebEngineCore",
                    "QtWebEngineWidgets", "Qt3DCore", "QtCharts", "QtMultimedia"):
            if mod not in used:
                args += ["--exclude-module", f"PySide6.{mod}"]
        if self.single_file:
            args.append("--onefile")
        if self.p.external:             # files the code loads itself (uic.loadUi...) go along
            from .winpublish import external_data
            for f, dest in external_data(self.p):
                args += ["--add-data", f"{f}{sep}{dest}"]
        args.append(os.path.join(root, self.p["startup"] or "main.py"))
        self._run(args, self._built)

    def _project_source(self):
        text = []
        for dirpath, dirnames, files in os.walk(self.p.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("publish", "venv", "__pycache__")]
            for f in files:
                # pystudio_runtime.py mentions every Qt module it can use (Chart, WebBrowser) -
                # only the project's own code and forms say what's really used
                if f.endswith((".py", ".ui")) and f not in ("pystudio_runtime.py", "my.py"):
                    try:
                        with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                            text.append(fh.read())
                    except OSError:
                        pass
        return "\n".join(text)

    def _icon_path(self):
        icon = self.p["icon"]
        if not icon:
            return None
        full = icon if os.path.isabs(icon) else os.path.join(self.p.root, icon)
        return full if os.path.exists(full) else None

    def _built(self, code):
        if code != 0:
            return self._fail(f"PyInstaller failed (exit code {code}). See the log above.")
        # Copying, trimming Qt, the .tar.gz and the .deb take a minute or more: off the main thread, so
        # PyStudio keeps responding (no "not responding" box)
        self.log.emit("Packaging (the .tar.gz" + (" and .deb" if self.make_deb else "") + ") - this takes a "
                      "minute or two; PyStudio stays usable meanwhile...\n")

        def work():
            try:
                self._package()
                err = ""
            except Exception as e:  # noqa: BLE001 - report any packaging problem to the user
                err = str(e) or type(e).__name__
            try:
                self._packaged.emit(err)
            except RuntimeError:          # PyStudio closed meanwhile
                pass
        self._thread = threading.Thread(target=work, name="publish-package", daemon=True)
        self._thread.start()

    def _package_done(self, err):
        if self._canceled:
            return
        if err:
            return self._fail(f"Packaging failed: {err}")
        self.log.emit(f"\nPublished to {self.release}\n")
        self.finished.emit(True, self.release)

    # ------------------------------------------------------------ packaging
    def _package(self):
        dist = os.path.join(self.out, "dist")
        files = os.path.join(self.release, "files")
        if os.path.exists(self.release):
            shutil.rmtree(self.release)
        os.makedirs(files)
        if self.single_file:
            shutil.copy2(os.path.join(dist, self.app), os.path.join(files, self.app))
        else:
            # keep PyInstaller's symlinks (copying them as files nearly doubles the size)
            shutil.copytree(os.path.join(dist, self.app), files, symlinks=True, dirs_exist_ok=True)
            saved = prune_qt(files, "PySide6.QtQml" in self._project_source() or "QtQuick" in self._project_source())
            if saved:
                self.log.emit(f"Removed {saved / 1e6:.0f} MB of Qt files the app never loads.\n")
        os.chmod(os.path.join(files, self.app), 0o755)

        icon = self._icon_path()
        if icon:
            icon_name = "icon" + os.path.splitext(icon)[1].lower()
            shutil.copy2(icon, os.path.join(files, icon_name))
        else:
            icon_name = "icon.svg"
            with open(os.path.join(files, icon_name), "w") as f:
                f.write(DEFAULT_ICON)

        self._write_scripts(icon_name)
        archive = shutil.make_archive(self.release, "gztar", self.out, os.path.basename(self.release))
        self.log.emit(f"Created {archive}\n")
        if self.make_deb:
            self._make_deb(files, icon_name)

    def _desktop_entry(self, exec_path, folder, icon_path):
        desc = self.p["description"] or self.display
        return (f"[Desktop Entry]\nType=Application\nName={self.display}\nComment={desc}\n"
                f"Exec=\"{exec_path}\" %F\nPath={folder}\nIcon={icon_path}\nTerminal=false\n"
                f"Categories=Utility;\nStartupWMClass={self.app}\n")

    def _write_scripts(self, icon_name):
        pkg, app = self.pkg, self.app
        install = f'''#!/usr/bin/env bash
# Installs {self.display} {self.version} for the current user:
# app menu entry, desktop icon, and the '{pkg}' terminal command.
set -e
HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
DEST="$HOME/.local/share/{pkg}"

if ! dpkg -s libxcb-cursor0 >/dev/null 2>&1; then
    echo "Installing libxcb-cursor0 (needed by Qt; sudo password may be requested)"
    sudo apt-get install -y libxcb-cursor0
fi

rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$HERE/files/." "$DEST/"
chmod +x "$DEST/{app}"
mkdir -p "$HOME/.local/bin"
ln -sf "$DEST/{app}" "$HOME/.local/bin/{pkg}"

DESKTOP_FILE="$HOME/.local/share/applications/{pkg}.desktop"
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<EOF
{self._desktop_entry("$DEST/" + app, "$DEST", "$DEST/" + icon_name)}EOF
chmod +x "$DESKTOP_FILE"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
    cp "$DESKTOP_FILE" "$DESKTOP_DIR/{pkg}.desktop"
    chmod +x "$DESKTOP_DIR/{pkg}.desktop"
    gio set "$DESKTOP_DIR/{pkg}.desktop" metadata::trusted true 2>/dev/null || true
fi
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "{self.display} installed. Start it from the desktop icon, the app menu, or: {pkg}"
'''
        uninstall = f'''#!/usr/bin/env bash
# Removes {self.display} (installed by install.sh).
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
rm -rf "$HOME/.local/share/{pkg}"
rm -f "$HOME/.local/bin/{pkg}" "$HOME/.local/share/applications/{pkg}.desktop" "$DESKTOP_DIR/{pkg}.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "{self.display} removed."
'''
        readme = (f"{self.display} {self.version}\n\n"
                  f"Install (current user):   bash install.sh\n"
                  f"Remove:                   bash uninstall.sh\n"
                  f"Run without installing:   ./files/{app}\n")
        for name, text, mode in (("install.sh", install, 0o755), ("uninstall.sh", uninstall, 0o755),
                                 ("README.txt", readme, 0o644)):
            path = os.path.join(self.release, name)
            with open(path, "w", newline="\n") as f:
                f.write(text)
            os.chmod(path, mode)

    def _make_deb(self, files, icon_name):
        if not shutil.which("dpkg-deb"):
            self.log.emit("dpkg-deb not found - skipping the .deb\n")
            return
        pkg, app = self.pkg, self.app
        ver = deb_version(self.version)
        arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True).stdout.strip() or "amd64"
        stage = os.path.join(self.out, "deb", f"{pkg}_{ver}")
        if os.path.exists(stage):
            shutil.rmtree(stage)
        opt = os.path.join(stage, "opt", pkg)
        shutil.copytree(files, opt, symlinks=True)
        apps = os.path.join(stage, "usr", "share", "applications")
        os.makedirs(apps)
        with open(os.path.join(apps, f"{pkg}.desktop"), "w") as f:
            f.write(self._desktop_entry(f"/opt/{pkg}/{app}", f"/opt/{pkg}", f"/opt/{pkg}/{icon_name}"))
        bindir = os.path.join(stage, "usr", "bin")
        os.makedirs(bindir)
        os.symlink(f"/opt/{pkg}/{app}", os.path.join(bindir, pkg))
        size_kb = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(opt)
                      for f in fs if not os.path.islink(os.path.join(dp, f))) // 1024
        maint = self.p["company"] or self.display
        desc = self.p["description"] or self.display
        os.makedirs(os.path.join(stage, "DEBIAN"))
        with open(os.path.join(stage, "DEBIAN", "control"), "w") as f:
            f.write(f"Package: {pkg}\nVersion: {ver}\nSection: utils\nPriority: optional\n"
                    f"Architecture: {arch}\nDepends: libxcb-cursor0\nInstalled-Size: {size_kb}\n"
                    f"Maintainer: {maint} <noreply@localhost>\n"
                    f"Description: {self.display}\n {desc}\n")
        for dp, dirs, _fs in os.walk(stage):
            for d in dirs:
                os.chmod(os.path.join(dp, d), 0o755)
        deb = os.path.join(self.out, f"{pkg}_{ver}_{arch}.deb")
        r = subprocess.run(["dpkg-deb", "--build", "--root-owner-group", stage, deb],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "dpkg-deb failed")
        shutil.rmtree(os.path.join(self.out, "deb"), ignore_errors=True)
        self.log.emit(f"Created {deb}\n   Install with:  sudo apt install ./{os.path.basename(deb)}\n")


def _solution_refs(project):
    """Referenced projects of the solution (their packages get bundled like the project's own)."""
    from .solution import all_references
    try:
        return all_references(project)
    except Exception:  # noqa: BLE001
        return []
