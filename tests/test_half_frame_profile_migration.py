"""The shared Half Frame split profile moves onto each roll in Half Frame mode."""

import pytest

from negpy.infrastructure.storage.repository import StorageRepository
from negpy.services.assets import rolls
from negpy.services.assets.migrations.half_frame_profile import migrate_shared_half_frame_profile

SHARED = {"crop_rect": [0.0, 0.0, 1.0, 1.0], "split_x": 0.55, "gutter_thickness": 0.01, "split_axis": "x"}
OWN = {"crop_rect": [0.0, 0.0, 1.0, 1.0], "split_x": 0.4, "gutter_thickness": 0.0, "split_axis": "y"}


@pytest.fixture
def repo(tmp_path):
    r = StorageRepository(str(tmp_path / "edits.db"), str(tmp_path / "settings.db"))
    r.initialize()
    return r


def test_each_half_frame_roll_without_a_profile_takes_the_shared_one(repo):
    on, off, own = (rolls.recognize_folder(repo, f"/scans/{name}") for name in ("on", "off", "own"))
    rolls.set_roll_half_frame_profile(repo, own, OWN)
    repo.save_global_setting("half_frame_mode_by_roll", {on: True, off: False, own: True, "deleted": True})
    repo.save_global_setting("half_frame_profile", SHARED)

    migrate_shared_half_frame_profile(repo)

    assert rolls.roll_half_frame_profile(repo, on) == SHARED
    assert rolls.roll_half_frame_profile(repo, off) is None
    assert rolls.roll_half_frame_profile(repo, own) == OWN
    assert repo.get_global_setting("half_frame_profile") is None


def test_without_a_shared_profile_nothing_changes(repo):
    roll = rolls.recognize_folder(repo, "/scans/on")
    repo.save_global_setting("half_frame_mode_by_roll", {roll: True})

    migrate_shared_half_frame_profile(repo)

    assert rolls.roll_half_frame_profile(repo, roll) is None
