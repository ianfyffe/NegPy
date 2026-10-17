"""Sidecars at the controller: Keep Current writes every saved edit and mark, whichever code path
saved it, and discovery fills, merges and offers what another computer wrote."""

import gc
import json
import os
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from negpy.desktop.controller import AppController
from negpy.desktop.session import AppState, DesktopSessionManager
from negpy.domain.models import WorkspaceConfig
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.services.assets.sidecar import sidecar_path_for
from negpy.services.rendering.preview_manager import PreviewManager


@pytest.fixture
def ctl(tmp_path):
    repo = StorageRepository(str(tmp_path / "edits.db"), str(tmp_path / "settings.db"))
    repo.initialize()
    session = MagicMock(spec=DesktopSessionManager)
    session.state = AppState()
    session.repo = repo
    session.asset_model = MagicMock()
    with (
        patch("negpy.desktop.controller.RenderWorker") as render_worker,
        patch("negpy.desktop.controller.PreviewManager") as preview_manager,
    ):
        render_worker.return_value = MagicMock()
        preview_manager.side_effect = lambda: MagicMock(spec=PreviewManager)
        controller = AppController(session)
    controller.set_status = MagicMock()
    paths = []
    for name in ("a.tif", "b.tif"):
        (tmp_path / name).write_bytes(b"scan")
        paths.append(str(tmp_path / name))
    session.state.uploaded_files = [{"name": os.path.basename(p), "path": p, "hash": h} for p, h in zip(paths, ("ha", "hb"))]
    yield controller
    for thread in (
        controller.render_thread,
        controller.export_thread,
        controller.thumb_thread,
        controller.norm_thread,
        controller.discovery_thread,
        controller.preview_load_thread,
        controller.prefetch_load_thread,
        controller.scan_thread,
    ):
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait()
    del controller
    gc.collect()


def _sidecar(path: str):
    side = sidecar_path_for(path)
    if not os.path.exists(side):
        return None
    with open(side, encoding="utf-8") as fh:
        return json.load(fh)


def test_keep_current_persists_emits_and_writes_the_frame_on_screen(ctl):
    a = ctl.state.uploaded_files[0]
    ctl.session.repo.save_file_settings("ha", WorkspaceConfig(), file_path=a["path"])
    ctl.state.current_file_hash, ctl.state.current_file_path = "ha", a["path"]
    seen = []
    ctl.keep_sidecars_current_changed.connect(seen.append)

    ctl.set_keep_sidecars_current(True)
    ctl.flush_sidecars()

    assert seen == [True]
    assert ctl.session.repo.get_global_setting("keep_sidecars_current") is True
    assert _sidecar(a["path"])["sync"]["saved_at"] == ctl.session.repo.load_file_updated_at("ha")


def test_a_save_or_mark_is_written_only_while_keep_current_is_on(ctl):
    a, b = ctl.state.uploaded_files
    ctl.session.repo.save_file_settings("ha", WorkspaceConfig(), file_path=a["path"])
    ctl.flush_sidecars()
    assert _sidecar(a["path"]) is None

    ctl.state.keep_sidecars_current = True
    ctl.session.repo.save_file_mark("hb", "excluded", file_path=b["path"])
    ctl.flush_sidecars()

    assert _sidecar(a["path"]) is None
    assert _sidecar(b["path"])["sync"]["mark"] == "excluded"


def test_bake_to_roll_reaches_the_sidecars(ctl):
    """Bake to Roll writes other frames straight to the repository, with no session signal."""
    _, b = ctl.state.uploaded_files
    ctl.state.keep_sidecars_current = True
    ctl.state.current_file_hash = "ha"
    ctl.session.asset_model.visible_actual_indices_ordered.return_value = [1]
    meters = {"metered_anchor": 0.55, "textural_range": 1.1, "norm_density_range": 1.4, "shadow_point": 0.9, "highlight_point": 0.03}
    ctl.state.auto_meters = {"hb": meters}
    with patch("negpy.desktop.controller.load_or_promote", return_value=WorkspaceConfig()):
        ctl.bake_auto_density_grade_to_roll()
    ctl.flush_sidecars()

    data = _sidecar(b["path"])
    assert data is not None
    assert data["auto_exposure"] is False


def test_a_frame_switch_writes_the_queued_sidecars(ctl):
    ctl.session.active_file_changing.connect.assert_any_call(ctl.flush_sidecars)


