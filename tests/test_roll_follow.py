"""A folder renamed on one computer: another computer on the same share, mounted at another
path, follows it by the former names in the folder's roll file instead of making a new roll."""

import gc
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from negpy.desktop.controller import AppController
from negpy.desktop.session import AppState, DesktopSessionManager
from negpy.domain.models import WorkspaceConfig
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.services.assets import composites, follow, rolls
from negpy.services.assets.sidecar import SidecarMirror, read_roll_sidecar
from negpy.services.assets.triplets import remember_triplets, saved_triplets
from negpy.services.rendering.preview_manager import PreviewManager


def _repo(root):
    repo = StorageRepository(str(root / "edits.db"), str(root / "settings.db"))
    repo.initialize()
    return repo


@pytest.fixture
def share(tmp_path):
    """The share's 2026 folder as pc1 (…/pc1/mnt/nas) and pc2 (…/pc2/Volumes/share) mount it."""
    nas = tmp_path / "nas"
    (nas / "2026" / "roll1").mkdir(parents=True)
    (nas / "2026" / "roll1" / "a.tif").write_bytes(b"scan")
    mounts = []
    for name, mount in (("pc1", "mnt/nas"), ("pc2", "Volumes/share")):
        link = tmp_path / name / mount
        link.parent.mkdir(parents=True)
        os.symlink(nas, link)
        mounts.append(os.path.join(str(link), "2026"))
    return SimpleNamespace(tmp=tmp_path, pc1=mounts[0], pc2=mounts[1])


@pytest.fixture
def pc2(share):
    repo = _repo(share.tmp / "pc2")
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
    controller.request_asset_discovery = MagicMock()
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


@pytest.fixture
def pc1(share):
    """pc1 holds the roll with Keep Current on; its roll file is on the share."""
    repo = _repo(share.tmp / "pc1")
    roll_id = rolls.recognize_folder(repo, os.path.join(share.pc1, "roll1"))
    rolls.set_roll_defaults(repo, roll_id, hue_trim=2.0)
    SidecarMirror(repo).flush([os.path.join(share.pc1, "roll1")])
    return SimpleNamespace(repo=repo, roll_id=roll_id)


def _rename_on_pc1(pc1, share, new_name: str) -> None:
    """Rename with Also rename the folder on disk."""
    rolls.rename_folder_roll_disk(pc1.repo, pc1.roll_id, new_name)
    SidecarMirror(pc1.repo).flush([os.path.join(share.pc1, new_name)])


def _known_on_pc2(ctl, share) -> str:
    repo = ctl.session.repo
    old = os.path.join(share.pc2, "roll1")
    roll_id = rolls.recognize_folder(repo, old)
    read_roll_sidecar(repo, roll_id)
    a = os.path.join(old, "a.tif")
    repo.save_file_settings("ha", WorkspaceConfig(), file_path=a)
    repo.save_global_setting(composites.COMPOSITES_KEY, {a: {"kind": "stitch", "paths": [a, os.path.join(old, "b.tif")], "hash": "hs"}})
    green, blue = os.path.join(old, "g.tif"), os.path.join(old, "b.tif")
    remember_triplets(repo, [{"path": a, "hash": "ha", "green_path": green, "blue_path": blue, "green_hash": "hg", "blue_hash": "hb"}])
    return roll_id


def test_a_folder_renamed_on_another_computer_is_followed(pc1, pc2, share):
    roll_id = _known_on_pc2(pc2, share)
    _rename_on_pc1(pc1, share, "roll1-beach")
    new = os.path.join(share.pc2, "roll1-beach")

    pc2.open_library_folders([new])

    repo = pc2.session.repo
    assert rolls.folder_roll_id_for_path(repo, new) == roll_id
    assert len(rolls.saved_rolls(repo)) == 1
    assert rolls.roll_for_id(repo, roll_id)["name"] == "roll1-beach"
    assert repo.load_file_settings_by_path(os.path.join(new, "a.tif"))[0] == "ha"
    assert list(composites.saved_composites(repo)) == [os.path.join(new, "a.tif")]
    assert list(saved_triplets(repo)) == [os.path.join(new, "a.tif")]
    assert read_roll_sidecar(repo, roll_id) is None
    pc2.session.rehome_folder_paths.assert_called_once_with(os.path.join(share.pc2, "roll1"), new)


