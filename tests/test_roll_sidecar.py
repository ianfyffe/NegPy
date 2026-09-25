"""Roll locks in frame sidecars: each frame's ``roll_locks`` say which cards it keeps
away from its folder roll, so a frame opened on another computer follows the same cards.
Each "machine" is its own database and its own copy of the folder."""

import json
import os
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest

from negpy.desktop.session import DesktopSessionManager
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.kernel.system.config import DEFAULT_WORKSPACE_CONFIG
from negpy.services.assets import rolls
from negpy.services.assets.sidecar import (
    SidecarMirror,
    pending_sidecar_offers,
    promote_sidecar,
    promote_unedited,
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
    mirror.flush()


def _copy_sidecars(src, dst) -> None:
    for name in os.listdir(src.folder):
        if name.endswith(".negpy"):
            shutil.copy2(os.path.join(src.folder, name), os.path.join(dst.folder, name))


def _look(m) -> list:
    return [m.session.config_for_asset(a) for a in m.assets]


@pytest.fixture()
def pair(tmp_path):
    a, b = _machine(tmp_path, "a"), _machine(tmp_path, "b")
    for asset in a.assets:
        a.repo.save_file_settings(asset["hash"], _cfg(), file_path=asset["path"])
    # Apply to Roll from h1: the roll takes its Calibration; h2 keeps its own, locked.
    for m in (a, b):
        rolls.set_roll_defaults(m.repo, m.roll_id, hue_trim=2.0)
    a.repo.save_file_settings("h2", _cfg(hue_trim=5.0), file_path=a.assets[1]["path"])
    rolls.set_frame_override(a.repo, a.roll_id, "h2", "sensor", True)
    return a, b


def test_a_frame_carries_its_locks_to_the_other_machine(pair):
    a, b = pair
    _mirror(a)
    _copy_sidecars(a, b)

    promote_unedited(b.repo, b.assets)

    assert a.folder != b.folder
    assert pending_sidecar_offers(b.repo, b.assets) == []
    assert _look(b) == _look(a)
    assert rolls.frame_override_cards(b.repo, b.roll_id, "h2") == {"sensor"}
    rolls.set_roll_defaults(b.repo, b.roll_id, hue_trim=3.0)
    assert [c.process.hue_trim for c in _look(b)] == [3.0, 5.0, 3.0]


def test_a_format_2_sidecar_locks_only_where_the_edit_differs_from_the_roll(tmp_path):
    b = _machine(tmp_path, "b")
    rolls.set_roll_defaults(b.repo, b.roll_id, hue_trim=2.0)
    for asset, hue in zip(b.assets[:2], (2.0, 5.0)):
        payload = {"sidecar_format": 2, "saved_at": 10.0, "source_hash": asset["hash"], "mark": None, "edit": _cfg(hue).to_dict()}
        with open(sidecar_path_for(asset["path"]), "w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)

    promote_unedited(b.repo, b.assets)

    assert rolls.frame_override_cards(b.repo, b.roll_id, "h1") == set()
    assert rolls.frame_override_cards(b.repo, b.roll_id, "h2") == {"sensor"}
    assert [c.process.hue_trim for c in _look(b)[:2]] == [2.0, 5.0]


def test_a_forked_frame_writes_no_locks(pair):
    a, _ = pair
    rolls.fork_edit(a.repo, a.roll_id, "h2", a.assets[1]["path"], _cfg(hue_trim=7.0))
    _mirror(a)

    assert sidecar_from_repo(a.repo, "h2", a.assets[1]["path"]).roll_locks is None
    assert sidecar_from_repo(a.repo, "h1", a.assets[0]["path"]).roll_locks == ()


def test_a_fork_here_keeps_its_locks_when_the_shared_sidecar_loads(pair):
    a, b = pair
    rolls.fork_edit(b.repo, b.roll_id, "h2", b.assets[1]["path"], _cfg())
    rolls.set_frame_override(b.repo, b.roll_id, "h2", "lens", True)
    _mirror(a)

    promote_sidecar(b.repo, "h2", b.assets[1]["path"], sidecar_from_repo(a.repo, "h2", a.assets[1]["path"]))

    assert rolls.frame_override_cards(b.repo, b.roll_id, "h2") == {"lens"}


def test_a_frame_in_a_virtual_roll_writes_no_locks(tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    repo = StorageRepository(str(tmp_path / "edits.db"), str(tmp_path / "settings.db"))
    repo.initialize()
    rolls.create_virtual_roll(repo, "Picks", [str(root / "a.tif")])
    repo.save_file_settings("h1", _cfg(), file_path=str(root / "a.tif"))

    assert sidecar_from_repo(repo, "h1", str(root / "a.tif")).roll_locks is None


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
