# ---------------------------------------------------------------- PyStudio data core
# Pure Python (no Qt). Used by the PyStudio IDE and appended to pystudio_runtime.py,
# so apps get exactly the same connection and query behavior as the IDE.
import datetime as _dt
import decimal as _decimal
import os as _os
import re as _re
import sqlite3 as _sqlite3
import time as _time

PROVIDERS = ["SQLite", "DB2", "ODBC", "Snowflake"]
PROVIDER_MODULE = {"SQLite": "sqlite3", "DB2": "ibm_db", "ODBC": "pyodbc",
                   "Snowflake": "snowflake.connector"}
PASSWORD_MODES = ["None", "Prompt", "Environment", "Saved"]


class DataError(Exception):
    pass


# ------------------------------------------------------------------ rows
class Row(tuple):
    """One result row: row[0], row["NAME"] or row.NAME (names are case-insensitive)."""

    def __new__(cls, values, index):
        r = super().__new__(cls, values)
        r._index = index
        return r

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._index[key.lower()])
            except KeyError:
                raise KeyError(f"No column named {key!r}") from None
        return tuple.__getitem__(self, key)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return tuple.__getitem__(self, self._index[name.lower()])
        except KeyError:
            raise AttributeError(f"No column named {name!r}") from None

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def to_dict(self):
        names = getattr(self, "_names", None) or sorted(self._index, key=self._index.get)
        return dict(zip(names, self))


class DataTable(list):
    """Query results: a list of Row objects plus .columns (like a DataTable)."""

    def __init__(self, columns=(), rows=(), more=False, rowcount=-1, elapsed=0.0):
        self.columns = list(columns)
        index = {}
        for i, c in enumerate(self.columns):
            index.setdefault(str(c).lower(), i)
        self._index = index
        super().__init__(Row(r, index) for r in rows)
        for r in self:
            r._names = self.columns
        self.more = more              # True when a row limit cut the results short
        self.rowcount = rowcount
        self.elapsed = elapsed

    def column(self, key):
        """All values of one column (by name or index)."""
        i = self._index[key.lower()] if isinstance(key, str) else key
        return [tuple.__getitem__(r, i) for r in self]

    def to_dicts(self):
        return [dict(zip(self.columns, r)) for r in self]

    def first(self):
        return self[0] if self else None


# ------------------------------------------------------------------ SQL text
_TOKEN = _re.compile(r"""('(?:[^']|'')*')|("(?:[^"]|"")*")|(--[^\n]*)|(/\*.*?\*/)|(::)|(?<![:\w]):([A-Za-z_]\w*)""",
                     _re.S)


def find_params(sql):
    """Named parameters (:name) in order of first use, skipping strings and comments."""
    out = []
    for m in _TOKEN.finditer(sql):
        name = m.group(6)
        if name and name not in out:
            out.append(name)
    return out


def to_qmark(sql, params):
    """Convert :name parameters to ? placeholders. Returns (sql, args)."""
    args = []
    missing = []

    def rep(m):
        name = m.group(6)
        if not name:
            return m.group(0)
        if name not in params:
            missing.append(name)
            return m.group(0)
        args.append(params[name])
        return "?"
    out = _TOKEN.sub(rep, sql)
    if missing:
        raise DataError("No value given for parameter(s): " + ", ".join(dict.fromkeys(missing)))
    return out, args


def split_statements(sql):
    """Split a script on ; outside strings and comments. Empty statements are dropped."""
    parts, start = [], 0
    for m in _re.finditer(r"""('(?:[^']|'')*')|("(?:[^"]|"")*")|(--[^\n]*)|(/\*.*?\*/)|(;)""", sql, _re.S):
        if m.group(5):
            parts.append(sql[start:m.start()])
            start = m.end()
    parts.append(sql[start:])
    return [p.strip() for p in parts if _strip_comments(p).strip()]


