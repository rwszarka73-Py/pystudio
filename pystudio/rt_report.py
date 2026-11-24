# ------------------------------------------------------------------ Reports (like VB's ReportViewer / RDLC)
# A report (reports/<Name>.psrpt) is bands - Report Header, Page Header, Group Header, Detail, Group Footer,
# Page Footer, Report Footer - with text, lines, boxes and pictures placed on them. Text can hold fields and
# totals in braces:  {Name}  {Sum(Score)}  {Count()}  {Avg(Score)}  {PageNumber} of {TotalPages}  {Today}
#   show_report("DriverScores", region="East")         - a preview window with Print and Export PDF
#   export_report("DriverScores", "scores.pdf", ...)   - straight to a PDF file
#   print_report("DriverScores", ...)                  - the print dialog
# This file is also used by PyStudio's report designer (preview), so both look the same.
import datetime as _rp_dt
import json as _rp_json
import sys
import os as _rp_os
import re as _rp_re

BAND_ORDER = ["reportHeader", "pageHeader", "groupHeader", "detail", "groupFooter", "pageFooter", "reportFooter"]
BAND_TITLES = {"reportHeader": "Report Header", "pageHeader": "Page Header", "groupHeader": "Group Header",
               "detail": "Detail", "groupFooter": "Group Footer", "pageFooter": "Page Footer",
               "reportFooter": "Report Footer"}
PAGE_SIZES = {"Letter": (816, 1056), "Legal": (816, 1344), "A4": (794, 1123), "Tabloid": (1056, 1632)}
_RP_PLACEHOLDER = _rp_re.compile(r"\{\{|\}\}|\{([^{}]+)\}")
_RP_AGG = _rp_re.compile(r"^\s*(Sum|Avg|Min|Max|Count)\s*\(\s*(\w*)\s*\)\s*$", _rp_re.I)


def report_page_size(report):
    pg = report.get("page", {})
    w, h = PAGE_SIZES.get(pg.get("size", "Letter"), PAGE_SIZES["Letter"])
    if pg.get("orientation") == "Landscape":
        w, h = h, w
    return w, h


def report_margins(report):
    m = report.get("page", {}).get("margins") or [48, 48, 48, 48]      # left, top, right, bottom (1/96 in)
    return [int(x) for x in (list(m) + [48] * 4)[:4]]


def report_content_width(report):
    w, _h = report_page_size(report)
    m = report_margins(report)
    return w - m[0] - m[2]


def new_report(name, data_source="", columns=()):
    """A starting layout like VS's Report Wizard: title, column headings, one line per row, totals, page numbers."""
    rep = {"name": name, "dataSource": data_source, "connection": "", "groupBy": "", "sortBy": "",
           "alternateRows": "#f3f6fa", "page": {"size": "Letter", "orientation": "Portrait",
                                                 "margins": [48, 48, 48, 48]},
           "bands": {b: {"height": 0, "items": []} for b in BAND_ORDER}}
    width = report_content_width(rep)
    title = {"type": "text", "x": 0, "y": 4, "w": width, "h": 34, "text": name,
             "font": {"size": 18, "bold": True}, "color": "#1f4e79"}
    sub = {"type": "text", "x": 0, "y": 38, "w": width, "h": 18, "text": "Printed {Now}",
           "font": {"size": 8}, "color": "#666666", "format": "%m/%d/%Y %I:%M %p"}
    rep["bands"]["reportHeader"] = {"height": 64, "items": [title, sub]}
    cols = list(columns)[:8]
    if cols:
        cw = width // len(cols)
        heads, fields = [], []
        for i, c in enumerate(cols):
            heads.append({"type": "text", "x": i * cw, "y": 4, "w": cw - 4, "h": 18, "text": str(c),
                          "font": {"size": 9, "bold": True}, "color": "#1f4e79"})
            fields.append({"type": "text", "x": i * cw, "y": 2, "w": cw - 4, "h": 18, "text": "{" + str(c) + "}",
                           "font": {"size": 9}})
        heads.append({"type": "line", "x": 0, "y": 24, "w": width, "h": 0, "color": "#1f4e79"})
        rep["bands"]["pageHeader"] = {"height": 28, "items": heads}
        rep["bands"]["detail"] = {"height": 22, "items": fields}
    rep["bands"]["reportFooter"] = {"height": 30, "items": [
        {"type": "line", "x": 0, "y": 4, "w": width, "h": 0, "color": "#999999"},
        {"type": "text", "x": 0, "y": 8, "w": width, "h": 18, "text": "{Count()} rows", "font": {"size": 9,
                                                                                               "bold": True}}]}
    rep["bands"]["pageFooter"] = {"height": 24, "items": [
        {"type": "text", "x": 0, "y": 4, "w": width, "h": 16, "text": "Page {PageNumber} of {TotalPages}",
         "font": {"size": 8}, "color": "#666666", "align": "right"}]}
    return rep


