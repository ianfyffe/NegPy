"""Path-keyed records follow a folder renamed on disk."""

import os
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from negpy.services.assets import composites, rolls, triplets

if TYPE_CHECKING:
    from negpy.domain.models import WorkspaceConfig

_LIBRARY_ROOTS_KEY = "library_roots"  # AppController.library_roots

# The source-path fields of each config section. A flat config holds the same keys.
_SOURCE_PATHS = {"rgbscan": ("green_path", "blue_path"), "stitch": ("stitch_paths", "stitch_triplets"), "hdr": ("hdr_paths", "hdr_anchor")}


def moved_path(path: str, old_prefix: str, new_prefix: str) -> str:
    """*path* under *new_prefix* when it is the folder *old_prefix* or under it; any other path unchanged."""
    old_prefix, new_prefix = old_prefix.rstrip("/\\"), new_prefix.rstrip("/\\")
    if path == old_prefix or path.startswith(old_prefix + os.sep):
        return new_prefix + path[len(old_prefix) :]
    return path


def moved_value(value: Any, move: Callable[[str], str]) -> Any:
    """*value* with every string in it, at any list or tuple depth, passed through *move*."""
    if isinstance(value, str):
        return move(value)
    if isinstance(value, (list, tuple)):
        return type(value)(moved_value(v, move) for v in value)
    return value


def moved_config(config: "WorkspaceConfig", move: Callable[[str], str]) -> "WorkspaceConfig":
    """*config* with its triplet, stitch and HDR source paths passed through *move*; every other field unchanged."""
    sections = {}
    for name, fields in _SOURCE_PATHS.items():
        section = getattr(config, name)
        sections[name] = replace(section, **{f: moved_value(getattr(section, f), move) for f in fields})
    return replace(config, **sections)


def _moved_flat_config(data: Dict[str, Any], move: Callable[[str], str]) -> Optional[Dict[str, Any]]:
    keys = [key for fields in _SOURCE_PATHS.values() for key in fields if key in data]
    updated = {**data, **{key: moved_value(data[key], move) for key in keys}}
    return updated if updated != data else None


def rehome_path_prefix(repo: Any, old_prefix: str, new_prefix: str) -> None:
    """Repoint the edits DB, the rolls, the composites, the Trichrome triplets, the library roots and the
    source paths inside saved configs from *old_prefix* to *new_prefix*.
    Content hashes do not move, but these stores find a frame by its path."""
    old_prefix, new_prefix = old_prefix.rstrip("/\\"), new_prefix.rstrip("/\\")
    if not old_prefix or not new_prefix or old_prefix == new_prefix:
        return

    def move(path: str) -> str:
        return moved_path(path, old_prefix, new_prefix)

    repo.rehome_path_prefix(old_prefix, new_prefix)
    rolls.rehome_paths(repo, move)
    composites.rehome_paths(repo, move)
    triplets.rehome_paths(repo, move)
    repo.rehome_saved_configs(old_prefix, lambda data: _moved_flat_config(data, move))
    roots = repo.get_global_setting(_LIBRARY_ROOTS_KEY, []) or []
    if isinstance(roots, list):
        moved = list(dict.fromkeys(moved_value(roots, move)))
        if moved != roots:
            repo.save_global_setting(_LIBRARY_ROOTS_KEY, moved)
