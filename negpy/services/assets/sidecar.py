"""``.negpy`` edit sidecars: one frame's flat config next to its source, plus ``bounds_version``
and one reserved key, ``sync`` (``saved_at``, ``mark``, ``mark_at``). A reader without ``sync``
drops that key and keeps the edit. A folder roll's state travels in ``<folder>/.negpy-roll``."""

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Collection, Dict, List, NamedTuple, Optional

from negpy.domain.models import WorkspaceConfig
from negpy.features.exposure.normalization import BOUNDS_VERSION
from negpy.kernel.system.logging import get_logger
from negpy.services.assets import rolls
from negpy.services.assets.migrations.roll_fields import CARD_KEYS_VERSION, migrate_roll_entry
from negpy.services.assets.rolls import unforked_hash

logger = get_logger(__name__)

SIDECAR_EXT = ".negpy"
SYNC_KEY = "sync"
_NOT_CONFIG = (SYNC_KEY, "bounds_version")
_MARKS = ("keeper", "excluded")


@dataclass(frozen=True)
class SidecarSync:
    """When the file's edit and mark were saved. ``saved_at`` is None without an edit; ``mark``
    is None when cleared, and ``mark_at`` is None when the file never held a mark."""

    saved_at: Optional[float] = None
    mark: Optional[str] = None
    mark_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"saved_at": self.saved_at, "mark": self.mark, "mark_at": self.mark_at}


def sidecar_path_for(source_path: str, half: int = 0) -> str:
    """Sidecar path next to the source file: ``<basename>.negpy`` (``<basename>.<half>.negpy`` for half-frame assets)."""
    base = os.path.splitext(os.path.basename(source_path))[0]
    suffix = f".{half}" if half else ""
    return os.path.join(os.path.dirname(source_path), base + suffix + SIDECAR_EXT)


def sidecar_payload(config: Optional[WorkspaceConfig], sync: Optional[SidecarSync] = None) -> Dict[str, Any]:
    """The JSON object a sidecar holds: the flat config with its ``bounds_version``, and ``sync``."""
    payload: Dict[str, Any] = {**config.to_dict(), "bounds_version": BOUNDS_VERSION} if config is not None else {}
    if sync is not None:
        payload[SYNC_KEY] = sync.to_dict()
    return payload


def write_payload(path: str, payload: Dict[str, Any]) -> bool:
    """Write *payload* to *path* atomically. False, writing nothing, when the folder is gone:
    a sidecar never creates one."""
    folder = os.path.dirname(path) or "."
    if not os.path.isdir(folder):
        return False
    text = json.dumps(payload, default=str, indent=2)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=folder, delete=False, suffix=".part", encoding="utf-8") as tmp:
            tmp_path = tmp.name
            tmp.write(text)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return True


def write_sidecar(source_path: str, config: WorkspaceConfig, half: int = 0, sync: Optional[SidecarSync] = None) -> Optional[str]:
    """Write the full edit next to the source. Returns the path written, or None when the source's folder is gone."""
    path = sidecar_path_for(source_path, half)
    return path if write_payload(path, sidecar_payload(config, sync)) else None


def read_payload(path: str) -> Optional[Dict[str, Any]]:
    """The JSON object at *path*. None if absent, unreadable or not an object."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f_in:
            data = json.load(f_in)
    except Exception as exc:
        logger.warning("Failed to load sidecar %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def config_keys(data: Dict[str, Any]) -> Dict[str, Any]:
    """The payload's flat config keys, without ``bounds_version`` and ``sync``."""
    return {k: v for k, v in data.items() if k not in _NOT_CONFIG}


def config_from_payload(data: Dict[str, Any]) -> Optional[WorkspaceConfig]:
    """The payload's edit, or None when it holds none. Frame bounds saved under an older
    BOUNDS_VERSION are dropped, except under Lock Bounds, so the frame meters again."""
    flat = config_keys(data)
    if not flat:
        return None
    if int(data.get("bounds_version", 0) or 0) < BOUNDS_VERSION and not flat.get("lock_bounds"):
        flat.pop("local_floors", None)
        flat.pop("local_ceils", None)
    return WorkspaceConfig.from_flat_dict(flat)


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def sync_from_payload(data: Dict[str, Any]) -> Optional[SidecarSync]:
    """The payload's ``sync``, or None for a file written without one."""
    raw = data.get(SYNC_KEY)
    if not isinstance(raw, dict):
        return None
    mark = raw.get("mark")
    return SidecarSync(
        saved_at=_number(raw.get("saved_at")) if config_keys(data) else None,
        mark=mark if mark in _MARKS else None,
        mark_at=_number(raw.get("mark_at")),
    )


