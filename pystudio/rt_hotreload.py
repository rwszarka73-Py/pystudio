# ------------------------------------------------------------------ Hot Reload (like VS's XAML Hot Reload)
# Only active when PyStudio runs the program (PYSTUDIO_HOT_RELOAD=1). When a form is saved in the designer,
# PyStudio regenerates ui_<form>.py; the running program sees the new file and changes the open forms to
# match: moved/resized controls, new text, colors, fonts, new controls, removed controls. Only what you
# changed in the designer is applied, so values your code set while running (text typed, items added)
# stay as they are. New controls have no event code until you restart.
import importlib as _hr_importlib
import weakref as _hr_weakref

_hr_state = {"watcher": None, "forms": {}, "timer": None, "pending": set()}


def _hr_enabled():
    return os.environ.get("PYSTUDIO_HOT_RELOAD") == "1"


def _hr_ui_module(window):
    ui = getattr(window, "ui", None)
    if ui is None:
        return None, None
    mod = sys.modules.get(type(ui).__module__)
    path = getattr(mod, "__file__", None)
    return (mod, os.path.abspath(path)) if path else (None, None)


def _hr_register(window):
    if not _hr_enabled():
        return
    try:
        mod, path = _hr_ui_module(window)
        if mod is None or not os.path.exists(path):
            return
        from PySide6.QtCore import QFileSystemWatcher
        st = _hr_state
        if st["watcher"] is None:
            st["watcher"] = QFileSystemWatcher(QApplication.instance())
            st["watcher"].fileChanged.connect(_hr_changed)
            st["timer"] = QTimer(QApplication.instance())
            st["timer"].setSingleShot(True)
            st["timer"].setInterval(250)
            st["timer"].timeout.connect(_hr_apply_pending)
        forms = st["forms"].setdefault(path, [])
        forms.append((_hr_weakref.ref(window), _hr_design(window, type(window.ui))))
        if path not in st["watcher"].files():
            st["watcher"].addPath(path)
    except Exception as e:  # noqa: BLE001 - Hot Reload must never break the program
        print(f"[Hot Reload] not available for {type(window).__name__}: {e}")


def _hr_design(window, ui_class):
    """A hidden copy of the form as designed (to compare the next design against)."""
    host = type(window).__mro__[1]
    try:
        tmp = QMainWindow() if issubclass(host, QMainWindow) else QWidget()
    except NameError:
        tmp = QWidget()
    ui = ui_class()
    ui.setupUi(tmp)
    tmp.hide()
    return tmp, ui


def _hr_changed(path):
    st = _hr_state
    st["pending"].add(path)
    st["timer"].start()


def _hr_apply_pending():
    st = _hr_state
    paths, st["pending"] = st["pending"], set()
    for path in paths:
        if os.path.exists(path) and path not in st["watcher"].files():
            st["watcher"].addPath(path)          # editors replace the file: watch the new one
        entries = st["forms"].get(path, [])
        live = [(ref, old) for ref, old in entries if ref() is not None]
        if not live:
            continue
        try:
            mod = None
            for name, m in list(sys.modules.items()):
                if getattr(m, "__file__", None) and os.path.abspath(m.__file__) == path:
                    mod = _hr_importlib.reload(m)
                    break
            if mod is None:
                continue
            new_entries = []
            for ref, (old_tmp, old_ui) in live:
                window = ref()
                ui_class = getattr(mod, type(window.ui).__name__, None)
                if ui_class is None:
                    continue
                new_tmp, new_ui = _hr_design(window, ui_class)
                donor_tmp, donor_ui = _hr_design(window, ui_class)
                changes = _hr_sync(window, old_tmp, old_ui, new_tmp, new_ui, donor_ui)
                donor_tmp.deleteLater()
                old_tmp.deleteLater()
                new_entries.append((ref, (new_tmp, new_ui)))
                print(f"[Hot Reload] {type(window).__name__} updated ({changes} change"
                      f"{'' if changes == 1 else 's'})")
            st["forms"][path] = new_entries
        except Exception as e:  # noqa: BLE001
            print(f"[Hot Reload] couldn't apply the change: {e}  (restart the program to see it)")


_HR_SKIP = {"objectName", "parent", "pos", "x", "y", "frameGeometry", "normalGeometry", "rect", "childrenRect",
            "childrenRegion", "size", "width", "height", "visible", "isActiveWindow", "focus", "underMouse",
            "minimized", "maximized", "fullScreen", "modal", "sizeHint", "minimumSizeHint", "frameSize",
            "windowModality", "windowFilePath", "inputMethodHints", "cursor", "palette", "locale",
            "styleSheet", "font"}


def _hr_props(w):
    mo = w.metaObject()
    out = {}
    for i in range(mo.propertyCount()):
        p = mo.property(i)
        name = p.name()
        if name in _HR_SKIP or not p.isWritable() or not p.isDesignable():
            continue
        try:
            out[name] = p.read(w)
        except Exception:  # noqa: BLE001
            pass
    out["styleSheet"] = w.styleSheet()
    out["font"] = w.font().toString()
    out["_hidden"] = w.isHidden()
    for n in w.dynamicPropertyNames():
        key = bytes(n).decode()
        out["dyn:" + key] = w.property(key)
    return out


