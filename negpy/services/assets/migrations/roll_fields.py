"""One-time lock for fields that became roll defaults after frames already carried their
own value: White/Black Point and their per-layer trims (Normalization), and Highlight
Recovery (Raw Decode). No roll has a default for them yet, so a frame's own value still
renders; the first Roll push would overwrite it on every frame that is not locked. This
locks the card on each frame whose saved value differs from the default, in every roll
that holds it. Best-effort and idempotent: guarded by a done flag, and it never raises
into app startup.
"""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from typing import Collection

from negpy.domain.models import WorkspaceConfig
from negpy.features.geometry.models import with_auto_crop
from negpy.features.process.models import ProcessConfig
from negpy.kernel.system.logging import get_logger
from negpy.services.assets import rolls

logger = get_logger(__name__)

_DONE_FLAG = "roll_field_locks_migrated_v1"
_BASELINE_SPLIT_FLAG = "baseline_card_split_v1"
_CAST_REMOVAL_FLAG = "cast_removal_to_frames_v1"
_PAPER_SPLIT_FLAG = "paper_card_split_v1"
_AUTO_CROP_FLAG = "auto_crop_roll_card_v1"
_RAW_DECODE_SPLIT_FLAG = "raw_decode_linear_raw_v1"
_CROP_TO_FRAMES_FLAG = "crop_to_frames_v1"

# The Crop card's settings, which were the roll's "autocrop" card; Auto Crop was its "auto_crop" card.
_CROP_SHAPE_FIELDS = ("autocrop_mode", "autocrop_offset", "autocrop_rebate_trim", "autocrop_ratio", "autocrop_square")

# The Paper Response card's fields, which left the Tone card (settings_catalog.PAPER_FIELDS).
_PAPER_FIELDS = (
    "paper_profile",
    "paper_black",
    "paper_dmin",
    "midtone_gamma",
    "toe",
    "toe_width",
    "shoulder",
    "shoulder_width",
    "dye_separation",
    "separation_damping",
    *(
        f"{base}_trim_{ch}"
        for base in ("midtone_gamma", "toe", "toe_width", "shoulder", "shoulder_width", "dye_separation")
        for ch in ("red", "green", "blue")
    ),
)

NEW_ROLL_FIELDS = {
    "process": (
        "white_point_offset",
        "black_point_offset",
        "white_point_trim_red",
        "white_point_trim_green",
        "white_point_trim_blue",
        "black_point_trim_red",
        "black_point_trim_green",
        "black_point_trim_blue",
    ),
    "demosaic": ("highlight_reconstruction",),
}


def _rolls_for_row(repo, file_hash: str, file_path: str) -> list:
    """A fork belongs to its own roll alone; a shared edit to every roll holding its path."""
    base = rolls.unforked_hash(file_hash)
    if base != file_hash:
        return [file_hash[len(base) + len(rolls._FORK_SEP) :]]
    return rolls.rolls_containing_path(repo, file_path) if file_path else []


def migrate_new_roll_field_locks(repo) -> None:
    if repo.get_global_setting(_DONE_FLAG):
        return
    try:
        defaults = ProcessConfig()
        locks = []
        with closing(sqlite3.connect(repo.edits_db_path)) as conn:
            for file_hash, settings_json, file_path in conn.execute("SELECT file_hash, settings_json, file_path FROM file_settings"):
                try:
                    data = json.loads(settings_json) if settings_json else {}
                except (ValueError, TypeError):
                    continue
                cards = [
                    card
                    for card, fields in NEW_ROLL_FIELDS.items()
                    if any(data.get(f, getattr(defaults, f)) != getattr(defaults, f) for f in fields)
                ]
                if not cards:
                    continue
                for roll_id in _rolls_for_row(repo, file_hash, file_path):
                    locks.extend((roll_id, rolls.unforked_hash(file_hash), card) for card in cards)
        for roll_id, file_hash, card in locks:
            rolls.set_frame_override(repo, roll_id, file_hash, card, True, stamp=False)
    except Exception:
        logger.exception("New roll-field lock migration failed; continuing without it")
    repo.save_global_setting(_DONE_FLAG, True)


def _split_locks(entry: dict, old: str, new: str) -> None:
    """A frame locked on the *old* card is also locked on the *new* one split from it."""
    overrides = entry.get("frame_overrides", {})
    split = {h: sorted({*cards, new}) if old in cards else cards for h, cards in overrides.items()}
    if split != overrides:
        entry["frame_overrides"] = split


def _drop_locks(entry: dict, cards: Collection[str]) -> None:
    """Unlock *cards* on every frame of the roll; a frame left with no lock is dropped."""
    overrides = entry.get("frame_overrides", {})
    kept = {h: [c for c in locked if c not in cards] for h, locked in overrides.items()}
    kept = {h: locked for h, locked in kept.items() if locked}
    if kept != overrides:
        entry["frame_overrides"] = kept


