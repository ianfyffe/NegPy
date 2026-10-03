"""A roll that follows its folder to where another computer renamed or moved it, found by the
roll file the folder carries."""

import os
from typing import Any, Callable, Iterable, Iterator, Optional

from negpy.services.assets import rolls
from negpy.services.assets.rehome import rehome_path_prefix
from negpy.services.assets.sidecar import claim_copy, is_copy_of, load_roll_sidecar, roll_sidecar_path, take_roll_file_identity

MOVED, COPY, UNSURE = "moved", "copy", "unsure"


def roll_moved_to(repo: Any, folder: str) -> Optional[tuple[str, str]]:
    """The roll here that *folder*'s roll file names (by ``roll_uid`` or a former folder name in the same
    parent), with ``MOVED``, ``COPY`` (both folders hold its file) or ``UNSURE``. None when *folder* is a
    roll's own or names no roll here."""
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


def _followed_name(name: str, old: str, new: str) -> Optional[str]:
    """The name a roll takes at *new* when its leaf is the one its folder gave it at *old*."""
    if rolls.name_leaf(name) == rolls.folder_roll_name(old):
        return rolls.with_leaf(name, rolls.folder_roll_name(new))
    return None


def follow_folder(repo: Any, roll_id: str, folder: str) -> str:
    """Point the roll, and every path stored under its folder, at *folder*, keeping the roll's
    id, and take the roll file's identity and newer name. A name the old folder gave the
    roll becomes the one *folder* gives it. Returns the old folder path."""
    entry = rolls.roll_for_id(repo, roll_id) or {}
    old = entry.get("folder_path") or ""
    label = _followed_name(entry.get("name") or "", old, folder)
    rehome_path_prefix(repo, old, folder)
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
