from dataclasses import replace
from unittest.mock import MagicMock, patch

from negpy.desktop.session import AppState
from negpy.desktop.view.sidebar import flatfield
from negpy.desktop.view.sidebar.flatfield import FlatFieldSidebar


def _sidebar(profile_id: str):
    controller = MagicMock()
    controller.state = AppState()
    cfg = controller.state.config
    controller.state.config = replace(cfg, flatfield=replace(cfg.flatfield, profile_id=profile_id, apply=bool(profile_id)))
    with patch("negpy.services.assets.flatfield.FlatFieldProfiles.list_profiles", return_value=[("rig-a", "Rig A")]):
        sidebar = FlatFieldSidebar(controller)
        sidebar.sync_ui()
    return sidebar


def test_a_profile_that_is_not_here_says_so_and_cannot_be_applied(qapp):
    sidebar = _sidebar("rig-b")
    assert not sidebar.hint.isHidden() and sidebar.hint.text() == flatfield._MISSING_HINT
    assert not sidebar.enable_btn.isEnabled()

    here = _sidebar("rig-a")
    assert here.hint.isHidden() and here.enable_btn.isEnabled()

    none = _sidebar("")
    assert not none.hint.isHidden() and none.hint.text() == flatfield._ADD_HINT
