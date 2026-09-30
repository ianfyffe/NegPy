"""A control's default is one value: the one a card's Reset restores.

A slider keeps the value it was built with as its double-click default, so the config the
panels are built against has to be the config a fresh frame and every reset path land on
(DEFAULT_WORKSPACE_CONFIG).
"""

from dataclasses import fields, is_dataclass
from unittest.mock import MagicMock

import pytest

from negpy.desktop.session import AppState
from negpy.desktop.view.sidebar.controls_panel import ControlsPanel
from negpy.domain.models import WorkspaceConfig
from negpy.features.exposure.models import EXPOSURE_CONSTANTS
from negpy.kernel.system.config import DEFAULT_WORKSPACE_CONFIG


def test_a_fresh_session_starts_on_the_shipped_config():
    assert AppState().config == DEFAULT_WORKSPACE_CONFIG


def test_the_dataclass_defaults_carry_the_shipped_values():
    """DEFAULT_WORKSPACE_CONFIG spells out what the app ships; a dataclass's own default is
    what from_flat_dict falls back to for a key a saved edit is missing. A field that
    disagrees ships one value and recovers as another, field by field so a failure names it.
    """
    bare, shipped = WorkspaceConfig(), DEFAULT_WORKSPACE_CONFIG
    for section in fields(bare):
        bare_section, shipped_section = getattr(bare, section.name), getattr(shipped, section.name)
        if not is_dataclass(bare_section):
            assert bare_section == shipped_section, section.name
            continue
        for f in fields(bare_section):
            assert getattr(bare_section, f.name) == getattr(shipped_section, f.name), f"{section.name}.{f.name}"


@pytest.fixture
def panel(qapp):
    controller = MagicMock()
    controller.state = AppState()
    return ControlsPanel(controller)


def test_sliders_double_click_to_the_value_their_card_resets_to(panel):
    """ISO-R Grade and Crosstalk Strength: the two the shipped config used to spell
    differently from their own field defaults."""
    assert panel.tone_sidebar.grade_slider._default == DEFAULT_WORKSPACE_CONFIG.exposure.grade
    assert panel.sensor_sidebar.crosstalk_strength_slider._default == DEFAULT_WORKSPACE_CONFIG.process.crosstalk_strength


def test_the_grade_slider_travels_exactly_the_curves_own_clamp(panel):
    """A grade past iso_r_min/iso_r_max is clipped by the kernel, so the slider must not
    offer one."""
    slider = panel.tone_sidebar.grade_slider
    assert (slider._min, slider._max) == (EXPOSURE_CONSTANTS["iso_r_min"], EXPOSURE_CONSTANTS["iso_r_max"])
