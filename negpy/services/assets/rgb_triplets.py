"""RGB-scan triplet membership: which green and blue exposures belong to each red one.

Grouping a folder reads the capture time and the mosaic of every file, so a roll that was
grouped once is re-attached from here while RGB Scan is on and only its loose files are
examined again. Keyed by the red exposure's path, across every roll. The store follows the
open files: a file that is open loose, or in another triplet, drops the entry that held it.
"""

from typing import Any, Dict, Iterable

TRIPLETS_KEY = "rgb_triplets_by_path"


def saved_triplets(repo: Any) -> Dict[str, list]:
    """``{red_path: [green_path, blue_path, align]}``, the shape discovery restores from."""
    saved = repo.get_global_setting(TRIPLETS_KEY, default=None)
    return dict(saved) if isinstance(saved, dict) else {}


def remember_triplets(repo: Any, assets: Iterable[dict]) -> None:
    """Make the store agree with ``assets`` for every file they hold; leave other files' entries."""
    assets = list(assets)
    held = {a[k] for a in assets for k in ("path", "green_path", "blue_path") if a.get(k)}
    store = saved_triplets(repo)
    updated = {red: entry for red, entry in store.items() if red not in held and not held.intersection(entry[:2])}
    for a in assets:
        if a.get("green_path") and a.get("blue_path"):
            updated[a["path"]] = [a["green_path"], a["blue_path"], bool(a.get("align", True))]
    if updated != store:
        repo.save_global_setting(TRIPLETS_KEY, updated)
