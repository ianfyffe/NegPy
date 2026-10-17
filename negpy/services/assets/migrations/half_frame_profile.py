"""One-time move of the shared Half Frame split profile onto each roll in Half Frame mode.

The shared profile applied to every roll without an override. Each roll in Half Frame mode
that has no profile of its own takes it, so its frames keep their split, and the shared key
is cleared, so a roll that has none detects its gutters. Best-effort; it never raises into
app startup.
"""

from negpy.kernel.system.logging import get_logger
from negpy.services.assets import rolls

logger = get_logger(__name__)

_SHARED_KEY = "half_frame_profile"
_MODE_BY_ROLL_KEY = "half_frame_mode_by_roll"


def migrate_shared_half_frame_profile(repo) -> None:
    shared = repo.get_global_setting(_SHARED_KEY, None)
    if not isinstance(shared, dict) or not shared:
        return
    try:
        modes = repo.get_global_setting(_MODE_BY_ROLL_KEY, None) or {}
        for roll_id, enabled in modes.items():
            if enabled and rolls.roll_half_frame_profile(repo, roll_id) is None:
                rolls.set_roll_half_frame_profile(repo, roll_id, shared)
    except Exception:
        logger.exception("Half-frame profile migration failed; continuing without it")
        return
    repo.save_global_setting(_SHARED_KEY, None)
