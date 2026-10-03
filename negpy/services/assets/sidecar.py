"""``.negpy`` edit sidecars: a plain-file copy of one frame's edit next to its source.

Format 2 is an envelope: ``saved_at`` (the DB row's ``updated_at``, so a sidecar this
machine mirrored compares equal to its row, not newer), ``source_hash``, ``mark`` and
``mark_at`` (the mark's own time), the ``edit`` (flat config), named ``work_prints`` with
their ``updated_at`` and ``deleted_work_prints`` (name: deletion time). A frame with a mark
or work prints but no saved edit has a null ``edit`` and ``saved_at``. A file without
``mark_at`` dates a mark by ``saved_at`` and carries no clear. A format-1 file is a bare
flat config and has no timestamp, so it only ever fills a DB miss.
"""

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, NamedTuple, Optional

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
    updated_at: Optional[float] = None

    @property
    def stamp(self) -> float:
        """When the print was last saved or renamed, which its name's tombstone is compared against."""
        return self.updated_at if self.updated_at is not None else self.created_at


@dataclass(frozen=True)
class Sidecar:
    """One frame's sidecar. ``config`` and ``saved_at`` are None without a saved edit;
    ``saved_at`` and ``mark_at`` are None for a format-1 file."""

    config: Optional[WorkspaceConfig]
    saved_at: Optional[float] = None
    source_hash: str = ""
    mark: Optional[str] = None
    mark_at: Optional[float] = None
    work_prints: Dict[str, SidecarWorkPrint] = field(default_factory=dict)
    deleted_work_prints: Dict[str, float] = field(default_factory=dict)

    @property
    def work_print_stamps(self) -> Dict[str, float]:
        return {name: wp.stamp for name, wp in self.work_prints.items()}

    def work_print(self, name: str) -> Optional[SidecarWorkPrint]:
        return self.work_prints.get(name)


@dataclass(frozen=True)
class SidecarScan:
    """What a sidecar says about times, the mark and work-print names, read without building
    a config. ``sidecar()`` and ``work_print()`` build from the raw JSON it keeps."""

    saved_at: Optional[float]
    mark: Optional[str]
    mark_at: Optional[float]
    work_print_stamps: Dict[str, float]
    deleted_work_prints: Dict[str, float]
    edit: Optional[Dict[str, Any]] = field(default=None, repr=False, compare=False)
    prints: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False, compare=False)
    source_hash: str = ""

    @property
    def has_edit(self) -> bool:
        return self.edit is not None

    def work_print(self, name: str) -> Optional[SidecarWorkPrint]:
        entry = self.prints.get(name)
        if entry is None:
            return None
        try:
            updated = entry.get("updated_at")
            return SidecarWorkPrint(
                float(entry.get("created_at") or 0.0),
                WorkspaceConfig.from_flat_dict(entry["edit"]),
                float(updated) if isinstance(updated, (int, float)) else None,
            )
        except Exception as exc:
            logger.warning("Skipping work print %r in sidecar: %s", name, exc)
            return None

    def sidecar(self) -> Sidecar:
        """The whole sidecar, every config built."""
        work_prints = {name: wp for name in self.prints if (wp := self.work_print(name)) is not None}
        return Sidecar(
            config=WorkspaceConfig.from_flat_dict(self.edit) if self.edit is not None else None,
            saved_at=self.saved_at,
            source_hash=self.source_hash,
            mark=self.mark,
            mark_at=self.mark_at,
            work_prints=work_prints,
            deleted_work_prints=dict(self.deleted_work_prints),
        )


def sidecar_path_for(source_path: str, half: int = 0) -> str:
    """Sidecar path next to the source file: ``<basename>.negpy`` (``<basename>.<half>.negpy`` for half-frame assets)."""
    base = os.path.splitext(os.path.basename(source_path))[0]
    suffix = f".{half}" if half else ""
    return os.path.join(os.path.dirname(source_path), base + suffix + SIDECAR_EXT)


def write_sidecar(source_path: str, sidecar: Sidecar, half: int = 0) -> str:
    """Write the sidecar as JSON next to the source, atomically. Returns the path written."""
    path = sidecar_path_for(source_path, half)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_json(path, _to_payload(sidecar))
    return path