def test_save_on_export_keeps_a_newer_edit_the_file_holds(ctl):
    from negpy.services.assets.sidecar import SidecarSync, write_sidecar

    a, b = ctl.state.uploaded_files
    repo = ctl.session.repo
    ctl.session.config_for_asset.side_effect = lambda asset: WorkspaceConfig()
    cfg = WorkspaceConfig()
    theirs = replace(cfg, exposure=replace(cfg.exposure, density=0.9))
    write_sidecar(a["path"], theirs, sync=SidecarSync(saved_at=300.0))
    repo.save_file_settings("ha", cfg, file_path=a["path"], updated_at=100.0)

    assert ctl._write_edit_sidecars([a, b], keep_newer=True) == (2, 0, 0)

    assert _sidecar(a["path"])["density"] == 0.9
    assert _sidecar(a["path"])["sync"]["saved_at"] == 300.0
    assert _sidecar(b["path"])["sync"]["saved_at"] is None

    assert ctl._write_edit_sidecars([a]) == (1, 0, 0)
    assert _sidecar(a["path"])["sync"]["saved_at"] == 100.0


# --- Discovery and Load Edit from Sidecar ------------------------------------------------


def test_the_discovery_worker_plans_the_sidecars_it_found(ctl, tmp_path):
    from negpy.desktop.workers.render import AssetDiscoveryWorker

    a, _ = ctl.state.uploaded_files
    _dated(a["path"], saved_at=200.0)

    plan = AssetDiscoveryWorker(ctl.session.repo)._plan_sidecars([a])

    assert [entry[0]["hash"] for entry in plan.fill] == ["ha"]
    assert AssetDiscoveryWorker()._plan_sidecars([a]) is None


def test_a_planned_fill_skips_a_frame_the_active_roll_forked(ctl):
    from negpy.services.assets.sidecar import plan_frame_sidecars

    a, b = ctl.state.uploaded_files
    for frame in (a, b):
        _dated(frame["path"], saved_at=200.0)
    plan = plan_frame_sidecars(ctl.session.repo, [a, b])

    ctl._apply_sidecar_plan([{**a, "hash": "ha#roll:r1"}, b], plan)

    assert ctl.session.repo.load_file_updated_at("ha") is None
    assert ctl.session.repo.load_file_updated_at("hb") == 200.0


def test_loading_an_offer_dates_it_by_its_file_and_keep_mine_declines_the_rest(ctl):
    from negpy.services.assets.sidecar import SidecarOffer, offers_from_plan, plan_frame_sidecars

    a, b = ctl.state.uploaded_files
    repo = ctl.session.repo
    for frame, file_hash in ((a, "ha"), (b, "hb")):
        repo.save_file_settings(file_hash, WorkspaceConfig(), file_path=frame["path"], updated_at=100.0)
    theirs = _density(0.9)
    ctl.state.current_file_hash = "ha"
    ctl.refresh_thumbnails_for = MagicMock()

    ctl.apply_sidecar_offers([SidecarOffer(a, theirs, 300.0)], [SidecarOffer(b, theirs, 300.0)])

    assert repo.load_file_settings("ha").exposure.density == 0.9
    assert repo.load_file_updated_at("ha") == 300.0
    ctl.session.reload_current_file.assert_called_once()
    ctl.refresh_thumbnails_for.assert_called_once_with(["ha"])
    _dated(b["path"], cfg=theirs, saved_at=300.0)
    assert offers_from_plan(repo, plan_frame_sidecars(repo, [b])) == []


def test_the_frames_own_sidecar_keeps_its_saved_time_and_brings_its_mark(ctl, tmp_path):
    a, _ = ctl.state.uploaded_files
    ctl.state.selected_file_idx = 0
    path = _dated(a["path"], cfg=_density(0.9), saved_at=300.0, mark="keeper", mark_at=310.0)

    assert ctl.load_edit_from_sidecar(path)

    assert ctl.session.load_edit_from_sidecar.call_args.kwargs["saved_at"] == 300.0
    assert ctl.session.load_edit_from_sidecar.call_args.args[0].exposure.density == 0.9
    assert ctl.session.repo.load_mark_record("ha") == ("keeper", 310.0)
    ctl.session.refresh_marks.assert_called_once()


def test_another_frames_sidecar_is_a_new_edit_and_a_mark_only_file_loads_nothing(ctl, tmp_path):
    from negpy.services.assets.sidecar import write_payload

    a, b = ctl.state.uploaded_files
    ctl.state.selected_file_idx = 0
    foreign = _dated(b["path"], cfg=_density(0.9), saved_at=300.0, mark="keeper", mark_at=310.0)

    assert ctl.load_edit_from_sidecar(foreign)
    assert ctl.session.load_edit_from_sidecar.call_args.kwargs["saved_at"] is None
    assert ctl.session.repo.load_mark_record("ha") is None

    ctl.session.load_edit_from_sidecar.reset_mock()
    mark_only = str(tmp_path / "marks.negpy")
    write_payload(mark_only, {"sync": {"saved_at": None, "mark": "keeper", "mark_at": 5.0}})
    assert ctl.load_edit_from_sidecar(mark_only)
    ctl.session.load_edit_from_sidecar.assert_not_called()
    assert not ctl.load_edit_from_sidecar(str(tmp_path / "missing.negpy"))


def _density(value: float) -> WorkspaceConfig:
    cfg = WorkspaceConfig()
    return replace(cfg, exposure=replace(cfg.exposure, density=value))


