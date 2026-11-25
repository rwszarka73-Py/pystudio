"""Publish for Windows - a Windows .exe of a project, made on Linux. Two ways:

1. Build here with Wine: PyStudio keeps its own Wine prefix (~/.local/share/PyStudio/wine-win64) with the
   Windows version of Python, installs PyInstaller and the project's packages there (one time), and runs
   PyInstaller - the result is a real Windows program (a folder with App.exe, zipped, or one App.exe).
2. Build on GitHub: a GitHub Actions workflow builds it on GitHub's Windows machines. PyStudio writes the
   workflow; push it, then Run on GitHub and Download (needs the gh command, signed in), or use the website."""
import glob
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget)

from . import interp
from .publish import _solution_refs, exe_name
from .qttools import compile_all_stale

WIN_PYTHON_VERSION = "3.12.8"
# A portable Windows Python (python-build-standalone, the builds uv uses): an archive to unpack - no Windows
# installer to run under Wine, and pip comes with it.
WIN_PYTHON_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/20241219/"
                  f"cpython-{WIN_PYTHON_VERSION}+20241219-x86_64-pc-windows-msvc-install_only.tar.gz")
PREFIX = os.path.join(os.path.expanduser("~"), ".local", "share", "PyStudio", "wine-win64")
WIN_PY_DIR = "C:\\Python312"
DATA_FOLDERS = ("images", "resources", "data", "assets", "queries", "reports")
LINUX_ONLY = {"gi", "dbus", "evdev", "pyudev", "pwd", "grp", "fcntl", "termios", "posix", "resource",
              "pyinotify", "systemd", "apt", "xdg", "Xlib", "notify2"}
WORKFLOW = "pystudio-windows.yml"
# Wine has no icuuc.dll (Windows 10+ does), and Qt 6.10+ needs it - under Wine, PyInstaller can't load QtCore and
# leaves out Qt's platform plugin (the .exe wouldn't start). PySide6 6.9 is the newest without it.
WINE_PYSIDE = "6.9.3"
ADDON_MODULES = ("QtCharts", "QtWebEngine", "QtMultimedia", "QtPdf", "Qt3D", "QtSerialPort", "QtWebSockets",
                 "QtBluetooth", "QtPositioning", "QtDataVisualization", "QtGraphs", "QtRemoteObjects", "QtSensors",
                 "QtScxml", "QtTextToSpeech", "QtHttpServer", "QtNetworkAuth", "QtStateMachine", "QtLocation",
                 "QtNfc", "QtSpatialAudio", "PsChart", "PsWebBrowser")


def wine_exe():
    return shutil.which("wine") or shutil.which("wine64")


def win_python_path():
    """Where Windows Python lives inside PyStudio's Wine prefix (a Linux path)."""
    return os.path.join(PREFIX, "drive_c", "Python312", "python.exe")


def to_wine(path):
    """A Linux path as Wine sees it (Z: is the Linux root)."""
    return "Z:" + os.path.abspath(path).replace("/", "\\")


def startup_file(project):
    return project["startup"] or "main.py"


def pip_packages(project):
    """pip names of what the project imports (plus PyInstaller), and Linux-only modules left out."""
    mods = set(interp.project_imports(project.root))
    for ref in _solution_refs(project):
        mods |= set(interp.project_imports(ref))
    if not project.external:
        mods.add("PySide6")                  # pystudio_runtime.py and the generated forms use it
    skipped = sorted(m for m in mods if m in LINUX_ONLY)
    pkgs = sorted({interp.pip_name(m) for m in mods if m not in LINUX_ONLY and m != "pystudio_runtime"})
    return ["pyinstaller"] + pkgs, skipped


DATA_SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "env", "build", "dist", "publish", "installer",
                  "node_modules", ".pytest_cache", ".mypy_cache", ".idea", ".vscode", ".ipynb_checkpoints", ".github"}
CODE_EXTS = {".py", ".pyc", ".pyo", ".pyd", ".pyproj", ".pysln", ".spec", ".nsi", ".log",
             ".bak", ".orig", ".rej", ".tmp", ".swp"}       # code, PyStudio's files, backups
MAX_DATA_FILE = 200 * 1024 * 1024


def external_data(project):
    """[(file, destination folder in the bundle)] for a project made outside PyStudio: every file the code may
    read while it runs - stylesheets, icons, images, fonts, .ui forms, JSON/INI/CSV data - from the startup file's
    folder and its subfolders, placed the same way relative to it. In the built program the startup file's folder
    (os.path.dirname(__file__)) is the top of the bundle, so open(os.path.join(APP_DIR, "style.qss")) and
    loadUi(os.path.join(APP_DIR, "mainwindow.ui")) find them."""
    root = project.root
    start_dir = os.path.dirname(os.path.join(root, startup_file(project)))
    out = []
    for dirpath, dirs, files in os.walk(start_dir):
        dirs[:] = sorted(d for d in dirs if d not in DATA_SKIP_DIRS and not d.startswith(".")
                         and not os.path.exists(os.path.join(dirpath, d, "pyvenv.cfg")))
        rel = os.path.relpath(dirpath, start_dir).replace(os.sep, "/")
        for f in sorted(files):
            full = os.path.join(dirpath, f)
            ext = os.path.splitext(f)[1].lower()
            if ext in CODE_EXTS or f.startswith(".") or f.endswith("~") or ".bak" in f.lower() or \
                    ext == ".converted":         # PyStudio's form-conversion leftovers
                continue
            try:
                if os.path.getsize(full) > MAX_DATA_FILE:
                    continue
            except OSError:
                continue
            out.append((full, rel))
    if start_dir != root:                    # forms kept at the project's top level
        for f in sorted(glob.glob(os.path.join(root, "*.ui"))):
            out.append((f, "."))
    return out


