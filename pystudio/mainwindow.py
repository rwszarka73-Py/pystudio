"""Main IDE window: menus, toolbar, Solution Explorer, editor tabs, Output."""
import glob
import os
import sys

from PySide6.QtCore import QDir, QEvent, QFile, QObject, QSettings, QSortFilterProxyModel, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont, QIcon, QKeySequence, QTextCursor
from PySide6.QtWidgets import (QApplication, QDialog, QDockWidget, QFileDialog, QFileSystemModel, QHBoxLayout, QToolButton,
                               QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox,
                               QStyle, QTabWidget, QTreeView, QVBoxLayout, QWidget)

from . import __version__
import re

from . import model as M
from .codegen import add_event_handler, ensure_form_base, ensure_runtime_call
from .designer import FormDesigner, Toolbox, UnsupportedForm
from .project import Project, ProjectPropertiesDialog
import json

from .debugger import CallStackWindow, DebugSession, LocalsWindow, ThreadsWindow, WatchWindow
from .language import LanguageService, check_source
from .options import OptionsDialog, startup_mode
from .panels import ErrorList, FindResults
from .properties import PropertiesPanel
from .startwindow import StartWindow, add_recent, load_recent, too_big_to_open
from .editor import CodeEditor
from .findbar import FindBar
from .newproject import NewProjectDialog
from .output import OutputPanel, Runner
from .qttools import DesignerBridge, compile_all_stale, compile_ui
from .templates import FORM_PY, add_code_file, add_form, create_project, next_item_name
from . import dbcore, mygen
from .dataservice import DataService
from .querydesigner import QueryDocument
from .gitpanel import DiffView, GitChangesPanel, GitService
from . import findinfiles, formatter
from . import exceptionsettings
from .immediate import ImmediateWindow
from .profiler import ProfileReport, load_stats
from .testexplorer import SHIM as PYTEST_SHIM, TestExplorer, new_test_file
from .language import is_generated as is_generated_path
from .serverexplorer import ServerExplorer
from .navigation import NavigationMixin
from .assistant import AssistantMixin
from . import solution as sln
from .solution import Solution
from . import interp, projectfit
from . import icons
from .options import hot_reload_enabled

HIDDEN_NAMES = {"__pycache__", ".git", ".venv", "venv", ".idea", ".mypy_cache", ".pytest_cache", ".ipynb_checkpoints"}
ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "resources", "pystudio.svg")



GENERATED_FILES = {"pystudio_runtime.py", "pyproject.toml", "publish", "my.py"}


class HideJunkProxy(QSortFilterProxyModel):
    """Hides __pycache__, .git, virtual envs - and, unless Show All Files is on, the
    generated files (ui_*.py, pystudio_runtime.py, *.pyproj), like VS hides Designer.vb."""

    show_all = False
    solution = None
    solution_root = None
    startup = None
    top = None                 # the solution / project folder: the tree's one top node (VS's project node)
    top_title = ""
    top_tip = ""
    top_is_solution = False
    _projects = frozenset()

    def set_top(self, path, title, tip="", is_solution=False):
        self.top, self.top_title, self.top_tip, self.top_is_solution = path, title, tip, is_solution
        self.invalidateFilter()

    def set_solution(self, solution, root, startup):
        self.solution, self.solution_root, self.startup = solution, root, startup
        self._projects = frozenset(solution.projects()) if solution else frozenset()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        idx = self.sourceModel().index(row, 0, parent)
        if self.top and os.path.abspath(self.sourceModel().filePath(parent)) == os.path.dirname(self.top):
            return os.path.abspath(self.sourceModel().filePath(idx)) == self.top   # only the project node
        name = self.sourceModel().fileName(idx)
        if name in HIDDEN_NAMES:
            return False
        if not self.show_all and (name in GENERATED_FILES or name.endswith((".pyproj", ".pysln"))
                                  or (name.startswith("ui_") and name.endswith(".py"))):
            return False
        if self.solution is not None and not self.show_all and self.sourceModel().isDir(idx):
            path = os.path.abspath(self.sourceModel().filePath(idx))
            if path not in self._projects and glob.glob(os.path.join(path, "*.pyproj")):
                return False             # a project removed from the solution
        return True

    def data(self, index, role=Qt.DisplayRole):
        if self.top and index.column() == 0 and role in (Qt.DisplayRole, Qt.FontRole, Qt.DecorationRole,
                                                         Qt.ToolTipRole) \
                and os.path.abspath(self.sourceModel().filePath(self.mapToSource(index))) == self.top:
            if role == Qt.DisplayRole:
                return self.top_title
            if role == Qt.ToolTipRole:
                return self.top_tip
            if role == Qt.DecorationRole:
                from . import icons
                return icons.solution() if self.top_is_solution else icons.project()
            f = QFont(super().data(index, Qt.FontRole) or QFont())
            f.setBold(True)
            return f
        if self._projects and role in (Qt.FontRole, Qt.DecorationRole, Qt.ToolTipRole) and index.column() == 0:
            path = os.path.abspath(self.sourceModel().filePath(self.mapToSource(index)))
            if path in self._projects:
                if role == Qt.DecorationRole:
                    from . import icons
                    return icons.project()
                if role == Qt.ToolTipRole:
                    return "Startup project (F5 runs it)" if path == self.startup else \
                        "Project - right-click > Set as Startup Project"
                f = QFont(super().data(index, Qt.FontRole) or QFont())
                f.setBold(path == self.startup)
                return f
        return super().data(index, role)


LAYOUT_VERSION = 2       # bump when the default dock layout changes (see restoreState)


class _FitToParent(QObject):
    """Keeps an overlay (the Start page) sized to the widget it covers."""

    def __init__(self, overlay):
        super().__init__(overlay)
        self.overlay = overlay

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize:
            self.overlay.setGeometry(obj.rect())
        return False


