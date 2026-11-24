"""Server Explorer (VS: View > Server Explorer) - the project's data connections, their
tables, views and columns - and the Add Connection dialog."""
import importlib.util
import os
import sys

from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QStackedWidget, QStyle, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from . import dbcore

ROLE = Qt.UserRole
PIP_NAME = {"DB2": "ibm_db", "ODBC": "pyodbc", "Snowflake": "snowflake-connector-python"}
PASSWORD_LABELS = [("None", "No password"), ("Prompt", "Ask when connecting (recommended)"),
                   ("Environment", "From an environment variable"),
                   ("Saved", "Saved in the project file (not secure)")]


def driver_available(provider):
    mod = dbcore.PROVIDER_MODULE.get(provider)
    if provider == "SQLite":
        return True
    if mod == "ibm_db":
        return importlib.util.find_spec("ibm_db_dbi") is not None or importlib.util.find_spec("ibm_db") is not None
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):       # "snowflake.connector" when "snowflake" isn't there
        return False


class ConnectionDialog(QDialog):
    """Add Connection / Modify Connection."""

    def __init__(self, service, project_root, existing_names, cfg=None, project_python=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.root = project_root
        self.existing = {n.lower() for n in existing_names}
        self.project_python = project_python
        self.proc = None
        cfg = dict(cfg or {})
        self.original_name = cfg.get("name")
        self.setWindowTitle("Modify Connection" if cfg else "Add Connection")
        self.resize(620, 520)

        self.e_name = QLineEdit(cfg.get("name", ""))
        self.cb_provider = QComboBox()
        self.cb_provider.addItems(["SQLite (database file)", "IBM DB2 (ibm_db)", "ODBC (any driver: DB2 for i, "
                                   "SQL Server, MySQL...)", "Snowflake"])
        self.cb_provider.setCurrentIndex(dbcore.PROVIDERS.index(cfg.get("provider", "SQLite")))

        # --- SQLite page
        sq = QWidget()
        f = QFormLayout(sq)
        f.setContentsMargins(0, 0, 0, 0)
        self.e_file = QLineEdit(cfg.get("file", ""))
        self.e_file.setPlaceholderText("data/mydata.db  (relative paths are inside the project)")
        b_browse = QPushButton("Browse...")
        b_browse.clicked.connect(self._browse)
        b_new = QPushButton("New...")
        b_new.setToolTip("Create a new, empty database file")
        b_new.clicked.connect(self._new_db)
        b_sample = QPushButton("Sample")
        b_sample.setToolTip("Create a small sample database (Customers, Products, Orders) to learn with")
        b_sample.clicked.connect(self._sample_db)
        row = QHBoxLayout()
        row.addWidget(self.e_file, 1)
        for b in (b_browse, b_new, b_sample):
            row.addWidget(b)
        f.addRow("Database file:", row)
        f.addRow(QLabel("<small>Put the file in the project's <b>data</b> folder so Publish includes it.</small>"))

        # --- DB2 page
        d2 = QWidget()
        f = QFormLayout(d2)
        f.setContentsMargins(0, 0, 0, 0)
        self.e_host = QLineEdit(cfg.get("host", ""))
        self.e_host.setPlaceholderText("db2server.example.com")
        self.e_port = QSpinBox()
        self.e_port.setRange(1, 65535)
        self.e_port.setValue(int(cfg.get("port", 50000) or 50000))
        self.e_database = QLineEdit(cfg.get("database", ""))
        self.cb_security = QComboBox()
        self.cb_security.addItems(["None", "SSL"])
        self.cb_security.setCurrentText(cfg.get("security", "None") or "None")
        self.e_extra = QLineEdit(cfg.get("extra", ""))
        self.e_extra.setPlaceholderText("Optional extra keywords, e.g. CONNECTTIMEOUT=15;")
        f.addRow("Server:", self.e_host)
        f.addRow("Port:", self.e_port)
        f.addRow("Database:", self.e_database)
        f.addRow("Security:", self.cb_security)
        f.addRow("Extra:", self.e_extra)

        # --- ODBC page
        od = QWidget()
        f = QFormLayout(od)
        f.setContentsMargins(0, 0, 0, 0)
        self.cb_dsn = QComboBox()
        self.cb_dsn.setEditable(True)
        self.cb_dsn.addItem("")
        try:
            import pyodbc
            self.cb_dsn.addItems(sorted(pyodbc.dataSources()))
        except Exception:
            pass
        self.cb_dsn.setCurrentText(cfg.get("dsn", ""))
        self.e_cs = QLineEdit(cfg.get("connectionString", ""))
        self.e_cs.setPlaceholderText("DRIVER={IBM i Access ODBC Driver};SYSTEM=myas400;  (when there's no DSN)")
        f.addRow("Data source (DSN):", self.cb_dsn)
        f.addRow("or connection string:", self.e_cs)
        f.addRow(QLabel("<small>DSNs come from /etc/odbc.ini and ~/.odbc.ini. Use ${NAME} to pull a "
                        "value from an environment variable.</small>"))

        # --- Snowflake page
        sf = QWidget()
        f = QFormLayout(sf)
        f.setContentsMargins(0, 0, 0, 0)
        self.e_account = QLineEdit(cfg.get("account", ""))
        self.e_account.setPlaceholderText("account identifier, e.g. myorg-myaccount  (the part before "
                                          ".snowflakecomputing.com)")
        self.cb_auth = QComboBox()
        for key, label in zip(dbcore.SNOWFLAKE_AUTH, dbcore.SNOWFLAKE_AUTH_LABELS):
            self.cb_auth.addItem(label, key)
        self.cb_auth.setItemData(1, "Sign in with your company's single sign-on (Okta, Azure AD...) in the "
                                    "browser", Qt.ToolTipRole)
        self.cb_auth.setItemData(2, "For service accounts: an encrypted private key file (.p8) and its "
                                    "passphrase, instead of a password and 2FA", Qt.ToolTipRole)
        auth = cfg.get("auth", "Password")
        self.cb_auth.setCurrentIndex(dbcore.SNOWFLAKE_AUTH.index(auth) if auth in dbcore.SNOWFLAKE_AUTH else 0)
        self.e_keyfile = QLineEdit(cfg.get("keyFile", ""))
        self.e_keyfile.setPlaceholderText("~/keys/rsa_key.p8   (${NAME} = an environment variable)")
        b_key = QPushButton("Browse...")
        b_key.clicked.connect(self._browse_key)
        self.keyrow = QWidget()
        kr = QHBoxLayout(self.keyrow)
        kr.setContentsMargins(0, 0, 0, 0)
        kr.addWidget(self.e_keyfile, 1)
        kr.addWidget(b_key)
        self.l_keyfile = QLabel("Private key file:")
        self.e_warehouse = QLineEdit(cfg.get("warehouse", ""))
        self.e_sfdb = QLineEdit(cfg.get("database", "") if cfg.get("provider") == "Snowflake" else "")
        self.e_role = QLineEdit(cfg.get("role", ""))
        for e, ph in ((self.e_warehouse, "e.g. COMPUTE_WH (optional: your default)"),
                      (self.e_sfdb, "e.g. ANALYTICS (optional)"), (self.e_role, "optional: your default role")):
            e.setPlaceholderText(ph)
        f.addRow("Account:", self.e_account)
        f.addRow("Sign in with:", self.cb_auth)
        f.addRow(self.l_keyfile, self.keyrow)
        f.addRow("Warehouse:", self.e_warehouse)
        f.addRow("Database:", self.e_sfdb)
        f.addRow("Role:", self.e_role)
        self.cb_auth.currentIndexChanged.connect(lambda _i: (self._provider_changed(), self._pw_mode_changed()))

        self.stack = QStackedWidget()
        for w in (sq, d2, od, sf):
            self.stack.addWidget(w)

        # --- sign-in (DB2/ODBC)
        self.signin = QWidget()
        f = QFormLayout(self.signin)
        f.setContentsMargins(0, 0, 0, 0)
        self.e_user = QLineEdit(cfg.get("user", ""))
        self.cb_pwmode = QComboBox()
        for key, text in PASSWORD_LABELS:
            self.cb_pwmode.addItem(text, key)
        keys = [k for k, _t in PASSWORD_LABELS]
        mode = cfg.get("passwordMode", "Prompt")
        self.cb_pwmode.setCurrentIndex(keys.index(mode) if mode in keys else 1)
        self.e_env = QLineEdit(cfg.get("passwordEnv", ""))
        self.e_env.setPlaceholderText("e.g. DB2_PASSWORD")
        self.e_pw = QLineEdit(cfg.get("password", ""))
        self.e_pw.setEchoMode(QLineEdit.Password)
        self.e_schema = QLineEdit(cfg.get("schema", ""))
        self.e_schema.setPlaceholderText("Optional: only list this schema's tables (and use it by default)")
        f.addRow("User name:", self.e_user)
        f.addRow("Password:", self.cb_pwmode)
        self.l_env = QLabel("Variable:")
        f.addRow(self.l_env, self.e_env)
        self.l_pw = QLabel("Password:")
        f.addRow(self.l_pw, self.e_pw)
        f.addRow("Schema:", self.e_schema)
        self.cb_pwmode.currentIndexChanged.connect(self._pw_mode_changed)

        # --- driver + test
        self.l_driver = QLabel()
        self.l_driver.setWordWrap(True)
        self.b_install = QPushButton("Install driver")
        self.b_install.clicked.connect(self._install_driver)
        drow = QHBoxLayout()
        drow.addWidget(self.l_driver, 1)
        drow.addWidget(self.b_install)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(90)
        self.log.hide()
        self.b_test = QPushButton("Test Connection")
        self.b_test.clicked.connect(self._test)
        self.l_test = QLabel()
        self.l_test.setWordWrap(True)
        trow = QHBoxLayout()
        trow.addWidget(self.b_test)
        trow.addWidget(self.l_test, 1)

        top = QFormLayout()
        top.addRow("Connection name:", self.e_name)
        top.addRow("Provider:", self.cb_provider)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.stack)
        lay.addWidget(self.signin)
        lay.addLayout(drow)
        lay.addWidget(self.log)
        lay.addStretch()
        lay.addLayout(trow)
        lay.addWidget(bb)
        self.cb_provider.currentIndexChanged.connect(self._provider_changed)
        self._provider_changed()
        self._pw_mode_changed()
        if not cfg:
            self.e_name.setText(self._unique("Database"))

    def _unique(self, base):
        n, name = 1, base
        while name.lower() in self.existing:
            n += 1
            name = f"{base}{n}"
        return name

    @property
    def provider(self):
        return dbcore.PROVIDERS[self.cb_provider.currentIndex()]

    def _provider_changed(self):
        self.stack.setCurrentIndex(self.cb_provider.currentIndex())
        self.signin.setVisible(self.provider != "SQLite")
        ok = driver_available(self.provider)
        if self.provider == "SQLite":
            self.l_driver.setText("<small>SQLite is built into Python - nothing to install.</small>")
        elif ok:
            self.l_driver.setText(f"<small>Driver: <b>{PIP_NAME[self.provider]}</b> is installed.</small>")
        else:
            extra = {"ODBC": " (also needs unixODBC: <tt>sudo apt install unixodbc</tt>)",
                     "DB2": " (includes IBM's CLI driver - no other setup needed)"}.get(self.provider, "")
            self.l_driver.setText(f"<small>Driver <b>{PIP_NAME[self.provider]}</b> isn't installed{extra}.</small>")
        self.b_install.setVisible(self.provider != "SQLite" and not ok)
        sso = self.provider == "Snowflake" and self.cb_auth.currentData() == "Browser SSO"
        keypair = self.provider == "Snowflake" and self.cb_auth.currentData() == "Key pair"
        self.l_keyfile.setVisible(keypair)
        self.keyrow.setVisible(keypair)
        word = "Passphrase" if keypair else "Password"
        lab = self.signin.layout().labelForField(self.cb_pwmode)
        if lab is not None:
            lab.setText(word + ":")
        self.l_pw.setText(word + ":")
        self.e_env.setPlaceholderText("e.g. SNOWFLAKE_KEY_PASSPHRASE" if keypair else "e.g. DB2_PASSWORD")
        for i, (key, text) in enumerate(PASSWORD_LABELS):
            self.cb_pwmode.setItemText(i, text.replace("password", "passphrase").replace("No passphrase",
                                       "No passphrase (the key file isn't encrypted)") if keypair else text)
        self.cb_pwmode.setEnabled(not sso)
        if sso:
            self.cb_pwmode.setCurrentIndex(0)          # No password: the browser signs you in
        self.cb_pwmode.setToolTip("Browser SSO doesn't use a password." if sso else
                                  ("The key file's passphrase, if it has one." if keypair else ""))

    def _browse_key(self):
        start = os.path.dirname(os.path.expanduser(self.e_keyfile.text())) or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Private key file", start,
                                              "Private keys (*.p8 *.pem *.key *.der);;All files (*)")
        if path:
            home = os.path.expanduser("~")
            self.e_keyfile.setText("~" + path[len(home):] if path.startswith(home + os.sep) else path)

    def _pw_mode_changed(self):
        mode = self.cb_pwmode.currentData()
        self.l_env.setVisible(mode == "Environment")
        self.e_env.setVisible(mode == "Environment")
        self.l_pw.setVisible(mode == "Saved")
        self.e_pw.setVisible(mode == "Saved")

    def _rel(self, path):
        try:
            rel = os.path.relpath(path, self.root)
        except ValueError:
            return path
        return path if rel.startswith("..") else rel

    def _browse(self):
        start = os.path.join(self.root, "data") if os.path.isdir(os.path.join(self.root, "data")) else self.root
        path, _ = QFileDialog.getOpenFileName(self, "Database file", start,
                                              "SQLite databases (*.db *.sqlite *.sqlite3 *.db3);;All files (*)")
        if path:
            self.e_file.setText(self._rel(path))

    def _new_db(self, sample=False):
        os.makedirs(os.path.join(self.root, "data"), exist_ok=True)
        default = os.path.join(self.root, "data", "sample.db" if sample else (self.e_name.text() or "data") + ".db")
        path, _ = QFileDialog.getSaveFileName(self, "Create database", default, "SQLite databases (*.db)")
        if not path:
            return
        if not path.lower().endswith((".db", ".sqlite", ".sqlite3", ".db3")):
            path += ".db"
        try:
            if os.path.exists(path):
                os.remove(path)
            if sample:
                dbcore.sample_database(path)
            else:
                import sqlite3
                sqlite3.connect(path).close()
        except OSError as e:
            QMessageBox.critical(self, "Create database", str(e))
            return
        self.e_file.setText(self._rel(path))
        if sample and self.e_name.text().startswith("Database"):
            self.e_name.setText(self._unique("Sample"))

    def _sample_db(self):
        self._new_db(sample=True)

    def config(self):
        p = self.provider
        cfg = {"name": self.e_name.text().strip(), "provider": p}
        if p == "SQLite":
            cfg["file"] = self.e_file.text().strip()
            return cfg
        if p == "Snowflake":
            cfg.update(account=self.e_account.text().strip(), auth=self.cb_auth.currentData())
            for key, e in (("warehouse", self.e_warehouse), ("database", self.e_sfdb), ("role", self.e_role)):
                if e.text().strip():
                    cfg[key] = e.text().strip()
            if cfg["auth"] == "Key pair":
                cfg["keyFile"] = self.e_keyfile.text().strip()
        elif p == "DB2":
            cfg.update(host=self.e_host.text().strip(), port=self.e_port.value(),
                       database=self.e_database.text().strip(), security=self.cb_security.currentText())
            if self.e_extra.text().strip():
                cfg["extra"] = self.e_extra.text().strip()
        else:
            if self.cb_dsn.currentText().strip():
                cfg["dsn"] = self.cb_dsn.currentText().strip()
            if self.e_cs.text().strip():
                cfg["connectionString"] = self.e_cs.text().strip()
        if self.e_user.text().strip():
            cfg["user"] = self.e_user.text().strip()
        cfg["passwordMode"] = self.cb_pwmode.currentData()
        if cfg["passwordMode"] == "Environment":
            cfg["passwordEnv"] = self.e_env.text().strip()
        if cfg["passwordMode"] == "Saved":
            cfg["password"] = self.e_pw.text()
        if self.e_schema.text().strip():
            cfg["schema"] = self.e_schema.text().strip()
        return cfg

    def _validate(self):
        cfg = self.config()
        if not cfg["name"]:
            return "Enter a connection name."
        if cfg["name"].lower() in self.existing and cfg["name"] != self.original_name:
            return f"There's already a connection named {cfg['name']}."
        if cfg["provider"] == "SQLite" and not cfg.get("file"):
            return "Choose a database file (or click New / Sample)."
        if cfg["provider"] == "DB2" and not (cfg.get("host") and cfg.get("database")):
            return "Enter the server and database."
        if cfg["provider"] == "Snowflake" and not (cfg.get("account") and cfg.get("user")):
            return "Enter the account and user name."
        if dbcore.is_key_pair(cfg) and not cfg.get("keyFile"):
            return "Choose the private key file (.p8)."
        if cfg["provider"] == "ODBC" and not (cfg.get("dsn") or cfg.get("connectionString")):
            return "Choose a DSN or enter a connection string."
        if cfg.get("passwordMode") == "Environment" and not cfg.get("passwordEnv"):
            return "Enter the environment variable that holds the password."
        return None

    def _test(self):
        err = self._validate()
        if err:
            self.l_test.setText(f"<span style='color:#f48771'>{err}</span>")
            return
        cfg = self.config()
        self.service.forget(cfg["name"])
        pw = self.service.password_for(cfg, self)
        if pw is None:
            return
        self.l_test.setText("Connecting...")
        self.b_test.setEnabled(False)

        def ok(n):
            self.b_test.setEnabled(True)
            self.l_test.setText(f"<span style='color:#89d185'>Test connection succeeded - {n} tables/views.</span>")

        def fail(msg):
            self.b_test.setEnabled(True)
            self.service.forget(cfg["name"])
            self.l_test.setText(f"<span style='color:#f48771'>{msg.splitlines()[0]}</span>")
        self.service.test(cfg, pw, ok, fail)

    def _install_driver(self):
        pkg = PIP_NAME[self.provider]
        pythons = [sys.executable]
        if self.project_python and os.path.abspath(self.project_python) != os.path.abspath(sys.executable):
            pythons.append(self.project_python)
        self.log.show()
        self.log.clear()
        self.b_install.setEnabled(False)
        self._pip_queue = [(py, pkg) for py in pythons]
        self._pip_next()

    def _pip_next(self):
        if not self._pip_queue:
            self.b_install.setEnabled(True)
            importlib.invalidate_caches()
            self._provider_changed()
            if driver_available(self.provider):
                self.log.appendPlainText("Done. (Restart PyStudio if the driver still isn't found.)")
            return
        py, pkg = self._pip_queue.pop(0)
        self.log.appendPlainText(f"$ {py} -m pip install {pkg}")
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.log.appendPlainText(bytes(self.proc.readAllStandardOutput()).decode(errors="replace").rstrip()))
        self.proc.finished.connect(lambda *_: self._pip_next())
        self.proc.start(py, ["-m", "pip", "install", "--disable-pip-version-check", pkg])

    def _ok(self):
        err = self._validate()
        if err:
            QMessageBox.warning(self, self.windowTitle(), err)
            return
        cfg = self.config()
        if cfg.get("passwordMode") == "Saved":
            r = QMessageBox.question(self, "Saved password",
                                     "The password will be stored as plain text in the project file and in "
                                     "published apps. Continue?")
            if r != QMessageBox.Yes:
                return
        self.accept()

    def reject(self):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
        super().reject()