def package_data(project):
    """[(file, destination folder)] for a PyStudio project: the non-code files inside its Python package folders
    (any top-level folder with .py files in it), placed the same way, so code that opens a file next to itself
    (os.path.join(os.path.dirname(__file__), "mainwindow.ui"), assets/fonts...) finds it in the built program."""
    root = project.root
    out = []
    for top in sorted(os.listdir(root)):
        full_top = os.path.join(root, top)
        if (not os.path.isdir(full_top) or top in DATA_SKIP_DIRS or top in DATA_FOLDERS or top.startswith(".")
                or os.path.exists(os.path.join(full_top, "pyvenv.cfg"))):
            continue
        walk = list(os.walk(full_top))
        if not any(f.endswith(".py") for _d, _s, files in walk for f in files):
            continue                          # not code: docs, test videos...
        for dirpath, dirs, files in os.walk(full_top):
            dirs[:] = sorted(d for d in dirs if d not in DATA_SKIP_DIRS and not d.startswith(".")
                             and not os.path.exists(os.path.join(dirpath, d, "pyvenv.cfg")))
            rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            for f in sorted(files):
                full = os.path.join(dirpath, f)
                ext = os.path.splitext(f)[1].lower()
                if ext in CODE_EXTS or f.startswith(".") or f.endswith("~") or ".bak" in f.lower() or \
                        ext == ".converted":
                    continue
                try:
                    if os.path.getsize(full) > MAX_DATA_FILE:
                        continue
                except OSError:
                    continue
                out.append((full, rel))
    return out


def icon_source(project):
    """The program's icon: the one set in Project Properties, or else an icon file next to the startup file
    (icon / app / appicon / <name> . ico / png / svg)."""
    icon = project["icon"]
    full = icon if icon and os.path.isabs(icon) else os.path.join(project.root, icon) if icon else ""
    if full and os.path.exists(full):
        return full
    start_dir = os.path.dirname(os.path.join(project.root, startup_file(project)))
    for folder in dict.fromkeys([start_dir, project.root, os.path.join(start_dir, "icons"),
                                 os.path.join(start_dir, "resources")]):
        for stem in ("icon", "app", "appicon", "app_icon", project.name, exe_name(project.name)):
            for ext in (".ico", ".png", ".svg"):
                p = os.path.join(folder, stem + ext)
                if os.path.exists(p):
                    return p
    return None


def pyinstaller_args(project, single_file, sep, path=lambda p: p, out=None, ico=None):
    """PyInstaller arguments shared by the Wine build and the GitHub workflow. path() maps a file path for
    the machine that builds (Wine: Z:\\..., GitHub: relative to the project)."""
    root = project.root
    name = exe_name(project.name)
    args = ["--noconfirm", "--clean", "--windowed", "--name", name]
    if out:
        args += ["--distpath", path(os.path.join(out, "dist")), "--workpath", path(os.path.join(out, "build")),
                 "--specpath", path(out)]
    args += ["--paths", path(root)]
    for ref in _solution_refs(project):
        args += ["--paths", path(ref)]
    if os.path.exists(project.file) and not project.external:     # PyStudio projects read it (setup_app)
        args += ["--add-data", f"{path(project.file)}{sep}."]
    for folder in DATA_FOLDERS:
        if os.path.isdir(os.path.join(root, folder)):
            args += ["--add-data", f"{path(os.path.join(root, folder))}{sep}{folder}"]
    for f in sorted(glob.glob(os.path.join(root, "*.sql"))):
        args += ["--add-data", f"{path(f)}{sep}."]
    # files the code loads itself (QUiLoader / uic.loadUi, fonts, icons, data) go along
    for f, dest in (external_data(project) if project.external else package_data(project)):
        args += ["--add-data", f"{path(f)}{sep}{dest}"]
    if ico:                                   # the .exe's own icon (Explorer, taskbar, shortcuts): must be .ico
        args += ["--icon", path(ico)]
    full = icon_source(project)
    if full and not project.external:         # PyStudio projects load it at run time (setup_app)
        args += ["--add-data", f"{path(full)}{sep}."]
    if single_file:
        args.append("--onefile")
    args.append(path(os.path.join(root, startup_file(project))))
    return args


# ============================================================ Setup.exe (NSIS)
def makensis_exe():
    return shutil.which("makensis")


def _version4(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "")][:4]
    return ".".join(str(n) for n in (nums + [0, 0, 0, 0])[:4])


def _nsis_str(text):
    return (text or "").replace("$", "$$").replace('"', "$\\\"")


def make_icon(project, dest):
    """The program's icon (icon_source) as a Windows .ico - Qt converts PNG / SVG - or None."""
    full = icon_source(project)
    if not full:
        return None
    if full.lower().endswith(".ico"):
        shutil.copy2(full, dest)
        return dest
    from PySide6.QtGui import QIcon
    img = QIcon(full).pixmap(256, 256).toImage()
    return dest if not img.isNull() and img.save(dest, "ICO") else None


def nsis_target():
    """amd64-unicode when this NSIS has 64-bit stubs (Ubuntu's does): a 64-bit installer for the 64-bit app."""
    for base in ("/usr/share/nsis/Stubs", "/usr/local/share/nsis/Stubs"):
        if os.path.exists(os.path.join(base, "lzma_solid-amd64-unicode")):
            return "amd64-unicode"
    return None


