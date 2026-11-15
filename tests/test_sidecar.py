import dataclasses
import json
import os
from dataclasses import replace

import pytest

from negpy.domain.models import WorkspaceConfig
from negpy.features.exposure.models import ExposureConfig
from negpy.features.exposure.normalization import BOUNDS_VERSION
from negpy.features.geometry.models import GeometryConfig
from negpy.features.local.models import LocalAdjustmentsConfig, LocalMask
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.services.assets.sidecar import load_or_promote, load_sidecar, promote_sidecars, sidecar_path_for, write_sidecar


def _rich_config() -> WorkspaceConfig:
    """A config exercising scalar + crop + local-mask paths, so the round trip is meaningful."""
    return WorkspaceConfig(
        exposure=ExposureConfig(density=0.42, grade=130.0),
        geometry=GeometryConfig(fine_rotation=1.5, crop_rect=(0.1, 0.2, 0.8, 0.9)),
        local=LocalAdjustmentsConfig(masks=(LocalMask(vertices=((0.0, 0.0), (0.5, 0.5)), stops=-0.7, feather=0.05),)),
    )


def test_sidecar_path_for_next_to_source():
    assert sidecar_path_for("/photos/IMG_001.NEF") == os.path.join("/photos", "IMG_001.negpy")


def test_roundtrip_next_to_source(tmp_path):
    src = str(tmp_path / "IMG_001.NEF")
    cfg = _rich_config()
    path = write_sidecar(src, cfg)

    assert path == str(tmp_path / "IMG_001.negpy")
    assert os.path.exists(path)

    loaded = load_sidecar(src)
    assert loaded is not None
    d = loaded.to_dict()
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
    write_sidecar(src, _rich_config())

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
    write_sidecar(src, replace(_rich_config(), exposure=ExposureConfig(density=0.11)))
    repo.save_file_settings("h5", replace(_rich_config(), exposure=ExposureConfig(density=0.99)))

    loaded = load_or_promote(repo, "h5", src)
    assert loaded is not None
    assert loaded.exposure.density == 0.99  # DB value, sidecar ignored


def test_load_or_promote_none_when_neither(tmp_path, repo):
    assert load_or_promote(repo, "h6", str(tmp_path / "IMG_006.NEF")) is None


def test_write_payload_is_to_dict_json_with_the_bounds_version(tmp_path):
    src = str(tmp_path / "IMG_007.NEF")
    cfg = _rich_config()
    write_sidecar(src, cfg)
    with open(sidecar_path_for(src), "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data == {**json.loads(json.dumps(cfg.to_dict(), default=str)), "bounds_version": BOUNDS_VERSION}


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
    write_sidecar(src, _rich_config())

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
    repo.save_file_settings("h10", cfg, file_path="/photos/IMG_010.NEF")

    # Re-home to new hash
    repo.rehome_file_settings("h10", "h11", "/photos/IMG_010.NEF")

    # Old hash gone
    assert repo.load_file_settings("h10") is None

    # New hash has the data
    loaded = repo.load_file_settings("h11")
    assert loaded is not None
    assert loaded.exposure.density == 0.42

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
    write_sidecar(src, _rich_config())

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


_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "sidecars")


@pytest.mark.parametrize("version", sorted(f[: -len(".negpy")] for f in os.listdir(_FIXTURES)))
def test_sidecar_from_older_release_loads(tmp_path, version, caplog):
    """Each fixture is a release's own write_sidecar output for the same edit."""
    src = str(tmp_path / "frame.tif")
    with open(os.path.join(_FIXTURES, f"{version}.negpy"), encoding="utf-8") as f_in:
        payload = f_in.read()
    with open(sidecar_path_for(src), "w", encoding="utf-8") as f_out:
        f_out.write(payload)

    with caplog.at_level("WARNING", logger="negpy"):
        cfg = load_sidecar(src)

    assert cfg is not None
    assert not [r for r in caplog.records if "Dropping" in r.getMessage()]
    assert cfg.exposure.density == 0.42
    assert cfg.exposure.grade == 80.0  # paper grade 3.5 on the ISO R scale
    assert cfg.exposure.toe == 0.3
    assert cfg.process.process_mode == "B&W Negative"
    assert cfg.geometry.crop_rect == (0.1, 0.1, 0.8, 0.8)
    assert cfg.geometry.rotation == 1
    assert cfg.geometry.fine_rotation == 0.5
    assert cfg.lab.saturation == 1.3
    assert len(cfg.local.masks) == 1
    # A pre-0.51 mask was a +0.5 dodge; the same mask is a -0.5 stop burn now.
    assert cfg.local.masks[0].stops == (-0.5 if version < "0.51" else 0.5)


