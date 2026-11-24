"""Runs database work on a background thread so a slow DB2 query never freezes PyStudio."""
import json
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout

from . import dbcore


class _Relay(QObject):
    done = Signal(object, object)    # callback, result
    failed = Signal(object, str)     # callback, message


class _Job(QRunnable):
    def __init__(self, fn, relay, on_done, on_error):
        super().__init__()
        self.fn, self.relay, self.on_done, self.on_error = fn, relay, on_done, on_error

    def run(self):
        try:
            try:
                result = self.fn()
            except dbcore.DataError as e:
                self.relay.failed.emit(self.on_error, str(e))
            except Exception as e:  # driver errors, bugs
                self.relay.failed.emit(self.on_error, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
            else:
                self.relay.done.emit(self.on_done, result)
        except RuntimeError:   # PyStudio closed while the query was running
            pass


class PasswordDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Connect to {cfg.get('name')}")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(dbcore.connection_summary(cfg)))
        f = QFormLayout()
        self.user = QLabel(cfg.get("user") or "(none)")
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        f.addRow("User:", self.user)
        if dbcore.is_key_pair(cfg):
            f.addRow("Key file:", QLabel(cfg.get("keyFile", "")))
        f.addRow(dbcore.secret_name(cfg) + ":", self.pw)
        lay.addLayout(f)
        self.remember = QCheckBox("Remember in PyStudio on this computer (not stored in the project)")
        lay.addWidget(self.remember)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class DataService(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)     # one statement at a time, connections stay on one thread
        self.pool.setExpiryTimeout(-1)
        self.relay = _Relay()
        self.relay.done.connect(lambda cb, r: cb and cb(r))
        self.relay.failed.connect(lambda cb, m: cb and cb(m))
        self._conns = {}       # key -> DB-API connection (used only on the worker thread)
        self._passwords = {}   # connection name -> password typed this session
        self.base_dir = "."
        self.project_key = ""

    # ------------------------------------------------------------ passwords (GUI thread)
    def _settings_key(self, cfg):
        return f"dbpasswords/{self.project_key}/{cfg.get('name')}"

    def password_for(self, cfg, parent=None):
        """The password for cfg, asking if needed. None = the user canceled."""
        mode = cfg.get("passwordMode", "None")
        if mode != "Prompt":
            try:
                return dbcore.resolve_password(cfg)
            except dbcore.DataError:
                return ""   # the error shows when connecting
        name = cfg.get("name")
        if name in self._passwords:
            return self._passwords[name]
        from . import keystore
        saved = keystore.get(self._settings_key(cfg))
        if saved:
            self._passwords[name] = saved
            return saved
        dlg = PasswordDialog(cfg, parent)
        if dlg.exec() != QDialog.Accepted:
            return None
        self._passwords[name] = dlg.pw.text()
        if dlg.remember.isChecked():
            from . import keystore
            keystore.set(self._settings_key(cfg), dlg.pw.text())
        return dlg.pw.text()

    def known_password(self, name):
        return self._passwords.get(name)

    def forget(self, cfg_or_name):
        name = cfg_or_name.get("name") if isinstance(cfg_or_name, dict) else cfg_or_name
        self._passwords.pop(name, None)
        if isinstance(cfg_or_name, dict):
            from . import keystore
            keystore.delete(self._settings_key(cfg_or_name))
        stale = [k for k in self._conns if k[0] == name]

        def close():
            for k in stale:
                c = self._conns.pop(k, None)
                try:
                    c and c.close()
                except Exception:
                    pass
        self.pool.start(_Job(close, self.relay, None, None))

    # ------------------------------------------------------------ worker side
    def _conn(self, cfg, password):
        key = (cfg.get("name"), json.dumps(cfg, sort_keys=True), password)
        c = self._conns.get(key)
        if c is None:
            c = dbcore.connect(cfg, password, self.base_dir)
            self._conns[key] = c
        return c

    def _drop(self, cfg, password):
        key = (cfg.get("name"), json.dumps(cfg, sort_keys=True), password)
        c = self._conns.pop(key, None)
        try:
            c and c.close()
        except Exception:
            pass

    def _with_conn(self, cfg, password, work):
        def job():
            try:
                return work(self._conn(cfg, password))
            except dbcore.DataError:
                self._drop(cfg, password)   # e.g. lost connection: reconnect next time
                raise
        return job

    # ------------------------------------------------------------ API (GUI thread)
    def submit(self, fn, on_done, on_error):
        self.pool.start(_Job(fn, self.relay, on_done, on_error))

    def test(self, cfg, password, on_done, on_error):
        def job():
            c = dbcore.connect(cfg, password, self.base_dir)
            try:
                n = len(dbcore.schema_tables(c, cfg))
            finally:
                c.close()
            return n
        self.submit(job, on_done, on_error)

    def tables(self, cfg, password, on_done, on_error):
        self.submit(self._with_conn(cfg, password, lambda c: dbcore.schema_tables(c, cfg)), on_done, on_error)

    def columns(self, cfg, password, schema, table, on_done, on_error):
        self.submit(self._with_conn(cfg, password, lambda c: (
            dbcore.schema_columns(c, cfg, schema, table), dbcore.schema_foreign_keys(c, cfg, schema, table))),
            on_done, on_error)

    def run_script(self, cfg, password, statements, params, limit, on_done, on_error):
        """Run statements in order; returns [(sql, DataTable)]."""
        def work(c):
            out = []
            for sql in statements:
                names = dbcore.find_params(sql)
                out.append((sql, dbcore.run(c, sql, {n: params.get(n) for n in names}, limit)))
            return out
        self.submit(self._with_conn(cfg, password, work), on_done, on_error)