def select_columns(sql):
    """Column names a SELECT returns, from its text (None when it uses *)."""
    text = _strip_comments(sql).strip().rstrip(";")
    m = _re.match(r"\s*select\s+(?:distinct\s+)?", text, _re.I)
    if not m:
        return None
    depth, start, items, i = 0, m.end(), [], m.end()
    quote = None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch == ",":
            items.append(text[start:i])
            start = i + 1
        elif depth == 0 and _re.match(r"\bfrom\b", text[i:i + 5], _re.I) and text[i - 1:i].isspace():
            break
        i += 1
    items.append(text[start:i])
    out = []
    for it in (x.strip() for x in items):
        if not it:
            continue
        if it == "*" or it.endswith(".*"):
            return None
        a = _re.search(r"(?:\bas\s+|\s)(\"[^\"]+\"|[A-Za-z_]\w*)\s*$", it, _re.I)
        if a and not _re.fullmatch(r"[\w.\"]+", it):
            out.append(a.group(1).strip('"'))
        else:
            out.append(it.split(".")[-1].strip('"'))
    return out


def _strip_comments(sql):
    return _re.sub(r"""('(?:[^']|'')*')|--[^\n]*|/\*.*?\*/""",
                   lambda m: m.group(1) or "", sql, flags=_re.S)


def is_select(sql):
    s = _strip_comments(sql).lstrip().lower()
    return s.startswith(("select", "with", "values", "pragma", "explain", "show", "call"))


# ------------------------------------------------------------------ query files
def parse_query_file(text):
    """A .sql query file: '-- connection: Name', '-- param: name = default' and
    '-- description: ...' header lines, then the SQL.
    Returns {"connection", "params": {name: default or None}, "description", "sql"}."""
    conn, desc, defaults = "", "", {}
    body = []
    header = True
    for line in text.splitlines():
        s = line.strip()
        if header and s.startswith("--"):
            m = _re.match(r"--\s*(connection|param|description)\s*:\s*(.*)$", s, _re.I)
            if m:
                key, val = m.group(1).lower(), m.group(2).strip()
                if key == "connection":
                    conn = val
                elif key == "description":
                    desc = val
                else:
                    pm = _re.match(r"([A-Za-z_]\w*)\s*(?:=\s*(.*))?$", val)
                    if pm:
                        defaults[pm.group(1)] = _unquote(pm.group(2)) if pm.group(2) is not None else None
                continue
            if s.lower().startswith("-- pystudio query"):
                continue
        if header and not s:
            continue
        header = False
        body.append(line)
    sql = "\n".join(body).strip()
    params = {n: defaults.get(n) for n in find_params(sql)}
    return {"connection": conn, "params": params, "description": desc, "sql": sql}


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def format_query_file(connection, sql, params=None, description=""):
    lines = ["-- PyStudio query"]
    if connection:
        lines.append(f"-- connection: {connection}")
    if description:
        lines.append(f"-- description: {description}")
    for name, default in (params or {}).items():
        if default is not None and default != "":
            lines.append(f"-- param: {name} = {default}")
    return "\n".join(lines) + "\n\n" + sql.strip() + "\n"


def coerce_param(text):
    """Turn a typed parameter value into int/float when it looks like one."""
    if not isinstance(text, str):
        return text
    s = text.strip()
    if s.upper() == "NULL":
        return None
    if _re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    if _re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+)", s):
        return float(s)
    return text


# ------------------------------------------------------------------ connections
def connection_summary(cfg):
    p = cfg.get("provider", "SQLite")
    if p == "SQLite":
        return f"SQLite: {cfg.get('file', '')}"
    if p == "DB2":
        return f"DB2: {cfg.get('database', '')} on {cfg.get('host', '')}:{cfg.get('port', 50000)}"
    if p == "Snowflake":
        where = ".".join(x for x in (cfg.get("database", ""), cfg.get("schema", "")) if x)
        return f"Snowflake: {cfg.get('account', '')}" + (f" ({where})" if where else "")
    return "ODBC: " + (f"DSN={cfg['dsn']}" if cfg.get("dsn") else (cfg.get("connectionString", "")[:60]))


def _expand(text):
    """${NAME} -> environment variable NAME (so secrets can stay out of the project)."""
    return _re.sub(r"\$\{(\w+)\}", lambda m: _os.environ.get(m.group(1), ""), text or "")


