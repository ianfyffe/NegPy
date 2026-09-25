"""Folder moves: every path stored under a folder that was renamed or moved, rewritten to
the folder's new path, and a roll that follows its folder to where another computer moved
it. Edits, marks and history are keyed by content hash and never move; only the stores
that remember a path do."""

import json
import os
from typing import Any, Callable, Dict, Iterable, Iterator, Optional

from negpy.services.assets import rolls
from negpy.services.assets.composites import COMPOSITES_KEY
from negpy.services.assets.sidecar import claim_copy, is_copy_of, load_roll_sidecar, roll_sidecar_path, take_roll_file_identity

SESSION_FILES_KEY = "session_files"
SESSION_ACTIVE_KEY = "session_active_path"
SESSION_TRIPLETS_KEY = "session_triplets"
LIBRARY_ROOTS_KEY = "library_roots"

# Flat config keys that hold a source path, and those that hold lists of them.
_CONFIG_PATH = ("green_path", "blue_path")
_CONFIG_LISTS = ("stitch_paths", "hdr_paths", "stitch_triplets")


def moved_path(path: str, old: str, new: str) -> str:
    """*path* rebased from folder *old* onto *new* when it is *old* or under it, compared as
    ``rolls._folder_key`` compares folders; any other path comes back unchanged."""
    if not path or not old:
        return path
    norm, base = os.path.normpath(path), os.path.normpath(old)
    key, base_key = os.path.normcase(norm), os.path.normcase(base)
    if key == base_key:
        return new
    prefix = base_key.rstrip(os.sep) + os.sep
    if key.startswith(prefix):
        return os.path.join(new, norm[len(prefix) :])
    return path


def _moved_list(values: Any, old: str, new: str) -> Any:
    if not isinstance(values, list):
        return values
    return [moved_path(v, old, new) if isinstance(v, str) else _moved_list(v, old, new) for v in values]


def _repoint_rolls(repo: Any, old: str, new: str) -> None:
    store = rolls.saved_rolls(repo)
    changed = False
    for entry in store.values():
        if isinstance(entry.get("folder_path"), str) and (moved := moved_path(entry["folder_path"], old, new)) != entry["folder_path"]:
            entry["folder_path"] = moved
            changed = True
        for key in ("extra_paths", "member_paths"):
            if key in entry and (moved := _moved_list(entry[key], old, new)) != entry[key]:
                entry[key] = moved
                changed = True
    if changed:
        repo.save_global_setting(rolls.ROLLS_KEY, store)


def _repoint_composites(repo: Any, old: str, new: str) -> None:
    saved = repo.get_global_setting(COMPOSITES_KEY, default=None)
    if not isinstance(saved, dict):
        return
    updated: Dict[str, Any] = {}
    for path, entry in saved.items():
        if isinstance(entry, dict):
            entry = {**entry, **{k: _moved_list(entry[k], old, new) for k in ("paths", "triplets") if k in entry}}
        updated[moved_path(path, old, new)] = entry
    if updated != saved:
        repo.save_global_setting(COMPOSITES_KEY, updated)


def _repoint_setting(repo: Any, key: str, old: str, new: str) -> None:
    saved = repo.get_global_setting(key, default=None)
    if isinstance(saved, str):
        updated: Any = moved_path(saved, old, new)
    elif isinstance(saved, list):
        updated = _moved_list(saved, old, new)
    elif isinstance(saved, dict):
        updated = {moved_path(k, old, new): _moved_list(v, old, new) for k, v in saved.items()}
    else:
        return
    if updated != saved:
        repo.save_global_setting(key, updated)


def _moved_config(data: Dict[str, Any], old: str, new: str) -> Optional[Dict[str, Any]]:
    updated = dict(data)
    for key in _CONFIG_PATH:
        if isinstance(data.get(key), str):
            updated[key] = moved_path(data[key], old, new)
    for key in _CONFIG_LISTS:
        if key in data:
            updated[key] = _moved_list(data[key], old, new)
    return updated if updated != data else None


def repoint_folder(repo: Any, old: str, new: str) -> None:
    """Rewrite every stored path equal to *old* or under it to the same place under *new*:
    rolls (folders, extra and member paths), composites, the saved session, the import
    sources, dismissed folders and library roots, the path columns of edits, marks and
    embeddings, and the source paths inside saved configs. A saved edit keeps its
    ``updated_at``, because a path change is not an edit. Idempotent."""
    if not old or not new or os.path.normcase(os.path.normpath(old)) == os.path.normcase(os.path.normpath(new)):
        return
    _repoint_rolls(repo, old, new)
    _repoint_composites(repo, old, new)
    for key in (
        SESSION_FILES_KEY,
        SESSION_ACTIVE_KEY,
        SESSION_TRIPLETS_KEY,
        rolls.DISMISSED_FOLDERS_KEY,
        rolls.IMPORT_SOURCES_KEY,
        LIBRARY_ROOTS_KEY,
    ):
        _repoint_setting(repo, key, old, new)
    name = os.path.basename(os.path.normpath(old))
    repo.repoint_file_paths(name, lambda path: moved_path(path, old, new))
    repo.repoint_saved_configs(json.dumps(name)[1:-1], lambda data: _moved_config(data, old, new))


MOVED, COPY, UNSURE = "moved", "copy", "unsure"


