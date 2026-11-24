"""Import Data: load a CSV or Excel file into a table on any connection (SQLite, ODBC, DB2, Snowflake),
like SQL Server's Import and Export Wizard.

Server Explorer > right-click a connection > Import Data..., or right-click a table > Import Data into This
Table..., or Tools > Import Data. Pick the file; the preview shows its first rows. Choose a new table (PyStudio
suggests a name and a type for each column from what's in it) or an existing one (columns are matched by
name). Untick columns you don't want, rename them, change types, then Import: the rows go in batches of 1,000
in one transaction where the database allows, so a failure leaves the table as it was."""
import csv
import datetime
import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
                               QRadioButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import dbcore

PREVIEW_ROWS = 100
BATCH = 1000
TYPES = ["Text", "Whole number", "Decimal number", "Date", "Date and time", "True/False"]


# ============================================================ reading files
def read_csv(path, delimiter=None):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    return [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_DATE_FMTS = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}


def xlsx_sheets(path):
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        return [s.get("name") for s in wb.find("m:sheets", _NS)]


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(path, sheet=None):
    """Rows of one worksheet (values: str, int, float, bool, date/datetime)."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = [(s.get("name"), s.get(f"{{{_NS['r']}}}id")) for s in wb.find("m:sheets", _NS)]
        name, rid = next(((n, r) for n, r in sheets if n == sheet), sheets[0])
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels if r.get("Id") == rid)
        target = target.lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        date_styles = set()
        if "xl/styles.xml" in names:
            st = ET.fromstring(z.read("xl/styles.xml"))
            custom = {}
            nf = st.find("m:numFmts", _NS)
            for f in (nf if nf is not None else []):
                code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", f.get("formatCode", "")).lower()
                custom[int(f.get("numFmtId"))] = bool(re.search(r"[dy]|mm?:|h", code))
            xfs = st.find("m:cellXfs", _NS)
            for i, xf in enumerate(xfs if xfs is not None else []):
                fid = int(xf.get("numFmtId", 0))
                if fid in _DATE_FMTS or custom.get(fid):
                    date_styles.add(i)
        epoch = datetime.datetime(1899, 12, 30)
        if b"date1904=\"1\"" in z.read("xl/workbook.xml") or b"date1904=\"true\"" in z.read("xl/workbook.xml"):
            epoch = datetime.datetime(1904, 1, 1)
        rows = []
        root = ET.fromstring(z.read(target))
        data = root.find("m:sheetData", _NS)
        for row in (data if data is not None else []):
            vals = {}
            for c in row.findall("m:c", _NS):
                ref, t, s = c.get("r"), c.get("t"), int(c.get("s", 0))
                v = c.find("m:v", _NS)
                text = v.text if v is not None else None
                if t == "s" and text is not None:
                    val = shared[int(text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
                elif t == "b":
                    val = text == "1"
                elif t in ("str", "e"):
                    val = text or ""
                elif text is None:
                    continue
                else:
                    num = float(text)
                    if s in date_styles:
                        d = epoch + datetime.timedelta(days=num)
                        val = d.date() if num == int(num) else d.replace(microsecond=0)
                    else:
                        val = int(num) if num.is_integer() and abs(num) < 2 ** 53 and "." not in text and \
                            "E" not in text.upper() else num
                vals[_col_index(ref) if ref else len(vals)] = val
            r_idx = int(row.get("r", len(rows) + 1)) - 1
            while len(rows) < r_idx:
                rows.append([])
            width = max(vals) + 1 if vals else 0
            rows.append([vals.get(i, "") for i in range(width)])
        return rows


def read_file(path, sheet=None, delimiter=None):
    if path.lower().endswith((".xlsx", ".xlsm")):
        return read_xlsx(path, sheet)
    return read_csv(path, delimiter)


# ============================================================ types
_INT = re.compile(r"^[+-]?\d{1,18}$")
_DEC = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d*)(\.\d+)?$")
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%Y/%m/%d")
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
               "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f")


def _parse(fmts, s):
    for f in fmts:
        try:
            return datetime.datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def convert(v, kind):
    """A file value as the column's type (None for empty). Raises ValueError when it doesn't fit."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if kind == "Text":
        if isinstance(v, datetime.datetime):
            return v.isoformat(sep=" ")
        if isinstance(v, datetime.date):
            return v.isoformat()
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    if kind == "Whole number":
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            if float(v) != int(v):
                raise ValueError(f"{v} isn't a whole number")
            return int(v)
        return int(str(v).strip().replace(",", ""))
    if kind == "Decimal number":
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        s = str(v).strip().replace(",", "").replace("$", "")
        if s.endswith("%"):
            return float(s[:-1]) / 100
        return float(s)
    if kind == "True/False":
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("true", "yes", "y", "1", "t"):
            return True
        if s in ("false", "no", "n", "0", "f"):
            return False
        raise ValueError(f"{v!r} isn't true/false")
    if kind == "Date":
        if isinstance(v, datetime.datetime):
            return v.date()
        if isinstance(v, datetime.date):
            return v
        d = _parse(_DATE_FORMATS, str(v).strip()) or _parse(_DT_FORMATS, str(v).strip())
        if d is None:
            raise ValueError(f"{v!r} isn't a date")
        return d.date()
    if kind == "Date and time":
        if isinstance(v, datetime.datetime):
            return v
        if isinstance(v, datetime.date):
            return datetime.datetime(v.year, v.month, v.day)
        d = _parse(_DT_FORMATS, str(v).strip()) or _parse(_DATE_FORMATS, str(v).strip())
        if d is None:
            raise ValueError(f"{v!r} isn't a date and time")
        return d
    return v


