"""Dot-files are never images. macOS writes an AppleDouble `._<name>` beside every file on SMB,
exFAT and FAT volumes, with the image's own extension, so every walk must skip them by name."""

import os

from negpy.desktop.workers.render import AssetDiscoveryTask, AssetDiscoveryWorker
from negpy.infrastructure.filesystem.watcher import FolderWatchService
from negpy.infrastructure.loaders.constants import is_hidden_path
from negpy.services.assets.library import folder_counts, iter_library_files


def _roll(tmp_path):
    for name in ("frame1.nef", "frame2.nef"):
        (tmp_path / name).write_bytes(name.encode() * 64)
        (tmp_path / f"._{name}").write_bytes(b"\x00\x05\x16\x07" * 16)
    return tmp_path


def _discover(paths):
    worker = AssetDiscoveryWorker()
    seen: list = []
    worker.finished.connect(seen.append)
    worker.process(AssetDiscoveryTask(paths=paths, supported_extensions=(".nef",)))
    return sorted(a["name"] for a in seen.pop())


def test_is_hidden_path():
    assert is_hidden_path("/roll/._frame1.nef")
    assert is_hidden_path(".DS_Store")
    assert not is_hidden_path("/roll/.hidden_dir/frame1.nef")
    assert not is_hidden_path("/roll/frame1.nef")


def test_folder_discovery_skips_appledouble_files(tmp_path):
    assert _discover([str(_roll(tmp_path))]) == ["frame1.nef", "frame2.nef"]


def test_explicit_appledouble_path_is_skipped(tmp_path):
    roll = _roll(tmp_path)
    assert _discover([str(roll / "frame1.nef"), str(roll / "._frame1.nef")]) == ["frame1.nef"]


def test_library_walk_and_count_skip_appledouble_files(tmp_path):
    roll = _roll(tmp_path)
    assert sorted(f["name"] for f in iter_library_files([str(roll)])) == ["frame1.nef", "frame2.nef"]
    assert folder_counts(str(roll)) == (2, 0)


def test_hot_folder_skips_appledouble_files(tmp_path):
    roll = _roll(tmp_path)
    found = FolderWatchService.scan_for_new_files(str(roll), set())
    assert sorted(os.path.basename(p) for p in found) == ["frame1.nef", "frame2.nef"]
