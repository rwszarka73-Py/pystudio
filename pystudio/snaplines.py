"""Visual Studio-style snaplines for the form designer.

While you drag or resize a control, its edges snap to the edges and centers of the other controls in
the same container (blue lines), to their text baseline (magenta line), to the standard 6-pixel spacing
between controls, and to a 12-pixel margin from the form's edges. Hold Alt to place freely."""
from PySide6.QtCore import QLine, QRect
from PySide6.QtGui import QFontMetrics

from . import model as M

TOL = 6            # how close (pixels) before an edge snaps
SPACING = 6        # VS: Margin 3 + 3 between controls
FORM_MARGIN = 12   # VS: distance from the form's edges
BLUE, MAGENTA = "#3399ff", "#e040e0"
TEXT_TOOLS = {"Label", "TextBox", "Button", "CheckBox", "RadioButton", "ComboBox", "LinkLabel",
              "MaskedTextBox", "NumericUpDown", "DateTimePicker"}


def baseline(d, c, r=None):
    """y of the text baseline in surface coordinates (None for controls without one-line text)."""
    if M.tool_of(c) not in TEXT_TOOLS:
        return None
    r = r or d.sgeo(c)
    fm = QFontMetrics(c.font())
    if M.tool_of(c) == "Label":
        return r.top() + fm.ascent() + 1 if r.height() > fm.height() + 8 else r.top() + (r.height() + fm.ascent() - fm.descent()) // 2
    return r.top() + (r.height() + fm.ascent() - fm.descent()) // 2


def _others(d, prim, exclude):
    parent = prim.parentWidget()
    return [c for c in d.controls if c.parentWidget() is parent and c not in exclude and c.isVisible()
            and M.tool_of(c) in M.TOOLS]


def _bounds(d, prim):
    """Rect of the container the control sits in, in surface coordinates."""
    parent = prim.parentWidget()
    if parent is d.surface:
        return QRect(0, 0, d.surface.width(), d.surface.height())
    return d.sgeo(parent) if parent in d.controls else QRect(parent.mapTo(d.surface, parent.rect().topLeft()),
                                                                  parent.size())


def _best(features, targets):
    """features: {name: value}; targets: {name: [(value, kind, other)]} -> (delta, ) of the nearest."""
    best = None
    for name, v in features.items():
        for t, kind, other in targets.get(name, []):
            delta = t - v
            if abs(delta) <= TOL and (best is None or abs(delta) < abs(best)):
                best = delta
    return best


def _x_targets(d, prim, others, bounds, sides):
    t = {s: [] for s in sides}
    for o in others:
        r = d.sgeo(o)
        if "l" in t:
            t["l"] += [(r.left(), "align", o), (r.right() + 1 + SPACING, "space", o)]
        if "r" in t:
            t["r"] += [(r.right(), "align", o), (r.left() - 1 - SPACING, "space", o)]
        if "cx" in t:
            t["cx"].append((r.center().x(), "align", o))
    if prim.parentWidget() is d.surface:
        if "l" in t:
            t["l"].append((bounds.left() + FORM_MARGIN, "margin", None))
        if "r" in t:
            t["r"].append((bounds.right() - FORM_MARGIN, "margin", None))
    return t


def _y_targets(d, prim, others, bounds, sides, my_base_off):
    t = {s: [] for s in sides}
    for o in others:
        r = d.sgeo(o)
        if "t" in t:
            t["t"] += [(r.top(), "align", o), (r.bottom() + 1 + SPACING, "space", o)]
        if "b" in t:
            t["b"] += [(r.bottom(), "align", o), (r.top() - 1 - SPACING, "space", o)]
        if "cy" in t:
            t["cy"].append((r.center().y(), "align", o))
        if "base" in t and my_base_off is not None:
            b = baseline(d, o)
            if b is not None:
                t["base"].append((b, "base", o))
    if prim.parentWidget() is d.surface:
        if "t" in t:
            t["t"].append((bounds.top() + FORM_MARGIN, "margin", None))
        if "b" in t:
            t["b"].append((bounds.bottom() - FORM_MARGIN, "margin", None))
    return t