class MainWindow(AssistantMixin, NavigationMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings()
        self.project_root = None      # the active project (where Add Form, Properties, tests... go)
        self.project = None
        self.solution = None          # Solution when a .pysln is open (several projects)
        self.solution_root = None     # the folder shown in Solution Explorer
        self._start_page = self._start_fit = None
        self.python = self.settings.value("python", sys.executable)

        self.setWindowTitle("PyStudio")
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(1400, 850)

        self._build_center()
        self._build_solution_explorer()
        self._build_output()
        self._build_design_docks()
        self._build_server_explorer()
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_statusbar()

        # every panel can be made small: drag the edge between it and the editor (the bottom panel down to a
        # few lines - Test Explorer's buttons alone would otherwise hold it at 184 px)
        for dk in self.findChildren(QDockWidget):
            if dk.widget() is not None:
                dk.widget().setMinimumHeight(40)
        self._default_state = self.saveState()     # Window > Reset Window Layout
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        state = self.settings.value("windowState")
        if state:
            # LAYOUT_VERSION changes when the default layout does: older saved layouts are
            # ignored once (v1.5.1: debug windows became tabs instead of a side-by-side split)
            self.restoreState(state, LAYOUT_VERSION)

        # What opens at startup is decided by main.py (Start Window / last project / empty)
        self._update_title()
        self._update_run_state()

    # ================================================================ layout
    def _build_center(self):
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.findbar = FindBar(self.current_editor)
        self.findbar.closed.connect(lambda: self.current_editor() and self.current_editor().setFocus())

        self.welcome = QLabel(
            "<div style='color:#888; font-size:15px;'>"
            "<p style='font-size:22px; color:#ccc;'>PyStudio</p>"
            "<p>File &gt; New Project (Ctrl+Shift+N) to start something new</p>"
            "<p>File &gt; Open Folder (Ctrl+K, Ctrl+O) to open an existing project</p>"
            "<p>F5 runs the project</p></div>")
        self.welcome.setAlignment(Qt.AlignCenter)

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.findbar)
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.welcome, 1)
        self.setCentralWidget(center)

    def _build_solution_explorer(self):
        self.fs_model = QFileSystemModel()
        self.fs_model.setReadOnly(False)
        self.fs_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.Hidden)
        self.proxy = HideJunkProxy()
        self.proxy.setSourceModel(self.fs_model)

        self.tree = QTreeView()
        self.tree.setHeaderHidden(True)
        self.tree.setSortingEnabled(True)
        self.tree.doubleClicked.connect(self._on_tree_double_click)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_context_menu)

        # Project header (VS shows the project node with a Properties button)
        self.proj_label = QLabel("  (no project)")
        self.proj_label.setTextFormat(Qt.RichText)
        self.proj_label.setStyleSheet("font-weight: bold; padding: 3px;")
        from PySide6.QtWidgets import QToolButton

        def tb(text, tip, slot, checkable=False):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setCheckable(checkable)
            b.clicked.connect(slot)
            return b
        self.b_props = tb("⚙", "Project Properties (Alt+Enter)", lambda: self.project_properties())
        self.b_showall = tb("👁", "Show All Files (including generated ones)", self.toggle_show_all, True)
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(4, 2, 4, 2)
        hl.addWidget(self.proj_label, 1)
        self._head_stretch = QWidget()          # keeps the buttons right-aligned when the label is hidden
        hl.addWidget(self._head_stretch, 1)
        self._head_stretch.hide()
        hl.addWidget(self.b_showall)
        hl.addWidget(self.b_props)
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        bl.addWidget(head)
        bl.addWidget(self.tree, 1)

        self.explorer_dock = QDockWidget("Solution Explorer", self)
        self.explorer_dock.setObjectName("SolutionExplorer")
        self.explorer_dock.setWidget(box)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.explorer_dock)

    def _build_output(self):
        self.output = OutputPanel()
        self.output.location_activated.connect(self.open_file_at)
        self.runner = Runner(self.output, self)
        self.runner.started.connect(self._update_run_state)
        self.runner.finished.connect(lambda _c: self._update_run_state())
        self.runner.finished.connect(self._check_run_output_for_missing)
        self.qt_designer = DesignerBridge(self)
        self.qt_designer.message.connect(self.output.write)

        self.output_dock = QDockWidget("Output", self)
        self.output_dock.setObjectName("Output")
        self.output_dock.setWidget(self.output)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.output_dock)

        # IntelliSense service + Error List / Find Results (tabbed with Output, like VS)
        self.lang = LanguageService(self)
        self.error_list = ErrorList()
        self.error_list.activated.connect(self.open_file_at)
        self.error_list.counts_changed.connect(self._update_error_title)
        self.errors_dock = QDockWidget("Error List", self)
        self.errors_dock.setObjectName("ErrorList")
        self.errors_dock.setWidget(self.error_list)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.errors_dock)
        self.find_results = FindResults()
        self.find_results.activated.connect(self.open_file_at)
        self.find_dock = QDockWidget("Find Results", self)
        self.find_dock.setObjectName("FindResults")
        self.find_dock.setWidget(self.find_results)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.find_dock)
        from .callhierarchy import CallHierarchyWindow
        self.call_hierarchy = CallHierarchyWindow(None, lambda: self._lang_context())
        self.call_hierarchy.open_location.connect(self.open_file_at)
        self.calls_dock = QDockWidget("Call Hierarchy", self)
        self.calls_dock.setObjectName("CallHierarchy")
        self.calls_dock.setWidget(self.call_hierarchy)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.calls_dock)

        # Debugger: Locals/Watch (bottom left) and Call Stack (with Output), like VS
        self.session = DebugSession(self.output, self)
        self.session.stopped.connect(self._on_debug_stopped)
        self.session.continued.connect(self._on_debug_continued)
        self.session.exited.connect(self._on_debug_exited)
        self.session.trace_output.connect(lambda t: self.output.write(t + "\n", "info"))
        self.session.attach_failed.connect(lambda msg: QMessageBox.warning(self, "Attach to Process", msg))
        self.current_frame = 0
        self.locals_win = LocalsWindow(self.session)
        self.watch_win = WatchWindow(self.session)
        self.stack_win = CallStackWindow()
        self.stack_win.frame_selected.connect(self._on_frame_selected)
        self.threads_win = ThreadsWindow()
        self.threads_win.thread_selected.connect(self._on_thread_selected)
        self.debug_docks = []
        for title, name, widget in (("Locals", "Locals", self.locals_win), ("Watch 1", "Watch", self.watch_win),
                                    ("Call Stack", "CallStack", self.stack_win), ("Threads", "Threads",
                                                                                   self.threads_win)):
            dock = QDockWidget(title, self)
            dock.setObjectName(name)
            dock.setWidget(widget)
            self.addDockWidget(Qt.BottomDockWidgetArea, dock)
            self.debug_docks.append(dock)
        self.locals_dock, self.watch_dock, self.stack_dock, self.threads_dock = self.debug_docks
        # Split first (Qt turns a split into a tab once a dock is tabified), then tab
        # All bottom windows are tabs of one group (drag a tab out to float or dock it elsewhere -
        # PyStudio remembers where you put it)
        self.tabifyDockWidget(self.output_dock, self.errors_dock)
        self.tabifyDockWidget(self.errors_dock, self.find_dock)
        self.tabifyDockWidget(self.find_dock, self.calls_dock)
        self.calls_dock.hide()
        self.tabifyDockWidget(self.find_dock, self.locals_dock)
        self.tabifyDockWidget(self.locals_dock, self.watch_dock)
        self.tabifyDockWidget(self.watch_dock, self.stack_dock)
        self.tabifyDockWidget(self.stack_dock, self.threads_dock)
        self.threads_dock.toggleViewAction().setShortcut("Ctrl+Alt+H")
        # Diagnostic Tools: on the right, like VS; shown while debugging
        from .diagtools import DiagnosticTools
        self.diag_tools = DiagnosticTools(self.session, self.runner)
        self.diag_dock = QDockWidget("Diagnostic Tools", self)
        self.diag_dock.setObjectName("DiagnosticTools")
        self.diag_dock.setWidget(self.diag_tools)
        self.addDockWidget(Qt.RightDockWidgetArea, self.diag_dock)
        self.diag_dock.toggleViewAction().setShortcut("Ctrl+Alt+F2")
        self.debug_docks.append(self.diag_dock)
        self.session.exited.connect(lambda _c: self.diag_tools.stop())
        self.runner.finished.connect(lambda _c: self.diag_tools.stop())
        self.runner.started.connect(lambda: self.diag_tools.start(
            None if getattr(self.runner, "_remote", None) is not None else self.runner.proc.processId()))
        for dk in self.debug_docks:
            dk.hide()
        # v1.4: Immediate / Python Interactive and Test Explorer, also tabs with Output
        self.immediate = ImmediateWindow()
        self.immediate.session = self.session
        self.immediate.frame = lambda: self.current_frame
        self.immediate.on_changed = lambda: (self.locals_win.load(self.current_frame),
                                             self.watch_win.refresh(self.current_frame))
        self.immediate_dock = QDockWidget("Immediate", self)
        self.immediate_dock.setObjectName("Immediate")
        self.immediate_dock.setWidget(self.immediate)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.immediate_dock)
        self.tabifyDockWidget(self.stack_dock, self.immediate_dock)
        self.tests = TestExplorer()
        self.tests.open_location.connect(self.open_file_at)
        self.tests.log.connect(self.output.write)
        self.tests.debug_requested.connect(self.debug_tests)
        self.tests.coverage_ready.connect(self._on_coverage)
        self.tests.install_coverage_requested.connect(lambda: self.install_pytest("coverage"))
        self.tests.install_requested.connect(self.install_pytest)
        self.tests_dock = QDockWidget("Test Explorer", self)
        self.tests_dock.setObjectName("TestExplorer")
        self.tests_dock.setWidget(self.tests)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.tests_dock)
        self.tabifyDockWidget(self.immediate_dock, self.tests_dock)
        self._terminal_init()
        from .testexplorer import CoverageResults
        self.coverage = {}                 # {abs path: {"covered", "missing", "percent"}}
        self.coverage_win = CoverageResults()
        self.coverage_win.open_location.connect(self.open_file_at)
        self.coverage_win.clear_requested.connect(self.clear_coverage)
        self.coverage_dock = QDockWidget("Code Coverage Results", self)
        self.coverage_dock.setObjectName("CodeCoverage")
        self.coverage_dock.setWidget(self.coverage_win)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.coverage_dock)
        self.tabifyDockWidget(self.tests_dock, self.coverage_dock)
        self.coverage_dock.hide()
        self._nav_init()
        self._tasklist_init()
        es = exceptionsettings.load()
        self.session.set_exception_settings(es["types"], es["all"])
        self.profile_proc = None
        try:
            self.breakpoints = json.loads(self.settings.value("breakpoints", "{}") or "{}")
        except ValueError:
            self.breakpoints = {}
        self.output_dock.raise_()
        self.resizeDocks([self.output_dock], [220], Qt.Vertical)
        self.resizeDocks([self.explorer_dock], [260], Qt.Horizontal)

    def _build_design_docks(self):
        """Toolbox (left, under Solution Explorer) and Properties (right), like VS."""
        self.toolbox = Toolbox()
        self.toolbox_dock = QDockWidget("Toolbox", self)
        self.toolbox_dock.setObjectName("Toolbox")
        self.toolbox_dock.setWidget(self.toolbox)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.toolbox_dock)
        self.splitDockWidget(self.explorer_dock, self.toolbox_dock, Qt.Vertical)

        self.props = PropertiesPanel()
        self.props.handler_lookup = self._handlers_for
        self.props.event_requested.connect(
            lambda ctrl, sig, arg: self.create_event_handler(self.current_designer(), ctrl, (sig, arg)))
        self.props.set_designer(None)
        self.props_dock = QDockWidget("Properties", self)
        self.props_dock.setObjectName("Properties")
        self.props_dock.setWidget(self.props)
        self.addDockWidget(Qt.RightDockWidgetArea, self.props_dock)
        self.resizeDocks([self.props_dock], [300], Qt.Horizontal)
        self._assistant_init()

    def _build_server_explorer(self):
        """Server Explorer (data connections), tabbed with Solution Explorer like VS."""
        self.data = DataService(self)
        self.server = ServerExplorer(self.data)
        self.server.open_query.connect(self.new_query)
        self.server.add_query.connect(self.add_query_file)
        self.server.connections_changed.connect(self._on_connections_changed)
        self.server_dock = QDockWidget("Server Explorer", self)
        self.server_dock.setObjectName("ServerExplorer")
        self.server_dock.setWidget(self.server)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.server_dock)
        self.tabifyDockWidget(self.explorer_dock, self.server_dock)
        from .classview import ClassView
        self.class_view = ClassView()
        self.class_view.open_location.connect(self.open_file_at)
        self.class_dock = QDockWidget("Class View", self)
        self.class_dock.setObjectName("ClassView")
        self.class_dock.setWidget(self.class_view)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.class_dock)
        self.tabifyDockWidget(self.server_dock, self.class_dock)
        self.class_dock.hide()

        # Git Changes (VS: View > Git Changes), also a tab there
        self.git = GitService(self)
        self.git.log.connect(lambda t: self.output.write(t, "info"))
        self.git_panel = GitChangesPanel(self.git)
        self.git_dock = QDockWidget("Git Changes", self)
        self.git_dock.setObjectName("GitChanges")
        self.git_dock.setWidget(self.git_panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.git_dock)
        self.tabifyDockWidget(self.server_dock, self.git_dock)
        self.git_panel.open_diff.connect(lambda title, text: self._add_editor(DiffView(title, text)))
        self.git_panel.open_file.connect(self.open_file)
        self.git_panel.files_changed.connect(self.reload_changed_files)
        self.git_panel.open_merge.connect(self.open_merge_editor)
        from .gittools import PullRequestsWindow
        self.prs = PullRequestsWindow(self.git)
        self.prs.open_diff.connect(lambda title, text: self._add_editor(DiffView(title, text)))
        self.prs.files_changed.connect(lambda: (self.reload_changed_files(), self.git_panel.refresh()))
        self.prs.log.connect(lambda t: self.output.write(t, "info"))
        self.prs_dock = QDockWidget("GitHub Pull Requests", self)
        self.prs_dock.setObjectName("PullRequests")
        self.prs_dock.setWidget(self.prs)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.prs_dock)
        self.prs_dock.hide()
        from .ghissues import IssuesWindow
        self.issues = IssuesWindow(self.git)
        self.issues.log.connect(lambda t: self.output.write(t, "info"))
        self.issues.fixes.connect(self._fixes_in_commit)
        self.issues_dock = QDockWidget("GitHub Issues", self)
        self.issues_dock.setObjectName("Issues")
        self.issues_dock.setWidget(self.issues)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.issues_dock)
        self.issues_dock.hide()
        self._blame_cache = {}             # editor id -> (document revision, [entries])
        self._blame_timer = QTimer(self, singleShot=True, interval=350)
        self._blame_timer.timeout.connect(self._update_inline_blame)
        self.git_status = QToolButton()
        self.git_status.setAutoRaise(True)
        self.git_status.setToolTip("Git: branch, commits to push/pull, changed files - click for Git Changes")
        self.git_status.clicked.connect(self.show_git_changes)
        self.git_status.hide()
        self.statusBar().insertPermanentWidget(0, self.git_status)
        self.git_panel.status_changed.connect(lambda t: (self.git_status.setText(t), self.git_status.setVisible(bool(t))))
        self.explorer_dock.raise_()

    def _act(self, text, slot, shortcut=None, icon=None):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if icon is not None:
            a.setIcon(self.style().standardIcon(icon))
        a.triggered.connect(slot)
        return a

    def _build_actions(self):
        S = QStyle
        self.a_new_project = self._act("New &Project...", self.new_project, "Ctrl+Shift+N")
        self.a_new_project.setIcon(icons.new_project())
        self.a_new_file = self._act("&New File", self.new_file, "Ctrl+N")
        self.a_new_file.setIcon(icons.new_file())
        self.a_open_file = self._act("&Open File...", self.open_file_dialog, "Ctrl+O")
        self.a_open_file.setIcon(icons.open_folder())
        self.a_open_folder = self._act("Open &Folder...", self.open_folder_dialog, "Ctrl+K, Ctrl+O")
        self.a_open_remote = self._act("Open &Remote Folder (SSH)...", self.open_remote_folder)
        self.a_open_folder.setIcon(icons.open_folder())
        self.a_save = self._act("&Save", self.save_current, "Ctrl+S")
        self.a_save.setIcon(icons.save())
        self.a_save_as = self._act("Save &As...", self.save_current_as, "Ctrl+Shift+Alt+S")
        self.a_save_all = self._act("Save A&ll", self.save_all, "Ctrl+Shift+S")
        self.a_save_all.setIcon(icons.save_all())
        self.a_close_tab = self._act("&Close", lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W")
        self.a_close_folder = self._act("Close Fol&der", self.close_folder)
        self.a_close_solution = self._act("Close Solu&tion", self.close_solution)
        self.a_exit = self._act("E&xit", self.close, "Alt+F4")

        # Clipboard/undo go to whichever box has focus (editor, Output, Find bar)
        self.a_undo = self._act("&Undo", lambda: self._focus_call("undo"), "Ctrl+Z")
        self.a_redo = self._act("&Redo", lambda: self._focus_call("redo"), "Ctrl+Y")
        self.a_cut = self._act("Cu&t", lambda: self._focus_call("cut"), "Ctrl+X")
        self.a_copy = self._act("&Copy", lambda: self._focus_call("copy"), "Ctrl+C")
        self.a_paste = self._act("&Paste", lambda: self._focus_call("paste"), "Ctrl+V")
        self.a_select_all = self._act("Select &All", lambda: self._focus_call("selectAll"), "Ctrl+A")
        self.a_find = self._act("&Find...", lambda: self.findbar.show_find(False), "Ctrl+F")
        self.a_replace = self._act("&Replace...", lambda: self.findbar.show_find(True), "Ctrl+H")
        self.a_goto = self._act("&Go To Line...", self.go_to_line, "Ctrl+G")
        self._nav_actions()
        self._assistant_actions()
        self.a_task_list = self._act("Tas&k List", self.show_task_list)
        self.a_task_list.setShortcuts([QKeySequence("Ctrl+\\, T"), QKeySequence("Ctrl+Alt+K")])
        self.a_comment = self._act("Toggle &Comment", lambda: self._ed_call("toggle_comment"), "Ctrl+/")
        self.a_goto_def = self._act("Go To &Definition", self.go_to_definition, "F12")
        self.a_find_refs = self._act("Find All &References", self.find_references, "Shift+F12")
        self.a_call_hierarchy = self._act("View Call &Hierarchy", self.view_call_hierarchy, "Ctrl+K, Ctrl+T")
        self.a_quickfix = self._act("&Quick Actions and Refactorings...", lambda: self._ed_call("show_quick_fixes"), "Ctrl+.")
        self.a_rename = self._act("Re&name...", self.rename_symbol, "F2")
        self.a_complete = self._act("Complete &Word", lambda: self._ed_call("request_completion_explicit"), "Ctrl+Space")
        self.a_paraminfo = self._act("&Parameter Info", lambda: self._ed_call("request_signature"), "Ctrl+Shift+Space")
        self.a_fold = self._act("&Toggle Outlining Expansion", lambda: self._ed_call("toggle_fold"), "Ctrl+M, Ctrl+M")
        self.a_fold_all = self._act("&Collapse to Definitions", lambda: self._ed_call("fold_all"), "Ctrl+M, Ctrl+O")
        self.a_unfold_all = self._act("&Expand All", lambda: self._ed_call("unfold_all"), "Ctrl+M, Ctrl+L")
        self.a_view_code = self._act("&Code", self.view_code, "F7")
        self.a_view_designer = self._act("&Designer", self.view_designer, "Shift+F7")
        self.a_add_form = self._act("Add Windows &Form...", lambda: self.add_form_dialog(self.project_root))
        self.a_find_files = self._act("Find in &Files...", lambda: self.find_in_files(False), "Ctrl+Shift+F")
        self.a_replace_files = self._act("Replace in Fi&les...", lambda: self.find_in_files(True), "Ctrl+Shift+H")
        self.a_format_doc = self._act("Format &Document", self.format_document, "Ctrl+K, Ctrl+D")
        self.a_format_sel = self._act("Format &Selection", lambda: self.format_document(selection=True), "Ctrl+K, Ctrl+F")
        self.a_remove_unused = self._act("Remove &Unused Imports", self.remove_unused_imports)
        self.a_extract_method = self._act("Extract &Method...", lambda: self.extract("function"), "Ctrl+R, Ctrl+M")
        self.a_extract_var = self._act("Extract &Variable...", lambda: self.extract("variable"), "Ctrl+R, Ctrl+V")
        self.a_caret_next = self._act("Insert &Next Matching Caret", lambda: self._ed_call("add_next_occurrence"), "Shift+Alt+.")
        self.a_caret_all = self._act("Insert Carets at &All Matching", lambda: self._ed_call("add_all_occurrences"), "Shift+Alt+;")
        self.a_caret_up = self._act("Add Caret &Above", lambda: self._ed_call("add_caret_vertical", False), "Shift+Alt+Up")
        self.a_caret_down = self._act("Add Caret &Below", lambda: self._ed_call("add_caret_vertical", True), "Shift+Alt+Down")
        self.a_git_changes = self._act("&Git Changes", self.show_git_changes, "Ctrl+0, Ctrl+G")
        self.a_immediate = self._act("&Immediate", self.show_immediate, "Ctrl+Alt+I")
        self.a_exceptions = self._act("E&xception Settings...", self.exception_settings, "Ctrl+Alt+E")
        self.a_profile = self._act("Performance &Profiler", self.profile_project, "Alt+F2")
        self.a_test_explorer = self._act("Test E&xplorer", self.show_tests, "Ctrl+E, T")
        self.a_run_tests = self._act("&Run All Tests", lambda: (self.show_tests(), self.tests.run_all()), "Ctrl+R, A")
        self.a_run_sel_tests = self._act("Run &Selected Tests", lambda: (self.show_tests(), self.tests.run_selected()), "Ctrl+R, T")
        self.a_debug_tests = self._act("&Debug All Tests", lambda: self.debug_tests(list(self.tests.nodes)), "Ctrl+R, Ctrl+A")
        self.a_add_test = self._act("Add Unit &Test...", self.add_unit_test)
        self.a_help = self._act("&Help on Word Under Cursor", self.show_help, "F1")
        self.a_help.setToolTip("F1: the documentation for the word at the cursor, or the selected control")
        self.a_add_dialog = self._act("Add &Dialog...", lambda: self.add_item_dialog("Dialog"))
        self.a_add_uc = self._act("Add &User Control...", lambda: self.add_item_dialog("User Control"))
        self.a_add_class = self._act("Add &Class...", lambda: self.add_item_dialog("Class"))
        self.a_add_module = self._act("Add &Module...", lambda: self.add_item_dialog("Module"))
        self.a_add_query = self._act("Add New &Query...", lambda: self.add_query_file(), "Ctrl+Shift+Q")
        self.a_add_report = self._act("Add &Report...", self.add_report)
        self.a_add_notebook = self._act("Add New &Notebook...", lambda: self.add_notebook())
        self.a_settings = self._act("&Settings...", lambda: self.project_properties("Settings"))
        self.a_resources = self._act("&Resources...", lambda: self.project_properties("Resources"))
        self.a_packages = self._act("Manage &Packages...", lambda: self.project_properties("Packages"),
                                    "Ctrl+Alt+P")

        self.a_start_window = self._act("Start &Window...", self.show_start_window)
        self.a_tab_order = self._act("Tab &Order", self.toggle_tab_order)
        self.a_tab_order.setCheckable(True)
        self.a_tab_order.setEnabled(False)
        self.a_publish = self._act("&Publish...", self.publish_project)
        self.a_rebuild = self._act("&Rebuild Forms", self.rebuild_forms, "Ctrl+Shift+B")
        self.a_options = self._act("&Options...", self.show_options)
        self.a_run = self._act("&Start Debugging", self.f5, "F5", S.SP_MediaPlay)
        self.a_run_nodebug = self._act("Start &Without Debugging", self.run_current, "Ctrl+F5")
        self.a_stop = self._act("Sto&p Debugging", self.stop_all, "Shift+F5", S.SP_MediaStop)
        self.a_restart = self._act("&Restart", self.restart_debugging, "Ctrl+Shift+F5")
        self.a_restart.setIcon(icons.refresh())
        self.a_break = self._act("Brea&k All", self.session.pause, "Ctrl+Alt+Pause", S.SP_MediaPause)
        self.a_step_into = self._act("Step &Into", lambda: self.step("step"), "F11", S.SP_ArrowDown)
        self.a_step_over = self._act("Step &Over", lambda: self.step("next"), "F10", S.SP_ArrowForward)
        self.a_step_out = self._act("Step O&ut", lambda: self.step("return"), "Shift+F11", S.SP_ArrowUp)
        self.a_toggle_bp = self._act("Toggle &Breakpoint", lambda: self._ed_call("toggle_breakpoint"), "F9")
        self.a_run_to_cursor = self._act("Run To C&ursor", self.run_to_cursor, "Ctrl+F10")
        self.a_set_next = self._act("Set Ne&xt Statement", lambda: self.set_next_statement(), "Ctrl+Shift+F10")
        self.a_set_next.setEnabled(False)
        self.a_clear_bps = self._act("&Delete All Breakpoints", self.delete_all_breakpoints, "Ctrl+Shift+F9")
        self.a_add_watch = self._act("Add &Watch", self.add_watch)
        self.a_interp = self._act("Select Python &Interpreter...", self.select_interpreter)

    def _build_menus(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        self.a_open_project = self._act("Open Pro&ject...", self.open_project_dialog, "Ctrl+Shift+O")
        for a in (self.a_new_project, self.a_new_file, self.a_add_notebook, None, self.a_open_project,
                  self.a_open_file, self.a_open_folder, self.a_open_remote):
            m.addSeparator() if a is None else m.addAction(a)
        self.a_disconnect_remote = self._act("Disconnect Remote Folder", self.disconnect_remote)
        m.addAction(self.a_disconnect_remote)
        m.aboutToShow.connect(lambda: self.a_disconnect_remote.setVisible(
            __import__("pystudio.remote", fromlist=["x"]).remote_for(self.project_root) is not None))
        m.addAction(self.a_start_window)
        add = m.addMenu("A&dd")
        add.addAction(self._act("&New Project...", self.add_new_project_to_solution))
        add.addAction(self._act("&Existing Project...", self.add_existing_project))
        m.addAction(self._act("&Import VB.NET or C# Project...", self.import_vb_project))
        self.recent_menu = m.addMenu("Recent &Projects")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        for a in (None, self.a_save, self.a_save_as, self.a_save_all, None,
                  self.a_close_tab, self.a_close_solution, None, self.a_exit):
            m.addSeparator() if a is None else m.addAction(a)

        m = mb.addMenu("&Edit")
        for a in (self.a_undo, self.a_redo, None, self.a_cut, self.a_copy, self.a_paste,
                  self.a_select_all, None, self.a_find, self.a_replace, self.a_goto, self.a_goto_all, None,
                  self.a_comment):
            m.addSeparator() if a is None else m.addAction(a)
        m.addSeparator()
        m.addAction(self.a_nav_back)
        m.addAction(self.a_nav_fwd)
        sub = m.addMenu("&Bookmarks")
        for a in (self.a_bm_toggle, self.a_bm_prev, self.a_bm_next, None, self.a_bm_clear, None, self.a_bm_window):
            sub.addSeparator() if a is None else sub.addAction(a)
        m.addSeparator()
        sub = m.addMenu("&IntelliSense")
        for a in (self.a_complete, self.a_paraminfo, self.a_suggest):
            sub.addAction(a)
        sub = m.addMenu("&Outlining")
        for a in (self.a_fold, self.a_fold_all, self.a_unfold_all):
            sub.addAction(a)
        sub = m.addMenu("Find and Re&place")
        for a in (self.a_find, self.a_replace, None, self.a_find_files, self.a_replace_files):
            sub.addSeparator() if a is None else sub.addAction(a)
        sub = m.addMenu("Ad&vanced")
        for a in (self.a_format_doc, self.a_format_sel, self.a_remove_unused):
            sub.addAction(a)
        sub = m.addMenu("&Multiple Carets")
        for a in (self.a_caret_next, self.a_caret_all, None, self.a_caret_up, self.a_caret_down):
            sub.addSeparator() if a is None else sub.addAction(a)
        sub.addSeparator()
        hint = sub.addAction("Ctrl+Alt+Click: add a caret   Shift+Alt+drag: box selection   Esc: one caret")
        hint.setEnabled(False)
        m.addSeparator()
        m.addAction(self.a_quickfix)
        sub = m.addMenu("&Refactor")
        for a in (self.a_rename, self.a_extract_method, self.a_extract_var):
            sub.addAction(a)
        m.addAction(self.a_rename)
        m.addAction(self.a_goto_def)
        m.addAction(self.a_find_refs)
        m.addAction(self.a_call_hierarchy)

        m = mb.addMenu("&View")
        m.addAction(self.a_view_code)
        m.addAction(self.a_view_designer)
        m.addAction(self.a_tab_order)
        m.addSeparator()
        a = self.explorer_dock.toggleViewAction()
        a.setShortcut("Ctrl+Alt+L")
        m.addAction(a)
        a = self.server_dock.toggleViewAction()
        a.setShortcut("Ctrl+Alt+S")
        m.addAction(a)
        m.addAction(self.a_git_changes)
        m.addAction(self.a_test_explorer)
        m.addAction(self.a_bm_window)
        m.addAction(self.a_task_list)
        m.addAction(self.a_terminal)
        self.a_class_view = self._act("Cla&ss View", self.show_class_view)
        self.a_class_view.setShortcut("Ctrl+Shift+C")
        m.addAction(self.a_class_view)
        m.addAction(self.a_assistant)
        m.addAction(self.a_immediate)
        a = self.errors_dock.toggleViewAction()
        a.setShortcut("Ctrl+\\, E")
        m.addAction(a)
        m.addAction(self.find_dock.toggleViewAction())
        a = self.output_dock.toggleViewAction()
        a.setShortcut("Ctrl+Alt+O")
        m.addAction(a)
        a = self.toolbox_dock.toggleViewAction()
        a.setShortcut("Ctrl+Alt+X")
        m.addAction(a)
        a = self.props_dock.toggleViewAction()
        a.setShortcut("F4")
        m.addAction(a)

        self.format_menu = m = mb.addMenu("F&ormat")
        sub = m.addMenu("&Align")
        for text, how in (("&Lefts", "lefts"), ("&Centers", "centers"), ("&Rights", "rights"),
                          (None, None), ("&Tops", "tops"), ("&Middles", "middles"), ("&Bottoms", "bottoms")):
            if text is None:
                sub.addSeparator()
            else:
                sub.addAction(text, lambda h=how: self._fmt("align", h))
        sub = m.addMenu("Make Same &Size")
        sub.addAction("&Width", lambda: self._fmt("make_same", "width"))
        sub.addAction("&Height", lambda: self._fmt("make_same", "height"))
        sub.addAction("&Both", lambda: self._fmt("make_same", "both"))
        sub = m.addMenu("&Center in Form")
        sub.addAction("&Horizontally", lambda: self._fmt("center_in_form", "h"))
        sub.addAction("&Vertically", lambda: self._fmt("center_in_form", "v"))
        sub = m.addMenu("&Order")
        sub.addAction("Bring to &Front", lambda: self._fmt("z_order", True))
        sub.addAction("Send to &Back", lambda: self._fmt("z_order", False))
        m.setEnabled(False)

        m = mb.addMenu("&Git")          # VS 2022's Git menu
        m.addAction(self.a_git_changes)
        m.addSeparator()
        m.addAction("&Fetch", lambda: self.git_panel.fetch())
        m.addAction("&Pull", lambda: self.git_panel.pull())
        m.addAction("P&ush", lambda: self.git_panel.push())
        m.addAction("&Sync (Pull then Push)", lambda: self.git_panel.sync())
        m.addSeparator()
        m.addAction("&New Branch...", lambda: self.git_panel.new_branch())
        m.addAction("View Branch &History", lambda: self.git_panel.show_history())
        m.addAction("View File Hi&story", self._file_history)
        m.addAction("&Blame (Annotate)", self.show_blame)
        self.a_inline_blame = m.addAction("Inline Blame for the Current Line")
        self.a_inline_blame.setCheckable(True)
        self.a_inline_blame.setChecked(self.settings.value("inlineBlame", "false") == "true")
        self.a_inline_blame.toggled.connect(self._toggle_inline_blame)
        m.addSeparator()
        m.addAction("GitHub Pull Re&quests", self.show_pull_requests)
        m.addAction("GitHub &Issues", self.show_issues)
        m.addAction("Create GitHub &Release...", self.create_release)
        m.addSeparator()
        m.addAction("&Create Git Repository...", lambda: self.git_panel.create_repository())
        m.addAction("Add &Remote...", lambda: self.git_panel.add_remote())

        m = mb.addMenu("&Project")
        m.addAction(self.a_add_form)
        m.addAction(self.a_add_dialog)
        m.addAction(self.a_add_uc)
        m.addAction(self.a_add_class)
        m.addAction(self.a_add_module)
        m.addAction(self.a_add_test)
        m.addAction(self.a_add_query)
        m.addAction(self.a_add_report)
        m.addAction(self.a_add_notebook)
        m.addSeparator()
        m.addAction(self._act("Add &Reference...", lambda: self.add_reference()))
        m.addAction(self._act("Set as S&tartup Project", lambda: self.set_startup_project()))
        m.addSeparator()
        m.addAction(self.a_settings)
        m.addAction(self.a_resources)
        m.addAction(self.a_packages)
        m.addSeparator()
        a = m.addAction("Project &Properties...", lambda: self.project_properties())
        a.setShortcut("Alt+Return")

        m = mb.addMenu("&Build")
        m.addAction(self.a_rebuild)
        m.addSeparator()
        m.addAction(self.a_publish)
        self.a_publish_win = self._act("Publish for &Windows...", self.publish_windows)
        self.a_wincheck = self._act("Check for Windows Problems", self.windows_check)
        m.addAction(self.a_publish_win)
        m.addAction(self.a_wincheck)
        self.a_import_data = self._act("&Import Data...", lambda: self.server.import_data())

        m = mb.addMenu("&Debug")
        sub = m.addMenu("&Windows")
        for dk in self.debug_docks:
            sub.addAction(dk.toggleViewAction())
        sub.addAction(self.a_immediate)
        sub.addAction(self.a_exceptions)
        m.addSeparator()
        self.a_attach = self._act("Attach to &Process...", self.attach_to_process)
        self.a_tracepoint = self._act("Insert &Tracepoint...", lambda: self.current_editor() and
                                      self.current_editor().insert_tracepoint())
        self.a_bp_settings = self._act("Breakpoint &Settings...", lambda: self.current_editor() and
                                       self.current_editor().breakpoint_settings())
        self.a_apply_edits = self._act("Apply Code Changes", self.apply_code_changes, "Alt+F10")
        self.a_apply_edits.setToolTip("Edit and Continue: put your changes into the paused program now "
                                      "(Continue and Step do it too)")
        for a in (self.a_run, self.a_run_nodebug, self.a_attach, self.a_break, self.a_stop, self.a_restart, None,
                  self.a_step_into, self.a_step_over, self.a_step_out, self.a_run_to_cursor, self.a_set_next,
                  self.a_apply_edits, None,
                  self.a_toggle_bp, self.a_tracepoint, self.a_bp_settings, self.a_clear_bps, self.a_add_watch, None,
                  self.a_profile):
            m.addSeparator() if a is None else m.addAction(a)

        m = mb.addMenu("Te&st")         # VS's Test menu
        self.a_coverage = self._act("Analyze Code &Coverage for All Tests", self.analyze_coverage)
        self.a_coverage_results = self.coverage_dock.toggleViewAction()
        self.a_coverage_results.setText("Code Coverage &Results")
        for a in (self.a_run_tests, self.a_run_sel_tests, self.a_debug_tests, None, self.a_coverage,
                  self.a_coverage_results, None, self.a_test_explorer, None, self.a_add_test):
            m.addSeparator() if a is None else m.addAction(a)

        m = mb.addMenu("&Tools")
        m.addAction(self.a_assistant)
        m.addSeparator()
        sub = m.addMenu("Python &Package Manager")    # VS: Tools > NuGet Package Manager
        sub.addAction(self.a_packages)
        self.a_components = self._act("Install &Components...", self.install_components)
        m.addAction(self.a_components)
        m.addAction(self.a_import_data)
        m.addAction(self._act("Import and &Export Settings...", lambda: __import__(
            "pystudio.settingsio", fromlist=["x"]).SettingsDialog(self).exec()))
        m.addSeparator()
        m.addAction(self.a_options)

        m = mb.addMenu("&Window")
        m.addAction(self._act("&Save Window Layout...", self.save_layout))
        self.layouts_menu = m.addMenu("&Apply Window Layout")
        self.layouts_menu.aboutToShow.connect(self._fill_layouts_menu)
        self._layout_shortcuts = []
        m.addAction(self._act("&Manage Window Layouts...", self.manage_layouts))
        m.addAction(self._act("&Reset Window Layout", self.reset_layout))
        m.addSeparator()
        m.addAction(self._act("Close &All Documents", self.close_all_documents, "Ctrl+Shift+W"))
        m.addAction(self._act("Close All &But This", self.close_all_but_this))
        self._apply_layout_shortcuts()

        m = mb.addMenu("&Help")
        m.addAction(self.a_help)
        m.addAction(self._act("PyStudio &Guide (README)", self.open_readme))
        m.addSeparator()
        m.addAction(self._act("&Python Documentation", lambda: self._open_url("https://docs.python.org/3/")))
        m.addAction(self._act("&Qt for Python Documentation", lambda: self._open_url("https://doc.qt.io/qtforpython-6/")))
        m.addAction(self._act("Python &Tutorial", lambda: self._open_url("https://docs.python.org/3/tutorial/")))
        m.addSeparator()
        m.addAction(self._act("View &Log", self.view_log))
        m.addAction(self._act("Open Log &Folder", lambda: self._open_url(
            __import__("pystudio.errorlog", fromlist=["x"]).LOG_DIR)))
        m.addAction(self._act("Copy &System Info", self.copy_system_info))
        m.addSeparator()
        m.addAction(self._act("Check for &Updates...", self.check_for_updates))
        m.addAction(self._act("Install Update from &Zip...", lambda: __import__(
            "pystudio.updater", fromlist=["x"]).install_zip(self)))
        m.addSeparator()
        m.addAction(self._act("&About PyStudio", self.about))

    def _build_toolbar(self):
        tb = self.addToolBar("Standard")
        tb.setObjectName("StandardToolbar")
        tb.setMovable(False)
        for a in (self.a_nav_back, self.a_nav_fwd, None, self.a_new_project, self.a_new_file,
                  self.a_open_folder, self.a_save, self.a_save_all, None, self.a_run, self.a_stop):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.a_run.setIconText("Start")
        tb.widgetForAction(self.a_run).setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        dtb = self.addToolBar("Debug")
        dtb.setObjectName("DebugToolbar")
        dtb.setMovable(False)
        for a in (self.a_break, self.a_restart, None, self.a_step_into, self.a_step_over, self.a_step_out):
            dtb.addSeparator() if a is None else dtb.addAction(a)

    def _build_statusbar(self):
        self.lbl_state = QLabel("Ready")
        self.lbl_pos = QLabel("")
        self.lbl_python = QLabel("")
        sb = self.statusBar()
        sb.addWidget(self.lbl_state, 1)
        from .pkgupdates import StatusBadge
        self.pkg_badge = StatusBadge(self)
        sb.addPermanentWidget(self.lbl_pos)
        sb.addPermanentWidget(self.lbl_python)
        self._update_python_label()

    # =============================================================== helpers
    def current_editor(self) -> CodeEditor | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, CodeEditor) else None

    def current_doc(self):
        """The active tab: a CodeEditor or a FormDesigner."""
        return self.tabs.currentWidget()

    def current_designer(self) -> FormDesigner | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, FormDesigner) else None

    def editors(self):
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def _focus_call(self, name):
        w = QApplication.focusWidget()
        if w is None or not hasattr(w, name):
            w = self.current_editor()
        if w is not None and hasattr(w, name):
            getattr(w, name)()

    def _ed_call(self, name, *args):
        ed = self.current_editor()
        if ed:
            getattr(ed, name)(*args)

    def _update_title(self):
        ed = self.current_doc()
        parts = []
        if ed:
            parts.append(ed.display_name() + ("*" if ed.is_dirty else ""))
        if self.solution and len(self.solution.projects()) > 1:
            parts.append(self.solution.name)
        elif self.project_root:
            parts.append(os.path.basename(self.project_root))
        parts.append("PyStudio")
        self.setWindowTitle(" - ".join(parts))

    def _update_tab_text(self, ed):
        i = self.tabs.indexOf(ed)
        if i >= 0:
            self.tabs.setTabText(i, ed.display_name() + ("*" if ed.is_dirty else ""))
            self.tabs.setTabToolTip(i, ed.path or "Untitled")
        self._update_title()

    def _update_cursor_label(self):
        ed = self.current_editor()
        if ed and getattr(self, "a_inline_blame", None) is not None and self.a_inline_blame.isChecked():
            self._blame_timer.start()
        if ed:
            c = ed.textCursor()
            self.lbl_pos.setText(f"Ln {c.blockNumber() + 1}   Col {c.positionInBlock() + 1}")
        else:
            self.lbl_pos.setText("")
        self._nav_track()

    def _update_python_label(self):
        self.lbl_python.setText(f"Python: {self.python}")

    def _update_run_state(self):
        running = self.runner.is_running
        debugging = self.session.active
        paused = debugging and self.session.paused
        self.a_run.setEnabled(not running and (not debugging or paused))
        self.a_run.setText("&Continue" if debugging else "&Start Debugging")
        self.a_run.setIconText("Continue" if debugging else "Start")
        self.a_run_nodebug.setEnabled(not running and not debugging)
        profiling = getattr(self, "profile_proc", None) is not None
        self.a_stop.setEnabled(running or debugging or profiling)
        self.a_restart.setEnabled(debugging)
        self.a_break.setEnabled(debugging and not paused)
        for a in (self.a_step_into, self.a_step_over, self.a_step_out):
            a.setEnabled(not running and (paused or not debugging))
        self.a_step_out.setEnabled(paused)
        self.a_set_next.setEnabled(paused)
        self.a_run_to_cursor.setEnabled(not running and (paused or not debugging))
        if debugging:
            self.lbl_state.setText("Paused (debugging)" if paused else "Running (debugging)...")
            self.statusBar().setStyleSheet("QStatusBar { background: #ca5100; }")
        else:
            self.lbl_state.setText("Profiling..." if profiling else "Running..." if running else "Ready")
            self.statusBar().setStyleSheet("")

    def _on_tab_changed(self, _i):
        self._maybe_activate_for(getattr(self.current_doc(), "path", None))
        has = self.tabs.count() > 0
        self.tabs.setVisible(has)
        self.welcome.setVisible(not has)
        if not has:
            self.findbar.hide()
        d = self.current_designer()
        if d is not None:
            self.findbar.hide()
        self.toolbox.set_designer(d)
        self.props.set_designer(d)
        # the layout designer has its own Toolbox / Object Inspector / Properties: step the WinForms ones aside
        layout_doc = type(self.current_doc()).__name__ == "LayoutDesigner"
        if layout_doc and not getattr(self, "_docks_hidden_for_layout", None):
            self._docks_hidden_for_layout = [dk for dk in (self.toolbox_dock, self.props_dock) if dk.isVisible()]
            for dk in self._docks_hidden_for_layout:
                dk.hide()
        elif not layout_doc and getattr(self, "_docks_hidden_for_layout", None):
            for dk in self._docks_hidden_for_layout:
                dk.show()
            self._docks_hidden_for_layout = None
        self.format_menu.setEnabled(d is not None)
        self.a_tab_order.setEnabled(d is not None)
        self.a_tab_order.setChecked(bool(d is not None and d.tab_mode))
        self._update_cursor_label()
        self._update_title()

    def _add_editor(self, ed):
        ed.dirty_changed.connect(lambda _d, e=ed: self._update_tab_text(e))
        if isinstance(ed, CodeEditor):
            ed.cursorPositionChanged.connect(self._update_cursor_label)
            if getattr(self, "coverage", None):
                QTimer.singleShot(0, lambda e=ed: self._apply_coverage(e))   # after the file is loaded
            ed.lang = self.lang
            ed.context = self._lang_context
            ed.diagnostics_changed.connect(self.error_list.set_file)
            ed.document().contentsChanged.connect(lambda ed=ed: self._enc_mark(ed))
            if ed.path and os.path.abspath(ed.path) in self.breakpoints:
                ed.set_breakpoints(self.breakpoints[os.path.abspath(ed.path)])
            ed.breakpoints_changed.connect(self._on_editor_breakpoints)
            ed.set_next_requested.connect(lambda line: self.set_next_statement(line))
            self.attach_ghost(ed)
            self._nav_attach_editor(ed)
            ed.context_actions = [self.a_quickfix, self.a_rename, self.a_extract_method, self.a_extract_var,
                                  self.a_goto_def, self.a_find_refs, self.a_call_hierarchy, None,
                                  self.a_format_doc, None,
                                  self.a_toggle_bp, self.a_run_to_cursor, self.a_set_next, self.a_add_watch,
                                  None, self.a_ask_explain,
                                  self.a_ask_fix, self.a_ask_vb]
            if self.session.active and self.session.paused:
                ed.debug_eval = lambda expr, cb: self.session.evaluate(expr, self.current_frame, cb)
        elif isinstance(ed, QueryDocument):
            ed.saved.connect(self._on_query_saved)
        elif isinstance(ed, DiffView) or type(ed).__name__ in ("ReportDesigner", "LayoutDesigner",
                                                                  "NotebookDocument", "MergeEditor", "BlameView"):
            pass
        elif isinstance(ed, ProfileReport):
            ed.open_location.connect(self.open_file_at)
        else:
            ed.handler_requested.connect(lambda name, d=ed: self.create_event_handler(d, name))
            ed.dirty_changed.connect(lambda dirty, d=ed: self._hot_reload_soon(d) if dirty else None)
            ed.control_renamed.connect(lambda old, new, d=ed: self._rename_in_code(d, old, new))
            ed.tab_mode_changed.connect(lambda on, d=ed: d is self.current_designer() and
                                        self.a_tab_order.setChecked(on))
        i = self.tabs.addTab(ed, ed.display_name())
        self.tabs.setTabToolTip(i, ed.path or "Untitled")
        self.tabs.setCurrentIndex(i)
        ed.setFocus()
        return ed

    # ================================================================= files
    def new_file(self):
        self._add_editor(CodeEditor())

    def open_file_dialog(self):
        start = self.project_root or os.path.expanduser("~")
        paths, _ = QFileDialog.getOpenFileNames(self, "Open File", start,
                                                "Python files (*.py *.pyw);;All files (*)")
        for p in paths:
            self.open_file(p)

    def open_file(self, path):
        self.dismiss_start_page()
        path = os.path.abspath(path)
        for ed in self.editors():
            if ed.path and os.path.abspath(ed.path) == path:
                self.tabs.setCurrentWidget(ed)
                return ed
        if path.endswith(".ui"):
            return self.open_designer(path)
        if path.lower().endswith(".psrpt"):
            from .reportdesigner import ReportDesigner
            try:
                return self._add_editor(ReportDesigner(self.data, self.project, path))
            except (OSError, ValueError) as e:
                QMessageBox.critical(self, "Report designer", f"Could not open {path}:\n{e}")
                return None
        if path.lower().endswith(".ipynb"):
            from .notebook import NotebookDocument
            try:
                return self._add_editor(NotebookDocument(path, self._notebook_context))
            except (OSError, ValueError) as e:
                QMessageBox.critical(self, "Notebook", f"Could not open {os.path.basename(path)}:\n{e}")
                return None
        if path.lower().endswith(".sql"):
            try:
                return self._add_editor(QueryDocument(self.data, self.project, path))
            except OSError as e:
                QMessageBox.critical(self, "Open failed", f"Could not open {path}:\n{e}")
                return None
        try:
            ed = CodeEditor(path)
        except OSError as e:
            QMessageBox.critical(self, "Open failed", f"Could not open {path}:\n{e}")
            return None
        return self._add_editor(ed)

    def open_designer(self, path):
        try:
            d = FormDesigner(path)
        except UnsupportedForm as e:
            return self._open_qt_layout_form(path, str(e))
        except Exception as e:  # malformed XML etc.
            QMessageBox.critical(self, "Form designer", f"Could not open {path}:\n{e}")
            return None
        return self._add_editor(d)

    def _set_startup_file(self, rel):
        self.project["startup"] = rel
        self.project.save()
        self.statusBar().showMessage(f"F5 now runs {rel}", 4000)

    def _restore_ui_backup(self, bak):
        ui = bak[:-4]
        name = os.path.basename(ui)
        if QMessageBox.question(self, "Restore Backup",
                                f"Put the original {name} back from {os.path.basename(bak)}?\n\nThe converted "
                                f"version is kept as {name}.converted.") != QMessageBox.Yes:
            return
        for ed in self.editors():                     # close its designer tab first
            if getattr(ed, "path", None) and os.path.abspath(ed.path) == os.path.abspath(ui):
                ed._set_dirty(False) if hasattr(ed, "_set_dirty") else None
                self.close_tab(self.tabs.indexOf(ed))
        try:
            projectfit.restore_backup(bak)
        except OSError as e:
            QMessageBox.critical(self, "Restore Backup", str(e))
            return
        self.output.write(f"Restored the original {name} (with its layouts).\n", "info")

    def _open_qt_layout_form(self, path, reason):
        """A form PyStudio's WinForms-style designer can't take (Qt layouts, or a QDialog/QWidget form): open it
        in the layout designer, which edits it as it is."""
        from . import layoutconvert
        from .layoutdesigner import LayoutDesigner
        name = os.path.basename(path)
        proj_root = (self.solution.project_for(path) if self.solution else None) or self.project_root or \
            os.path.dirname(os.path.abspath(path))
        own_loader = self._is_external(path) and (projectfit.loads_ui_at_runtime(proj_root)
                                                  or projectfit.uses_pyqt(proj_root))
        try:
            d = LayoutDesigner(path, proj_root, self._is_external(path))
        except Exception as e:  # noqa: BLE001 - not a form, broken XML...
            QMessageBox.critical(self, "Form designer", f"Could not open {name}:\n{e}")
            return None
        d.open_code.connect(lambda p, line: self.open_file_at(p, line))
        d.control_renamed.connect(lambda old, new, d=d: self._rename_control_code(d.path, old, new))
        if layoutconvert.uses_layouts(path) and d.model.form_class == "QMainWindow" and not own_loader:
            d.add_info_button("Convert to fixed positions", lambda: self._convert_layout_form(d),
                              "Use PyStudio's WinForms-style designer instead: every control keeps its place and "
                              f"gets an Anchor; the original is kept as {name}.bak")
        d.add_info_button("Open in Qt Designer", lambda: self.qt_designer.open(path))
        bak = path + ".bak"
        if os.path.isfile(bak) and layoutconvert.uses_layouts(bak) and not layoutconvert.uses_layouts(path):
            d.add_info_button("Restore Original (with layouts)",
                              lambda: (self._restore_ui_backup(bak), self.open_file(path)),
                              f"This form was converted to fixed positions; {name}.bak is the original with its "
                              "layouts. Put it back (the converted one is kept as .converted).")
        return self._add_editor(d)

    def _convert_layout_form(self, d):
        from . import layoutconvert
        path, name = d.path, os.path.basename(d.path)
        if d.is_dirty and not self._save(d):
            return
        if QMessageBox.question(self, "Convert form", f"Convert {name} to fixed positions (WinForms-style)? "
                                f"The original is kept as {name}.bak.") != QMessageBox.Yes:
            return
        self.close_tab(self.tabs.indexOf(d))
        try:
            report = layoutconvert.convert(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Convert form", f"Couldn't convert {name}:\n{e}")
            return
        done, msg = compile_ui(path)
        self.output_dock.show()
        self.output.write(f"Converted {name}:\n  " + "\n  ".join(report) + "\n", "info")
        if not done:
            self.output.write(msg + "\n", "err")
        self.open_file(path)

    def _fmt(self, method, arg):
        d = self.current_designer()
        if d is not None:
            getattr(d, method)(arg)

    # ---------------------------------------------- form code (form1.py) helpers
    def _code_text(self, py):
        """Current text of a .py file: from its open tab if any, else from disk."""
        for ed in self.editors():
            if isinstance(ed, CodeEditor) and ed.path and os.path.abspath(ed.path) == os.path.abspath(py):
                return ed.toPlainText()
        if os.path.exists(py):
            with open(py, encoding="utf-8", errors="replace") as f:
                return f.read()
        return ""

    def _set_code_text(self, py, text):
        """Replace a .py file's text (in its tab if open - undoable - else on disk)."""
        for ed in self.editors():
            if isinstance(ed, CodeEditor) and ed.path and os.path.abspath(ed.path) == os.path.abspath(py):
                c = ed.textCursor()
                pos = c.position()
                c.beginEditBlock()
                c.select(QTextCursor.Document)
                c.insertText(text)
                c.endEditBlock()
                c.setPosition(min(pos, len(text)))
                ed.setTextCursor(c)
                return
        with open(py, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def _handlers_for(self, designer):
        """{(control, signal): handler} from the form's .py wiring lines."""
        py, _ = self._form_pair(designer.path)
        found = {}
        for m in re.finditer(r"self\.ui\.(\w+)\.(\w+)\.connect\(self\.(\w+)\)", self._code_text(py)):
            found[(m.group(1), m.group(2))] = m.group(3)
        return found

    def _rename_in_code(self, designer, old, new):
        """Keep the form's code working when a control is renamed (self.ui.old -> self.ui.new)."""
        py, _ = self._form_pair(designer.path)
        text = self._code_text(py)
        new_text = re.sub(rf"\bself\.ui\.{re.escape(old)}\b", f"self.ui.{new}", text)
        if new_text != text:
            self._set_code_text(py, new_text)
            self.output.write(f"Updated references: self.ui.{old} -> self.ui.{new} "
                              f"in {os.path.basename(py)}\n", "info")

    def create_event_handler(self, designer, control, event=None):
        """VS behavior: double-click a control (or click an event in the Events tab)
        -> jump to, or create, its handler."""
        if designer is None:
            return
        py, _ui = self._form_pair(designer.path)
        if not os.path.exists(py):
            with open(py, "w", encoding="utf-8", newline="\n") as f:
                stem = os.path.splitext(os.path.basename(py))[0]
                f.write(FORM_PY.format(cls=designer.form_class, stem=stem))
        ctrl = designer.find_control(control) if control else None
        if event is None and ctrl is not None:
            event = M.default_event(M.tool_of(ctrl))
        comp = designer.find_component(control) if control and ctrl is None else None
        if event is None and comp is not None:
            event = M.default_event(comp["type"])
        tool_names = {it["name"] for it in (designer.toolbar or {}).get("items", []) if it.get("name")}
        if event is None and control and ctrl is None and comp is None and \
                control in {n for n, _t in M.all_actions(designer.menu)} | tool_names:
            event = ("triggered", "")      # menu item: VS's Click
        ed = self.open_file(py)
        if ed is None or event is None:
            return  # form or a control without a default event: just show the code
        if designer.is_dirty:
            self._save(designer)   # so ui_<form>.py knows the control exists
        signal, arg = event
        try:
            new_src, line = add_event_handler(ed.toPlainText(), control, signal, arg)
        except ValueError as e:
            QMessageBox.warning(self, "Event handler", str(e))
            return
        if new_src != ed.toPlainText():
            c = ed.textCursor()
            c.beginEditBlock()
            c.select(QTextCursor.Document)
            c.insertText(new_src)
            c.endEditBlock()
        ed.go_to_line(line, 8)
        c = ed.textCursor()
        c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)   # select 'pass' so typing replaces it
        ed.setTextCursor(c)

    def open_file_at(self, path, line, col=0):
        ed = self.open_file(path)
        if ed and hasattr(ed, "go_to_line"):
            ed.go_to_line(line, col)

    # ------------------------------------------------------------ IntelliSense
    def _lang_context(self):
        if self.project is not None:
            return self.project.root, self.project.python(self.python)
        return self.project_root, self.python

    def _update_error_title(self, errors, warnings):
        self.errors_dock.setWindowTitle(f"Error List ({errors})" if errors else "Error List")

    def _open_texts(self):
        return {os.path.abspath(ed.path): ed.toPlainText() for ed in self.editors()
                if isinstance(ed, CodeEditor) and ed.path}

    def scan_errors(self):
        self.error_list.scan_project(self.project_root, self._open_texts())

    def go_to_definition(self):
        ed = self.current_editor()
        if ed is None:
            return
        line, col = ed.position()

        def done(results):
            target = next((r for r in results if r[0]), None)
            if target is None:
                self.statusBar().showMessage(
                    "Definition is in a built-in/compiled module." if results else "No definition found.", 4000)
                return
            path, ln, cl, name, builtin = target
            opened = self.open_file(path)
            if opened is None:
                return
            mine = self.solution_root or self.project_root
            if mine and not os.path.abspath(path).startswith(mine + os.sep):
                opened.setReadOnly(True)     # library code (PySide6, Python) - look, don't touch
                i = self.tabs.indexOf(opened)
                self.tabs.setTabText(i, opened.display_name() + " [read-only]")
            opened.go_to_line(ln or 1, cl or 0)
            self.statusBar().showMessage(name, 4000)
        self.statusBar().showMessage("Finding definition...", 2000)
        self.lang.request("goto", ed.toPlainText(), ed.path, line, col, self._lang_context(), done)

    def rename_symbol(self):
        """F2 in code: rename a variable/function/class everywhere in the project (Jedi)."""
        d = self.current_designer()
        if d is not None:                     # in the designer F2 edits a menu item's text
            if d.special == "menuitem":
                d.begin_menu_edit()
            return
        ed = self.current_editor()
        if ed is None or ed.isReadOnly():
            return
        c = ed.textCursor()
        c.select(QTextCursor.WordUnderCursor)
        old = c.selectedText()
        if not old.isidentifier():
            self.statusBar().showMessage("Put the cursor on a name to rename it.", 4000)
            return
        new, ok = QInputDialog.getText(self, "Rename", f"New name for '{old}':", text=old)
        new = new.strip()
        if not ok or not new or new == old:
            return
        if not new.isidentifier():
            QMessageBox.warning(self, "Rename", f"'{new}' isn't a valid Python name.")
            return
        if not self.save_all():
            return
        line, col = ed.position()
        col = c.selectionStart() - c.block().position()
        from . import refactor
        root = self.project_root or os.path.dirname(ed.path or "")
        # a form's control (self.ui.goButton / self.goButton with uic.loadUi): the .ui and the code using it
        if root and refactor.is_control_reference(c.block().text(), col):
            uis = refactor.ui_files_with(root, old)
            if uis:
                taken = [u for u in uis if new in refactor.ui_names(open(u, encoding="utf-8").read())]
                if taken:
                    QMessageBox.warning(self, "Rename", f"'{new}' is already used on {os.path.basename(taken[0])}.")
                    return
                changes = refactor.control_rename_changes(root, old, new, ui_paths=uis)
                self._preview_and_apply(f"Rename control '{old}' to '{new}'", changes, root)
                return

        def done(result):
            if "__error__" in result:
                QMessageBox.warning(self, "Rename", f"Couldn't rename '{old}':\n{result['__error__']}")
                return
            from .language import is_generated
            if any(is_generated(p) for p in result):
                QMessageBox.information(
                    self, "Rename", f"'{old}' is defined in a form's generated code (ui_*.py). To rename a "
                    "control, put the cursor on it where the code uses it (self.ui.name), or change (Name) in "
                    "the designer's Properties window - both update the form and your code.")
                return
            changes = {}
            for path, code in result.items():
                opened = self._open_code_editor(path)
                before = opened.toPlainText() if opened is not None else self._code_text(path)
                if before != code:
                    changes[path] = (before, code)
            if not changes:
                self.statusBar().showMessage(f"Nothing to rename for '{old}'.", 4000)
                return
            self._preview_and_apply(f"Rename '{old}' to '{new}'", changes, root)
        self.lang.request_rename(ed.toPlainText(), ed.path, line, col, self._lang_context(), new, done)

    def _open_code_editor(self, path):
        return next((e for e in self.editors() if isinstance(e, CodeEditor) and e.path and
                     os.path.abspath(e.path) == os.path.abspath(path)), None)

    def _preview_and_apply(self, summary, changes, root=None, preview=True):
        """Show Preview Changes, then write the checked files (open editors change in place - Ctrl+Z undoes)."""
        from . import refactor
        if not changes:
            return []
        if preview:
            dlg = refactor.PreviewDialog(summary.split(" '")[0], summary, changes, root, self)
            if dlg.exec() != QDialog.Accepted:
                return []
            chosen = dlg.selected()
        else:
            chosen = list(changes)
        done = []
        for path in chosen:
            new = changes[path][1]
            opened = self._open_code_editor(path)
            designers = [e for e in self.editors() if not isinstance(e, CodeEditor) and getattr(e, "path", None)
                         and os.path.abspath(e.path) == os.path.abspath(path)]
            if opened is not None:
                opened.replace_all_text(new)
                self._save(opened)
            else:
                for d in designers:            # a form open in a designer: reopen it with the new names
                    self.tabs.removeTab(self.tabs.indexOf(d))
                    d.deleteLater()
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new)
                if path.endswith(".ui") and not self._is_external(path):
                    ok, msg = compile_ui(path)
                    if not ok:
                        self.output.write(msg + "\n", "err")
                if designers:
                    self.open_file(path)
            done.append(path)
            if path.endswith(".py"):
                self.class_view.file_saved(path)
        if done:
            self.statusBar().showMessage(f"{summary}: {len(done)} file(s) changed.", 6000)
            self.output.write(f"{summary}: " + ", ".join(os.path.basename(p) for p in done) + "\n", "info")
            self.scan_errors()
        return done

    def _rename_control_code(self, ui_path, old, new):
        """The layout designer renamed a control: keep the code using the form working (no preview, like the
        WinForms-style designer)."""
        from . import refactor
        root = self.project_root or os.path.dirname(ui_path)
        texts = {os.path.abspath(e.path): e.toPlainText() for e in self.editors()
                 if isinstance(e, CodeEditor) and e.path}
        changes = refactor.control_rename_changes(root, old, new, texts=texts, ui_paths=[ui_path], code_only=True)
        self._preview_and_apply(f"Renamed control '{old}' to '{new}'", changes, root, preview=False)

    def find_references(self):
        ed = self.current_editor()
        if ed is None:
            return
        line, col = ed.position()
        c = ed.textCursor()
        c.select(QTextCursor.WordUnderCursor)
        word = c.selectedText()

        def done(results):
            res = [r for r in results if r[0]]
            self.find_results.show_results(f"Find All References: '{word}'", res, self.project_root)
            self.find_dock.show()
            self.find_dock.raise_()
        self.statusBar().showMessage("Finding references...", 2000)
        self.lang.request("references", ed.toPlainText(), ed.path, line, col, self._lang_context(), done)

    def view_call_hierarchy(self):
        ed = self.current_editor()
        if ed is None:
            return
        line, col = ed.position()
        self.call_hierarchy.lang = self.lang
        self.calls_dock.show()
        self.calls_dock.raise_()
        self.call_hierarchy.show_for(ed.toPlainText(), ed.path, line, col)

    def open_path(self, path):
        if os.path.isdir(path):
            self.open_folder(path)
        elif os.path.isfile(path):
            self.open_file(path)

    def save_current(self):
        self.props.flush()
        ed = self.current_doc()
        return self._save(ed) if ed else False

    def save_current_as(self):
        ed = self.current_doc()
        return self._save(ed, force_dialog=True) if ed else False

    def _save(self, ed, force_dialog=False):
        if (isinstance(ed, CodeEditor) and ed.path and ed.path.endswith(".py") and formatter.format_on_save()
                and not is_generated_path(ed.path)):
            self.format_document(ed=ed, quiet=True)
        if ed.path and not force_dialog:
            ok = ed.save()
        elif isinstance(ed, QueryDocument):
            folder = os.path.join(self.project_root, "queries") if self.project_root else os.path.expanduser("~")
            path, _ = QFileDialog.getSaveFileName(self, "Save Query As", ed.path or os.path.join(
                folder, ed.display_name()), "SQL queries (*.sql);;All files (*)")
            if not path:
                return False
            if not path.lower().endswith(".sql"):
                path += ".sql"
            ok = ed.save(path)
        else:
            start = ed.path or os.path.join(self.project_root or os.path.expanduser("~"), "untitled.py")
            path, _ = QFileDialog.getSaveFileName(self, "Save As", start,
                                                  "Python files (*.py);;All files (*)")
            if not path:
                return False
            ok = ed.save(path)
        self._update_tab_text(ed)
        if ok and type(ed).__name__ == "LayoutDesigner" and not self._is_external(ed.path):
            done, msg = compile_ui(ed.path)   # PyStudio projects: keep ui_<form>.py in step
            if not done:
                self.output.write(msg + "\n", "err")
        if ok and isinstance(ed, CodeEditor) and ed.path and self.solution_root:
            self.task_list.update_file(ed.path, ed.toPlainText())
            self.class_view.file_saved(ed.path)
        if ok and isinstance(ed, FormDesigner) and not self._is_external(ed.path):
            done, msg = compile_ui(ed.path)   # keep ui_<form>.py in step with the form
            if not done:
                self.output.write(msg + "\n", "err")
            self._ensure_form_runtime(ed)
            if ed.form.get("kind") == "UserControl":
                self.refresh_user_controls()   # new size/look for its copies on other forms
        return ok

    def _ensure_form_runtime(self, designer):
        """Forms made before v0.5 get the pystudio_runtime hookup (Anchor etc.)."""
        folder = os.path.dirname(designer.path)
        Project(folder).ensure_runtime() if self.project is None else self.project.ensure_runtime()
        py, _ = self._form_pair(designer.path)
        if os.path.exists(py):
            text = self._code_text(py)
            new = ensure_runtime_call(text)
            if designer.form.get("kind") != "UserControl":
                new = ensure_form_base(new)    # v1.2: Form base class (show_dialog, dialog_result)
            if new != text:
                self._set_code_text(py, new)
                for ed in self.editors():
                    if isinstance(ed, CodeEditor) and ed.path and os.path.abspath(ed.path) == os.path.abspath(py):
                        self._save(ed)

    def save_all(self):
        self.props.flush()   # a Properties field still being typed in counts as a change
        for ed in self.editors():
            if ed.is_dirty and not self._save(ed):
                return False
        if self.git.root and not self.git.busy:
            self.git_panel.refresh()
        return True

    def _confirm_discard(self, ed) -> bool:
        """Returns True if it's OK to close this editor."""
        if not ed.is_dirty:
            return True
        r = QMessageBox.question(
            self, "PyStudio", f"Save changes to {ed.display_name()}?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if r == QMessageBox.Save:
            return self._save(ed)
        return r == QMessageBox.Discard

    def close_tab(self, index):
        if index < 0:
            return
        ed = self.tabs.widget(index)
        if self._confirm_discard(ed):
            self.tabs.removeTab(index)
            if hasattr(ed, "shutdown"):
                ed.shutdown()                 # a notebook's Python
            if isinstance(ed, CodeEditor) and ed.path and os.path.exists(ed.path):
                # back to the saved file's errors (unsaved edits were discarded)
                try:
                    with open(ed.path, encoding="utf-8", errors="replace") as f:
                        self.error_list.set_file(ed.path, check_source(f.read(), ed.path))
                except OSError:
                    pass
            ed.deleteLater()

    # =============================================================== project
    def import_vb_project(self, vbproj=None):
        """File > Import VB.NET Project: a Visual Studio WinForms .vbproj / .csproj (or the .sln holding one)
        becomes a PyStudio project."""
        from .templates import default_repos_dir
        from . import vbimport
        if not vbproj:
            vbproj, _f = QFileDialog.getOpenFileName(
                self, "Import VB.NET or C# Project", os.path.expanduser("~"),
                "Visual Studio solutions and WinForms projects (*.sln *.vbproj *.csproj);;All files (*)")
        if not vbproj:
            return
        if vbproj.lower().endswith(".sln"):
            vbproj = self._project_in_sln(vbproj)
            if not vbproj:
                return
        if vbproj.lower().endswith(".csproj"):
            from . import csimport
            info = csimport.parse_csproj(vbproj)
        else:
            info = vbimport.parse_vbproj(vbproj)
        base = self.settings.value("projectsDir", default_repos_dir())
        name = re.sub(r"[^A-Za-z0-9_-]", "", info["name"]) or "ImportedApp"
        root, n = os.path.join(base, name), 2
        while os.path.exists(root):
            root, n = os.path.join(base, f"{name}{n}"), n + 1
        if not info["forms"]:
            QMessageBox.warning(self, "Import VB.NET Project",
                                "No Windows Forms were found in this project (no *.Designer.vb / *.Designer.cs with "
                                "InitializeComponent).")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            files, report = vbimport.import_project(vbproj, root, os.path.basename(root))
        except Exception as e:  # noqa: BLE001 - show the reason, don't crash the IDE
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Import VB.NET Project", f"The import failed:\n{e}")
            return
        QApplication.restoreOverrideCursor()
        for i in reversed(range(self.tabs.count())):
            self.close_tab(i)
        self.open_folder(root)
        for f in files:
            self.open_file(f)
        self.open_file(os.path.join(root, "IMPORT_REPORT.txt"))
        self.output.clear()
        self.output.write("\n".join(report) + "\n", "info")
        self.statusBar().showMessage(f"Imported {len(info['forms'])} form(s) into {root}", 8000)

    def new_project(self, add_to_solution=False):
        dlg = NewProjectDialog(self, solution_root=self.solution_root if self.project_root else None,
                               add_to_solution=add_to_solution)
        if dlg.exec() != NewProjectDialog.Accepted:
            return
        root = dlg.project_root
        name = os.path.basename(root)
        try:
            files = create_project(root, name, dlg.template.currentText())
        except OSError as e:
            QMessageBox.critical(self, "New Project", f"Could not create the project:\n{e}")
            return
        if dlg.add_to_solution:
            self._ensure_solution().add(root)
            for f in files:
                if f.endswith(".ui"):
                    compile_ui(f)
            self._activate_project(root)
            for f in files:
                if f.endswith(".py"):
                    self.open_file(f)
            self.output.write(f"Added project {name} to solution '{self.solution.name}' ({root})\n"
                              "Use it from another project: right-click that project > Add Reference.\n", "info")
            return
        # Close tabs from the previous project (prompts to save if needed)
        for i in reversed(range(self.tabs.count())):
            self.close_tab(i)
        self.open_folder(root)
        for f in files:
            if f.endswith(".ui"):
                compile_ui(f)
        for f in files:
            if f.endswith(".py"):
                self.open_file(f)
        ui = next((f for f in files if f.endswith(".ui")), None)
        if ui:
            d = self.open_file(ui)          # Form1 [Design] tab, like VS
            if d is not None:
                self.tabs.tabBar().moveTab(self.tabs.indexOf(d), 0)
                self.tabs.setCurrentWidget(d)
        self.output.clear()
        self.output.write(f"Created project {name} in {root}\n", "info")
        if dlg.publish_github:
            QTimer.singleShot(300, lambda: self._publish_new_project(root))

    def create_release(self):
        from . import ghrelease
        ghrelease.create_release(self.project, self)

    def _publish_new_project(self, root):
        """New Project's "Publish to GitHub": a repository with an initial commit, then on GitHub."""
        from . import ghpublish
        p = self.git_panel
        if os.path.abspath(p.git.folder or "") != os.path.abspath(root):
            p.set_folder(root)
        if not p.git.root:
            p.create_repository()
            if not p.git.root:
                return
        self.git_dock.show()
        self.git_dock.raise_()
        ghpublish.publish(p, quiet=True)

    def add_form_dialog(self, folder):
        self.add_item_dialog("Windows Form", folder)

    def add_item_dialog(self, kind, folder=None):
        """Project > Add Windows Form / Dialog / User Control / Class / Module."""
        folder = folder or self.project_root
        title = f"Add {kind}"
        if not folder:
            QMessageBox.information(self, title, "Open or create a project first.")
            return
        label = {"Module": "Module name (lower case, e.g. helpers):"}.get(kind, "Name:")
        name, ok = QInputDialog.getText(self, title, label, text=next_item_name(folder, kind))
        name = name.strip()
        if name.lower().endswith((".py", ".ui")):
            name = name[:-3]
        if not ok or not name:
            return
        if not name.isidentifier():
            QMessageBox.warning(self, title, "Use letters, numbers and _ only (no spaces; can't start with a number).")
            return
        try:
            if kind in ("Class", "Module"):
                self.open_file(add_code_file(folder, name, kind))
                return
            ui, py = add_form(folder, name, kind, self.project.name if self.project else "")
        except FileExistsError as e:
            QMessageBox.warning(self, title, str(e))
            return
        compile_ui(ui)
        if kind == "User Control":
            self.refresh_user_controls()
        self.open_file(py)
        self.open_file(ui)

    def refresh_user_controls(self):
        """Toolbox '<project> Components' group + up-to-date previews of placed copies."""
        classes = M.register_user_controls(self.project_root) if self.project_root else M.register_user_controls(None)
        self.toolbox.set_project_controls(self.project.name if self.project else "Project", classes)
        for ed in self.editors():
            if isinstance(ed, FormDesigner):
                for c in ed.controls:
                    if c._ps.get("tool") in M.USER_CONTROLS:
                        M.apply_visuals(c, ed.project_dir)

    # ------------------------------------------------- code <-> designer (F7)
    def _form_pair(self, path):
        stem, ext = os.path.splitext(path)
        return (stem + ".py", stem + ".ui")

    def view_code(self):
        ed = self.current_doc()
        if ed and ed.path and ed.path.endswith(".ui"):
            py, _ = self._form_pair(ed.path)
            if os.path.exists(py):
                self.open_file(py)

    def view_designer(self):
        ed = self.current_editor()
        if ed and ed.path:
            _, ui = self._form_pair(ed.path)
            if os.path.exists(ui):
                self.open_file(ui)
                return
        self.statusBar().showMessage("This file has no form.", 3000)

    # ------------------------------------------------------- start / options
    def show_start_window(self):
        """VS-style Start page drawn over the editor area, inside this window. (A separate modal
        Start Window could stay unmapped on GNOME/Wayland and lock the IDE, and it took the
        dock icon with it when it closed.)"""
        if self._start_page is not None:
            self._start_page.raise_()
            return
        host = self.centralWidget()
        sw = StartWindow(host)
        sw.setWindowFlags(Qt.Widget)
        sw.setAttribute(Qt.WA_StyledBackground, True)
        sw.setGeometry(host.rect())
        self._start_page = sw
        self._start_fit = _FitToParent(sw)
        host.installEventFilter(self._start_fit)
        sw.finished.connect(lambda _r: self._start_finished(sw))
        sw.show()
        sw.raise_()
        sw.setFocus()

    def dismiss_start_page(self):
        """Opening a project or file any other way (File menu, import, Solution Explorer) closes the Start page,
        like Visual Studio - otherwise it keeps covering the editor."""
        sw = self._start_page
        if sw is None:
            return
        self.centralWidget().removeEventFilter(self._start_fit)
        self._start_page = self._start_fit = None
        try:
            sw.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        sw.hide()
        sw.deleteLater()

    def _start_finished(self, sw):
        if self._start_page is not sw:
            return
        self.centralWidget().removeEventFilter(self._start_fit)
        self._start_page = self._start_fit = None
        sw.hide()
        sw.deleteLater()
        self.run_start_action(sw.result_action)

    def run_start_action(self, action):
        kind, path = action
        if kind == "open" and path:
            self.open_folder(path)
            self._open_startup_form()
        elif kind == "new":
            self.new_project()

    def _open_startup_form(self):
        """Like VS reopening your documents: open the startup form's designer."""
        if self.project is None or self.tabs.count():
            return
        cls = self.project.current_startup_form()
        for c, stem in self.project.forms():
            if c == cls or not cls:
                ui = os.path.join(self.project.root, stem + ".ui")
                self.open_file(ui)
                break

    def show_options(self):
        dlg = OptionsDialog(self.python, self)
        if dlg.exec():
            dlg.apply()
            self._repaint_editors()
            from . import theme
            if theme.saved_theme() != theme.NAME:
                if QMessageBox.question(self, "Color theme", f"Restart PyStudio now to use the {theme.saved_theme()} "
                                        "theme? (Your work is saved first.)") == QMessageBox.Yes:
                    self.restart_pystudio()
            self.python = self.settings.value("python", sys.executable)
            self._update_python_label()
            from .options import editor_font_size
            for ed in self.editors():
                if isinstance(ed, CodeEditor):
                    f = ed.font()
                    f.setPointSize(editor_font_size())
                    ed.setFont(f)

    def toggle_tab_order(self):
        d = self.current_designer()
        if d is not None:
            d.toggle_tab_order(self.a_tab_order.isChecked())
            if d.tab_mode:
                self.statusBar().showMessage("Tab Order: click controls in the order Tab should visit "
                                             "them. Esc or View > Tab Order when done.", 8000)

    def rebuild_forms(self):
        if not self.project_root:
            return
        self.save_all()
        from .qttools import compile_ui
        self.output.clear()
        n = 0
        for dirpath, dirnames, files in os.walk(self.project_root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in HIDDEN_NAMES
                           and d != "publish"]
            for f in files:
                if f.endswith(".ui"):
                    ok, msg = compile_ui(os.path.join(dirpath, f))
                    self.output.write(msg + "\n", "info" if ok else "err")
                    n += 1
        self.output.write(f"Rebuild: {n} form(s).\n", "info")

    def publish_project(self):
        if self.project is None:
            QMessageBox.information(self, "Publish", "Open or create a project first.")
            return
        if not self.save_all():
            return
        ProjectPropertiesDialog(self.project, self.python, self.output, self, page="Publish").exec()

    def windows_check(self, root=None):
        """Build > Check for Windows Problems: Linux-only code, before publishing for Windows."""
        root = root or self.startup_root()
        if not root or not sln.is_project_dir(root):
            QMessageBox.information(self, "Windows Check", "Open or create a project first.")
            return
        self.save_all()
        from .wincheck import WindowsCheckDialog
        WindowsCheckDialog(root, open_at=self.open_file_at, parent=self).exec()

    def publish_windows(self, root=None):
        """Build > Publish for Windows: a Windows .exe made here with Wine, or on GitHub."""
        root = root or self.startup_root()
        if not root or not sln.is_project_dir(root):
            QMessageBox.information(self, "Publish for Windows", "Open or create a project first.")
            return
        if not self.save_all():
            return
        from .winpublish import WindowsPublishDialog
        WindowsPublishDialog(Project(root), parent=self).exec()

    def _project_in_sln(self, sln_path):
        """The .vbproj / .csproj a Visual Studio .sln points to (asks when there are several), or None."""
        try:
            text = open(sln_path, encoding="utf-8-sig", errors="replace").read()
        except OSError as e:
            QMessageBox.warning(self, "Import", f"Couldn't read {sln_path}:\n{e}")
            return None
        base = os.path.dirname(os.path.abspath(sln_path))
        listed = [p.replace("\\", "/") for p in re.findall(r'^Project\([^)]*\)\s*=\s*"[^"]*",\s*"([^"]+\.(?:vb|cs)proj)"',
                                                           text, re.M | re.I)]
        found = [os.path.normpath(os.path.join(base, p)) for p in listed]
        have = [p for p in found if os.path.exists(p)]
        if not have:
            names = "\n".join("  " + p for p in listed) or "  (no VB.NET or C# projects listed)"
            QMessageBox.warning(self, "Import", "The solution lists:\n" + names + "\n\nbut " + (
                "that file isn't" if len(listed) == 1 else "those files aren't") + " next to it. A .sln is only an "
                "index: copy the whole solution folder (the .sln plus the project folder with the .vbproj and the "
                ".vb files) to this computer, then open the .sln from there.")
            return None
        if len(have) == 1:
            return have[0]
        labels = [os.path.relpath(p, base) for p in have]
        choice, ok = QInputDialog.getItem(self, "Import", "This solution has several projects. Import which one?",
                                          labels, 0, False)
        return have[labels.index(choice)] if ok else None

    def open_project_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project/Solution", self.settings.value("projectsDir", os.path.expanduser("~/repos")),
            "PyStudio projects and solutions (*.pyproj *.pysln);;"
            "Visual Studio solutions and projects - imported (*.sln *.vbproj *.csproj);;All files (*)")
        if path and path.lower().endswith((".sln", ".vbproj", ".csproj")):
            return self.import_vb_project(path)
        if path:
            folder = os.path.dirname(os.path.abspath(path))
            if path.endswith(".pyproj") and not sln.find_solution_file(folder):
                parent = os.path.dirname(folder)            # a project inside a solution: open the solution
                ps = sln.find_solution_file(parent)
                if ps and Solution(ps).contains(folder):
                    folder = parent
            self.open_folder(folder)

    def toggle_show_all(self):
        self.proxy.show_all = self.b_showall.isChecked()
        self.proxy.invalidateFilter()

    def project_properties(self, page=0, tab=None):
        if self.project is None:
            QMessageBox.information(self, "Project Properties", "Open or create a project first.")
            return
        if isinstance(page, bool):   # from a clicked(bool) signal
            page = 0
        dlg = ProjectPropertiesDialog(self.project, self.python, self.output, self, page=page, tab=tab)
        accepted = dlg.exec()
        self.pkg_badge.project_changed(self.project.python(self.python) if self.project else None)
        if accepted:
            self.project = Project(self.project.root)
            self.lang.extra_paths[self.project.root] = sln.all_references(self.project)
            self.lang.warmup(self._lang_context())
            self._update_tool_projects()
            self._update_solution_view()
            self.output.write("Project properties saved.\n", "info")
            # main.py may have changed (startup form)
            main = os.path.join(self.project.root, "main.py")
            for ed in self.editors():
                if isinstance(ed, CodeEditor) and ed.path and os.path.abspath(ed.path) == main and not ed.is_dirty:
                    ed.load(main)

    def open_folder_dialog(self):
        path = QFileDialog.getExistingDirectory(self, "Open Folder",
                                                self.project_root or os.path.expanduser("~"))
        if path:
            self.open_folder(path)

    def open_remote_folder(self):
        """File > Open Remote Folder: a project on another Linux machine, over SSH."""
        from . import components, remote
        if components.missing(["sshfs"]):
            if QMessageBox.question(self, "Open Remote Folder", "Opening a folder over SSH needs sshfs, which "
                                    "isn't installed. Install it now (Ubuntu asks for your password)?") \
                    != QMessageBox.Yes:
                return
            self.install_components(["sshfs"])
            if components.missing(["sshfs"]):
                return
        dlg = remote.OpenRemoteDialog(self)
        if dlg.exec() != QDialog.Accepted or not dlg.cfg:
            return
        self.open_folder(dlg.local_root)
        self.output.write(f"Opened {remote.label(dlg.cfg)} (over SSH). Run, Debug and the Terminal run on "
                          f"{dlg.cfg['host']}.\n", "info")

    def disconnect_remote(self):
        from . import remote
        r = remote.remote_for(self.project_root)
        if r is None:
            return
        self.close_folder()
        remote.unmount(r[1])
        self.statusBar().showMessage(f"Disconnected from {r[0]['host']}", 5000)

    def open_folder(self, path):
        self.dismiss_start_page()
        from . import remote
        if path and remote.remote_for(path) is not None and not remote.ensure_mounted(path, self):
            return
        if not path or not os.path.isdir(path):
            QMessageBox.warning(self, "Open Folder", f"The folder doesn't exist:\n{path or '(empty path)'}")
            return
        if too_big_to_open(path):
            QMessageBox.warning(self, "Open Folder",
                                f"{os.path.abspath(path)} is too big to open as a project - PyStudio would "
                                "scan every file in it and freeze.\n\nOpen the project's own folder instead, "
                                "for example ~/repos/MyProject.")
            return
        path = os.path.abspath(path)
        sln_file = sln.find_solution_file(path)
        self.solution = Solution(sln_file) if sln_file else None
        self.solution_root = path
        if self.tree.model() is None:
            self.tree.setModel(self.proxy)
            for col in (1, 2, 3):
                self.tree.hideColumn(col)
            self.tree.sortByColumn(0, Qt.AscendingOrder)
            self.tree.selectionModel().currentChanged.connect(self._on_tree_current)
        parent_dir = os.path.dirname(path) or path
        self.proxy.set_top(path if parent_dir != path else None, os.path.basename(path))
        src_root = self.fs_model.setRootPath(parent_dir)
        self.tree.setRootIndex(self.proxy.mapFromSource(src_root))
        self.tree.expand(self.proxy.mapFromSource(self.fs_model.index(path)))
        self.proj_label.setVisible(self.proxy.top is None)       # the tree's top node names it now
        self._head_stretch.setVisible(self.proxy.top is not None)
        self.git_panel.set_folder(path)
        active = self.solution.startup_root() if self.solution else path
        self._activate_project(active)
        QTimer.singleShot(0, self.rebuild_reference_index)
        self.task_list.root = path
        if self.tasks_dock.isVisible():
            QTimer.singleShot(0, self.task_list.refresh)
        self.settings.setValue("lastProject", path)
        add_recent(path, self.solution.name if self.solution and len(self.solution.projects()) > 1
                   else (self.project.name if self.project else os.path.basename(path)))
        self._update_title()

    def _activate_project(self, root):
        """Make root the active project: Add Form, Project Properties, IntelliSense, the Error List and
        Test Explorer work on it (like selecting a project in VS's Solution Explorer)."""
        path = os.path.abspath(root)
        self.project_root = path
        # VS-style project file: create one for folders that look like PyStudio projects
        self.project = Project(path)
        looks_like_project = self.project.exists() or projectfit.has_code(path)
        if looks_like_project:
            if "external" not in self.project.data:
                # made outside PyStudio: no PyStudio code in it, and either no .pyproj yet or clearly
                # another toolkit's project (PyQt, forms loaded at runtime)
                self.project["external"] = not projectfit.uses_pystudio(path) and (
                    not self.project.exists() or projectfit.uses_pyqt(path) or projectfit.loads_ui_at_runtime(path))
                if self.project["external"] and not os.path.exists(os.path.join(path, self.project["startup"])):
                    self.project["startup"] = projectfit.find_startup(path) or "main.py"
            if not self.project.exists() or "external" not in self._saved_keys(self.project):
                self.project.save()
            if self.project.external:
                notes = projectfit.cleanup_generated(path)
                notes += projectfit.converted_forms(path)
                if notes:
                    self.output_dock.show()
                    self.output.write(f"{self.project.name} was made outside PyStudio - PyStudio leaves its files "
                                      "as they are:\n  " + "\n  ".join(notes) + "\n", "info")
            self.project.ensure_runtime()
            self._choose_interpreter(self.project)
        else:
            self.project = None
        self.lang.extra_paths[path] = sln.all_references(self.project) if self.project else []
        self._update_solution_view()
        self.server.set_project(self.project, self.project.python(self.python) if self.project else None)
        self.pkg_badge.project_changed(self.project.python(self.python) if self.project else None)
        if self.project:
            mygen.write(self.project)
        self.a_publish.setText(f"&Publish {self.startup_project_name()}..." if self.project else "&Publish...")
        self.b_props.setEnabled(self.project is not None)
        self.lang.warmup(self._lang_context())
        self.refresh_user_controls()
        self._update_tool_projects()
        self.scan_errors()
        self._update_title()

    @staticmethod
    def _saved_keys(project):
        try:
            with open(project.file, encoding="utf-8") as f:
                return set(json.load(f))
        except (OSError, ValueError):
            return set()

    def _choose_interpreter(self, project):
        """Pick a Python that has what the project imports (e.g. PyQt5 from apt is only in the system's
        Python), unless one was chosen in Project Properties > Debug."""
        if project["interpreter"] or project.root in getattr(self, "_interp_checked", set()):
            return
        self._interp_checked = getattr(self, "_interp_checked", set()) | {project.root}
        current = project.python(self.python)
        best, still_missing, was_missing = interp.best_python(project.root, current, self.python)
        if best != current:
            project["interpreter"] = best
            project.save()
            self.lang.warmup(self._lang_context())
            self.output_dock.show()
            self.output.write(f"{project.name} imports {', '.join(was_missing)}, which PyStudio's own Python doesn't "
                              f"have - it runs with {best} (change it in Project Properties > Debug).\n", "info")
        if still_missing:
            apt = [interp.APT_NAMES[m] for m in still_missing if m in interp.APT_NAMES]
            self.output_dock.show()
            self.output.write(f"{project.name} imports {', '.join(still_missing)}, which no Python on this computer "
                              "has. Install it: Project > Manage Packages"
                              + (f", or for the system Python: sudo apt install {' '.join(apt)}" if apt else "")
                              + "\n", "err")

    def offer_module_fix(self, module):
        """A program stopped with ModuleNotFoundError: use a Python that has it, or install it."""
        proj = self.project
        if proj is None:
            return False
        current = proj.python(self.python)
        other = interp.python_with(module, proj.root, current, self.python)
        box = QMessageBox(self)
        box.setWindowTitle("Missing module")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"The program needs <b>{module}</b>, but the Python it ran with doesn't have it:<br>{current}")
        b_use = b_pip = None
        if other:
            box.setInformativeText(f"{other} has {module} - use that Python for {proj.name}? "
                                   "(Project Properties > Debug)")
            b_use = box.addButton(f"Use {other}", QMessageBox.AcceptRole)
        else:
            apt = interp.APT_NAMES.get(module)
            box.setInformativeText(f"Install it with pip ({interp.pip_name(module)})"
                                   + (f", or for the system Python:  sudo apt install {apt}" if apt else "") + ".")
            b_pip = box.addButton(f"Install {interp.pip_name(module)}", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() not in (b_use, b_pip) or box.clickedButton() is None:
            return True                                # shown and canceled: no second message
        if box.clickedButton() is b_use and b_use is not None:
            if self.session.active:
                self.session.stop()
            proj["interpreter"] = other
            proj.save()
            self._update_tool_projects()
            self.lang.warmup(self._lang_context())
            self.output.write(f"{proj.name} now runs with {other}. Press F5 to start again.\n", "info")
            return True
        if box.clickedButton() is b_pip and b_pip is not None:
            if self.session.active:
                self.session.stop()
            self.output_dock.show()
            self.output.write(f"> {current} -m pip install {interp.pip_name(module)}\n", "info")
            from PySide6.QtCore import QProcess
            p = QProcess(self)
            p.setProcessChannelMode(QProcess.MergedChannels)
            p.readyRead.connect(lambda: self.output.write(bytes(p.readAll()).decode("utf-8", "replace")))
            p.finished.connect(lambda code, _s: self.output.write(
                ("Installed. Press F5 to start again.\n" if code == 0 else
                 "pip couldn't install it (see above)."
                 + (f" For the system Python use apt: sudo apt install {interp.APT_NAMES[module]}"
                    if module in interp.APT_NAMES else "") + "\n"), "info" if code == 0 else "err"))
            p.start(current, ["-m", "pip", "install", "--disable-pip-version-check", interp.pip_name(module)])
            return True
        return False

    def _check_run_output_for_missing(self, _code=0):
        """Ctrl+F5 run ended: a ModuleNotFoundError gets the same offer as under the debugger."""
        tail = self.output.toPlainText()[-3000:]
        m = re.search(r"ModuleNotFoundError: No module named '([\w.]+)'", tail)
        if m:
            QTimer.singleShot(0, lambda: self.offer_module_fix(m.group(1).split(".")[0]))

    def _is_external(self, path):
        root = (self.solution.project_for(path) if self.solution else None) or self.project_root
        if root and path and os.path.abspath(path).startswith(root + os.sep):
            p = self.project if (self.project and self.project.root == root) else Project(root)
            return p.external
        return False

    def _update_solution_view(self):
        """Solution Explorer header and project folders (bold = startup project)."""
        name = os.path.basename(self.solution_root or "")
        projects = self.solution.projects() if self.solution else ([self.project_root] if self.project_root else [])
        if self.solution and len(projects) > 1:
            startup = Project(self.solution.startup_root()).name
            active = self.project.name if self.project else "-"
            self.proj_label.setText(f"&nbsp;&nbsp;Solution '{self.solution.name}' ({len(projects)} projects)<br>"
                                    f"<span style='font-weight: normal; font-size: small;'>&nbsp;&nbsp;▶ Startup: "
                                    f"{startup}&nbsp;&nbsp;&nbsp;Active: {active}</span>")
            self.proj_label.setToolTip(f"Active project: {self.project.name if self.project else '-'}\n"
                                       f"Startup project: {Project(self.solution.startup_root()).name}")
            self.explorer_dock.setWindowTitle(f"Solution Explorer - {self.solution.name}")
        else:
            self.proj_label.setText(f"&nbsp;&nbsp;{self.project.name}" if self.project else f"&nbsp;&nbsp;{name}")
            self.proj_label.setToolTip("")
            self.explorer_dock.setWindowTitle(f"Solution Explorer - {name}")
        self.proxy.set_solution(self.solution, self.solution_root,
                                self.solution.startup_root() if self.solution else None)
        if self.solution_root and self.proxy.top:
            if self.solution and len(projects) > 1:
                self.proxy.set_top(self.solution_root, f"Solution '{self.solution.name}' ({len(projects)} projects)",
                                   "Right-click: Add Project, Close Solution...", True)
            else:
                title = self.project.name if self.project else name
                kind = " (external)" if self.project is not None and self.project.external else ""
                self.proxy.set_top(self.solution_root, title + kind,
                                   f"{self.solution_root}\nRight-click for project commands; Alt+Enter: Properties"
                                   if self.project else self.solution_root)
            self.tree.expand(self.proxy.mapFromSource(self.fs_model.index(self.solution_root)))
        self.tree.viewport().update()
        self.class_view.set_projects([(Project(r).name if sln.is_project_dir(r) else os.path.basename(r), r)
                                      for r in projects], self.solution_root)

    def startup_root(self):
        if self.solution:
            return self.solution.startup_root()
        return self.project_root

    def startup_project_name(self):
        root = self.startup_root()
        return Project(root).name if root else ""

    def _maybe_activate_for(self, path):
        """Switch the active project when you work in another project's file."""
        if not self.solution or not path:
            return
        root = self.solution.project_for(path)
        if root and root != self.project_root:
            self._activate_project(root)

    def _on_tree_current(self, current, _previous):
        if current.isValid():
            self._maybe_activate_for(self._path_at(current))

    # ---------------------------------------------------------------- Task List
    def _tasklist_init(self):
        from .tasklist import TaskList
        self.task_list = TaskList()
        self.task_list.open_texts = lambda: {os.path.abspath(e.path): e.toPlainText() for e in self.editors()
                                             if isinstance(e, CodeEditor) and e.path}
        self.task_list.open_location.connect(self.open_file_at)
        self.tasks_dock = QDockWidget("Task List", self)
        self.tasks_dock.setObjectName("TaskList")
        self.tasks_dock.setWidget(self.task_list)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.tasks_dock)
        self.tabifyDockWidget(self.bookmarks_dock, self.tasks_dock)
        self.tasks_dock.hide()

    # ---------------------------------------------------------------- Terminal
    def _terminal_init(self):
        from .terminal import TerminalPanel
        self.terminal = TerminalPanel()
        self.terminal.context = self._terminal_context
        self.terminal.install_requested.connect(self._install_pyte)
        self.terminal_dock = QDockWidget("Terminal", self)
        self.terminal_dock.setObjectName("Terminal")
        self.terminal_dock.setWidget(self.terminal)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.terminal_dock)
        self.tabifyDockWidget(self.tests_dock, self.terminal_dock)
        self.terminal_dock.hide()
        self.a_terminal = self._act("&Terminal", self.show_terminal)
        self.a_terminal.setShortcut("Ctrl+`")
        self.a_new_terminal = self._act("New Terminal", lambda: self.show_terminal(new=True))
        self.a_new_terminal.setShortcut("Ctrl+Shift+`")
        self.addAction(self.a_new_terminal)

    def _terminal_context(self):
        """(folder, environment, description) for a new terminal: the active project, its Python first on PATH
        (its .venv activated) and referenced projects on PYTHONPATH."""
        root = self.project_root or self.solution_root or os.path.expanduser("~")
        env, label = {}, ""
        if self.project is not None:
            py = self.project.python(self.python)
            bindir = os.path.dirname(os.path.abspath(py))
            venv = os.path.dirname(bindir)
            path = os.environ.get("PATH", "")
            if os.path.exists(os.path.join(venv, "pyvenv.cfg")):
                env.update({"VIRTUAL_ENV": venv, "PATH": bindir + os.pathsep + path})
                env["VIRTUAL_ENV_PROMPT"] = os.path.basename(venv)
                label = f"Python: {py} (venv {os.path.basename(venv)} active)"
            else:
                label = f"Python: {py}"
            env.update(sln.python_path_env(self.project))
            env["PYSTUDIO_PROJECT"] = self.project.root
        return root, env, label

    def show_terminal(self, new=False, cwd=None):
        self.terminal_dock.show()
        self.terminal_dock.raise_()
        t = self.terminal.new_terminal(cwd) if (new or cwd or self.terminal.current() is None) \
            else self.terminal.current()
        if t is not None:
            t.setFocus()
        return t

    def _install_pyte(self):
        from PySide6.QtCore import QProcess
        self.output_dock.show()
        self.output.write("Installing pyte for PyStudio's Terminal...\n", "info")
        p = QProcess(self)
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.readyReadStandardOutput.connect(lambda: self.output.write(
            bytes(p.readAllStandardOutput()).decode("utf-8", "replace")))

        def done(code, _s):
            from .terminal import available
            if code == 0 and available():
                self.terminal.set_available()
                self.output.write("pyte installed.\n", "info")
                self.show_terminal(new=True)
            else:
                self.output.write("Couldn't install pyte. In a terminal: " + sys.executable +
                                  " -m pip install pyte\n", "err")
        p.finished.connect(done)
        p.start(sys.executable, ["-m", "pip", "install", "--disable-pip-version-check", "pyte"])

    # ---------------------------------------------------------------- PyStudio's own log
    def view_log(self):
        from . import errorlog
        if not os.path.exists(errorlog.LOG_FILE):
            QMessageBox.information(self, "Log", "Nothing has been logged yet.")
            return
        ed = self.open_file(errorlog.LOG_FILE)
        if isinstance(ed, CodeEditor):
            ed.setReadOnly(True)
            ed.moveCursor(ed.textCursor().MoveOperation.End)
            ed.ensureCursorVisible()

    def check_for_updates(self):
        from .updater import UpdatesDialog
        UpdatesDialog(self).exec()

    def copy_system_info(self):
        from . import components, errorlog
        info = errorlog.system_info()
        comps = ", ".join(f"{c[1]} {'yes' if components.is_installed(c[0]) else 'no'}" for c in components.COMPONENTS)
        QApplication.clipboard().setText(f"{info}\nComponents: {comps}")
        self.statusBar().showMessage("Copied: " + info, 6000)

    def report_previous_crash(self, report):
        from . import errorlog
        box = QMessageBox(QMessageBox.Warning, "PyStudio closed unexpectedly",
                          "PyStudio didn't close normally last time. Files you had saved are fine; unsaved changes "
                          "may be lost.\n\nIf it happens again, Copy Details and send them to get it fixed.")
        box.setDetailedText(f"{errorlog.system_info()}\n\n{report}")
        b_copy = box.addButton("Copy Details", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is b_copy:
            QApplication.clipboard().setText(f"{errorlog.system_info()}\n\n{report}")

    def install_components(self, preselect=None):
        """Tools > Install Components: Git, GitHub CLI, Wine, NSIS, unixODBC."""
        from .components import InstallDialog
        InstallDialog(preselect, self).exec()

    def show_class_view(self):
        self.class_dock.show()
        self.class_dock.raise_()
        self.class_view.search.setFocus()

    def show_task_list(self):
        self.task_list.root = self.solution_root
        self.task_list.refresh()
        self.tasks_dock.show()
        self.tasks_dock.raise_()

    # ---------------------------------------------------------------- solutions
    def _ensure_solution(self):
        """Turn the open project into a solution (its folder gets <Name>.pysln) when a 2nd project is added."""
        if self.solution is None:
            root = self.solution_root
            name = Project(root).name if sln.is_project_dir(root) else os.path.basename(root)
            self.solution = Solution.create(root, name, ["."] if sln.is_project_dir(root) else [], ".")
        return self.solution

    def add_new_project_to_solution(self):
        self.new_project(add_to_solution=bool(self.solution_root))

    def add_existing_project(self):
        if not self.solution_root:
            QMessageBox.information(self, "Add Existing Project", "Open a project or solution first.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Add Existing Project", self.solution_root,
                                              "PyStudio projects (*.pyproj)")
        if not path:
            return
        root = os.path.dirname(os.path.abspath(path))
        if not (root == self.solution_root or root.startswith(self.solution_root + os.sep)):
            dest = os.path.join(self.solution_root, os.path.basename(root))
            if os.path.exists(dest):
                QMessageBox.warning(self, "Add Existing Project", f"{dest} already exists.")
                return
            if QMessageBox.question(self, "Add Existing Project",
                                    f"{os.path.basename(root)} is outside the solution folder.\n\nCopy it into "
                                    f"{self.solution_root}? (The original stays where it is.)") != QMessageBox.Yes:
                return
            import shutil
            shutil.copytree(root, dest, ignore=shutil.ignore_patterns(".venv", "venv", "__pycache__", "publish",
                                                                      "build", "dist", ".git"))
            root = dest
        self._ensure_solution().add(root)
        self._update_solution_view()
        self.output.write(f"Added project {Project(root).name} to the solution\n", "info")

    def remove_from_solution(self, root):
        if not self.solution or root == self.solution_root:
            return
        name = Project(root).name
        if QMessageBox.question(self, "Remove from Solution",
                                f"Remove {name} from the solution?\n\nIts files stay on disk; references to it "
                                "from other projects are removed.") != QMessageBox.Yes:
            return
        self.solution.remove(root)
        for other in self.solution.projects():
            p = Project(other)
            refs = sln.references(p)
            if root in refs:
                sln.set_references(p, [r for r in refs if r != root])
                self.lang.extra_paths[other] = sln.all_references(p)
        if self.project_root == root or (self.project_root or "").startswith(root + os.sep):
            self._activate_project(self.solution.startup_root())
        else:
            self._update_solution_view()

    def set_startup_project(self, root=None):
        root = root or self.project_root
        if not root:
            return
        if self.solution is None:
            self.statusBar().showMessage(f"{os.path.basename(root)} is the only project - it's the startup "
                                         "project.", 4000)
            return
        self.solution.set_startup(root)
        self._update_solution_view()
        self.a_publish.setText(f"&Publish {self.startup_project_name()}...")
        self.statusBar().showMessage(f"Startup project: {Project(root).name} (F5 runs it)", 4000)

    def add_reference(self, root=None):
        root = root or self.project_root
        if not root or not sln.is_project_dir(root):
            QMessageBox.information(self, "Add Reference", "Select a project first.")
            return
        if self.solution is None or len(self.solution.projects()) < 2:
            QMessageBox.information(self, "Add Reference",
                                    "A project can reference other projects in its solution.\n\nAdd one first: "
                                    "File > Add > New Project (for example a Class Library).")
            return
        proj = Project(root)
        dlg = sln.ReferenceManager(proj, self.solution, self)
        if dlg.exec() != QDialog.Accepted:
            return
        sln.set_references(proj, dlg.checked())
        self.lang.extra_paths[root] = sln.all_references(proj)
        if root == self.project_root:
            self.project = Project(root)
            self.lang.warmup(self._lang_context())
            self._update_tool_projects()
        refs = ", ".join(os.path.basename(r) for r in sln.references(proj)) or "none"
        self.output.write(f"{proj.name} references: {refs}\n", "info")

    def close_folder(self):
        self.ref_index.clear()
        self.solution = None
        self.solution_root = None
        self.proxy.set_solution(None, None, None)
        self.proxy.set_top(None, "")
        self.class_view.set_projects([])
        self.project_root = None
        self.project = None
        self.pkg_badge.project_changed(None)
        self.proj_label.setText("  (no project)")
        self.server.set_project(None)
        self.git_panel.set_folder(None)
        self.immediate.set_project(self.python, None)
        self.tests.set_project(None, None)
        self.tree.setModel(None)
        self.explorer_dock.setWindowTitle("Solution Explorer")
        self.settings.remove("lastProject")
        self._update_title()

    def _fill_recent_menu(self):
        self.recent_menu.clear()
        recent = load_recent()[:10]
        if not recent:
            self.recent_menu.addAction("(none)").setEnabled(False)
        for i in recent:
            self.recent_menu.addAction(f"{i['name']}   ({i['path']})", lambda p=i["path"]: self.open_folder(p))

    def _path_at(self, proxy_index):
        return self.fs_model.filePath(self.proxy.mapToSource(proxy_index))

    def _on_tree_double_click(self, index):
        path = self._path_at(index)
        if path.endswith(".pyproj"):
            self.project_properties()
        elif os.path.isfile(path):
            self.open_file(path)

    def _tree_context_menu(self, pos):
        if not self.project_root:
            return
        index = self.tree.indexAt(pos)
        path = self._path_at(index) if index.isValid() else self.project_root
        folder = path if os.path.isdir(path) else os.path.dirname(path)

        menu = QMenu(self)
        proj_dirs = set(self.solution.projects()) if self.solution else {self.solution_root}
        target = os.path.abspath(path) if (index.isValid() and os.path.isdir(path)) else None
        if not index.isValid():
            target = self.solution_root if self.solution_root in proj_dirs else None
        if target in proj_dirs:
            pname = Project(target).name
            if self.solution and len(proj_dirs) > 1:
                a = menu.addAction("Set as Startup Project", lambda: self.set_startup_project(target))
                a.setEnabled(target != self.solution.startup_root())
            menu.addAction("Add Reference...", lambda: self.add_reference(target))
            menu.addAction(f"Properties ({pname})", lambda: (self._activate_project(target),
                                                              self.project_properties()))
            if self.solution and target != self.solution_root:
                menu.addAction("Remove from Solution", lambda: self.remove_from_solution(target))
            menu.addSeparator()
        is_top = index.isValid() and os.path.abspath(path) == self.solution_root
        if is_top or target in proj_dirs:
            where = target or self.solution_root
            menu.addAction("Open in Terminal", lambda: self.open_terminal(where))
            menu.addAction("Open in External Terminal", lambda: self.open_external_terminal(where))
            menu.addAction("Open Folder in Files", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(where)))
            menu.addAction("Copy Full Path", lambda: QApplication.clipboard().setText(where))
            if where in proj_dirs and sln.is_project_dir(where):
                menu.addAction("Publish...", lambda: (self._activate_project(where), self.publish_project()))
                menu.addAction("Publish for Windows...", lambda: self.publish_windows(where))
            if is_top:
                menu.addAction("Close Solution", self.close_solution)
            menu.addSeparator()
        sub = menu.addMenu("Add Project")
        sub.addAction("New Project...", self.add_new_project_to_solution)
        sub.addAction("Existing Project...", self.add_existing_project)
        if index.isValid() and path.endswith(".py") and os.path.isfile(path) and self.project is not None \
                and path.startswith(self.project.root + os.sep):
            rel = os.path.relpath(path, self.project.root).replace(os.sep, "/")
            a = menu.addAction("Set as Startup File (F5 runs it)", lambda: self._set_startup_file(rel))
            a.setEnabled(rel != self.project["startup"])
            menu.addSeparator()
        if index.isValid() and path.endswith(".ui.bak") and os.path.isfile(path):
            menu.addAction(f"Restore Backup (put back {os.path.basename(path)[:-4]})",
                           lambda: self._restore_ui_backup(path))
            menu.addSeparator()
        if index.isValid() and os.path.isfile(path):
            py, ui = self._form_pair(path)
            if os.path.exists(ui):
                menu.addAction("View Designer", lambda: self.open_file(ui))
            if path.endswith(".ui") and os.path.exists(py):
                menu.addAction("View Code", lambda: self.open_file(py))
            if path.endswith(".ui"):
                menu.addAction("Open With Qt Designer", lambda: self.qt_designer.open(path))
            if os.path.exists(ui):
                menu.addSeparator()
        add = menu.addMenu("Add")
        add.addAction("Windows Form...", lambda: self.add_form_dialog(folder))
        add.addAction("New Query...", lambda: self.add_query_file())
        menu.addAction("New Python File...", lambda: self._tree_new_file(folder))
        menu.addAction("New Folder...", lambda: self._tree_new_folder(folder))
        if index.isValid() and not is_top:
            menu.addSeparator()
            if os.path.isfile(path) and not path.endswith(".ui"):
                menu.addAction("Open", lambda: self.open_file(path))
            menu.addAction("Rename...", lambda: self._tree_rename(path))
            menu.addAction("Delete (move to Trash)", lambda: self._tree_delete(path))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def open_terminal(self, folder):
        """PyStudio's Terminal in folder (the desktop's terminal when the Terminal can't run)."""
        from .terminal import available
        if available():
            return self.show_terminal(cwd=folder) is not None
        return self.open_external_terminal(folder)

    def open_external_terminal(self, folder):
        """The desktop's terminal, in folder (Ubuntu: gnome-terminal / ptyxis via x-terminal-emulator)."""
        import shutil
        from PySide6.QtCore import QProcess
        for prog in ("x-terminal-emulator", "ptyxis", "gnome-terminal", "konsole", "xfce4-terminal",
                     "mate-terminal", "tilix", "kitty", "alacritty", "xterm"):
            exe = shutil.which(prog)
            if exe and QProcess.startDetached(exe, [], folder)[0]:
                return True
        QMessageBox.information(self, "Open in Terminal", f"No terminal program was found.\n\nFolder:\n{folder}")
        return False

    def _tree_new_file(self, folder):
        name, ok = QInputDialog.getText(self, "New Python File", "File name:", text="module.py")
        name = name.strip()
        if not ok or not name:
            return
        if "." not in name:
            name += ".py"
        path = os.path.join(folder, name)
        if os.path.exists(path):
            QMessageBox.warning(self, "New File", f"{name} already exists.")
            return
        open(path, "w", encoding="utf-8").close()
        self.open_file(path)

    def _tree_new_folder(self, folder):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        name = name.strip()
        if ok and name:
            os.makedirs(os.path.join(folder, name), exist_ok=True)

    def _tree_rename(self, path):
        old = os.path.basename(path)
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=old)
        name = name.strip()
        if not ok or not name or name == old:
            return
        new = os.path.join(os.path.dirname(path), name)
        try:
            os.rename(path, new)
        except OSError as e:
            QMessageBox.critical(self, "Rename failed", str(e))
            return
        for ed in self.editors():  # keep open tabs pointing at the right file
            if ed.path and (ed.path == path or ed.path.startswith(path + os.sep)):
                ed.path = new + ed.path[len(path):]
                self._update_tab_text(ed)

    def _tree_delete(self, path):
        name = os.path.basename(path)
        if QMessageBox.question(self, "Delete", f"Move '{name}' to the Trash?") != QMessageBox.Yes:
            return
        if not QFile.moveToTrash(path):
            QMessageBox.critical(self, "Delete failed", f"Could not move {name} to the Trash.")
            return
        for ed in list(self.editors()):
            if ed.path and (ed.path == path or ed.path.startswith(path + os.sep)):
                self.tabs.removeTab(self.tabs.indexOf(ed))
                ed.deleteLater()

    # ================================================================== edit
    # ================================================================== data
    def _query_docs(self):
        return [ed for ed in self.editors() if isinstance(ed, QueryDocument)]

    def new_query(self, conn="", sql="", run=False):
        """An untitled query tab (Server Explorer: New Query / Show Table Data)."""
        if self.project is None:
            QMessageBox.information(self, "New Query", "Open or create a project first.")
            return None
        names = {ed.display_name().lower() for ed in self.editors()}
        n = 1
        while f"query{n}.sql" in names or os.path.exists(os.path.join(self.project.root, "queries", f"Query{n}.sql")):
            n += 1
        if not conn and self.project.data.get("connections"):
            conn = self.project.data["connections"][0]["name"]
        doc = QueryDocument(self.data, self.project, None, conn, sql)
        doc.untitled_name = f"Query{n}.sql"
        self._add_editor(doc)
        if run:
            doc.execute()
        return doc

    def add_report(self):
        """Project > Add Report: reports/<Name>.psrpt from a saved query, laid out as a table to start with."""
        if self.project is None:
            QMessageBox.information(self, "Add Report", "Open or create a project first.")
            return
        queries = sorted(mygen.query_files(self.project.root))
        n = 1
        while os.path.exists(os.path.join(self.project.root, "reports", f"Report{n}.psrpt")):
            n += 1
        name, ok = QInputDialog.getText(self, "Add Report", "Report name:", text=f"Report{n}")
        name = re.sub(r"[^A-Za-z0-9_]", "", name or "")
        if not ok or not name:
            return
        if os.path.exists(os.path.join(self.project.root, "reports", name + ".psrpt")):
            QMessageBox.warning(self, "Add Report", f"reports/{name}.psrpt already exists.")
            return
        source = ""
        if queries:
            source, ok = QInputDialog.getItem(self, "Add Report", "Data (a saved query):", ["(none yet)"] + queries,
                                              1, False)
            if not ok:
                return
            source = "" if source == "(none yet)" else source
        from .reportdesigner import create_report
        path = create_report(self.project.root, name, source)
        d = self.open_file(path)
        if d is not None and source:
            d.load_fields(then=lambda: d.auto_layout(force=True) if d.columns else None)
        self.output.write(f"Added reports/{name}.psrpt. In code:  show_report(\"{name}\")  previews it, "
                          f"export_report(\"{name}\", \"file.pdf\") saves a PDF.\n", "info")

    def _notebook_context(self):
        """(python, environment, description) for a notebook's kernel: the project's Python (its .venv), with
        referenced projects on PYTHONPATH - like the Terminal."""
        _root, env, label = self._terminal_context()
        python = self.project.python(self.python) if self.project is not None else self.python
        return python, env, label or f"Python: {python}"

    def add_notebook(self, name=None):
        """Project > Add New Notebook: <Name>.ipynb in the project (or a folder you pick)."""
        from .notebook import new_notebook
        if self.project is not None:
            folder = self.project.root
        else:
            folder = QFileDialog.getExistingDirectory(self, "Folder for the notebook", os.path.expanduser("~"))
            if not folder:
                return None
        n = 1
        while os.path.exists(os.path.join(folder, f"Notebook{n}.ipynb")):
            n += 1
        if name is None:
            name, ok = QInputDialog.getText(self, "Add New Notebook", "Name:", text=f"Notebook{n}")
            if not ok or not name.strip():
                return None
        name = re.sub(r"[^\w\- ]", "", name.strip()) or f"Notebook{n}"
        path = os.path.join(folder, name + ".ipynb")
        if os.path.exists(path):
            QMessageBox.warning(self, "Add New Notebook", f"{name}.ipynb already exists.")
            return self.open_file(path)
        new_notebook(path)
        return self.open_file(path)

    def add_query_file(self, conn="", name="", sql=""):
        """Project > Add New Query: queries/<Name>.sql, opened in the Query Designer."""
        if self.project is None:
            QMessageBox.information(self, "Add New Query", "Open or create a project first.")
            return None
        folder = os.path.join(self.project.root, "queries")
        base = mygen.ident(name or "Query")
        default, n = base if name else "Query1", 2
        while os.path.exists(os.path.join(folder, default + ".sql")):
            default = f"{base}{n}"
            n += 1
        text, ok = QInputDialog.getText(self, "Add New Query",
                                        "Query name (use it in code as my.queries.<name>()):", text=default)
        text = text.strip()
        if not ok or not text:
            return None
        if text.lower().endswith(".sql"):
            text = text[:-4]
        if not mygen.valid_name(text):
            QMessageBox.warning(self, "Add New Query", f"{text!r} isn't a valid name (letters, digits and _).")
            return None
        path = os.path.join(folder, text + ".sql")
        if os.path.exists(path):
            QMessageBox.warning(self, "Add New Query", f"{text}.sql already exists.")
            return None
        if not conn and self.project.data.get("connections"):
            conn = self.project.data["connections"][0]["name"]
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(dbcore.format_query_file(conn, sql or "SELECT *\nFROM "))
        mygen.write(self.project)
        doc = self.open_file(path)
        if doc is not None and not sql:
            c = doc.editor.textCursor()
            c.movePosition(QTextCursor.End)
            c.movePosition(QTextCursor.PreviousCharacter)
            doc.editor.setTextCursor(c)
        return doc

    def _on_query_saved(self, path):
        if self.project and os.path.abspath(path).startswith(self.project.root + os.sep):
            mygen.write(self.project)

    def _on_connections_changed(self):
        if self.project:
            mygen.write(self.project)
        for doc in self._query_docs():
            doc.reload_connections()

    def go_to_line(self):
        ed = self.current_editor()
        if not ed:
            return
        line, ok = QInputDialog.getInt(self, "Go To Line", f"Line number (1 - {ed.blockCount()}):",
                                       ed.textCursor().blockNumber() + 1, 1, ed.blockCount())
        if ok:
            ed.go_to_line(line)

    # =================================================================== run
    def _prepare_launch(self):
        """Save, check errors, regenerate forms. Returns (python, script, cwd, args, env, root) or None."""
        if not self.save_all():
            return None
        if self.project_root:
            self.scan_errors()
            n = len(self.error_list.errors())
            if n:
                r = QMessageBox.question(
                    self, "PyStudio", f"There {'is' if n == 1 else 'are'} {n} error{'s' if n != 1 else ''} "
                    "in the Error List. Run anyway?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if r != QMessageBox.Yes:
                    self.errors_dock.show()
                    self.errors_dock.raise_()
                    return None
        script = None
        run_root = self.startup_root()
        start_rel = "main.py"
        if run_root and sln.is_project_dir(run_root):
            start_rel = Project(run_root)["startup"] or "main.py"
        if run_root and os.path.isfile(os.path.join(run_root, start_rel)):
            script = os.path.join(run_root, start_rel)
        else:
            ed = self.current_editor()
            if not ed:
                QMessageBox.information(self, "Run", "Which file should run? Open it, or right-click it in "
                                        "Solution Explorer > Set as Startup File.")
                return None
            if not ed.path and not self._save(ed):
                return None
            script = ed.path
        root = run_root if (run_root and script.startswith(run_root + os.sep)) else os.path.dirname(script)
        self.output_dock.show()
        # Make sure every form's ui_*.py matches its .ui before running (all projects of a solution)
        in_solution = bool(self.solution_root and script.startswith(self.solution_root + os.sep))
        external = bool(run_root and self._is_external(os.path.join(run_root, "x")))
        errors = [] if external else \
            [m for ok, m in compile_all_stale(self.solution_root if in_solution else root) if not ok]
        proj = None
        if run_root and script.startswith(run_root + os.sep) and sln.is_project_dir(run_root):
            proj = self.project if (self.project and self.project.root == run_root) else Project(run_root)
        for m in errors:
            self.output.write(m + "\n", "err")
        if proj:
            proj.ensure_runtime()
            mygen.write(proj)
            env = proj.env()
            for c in proj.data.get("connections", []):
                pw = self.data.known_password(c.get("name"))
                if pw is not None and dbcore.needs_password(c):   # don't ask again in the app
                    env["PYSTUDIO_PWD_" + re.sub(r"\W", "_", c["name"]).upper()] = pw
            if hot_reload_enabled():
                env["PYSTUDIO_HOT_RELOAD"] = "1"   # designer changes show up in the running program
            env.update(sln.python_path_env(proj, env))  # referenced projects can be imported
            for ref in sln.all_references(proj):
                Project(ref).ensure_runtime()
            debug_root = self.solution_root if (self.solution and in_solution) else root  # Just My Code
            cwd = proj.working_dir()
            if proj.external and not proj["workingDir"]:
                cwd = os.path.dirname(script)       # a script in app/ runs from app/, like in a terminal
                debug_root = run_root
            return proj.python(self.python), script, cwd, proj.run_args(), env, debug_root
        return self.python, script, root, [], {}, root

    # ---------------------------------------------------------------- Hot Reload
    def _hot_reload_soon(self, designer):
        """While the program runs, a changed form is saved shortly after each edit, so the running
        program updates (VS's Hot Reload)."""
        if not (self.runner.is_running or self.session.active) or not hot_reload_enabled():
            return
        if not hasattr(self, "_hot_timer"):
            self._hot_timer = QTimer(self)
            self._hot_timer.setSingleShot(True)
            self._hot_timer.setInterval(600)
            self._hot_timer.timeout.connect(self._hot_reload_now)
            self._hot_targets = set()
        self._hot_targets.add(designer)
        self._hot_timer.start()

    def _hot_reload_now(self):
        targets, self._hot_targets = self._hot_targets, set()
        for d in targets:
            try:
                if d.is_dirty:
                    self._save(d)
            except RuntimeError:
                pass                                   # the tab was closed
        if targets and (self.runner.is_running or self.session.active):
            self.statusBar().showMessage("Hot Reload: form changes sent to the running program", 3000)

    def run_current(self):
        """Ctrl+F5: Start Without Debugging."""
        if self.session.active or self.runner.is_running:
            return
        launch = self._prepare_launch()
        if launch:
            python, script, cwd, args, env, _root = launch
            self.runner.run(python, script, cwd, args, env)

    # ================================================================ debugging
    def f5(self):
        """F5: Start Debugging, or Continue when paused (like VS). In a query: Execute."""
        if isinstance(self.current_doc(), QueryDocument) and not self.session.active:
            self.current_doc().execute()
            return
        if self.session.active:
            if self.session.paused:
                self.session.resume("continue")
            return
        if not self.runner.is_running:
            self.start_debugging()

    def start_debugging(self, stop_on_entry=False, tests=None):
        """tests: pytest node ids to debug instead of the program (Test Explorer > Debug)."""
        launch = self._prepare_launch()
        if not launch:
            return
        python, script, cwd, args, env, root = launch
        self._last_tests = tests
        if tests is not None:
            cwd = self.project_root or root
            ignore = [f"--ignore={d}" for d in sln.nested_projects(cwd)]
            script, args = PYTEST_SHIM, ["-q", "-s", "-p", "no:cacheprovider", "-o", "pythonpath=."] + ignore + list(tests)
            if self.project is not None:
                env = dict(env)
                env.update(sln.python_path_env(self.project, env))
        self._sync_breakpoints_from_editors()
        self._last_launch = stop_on_entry
        for dk in self.debug_docks:
            dk.show()                 # back where they were (a tab at the bottom unless moved)
        self.output_dock.raise_()     # program output while it runs; Locals comes up when it pauses
        self._raise_locals = True
        self.locals_win.clear()
        self.stack_win.clear()
        self._enc_changed = set()
        self.session.before_resume = self.apply_code_changes
        self.session.start(python, script, cwd, args, env, root, self._breakpoints_for_session(),
                           stop_on_entry)
        proc = getattr(self.session, "proc", None)
        self.diag_tools.start(None if getattr(self.session, "remote", None) is not None or proc is None
                              else proc.processId())
        self._update_run_state()

    def attach_to_process(self):
        """Debug > Attach to Process: debug a Python program that's already running."""
        from .attach import AttachDialog
        if self.session.active:
            QMessageBox.information(self, "Attach to Process", "Stop debugging first.")
            return
        dlg = AttachDialog(self)
        if dlg.exec() != QDialog.Accepted or dlg.selected() is None:
            return
        self._sync_breakpoints_from_editors()
        self._last_tests = None
        for dk in self.debug_docks:
            dk.show()
        self.output_dock.raise_()
        self._raise_locals = True
        self.locals_win.clear()
        self.stack_win.clear()
        root = self.project_root or os.path.dirname(dlg.selected()["cmd"].split()[-1])
        self.session.attach(dlg.selected(), root, self._breakpoints_for_session())
        self._update_run_state()

    def restart_debugging(self):
        if self.session.active:
            self._restart_pending = True
            self.session.stop()

    def stop_all(self):
        self.runner.stop()
        self.session.stop()
        if self.profile_proc is not None:
            self.profile_proc.terminate()

    # ---------------------------------------------------------------- Edit and Continue
    def _enc_mark(self, ed):
        if self.session.active and ed.path and ed.path.endswith((".py", ".pyw")):
            if not hasattr(self, "_enc_changed"):
                self._enc_changed = set()
            self._enc_changed.add(ed)

    def apply_code_changes(self):
        """Edit and Continue: the functions you changed while paused go into the running program
        (from their next call). Runs before Continue / Step, or with Debug > Apply Code Changes."""
        if not (self.session.active and self.session.paused):
            return
        changed, self._enc_changed = getattr(self, "_enc_changed", set()), set()
        for ed in list(changed):
            try:
                path, text = ed.path, ed.toPlainText()
            except RuntimeError:
                continue                      # the tab was closed
            running = (self.session.stack[0].get("name") if self.session.stack else "") or ""

            def done(msg, path=path, running=running):
                name = os.path.basename(path)
                if msg.get("error"):
                    self.output.write(f"Edit and Continue ({name}): {msg['error']}\n", "err")
                    return
                applied, added, skipped = msg.get("applied", []), msg.get("added", []), msg.get("skipped", [])
                if applied or added:
                    parts = (["updated " + ", ".join(applied)] if applied else []) + \
                        (["added " + ", ".join(added)] if added else [])
                    self.output.write(f"Edit and Continue ({name}): {'; '.join(parts)}.\n", "info")
                    if running and any(a == running or a.endswith("." + running) for a in applied):
                        self.output.write(f"  {running} is running right now: it keeps its old code until it's "
                                          "called again.\n", "info")
                    self.statusBar().showMessage("Code changes applied", 3000)
                for label, why in skipped:
                    self.output.write(f"Edit and Continue: {label} - {why}.\n", "err")
                if not (applied or added or skipped):
                    self.output.write(f"Edit and Continue ({name}): no function changed (changes outside "
                                      "functions need a restart).\n", "info")
            self.session.request({"cmd": "applyEdits", "file": path, "source": text}, done)

    def step(self, how):
        if self.session.active:
            if self.session.paused:
                self.session.resume(how)
        elif how in ("next", "step") and not self.runner.is_running:
            self.start_debugging(stop_on_entry=True)   # F10/F11 starts and stops at the first line

    def _clear_exec_markers(self):
        for ed in self.editors():
            if isinstance(ed, CodeEditor):
                if ed.exec_line:
                    ed.set_exec_line(None)
                ed.debug_eval = None

    def _on_debug_stopped(self, msg):
        self._update_run_state()
        run_to = getattr(self, "_run_to", None)
        if run_to:                                   # the Run To Cursor stop: remove its temporary breakpoint
            self._run_to = None
            self.session.set_breakpoints(run_to[0], self.breakpoints.get(run_to[0], []))
        if getattr(self, "_raise_locals", False):
            self._raise_locals = False
            self.locals_dock.raise_()     # first pause: show Locals (later, keep the tab you picked)
        stack = msg.get("stack", [])
        top = next((fr for fr in stack if fr.get("user")), stack[0] if stack else None)
        self.current_frame = top["id"] if top else 0
        self.stack_win.current = self.current_frame
        self.stack_win.load(stack, self.project_root)
        self.threads_win.load(msg.get("threads", []), msg.get("thread"), self.project_root)
        self.threads_dock.setWindowTitle(f"Threads ({len(msg.get('threads', []))})")
        self.locals_win.load(self.current_frame)
        self.watch_win.refresh(self.current_frame)
        self._clear_exec_markers()
        if top:
            ed = self.open_file(top["file"])
            if isinstance(ed, CodeEditor):
                ed.set_exec_line(top["line"])
        for ed in self.editors():
            if isinstance(ed, CodeEditor):
                ed.debug_eval = lambda expr, cb: self.session.evaluate(expr, self.current_frame, cb)
        reason = msg.get("reason", "")
        where = f"{os.path.basename(top['file'])}, line {top['line']}" if top else ""
        tname = next((t["name"] for t in msg.get("threads", []) if t["id"] == msg.get("thread") and not t.get("main")),
                     "")
        if tname:
            where += f" (thread {tname})"
        self.lbl_state.setText(f"Paused: {reason} at {where}" if where else "Paused")
        self.raise_()
        self.activateWindow()
        self.immediate.update_mode()
        if reason == "exception":
            m = re.match(r"ModuleNotFoundError: No module named '([\w.]+)'", msg.get("exception", ""))
            if m and self.offer_module_fix(m.group(1).split(".")[0]):
                return
            thrown = "(thrown" in msg.get("exception", "")
            QMessageBox.warning(self, "Exception Thrown" if thrown else "Exception Unhandled",
                                f"{msg.get('exception', '')}\n\nat {where}\n\n"
                                "Look at Locals and Call Stack to see what happened. "
                                "Continue (F5) to keep going, or Stop (Shift+F5).")

    def _on_debug_continued(self):
        self.immediate.update_mode()
        self._clear_exec_markers()
        self.locals_win.clear()
        self._update_run_state()

    def _on_debug_exited(self, code):
        self._run_to = None
        self.immediate.update_mode()
        if getattr(self, "_last_tests", None) is not None and not getattr(self, "_restart_pending", False):
            self._last_tests = None
            self.tests.discover()
        self._clear_exec_markers()
        self.locals_win.clear()
        self.stack_win.clear()
        self.threads_win.clear()
        for dk in self.debug_docks:
            dk.hide()
        self.output_dock.raise_()
        self._update_run_state()
        if getattr(self, "_restart_pending", False):
            self._restart_pending = False
            self.start_debugging(self._last_launch, getattr(self, "_last_tests", None))

    def _on_thread_selected(self, ident):
        """Threads window: show another thread's stack; Locals / Watch / Immediate use its frames."""
        if not self.session.paused:
            return

        def done(stack, thread):
            top = next((fr for fr in stack if fr.get("user")), stack[0] if stack else None)
            self.current_frame = top["id"] if top else 0
            self.stack_win.current = self.current_frame
            self.stack_win.load(stack, self.project_root)
            self.threads_win.show_viewing(thread)
            self.locals_win.load(self.current_frame)
            self.watch_win.refresh(self.current_frame)
            self.stack_dock.raise_()
            if top:
                ed = self.open_file(top["file"])
                if isinstance(ed, CodeEditor):
                    ed.go_to_line(top["line"])
        self.session.select_thread(ident, done)

    def _on_frame_selected(self, frame_id, path, line):
        self.current_frame = frame_id
        self.locals_win.load(frame_id)
        self.watch_win.refresh(frame_id)
        ed = self.open_file(path)
        if isinstance(ed, CodeEditor):
            ed.go_to_line(line)

    def add_watch(self):
        ed = self.current_editor()
        expr = ""
        if ed is not None:
            c = ed.textCursor()
            if not c.hasSelection():
                c.select(QTextCursor.WordUnderCursor)
            expr = c.selectedText().strip()
        if not expr:
            expr, ok = QInputDialog.getText(self, "Add Watch", "Expression:")
            if not ok:
                return
        self.watch_win.add(expr)
        self.watch_dock.show()
        self.watch_dock.raise_()

    # ------------------------------------------------------------ breakpoints
    def _on_editor_breakpoints(self, path):
        if not path:
            return
        ed = next((e for e in self.editors() if isinstance(e, CodeEditor) and e.path == path), None)
        if ed is None:
            return
        bps = ed.breakpoints()
        key = os.path.abspath(path)
        if bps:
            self.breakpoints[key] = bps
        else:
            self.breakpoints.pop(key, None)
        self.settings.setValue("breakpoints", json.dumps(self.breakpoints))
        if self.session.active:
            self.session.set_breakpoints(key, bps)

    def _sync_breakpoints_from_editors(self):
        for ed in self.editors():
            if isinstance(ed, CodeEditor) and ed.path:
                bps = ed.breakpoints()
                key = os.path.abspath(ed.path)
                if bps:
                    self.breakpoints[key] = bps
                else:
                    self.breakpoints.pop(key, None)
        self.settings.setValue("breakpoints", json.dumps(self.breakpoints))

    def _breakpoints_for_session(self):
        out = {f: list(bps) for f, bps in self.breakpoints.items() if os.path.exists(f)}
        run_to = getattr(self, "_run_to", None)
        if run_to:                                   # Run To Cursor before the program started
            out.setdefault(run_to[0], []).append({"line": run_to[1], "condition": ""})
        return out

    def run_to_cursor(self):
        """Ctrl+F10: run until the line with the cursor (starts debugging if needed), like VS."""
        ed = self.current_editor()
        if ed is None or not ed.path:
            return
        line = ed.textCursor().blockNumber() + 1
        path = os.path.abspath(ed.path)
        if self.session.active:
            if self.session.paused:
                self.session.run_to_line(path, line)
            return
        self._run_to = (path, line)
        self.start_debugging()

    def set_next_statement(self, line=None):
        """Ctrl+Shift+F10 / drag the yellow arrow: the paused program continues from another line of the
        same function (lines in between are skipped, or run again)."""
        if not (self.session.active and self.session.paused):
            return
        ed = self.current_editor()
        if line is None:
            if ed is None:
                return
            line = ed.textCursor().blockNumber() + 1

        def done(msg):
            if msg.get("error"):
                QMessageBox.information(self, "Set Next Statement",
                                        f"Can't move the next statement to line {line}:\n{msg['error']}\n\n"
                                        "It has to be a line in the same function, and not inside a different "
                                        "loop or try block.")
            else:
                self.statusBar().showMessage(f"Next statement: line {msg.get('line', line)}", 3000)
        self.session.set_next_statement(line, done)

    def delete_all_breakpoints(self):
        for ed in self.editors():
            if isinstance(ed, CodeEditor):
                ed.clear_breakpoints()
        for f in list(self.breakpoints):
            if self.session.active:
                self.session.set_breakpoints(f, [])
        self.breakpoints = {}
        self.settings.setValue("breakpoints", "{}")

    def select_interpreter(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Python Interpreter",
                                              os.path.dirname(self.python), "Python (python*);;All files (*)")
        if path:
            self.python = path
            self.settings.setValue("python", path)
            self._update_python_label()

    # ================================================================= misc
    def about(self):
        QMessageBox.about(self, "About PyStudio",
                          f"<b>PyStudio {__version__}</b><br>"
                          "A Visual Studio-style Python IDE for Linux.<br><br>"
                          f"Interpreter: {self.python}")

    # ------------------------------------------------------- v1.5: layouts, help
    def _layouts(self):
        try:
            return json.loads(self.settings.value("layouts", "{}") or "{}")
        except ValueError:
            return {}

    def save_layout(self):
        names = list(self._layouts())
        name, ok = QInputDialog.getText(self, "Save Window Layout", "Layout name (e.g. Debugging, Designing):",
                                        text=f"Layout {len(names) + 1}")
        name = name.strip()
        if not ok or not name:
            return
        layouts = self._layouts()
        if name in layouts and QMessageBox.question(self, "Save Window Layout",
                                                    f"Replace the layout '{name}'?") != QMessageBox.Yes:
            return
        layouts[name] = bytes(self.saveState(LAYOUT_VERSION).toBase64()).decode("ascii")
        self.settings.setValue("layouts", json.dumps(layouts))
        self._apply_layout_shortcuts()
        self.statusBar().showMessage(f"Saved window layout '{name}'.", 4000)

    def apply_layout(self, name):
        from PySide6.QtCore import QByteArray
        data = self._layouts().get(name)
        if data:
            state = QByteArray.fromBase64(data.encode("ascii"))
            if not self.restoreState(state, LAYOUT_VERSION):
                self.restoreState(state)          # saved before v1.5.1
            self.statusBar().showMessage(f"Window layout '{name}'.", 3000)

    def _fill_layouts_menu(self):
        self.layouts_menu.clear()
        names = list(self._layouts())
        for i, name in enumerate(names):
            # the key is shown only (the window-wide shortcut does the work - two would be ambiguous)
            self.layouts_menu.addAction(name + (f"\tCtrl+Alt+{i + 1}" if i < 9 else ""),
                                        lambda n=name: self.apply_layout(n))
        if not names:
            a = self.layouts_menu.addAction("(no saved layouts - Window > Save Window Layout)")
            a.setEnabled(False)

    def _apply_layout_shortcuts(self):
        """Ctrl+Alt+1..9 apply saved layouts 1..9 (like VS)."""
        from PySide6.QtGui import QShortcut
        for sc in self._layout_shortcuts:
            sc.setEnabled(False)
            sc.deleteLater()
        self._layout_shortcuts = []
        for i, name in enumerate(list(self._layouts())[:9]):
            sc = QShortcut(QKeySequence(f"Ctrl+Alt+{i + 1}"), self)
            sc.activated.connect(lambda n=name: self.apply_layout(n))
            self._layout_shortcuts.append(sc)

    def manage_layouts(self):
        layouts = self._layouts()
        if not layouts:
            QMessageBox.information(self, "Manage Window Layouts", "There are no saved layouts yet.")
            return
        name, ok = QInputDialog.getItem(self, "Manage Window Layouts", "Delete which layout?",
                                        list(layouts), 0, False)
        if ok and name and QMessageBox.question(self, "Manage Window Layouts",
                                                f"Delete the layout '{name}'?") == QMessageBox.Yes:
            layouts.pop(name, None)
            self.settings.setValue("layouts", json.dumps(layouts))
            self._apply_layout_shortcuts()

    def reset_layout(self):
        self.restoreState(self._default_state)
        for dk in self.debug_docks:
            dk.setVisible(self.session.active)
        self.statusBar().showMessage("Window layout reset.", 3000)

    def close_solution(self):
        """File > Close Solution: close the project/solution and every document, then show the Start Window
        (like Visual Studio)."""
        if self.runner.is_running or self.session.active:
            if QMessageBox.question(self, "Close Solution", "The program is still running. Stop it and close "
                                    "the solution?") != QMessageBox.Yes:
                return
            self.stop_all()
        for i in reversed(range(self.tabs.count())):
            self.close_tab(i)
        if self.tabs.count():
            return                                  # canceled saving a file: keep the solution open
        self._sync_breakpoints_from_editors()
        self.close_folder()
        self.error_list.scan_project(None)
        self.find_results.show_results("", [], None)
        self.task_list.root = None
        self.task_list.refresh()
        self.bookmark_window.set_bookmarks([], None)
        self.output.clear()
        self._nav_back, self._nav_fwd, self._nav_cur = [], [], None
        self._nav_update_actions()
        self.show_start_window()

    def close_all_documents(self):
        for i in reversed(range(self.tabs.count())):
            self.close_tab(i)

    def close_all_but_this(self):
        keep = self.tabs.currentWidget()
        for i in reversed(range(self.tabs.count())):
            if self.tabs.widget(i) is not keep:
                self.close_tab(i)

    def _open_url(self, url):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(url) if "://" in url else QUrl.fromLocalFile(url))
        self.statusBar().showMessage(f"Help: {url}", 5000)

    def open_readme(self):
        from .helpdocs import README
        if os.path.exists(README):
            self._open_url(README)
        else:
            QMessageBox.information(self, "PyStudio Guide", f"README.pdf wasn't found next to PyStudio ({README}).")

    def _help_hint(self, text=None):
        self.statusBar().showMessage(text or "F1 looks up the word at the cursor: put the cursor on a word in the code "
                                     "(or select a control in a form), then press F1. Help > PyStudio Guide covers "
                                     "PyStudio itself.", 8000)

    def show_help(self):
        """F1: docs for the word under the cursor (editor) or the selected control (designer)."""
        from . import helpdocs
        d = self.current_designer()
        if d is not None:
            sel = d.selected
            if sel is None:
                self._open_url(helpdocs.qt_url("QtWidgets", "QMainWindow"))
                return
            tool = sel._ps.get("tool")
            if tool in helpdocs.TOOL_CLASSES:
                self._open_url(helpdocs.qt_url(*helpdocs.TOOL_CLASSES[tool]))
            elif tool in M.TOOLS and M.TOOLS[tool][0].startswith("Q"):
                self._open_url(helpdocs.qt_url("QtWidgets", M.TOOLS[tool][0]))
            else:
                self._help_hint(f"There's no help page for a {tool}. Help > PyStudio Guide covers PyStudio itself.")
            return
        ed = self.current_editor()
        if ed is None:
            self._help_hint()
            return
        c = ed.textCursor()
        c.select(QTextCursor.WordUnderCursor)
        word = c.selectedText().strip()
        if not word or not (word.isidentifier()):
            self._help_hint()
            return
        url = helpdocs.url_for("", word)
        if url:
            self._open_url(url)
            return
        line = c.block().blockNumber() + 1
        col = c.selectionStart() - c.block().position()
        mods = set()
        if self.project_root:
            mods = {os.path.splitext(f)[0] for f in os.listdir(self.project_root) if f.endswith(".py")}

        def done(info):
            full = info.get("full", "") if isinstance(info, dict) else ""
            u = helpdocs.url_for(full, word, mods)
            if u is None and full.split(".")[0] in mods:
                self.go_to_definition()       # your own code: F1 shows where it's defined
                return
            self._open_url(u or helpdocs.search_url(word))
        if self.lang.available:
            self.lang.request("hover", ed.toPlainText(), ed.path, line, col, self._lang_context(), done)
        else:
            self._open_url(helpdocs.search_url(word))

    def restart_pystudio(self):
        """Close (asks to save) and start PyStudio again with the same project."""
        from PySide6.QtCore import QProcess
        root = self.project_root
        if not self.close():
            return
        args = [os.path.abspath(sys.argv[0])] + ([root] if root else [])
        QProcess.startDetached(sys.executable, args)
        QApplication.instance().quit()

    # ------------------------------------------------------- v1.4: tests, immediate, exceptions, profiler
    def _update_tool_projects(self):
        if self.project is not None:
            python, env, cwd = self.project.python(self.python), self._project_env(), self.project_root
            env.update(sln.python_path_env(self.project, env))
        else:
            python, env, cwd = self.python, {}, self.project_root
        self.immediate.set_project(python, cwd)
        self.tests.set_project(self.project_root, python, env)

    def _project_env(self):
        env = {}
        raw = (self.project.data.get("env") or "") if self.project else ""
        for line in str(raw).splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        return env

    def show_immediate(self):
        self.immediate_dock.show()
        self.immediate_dock.raise_()
        self.immediate.update_mode()
        self.immediate.input.setFocus()

    def show_tests(self):
        self.tests_dock.show()
        self.tests_dock.raise_()
        if self.tests.root and not self.tests.nodes and self.tests.proc is None:
            self.tests.discover()

    def debug_tests(self, nodes):
        if self.session.active or self.runner.is_running:
            return
        self.start_debugging(False, tests=list(nodes))

    def add_unit_test(self):
        if not self.project_root:
            QMessageBox.information(self, "Add Unit Test", "Open or create a project first.")
            return
        mods = sorted(os.path.splitext(f)[0] for f in os.listdir(self.project_root)
                      if f.endswith(".py") and not is_generated_path(os.path.join(self.project_root, f))
                      and f not in ("main.py", "my.py", "pystudio_runtime.py") and not f.startswith("test_"))
        choice, ok = QInputDialog.getItem(self, "Add Unit Test", "Which module will these tests check?",
                                          ["(none - a sample test)"] + mods, 1 if mods else 0, False)
        if not ok:
            return
        path = new_test_file(self.project_root, None if choice.startswith("(") else choice)
        self.open_file(path)
        self.show_tests()
        self.tests.discover()

    # ---------------------------------------------------------------- code coverage
    def analyze_coverage(self):
        """Test > Analyze Code Coverage for All Tests."""
        if not self.save_all():
            return
        self.show_tests()
        self.tests.run_coverage()

    def _on_coverage(self, files):
        self.coverage = files
        self.coverage_win.load(files, self.project_root)
        self.coverage_dock.show()
        self.coverage_dock.raise_()
        for ed in self.editors():
            if isinstance(ed, CodeEditor):
                self._apply_coverage(ed)

    def _apply_coverage(self, ed):
        c = self.coverage.get(os.path.abspath(ed.path)) if ed.path else None
        if c is not None and not ed.is_dirty:
            ed.set_coverage(c["covered"], c["missing"])
        elif ed.coverage is not None:
            ed.set_coverage(None, None)

    def clear_coverage(self):
        self.coverage = {}
        self.coverage_win.clear()
        for ed in self.editors():
            if isinstance(ed, CodeEditor) and ed.coverage is not None:
                ed.set_coverage(None, None)

    def install_pytest(self, package="pytest"):
        import subprocess
        if not isinstance(package, str):
            package = "pytest"
        python = self.tests.python or self.python
        self.output.write(f"Installing {package} for {python}...\n", "info")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            r = subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check", package],
                               capture_output=True, text=True, timeout=300)
            self.output.write((r.stdout + r.stderr)[-2000:] + "\n", "info" if r.returncode == 0 else "err")
        except (OSError, subprocess.SubprocessError) as e:
            self.output.write(f"{e}\n", "err")
        finally:
            QApplication.restoreOverrideCursor()
        self.tests.discover()

    def exception_settings(self):
        dlg = exceptionsettings.ExceptionSettingsDialog(self)
        if dlg.exec() == exceptionsettings.QDialog.Accepted:
            es = dlg.settings()
            self.session.set_exception_settings(es["types"], es["all"])
            n = len(es["types"])
            self.statusBar().showMessage("Break on every exception when thrown." if es["all"] else
                                         f"Break when thrown: {n} exception type(s)." if n else
                                         "Only unhandled exceptions stop the debugger.", 5000)

    def profile_project(self):
        """Alt+F2: run with cProfile; the report opens when the program exits."""
        if self.session.active or self.runner.is_running or self.profile_proc is not None:
            return
        launch = self._prepare_launch()
        if not launch:
            return
        python, script, cwd, args, env, root = launch
        import tempfile
        from PySide6.QtCore import QProcess, QProcessEnvironment
        fd, out = tempfile.mkstemp(suffix=".prof", prefix="pystudio-")
        os.close(fd)
        p = QProcess(self)
        penv = QProcessEnvironment.systemEnvironment()
        penv.insert("PYTHONUNBUFFERED", "1")
        for k, v in (env or {}).items():
            penv.insert(k, v)
        p.setProcessEnvironment(penv)
        p.setWorkingDirectory(cwd)
        p.readyReadStandardOutput.connect(
            lambda: self.output.write(bytes(p.readAllStandardOutput()).decode("utf-8", "replace")))
        p.readyReadStandardError.connect(
            lambda: self.output.write(bytes(p.readAllStandardError()).decode("utf-8", "replace"), "err"))

        def finished(code, _status):
            self.profile_proc = None
            self._update_run_state()
            p.deleteLater()
            try:
                rows, total = load_stats(out)
            except Exception:  # noqa: BLE001 - no profile file (stopped or crashed early)
                self.output.write("\nNo profile data - close the program normally (not Stop) to get the report.\n",
                                  "err")
                return
            finally:
                try:
                    os.remove(out)
                except OSError:
                    pass
            self.output.write(f"\nProfiling finished (exit code {code}).\n", "info")
            self._add_editor(ProfileReport(f"Profile - {os.path.basename(script)}", rows, total, root))
        p.finished.connect(finished)
        self.output.clear()
        self.output.write(f"> Profiling {script} - use the program, then close it to see the report.\n\n", "info")
        self.output_dock.raise_()
        self.profile_proc = p
        p.start(python, ["-m", "cProfile", "-o", out, script] + list(args))
        self._update_run_state()

    # ------------------------------------------------------- v1.3: git, find in files, format, extract
    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.ActivationChange and self.isActiveWindow() and self.git.root and not self.git.busy:
            self.git_panel.refresh()       # files may have changed outside PyStudio (terminal git)

    def show_git_changes(self):
        self.git_dock.show()
        self.git_dock.raise_()
        if self.git.root:
            self.git_panel.refresh()
            self.git_panel.message.setFocus()

    # ---------------------------------------------------------------- Git: merge, blame, pull requests
    def open_merge_editor(self, path):
        from .gittools import MergeEditor
        for ed in self.editors():
            if type(ed).__name__ == "MergeEditor" and ed.path == os.path.abspath(path):
                self.tabs.setCurrentWidget(ed)
                return ed
        try:
            m = MergeEditor(self.git, path)
        except OSError as e:
            QMessageBox.warning(self, "Merge Editor", str(e))
            return None
        m.resolved.connect(lambda p, m=m: self._merge_resolved(m, p))
        return self._add_editor(m)

    def _merge_resolved(self, m, path):
        i = self.tabs.indexOf(m)
        if i >= 0:
            self.tabs.removeTab(i)
            m.deleteLater()
        self.reload_changed_files()
        self.git_panel.refresh()
        self.statusBar().showMessage(f"{os.path.basename(path)} resolved and staged.", 5000)

    def show_blame(self):
        from .gittools import BlameView
        ed = self.current_doc()
        path = getattr(ed, "path", None)
        if not path or not self.git.root or not os.path.abspath(path).startswith(self.git.root):
            QMessageBox.information(self, "Blame", "Open a file from a Git repository first.")
            return
        if getattr(ed, "is_dirty", False) and not self._save(ed):
            return
        try:
            v = BlameView(self.git, path)
        except RuntimeError as e:
            QMessageBox.information(self, "Blame", f"Git can't blame {os.path.basename(path)}:\n{e}")
            return
        v.open_commit.connect(lambda sha: self._add_editor(DiffView(f"{sha[:8]} (commit)", self.git.show(sha))))
        v.open_line.connect(self.open_file_at)
        self._add_editor(v)

    def show_issues(self):
        self.issues_dock.show()
        self.issues_dock.raise_()
        self.issues.refresh()

    def _fixes_in_commit(self, line):
        """Issues > Fixes #N: a line in the commit message (GitHub closes the issue when the commit arrives)."""
        m = self.git_panel.message
        text = m.toPlainText().rstrip()
        if line not in text:
            m.setPlainText((text + "\n\n" if text else "") + line)
        self.git_dock.show()
        self.git_dock.raise_()
        m.setFocus()
        c = m.textCursor()
        c.movePosition(c.MoveOperation.Start)
        m.setTextCursor(c)
        self.statusBar().showMessage(f"Added \"{line}\" to the commit message - type what you changed above it.",
                                     8000)

    def show_pull_requests(self):
        self.prs_dock.show()
        self.prs_dock.raise_()
        self.prs.refresh()

    def _toggle_inline_blame(self, on):
        self.settings.setValue("inlineBlame", "true" if on else "false")
        if on:
            self._update_inline_blame()
        else:
            for ed in self.editors():
                if isinstance(ed, CodeEditor) and getattr(ed, "line_note", None):
                    ed.line_note = None
                    ed.viewport().update()

    def _update_inline_blame(self):
        """The current line's author, age and commit message, after the line (like GitLens)."""
        from .gittools import blame, inline_note
        ed = self.current_editor()
        if ed is None or not self.a_inline_blame.isChecked():
            return
        path = ed.path
        if not path or not self.git.root or not os.path.abspath(path).startswith(self.git.root + os.sep):
            ed.line_note = None
            return
        rev = ed.document().revision()
        cached = self._blame_cache.get(id(ed))
        if cached is None or cached[0] != rev:
            try:
                entries = blame(self.git, path, ed.toPlainText())
            except RuntimeError:
                entries = []                      # not in git yet / binary
            cached = (rev, entries)
            self._blame_cache[id(ed)] = cached
        line = ed.textCursor().blockNumber() + 1
        e = next((x for x in cached[1] if x.get("line") == line), None)
        ed.line_note = (line, inline_note(e)) if e else None
        ed.viewport().update()

    def _file_history(self):
        ed = self.current_doc()
        path = getattr(ed, "path", None)
        if not path or not self.git.root:
            QMessageBox.information(self, "File History", "Open a file from a Git repository first.")
            return
        self.git_panel.show_history(os.path.relpath(path, self.git.root))

    def reload_changed_files(self):
        """After a pull, branch switch or Undo Changes: show what's on disk now."""
        for ed in self.editors():
            path = getattr(ed, "path", None)
            if not path:
                continue
            if not os.path.exists(path):
                continue
            if isinstance(ed, CodeEditor):
                with open(path, encoding="utf-8", errors="replace") as f:
                    disk = f.read()
                if ed.toPlainText() != disk:
                    if ed.is_dirty and QMessageBox.question(
                            self, "File changed", f"{os.path.basename(path)} changed on disk. Reload it and "
                            "lose your unsaved edits?") != QMessageBox.Yes:
                        continue
                    ed.apply_text(disk)
                    ed.document().setModified(False)
            elif isinstance(ed, FormDesigner) and not ed.is_dirty:
                try:
                    with open(path, encoding="utf-8") as f:
                        xml = f.read()
                    if xml.strip() and xml.strip() != ('<?xml version="1.0" encoding="UTF-8"?>\n' + ed.to_xml_string()).strip():
                        import xml.etree.ElementTree as ET
                        ed._apply_xml(ET.fromstring(xml))
                        ed._set_dirty(False)
                except (OSError, Exception):  # noqa: BLE001 - leave the designer as it was
                    pass
        self.scan_errors()

    def find_in_files(self, replace=False):
        ed = self.current_editor()
        initial = ed.textCursor().selectedText() if ed is not None and "\u2029" not in ed.textCursor().selectedText() else ""
        dlg = findinfiles.FindInFilesDialog(replace, initial, self)
        if dlg.exec() != findinfiles.QDialog.Accepted:
            return
        o = dlg.options()
        pattern = findinfiles.make_pattern(o["find"], o["case"], o["whole"], o["regex"])
        open_eds = {os.path.abspath(e.path): e for e in self.editors() if isinstance(e, CodeEditor) and e.path}
        if o["scope"] == "Current document":
            paths = [os.path.abspath(ed.path)] if ed is not None and ed.path else []
        elif o["scope"] == "All open documents" or not self.project_root:
            paths = list(open_eds)
        elif o["scope"] == "Entire solution" and self.solution_root:
            paths = [os.path.abspath(p) for p in findinfiles.project_files(self.solution_root, o["types"])]
        else:
            paths = [os.path.abspath(p) for p in findinfiles.project_files(self.project_root, o["types"],
                                                                            skip_nested=True)]

        def text_of(p):
            if p in open_eds:
                return open_eds[p].toPlainText()
            try:
                with open(p, encoding="utf-8") as f:
                    return f.read()
            except (OSError, UnicodeDecodeError):
                return None
        results, files_hit, total = [], 0, 0
        for p in paths:
            text = text_of(p)
            if text is None:
                continue
            hits = findinfiles.search_text(text, pattern)
            if not hits:
                continue
            files_hit += 1
            if replace:
                new, n = findinfiles.replace_text(text, pattern, o["replace"], o["regex"])
                if n:
                    total += n
                    e = open_eds.get(p) or self.open_file(p)
                    if isinstance(e, CodeEditor):
                        e.apply_text(new)
                        open_eds[p] = e
            for line, col, line_text in hits:
                results.append((p, line, col, line_text.strip()))
        if replace:
            title = f"Replace '{o['find']}' with '{o['replace']}': {total} in {files_hit} file(s) - review and Save All"
        else:
            title = f"Find all '{o['find']}' ({o['scope'].lower()})"
        self.find_results.show_results(title, results if not replace else results, self.project_root)
        self.find_dock.show()
        self.find_dock.raise_()
        self.statusBar().showMessage(title, 6000)

    def _ensure_black(self):
        if formatter.black_installed():
            return True
        if QMessageBox.question(self, "Format Document",
                                "Formatting uses 'black', which isn't installed yet. Install it now "
                                "(one time, needs internet)?") != QMessageBox.Yes:
            return False
        self.output.write("Installing black...\n", "info")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            import subprocess
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "black"],
                               capture_output=True, text=True, timeout=300)
            self.output.write((r.stdout + r.stderr)[-2000:] + "\n", "info" if r.returncode == 0 else "err")
        finally:
            QApplication.restoreOverrideCursor()
        return formatter.black_installed()

    def format_document(self, selection=False, ed=None, quiet=False):
        """Edit > Advanced > Format Document (Ctrl+K, Ctrl+D) - black, keeping breakpoints and undo."""
        ed = ed or self.current_editor()
        if ed is None or ed.isReadOnly():
            return
        if ed.path and is_generated_path(ed.path):
            if not quiet:
                self.statusBar().showMessage("Generated files aren't formatted.", 4000)
            return
        if quiet and not formatter.black_installed():
            return
        if not quiet and not self._ensure_black():
            return
        first = last = None
        if selection:
            c = ed.textCursor()
            if not c.hasSelection():
                self.statusBar().showMessage("Select the lines to format first.", 4000)
                return
            doc = ed.document()
            first = doc.findBlock(c.selectionStart()).blockNumber() + 1
            last = doc.findBlock(c.selectionEnd()).blockNumber() + 1
        ok, out = formatter.format_source(ed.toPlainText(), first, last)
        if not ok:
            if not quiet:
                QMessageBox.information(self, "Format Document", out)
            return
        changed = ed.apply_text(out)
        if not quiet:
            self.statusBar().showMessage("Formatted." if changed else "Already formatted.", 3000)

    def remove_unused_imports(self):
        ed = self.current_editor()
        if ed is None or ed.isReadOnly():
            return
        from .language import check_source
        from .quickfix import remove_import
        text = ed.toPlainText()
        n = 0
        for _ in range(50):
            unused = [d for d in check_source(text, ed.path or "") if "imported but unused" in d.message]
            if not unused:
                break
            d = unused[-1]
            m = re.search(r"'([^']+)' imported but unused", d.message)
            if not m:
                break
            new = remove_import(text, d.line, m.group(1))
            if new == text:
                break
            text, n = new, n + 1
        if n:
            if not ed.toPlainText().startswith("\n") and text.startswith("\n"):
                text = text.lstrip("\n")       # every import went: drop the blank lines left above
            ed.apply_text(text)
        self.statusBar().showMessage(f"Removed {n} unused import(s)." if n else "No unused imports.", 4000)

    def extract(self, kind):
        """Extract Method (Ctrl+R, Ctrl+M) / Extract Variable (Ctrl+R, Ctrl+V) on the selection."""
        ed = self.current_editor()
        if ed is None or ed.isReadOnly():
            return
        c = ed.textCursor()
        what = "Method" if kind == "function" else "Variable"
        if not c.hasSelection():
            QMessageBox.information(self, f"Extract {what}",
                                    "Select the code to extract first" + (" - whole lines of a method." if
                                                                          kind == "function" else " - an expression."))
            return
        doc = ed.document()
        s_block, e_block = doc.findBlock(c.selectionStart()), doc.findBlock(c.selectionEnd())
        start = (s_block.blockNumber() + 1, c.selectionStart() - s_block.position())
        end_col = c.selectionEnd() - e_block.position()
        end = (e_block.blockNumber() + 1, end_col)
        if kind == "function" and end_col == 0 and e_block.blockNumber() > s_block.blockNumber():
            prev = e_block.previous()         # selection ends at the start of the next line
            end = (prev.blockNumber() + 1, len(prev.text()))
        default = "new_method" if kind == "function" else "value"
        name, ok = QInputDialog.getText(self, f"Extract {what}", f"Name for the new {what.lower()}:", text=default)
        name = name.strip()
        if not ok or not name:
            return
        if not name.isidentifier():
            QMessageBox.warning(self, f"Extract {what}", f"'{name}' isn't a valid Python name.")
            return

        def done(changes):
            if "__error__" in changes:
                QMessageBox.information(self, f"Extract {what}", f"Can't extract that selection:\n{changes['__error__']}")
                return
            new = next(iter(changes.values()), None)
            if new is None or new == ed.toPlainText():
                return
            from . import refactor
            dlg = refactor.PreviewDialog(f"Extract {what}", f"Extract {what.lower()} '{name}'",
                                         {ed.path or "(this file)": (ed.toPlainText(), new)}, self.project_root, self)
            if dlg.exec() == QDialog.Accepted and dlg.selected():
                ed.apply_text(new)
                self.statusBar().showMessage(f"Extracted {what.lower()} '{name}'.", 4000)
        self.lang.request_extract(kind, ed.toPlainText(), ed.path, start, end, self._lang_context(), name, done)

    def closeEvent(self, event):
        self.props.flush()
        for ed in self.editors():
            if not self._confirm_discard(ed):
                event.ignore()
                return
        self.runner.stop()
        self.session.stop()
        self.tests.shutdown()          # background pytest / Python Interactive processes
        self.pkg_badge.check.cancel()  # a background package-update check
        self.terminal.close_all()
        for ed in self.editors():
            if hasattr(ed, "shutdown"):
                ed.shutdown()
        self.immediate.shutdown()
        if self.profile_proc is not None:
            self.profile_proc.kill()
        self._sync_breakpoints_from_editors()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState(LAYOUT_VERSION))
        event.accept()