def guess_type(values):
    vals = [v for v in values if not (v is None or (isinstance(v, str) and not v.strip()))][:2000]
    if not vals:
        return "Text"
    for kind in ("True/False", "Whole number", "Decimal number", "Date", "Date and time"):
        if kind == "True/False" and not all(isinstance(v, bool) or str(v).strip().lower() in
                                            ("true", "false", "yes", "no") for v in vals):
            continue
        if kind == "Whole number" and not all(isinstance(v, int) and not isinstance(v, bool) or
                                              (isinstance(v, str) and _INT.match(v.strip().replace(",", ""))
                                               and not (len(v.strip()) > 1 and v.strip().startswith("0")))
                                              for v in vals):
            continue          # leading zeros (zip codes, IDs) stay text
        if kind == "Decimal number" and not all((isinstance(v, (int, float)) and not isinstance(v, bool)) or
                                                (isinstance(v, str) and _DEC.match(v.strip().replace("$", ""))
                                                 and any(ch.isdigit() for ch in v)
                                                 and not re.match(r"^[+-]?0\d", v.strip())) for v in vals):
            continue
        if kind == "Date" and not all(type(v) is datetime.date or (isinstance(v, str) and
                                                                    _parse(_DATE_FORMATS, v.strip())) for v in vals):
            continue
        if kind == "Date and time" and not all(isinstance(v, datetime.datetime) or (isinstance(v, str) and
                                               _parse(_DT_FORMATS, v.strip())) for v in vals):
            continue
        return kind
    return "Text"


def sql_type(kind, provider, max_len=0):
    n = max(50, 1 << max(0, int(max_len * 1.5)).bit_length()) if max_len else 255
    n = min(n, 4000)
    p = (provider or "").lower()
    if p == "sqlite":
        return {"Text": "TEXT", "Whole number": "INTEGER", "Decimal number": "REAL", "Date": "DATE",
                "Date and time": "TIMESTAMP", "True/False": "INTEGER"}[kind]
    if p == "snowflake":
        return {"Text": f"VARCHAR({n})", "Whole number": "NUMBER(38,0)", "Decimal number": "FLOAT", "Date": "DATE",
                "Date and time": "TIMESTAMP_NTZ", "True/False": "BOOLEAN"}[kind]
    if p == "db2":
        return {"Text": f"VARCHAR({n})", "Whole number": "BIGINT", "Decimal number": "DOUBLE", "Date": "DATE",
                "Date and time": "TIMESTAMP", "True/False": "SMALLINT"}[kind]
    return {"Text": f"VARCHAR({n})", "Whole number": "BIGINT", "Decimal number": "DOUBLE PRECISION", "Date": "DATE",
            "Date and time": "TIMESTAMP", "True/False": "SMALLINT"}[kind]