def load_report_file(path):
    with open(path, encoding="utf-8") as f:
        return _rp_json.load(f)


# ------------------------------------------------------------------ values
def _rp_text(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.10g}"
    return str(v)


def _rp_format(v, fmt):
    if not fmt or v is None:
        return _rp_text(v)
    try:
        if "%" in fmt:
            if isinstance(v, (_rp_dt.date, _rp_dt.datetime)):
                return v.strftime(fmt)
            s = str(v)
            for pat in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return _rp_dt.datetime.strptime(s[:len(_rp_dt.datetime.now().strftime(pat))], pat).strftime(fmt)
                except ValueError:
                    pass
            return s
        if isinstance(v, str):
            try:
                v = float(v) if "." in v else int(v)
            except ValueError:
                return v
        return format(v, fmt)
    except (ValueError, TypeError):
        return _rp_text(v)


def _rp_num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class _RpScope:
    def __init__(self, renderer, row=None, rows=(), page=1, pages=1):
        self.r, self.row, self.rows, self.page, self.pages = renderer, row, rows, page, pages


def _rp_value(scope, expr):
    e = expr.strip()
    low = e.lower()
    if low == "pagenumber":
        return scope.page
    if low == "totalpages":
        return scope.pages
    if low == "today":
        return _rp_dt.date.today()
    if low == "now":
        return _rp_dt.datetime.now()
    if low == "reportname":
        return scope.r.report.get("name", "")
    m = _rp_re.match(r"^param(?:eter)?s?\s*\(\s*(\w+)\s*\)$", e, _rp_re.I)
    if m:
        return scope.r.params.get(m.group(1), "")
    m = _RP_AGG.match(e)
    if m:
        fn, col = m.group(1).lower(), m.group(2)
        rows = scope.rows
        if fn == "count":
            return len(rows)
        i = scope.r.col_index(col)
        if i is None:
            return f"#Error({col})"
        vals = [x for x in (_rp_num(r[i]) for r in rows) if x is not None]
        if not vals:
            return 0 if fn == "sum" else None
        if fn == "sum":
            return sum(vals)
        if fn == "avg":
            return sum(vals) / len(vals)
        return min(vals) if fn == "min" else max(vals)
    i = scope.r.col_index(e)
    if i is None:
        return f"#Error({e})"
    row = scope.row if scope.row is not None else (scope.rows[0] if scope.rows else None)
    return row[i] if row is not None else None


def evaluate_text(scope, text, fmt=""):
    def sub(m):
        if m.group(0) == "{{":
            return "{"
        if m.group(0) == "}}":
            return "}"
        return _rp_format(_rp_value(scope, m.group(1)), fmt)
    return _RP_PLACEHOLDER.sub(sub, text or "")


