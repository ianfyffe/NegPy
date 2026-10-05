import os
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

from negpy.desktop.controller import AppController
from negpy.desktop.session import AppState
from negpy.services.assets.rolls import folder_roll_id_for_path, recognize_folder, roll_for_id, saved_rolls


def _controller(as_roll=False, capture_req=None):
    store: dict = {}
    c = MagicMock()
    c.state = AppState()
    c.session.repo.get_global_setting.side_effect = lambda key, default=None: store.get(key, default)
    c.session.repo.save_global_setting.side_effect = lambda key, value: store.__setitem__(key, value)
    c._pending_scanned_file = None
    c._pending_capture_imports = {}
    c._scan_as_roll = as_roll
    c._batch_frame_selected = False
    c._last_capture_req = capture_req
    c._discover_scanned = MethodType(AppController._discover_scanned, c)
    c._save_rgb_scan_mode = MethodType(AppController._save_rgb_scan_mode, c)
    c._RGB_SCAN_MODE_BY_ROLL_KEY = AppController._RGB_SCAN_MODE_BY_ROLL_KEY
    c._store = store
    return c


def test_first_scan_opens_its_folder_as_a_roll_before_discovery():
    c = _controller(as_roll=True)

    AppController._on_scan_frame_done(c, 1, "/out/Roll001/a.tif")

    roll_id = folder_roll_id_for_path(c.session.repo, "/out/Roll001")
    assert roll_id is not None and c.state.active_roll_id == roll_id
    c._announce_roll_modes.assert_called_once_with(roll_id)
    c.library_cleared.emit.assert_called_once()
    c.request_asset_discovery.assert_called_once_with(
        ["/out/Roll001"], auto_open=True, replace_existing=True, reselect_path="/out/Roll001/a.tif", restore_triplets=None
    )
    assert c._pending_scanned_file is None


def test_later_batch_frames_load_without_taking_the_selection():
    c = _controller(as_roll=True)
    AppController._on_scan_frame_done(c, 1, "/out/Roll001/a.tif")
    c.request_asset_discovery.reset_mock()

    AppController._on_scan_frame_done(c, 2, "/out/Roll001/b.tif")

    c.request_asset_discovery.assert_called_once_with(["/out/Roll001/b.tif"], restore_triplets=None)
    assert c._pending_scanned_file is None
    c.scan_frame_done.emit.assert_called_with(2, "/out/Roll001/b.tif")


def test_batch_end_loads_nothing_more():
    c = _controller(as_roll=True)
    AppController._on_scan_batch_finished(c, ["/out/Roll001/a.tif"])
    c.request_asset_discovery.assert_not_called()
    c.scan_batch_finished.emit.assert_called_once_with(["/out/Roll001/a.tif"])


def test_next_scan_into_the_open_roll_appends():
    c = _controller(as_roll=True)
    roll_id = recognize_folder(c.session.repo, "/out/Roll001")
    c.state.active_roll_id = roll_id

    AppController._on_scan_finished(c, "/out/Roll001/c.tif")

    assert c.state.active_roll_id == roll_id
    c.request_asset_discovery.assert_called_once_with(["/out/Roll001/c.tif"], restore_triplets=None)
    assert c._pending_scanned_file == "/out/Roll001/c.tif"


def test_a_new_roll_name_opens_a_separate_roll():
    c = _controller(as_roll=True)
    first = recognize_folder(c.session.repo, "/out/Roll001")
    c.state.active_roll_id = first

    AppController._on_scan_finished(c, "/out/Roll002/a.tif")

    assert c.state.active_roll_id not in (None, first)
    assert roll_for_id(c.session.repo, first)["extra_paths"] == []
    assert c.request_asset_discovery.call_args.kwargs["replace_existing"] is True


def test_scan_without_as_roll_creates_no_roll():
    c = _controller(as_roll=False)

    AppController._on_scan_finished(c, "/out/a.tif")

    assert saved_rolls(c.session.repo) == {}
    assert c.state.active_roll_id is None
    c.request_asset_discovery.assert_called_once_with(["/out/a.tif"], restore_triplets=None)


def test_capture_triplet_reaches_the_roll_open():
    req = SimpleNamespace(white_mode=False, rgb_mode=True, white_process_mode="auto", roll_name="R1", frame_number=1, as_roll=True)
    c = _controller(capture_req=req)
    paths = ["/hot/R1/r.ARW", "/hot/R1/g.ARW", "/hot/R1/b.ARW"]

    AppController._on_capture_finished(c, paths)

    c.request_asset_discovery.assert_called_once_with(
        ["/hot/R1"],
        auto_open=True,
        replace_existing=True,
        reselect_path=paths[0],
        restore_triplets={paths[0]: paths[1:]},
    )


