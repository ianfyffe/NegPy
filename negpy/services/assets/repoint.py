"""Folder moves: every path stored under a folder that was renamed or moved, rewritten to
the folder's new path. Edits, marks and history are keyed by content hash and never move;
only the stores that remember a path do."""

import json
import os
from typing import Any, Dict, Optional

from negpy.services.assets import rolls
from negpy.services.assets.composites import COMPOSITES_KEY

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