def _dated(src: str, cfg=None, saved_at=None, mark=None, mark_at=None) -> str:
    from negpy.services.assets.sidecar import SidecarSync, write_sidecar

    return write_sidecar(src, cfg or WorkspaceConfig(), sync=SidecarSync(saved_at, mark, mark_at))


def _roll(ctl, tmp_path) -> str:
    from negpy.services.assets import rolls

    roll_id = rolls.recognize_folder(ctl.session.repo, str(tmp_path))
    ctl.state.active_roll_id = roll_id
    return roll_id


def _roll_file(tmp_path):
    path = tmp_path / ".negpy-roll"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def test_a_roll_write_with_no_frame_save_reaches_the_roll_file(ctl, tmp_path):
    from negpy.services.assets import rolls

    roll_id = _roll(ctl, tmp_path)
    rolls.set_roll_film_base(ctl.session.repo, roll_id, (0.1, 0.2, 0.3))
    ctl.flush_sidecars()
    assert _roll_file(tmp_path) is None

    ctl.set_keep_sidecars_current(True)
    ctl.flush_sidecars()
    assert _roll_file(tmp_path)["roll"]["film_base"] == [0.1, 0.2, 0.3]

    rolls.create_scene(ctl.session.repo, roll_id, "Beach", ["ha"])
    assert ctl._sidecar_mirror.pending()
    ctl.flush_sidecars()
    assert list(_roll_file(tmp_path)["roll"]["scenes"].values())[0]["name"] == "Beach"


def test_discovery_takes_a_new_rolls_file_before_it_splits_the_frames(ctl, tmp_path):
    _roll(ctl, tmp_path)
    (tmp_path / ".negpy-roll").write_text(
        json.dumps({"roll_sidecar_format": 1, "saved_at": 10.0, "half_frame_mode": True, "roll": {}}), encoding="utf-8"
    )
    ctl._start_asset_discovery = MagicMock()

    ctl.request_asset_discovery([str(tmp_path)])

    assert ctl._start_asset_discovery.call_args[0][0].half_frame is True
    assert not ctl._pending_roll_offers


def test_a_newer_roll_file_is_offered_and_loading_it_splits_the_loaded_frames(ctl, tmp_path):
    from negpy.services.assets import rolls
    from negpy.services.assets.sidecar import SidecarPlan

    roll_id = _roll(ctl, tmp_path)
    rolls.set_roll_defaults(ctl.session.repo, roll_id, hue_trim=2.0)
    payload = {"roll_sidecar_format": 1, "saved_at": 4e9, "half_frame_mode": True, "roll": {"defaults": {"hue_trim": 3.0}}}
    (tmp_path / ".negpy-roll").write_text(json.dumps(payload), encoding="utf-8")
    ctl._start_asset_discovery = MagicMock()
    ctl.request_asset_discovery([str(tmp_path)])
    assert ctl._start_asset_discovery.call_args[0][0].half_frame is False
    shown = []
    ctl._show_sidecar_offers = shown.append
    with patch("negpy.desktop.controller.QTimer.singleShot", lambda _ms, fn: fn()):
        ctl._apply_sidecar_plan(ctl.state.uploaded_files, SidecarPlan())
    (offers,) = shown
    assert [o.label for o in offers] == ["Roll settings: " + rolls.roll_for_id(ctl.session.repo, roll_id)["name"]]

    ctl.request_asset_discovery = MagicMock()
    ctl.apply_sidecar_offers(offers, [])

    assert rolls.roll_defaults(ctl.session.repo, roll_id) == {"hue_trim": 3.0}
    assert ctl.half_frame_mode_for_roll(roll_id) is True
    assert ctl.request_asset_discovery.call_args.kwargs["replace_existing"] is True


def test_a_filled_edit_locks_its_diverged_cards_only_without_a_roll_file(ctl, tmp_path):
    asset = ctl.state.uploaded_files[0]
    ctl._lock_diverged_without_roll_file([(asset, WorkspaceConfig())])
    ctl.session.lock_diverged_cards.assert_called_once_with(asset, WorkspaceConfig())

    ctl.session.lock_diverged_cards.reset_mock()
    (tmp_path / ".negpy-roll").write_text("{}", encoding="utf-8")
    ctl._lock_diverged_without_roll_file([(asset, WorkspaceConfig())])
    ctl.session.lock_diverged_cards.assert_not_called()


def test_export_sidecars_writes_the_roll_file(ctl, tmp_path):
    from negpy.services.assets import rolls

    roll_id = _roll(ctl, tmp_path)
    rolls.set_roll_defaults(ctl.session.repo, roll_id, hue_trim=2.0)
    ctl.session.config_for_asset.return_value = WorkspaceConfig()

    ctl._write_edit_sidecars(ctl.state.uploaded_files)

    assert _roll_file(tmp_path)["roll"]["defaults"] == {"hue_trim": 2.0}
