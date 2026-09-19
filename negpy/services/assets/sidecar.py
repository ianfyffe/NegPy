"""``.negpy`` edit sidecars: a plain-file copy of one frame's edit next to its source.

Format 2 is an envelope: ``saved_at`` (the DB row's ``updated_at``, so a sidecar this
machine mirrored compares equal to its row, not newer), ``source_hash``, ``mark``, the
``edit`` (flat config) and named ``work_prints``. A format-1 file is a bare flat config
and has no timestamp, so it only ever fills a DB miss.
"""

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, NamedTuple, Optional

from negpy.domain.models import WorkspaceConfig
from negpy.kernel.system.logging import get_logger
from negpy.services.assets.rolls import unforked_hash

logger = get_logger(__name__)

SIDECAR_EXT = ".negpy"
SIDECAR_FORMAT = 2
_MARKS = ("keeper", "excluded")
DECLINED_KEY = "sidecar_offers_declined"


class SidecarWorkPrint(NamedTuple):
    created_at: float
    config: WorkspaceConfig


@dataclass(frozen=True)
class Sidecar:
    """One frame's sidecar. ``saved_at`` is None for a format-1 file."""

    config: WorkspaceConfig
    saved_at: Optional[float] = None
    source_hash: str = ""
    mark: Optional[str] = None
    work_prints: Dict[str, SidecarWorkPrint] = field(default_factory=dict)


def sidecar_path_for(source_path: str, half: int = 0) -> str:
    """Sidecar path next to the source file: ``<basename>.negpy`` (``<basename>.<half>.negpy`` for half-frame assets)."""
    base = os.path.splitext(os.path.basename(source_path))[0]
    suffix = f".{half}" if half else ""
    return os.path.join(os.path.dirname(source_path), base + suffix + SIDECAR_EXT)


def write_sidecar(source_path: str, sidecar: Sidecar, half: int = 0) -> str:
    """Write the sidecar as JSON next to the source, atomically. Returns the path written."""
    path = sidecar_path_for(source_path, half)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps(_to_payload(sidecar), default=str, indent=2)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path), delete=False, suffix=".part", encoding="utf-8") as tmp:
            tmp_path = tmp.name
            tmp.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return path


def _to_payload(sidecar: Sidecar) -> Dict[str, Any]:
    return {
        "sidecar_format": SIDECAR_FORMAT,
        "saved_at": sidecar.saved_at,
        "source_hash": sidecar.source_hash,
        "mark": sidecar.mark,
        "edit": sidecar.config.to_dict(),
        "work_prints": {name: {"created_at": wp.created_at, "edit": wp.config.to_dict()} for name, wp in sidecar.work_prints.items()},
    }


def _from_payload(data: Dict[str, Any]) -> Optional[Sidecar]:
    if "sidecar_format" not in data:
        return Sidecar(config=WorkspaceConfig.from_flat_dict(data))
    edit = data.get("edit")
    if not isinstance(edit, dict):
        return None
    saved_at = data.get("saved_at")
    mark = data.get("mark")
    work_prints: Dict[str, SidecarWorkPrint] = {}
    for name, entry in (data.get("work_prints") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("edit"), dict):
            try:
                work_prints[str(name)] = SidecarWorkPrint(
                    float(entry.get("created_at") or 0.0), WorkspaceConfig.from_flat_dict(entry["edit"])
                )
            except Exception as exc:
                logger.warning("Skipping work print %r in sidecar: %s", name, exc)
    return Sidecar(
        config=WorkspaceConfig.from_flat_dict(edit),
        saved_at=float(saved_at) if isinstance(saved_at, (int, float)) else None,
        source_hash=str(data.get("source_hash") or ""),
        mark=mark if mark in _MARKS else None,
        work_prints=work_prints,
    )


def load_sidecar(source_path: str, half: int = 0) -> Optional[Sidecar]:
    """The sidecar next to the source file. None if absent or malformed."""
    path = sidecar_path_for(source_path, half)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f_in:
            data = json.load(f_in)
        if not isinstance(data, dict):
            return None
        return _from_payload(data)
    except Exception as exc:
        logger.warning("Failed to load sidecar %s: %s", path, exc)
        return None


def sidecar_from_repo(repo, file_hash: str) -> Optional[Sidecar]:
    """This hash's saved edit, mark and work prints as a sidecar. None with no saved edit."""
    record = repo.load_file_record(file_hash)
    if record is None:
        return None
    config, updated_at = record
    return Sidecar(
        config=config,
        saved_at=updated_at,
        source_hash=file_hash,
        mark=repo.load_file_mark(file_hash),
        work_prints={name: SidecarWorkPrint(created_at, cfg) for name, created_at, cfg in repo.load_work_prints(file_hash)},
    )


def promote_sidecar(repo, file_hash: str, source_path: str, sidecar: Sidecar) -> WorkspaceConfig:
    """Make the sidecar this hash's saved edit. Work prints merge by name; local ones stay."""
    repo.save_file_settings(file_hash, sidecar.config, file_path=source_path, updated_at=sidecar.saved_at or time.time())
    if sidecar.saved_at is not None:
        repo.save_file_mark(file_hash, sidecar.mark, file_path=source_path)
    for name, wp in sidecar.work_prints.items():
        repo.save_work_print(file_hash, name, wp.config, created_at=wp.created_at or None)
    return sidecar.config


def newer_sidecar(repo, file_hash: str, source_path: str, half: int = 0) -> Optional[Sidecar]:
    """A format-2 sidecar saved after this hash's DB row, else None.

    A DB miss is not "newer": ``load_or_promote`` fills it without asking.
    """
    sidecar = load_sidecar(source_path, half)
    if sidecar is None or sidecar.saved_at is None:
        return None
    record = repo.load_file_record(file_hash)
    if record is None or sidecar.saved_at <= record[1]:
        return None
    return sidecar


