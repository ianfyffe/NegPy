"""Renaming a roll's folder on disk carries every path-keyed record with it."""

import os
from dataclasses import replace

import pytest

from negpy.domain.models import WorkspaceConfig
from negpy.features.rgbscan.models import RgbScanConfig
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.kernel.system.config import DEFAULT_WORKSPACE_CONFIG
from negpy.services.assets import composites, rolls
from negpy.services.assets.composites import remember_composites, restore_maps
from negpy.services.assets.rehome import moved_path, rehome_path_prefix
from negpy.services.assets.triplets import remember_triplets, saved_triplets


def test_every_path_keyed_store_follows_the_folder(tmp_path):
    repo = StorageRepository(str(tmp_path / "edits.db"), str(tmp_path / "settings.db"))
    repo.initialize()
    old, new = str(tmp_path / "roll1"), str(tmp_path / "roll1-portra")
    a, b = os.path.join(old, "a.tif"), os.path.join(old, "b.tif")
    elsewhere = str(tmp_path / "roll10" / "c.tif")
    repo.save_file_settings("ha", WorkspaceConfig(), file_path=a)
    repo.save_file_settings("hc", WorkspaceConfig(), file_path=elsewhere)
    repo.save_file_mark("ha", "keeper", file_path=a)
    vroll = rolls.create_virtual_roll(repo, "picks", [a, elsewhere])
    repo.save_global_setting(composites.COMPOSITES_KEY, {a: {"kind": "stitch", "paths": [a, b], "hash": "hs"}})

    rehome_path_prefix(repo, old, new)

    assert repo.load_file_settings_by_path(os.path.join(new, "a.tif")) is not None
    assert repo.load_file_settings_by_path(elsewhere) is not None  # a sibling sharing the prefix stays
    assert os.path.join(new, "a.tif") in repo.load_file_marks_by_path()
    assert rolls.roll_for_id(repo, vroll)["member_paths"] == [os.path.join(new, "a.tif"), elsewhere]
    saved = composites.saved_composites(repo)
    assert list(saved) == [os.path.join(new, "a.tif")]
    assert saved[os.path.join(new, "a.tif")]["paths"] == [os.path.join(new, "a.tif"), os.path.join(new, "b.tif")]


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
    rolls.add_extra_members(repo, folder_roll, [_in(OLD, "sub", "x.tif"), "/elsewhere/y.tif"])
    other = rolls.recognize_folder(repo, "/nas/photos/roll_c")
    picks = rolls.create_virtual_roll(repo, "Picks", [_in(OLD, "a.tif"), "/nas/photos/roll_c/c.tif"])

    rehome_path_prefix(repo, OLD, NEW)

    assert rolls.roll_for_id(repo, folder_roll)["folder_path"] == NEW
    assert rolls.roll_for_id(repo, folder_roll)["extra_paths"] == [_in(NEW, "sub", "x.tif"), "/elsewhere/y.tif"]
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

    rehome_path_prefix(repo, OLD, NEW)

    stitches, _ = restore_maps(repo)
    assert list(stitches) == [_in(NEW, "1.tif")]
    entry = stitches[_in(NEW, "1.tif")]
    assert entry["paths"] == [_in(NEW, "2.tif")]
    assert entry["triplets"] == [[_in(NEW, "1g.tif"), _in(NEW, "1b.tif")]]
    assert _in(OLD, "1.tif") not in repo.get_global_setting(composites.COMPOSITES_KEY)


def _triplet(red: str, green: str, blue: str, hashes=("hr", "hg", "hb"), align=False) -> dict:
    return {
        "name": "r (RGB)",
        "path": red,
        "hash": hashes[0],
        "green_path": green,
        "blue_path": blue,
        "green_hash": hashes[1],
        "blue_hash": hashes[2],
        "align": align,
    }


def test_a_triplet_moves_its_key_and_exposures_and_keeps_its_hashes(repo):
    remember_triplets(repo, [_triplet(_in(OLD, "r.tif"), _in(OLD, "g.tif"), _in(OLD, "sub", "b.tif"))])

    rehome_path_prefix(repo, OLD, NEW)

    assert saved_triplets(repo) == {_in(NEW, "r.tif"): [_in(NEW, "g.tif"), _in(NEW, "sub", "b.tif"), False, ["hr", "hg", "hb"]]}


def test_a_triplet_outside_the_folder_stays(repo):
    outside = _triplet("/nas/photos/roll_c/r.tif", "/nas/photos/roll_c/g.tif", "/nas/photos/roll_c/b.tif")
    remember_triplets(repo, [outside])
    before = saved_triplets(repo)

    rehome_path_prefix(repo, OLD, NEW)

    assert saved_triplets(repo) == before


def test_a_hand_made_triplet_reattaches_after_a_rename_without_a_raw_read(repo, tmp_path, monkeypatch):
    from negpy.desktop.workers.render import AssetDiscoveryTask, AssetDiscoveryWorker
    from negpy.features.rgbscan import logic
    from negpy.kernel.image.logic import calculate_file_hash

    folder = tmp_path / "roll_a"
    folder.mkdir()
    names = ("f1.raw", "f2.raw", "f3.raw")
    for name in names:
        (folder / name).write_bytes(name.encode() * 64)
    r, g, b = (str(folder / n) for n in names)
    remember_triplets(repo, [_triplet(r, g, b, hashes=tuple(calculate_file_hash(p) for p in (r, g, b)))])
    new_folder = tmp_path / "roll_b"
    folder.rename(new_folder)

    rehome_path_prefix(repo, str(folder), str(new_folder))

    probed: list = []
    monkeypatch.setattr(logic, "probe_frame", lambda path: probed.append(path))
    worker = AssetDiscoveryWorker()
    seen: list = []
    worker.finished.connect(seen.append)
    worker.process(
        AssetDiscoveryTask(paths=[str(new_folder)], supported_extensions=(".raw",), rgb_scan=True, restore_triplets=saved_triplets(repo))
    )
    [frame] = seen.pop()
    assert (frame["path"], frame["green_path"], frame["blue_path"]) == tuple(str(new_folder / n) for n in names)
    assert probed == []


