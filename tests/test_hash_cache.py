"""The fingerprint cache serves a stored digest only for a file whose stat is unchanged."""

import builtins
import os
import sqlite3
import time
from contextlib import closing

import pytest

from negpy.desktop.workers.render import AssetDiscoveryTask, AssetDiscoveryWorker
from negpy.infrastructure.storage import hash_cache
from negpy.infrastructure.storage.hash_cache import FileHashCache, file_stamp, resolve
from negpy.kernel.image.logic import file_hashes
from negpy.kernel.system.config import APP_CONFIG

MIB = 1024 * 1024
_OLD = time.time() - 3600


def _scan(tmp_path, name: str = "frame.tif", filler: bytes = b"\x01") -> str:
    """A settled 5 MiB file: the interior makes the two digests differ, the head keeps legacy digests apart."""
    path = tmp_path / name
    path.write_bytes(filler * MIB + b"M" * (3 * MIB) + b"T" * MIB)
    os.utime(path, (_OLD, _OLD))
    return str(path)


@pytest.fixture
def cache(tmp_path) -> FileHashCache:
    return FileHashCache(str(tmp_path / "db" / "hash_cache.db"))


def _hashes(cache: FileHashCache, path: str):
    return cache.file_hashes([path], lambda fn: [fn(path)])[0]


def _forbid_hashing(monkeypatch) -> None:
    monkeypatch.setattr(hash_cache, "file_hashes", lambda p: pytest.fail(f"hashed {p}"))


def test_unchanged_file_is_a_hit(tmp_path, cache, monkeypatch):
    path = _scan(tmp_path)
    expected = _hashes(cache, path)
    _forbid_hashing(monkeypatch)

    assert _hashes(cache, path) == expected == file_hashes(path)


def test_hit_returns_the_legacy_digest(tmp_path, cache, monkeypatch):
    path = _scan(tmp_path)
    current, legacy = file_hashes(path)
    _hashes(cache, path)
    _forbid_hashing(monkeypatch)

    assert _hashes(cache, path) == (current, legacy)
    assert legacy and legacy != current


def test_changed_size_is_a_miss(tmp_path, cache):
    path = _scan(tmp_path)
    before = _hashes(cache, path)
    with open(path, "ab") as f:
        f.write(b"more")
    os.utime(path, (_OLD, _OLD))

    assert _hashes(cache, path) == file_hashes(path) != before


def test_changed_mtime_is_a_miss(tmp_path, cache):
    path = _scan(tmp_path)
    before = _hashes(cache, path)
    with open(path, "r+b") as f:
        f.seek(2 * MIB)
        f.write(b"\x02" * 16)
    os.utime(path, (_OLD + 1, _OLD + 1))

    assert _hashes(cache, path) == file_hashes(path) != before


def test_changed_ctime_is_a_miss(tmp_path, cache):
    path = _scan(tmp_path)
    size, mtime_ns, ctime_ns = file_stamp(path)
    stale = ("stale", "stale-legacy")
    cache.store({path: ((size, mtime_ns, ctime_ns - 1), stale)})

    assert _hashes(cache, path) == file_hashes(path)


def test_stat_error_is_a_miss(tmp_path, cache, monkeypatch):
    path = _scan(tmp_path)
    entry = (file_stamp(path), ("stale", "stale-legacy"))

    real_stat = os.stat
    failures = [PermissionError("stat denied")]

    def _flaky(target, *args, **kwargs):
        if target == path and failures:
            raise failures.pop()
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _flaky)
    digest, stamp = resolve(path, entry)

    assert digest == file_hashes(path)
    assert stamp is None


def test_missing_file_is_not_cached(tmp_path, cache):
    path = str(tmp_path / "gone.tif")

    assert _hashes(cache, path)[0].startswith("err_")
    assert cache.load([path]) == {}


def test_recently_written_file_is_not_cached(tmp_path, cache):
    path = _scan(tmp_path)
    os.utime(path, None)

    assert _hashes(cache, path) == file_hashes(path)
    assert cache.load([path]) == {}


def test_file_changed_during_the_hash_is_not_cached(tmp_path, monkeypatch):
    path = _scan(tmp_path)
    real = file_hashes

    def _hash_then_touch(p: str):
        digest = real(p)
        os.utime(p, (_OLD + 5, _OLD + 5))
        return digest

    monkeypatch.setattr(hash_cache, "file_hashes", _hash_then_touch)

    assert resolve(path, None)[1] is None


def test_other_fingerprint_version_is_a_miss(tmp_path, cache, monkeypatch):
    path = _scan(tmp_path)
    _hashes(cache, path)
    monkeypatch.setattr(hash_cache, "FINGERPRINT_VERSION", hash_cache.FINGERPRINT_VERSION + 1)

    assert cache.load([path]) == {}


def test_unusable_database_falls_back_to_hashing(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_bytes(b"")
    cache = FileHashCache(str(blocker / "hash_cache.db"))
    path = _scan(tmp_path)

    assert _hashes(cache, path) == file_hashes(path)


def test_empty_db_path_disables_the_cache(tmp_path):
    path = _scan(tmp_path)

    assert _hashes(FileHashCache(""), path) == file_hashes(path)


def test_entry_is_keyed_on_the_absolute_path(tmp_path, cache, monkeypatch):
    path = _scan(tmp_path)
    _hashes(cache, path)
    monkeypatch.chdir(tmp_path)

    assert "frame.tif" in cache.load(["frame.tif"])
    with closing(sqlite3.connect(cache.db_path)) as conn:
        assert conn.execute("SELECT path FROM file_hashes").fetchall() == [(os.path.abspath(path),)]


def _discover(folder) -> list[dict]:
    worker = AssetDiscoveryWorker()
    found: list[list] = []
    worker.finished.connect(found.append)
    worker.process(AssetDiscoveryTask(paths=[str(folder)], supported_extensions=(".tif",)))
    return found[0]


def test_discovery_does_not_open_a_file_with_a_valid_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(APP_CONFIG, "hash_cache_db_path", str(tmp_path / "hash_cache.db"))
    roll = tmp_path / "roll"
    roll.mkdir()
    paths = [_scan(roll, f"frame{i}.tif", bytes([i + 1])) for i in range(3)]
    first = _discover(roll)

    real_open = builtins.open

    def _guarded(file, *args, **kwargs):
        if str(file) in paths:
            raise AssertionError(f"opened {file}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded)
    second = _discover(roll)

    assert [(a["path"], a["hash"], a["legacy_hash"]) for a in second] == [(a["path"], a["hash"], a["legacy_hash"]) for a in first]
    assert sorted(a["path"] for a in second) == sorted(paths)
    assert all(a["legacy_hash"] and a["legacy_hash"] != a["hash"] for a in second)


def test_discovery_rehashes_a_file_replaced_under_the_same_name(tmp_path, monkeypatch):
    monkeypatch.setattr(APP_CONFIG, "hash_cache_db_path", str(tmp_path / "hash_cache.db"))
    roll = tmp_path / "roll"
    roll.mkdir()
    path = _scan(roll, "frame.tif", b"\x01")
    before = _discover(roll)[0]["hash"]

    _scan(roll, "frame.tif", b"\x02")
    os.utime(path, (_OLD + 60, _OLD + 60))

    assert _discover(roll)[0]["hash"] == file_hashes(path)[0] != before