def test_start_scan_remembers_as_roll():
    c = _controller()
    c._batch_frame_selected = True
    AppController.start_batch(c, SimpleNamespace(as_roll=True))
    assert c._scan_as_roll is True and c._batch_frame_selected is False
    AppController.start_scan(c, SimpleNamespace(as_roll=False))
    assert c._scan_as_roll is False


def test_capture_records_trichrome_mode_on_the_roll_it_lands_in():
    req = SimpleNamespace(white_mode=False, rgb_mode=True, white_process_mode="auto", roll_name="R1", frame_number=1, as_roll=True)
    c = _controller(capture_req=req)
    c._store["rgbscan_mode_by_roll"] = {"other": False}

    AppController._on_capture_finished(c, ["/hot/R1/r.ARW", "/hot/R1/g.ARW", "/hot/R1/b.ARW"])

    roll_id = folder_roll_id_for_path(c.session.repo, "/hot/R1")
    assert c._store["rgbscan_mode_by_roll"] == {"other": False, roll_id: True}
    assert c._store["rgbscan_mode"] is True


def test_capture_without_as_roll_leaves_an_open_roll_in_another_folder_alone():
    req = SimpleNamespace(white_mode=False, rgb_mode=True, white_process_mode="auto", roll_name="R2", frame_number=1, as_roll=False)
    c = _controller(capture_req=req)
    open_roll = recognize_folder(c.session.repo, "/film/Roll1")
    c.state.active_roll_id = open_roll
    c._store["rgbscan_mode_by_roll"] = {open_roll: False}

    AppController._on_capture_finished(c, ["/hot/R2/r.ARW", "/hot/R2/g.ARW", "/hot/R2/b.ARW"])

    assert c._store["rgbscan_mode_by_roll"] == {open_roll: False}
    assert c._store["rgbscan_mode"] is True


def test_capture_without_as_roll_records_on_its_folders_own_roll():
    req = SimpleNamespace(white_mode=True, rgb_mode=False, white_process_mode="auto", roll_name="R1", frame_number=1, as_roll=False)
    c = _controller(capture_req=req)
    roll_id = recognize_folder(c.session.repo, "/hot/R1")
    c._store["rgbscan_mode_by_roll"] = {roll_id: True}

    AppController._on_capture_finished(c, ["/hot/R1/w.ARW"])

    assert c._store["rgbscan_mode_by_roll"] == {roll_id: False}


def test_a_scan_into_another_roll_reads_that_rolls_split_profile():
    from negpy.services.assets.half_frame import save_half_frame_profile

    c = _controller(as_roll=True)
    c.half_frame_profile = MethodType(AppController.half_frame_profile, c)
    roll_id = recognize_folder(c.session.repo, "/out/Roll002")
    save_half_frame_profile(c.session.repo, None, {"split_x": 0.5, "split_axis": "x"})
    save_half_frame_profile(c.session.repo, roll_id, {"split_x": 0.4, "split_axis": "y"})
    c.state.active_roll_id = recognize_folder(c.session.repo, "/out/Roll001")
    seen = []
    c.request_asset_discovery.side_effect = lambda *_a, **_k: seen.append(c.half_frame_profile())

    AppController._on_scan_finished(c, "/out/Roll002/a.tif")

    assert seen == [{"split_x": 0.4, "split_axis": "y"}]


def test_scan_as_roll_reads_the_folders_roll_file_before_the_mode_dates_the_roll(tmp_path):
    from negpy.services.assets import rolls
    from negpy.services.assets.sidecar import RollSidecar, _read_json, plan_roll_file_write, roll_sidecar_path, write_roll_sidecar

    folder = str(tmp_path / "R1")
    os.mkdir(folder)
    write_roll_sidecar(folder, RollSidecar(saved_at=1000.0, name="R1", state={"defaults": {"hue_trim": 2.0}}, trichrome_mode=False))
    req = SimpleNamespace(white_mode=False, rgb_mode=True, white_process_mode="auto", roll_name="R1", frame_number=1, as_roll=True)
    c = _controller(capture_req=req)
    c._pending_roll_offers = {}
    c._read_roll_sidecars = MethodType(AppController._read_roll_sidecars, c)

    AppController._on_capture_finished(c, [os.path.join(folder, n) for n in ("r.ARW", "g.ARW", "b.ARW")])

    roll_id = folder_roll_id_for_path(c.session.repo, folder)
    assert rolls.roll_defaults(c.session.repo, roll_id) == {"hue_trim": 2.0}
    assert rolls.roll_trichrome_mode(c.session.repo, roll_id) is True
    assert rolls.roll_updated_at(c.session.repo, roll_id) > 1000.0
    c._mirror_roll.assert_called_once_with(roll_id)
    written = plan_roll_file_write(c.session.repo, roll_id, _read_json(roll_sidecar_path(folder)))
    assert written["defaults"] == {"hue_trim": 2.0} and written["trichrome_mode"] is True
