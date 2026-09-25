"""``.negpy`` edit sidecars: a plain-file copy of one frame's edit next to its source,
and one ``.negpy-roll`` file per folder roll.

Format 3 is an envelope: ``saved_at`` (the DB row's ``updated_at``, so a sidecar this
machine mirrored compares equal to its row, not newer), ``source_hash``, ``mark`` and
``mark_at`` (the mark's own time), the ``edit`` (flat config), named ``work_prints`` with
their ``updated_at``, ``deleted_work_prints`` (name: deletion time) and ``roll_locks``, the
cards the frame keeps locked in its folder roll. A frame with a mark or
work prints but no saved edit has a null ``edit`` and ``saved_at``. A file without
``mark_at`` dates a mark by ``saved_at`` and carries no clear; a format-2 file has no ``roll_locks``. A
format-1 file is a bare flat config and has no timestamp, so it only ever fills a DB miss.
Roll ids are per machine, so a baseline source naming the frame's folder roll is written
``roll:`` and read back as the folder roll here.

The roll file carries what a folder roll holds for all its frames (defaults, scenes,
baselines, section pushes, half-frame mode), stamped with the roll's ``updated_at``.
"""

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, NamedTuple, Optional

from negpy.domain.models import WorkspaceConfig
from negpy.kernel.system.logging import get_logger
from negpy.services.assets import rolls
from negpy.services.assets.rolls import unforked_hash

logger = get_logger(__name__)

SIDECAR_EXT = ".negpy"
FOLDER_ROLL_SOURCE = "roll:"
SIDECAR_FORMAT = 3
ROLL_SIDECAR_NAME = ".negpy-roll"
ROLL_SIDECAR_FORMAT = 1
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
    # None when the file does not say: format 2, no folder roll, or a frame forked in it.
    roll_locks: Optional[tuple] = None

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
    roll_locks: Optional[tuple] = None

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
            roll_locks=self.roll_locks,
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
        "roll_locks": list(sidecar.roll_locks) if sidecar.roll_locks is not None else None,
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
    locks = data.get("roll_locks")
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
        roll_locks=tuple(str(c) for c in locks) if isinstance(locks, list) else None,
    )


def _from_payload(data: Dict[str, Any]) -> Optional[Sidecar]:
    scan = _scan_payload(data)
    return scan.sidecar() if scan is not None else None


def load_sidecar(source_path: str, half: int = 0) -> Optional[Sidecar]:
    """The sidecar next to the source file. None if absent or malformed."""
    data = _read_json(sidecar_path_for(source_path, half))
    if data is None:
        return None
    try:
        return _from_payload(data)
    except Exception as exc:
        logger.warning("Failed to load sidecar for %s: %s", source_path, exc)
        return None


def _folder_roll(repo, source_path: str) -> Optional[str]:
    return rolls.folder_roll_id_for_path(repo, os.path.dirname(source_path)) if source_path else None


def _roll_locks(repo, file_hash: str, source_path: str) -> Optional[tuple]:
    roll_id = _folder_roll(repo, source_path) if unforked_hash(file_hash) == file_hash else None
    if roll_id is None or rolls.is_forked(repo, roll_id, file_hash):
        return None
    return tuple(sorted(rolls.frame_override_cards(repo, roll_id, file_hash)))


def _restore_roll_locks(repo, file_hash: str, source_path: str, sidecar: Sidecar) -> None:
    """Set the frame's locks in its folder roll from the sidecar. A file that does not say
    locks every card where the edit differs from this roll's defaults, and unlocks none. A
    fork hash has no locks of its own to set."""
    roll_id = _folder_roll(repo, source_path) if unforked_hash(file_hash) == file_hash else None
    if roll_id is None or rolls.is_forked(repo, roll_id, file_hash) or sidecar.config is None:
        return
    current = rolls.frame_override_cards(repo, roll_id, file_hash)
    if sidecar.roll_locks is not None:
        cards = set(sidecar.roll_locks)
    else:
        cards = current | rolls.diverged_cards(rolls.roll_defaults(repo, roll_id), sidecar.config)
    if cards != current:
        rolls.set_frame_locks(repo, roll_id, file_hash, cards)


