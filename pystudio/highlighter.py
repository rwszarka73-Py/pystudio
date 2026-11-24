"""Python syntax highlighter for the code editor."""
import builtins
import keyword
import re

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

from .theme import SYNTAX


def _fmt(color, bold=False, italic=False):
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    if italic:
        f.setFontItalic(True)
    return f


CONTROL_WORDS = {
    "if", "elif", "else", "for", "while", "break", "continue", "return",
    "try", "except", "finally", "raise", "with", "yield", "import", "from",
    "as", "pass", "match", "case", "await", "async",
}

# Block states for multi-line triple-quoted strings
STATE_NONE = 0
STATE_TRIPLE_SINGLE = 1
STATE_TRIPLE_DOUBLE = 2


class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.f_keyword = _fmt(SYNTAX["keyword"])
        self.f_control = _fmt(SYNTAX["control"])
        self.f_builtin = _fmt(SYNTAX["builtin"])
        self.f_self = _fmt(SYNTAX["self"], italic=True)
        self.f_string = _fmt(SYNTAX["string"])
        self.f_comment = _fmt(SYNTAX["comment"], italic=True)
        self.f_number = _fmt(SYNTAX["number"])
        self.f_decorator = _fmt(SYNTAX["decorator"])
        self.f_defname = _fmt(SYNTAX["defname"])
        self.f_classname = _fmt(SYNTAX["classname"])

        kw = [k for k in keyword.kwlist + list(keyword.softkwlist) if k not in CONTROL_WORDS]
        bi = [b for b in dir(builtins) if not b.startswith("_")]

        self.rules = [
            (re.compile(r"\b(" + "|".join(kw) + r")\b"), self.f_keyword),
            (re.compile(r"\b(" + "|".join(sorted(CONTROL_WORDS)) + r")\b"), self.f_control),
            (re.compile(r"\b(" + "|".join(bi) + r")\b"), self.f_builtin),
            (re.compile(r"\b(self|cls)\b"), self.f_self),
            (re.compile(r"\b(0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|\d[\d_]*\.?\d*([eE][+-]?\d+)?j?)\b"),
             self.f_number),
            (re.compile(r"^\s*@[\w.]+"), self.f_decorator),
        ]
        self.re_def = re.compile(r"\bdef\s+(\w+)")
        self.re_class = re.compile(r"\bclass\s+(\w+)")
        # Single-line strings with optional prefix (r, b, f, rb, etc.)
        self.re_string = re.compile(
            r"""(?i)\b[rbuf]{0,2}("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')"""
        )

    def highlightBlock(self, text):
        # 1) Regular token rules
        for regex, fmt in self.rules:
            for m in regex.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)
        for m in self.re_def.finditer(text):
            self.setFormat(m.start(1), m.end(1) - m.start(1), self.f_defname)
        for m in self.re_class.finditer(text):
            self.setFormat(m.start(1), m.end(1) - m.start(1), self.f_classname)

        # 2) Strings, comments and triple-quoted strings - scanned left to right
        #    so a '#' inside a string is not treated as a comment.
        self.setCurrentBlockState(STATE_NONE)
        i = 0
        n = len(text)
        prev = self.previousBlockState()

        if prev in (STATE_TRIPLE_SINGLE, STATE_TRIPLE_DOUBLE):
            delim = "'''" if prev == STATE_TRIPLE_SINGLE else '"""'
            end = text.find(delim)
            if end == -1:
                self.setFormat(0, n, self.f_string)
                self.setCurrentBlockState(prev)
                return
            self.setFormat(0, end + 3, self.f_string)
            i = end + 3

        while i < n:
            ch = text[i]
            if ch in "\"'":
                # Include a string prefix such as f, r, b, rb in the string color
                pre = i
                while pre > 0 and i - pre < 2 and text[pre - 1] in "rRbBfFuU":
                    pre -= 1
                if pre > 0 and (text[pre - 1].isalnum() or text[pre - 1] == "_"):
                    pre = i
                if pre < i:
                    self.setFormat(pre, i - pre, self.f_string)
            if ch == "#":
                self.setFormat(i, n - i, self.f_comment)
                return
            if text.startswith('"""', i) or text.startswith("'''", i):
                delim = text[i:i + 3]
                end = text.find(delim, i + 3)
                if end == -1:
                    self.setFormat(i, n - i, self.f_string)
                    self.setCurrentBlockState(
                        STATE_TRIPLE_DOUBLE if delim == '"""' else STATE_TRIPLE_SINGLE)
                    return
                self.setFormat(i, end + 3 - i, self.f_string)
                i = end + 3
                continue
            if ch in "\"'":
                m = self.re_string.match(text, i)
                if m:
                    self.setFormat(i, m.end() - i, self.f_string)
                    i = m.end()
                    continue
                # Unterminated string: color to end of line
                self.setFormat(i, n - i, self.f_string)
                return
            i += 1
