"""Roll-centric sidecars: a folder roll's ``.negpy-roll`` file and each frame's
``roll_locks`` carry what the roll holds, so a frame opened on another computer looks the
same there. Each "machine" is its own database and its own copy of the folder."""

import json
import os
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest

from negpy.desktop.session import DesktopSessionManager
from negpy.kernel.system.config import DEFAULT_WORKSPACE_CONFIG
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.services.assets import rolls
from negpy.services.assets.sidecar import (
    ROLL_SIDECAR_NAME,
    RollSidecarOffer,
    SidecarMirror,
    decline_sidecar_offers,
    export_roll_sidecar,
    load_roll_sidecar,
    load_sidecar,
    pending_sidecar_offers,
    promote_sidecar,
    read_frame_sidecars,
    read_roll_sidecar,
    roll_sidecar_path,
    sidecar_from_repo,
    sidecar_path_for,
)

_FRAMES = (("a.tif", "h1"), ("b.tif", "h2"), ("c.tif", "h3"))


def _cfg(hue_trim: float = 0.0, density: float = 1.0):
    base = DEFAULT_WORKSPACE_CONFIG
    return replace(base, process=replace(base.process, hue_trim=hue_trim), exposure=replace(base.exposure, density=density))


def _machine(tmp_path, name: str):
    root = tmp_path / name
    folder = root / "photos" / "roll"
    folder.mkdir(parents=True)
    for file_name, _ in _FRAMES:
        (folder / file_name).write_bytes(b"x")
    repo = StorageRepository(str(root / "edits.db"), str(root / "settings.db"))
    repo.initialize()
    session = DesktopSessionManager(repo)
    roll_id = rolls.recognize_folder(repo, str(folder))
    session.state.active_roll_id = roll_id
    assets = [{"name": n, "path": str(folder / n), "hash": h} for n, h in _FRAMES]
    return SimpleNamespace(repo=repo, session=session, folder=str(folder), roll_id=roll_id, assets=assets)


def _mirror(m) -> None:
    mirror = SidecarMirror(m.repo)
    for asset in m.assets:
        mirror.mark_dirty(asset["hash"], asset["path"])
    mirror.mark_roll_dirty(m.roll_id)
    mirror.flush()


def _copy_sidecars(src, dst) -> None:
    for name in os.listdir(src.folder):
        if name.endswith(".negpy") or name == ROLL_SIDECAR_NAME:
            shutil.copy2(os.path.join(src.folder, name), os.path.join(dst.folder, name))


def _open_on(m) -> list:
    """What a folder open does: read the roll file, fill unedited frames, list newer ones."""
    roll_offer = read_roll_sidecar(m.repo, m.roll_id)
    read_frame_sidecars(m.repo, m.assets)
    return [o for o in [roll_offer] if o is not None] + pending_sidecar_offers(m.repo, m.assets)


def _look(m) -> list:
    return [m.session.config_for_asset(a) for a in m.assets]


@pytest.fixture()
def pair(tmp_path):
    a, b = _machine(tmp_path, "a"), _machine(tmp_path, "b")
    for asset in a.assets:
        a.repo.save_file_settings(asset["hash"], _cfg(), file_path=asset["path"])
    # Apply to Roll from h1: the roll takes its Calibration; h2 keeps its own, locked.
    rolls.set_roll_defaults(a.repo, a.roll_id, hue_trim=2.0)
    a.repo.save_file_settings("h2", _cfg(hue_trim=5.0), file_path=a.assets[1]["path"])
    rolls.set_frame_override(a.repo, a.roll_id, "h2", "sensor", True)
    return a, b


def test_frames_look_the_same_on_another_machine_after_an_apply(pair):
    a, b = pair
    _mirror(a)
    _copy_sidecars(a, b)

    assert _open_on(b) == []
    assert a.folder != b.folder
    assert _look(b) == _look(a)
    assert [c.process.hue_trim for c in _look(b)] == [2.0, 5.0, 2.0]
    assert rolls.frame_override_cards(b.repo, b.roll_id, "h2") == {"sensor"}


def test_a_locked_frame_keeps_its_value_through_a_later_push_on_the_other_machine(pair):
    a, b = pair
    _mirror(a)
    _copy_sidecars(a, b)
    _open_on(b)

    rolls.set_roll_defaults(b.repo, b.roll_id, hue_trim=3.0)

    assert [c.process.hue_trim for c in _look(b)] == [3.0, 5.0, 3.0]


