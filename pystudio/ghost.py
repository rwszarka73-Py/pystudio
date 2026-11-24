"""Inline AI suggestions (Copilot-style "ghost text") from Claude.

When you pause typing at the end of a line, Claude suggests what comes next; it shows in gray after the
cursor. Tab accepts it, Ctrl+Right accepts one word, Esc (or typing something else) dismisses it.
Alt+\\ asks for a suggestion right now. Uses the Claude Assistant's API key; turn the automatic
suggestions on or off in Tools > Options (they use API credits)."""
import json
import re

from PySide6.QtCore import QByteArray, QObject, QSettings, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

DELAY_MS = 750
PREFIX_CHARS = 4000
SUFFIX_CHARS = 1500
MAX_LINES = 12
DEFAULT_MODEL = "claude-haiku-5-5"

SYSTEM = ("You are the inline code completion engine of PyStudio, a Python IDE (PySide6 desktop apps; controls "
          "are self.ui.<name>; pystudio_runtime has msgbox, my.query, fill). You get the code before and after "
          "the cursor. Reply with ONLY the text to insert at the cursor - no explanation, no backticks, no "
          "repeating the code before the cursor. Finish the current statement or write the next few lines "
          "(at most 10), matching the file's indentation and style. If nothing useful fits, reply with nothing.")


def enabled():
    return QSettings().value("inlineSuggestions", False, type=bool)


def model():
    return QSettings().value("inlineModel", DEFAULT_MODEL) or DEFAULT_MODEL


def clean(text, line_before):
    """The model's reply -> text to insert."""
    if not text:
        return ""
    t = text.replace("\r\n", "\n")
    m = re.match(r"^\s*```[\w+-]*\n(.*?)\n?```\s*$", t, re.S)
    if m:
        t = m.group(1)
    t = t.rstrip()
    stripped = line_before.strip()
    if stripped and t.lstrip().startswith(stripped):      # it repeated the current line
        t = t.lstrip()[len(stripped):]
    lines = t.split("\n")[:MAX_LINES]
    return "\n".join(lines) if any(ln.strip() for ln in lines) else ""


class GhostCompleter(QObject):
    """One per code editor."""

    def __init__(self, editor, api_url, api_key_fn, versions):
        super().__init__(editor)
        self.ed = editor
        self.api_url, self.api_key_fn, self.versions = api_url, api_key_fn, versions
        self.net = QNetworkAccessManager(self)
        self.reply = None
        self._req_state = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(DELAY_MS)
        self.timer.timeout.connect(self.request)
        editor.textChanged.connect(self._typed)

    # ---------------------------------------------------------- triggering
    def _typed(self):
        if self.ed.ghost_text and self.ed.ghost_kept:
            self.ed.ghost_kept = False                  # the editor kept the ghost (typed its next char)
            return
        self.ed.clear_ghost()
        self.cancel()
        if enabled() and self.api_key_fn() and not self.ed.isReadOnly() and self._at_good_spot():
            self.timer.start()

    def _at_good_spot(self):
        c = self.ed.textCursor()
        if c.hasSelection() or self.ed.completer.popup().isVisible():
            return False
        rest = c.block().text()[c.positionInBlock():]
        return not rest.strip() or rest.strip() in (")", "]", "}", "):", "):")

    def request(self, manual=False):
        """Ask Claude for a suggestion at the cursor (Alt+\\ = manual)."""
        self.timer.stop()
        key = self.api_key_fn()
        if not key or (not manual and not self._at_good_spot()):
            return
        self.cancel()
        doc = self.ed.toPlainText()
        c = self.ed.textCursor()
        pos = c.position()
        before, after = doc[max(0, pos - PREFIX_CHARS):pos], doc[pos:pos + SUFFIX_CHARS]
        name = (self.ed.path or "untitled.py").split("/")[-1]
        prompt = (f"File: {name}\n<code_before_cursor>\n{before}</code_before_cursor>\n"
                  f"<code_after_cursor>\n{after}\n</code_after_cursor>\nText to insert at the cursor:")
        payload = {"model": model(), "max_tokens": 300, "system": SYSTEM,
                   "messages": [{"role": "user", "content": prompt}]}
        req = QNetworkRequest(QUrl(self.api_url().rstrip("/") + "/v1/messages"))
        req.setRawHeader(b"x-api-key", key.encode())
        req.setRawHeader(b"anthropic-version", self.versions[0].encode())
        req.setRawHeader(b"content-type", b"application/json")
        self._req_state = (self.ed.document().revision(), pos, c.block().text()[:c.positionInBlock()])
        self.reply = self.net.post(req, QByteArray(json.dumps(payload).encode()))
        self.reply.finished.connect(self._done)

    def cancel(self):
        if self.reply is not None:
            r, self.reply = self.reply, None
            r.finished.disconnect()
            r.abort()
            r.deleteLater()

    def _done(self):
        r, self.reply = self.reply, None
        if r is None:
            return
        data = bytes(r.readAll())
        r.deleteLater()
        state = self._req_state
        c = self.ed.textCursor()
        if state is None or self.ed.document().revision() != state[0] or c.position() != state[1]:
            return                                       # you kept typing: this answer is stale
        try:
            obj = json.loads(data.decode("utf-8"))
            text = "".join(b.get("text", "") for b in obj.get("content", []) if b.get("type") == "text")
        except (ValueError, AttributeError):
            return
        text = clean(text, state[2])
        if text:
            self.ed.show_ghost(text)
