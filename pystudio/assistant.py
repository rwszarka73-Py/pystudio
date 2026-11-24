"""Claude Assistant panel (View > Claude Assistant, Ctrl+Alt+A) - like Visual Studio's Copilot Chat.

Ask about your code; Claude sees the current file (and selection, errors, and the form's controls) and
answers in the panel. Code in an answer has Insert / Replace selection / Copy links. Quick buttons:
Explain, Fix Errors, Translate VB/C# (for projects imported from VB.NET or C#), Write Tests.

Uses the Claude API (https://api.anthropic.com/v1/messages) with your own API key from
console.anthropic.com (Settings > API Keys). The key is kept in the desktop keyring (see keystore.py) or read from the
ANTHROPIC_API_KEY environment variable."""
import json
import os
import re

from PySide6.QtCore import QByteArray, QObject, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTextBrowser,
                               QToolButton, QVBoxLayout, QWidget)

from . import aiproviders, theme

API_URL = os.environ.get("PYSTUDIO_CLAUDE_URL", "https://api.anthropic.com")
API_VERSIONS = ["2023-06-01", "2026-10-21"]      # tried in order if the API rejects the version
MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
MAX_FILE_CHARS = 60000

SYSTEM_PROMPT = """You are Claude, the coding assistant built into PyStudio - a Visual Studio-style Python IDE for \
Linux. PyStudio projects are PySide6 (Qt) desktop apps designed like VB.NET WinForms:
- Each form is form1.ui (designed in PyStudio's designer) + form1.py (the code-behind). Controls are \
self.ui.<name>, e.g. self.ui.textBox1.text(), self.ui.label1.setText("Hi"), self.ui.dataGridView1.
- Event handlers are methods connected in __init__ under "# Event wiring", e.g. \
self.ui.button1.clicked.connect(self.on_button1_clicked).
- pystudio_runtime provides msgbox(text, title, buttons, icon) like VB's MsgBox, the Form base class with \
show_dialog()/dialog_result, and data helpers: my.query(name_or_sql, **params), my.execute(...), my.scalar(...), \
fill(widget, query), current_row(widget), save(widget). my.settings.<Name> holds project settings.
- Projects imported from VB.NET or C# have the original code as comments inside each handler, starting with "# VB" or "# C#".
The user is an experienced VB.NET WinForms developer learning Python: relate Python to VB.NET when it helps \
(e.g. "like VB's For Each"), and point out Python-specific gotchas. Use US spelling. Be concise and direct.
When you give code, give complete, working code in ```python fences - a whole method or a whole file, not \
fragments with "..." - so it can be inserted as-is. Don't change things the user didn't ask about."""


def api_key():
    from . import keystore
    return keystore.get("claudeApiKey") or os.environ.get("ANTHROPIC_API_KEY", "")


def model_name():
    return QSettings().value("claudeModel", MODELS[0]) or MODELS[0]