class SidecarOffer(NamedTuple):
    asset: Dict[str, Any]
    sidecar: Sidecar


def _is_composite(asset: Dict[str, Any]) -> bool:
    return bool(asset.get("hdr_paths") or asset.get("stitch_paths"))


def pending_sidecar_offers(repo, assets) -> list[SidecarOffer]:
    """Frames whose sidecar was saved after their edit here, minus declined versions."""
    declined = repo.get_global_setting(DECLINED_KEY, default=None) or {}
    offers = []
    for asset in assets:
        file_hash, path = asset.get("hash") or "", asset.get("path") or ""
        if not file_hash or not path or _is_composite(asset) or unforked_hash(file_hash) != file_hash:
            continue
        sidecar = newer_sidecar(repo, file_hash, path, half=int(asset.get("half") or 0))
        if sidecar is not None and declined.get(file_hash) != sidecar.saved_at:
            offers.append(SidecarOffer(asset, sidecar))
    return offers


def decline_sidecar_offers(repo, offers) -> None:
    """Remember these sidecar versions as declined; a later save to the file asks again."""
    if not offers:
        return
    declined = dict(repo.get_global_setting(DECLINED_KEY, default=None) or {})
    for offer in offers:
        declined[offer.asset["hash"]] = offer.sidecar.saved_at
    repo.save_global_setting(DECLINED_KEY, declined)


def promote_unedited(repo, assets) -> list[str]:
    """Load the sidecar of every frame with no edit here. Returns the hashes filled.

    Skips what ``load_or_promote`` resolves without a sidecar: a saved row, a path match,
    a composite.
    """
    filled = []
    for asset in assets:
        file_hash, path = asset.get("hash") or "", asset.get("path") or ""
        if not file_hash or not path or _is_composite(asset) or unforked_hash(file_hash) != file_hash:
            continue
        half = int(asset.get("half") or 0)
        if repo.load_file_record(file_hash) is not None:
            continue
        if not half and repo.load_file_settings_by_path(path) is not None:
            continue
        sidecar = load_sidecar(path, half)
        if sidecar is None:
            continue
        promote_sidecar(repo, file_hash, path, sidecar)
        filled.append(file_hash)
    return filled


def load_or_promote(
    repo, file_hash: str, source_path: str, half: int = 0, composite: bool = False, forked: bool = False
) -> Optional[WorkspaceConfig]:
    """DB first; on miss, try path-based fallback (handles EXIF-modified files),
    then fall back to sidecar. Re-homes on successful path match.

    Both fallbacks are keyed by path, so they only apply where the hash *is* the identity
    of the file at that path. Three kinds of asset break that:

    - a **half-frame**, which shares its path with the other half; a path match would
      steal the sibling's edit.
    - a **composite** (HDR merge, stitch), whose path is its reference frame's. A path
      match hands the composite that frame's whole edit — its rotation, its film process
      — and `rehome_file_settings` then *moves* the row, so the source frame loses its
      own edit. That is how a merge of five slides opened rotated and in the wrong mode.
    - a **roll-forked edit**, which shares its path with the roll's shared edit. A path
      match would hand the fork the shared edit, and rehoming it would delete the shared
      row out from under every other roll still using it.

    A composite or a fork skips the sidecar too: the `.negpy` beside the source describes
    the shared frame, not a variant of it.
    """
    cfg = repo.load_file_settings(file_hash)
    if cfg is not None:
        return cfg

    if not half and not composite and not forked:
        path_result = repo.load_file_settings_by_path(source_path)
        if path_result is not None:
            old_hash, cfg = path_result
            repo.rehome_file_settings(old_hash, file_hash, source_path)
            return cfg

    if composite or forked:
        return None

    # Sidecar fallback
    sidecar = load_sidecar(source_path, half)
    if sidecar is None:
        return None
    return promote_sidecar(repo, file_hash, source_path, sidecar)


class SidecarMirror:
    """Keeps each edited frame's sidecar equal to its DB row.

    Frames are marked dirty as they are saved and written in one flush, so a slider drag
    costs one file write, not one per tick. The flush reads the row back from the repo,
    which is what makes the file match the DB rather than the in-flight config.
    """

    def __init__(self, repo) -> None:
        self._repo = repo
        self._dirty: Dict[str, tuple[str, int]] = {}
        self._unwritable_dirs: set[str] = set()

    def mark_dirty(self, file_hash: str, source_path: str, half: int = 0) -> None:
        # The sidecar describes the shared edit; a roll fork of it never writes there.
        if file_hash and source_path and unforked_hash(file_hash) == file_hash:
            self._dirty[file_hash] = (source_path, half)

    def pending(self) -> int:
        return len(self._dirty)

    def flush(self) -> tuple[int, int]:
        """Write every dirty frame. Returns (written, failed); a folder that refuses a
        write is skipped for the rest of the session."""
        pending, self._dirty = self._dirty, {}
        written = failed = 0
        for file_hash, (source_path, half) in pending.items():
            if os.path.dirname(source_path) in self._unwritable_dirs:
                continue
            sidecar = sidecar_from_repo(self._repo, file_hash)
            if sidecar is None:
                continue
            try:
                write_sidecar(source_path, sidecar, half=half)
                written += 1
            except OSError as exc:
                failed += 1
                self._unwritable_dirs.add(os.path.dirname(source_path))
                logger.warning("Sidecar write failed for %s, skipping that folder: %s", source_path, exc)
        return written, failed
