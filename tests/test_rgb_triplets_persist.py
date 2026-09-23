"""A grouped RGB-scan roll re-attaches its triplets on the next open instead of reading every raw again."""

import os
import unittest
from unittest.mock import MagicMock, patch

from negpy.desktop.controller import AppController
from negpy.desktop.session import AppState, DesktopSessionManager
from negpy.desktop.workers.render import AssetDiscoveryTask, AssetDiscoveryWorker
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.services.assets.rgb_triplets import TRIPLETS_KEY, remember_triplets, saved_triplets
from negpy.services.rendering.preview_manager import PreviewManager
from tests.test_rgbscan import BLUE, GREEN, RED, _fake_probes


def _repo() -> MagicMock:
    repo = MagicMock(spec=StorageRepository)
    store: dict = {}
    repo.get_global_setting.side_effect = lambda key, default=None: store.get(key, default)
    repo.save_global_setting.side_effect = lambda key, value: store.__setitem__(key, value)
    repo.settings = store
    return repo


def _triplet(red: str, green: str, blue: str, align: bool = True) -> dict:
    return {"name": f"{red} (RGB)", "path": red, "hash": red, "green_path": green, "blue_path": blue, "align": align}


def _loose(path: str) -> dict:
    return {"name": path, "path": path, "hash": path}


def test_opening_another_roll_keeps_the_first_rolls_triplets():
    repo = _repo()
    remember_triplets(repo, [_triplet("/a/r1", "/a/g1", "/a/b1", align=False)])
    remember_triplets(repo, [_loose("/b/x")])
    assert saved_triplets(repo) == {"/a/r1": ["/a/g1", "/a/b1", False]}


def test_a_file_open_loose_drops_the_triplet_that_held_it():
    repo = _repo()
    remember_triplets(repo, [_triplet("/a/r1", "/a/g1", "/a/b1"), _triplet("/a/r2", "/a/g2", "/a/b2")])
    remember_triplets(repo, [_loose("/a/g1"), _triplet("/a/r2", "/a/g2", "/a/b2")])
    assert list(saved_triplets(repo)) == ["/a/r2"]


def test_a_regrouped_file_drops_its_old_triplet():
    repo = _repo()
    remember_triplets(repo, [_triplet("/a/r1", "/a/g1", "/a/b1")])
    remember_triplets(repo, [_triplet("/a/g1", "/a/b1", "/a/r1")])
    assert saved_triplets(repo) == {"/a/g1": ["/a/b1", "/a/r1", True]}


def test_a_file_claimed_twice_goes_to_the_first_triplet(tmp_path):
    names = ("r1", "g1", "b1", "b2")
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    p = {n: str(tmp_path / n) for n in names}
    assets = [_loose(p["r1"]), _loose(p["g1"]), _loose(p["b1"]), _loose(p["b2"])]
    triplets = {p["r1"]: [p["g1"], p["b1"]], p["g1"]: [p["b2"], p["r1"]]}

    out = AssetDiscoveryWorker()._attach_restored_triplets(assets, triplets)
    assert [(a["path"], a.get("green_path")) for a in out] == [(p["r1"], p["g1"]), (p["b2"], None)]


def test_reopening_a_grouped_roll_reads_no_raw(tmp_path, monkeypatch):
    from negpy.features.rgbscan import logic

    names = ["f1_r.raw", "f1_g.raw", "f1_b.raw", "f2_r.raw", "f2_g.raw", "f2_b.raw"]
    for n in names:
        (tmp_path / n).write_bytes(n.encode() * 64)
    _fake_probes(monkeypatch, {n: {"r": RED, "g": GREEN, "b": BLUE}[n[-5]] for n in names})
    probed: list = []
    probe = logic.probe_frame
    monkeypatch.setattr(logic, "probe_frame", lambda path: probed.append(os.path.basename(path)) or probe(path))

    repo = StorageRepository(str(tmp_path / "edits.db"), str(tmp_path / "settings.db"))
    repo.initialize()
    worker = AssetDiscoveryWorker()
    seen: list = []
    worker.finished.connect(seen.append)

    def open_roll() -> list:
        worker.process(
            AssetDiscoveryTask(paths=[str(tmp_path)], supported_extensions=(".raw",), rgb_scan=True, restore_triplets=saved_triplets(repo))
        )
        assets = seen.pop()
        remember_triplets(repo, assets)
        return sorted(a["name"] for a in assets)

    assert open_roll() == ["f1_r (RGB)", "f2_r (RGB)"]
    assert sorted(probed) == sorted(names)
    probed.clear()

    assert open_roll() == ["f1_r (RGB)", "f2_r (RGB)"]
    assert probed == []


class TestDiscoveryReadsTheStore(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock(spec=DesktopSessionManager)
        self.session.state = AppState()
        self.session.repo = _repo()
        self.session.asset_model = MagicMock()
        with (
            patch("negpy.desktop.controller.RenderWorker") as rw,
            patch("negpy.desktop.controller.PreviewManager") as pm,
        ):
            rw.return_value = MagicMock()
            pm.return_value = MagicMock(spec=PreviewManager)
            self.controller = AppController(self.session)
        self.tasks = []
        self.controller.asset_discovery_requested.connect(self.tasks.append)
        self.session.repo.settings[TRIPLETS_KEY] = {"/roll_a/r1": ["/roll_a/g1", "/roll_a/b1", True]}

    def tearDown(self):
        import gc

        for thread in [
            self.controller.render_thread,
            self.controller.export_thread,
            self.controller.thumb_thread,
            self.controller.norm_thread,
            self.controller.discovery_thread,
            self.controller.preview_load_thread,
            self.controller.scan_thread,
        ]:
            if thread is not None and thread.isRunning():
                thread.quit()
                thread.wait()
        del self.controller
        gc.collect()

    def _open(self):
        self.controller.request_asset_discovery(["/roll_a"])
        self.assertEqual(len(self.tasks), 1)
        return self.tasks[0]

    def test_rgb_scan_on_reattaches_saved_triplets(self):
        self.session.repo.settings["rgbscan_mode"] = True
        self.assertEqual(self._open().restore_triplets, {"/roll_a/r1": ["/roll_a/g1", "/roll_a/b1", True]})

    def test_rgb_scan_off_leaves_every_file_loose(self):
        self.assertFalse(self._open().restore_triplets)


def test_restored_triplets_count_as_assembled_in_the_report(tmp_path, monkeypatch):
    """A roll whose triplets all came from the store is not one where nothing assembled."""
    names = ["f1_r.raw", "f1_g.raw", "f1_b.raw", "stray_r.raw"]
    for n in names:
        (tmp_path / n).write_bytes(n.encode() * 64)
    _fake_probes(monkeypatch, {n: {"r": RED, "g": GREEN, "b": BLUE}[n[-5]] for n in names})
    p = {n: str(tmp_path / n) for n in names}

    worker = AssetDiscoveryWorker()
    reports: list = []
    worker.rgb_grouped.connect(reports.append)
    worker.finished.connect(lambda assets: None)
    worker.process(
        AssetDiscoveryTask(
            paths=[str(tmp_path)],
            supported_extensions=(".raw",),
            rgb_scan=True,
            restore_triplets={p["f1_r.raw"]: [p["f1_g.raw"], p["f1_b.raw"], True]},
        )
    )
    assert [(r["made"], r["loose"]) for r in reports] == [(1, 1)]