def _defaults_to_seed(entry: dict, fields: Collection[str]) -> None:
    """Move the roll's defaults for *fields* into its frame seed."""
    defaults = entry.get("defaults", {})
    moved = {f: v for f, v in defaults.items() if f in fields}
    if moved:
        entry["defaults"] = {f: v for f, v in defaults.items() if f not in moved}
        entry["frame_seed"] = {**entry.get("frame_seed", {}), **moved}


def split_baseline_locks(entry: dict) -> None:
    """Use Luma/Color Average left the Normalization (``process``) card for their own
    ``baseline`` card. A frame locked on the old card keeps both halves of that lock, so
    nothing it renders changes."""
    _split_locks(entry, "process", "baseline")


def split_paper_push(entry: dict) -> None:
    """The paper curve controls left the Tone card for their own ``paper`` card. A whole-roll
    Tone apply recorded before the split moves its paper fields to ``paper``, so each card
    reads Roll and resets to the roll against its own fields alone."""
    rolls.move_section_push_fields(entry, "tone", "paper", _PAPER_FIELDS)


def split_raw_decode_locks(entry: dict) -> None:
    """Linear RAW left the Calibration (``sensor``) card for Raw Decode (``demosaic``). A
    frame locked on the old card keeps both halves of that lock, so nothing it renders changes."""
    _split_locks(entry, "sensor", "demosaic")


def cast_removal_to_seed(entry: dict) -> None:
    """Cast Removal is a Filtration field, not a roll card: its locks go, and the roll's value
    moves to the frame seed that a frame with no saved edit starts from."""
    _drop_locks(entry, ("cast_removal",))
    _defaults_to_seed(entry, ("cast_removal_strength",))


def crop_to_seed(entry: dict) -> None:
    """Crop is a frame card, not two roll cards: a whole-roll Geometry apply hands its crop fields
    to Crop, the cards' locks go, and the roll's crop values move to the frame seed."""
    rolls.move_section_push_fields(entry, "geometry", "autocrop", ("crop_rect", "crop_from_auto", "crop_detect_key"))
    _drop_locks(entry, ("autocrop", "auto_crop"))
    _defaults_to_seed(entry, (*_CROP_SHAPE_FIELDS, "crop_from_auto"))


# Card-key rewrites of one roll entry, in the order the cards changed. A roll file records how
# many its writer knew (CARD_KEYS_VERSION), and a reader runs the rest (migrate_roll_entry).
ENTRY_MIGRATIONS = (split_baseline_locks, split_paper_push, split_raw_decode_locks, cast_removal_to_seed, crop_to_seed)
CARD_KEYS_VERSION = len(ENTRY_MIGRATIONS)


def migrate_roll_entry(entry: dict, version: int) -> None:
    for migrate in ENTRY_MIGRATIONS[max(0, version) :]:
        migrate(entry)


def migrate_baseline_card_split(repo) -> None:
    """``split_baseline_locks`` over every roll. Runs before migrate_new_roll_field_locks,
    whose new ``process`` locks are about White/Black Point alone."""
    if repo.get_global_setting(_BASELINE_SPLIT_FLAG):
        return
    try:
        rolls.rewrite_entries(repo, split_baseline_locks)
    except Exception:
        logger.exception("Roll Analysis card split migration failed; continuing without it")
    repo.save_global_setting(_BASELINE_SPLIT_FLAG, True)


def migrate_cast_removal_to_frames(repo) -> None:
    """A saved frame that followed its roll's Cast Removal keeps that value as its own; then
    ``cast_removal_to_seed`` over every roll."""
    if repo.get_global_setting(_CAST_REMOVAL_FLAG):
        return
    try:
        saved = rolls.saved_rolls(repo)
        roll_values = {
            roll_id: e["defaults"]["cast_removal_strength"]
            for roll_id, e in saved.items()
            if "cast_removal_strength" in e.get("defaults", {})
        }
        if roll_values:
            with closing(sqlite3.connect(repo.edits_db_path)) as conn, conn:
                rows = conn.execute("SELECT file_hash, settings_json, file_path FROM file_settings").fetchall()
                for file_hash, settings_json, file_path in rows:
                    base = rolls.unforked_hash(file_hash)
                    roll_id = next(
                        (
                            r
                            for r in _rolls_for_row(repo, file_hash, file_path)
                            if r in roll_values and "cast_removal" not in rolls.frame_override_cards(repo, r, base)
                        ),
                        None,
                    )
                    if roll_id is None:
                        continue
                    try:
                        data = json.loads(settings_json) if settings_json else {}
                    except (ValueError, TypeError):
                        continue
                    data["cast_removal_strength"] = float(roll_values[roll_id])
                    conn.execute(
                        "UPDATE file_settings SET settings_json = ? WHERE file_hash = ?", (json.dumps(data, default=str), file_hash)
                    )
        rolls.rewrite_entries(repo, cast_removal_to_seed)
    except Exception:
        logger.exception("Cast Removal move to Filtration failed; continuing without it")
    repo.save_global_setting(_CAST_REMOVAL_FLAG, True)


