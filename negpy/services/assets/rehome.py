"""Path-keyed records follow a folder renamed on disk."""

import json
import os
from typing import Any, Callable, Dict, Optional

from negpy.services.assets import composites, rolls, triplets

# Global settings that hold a path, a list of paths, or a dict keyed by path.
_PATH_SETTINGS = (
    "session_files",
    "session_active_path",
    "session_triplets",
    rolls.DISMISSED_FOLDERS_KEY,
    rolls.IMPORT_SOURCES_KEY,
    "library_roots",
)
# Flat config keys that hold a source path, or lists of them.
_CONFIG_PATHS = ("green_path", "blue_path", "hdr_anchor", "stitch_paths", "hdr_paths", "stitch_triplets")


def moved_path(path: str, old_prefix: str, new_prefix: str) -> str:
    """*path* under *new_prefix* when it is the folder *old_prefix* or under it; any other path unchanged."""
    old_prefix, new_prefix = old_prefix.rstrip("/\\"), new_prefix.rstrip("/\\")
    if old_prefix and (path == old_prefix or path.startswith(old_prefix + os.sep)):
        return new_prefix + path[len(old_prefix) :]
    return path


def _moved_value(value: Any, move: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return move(value)
    if isinstance(value, list):
        return [_moved_value(v, move) for v in value]
    return value


def _rehome_setting(repo: Any, key: str, move: Callable[[str], str]) -> None:
    saved = repo.get_global_setting(key, default=None)
    if isinstance(saved, dict):
        updated = {move(k): _moved_value(v, move) for k, v in saved.items()}
    else:
        updated = _moved_value(saved, move)
    if updated != saved:
        repo.save_global_setting(key, updated)


def _moved_config(data: Dict[str, Any], move: Callable[[str], str]) -> Optional[Dict[str, Any]]:
    updated = {**data, **{key: _moved_value(data[key], move) for key in _CONFIG_PATHS if key in data}}
    return updated if updated != data else None


def rehome_path_prefix(repo: Any, old_prefix: str, new_prefix: str) -> None:
    """Repoint the edits DB, the rolls, the composites, the Trichrome triplets, the saved
    session, the library lists and the source paths inside saved configs from *old_prefix*
    to *new_prefix*. Content hashes do not move, but these stores find a frame by its path."""
    old_prefix, new_prefix = old_prefix.rstrip("/\\"), new_prefix.rstrip("/\\")
    if not old_prefix or not new_prefix or old_prefix == new_prefix:
        return

    def move(path: str) -> str:
        return moved_path(path, old_prefix, new_prefix)

    repo.rehome_path_prefix(old_prefix, new_prefix)
    rolls.rehome_paths(repo, move)
    composites.rehome_paths(repo, move)
    triplets.rehome_paths(repo, move)
    for key in _PATH_SETTINGS:
        _rehome_setting(repo, key, move)
    repo.rehome_saved_configs(json.dumps(old_prefix)[1:-1], lambda data: _moved_config(data, move))