def test_a_library_refresh_follows_a_missing_folder(pc1, pc2, share):
    roll_id = _known_on_pc2(pc2, share)
    _rename_on_pc1(pc1, share, "roll1-beach")

    pc2.rediscover_rolls()

    assert rolls.roll_for_id(pc2.session.repo, roll_id)["folder_path"] == os.path.join(share.pc2, "roll1-beach")


def test_a_folder_whose_old_path_still_exists_is_not_followed(pc1, pc2, share):
    roll_id = _known_on_pc2(pc2, share)
    _rename_on_pc1(pc1, share, "roll1-beach")
    os.mkdir(os.path.join(share.pc2, "roll1"))

    assert follow.roll_moved_to(pc2.session.repo, os.path.join(share.pc2, "roll1-beach")) is None
    assert follow.moved_folder(pc2.session.repo, roll_id) is None


def test_a_former_name_at_another_parent_is_not_followed(pc1, pc2, share):
    repo = pc2.session.repo
    elsewhere = share.tmp / "pc2" / "archive" / "roll1"
    roll_id = rolls.recognize_folder(repo, str(elsewhere))
    _rename_on_pc1(pc1, share, "roll1-beach")

    assert follow.roll_moved_to(repo, os.path.join(share.pc2, "roll1-beach")) is None
    assert follow.moved_folder(repo, roll_id) is None
    pc2.open_library_folders([os.path.join(share.pc2, "roll1-beach")])
    assert rolls.roll_for_id(repo, roll_id)["folder_path"] == str(elsewhere)
    assert len(rolls.saved_rolls(repo)) == 2


def test_a_disk_rename_records_the_former_names_newest_last(tmp_path):
    repo = _repo(tmp_path)
    (tmp_path / "a").mkdir()
    roll_id = rolls.recognize_folder(repo, str(tmp_path / "a"))
    for name in ("b", "c", "a"):
        rolls.rename_folder_roll_disk(repo, roll_id, name)

    assert rolls.roll_for_id(repo, roll_id)["former_names"] == ["b", "c"]


def test_a_roll_with_no_state_here_still_takes_the_followed_folders_file(pc2, share, tmp_path):
    repo = pc2.session.repo
    roll_id = rolls.recognize_folder(repo, os.path.join(share.pc2, "roll1"))
    pc1 = _repo(tmp_path / "pc1")
    other = rolls.recognize_folder(pc1, os.path.join(share.pc1, "roll1"))
    rolls.set_roll_defaults(pc1, other, hue_trim=2.0)
    rolls.rename_folder_roll_disk(pc1, other, "roll1-beach")
    SidecarMirror(pc1).flush([os.path.join(share.pc1, "roll1-beach")])

    pc2.open_library_folders([os.path.join(share.pc2, "roll1-beach")])

    assert read_roll_sidecar(repo, roll_id) is None
    assert rolls.roll_defaults(repo, roll_id) == {"hue_trim": 2.0}
    assert rolls.roll_for_id(repo, roll_id)["former_names"] == ["roll1"]


def test_a_disk_rename_writes_the_former_name_to_an_existing_roll_file(pc1, pc2, share):
    repo = pc2.session.repo
    roll_id = rolls.recognize_folder(repo, os.path.join(share.pc2, "roll1"))
    read_roll_sidecar(repo, roll_id)

    assert pc2.request_rename_roll(roll_id, "roll1-beach", rename_folder=True)

    assert follow.former_names(os.path.join(share.pc2, "roll1-beach")) == ["roll1"]