def test_invalid_value_drops_only_that_field(tmp_path):
    src = str(tmp_path / "IMG_011.NEF")
    data = json.loads(json.dumps(_rich_config().to_dict(), default=str))
    data["locked_floors"] = None
    data["hdr_ratios"] = "garbage"
    data["local_masks"]["masks"].append({"vertices": [], "shape": "no-such-shape"})
    with open(sidecar_path_for(src), "w", encoding="utf-8") as f:
        json.dump(data, f)

    cfg = load_sidecar(src)

    assert cfg is not None
    assert cfg.process.locked_floors == WorkspaceConfig().process.locked_floors
    assert cfg.hdr.hdr_ratios == ()
    assert cfg.exposure.density == 0.42
    assert cfg.geometry.crop_rect == (0.1, 0.2, 0.8, 0.9)
    assert [m.stops for m in cfg.local.masks] == [-0.7]


def test_saved_hashes(repo):
    repo.save_file_settings("a", _rich_config())
    assert repo.saved_hashes(["a", "b"]) == {"a"}
    assert repo.saved_hashes([]) == set()


def test_promote_sidecars_fills_db_before_open(tmp_path, repo):
    plain, edited, half, composite, forked = (str(tmp_path / f"{n}.NEF") for n in ("plain", "edited", "half", "comp", "fork"))
    for p in (plain, edited, composite, forked):
        write_sidecar(p, _rich_config())
    write_sidecar(half, _rich_config(), half=2)
    repo.save_file_settings("edited", replace(_rich_config(), exposure=ExposureConfig(density=0.99)), file_path=edited)

    promote_sidecars(
        repo,
        [
            {"hash": "plain", "path": plain},
            {"hash": "edited", "path": edited},
            {"hash": "half#2", "path": half, "half": 2},
            {"hash": "comp", "path": composite, "hdr_paths": [composite]},
            {"hash": "fork#roll:r1", "path": forked},
        ],
    )

    assert repo.load_file_settings("plain").exposure.density == 0.42
    assert repo.load_file_settings("edited").exposure.density == 0.99
    assert repo.load_file_settings("half#2").exposure.density == 0.42
    assert repo.saved_hashes(["comp", "fork#roll:r1"]) == set()


_BOUNDS = dict(local_floors=(-1.1, -1.2, -1.0), local_ceils=(-0.2, -0.4, -0.6))


def _measured(**process) -> WorkspaceConfig:
    cfg = WorkspaceConfig()
    return replace(cfg, process=replace(cfg.process, **_BOUNDS, **process))


def _unstamp(path: str) -> None:
    with open(path) as fh:
        data = json.load(fh)
    del data["bounds_version"]
    with open(path, "w") as fh:
        json.dump(data, fh)


def test_a_sidecar_keeps_bounds_saved_under_the_current_analysis(tmp_path):
    src = str(tmp_path / "IMG_001.NEF")
    write_sidecar(src, _measured())

    assert load_sidecar(src).process.local_floors == _BOUNDS["local_floors"]


def test_a_sidecar_from_an_older_analysis_drops_its_bounds(tmp_path):
    src = str(tmp_path / "IMG_001.NEF")
    _unstamp(write_sidecar(src, _measured()))

    assert not load_sidecar(src).process.is_local_initialized


def test_lock_bounds_keeps_them_in_a_sidecar_from_an_older_analysis(tmp_path):
    src = str(tmp_path / "IMG_001.NEF")
    _unstamp(write_sidecar(src, _measured(lock_bounds=True)))

    assert load_sidecar(src).process.local_floors == _BOUNDS["local_floors"]


def test_ir_method_is_dropped_on_load(caplog):
    flat = WorkspaceConfig().to_dict()
    flat["ir_method"] = "openice"
    with caplog.at_level("WARNING", logger="negpy"):
        cfg = WorkspaceConfig.from_flat_dict(flat)
    assert not hasattr(cfg.retouch, "ir_method")
    assert not [r for r in caplog.records if "Dropping" in r.getMessage()]


# --- Format: flat config, bounds_version and the reserved sync key ---------------------


