"""Python Interactive back end (run by PyStudio in the project's Python, in the project folder).
Reads {"code": ...} JSON lines on stdin, answers {"output", "result", "error"} on stdout."""
import io
import json
import os
import sys
import traceback


def main():
    sys.path.insert(0, os.getcwd())          # import the project's modules
    ns = {"__name__": "__main__", "__builtins__": __builtins__}
    out = sys.stdout
    for line in sys.stdin:
        try:
            src = json.loads(line).get("code", "")
        except ValueError:
            continue
        buf = io.StringIO()
        result, error = "", False
        sys.stdout = sys.stderr = buf
        try:
            try:
                code, is_expr = compile(src, "<interactive>", "eval"), True
            except SyntaxError:
                code, is_expr = compile(src, "<interactive>", "exec"), False
            if is_expr:
                value = eval(code, ns)
                if value is not None:
                    ns["_"] = value
                    r = repr(value)
                    result = r if len(r) <= 4000 else r[:4000] + " ..."
            else:
                exec(code, ns)
        except SystemExit:
            result = "(exit() is ignored here - use Reset to start over)"
        except BaseException as e:  # noqa: BLE001 - shown like the Python REPL
            tb = traceback.format_exception(type(e), e, e.__traceback__)
            keep = [t for t in tb if "repl_server.py" not in t]
            result, error = "".join(keep).strip(), True
        finally:
            sys.stdout, sys.stderr = out, sys.__stderr__
        out.write(json.dumps({"output": buf.getvalue(), "result": result, "error": error}) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
