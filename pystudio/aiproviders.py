"""The AI behind the Assistant, the Ask actions and inline suggestions: Claude, Gemini or a local model.

Claude  - Anthropic's API; a key from console.anthropic.com, paid per use (prepaid credits).
Gemini  - Google's API; a free key from aistudio.google.com/apikey. The free tier has daily and per-minute
          limits, and Google may use what you send on the free tier to improve its products - keep work
          code and data out of it.
Local   - Ollama runs a model on this computer: free, private (nothing leaves the machine), no key. Smaller
          and slower than Claude or Gemini, especially without a graphics card. The Assistant's settings
          install Ollama and download a model for you.

All three clients have the same shape: send(messages, system, model, max_tokens) streams the answer
through delta(str), then finished(full text) or failed(message); cancel(); busy; list_models(callback)."""
import json
import os
import shutil

from PySide6.QtCore import QByteArray, QObject, QSettings, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

GEMINI_URL = os.environ.get("PYSTUDIO_GEMINI_URL", "https://generativelanguage.googleapis.com")
OLLAMA_URL = os.environ.get("PYSTUDIO_OLLAMA_URL", "http://127.0.0.1:11434")

PROVIDERS = ["Claude", "Gemini", "Local (Ollama)"]
GEMINI_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-pro-latest"]
OLLAMA_MODELS = ["qwen2.5-coder:3b", "qwen2.5-coder:7b"]
OLLAMA_SUGGESTED = [("qwen2.5-coder:3b", "about 2 GB - quickest on a laptop"),
                    ("qwen2.5-coder:7b", "about 4.7 GB - better answers, slower without a graphics card")]
INSTALL_OLLAMA = ("(command -v curl >/dev/null && curl -fsSL https://ollama.com/install.sh "
                  "|| wget -qO- https://ollama.com/install.sh) | sh")


def provider():
    p = QSettings().value("aiProvider", "Claude") or "Claude"
    return p if p in PROVIDERS else "Claude"


def short_name(p=None):
    p = p or provider()
    return "Ollama" if p.startswith("Local") else p


def model_key(p=None):
    return {"Claude": "claudeModel", "Gemini": "geminiModel"}.get(p or provider(), "ollamaModel")


def default_models(p=None):
    p = p or provider()
    if p == "Claude":
        from .assistant import MODELS
        return list(MODELS)
    return list(GEMINI_MODELS if p == "Gemini" else OLLAMA_MODELS)


def model_name(p=None):
    p = p or provider()
    return QSettings().value(model_key(p), "") or default_models(p)[0]


def inline_model(p=None):
    """The model for inline suggestions: a fast one."""
    p = p or provider()
    if p == "Claude":
        from .ghost import DEFAULT_MODEL
        return QSettings().value("inlineModel", DEFAULT_MODEL) or DEFAULT_MODEL
    if p == "Gemini":
        return "gemini-flash-lite-latest"
    return model_name(p)


def gemini_key():
    from . import keystore
    return keystore.get("geminiApiKey") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")


def ready(p=None):
    """Can it be used without asking for anything? (Local: assumed; a connection error explains.)"""
    p = p or provider()
    if p == "Claude":
        from .assistant import api_key
        return bool(api_key())
    if p == "Gemini":
        return bool(gemini_key())
    return True


def make_client(parent=None, p=None):
    p = p or provider()
    if p == "Claude":
        from .assistant import ClaudeClient
        return ClaudeClient(parent)
    return GeminiClient(parent) if p == "Gemini" else OllamaClient(parent)


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


class _StreamClient(QObject):
    """Shared streaming plumbing (POST, read as it arrives, errors)."""
    delta = Signal(str)
    finished = Signal(str)
    failed = Signal(str)
    NAME = "AI"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.net = QNetworkAccessManager(self)
        self.reply = None
        self._buf = b""
        self._text = ""
        self._err = b""

    @property
    def busy(self):
        return self.reply is not None

    def cancel(self):
        if self.reply is not None:
            r, self.reply = self.reply, None
            r.readyRead.disconnect()
            r.finished.disconnect()
            r.abort()
            r.deleteLater()

    def _post(self, url, body, headers):
        self.cancel()
        self._buf, self._text, self._err = b"", "", b""
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"content-type", b"application/json")
        for k, v in headers.items():
            req.setRawHeader(k.encode(), v.encode())
        req.setTransferTimeout(600000)
        self.reply = self.net.post(req, QByteArray(json.dumps(body).encode("utf-8")))
        self.reply.readyRead.connect(self._on_read)
        self.reply.finished.connect(self._on_finished)

    def _status(self):
        s = self.reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) if self.reply else None
        return int(s) if s else 0

    def _on_read(self):
        if self.reply is None:
            return
        data = bytes(self.reply.readAll())
        if self._status() >= 400:
            self._err += data
            return
        self._buf += data
        self._parse()

    def _piece(self, text):
        if text:
            self._text += text
            self.delta.emit(text)

    def _on_finished(self):
        r = self.reply
        if r is None:
            return
        self._on_read()
        self._parse(final=True)
        status, err, err_s = self._status(), r.error(), r.errorString()
        self.reply = None
        r.deleteLater()
        if self._err:
            self.failed.emit(self._explain(status, self._err.decode("utf-8", "replace")))
        elif err != QNetworkReply.NoError and not self._text:
            self.failed.emit(self._unreachable(err, err_s))
        else:
            self.finished.emit(self._text)

    def _unreachable(self, err, text):
        return f"Couldn't reach {self.NAME}: {text}"