def clean_name(name, i):
    s = re.sub(r"\W+", "_", str(name or "").strip()).strip("_")
    if not s:
        s = f"Column{i + 1}"
    if s[0].isdigit():
        s = "C_" + s
    return s


# ============================================================ the import itself (worker thread)
def do_import(conn, cfg, plan, rows):
    """plan: {"schema", "table", "create": bool, "empty": bool, "columns": [(src index, name, kind)]}"""
    provider = (cfg.get("provider") or "")
    table = dbcore.qualified(plan.get("schema") or "", plan["table"], provider)
    cols = plan["columns"]
    cur = conn.cursor()
    sqlite = provider.lower() == "sqlite"
    if plan.get("create"):
        defs = ", ".join(f"{dbcore.quote_ident(name, provider)} {sql_type(kind, provider, ml)}"
                         for _i, name, kind, ml in cols)
        cur.execute(f"CREATE TABLE {table} ({defs})")
    elif plan.get("empty"):
        cur.execute(f"DELETE FROM {table}")
    marks = ", ".join("?" for _ in cols)
    insert = f"INSERT INTO {table} ({', '.join(dbcore.quote_ident(n, provider) for _i, n, _k, _m in cols)}) " \
             f"VALUES ({marks})"
    done = 0
    batch = []
    for rn, row in enumerate(rows, 1):
        vals = []
        for i, name, kind, _ml in cols:
            v = row[i] if i < len(row) else None
            try:
                v = convert(v, kind)
            except (ValueError, TypeError) as e:
                raise dbcore.DataError(f"Row {rn + plan.get('header_rows', 0)}, column {name}: {e}") from None
            if kind == "True/False" and not sqlite and provider.lower() != "snowflake" and v is not None:
                v = int(v)
            if sqlite and isinstance(v, (datetime.date, datetime.datetime)):
                v = v.isoformat(sep=" ") if isinstance(v, datetime.datetime) else v.isoformat()
            vals.append(v)
        batch.append(vals)
        if len(batch) >= BATCH:
            cur.executemany(insert, batch)
            done += len(batch)
            batch = []
    if batch:
        cur.executemany(insert, batch)
        done += len(batch)
    try:
        conn.commit()
    except Exception:  # noqa: BLE001 - autocommit connections
        pass
    return done