def _rebind_source(config: WorkspaceConfig, old: str, new: str) -> WorkspaceConfig:
    if config.process.baseline_source != old:
        return config
    return replace(config, process=replace(config.process, baseline_source=new))


def sidecar_from_repo(repo, file_hash: str, source_path: str = "") -> Optional[Sidecar]:
    """This hash's saved edit, mark, work prints and folder-roll locks as a sidecar. None
    when the frame has none of them; the edit and locks are None without a saved edit."""
    record = repo.load_file_record(file_hash)
    mark_record = repo.load_mark_record(file_hash)
    saved_prints = repo.load_work_prints(file_hash)
    tombstones = repo.load_work_print_tombstones(file_hash)
    if record is None and mark_record is None and not saved_prints and not tombstones:
        return None
    stamps = repo.load_work_print_stamps(file_hash)
    roll_id = _folder_roll(repo, source_path)

    def portable(cfg: WorkspaceConfig) -> WorkspaceConfig:
        return _rebind_source(cfg, f"roll:{roll_id}", FOLDER_ROLL_SOURCE) if roll_id else cfg

    return Sidecar(
        config=portable(record[0]) if record else None,
        saved_at=record[1] if record else None,
        source_hash=file_hash,
        mark=mark_record[0] if mark_record else None,
        mark_at=mark_record[1] if mark_record else None,
        work_prints={name: SidecarWorkPrint(created_at, portable(cfg), stamps.get(name)) for name, created_at, cfg in saved_prints},
        deleted_work_prints=tombstones,
        roll_locks=_roll_locks(repo, file_hash, source_path) if record else None,
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
    local_bind: Optional[str] = None
    for name, ((when, deleted), held) in take.items():
        if deleted:
            repo.delete_work_print(file_hash, name, deleted_at=when)
            changed = changed or held
        elif (wp := sidecar.work_print(name)) is not None:
            if local_bind is None:
                roll_id = _folder_roll(repo, source_path)
                local_bind = f"roll:{roll_id}" if roll_id else ""
            config = _rebind_source(wp.config, FOLDER_ROLL_SOURCE, local_bind)
            repo.save_work_print(file_hash, name, config, created_at=wp.created_at or None, updated_at=wp.stamp)
            changed = True
    return changed


def promote_sidecar(repo, file_hash: str, source_path: str, sidecar: Sidecar) -> Optional[WorkspaceConfig]:
    """Make the sidecar's edit this hash's saved edit and its folder-roll locks, then merge
    its mark and work prints (``merge_sidecar_extras``). A sidecar without an edit leaves the
    edit here alone and returns None. A folder-roll baseline source binds to the folder roll
    here, or to none without one."""
    config = None
    if sidecar.config is not None:
        roll_id = _folder_roll(repo, source_path)
        config = _rebind_source(sidecar.config, FOLDER_ROLL_SOURCE, f"roll:{roll_id}" if roll_id else "")
        repo.save_file_settings(file_hash, config, file_path=source_path, updated_at=sidecar.saved_at or time.time())
        _restore_roll_locks(repo, file_hash, source_path, sidecar)
    merge_sidecar_extras(repo, file_hash, source_path, sidecar)
    return config


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


def _offer_key(offer) -> str:
    return f"roll:{offer.roll_id}" if isinstance(offer, RollSidecarOffer) else offer.asset["hash"]


def decline_sidecar_offers(repo, offers) -> None:
    """Remember these sidecar versions as declined; a later save to the file asks again."""
    if not offers:
        return
    declined = dict(repo.get_global_setting(DECLINED_KEY, default=None) or {})
    for offer in offers:
        declined[_offer_key(offer)] = offer.sidecar.saved_at
    repo.save_global_setting(DECLINED_KEY, declined)


@dataclass(frozen=True)
class RollSidecar:
    """One folder roll's ``.negpy-roll`` file. ``state`` holds ``rolls.PORTABLE_FIELDS``."""

    saved_at: float
    name: str = ""
    half_frame_mode: bool = False
    state: Dict[str, Any] = field(default_factory=dict)


class RollSidecarOffer(NamedTuple):
    roll_id: str
    name: str
    sidecar: RollSidecar


def roll_sidecar_path(folder: str) -> str:
    return os.path.join(folder, ROLL_SIDECAR_NAME)


def write_roll_sidecar(folder: str, sidecar: RollSidecar) -> str:
    """Write the roll file into *folder*, atomically. Returns the path written."""
    path = roll_sidecar_path(folder)
    payload = {
        "roll_sidecar_format": ROLL_SIDECAR_FORMAT,
        "saved_at": sidecar.saved_at,
        "name": sidecar.name,
        "half_frame_mode": sidecar.half_frame_mode,
        **{key: sidecar.state.get(key) for key in rolls.PORTABLE_FIELDS},
    }
    _write_json(path, payload)
    return path


def load_roll_sidecar(folder: str) -> Optional[RollSidecar]:
    """The roll file in *folder*. None if absent, malformed or without a time."""
    data = _read_json(roll_sidecar_path(folder))
    saved_at = data.get("saved_at") if data else None
    if data is None or "roll_sidecar_format" not in data or not isinstance(saved_at, (int, float)):
        return None
    return RollSidecar(
        saved_at=float(saved_at),
        name=str(data.get("name") or ""),
        half_frame_mode=bool(data.get("half_frame_mode")),
        state={key: data[key] for key in rolls.PORTABLE_FIELDS if isinstance(data.get(key), dict)},
    )


def roll_sidecar_from_repo(repo, roll_id: str) -> Optional[tuple[str, RollSidecar]]:
    """(folder, file) to write for a folder roll this machine has changed. None for a virtual
    roll, or when the folder's file was saved after the state here: writing it would hide
    that newer state from every machine."""
    entry = rolls.roll_for_id(repo, roll_id)
    saved_at = rolls.roll_updated_at(repo, roll_id)
    if not entry or entry.get("kind") != "folder" or not entry.get("folder_path") or saved_at is None:
        return None
    on_disk = load_roll_sidecar(entry["folder_path"])
    if on_disk is not None and on_disk.saved_at > saved_at:
        return None
    state = {key: entry[key] for key in rolls.PORTABLE_FIELDS if entry.get(key)}
    return entry["folder_path"], RollSidecar(saved_at, entry.get("name") or "", rolls.roll_half_frame_mode(repo, roll_id), state)


def export_roll_sidecar(repo, roll_id: str) -> Optional[str]:
    """Write a folder roll's file, dating state no change here has dated yet unless the
    folder already has a file. Returns the path, or None when nothing was written."""
    entry = rolls.roll_for_id(repo, roll_id) or {}
    folder = entry.get("folder_path") or ""
    if entry.get("kind") != "folder" or not os.path.isdir(folder):
        return None
    if rolls.roll_updated_at(repo, roll_id) is None and rolls.has_portable_state(repo, roll_id) and load_roll_sidecar(folder) is None:
        rolls.touch_roll(repo, roll_id)
    found = roll_sidecar_from_repo(repo, roll_id)
    return write_roll_sidecar(*found) if found is not None else None


def adopt_roll_sidecar(repo, roll_id: str, sidecar: RollSidecar) -> None:
    """Make the file this roll's state. The local name stays."""
    rolls.replace_portable_state(repo, roll_id, sidecar.state, sidecar.half_frame_mode, sidecar.saved_at)
    entry = rolls.roll_for_id(repo, roll_id)
    if entry is not None and not entry.get("name") and sidecar.name:
        rolls.rename_roll(repo, roll_id, sidecar.name)


def read_roll_sidecar(repo, roll_id: str, any_age: bool = False) -> Optional[RollSidecarOffer]:
    """Adopt the folder's roll file when ``rolls.adopts_roll_file``. Otherwise offer
    it when saved after the state here and not declined; *any_age* offers any version
    other than the one held here, declined or not."""
    entry = rolls.roll_for_id(repo, roll_id)
    if not entry or entry.get("kind") != "folder":
        return None
    sidecar = load_roll_sidecar(entry.get("folder_path") or "")
    if sidecar is None:
        return None
    local = rolls.roll_updated_at(repo, roll_id)
    if rolls.adopts_roll_file(repo, roll_id):
        adopt_roll_sidecar(repo, roll_id, sidecar)
        return None
    offer = RollSidecarOffer(roll_id, entry.get("name") or "", sidecar)
    if any_age:
        return offer if sidecar.saved_at != local else None
    declined = repo.get_global_setting(DECLINED_KEY, default=None) or {}
    if sidecar.saved_at <= (local or 0.0) or declined.get(_offer_key(offer)) == sidecar.saved_at:
        return None
    return offer


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


class SidecarMirror:
    """Keeps each frame's sidecar equal to its DB rows, and each changed folder roll's file
    equal to its roll.

    Frames and rolls are marked dirty as they are saved and written in one flush, so a
    slider drag costs one file write, not one per tick. The flush reads the rows back from
    the repo, which is what makes the file match the DB rather than the in-flight config.
    Before writing, a frame takes what its file holds that is newer: the mark and work
    prints (``merge_sidecar_extras``), and the edit when it has none here
    (``load_or_promote``). *on_merged* gets the hashes whose mark or work prints changed.
    """

    def __init__(self, repo, on_merged: Optional[Callable[[list[str]], None]] = None) -> None:
        self._repo = repo
        self._on_merged = on_merged
        self._dirty: Dict[str, tuple[str, int]] = {}
        self._dirty_rolls: set[str] = set()
        self._unwritable_dirs: set[str] = set()

    def mark_dirty(self, file_hash: str, source_path: str, half: int = 0) -> None:
        # The sidecar describes the shared edit; a roll fork of it never writes there.
        if file_hash and source_path and unforked_hash(file_hash) == file_hash:
            self._dirty[file_hash] = (source_path, half)

    def mark_roll_dirty(self, roll_id: str) -> None:
        entry = rolls.roll_for_id(self._repo, roll_id) if roll_id else None
        if entry and entry.get("kind") == "folder":
            self._dirty_rolls.add(roll_id)

    def pending(self) -> int:
        return len(self._dirty) + len(self._dirty_rolls)

    def flush(self) -> tuple[int, int]:
        """Write every dirty frame and roll. Returns (written, failed); a folder that
        refuses a write is skipped for the rest of the session."""
        pending, self._dirty = self._dirty, {}
        pending_rolls, self._dirty_rolls = self._dirty_rolls, set()
        written = failed = 0
        merged: list[str] = []
        for file_hash, (source_path, half) in pending.items():
            if os.path.dirname(source_path) in self._unwritable_dirs:
                continue
            on_disk = load_sidecar(source_path, half)
            if on_disk is not None and merge_sidecar_extras(self._repo, file_hash, source_path, on_disk):
                merged.append(file_hash)
            load_or_promote(self._repo, file_hash, source_path, half=half)
            sidecar = sidecar_from_repo(self._repo, file_hash, source_path)
            if sidecar is None:
                continue
            ok = self._write(os.path.dirname(source_path), write_sidecar, source_path, sidecar, half=half)
            written, failed = written + ok, failed + (not ok)
        for roll_id in pending_rolls:
            found = roll_sidecar_from_repo(self._repo, roll_id)
            if found is None or os.path.normpath(found[0]) in self._unwritable_dirs or not os.path.isdir(found[0]):
                continue
            ok = self._write(os.path.normpath(found[0]), write_roll_sidecar, *found)
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