def resolve_password(cfg, prompt=None):
    """The password for a connection according to its password mode.
    prompt(cfg) is called for "Prompt" mode (GUI apps pass a dialog)."""
    mode = cfg.get("passwordMode", "None")
    if mode == "Saved":
        return cfg.get("password", "")
    if mode == "Environment":
        var = cfg.get("passwordEnv", "")
        if var not in _os.environ:
            raise DataError(f"Environment variable {var} isn't set (password for {cfg.get('name')})")
        return _os.environ[var]
    if mode == "Prompt":
        env = "PYSTUDIO_PWD_" + _re.sub(r"\W", "_", cfg.get("name", "")).upper()
        if env in _os.environ:          # handed over by PyStudio when you press F5
            return _os.environ[env]
        if prompt is None:
            raise DataError(f"A {secret_name(cfg).lower()} is needed for {cfg.get('name')}")
        pw = prompt(cfg)
        if pw is None:
            raise DataError("Canceled")
        return pw
    return ""


def needs_password(cfg):
    return cfg.get("passwordMode", "None") == "Prompt"


def connect(cfg, password="", base_dir="."):
    """Open a DB-API connection for a connection definition (a dict from the .pyproj)."""
    provider = cfg.get("provider", "SQLite")
    if provider == "SQLite":
        path = _os.path.expanduser(_expand(cfg.get("file", "")))
        if not path:
            raise DataError("No database file set")
        if path != ":memory:" and not _os.path.isabs(path):
            path = _os.path.join(base_dir, path)
        if path != ":memory:" and not _os.path.exists(path) and not cfg.get("create"):
            raise DataError(f"Database file not found: {path}")
        conn = _sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    if provider == "DB2":
        try:
            import ibm_db_dbi
        except ImportError:
            raise DataError("The DB2 driver isn't installed. In PyStudio: Project Properties > "
                            "Packages > install ibm_db") from None
        parts = [f"DATABASE={cfg.get('database', '')}", f"HOSTNAME={cfg.get('host', '')}",
                 f"PORT={cfg.get('port', 50000)}", "PROTOCOL=TCPIP"]
        if cfg.get("user"):
            parts.append(f"UID={_expand(cfg['user'])}")
        if password:
            parts.append(f"PWD={password}")
        if cfg.get("security") == "SSL":
            parts.append("SECURITY=SSL")
        extra = _expand(cfg.get("extra", "")).strip().strip(";")
        if extra:
            parts.append(extra)
        if cfg.get("schema"):
            parts.append(f"CURRENTSCHEMA={cfg['schema']}")
        try:
            return ibm_db_dbi.connect(";".join(parts) + ";", "", "")
        except Exception as e:
            raise DataError(_db2_message(e)) from None
    if provider == "ODBC":
        try:
            import pyodbc
        except ImportError as e:
            msg = str(e)
            if "libodbc" in msg:
                raise DataError("pyodbc needs unixODBC:  sudo apt install unixodbc") from None
            raise DataError("The ODBC driver module isn't installed. In PyStudio: Project Properties > "
                            "Packages > install pyodbc") from None
        if cfg.get("dsn"):
            cs = f"DSN={cfg['dsn']}"
        else:
            cs = _expand(cfg.get("connectionString", "")).strip().strip(";")
        if cfg.get("user"):
            cs += f";UID={_expand(cfg['user'])}"
        if password:
            cs += f";PWD={password}"
        try:
            return pyodbc.connect(cs, autocommit=bool(cfg.get("autocommit", True)))
        except Exception as e:
            raise DataError(str(e)) from None
    if provider == "Snowflake":
        return _snowflake_connect(cfg, password)
    raise DataError(f"Unknown provider {provider}")


SNOWFLAKE_AUTH = ["Password", "Browser SSO", "Key pair"]
SNOWFLAKE_AUTH_LABELS = ["Password", "Browser SSO (company single sign-on)",
                         "Key pair (.p8 key file + passphrase)"]


def is_key_pair(cfg):
    return cfg.get("provider") == "Snowflake" and cfg.get("auth") == "Key pair"


def secret_name(cfg):
    """What the 'password' of a connection is called in prompts."""
    return "Key passphrase" if is_key_pair(cfg) else "Password"