def _hr_same(a, b):
    """Equal for Hot Reload purposes. Icons and pictures compare by their image, not by object."""
    from PySide6.QtGui import QIcon, QPixmap
    if isinstance(a, (QIcon, QPixmap)) or isinstance(b, (QIcon, QPixmap)):
        def key(v):
            if v is None or not isinstance(v, (QIcon, QPixmap)) or v.isNull():
                return None
            img = (v.pixmap(32) if isinstance(v, QIcon) else v).toImage()
            return (img.width(), img.height(), bytes(img.constBits()) if img.constBits() is not None else b"")
        return key(a) == key(b)
    try:
        return a == b
    except Exception:  # noqa: BLE001
        return False


def _hr_items(w):
    """Items of list-like controls (ComboBox/ListWidget) as designed."""
    from PySide6.QtWidgets import QComboBox, QListWidget
    if isinstance(w, QComboBox):
        return [w.itemText(i) for i in range(w.count())]
    if isinstance(w, QListWidget):
        return [w.item(i).text() for i in range(w.count())]
    return None


def _hr_set_items(w, items):
    from PySide6.QtWidgets import QComboBox, QListWidget
    w.clear()
    if isinstance(w, QComboBox):
        w.addItems(items)
    elif isinstance(w, QListWidget):
        w.addItems(items)


def _hr_widgets(ui):
    return {k: v for k, v in vars(ui).items() if isinstance(v, QWidget)}


def _hr_sync(window, old_tmp, old_ui, new_tmp, new_ui, donor_ui):
    from PySide6.QtGui import QFont as _QFont
    old_w, new_w, donor_w = _hr_widgets(old_ui), _hr_widgets(new_ui), _hr_widgets(donor_ui)
    live_ui = window.ui
    changes = 0
    geometry_changed = set()

    # the form itself
    before, after = _hr_props(old_tmp), _hr_props(new_tmp)
    for k, v in after.items():
        if not _hr_same(before.get(k), v) and k in ("windowTitle", "toolTip", "styleSheet", "enabled"):
            if k == "styleSheet":
                window.setStyleSheet(v)
            else:
                window.setProperty(k, v)
            changes += 1
    if old_tmp.size() != new_tmp.size():
        window.resize(window.width() + new_tmp.width() - old_tmp.width(),
                      window.height() + new_tmp.height() - old_tmp.height())
        changes += 1
    # changed controls
    for name, nw in new_w.items():
        ow, lw = old_w.get(name), getattr(live_ui, name, None)
        if ow is None or lw is None or type(ow) is not type(nw):
            continue
        b, a = _hr_props(ow), _hr_props(nw)
        for k, v in a.items():
            if _hr_same(b.get(k), v):
                continue
            if os.environ.get("PYSTUDIO_HR_DEBUG"):
                print(f"[Hot Reload] {name}.{k}: {b.get(k)!r} -> {v!r}")
            changes += 1
            if k == "_hidden":
                lw.setHidden(v)
            elif k == "font":
                f = _QFont()
                f.fromString(v)
                lw.setFont(f)
            elif k == "styleSheet":
                lw.setStyleSheet(v)
            elif k.startswith("dyn:"):
                lw.setProperty(k[4:], v)
            else:
                if k == "geometry":
                    geometry_changed.add(lw)
                lw.setProperty(k, v)
        if ow.geometry() != nw.geometry():
            geometry_changed.add(lw)
            if "geometry" not in a:
                lw.setGeometry(nw.geometry())
        oi, ni = _hr_items(ow), _hr_items(nw)
        if ni is not None and oi != ni:
            _hr_set_items(lw, ni)
            changes += 1
    # new controls: move them from a fresh copy into the running form
    for name, dw in donor_w.items():
        if name in old_w or getattr(live_ui, name, None) is not None:
            continue
        parent = dw.parentWidget()
        if parent is not None and isinstance(getattr(donor_ui, name, None), QWidget):
            pname = parent.objectName()
            live_parent = getattr(live_ui, pname, None) if pname else None
            if live_parent is None and parent is getattr(donor_ui, "centralwidget", None):
                live_parent = window.centralWidget()
            if live_parent is None:
                continue
            g = dw.geometry()
            dw.setParent(live_parent)
            dw.setGeometry(g)
            dw.show()
            setattr(live_ui, name, dw)
            for cname, cw in donor_w.items():          # its child controls come along
                if cw is not dw and dw.isAncestorOf(cw) and getattr(live_ui, cname, None) is None:
                    setattr(live_ui, cname, cw)
            changes += 1
    # removed controls: hide them (your code may still use them)
    for name, ow in old_w.items():
        if name not in new_w:
            lw = getattr(live_ui, name, None)
            if lw is not None:
                lw.hide()
                changes += 1
    if geometry_changed:
        _hr_rebase(window, new_ui, new_w)
    return changes


def _hr_rebase(window, design_ui, design_w):
    """Anchors keep distances measured on the designed form: measure again from the new design."""
    by_live = {}
    for name, dw in design_w.items():
        lw = getattr(window.ui, name, None)
        if lw is not None:
            by_live[lw] = dw
    central = getattr(design_ui, "centralwidget", None)
    for obj in window.findChildren(QObject):
        if isinstance(obj, _Anchors):
            dc = central if obj.c is window.centralWidget() else by_live.get(obj.c)
            if dc is None:
                continue
            obj.dw, obj.dh = _size(dc)
            items = []
            for ch, sides, x, y, w, h in obj.items:
                d = by_live.get(ch)
                if d is not None:
                    g = d.geometry()
                    x, y, w, h = g.x(), g.y(), g.width(), g.height()
                items.append((ch, sides, x, y, w, h))
            obj.items = items
            obj.apply()