def migrate_linear_raw_card_split(repo) -> None:
    """``split_raw_decode_locks`` over every roll."""
    if repo.get_global_setting(_RAW_DECODE_SPLIT_FLAG):
        return
    try:
        rolls.rewrite_entries(repo, split_raw_decode_locks)
    except Exception:
        logger.exception("Raw Decode card split migration failed; continuing without it")
    repo.save_global_setting(_RAW_DECODE_SPLIT_FLAG, True)


def migrate_paper_card_split(repo) -> None:
    """``split_paper_push`` over every roll."""
    if repo.get_global_setting(_PAPER_SPLIT_FLAG):
        return
    try:
        rolls.rewrite_entries(repo, split_paper_push)
    except Exception:
        logger.exception("Paper Response card split migration failed; continuing without it")
    repo.save_global_setting(_PAPER_SPLIT_FLAG, True)


def migrate_auto_crop_roll_locks(repo) -> None:
    """Auto Crop is a roll default (the ``auto_crop`` card). Locks that card on every frame saved with Auto Crop on, so the
    first Roll push of it off does not clear their crops. A hand-drawn crop needs no lock."""
    if repo.get_global_setting(_AUTO_CROP_FLAG):
        return
    try:
        locks = []
        with closing(sqlite3.connect(repo.edits_db_path)) as conn:
            for file_hash, settings_json, file_path in conn.execute("SELECT file_hash, settings_json, file_path FROM file_settings"):
                try:
                    data = json.loads(settings_json) if settings_json else {}
                except (ValueError, TypeError):
                    continue
                if not data.get("crop_from_auto"):
                    continue
                for roll_id in _rolls_for_row(repo, file_hash, file_path):
                    locks.append((roll_id, rolls.unforked_hash(file_hash)))
        for roll_id, file_hash in locks:
            rolls.set_frame_override(repo, roll_id, file_hash, "auto_crop", True, stamp=False)
    except Exception:
        logger.exception("Auto Crop roll-card lock migration failed; continuing without it")
    repo.save_global_setting(_AUTO_CROP_FLAG, True)


def migrate_crop_to_frames(repo) -> None:
    """A saved frame keeps the crop it resolved to through its roll as its own; then
    ``crop_to_seed`` over every roll. Runs after migrate_auto_crop_roll_locks, so a frame saved
    with Auto Crop on keeps it."""
    if repo.get_global_setting(_CROP_TO_FRAMES_FLAG):
        return
    try:
        saved = rolls.saved_rolls(repo)
        roll_values = {}
        for roll_id, entry in saved.items():
            values = {f: v for f, v in entry.get("defaults", {}).items() if f in (*_CROP_SHAPE_FIELDS, "crop_from_auto")}
            if values:
                roll_values[roll_id] = values
        if roll_values:
            with closing(sqlite3.connect(repo.edits_db_path)) as conn, conn:
                rows = conn.execute("SELECT file_hash, settings_json, file_path FROM file_settings").fetchall()
                for file_hash, settings_json, file_path in rows:
                    roll_id = next((r for r in _rolls_for_row(repo, file_hash, file_path) if r in roll_values), None)
                    if roll_id is None:
                        continue
                    try:
                        data = json.loads(settings_json) if settings_json else {}
                    except (ValueError, TypeError):
                        continue
                    before = WorkspaceConfig.from_flat_dict(data)
                    locked = rolls.frame_override_cards(repo, roll_id, rolls.unforked_hash(file_hash))
                    old, new = before.to_dict(), _with_roll_crop(before, roll_values[roll_id], locked).to_dict()
                    changed = {k: v for k, v in new.items() if old.get(k) != v}
                    if changed:
                        data.update(changed)
                        conn.execute(
                            "UPDATE file_settings SET settings_json = ? WHERE file_hash = ?", (json.dumps(data, default=str), file_hash)
                        )
        rolls.rewrite_entries(repo, crop_to_seed)
    except Exception:
        logger.exception("Crop move to the frames failed; continuing without it")
    repo.save_global_setting(_CROP_TO_FRAMES_FLAG, True)


def with_frame_seed(config: WorkspaceConfig, seed: dict) -> WorkspaceConfig:
    """*config* with the crop and Cast Removal of a roll's frame seed."""
    config = _with_roll_crop(config, seed, set())
    if "cast_removal_strength" in seed:
        config = replace(config, exposure=replace(config.exposure, cast_removal_strength=float(seed["cast_removal_strength"])))
    return config


def _with_roll_crop(config: WorkspaceConfig, values: dict, locked: set) -> WorkspaceConfig:
    """*config* as its roll's crop resolved it: the shape on an unlocked "autocrop" card, Auto Crop
    on an unlocked "auto_crop" card unless the frame holds a hand-drawn crop."""
    if "autocrop" not in locked:
        shape = {f: rolls.config_value(values[f]) for f in _CROP_SHAPE_FIELDS if f in values}
        config = replace(config, geometry=replace(config.geometry, **shape))
    geometry = config.geometry
    hand_drawn = geometry.crop_rect is not None and not geometry.crop_from_auto
    if "auto_crop" not in locked and not hand_drawn and "crop_from_auto" in values:
        on = bool(values["crop_from_auto"])
        if on != geometry.crop_from_auto:
            config = with_auto_crop(config, on)
    return config