# ------------------------------------------------------------------ renderer
class ReportRenderer:
    def __init__(self, report, columns, rows, params=None, base_dir="."):
        self.report = report
        self.columns = [str(c) for c in columns]
        self._lower = {c.lower(): i for i, c in enumerate(self.columns)}
        self.params = dict(params or {})
        self.base_dir = base_dir
        self.rows = self._sorted(list(rows))
        self.groups = self._grouped(self.rows)

    def col_index(self, name):
        return self._lower.get(str(name).lower())

    def _sorted(self, rows):
        keys = [k.strip() for k in (self.report.get("sortBy") or "").split(",") if k.strip()]
        group = (self.report.get("groupBy") or "").strip()
        if group:
            keys = [group] + [k for k in keys if k.split()[0].lower() != group.lower()]
        for k in reversed(keys):
            bits = k.split()
            i = self.col_index(bits[0])
            if i is None:
                continue
            desc = len(bits) > 1 and bits[1].upper() == "DESC"
            rows.sort(key=lambda r, i=i: (r[i] is None, r[i] if not isinstance(r[i], str) else r[i].lower()),
                      reverse=desc)
        return rows

    def _grouped(self, rows):
        i = self.col_index(self.report.get("groupBy") or "")
        if i is None:
            return [rows]
        out, cur, key = [], [], object()
        for r in rows:
            if cur and r[i] != key:
                out.append(cur)
                cur = []
            key = r[i]
            cur.append(r)
        if cur:
            out.append(cur)
        return out or [[]]

    def band(self, name):
        b = self.report.get("bands", {}).get(name) or {}
        return b if b.get("items") or b.get("height") else None

    def _height(self, name):
        b = self.band(name)
        return int(b.get("height", 0)) if b else 0

    # -------------------------------------------------- layout: what goes on which page
    def paginate(self, page_h):
        """[[(band, y, scope_args)]] per page; page_h = printable height in 1/96 in. The page footer is
        drawn separately at the bottom of every page."""
        grouped = self.col_index(self.report.get("groupBy") or "") is not None
        ph, pf = self._height("pageHeader"), self._height("pageFooter")
        bottom = page_h - pf
        pages, st = [], {"cur": [], "y": 0}
        first_top = 0

        def start():
            st["cur"], st["y"] = [], 0
            if ph:
                st["cur"].append(("pageHeader", 0, {}))
                st["y"] = ph

        def place(band, args):
            h = self._height(band)
            if not h:
                return
            if st["y"] + h > bottom and st["y"] > (first_top if not pages else ph):
                pages.append(st["cur"])
                start()
            st["cur"].append((band, st["y"], args))
            st["y"] += h

        # page 1: the report header (title) first, then the page header (column headings) under it
        rh = self._height("reportHeader")
        if rh:
            st["cur"].append(("reportHeader", 0, {"rows": self.rows}))
            st["y"] = rh
        if ph:
            st["cur"].append(("pageHeader", st["y"], {}))
            st["y"] += ph
        first_top = st["y"]
        for g in self.groups:
            if grouped:
                place("groupHeader", {"rows": g, "row": g[0] if g else None})
            for n, r in enumerate(g):
                place("detail", {"rows": g, "row": r, "index": n})
            if grouped:
                place("groupFooter", {"rows": g, "row": g[-1] if g else None})
        place("reportFooter", {"rows": self.rows})
        pages.append(st["cur"])
        return pages

    # -------------------------------------------------- drawing
    def render(self, printer):
        """Draw every page on a QPrinter (preview, real printer or PDF)."""
        from PySide6.QtCore import QMarginsF
        from PySide6.QtGui import QPageLayout, QPageSize, QPainter
        pg = self.report.get("page", {})
        size_ids = {"Letter": QPageSize.Letter, "Legal": QPageSize.Legal, "A4": QPageSize.A4,
                    "Tabloid": QPageSize.Tabloid}
        printer.setPageSize(QPageSize(size_ids.get(pg.get("size", "Letter"), QPageSize.Letter)))
        printer.setPageOrientation(QPageLayout.Landscape if pg.get("orientation") == "Landscape"
                                   else QPageLayout.Portrait)
        ml, mt, mr, mb = report_margins(self.report)
        printer.setPageMargins(QMarginsF(ml * 0.75, mt * 0.75, mr * 0.75, mb * 0.75), QPageLayout.Point)
        _w, page_h = report_page_size(self.report)
        pages = self.paginate(page_h - mt - mb)
        p = QPainter()
        if not p.begin(printer):
            raise RuntimeError("Couldn't start printing")
        scale = printer.resolution() / 96.0
        footer_y = page_h - mt - mb - self._height("pageFooter")
        try:
            for n, page in enumerate(pages, 1):
                if n > 1:
                    printer.newPage()
                p.save()
                p.scale(scale, scale)
                for band, y, args in page:
                    rows = args.get("rows", self.rows)
                    self._draw_band(p, band, y, _RpScope(self, args.get("row"), rows, n, len(pages)),
                                    args.get("index"))
                if self._height("pageFooter"):
                    self._draw_band(p, "pageFooter", footer_y, _RpScope(self, None, self.rows, n, len(pages)))
                p.restore()
        finally:
            p.end()
        return len(pages)

    def _draw_band(self, p, band, y0, scope, index=None):
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QFont, QImage, QPen
        b = self.band(band)
        if not b:
            return
        alt = self.report.get("alternateRows")
        if band == "detail" and alt and index is not None and index % 2 == 1:
            p.fillRect(QRectF(0, y0, report_content_width(self.report), int(b.get("height", 0))), QColor(alt))
        for it in b.get("items", []):
            r = QRectF(it.get("x", 0), y0 + it.get("y", 0), max(0, it.get("w", 0)), max(0, it.get("h", 0)))
            color = QColor(it.get("color") or "#000000")
            t = it.get("type", "text")
            if t == "line":
                pen = QPen(color, max(0.5, float(it.get("width", 1))))
                p.setPen(pen)
                p.drawLine(r.topLeft(), r.bottomRight())
                continue
            if t == "box":
                if it.get("fill"):
                    p.fillRect(r, QColor(it["fill"]))
                p.setPen(QPen(color, max(0.5, float(it.get("width", 1)))))
                p.setBrush(Qt.NoBrush)
                p.drawRect(r)
                continue
            if t == "image":
                path = it.get("image", "")
                full = path if _rp_os.path.isabs(path) else _rp_os.path.join(self.base_dir, path)
                img = QImage(full)
                if not img.isNull():
                    sized = img.scaled(int(r.width() * 3), int(r.height() * 3), Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
                    w, h = sized.width() / 3, sized.height() / 3
                    p.drawImage(QRectF(r.x(), r.y(), w, h), sized)
                continue
            if it.get("fill"):
                p.fillRect(r, QColor(it["fill"]))
            f = it.get("font") or {}
            font = QFont(f.get("family") or "Sans Serif")
            font.setPixelSize(max(4, round(float(f.get("size", 9)) * 96 / 72)))
            font.setBold(bool(f.get("bold")))
            font.setItalic(bool(f.get("italic")))
            font.setUnderline(bool(f.get("underline")))
            p.setFont(font)
            p.setPen(color)
            align = {"left": Qt.AlignLeft, "center": Qt.AlignHCenter, "right": Qt.AlignRight}.get(
                it.get("align", "left"), Qt.AlignLeft)
            flags = int(align) | int(Qt.AlignVCenter) | (int(Qt.TextWordWrap) if it.get("wrap") else 0)
            text = evaluate_text(scope, it.get("text", ""), it.get("format", ""))
            p.drawText(r, flags, text)
            if it.get("border"):
                p.setPen(QPen(QColor("#999999"), 0.75))
                p.drawRect(r)


# ------------------------------------------------------------------ runtime API
def _report_path(name):
    if name.endswith(".psrpt") and _rp_os.path.exists(name):
        return name
    for base in (getattr(sys, "_MEIPASS", None), _rp_os.path.dirname(_rp_os.path.abspath(sys.argv[0] or ".")),
                 _rp_os.getcwd()):
        if base:
            p = _rp_os.path.join(base, "reports", name if name.endswith(".psrpt") else name + ".psrpt")
            if _rp_os.path.exists(p):
                return p
    raise FileNotFoundError(f"Report not found: reports/{name}.psrpt")


def _report_renderer(name, params, data=None):
    path = _report_path(name)
    rep = load_report_file(path)
    if data is None:
        src = rep.get("dataSource") or ""
        data = query(src, rep.get("connection") or None, **params) if src else DataTable()
    base = _rp_os.path.dirname(_rp_os.path.dirname(path))
    return ReportRenderer(rep, data.columns, list(data), params, base)


def export_report(name, pdf_path, data=None, **params):
    """Save a report as a PDF. data: a DataTable to use instead of the report's query."""
    from PySide6.QtPrintSupport import QPrinter
    r = _report_renderer(name, params, data)
    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(pdf_path)
    return r.render(printer)


def print_report(name, data=None, parent=None, **params):
    from PySide6.QtPrintSupport import QPrintDialog, QPrinter
    r = _report_renderer(name, params, data)
    printer = QPrinter(QPrinter.HighResolution)
    dlg = QPrintDialog(printer, parent or QApplication.activeWindow())
    if dlg.exec():
        r.render(printer)
        return True
    return False


def show_report(name, data=None, parent=None, **params):
    """Open a report in a preview window (zoom, Print, Export PDF) - like VB's ReportViewer."""
    from PySide6.QtPrintSupport import QPrinter, QPrintPreviewDialog
    from PySide6.QtWidgets import QFileDialog, QToolBar
    r = _report_renderer(name, params, data)
    printer = QPrinter(QPrinter.HighResolution)
    dlg = QPrintPreviewDialog(printer, parent or QApplication.activeWindow())
    dlg.setWindowTitle(r.report.get("name") or name)
    dlg.resize(1000, 800)
    dlg.paintRequested.connect(r.render)
    bar = dlg.findChild(QToolBar)
    if bar is not None:
        def export():
            path, _f = QFileDialog.getSaveFileName(dlg, "Export PDF", (r.report.get("name") or name) + ".pdf",
                                                   "PDF files (*.pdf)")
            if path:
                pdf = QPrinter(QPrinter.HighResolution)
                pdf.setOutputFormat(QPrinter.PdfFormat)
                pdf.setOutputFileName(path if path.lower().endswith(".pdf") else path + ".pdf")
                r.render(pdf)
        bar.addSeparator()
        bar.addAction("Export PDF", export)
    dlg.exec()
