"""C# WinForms projects (.csproj) for the VB.NET importer.

Form1.Designer.cs is the same InitializeComponent as Form1.Designer.vb in C# syntax, so it is translated
statement by statement into the VB form the importer already reads (this. -> Me., new -> New, | -> Or,
"..." -> VB strings, event += -> Handles). Form1.cs methods become the same (kind, name, params,
handles, body) tuples the VB parser produces, so wiring and the commented code work the same way."""
import os
import re

CS_KEYWORDS = {"if", "while", "for", "foreach", "switch", "catch", "using", "return", "new", "else", "lock",
               "fixed", "do", "try", "throw", "get", "set", "nameof", "typeof", "sizeof", "default"}


# ============================================================ lexing helpers
def _strip_comments(code):
    """Remove // and /* */ comments, keeping strings intact."""
    out, i, n = [], 0, len(code)
    while i < n:
        c = code[i]
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            j = code.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '"' or (c == "@" and i + 1 < n and code[i + 1] == '"') or c == "'":
            j = _string_end(code, i)
            out.append(code[i:j])
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _string_end(code, i):
    """Index just past the string/char literal starting at i."""
    n = len(code)
    if code[i] == "@":
        j = i + 2
        while j < n:
            if code[j] == '"':
                if j + 1 < n and code[j + 1] == '"':
                    j += 2
                    continue
                return j + 1
            j += 1
        return n
    q = code[i]
    j = i + 1
    while j < n:
        if code[j] == "\\":
            j += 2
            continue
        if code[j] == q:
            return j + 1
        j += 1
    return n


def _block(code, open_index):
    """(body, end) for the {...} starting at open_index (string-aware)."""
    depth, i, n = 0, open_index, len(code)
    while i < n:
        c = code[i]
        if c == '"' or c == "'" or (c == "@" and i + 1 < n and code[i + 1] == '"'):
            i = _string_end(code, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return code[open_index + 1:i], i + 1
        i += 1
    return code[open_index + 1:], n


def _cs_string_value(lit):
    if lit.startswith("@"):
        return lit[2:-1].replace('""', '"')
    body = lit[1:-1]
    esc = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'", "0": "\0"}
    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1).startswith("u")
                  and len(m.group(1)) == 5 else esc.get(m.group(1), m.group(1)), body)


def _vb_string(value):
    value = value.replace("\r\n", "\n")
    parts = value.split("\n")
    return " & vbCrLf & ".join('"' + p.replace('"', '""') + '"' for p in parts)


# ============================================================ designer
def _statements(body):
    out, cur, i, n = [], [], 0, len(body)
    while i < n:
        c = body[i]
        if c == '"' or c == "'" or (c == "@" and i + 1 < n and body[i + 1] == '"'):
            j = _string_end(body, i)
            cur.append(body[i:j])
            i = j
            continue
        if c == ";":
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return [s for s in out if s]


def _to_vb(stmt):
    """One C# designer statement -> VB (strings become VB literals)."""
    lits = []

    def keep(m):
        lit = m.group(0)
        if lit.startswith("'"):
            v = _cs_string_value('"' + lit[1:-1] + '"')
            lits.append(f"Global.Microsoft.VisualBasic.ChrW({ord(v[0]) if v else 0})")
        else:
            lits.append(_vb_string(_cs_string_value(lit)))
        return f"\x00{len(lits) - 1}\x00"
    s = re.sub(r'@"(?:[^"]|"")*"|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\'', keep, stmt)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\bthis\.", "Me.", s)
    s = re.sub(r"\bnew ([\w.]+)\s*\[\s*\]\s*\{", r"New \1() {", s)
    s = re.sub(r"\bnew\b", "New", s)
    s = re.sub(r"\btrue\b", "True", s)
    s = re.sub(r"\bfalse\b", "False", s)
    s = re.sub(r"\bnull\b", "Nothing", s)
    s = re.sub(r"\bobject\b", "Object", s)
    s = re.sub(r"\bint\b", "Integer", s)
    s = re.sub(r"\bdecimal\b", "Decimal", s)
    s = re.sub(r"(\d)[fFmMdDlL]\b", r"\1", s)
    s = s.replace("||", " OrElse ").replace("|", " Or ")
    s = re.sub(r"\{\s*", "{", s)
    s = re.sub(r"\s*\}", "}", s)
    m = re.match(r"^([\w.]+)\s+(\w+)\s*=\s*(New .*)$", s)          # Type name = new Type(...)
    if m and not m.group(1).startswith("Me."):
        s = f"Dim {m.group(2)} As {m.group(1)} = {m.group(3)}"
    return re.sub(r"\x00(\d+)\x00", lambda m: lits[int(m.group(1))], s)