def test_the_sync_key_collides_with_no_config_field():
    from negpy.domain import migrations
    from negpy.services.assets.sidecar import SYNC_KEY

    config = WorkspaceConfig()
    fields = {name for section in dataclasses.fields(config) for name in getattr(getattr(config, section.name), "__dataclass_fields__", {})}
    assert "density" in fields
    assert SYNC_KEY not in fields
    assert SYNC_KEY not in WorkspaceConfig().to_dict()
    assert SYNC_KEY not in migrations.KEY_RENAMES
    assert SYNC_KEY not in migrations.DROPPED_KEYS


def test_a_reader_without_sync_keeps_the_edit(tmp_path):
    """The reading rule of a release that knows only bounds_version drops sync and keeps the edit."""
    from negpy.services.assets.sidecar import SidecarSync

    src = str(tmp_path / "IMG_001.NEF")
    cfg = _rich_config()
    write_sidecar(src, cfg, sync=SidecarSync(saved_at=200.0, mark="keeper", mark_at=210.0))
    with open(sidecar_path_for(src), encoding="utf-8") as fh:
        data = json.load(fh)
    data.pop("bounds_version", None)

    assert WorkspaceConfig.from_flat_dict(data).to_dict() == WorkspaceConfig.from_flat_dict(cfg.to_dict()).to_dict()


def test_read_sidecar_returns_the_edit_and_read_sidecar_meta_its_sync(tmp_path):
    from negpy.services.assets.sidecar import SidecarSync, read_sidecar, read_sidecar_meta

    src = str(tmp_path / "IMG_001.NEF")
    sync = SidecarSync(saved_at=200.0, mark="excluded", mark_at=210.0)
    path = write_sidecar(src, _rich_config(), sync=sync)

    assert read_sidecar(path).exposure.density == 0.42
    assert read_sidecar_meta(path) == sync


def test_a_file_without_sync_has_no_meta_and_a_mark_only_file_no_edit(tmp_path):
    from negpy.services.assets.sidecar import SidecarSync, read_sidecar, read_sidecar_meta, write_payload

    src = str(tmp_path / "IMG_001.NEF")
    assert read_sidecar_meta(write_sidecar(src, _rich_config())) is None

    path = sidecar_path_for(src)
    write_payload(path, {"sync": {"saved_at": None, "mark": "keeper", "mark_at": 5.0}})
    assert read_sidecar(path) is None
    assert read_sidecar_meta(path) == SidecarSync(mark="keeper", mark_at=5.0)


def test_promotion_dates_the_edit_by_the_file(tmp_path, repo):
    from negpy.services.assets.sidecar import SidecarSync

    dated, undated = str(tmp_path / "a.NEF"), str(tmp_path / "b.NEF")
    write_sidecar(dated, _rich_config(), sync=SidecarSync(saved_at=200.0))
    write_sidecar(undated, _rich_config())

    load_or_promote(repo, "ha", dated)
    load_or_promote(repo, "hb", undated)

    assert repo.load_file_updated_at("ha") == 200.0
    assert repo.load_file_updated_at("hb") == 0.0


def test_a_sidecar_never_creates_a_folder(tmp_path):
    assert write_sidecar(str(tmp_path / "gone" / "a.NEF"), _rich_config()) is None
    assert not (tmp_path / "gone").exists()


# --- Keep Current: SidecarMirror ---------------------------------------------------------


def _source(tmp_path, name: str = "a.NEF") -> str:
    path = tmp_path / name
    path.write_bytes(b"raw")
    return str(path)


def _payload(src: str, half: int = 0) -> dict:
    with open(sidecar_path_for(src, half), encoding="utf-8") as fh:
        return json.load(fh)


def _flush(repo, *frames) -> tuple[int, int]:
    from negpy.services.assets.sidecar import SidecarMirror

    mirror = SidecarMirror(repo)
    for file_hash, src in frames:
        mirror.queue(file_hash, src)
    return mirror.flush()


def test_the_mirror_writes_the_rows_with_their_dates(tmp_path, repo):
    src = _source(tmp_path)
    repo.save_file_settings("h", _rich_config(), file_path=src, updated_at=100.0)
    repo.save_file_mark("h", "keeper", file_path=src, marked_at=110.0)

    assert _flush(repo, ("h", src)) == (1, 0)

    data = _payload(src)
    assert data["sync"] == {"saved_at": 100.0, "mark": "keeper", "mark_at": 110.0}
    assert data["bounds_version"] == BOUNDS_VERSION
    assert data["density"] == 0.42


