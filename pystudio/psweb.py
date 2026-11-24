# ------------------------------------------------------------------ WebBrowser (VB: WebBrowser control)
# Appended to pystudio_runtime.py. QtWebEngine is imported only when a WebBrowser is created.
def make_browser_class():
    from PySide6.QtCore import QEvent, QUrl, Signal
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView as _Base
        ok = True
    except ImportError:
        from PySide6.QtWidgets import QLabel as _Base
        ok = False

    class PsWebBrowser(_Base):
        """A web page inside a form (VB: WebBrowser).

            self.ui.webBrowser1.navigate("https://www.weather.gov")
            self.ui.webBrowser1.set_html("<h1>Hello</h1>")
            self.ui.webBrowser1.go_back() / go_forward() / refresh() / stop()

        Events: documentCompleted(url), navigated(url), titleChanged(title), loadProgress(percent)."""
        documentCompleted = Signal(str)
        navigated = Signal(str)

        def __init__(self, parent=None):
            super().__init__(parent)
            if ok:
                self.loadFinished.connect(lambda _ok: self.documentCompleted.emit(self.url().toString()))
                self.urlChanged.connect(lambda u: self.navigated.emit(u.toString()))
            else:
                self.setText("WebBrowser needs QtWebEngine:\npip install PySide6-Addons")
                self.setWordWrap(True)

        def event(self, e):
            if e.type() == QEvent.DynamicPropertyChange and bytes(e.propertyName()) == b"startUrl":
                if self.property("startUrl"):
                    self.navigate(self.property("startUrl"))
            return super().event(e)

        def navigate(self, url):
            """Go to a web address, or show a local file (path)."""
            import os
            if not ok:
                return
            url = str(url).strip()
            if os.path.exists(url):
                self.setUrl(QUrl.fromLocalFile(os.path.abspath(url)))
            else:
                if "://" not in url and not url.startswith(("about:", "data:", "file:")):
                    url = "https://" + url
                self.setUrl(QUrl(url))

        def set_html(self, html, base_url=""):
            if ok:
                self.setHtml(html, QUrl(base_url)) if base_url else self.setHtml(html)

        @property
        def url_text(self):
            return self.url().toString() if ok else ""

        def go_back(self):
            ok and self.back()

        def go_forward(self):
            ok and self.forward()

        def refresh(self):
            ok and self.reload()

    return PsWebBrowser


try:
    _LAZY["PsWebBrowser"] = make_browser_class     # inside pystudio_runtime.py
except NameError:
    pass