def load_sidecar(source_path: str, half: int = 0) -> Optional[WorkspaceConfig]:
    """Load edits from a sidecar next to the source file. None if absent or malformed."""
    return read_sidecar(sidecar_path_for(source_path, half))


def read_sidecar(path: str) -> Optional[WorkspaceConfig]:
    """Load edits from the sidecar at *path*. None if unreadable, malformed or without an edit."""
    data = read_payload(path)
    if data is None:
        return None
    try:
        return config_from_payload(data)
    except Exception as exc:
        logger.warning("Failed to load sidecar %s: %s", path, exc)
        return None


def read_sidecar_meta(path: str) -> Optional[SidecarSync]:
    """The ``sync`` of the sidecar at *path*. None if absent, unreadable or written without one."""
    data = read_payload(path)
    return sync_from_payload(data) if data is not None else None


def same_edit(a: Optional[WorkspaceConfig], b: Optional[WorkspaceConfig]) -> bool:
    if a is None or b is None:
        return a is b
    return json.dumps(a.to_dict(), sort_keys=True, default=str) == json.dumps(b.to_dict(), sort_keys=True, default=str)


def sidecar_edit(repo, source_path: str, config: Optional[WorkspaceConfig]) -> Optional[WorkspaceConfig]:
    """*config* read from the sidecar beside *source_path*. Roll ids are per computer, so a
    baseline taken from a roll unknown here names the frame's folder roll, or no roll."""
    if config is None:
        return None
    kind, _, ref = config.process.baseline_source.partition(":")
    if kind != "roll" or rolls.roll_for_id(repo, ref) is not None:
        return config
    roll_id = rolls.folder_roll_id_for_path(repo, os.path.dirname(source_path)) if source_path else None
    return replace(config, process=replace(config.process, baseline_source=f"roll:{roll_id}" if roll_id else ""))


def sync_from_repo(repo, file_hash: str) -> SidecarSync:
    """The ``sync`` for this hash's saved edit and mark."""
    record = repo.load_mark_record(file_hash)
    return SidecarSync(repo.load_file_updated_at(file_hash), *(record or (None, None)))


def promote_payload(repo, file_hash: str, source_path: str, data: Dict[str, Any]) -> Optional[WorkspaceConfig]:
    """Make the payload's edit this hash's saved edit, dated by the file (0 when undated). None without an edit."""
    cfg = sidecar_edit(repo, source_path, config_from_payload(data))
    if cfg is not None:
        sync = sync_from_payload(data)
        saved_at = sync.saved_at if sync is not None and sync.saved_at is not None else 0.0
        repo.save_file_settings(file_hash, cfg, file_path=source_path, updated_at=saved_at)
    return cfg


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

    data = read_payload(sidecar_path_for(source_path, half))
    if data is None:
        return None
    try:
        return promote_payload(repo, file_hash, source_path, data)
    except Exception as exc:
        logger.warning("Failed to load sidecar for %s: %s", source_path, exc)
        return None


def promote_sidecars(repo, assets: List[dict]) -> None:
    """Promote the sidecar of every asset with no saved edit, so readers that go straight
    to the DB (batch export, thumbnails, search) see it before the frame is opened."""
    saved = repo.saved_hashes([a["hash"] for a in assets])
    for a in assets:
        if a["hash"] in saved:
            continue
        load_or_promote(
            repo,
            a["hash"],
            a["path"],
            half=int(a.get("half") or 0),
            composite=bool(a.get("hdr_paths") or a.get("stitch_paths")),
            forked="#roll:" in a["hash"],
        )


DECLINED_KEY = "sidecar_offers_declined"


