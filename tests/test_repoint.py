"""A folder that moves takes every path stored under it along: rolls, composites, the saved
session, the library lists, and the path columns and source paths of saved edits."""

import os
from dataclasses import replace

import numpy as np
import pytest

from negpy.features.rgbscan.models import RgbScanConfig
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.kernel.system.config import DEFAULT_WORKSPACE_CONFIG
from negpy.services.assets import rolls
from negpy.services.assets.composites import COMPOSITES_KEY, remember_composites, restore_maps
from negpy.services.assets.repoint import moved_path, repoint_folder


@pytest.fixture
def repo(tmp_path):
    r = StorageRepository(str(tmp_path / "edits.db"), str(tmp_path / "settings.db"))
    r.initialize()
    return r


OLD = os.path.join(os.sep, "nas", "photos", "roll_a")
NEW = os.path.join(os.sep, "nas", "photos", "roll_b")


def _in(folder: str, *names: str) -> str:
    return os.path.join(folder, *names)


def test_moved_path_rebases_only_the_folder_and_what_is_under_it():
    assert moved_path(OLD, OLD, NEW) == NEW
    assert moved_path(_in(OLD, "sub", "a.tif"), OLD, NEW) == _in(NEW, "sub", "a.tif")
    assert moved_path(OLD + "_2", OLD, NEW) == OLD + "_2"
    assert moved_path(_in(OLD + "x", "a.tif"), OLD, NEW) == _in(OLD + "x", "a.tif")
    assert moved_path("", OLD, NEW) == ""


def test_rolls_follow_the_folder(repo):
    folder_roll = rolls.recognize_folder(repo, OLD)
    rolls.add_extra_members(repo, folder_roll, [_in(OLD, "x.tif"), "/elsewhere/y.tif"])
    other = rolls.recognize_folder(repo, "/nas/photos/roll_c")
    picks = rolls.create_virtual_roll(repo, "Picks", [_in(OLD, "a.tif"), "/nas/photos/roll_c/c.tif"])

    repoint_folder(repo, OLD, NEW)

    assert rolls.roll_for_id(repo, folder_roll)["folder_path"] == NEW
    assert rolls.roll_for_id(repo, folder_roll)["extra_paths"] == [_in(NEW, "x.tif"), "/elsewhere/y.tif"]
    assert rolls.roll_for_id(repo, other)["folder_path"] == "/nas/photos/roll_c"
    assert rolls.roll_for_id(repo, picks)["member_paths"] == [_in(NEW, "a.tif"), "/nas/photos/roll_c/c.tif"]
    assert rolls.folder_roll_id_for_path(repo, NEW) == folder_roll


def test_composites_move_their_key_and_parts(repo):
    stitch = {
        "path": _in(OLD, "1.tif"),
        "hash": "s1",
        "stitch_paths": [_in(OLD, "2.tif")],
        "stitch_transforms": [[1.0, 0.0, 0.0]],
        "stitch_canvas": [10, 10],
        "stitch_sizes": [[5, 10]],
        "stitch_triplets": [[_in(OLD, "1g.tif"), _in(OLD, "1b.tif")]],
    }
    remember_composites(repo, [stitch])

    repoint_folder(repo, OLD, NEW)

    stitches, _ = restore_maps(repo)
    assert list(stitches) == [_in(NEW, "1.tif")]
    entry = stitches[_in(NEW, "1.tif")]
    assert entry["paths"] == [_in(NEW, "2.tif")]
    assert entry["triplets"] == [[_in(NEW, "1g.tif"), _in(NEW, "1b.tif")]]
    assert _in(OLD, "1.tif") not in repo.get_global_setting(COMPOSITES_KEY)


def test_the_saved_session_and_library_lists_follow(repo):
    repo.save_global_setting("session_files", [_in(OLD, "a.tif"), "/other/b.tif"])
    repo.save_global_setting("session_active_path", _in(OLD, "a.tif"))
    repo.save_global_setting("session_triplets", {_in(OLD, "r.tif"): [_in(OLD, "g.tif"), _in(OLD, "b.tif"), True]})
    repo.save_global_setting(rolls.DISMISSED_FOLDERS_KEY, [OLD])
    repo.save_global_setting(rolls.IMPORT_SOURCES_KEY, [OLD, "/other"])
    repo.save_global_setting("library_roots", [_in(OLD, "sub")])

    repoint_folder(repo, OLD, NEW)

    assert repo.get_global_setting("session_files") == [_in(NEW, "a.tif"), "/other/b.tif"]
    assert repo.get_global_setting("session_active_path") == _in(NEW, "a.tif")
    assert repo.get_global_setting("session_triplets") == {_in(NEW, "r.tif"): [_in(NEW, "g.tif"), _in(NEW, "b.tif"), True]}
    assert repo.get_global_setting(rolls.DISMISSED_FOLDERS_KEY) == [NEW]
    assert repo.get_global_setting(rolls.IMPORT_SOURCES_KEY) == [NEW, "/other"]
    assert repo.get_global_setting("library_roots") == [_in(NEW, "sub")]


