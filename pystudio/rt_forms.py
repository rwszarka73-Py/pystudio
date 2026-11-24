# ------------------------------------------------------------------ forms, dialogs, user controls
# Appended to pystudio_runtime.py (not imported by the IDE).
import types as _types

from PySide6.QtCore import QEventLoop, QRectF, Signal
from PySide6.QtGui import QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import QMainWindow, QPushButton


if QApplication.instance() is None:      # lets a WebBrowser (QtWebEngine) work in any app
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)


class DialogResult(str):
    """What show_dialog() returns: "OK", "Cancel", "Yes", "No", "Abort", "Retry", "Ignore" or "None".
    It's also True for OK/Yes, so both  if dlg.show_dialog():  and  == "OK"  work."""

    def __bool__(self):
        return str(self) in ("OK", "Yes")


DIALOG_RESULTS = ("None", "OK", "Cancel", "Yes", "No", "Abort", "Retry", "Ignore")


class _HideWatch(QObject):
    def __init__(self, window, loop):
        super().__init__(window)
        self.loop = loop
        window.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.Hide, QEvent.Close) and not obj.isVisible():
            self.loop.quit()
        return False


def _show_dialog(self, owner=None):
    """Show this form as a modal dialog (VB: ShowDialog) and wait until it closes.
    Returns its DialogResult - set by a button's DialogResult property or self.dialog_result."""
    owner = owner if owner is not None else QApplication.activeWindow()
    if owner is self:
        owner = None
    if owner is not None and self.parent() is None:
        self.setParent(owner, self.windowFlags() | Qt.Dialog)    # stays above its owner
        if self.property("startPosition") in (None, "", "CenterParent", "WindowsDefaultLocation"):
            g = self.frameGeometry()
            g.moveCenter(owner.frameGeometry().center())
            self.move(g.topLeft())
    self._ps_result = None
    loop = QEventLoop()
    self._ps_loop = loop
    watch = _HideWatch(self, loop)
    self.setWindowModality(Qt.ApplicationModal)
    self.show()
    self.raise_()
    self.activateWindow()
    loop.exec()
    self._ps_loop = None
    watch.deleteLater()
    self.setWindowModality(Qt.NonModal)
    return DialogResult(self._ps_result or "Cancel")


def _set_dialog_result(window, value):
    window._ps_result = value
    if getattr(window, "_ps_loop", None) is not None and value not in (None, "", "None"):
        window.close()


class Form(QMainWindow):
    """Base class of PyStudio forms: a QMainWindow plus VB's ShowDialog/DialogResult.

        dlg = Form2()
        if dlg.show_dialog() == "OK":      # waits until Form2 closes
            print(dlg.ui.textBox1.text())
    """
    _ps_result = None
    _ps_loop = None

    def __init__(self, parent=None):
        super().__init__(parent)

    show_dialog = _show_dialog

    @property
    def dialog_result(self):
        return DialogResult(self._ps_result or "None")

    @dialog_result.setter
    def dialog_result(self, value):
        """Setting it on a form shown with show_dialog() closes the form (like VB)."""
        _set_dialog_result(self, value)


class UserControl(QMainWindow):
    """Base class of a UserControl: a piece of UI designed once and placed on forms like
    any control (from the Toolbox's '<project> Components' group)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Widget)


def _in_user_control(w, window):
    """True for widgets that belong to a UserControl placed on window (it sets itself up)."""
    p = w.parentWidget()
    while p is not None and p is not window:
        if isinstance(p, UserControl):
            return True
        p = p.parentWidget()
    return False


def _own_widgets(window):
    return [w for w in window.findChildren(QWidget) if not _in_user_control(w, window)]


def _form_extras(window):
    """AcceptButton (Enter), CancelButton (Esc) and buttons' DialogResult."""
    if not isinstance(window, Form) and not isinstance(window, UserControl):
        window.show_dialog = _types.MethodType(_show_dialog, window)     # forms made before v1.2
    for b in window.findChildren(QPushButton):
        v = b.property("dialogResult")
        if v and v != "None" and not _in_user_control(b, window):
            b.clicked.connect(lambda _c=False, v=v: _set_dialog_result(window, v))
    if isinstance(window, UserControl):
        return
    acc = window.findChild(QPushButton, window.property("acceptButton") or "-")
    if acc is not None:
        acc.setDefault(True)
        for key in (Qt.Key_Return, Qt.Key_Enter):
            QShortcut(QKeySequence(key), window, acc.click)
    can = window.findChild(QPushButton, window.property("cancelButton") or "-")
    if can is not None:
        QShortcut(QKeySequence(Qt.Key_Escape), window, can.click)


# ------------------------------------------------------------------ printing
class PrintPageEventArgs:
    """Passed to a PrintDocument's printPage handler (VB: PrintPageEventArgs).
    e.graphics is a QPainter; units are screen pixels (about 1/96 inch).
    Set e.has_more_pages = True to print another page."""

    def __init__(self, painter, printer, page_number):
        self.graphics = painter
        self.printer = printer
        self.page_number = page_number
        r = printer.pageLayout().fullRectPixels(printer.resolution())
        self.page_bounds = QRectF(0, 0, r.width(), r.height())
        m = printer.pageLayout().paintRectPixels(printer.resolution())
        self.margin_bounds = QRectF(m.x(), m.y(), m.width(), m.height())
        self.has_more_pages = False
        self.cancel = False


