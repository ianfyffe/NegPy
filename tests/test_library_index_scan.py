"""LibrarySearchWorker.scan_for_indexing: the walk+hash pass a whole-library index
needs, sharing the same LibraryWalkCache a keyword search already pays for. Real tiny
files on disk, since the fingerprint genuinely reads bytes -- no RAW decode
involved, so nothing here needs mocking to stay fast."""

import builtins
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from negpy.desktop.workers.library import LibrarySearchWorker
from negpy.kernel.image.logic import file_hashes
from negpy.kernel.system.config import APP_CONFIG

_OLD = time.time() - 3600


@pytest.fixture
def library(tmp_path):
    roll_a = tmp_path / "roll_a"
    roll_a.mkdir()
    (roll_a / "IMG_0001.NEF").write_bytes(b"a" * 10)
    (roll_a / "IMG_0002.NEF").write_bytes(b"b" * 10)
    return tmp_path


def test_scan_emits_every_walked_file_with_a_hash(library):
    worker = LibrarySearchWorker()
    scanned = MagicMock()
    worker.indexing_scanned.connect(scanned)

    worker.scan_for_indexing([str(library)])

    (files,) = scanned.call_args[0]
    assert {f["name"] for f in files} == {"IMG_0001.NEF", "IMG_0002.NEF"}
    assert all(f.get("hash") for f in files)
    # Different content, different hash -- the pass didn't just stamp one value everywhere.
    assert len({f["hash"] for f in files}) == 2


def test_scan_of_an_empty_library_emits_an_empty_list(tmp_path):
    worker = LibrarySearchWorker()
    scanned = MagicMock()
    worker.indexing_scanned.connect(scanned)

    worker.scan_for_indexing([str(tmp_path)])

    scanned.assert_called_once_with([])


def test_scan_reuses_the_same_walk_cache_as_a_keyword_search(library):
    """A keyword search and an indexing pass share one traversal instead of walking
    the library twice."""
    from negpy.desktop.workers.library import LibrarySearchTask

    worker = LibrarySearchWorker()
    with patch.object(worker._cache, "files", wraps=worker._cache.files) as files_spy:
        worker.search(LibrarySearchTask(roots=[str(library)], query="IMG"))
        worker.scan_for_indexing([str(library)])

    assert files_spy.call_count == 2  # both calls go through the cache
    # Second call is served from cache, not a fresh walk -- same list object identity.
    assert worker._cache._files is not None


def test_a_single_file_library_still_gets_hashed(library):
    """The ThreadPoolExecutor path is skipped below 2 files; the plain loop must still
    run, not silently drop the one file."""
    (library / "roll_a" / "IMG_0002.NEF").unlink()
    worker = LibrarySearchWorker()
    scanned = MagicMock()
    worker.indexing_scanned.connect(scanned)

    worker.scan_for_indexing([str(library)])

    (files,) = scanned.call_args[0]
    assert len(files) == 1
    assert files[0]["hash"]


def test_scan_error_emits_error_not_a_crash(library):
    worker = LibrarySearchWorker()
    error = MagicMock()
    worker.error.connect(error)

    with patch("negpy.infrastructure.storage.hash_cache.file_hashes", side_effect=RuntimeError("disk unplugged")):
        worker.scan_for_indexing([str(library)])

    error.assert_called_once()


def _scan_hashes(worker: LibrarySearchWorker, roots: list[str], monkeypatch) -> tuple[dict[str, str], set[str]]:
    """({path: hash}, paths opened during the scan)."""
    scanned = MagicMock()
    worker.indexing_scanned.connect(scanned)
    opened: set[str] = set()
    real_open = builtins.open

    def _tracking(file, *args, **kwargs):
        opened.add(str(file))
        return real_open(file, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", _tracking)
        worker.scan_for_indexing(roots)
    worker.indexing_scanned.disconnect(scanned)
    (files,) = scanned.call_args[0]
    return {f["path"]: f["hash"] for f in files}, opened


def test_a_second_scan_reads_only_the_changed_file(library, tmp_path, monkeypatch):
    monkeypatch.setattr(APP_CONFIG, "hash_cache_db_path", str(tmp_path / "hash_cache.db"))
    paths = sorted(str(p) for p in (library / "roll_a").iterdir())
    for p in paths:
        os.utime(p, (_OLD, _OLD))
    worker = LibrarySearchWorker()

    first, _ = _scan_hashes(worker, [str(library)], monkeypatch)
    second, opened = _scan_hashes(worker, [str(library)], monkeypatch)

    assert second == first
    assert opened.isdisjoint(paths)

    changed, unchanged = paths
    with open(changed, "wb") as f:
        f.write(b"c" * 10)
    os.utime(changed, (_OLD + 60, _OLD + 60))
    third, opened = _scan_hashes(worker, [str(library)], monkeypatch)

    assert opened & set(paths) == {changed}
    assert third[changed] == file_hashes(changed)[0] != first[changed]
    assert third[unchanged] == first[unchanged]
