#!/usr/bin/env python3
"""PyStudio - a Visual Studio-style Python IDE for Linux.

Entry point. Equivalent of Program.vb / Sub Main in a WinForms app.
"""
import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from pystudio import theme


def main():
    # Harmless GNOME/Wayland text-input warning; keep the console clean
    from PySide6.QtCore import QLoggingCategory
    QLoggingCategory.setFilterRules("qt.qpa.wayland.textinput=false")
    # Set before QApplication exists: Wayland matches windows to pystudio.desktop by the
    # desktop file name and X11 by WM_CLASS (from the application name) - both are read
    # when the platform plugin starts, so setting them afterwards can be too late.
    QApplication.setApplicationName("PyStudio")
    QApplication.setOrganizationName("PyStudio")
    QApplication.setDesktopFileName("pystudio")
    app = QApplication(sys.argv)
    from pystudio import errorlog
    errorlog.install(app)            # Help > View Log: errors, Qt warnings, crashes
    from pystudio import settingsio
    try:
        settings_msg = settingsio.apply_pending()      # Tools > Import and Export Settings, then restart
    except Exception:  # noqa: BLE001
        settings_msg = ""
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "pystudio.svg")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    theme.select(theme.saved_theme())     # before any window is built (Tools > Options > Theme)
    theme.apply_theme(app)
    from pystudio.mainwindow import MainWindow
    from pystudio.options import startup_mode

    window = MainWindow()
    if settings_msg:
        window.statusBar().showMessage(settings_msg, 10000)

    if sys.argv[1:]:
        # "pystudio somefile.py" or "pystudio /some/folder" from a terminal
        window.show()
        for arg in sys.argv[1:]:
            window.open_path(arg)
    else:
        mode = startup_mode()   # Tools > Options > At startup
        last = window.settings.value("lastProject")
        if mode == "last" and last and os.path.isdir(last):
            window.show()
            window.open_folder(last)
            window._open_startup_form()
        elif mode == "empty":
            window.show()
        else:
            # VS-style Start page inside the IDE window: one top-level window, so GNOME's dock
            # icon stays and no separate (modal) window can get lost behind it.
            window.show()
            window.show_start_window()

    from PySide6.QtCore import QTimer
    from pystudio import updater
    QTimer.singleShot(4000, lambda: setattr(window, "_update_check", updater.auto_check(window)))
    crash = errorlog.previous_crash()
    if crash:
        QTimer.singleShot(800, lambda: window.report_previous_crash(crash))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