def test_edit_mark_and_embedding_paths_follow_without_dating_the_edit(repo):
    cfg = DEFAULT_WORKSPACE_CONFIG
    repo.save_file_settings("h1", cfg, file_path=_in(OLD, "a.tif"), updated_at=5.0)
    repo.save_file_settings("h2", cfg, file_path="/other/roll_a/b.tif", updated_at=6.0)
    repo.save_file_mark("h1", "keeper", file_path=_in(OLD, "a.tif"))
    repo.save_embedding("h1", np.zeros(4, dtype=np.float32), "m1", file_path=_in(OLD, "a.tif"))

    repoint_folder(repo, OLD, NEW)

    assert repo.load_file_settings_by_path(_in(NEW, "a.tif"))[0] == "h1"
    assert repo.load_file_settings_by_path(_in(OLD, "a.tif")) is None
    assert repo.load_file_updated_at("h1") == 5.0
    assert repo.load_file_settings_by_path("/other/roll_a/b.tif")[0] == "h2"
    assert repo.load_file_marks_by_path() == {_in(NEW, "a.tif"): "keeper"}
    assert repo.load_all_embeddings("m1")["h1"][0] == _in(NEW, "a.tif")


def test_source_paths_inside_saved_configs_follow(repo):
    cfg = DEFAULT_WORKSPACE_CONFIG
    triplet = replace(cfg, rgbscan=RgbScanConfig(enabled=True, green_path=_in(OLD, "g.tif"), blue_path=_in(OLD, "b.tif")))
    stitched = replace(cfg, stitch=replace(cfg.stitch, stitch_enabled=True, stitch_paths=(_in(OLD, "2.tif"),)))
    repo.save_file_settings("h1", triplet, file_path=_in(OLD, "r.tif"), updated_at=5.0)
    repo.save_file_settings("s1", stitched, updated_at=7.0)
    repo.save_work_print("h1", "Print 1", triplet)
    repo.save_history_step("h1", 0, triplet)

    repoint_folder(repo, OLD, NEW)

    moved = repo.load_file_settings("h1").rgbscan
    assert (moved.green_path, moved.blue_path) == (_in(NEW, "g.tif"), _in(NEW, "b.tif"))
    assert repo.load_file_settings("s1").stitch.stitch_paths == (_in(NEW, "2.tif"),)
    assert repo.load_work_print("h1", "Print 1").rgbscan.green_path == _in(NEW, "g.tif")
    assert repo.load_history_step("h1", 0).rgbscan.blue_path == _in(NEW, "b.tif")
    assert (repo.load_file_updated_at("h1"), repo.load_file_updated_at("s1")) == (5.0, 7.0)


def test_repointing_again_or_onto_itself_changes_nothing(repo):
    rolls.recognize_folder(repo, OLD)
    repo.save_global_setting("session_files", [_in(OLD, "a.tif")])
    repoint_folder(repo, OLD, NEW)
    before = (rolls.saved_rolls(repo), repo.get_global_setting("session_files"))

    repoint_folder(repo, OLD, NEW)
    repoint_folder(repo, NEW, NEW)

    assert (rolls.saved_rolls(repo), repo.get_global_setting("session_files")) == before


def test_a_folder_renamed_here_keeps_its_stitch_and_virtual_roll_membership(repo, tmp_path):
    """The renaming computer itself: nothing stored by path is left behind."""
    folder = tmp_path / "roll_a"
    folder.mkdir()
    for name in ("1.tif", "2.tif"):
        (folder / name).write_bytes(b"x")
    roll_id = rolls.recognize_folder(repo, str(folder))
    picks = rolls.create_virtual_roll(repo, "Picks", [str(folder / "1.tif")])
    remember_composites(
        repo,
        [
            {
                "path": str(folder / "1.tif"),
                "hash": "s1",
                "stitch_paths": [str(folder / "2.tif")],
                "stitch_transforms": [[1.0, 0.0, 0.0]],
                "stitch_canvas": [10, 10],
                "stitch_sizes": [[5, 10]],
            }
        ],
    )

    new_path = rolls.rename_folder_roll_disk(repo, roll_id, "roll_b")
    repoint_folder(repo, str(folder), new_path)

    stitches, _ = restore_maps(repo)
    assert stitches[os.path.join(new_path, "1.tif")]["paths"] == [os.path.join(new_path, "2.tif")]
    assert rolls.roll_for_id(repo, picks)["member_paths"] == [os.path.join(new_path, "1.tif")]
    assert all(
        os.path.exists(p) for p in (*stitches[os.path.join(new_path, "1.tif")]["paths"], *rolls.roll_for_id(repo, picks)["member_paths"])
    )