class ServerExplorer(QWidget):
    open_query = Signal(str, str, bool)     # connection name, SQL, run now
    add_query = Signal(str, str, str)       # connection name, suggested name, SQL
    connections_changed = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.project = None
        self.project_python = None
        self._columns_cache = {}

        def tb(text, tip, slot, icon=None):
            b = QToolButton()
            if icon is not None:
                b.setIcon(self.style().standardIcon(icon))
            else:
                b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(slot)
            return b
        self.b_add = tb("+", "Add Connection...", self.add_connection)
        self.b_refresh = tb("", "Refresh", self.refresh, QStyle.SP_BrowserReload)
        from .icons import refresh as _refresh_icon
        self.b_refresh.setIcon(_refresh_icon())
        self.b_query = tb("SQL", "New Query (Ctrl+Shift+Q)", lambda: self.new_query())
        head = QHBoxLayout()
        head.setContentsMargins(4, 2, 4, 2)
        head.addWidget(QLabel("<b>Data Connections</b>"), 1)
        for b in (self.b_query, self.b_refresh, self.b_add):
            head.addWidget(b)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.empty = QLabel("Open a project, then click + to add a data connection\n(SQLite, DB2 or ODBC).")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: #888; padding: 12px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(head)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.empty)
        self.set_project(None)

    # ------------------------------------------------------------ model
    def connections(self):
        return list(self.project.data.get("connections", [])) if self.project else []

    def cfg(self, name):
        for c in self.connections():
            if c.get("name") == name:
                return c
        return None

    def set_project(self, project, project_python=None):
        self.project = project
        self.project_python = project_python
        self._columns_cache.clear()
        if project:
            self.service.base_dir = project.root
            self.service.project_key = project.root
        self.b_add.setEnabled(project is not None)
        self.b_query.setEnabled(project is not None)
        self.refresh()

    def refresh(self):
        expanded = {self._key(it) for it in self._iter() if it.isExpanded()}
        self._columns_cache.clear()
        self.tree.clear()
        icon = self.style().standardIcon(QStyle.SP_DriveNetIcon)
        for c in self.connections():
            it = QTreeWidgetItem([c["name"]])
            it.setIcon(0, icon)
            it.setToolTip(0, dbcore.connection_summary(c))
            it.setData(0, ROLE, ("conn", c["name"]))
            it.addChild(QTreeWidgetItem(["Loading..."]))
            self.tree.addTopLevelItem(it)
            if self._key(it) in expanded:
                it.setExpanded(True)
        has = self.tree.topLevelItemCount() > 0
        self.tree.setVisible(has)
        self.empty.setVisible(not has)

    def _iter(self):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            yield it
            stack.extend(it.child(i) for i in range(it.childCount()))

    def _key(self, it):
        d = it.data(0, ROLE)
        return tuple(d) if d else None

    def _conn_of(self, it):
        while it.parent():
            it = it.parent()
        d = it.data(0, ROLE)
        return d[1] if d else None

    # ------------------------------------------------------------ lazy loading
    def _expanded(self, it):
        d = it.data(0, ROLE)
        if not d or it.data(0, ROLE + 1):   # already loaded
            return
        kind = d[0]
        cfg = self.cfg(self._conn_of(it))
        if cfg is None:
            return
        pw = self.service.password_for(cfg, self)
        if pw is None:
            it.setExpanded(False)
            return
        it.setData(0, ROLE + 1, True)
        if kind == "conn":
            self.service.tables(cfg, pw, lambda rows, it=it: self._fill_tables(it, rows),
                                lambda msg, it=it, cfg=cfg: self._failed(it, cfg, msg))
        elif kind == "table":
            _k, _c, schema, table = d
            self.service.columns(cfg, pw, schema, table, lambda res, it=it: self._fill_columns(it, res),
                                 lambda msg, it=it, cfg=cfg: self._failed(it, cfg, msg))

    def _failed(self, it, cfg, msg):
        it.takeChildren()
        it.setData(0, ROLE + 1, False)
        err = QTreeWidgetItem([msg.splitlines()[0]])
        err.setForeground(0, Qt.red)
        err.setToolTip(0, msg)
        it.addChild(err)
        if it.parent() is None:
            self.service.forget(cfg["name"])   # wrong password? ask again next time
            it.addChild(QTreeWidgetItem(["(collapse and expand to retry)"]))

    def _fill_tables(self, it, rows):
        it.takeChildren()
        groups = {}
        folder_icon = self.style().standardIcon(QStyle.SP_DirIcon)
        table_icon = self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
        for kind, label in (("TABLE", "Tables"), ("VIEW", "Views")):
            g = QTreeWidgetItem([f"{label} ({sum(1 for r in rows if r[2] == kind)})"])
            g.setIcon(0, folder_icon)
            it.addChild(g)
            groups[kind] = g
        multi_schema = len({r[0] for r in rows}) > 1
        for schema, name, kind in rows:
            t = QTreeWidgetItem([f"{schema}.{name}" if multi_schema and schema else name])
            t.setIcon(0, table_icon)
            t.setData(0, ROLE, ("table", self._conn_of(it), schema, name))
            t.addChild(QTreeWidgetItem(["Loading..."]))
            groups[kind].addChild(t)
        groups["TABLE"].setExpanded(True)

    def _fill_columns(self, it, res):
        cols, fks = res
        it.takeChildren()
        _k, conn, schema, table = it.data(0, ROLE)
        self._columns_cache[(conn, schema, table)] = res
        fk_cols = {f[0] for f in fks}
        for name, typ, key in cols:
            mark = " [PK]" if key else (" [FK]" if name in fk_cols else "")
            c = QTreeWidgetItem([f"{name}  {typ}{mark}"])
            c.setData(0, ROLE, ("column", conn, schema, table, name))
            it.addChild(c)

    # ------------------------------------------------------------ actions
    def _sql_for(self, d):
        cfg = self.cfg(d[1]) or {}
        return f"SELECT *\nFROM {dbcore.qualified(d[2], d[3], cfg.get('provider'))}"

    def _double_clicked(self, it, _col):
        d = it.data(0, ROLE)
        if d and d[0] == "table":
            self.open_query.emit(d[1], self._sql_for(d), True)

    def new_query(self, conn=None):
        if conn is None:
            it = self.tree.currentItem()
            conn = self._conn_of(it) if it else (self.connections()[0]["name"] if self.connections() else "")
        self.open_query.emit(conn or "", "", False)

    def import_data(self, conn_name="", table=None, path=None):
        """CSV / Excel -> a table (Import Data wizard)."""
        from .importdata import ImportDataDialog
        if not self.connections():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Import Data", "Add a connection first (Server Explorer > Add Connection).")
            return None
        dlg = ImportDataDialog(self.service, self.connections(), conn_name, table, self.window())
        if path:
            dlg.set_file(path)
        dlg.exec()
        if dlg.imported:
            self.refresh()
        return dlg

    def _menu(self, pos):
        it = self.tree.itemAt(pos)
        m = QMenu(self)
        d = it.data(0, ROLE) if it else None
        if d and d[0] == "conn":
            m.addAction("New &Query", lambda: self.new_query(d[1]))
            m.addAction("&Import Data...", lambda: self.import_data(d[1]))
            m.addAction("&Refresh", self.refresh)
            m.addSeparator()
            m.addAction("&Modify Connection...", lambda: self.modify_connection(d[1]))
            m.addAction("&Delete Connection", lambda: self.delete_connection(d[1]))
        elif d and d[0] == "table":
            m.addAction("Show Table &Data", lambda: self.open_query.emit(d[1], self._sql_for(d), True))
            m.addAction("New &Query", lambda: self.open_query.emit(d[1], self._sql_for(d), False))
            m.addAction("&Add Query to Project...",
                        lambda: self.add_query.emit(d[1], "Get" + d[3].title().replace("_", ""), self._sql_for(d)))
            m.addAction("&Import Data into This Table...", lambda: self.import_data(d[1], (d[2], d[3])))
            m.addSeparator()
            m.addAction("&Copy Name", lambda: QGuiApplication.clipboard().setText(
                dbcore.qualified(d[2], d[3], (self.cfg(d[1]) or {}).get("provider"))))
        elif d and d[0] == "column":
            m.addAction("&Copy Name", lambda: QGuiApplication.clipboard().setText(d[4]))
        else:
            m.addAction("&Add Connection...", self.add_connection).setEnabled(self.project is not None)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _save(self, conns):
        self.project["connections"] = conns
        self.project.save()
        self.refresh()
        self.connections_changed.emit()

    def add_connection(self):
        if self.project is None:
            return
        dlg = ConnectionDialog(self.service, self.project.root, [c["name"] for c in self.connections()],
                               project_python=self.project_python, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self._save(self.connections() + [dlg.config()])

    def modify_connection(self, name):
        cfg = self.cfg(name)
        if cfg is None:
            return
        dlg = ConnectionDialog(self.service, self.project.root, [c["name"] for c in self.connections()],
                               cfg, project_python=self.project_python, parent=self)
        if dlg.exec() == QDialog.Accepted:
            new = dlg.config()
            self.service.forget(cfg)
            self._save([new if c.get("name") == name else c for c in self.connections()])

    def delete_connection(self, name):
        r = QMessageBox.question(self, "Delete Connection",
                                 f"Delete the connection {name}? (The database itself isn't touched.)")
        if r == QMessageBox.Yes:
            cfg = self.cfg(name)
            if cfg:
                self.service.forget(cfg)
            self._save([c for c in self.connections() if c.get("name") != name])