class SidecarOffer(NamedTuple):
    """An edit another computer saved after the one here. ``key`` and ``saved_at`` identify the version
    Keep Mine declines; ``label`` names the row in the Newer Sidecars dialog."""

    asset: Dict[str, Any]
    config: WorkspaceConfig
    saved_at: float

    @property
    def key(self) -> str:
        return str(self.asset["hash"])

    @property
    def label(self) -> str:
        return str(self.asset.get("name") or os.path.basename(self.asset["path"]))


@dataclass
class SidecarPlan:
    """What a folder's sidecars hold that this computer lacks: edits to fill (asset, config, saved_at),
    marks to merge (asset, sync) and edits to offer. Built with reads only, so it runs off the UI thread;
    ``apply_sidecar_plan`` and ``offers_from_plan`` check each entry again."""

    fill: List[tuple[Dict[str, Any], WorkspaceConfig, float]] = field(default_factory=list)
    merge: List[tuple[Dict[str, Any], SidecarSync]] = field(default_factory=list)
    offer: List[SidecarOffer] = field(default_factory=list)

    def without_edits_for(self, keep: Callable[[Dict[str, Any]], bool]) -> "SidecarPlan":
        """This plan with the fills and offers of the assets *keep* rejects; their marks still merge."""
        return SidecarPlan([f for f in self.fill if keep(f[0])], list(self.merge), [o for o in self.offer if keep(o.asset)])


def _mark_is_newer(repo, file_hash: str, sync: SidecarSync) -> bool:
    record = repo.load_mark_record(file_hash)
    if sync.mark_at is None or (record is not None and sync.mark_at <= record[1]):
        return False
    return sync.mark != (record[0] if record else None)


def plan_frame_sidecars(repo, assets: List[dict]) -> SidecarPlan:
    """Read each frame's sidecar and compare it with the DB. A frame with no edit here fills from its file;
    an edit here older than the file's and different from it is offered the file's; a newer mark of a
    different value merges into the shared hash. A roll fork takes only the mark, and a composite has no sidecar."""
    plan = SidecarPlan()
    for asset in assets:
        file_hash, path = asset.get("hash") or "", asset.get("path") or ""
        if not file_hash or not path or asset.get("hdr_paths") or asset.get("stitch_paths"):
            continue
        data = read_payload(sidecar_path_for(path, int(asset.get("half") or 0)))
        if data is None:
            continue
        try:
            config = sidecar_edit(repo, path, config_from_payload(data)) if "#roll:" not in file_hash else None
        except Exception as exc:
            logger.warning("Failed to load sidecar for %s: %s", path, exc)
            config = None
        sync = sync_from_payload(data) or SidecarSync()
        if config is not None:
            local_at = repo.load_file_updated_at(file_hash)
            saved_at = sync.saved_at or 0.0
            if local_at is None:
                plan.fill.append((asset, config, saved_at))
            elif saved_at > local_at and not same_edit(config, repo.load_file_settings(file_hash)):
                plan.offer.append(SidecarOffer(asset, config, saved_at))
        if _mark_is_newer(repo, unforked_hash(file_hash), sync):
            plan.merge.append((asset, sync))
    return plan


def apply_sidecar_plan(repo, plan: SidecarPlan) -> tuple[list[str], list[str]]:
    """Write the plan's fills and marks. Returns (hashes whose edit was filled, hashes whose mark changed).
    A frame that took an edit since the plan is not filled, and a whole frame whose path still has
    a saved edit takes that edit instead (``load_or_promote``)."""
    filled: list[str] = []
    merged: list[str] = []
    for asset, config, saved_at in plan.fill:
        file_hash, path = asset["hash"], asset["path"]
        if repo.load_file_updated_at(file_hash) is not None:
            continue
        if not asset.get("half") and (found := repo.load_file_settings_by_path(path)) is not None:
            repo.rehome_file_settings(found[0], file_hash, path)
            continue
        repo.save_file_settings(file_hash, config, file_path=path, updated_at=saved_at)
        filled.append(file_hash)
    for asset, sync in plan.merge:
        if merge_sidecar_mark(repo, asset["hash"], asset["path"], sync):
            merged.append(unforked_hash(asset["hash"]))
    return filled, merged