EVENT_RE = re.compile(r"^Me\.(?:(\w+)\.)?(\w+)\s*\+=\s*(?:New [\w.]+\()?(?:Me\.)?(\w+)\)?$")


def designer_to_vb(text):
    """(vb_designer_text, events, class_name) for a Form1.Designer.cs. events = [(control|'Me', event, handler)]."""
    code = _strip_comments(text)
    cm = re.search(r"\bpartial\s+class\s+(\w+)", code) or re.search(r"\bclass\s+(\w+)", code)
    cls = cm.group(1) if cm else "Form1"
    m = re.search(r"void\s+InitializeComponent\s*\(\s*\)\s*\{", code)
    if not m:
        return None, [], cls
    body, _end = _block(code, m.end() - 1)
    lines, events = [], []
    for st in _statements(body):
        vb = _to_vb(st)
        ev = EVENT_RE.match(vb)
        if ev:
            events.append((ev.group(1) or "Me", ev.group(2), ev.group(3)))
            continue
        lines.append("        " + vb)
    return lines, events, cls


def build_vb_designer(lines, cls, user_control=False):
    base = "System.Windows.Forms.UserControl" if user_control else "System.Windows.Forms.Form"
    return (f"Partial Class {cls}\n    Inherits {base}\n    Private Sub InitializeComponent()\n"
            + "\n".join(lines) + "\n    End Sub\nEnd Class\n")


# ============================================================ code-behind
METHOD_RE = re.compile(r"(?:\b(?:public|private|protected|internal|static|async|override|virtual|sealed|new|"
                       r"extern|unsafe)\s+)*([\w<>\[\],.?]+)\s+(\w+)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*"
                       r"(?:where[^{]*)?\{")


ARROW_RE = re.compile(r"(?:\b(?:public|private|protected|internal|static|async|override|virtual|sealed)\s+)*"
                      r"([\w<>\[\],.?]+)\s+(\w+)\s*\(([^()]*)\)\s*=>\s*([^;]+);")


def _vb_params(params):
    """C# 'object sender, EventArgs e' -> VB-style 'sender As object, e As EventArgs' (the importer's form)."""
    out = []
    for p in [x.strip() for x in params.split(",") if x.strip()]:
        p = re.sub(r"=.*$", "", p).strip()
        p = re.sub(r"^(ref|out|in|params|this)\s+", "", p)
        bits = p.split()
        if len(bits) >= 2:
            out.append(f"{bits[-1]} As {' '.join(bits[:-1])}")
        elif bits:
            out.append(bits[0])
    return ", ".join(out)


