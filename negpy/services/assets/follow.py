"""A folder roll that follows its folder to the name another computer gave it, found by the
``former_names`` the folder's roll file carries."""

import os
from typing import Any, Optional

from negpy.services.assets import rolls
from negpy.services.assets.sidecar import leaf_names, load_roll_sidecar, roll_sidecar_path


def former_names(folder: str) -> list:
    """The leaf names *folder* had, from its roll file, newest last."""
    sidecar = load_roll_sidecar(folder)
    return leaf_names(sidecar.state.get("former_names")) if sidecar is not None else []


def roll_moved_to(repo: Any, folder: str) -> Optional[str]:
    """The folder roll here that *folder* was before another computer renamed it: the roll's folder is
    a sibling of *folder* named in its roll file's former names, and it is gone. None when a roll here
    holds *folder*."""
    if rolls.folder_roll_id_for_path(repo, folder) is not None:
        return None
    parent = os.path.dirname(os.path.normpath(folder))
    for name in reversed(former_names(folder)):
        old = os.path.join(parent, name)
        roll_id = rolls.folder_roll_id_for_path(repo, old)
        if roll_id is not None and not os.path.isdir(old):
            return roll_id
    return None


def moved_folder(repo: Any, roll_id: str) -> Optional[str]:
    """Where another computer renamed the folder roll's missing folder: the sibling whose roll file
    names it (``roll_moved_to``). None while the folder or its parent is missing."""
    entry = rolls.roll_for_id(repo, roll_id) or {}
    old = entry.get("folder_path") or ""
    parent = os.path.dirname(os.path.normpath(old)) if old else ""
    if entry.get("kind") != "folder" or not parent or os.path.isdir(old) or not os.path.isdir(parent):
        return None
    for name in sorted(os.listdir(parent)):
        candidate = os.path.join(parent, name)
        if os.path.isfile(roll_sidecar_path(candidate)) and roll_moved_to(repo, candidate) == roll_id:
            return candidate
    return None