# ============================================================ API client
class ClaudeClient(QObject):
    """Streams one reply from the Messages API."""
    delta = Signal(str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.net = QNetworkAccessManager(self)
        self.reply = None
        self._buf = b""
        self._text = ""
        self._payload = None
        self._version_i = 0
        self._error_body = b""

    @property
    def busy(self):
        return self.reply is not None

    def _request(self, path):
        req = QNetworkRequest(QUrl(API_URL.rstrip("/") + path))
        req.setRawHeader(b"x-api-key", api_key().encode())
        req.setRawHeader(b"anthropic-version", API_VERSIONS[self._version_i].encode())
        req.setRawHeader(b"content-type", b"application/json")
        return req

    def send(self, messages, system=SYSTEM_PROMPT, model=None, max_tokens=4096):
        self.cancel()
        self._payload = {"model": model or model_name(), "max_tokens": max_tokens, "stream": True,
                         "system": system, "messages": messages}
        self._version_i = 0
        self._start()

    def _start(self):
        self._buf, self._text, self._error_body = b"", "", b""
        self.reply = self.net.post(self._request("/v1/messages"),
                                   QByteArray(json.dumps(self._payload).encode("utf-8")))
        self.reply.readyRead.connect(self._on_read)
        self.reply.finished.connect(self._on_finished)

    def cancel(self):
        if self.reply is not None:
            r, self.reply = self.reply, None
            r.readyRead.disconnect()
            r.finished.disconnect()
            r.abort()
            r.deleteLater()

    def _status(self):
        return self.reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) if self.reply else None

    def _on_read(self):
        if self.reply is None:
            return
        data = bytes(self.reply.readAll())
        status = self._status()
        if status and int(status) >= 400:
            self._error_body += data
            return
        self._buf += data
        while b"\n\n" in self._buf:
            event, self._buf = self._buf.split(b"\n\n", 1)
            self._handle_event(event.decode("utf-8", "replace"))

    def _handle_event(self, event):
        for line in event.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            t = ev.get("type")
            if t == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                piece = ev["delta"].get("text", "")
                self._text += piece
                self.delta.emit(piece)
            elif t == "error":
                self._error_body = json.dumps(ev).encode()

    def _on_finished(self):
        r = self.reply
        if r is None:
            return
        self._on_read()
        status = self._status()
        err = r.error()
        self.reply = None
        r.deleteLater()
        body = self._error_body.decode("utf-8", "replace")
        if status and int(status) == 400 and "version" in body.lower() and self._version_i + 1 < len(API_VERSIONS):
            self._version_i += 1                     # the API wants a different anthropic-version
            self._start()
            return
        if body:
            self.failed.emit(self._explain(status, body))
        elif err != QNetworkReply.NoError and not self._text:
            self.failed.emit(f"Couldn't reach the Claude API: {r.errorString()}")
        else:
            self.finished.emit(self._text)

    @staticmethod
    def _explain(status, body):
        try:
            msg = json.loads(body).get("error", {}).get("message", body)
        except ValueError:
            msg = body[:500]
        hints = {401: "The API key was rejected - check it in the AI Assistant's settings (gear button).",
                 403: "This API key isn't allowed to do that.",
                 404: "Unknown model - pick another in the model list (or click Refresh).",
                 429: "Rate limited or out of credits - wait a moment, or check console.anthropic.com.",
                 529: "Claude is overloaded right now - try again in a minute."}
        hint = hints.get(int(status) if status else 0, "")
        return f"Claude API error {status or ''}: {msg}" + (f"\n{hint}" if hint else "")

    def list_models(self, callback):
        r = self.net.get(self._request("/v1/models?limit=100"))

        def done():
            ids = []
            try:
                data = json.loads(bytes(r.readAll()).decode("utf-8"))
                ids = [m["id"] for m in data.get("data", []) if "id" in m]
            except (ValueError, KeyError, TypeError):
                pass
            r.deleteLater()
            callback(ids)
        r.finished.connect(done)


