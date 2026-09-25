import json
import os
from dataclasses import replace

import pytest

from negpy.domain.models import WorkspaceConfig
from negpy.features.exposure.models import ExposureConfig
from negpy.features.geometry.models import GeometryConfig
from negpy.features.local.models import LocalAdjustmentsConfig, LocalMask
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.services.assets.sidecar import (
    DECLINED_KEY,
    Sidecar,
    SidecarMirror,
    SidecarWorkPrint,
    decline_sidecar_offers,
    load_or_promote,
    load_sidecar,
    newer_sidecar,
    pending_sidecar_offers,
    promote_sidecar,
    read_frame_sidecars,
    sidecar_from_repo,
    sidecar_path_for,
    write_sidecar,
)


def _rich_config() -> WorkspaceConfig:
    """A config exercising scalar + crop + local-mask paths, so the round trip is meaningful."""
    return WorkspaceConfig(
        exposure=ExposureConfig(density=0.42, grade=130.0),
        geometry=GeometryConfig(fine_rotation=1.5, crop_rect=(0.1, 0.2, 0.8, 0.9)),
        local=LocalAdjustmentsConfig(masks=(LocalMask(vertices=((0.0, 0.0), (0.5, 0.5)), stops=-0.7, feather=0.05),)),
    )


def _write(src: str, cfg: WorkspaceConfig, half: int = 0, **kw) -> str:
    return write_sidecar(src, Sidecar(config=cfg, **kw), half=half)


def test_sidecar_path_for_next_to_source():
    assert sidecar_path_for("/photos/IMG_001.NEF") == os.path.join("/photos", "IMG_001.negpy")


def test_roundtrip_next_to_source(tmp_path):
    src = str(tmp_path / "IMG_001.NEF")
    cfg = _rich_config()
    path = _write(src, cfg)

    assert path == str(tmp_path / "IMG_001.negpy")
    assert os.path.exists(path)

    loaded = load_sidecar(src)
    assert loaded is not None
    d = loaded.config.to_dict()
    assert d["density"] == 0.42
    assert d["grade"] == 130.0
    assert tuple(d["crop_rect"]) == (0.1, 0.2, 0.8, 0.9)
    masks = d["local_masks"]["masks"]
    assert len(masks) == 1
    assert masks[0]["stops"] == -0.7
    assert masks[0]["feather"] == 0.05


def test_load_missing_returns_none(tmp_path):
    assert load_sidecar(str(tmp_path / "nope.NEF")) is None


def test_load_malformed_returns_none(tmp_path):
    src = str(tmp_path / "IMG_003.NEF")
    with open(sidecar_path_for(src), "w", encoding="utf-8") as f:
        f.write("{ not valid json")
    assert load_sidecar(src) is None


@pytest.fixture()
def repo(tmp_path):
    r = StorageRepository(str(tmp_path / "edits.db"), str(tmp_path / "settings.db"))
    r.initialize()
    return r


def test_load_or_promote_promotes_sidecar_to_db(tmp_path, repo):
    src = str(tmp_path / "IMG_004.NEF")
    _write(src, _rich_config())

    assert repo.load_file_settings("h4") is None  # DB starts empty
    loaded = load_or_promote(repo, "h4", src)
    assert loaded is not None
    assert loaded.exposure.density == 0.42

    # Promotion: the DB now holds it, so a later load never needs the sidecar.
    promoted = repo.load_file_settings("h4")
    assert promoted is not None
    assert promoted.exposure.density == 0.42


def test_load_or_promote_db_wins(tmp_path, repo):
    src = str(tmp_path / "IMG_005.NEF")
    _write(src, replace(_rich_config(), exposure=ExposureConfig(density=0.11)), saved_at=9e9)
    repo.save_file_settings("h5", replace(_rich_config(), exposure=ExposureConfig(density=0.99)))

    loaded = load_or_promote(repo, "h5", src)
    assert loaded is not None
    assert loaded.exposure.density == 0.99  # DB value, sidecar ignored


def test_load_or_promote_none_when_neither(tmp_path, repo):
    assert load_or_promote(repo, "h6", str(tmp_path / "IMG_006.NEF")) is None


