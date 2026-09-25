import os
import time
from typing import Optional

from PyQt6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QPushButton, QScrollArea, QVBoxLayout, QWidget

from negpy.desktop.view.styles.templates import hint_label, pin_dialog_default
from negpy.services.assets.sidecar import RollSidecarOffer, SidecarOffer


_VISIBLE_ROWS = 12


class SidecarReloadDialog(QDialog):
    """Sidecars that differ from the state on this computer: load which? Roll settings rows
    come first.

    ``decision`` is "load" (the checked rows; the rest are declined), "keep" (all
    declined) or None when the dialog was closed, which leaves every offer open.
    """

    def __init__(self, offers: list[SidecarOffer | RollSidecarOffer], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Newer Sidecars")
        self.setMinimumWidth(320)
        self.decision: Optional[str] = None

        roll_offers = [o for o in offers if isinstance(o, RollSidecarOffer)]
        offers = [*roll_offers, *(o for o in offers if not isinstance(o, RollSidecarOffer))]
        frames = len(offers) > len(roll_offers)
        if not roll_offers:
            lead = "These frames have a .negpy sidecar saved after the edit on this computer. Load replaces the edit here"
        elif frames:
            lead = "The folder's .negpy-roll file and these frames' sidecars were saved after the settings on this computer. Load replaces the settings here"
        else:
            lead = "The folder's .negpy-roll file differs from the roll settings on this computer. Load replaces the roll settings here"
        root = QVBoxLayout(self)
        root.addWidget(hint_label(lead + "; Keep Mine leaves the files alone and does not ask again for these versions."))

        self._checks: list[tuple[QCheckBox, SidecarOffer | RollSidecarOffer]] = []
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        for offer in offers:
            if isinstance(offer, RollSidecarOffer):
                name = f"Roll settings: {offer.name}" if len(roll_offers) > 1 else "Roll settings"
            else:
                name = offer.asset.get("name") or os.path.basename(offer.asset["path"])
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(offer.sidecar.saved_at or 0.0))
            box = QCheckBox(f"{name}  ·  {when}")
            box.setChecked(True)
            self._checks.append((box, offer))
            body_layout.addWidget(box)
        body_layout.addStretch()

        if len(self._checks) <= _VISIBLE_ROWS:
            root.addWidget(body)
        else:
            # A long roll scrolls at _VISIBLE_ROWS rows instead of growing off the screen.
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(body)
            row_h = self._checks[0][0].sizeHint().height() + body_layout.spacing()
            scroll.setFixedHeight(_VISIBLE_ROWS * row_h)
            root.addWidget(scroll)

        footer = QHBoxLayout()
        footer.addStretch()
        keep_btn = QPushButton("Keep Mine")
        keep_btn.clicked.connect(self._keep)
        load_btn = QPushButton("Load Selected")
        load_btn.clicked.connect(self._load)
        footer.addWidget(keep_btn)
        footer.addWidget(load_btn)
        pin_dialog_default(load_btn, keep_btn)
        root.addLayout(footer)

    def selected_offers(self) -> list[SidecarOffer | RollSidecarOffer]:
        return [offer for box, offer in self._checks if box.isChecked()]

    def _load(self) -> None:
        self.decision = "load"
        self.accept()

    def _keep(self) -> None:
        self.decision = "keep"
        self.reject()