def _body_lines(body):
    lines = body.strip("\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def parse_methods(code, class_name=None, events=()):
    """[(kind, name, params, handles, body_lines)] like vbimport.parse_subs; handles come from the
    designer's event += lines."""
    handles = {}
    for ctrl, event, handler in events:
        handles.setdefault(handler, []).append(f"{'Me' if ctrl == 'Me' else ctrl}.{event}")
    out = []
    pos = 0
    text = _strip_comments(code) if False else code
    while True:
        m = METHOD_RE.search(text, pos)
        a = ARROW_RE.search(text, pos)
        if a and (not m or a.start() < m.start()):        # int Total() => a + b;
            rtype, name, params = a.group(1), a.group(2), a.group(3)
            pos = a.end()
            if rtype in CS_KEYWORDS or name in CS_KEYWORDS or name == class_name:
                continue
            out.append(("Sub" if rtype == "void" else "Function", name, _vb_params(params),
                        handles.get(name, []), [a.group(4).strip() + ";"]))
            continue
        if not m:
            break
        rtype, name, params = m.group(1), m.group(2), m.group(3)
        body, end = _block(text, m.end() - 1)
        pos = end
        if rtype in CS_KEYWORDS or name in CS_KEYWORDS or name in ("InitializeComponent", "Dispose") \
                or name == class_name or rtype in ("class", "struct", "interface", "enum", "namespace"):
            continue
        kind = "Sub" if rtype == "void" else "Function"
        out.append((kind, name, _vb_params(params), handles.get(name, []), _body_lines(body)))
    return out


def convert_module(code, rel):
    """A C# class file (not a form) -> Python with the C# code as comments."""
    clean = _strip_comments(code)
    cm = re.search(r"\b(?:static\s+)?class\s+(\w+)", clean)
    name = cm.group(1) if cm else "Module1"
    static = bool(re.search(r"\bstatic\s+class\b", clean))
    out = [f'"""{name} - imported from {rel} (C# {"static class" if static else "class"}). '
           'Translate the C# code in the comments."""', ""]
    methods = parse_methods(code, name)
    ind = "" if static else "    "
    if not static:
        out += ["", f"class {name}:"]
        if not methods:
            out.append("    pass")
    for kind, n, params, _h, body in methods:
        from .vbimport import _py_params, _dedent
        args = _py_params(params)
        if not static:
            args = ["self"] + args
        out += ["", f"{ind}def {n}({', '.join(args)}):"]
        out += [f"{ind}    # {ln}" if ln else f"{ind}    #" for ln in [f"C# {n}({params}):"] + _dedent(body)]
        out.append(f"{ind}    pass")
    return "\n".join(out) + "\n"


# ============================================================ project
def parse_csproj(path):
    folder = os.path.dirname(os.path.abspath(path))
    text = open(path, encoding="utf-8-sig", errors="replace").read()
    name = (re.search(r"<AssemblyName>([^<]+)</AssemblyName>", text) or
            re.search(r"<RootNamespace>([^<]+)</RootNamespace>", text))
    name = name.group(1) if name else os.path.splitext(os.path.basename(path))[0]
    files = [m.replace("\\", os.sep) for m in re.findall(r'<Compile Include="([^"]+\.cs)"', text)]
    if not files or "Microsoft.NET.Sdk" in text:
        files = []
        for dirpath, dirs, fs in os.walk(folder):
            dirs[:] = [d for d in dirs if d.lower() not in ("bin", "obj", ".vs")]
            files += [os.path.relpath(os.path.join(dirpath, f), folder) for f in fs if f.endswith(".cs")]
    forms = []
    for rel in files:
        if not rel.endswith(".Designer.cs") or os.path.basename(rel).startswith(("Resources.", "Settings.")):
            continue
        code = rel[:-len(".Designer.cs")] + ".cs"
        try:
            t = open(os.path.join(folder, rel), encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        if "InitializeComponent" not in t:
            continue
        forms.append((rel, code if os.path.exists(os.path.join(folder, code)) else None))
    startup = None
    for rel in files:
        if os.path.basename(rel).lower() == "program.cs":
            m = re.search(r"Application\.Run\s*\(\s*new\s+(\w+)\s*\(", open(os.path.join(folder, rel),
                                                                          encoding="utf-8-sig", errors="replace").read())
            startup = m.group(1) if m else None
    settings = os.path.join(folder, "Properties", "Settings.settings")
    return {"name": name, "folder": folder, "vb_files": files, "forms": forms, "startup": startup,
            "settings": settings if os.path.exists(settings) else None, "lang": "C#"}


def is_user_control(code_text):
    return bool(re.search(r":\s*(?:System\.Windows\.Forms\.)?UserControl\b", code_text or ""))