def merge_sidecar_mark(repo, file_hash: str, source_path: str, sync: SidecarSync) -> bool:
    """Take the file's mark into the shared hash when it is newer and different. True when the mark changed."""
    shared = unforked_hash(file_hash)
    if not _mark_is_newer(repo, shared, sync):
        return False
    repo.save_file_mark(shared, sync.mark, file_path=source_path, marked_at=sync.mark_at)
    return True


def offers_from_plan(repo, plan: SidecarPlan) -> list[SidecarOffer]:
    """The plan's offers still newer than the edit here, minus the versions Keep Mine declined."""
    declined = repo.get_global_setting(DECLINED_KEY, default=None) or {}
    offers = []
    for offer in plan.offer:
        local = repo.load_file_updated_at(offer.asset["hash"])
        if local is not None and offer.saved_at > local and declined.get(offer.key) != offer.saved_at:
            offers.append(offer)
    return offers


def decline_sidecar_offers(repo, offers) -> None:
    """Remember these versions as declined; a later save to the file asks again."""
    if not offers:
        return
    declined = dict(repo.get_global_setting(DECLINED_KEY, default=None) or {})
    for offer in offers:
        declined[offer.key] = offer.saved_at
    repo.save_global_setting(DECLINED_KEY, declined)


ROLL_SIDECAR_NAME = ".negpy-roll"
ROLL_SIDECAR_FORMAT = 1


@dataclass(frozen=True)
class RollSidecar:
    """One folder roll's ``.negpy-roll``: its portable state (``rolls.portable_state``) and its
    Half Frame and Trichrome Mode, each None when the writer had none."""

    saved_at: float
    state: Dict[str, Any]
    modes: Dict[str, Optional[bool]]

    def same_as(self, state: Dict[str, Any], modes: Dict[str, Optional[bool]]) -> bool:
        """Whether *state* and *modes* hold what this file holds; a mode the file lacks matches any."""
        return _json(self.state) == _json(state) and all(v is None or modes.get(k) == v for k, v in self.modes.items())


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


class RollSidecarOffer(NamedTuple):
    """A roll file saved after the roll's state here, a row in the Newer Sidecars dialog."""

    roll_id: str
    name: str
    sidecar: RollSidecar

    @property
    def key(self) -> str:
        return f"roll:{self.roll_id}"

    @property
    def saved_at(self) -> float:
        return self.sidecar.saved_at


def roll_sidecar_path(folder: str) -> str:
    return os.path.join(folder, ROLL_SIDECAR_NAME)


def leaf_names(value: Any) -> list:
    """The plain file names in *value*; a path or a non-string is not one."""
    if not isinstance(value, list):
        return []
    return [n for n in value if isinstance(n, str) and n not in ("", ".", "..") and os.path.basename(n) == n]


def load_roll_sidecar(folder: str) -> Optional[RollSidecar]:
    """The roll file in *folder*, its card keys migrated to this build's. None if absent, malformed
    or written in a newer ``roll_sidecar_format``."""
    data = read_payload(roll_sidecar_path(folder))
    if data is None:
        return None
    version, saved_at, state = data.get("roll_sidecar_format"), _number(data.get("saved_at")), data.get("roll")
    if not isinstance(version, int) or version > ROLL_SIDECAR_FORMAT:
        logger.warning("Roll file in %s has format %r; this build reads up to %d", folder, version, ROLL_SIDECAR_FORMAT)
        return None
    if saved_at is None or not isinstance(state, dict):
        return None
    state = {k: v for k, v in state.items() if k not in rolls.LOCAL_FIELDS}
    if "excluded_names" in state:
        state["excluded_names"] = leaf_names(state["excluded_names"])
    card_keys = data.get("card_keys_version")
    migrate_roll_entry(state, card_keys if isinstance(card_keys, int) else 0)
    modes = {k: data.get(k) if isinstance(data.get(k), bool) else None for k in ("half_frame_mode", "trichrome_mode")}
    return RollSidecar(saved_at, state, modes)


