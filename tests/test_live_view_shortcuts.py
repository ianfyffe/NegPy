"""The Live View window's Focus key and the tooltips that name its keys."""

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from negpy.desktop.view.shortcut_registry import tooltip_with_shortcut
from negpy.desktop.view.sidebar.live_view_window import LiveViewWindow
from negpy.desktop.view.styles.templates import wrap_tooltip


def test_the_focus_key_drives_autofocus_only_while_a_drive_is_available(qapp):
    window = LiveViewWindow()
    fired: list[str] = []
    window.focus_btn.clicked.connect(lambda: fired.append("focus"))
    QTest.keyClick(window, Qt.Key.Key_F)
    assert fired == []
    window.set_autofocus_available(True)
    QTest.keyClick(window, Qt.Key.Key_F)
    assert fired == ["focus"]


def test_tooltips_read_the_bound_key(qapp):
    window = LiveViewWindow()
    window.apply_shortcut_tooltips()
    assert window.scan_btn.toolTip() == wrap_tooltip(tooltip_with_shortcut(window.scan_btn.plain_tooltip, "live_view_scan"))
    assert window.retake_btn.toolTip() == wrap_tooltip(tooltip_with_shortcut(window.retake_btn.plain_tooltip, "live_view_retake"))


def test_focus_tooltip_names_its_key_only_while_a_drive_is_available(qapp):
    window = LiveViewWindow()
    bound = wrap_tooltip(tooltip_with_shortcut(window.focus_btn.plain_tooltip, "live_view_focus"))
    assert window.focus_btn.toolTip() != bound
    window.set_autofocus_available(True)
    assert window.focus_btn.toolTip() == bound
