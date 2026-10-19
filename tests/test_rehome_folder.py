"""Renaming a roll's folder on disk carries every path-keyed record with it."""

import os
from dataclasses import replace

import pytest

from negpy.domain.models import WorkspaceConfig
from negpy.features.rgbscan.models import RgbScanConfig
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.kernel.system.config import DEFAULT_WORKSPACE_CONFIG
from negpy.services.assets import composites, rolls
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


def test_a_library_root_at_the_folder_follows(repo):
    parent = os.path.dirname(OLD)
    repo.save_global_setting("library_roots", [OLD, parent, NEW])

    rehome_path_prefix(repo, OLD, NEW)

    assert repo.get_global_setting("library_roots") == [NEW, parent]


def test_moved_path_rebases_only_the_folder_and_what_is_under_it():
    assert moved_path(OLD, OLD, NEW) == NEW
    assert moved_path(_in(OLD, "sub", "a.tif"), OLD, NEW) == _in(NEW, "sub", "a.tif")
    assert moved_path(OLD + "_2", OLD, NEW) == OLD + "_2"
    assert moved_path(_in(OLD + "x", "a.tif"), OLD, NEW) == _in(OLD + "x", "a.tif")
    assert moved_path("", OLD, NEW) == ""


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


def test_import_sources_follow(repo):
    repo.save_global_setting(rolls.IMPORT_SOURCES_KEY, [OLD, "/other"])

    rehome_path_prefix(repo, OLD, NEW)

    assert repo.get_global_setting(rolls.IMPORT_SOURCES_KEY) == [NEW, "/other"]


def test_source_paths_inside_saved_configs_follow(repo):
    cfg = DEFAULT_WORKSPACE_CONFIG
    triplet = replace(cfg, rgbscan=RgbScanConfig(enabled=True, green_path=_in(OLD, "g.tif"), blue_path=_in(OLD, "b.tif")))
    stitched = replace(cfg, stitch=replace(cfg.stitch, stitch_enabled=True, stitch_paths=(_in(OLD, "2.tif"),)))
    merged = replace(cfg, hdr=replace(cfg.hdr, hdr_paths=(_in(OLD, "2.tif"),), hdr_anchor=_in(OLD, "2.tif")))
    repo.save_file_settings("h1", triplet, file_path=_in(OLD, "r.tif"))
    repo.save_file_settings("s1", stitched)
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


def test_a_root_or_identical_prefix_changes_nothing(repo):
    roll = rolls.recognize_folder(repo, OLD)
    repo.save_file_settings("h1", DEFAULT_WORKSPACE_CONFIG, file_path=_in(OLD, "a.tif"))

    rehome_path_prefix(repo, os.sep, NEW)
    rehome_path_prefix(repo, OLD, OLD)

    assert rolls.roll_for_id(repo, roll)["folder_path"] == OLD
    assert repo.load_file_settings_by_path(_in(OLD, "a.tif")) is not None


def test_only_configs_that_name_the_old_folder_are_read(repo, monkeypatch):
    cfg = DEFAULT_WORKSPACE_CONFIG
    here = replace(cfg, rgbscan=RgbScanConfig(enabled=True, green_path=_in(OLD, "g.tif"), blue_path=_in(OLD, "b.tif")))
    elsewhere = replace(cfg, rgbscan=RgbScanConfig(enabled=True, green_path="/mnt/other/roll_a/g.tif", blue_path="/mnt/other/roll_a/b.tif"))
    repo.save_file_settings("h1", here)
    repo.save_file_settings("h2", elsewhere)
    read = []
    original = repo.rehome_saved_configs
    monkeypatch.setattr(repo, "rehome_saved_configs", lambda old_prefix, move: original(old_prefix, lambda d: read.append(d) or move(d)))

    rehome_path_prefix(repo, OLD, NEW)

    assert [d["green_path"] for d in read] == [_in(OLD, "g.tif")]
    assert repo.load_file_settings("h2").rgbscan.green_path == "/mnt/other/roll_a/g.tif"


def test_a_rehome_keeps_each_edits_saved_time(repo):
    repo.save_file_settings("ha", WorkspaceConfig(), file_path=_in(OLD, "a.tif"), updated_at=5.0)
    repo.save_file_mark("ha", "keeper", file_path=_in(OLD, "a.tif"), marked_at=6.0)

    rehome_path_prefix(repo, OLD, NEW)

    assert repo.load_file_settings_by_path(_in(NEW, "a.tif"))[0] == "ha"
    assert repo.load_file_updated_at("ha") == 5.0
    assert repo.load_mark_record("ha") == ("keeper", 6.0)
