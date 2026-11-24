"""Notebook kernel - runs in the project's Python, in the notebook's folder. PyStudio sends one JSON line per
request on stdin ({"id", "code"} to run a cell, {"input": text} to answer input()); the kernel answers with JSON
lines on its own channel (a copy of the original stdout):

    {"id", "type": "stream", "name": "stdout"|"stderr", "text"}     printed output, sent as it happens
    {"id", "type": "display", "data": {mime: value}}                 display(obj), matplotlib figures
    {"id", "type": "result", "data": {mime: value}, "count": n}      the value of the cell's last expression
    {"id", "type": "error", "ename", "evalue", "traceback": [...]}
    {"id", "type": "input", "prompt"}                                 input() is waiting for an answer
    {"id", "type": "done", "count": n}

Output from C code or other programs (fd 1 and 2) goes to stderr, which PyStudio shows in the cell too.
Like Jupyter: !command runs a shell command, %pip install x installs into this Python, %cd changes folder,
%time runs a statement and times it, %matplotlib is accepted (charts show under the cell)."""
import ast
import base64
import builtins
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

_PROTO = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)                                  # C-level / child-process output -> stderr (shown in the cell)
_LOCK = threading.Lock()
_CURRENT = {"id": None}
_STDIN = sys.stdin
os.environ["MPLBACKEND"] = "module://pystudio_nb_backend"     # plt.show() -> image under the cell


def send(obj):
    obj.setdefault("id", _CURRENT["id"])
    with _LOCK:
        _PROTO.write(json.dumps(obj, default=str) + "\n")
        _PROTO.flush()


class _Stream(io.TextIOBase):
    """print() -> stream messages (buffered a little so a loop of prints isn't thousands of messages)."""

    def __init__(self, name):
        self.name = name
        self.buf = []
        self.last = time.monotonic()

    def writable(self):
        return True

    def write(self, s):
        if not s:
            return 0
        self.buf.append(s)
        if "\n" in s and (time.monotonic() - self.last > 0.05 or sum(len(x) for x in self.buf) > 4000):
            self.flush()
        return len(s)

    def flush(self):
        if self.buf:
            text = "".join(self.buf)
            self.buf = []
            self.last = time.monotonic()
            send({"type": "stream", "name": self.name, "text": text})

    def isatty(self):
        return False


def mimebundle(obj):
    """What a value looks like: HTML (pandas tables), images, Markdown, and always plain text."""
    data = {}
    for meth, mime in (("_repr_html_", "text/html"), ("_repr_markdown_", "text/markdown"),
                       ("_repr_svg_", "image/svg+xml"), ("_repr_png_", "image/png"), ("_repr_jpeg_", "image/jpeg"),
                       ("_repr_latex_", "text/latex")):
        f = getattr(obj, meth, None)
        if callable(f) and not isinstance(obj, type):
            try:
                v = f()
            except Exception:  # noqa: BLE001
                v = None
            if isinstance(v, tuple):
                v = v[0]
            if v is None:
                continue
            if isinstance(v, bytes):
                v = base64.b64encode(v).decode("ascii")
            data[mime] = v
    try:
        text = repr(obj)
    except Exception as e:  # noqa: BLE001
        text = f"<repr failed: {e}>"
    data["text/plain"] = text if len(text) <= 20000 else text[:20000] + " ..."
    return data


class _Display:
    """display(obj) like IPython; HTML(...), Markdown(...), Image(...) helpers."""

    def __call__(self, *objs):
        for o in objs:
            send({"type": "display", "data": mimebundle(o)})


class HTML:
    def __init__(self, html):
        self.html = html

    def _repr_html_(self):
        return self.html


class Markdown:
    def __init__(self, text):
        self.text = text

    def _repr_markdown_(self):
        return self.text


class Image:
    def __init__(self, filename=None, data=None):
        if filename:
            with open(filename, "rb") as f:
                data = f.read()
        self.data = data or b""

    def _repr_png_(self):
        return self.data


def _flush_figures():
    """matplotlib figures made by the cell -> images under it."""
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None:
        return False
    sys.stdout.flush()                   # what was printed before the chart comes first
    shown = False
    for num in plt.get_fignums():
        fig = plt.figure(num)
        buf = io.BytesIO()
        try:
            fig.savefig(buf, format="png", dpi=96, bbox_inches="tight")
        except Exception as e:  # noqa: BLE001
            send({"type": "stream", "name": "stderr", "text": f"(couldn't draw the figure: {e})\n"})
            continue
        send({"type": "display", "data": {"image/png": base64.b64encode(buf.getvalue()).decode("ascii"),
                                          "text/plain": f"<Figure {num}>"}})
        shown = True
    plt.close("all")
    return shown


def _input(prompt=""):
    sys.stdout.flush()
    send({"type": "input", "prompt": str(prompt)})
    line = _STDIN.readline()
    try:
        return json.loads(line).get("input", "")
    except ValueError:
        return ""