def roll_moved_to(repo: Any, folder: str) -> Optional[tuple[str, str]]:
    """The roll here that *folder*'s roll file names, when *folder* is not yet its own, and
    ``MOVED`` (its folder is gone from a parent that is there), ``COPY`` (both folders hold its
    file) or ``UNSURE`` (another path to one folder, an offline share, a folder that cannot be
    told apart yet). The file names the roll by ``roll_uid``, or by a former folder name in the
    same parent. None otherwise."""
    if rolls.folder_roll_id_for_path(repo, folder) is not None:
        return None
    sidecar = load_roll_sidecar(folder)
    if sidecar is None:
        return None
    roll_id = rolls.roll_id_for_uid(repo, sidecar.roll_uid)
    if roll_id is not None:
        old = (rolls.roll_for_id(repo, roll_id) or {}).get("folder_path") or ""
        if is_copy_of(old, folder, sidecar.roll_uid):
            return roll_id, COPY
        return roll_id, MOVED if _moved_away(old) else UNSURE
    parent = os.path.dirname(os.path.normpath(folder))
    for name in reversed(sidecar.former_names):
        old = os.path.join(parent, name)
        roll_id = rolls.folder_roll_id_for_path(repo, old)
        if roll_id is not None and not os.path.isdir(old) and rolls.roll_uid(repo, roll_id) in ("", sidecar.roll_uid):
            return roll_id, MOVED
    return None


def _moved_away(old: str) -> bool:
    """Whether folder *old* is gone while its parent is there: a rename or a move, not a share
    that is offline."""
    return bool(old) and not os.path.isdir(old) and os.path.isdir(os.path.dirname(os.path.normpath(old)))


def _followed_name(repo: Any, name: str, old: str, new: str) -> Optional[str]:
    """The name a roll takes at *new* when *name* is the one its folder gave it at *old*."""
    if name == rolls.folder_roll_name(old):
        return rolls.folder_roll_name(new)
    for source in rolls.import_sources(repo):
        if rolls.under_folder(old, source) and name == rolls.imported_roll_name(old, source):
            new_source = moved_path(source, old, new)
            return rolls.imported_roll_name(new, new_source) if rolls.under_folder(new, new_source) else rolls.folder_roll_name(new)
    return None


def follow_folder(repo: Any, roll_id: str, folder: str) -> str:
    """Point the roll, and every path stored under its folder, at *folder*, keeping the roll's
    id, and take the roll file's identity and newer name. A name the old folder gave the
    roll becomes the one *folder* gives it. Returns the old folder path."""
    entry = rolls.roll_for_id(repo, roll_id) or {}
    old = entry.get("folder_path") or ""
    label = _followed_name(repo, entry.get("name") or "", old, folder)
    repoint_folder(repo, old, folder)
    if label:
        rolls.relabel_roll(repo, roll_id, label)
    sidecar = load_roll_sidecar(folder)
    if sidecar is not None:
        take_roll_file_identity(repo, roll_id, sidecar)
    return old


def recognize_roll_folder(repo: Any, folder: str, name: str = "", on_moved: Optional[Callable[[str, str], None]] = None) -> Optional[str]:
    """``rolls.recognize_folder``, except that a folder another computer moved takes over its
    roll here (*on_moved* gets the old and new path), a copy becomes a roll of its own
    (``sidecar.claim_copy``), and an ``UNSURE`` folder is left for a later pass: None."""
    moved = roll_moved_to(repo, folder)
    if moved is not None and moved[1] == MOVED:
        old = follow_folder(repo, moved[0], folder)
        if on_moved is not None:
            on_moved(old, folder)
        return moved[0]
    if moved is not None and moved[1] == UNSURE:
        return None
    known = rolls.folder_roll_id_for_path(repo, folder)
    roll_id = rolls.recognize_folder(repo, folder, name)
    if moved is not None:
        claim_copy(repo, roll_id)
    elif (known is None or not rolls.roll_uid(repo, roll_id)) and (sidecar := load_roll_sidecar(folder)) is not None:
        take_roll_file_identity(repo, roll_id, sidecar)
    return roll_id


def _candidates(old: str, roots: Iterable[str], filters: list) -> Iterator[str]:
    """The old folder's siblings, then every roll folder under *roots*, each walked only when
    the ones before it did not match."""
    parent = os.path.dirname(os.path.normpath(old))
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        names = []
    yield from (os.path.join(parent, n) for n in names if not n.startswith(".") and os.path.isdir(os.path.join(parent, n)))
    for root in roots:
        yield from rolls.iter_roll_folders(root, filters)


def find_moved_folder(repo: Any, roll_id: str, roots: Iterable[str]) -> Optional[str]:
    """Where the folder roll's folder went: a sibling of the old folder, else, for a roll with a
    uid, a roll folder under *roots*, whose roll file names this roll. Reads only roll files.
    None unless the folder is gone from a parent that is there, or when nothing names it."""
    entry = rolls.roll_for_id(repo, roll_id) or {}
    old = entry.get("folder_path") or ""
    if entry.get("kind") != "folder" or not _moved_away(old):
        return None
    seen: set = set()
    for folder in _candidates(old, roots if entry.get("roll_uid") else (), rolls.discovery_filters(repo)):
        key = os.path.normcase(os.path.normpath(folder))
        if key not in seen and os.path.isfile(roll_sidecar_path(folder)) and roll_moved_to(repo, folder) == (roll_id, MOVED):
            return folder
        seen.add(key)
    return None