def test_a_format_2_sidecar_locks_only_where_the_edit_differs_from_the_roll(tmp_path):
    b = _machine(tmp_path, "b")
    rolls.set_roll_defaults(b.repo, b.roll_id, hue_trim=2.0)
    for asset, hue in zip(b.assets[:2], (2.0, 5.0)):
        payload = {"sidecar_format": 2, "saved_at": 10.0, "source_hash": asset["hash"], "mark": None, "edit": _cfg(hue).to_dict()}
        with open(sidecar_path_for(asset["path"]), "w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)

    read_frame_sidecars(b.repo, b.assets)

    assert rolls.frame_override_cards(b.repo, b.roll_id, "h1") == set()
    assert rolls.frame_override_cards(b.repo, b.roll_id, "h2") == {"sensor"}
    assert [c.process.hue_trim for c in _look(b)[:2]] == [2.0, 5.0]


def test_a_roll_new_here_is_adopted_silently_and_a_newer_one_offered_until_declined(pair):
    a, b = pair
    _mirror(a)
    _copy_sidecars(a, b)
    saved_at = load_roll_sidecar(a.folder).saved_at

    assert read_roll_sidecar(b.repo, b.roll_id) is None
    assert rolls.roll_defaults(b.repo, b.roll_id) == {"hue_trim": 2.0}
    assert rolls.roll_updated_at(b.repo, b.roll_id) == saved_at
    assert read_roll_sidecar(b.repo, b.roll_id) is None

    rolls.set_roll_defaults(a.repo, a.roll_id, hue_trim=4.0)
    rolls.touch_roll(a.repo, a.roll_id, saved_at + 60.0)
    _mirror(a)
    _copy_sidecars(a, b)

    offer = read_roll_sidecar(b.repo, b.roll_id)
    assert isinstance(offer, RollSidecarOffer)
    assert offer.sidecar.state["defaults"] == {"hue_trim": 4.0}
    assert rolls.roll_defaults(b.repo, b.roll_id) == {"hue_trim": 2.0}

    decline_sidecar_offers(b.repo, [offer])
    assert read_roll_sidecar(b.repo, b.roll_id) is None
    assert read_roll_sidecar(b.repo, b.roll_id, any_age=True) == offer


def test_a_roll_with_state_from_before_roll_files_is_offered_not_replaced(tmp_path):
    a, b = _machine(tmp_path, "a"), _machine(tmp_path, "b")
    rolls.set_roll_defaults(a.repo, a.roll_id, hue_trim=2.0)
    export_roll_sidecar(a.repo, a.roll_id)
    _copy_sidecars(a, b)
    b.repo.save_global_setting(rolls.ROLLS_KEY, {b.roll_id: {**rolls.roll_for_id(b.repo, b.roll_id), "defaults": {"hue_trim": 9.0}}})

    assert read_roll_sidecar(b.repo, b.roll_id) is not None
    assert rolls.roll_defaults(b.repo, b.roll_id) == {"hue_trim": 9.0}


def test_half_frame_mode_crosses_machines(tmp_path):
    a, b = _machine(tmp_path, "a"), _machine(tmp_path, "b")
    a.repo.save_global_setting(rolls.HALF_FRAME_MODE_KEY, {a.roll_id: True})
    rolls.touch_roll(a.repo, a.roll_id)
    export_roll_sidecar(a.repo, a.roll_id)
    _copy_sidecars(a, b)

    read_roll_sidecar(b.repo, b.roll_id)

    assert rolls.roll_half_frame_mode(b.repo, b.roll_id)


def test_scenes_and_their_baselines_match_by_hash(pair):
    a, b = pair
    scene_id = rolls.create_scene(a.repo, a.roll_id, "Beach", ["h1", "h3"])
    rolls.set_scene_normalization(a.repo, a.roll_id, scene_id, (0.1, 0.2, 0.3), (1.1, 1.2, 1.3))
    rolls.set_roll_normalization(a.repo, a.roll_id, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    _mirror(a)
    _copy_sidecars(a, b)
    _open_on(b)

    assert rolls.scene_by_hash(b.repo, b.roll_id) == rolls.scene_by_hash(a.repo, a.roll_id)
    assert rolls.scene_normalization(b.repo, b.roll_id, scene_id) == rolls.scene_normalization(a.repo, a.roll_id, scene_id)
    riding = replace(_cfg(), process=replace(_cfg().process, use_luma_average=True))
    on_b = rolls.resolve_roll_baseline(b.repo, b.roll_id, "h3", riding)
    assert on_b.process.locked_floors == (0.1, 0.2, 0.3)
    assert rolls.resolve_roll_baseline(b.repo, b.roll_id, "h2", riding).process.locked_floors == (0.0, 0.0, 0.0)


def _with_baseline(source: str):
    cfg = _cfg()
    return replace(cfg, process=replace(cfg.process, locked_floors=(0.1,) * 3, locked_ceils=(0.9,) * 3, baseline_source=source))


def test_a_baseline_from_the_folder_roll_names_the_roll_here(pair, tmp_path):
    """Roll ids are per machine; scene ids travel in the roll file."""
    a, b = pair
    scene_id = rolls.create_scene(a.repo, a.roll_id, "Beach", ["h3"])
    sources = (f"roll:{a.roll_id}", "roll:elsewhere", f"scene:{scene_id}")
    for asset, source in zip(a.assets, sources):
        a.repo.save_file_settings(asset["hash"], _with_baseline(source), file_path=asset["path"])
    a.repo.save_work_print("h1", "Print 1", _with_baseline(sources[0]))
    _mirror(a)
    _copy_sidecars(a, b)
    _open_on(b)

    with open(sidecar_path_for(b.assets[0]["path"]), encoding="utf-8") as f:
        assert a.roll_id not in f.read()
    on_b = [b.repo.load_file_settings(h).process.baseline_source for _, h in _FRAMES]
    assert on_b == [f"roll:{b.roll_id}", "roll:elsewhere", f"scene:{scene_id}"]
    assert [cfg.process.baseline_source for _, _, cfg in b.repo.load_work_prints("h1")] == [f"roll:{b.roll_id}"]
    assert rolls.baseline_label(b.repo, b.repo.load_file_settings("h3").process) == "Scene “Beach”"

    promote_sidecar(a.repo, "h1", a.assets[0]["path"], sidecar_from_repo(a.repo, "h1", a.assets[0]["path"]))
    assert a.repo.load_file_settings("h1").process.baseline_source == f"roll:{a.roll_id}"

    stray = tmp_path / "stray.tif"
    stray.write_bytes(b"x")
    promote_sidecar(b.repo, "h9", str(stray), sidecar_from_repo(a.repo, "h1", a.assets[0]["path"]))
    assert b.repo.load_file_settings("h9").process.baseline_source == ""


def test_a_forked_frame_writes_no_locks_and_the_roll_file_no_forks(pair):
    a, _ = pair
    rolls.fork_edit(a.repo, a.roll_id, "h2", a.assets[1]["path"], _cfg(hue_trim=7.0))
    _mirror(a)

    assert sidecar_from_repo(a.repo, "h2", a.assets[1]["path"]).roll_locks is None
    assert sidecar_from_repo(a.repo, "h1", a.assets[0]["path"]).roll_locks == ()
    with open(roll_sidecar_path(a.folder), encoding="utf-8") as f:
        payload = json.load(f)
    assert not {"forked_hashes", "frame_overrides", "folder_path", "extra_paths"} & set(payload)
    assert "h2" not in json.dumps(payload)


def test_a_fork_here_keeps_its_locks_when_the_shared_sidecar_loads(pair):
    a, b = pair
    rolls.fork_edit(b.repo, b.roll_id, "h2", b.assets[1]["path"], _cfg())
    rolls.set_frame_override(b.repo, b.roll_id, "h2", "lens", True)
    _mirror(a)

    promote_sidecar(b.repo, "h2", b.assets[1]["path"], sidecar_from_repo(a.repo, "h2", a.assets[1]["path"]))

    assert rolls.frame_override_cards(b.repo, b.roll_id, "h2") == {"lens"}


def test_a_virtual_roll_writes_no_roll_file(tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    (root / "a.tif").write_bytes(b"x")
    repo = StorageRepository(str(tmp_path / "edits.db"), str(tmp_path / "settings.db"))
    repo.initialize()
    roll_id = rolls.create_virtual_roll(repo, "Picks", [str(root / "a.tif")])
    rolls.set_roll_defaults(repo, roll_id, hue_trim=2.0)
    repo.save_file_settings("h1", _cfg(), file_path=str(root / "a.tif"))

    mirror = SidecarMirror(repo)
    mirror.mark_roll_dirty(roll_id)
    assert mirror.pending() == 0
    assert export_roll_sidecar(repo, roll_id) is None
    assert sidecar_from_repo(repo, "h1", str(root / "a.tif")).roll_locks is None
    assert not os.path.exists(roll_sidecar_path(str(root)))


def test_a_roll_write_is_stamped_and_mirrors_at_that_time(tmp_path):
    a = _machine(tmp_path, "a")
    assert rolls.roll_updated_at(a.repo, a.roll_id) is None
    rolls.set_section_push(a.repo, a.roll_id, "exposure", {"density": 1.2})
    stamp = rolls.roll_updated_at(a.repo, a.roll_id)
    assert stamp is not None

    _mirror(a)

    assert load_roll_sidecar(a.folder).saved_at == stamp


def test_a_lock_change_bumps_the_rows_time_and_remirrors_the_frame(tmp_path):
    a = _machine(tmp_path, "a")
    asset = a.assets[0]
    a.repo.save_file_settings("h1", _cfg(), file_path=asset["path"], updated_at=5.0)
    emitted = []
    a.session.locks_changed.connect(emitted.append)

    a.session.frame_locks_changed(a.roll_id, asset)
    assert a.repo.load_file_record("h1")[1] > 5.0
    assert emitted == [[asset]]

    other = rolls.create_virtual_roll(a.repo, "Picks", [asset["path"]])
    a.repo.touch_file_settings("h1", 5.0)
    a.session.frame_locks_changed(other, asset)
    a.session.frame_locks_changed(a.roll_id, {**asset, "hash": "h1#roll:x"})
    assert a.repo.load_file_record("h1")[1] == 5.0
    assert len(emitted) == 1


def test_undo_that_relocks_a_card_bumps_the_row(tmp_path):
    a = _machine(tmp_path, "a")
    rolls.set_roll_defaults(a.repo, a.roll_id, hue_trim=2.0)
    a.session.state.uploaded_files = list(a.assets)
    a.session.state.selected_file_idx = 0
    a.session.state.current_file_hash = "h1"
    a.session.state.current_file_path = a.assets[0]["path"]
    a.repo.save_file_settings("h1", _cfg(hue_trim=2.0), file_path=a.assets[0]["path"], updated_at=5.0)
    a.session.state.config = _cfg(hue_trim=6.0)

    a.session._relock_diverged_cards()

    assert rolls.frame_override_cards(a.repo, a.roll_id, "h1") == {"sensor"}
    assert a.repo.load_file_record("h1")[1] > 5.0


def test_no_writer_replaces_a_roll_file_saved_after_the_state_here(pair):
    """B closed the offer for A's newer file; its export and mirror leave that file alone
    until B changes the roll itself."""
    a, b = pair
    rolls.touch_roll(b.repo, b.roll_id, 50.0)
    rolls.set_roll_defaults(a.repo, a.roll_id, hue_trim=4.0)
    rolls.touch_roll(a.repo, a.roll_id, 100.0)
    export_roll_sidecar(a.repo, a.roll_id)
    _copy_sidecars(a, b)

    assert export_roll_sidecar(b.repo, b.roll_id) is None
    _mirror(b)
    assert load_roll_sidecar(b.folder).saved_at == 100.0
    assert isinstance(read_roll_sidecar(b.repo, b.roll_id), RollSidecarOffer)

    rolls.set_roll_defaults(b.repo, b.roll_id, hue_trim=6.0)
    rolls.touch_roll(b.repo, b.roll_id, 200.0)
    assert export_roll_sidecar(b.repo, b.roll_id) is not None
    assert load_roll_sidecar(b.folder).state["defaults"] == {"hue_trim": 6.0}


def test_exporting_a_roll_with_undated_state_leaves_an_existing_file_to_the_offer(tmp_path):
    a, b = _machine(tmp_path, "a"), _machine(tmp_path, "b")
    rolls.set_roll_defaults(a.repo, a.roll_id, hue_trim=2.0)
    export_roll_sidecar(a.repo, a.roll_id)
    _copy_sidecars(a, b)
    b.repo.save_global_setting(rolls.ROLLS_KEY, {b.roll_id: {**rolls.roll_for_id(b.repo, b.roll_id), "defaults": {"hue_trim": 9.0}}})

    assert export_roll_sidecar(b.repo, b.roll_id) is None
    assert rolls.roll_updated_at(b.repo, b.roll_id) is None
    assert load_roll_sidecar(b.folder).state["defaults"] == {"hue_trim": 2.0}


def test_marking_a_forked_frame_mirrors_the_shared_sidecar(tmp_path):
    a = _machine(tmp_path, "a")
    asset = a.assets[1]
    a.repo.save_file_settings("h2", _cfg(), file_path=asset["path"], updated_at=5.0)
    fork = rolls.fork_edit(a.repo, a.roll_id, "h2", asset["path"], _cfg(hue_trim=7.0))
    a.session.state.uploaded_files = [{**asset, "hash": fork}]
    a.session.state.selected_file_idx = 0
    a.session.asset_model = SimpleNamespace(refresh=lambda: None)
    mirror = SidecarMirror(a.repo)
    a.session.marks_changed.connect(lambda assets: [mirror.mark_dirty(f["hash"], f["path"]) for f in assets])

    a.session.toggle_mark("keeper")
    mirror.flush()

    mark, marked_at = a.repo.load_mark_record("h2")
    sidecar = load_sidecar(asset["path"])
    assert (sidecar.mark, sidecar.mark_at, sidecar.saved_at) == ("keeper", marked_at, 5.0)


def test_a_forks_work_print_stays_with_the_fork(tmp_path):
    a = _machine(tmp_path, "a")
    asset = a.assets[1]
    a.repo.save_file_settings("h2", _cfg(), file_path=asset["path"], updated_at=5.0)
    fork = rolls.fork_edit(a.repo, a.roll_id, "h2", asset["path"], _cfg(hue_trim=7.0))
    a.session.state.current_file_hash = fork
    a.session.state.config = _cfg(hue_trim=7.0)

    a.session.save_work_print("Print 1")

    assert a.repo.load_file_record("h2")[1] == 5.0
    assert sidecar_from_repo(a.repo, "h2", asset["path"]).work_prints == {}
    assert a.repo.list_work_prints(fork) == ["Print 1"]


def test_a_folder_opened_again_after_its_roll_was_deleted_is_offered_its_roll_file(tmp_path):
    """Delete forgets the roll here only; the file stays for the other computers."""
    a = _machine(tmp_path, "a")
    rolls.set_roll_defaults(a.repo, a.roll_id, hue_trim=2.0)
    export_roll_sidecar(a.repo, a.roll_id)

    rolls.delete_roll(a.repo, a.roll_id)
    roll_id = rolls.recognize_folder(a.repo, a.folder)
    offer = read_roll_sidecar(a.repo, roll_id)

    assert os.path.exists(roll_sidecar_path(a.folder))
    assert isinstance(offer, RollSidecarOffer)
    assert rolls.roll_defaults(a.repo, roll_id) == {}
    decline_sidecar_offers(a.repo, [offer])
    assert read_roll_sidecar(a.repo, roll_id) is None
    assert rolls.roll_defaults(a.repo, roll_id) == {}


def test_a_fork_hash_neither_writes_nor_restores_roll_locks(pair):
    a, _ = pair
    fork = rolls.fork_edit(a.repo, a.roll_id, "h2", a.assets[1]["path"], _cfg(hue_trim=7.0))
    before = rolls.roll_for_id(a.repo, a.roll_id).get("frame_overrides")

    assert sidecar_from_repo(a.repo, fork, a.assets[1]["path"]).roll_locks is None
    promote_sidecar(a.repo, fork, a.assets[1]["path"], sidecar_from_repo(a.repo, "h1", a.assets[0]["path"]))

    assert rolls.roll_for_id(a.repo, a.roll_id).get("frame_overrides") == before


def test_a_mark_and_work_print_on_an_unedited_frame_reach_the_other_machine(tmp_path):
    a, b = _machine(tmp_path, "a"), _machine(tmp_path, "b")
    asset = a.assets[0]
    a.session.state.uploaded_files = [dict(asset)]
    a.session.state.selected_file_idx = 0
    a.session.asset_model = SimpleNamespace(refresh=lambda: None)
    a.session.toggle_mark("excluded")
    a.repo.save_work_print("h1", "Print 1", _cfg(hue_trim=4.0), created_at=3.0)
    _mirror(a)
    assert a.repo.load_file_record("h1") is None
    _copy_sidecars(a, b)

    assert _open_on(b) == []
    assert b.repo.load_file_record("h1") is None
    assert b.repo.load_file_mark("h1") == "excluded"
    assert b.repo.list_work_prints("h1") == ["Print 1"]


def test_export_sidecars_writes_a_frame_that_has_only_a_mark(tmp_path):
    from negpy.desktop.controller import AppController

    a = _machine(tmp_path, "a")
    a.repo.save_file_mark("h2", "keeper", file_path=a.assets[1]["path"])
    written, failed = AppController._write_edit_sidecars(SimpleNamespace(session=a.session), a.assets)

    assert (written, failed) == (1, 0)
    sidecar = load_sidecar(a.assets[1]["path"])
    assert sidecar is not None and (sidecar.config, sidecar.mark) == (None, "keeper")
    assert not os.path.exists(sidecar_path_for(a.assets[0]["path"]))
