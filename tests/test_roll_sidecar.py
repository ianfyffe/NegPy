"""A folder roll's ``.negpy-roll``: two computers share one folder over a NAS mounted at different
paths, and each follows the roll state the other writes."""

import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from negpy.domain.models import WorkspaceConfig
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.services.assets import rolls
from negpy.services.assets.migrations.roll_fields import CARD_KEYS_VERSION
from negpy.services.assets.sidecar import (
    ROLL_SIDECAR_FORMAT,
    SidecarMirror,
    apply_sidecar_plan,
    export_roll_sidecar,
    load_roll_sidecar,
    plan_frame_sidecars,
    plan_roll_file_write,
    read_roll_sidecar,
    roll_sidecar_path,
)

_FRAMES = (("a.tif", "h1"), ("b.tif", "h2"), ("c.tif", "h3"))


def _computer(tmp_path, nas, name: str, mount: str):
    link = tmp_path / name / mount
    link.parent.mkdir(parents=True)
    os.symlink(nas, link)
    repo = StorageRepository(str(tmp_path / name / "edits.db"), str(tmp_path / name / "settings.db"))
    repo.initialize()
    folder = os.path.join(str(link), "2026", "portra400")
    roll_id = rolls.recognize_folder(repo, folder)
    assets = [{"name": n, "path": os.path.join(folder, n), "hash": h} for n, h in _FRAMES]
    return SimpleNamespace(repo=repo, folder=folder, roll_id=roll_id, assets=assets)


@pytest.fixture
def pcs(tmp_path):
    """Two computers on one share: /…/pc1/mnt/nas and /…/pc2/Volumes/share are the same folder."""
    nas = tmp_path / "nas"
    (nas / "2026" / "portra400").mkdir(parents=True)
    for name, _ in _FRAMES:
        (nas / "2026" / "portra400" / name).write_bytes(b"scan")
    return _computer(tmp_path, nas, "pc1", "mnt/nas"), _computer(tmp_path, nas, "pc2", "Volumes/share")


def _flush(pc) -> None:
    SidecarMirror(pc.repo).flush([pc.folder])


def _file(pc) -> dict:
    with open(roll_sidecar_path(pc.folder), encoding="utf-8") as fh:
        return json.load(fh)