def roll_sidecar_payload(repo, roll_id: str, saved_at: float) -> Dict[str, Any]:
    entry = rolls.roll_for_id(repo, roll_id) or {}
    return {
        "roll_sidecar_format": ROLL_SIDECAR_FORMAT,
        "card_keys_version": CARD_KEYS_VERSION,
        "saved_at": saved_at,
        **rolls.roll_modes(repo, roll_id),
        "roll": rolls.portable_state(entry),
    }


def plan_roll_file_write(repo, roll_id: str) -> Optional[tuple[str, Dict[str, Any]]]:
    """(path, payload) to write for a folder roll this computer has dated: only over no file or
    an older one. A file this build cannot read is never written over."""
    entry = rolls.roll_for_id(repo, roll_id)
    saved_at = rolls.roll_updated_at(repo, roll_id)
    if not entry or entry.get("kind") != "folder" or not entry.get("folder_path") or saved_at is None:
        return None
    path = roll_sidecar_path(entry["folder_path"])
    if os.path.lexists(path):
        on_disk = load_roll_sidecar(entry["folder_path"])
        if on_disk is None or on_disk.saved_at >= saved_at:
            return None
    return path, roll_sidecar_payload(repo, roll_id, saved_at)


def export_roll_sidecar(repo, roll_id: str) -> Optional[str]:
    """Write a folder roll's file for Export Sidecars. State no change here has dated is dated now,
    unless the folder already has a file. Returns the path, or None when nothing was written."""
    entry = rolls.roll_for_id(repo, roll_id) or {}
    folder = entry.get("folder_path") or ""
    if entry.get("kind") != "folder" or not os.path.isdir(folder):
        return None
    if rolls.roll_updated_at(repo, roll_id) is None and rolls.portable_state(entry) and not os.path.lexists(roll_sidecar_path(folder)):
        rolls.touch_roll(repo, roll_id)
    found = plan_roll_file_write(repo, roll_id)
    return found[0] if found is not None and write_payload(*found) else None


def read_roll_sidecar(repo, roll_id: str) -> Optional[RollSidecarOffer]:
    """Read a folder roll's file. A roll with no state here, dated or held, takes it; a file saved
    after the state here that holds something else is offered, unless Keep Mine declined it."""
    entry = rolls.roll_for_id(repo, roll_id)
    if not entry or entry.get("kind") != "folder":
        return None
    sidecar = load_roll_sidecar(entry.get("folder_path") or "")
    if sidecar is None or sidecar.same_as(rolls.portable_state(entry), rolls.roll_modes(repo, roll_id)):
        return None
    local = rolls.roll_updated_at(repo, roll_id)
    if local is None and not rolls.portable_state(entry):
        rolls.replace_portable_state(repo, roll_id, sidecar.state, sidecar.modes, sidecar.saved_at)
        return None
    offer = RollSidecarOffer(roll_id, entry.get("name") or "", sidecar)
    declined = repo.get_global_setting(DECLINED_KEY, default=None) or {}
    if sidecar.saved_at <= (local or 0.0) or declined.get(offer.key) == sidecar.saved_at:
        return None
    return offer


def sidecar_half(file_hash: str) -> Optional[int]:
    """The half whose sidecar holds this hash's edit (0 for a whole frame), or None when the hash
    has no sidecar of its own: a roll fork or a composite."""
    base, sep, suffix = file_hash.partition("#")
    if not base:
        return None
    if not sep:
        return 0
    return int(suffix) if suffix.isdigit() else None


