"""A small Excel (.xlsx) writer with no extra packages - query results to a workbook: one sheet per result, bold
header row frozen at the top with filter buttons, columns sized to fit, numbers as numbers and dates as dates."""
import datetime
import decimal
import re
import zipfile
from xml.sax.saxutils import escape

_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
EPOCH = datetime.datetime(1899, 12, 30)


def _col(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_name(name, used):
    name = re.sub(r"[\[\]:*?/\\]", "_", name or "Sheet").strip("'")[:31] or "Sheet"
    base, i = name, 2
    while name.lower() in used:
        suffix = f" ({i})"
        name = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(name.lower())
    return name


def _cell(ref, v):
    """(xml, style index, display width)"""
    if v is None:
        return "", 0, 0
    if isinstance(v, bool):
        return f'<c r="{ref}" t="b"><v>{int(v)}</v></c>', 0, 5
    if isinstance(v, (int, float, decimal.Decimal)) and not (isinstance(v, float) and (v != v or v in (float("inf"),
                                                                                                     float("-inf")))):
        text = str(v)
        return f'<c r="{ref}"><v>{text}</v></c>', 0, len(text)
    if isinstance(v, datetime.datetime):
        d = v.replace(tzinfo=None) - EPOCH
        return f'<c r="{ref}" s="3"><v>{d.days + d.seconds / 86400 + d.microseconds / 86400e6:.10f}</v></c>', 3, 19
    if isinstance(v, datetime.date):
        return f'<c r="{ref}" s="2"><v>{(datetime.datetime(v.year, v.month, v.day) - EPOCH).days}</v></c>', 2, 10
    if isinstance(v, (bytes, bytearray, memoryview)):
        v = "0x" + bytes(v).hex()
    text = _BAD_XML.sub("", str(v))[:32767]
    return (f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>', 0,
            max((len(x) for x in text.split("\n")), default=0))


def _sheet_xml(columns, rows):
    widths = [len(str(c)) for c in columns]
    out = []
    head = "".join(f'<c r="{_col(i)}1" t="inlineStr" s="1"><is><t xml:space="preserve">'
                   f'{escape(_BAD_XML.sub("", str(c)))}</t></is></c>' for i, c in enumerate(columns))
    out.append(f'<row r="1">{head}</row>')
    n = 1
    for r in rows:
        n += 1
        cells = []
        for i, v in enumerate(r):
            xml, _s, w = _cell(f"{_col(i)}{n}", v)
            if xml:
                cells.append(xml)
            if i < len(widths):
                widths[i] = max(widths[i], min(w, 60))
        out.append(f'<row r="{n}">{"".join(cells)}</row>')
    last = f"{_col(max(0, len(columns) - 1))}{max(1, n)}"
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{min(62, w + 3):.1f}" customWidth="1"/>'
                   for i, w in enumerate(widths))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
            'state="frozen"/></sheetView></sheetViews>'
            f'<sheetFormatPr defaultRowHeight="15"/>{"<cols>" + cols + "</cols>" if cols else ""}'
            f'<sheetData>{"".join(out)}</sheetData>'
            + (f'<autoFilter ref="A1:{last}"/>' if columns else "") + '</worksheet>')


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
          '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/></numFmts>'
          '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
          '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
          '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/>'
          '</fill><fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/></patternFill></fill></fills>'
          '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
          '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
          '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
          '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
          '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def write(path, sheets):
    """sheets: [(name, columns, rows)] -> an .xlsx workbook at path."""
    used = set()
    names = [_sheet_name(n, used) for n, _c, _r in sheets] or ["Sheet1"]
    if not sheets:
        sheets = [("Sheet1", [], [])]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
                   'officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
                   'officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/'
                             'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(sheets))) + '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                             for i, n in enumerate(names)) + '</sheets>'
                   + '</workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/'
                             f'2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                             for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/'
                     'officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", STYLES)
        for i, (_n, columns, rows) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet_xml(list(columns), rows))
    return path