# ============================================================ settings dialog
class AssistantSettings(QDialog):
    """Which AI (Claude / Gemini / local / any OpenAI-compatible), its key, and - for the local one - installing
    Ollama and models."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QStackedWidget, QWidget as _W
        from . import keystore
        self.setWindowTitle("AI Assistant Settings")
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.cb_provider = QComboBox()
        self.cb_provider.addItems(aiproviders.PROVIDERS)
        self.cb_provider.setCurrentText(aiproviders.provider())
        f.addRow("AI:", self.cb_provider)
        v.addLayout(f)
        self.pages = QStackedWidget()
        v.addWidget(self.pages)

        def key_row(form, edit, placeholder):
            edit.setEchoMode(QLineEdit.Password)
            edit.setPlaceholderText(placeholder)
            show = QCheckBox("Show")
            show.toggled.connect(lambda on: edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(show)
            form.addRow("API key:", row)

        def note(form, html):
            lab = QLabel(html)
            lab.setOpenExternalLinks(True)
            lab.setWordWrap(True)
            form.addRow(lab)
            return lab
        # Claude
        pc = _W()
        fc = QFormLayout(pc)
        fc.setContentsMargins(0, 0, 0, 0)
        self.e_key = QLineEdit(keystore.get("claudeApiKey"))
        key_row(fc, self.e_key, "sk-ant-...  (or set ANTHROPIC_API_KEY)")
        note(fc, 'Get a key at <a href="https://console.anthropic.com/settings/keys">console.anthropic.com</a>. '
                 "Claude API use is paid per use (prepaid credits), separately from a Claude subscription.")
        self.pages.addWidget(pc)
        # Gemini
        pg = _W()
        fg = QFormLayout(pg)
        fg.setContentsMargins(0, 0, 0, 0)
        self.e_gkey = QLineEdit(keystore.get("geminiApiKey"))
        key_row(fg, self.e_gkey, "AIza...  (or set GEMINI_API_KEY)")
        note(fg, 'Get a <b>free</b> key at <a href="https://aistudio.google.com/apikey">aistudio.google.com/apikey</a> '
                 "(sign in with a Google account, Create API key). The free tier has per-minute and daily limits. "
                 "<b>Google may use what you send on the free tier to improve its products</b> - keep work code "
                 "and data out of it (use the local model for those).")
        self.pages.addWidget(pg)
        # Local (Ollama)
        po = _W()
        fo = QFormLayout(po)
        fo.setContentsMargins(0, 0, 0, 0)
        note(fo, "Runs a model on this computer with <b>Ollama</b>: free, private (nothing leaves the machine), no "
                 "key. It's smaller and slower than Claude or Gemini - on a laptop without a graphics card, "
                 "expect answers in tens of seconds.")
        self.l_ollama = QLabel()
        self.l_ollama.setWordWrap(True)
        fo.addRow("Status:", self.l_ollama)
        self.cb_pull = QComboBox()
        for name, about in aiproviders.OLLAMA_SUGGESTED:
            self.cb_pull.addItem(f"{name}  ({about})", name)
        self.cb_pull.setEditable(False)
        fo.addRow("Model:", self.cb_pull)
        self.b_setup = QPushButton()
        self.b_setup.clicked.connect(self.set_up_local)
        fo.addRow("", self.b_setup)
        self.pending_command = None          # run in the Terminal after this window closes
        self.pending_model = None
        self.pages.addWidget(po)
        # Other (OpenAI-compatible)
        px = _W()
        fx = QFormLayout(px)
        fx.setContentsMargins(0, 0, 0, 0)
        note(fx, "Any AI that accepts OpenAI's request format: ChatGPT, OpenRouter, Groq, Mistral, DeepSeek, xAI, "
                 "LM Studio and many more. Pick one to fill in its server, or type your own.")
        self.cb_preset = QComboBox()
        self.cb_preset.addItem("Pick a service...", None)
        for name, url, model in aiproviders.OTHER_PRESETS:
            self.cb_preset.addItem(name, (name, url, model))
        self.cb_preset.activated.connect(self._preset_picked)
        fx.addRow("Service:", self.cb_preset)
        self.e_oname = QLineEdit(QSettings().value("otherName", "") or "")
        self.e_oname.setPlaceholderText("what to call it, e.g. ChatGPT")
        fx.addRow("Name:", self.e_oname)
        self.e_ourl = QLineEdit(QSettings().value("otherUrl", "") or "")
        self.e_ourl.setPlaceholderText("https://api.openai.com/v1")
        fx.addRow("Server URL:", self.e_ourl)
        self.e_okey = QLineEdit(keystore.get("otherApiKey"))
        key_row(fx, self.e_okey, "from the service's site (leave empty for a local server)")
        self.e_omodel = QLineEdit(QSettings().value("otherModel", "") or "")
        self.e_omodel.setPlaceholderText("e.g. gpt-4o-mini  (the refresh button next to the model list loads them)")
        fx.addRow("Model:", self.e_omodel)
        note(fx, "These services charge per use or have their own free limits, and their own rules about what "
                 "they keep - check the service's site before sending work code.")
        self.pages.addWidget(px)
        self.cb_provider.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.pages.setCurrentIndex(self.cb_provider.currentIndex())
        f2 = QFormLayout()
        self.cb_files = QCheckBox("Send the current file with each question")
        self.cb_files.setChecked(QSettings().value("claudeSendFile", True, type=bool))
        f2.addRow(self.cb_files)
        kl = QLabel(f"Keys are stored in {keystore.where()}, never in your projects or settings exports.")
        kl.setStyleSheet("color: #9d9d9d;")
        kl.setWordWrap(True)
        f2.addRow(kl)
        v.addLayout(f2)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.check_ollama()

    def check_ollama(self):
        if not aiproviders.ollama_installed():
            self.l_ollama.setText("Ollama isn't installed yet. Pick a model and click the button: this window "
                                  "closes, the Terminal asks for your password, then Ollama and the model are "
                                  "installed. The Assistant tells you when it's ready.")
            self.b_setup.setText("Install Ollama and Download the Model")
            return
        self.b_setup.setText("Download Model")
        self.l_ollama.setText("Ollama is installed. Checking its models...")
        c = aiproviders.OllamaClient(self)

        def got(ids):
            try:
                self.l_ollama.setText(("Installed. Downloaded models: " + ", ".join(ids)) if ids else
                                      "Installed, but no model is downloaded yet - Download Model below. (If this "
                                      "stays empty after downloading, Ollama isn't running: restart the computer "
                                      "or run  ollama serve.)")
            except RuntimeError:
                pass
        c.list_models(got)

    def set_up_local(self):
        """Close this window (it would keep the Terminal from getting your password) and let the Assistant run
        the install / download there and watch for the model."""
        name = self.cb_pull.currentData()
        pull = f"ollama pull {name}"
        if aiproviders.ollama_installed():
            self.pending_command = pull
        else:
            self.pending_command = f"{aiproviders.INSTALL_OLLAMA} && {pull}"
        self.pending_model = name
        self.cb_provider.setCurrentText("Local (Ollama)")
        QSettings().setValue("ollamaModel", name)
        self.accept()

    def _preset_picked(self, i):
        d = self.cb_preset.itemData(i)
        if not d:
            return
        name, url, model = d
        self.e_oname.setText(name.split(" (")[0] if "(local)" not in name else name.replace(" (local)", ""))
        self.e_ourl.setText(url)
        if model:
            self.e_omodel.setText(model)
        if self.e_omodel.text() and QSettings().value("otherUrl", "") != url:
            QSettings().remove("otherModels")          # another service: its own model list
        self.e_okey.setFocus()

    def apply(self):
        s = QSettings()
        from . import keystore
        keystore.set("claudeApiKey", self.e_key.text().strip())
        keystore.set("geminiApiKey", self.e_gkey.text().strip())
        keystore.set("otherApiKey", self.e_okey.text().strip())
        url = self.e_ourl.text().strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            local = url.startswith(("localhost", "127.", "0.0.0.0", "192.168.", "10.")) or ".local" in url
            url = ("http://" if local else "https://") + url
        s.setValue("otherUrl", url)
        s.setValue("otherName", self.e_oname.text().strip())
        s.setValue("otherModel", self.e_omodel.text().strip())
        s.setValue("aiProvider", self.cb_provider.currentText())
        s.setValue("claudeSendFile", self.cb_files.isChecked())


# ============================================================ panel
CODE_RE = re.compile(r"```[\w+-]*\n(.*?)```", re.S)


class _Input(QPlainTextEdit):
    submit = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.submit.emit()
            return
        super().keyPressEvent(e)


class AssistantPanel(QWidget):
    """The dock contents. MainWindow supplies context() -> dict and the editor actions."""
    insert_code = Signal(str)         # at the cursor
    replace_selection = Signal(str)

    def __init__(self, context_provider=None):
        super().__init__()
        self.context_provider = context_provider     # callable -> {"path", "text", "selection", "errors", "form"}
        self.client = None
        self._make_client()
        self.history = []          # API messages [{"role", "content"}]
        self.shown = []            # (role, markdown) as displayed
        self.codes = []            # code blocks in answers, for the Insert/Copy links
        self._streaming = ""
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(80)
        self._render_timer.timeout.connect(self._render)

        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.l_provider = QLabel()
        self.l_provider.setStyleSheet("color: #9d9d9d;")
        top.addWidget(self.l_provider)
        self.cb_model = QComboBox()
        self.cb_model.setEditable(True)
        self.cb_model.currentTextChanged.connect(
            lambda t: t.strip() and QSettings().setValue(aiproviders.model_key(), t.strip()))
        self._fill_models()
        top.addWidget(self.cb_model, 1)
        b_refresh = QToolButton()
        b_refresh.setText("⟳")
        b_refresh.setToolTip("Refresh the model list from the API")
        b_refresh.clicked.connect(self._refresh_models)
        top.addWidget(b_refresh)
        b_new = QToolButton()
        b_new.setText("New chat")
        b_new.clicked.connect(self.new_chat)
        top.addWidget(b_new)
        b_set = QToolButton()
        b_set.setText("⚙")
        b_set.setToolTip("API key and settings")
        b_set.clicked.connect(self.settings)
        top.addWidget(b_set)
        v.addLayout(top)

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._link)
        v.addWidget(self.view, 1)

        quick = QHBoxLayout()
        for text, tip, prompt in (
                ("Explain", "Explain the selection (or the file)", "explain"),
                ("Fix Errors", "Fix the errors in the Error List for this file", "fix"),
                ("Translate VB/C#", "Translate the commented VB.NET or C# code in the selection or this file "
                                    "to Python", "vb"),
                ("Write Tests", "Write pytest tests for the selection or this file", "tests")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, p=prompt: self.quick(p))
            quick.addWidget(b)
        v.addLayout(quick)

        self.l_note = QLabel()
        self.l_note.setWordWrap(True)
        self.l_note.hide()
        v.addWidget(self.l_note)
        self.input = _Input()
        self.input.setPlaceholderText(f"Ask {aiproviders.short_name()} about your code...  (Enter sends, "
                                      "Shift+Enter for a new line)")
        self.input.setMaximumHeight(90)
        self.input.submit.connect(self.send)
        v.addWidget(self.input)
        bottom = QHBoxLayout()
        self.ck_context = QCheckBox("Include current file")
        self.ck_context.setChecked(QSettings().value("claudeSendFile", True, type=bool))
        self.ck_context.setToolTip("Send the open file (and selection, errors, form controls) with the question")
        bottom.addWidget(self.ck_context)
        bottom.addStretch()
        self.b_send = QPushButton("Send")
        self.b_send.clicked.connect(self.send)
        self.b_stop = QPushButton("Stop")
        self.b_stop.clicked.connect(self.stop)
        self.b_stop.hide()
        bottom.addWidget(self.b_send)
        bottom.addWidget(self.b_stop)
        v.addLayout(bottom)
        self._render()

    # ---------------------------------------------------------- provider
    def _make_client(self):
        if self.client is not None:
            self.client.cancel()
            self.client.deleteLater()
        self.client = aiproviders.make_client(self)
        self.client.delta.connect(self._on_delta)
        self.client.finished.connect(self._on_finished)
        self.client.failed.connect(self._on_failed)

    def _fill_models(self):
        p = aiproviders.provider()
        self.cb_model.blockSignals(True)
        self.cb_model.clear()
        self.cb_model.addItems(aiproviders.default_models(p))
        self.cb_model.setCurrentText(aiproviders.model_name(p))
        self.cb_model.blockSignals(False)
        self.l_provider.setText(aiproviders.short_name(p) + ":")
        self.cb_model.setToolTip({"Claude": "Claude model (Sonnet: fast and smart; Opus: the most capable; Haiku: "
                                            "fastest)",
                                  "Gemini": "Gemini model (flash: fast, free tier; flash-lite: fastest; pro: "
                                            "smartest, may not be free)",
                                  aiproviders.OTHER: "The service's model (the refresh button loads its list)"}.get(
            p, "Local model (downloaded with Ollama; the gear button downloads more)"))
        if hasattr(self, "input"):
            self.input.setPlaceholderText(f"Ask {aiproviders.short_name(p)} about your code...  (Enter sends, "
                                          "Shift+Enter for a new line)")
            self.input.viewport().update()     # Qt only repaints a placeholder when it appears or disappears

    def provider_changed(self):
        self.stop()
        self._make_client()
        self._fill_models()
        self._render()

    # ---------------------------------------------------------- actions
    def settings(self):
        before = aiproviders.provider()
        dlg = AssistantSettings(self)
        if dlg.exec():
            dlg.apply()
            self.ck_context.setChecked(QSettings().value("claudeSendFile", True, type=bool))
            if aiproviders.provider() != before or before == aiproviders.OTHER:
                self.provider_changed()
            self._render()
            if dlg.pending_command:
                self.run_in_terminal(dlg.pending_command)
                self.watch_ollama(dlg.pending_model)

    def run_in_terminal(self, command):
        """Runs a command in PyStudio's Terminal and puts the keyboard there (for the sudo password)."""
        win = self.window()
        term = getattr(win, "terminal", None)
        if term is None:
            QMessageBox.information(self, "AI Assistant", f"Run this in a terminal:\n\n{command}")
            return
        dock = getattr(win, "terminal_dock", None)
        if dock is not None:
            dock.show()
            dock.raise_()
        t = term.current() or term.new_terminal(os.path.expanduser("~"))

        def go():
            try:
                os.write(t.master, (command + "\n").encode())
                t.setFocus()
                t.activateWindow()
            except (OSError, AttributeError, RuntimeError):
                pass
        QTimer.singleShot(700, go)

    def watch_ollama(self, name):
        """Polls Ollama until the model is downloaded, then switches to it and says so."""
        self._watch_for = name
        self._watch_tries = 0
        self.note(f"Setting up {name} in the Terminal - type your password there if it asks. A 2-5 GB download "
                  "can take a while; this says when it's ready.")
        if getattr(self, "_watch_timer", None) is None:
            self._watch_timer = QTimer(self)
            self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_timer.start(4000)

    def _watch_tick(self):
        self._watch_tries += 1
        if self._watch_tries > 900:                       # an hour
            self._watch_timer.stop()
            self.note("")
            return
        if not aiproviders.ollama_installed():
            return
        name = self._watch_for

        def got(ids):
            try:
                if self._watch_timer.isActive() and any(i == name or i == name + ":latest" or
                                                        i.split(":")[0] == name for i in ids):
                    self._watch_timer.stop()
                    QSettings().setValue("aiProvider", "Local (Ollama)")
                    QSettings().setValue("ollamaModel", name)
                    self.provider_changed()
                    self.note(f"✓ {name} is ready - ask away. (It runs on this computer, so the first answer "
                              "takes a little longer while it loads.)", ok=True)
                    win = self.window()
                    win.statusBar().showMessage(f"Local AI model {name} is ready.", 10000)
            except RuntimeError:
                pass
        if getattr(self, "_watch_client", None) is None:
            self._watch_client = aiproviders.OllamaClient(self)
        self._watch_client.list_models(got)

    def note(self, text, ok=False):
        self.l_note.setText(text)
        self.l_note.setStyleSheet("color: %s; padding: 2px;" % ("#89d185" if ok else "#e5c07b"))
        self.l_note.setVisible(bool(text))

    def _refresh_models(self):
        if not aiproviders.ready():
            self.settings()
            return

        def got(ids):
            if ids:
                cur = self.cb_model.currentText()
                self.cb_model.clear()
                self.cb_model.addItems(ids)
                self.cb_model.setCurrentText(cur if cur in ids else ids[0])
        self.client.list_models(got)

    def new_chat(self):
        self.stop()
        self.history, self.shown, self.codes, self._streaming = [], [], [], ""
        self._render()

    def stop(self):
        if self.client.busy:
            self.client.cancel()
            self._finish_turn(self._streaming + "\n\n*(stopped)*")

    QUICK = {
        "explain": "Explain what this code does, step by step, relating it to VB.NET where that helps.",
        "fix": "Fix the errors listed below. Show the corrected code and briefly say what was wrong.",
        "vb": "Translate the commented VB.NET or C# code (the lines after '# VB' or '# C#') into working Python for this "
              "PyStudio form, replacing the commented code and the 'pass'. Use self.ui.<control> for controls. "
              "Give the complete translated method(s).",
        "tests": "Write pytest tests for this code (a tests/test_<module>.py file). Cover normal cases and edge cases.",
    }

    def quick(self, kind):
        self.input.setPlainText(self.QUICK[kind])
        self.send(force_context=True, include_errors=(kind == "fix"))

    def send(self, force_context=False, include_errors=False):
        if not (getattr(self, "_watch_timer", None) is not None and self._watch_timer.isActive()):
            self.note("")
        text = self.input.toPlainText().strip()
        if not text or self.client.busy:
            return
        if not aiproviders.ready():
            self.settings()
            if not aiproviders.ready():
                return
        ctx = self.context_provider() if self.context_provider else {}
        content = text
        with_ctx = bool((self.ck_context.isChecked() or force_context) and ctx.get("path"))
        if with_ctx:
            content = self._with_context(text, ctx, include_errors or "error" in text.lower()
                                         or "fix" in text.lower())
        self.history.append({"role": "user", "content": content})
        label = text + (f"\n\n<sub>📎 {os.path.basename(ctx['path'])}"
                        + (" (selection)" if ctx.get("selection") else "") + "</sub>" if with_ctx else "")
        self.shown.append(("user", label))
        self.input.clear()
        self._streaming = ""
        self.b_send.hide()
        self.b_stop.show()
        self.client.send(self.history, SYSTEM_PROMPT.replace("You are Claude,", f"You are "
                         f"{aiproviders.short_name()},") if aiproviders.provider() != "Claude" else SYSTEM_PROMPT,
                         model=self.cb_model.currentText().strip() or None)
        self._render()

    @staticmethod
    def _with_context(question, ctx, include_errors):
        parts = []
        path = ctx.get("path") or ""
        body = ctx.get("text") or ""
        if len(body) > MAX_FILE_CHARS:
            body = body[:MAX_FILE_CHARS] + "\n# ... (file truncated)"
        parts.append(f"Current file: {os.path.basename(path)}\n```python\n{body}\n```")
        if ctx.get("form"):
            parts.append("Controls on this file's form (name: type): " + ", ".join(ctx["form"]))
        if ctx.get("selection"):
            parts.append(f"Selected code (lines {ctx.get('sel_lines', '?')}):\n```python\n{ctx['selection']}\n```")
        if include_errors and ctx.get("errors"):
            parts.append("Errors and warnings in this file:\n" + "\n".join(ctx["errors"]))
        return "\n\n".join(parts) + f"\n\nQuestion: {question}"

    # ---------------------------------------------------------- streaming
    def _on_delta(self, piece):
        self._streaming += piece
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _on_finished(self, text):
        self._finish_turn(text)

    def _on_failed(self, msg):
        if self.history and self.history[-1]["role"] == "user":
            self.history.pop()                       # let the user resend
        self.shown.append(("error", msg))
        self._streaming = ""
        self.b_stop.hide()
        self.b_send.show()
        self._render()

    def _finish_turn(self, text):
        self.history.append({"role": "assistant", "content": text or "(no answer)"})
        self.shown.append(("assistant", text))
        self._streaming = ""
        self.b_stop.hide()
        self.b_send.show()
        self._render()

    # ---------------------------------------------------------- display
    def _answer_md(self, text):
        """Markdown of an answer, with Insert/Replace/Copy links after each code block."""
        out, pos = [], 0
        for m in CODE_RE.finditer(text):
            out.append(text[pos:m.end()])
            i = len(self.codes)
            self.codes.append(m.group(1))
            out.append(f"\n[Insert at cursor](ps:insert:{i}) · [Replace selection](ps:replace:{i})"
                       f" · [Copy](ps:copy:{i})\n")
            pos = m.end()
        out.append(text[pos:])
        return "".join(out)

    def _render(self):
        self.codes = []
        fg, muted, accent, err = theme.c("#d4d4d4"), theme.c("#9d9d9d"), theme.c("#4fc1ff"), theme.c("#f48771")
        md = []
        if not self.shown and not self._streaming:
            name = aiproviders.short_name()
            md.append(f"### AI Assistant ({name})\nAsk anything about your code: *why doesn't this work?*, "
                      "*how do I read a CSV into the grid?*, *what's the Python for VB's Select Case?*\n\n"
                      f"{name} sees the open file (and your selection, errors, and form's controls). "
                      "Code in answers gets **Insert at cursor** / **Replace selection** / **Copy** links. "
                      "The ⚙ button switches between Claude, Gemini (free key), a local model (free, private), and "
                      "other AIs like ChatGPT.")
            if not aiproviders.ready() and aiproviders.provider() == aiproviders.OTHER:
                md.append("\n\n**First, set it up:** click ⚙ above and pick a service (or enter its server URL).")
            elif not aiproviders.ready():
                md.append("\n\n**First, add your API key:** click ⚙ above. "
                          + ("[Get a free key at aistudio.google.com](https://aistudio.google.com/apikey)"
                             if aiproviders.provider() == "Gemini" else
                             "[Get a key at console.anthropic.com](https://console.anthropic.com/settings/keys)"))
        for role, text in self.shown:
            if role == "user":
                md.append(f"\n\n---\n\n**You:** {text}")
            elif role == "assistant":
                md.append(f"\n\n**{aiproviders.short_name()}:**\n\n" + self._answer_md(text))
            else:
                md.append(f"\n\n**⚠ {text}**")
        if self._streaming or self.client.busy:
            md.append(f"\n\n**{aiproviders.short_name()}:**\n\n" + (self._streaming or "*thinking...*"))
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 30
        self.view.document().setDefaultStyleSheet(
            f"body {{ color: {fg}; }} a {{ color: {accent}; }} code, pre {{ font-family: monospace; }}"
            f" .err {{ color: {err}; }} sub {{ color: {muted}; }}")
        self.view.setMarkdown("".join(md))
        if at_bottom or self.client.busy:
            sb.setValue(sb.maximum())

    def _link(self, url):
        s = url.toString()
        m = re.match(r"ps:(insert|replace|copy):(\d+)", s)
        if not m:
            QDesktopServices.openUrl(url)
            return
        # codes are numbered as rendered: re-render to make sure the list is current
        idx = int(m.group(2))
        if idx >= len(self.codes):
            return
        code = self.codes[idx]
        if m.group(1) == "copy":
            QApplication.clipboard().setText(code)
        elif m.group(1) == "insert":
            self.insert_code.emit(code)
        else:
            self.replace_selection.emit(code)

    def ask(self, prompt, force_context=True, include_errors=False):
        """From the editor's right-click menu."""
        self.input.setPlainText(prompt)
        self.send(force_context=force_context, include_errors=include_errors)


