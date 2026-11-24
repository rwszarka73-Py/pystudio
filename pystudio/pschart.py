# ------------------------------------------------------------------ Chart (VB: Chart control)
# Used by the PyStudio designer and appended to pystudio_runtime.py. QtCharts is imported only
# when a chart is created, so apps without charts don't load (or bundle) it.
CHART_TYPES = ["Column", "Bar", "Line", "Spline", "Area", "Point", "Pie", "Doughnut"]


def make_chart_class():
    from PySide6.QtCharts import (QAreaSeries, QBarCategoryAxis, QBarSeries, QBarSet, QChart, QChartView,
                                  QHorizontalBarSeries, QLineSeries, QPieSeries, QScatterSeries, QSplineSeries,
                                  QValueAxis)
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QPainter

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    class PsChart(QChartView):
        """A chart (VB: Chart control).

            self.ui.chart1.add_point("Mon", 12)                 # Series1
            self.ui.chart1.add_point("Mon", 7, series="Returns")
            self.ui.chart1.chart_type = "Line"                   # Column, Bar, Line, Spline, Area, Point, Pie, Doughnut
            self.ui.chart1.bind(my.queries.SalesByCustomer(), x="Name", y=["TotalQty"])

        Set DataSource in the designer to fill it from a saved query when the form opens."""
        _ps_is_chart = True

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setRenderHint(QPainter.Antialiasing)
            self._data = {}          # series name -> [(x, y)]
            self._keep = []          # series QtCharts doesn't own (Area's line)
            self._building = False
            self._rebuild()

        # -------------------------------------------------- properties (designer)
        @property
        def chart_type(self):
            t = self.property("chartType") or "Column"
            return t if t in CHART_TYPES else "Column"

        @chart_type.setter
        def chart_type(self, value):
            self.setProperty("chartType", value)

        @property
        def title(self):
            return self.property("chartTitle") or ""

        @title.setter
        def title(self, value):
            self.setProperty("chartTitle", value)

        @property
        def legend_visible(self):
            v = self.property("legendVisible")
            return True if v is None else bool(v)

        @legend_visible.setter
        def legend_visible(self, value):
            self.setProperty("legendVisible", bool(value))

        def event(self, e):
            if e.type() == QEvent.DynamicPropertyChange and not self._building:
                self._rebuild()
            return super().event(e)

        # -------------------------------------------------- data
        @property
        def series_names(self):
            return list(self._data)

        def add_point(self, x, y, series="Series1"):
            self._data.setdefault(series, []).append((x, y))
            self._rebuild()

        def set_points(self, points, series="Series1"):
            """Replace a series: points = [(x, y), ...]"""
            self._data[series] = list(points)
            self._rebuild()

        def clear(self, series=None):
            if series is None:
                self._data.clear()
            else:
                self._data.pop(series, None)
            self._rebuild()

        def bind(self, table, x=None, y=None):
            """Show a DataTable (query result): x = label column, y = value column(s).
            Defaults: XMember / YMembers properties, else the first column and every numeric column."""
            cols = list(getattr(table, "columns", []) or (list(table[0].keys()) if table and isinstance(table[0], dict) else []))
            x = x or self.property("xMember") or (cols[0] if cols else None)
            if y is None:
                y = [s.strip() for s in str(self.property("yMembers") or "").split(",") if s.strip()]
            if isinstance(y, str):
                y = [y]
            low = {str(c).lower(): i for i, c in enumerate(cols)}

            def get(row, name):
                if isinstance(row, dict):
                    return row.get(name)
                i = low.get(str(name).lower())
                return tuple.__getitem__(row, i) if i is not None else None
            if not y:
                y = [c for c in cols if str(c).lower() != str(x).lower() and table
                     and any(num(get(r, c)) is not None for r in table)]
            self._data = {str(name): [(get(r, x), get(r, name)) for r in table] for name in y}
            self._rebuild()

        # -------------------------------------------------- drawing
        def _rebuild(self):
            self._building = True
            try:
                self._build()
            finally:
                self._building = False

        def _build(self):
            chart = QChart()
            chart.setTitle(self.title)
            chart.setBackgroundRoundness(0)
            t = self.chart_type
            data = {k: [(x, num(y)) for x, y in v if num(y) is not None] for k, v in self._data.items()}
            cats = []
            for pts in data.values():
                for x, _y in pts:
                    if str(x) not in cats:
                        cats.append(str(x))
            numeric_x = bool(cats) and all(num(x) is not None for pts in data.values() for x, _ in pts)
            self._keep = []
            if t in ("Pie", "Doughnut"):
                s = QPieSeries()
                for x, y in next(iter(data.values()), []):
                    if y > 0:
                        s.append(str(x), y)
                if t == "Doughnut":
                    s.setHoleSize(0.4)
                chart.addSeries(s)
                chart.legend().setVisible(self.legend_visible)
            elif t in ("Column", "Bar"):
                bs = QBarSeries() if t == "Column" else QHorizontalBarSeries()
                for name, pts in data.items():
                    vals = {str(x): y for x, y in pts}
                    bset = QBarSet(name)
                    for c in cats:
                        bset.append(vals.get(c, 0.0))
                    bs.append(bset)
                chart.addSeries(bs)
                ca, va = QBarCategoryAxis(), QValueAxis()
                ca.append(cats)
                ys = [y for pts in data.values() for _x, y in pts]
                va.setRange(min([0.0] + ys), max([1.0] + ys))
                va.applyNiceNumbers()
                chart.addAxis(ca, Qt.AlignBottom if t == "Column" else Qt.AlignLeft)
                chart.addAxis(va, Qt.AlignLeft if t == "Column" else Qt.AlignBottom)
                bs.attachAxis(ca)
                bs.attachAxis(va)
                chart.legend().setVisible(self.legend_visible and len(data) > 1)
            else:
                xa = QValueAxis() if numeric_x else QBarCategoryAxis()
                if not numeric_x:
                    xa.append(cats)
                ya = QValueAxis()
                chart.addAxis(xa, Qt.AlignBottom)
                chart.addAxis(ya, Qt.AlignLeft)
                ys, xs = [], []
                for name, pts in data.items():
                    line = {"Spline": QSplineSeries, "Point": QScatterSeries}.get(t, QLineSeries)()
                    line.setName(name)
                    for x, y in pts:
                        xv = num(x) if numeric_x else float(cats.index(str(x)))
                        line.append(xv, y)
                        xs.append(xv)
                        ys.append(y)
                    if t == "Area":
                        self._keep.append(line)
                        s = QAreaSeries(line)
                        s.setName(name)
                    else:
                        s = line
                    chart.addSeries(s)
                    s.attachAxis(xa)
                    s.attachAxis(ya)
                ya.setRange(min([0.0] + ys), max([1.0] + ys))
                ya.applyNiceNumbers()
                if numeric_x and xs:
                    xa.setRange(min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1)
                chart.legend().setVisible(self.legend_visible and len(data) > 1)
            old = self.chart()
            self.setChart(chart)
            if old is not None:
                old.deleteLater()

    return PsChart


try:
    _LAZY["PsChart"] = make_chart_class        # inside pystudio_runtime.py
except NameError:
    pass