def _write_file(pc, payload: dict) -> None:
    with open(roll_sidecar_path(pc.folder), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _entry(pc) -> dict:
    return rolls.roll_for_id(pc.repo, pc.roll_id) or {}


def test_a_roll_field_no_sidecar_code_names_reaches_the_other_computer(pcs):
    pc1, pc2 = pcs
    rolls.set_roll_film_base(pc1.repo, pc1.roll_id, (0.1, 0.2, 0.3))
    _flush(pc1)

    assert read_roll_sidecar(pc2.repo, pc2.roll_id) is None
    assert rolls.roll_film_base(pc2.repo, pc2.roll_id) == (0.1, 0.2, 0.3)
    assert _entry(pc2)["folder_path"] == pc2.folder != pc1.folder


def test_a_round_trip_carries_defaults_baselines_scenes_locks_modes_and_exclusions(pcs):
    pc1, pc2 = pcs
    repo = pc1.repo
    rolls.set_roll_defaults(repo, pc1.roll_id, hue_trim=2.0)
    rolls.set_roll_normalization(repo, pc1.roll_id, (0.1, 0.1, 0.1), (0.9, 0.9, 0.9))
    scene = rolls.create_scene(repo, pc1.roll_id, "Beach", ["h1"])
    rolls.set_frame_override(repo, pc1.roll_id, "h2", "sensor", True)
    rolls.set_roll_half_frame_profile(repo, pc1.roll_id, {"crop_rect": [0, 0, 1, 1], "split_x": 0.5, "gutter_thickness": 0.01})
    repo.save_global_setting(rolls.HALF_FRAME_MODE_KEY, {pc1.roll_id: True})
    repo.save_global_setting(rolls.TRICHROME_MODE_KEY, {pc1.roll_id: False})
    rolls.touch_roll(repo, pc1.roll_id)
    rolls.remove_members(repo, pc1.roll_id, [pc1.assets[2]["path"]], ["h3"])
    _flush(pc1)

    read_roll_sidecar(pc2.repo, pc2.roll_id)

    assert rolls.portable_state(_entry(pc2)) == json.loads(json.dumps(rolls.portable_state(_entry(pc1))))
    assert rolls.roll_normalization(pc2.repo, pc2.roll_id)["floors"] == (0.1, 0.1, 0.1)
    assert rolls.scene_by_hash(pc2.repo, pc2.roll_id)["h1"][1] == scene
    assert rolls.frame_override_cards(pc2.repo, pc2.roll_id, "h2") == {"sensor"}
    assert rolls.roll_modes(pc2.repo, pc2.roll_id) == {"half_frame_mode": True, "trichrome_mode": False}
    assert rolls.excluded_paths(pc2.repo, [pc2.folder]) == {os.path.normcase(os.path.normpath(pc2.assets[2]["path"]))}
    assert rolls.roll_updated_at(pc2.repo, pc2.roll_id) == rolls.roll_updated_at(pc1.repo, pc1.roll_id)

    # Back the other way: a push on pc2 is a newer file, which pc1 is offered.
    rolls.set_roll_defaults(pc2.repo, pc2.roll_id, hue_trim=3.0)
    _flush(pc2)
    offer = read_roll_sidecar(pc1.repo, pc1.roll_id)
    assert offer is not None and offer.key == f"roll:{pc1.roll_id}"
    rolls.replace_portable_state(pc1.repo, pc1.roll_id, offer.sidecar.state, offer.sidecar.modes, offer.saved_at)
    assert rolls.roll_defaults(pc1.repo, pc1.roll_id) == {"hue_trim": 3.0}
    assert plan_roll_file_write(pc1.repo, pc1.roll_id) is None


def test_a_roll_with_state_here_is_offered_a_newer_file_and_never_an_older_one(pcs):
    pc1, pc2 = pcs
    rolls.set_roll_defaults(pc2.repo, pc2.roll_id, hue_trim=5.0)
    rolls.set_roll_defaults(pc1.repo, pc1.roll_id, hue_trim=2.0)
    _flush(pc1)

    offer = read_roll_sidecar(pc2.repo, pc2.roll_id)
    assert offer is not None and offer.name == os.path.basename(pc2.folder)
    assert rolls.roll_defaults(pc2.repo, pc2.roll_id) == {"hue_trim": 5.0}

    pc2.repo.save_global_setting("sidecar_offers_declined", {offer.key: offer.saved_at})
    assert read_roll_sidecar(pc2.repo, pc2.roll_id) is None

    rolls.set_roll_defaults(pc2.repo, pc2.roll_id, hue_trim=6.0)
    assert read_roll_sidecar(pc2.repo, pc2.roll_id) is None


def test_a_file_that_holds_the_state_here_is_not_offered(pcs):
    pc1, pc2 = pcs
    for pc in pcs:
        rolls.set_roll_defaults(pc.repo, pc.roll_id, hue_trim=2.0)
    _flush(pc2)

    assert read_roll_sidecar(pc1.repo, pc1.roll_id) is None


def test_only_a_change_to_travelling_state_dates_the_roll(pcs, monkeypatch):
    pc1, _ = pcs
    repo, roll_id = pc1.repo, pc1.roll_id
    seen: list = []
    repo.add_roll_observer(seen.append)

    rolls.rename_roll(repo, roll_id, "Portra")
    rolls.add_extra_members(repo, roll_id, ["/elsewhere/x.tif"])
    rolls.rehome_paths(repo, lambda p: p)
    assert rolls.roll_updated_at(repo, roll_id) is None and seen == []

    monkeypatch.setattr(rolls.time, "time", lambda: 100.0)
    rolls.set_roll_film_base(repo, roll_id, (0.1, 0.2, 0.3))
    monkeypatch.setattr(rolls.time, "time", lambda: 200.0)
    rolls.set_roll_film_base(repo, roll_id, (0.1, 0.2, 0.3))
    assert rolls.roll_updated_at(repo, roll_id) == 100.0 and seen == [roll_id]

    rolls.set_frame_override(repo, roll_id, "h1", "lens", True, stamp=False)
    assert rolls.roll_updated_at(repo, roll_id) == 100.0
    rolls.set_frame_override(repo, roll_id, "h2", "lens", True)
    assert rolls.roll_updated_at(repo, roll_id) == 200.0 and seen == [roll_id, roll_id]


def test_the_mirror_never_writes_over_a_newer_file_or_one_it_cannot_read(pcs):
    pc1, pc2 = pcs
    rolls.set_roll_defaults(pc1.repo, pc1.roll_id, hue_trim=2.0)
    rolls.set_roll_defaults(pc2.repo, pc2.roll_id, hue_trim=3.0)
    _flush(pc2)
    _flush(pc1)
    assert _file(pc1)["roll"]["defaults"] == {"hue_trim": 3.0}

    newer = {**_file(pc1), "roll_sidecar_format": ROLL_SIDECAR_FORMAT + 1, "saved_at": 0.0}
    _write_file(pc1, newer)
    rolls.set_roll_defaults(pc1.repo, pc1.roll_id, hue_trim=4.0)

    assert load_roll_sidecar(pc1.folder) is None
    assert read_roll_sidecar(pc1.repo, pc1.roll_id) is None
    assert plan_roll_file_write(pc1.repo, pc1.roll_id) is None
    _flush(pc1)
    assert _file(pc1) == newer


def test_a_file_from_before_a_card_split_is_migrated_on_read(pcs):
    pc1, pc2 = pcs
    state = {"frame_overrides": {"h1": ["process"]}, "section_pushes": {"tone": {"density": 1.2, "toe": 0.3}}}
    _write_file(pc1, {"roll_sidecar_format": 1, "card_keys_version": 0, "saved_at": 10.0, "roll": state})

    read_roll_sidecar(pc2.repo, pc2.roll_id)

    assert rolls.frame_override_cards(pc2.repo, pc2.roll_id, "h1") == {"process", "baseline"}
    assert rolls.section_push(pc2.repo, pc2.roll_id, "paper") == {"toe": 0.3}
    current = {"roll_sidecar_format": 1, "card_keys_version": CARD_KEYS_VERSION, "saved_at": 10.0, "roll": state}
    _write_file(pc1, current)
    assert load_roll_sidecar(pc1.folder).state["frame_overrides"] == {"h1": ["process"]}


def test_a_file_never_puts_local_fields_or_paths_into_the_roll(pcs):
    pc1, pc2 = pcs
    hostile = {"folder_path": "/x", "kind": "virtual", "forked_hashes": ["h1"], "excluded_names": ["../a.tif", "b.tif"]}
    _write_file(pc1, {"roll_sidecar_format": 1, "saved_at": 10.0, "roll": hostile})

    read_roll_sidecar(pc2.repo, pc2.roll_id)

    entry = _entry(pc2)
    assert (entry["folder_path"], entry["kind"], entry.get("forked_hashes")) == (pc2.folder, "folder", None)
    assert entry["excluded_paths"] == [os.path.join(pc2.folder, "b.tif")]


def test_export_dates_an_undated_roll_once_and_writes_its_file(pcs):
    pc1, pc2 = pcs
    store = pc1.repo.get_global_setting(rolls.ROLLS_KEY)
    store[pc1.roll_id]["defaults"] = {"hue_trim": 2.0}
    pc1.repo.save_global_setting(rolls.ROLLS_KEY, store)
    assert rolls.roll_updated_at(pc1.repo, pc1.roll_id) is None

    assert export_roll_sidecar(pc1.repo, pc1.roll_id) == roll_sidecar_path(pc1.folder)
    assert export_roll_sidecar(pc1.repo, pc1.roll_id) is None
    read_roll_sidecar(pc2.repo, pc2.roll_id)
    assert rolls.roll_defaults(pc2.repo, pc2.roll_id) == {"hue_trim": 2.0}


def test_a_baseline_from_the_folder_roll_names_the_roll_on_the_other_computer(pcs):
    pc1, pc2 = pcs
    config = WorkspaceConfig()
    config = replace(config, process=replace(config.process, baseline_source=f"roll:{pc1.roll_id}"))
    pc1.repo.save_file_settings("h1", config, file_path=pc1.assets[0]["path"])
    mirror = SidecarMirror(pc1.repo)
    mirror.queue("h1", pc1.assets[0]["path"])
    mirror.flush()

    apply_sidecar_plan(pc2.repo, plan_frame_sidecars(pc2.repo, pc2.assets))

    assert pc2.repo.load_file_settings("h1").process.baseline_source == f"roll:{pc2.roll_id}"
    # pc1's file is current for pc2's edit: the ids differ, the baseline does not.
    mirror = SidecarMirror(pc2.repo)
    mirror.queue("h1", pc2.assets[0]["path"])
    assert mirror.flush() == (0, 0)