def apply_code(editor, code, replace):
    """Insert code at the editor's cursor (or replace the selection), as one undo step, matching the
    indentation of the line it goes into."""
    c = editor.textCursor()
    if not replace and c.hasSelection():
        c.setPosition(c.selectionEnd())
    block_text = c.block().text()
    indent = block_text[:len(block_text) - len(block_text.lstrip())]
    lines = code.rstrip("\n").split("\n")
    base = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
    if replace and c.hasSelection():
        start = editor.document().findBlock(c.selectionStart())
        st = start.text()
        indent = st[:len(st) - len(st.lstrip())]
        # replace whole lines
        s, e = c.selectionStart(), c.selectionEnd()
        if e > s and editor.document().findBlock(e).position() == e:
            e -= 1                                    # selection ends at the start of the next line
        c.setPosition(start.position())
        end_block = editor.document().findBlock(e)
        c.setPosition(end_block.position() + end_block.length() - 1, QTextCursor.KeepAnchor)
        out = "\n".join((indent + ln[base:]) if ln.strip() else "" for ln in lines)
    else:
        at_line_start = not block_text[:c.positionInBlock()].strip()
        if at_line_start:
            c.movePosition(QTextCursor.StartOfBlock)
            if block_text.strip():
                out = "\n".join((indent + ln[base:]) if ln.strip() else "" for ln in lines) + "\n"
            else:
                c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                out = "\n".join((indent + ln[base:]) if ln.strip() else "" for ln in lines)
        else:
            out = "\n".join(lines)
    c.beginEditBlock()
    c.insertText(out)
    c.endEditBlock()
    editor.setTextCursor(c)
    editor.setFocus()