def load_private_key(path, passphrase=""):
    """A private key file (.p8 / .pem, encrypted with a passphrase or not, or DER) -> unencrypted PKCS#8
    DER bytes, which is what the Snowflake driver takes (works with every driver version)."""
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise DataError("Reading a key file needs the 'cryptography' package (it comes with "
                        "snowflake-connector-python): Project Properties > Packages") from None
    path = _os.path.expanduser(_expand(path or ""))
    if not path or not _os.path.exists(path):
        raise DataError(f"Private key file not found: {path or '(not set)'}")
    with open(path, "rb") as f:
        data = f.read()
    pw = passphrase.encode("utf-8") if passphrase else None
    pem = b"-----BEGIN" in data
    load = serialization.load_pem_private_key if pem else serialization.load_der_private_key
    try:
        key = load(data, password=pw)
    except TypeError:
        if pw is None:
            raise DataError("The key file is encrypted - its passphrase is needed (set the connection's "
                            "Passphrase).") from None
        key = load(data, password=None)          # not encrypted: the passphrase isn't needed
    except ValueError:
        raise DataError("The passphrase is wrong, or the file isn't a private key (.p8).") from None
    except Exception as e:  # unsupported key type etc.
        raise DataError(f"Couldn't read the key file: {e}") from None
    return key.private_bytes(encoding=serialization.Encoding.DER, format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())


def _snowflake_connect(cfg, password):
    """Snowflake: account + user, then a password, single sign-on in the browser (Okta/Azure AD -
    authenticator=externalbrowser), or a key-pair file."""
    try:
        import snowflake.connector as _sf
    except ImportError:
        raise DataError("The Snowflake driver isn't installed. In PyStudio: Project Properties > "
                        "Packages > install snowflake-connector-python") from None
    _sf.paramstyle = "qmark"            # ? placeholders, like DB2/ODBC
    args = {"account": _expand(cfg.get("account", "")).strip(), "user": _expand(cfg.get("user", "")),
            "application": "PyStudio", "login_timeout": 60}
    for key in ("warehouse", "database", "schema", "role"):
        if cfg.get(key):
            args[key] = _expand(cfg[key]).strip()
    auth = cfg.get("auth", "Password")
    if auth == "Browser SSO":
        args["authenticator"] = "externalbrowser"
        args["client_store_temporary_credential"] = True     # don't open the browser every time
    elif auth == "Key pair":
        args["private_key"] = load_private_key(cfg.get("keyFile", ""), password)   # password = passphrase
    else:
        args["password"] = password
    try:
        return _sf.connect(**args)
    except Exception as e:
        msg = str(e)
        m = _re.search(r"\d{6} \([0-9A-Z]+\): (.*)", msg)
        raise DataError(m.group(1) if m else msg) from None


def _db2_message(e):
    s = str(e)
    m = _re.search(r"SQL\d{4,5}N[^\r\n]*", s)
    return m.group(0) if m else s


def param_style(conn):
    return "named" if isinstance(conn, _sqlite3.Connection) else "qmark"


def _cell(v):
    if isinstance(v, _decimal.Decimal):
        return int(v) if v == v.to_integral_value() and abs(v) < 2 ** 53 else float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    return v


def run(conn, sql, params=None, limit=None):
    """Run one statement. SELECTs return a DataTable (at most `limit` rows when given);
    other statements return a DataTable with no columns and .rowcount set."""
    params = dict(params or {})
    t0 = _time.perf_counter()
    cur = conn.cursor()
    try:
        names = find_params(sql)
        if param_style(conn) == "named":
            missing = [n for n in names if n not in params]
            if missing:
                raise DataError("No value given for parameter(s): " + ", ".join(missing))
            cur.execute(sql, {n: params[n] for n in names})
        else:
            q, args = to_qmark(sql, params)
            cur.execute(q, args) if args else cur.execute(q)
        if cur.description:
            cols = [d[0] for d in cur.description]
            if limit:
                rows = cur.fetchmany(limit + 1)
                more = len(rows) > limit
                rows = rows[:limit]
            else:
                rows, more = cur.fetchall(), False
            rows = [tuple(_cell(v) for v in r) for r in rows]
            return DataTable(cols, rows, more, len(rows), _time.perf_counter() - t0)
        try:
            conn.commit()      # harmless when the driver is in autocommit mode
        except Exception:
            pass
        return DataTable([], [], False, cur.rowcount, _time.perf_counter() - t0)
    except DataError:
        raise
    except Exception as e:
        msg = _db2_message(e) if type(conn).__module__.startswith("ibm_db") else str(e)
        raise DataError(msg) from None
    finally:
        try:
            cur.close()
        except Exception:
            pass