def test_the_mirror_leaves_a_file_that_already_matches(tmp_path, repo):
    src = _source(tmp_path)
    repo.save_file_settings("h", _rich_config(), file_path=src, updated_at=100.0)
    _flush(repo, ("h", src))
    before = os.stat(sidecar_path_for(src)).st_mtime_ns

    assert _flush(repo, ("h", src)) == (0, 0)
    assert os.stat(sidecar_path_for(src)).st_mtime_ns == before


def test_a_flush_never_writes_an_older_edit_over_a_newer_one_in_the_file(tmp_path, repo):
    """PC1 saved at 200; PC2 holds the edit of 100 and then marks the frame."""
    from negpy.services.assets.sidecar import SidecarSync

    src = _source(tmp_path)
    pc1 = replace(_rich_config(), exposure=ExposureConfig(density=0.9))
    write_sidecar(src, pc1, sync=SidecarSync(saved_at=200.0))
    repo.save_file_settings("h", _rich_config(), file_path=src, updated_at=100.0)
    repo.save_file_mark("h", "keeper", file_path=src, marked_at=300.0)

    _flush(repo, ("h", src))

    data = _payload(src)
    assert data["density"] == 0.9
    assert data["sync"] == {"saved_at": 200.0, "mark": "keeper", "mark_at": 300.0}
    assert repo.load_file_settings("h").exposure.density == 0.42


def test_a_newer_mark_in_the_file_stays(tmp_path, repo):
    from negpy.services.assets.sidecar import SidecarSync

    src = _source(tmp_path)
    write_sidecar(src, _rich_config(), sync=SidecarSync(saved_at=100.0, mark="excluded", mark_at=500.0))
    repo.save_file_settings("h", replace(_rich_config(), exposure=ExposureConfig(density=0.3)), file_path=src, updated_at=400.0)
    repo.save_file_mark("h", "keeper", file_path=src, marked_at=450.0)

    _flush(repo, ("h", src))

    data = _payload(src)
    assert data["density"] == 0.3
    assert data["sync"] == {"saved_at": 400.0, "mark": "excluded", "mark_at": 500.0}


def test_a_mark_without_an_edit_is_written_alone(tmp_path, repo):
    from negpy.services.assets.sidecar import read_sidecar

    src = _source(tmp_path)
    repo.save_file_mark("h", "keeper", file_path=src, marked_at=5.0)

    _flush(repo, ("h", src))

    assert _payload(src) == {"sync": {"saved_at": None, "mark": "keeper", "mark_at": 5.0}}
    assert read_sidecar(sidecar_path_for(src)) is None


def test_a_cleared_mark_with_no_file_writes_nothing(tmp_path, repo):
    src = _source(tmp_path)
    repo.save_file_mark("h", None, file_path=src)

    assert _flush(repo, ("h", src)) == (0, 0)
    assert not os.path.exists(sidecar_path_for(src))


def test_a_filled_frame_does_not_rewrite_the_file_it_came_from(tmp_path, repo):
    """A file with stale bounds fills its frame without them; the file stays as the other computer wrote it."""
    from negpy.services.assets.sidecar import SidecarSync

    src = _source(tmp_path)
    path = write_sidecar(src, _measured(), sync=SidecarSync(saved_at=100.0))
    _unstamp(path)
    load_or_promote(repo, "h", src)
    before = _payload(src)

    assert _flush(repo, ("h", src)) == (0, 0)
    assert _payload(src) == before


def test_the_mirror_names_a_half_by_its_hash_and_skips_forks_and_composites(tmp_path, repo):
    src = _source(tmp_path)
    for file_hash in ("h#2", "h#roll:r1", "s#stitch"):
        repo.save_file_settings(file_hash, _rich_config(), file_path=src)

    _flush(repo, ("h#2", src), ("h#roll:r1", src), ("s#stitch", src))

    assert os.path.exists(sidecar_path_for(src, 2))
    assert not os.path.exists(sidecar_path_for(src))


def test_the_mirror_skips_a_source_that_is_gone(tmp_path, repo):
    src = str(tmp_path / "a.NEF")
    repo.save_file_settings("h", _rich_config(), file_path=src)

    assert _flush(repo, ("h", src)) == (0, 0)
    assert not os.path.exists(sidecar_path_for(src))