def nsis_script(project, payload, out_exe, icon=None, rel=lambda p: p, target=None):
    """The installer script. payload: the program folder (with App.exe) or a single App.exe. Installs for the
    current user (no admin password): %LOCALAPPDATA%\\Programs\\<App>, Start menu, optional desktop icon, an
    entry in Settings > Apps, and an uninstaller. rel() maps paths (relative ones for the GitHub workflow)."""
    app = exe_name(project.name)
    title = project["title"] or project.name
    version = project["version"] or "1.0.0"
    company = project["company"] or ""
    folder = os.path.isdir(payload) if os.path.exists(payload) else not payload.lower().endswith(".exe")
    sep = "\\" if "\\" in rel(payload) else "/"
    files = f'  File /r "{rel(payload)}{sep}*"' if folder else f'  File "/oname={app}.exe" "{rel(payload)}"'
    icon_lines = f'!define MUI_ICON "{rel(icon)}"\n!define MUI_UNICON "{rel(icon)}"\n' if icon else ""
    lnk = _nsis_str(re.sub(r'[<>:"/\\|?*]', "", title).strip() or app)     # a valid Windows file name
    key = f"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{app}"
    return f"""; Made by PyStudio (Build > Publish for Windows). Build it with:  makensis {app}.nsi
{f"Target {target}" if target else "Unicode true"}
!include "MUI2.nsh"
!include "FileFunc.nsh"
Name "{_nsis_str(title)}"
OutFile "{rel(out_exe)}"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\\Programs\\{app}"
InstallDirRegKey HKCU "Software\\{app}" "InstallDir"
SetCompressor /SOLID lzma
{icon_lines}VIProductVersion "{_version4(version)}"
VIAddVersionKey "ProductName" "{_nsis_str(title)}"
VIAddVersionKey "ProductVersion" "{_nsis_str(version)}"
VIAddVersionKey "FileVersion" "{_nsis_str(version)}"
VIAddVersionKey "FileDescription" "{_nsis_str(title)} Setup"
VIAddVersionKey "CompanyName" "{_nsis_str(company)}"
VIAddVersionKey "LegalCopyright" "{_nsis_str(project['copyright'])}"

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\\{app}.exe"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "{_nsis_str(title)} (required)" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
{files}
  WriteUninstaller "$INSTDIR\\Uninstall.exe"
  CreateShortcut "$SMPROGRAMS\\{lnk}.lnk" "$INSTDIR\\{app}.exe"
  WriteRegStr HKCU "Software\\{app}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "{key}" "DisplayName" "{_nsis_str(title)}"
  WriteRegStr HKCU "{key}" "DisplayVersion" "{_nsis_str(version)}"
  WriteRegStr HKCU "{key}" "Publisher" "{_nsis_str(company)}"
  WriteRegStr HKCU "{key}" "DisplayIcon" "$INSTDIR\\{app}.exe"
  WriteRegStr HKCU "{key}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "{key}" "UninstallString" '"$INSTDIR\\Uninstall.exe"'
  WriteRegStr HKCU "{key}" "QuietUninstallString" '"$INSTDIR\\Uninstall.exe" /S'
  WriteRegDWORD HKCU "{key}" "NoModify" 1
  WriteRegDWORD HKCU "{key}" "NoRepair" 1
  ${{GetSize}} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "{key}" "EstimatedSize" $0
SectionEnd

Section "Desktop shortcut" SecDesktop
  CreateShortcut "$DESKTOP\\{lnk}.lnk" "$INSTDIR\\{app}.exe"
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\\{lnk}.lnk"
  Delete "$DESKTOP\\{lnk}.lnk"
  ; remove the whole folder only when it is the program's own folder (not, say, Desktop)
  ${{GetFileName}} "$INSTDIR" $0
  StrCmp $0 "{app}" 0 partial
    RMDir /r "$INSTDIR"
    Goto removed
  partial:
    Delete "$INSTDIR\\{app}.exe"
    RMDir /r "$INSTDIR\\_internal"
    Delete "$INSTDIR\\Uninstall.exe"
    RMDir "$INSTDIR"
  removed:
  DeleteRegKey HKCU "{key}"
  DeleteRegKey HKCU "Software\\{app}"
SectionEnd
"""


def build_installer(project, payload, out_dir, log=lambda t: None):
    """Writes <App>.nsi next to the build and runs makensis: <App>-<version>-setup.exe, or raises RuntimeError."""
    exe = makensis_exe()
    if not exe:
        raise RuntimeError("makensis isn't installed. In a terminal:  sudo apt install nsis")
    app = exe_name(project.name)
    os.makedirs(out_dir, exist_ok=True)
    icon = make_icon(project, os.path.join(out_dir, app + ".ico"))
    setup = os.path.join(out_dir, f"{app}-{project['version'] or '1.0.0'}-setup.exe")
    nsi = os.path.join(out_dir, app + ".nsi")
    with open(nsi, "w", encoding="utf-8") as f:
        f.write(nsis_script(project, payload, setup, icon, target=nsis_target()))
    log(f"> makensis {os.path.basename(nsi)}\n")
    r = subprocess.run([exe, "-V2", "-INPUTCHARSET", "UTF8", nsi], capture_output=True, text=True, timeout=600)
    if r.stdout.strip():
        log(r.stdout[-3000:] + "\n")
    if r.returncode != 0 or not os.path.exists(setup):
        raise RuntimeError("makensis failed:\n" + (r.stderr or r.stdout)[-2000:])
    return setup


