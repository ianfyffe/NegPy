"""The Live View window binds its registry keys itself, ahead of a main-window shortcut on the same key."""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMainWindow, QWidget

from negpy.desktop.view.shortcut_registry import tooltip_with_shortcut
from negpy.desktop.view.sidebar.live_view_window import LiveViewWindow
from negpy.desktop.view.styles.templates import wrap_tooltip


@pytest.mark.parametrize("tool_window", [False, True])
def test_a_live_view_key_outranks_the_main_window_shortcut_it_shares(qapp, tool_window):
    main = QMainWindow()
    main.setCentralWidget(QWidget())
    main.show()
    hits: list[str] = []
    for key in ("S", "Q"):
        shortcut = QShortcut(QKeySequence(key), main)
        shortcut.activated.connect(lambda key=key: hits.append(f"main {key}"))
        shortcut.activatedAmbiguously.connect(lambda key=key: hits.append(f"ambiguous {key}"))
    window = LiveViewWindow(main.centralWidget())
    if tool_window:  # macOS: float_over_app makes it a tool window, which also matches the main window's shortcuts
        window.setWindowFlags(window.windowFlags() | Qt.WindowType.Tool)
    window.set_shortcuts({"S": lambda: hits.append("live view S")})
    window.show()
    window.activateWindow()
    window.scan_btn.setFocus()
    qapp.processEvents()

    QTest.keyClick(window.scan_btn, Qt.Key.Key_S)
    QTest.keyClick(window.scan_btn, Qt.Key.Key_Q)
    qapp.processEvents()

    assert hits[0] == "live view S"
    assert "ambiguous S" not in hits and "main S" not in hits
    assert hits[1:] == (["main Q"] if tool_window else [])
    window.close()
    main.close()


def test_tooltips_read_the_bound_key(qapp):
    window = LiveViewWindow()
    assert window.scan_btn.toolTip() == wrap_tooltip(tooltip_with_shortcut(window.scan_btn.plain_tooltip, "live_view_scan"))
    assert window.retake_btn.toolTip() == wrap_tooltip(tooltip_with_shortcut(window.retake_btn.plain_tooltip, "live_view_retake"))
