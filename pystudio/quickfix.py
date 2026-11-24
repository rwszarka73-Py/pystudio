"""Quick Fix (Ctrl+.): 'Add import', 'Did you mean...', 'Remove unused import'."""
import builtins
import difflib
import keyword
import re

_INDEX = None

# Common standard-library names -> how to import them
STDLIB = {
    "os": "import os", "sys": "import sys", "re": "import re", "json": "import json",
    "math": "import math", "time": "import time", "random": "import random", "csv": "import csv",
    "shutil": "import shutil", "glob": "import glob", "subprocess": "import subprocess",
    "sqlite3": "import sqlite3", "datetime": "import datetime", "threading": "import threading",
    "pathlib": "import pathlib", "logging": "import logging", "string": "import string",
    "itertools": "import itertools", "collections": "import collections", "textwrap": "import textwrap",
    "urllib": "import urllib.request", "socket": "import socket", "platform": "import platform",
    "Path": "from pathlib import Path", "date": "from datetime import date",
    "timedelta": "from datetime import timedelta", "defaultdict": "from collections import defaultdict",
    "Counter": "from collections import Counter", "namedtuple": "from collections import namedtuple",
    "dataclass": "from dataclasses import dataclass", "partial": "from functools import partial",
    "pprint": "from pprint import pprint", "sleep": "from time import sleep",
    "msgbox": "from pystudio_runtime import msgbox",
}


def _index():
    """name -> [import statement] for PySide6 classes and the standard library."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    idx = {}
    for name, stmt in STDLIB.items():
        idx.setdefault(name, []).append(stmt)
    for mod in ("QtWidgets", "QtCore", "QtGui"):
        try:
            m = __import__(f"PySide6.{mod}", fromlist=["*"])
        except ImportError:
            continue
        for n in dir(m):
            if n.startswith("Q") or n in ("Qt", "Signal", "Slot", "Property"):
                idx.setdefault(n, []).append(f"from PySide6.{mod} import {n}")
    _INDEX = idx
    return idx


def _defined_names(source):
    names = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", source))
    return names | set(dir(builtins)) | set(keyword.kwlist)


def add_import(source, stmt):
    """Return source with the import added (merged into an existing 'from X import ...' line)."""
    lines = source.split("\n")
    m = re.match(r"from (\S+) import (\w+)$", stmt)
    if m:
        module, name = m.groups()
        for i, line in enumerate(lines):
            mm = re.match(rf"^from {re.escape(module)} import ([^()#]+?)\s*(#.*)?$", line)
            if mm:
                names = [x.strip() for x in mm.group(1).split(",") if x.strip()]
                if name in names:
                    return source
                names.append(name)
                names.sort(key=str.lower)
                lines[i] = f"from {module} import {', '.join(names)}" + (f"  {mm.group(2)}" if mm.group(2) else "")
                return "\n".join(lines)
    # new line after the last top-level import (or after the module docstring)
    last = -1
    in_doc = False
    for i, line in enumerate(lines):
        s = line.strip()
        if i == 0 and (s.startswith('"""') or s.startswith("'''")):
            in_doc = not (s.count('"""') >= 2 or s.count("'''") >= 2)
            last = i
            continue
        if in_doc:
            last = i
            if '"""' in s or "'''" in s:
                in_doc = False
            continue
        if line.startswith(("import ", "from ")):
            last = i
        elif s and not s.startswith("#") and last >= 0 and not line.startswith(("import", "from")):
            break
    lines.insert(last + 1, stmt)
    return "\n".join(lines)


def remove_import(source, line_no, name):
    lines = source.split("\n")
    i = line_no - 1
    if not (0 <= i < len(lines)):
        return source
    line = lines[i]
    short = name.split(".")[-1].split(" as ")[-1]
    m = re.match(r"^(\s*from \S+ import )(.+)$", line)
    if m:
        names = [x.strip() for x in m.group(2).split(",")]
        keep = [x for x in names if x.split(" as ")[-1] != short]
        if keep and len(keep) < len(names):
            lines[i] = m.group(1) + ", ".join(keep)
            return "\n".join(lines)
    del lines[i]
    return "\n".join(lines)


def fixes_for(diag, source):
    """[(title, new_source)] for one diagnostic."""
    out = []
    msg = diag.message
    m = re.search(r"undefined name '(\w+)'", msg)
    if m:
        name = m.group(1)
        for stmt in _index().get(name, [])[:3]:
            out.append((f"Add  {stmt}", add_import(source, stmt)))
        known = list(_defined_names(source)) + list(_index().keys())
        for guess in difflib.get_close_matches(name, known, n=3, cutoff=0.75):
            if guess == name:
                continue
            new = re.sub(rf"\b{re.escape(name)}\b", guess, source.split("\n")[diag.line - 1], count=1)
            lines = source.split("\n")
            lines[diag.line - 1] = new
            title = f"Change '{name}' to '{guess}'"
            out.append((title, "\n".join(lines)))
            if guess in _index() and guess not in _defined_names(source.replace(name, "")):
                stmt = _index()[guess][0]
                out.append((f"{title} and add  {stmt}", add_import("\n".join(lines), stmt)))
    m = re.search(r"'([\w.]+(?: as \w+)?)' imported but unused", msg)
    if m:
        out.append((f"Remove unused import '{m.group(1)}'", remove_import(source, diag.line, m.group(1))))
    return out