def _shell(cmd):
    sys.stdout.flush()
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    return None


def _pip(args):
    _shell(f'"{sys.executable}" -m pip {args}')
    print("(Restart the kernel if a package you already imported was upgraded.)")


def _time(stmt, ns):
    t = time.perf_counter()
    exec(compile(stmt, "<cell>", "exec"), ns)
    print(f"Wall time: {(time.perf_counter() - t) * 1000:.2f} ms")


_MAGIC = re.compile(r"^(\s*)(!|%pip\b|%cd\b|%time\b|%matplotlib\b|%env\b|%who\b|%pwd\b)(.*)$")


def transform(src):
    """!cmd and line magics -> Python calls (only at the start of a statement line)."""
    if not re.search(r"(?m)^\s*(!|%)", src):
        return src
    out = []
    for line in src.split("\n"):
        m = _MAGIC.match(line)
        if not m:
            if re.match(r"^\s*%%", line):           # cell magics: %%time - run the rest, timed
                out.append(line.replace("%%time", "pass  # %%time", 1) if "%%time" in line else "")
                continue
            out.append(line)
            continue
        ind, kind, rest = m.groups()
        rest_q = repr(rest.strip())
        if kind == "!":
            out.append(f"{ind}_nb_shell({rest_q})")
        elif kind == "%pip":
            out.append(f"{ind}_nb_pip({rest_q})")
        elif kind == "%cd":
            out.append(f"{ind}__import__('os').chdir(__import__('os').path.expanduser({rest_q})); "
                       f"print(__import__('os').getcwd())")
        elif kind == "%pwd":
            out.append(f"{ind}print(__import__('os').getcwd())")
        elif kind == "%time":
            out.append(f"{ind}_nb_time({rest_q}, globals())")
        elif kind == "%env":
            k, _, v = rest.strip().partition("=")
            out.append(f"{ind}__import__('os').environ[{k.strip()!r}] = {v.strip()!r}" if _ else
                       f"{ind}print(__import__('os').environ.get({k.strip()!r}))")
        elif kind == "%who":
            out.append(f"{ind}print(*sorted(k for k in globals() if not k.startswith('_') and k not in "
                       f"('In', 'Out', 'display', 'HTML', 'Markdown', 'Image', 'exit', 'quit', 'get_ipython')))")
        else:
            out.append(f"{ind}pass")
    return "\n".join(out)


def run_cell(src, ns, count):
    timed = "%%time" in src.split("\n", 1)[0]
    t0 = time.perf_counter()
    src = transform(src)
    tree = ast.parse(src, "<cell>", "exec")
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr) and not src.rstrip().endswith(";"):
        last = ast.Expression(tree.body.pop().value)
    exec(compile(tree, "<cell>", "exec"), ns)
    value = eval(compile(last, "<cell>", "eval"), ns) if last is not None else None
    sys.stdout.flush()
    sys.stderr.flush()
    if value is not None:
        ns["_"] = value
        ns.setdefault("Out", {})[count] = value
        send({"type": "result", "data": mimebundle(value), "count": count})
    _flush_figures()
    if timed:
        print(f"Wall time: {(time.perf_counter() - t0) * 1000:.2f} ms")


def main():
    sys.path.insert(0, os.getcwd())
    out, err = _Stream("stdout"), _Stream("stderr")
    sys.stdout, sys.stderr = out, err
    builtins.input = _input
    display = _Display()
    ns = {"__name__": "__main__", "__builtins__": builtins, "display": display, "HTML": HTML,
          "Markdown": Markdown, "Image": Image, "_nb_shell": _shell, "_nb_pip": _pip, "_nb_time": _time,
          "In": [], "Out": {}}
    count = 0
    send({"id": None, "type": "ready", "python": sys.version.split()[0], "executable": sys.executable})
    while True:
        try:
            line = _STDIN.readline()
        except KeyboardInterrupt:             # Interrupt while idle
            continue
        if not line:
            break
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if "code" not in req:
            continue
        _CURRENT["id"] = req.get("id")
        count += 1
        ns["In"].append(req["code"])
        try:
            run_cell(req["code"], ns, count)
        except SystemExit:
            out.flush()
            print("(exit() is ignored in a notebook - use Restart to start over)")
        except BaseException as e:  # noqa: BLE001 - KeyboardInterrupt included: shown like Jupyter
            out.flush()
            err.flush()
            tb = traceback.extract_tb(e.__traceback__)
            keep = [f for f in tb if f.filename not in (__file__,) and not f.filename.endswith("nbkernel.py")]
            lines = ["Traceback (most recent call last):\n"] + traceback.format_list(keep) if keep else []
            lines += traceback.format_exception_only(type(e), e)
            send({"type": "error", "ename": type(e).__name__, "evalue": str(e), "traceback": lines})
        out.flush()
        err.flush()
        send({"type": "done", "count": count})


if __name__ == "__main__":
    main()