def _new_printer(document_name="document", landscape=False):
    from PySide6.QtGui import QPageLayout
    from PySide6.QtPrintSupport import QPrinter
    printer = QPrinter(QPrinter.ScreenResolution)
    printer.setDocName(document_name)
    printer.setFullPage(True)       # (0, 0) is the paper's corner; margin_bounds is the printable area
    if landscape:
        printer.setPageOrientation(QPageLayout.Landscape)
    return printer


class PrintDocument(QObject):
    """VB's PrintDocument: handle printPage and draw the page with e.graphics.

        def printDocument1_printPage(self, e):
            e.graphics.drawText(e.margin_bounds.topLeft() + QPointF(0, 20), "Hello")
    """
    printPage = Signal(object)
    beginPrint = Signal(object)
    endPrint = Signal(object)

    def __init__(self, parent, documentName="document", landscape=False):
        super().__init__(parent)
        self.document_name = documentName
        self.printer = _new_printer(documentName, landscape)

    def print(self, printer=None):
        """Print on the default printer (or the one a PrintDialog chose) without asking."""
        self._render(printer or self.printer)

    def _render(self, printer):
        painter = QPainter()
        if not painter.begin(printer):
            raise RuntimeError("Couldn't start printing (is a printer available?)")
        self.beginPrint.emit(self)
        try:
            page = 1
            while True:
                e = PrintPageEventArgs(painter, printer, page)
                self.printPage.emit(e)
                if e.cancel or not e.has_more_pages or page >= 999:
                    break
                printer.newPage()
                page += 1
        finally:
            painter.end()
            self.endPrint.emit(self)


class PrintDialog:
    """Choose a printer and pages, then print:  if self.ui.printDialog1.show_dialog(): doc.print()"""

    def __init__(self, parent, document=""):
        self.parent, self.document = parent, document

    def show_dialog(self):
        from PySide6.QtPrintSupport import QPrintDialog
        doc = self.document if isinstance(self.document, PrintDocument) else None
        printer = doc.printer if doc else _new_printer()
        return QPrintDialog(printer, self.parent).exec() == 1


class PrintPreviewDialog:
    """Shows the Document's pages on screen with zoom and a Print button."""

    def __init__(self, parent, document="", title="Print Preview"):
        self.parent, self.document, self.title = parent, document, title

    def show_dialog(self):
        from PySide6.QtPrintSupport import QPrintPreviewDialog
        doc = self.document if isinstance(self.document, PrintDocument) else None
        if doc is None:
            raise RuntimeError("Set the PrintPreviewDialog's Document to a PrintDocument first")
        dlg = QPrintPreviewDialog(doc.printer, self.parent)
        dlg.setWindowTitle(self.title or "Print Preview")
        dlg.paintRequested.connect(doc._render)
        dlg.resize(900, 700)
        return dlg.exec() == 1


class PageSetupDialog:
    """Paper size, orientation and margins for a PrintDocument."""

    def __init__(self, parent, document=""):
        self.parent, self.document = parent, document

    def show_dialog(self):
        from PySide6.QtPrintSupport import QPageSetupDialog
        doc = self.document if isinstance(self.document, PrintDocument) else None
        printer = doc.printer if doc else _new_printer()
        return QPageSetupDialog(printer, self.parent).exec() == 1


def print_widget(widget, preview=True, landscape=None, title="Print"):
    """Print a form or any control as it looks on screen, scaled to fit the page
    (VB PowerPacks PrintForm). preview=True shows Print Preview first."""
    pm = widget.grab()
    doc = PrintDocument(widget, title, landscape if landscape is not None else pm.width() > pm.height())

    def page(e):
        area = e.margin_bounds
        s = min(area.width() / max(1, pm.width()), area.height() / max(1, pm.height()), 1.0)
        e.graphics.drawPixmap(QRectF(area.x(), area.y(), pm.width() * s, pm.height() * s), pm,
                              QRectF(pm.rect()))
    doc.printPage.connect(page)
    if preview:
        return PrintPreviewDialog(widget, doc, title).show_dialog()
    from PySide6.QtPrintSupport import QPrintDialog
    if QPrintDialog(doc.printer, widget).exec() == 1:
        doc.print()
        return True
    return False


_FACTORIES.update({"PrintDocument": PrintDocument, "PrintDialog": PrintDialog,
                   "PrintPreviewDialog": PrintPreviewDialog, "PageSetupDialog": PageSetupDialog})


def _link_components(target, objs):
    """Components that name another component (PrintPreviewDialog.Document = printDocument1)."""
    for obj in objs:
        ref = getattr(obj, "document", None)
        if isinstance(ref, str) and ref:
            obj.document = getattr(target, ref, ref)


# ------------------------------------------------------------------ Chart / WebBrowser classes
_LAZY = {}


def __getattr__(name):
    """PsChart and PsWebBrowser are built on first use, so apps that don't use them never
    load QtCharts / QtWebEngine."""
    if name in _LAZY:
        cls = _LAZY[name]()
        globals()[name] = cls
        return cls
    raise AttributeError(f"module 'pystudio_runtime' has no attribute {name!r}")