# ============================================================ MainWindow mixin
class AssistantMixin:
    def _assistant_actions(self):
        from PySide6.QtGui import QKeySequence
        self.a_assistant = self._act("&AI Assistant", self.show_assistant)
        self.a_assistant.setShortcuts([QKeySequence("Ctrl+Alt+A"), QKeySequence("Ctrl+\\, Ctrl+C")])
        self.a_ask_explain = self._act("Ask AI: &Explain", lambda: self._ask("explain"))
        self.a_ask_fix = self._act("Ask AI: &Fix Errors", lambda: self._ask("fix"))
        self.a_suggest = self._act("Inline Suggestion (&Claude)", self.inline_suggestion, "Alt+\\")
        self.a_ask_vb = self._act("Ask AI: Translate &VB/C#", lambda: self._ask("vb"))

    def _assistant_init(self):
        from PySide6.QtWidgets import QDockWidget
        self.assistant = AssistantPanel(self._assistant_context)
        self.assistant.insert_code.connect(lambda code: self._apply_assistant_code(code, False))
        self.assistant.replace_selection.connect(lambda code: self._apply_assistant_code(code, True))
        self.assistant_dock = QDockWidget("AI Assistant", self)
        self.assistant_dock.setObjectName("ClaudeAssistant")
        self.assistant_dock.setWidget(self.assistant)
        self.addDockWidget(Qt.RightDockWidgetArea, self.assistant_dock)
        self.tabifyDockWidget(self.props_dock, self.assistant_dock)
        self.assistant_dock.hide()

    def attach_ghost(self, ed):
        """Inline suggestions (gray ghost text) for a code editor."""
        from .ghost import GhostCompleter
        ed.ghost = GhostCompleter(ed)

    def inline_suggestion(self):
        """Alt+\\: ask Claude for a suggestion at the cursor now."""
        from .editor import CodeEditor
        ed = self.current_editor()
        if not isinstance(ed, CodeEditor) or ed.ghost is None:
            return
        if not aiproviders.ready():
            self.show_assistant()
            self.assistant.settings()
            if not aiproviders.ready():
                return
        self.statusBar().showMessage(f"Asking {aiproviders.short_name()} for a suggestion...", 3000)
        ed.ghost.request(manual=True)

    def show_assistant(self):
        self.assistant_dock.show()
        self.assistant_dock.raise_()
        self.assistant.input.setFocus()

    def _ask(self, kind):
        self.show_assistant()
        self.assistant.quick(kind)

    def _assistant_code_editor(self):
        """The code editor the assistant works with: the current one, or the code behind the open form."""
        from .editor import CodeEditor
        doc = self.current_doc()
        if isinstance(doc, CodeEditor):
            return doc
        if getattr(doc, "path", "") and doc.path.endswith(".ui"):
            py = os.path.splitext(doc.path)[0] + ".py"
            for ed in self.editors():
                if isinstance(ed, CodeEditor) and ed.path and os.path.abspath(ed.path) == os.path.abspath(py):
                    return ed
        return None

    def _assistant_context(self):
        from .navigation import ui_controls
        ed = self._assistant_code_editor()
        doc = self.current_doc()
        ctx = {}
        if ed is not None and ed.path:
            ctx["path"], ctx["text"] = ed.path, ed.toPlainText()
            c = ed.textCursor()
            if c.hasSelection():
                ctx["selection"] = c.selectedText().replace(" ", "\n")
                doc_ = ed.document()
                ctx["sel_lines"] = (f"{doc_.findBlock(c.selectionStart()).blockNumber() + 1}-"
                                    f"{doc_.findBlock(c.selectionEnd()).blockNumber() + 1}")
            ctx["errors"] = [f"line {d.line}: {d.severity}: {d.message}" for d in ed.diagnostics]
        elif getattr(doc, "path", "") and doc.path.endswith(".ui"):
            py = os.path.splitext(doc.path)[0] + ".py"
            if os.path.exists(py):
                ctx["path"], ctx["text"] = py, self._code_text(py)
        if ctx.get("path"):
            ui = os.path.splitext(ctx["path"])[0] + ".ui"
            if os.path.exists(ui):
                with open(ui, encoding="utf-8", errors="replace") as fh:
                    names = {n: c for c, n in re.findall(r'<widget class="(\w+)" name="(\w+)"', fh.read())}
                ctx["form"] = [f"{n}: {names.get(n, '?')}" for _k, n, _c, _l in ui_controls(ui)]
        return ctx

    def _apply_assistant_code(self, code, replace):
        ed = self._assistant_code_editor()
        if ed is None:
            QApplication.clipboard().setText(code)
            self.statusBar().showMessage("No code file is open - the code was copied to the clipboard.", 5000)
            return
        if self.current_doc() is not ed:
            self.tabs.setCurrentWidget(ed)
        if ed.isReadOnly():
            QApplication.clipboard().setText(code)
            self.statusBar().showMessage("That file is read-only - the code was copied to the clipboard.", 5000)
            return
        apply_code(ed, code, replace)
        self.statusBar().showMessage("Code inserted - Ctrl+Z undoes it.", 4000)
