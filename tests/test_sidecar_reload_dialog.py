"""Newer-sidecars dialog: which frames are loaded, which are declined, and a closed
dialog decides nothing."""

from negpy.desktop.view.widgets.sidecar_reload_dialog import SidecarReloadDialog
from negpy.domain.models import WorkspaceConfig
from negpy.services.assets.sidecar import RollSidecar, RollSidecarOffer, Sidecar, SidecarOffer


def _offers(n: int) -> list[SidecarOffer]:
    return [
        SidecarOffer({"name": f"f{i}.dng", "path": f"/p/f{i}.dng", "hash": f"h{i}"}, Sidecar(WorkspaceConfig(), saved_at=float(i)))
        for i in range(n)
    ]


def test_all_offers_start_checked_and_load_reports_the_checked_ones(qapp):
    offers = _offers(3)
    dlg = SidecarReloadDialog(offers)
    assert [o.asset["hash"] for o in dlg.selected_offers()] == ["h0", "h1", "h2"]
    dlg._checks[1][0].setChecked(False)
    dlg._load()
    assert dlg.decision == "load"
    assert [o.asset["hash"] for o in dlg.selected_offers()] == ["h0", "h2"]


def test_keep_mine_and_close(qapp):
    dlg = SidecarReloadDialog(_offers(1))
    dlg._keep()
    assert dlg.decision == "keep"
    closed = SidecarReloadDialog(_offers(1))
    closed.reject()
    assert closed.decision is None


def test_roll_settings_lead_and_load_with_the_frames(qapp):
    roll = RollSidecarOffer("r1", "Portra", RollSidecar(saved_at=5.0))
    dlg = SidecarReloadDialog([*_offers(2), roll])
    assert dlg._checks[0][0].text().startswith("Roll settings  ·  ")
    assert dlg.selected_offers()[0] is roll
    dlg._checks[1][0].setChecked(False)
    dlg._load()
    assert dlg.selected_offers() == [roll, dlg._checks[2][1]]
