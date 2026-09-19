"""Per-machine cache of file fingerprints, keyed on absolute path and stat.

The fingerprint is the identity of an edit, so a hit must be as good as a re-hash. An entry is
served only while size, mtime_ns and ctime_ns all match, and is stored only for a file that
did not change during the hash and whose mtime is older than ``_SETTLE_NS``. A file replaced
in place with the same size, mtime_ns and ctime_ns keeps its old fingerprint.

Only file fingerprints are stored. Half-frame and composite hashes derive from them.
"""

import os
import sqlite3
import time
from contextlib import closing
from typing import Callable, Iterable, Optional

from negpy.kernel.image.logic import FINGERPRINT_VERSION, file_hashes
from negpy.kernel.system.logging import get_logger

logger = get_logger(__name__)

Stamp = tuple[int, int, int]  # size, mtime_ns, ctime_ns
Digest = tuple[str, str]  # current, legacy

# A write inside the timestamp granularity of the file system leaves the stat unchanged.
# FAT and some SMB servers round mtime to 2 s; the margin covers client/server clock skew.
_SETTLE_NS = 10 * 1_000_000_000
# Under SQLite's default bound-parameter limit.
_LOOKUP_CHUNK = 500


def file_stamp(path: str) -> Optional[Stamp]:
    """Stat triple for ``path``, or None when the file cannot be stat'ed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns, st.st_ctime_ns


class FileHashCache:
    """SQLite-backed ``{path: (stamp, digests)}``. Every database error reads as a miss."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_hashes (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                version INTEGER NOT NULL,
                hash TEXT NOT NULL,
                legacy_hash TEXT NOT NULL
            )
        """)
        return conn

    def load(self, paths: Iterable[str]) -> dict[str, tuple[Stamp, Digest]]:
        """Stored entries for ``paths``, unvalidated. :meth:`resolve` checks them against the file."""
        if not self.db_path:
            return {}
        keys = {os.path.abspath(p): p for p in paths}
        names = list(keys)
        out: dict[str, tuple[Stamp, Digest]] = {}
        try:
            with closing(self._connect()) as conn:
                for i in range(0, len(names), _LOOKUP_CHUNK):
                    chunk = names[i : i + _LOOKUP_CHUNK]
                    marks = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT path, size, mtime_ns, ctime_ns, hash, legacy_hash FROM file_hashes "
                        f"WHERE version = ? AND path IN ({marks})",
                        [FINGERPRINT_VERSION, *chunk],
                    )
                    for path, size, mtime_ns, ctime_ns, f_hash, legacy in rows:
                        out[keys[path]] = ((size, mtime_ns, ctime_ns), (f_hash, legacy))
        except (sqlite3.Error, OSError) as e:
            logger.warning(f"Hash cache read failed: {e}")
            return {}
        return out

    def store(self, entries: dict[str, tuple[Stamp, Digest]]) -> None:
        if not self.db_path or not entries:
            return
        rows = [(os.path.abspath(path), *stamp, FINGERPRINT_VERSION, *digest) for path, (stamp, digest) in entries.items()]
        try:
            with closing(self._connect()) as conn, conn:
                conn.executemany("INSERT OR REPLACE INTO file_hashes VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        except (sqlite3.Error, OSError) as e:
            logger.warning(f"Hash cache write failed: {e}")

    def file_hashes(self, paths: list[str], run: Callable[[Callable[[str], object]], list]) -> list[Optional[Digest]]:
        """Digests in ``paths`` order. ``run(fn)`` maps ``fn`` over ``paths`` and gives None for a failed file."""
        cached = self.load(paths)
        resolved = run(lambda p: resolve(p, cached.get(p)))
        self.store({p: (r[1], r[0]) for p, r in zip(paths, resolved) if r is not None and r[1] is not None})
        return [r[0] if r is not None else None for r in resolved]


def resolve(path: str, entry: Optional[tuple[Stamp, Digest]]) -> tuple[Digest, Optional[Stamp]]:
    """(digests, stamp to store). The stamp is None on a hit and when the hash must not be cached.

    Touches no database, so a thread pool can run it per file.
    """
    before = file_stamp(path)
    if before is not None and entry is not None and entry[0] == before:
        return entry[1], None
    digest = file_hashes(path)
    if before is None or digest[0].startswith("err_") or file_stamp(path) != before:
        return digest, None
    if time.time_ns() - before[1] < _SETTLE_NS:
        return digest, None
    return digest, before