def mirror_payload(
    repo, file_hash: str, on_disk: Optional[Dict[str, Any]], fallback: Optional[WorkspaceConfig] = None, source_path: str = ""
) -> Optional[Dict[str, Any]]:
    """What this hash's sidecar must hold so it is current, or None when *on_disk* already is.
    Per part, the edit and the mark, the newer of the file and the rows wins; a tie keeps the rows.
    A newer part of the file stays as written. *fallback* is written undated when neither holds an edit."""
    disk = on_disk or {}
    disk_edit = config_keys(disk)
    disk_sync = sync_from_payload(disk) or SidecarSync()
    disk_at = disk_sync.saved_at or 0.0
    local_at = repo.load_file_updated_at(file_hash)
    config = repo.load_file_settings(file_hash) if local_at is not None else None

    if disk_edit and (config is None or disk_at > (local_at or 0.0)):
        edit: Dict[str, Any] = {k: v for k, v in disk.items() if k != SYNC_KEY}
        saved_at: Optional[float] = disk_sync.saved_at
        edit_current = True
    elif config is not None:
        edit, saved_at = sidecar_payload(config), local_at
        edit_current = (
            bool(disk_edit) and disk_at == local_at and same_edit(sidecar_edit(repo, source_path, config_from_payload(disk)), config)
        )
    elif fallback is not None:
        edit, saved_at, edit_current = sidecar_payload(fallback), None, False
    else:
        edit, saved_at, edit_current = {}, None, True

    record = repo.load_mark_record(file_hash)
    if disk_sync.mark_at is not None and (record is None or disk_sync.mark_at > record[1]):
        mark, mark_at, mark_current = disk_sync.mark, disk_sync.mark_at, True
    elif record is not None:
        mark, mark_at = record
        mark_current = (disk_sync.mark, disk_sync.mark_at) == record
    else:
        mark, mark_at, mark_current = None, None, True

    if edit_current and mark_current:
        return None
    if on_disk is None and not edit and mark is None:
        return None
    return {**edit, SYNC_KEY: SidecarSync(saved_at, mark, mark_at).to_dict()}


class SidecarMirror:
    """Keep Current: each queued frame's sidecar and each queued folder roll's file follow their
    rows at the next flush, which reads the rows back rather than an in-flight config."""

    def __init__(self, repo) -> None:
        self._repo = repo
        self._queued: Dict[str, str] = {}
        self._queued_rolls: set[str] = set()
        self._checked_rolls: Dict[str, float] = {}
        self._unwritable_dirs: set[str] = set()

    def queue(self, file_hash: str, source_path: str = "") -> None:
        if file_hash and sidecar_half(file_hash) is not None:
            self._queued[file_hash] = source_path or self._queued.get(file_hash, "")

    def queue_roll(self, roll_id: str) -> None:
        if roll_id:
            self._queued_rolls.add(roll_id)

    def pending(self) -> int:
        return len(self._queued) + len(self._queued_rolls)

    def flush(self, folders: Collection[str] = ()) -> tuple[int, int]:
        """Write every queued frame and roll whose file is not current, and the roll of each of *folders*
        dated since the last flush. Returns (written, failed). A frame whose source is gone is skipped,
        and a folder that refuses a write is skipped for the session."""
        queued, self._queued = self._queued, {}
        queued_rolls, self._queued_rolls = self._queued_rolls, set()
        written = failed = 0
        for file_hash, source_path in queued.items():
            half = sidecar_half(file_hash) or 0
            source_path = source_path or self._repo.path_for_file_hash(file_hash) or ""
            folder = os.path.dirname(source_path)
            if not source_path or folder in self._unwritable_dirs or not os.path.exists(source_path):
                continue
            path = sidecar_path_for(source_path, half)
            try:
                payload = mirror_payload(self._repo, file_hash, read_payload(path), source_path=source_path)
                if payload is not None and write_payload(path, payload):
                    written += 1
            except OSError as exc:
                failed += 1
                self._unwritable_dirs.add(folder)
                logger.warning("Sidecar write failed in %s, skipping that folder: %s", folder, exc)
            except Exception:
                failed += 1
                logger.exception("Sidecar write failed for %s", source_path)
        for roll_id in queued_rolls | set(rolls.folder_rolls_holding(self._repo, folders)):
            stamp = rolls.roll_updated_at(self._repo, roll_id)
            if stamp is None or self._checked_rolls.get(roll_id) == stamp:
                continue
            found = plan_roll_file_write(self._repo, roll_id)
            folder = os.path.dirname(found[0]) if found is not None else ""
            try:
                if found is not None and folder not in self._unwritable_dirs and write_payload(*found):
                    written += 1
                self._checked_rolls[roll_id] = stamp
            except OSError as exc:
                failed += 1
                self._unwritable_dirs.add(folder)
                logger.warning("Roll file write failed in %s, skipping that folder: %s", folder, exc)
        return written, failed