def _lines(d, r, others, bounds, prim, base_off):
    """The snaplines to draw for rect r: every exact match."""
    out = []
    feats_x = {"l": r.left(), "r": r.right(), "cx": r.center().x()}
    feats_y = {"t": r.top(), "b": r.bottom(), "cy": r.center().y()}
    my_base = r.top() + base_off if base_off is not None else None
    for o in others:
        q = d.sgeo(o)
        top, bot = min(r.top(), q.top()), max(r.bottom(), q.bottom())
        left, right = min(r.left(), q.left()), max(r.right(), q.right())
        for v in {feats_x["l"], feats_x["r"]} & {q.left(), q.right()}:
            out.append((QLine(v, top, v, bot), BLUE))
        if feats_x["cx"] == q.center().x() and r.width() != q.width():
            out.append((QLine(feats_x["cx"], top, feats_x["cx"], bot), BLUE))
        for v in {feats_y["t"], feats_y["b"]} & {q.top(), q.bottom()}:
            out.append((QLine(left, v, right, v), BLUE))
        if my_base is not None and baseline(d, o) == my_base:
            out.append((QLine(left, my_base, right, my_base), MAGENTA))
        # spacing: a short line in the gap
        ov_y = (max(r.top(), q.top()) + min(r.bottom(), q.bottom())) // 2
        ov_x = (max(r.left(), q.left()) + min(r.right(), q.right())) // 2
        if r.left() == q.right() + 1 + SPACING and r.bottom() >= q.top() and r.top() <= q.bottom():
            out.append((QLine(q.right() + 1, ov_y, r.left() - 1, ov_y), BLUE))
        if r.right() == q.left() - 1 - SPACING and r.bottom() >= q.top() and r.top() <= q.bottom():
            out.append((QLine(r.right() + 1, ov_y, q.left() - 1, ov_y), BLUE))
        if r.top() == q.bottom() + 1 + SPACING and r.right() >= q.left() and r.left() <= q.right():
            out.append((QLine(ov_x, q.bottom() + 1, ov_x, r.top() - 1), BLUE))
        if r.bottom() == q.top() - 1 - SPACING and r.right() >= q.left() and r.left() <= q.right():
            out.append((QLine(ov_x, r.bottom() + 1, ov_x, q.top() - 1), BLUE))
    if prim.parentWidget() is d.surface:
        cy, cx = r.center().y(), r.center().x()
        if r.left() == bounds.left() + FORM_MARGIN:
            out.append((QLine(bounds.left(), cy, r.left() - 1, cy), BLUE))
        if r.right() == bounds.right() - FORM_MARGIN:
            out.append((QLine(r.right() + 1, cy, bounds.right(), cy), BLUE))
        if r.top() == bounds.top() + FORM_MARGIN:
            out.append((QLine(cx, bounds.top(), cx, r.top() - 1), BLUE))
        if r.bottom() == bounds.bottom() - FORM_MARGIN:
            out.append((QLine(cx, r.bottom() + 1, cx, bounds.bottom()), BLUE))
    return out


def snap_move(d, prim, raw, exclude):
    """raw: where the primary control would be (QRect, surface coords, unsnapped).
    Returns (QRect or None per axis snapped..., lines): (dx, dy, lines) - dx/dy are None when that axis
    didn't snap (the caller falls back to the grid)."""
    others = _others(d, prim, exclude)
    bounds = _bounds(d, prim)
    b = baseline(d, prim, raw)
    base_off = b - raw.top() if b is not None else None
    xt = _x_targets(d, prim, others, bounds, ("l", "r", "cx"))
    yt = _y_targets(d, prim, others, bounds, ("t", "b", "cy", "base"), base_off)
    fx = {"l": raw.left(), "r": raw.right(), "cx": raw.center().x()}
    fy = {"t": raw.top(), "b": raw.bottom(), "cy": raw.center().y()}
    if base_off is not None:
        fy["base"] = raw.top() + base_off
    dx, dy = _best(fx, xt), _best(fy, yt)
    r = raw.translated(dx or 0, dy or 0)
    return dx, dy, _lines(d, r, others, bounds, prim, base_off)


def snap_resize(d, prim, raw, handle):
    """Snap the edges being dragged. Returns (QRect, lines, snapped_sides)."""
    others = _others(d, prim, [prim])
    bounds = _bounds(d, prim)
    r = QRect(raw)
    snapped = set()
    xs = [s for s in ("l", "r") if s in handle]
    ys = [s for s in ("t", "b") if s in handle]
    if xs:
        xt = _x_targets(d, prim, others, bounds, tuple(xs))
        for s in xs:
            delta = _best({s: r.left() if s == "l" else r.right()}, xt)
            if delta is not None:
                (r.setLeft if s == "l" else r.setRight)((r.left() if s == "l" else r.right()) + delta)
                snapped.add(s)
    if ys:
        yt = _y_targets(d, prim, others, bounds, tuple(ys), None)
        for s in ys:
            delta = _best({s: r.top() if s == "t" else r.bottom()}, yt)
            if delta is not None:
                (r.setTop if s == "t" else r.setBottom)((r.top() if s == "t" else r.bottom()) + delta)
                snapped.add(s)
    b = baseline(d, prim, r)
    return r, _lines(d, r, others, bounds, prim, b - r.top() if b is not None else None), snapped


def lines_for(d, prim, r, exclude):
    """Snaplines to draw for the primary control at rect r."""
    others = _others(d, prim, exclude)
    b = baseline(d, prim, r)
    return _lines(d, r, others, _bounds(d, prim), prim, b - r.top() if b is not None else None)