def test_the_mirror_finds_the_path_of_a_save_that_gave_none(tmp_path, repo):
    src = _source(tmp_path)
    repo.save_file_settings("h", _rich_config(), file_path=src)

    _flush(repo, ("h", ""))

    assert os.path.exists(sidecar_path_for(src))


def test_the_mirror_gives_up_on_a_folder_that_refuses_a_write(tmp_path, repo, monkeypatch):
    from negpy.services.assets import sidecar
    from negpy.services.assets.sidecar import SidecarMirror

    a, b = _source(tmp_path, "a.NEF"), _source(tmp_path, "b.NEF")
    for file_hash, src in (("ha", a), ("hb", b)):
        repo.save_file_settings(file_hash, _rich_config(), file_path=src)
    calls = []

    def refuse(path, payload):
        calls.append(path)
        raise PermissionError("read-only")

    monkeypatch.setattr(sidecar, "write_payload", refuse)
    mirror = SidecarMirror(repo)
    mirror.queue("ha", a)
    mirror.queue("hb", b)

    assert mirror.flush() == (0, 1)
    assert len(calls) == 1


def test_each_saved_edit_and_mark_reaches_the_write_observer(repo):
    seen = []
    repo.add_write_observer(lambda file_hash, file_path: seen.append((file_hash, file_path)))

    repo.save_file_settings("h", _rich_config(), file_path="/r/a.NEF")
    repo.save_file_mark("h", "keeper", file_path="/r/a.NEF")

    assert seen == [("h", "/r/a.NEF"), ("h", "/r/a.NEF")]


# --- Discovery: fill, merge and offer ----------------------------------------------------


def _frame(src: str, file_hash: str = "h", **extra) -> dict:
    return {"name": os.path.basename(src), "path": src, "hash": file_hash, **extra}


def _dated(src: str, cfg: WorkspaceConfig, saved_at=None, mark=None, mark_at=None, half: int = 0) -> str:
    from negpy.services.assets.sidecar import SidecarSync

    return write_sidecar(src, cfg, half=half, sync=SidecarSync(saved_at, mark, mark_at))


def test_a_frame_with_no_edit_here_fills_from_its_file(tmp_path, repo):
    from negpy.services.assets.sidecar import apply_sidecar_plan, plan_frame_sidecars

    src = _source(tmp_path)
    _dated(src, _rich_config(), saved_at=200.0)

    plan = plan_frame_sidecars(repo, [_frame(src)])

    assert repo.load_file_updated_at("h") is None
    assert apply_sidecar_plan(repo, plan) == (["h"], [])
    assert repo.load_file_settings("h").exposure.density == 0.42
    assert repo.load_file_updated_at("h") == 200.0


def test_a_fill_yields_to_an_edit_found_by_path_or_saved_since_the_plan(tmp_path, repo):
    from negpy.services.assets.sidecar import apply_sidecar_plan, plan_frame_sidecars

    a, b = _source(tmp_path, "a.NEF"), _source(tmp_path, "b.NEF")
    for src in (a, b):
        _dated(src, _rich_config(), saved_at=200.0)
    plan = plan_frame_sidecars(repo, [_frame(a, "ha"), _frame(b, "hb")])
    repo.save_file_settings("old", replace(_rich_config(), exposure=ExposureConfig(density=0.1)), file_path=a)
    repo.save_file_settings("hb", replace(_rich_config(), exposure=ExposureConfig(density=0.2)), file_path=b)

    assert apply_sidecar_plan(repo, plan) == ([], [])
    assert repo.load_file_settings("ha").exposure.density == 0.1
    assert repo.load_file_settings("hb").exposure.density == 0.2


def test_an_edit_is_offered_only_when_saved_later_and_different(tmp_path, repo):
    from negpy.services.assets.sidecar import offers_from_plan, plan_frame_sidecars

    newer, same, older = (_source(tmp_path, n) for n in ("a.NEF", "b.NEF", "c.NEF"))
    theirs = replace(_rich_config(), exposure=ExposureConfig(density=0.9))
    for src, file_hash, cfg, saved_at in ((newer, "ha", theirs, 300.0), (same, "hb", _rich_config(), 300.0), (older, "hc", theirs, 50.0)):
        _dated(src, cfg, saved_at=saved_at)
        repo.save_file_settings(file_hash, _rich_config(), file_path=src, updated_at=100.0)

    plan = plan_frame_sidecars(repo, [_frame(newer, "ha"), _frame(same, "hb"), _frame(older, "hc")])
    offers = offers_from_plan(repo, plan)

    assert [(o.key, o.saved_at, o.config.exposure.density) for o in offers] == [("ha", 300.0, 0.9)]
    assert offers[0].label == "a.NEF"