def test_the_saved_session_and_library_lists_follow(repo):
    repo.save_global_setting("session_files", [_in(OLD, "a.tif"), "/other/b.tif"])
    repo.save_global_setting("session_active_path", _in(OLD, "a.tif"))
    repo.save_global_setting("session_triplets", {_in(OLD, "r.tif"): [_in(OLD, "g.tif"), _in(OLD, "b.tif"), True]})
    repo.save_global_setting(rolls.DISMISSED_FOLDERS_KEY, [OLD])
    repo.save_global_setting(rolls.IMPORT_SOURCES_KEY, [OLD, "/other"])
    repo.save_global_setting("library_roots", [_in(OLD, "sub")])

    rehome_path_prefix(repo, OLD, NEW)

    assert repo.get_global_setting("session_files") == [_in(NEW, "a.tif"), "/other/b.tif"]
    assert repo.get_global_setting("session_active_path") == _in(NEW, "a.tif")
    assert repo.get_global_setting("session_triplets") == {_in(NEW, "r.tif"): [_in(NEW, "g.tif"), _in(NEW, "b.tif"), True]}
    assert repo.get_global_setting(rolls.DISMISSED_FOLDERS_KEY) == [NEW]
    assert repo.get_global_setting(rolls.IMPORT_SOURCES_KEY) == [NEW, "/other"]
    assert repo.get_global_setting("library_roots") == [_in(NEW, "sub")]


def test_a_rehome_keeps_each_edits_saved_time(repo):
    """A path change is not an edit, so the sidecar mirror must not date one."""
    repo.save_file_settings("h1", DEFAULT_WORKSPACE_CONFIG, file_path=_in(OLD, "a.tif"), updated_at=5.0)

    rehome_path_prefix(repo, OLD, NEW)

    assert repo.load_file_settings_by_path(_in(NEW, "a.tif"))[0] == "h1"
    assert repo.load_file_updated_at("h1") == 5.0


def test_source_paths_inside_saved_configs_follow(repo):
    cfg = DEFAULT_WORKSPACE_CONFIG
    triplet = replace(cfg, rgbscan=RgbScanConfig(enabled=True, green_path=_in(OLD, "g.tif"), blue_path=_in(OLD, "b.tif")))
    stitched = replace(cfg, stitch=replace(cfg.stitch, stitch_enabled=True, stitch_paths=(_in(OLD, "2.tif"),)))
    merged = replace(cfg, hdr=replace(cfg.hdr, hdr_paths=(_in(OLD, "2.tif"),), hdr_anchor=_in(OLD, "2.tif")))
    repo.save_file_settings("h1", triplet, file_path=_in(OLD, "r.tif"), updated_at=5.0)
    repo.save_file_settings("s1", stitched, updated_at=7.0)
    repo.save_file_settings("m1", merged)
    repo.save_work_print("h1", "Print 1", triplet)
    repo.save_history_step("h1", 0, triplet)

    rehome_path_prefix(repo, OLD, NEW)

    moved = repo.load_file_settings("h1").rgbscan
    assert (moved.green_path, moved.blue_path) == (_in(NEW, "g.tif"), _in(NEW, "b.tif"))
    assert repo.load_file_settings("s1").stitch.stitch_paths == (_in(NEW, "2.tif"),)
    assert repo.load_file_settings("m1").hdr.hdr_anchor == _in(NEW, "2.tif")
    assert repo.load_work_print("h1", "Print 1").rgbscan.green_path == _in(NEW, "g.tif")
    assert repo.load_history_step("h1", 0).rgbscan.blue_path == _in(NEW, "b.tif")
    assert (repo.load_file_updated_at("h1"), repo.load_file_updated_at("s1")) == (5.0, 7.0)


def test_rehoming_again_or_onto_itself_changes_nothing(repo):
    rolls.recognize_folder(repo, OLD)
    repo.save_global_setting("session_files", [_in(OLD, "a.tif")])
    rehome_path_prefix(repo, OLD, NEW)
    before = (rolls.saved_rolls(repo), repo.get_global_setting("session_files"))

    rehome_path_prefix(repo, OLD, NEW)
    rehome_path_prefix(repo, NEW, NEW)

    assert (rolls.saved_rolls(repo), repo.get_global_setting("session_files")) == before


def test_only_configs_that_name_the_old_folder_are_read(repo, monkeypatch):
    cfg = DEFAULT_WORKSPACE_CONFIG
    here = replace(cfg, rgbscan=RgbScanConfig(enabled=True, green_path=_in(OLD, "g.tif"), blue_path=_in(OLD, "b.tif")))
    elsewhere = replace(cfg, rgbscan=RgbScanConfig(enabled=True, green_path="/mnt/other/roll_a/g.tif", blue_path="/mnt/other/roll_a/b.tif"))
    repo.save_file_settings("h1", here)
    repo.save_file_settings("h2", elsewhere)
    read = []
    original = repo.rehome_saved_configs
    monkeypatch.setattr(repo, "rehome_saved_configs", lambda contains, move: original(contains, lambda d: read.append(d) or move(d)))

    rehome_path_prefix(repo, OLD, NEW)

    assert [d["green_path"] for d in read] == [_in(OLD, "g.tif")]
    assert repo.load_file_settings("h2").rgbscan.green_path == "/mnt/other/roll_a/g.tif"
