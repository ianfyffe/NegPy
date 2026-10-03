"""The folder-open sidecar scan: read and compared on a worker thread, written on the UI
thread from the plan, and dropped when superseded."""

from PyQt6.QtCore import QEventLoop, QObject, QThread, QTimer, pyqtSignal

from negpy.desktop.workers import sidecar_scan
from negpy.desktop.workers.sidecar_scan import SidecarScanWorker
from negpy.domain.models import WorkspaceConfig
from negpy.infrastructure.storage.repository import StorageRepository
from negpy.services.assets.sidecar import (
    Sidecar,
    SidecarReader,
    SidecarWorkPrint,
    apply_sidecar_plan,
    load_or_promote,
    plan_frame_sidecars,
    write_sidecar,
)


class _Requester(QObject):
    request = pyqtSignal(int, list)


def _repo(tmp_path) -> StorageRepository:
    repo = StorageRepository(str(tmp_path / "edits.db"), str(tmp_path / "settings.db"))
    repo.initialize()
    return repo


def _frames(tmp_path, repo) -> list[dict]:
    """h_edit has an edit here and a newer mark in its file; h_new has no edit here."""
    edited, fresh = str(tmp_path / "edited.NEF"), str(tmp_path / "fresh.NEF")
    repo.save_file_settings("h_edit", WorkspaceConfig(), file_path=edited, updated_at=100.0)
    repo.save_file_mark("h_edit", "keeper", file_path=edited, marked_at=10.0)
    write_sidecar(edited, Sidecar(config=WorkspaceConfig(), saved_at=50.0, mark="excluded", mark_at=60.0))
    write_sidecar(
        fresh,
        Sidecar(
            config=WorkspaceConfig(),
            saved_at=70.0,
            mark="keeper",
            mark_at=70.0,
            work_prints={"v1": SidecarWorkPrint(65.0, WorkspaceConfig())},
        ),
    )
    return [{"name": "edited", "path": edited, "hash": "h_edit"}, {"name": "fresh", "path": fresh, "hash": "h_new"}]


def test_the_scan_reads_on_the_worker_thread_and_writes_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    assets = _frames(tmp_path, repo)
    threads = []
    real_plan = sidecar_scan.plan_frame_sidecars
    monkeypatch.setattr(sidecar_scan, "plan_frame_sidecars", lambda *a, **k: threads.append(QThread.currentThread()) or real_plan(*a, **k))

    thread = QThread()
    worker = SidecarScanWorker(repo)
    worker.moveToThread(thread)
    requester = _Requester()
    requester.request.connect(worker.scan)
    results = []
    loop = QEventLoop()
    worker.planned.connect(lambda gen, plan: (results.append((gen, plan)), loop.quit()))
    thread.start()
    try:
        worker.supersede(1)
        requester.request.emit(1, assets)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
    finally:
        thread.quit()
        thread.wait()

    assert threads and threads[0] is not QThread.currentThread()
    ((gen, plan),) = results
    assert gen == 1
    assert [a["hash"] for a, _ in plan.fill] == ["h_new"] and [a["hash"] for a, _ in plan.merge] == ["h_edit"]
    assert repo.load_file_record("h_new") is None and repo.load_file_mark("h_edit") == "keeper"

    assert apply_sidecar_plan(repo, plan) == (["h_new"], ["h_edit"])
    assert repo.load_file_mark("h_edit") == "excluded"
    assert repo.list_work_prints("h_new") == ["v1"]


def test_a_superseded_scan_stops_and_emits_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    assets = _frames(tmp_path, repo)
    worker = SidecarScanWorker(repo)
    results = []
    worker.planned.connect(lambda gen, plan: results.append(gen))

    worker.supersede(2)
    worker.scan(1, assets)
    assert results == []

    seen = []
    real_scan = SidecarReader.scan

    def superseding_scan(reader, path, half=0):
        seen.append(path)
        worker.supersede(3)
        return real_scan(reader, path, half)

    monkeypatch.setattr(SidecarReader, "scan", superseding_scan)
    worker.scan(2, assets)
    assert results == [] and len(seen) == 1


def test_a_frame_opened_before_its_scan_lands_takes_its_sidecar_once(tmp_path):
    repo = _repo(tmp_path)
    assets = _frames(tmp_path, repo)
    plan = plan_frame_sidecars(repo, assets, SidecarReader())
    assert plan is not None and [a["hash"] for a, _ in plan.fill] == ["h_new"]

    fresh = assets[1]
    load_or_promote(repo, fresh["hash"], fresh["path"])
    record = repo.load_file_record("h_new")
    assert record is not None and record[1] == 70.0
    repo.save_file_mark("h_new", None, file_path=fresh["path"], marked_at=80.0)

    assert apply_sidecar_plan(repo, plan) == ([], ["h_edit"])
    assert repo.load_file_record("h_new") == record
    assert repo.load_file_mark("h_new") is None
    assert repo.list_work_prints("h_new") == ["v1"]