def _write_json(path: str, data: Dict[str, Any]) -> None:
    payload = json.dumps(data, default=str, indent=2)
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


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f_in:
            data = json.load(f_in)
    except Exception as exc:
        logger.warning("Failed to load sidecar %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _to_payload(sidecar: Sidecar) -> Dict[str, Any]:
    return {
        "sidecar_format": SIDECAR_FORMAT,
        "saved_at": sidecar.saved_at,
        "source_hash": sidecar.source_hash,
        "mark": sidecar.mark,
        "mark_at": sidecar.mark_at,
        "edit": sidecar.config.to_dict() if sidecar.config is not None else None,
        "work_prints": {
            name: {"created_at": wp.created_at, "updated_at": wp.stamp, "edit": wp.config.to_dict()}
            for name, wp in sidecar.work_prints.items()
        },
        "deleted_work_prints": dict(sidecar.deleted_work_prints),
    }


def _scan_payload(data: Dict[str, Any]) -> Optional[SidecarScan]:
    if "sidecar_format" not in data:
        return SidecarScan(saved_at=None, mark=None, mark_at=None, work_print_stamps={}, deleted_work_prints={}, edit=data)
    edit = data.get("edit")
    if "edit" not in data or (edit is not None and not isinstance(edit, dict)):
        return None
    saved_at = data.get("saved_at")
    saved_at = float(saved_at) if isinstance(saved_at, (int, float)) and edit is not None else None
    mark_at = data.get("mark_at")
    mark = data.get("mark")
    prints: Dict[str, Dict[str, Any]] = {}
    stamps: Dict[str, float] = {}
    for name, entry in (data.get("work_prints") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("edit"), dict):
            stamp = entry.get("updated_at", entry.get("created_at"))
            prints[str(name)] = entry
            stamps[str(name)] = float(stamp) if isinstance(stamp, (int, float)) else 0.0
    legacy_saved_at = data.get("saved_at")
    return SidecarScan(
        saved_at=saved_at,
        mark=mark if mark in _MARKS else None,
        # A file without mark_at dates a mark by saved_at; its null mark says nothing.
        mark_at=float(mark_at)
        if isinstance(mark_at, (int, float))
        else float(legacy_saved_at)
        if mark in _MARKS and isinstance(legacy_saved_at, (int, float))
        else None,
        work_print_stamps=stamps,
        deleted_work_prints={
            str(name): float(t) for name, t in (data.get("deleted_work_prints") or {}).items() if isinstance(t, (int, float))
        },
        edit=edit,
        prints=prints,
        source_hash=str(data.get("source_hash") or ""),
    )


def _from_payload(data: Dict[str, Any]) -> Optional[Sidecar]:
    scan = _scan_payload(data)
    return scan.sidecar() if scan is not None else None


def load_sidecar(source_path: str, half: int = 0) -> Optional[Sidecar]:
    """The sidecar next to the source file. None if absent or malformed."""
    return read_sidecar(sidecar_path_for(source_path, half))


def read_sidecar(path: str) -> Optional[Sidecar]:
    """The sidecar at *path*. None if absent or malformed."""
    data = _read_json(path)
    if data is None:
        return None
    try:
        return _from_payload(data)
    except Exception as exc:
        logger.warning("Failed to load sidecar %s: %s", path, exc)
        return None


def sidecar_from_repo(repo, file_hash: str) -> Optional[Sidecar]:
    """This hash's saved edit, mark and work prints as a sidecar. None when the frame has
    none of them; the edit is None without a saved edit."""
    record = repo.load_file_record(file_hash)
    mark_record = repo.load_mark_record(file_hash)
    saved_prints = repo.load_work_prints(file_hash)
    tombstones = repo.load_work_print_tombstones(file_hash)
    if record is None and mark_record is None and not saved_prints and not tombstones:
        return None
    stamps = repo.load_work_print_stamps(file_hash)
    return Sidecar(
        config=record[0] if record else None,
        saved_at=record[1] if record else None,
        source_hash=file_hash,
        mark=mark_record[0] if mark_record else None,
        mark_at=mark_record[1] if mark_record else None,
        work_prints={name: SidecarWorkPrint(created_at, cfg, stamps.get(name)) for name, created_at, cfg in saved_prints},
        deleted_work_prints=tombstones,
    )


def _latest(print_at: Optional[float], deleted_at: Optional[float]) -> Optional[tuple[float, bool]]:
    """(time, deleted) of the last thing that happened to one work-print name on one side.
    A deletion wins a tie with a save."""
    if deleted_at is not None and (print_at is None or deleted_at >= print_at):
        return deleted_at, True
    return (print_at, False) if print_at is not None else None


def _extras_to_take(repo, file_hash: str, sidecar: "Sidecar | SidecarScan") -> tuple[bool, Optional[str], dict]:
    """What of the sidecar's mark and work prints is newer than here, read-only: whether the
    file's mark wins, the mark here, and {name: ((time, deleted), held here)} for each
    work-print name whose last save or tombstone in the file beats the one here."""
    local = repo.load_mark_record(file_hash)
    mark_wins = sidecar.mark_at is not None and (sidecar.mark_at > local[1] if local else sidecar.mark is not None)
    take: dict = {}
    stamps = sidecar.work_print_stamps
    if stamps or sidecar.deleted_work_prints:
        held = repo.load_work_print_stamps(file_hash)
        gone = repo.load_work_print_tombstones(file_hash)
        for name in {*stamps, *sidecar.deleted_work_prints}:
            theirs = _latest(stamps.get(name), sidecar.deleted_work_prints.get(name))
            mine = _latest(held.get(name), gone.get(name))
            if theirs is not None and (mine is None or theirs > mine):
                take[name] = (theirs, name in held)
    return mark_wins, (local[0] if local else None), take


def extras_newer(repo, file_hash: str, sidecar: "Sidecar | SidecarScan") -> bool:
    """Whether ``merge_sidecar_extras`` has anything to take. Reads only."""
    mark_wins, _, take = _extras_to_take(repo, file_hash, sidecar)
    return mark_wins or bool(take)


def merge_sidecar_extras(repo, file_hash: str, source_path: str, sidecar: "Sidecar | SidecarScan") -> bool:
    """Take the sidecar's mark when dated after the mark here, and for each work-print name
    whatever happened to it last on either side: a save, or a deletion or rename away (a
    tombstone). Never touches the edit. True when the mark or the prints here changed."""
    mark_wins, local_mark, take = _extras_to_take(repo, file_hash, sidecar)
    changed = False
    if mark_wins:
        repo.save_file_mark(file_hash, sidecar.mark, file_path=source_path, marked_at=sidecar.mark_at)
        changed = local_mark != sidecar.mark
    for name, ((when, deleted), held) in take.items():
        if deleted:
            repo.delete_work_print(file_hash, name, deleted_at=when)
            changed = changed or held
        elif (wp := sidecar.work_print(name)) is not None:
            repo.save_work_print(file_hash, name, wp.config, created_at=wp.created_at or None, updated_at=wp.stamp)
            changed = True
    return changed


def promote_sidecar(repo, file_hash: str, source_path: str, sidecar: Sidecar) -> Optional[WorkspaceConfig]:
    """Make the sidecar's edit this hash's saved edit, then merge its mark and work prints
    (``merge_sidecar_extras``). A sidecar without an edit leaves the edit here alone and
    returns None."""
    if sidecar.config is not None:
        repo.save_file_settings(file_hash, sidecar.config, file_path=source_path, updated_at=sidecar.saved_at or time.time())
    merge_sidecar_extras(repo, file_hash, source_path, sidecar)
    return sidecar.config


def newer_sidecar(repo, file_hash: str, source_path: str, half: int = 0) -> Optional[Sidecar]:
    """A sidecar whose edit was saved after this hash's DB row, else None.

    A DB miss is not "newer": ``load_or_promote`` fills it without asking.
    """
    sidecar = load_sidecar(source_path, half)
    if sidecar is None or sidecar.config is None or sidecar.saved_at is None:
        return None
    local = repo.load_file_updated_at(file_hash)
    if local is None or sidecar.saved_at <= local:
        return None
    return sidecar


class SidecarOffer(NamedTuple):
    asset: Dict[str, Any]
    sidecar: Sidecar


def _is_composite(asset: Dict[str, Any]) -> bool:
    return bool(asset.get("hdr_paths") or asset.get("stitch_paths"))


def pending_sidecar_offers(repo, assets, reader: Optional["SidecarReader"] = None) -> list[SidecarOffer]:
    """Frames whose sidecar edit was saved after their edit here, minus declined versions."""
    return offers_from_plan(repo, plan_frame_sidecars(repo, assets, reader if reader is not None else SidecarReader()))


def _built(scan: SidecarScan, source_path: str) -> Optional[Sidecar]:
    try:
        return scan.sidecar()
    except Exception as exc:
        logger.warning("Failed to load sidecar for %s: %s", source_path, exc)
        return None


class SidecarReader:
    """Sidecar scans kept for the session: a file whose (mtime_ns, size) has not changed
    since it was last read is served from memory. One thread owns an instance."""

    def __init__(self) -> None:
        self._scans: Dict[str, tuple[tuple[int, int], Optional[SidecarScan]]] = {}

    def scan(self, source_path: str, half: int = 0) -> Optional[SidecarScan]:
        path = sidecar_path_for(source_path, half)
        try:
            st = os.stat(path)
        except OSError:
            self._scans.pop(path, None)
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        hit = self._scans.get(path)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        data = _read_json(path)
        try:
            scan = _scan_payload(data) if data is not None else None
        except Exception as exc:
            logger.warning("Failed to load sidecar %s: %s", path, exc)
            scan = None
        self._scans[path] = (stamp, scan)
        return scan


@dataclass
class SidecarPlan:
    """What a folder's sidecars hold that this computer lacks, as (asset, sidecar) pairs:
    edits to fill, newer marks or work prints to merge, newer edits to offer. Found without
    a DB write, so it can be built off the UI thread; ``apply_sidecar_plan`` and
    ``offers_from_plan`` check each entry again against the DB."""

    fill: list[tuple[Dict[str, Any], Sidecar]] = field(default_factory=list)
    merge: list[tuple[Dict[str, Any], Sidecar]] = field(default_factory=list)
    offer: list[tuple[Dict[str, Any], Sidecar]] = field(default_factory=list)


def plan_frame_sidecars(repo, assets, reader: SidecarReader, is_cancelled: Callable[[], bool] = lambda: False) -> Optional[SidecarPlan]:
    """Read each frame's sidecar and compare it with the DB, reading only. None when
    *is_cancelled* turns true. Builds a config only for an entry of the plan.

    A frame with no edit here fills from its file, except where a path match resolves it
    first. Any other frame, a roll fork included, merges into the shared hash; a frame with
    an older edit here is offered the file's. A composite has no sidecar of its own.
    """
    plan = SidecarPlan()
    for asset in assets:
        if is_cancelled():
            return None
        file_hash, path = asset.get("hash") or "", asset.get("path") or ""
        if not file_hash or not path or _is_composite(asset):
            continue
        half = int(asset.get("half") or 0)
        scan = reader.scan(path, half)
        if scan is None:
            continue
        shared = unforked_hash(file_hash)
        local_at = repo.load_file_updated_at(file_hash) if shared == file_hash else None
        unedited = shared == file_hash and local_at is None
        if unedited and not half and repo.has_settings_for_path(path):
            continue
        if unedited and scan.has_edit:
            if (sidecar := _built(scan, path)) is not None:
                plan.fill.append((asset, sidecar))
            continue
        newer_edit = local_at is not None and scan.has_edit and scan.saved_at is not None and scan.saved_at > local_at
        takes_extras = extras_newer(repo, shared, scan)
        if (newer_edit or takes_extras) and (sidecar := _built(scan, path)) is not None:
            if takes_extras:
                plan.merge.append((asset, sidecar))
            if newer_edit:
                plan.offer.append((asset, sidecar))
    return plan


def apply_sidecar_plan(repo, plan: SidecarPlan) -> tuple[list[str], list[str]]:
    """Write the plan's fills and merges. Returns (hashes whose edit was filled, hashes that
    took only a newer mark or work print). A frame that took an edit since the plan was
    made, as opening it does, merges instead of filling; a merge that is no longer newer
    writes nothing."""
    filled: list[str] = []
    merged: list[str] = []
    for asset, sidecar in plan.fill:
        file_hash, path = asset["hash"], asset["path"]
        if repo.load_file_updated_at(file_hash) is None:
            promote_sidecar(repo, file_hash, path, sidecar)
            filled.append(file_hash)
        elif merge_sidecar_extras(repo, file_hash, path, sidecar):
            merged.append(file_hash)
    for asset, sidecar in plan.merge:
        shared = unforked_hash(asset["hash"])
        if merge_sidecar_extras(repo, shared, asset["path"], sidecar) and shared not in merged:
            merged.append(shared)
    return filled, merged


def offers_from_plan(repo, plan: SidecarPlan) -> list[SidecarOffer]:
    """The plan's newer edits still newer than the edit here, minus declined versions."""
    declined = repo.get_global_setting(DECLINED_KEY, default=None) or {}
    offers = []
    for asset, sidecar in plan.offer:
        local = repo.load_file_updated_at(asset["hash"])
        saved_at = sidecar.saved_at
        if local is not None and saved_at is not None and saved_at > local and declined.get(asset["hash"]) != saved_at:
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


def read_frame_sidecars(repo, assets, reader: Optional[SidecarReader] = None) -> tuple[list[str], list[str]]:
    """Plan and apply in one call (``plan_frame_sidecars``, ``apply_sidecar_plan``)."""
    plan = plan_frame_sidecars(repo, assets, reader if reader is not None else SidecarReader())
    return apply_sidecar_plan(repo, plan) if plan is not None else ([], [])


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


def promote_sidecars(repo, assets) -> None:
    """Promote the sidecar of every asset with no saved edit, so readers that go straight
    to the DB (batch export, the unopened-frames guard) see it."""
    saved = repo.saved_hashes([a["hash"] for a in assets])
    for a in assets:
        if a["hash"] in saved:
            continue
        load_or_promote(
            repo,
            a["hash"],
            a["path"],
            half=int(a.get("half") or 0),
            composite=_is_composite(a),
            forked=unforked_hash(a["hash"]) != a["hash"],
        )


class SidecarMirror:
    """Keeps each frame's sidecar equal to its DB rows.

    Frames are marked dirty as they are saved and written in one flush, so a slider drag
    costs one file write, not one per tick. The flush reads the rows back from
    the repo, which is what makes the file match the DB rather than the in-flight config.
    Before writing, a frame takes what its file holds that is newer: the mark and work
    prints (``merge_sidecar_extras``), and the edit when it has none here
    (``load_or_promote``). *on_merged* gets the hashes whose mark or work prints changed.
    """

    def __init__(self, repo, on_merged: Optional[Callable[[list[str]], None]] = None) -> None:
        self._repo = repo
        self._on_merged = on_merged
        self._dirty: Dict[str, tuple[str, int]] = {}
        self._unwritable_dirs: set[str] = set()

    def mark_dirty(self, file_hash: str, source_path: str, half: int = 0) -> None:
        # The sidecar describes the shared edit; a roll fork of it never writes there.
        if file_hash and source_path and unforked_hash(file_hash) == file_hash:
            self._dirty[file_hash] = (source_path, half)

    def pending(self) -> int:
        return len(self._dirty)

    def flush(self) -> tuple[int, int]:
        """Write every dirty frame. Returns (written, failed); a folder that refuses a write
        is skipped for the rest of the session."""
        pending, self._dirty = self._dirty, {}
        written = failed = 0
        merged: list[str] = []
        for file_hash, (source_path, half) in pending.items():
            if os.path.dirname(source_path) in self._unwritable_dirs:
                continue
            on_disk = load_sidecar(source_path, half)
            if on_disk is not None and merge_sidecar_extras(self._repo, file_hash, source_path, on_disk):
                merged.append(file_hash)
            load_or_promote(self._repo, file_hash, source_path, half=half)
            sidecar = sidecar_from_repo(self._repo, file_hash)
            if sidecar is None:
                continue
            ok = self._write(os.path.dirname(source_path), write_sidecar, source_path, sidecar, half=half)
            written, failed = written + ok, failed + (not ok)
        if merged and self._on_merged is not None:
            self._on_merged(merged)
        return written, failed

    def _write(self, folder: str, write, *args, **kwargs) -> bool:
        try:
            write(*args, **kwargs)
            return True
        except OSError as exc:
            self._unwritable_dirs.add(folder)
            logger.warning("Sidecar write failed in %s, skipping that folder: %s", folder, exc)
            return False