# ------------------------------------------------------------------ schema
def schema_tables(conn, cfg):
    """[(schema, name, kind)] with kind "TABLE" or "VIEW"."""
    provider = cfg.get("provider", "SQLite")
    schema = (cfg.get("schema") or "").strip() or None
    out = []
    try:
        if provider == "SQLite":
            for name, kind in conn.execute("SELECT name, type FROM sqlite_master WHERE type IN "
                                           "('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                out.append(("", name, kind.upper()))
        elif provider == "DB2":
            cur = conn.cursor()
            sql = ("SELECT TABSCHEMA, TABNAME, TYPE FROM SYSCAT.TABLES WHERE TYPE IN ('T','V') "
                   + ("AND TABSCHEMA = ? " if schema else "AND TABSCHEMA NOT LIKE 'SYS%' ")
                   + "ORDER BY TABSCHEMA, TABNAME FETCH FIRST 5000 ROWS ONLY")
            cur.execute(sql, [schema.upper()] if schema else [])
            for s, n, t in cur.fetchall():
                out.append((s.strip(), n.strip(), "VIEW" if t.strip() == "V" else "TABLE"))
            cur.close()
        elif provider == "Snowflake":
            cur = conn.cursor()
            sql = ("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
                   "WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA' " + ("AND TABLE_SCHEMA = ? " if schema else "")
                   + "ORDER BY 1, 2 LIMIT 5000")
            cur.execute(sql, [schema.upper()] if schema else [])
            for s, n, t in cur.fetchall():
                out.append((s, n, "VIEW" if "VIEW" in (t or "").upper() else "TABLE"))
            cur.close()
        else:
            cur = conn.cursor()
            for r in cur.tables(schema=schema):
                if r.table_type in ("TABLE", "VIEW"):
                    out.append((r.table_schem or "", r.table_name, r.table_type))
            cur.close()
    except Exception as e:
        raise DataError(str(e)) from None
    return out


def schema_columns(conn, cfg, schema, table):
    """[(name, type, is_key)]"""
    provider = cfg.get("provider", "SQLite")
    out = []
    try:
        if provider == "SQLite":
            for r in conn.execute(f'PRAGMA table_info("{table}")'):
                out.append((r[1], r[2] or "", bool(r[5])))
        elif provider == "DB2":
            cur = conn.cursor()
            cur.execute("SELECT C.COLNAME, C.TYPENAME, C.LENGTH, C.SCALE, C.KEYSEQ FROM SYSCAT.COLUMNS C "
                        "WHERE C.TABSCHEMA = ? AND C.TABNAME = ? ORDER BY C.COLNO", [schema, table])
            for name, typ, length, scale, keyseq in cur.fetchall():
                typ = typ.strip()
                if typ in ("VARCHAR", "CHARACTER", "CHAR"):
                    typ += f"({length})"
                elif typ == "DECIMAL":
                    typ += f"({length},{scale})"
                out.append((name.strip(), typ, bool(keyseq)))
            cur.close()
        elif provider == "Snowflake":
            cur = conn.cursor()
            cur.execute("SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE "
                        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? "
                        "ORDER BY ORDINAL_POSITION", [schema, table])
            rows = cur.fetchall()
            keys = set()
            try:
                cur.execute(f"SHOW PRIMARY KEYS IN TABLE {qualified(schema, table, provider)}")
                names = [d[0].lower() for d in cur.description]
                ci = names.index("column_name")
                keys = {r[ci] for r in cur.fetchall()}
            except Exception:
                pass
            for name, typ, length, prec, scale in rows:
                if typ == "TEXT" and length:
                    typ = f"VARCHAR({length})"
                elif typ == "NUMBER" and prec:
                    typ = f"NUMBER({prec},{scale or 0})"
                out.append((name, typ, name in keys))
            cur.close()
        else:
            cur = conn.cursor()
            cols = list(cur.columns(table=table, schema=schema or None))
            keys = set()
            try:
                keys = {k.column_name for k in cur.primaryKeys(table=table, schema=schema or None)}
            except Exception:
                pass
            for c in cols:
                out.append((c.column_name, c.type_name + (f"({c.column_size})" if "CHAR" in c.type_name else ""),
                            c.column_name in keys))
            cur.close()
    except Exception as e:
        raise DataError(str(e)) from None
    return out


def schema_foreign_keys(conn, cfg, schema, table):
    """[(column, ref_schema, ref_table, ref_column)] for joins in the Query Builder."""
    provider = cfg.get("provider", "SQLite")
    out = []
    try:
        if provider == "SQLite":
            for r in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
                out.append((r[3], "", r[2], r[4]))
        elif provider == "DB2":
            cur = conn.cursor()
            cur.execute("SELECT FK_COLNAMES, REFTABSCHEMA, REFTABNAME, PK_COLNAMES FROM SYSCAT.REFERENCES "
                        "WHERE TABSCHEMA = ? AND TABNAME = ?", [schema, table])
            for fk, rs, rt, pk in cur.fetchall():
                fks, pks = fk.split(), pk.split()
                for a, b in zip(fks, pks):
                    out.append((a, rs.strip(), rt.strip(), b))
            cur.close()
        else:
            cur = conn.cursor()
            for r in cur.foreignKeys(foreignTable=table, foreignSchema=schema or None):
                out.append((r.fkcolumn_name, r.pktable_schem or "", r.pktable_name, r.pkcolumn_name))
            cur.close()
    except Exception:
        return []
    return out


def quote_ident(name, provider):
    if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        return name
    return '"' + name.replace('"', '""') + '"'


def qualified(schema, table, provider):
    t = quote_ident(table, provider)
    return f"{quote_ident(schema, provider)}.{t}" if schema else t


def to_text(v):
    """How a value shows in a grid cell."""
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v) if abs(v) >= 1e15 or (v and abs(v) < 1e-4) else f"{v:.10g}"
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, _dt.datetime) else v.isoformat()
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    return str(v)