# ============================================================ 1. Wine
class WineBuilder(QObject):
    _packaged = Signal(bool, str, str)   # from the packaging thread: ok, result or error, log text
    log = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, project, pystudio_python=sys.executable, single_file=False, installer=False, parent=None):
        super().__init__(parent)
        self._canceled = False
        self._packaged.connect(self._package_done)
        self.p = project
        self.python = pystudio_python
        self.single_file = single_file
        self.installer = installer
        self.proc = None
        self.wine = wine_exe()
        self.version = project["version"] or "1.0.0"
        self.app = exe_name(project.name)
        self.out = os.path.join(project.root, "publish", "windows")

    def _env(self):
        env = QProcessEnvironment.systemEnvironment()
        env.insert("WINEPREFIX", PREFIX)
        env.insert("WINEARCH", "win64")
        env.insert("WINEDEBUG", "-all")
        env.insert("WINEDLLOVERRIDES", "mscoree,mshtml=")     # no Mono / Gecko install prompts
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        return env

    def _run(self, program, args, then, show=None):
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(self._env())
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setWorkingDirectory(self.p.root)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.log.emit(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")))
        self.proc.finished.connect(lambda code, _s: then(code))
        self.proc.errorOccurred.connect(lambda e: e == QProcess.FailedToStart and self._fail(f"Couldn't start {program}"))
        self.log.emit(f"> {show or (os.path.basename(program) + ' ' + ' '.join(args))}\n")
        self.proc.start(program, args)

    def _wpy(self, args, then):
        self._run(self.wine, [WIN_PY_DIR + "\\python.exe"] + args, then,
                  show="(Windows) python " + " ".join(args))

    def _fail(self, msg):
        self.log.emit("\n" + msg + "\n")
        self.finished.emit(False, msg)

    def cancel(self):
        self._canceled = True
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            try:
                self.proc.finished.disconnect()
                self.proc.readyReadStandardOutput.disconnect()
            except (RuntimeError, TypeError):
                pass
            self.proc.kill()
            self.proc.waitForFinished(3000)

    # ------------------------------------------------------------ setup (one time)
    def start(self):
        if not self.wine:
            return self._fail("Wine isn't installed. In a terminal:  sudo apt install wine   (then try again)")
        start = os.path.join(self.p.root, startup_file(self.p))
        if not os.path.exists(start):
            return self._fail(f"{startup_file(self.p)} not found - set the startup file in Project Properties.")
        self.log.emit(f"Publishing {self.p.name} {self.version} for Windows (Wine: {self.wine})...\n")
        if not self.p.external:
            for ok, msg in compile_all_stale(self.p.root):
                self.log.emit(msg + "\n")
                if not ok:
                    return self._fail("A form failed to generate; fix it and try again.")
            self.p.ensure_runtime()
        if os.path.exists(win_python_path()):
            return self._packages()
        os.makedirs(PREFIX, exist_ok=True)
        self.log.emit("First time: setting up Windows Python in PyStudio's own Wine folder "
                      f"({PREFIX}). This downloads about 40 MB plus the packages, and takes a few minutes.\n")
        self._run(self.wine, ["wineboot", "-u"], lambda _c: self._set_win10(), show="wineboot -u")

    def _set_win10(self):
        self._run(self.wine, ["reg", "add", "HKCU\\Software\\Wine", "/v", "Version", "/d", "win10", "/f"],
                  lambda _c: self._download(), show="wine reg add ... Version win10")

    def _download(self):
        self.archive = os.path.join(PREFIX, os.path.basename(WIN_PYTHON_URL).replace("+", "-"))
        if os.path.exists(self.archive) and os.path.getsize(self.archive) > 10_000_000:
            return self._unpack()
        code = ("import sys, urllib.request\nurllib.request.urlretrieve(sys.argv[1], sys.argv[2])\n"
                "print('downloaded', sys.argv[2])")
        self._run(self.python, ["-c", code, WIN_PYTHON_URL, self.archive + ".part"],
                  lambda c: (os.replace(self.archive + ".part", self.archive), self._unpack()) if c == 0 else
                  self._fail("Couldn't download Windows Python (internet needed): " + WIN_PYTHON_URL),
                  show=f"download Windows Python {WIN_PYTHON_VERSION} (about 40 MB)")

    def _unpack(self):
        import tarfile
        drive_c = os.path.join(PREFIX, "drive_c")
        target = os.path.dirname(win_python_path())
        tmp = os.path.join(drive_c, "python-unpack")
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            with tarfile.open(self.archive) as t:
                try:
                    t.extractall(tmp, filter="data")
                except TypeError:                       # Python before 3.12 (no filter argument)
                    t.extractall(tmp)
            shutil.rmtree(target, ignore_errors=True)
            os.replace(os.path.join(tmp, "python"), target)
            shutil.rmtree(tmp, ignore_errors=True)
        except (OSError, tarfile.TarError) as e:
            return self._fail(f"Couldn't unpack Windows Python: {e}")
        self.log.emit(f"Windows Python {WIN_PYTHON_VERSION} is in {target}\n")
        self._packages()

    # ------------------------------------------------------------ packages, then PyInstaller
    def _wine_pyside(self):
        """PySide6 for the Wine build: 6.9.x (see WINE_PYSIDE); just the Essentials unless the code uses an
        add-on module (Charts, WebEngine, Multimedia...)."""
        from .publish import Publisher
        try:
            source = Publisher(self.p, self.python)._project_source()
        except Exception:  # noqa: BLE001
            source = ""
        full = any(m in source for m in ADDON_MODULES)
        return f"PySide6=={WINE_PYSIDE}" if full else f"PySide6-Essentials=={WINE_PYSIDE}"

    def _site_packages(self):
        return os.path.join(os.path.dirname(win_python_path()), "Lib", "site-packages")

    def _packages(self):
        stale = [d for d in glob.glob(os.path.join(self._site_packages(), "*.dist-info"))
                 if re.match(r"(?i)(pyside6|shiboken6)[^-]*-(\d[\w.]*)\.dist-info$", os.path.basename(d))
                 and not os.path.basename(d).split("-")[1].startswith(WINE_PYSIDE)]
        if stale:
            self.log.emit("Replacing PySide6 in the Windows build folder with " + WINE_PYSIDE + "...\n")
            names = sorted({os.path.basename(d).split("-")[0] for d in stale})
            return self._wpy(["-m", "pip", "uninstall", "-y"] + names, lambda _c: self._packages2())
        self._packages2()

    def _packages2(self):
        pkgs, skipped = pip_packages(self.p)
        pkgs = [self._wine_pyside() if x == "PySide6" else x for x in pkgs]
        if skipped:
            self.log.emit(f"Left out (Linux-only, no Windows version): {', '.join(skipped)} - code that uses them "
                          "must be skipped on Windows (if sys.platform == 'win32': ...).\n")
        self.queue = list(pkgs)
        self.failed = []
        self.log.emit("Packages for the Windows build: " + ", ".join(pkgs) + "\n")
        self._wpy(["-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "--quiet"] + pkgs,
                  self._installed_all)

    def _installed_all(self, code):
        if code == 0:
            return self._build()
        self.log.emit("Installing them together failed - trying one at a time...\n")
        self._next_package()

    def _next_package(self, code=None):
        if code not in (None, 0):
            self.failed.append(self.current)
        if not self.queue:
            if "pyinstaller" in self.failed:
                return self._fail("PyInstaller couldn't be installed in Windows Python.")
            if self.failed:
                self.log.emit(f"These packages have no Windows version or failed to install: "
                              f"{', '.join(self.failed)} - the .exe is built without them.\n")
            return self._build()
        self.current = self.queue.pop(0)
        self._wpy(["-m", "pip", "install", "--disable-pip-version-check", "--quiet", self.current], self._next_package)

    def _build(self):
        os.makedirs(self.out, exist_ok=True)
        files = external_data(self.p) if self.p.external else package_data(self.p)
        if self.p.external or files:
            start_dir = (os.path.dirname(os.path.join(self.p.root, startup_file(self.p))) if self.p.external
                         else self.p.root)
            names = [os.path.relpath(f, start_dir) for f, _d in files]
            self.log.emit(f"Including {len(names)} file(s) the program reads (stylesheets, icons, forms, data): "
                          + (", ".join(names[:40]) + (" ..." if len(names) > 40 else "") if names else "none")
                          + "\n")
        ico = make_icon(self.p, os.path.join(self.out, self.app + ".ico"))
        if ico:
            self.log.emit(f"Program icon: {os.path.relpath(icon_source(self.p), self.p.root)}\n")
        else:
            self.log.emit("No program icon (Windows shows a generic one): set one in Project Properties > "
                          "Application > Icon, or put icon.png / icon.svg / icon.ico next to the startup file.\n")
        args = ["-m", "PyInstaller", "--log-level", "WARN"] + pyinstaller_args(
            self.p, self.single_file, ";", to_wine, self.out, ico)
        self._wpy(args, self._built)

    def _built(self, code):
        if code != 0:
            return self._fail(f"PyInstaller failed (exit code {code}). See the log above.")
        # zipping and makensis take a minute or more: off the main thread, so PyStudio keeps responding
        self.log.emit("Packaging" + (" and building Setup.exe" if self.installer else "") + " - this takes a "
                      "minute or two; PyStudio stays usable meanwhile...\n")
        self._canceled = False

        def work():
            try:
                res = self._package()
            except Exception as e:  # noqa: BLE001 - any packaging problem goes to the log
                res = (False, f"Packaging failed: {e}", "")
            try:
                self._packaged.emit(*res)
            except RuntimeError:          # PyStudio closed meanwhile
                pass
        threading.Thread(target=work, name="winpublish-package", daemon=True).start()

    def _package_done(self, ok, msg, text):
        if getattr(self, "_canceled", False):
            return
        if not ok:
            return self._fail(msg)
        self.log.emit(text)
        self.finished.emit(True, msg)

    def _package(self):
        """(ok, result path or error, log text) - runs in a thread: no signals from here except log."""
        dist = os.path.join(self.out, "dist")
        try:
            if self.single_file:
                exe = os.path.join(dist, self.app + ".exe")
                if not os.path.exists(exe):
                    return False, f"{self.app}.exe wasn't made - see the log above.", ""
                result = os.path.join(self.out, f"{self.app}-{self.version}.exe")
                shutil.copy2(exe, result)
            else:
                folder = os.path.join(dist, self.app)
                if not os.path.exists(os.path.join(folder, self.app + ".exe")):
                    return False, f"{self.app}.exe wasn't made - see the log above.", ""
                result = os.path.join(self.out, f"{self.app}-{self.version}-windows.zip")
                with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as z:
                    for dirpath, _dirs, files in os.walk(folder):
                        for f in files:
                            full = os.path.join(dirpath, f)
                            z.write(full, os.path.join(self.app, os.path.relpath(full, folder)))
        except OSError as e:
            return False, f"Packaging failed: {e}", ""
        if self.installer:
            payload = os.path.join(dist, self.app + ".exe") if self.single_file else os.path.join(dist, self.app)
            try:
                setup = build_installer(self.p, payload, self.out, self.log.emit)
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                return False, f"The program was built ({result}), but the installer wasn't: {e}", ""
            size = os.path.getsize(setup) / 1_048_576
            return True, setup, (f"\nInstaller: {setup}  ({size:.0f} MB)\nRun it on Windows: it installs for the "
                                 "current user (no admin password), adds a Start menu entry, a desktop icon "
                                 "(optional) and an entry in Settings > Apps to uninstall it.\nAlso made: "
                                 + result + "\n")
        size = os.path.getsize(result) / 1_048_576
        return True, result, (f"\nWindows build: {result}  ({size:.0f} MB)\n" + (
            "Copy it to a Windows PC and run it." if self.single_file else
            f"Copy the zip to a Windows PC, extract it, and run {self.app}\\{self.app}.exe.") + "\n")


# ============================================================ 2. GitHub Actions
def git_root(path):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def workflow_text(project, single_file, repo_root, installer=False, ico=None):
    """The GitHub Actions workflow that builds the .exe on windows-latest."""
    rel = os.path.relpath(project.root, repo_root).replace(os.sep, "/")
    work = "." if rel == "." else rel

    def path(p):                              # the build step runs in the project folder
        return os.path.relpath(p, project.root).replace(os.sep, "/")

    args = pyinstaller_args(project, single_file, ";", path, ico=ico)
    # windows-latest runs steps in PowerShell, where ; ends a command: quote anything that isn't plain
    quoted = " ".join(a if re.fullmatch(r"[\w./:=-]+", a) else f'"{a}"' for a in args)
    pkgs, _skipped = pip_packages(project)
    name = exe_name(project.name)
    artifact = f"dist/{name}.exe" if single_file else f"dist/{name}"
    up = (f"{work}/{artifact}" if work != "." else artifact)
    forms = "" if project.external else (
        "      - name: Generate the forms (ui_*.py)\n"
        "        shell: bash\n"
        f"        working-directory: {work}\n"
        "        run: for f in $(find . -name '*.ui' -not -path './publish/*'); do "
        "d=$(dirname \"$f\"); b=$(basename \"$f\" .ui); pyside6-uic \"$f\" -o \"$d/ui_$b.py\"; done\n")
    return f"""# Made by PyStudio (Build > Publish for Windows). Builds {name}.exe on GitHub's Windows machines.
# Run it: GitHub > Actions > "Build {name} for Windows" > Run workflow (or PyStudio's Run on GitHub);
# the .exe is under the run's Artifacts. Pushing a tag like v1.2.0 also builds it.
name: Build {name} for Windows

on:
  workflow_dispatch:
  push:
    tags: ["v*"]

jobs:
  windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install packages
        run: python -m pip install --upgrade {" ".join(pkgs)}
{forms}      - name: Build {name}.exe
        working-directory: {work}
        run: python -m PyInstaller {quoted}
      - uses: actions/upload-artifact@v4
        with:
          name: {name}-windows
          path: {up}
""" + (_installer_steps(project, single_file, work) if installer else "")


def _installer_steps(project, single_file, work):
    """Workflow steps for Setup.exe: NSIS (preinstalled on GitHub's Windows machines, or from Chocolatey)
    builds installer/<App>.nsi (written by write_workflow)."""
    name = exe_name(project.name)
    folder = "installer" if work == "." else f"{work}/installer"
    return f"""      - name: Build {name} Setup.exe
        working-directory: {work}
        shell: pwsh
        run: |
          $env:Path += ";${{env:ProgramFiles(x86)}}\\NSIS;${{env:ProgramFiles}}\\NSIS"
          if (-not (Get-Command makensis -ErrorAction SilentlyContinue)) {{ choco install nsis -y --no-progress }}
          makensis installer/{name}.nsi
      - uses: actions/upload-artifact@v4
        with:
          name: {name}-setup
          path: {folder}/*-setup.exe
"""


def write_workflow(project, single_file, installer=False):
    """Writes .github/workflows/pystudio-windows.yml in the git repository (or the project folder) - and, for
    Setup.exe, installer/<App>.nsi (plus the icon) in the project, with paths relative to it."""
    repo = git_root(project.root) or project.root
    folder = os.path.join(repo, ".github", "workflows")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, WORKFLOW)
    app = exe_name(project.name)
    inst = os.path.join(project.root, "installer")
    icon = None
    if installer or icon_source(project):       # the .exe's icon (and the installer's): installer/<App>.ico
        os.makedirs(inst, exist_ok=True)
        icon = make_icon(project, os.path.join(inst, app + ".ico"))
    if installer:
        dist = os.path.join(project.root, "dist")
        payload = os.path.join(dist, app + ".exe") if single_file else os.path.join(dist, app)
        setup = os.path.join(inst, f"{app}-{project['version'] or '1.0.0'}-setup.exe")

        def rel(p):           # makensis runs in installer/ - Windows-style relative paths
            return os.path.relpath(p, inst).replace("/", "\\")
        with open(os.path.join(inst, app + ".nsi"), "w", encoding="utf-8", newline="\r\n") as f:
            f.write(nsis_script(project, payload, setup, icon, rel))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(workflow_text(project, single_file, repo, installer, icon))
    return path, repo != project.root or git_root(project.root) is not None


def gh_status():
    """(ok, message) - is the gh command installed and signed in?"""
    gh = shutil.which("gh")
    if not gh:
        return False, "The gh command isn't installed: Install GitHub CLI, then sign in once in the Terminal:  gh auth login"
    try:
        r = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return (True, "gh is signed in.") if r.returncode == 0 else (False, "gh isn't signed in: run  gh auth login")


# ============================================================ the dialog
class WindowsPublishDialog(QDialog):
    def __init__(self, project, pystudio_python=sys.executable, parent=None):
        super().__init__(parent)
        self.p = project
        self.python = pystudio_python
        self.builder = None
        self.proc = None
        self.result = None
        self.setWindowTitle(f"Publish {project.name} for Windows")
        self.resize(780, 600)
        self.ck_single = QCheckBox("Single file (one .exe - simpler to share, starts a little slower)")
        self.ck_single.setChecked(bool(project["publishSingleFile"]))
        self.ck_setup = QCheckBox("Also make a Setup.exe installer (Start menu, desktop icon, uninstall in Settings > "
                                  "Apps)")
        self.ck_setup.setChecked(bool(makensis_exe()))
        self.b_nsis = QPushButton("Install NSIS")
        self.b_nsis.setToolTip("The installer maker the Wine build uses (the GitHub build doesn't need it here)")
        self.b_nsis.clicked.connect(lambda: self._install(["nsis"]))
        self.b_nsis.setVisible(not makensis_exe())
        tabs = QTabWidget()
        tabs.addTab(self._wine_tab(), "Build here (Wine)")
        tabs.addTab(self._github_tab(), "Build on GitHub")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setStyleSheet("font-family: monospace;")
        self.bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.bb.rejected.connect(self.reject)
        self.b_release = self.bb.addButton("Create GitHub Release...", QDialogButtonBox.ActionRole)
        self.b_release.setToolTip("Put the .exe / Setup.exe on the project's GitHub Releases page")
        self.b_release.clicked.connect(self._github_release)
        self.b_folder = self.bb.addButton("Open Folder", QDialogButtonBox.ActionRole)
        self.b_folder.clicked.connect(self._open_folder)
        self.b_folder.setEnabled(False)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"<b>{project.name}</b> {project['version'] or '1.0.0'} - startup file "
                           f"<b>{startup_file(project)}</b>"))
        hw = QHBoxLayout()
        self.check_label = QLabel()
        self.check_label.setWordWrap(True)
        hw.addWidget(self.check_label, 1)
        self.b_check = QPushButton("Show")
        self.b_check.clicked.connect(self.show_check)
        hw.addWidget(self.b_check)
        v.addLayout(hw)
        self.run_check()
        v.addWidget(self.ck_single)
        hs = QHBoxLayout()
        hs.addWidget(self.ck_setup)
        hs.addWidget(self.b_nsis)
        hs.addStretch()
        v.addLayout(hs)
        v.addWidget(tabs)
        v.addWidget(self.log, 1)
        v.addWidget(self.bb)

    # ------------------------------------------------------------ Windows check
    def run_check(self):
        from . import wincheck
        try:
            self.findings = wincheck.check_project(self.p.root)
        except Exception as e:  # noqa: BLE001
            self.findings = []
            self.check_label.setText(f"Windows check couldn't run: {e}")
            return
        n = sum(1 for f in self.findings if f.level == wincheck.PROBLEM)
        color = "#f14c4c" if n else ("#cca700" if any(f.level == wincheck.WARNING for f in self.findings)
                                     else "#89d185")
        self.check_label.setText(f"Windows check: <b style='color:{color}'>{wincheck.summary(self.findings)}</b>"
                                 + (" - fix these first, or the program fails on Windows." if n else ""))
        self.b_check.setVisible(bool(self.findings))

    def show_check(self):
        from .wincheck import WindowsCheckDialog
        win = self.parent()
        WindowsCheckDialog(self.p.root, self.findings, getattr(win, "open_file_at", None), self).exec()
        self.run_check()

    def _check_ok(self):
        """Before building: problems found -> ask."""
        from . import wincheck
        self.run_check()
        n = sum(1 for f in self.findings if f.level == wincheck.PROBLEM)
        if not n:
            return True
        box = QMessageBox(QMessageBox.Warning, "Windows Check",
                          f"The Windows check found {n} problem{'s' * (n != 1)} that will make the program fail on "
                          "Windows. Build anyway?", parent=self)
        b_show = box.addButton("Show Problems", QMessageBox.ActionRole)
        b_build = box.addButton("Build Anyway", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is b_show:
            self.show_check()
        return box.clickedButton() is b_build

    def _note(self, text):
        q = QLabel(text)
        q.setWordWrap(True)
        q.setTextFormat(Qt.RichText)
        q.setOpenExternalLinks(True)
        return q

    def _wine_status(self):
        wine = wine_exe()
        ready = os.path.exists(win_python_path())
        return ("Wine: <b>" + (wine or "not installed") + "</b> &nbsp; Windows Python: <b>" +
                ("ready" if ready else "not set up yet (done automatically the first time, ~5 min)") + "</b>")

    def _wine_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self._note(
            "Makes the Windows program on this computer. Wine runs the Windows version of Python and PyInstaller "
            "in PyStudio's own Wine folder (your other Wine programs aren't touched). The result goes in "
            "<i>publish/windows</i>: a zip with the program folder, or one .exe."))
        self.wine_status = self._note(self._wine_status())
        v.addWidget(self.wine_status)
        h = QHBoxLayout()
        self.b_build = QPushButton("Build Windows .exe")
        self.b_build.clicked.connect(self.build_wine)
        h.addWidget(self.b_build)
        self.b_wine = QPushButton("Install Wine")
        self.b_wine.clicked.connect(lambda: self._install(["wine"]))
        self.b_wine.setVisible(not wine_exe())
        h.addWidget(self.b_wine)
        h.addStretch()
        v.addLayout(h)
        return w

    def _install(self, keys):
        from .components import InstallDialog
        InstallDialog(keys, self).exec()
        self.wine_status.setText(self._wine_status())
        self.b_wine.setVisible(not wine_exe())
        self.b_nsis.setVisible(not makensis_exe())
        if makensis_exe() and "nsis" in keys:
            self.ck_setup.setChecked(True)
        ok, msg = gh_status()
        self.gh_label.setText(msg)
        self.b_gh.setVisible(not shutil.which("gh"))
        repo = git_root(self.p.root)
        self.b_run.setEnabled(ok and bool(repo))
        self.b_dl.setEnabled(ok and bool(repo))

    def _github_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        repo = git_root(self.p.root)
        ok, msg = gh_status()
        v.addWidget(self._note(
            "GitHub's Windows machines build it - nothing to install here. PyStudio writes a workflow file "
            "(<i>.github/workflows/" + WORKFLOW + "</i>); commit and push it (Git Changes), then run it. "
            "The .exe is under the run's <b>Artifacts</b> on GitHub, or use Download here.<br>Repository: <b>" +
            (repo or "none yet - Git > Create Git Repository, then publish it to GitHub") + "</b>"))
        self.gh_label = self._note(msg)
        v.addWidget(self.gh_label)
        h = QHBoxLayout()
        b = QPushButton("Create / Update Workflow")
        b.clicked.connect(self.make_workflow)
        h.addWidget(b)
        self.b_run = QPushButton("Run on GitHub")
        self.b_run.setEnabled(ok and bool(repo))
        self.b_run.clicked.connect(self.run_github)
        h.addWidget(self.b_run)
        self.b_dl = QPushButton("Download Latest .exe")
        self.b_dl.setEnabled(ok and bool(repo))
        self.b_dl.clicked.connect(self.download_github)
        h.addWidget(self.b_dl)
        self.b_gh = QPushButton("Install GitHub CLI")
        self.b_gh.setToolTip("The gh command - after installing, sign in once in the Terminal:  gh auth login")
        self.b_gh.clicked.connect(lambda: self._install(["gh"]))
        self.b_gh.setVisible(not shutil.which("gh"))
        h.addWidget(self.b_gh)
        h.addStretch()
        v.addLayout(h)
        return w

    def _write(self, text):
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.ensureCursorVisible()

    # ------------------------------------------------------------ wine
    def build_wine(self):
        from .components import ask_install
        if not wine_exe() and not ask_install(["wine"], self, "Building here needs Wine."):
            return
        self.wine_status.setText(self._wine_status())
        if self.ck_setup.isChecked() and not makensis_exe() and \
                not ask_install(["nsis"], self, "The Setup.exe installer needs NSIS."):
            return
        self.b_nsis.setVisible(not makensis_exe())
        if not self._check_ok():
            return
        self.b_build.setEnabled(False)
        self.log.clear()
        self.builder = WineBuilder(self.p, self.python, self.ck_single.isChecked(), self.ck_setup.isChecked(), self)
        self.builder.log.connect(self._write)
        self.builder.finished.connect(self._wine_done)
        self.builder.start()

    def _wine_done(self, ok, msg):
        self.b_build.setEnabled(True)
        if ok:
            self.result = msg
            self.b_folder.setEnabled(True)

    # ------------------------------------------------------------ github
    def make_workflow(self):
        path, in_repo = write_workflow(self.p, self.ck_single.isChecked(), self.ck_setup.isChecked())
        self._write(f"Wrote {path}\n" + ("Commit and push it (Git Changes: stage, commit, Push), then Run on "
                                         "GitHub.\n" if in_repo else
                                         "This folder isn't a git repository yet: Git > Create Git Repository, "
                                         "publish it to GitHub, then push.\n"))
        self.result = path
        self.b_folder.setEnabled(True)

    def _gh(self, args, then=None):
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setWorkingDirectory(self.p.root)
        self.proc.readyReadStandardOutput.connect(
            lambda: self._write(bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")))
        self.proc.finished.connect(lambda code, _s: then(code) if then else None)
        self._write("> gh " + " ".join(args) + "\n")
        self.proc.start(shutil.which("gh") or "gh", args)

    def run_github(self):
        if not self._check_ok():
            return
        self._gh(["workflow", "run", WORKFLOW], lambda c: self._write(
            "Started. Watch it on GitHub (Actions tab) - it takes about 5 minutes; then Download Latest .exe.\n"
            if c == 0 else "Couldn't start it - is the workflow committed and pushed to GitHub?\n"))

    def download_github(self):
        name = exe_name(self.p.name) + "-windows"
        dest = os.path.join(self.p.root, "publish", "windows-github")
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)

        def done(code):
            if code == 0:
                self._write(f"Downloaded to {dest}\n")
                self.result = dest
                self.b_folder.setEnabled(True)
            else:
                self._write("Nothing to download yet - has a run finished successfully? (GitHub > Actions)\n")
        self._gh(["run", "download", "-n", name, "-D", dest], done)

    def _github_release(self):
        from . import ghrelease
        ghrelease.create_release(self.p, self)

    def _open_folder(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        target = self.result if self.result and os.path.isdir(self.result) else os.path.dirname(self.result or "")
        if target:
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def reject(self):
        if self.builder is not None and self.builder.proc is not None and \
                self.builder.proc.state() != QProcess.NotRunning:
            if QMessageBox.question(self, "Publish for Windows", "Stop the build?") != QMessageBox.Yes:
                return
            self.builder.cancel()
        super().reject()
