"""Keep Current: every saved edit and mark reaches its frame's sidecar, whichever code path saved it."""

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