def is_number(v):
    return isinstance(v, (int, float, _decimal.Decimal)) and not isinstance(v, bool)


def sample_database(path):
    """Create a small sample database (Customers, Products, Orders) for learning."""
    conn = _sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE Customers (CustomerID INTEGER PRIMARY KEY, Name TEXT NOT NULL,
                                City TEXT, State TEXT, Phone TEXT);
        CREATE TABLE Products (ProductID INTEGER PRIMARY KEY, Name TEXT NOT NULL,
                               Price REAL NOT NULL, InStock INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE Orders (OrderID INTEGER PRIMARY KEY,
                             CustomerID INTEGER NOT NULL REFERENCES Customers(CustomerID),
                             ProductID INTEGER NOT NULL REFERENCES Products(ProductID),
                             Quantity INTEGER NOT NULL, OrderDate TEXT NOT NULL);
    """)
    conn.executemany("INSERT INTO Customers VALUES (?,?,?,?,?)", [
        (1, "Acme Freight", "Omaha", "NE", "(402) 555-0101"),
        (2, "Blue Line Logistics", "Lincoln", "NE", "(402) 555-0144"),
        (3, "Cornhusker Supply", "Kearney", "NE", "(308) 555-0170"),
        (4, "Prairie Parts", "Des Moines", "IA", "(515) 555-0123"),
        (5, "Rocky Ridge Hauling", "Denver", "CO", "(303) 555-0188"),
        (6, "Lakeside Grocers", "Chicago", "IL", "(312) 555-0199"),
    ])
    conn.executemany("INSERT INTO Products VALUES (?,?,?,?)", [
        (1, "Pallet Jack", 389.99, 12), (2, "Ratchet Strap 4-Pack", 39.95, 140),
        (3, "Load Bar", 64.50, 55), (4, "Reflective Triangle Kit", 24.99, 300),
        (5, "Tire Gauge", 12.49, 80),
    ])
    conn.executemany("INSERT INTO Orders VALUES (?,?,?,?,?)", [
        (1, 1, 2, 10, "2026-10-02"), (2, 1, 4, 25, "2026-10-05"), (3, 2, 1, 2, "2026-10-09"),
        (4, 3, 3, 8, "2026-10-14"), (5, 4, 2, 6, "2026-10-21"), (6, 5, 5, 30, "2026-11-01"),
        (7, 2, 4, 12, "2026-11-03"), (8, 6, 3, 4, "2026-11-10"), (9, 1, 1, 1, "2026-11-15"),
    ])
    conn.commit()
    conn.close()
