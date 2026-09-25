"""Reads a folder's ``.negpy`` sidecars and compares them with the DB off the UI thread.

The repository opens a connection per call, so its edit reads are safe here; every write
of the result (fills, merges) and the offer happen on the UI thread from ``planned``.
"""

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from negpy.kernel.system.logging import get_logger
from negpy.services.assets.sidecar import SidecarPlan, SidecarReader, plan_frame_sidecars

logger = get_logger(__name__)


class SidecarScanWorker(QObject):
    """One scan per discovery. ``planned`` carries the generation it was asked with; a scan
    whose generation is superseded stops before its next file and emits nothing."""

    planned = pyqtSignal(int, object)

    def __init__(self, repo) -> None:
        super().__init__()
        self._repo = repo
        self._reader = SidecarReader()
        self._wanted = 0

    def supersede(self, generation: int) -> None:
        """Called from the UI thread: only a scan of *generation* runs to the end."""
        self._wanted = generation

    @pyqtSlot(int, list)
    def scan(self, generation: int, assets: list) -> None:
        if generation != self._wanted:
            return
        try:
            plan = plan_frame_sidecars(self._repo, assets, self._reader, is_cancelled=lambda: self._wanted != generation)
        except Exception:
            logger.exception("Sidecar scan failed")
            plan = SidecarPlan()
        if plan is not None:
            self.planned.emit(generation, plan)