# ============================================================ the wizard
class ImportDataDialog(QDialog):
    def __init__(self, service, connections, conn_name="", table=None, parent=None):
        """table: (schema, name) to import into an existing table."""
        super().__init__(parent)
        self.service = service
        self.conns = {c["name"]: c for c in connections}
        self.setWindowTitle("Import Data")
        self.resize(1000, 700)
        self.rows = []
        self.existing_cols = []
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.e_file = QLineEdit()
        self.e_file.setPlaceholderText("A .csv or .xlsx file")
        b_file = QPushButton("Browse...")
        b_file.clicked.connect(self.browse)
        h = QHBoxLayout()
        h.addWidget(self.e_file, 1)
        h.addWidget(b_file)
        f.addRow("File:", h)
        h2 = QHBoxLayout()
        self.cb_sheet = QComboBox()
        self.cb_sheet.currentIndexChanged.connect(lambda _i: self.load())
        self.cb_delim = QComboBox()
        for text, d in (("Automatic", None), ("Comma", ","), ("Tab", "\t"), ("Semicolon", ";"), ("Pipe |", "|")):
            self.cb_delim.addItem(text, d)
        self.cb_delim.currentIndexChanged.connect(lambda _i: self.load())
        self.ck_header = QCheckBox("First row has the column names")
        self.ck_header.setChecked(True)
        self.ck_header.toggled.connect(lambda _c: self.load())
        self.l_sheet = QLabel("Sheet:")
        self.l_delim = QLabel("Separator:")
        for w in (self.l_sheet, self.cb_sheet, self.l_delim, self.cb_delim, self.ck_header):
            h2.addWidget(w)
        h2.addStretch()
        f.addRow("", h2)
        self.cb_conn = QComboBox()
        self.cb_conn.addItems(list(self.conns))
        if conn_name:
            self.cb_conn.setCurrentText(conn_name)
        self.cb_conn.currentTextChanged.connect(lambda _t: self.load_tables())
        f.addRow("Into connection:", self.cb_conn)
        self.rb_new = QRadioButton("New table:")
        self.e_table = QLineEdit()
        self.e_schema = QLineEdit()
        self.e_schema.setPlaceholderText("schema (optional)")
        self.e_schema.setMaximumWidth(180)
        self.rb_existing = QRadioButton("Existing table:")
        self.cb_table = QComboBox()
        self.cb_table.currentIndexChanged.connect(lambda _i: self.load_existing_columns())
        self.ck_empty = QCheckBox("Delete its rows first")
        hn = QHBoxLayout()
        hn.addWidget(self.rb_new)
        hn.addWidget(self.e_schema)
        hn.addWidget(self.e_table, 1)
        he = QHBoxLayout()
        he.addWidget(self.rb_existing)
        he.addWidget(self.cb_table, 1)
        he.addWidget(self.ck_empty)
        f.addRow("", hn)
        f.addRow("", he)
        self.rb_new.setChecked(True)
        self.rb_new.toggled.connect(lambda _c: self.fill_mapping())
        v.addLayout(f)
        split = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.addWidget(QLabel("<b>Columns</b> - untick the ones to leave out; rename them or change their type"))
        self.map = QTableWidget(0, 4)
        self.map.setHorizontalHeaderLabels(["Import", "From the file", "Into column", "Type"])
        self.map.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.map.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.map.verticalHeader().hide()
        tv.addWidget(self.map)
        split.addWidget(top)
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0)
        self.l_preview = QLabel("<b>Preview</b>")
        bv.addWidget(self.l_preview)
        self.preview = QTableWidget()
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        bv.addWidget(self.preview)
        split.addWidget(bottom)
        v.addWidget(split, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        self.b_import = bb.addButton("Import", QDialogButtonBox.AcceptRole)
        self.b_import.clicked.connect(self.run_import)
        self.b_import.setEnabled(False)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._want_table = table
        self.imported = 0
        self.load_tables()
        self._sheet_controls()

    # ------------------------------------------------------------ file
    def browse(self):
        p, _f = QFileDialog.getOpenFileName(self, "Import Data", os.path.expanduser("~"),
                                            "Data files (*.csv *.txt *.tsv *.xlsx *.xlsm);;All files (*)")
        if p:
            self.set_file(p)

    def set_file(self, path):
        self.e_file.setText(path)
        self.cb_sheet.blockSignals(True)
        self.cb_sheet.clear()
        if path.lower().endswith((".xlsx", ".xlsm")):
            try:
                self.cb_sheet.addItems(xlsx_sheets(path))
            except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as e:
                self.status.setText(f"Couldn't read the workbook: {e}")
        self.cb_sheet.blockSignals(False)
        self._sheet_controls()
        if not self.e_table.text():
            self.e_table.setText(clean_name(os.path.splitext(os.path.basename(path))[0], 0))
        self.load()

    def _sheet_controls(self):
        xl = self.cb_sheet.count() > 0
        self.l_sheet.setVisible(xl)
        self.cb_sheet.setVisible(xl)
        self.l_delim.setVisible(not xl)
        self.cb_delim.setVisible(not xl)

    def load(self):
        path = self.e_file.text().strip()
        if not path or not os.path.isfile(path):
            return
        try:
            rows = read_file(path, self.cb_sheet.currentText() or None, self.cb_delim.currentData())
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"<span style='color:#f48771'>Couldn't read {os.path.basename(path)}: {e}</span>")
            return
        while rows and not any(str(x).strip() for x in rows[-1]):
            rows.pop()
        width = max((len(r) for r in rows), default=0)
        if self.ck_header.isChecked() and rows:
            names = [str(x) for x in rows[0]] + [""] * (width - len(rows[0]))
            data = rows[1:]
        else:
            names = [""] * width
            data = rows
        self.src_names = names
        self.rows = data
        self.kinds = [guess_type([r[i] if i < len(r) else None for r in data]) for i in range(width)]
        self.max_len = [max((len(str(r[i])) for r in data if i < len(r)), default=0) for i in range(width)]
        self.fill_mapping()
        self.fill_preview()

    def fill_preview(self):
        width = len(self.src_names)
        self.preview.clear()
        self.preview.setColumnCount(width)
        self.preview.setHorizontalHeaderLabels([n or f"Column{i + 1}" for i, n in enumerate(self.src_names)])
        show = self.rows[:PREVIEW_ROWS]
        self.preview.setRowCount(len(show))
        for r, row in enumerate(show):
            for c in range(width):
                v = row[c] if c < len(row) else ""
                self.preview.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))
        self.l_preview.setText(f"<b>Preview</b> - {len(self.rows):,} rows" +
                               (f" (the first {PREVIEW_ROWS} shown)" if len(self.rows) > PREVIEW_ROWS else ""))

    # ------------------------------------------------------------ target
    def cfg(self):
        return self.conns.get(self.cb_conn.currentText())

    def load_tables(self):
        cfg = self.cfg()
        self.cb_table.clear()
        if not cfg:
            return
        pw = self.service.password_for(cfg, self)
        if pw is None:
            return

        def done(rows):
            self.cb_table.blockSignals(True)
            self.cb_table.clear()
            for s, n, kind in rows:
                if kind != "VIEW":
                    self.cb_table.addItem(f"{s}.{n}" if s else n, (s, n))
            self.cb_table.blockSignals(False)
            if self._want_table:
                ws, wn = self._want_table
                i = next((k for k in range(self.cb_table.count())
                          if (self.cb_table.itemData(k)[1] or "").lower() == (wn or "").lower()
                          and (not ws or (self.cb_table.itemData(k)[0] or "").lower() == ws.lower())), -1)
                if i >= 0:
                    self.cb_table.setCurrentIndex(i)
                    self.rb_existing.setChecked(True)
                self._want_table = None
            self.load_existing_columns()
        self.service.tables(cfg, pw, done, lambda m: self.status.setText(f"Couldn't list the tables: {m}"))

    def load_existing_columns(self):
        cfg, t = self.cfg(), self.cb_table.currentData()
        if not cfg or not t:
            self.existing_cols = []
            return
        pw = self.service.password_for(cfg, self)

        def done(res):
            self.existing_cols = [c[0] for c in res[0]]
            if self.rb_existing.isChecked():
                self.fill_mapping()
        self.service.columns(cfg, pw, t[0], t[1], done, lambda m: None)

    def fill_mapping(self):
        names = getattr(self, "src_names", [])
        existing = self.rb_existing.isChecked()
        self.map.setRowCount(len(names))
        lower = {c.lower(): c for c in self.existing_cols}
        for i, n in enumerate(names):
            ck = QTableWidgetItem()
            ck.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            target = clean_name(n, i)
            match = lower.get(target.lower()) or lower.get(str(n).strip().lower())
            ck.setCheckState(Qt.Checked if (not existing or match) else Qt.Unchecked)
            self.map.setItem(i, 0, ck)
            src = QTableWidgetItem(n or f"Column{i + 1}")
            src.setFlags(Qt.ItemIsEnabled)
            self.map.setItem(i, 1, src)
            if existing:
                cb = QComboBox()
                cb.addItems(self.existing_cols)
                if match:
                    cb.setCurrentText(match)
                self.map.setCellWidget(i, 2, cb)
            else:
                self.map.removeCellWidget(i, 2)
                self.map.setItem(i, 2, QTableWidgetItem(target))
            tcb = QComboBox()
            tcb.addItems(TYPES)
            tcb.setCurrentText(self.kinds[i] if i < len(self.kinds) else "Text")
            tcb.setToolTip("How the values are read" + ("" if not existing else " (the table's own type is kept)"))
            self.map.setCellWidget(i, 3, tcb)
        self.b_import.setEnabled(bool(names))

    def plan(self):
        cols = []
        for i in range(self.map.rowCount()):
            if self.map.item(i, 0).checkState() != Qt.Checked:
                continue
            w = self.map.cellWidget(i, 2)
            name = w.currentText() if w is not None else self.map.item(i, 2).text().strip()
            kind = self.map.cellWidget(i, 3).currentText()
            cols.append((i, name or clean_name("", i), kind, self.max_len[i] if i < len(self.max_len) else 0))
        if self.rb_existing.isChecked():
            t = self.cb_table.currentData() or ("", "")
            return {"schema": t[0], "table": t[1], "create": False, "empty": self.ck_empty.isChecked(),
                    "columns": cols, "header_rows": 1 if self.ck_header.isChecked() else 0}
        return {"schema": self.e_schema.text().strip(), "table": self.e_table.text().strip(), "create": True,
                "empty": False, "columns": cols, "header_rows": 1 if self.ck_header.isChecked() else 0}

    def run_import(self):
        cfg = self.cfg()
        plan = self.plan()
        if not cfg or not plan["table"] or not plan["columns"]:
            self.status.setText("Choose the connection, the table, and at least one column.")
            return
        names = [c[1].lower() for c in plan["columns"]]
        if len(set(names)) != len(names):
            self.status.setText("Two columns would get the same name - rename one.")
            return
        if plan["create"] and (plan["table"].lower() in {self.cb_table.itemData(i)[1].lower()
                                                          for i in range(self.cb_table.count())}):
            self.status.setText(f"There's already a table {plan['table']} - pick Existing table, or another name.")
            return
        pw = self.service.password_for(cfg, self)
        if pw is None:
            return
        rows = self.rows
        self.b_import.setEnabled(False)
        self.status.setText(f"Importing {len(rows):,} rows...")

        def done(n):
            self.imported = n
            self.b_import.setEnabled(True)
            self.status.setText(f"<span style='color:#89d185'>Imported {n:,} rows into {plan['table']}.</span>")
            self.done_plan = plan
            if plan.get("create"):
                self._want_table = (plan.get("schema") or "", plan["table"])
                self.load_tables()                  # the new table is now an existing one

        def failed(msg):
            self.b_import.setEnabled(True)
            self.status.setText(f"<span style='color:#f48771'>Nothing was imported: {msg}</span>")
        self.service.submit(self.service._with_conn(cfg, pw, lambda c: self._import(c, cfg, plan, rows)),
                            done, failed)

    @staticmethod
    def _import(conn, cfg, plan, rows):
        """All or nothing: one transaction where the database allows; a table this import created is
        dropped again if it fails."""
        restore = _autocommit_off(conn)
        try:
            return do_import(conn, cfg, plan, rows)
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            if plan.get("create"):
                try:
                    t = dbcore.qualified(plan.get("schema") or "", plan["table"], cfg.get("provider"))
                    conn.cursor().execute(f"DROP TABLE {t}")
                    conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            restore()


def _autocommit_off(conn):
    """Snowflake (autocommit(bool) method) and pyodbc (autocommit attribute) commit each statement by
    default; turn that off for the import. Returns a function that puts it back."""
    try:
        if callable(getattr(conn, "autocommit", None)) and type(conn).__module__.startswith("snowflake"):
            conn.autocommit(False)
            return lambda: conn.autocommit(True)
        if isinstance(getattr(conn, "autocommit", None), bool) and conn.autocommit:
            conn.autocommit = False

            def back():
                conn.autocommit = True
            return back
    except Exception:  # noqa: BLE001
        pass
    return lambda: None

