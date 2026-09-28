"""A control's default is one value: the one a card's Reset restores.

A slider keeps the value it was built with as its double-click default, so the config the
panels are built against has to be the config a fresh frame and every reset path land on
(DEFAULT_WORKSPACE_CONFIG).
"""

from dataclasses import fields
from unittest.mock import MagicMock

import pytest

from negpy.desktop.session import AppState
from negpy.desktop.view.sidebar.controls_panel import ControlsPanel
from negpy.domain.models import WorkspaceConfig
from negpy.features.exposure.models import EXPOSURE_CONSTANTS, ExposureConfig
from negpy.features.process.models import ProcessConfig
from negpy.kernel.system.config import DEFAULT_WORKSPACE_CONFIG


def test_a_fresh_session_starts_on_the_shipped_config():
    assert AppState().config == DEFAULT_WORKSPACE_CONFIG


def test_the_exposure_and_process_dataclasses_carry_the_shipped_defaults():
    """ExposureConfig()/ProcessConfig() are what from_flat_dict falls back to for a key a
    saved edit is missing, so a field's own default cannot disagree with the shipped one.
    Geometry is not held to this: the shipped autocrop ratio and offset are a fresh frame's,
    not what an internal GeometryConfig() means."""
    for section, cls in (("exposure", ExposureConfig), ("process", ProcessConfig)):
        shipped = getattr(DEFAULT_WORKSPACE_CONFIG, section)
        bare = getattr(WorkspaceConfig(), section)
        for f in fields(cls):
            assert getattr(bare, f.name) == getattr(shipped, f.name), f"{section}.{f.name}"


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