def test_write_payload_is_envelope_around_to_dict_json(tmp_path):
    src = str(tmp_path / "IMG_007.NEF")
    cfg = _rich_config()
    _write(src, cfg, saved_at=123.5, source_hash="h7", mark="keeper")
    with open(sidecar_path_for(src), "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["sidecar_format"] == 3
    assert data["saved_at"] == 123.5
    assert data["source_hash"] == "h7"
    assert data["mark"] == "keeper"
    assert data["edit"] == json.loads(json.dumps(cfg.to_dict(), default=str))
    assert data["work_prints"] == {}
    assert data["roll_locks"] is None


def test_format1_bare_config_still_loads(tmp_path):
    src = str(tmp_path / "IMG_007b.NEF")
    with open(sidecar_path_for(src), "w", encoding="utf-8") as f:
        json.dump(_rich_config().to_dict(), f, default=str)
    loaded = load_sidecar(src)
    assert loaded is not None
    assert loaded.saved_at is None
    assert loaded.mark is None
    assert loaded.config.exposure.density == 0.42


def test_envelope_carries_mark_and_work_prints(tmp_path):
    src = str(tmp_path / "IMG_007c.NEF")
    wp = SidecarWorkPrint(50.0, replace(_rich_config(), exposure=ExposureConfig(density=0.2)))
    _write(src, _rich_config(), saved_at=100.0, mark="excluded", work_prints={"soft": wp})
    loaded = load_sidecar(src)
    assert loaded is not None
    assert loaded.saved_at == 100.0
    assert loaded.mark == "excluded"
    assert loaded.work_prints["soft"].created_at == 50.0
    assert loaded.work_prints["soft"].config.exposure.density == 0.2


def test_envelope_without_edit_is_malformed(tmp_path):
    src = str(tmp_path / "IMG_007d.NEF")
    with open(sidecar_path_for(src), "w", encoding="utf-8") as f:
        json.dump({"sidecar_format": 2, "saved_at": 1.0}, f)
    assert load_sidecar(src) is None


def test_unknown_mark_is_dropped(tmp_path):
    src = str(tmp_path / "IMG_007e.NEF")
    with open(sidecar_path_for(src), "w", encoding="utf-8") as f:
        json.dump({"sidecar_format": 2, "saved_at": 1.0, "edit": _rich_config().to_dict(), "mark": "starred"}, f, default=str)
    loaded = load_sidecar(src)
    assert loaded is not None and loaded.mark is None


def test_sidecar_from_repo_matches_row(repo):
    repo.save_file_settings("h_repo", _rich_config(), file_path="/p/a.NEF", updated_at=77.0)
    repo.save_file_mark("h_repo", "keeper", file_path="/p/a.NEF")
    repo.save_work_print("h_repo", "v1", replace(_rich_config(), exposure=ExposureConfig(density=0.3)), created_at=5.0)
    sc = sidecar_from_repo(repo, "h_repo")
    assert sc is not None
    assert sc.saved_at == 77.0
    assert sc.source_hash == "h_repo"
    assert sc.mark == "keeper"
    assert sc.work_prints["v1"] == SidecarWorkPrint(5.0, repo.load_work_print("h_repo", "v1"))
    assert sidecar_from_repo(repo, "nope") is None


def test_promote_stamps_row_with_sidecar_time_and_merges_work_prints(repo):
    repo.save_file_settings("h_p", _rich_config(), file_path="/p/b.NEF", updated_at=10.0)
    repo.save_work_print("h_p", "mine", _rich_config(), created_at=1.0)
    theirs = Sidecar(
        config=replace(_rich_config(), exposure=ExposureConfig(density=0.5)),
        saved_at=20.0,
        mark="excluded",
        mark_at=20.0,
        work_prints={"theirs": SidecarWorkPrint(2.0, _rich_config())},
    )
    promote_sidecar(repo, "h_p", "/p/b.NEF", theirs)
    record = repo.load_file_record("h_p")
    assert record is not None
    assert record[0].exposure.density == 0.5
    assert record[1] == 20.0
    assert repo.load_file_mark("h_p") == "excluded"
    assert set(repo.list_work_prints("h_p")) == {"mine", "theirs"}


def test_promote_format1_keeps_local_mark(repo):
    repo.save_file_mark("h_p1", "keeper", file_path="/p/c.NEF")
    promote_sidecar(repo, "h_p1", "/p/c.NEF", Sidecar(config=_rich_config()))
    assert repo.load_file_mark("h_p1") == "keeper"
    record = repo.load_file_record("h_p1")
    assert record is not None and record[1] > 0


def test_newer_sidecar_only_when_saved_after_row(tmp_path, repo):
    src = str(tmp_path / "IMG_n.NEF")
    repo.save_file_settings("h_n", _rich_config(), file_path=src, updated_at=100.0)
    _write(src, _rich_config(), saved_at=50.0)
    assert newer_sidecar(repo, "h_n", src) is None
    _write(src, _rich_config(), saved_at=100.0)
    assert newer_sidecar(repo, "h_n", src) is None
    _write(src, _rich_config(), saved_at=100.5)
    found = newer_sidecar(repo, "h_n", src)
    assert found is not None and found.saved_at == 100.5


def test_newer_sidecar_ignores_format1_and_db_miss(tmp_path, repo):
    src = str(tmp_path / "IMG_n1.NEF")
    with open(sidecar_path_for(src), "w", encoding="utf-8") as f:
        json.dump(_rich_config().to_dict(), f, default=str)
    repo.save_file_settings("h_n1", _rich_config(), file_path=src, updated_at=0.0)
    assert newer_sidecar(repo, "h_n1", src) is None
    _write(str(tmp_path / "IMG_n2.NEF"), _rich_config(), saved_at=9e9)
    assert newer_sidecar(repo, "h_n2", str(tmp_path / "IMG_n2.NEF")) is None


def test_pending_offers_skip_composites_misses_and_declined(tmp_path, repo):
    newer = str(tmp_path / "newer.NEF")
    older = str(tmp_path / "older.NEF")
    missing = str(tmp_path / "missing.NEF")
    stitched = str(tmp_path / "stitched.NEF")
    for path, h in ((newer, "h_new"), (older, "h_old"), (stitched, "h_st")):
        repo.save_file_settings(h, _rich_config(), file_path=path, updated_at=100.0)
    _write(newer, _rich_config(), saved_at=200.0)
    _write(older, _rich_config(), saved_at=50.0)
    _write(missing, _rich_config(), saved_at=200.0)
    _write(stitched, _rich_config(), saved_at=200.0)
    assets = [
        {"name": "newer", "path": newer, "hash": "h_new"},
        {"name": "older", "path": older, "hash": "h_old"},
        {"name": "missing", "path": missing, "hash": "h_miss"},
        {"name": "stitched", "path": stitched, "hash": "h_st", "stitch_paths": [newer]},
    ]
    offers = pending_sidecar_offers(repo, assets)
    assert [o.asset["hash"] for o in offers] == ["h_new"]
    assert offers[0].sidecar.saved_at == 200.0

    decline_sidecar_offers(repo, offers)
    assert repo.get_global_setting(DECLINED_KEY) == {"h_new": 200.0}
    assert pending_sidecar_offers(repo, assets) == []
    # A later save to the file asks again.
    _write(newer, _rich_config(), saved_at=300.0)
    assert [o.sidecar.saved_at for o in pending_sidecar_offers(repo, assets)] == [300.0]


def test_promote_unedited_fills_only_frames_with_no_edit_here(tmp_path, repo):
    fresh = str(tmp_path / "fresh.NEF")
    edited = str(tmp_path / "edited.NEF")
    moved = str(tmp_path / "moved.NEF")
    bare = str(tmp_path / "bare.NEF")
    stitched = str(tmp_path / "stitched.NEF")
    repo.save_file_settings("h_edit", WorkspaceConfig(), file_path=edited, updated_at=100.0)
    repo.save_file_settings("h_moved_old", WorkspaceConfig(), file_path=moved, updated_at=100.0)
    for path in (fresh, edited, moved, stitched):
        _write(path, _rich_config(), saved_at=200.0)
    _write(fresh, _rich_config(), half=2, saved_at=200.0)
    assets = [
        {"name": "fresh", "path": fresh, "hash": "h_fresh"},
        {"name": "fresh [2]", "path": fresh, "hash": "h_fresh#2", "half": 2},
        {"name": "edited", "path": edited, "hash": "h_edit"},
        {"name": "moved", "path": moved, "hash": "h_moved_new"},
        {"name": "bare", "path": bare, "hash": "h_bare"},
        {"name": "stitched", "path": stitched, "hash": "h_st", "stitch_paths": [fresh]},
    ]

    assert read_frame_sidecars(repo, assets) == (["h_fresh", "h_fresh#2"], [])

    assert repo.load_file_record("h_fresh") == (_rich_config(), 200.0)
    assert repo.load_file_settings("h_edit") == WorkspaceConfig()
    assert repo.load_file_settings("h_moved_new") is None
    assert repo.load_file_settings("h_st") is None
    assert read_frame_sidecars(repo, assets) == ([], [])


def test_pending_offers_use_half_naming(tmp_path, repo):
    src = str(tmp_path / "strip.NEF")
    repo.save_file_settings("h#2", _rich_config(), file_path=src, updated_at=1.0)
    _write(src, _rich_config(), half=2, saved_at=2.0)
    _write(src, _rich_config(), saved_at=2.0)
    offers = pending_sidecar_offers(repo, [{"name": "strip [2]", "path": src, "hash": "h#2", "half": 2}])
    assert len(offers) == 1


def test_legacy_toggle_keys_are_dropped():
    # The mirror toggle is an app-wide preference now, so both its current name and its
    # pre-rename spelling are dropped from a saved edit rather than warned about.
    export = WorkspaceConfig().export
    assert not hasattr(export, "sidecars_enabled")
    for legacy in ("sidecars_enabled", "export_sidecars_enabled"):
        cfg = WorkspaceConfig.from_flat_dict({legacy: True})
        assert not hasattr(cfg.export, legacy)


def test_mirror_writes_row_time_and_dedups(tmp_path, repo):
    src = str(tmp_path / "IMG_m.NEF")
    repo.save_file_settings("h_m", _rich_config(), file_path=src, updated_at=42.0)
    mirror = SidecarMirror(repo)
    mirror.mark_dirty("h_m", src)
    mirror.mark_dirty("h_m", src)
    assert mirror.pending() == 1
    assert mirror.flush() == (1, 0)
    assert mirror.pending() == 0
    loaded = load_sidecar(src)
    assert loaded is not None and loaded.saved_at == 42.0 and loaded.source_hash == "h_m"
    # Equal times: the mirrored file is not "newer" than its own row.
    assert newer_sidecar(repo, "h_m", src) is None


def test_mirror_skips_unsaved_and_half_naming(tmp_path, repo):
    src = str(tmp_path / "IMG_h.NEF")
    repo.save_file_settings("h_h#1", _rich_config(), file_path=src)
    mirror = SidecarMirror(repo)
    mirror.mark_dirty("h_h#1", src, half=1)
    mirror.mark_dirty("unsaved", src)
    assert mirror.flush() == (1, 0)
    assert os.path.exists(sidecar_path_for(src, half=1))
    assert not os.path.exists(sidecar_path_for(src))


def test_roll_fork_never_reads_or_writes_the_sidecar(tmp_path, repo):
    src = str(tmp_path / "IMG_f.NEF")
    repo.save_file_settings("h_f#roll:r1", _rich_config(), file_path=src, updated_at=100.0)
    mirror = SidecarMirror(repo)
    mirror.mark_dirty("h_f#roll:r1", src)
    assert mirror.pending() == 0

    _write(src, _rich_config(), saved_at=200.0)
    fork = {"name": "f", "path": src, "hash": "h_f#roll:r1"}
    assert pending_sidecar_offers(repo, [fork]) == []
    repo.delete_file_settings("h_f#roll:r1")
    assert read_frame_sidecars(repo, [fork]) == ([], [])
    assert repo.load_file_record("h_f#roll:r1") is None


def test_touch_file_settings_advances_updated_at_only(tmp_path, repo):
    src = str(tmp_path / "IMG_t.NEF")
    cfg = _rich_config()
    repo.save_file_settings("h_t", cfg, file_path=src, updated_at=10.0)
    repo.touch_file_settings("h_t", updated_at=20.0)
    record = repo.load_file_record("h_t")
    assert record is not None and record[1] == 20.0
    assert record[0].to_dict()["density"] == cfg.to_dict()["density"]
    # No row: a no-op, not a stub row.
    repo.touch_file_settings("missing")
    assert repo.load_file_record("missing") is None


def test_mark_change_travels_on_its_own_time(tmp_path, repo):
    src = str(tmp_path / "IMG_p.NEF")
    repo.save_file_settings("h_p", _rich_config(), file_path=src, updated_at=10.0)
    repo.save_file_mark("h_p", "excluded", file_path=src, marked_at=30.0)
    mirror = SidecarMirror(repo)
    mirror.mark_dirty("h_p", src)
    mirror.flush()
    loaded = load_sidecar(src)
    assert loaded is not None and (loaded.saved_at, loaded.mark, loaded.mark_at) == (10.0, "excluded", 30.0)

    other = StorageRepository(str(tmp_path / "other_edits.db"), str(tmp_path / "other_settings.db"))
    other.initialize()
    other.save_file_settings("h_p", _rich_config(), file_path=src, updated_at=10.0)
    asset = {"name": "p", "path": src, "hash": "h_p"}
    # The edit is not newer, so nothing is offered; the mark is, so it loads on discovery.
    assert pending_sidecar_offers(other, [asset]) == []
    assert read_frame_sidecars(other, [asset]) == ([], ["h_p"])
    assert other.load_mark_record("h_p") == ("excluded", 30.0)
    assert read_frame_sidecars(other, [asset]) == ([], [])


def test_mirror_gives_up_on_unwritable_folder(tmp_path, repo, monkeypatch):
    src = str(tmp_path / "ro" / "IMG_r.NEF")
    repo.save_file_settings("h_r", _rich_config(), file_path=src)
    calls = []

    def _refuse(*a, **k):
        calls.append(a)
        raise OSError("read-only")

    monkeypatch.setattr("negpy.services.assets.sidecar.write_sidecar", _refuse)
    mirror = SidecarMirror(repo)
    mirror.mark_dirty("h_r", src)
    assert mirror.flush() == (0, 1)
    mirror.mark_dirty("h_r", src)
    assert mirror.flush() == (0, 0)
    assert len(calls) == 1


def test_save_file_settings_stores_path(repo):
    """save_file_settings with file_path persists the path for later recovery."""
    cfg = _rich_config()
    repo.save_file_settings("h7", cfg, file_path="/photos/IMG_007.NEF")
    result = repo.load_file_settings_by_path("/photos/IMG_007.NEF")
    assert result is not None
    old_hash, loaded = result
    assert old_hash == "h7"
    assert loaded.exposure.density == 0.42


def test_path_fallback_recovers_orphaned_settings(tmp_path, repo):
    """When EXIF changes the hash, path-based fallback re-homes the settings."""
    src = str(tmp_path / "IMG_008.NEF")
    _write(src, _rich_config())

    # First load: promotes sidecar to DB under hash "h8"
    loaded1 = load_or_promote(repo, "h8", src)
    assert loaded1 is not None
    assert loaded1.exposure.density == 0.42

    # Simulate EXIF change: new hash "h9" misses DB, but path matches
    # load_or_promote should find by path, re-home to "h9"
    loaded2 = load_or_promote(repo, "h9", src)
    assert loaded2 is not None
    assert loaded2.exposure.density == 0.42

    # Old hash "h8" should be deleted
    assert repo.load_file_settings("h8") is None

    # New hash "h9" should have the settings
    promoted = repo.load_file_settings("h9")
    assert promoted is not None
    assert promoted.exposure.density == 0.42


def test_rehome_file_settings(repo):
    """rehome_file_settings copies and deletes correctly."""
    cfg = _rich_config()
    repo_time = 1234.0
    repo.save_file_settings("h10", cfg, file_path="/photos/IMG_010.NEF", updated_at=repo_time)

    # Re-home to new hash
    repo.rehome_file_settings("h10", "h11", "/photos/IMG_010.NEF")

    # Old hash gone
    assert repo.load_file_settings("h10") is None

    # New hash has the data and the original timestamp
    record = repo.load_file_record("h11")
    assert record is not None
    assert record[0].exposure.density == 0.42
    assert record[1] == repo_time

    # Path lookup also finds it
    result = repo.load_file_settings_by_path("/photos/IMG_010.NEF")
    assert result is not None
    assert result[0] == "h11"


def test_load_file_settings_by_path_empty_or_missing(repo):
    """Empty path and non-existent path both return None."""
    assert repo.load_file_settings_by_path("") is None
    assert repo.load_file_settings_by_path("/nonexistent/file.NEF") is None


def test_e2e_exif_change_recovers_via_path_fallback(tmp_path, repo):
    """Integration: real calculate_file_hash + byte modification = hash change,
    path-based fallback recovers the settings, and double-re-home works."""
    from negpy.kernel.image.logic import calculate_file_hash

    # ── Create a synthetic 2 MB file with simulated EXIF bytes ──
    raw_path = str(tmp_path / "test.RAW")
    data = bytearray(2 * 1024 * 1024)
    data[:200] = b"EXIF: camera=Nikon D850 UserComment=original"
    with open(raw_path, "wb") as f:
        f.write(data)

    hash1 = calculate_file_hash(raw_path)
    assert len(hash1) == 64  # SHA-256

    # ── Save settings under hash1 ──
    cfg = _rich_config()
    repo.save_file_settings(hash1, cfg, file_path=raw_path)
    assert repo.load_file_settings(hash1) is not None

    # ── Modify EXIF area, verify hash changes ──
    data[100:115] = b"UserComment=MOD"
    with open(raw_path, "wb") as f:
        f.write(data)
    hash2 = calculate_file_hash(raw_path)
    assert hash2 != hash1, "hash should change after byte modification"

    # ── Path fallback recovers settings under new hash ──
    result = load_or_promote(repo, hash2, raw_path)
    assert result is not None
    assert result.exposure.density == 0.42
    assert repo.load_file_settings(hash1) is None  # old hash deleted
    assert repo.load_file_settings(hash2) is not None  # new hash works

    # ── Double re-home: EXIF changed again ──
    data[100:115] = b"UserComment=BAK"
    with open(raw_path, "wb") as f:
        f.write(data)
    hash3 = calculate_file_hash(raw_path)
    assert hash3 != hash2 and hash3 != hash1

    result3 = load_or_promote(repo, hash3, raw_path)
    assert result3 is not None
    assert repo.load_file_settings(hash2) is None  # intermediate hash deleted
    assert repo.load_file_settings(hash3) is not None  # latest hash works
    assert load_or_promote(repo, "unknown", "/nonexistent/path.RAW") is None  # no false positives


def test_e2e_backward_compat_save_without_path(repo):
    """save_file_settings without file_path still works (backward compat)."""
    cfg = _rich_config()
    repo.save_file_settings("h_no_path", cfg)  # no file_path arg
    result = load_or_promote(repo, "h_no_path", "/some/other/path.RAW")
    assert result is not None
    assert result.exposure.density == 0.42


def test_load_or_promote_forked_skips_path_fallback(tmp_path, repo):
    """A roll-forked hash never steals the shared edit via path-based rehoming: that
    would delete the shared row out from under every other roll still using it."""
    src = str(tmp_path / "IMG_009.NEF")
    repo.save_file_settings("h9", _rich_config(), file_path=src)

    assert load_or_promote(repo, "h9#roll:r1", src, forked=True) is None
    assert repo.load_file_settings("h9") is not None  # shared row untouched
    assert repo.load_file_settings("h9#roll:r1") is None  # nothing invented either


def test_load_or_promote_forked_skips_sidecar(tmp_path, repo):
    """A fork's `.negpy` (if any) describes the shared frame, not the fork, so it is
    never promoted onto the forked hash."""
    src = str(tmp_path / "IMG_010.NEF")
    _write(src, _rich_config())

    assert load_or_promote(repo, "h10#roll:r1", src, forked=True) is None
    assert repo.load_file_settings("h10#roll:r1") is None


def test_e2e_migration_on_fresh_db(tmp_path):
    """A fresh DB (no prior file_path column) migrates and works correctly."""
    import os

    home = str(tmp_path / "fresh_home")
    os.makedirs(home, exist_ok=True)
    repo = StorageRepository(
        edits_db_path=os.path.join(home, "edits.db"),
        settings_db_path=os.path.join(home, "settings.db"),
    )
    repo.initialize()

    cfg = _rich_config()
    # This triggers the migration path (ALTER TABLE)
    repo.save_file_settings("h_mig", cfg, file_path="/tmp/mig_test.RAW")
    loaded = repo.load_file_settings("h_mig")
    assert loaded is not None
    assert loaded.exposure.density == 0.42

    # Path lookup must work on the freshly migrated DB
    path_result = repo.load_file_settings_by_path("/tmp/mig_test.RAW")
    assert path_result is not None
    assert path_result[0] == "h_mig"


def test_updated_at_backfilled_for_rows_from_before_the_column(tmp_path):
    import sqlite3

    edits = str(tmp_path / "edits.db")
    with sqlite3.connect(edits) as conn:
        conn.execute("CREATE TABLE file_settings (file_hash TEXT PRIMARY KEY, settings_json TEXT, file_path TEXT)")
        conn.execute("INSERT INTO file_settings VALUES ('old', ?, '/p/old.NEF')", (json.dumps(_rich_config().to_dict(), default=str),))
    repo = StorageRepository(edits, str(tmp_path / "settings.db"))
    repo.initialize()
    record = repo.load_file_record("old")
    assert record is not None and record[1] > 0
    # A second initialize leaves the stamp alone.
    stamped = record[1]
    repo.initialize()
    assert repo.load_file_record("old")[1] == stamped


# --- Marks and work prints without a saved edit ---------------------------------------


def test_a_mark_without_an_edit_round_trips_with_a_null_edit(tmp_path, repo):
    src = str(tmp_path / "IMG_k.NEF")
    repo.save_file_mark("h_k", "keeper", file_path=src, marked_at=40.0)
    sc = sidecar_from_repo(repo, "h_k", src)
    assert sc is not None and (sc.config, sc.saved_at, sc.mark, sc.mark_at, sc.roll_locks) == (None, None, "keeper", 40.0, None)

    write_sidecar(src, sc)
    with open(sidecar_path_for(src), encoding="utf-8") as f:
        data = json.load(f)
    assert (data["edit"], data["saved_at"], data["mark_at"]) == (None, None, 40.0)
    assert load_sidecar(src) == sc


def test_a_frame_with_no_edit_mark_or_work_print_has_no_sidecar(repo):
    assert sidecar_from_repo(repo, "h_none") is None
    repo.save_work_print("h_wp", "v1", _rich_config(), created_at=3.0)
    sc = sidecar_from_repo(repo, "h_wp")
    assert sc is not None and sc.config is None and list(sc.work_prints) == ["v1"]


def test_a_sidecar_without_an_edit_never_creates_or_replaces_one(tmp_path, repo):
    fresh = str(tmp_path / "fresh.NEF")
    edited = str(tmp_path / "edited.NEF")
    repo.save_file_settings("h_ed", WorkspaceConfig(), file_path=edited, updated_at=5.0)
    wp = {"v1": SidecarWorkPrint(7.0, _rich_config())}
    for path in (fresh, edited):
        write_sidecar(path, Sidecar(config=None, mark="excluded", mark_at=50.0, work_prints=wp))

    assert load_or_promote(repo, "h_fr", fresh) is None
    assert repo.load_file_record("h_fr") is None
    assert repo.load_file_mark("h_fr") == "excluded"
    assert repo.list_work_prints("h_fr") == ["v1"]

    assets = [{"name": "e", "path": edited, "hash": "h_ed"}]
    assert pending_sidecar_offers(repo, assets) == []
    assert read_frame_sidecars(repo, assets) == ([], ["h_ed"])
    assert repo.load_file_record("h_ed") == (WorkspaceConfig(), 5.0)
    assert repo.load_file_mark("h_ed") == "excluded"


def test_a_newer_mark_loads_from_a_sidecar_whose_edit_is_older(tmp_path, repo):
    src = str(tmp_path / "IMG_o.NEF")
    mine = replace(_rich_config(), exposure=ExposureConfig(density=0.9))
    repo.save_file_settings("h_o", mine, file_path=src, updated_at=100.0)
    repo.save_file_mark("h_o", "keeper", file_path=src, marked_at=10.0)
    _write(src, _rich_config(), saved_at=50.0, mark="excluded", mark_at=60.0)
    asset = {"name": "o", "path": src, "hash": "h_o"}

    assert pending_sidecar_offers(repo, [asset]) == []
    assert read_frame_sidecars(repo, [asset]) == ([], ["h_o"])
    assert repo.load_file_record("h_o") == (mine, 100.0)
    assert repo.load_mark_record("h_o") == ("excluded", 60.0)


def test_an_older_mark_or_clear_in_a_sidecar_loses_to_the_mark_here(tmp_path, repo):
    src = str(tmp_path / "IMG_q.NEF")
    repo.save_file_mark("h_q", "keeper", file_path=src, marked_at=80.0)
    _write(src, _rich_config(), saved_at=50.0, mark="excluded", mark_at=50.0)
    assert read_frame_sidecars(repo, [{"name": "q", "path": src, "hash": "h_q"}]) == (["h_q"], [])
    assert repo.load_mark_record("h_q") == ("keeper", 80.0)


def test_a_cleared_mark_travels(tmp_path, repo):
    src = str(tmp_path / "IMG_c.NEF")
    repo.save_file_mark("h_c", "keeper", file_path=src, marked_at=10.0)
    repo.save_file_mark("h_c", None, file_path=src, marked_at=20.0)
    assert repo.load_file_marks() == {} and repo.load_file_marks_by_path() == {}
    assert repo.database_stats()["file_marks"] == 0
    sc = sidecar_from_repo(repo, "h_c", src)
    assert sc is not None and (sc.mark, sc.mark_at) == (None, 20.0)

    other = StorageRepository(str(tmp_path / "o_edits.db"), str(tmp_path / "o_settings.db"))
    other.initialize()
    other.save_file_mark("h_c", "keeper", file_path=src, marked_at=10.0)
    write_sidecar(src, sc)
    assert read_frame_sidecars(other, [{"name": "c", "path": src, "hash": "h_c"}]) == ([], ["h_c"])
    assert other.load_file_mark("h_c") is None


def test_work_prints_merge_by_name_and_save_time(tmp_path, repo):
    src = str(tmp_path / "IMG_w.NEF")
    older, newer = (
        replace(_rich_config(), exposure=ExposureConfig(density=0.1)),
        replace(_rich_config(), exposure=ExposureConfig(density=0.2)),
    )
    repo.save_work_print("h_w", "same", older, created_at=5.0)
    repo.save_work_print("h_w", "kept", newer, created_at=9.0)
    theirs = {
        "same": SidecarWorkPrint(6.0, newer),
        "kept": SidecarWorkPrint(1.0, older),
        "new": SidecarWorkPrint(2.0, older),
    }
    write_sidecar(src, Sidecar(config=None, work_prints=theirs))
    assert read_frame_sidecars(repo, [{"name": "w", "path": src, "hash": "h_w"}]) == ([], ["h_w"])
    assert {name: cfg.exposure.density for name, _, cfg in repo.load_work_prints("h_w")} == {"same": 0.2, "kept": 0.2, "new": 0.1}


def test_a_file_without_mark_at_dates_its_mark_by_saved_at(tmp_path):
    src = str(tmp_path / "IMG_2.NEF")
    with open(sidecar_path_for(src), "w", encoding="utf-8") as f:
        json.dump({"sidecar_format": 2, "saved_at": 12.0, "edit": _rich_config().to_dict(), "mark": "keeper"}, f, default=str)
    loaded = load_sidecar(src)
    assert loaded is not None and (loaded.mark, loaded.mark_at) == ("keeper", 12.0)


def test_mirror_writes_a_mark_only_frame_and_keeps_an_edit_its_file_holds(tmp_path, repo):
    bare = str(tmp_path / "bare.NEF")
    held = str(tmp_path / "held.NEF")
    repo.save_file_mark("h_bare", "keeper", file_path=bare, marked_at=30.0)
    repo.save_file_mark("h_held", "excluded", file_path=held, marked_at=30.0)
    _write(held, _rich_config(), saved_at=20.0, mark=None, mark_at=20.0)
    mirror = SidecarMirror(repo)
    mirror.mark_dirty("h_bare", bare)
    mirror.mark_dirty("h_held", held)
    assert mirror.flush() == (2, 0)

    loaded = load_sidecar(bare)
    assert loaded is not None and (loaded.config, loaded.mark) == (None, "keeper")
    loaded = load_sidecar(held)
    assert loaded is not None and (loaded.config, loaded.saved_at, loaded.mark, loaded.mark_at) == (_rich_config(), 20.0, "excluded", 30.0)


def test_marked_at_backfilled_from_the_edit_for_marks_from_before_the_column(tmp_path):
    import sqlite3

    edits = str(tmp_path / "edits.db")
    with sqlite3.connect(edits) as conn:
        conn.execute("CREATE TABLE file_settings (file_hash TEXT PRIMARY KEY, settings_json TEXT, file_path TEXT, updated_at REAL)")
        conn.execute("INSERT INTO file_settings VALUES ('ed', ?, '/p/ed.NEF', 42.0)", (json.dumps(_rich_config().to_dict(), default=str),))
        conn.execute("CREATE TABLE file_marks (file_hash TEXT PRIMARY KEY, mark TEXT NOT NULL, file_path TEXT)")
        conn.executemany("INSERT INTO file_marks VALUES (?, 'keeper', '')", (("ed",), ("bare",)))
    repo = StorageRepository(edits, str(tmp_path / "settings.db"))
    repo.initialize()
    assert repo.load_mark_record("ed") == ("keeper", 42.0)
    assert repo.load_mark_record("bare") == ("keeper", 0.0)