def test_keep_mine_stops_asking_until_the_file_is_saved_again(tmp_path, repo):
    from negpy.services.assets.sidecar import decline_sidecar_offers, offers_from_plan, plan_frame_sidecars

    src = _source(tmp_path)
    repo.save_file_settings("h", _rich_config(), file_path=src, updated_at=100.0)
    _dated(src, replace(_rich_config(), exposure=ExposureConfig(density=0.9)), saved_at=300.0)
    decline_sidecar_offers(repo, offers_from_plan(repo, plan_frame_sidecars(repo, [_frame(src)])))

    assert offers_from_plan(repo, plan_frame_sidecars(repo, [_frame(src)])) == []

    _dated(src, replace(_rich_config(), exposure=ExposureConfig(density=0.8)), saved_at=400.0)
    assert [o.saved_at for o in offers_from_plan(repo, plan_frame_sidecars(repo, [_frame(src)]))] == [400.0]


def test_a_newer_mark_of_another_value_merges_without_asking(tmp_path, repo):
    from negpy.services.assets.sidecar import apply_sidecar_plan, plan_frame_sidecars

    newer, older, same, cleared = (_source(tmp_path, n) for n in ("a.NEF", "b.NEF", "c.NEF", "d.NEF"))
    repo.save_file_mark("ha", "keeper", file_path=newer, marked_at=100.0)
    repo.save_file_mark("hb", "keeper", file_path=older, marked_at=100.0)
    repo.save_file_mark("hc", "keeper", file_path=same, marked_at=100.0)
    repo.save_file_mark("hd", "keeper", file_path=cleared, marked_at=100.0)
    _dated(newer, _rich_config(), mark="excluded", mark_at=200.0)
    _dated(older, _rich_config(), mark="excluded", mark_at=50.0)
    _dated(same, _rich_config(), mark="keeper", mark_at=200.0)
    _dated(cleared, _rich_config(), mark=None, mark_at=200.0)
    frames = [_frame(newer, "ha"), _frame(older, "hb"), _frame(same, "hc"), _frame(cleared, "hd")]
    for file_hash in ("ha", "hb", "hc", "hd"):
        repo.save_file_settings(file_hash, _rich_config())

    assert apply_sidecar_plan(repo, plan_frame_sidecars(repo, frames)) == ([], ["ha", "hd"])
    assert repo.load_file_marks() == {"ha": "excluded", "hb": "keeper", "hc": "keeper"}
    assert repo.load_mark_record("ha") == ("excluded", 200.0)


def test_a_fork_takes_only_the_mark_and_a_composite_nothing(tmp_path, repo):
    from negpy.services.assets.sidecar import apply_sidecar_plan, plan_frame_sidecars

    src = _source(tmp_path)
    _dated(src, _rich_config(), saved_at=200.0, mark="keeper", mark_at=200.0)
    frames = [_frame(src, "h#roll:r1"), _frame(src, "s#stitch", stitch_paths=[src])]

    plan = plan_frame_sidecars(repo, frames)

    assert (plan.fill, plan.offer) == ([], [])
    assert apply_sidecar_plan(repo, plan) == ([], ["h"])
    assert repo.load_file_settings("h") is None


def test_a_plan_drops_the_edits_of_assets_it_is_told_to(tmp_path, repo):
    from negpy.services.assets.sidecar import plan_frame_sidecars

    src = _source(tmp_path)
    _dated(src, _rich_config(), saved_at=200.0, mark="keeper", mark_at=200.0)
    plan = plan_frame_sidecars(repo, [_frame(src)]).without_edits_for(lambda asset: False)

    assert (plan.fill, plan.offer, len(plan.merge)) == ([], [], 1)


def test_a_half_frame_reads_its_own_sidecar(tmp_path, repo):
    from negpy.services.assets.sidecar import apply_sidecar_plan, plan_frame_sidecars

    src = _source(tmp_path)
    _dated(src, _rich_config(), saved_at=200.0, half=2)

    plan = plan_frame_sidecars(repo, [_frame(src, "h#1", half=1), _frame(src, "h#2", half=2)])

    assert apply_sidecar_plan(repo, plan) == (["h#2"], [])