# ============================================================ Gemini
class GeminiClient(_StreamClient):
    NAME = "Gemini"

    def send(self, messages, system="", model=None, max_tokens=4096):
        contents = [{"role": "model" if m.get("role") == "assistant" else "user",
                     "parts": [{"text": _text_of(m.get("content", ""))}]} for m in messages]
        body = {"contents": contents, "generationConfig": {"maxOutputTokens": max_tokens}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        m = (model or model_name("Gemini")).replace("models/", "")
        self._post(f"{GEMINI_URL.rstrip('/')}/v1beta/models/{m}:streamGenerateContent?alt=sse", body,
                   {"x-goog-api-key": gemini_key()})

    def _parse(self, final=False):
        buf = self._buf.replace(b"\r\n", b"\n")
        events = buf.split(b"\n\n")
        self._buf = b"" if final else events.pop()
        for ev in events:
            for line in ev.decode("utf-8", "replace").splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if "error" in obj:
                    self._err = json.dumps(obj).encode()
                    continue
                for cand in obj.get("candidates", []) or []:
                    for part in (cand.get("content") or {}).get("parts", []) or []:
                        if not part.get("thought"):
                            self._piece(part.get("text", ""))
                block = (obj.get("promptFeedback") or {}).get("blockReason")
                if block:
                    self._err = json.dumps({"error": {"message": f"Gemini declined to answer ({block})."}}).encode()

    @staticmethod
    def _explain(status, body):
        try:
            e = json.loads(body)
            e = e[0] if isinstance(e, list) else e
            msg = e.get("error", {}).get("message", body)
        except (ValueError, AttributeError):
            msg = body[:500]
        if "API key not valid" in msg or status in (401, 403) and "key" in msg.lower():
            hint = "The Gemini API key was rejected - check it in the Assistant's settings (gear button)."
        else:
            hint = {404: "Unknown model - pick another in the model list (or click Refresh).",
                    429: "Gemini's free-tier limit was reached (per minute or per day) - wait a bit, or check "
                         "aistudio.google.com.",
                    503: "Gemini is busy right now - try again in a minute."}.get(status, "")
        return f"Gemini error {status or ''}: {msg}" + (f"\n{hint}" if hint else "")

    def list_models(self, callback):
        req = QNetworkRequest(QUrl(f"{GEMINI_URL.rstrip('/')}/v1beta/models?pageSize=1000"))
        req.setRawHeader(b"x-goog-api-key", gemini_key().encode())
        r = self.net.get(req)

        def done():
            ids = []
            try:
                for m in json.loads(bytes(r.readAll()).decode("utf-8")).get("models", []):
                    name = m.get("name", "").replace("models/", "")
                    if "generateContent" in m.get("supportedGenerationMethods", []) and "gemini" in name and \
                            not any(x in name for x in ("tts", "image", "embedding", "live", "audio")):
                        ids.append(name)
            except (ValueError, AttributeError):
                pass
            r.deleteLater()
            callback(sorted(set(ids), key=lambda n: ("latest" not in n, n)))
        r.finished.connect(done)


# ============================================================ Ollama (local)
class OllamaClient(_StreamClient):
    NAME = "Ollama"

    def send(self, messages, system="", model=None, max_tokens=4096):
        msgs = ([{"role": "system", "content": system}] if system else []) + \
            [{"role": m.get("role", "user"), "content": _text_of(m.get("content", ""))} for m in messages]
        self._model = model or model_name("Local (Ollama)")
        self._post(f"{OLLAMA_URL.rstrip('/')}/api/chat",
                   {"model": self._model, "messages": msgs, "stream": True,
                    "options": {"num_predict": max_tokens}}, {})

    def _parse(self, final=False):
        lines = self._buf.split(b"\n")
        self._buf = b"" if final else lines.pop()
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("error"):
                self._err = json.dumps(obj).encode()
                continue
            self._piece((obj.get("message") or {}).get("content", ""))

    def _explain(self, status, body):
        try:
            msg = json.loads(body).get("error", body)
        except (ValueError, AttributeError):
            msg = body[:500]
        if "not found" in str(msg).lower():
            return (f"The model {getattr(self, '_model', '')} isn't downloaded yet. Open the Assistant's settings "
                    f"(gear button) > Download Model.")
        return f"Ollama error {status or ''}: {msg}"

    def _unreachable(self, err, text):
        if err in (QNetworkReply.ConnectionRefusedError, QNetworkReply.HostNotFoundError):
            if not shutil.which("ollama"):
                return ("Ollama isn't installed. Open the Assistant's settings (gear button) > Install Ollama - "
                        "it's free and runs on this computer.")
            return "Ollama isn't running. Start it from a terminal with:  ollama serve   (or restart the computer)."
        return f"Couldn't reach Ollama: {text}"

    def list_models(self, callback):
        r = self.net.get(QNetworkRequest(QUrl(f"{OLLAMA_URL.rstrip('/')}/api/tags")))

        def done():
            ids = []
            try:
                ids = [m["name"] for m in json.loads(bytes(r.readAll()).decode("utf-8")).get("models", [])]
            except (ValueError, KeyError, TypeError):
                pass
            r.deleteLater()
            callback(ids)
        r.finished.connect(done)


def ollama_installed():
    return bool(shutil.which("ollama"))
