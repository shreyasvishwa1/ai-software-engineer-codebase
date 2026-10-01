"""Ingestion maintenance tests (stale-job recovery and leftover storage cleanup).

Filesystem-only tests need no database. Recovery tests run against the already-migrated
throwaway test database (TEST_DATABASE_URL, name ending in "_test") inside rolled-back
transactions; queued ingestion runs inline with the real worker logic and DATA_DIR is a
temporary directory. Inside the test transaction ``now()`` is constant, so job ages are set
relative to it. Link tests use Windows directory junctions (no privilege needed) and real
symlinks where the platform allows creating them (skipped otherwise).
"""

import contextlib
import os
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import (
    AnalysisJob,
    AnalysisJobStatus,
    AnalysisJobType,
    File,
    Repository,
    RepositoryStatus,
)
from app.services import ingestion, maintenance
from app.services.maintenance import MaintenanceReport, maintenance_task, run_maintenance
from app.workers import worker
from app.workers.queue import CODE_METRICS_TASK, INGESTION_TASK, deserialize_job, serialize_job
from app.workers.worker import WorkerSettings
from tests.test_worker import _zip, install_inline_worker

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
SAMPLE = {"src/app.py": b"print('hi')\n", "README.md": b"# demo\n"}
OLD = time.time() - 2 * 3600  # older than the 1 h leftover threshold
HEX = "0123456789abcdef0123456789abcdef"


# --- Fixtures and helpers ----------------------------------------------------------------


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    assert TEST_DATABASE_URL
    url = make_url(TEST_DATABASE_URL)
    if not (url.database or "").endswith("_test"):
        pytest.fail("refusing to use TEST_DATABASE_URL: database name must end with '_test'")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            # Commits inside the app only release a savepoint; everything is rolled back below.
            session = AsyncSession(
                bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
            )
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    monkeypatch.setattr(get_settings(), "data_dir", path)
    return path


@pytest.fixture
async def client(db_session: AsyncSession, data_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@dataclass
class Owner:
    headers: dict[str, str]
    project_id: uuid.UUID
    repository_id: uuid.UUID


async def _new_repository(client: httpx.AsyncClient, owner: Owner) -> uuid.UUID:
    response = await client.post(
        f"/api/v1/projects/{owner.project_id}/repositories",
        json={"name": f"repo-{uuid.uuid4().hex[:8]}", "source_type": "zip"},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


@pytest.fixture
async def owner(client: httpx.AsyncClient) -> Owner:
    email = "maint@example.com"
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project = (await client.post("/api/v1/projects", json={"name": "P"}, headers=headers)).json()
    result = Owner(headers, uuid.UUID(project["id"]), uuid.uuid4())
    result.repository_id = await _new_repository(client, result)
    return result


async def _ingest(client: httpx.AsyncClient, owner: Owner, repository_id: uuid.UUID) -> int:
    response = await client.post(
        f"/api/v1/projects/{owner.project_id}/repositories/{repository_id}/ingest",
        files={"file": ("repo.zip", _zip(SAMPLE), "application/zip")},
        headers=owner.headers,
    )
    return response.status_code


async def _add_job(
    db: AsyncSession,
    owner: Owner,
    status: AnalysisJobStatus,
    age: timedelta,
    repository_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert an ingestion job created/started ``age`` before the transaction's now()."""
    job = AnalysisJob(
        project_id=owner.project_id,
        repository_id=repository_id or owner.repository_id,
        job_type=AnalysisJobType.INGESTION,
        status=status,
    )
    db.add(job)
    await db.flush()
    started = func.now() - age if status != AnalysisJobStatus.PENDING else None
    await db.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == job.id)
        .values(created_at=func.now() - age, started_at=started)
    )
    await db.commit()
    return job.id


async def _set_repository_status(
    db: AsyncSession, repository_id: uuid.UUID, status: RepositoryStatus
) -> None:
    await db.execute(update(Repository).where(Repository.id == repository_id).values(status=status))
    await db.commit()


async def _job(db: AsyncSession, job_id: uuid.UUID) -> AnalysisJob:
    db.expire_all()
    job = await db.get(AnalysisJob, job_id)
    assert job is not None
    return job


async def _repository_status(db: AsyncSession, repository_id: uuid.UUID) -> RepositoryStatus:
    db.expire_all()
    value = await db.scalar(select(Repository.status).where(Repository.id == repository_id))
    assert value is not None
    return value


async def _file_paths(db: AsyncSession, repository_id: uuid.UUID) -> set[str]:
    rows = await db.scalars(select(File.path).where(File.repository_id == repository_id))
    return set(rows)


def _uploads_root(data_dir: Path) -> Path:
    root = data_dir / "ingestion-uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _repositories_root(data_dir: Path) -> Path:
    root = data_dir / "repositories"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_file(path: Path, mtime: float = OLD) -> Path:
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def _make_dir(path: Path, mtime: float = OLD) -> Path:
    path.mkdir()
    (path / "inner.txt").write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
    assert os.path.isjunction(link)


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_file(outside / "keep.txt")
    return outside


# --- Configuration -----------------------------------------------------------------------


def test_thresholds() -> None:
    assert maintenance.PROCESSING_STALE_AFTER == timedelta(seconds=2100)
    assert maintenance.PROCESSING_STALE_AFTER.total_seconds() == worker.JOB_TIMEOUT_SECONDS + 300
    assert maintenance.PENDING_STALE_AFTER == timedelta(hours=6)
    assert maintenance.LEFTOVER_AFTER == timedelta(hours=1)
    assert maintenance.MAINTENANCE_INTERVAL_MINUTES == 5


def test_worker_registers_maintenance_cron() -> None:
    assert len(WorkerSettings.cron_jobs) == 1
    job = WorkerSettings.cron_jobs[0]
    assert job.name == "maintenance"
    assert job.coroutine is maintenance_task
    assert job.minute == set(range(0, 60, 5))
    assert job.run_at_startup is True
    assert job.unique is True
    assert job.max_tries == 1
    assert job.keep_result_s == 0
    assert job.timeout_s == maintenance.MAINTENANCE_TIMEOUT_SECONDS


def test_existing_ingestion_worker_settings_unchanged() -> None:
    assert [f.name for f in WorkerSettings.functions] == [INGESTION_TASK, CODE_METRICS_TASK]
    assert WorkerSettings.functions[0].coroutine is worker.ingest_repository
    assert WorkerSettings.max_tries == 1
    assert WorkerSettings.keep_result == 0
    assert WorkerSettings.job_timeout == 1800
    assert WorkerSettings.job_serializer is serialize_job
    assert WorkerSettings.job_deserializer is deserialize_job
    assert WorkerSettings.on_startup is worker.startup
    assert WorkerSettings.on_shutdown is worker.shutdown


# --- Stale job recovery ------------------------------------------------------------------


@requires_db
async def test_stale_processing_job_is_failed_and_repository_recovers(
    db_session: AsyncSession, owner: Owner
) -> None:
    await _set_repository_status(db_session, owner.repository_id, RepositoryStatus.PROCESSING)
    job_id = await _add_job(
        db_session, owner, AnalysisJobStatus.PROCESSING, timedelta(seconds=2101)
    )

    report = await run_maintenance(db_session)

    assert report.processing_jobs_failed == 1
    job = await _job(db_session, job_id)
    assert job.status == AnalysisJobStatus.FAILED
    assert job.error_message == maintenance.INTERRUPTED_MESSAGE
    assert job.completed_at is not None
    assert await _repository_status(db_session, owner.repository_id) == RepositoryStatus.FAILED


@requires_db
async def test_stale_pending_job_is_failed(db_session: AsyncSession, owner: Owner) -> None:
    job_id = await _add_job(
        db_session, owner, AnalysisJobStatus.PENDING, timedelta(hours=6, seconds=1)
    )

    report = await run_maintenance(db_session)

    assert report.pending_jobs_failed == 1
    job = await _job(db_session, job_id)
    assert job.status == AnalysisJobStatus.FAILED
    assert job.error_message == maintenance.NOT_STARTED_MESSAGE
    assert job.completed_at is not None
    # The repository was never claimed, so its status is left as it was.
    assert await _repository_status(db_session, owner.repository_id) == RepositoryStatus.PENDING


@requires_db
async def test_recent_jobs_are_untouched(
    db_session: AsyncSession, client: httpx.AsyncClient, owner: Owner
) -> None:
    other = await _new_repository(client, owner)
    await _set_repository_status(db_session, owner.repository_id, RepositoryStatus.PROCESSING)
    processing = await _add_job(
        db_session, owner, AnalysisJobStatus.PROCESSING, timedelta(seconds=2000)
    )
    pending = await _add_job(
        db_session, owner, AnalysisJobStatus.PENDING, timedelta(hours=5), repository_id=other
    )

    report = await run_maintenance(db_session)

    assert (report.processing_jobs_failed, report.pending_jobs_failed) == (0, 0)
    assert (await _job(db_session, processing)).status == AnalysisJobStatus.PROCESSING
    assert (await _job(db_session, pending)).status == AnalysisJobStatus.PENDING
    assert await _repository_status(db_session, owner.repository_id) == RepositoryStatus.PROCESSING


@requires_db
async def test_completed_and_failed_jobs_are_untouched(
    db_session: AsyncSession, owner: Owner
) -> None:
    await _set_repository_status(db_session, owner.repository_id, RepositoryStatus.READY)
    completed = await _add_job(db_session, owner, AnalysisJobStatus.COMPLETED, timedelta(days=2))
    failed = await _add_job(db_session, owner, AnalysisJobStatus.FAILED, timedelta(days=2))
    await db_session.execute(
        update(AnalysisJob).where(AnalysisJob.id == failed).values(error_message="original")
    )
    await db_session.commit()

    report = await run_maintenance(db_session)

    assert report == MaintenanceReport()
    assert (await _job(db_session, completed)).status == AnalysisJobStatus.COMPLETED
    failed_job = await _job(db_session, failed)
    assert (failed_job.status, failed_job.error_message) == (AnalysisJobStatus.FAILED, "original")
    assert await _repository_status(db_session, owner.repository_id) == RepositoryStatus.READY


@requires_db
async def test_job_completing_concurrently_is_not_overwritten(
    db_session: AsyncSession, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job finishes after it was seen as stale but before the recovery UPDATE runs; the
    conditional UPDATE re-checks the status and leaves the completed job alone."""
    await _set_repository_status(db_session, owner.repository_id, RepositoryStatus.PROCESSING)
    job_id = await _add_job(
        db_session, owner, AnalysisJobStatus.PROCESSING, timedelta(seconds=2101)
    )
    original_execute = db_session.execute
    completed_first = False

    async def complete_then_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal completed_first
        if not completed_first:
            completed_first = True
            await original_execute(
                update(AnalysisJob)
                .where(AnalysisJob.id == job_id)
                .values(status=AnalysisJobStatus.COMPLETED, completed_at=func.now())
            )
            await original_execute(
                update(Repository)
                .where(Repository.id == owner.repository_id)
                .values(status=RepositoryStatus.READY)
            )
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", complete_then_execute)
    report = await run_maintenance(db_session)
    monkeypatch.undo()

    assert completed_first
    assert report.processing_jobs_failed == 0
    job = await _job(db_session, job_id)
    assert (job.status, job.error_message) == (AnalysisJobStatus.COMPLETED, None)
    assert await _repository_status(db_session, owner.repository_id) == RepositoryStatus.READY


@requires_db
async def test_repository_no_longer_processing_keeps_its_status(
    db_session: AsyncSession, owner: Owner
) -> None:
    await _set_repository_status(db_session, owner.repository_id, RepositoryStatus.READY)
    job_id = await _add_job(
        db_session, owner, AnalysisJobStatus.PROCESSING, timedelta(seconds=2101)
    )

    await run_maintenance(db_session)

    assert (await _job(db_session, job_id)).status == AnalysisJobStatus.FAILED
    assert await _repository_status(db_session, owner.repository_id) == RepositoryStatus.READY


@requires_db
async def test_repeated_runs_are_idempotent(
    db_session: AsyncSession, owner: Owner, data_dir: Path
) -> None:
    await _set_repository_status(db_session, owner.repository_id, RepositoryStatus.PROCESSING)
    job_id = await _add_job(
        db_session, owner, AnalysisJobStatus.PROCESSING, timedelta(seconds=2101)
    )
    stale_upload = _make_file(_uploads_root(data_dir) / f"{uuid.uuid4()}.zip")

    first = await run_maintenance(db_session)
    completed_at = (await _job(db_session, job_id)).completed_at
    second = await run_maintenance(db_session)

    assert (first.processing_jobs_failed, first.uploads_removed) == (1, 1)
    assert second == MaintenanceReport()
    job = await _job(db_session, job_id)
    assert (job.status, job.completed_at) == (AnalysisJobStatus.FAILED, completed_at)
    assert not stale_upload.exists()


@requires_db
async def test_recovery_preserves_previous_snapshot_and_allows_reingestion(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
    owner: Owner,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_inline_worker(monkeypatch, db_session)
    assert await _ingest(client, owner, owner.repository_id) == 202
    storage = data_dir / "repositories" / str(owner.repository_id)
    assert await _file_paths(db_session, owner.repository_id) == set(SAMPLE)
    assert storage.is_dir()

    # A later ingestion is interrupted mid-run (worker died): the job and repository are left
    # ``processing``; a new ingestion is refused while that is the case.
    await _set_repository_status(db_session, owner.repository_id, RepositoryStatus.PROCESSING)
    stuck = await _add_job(db_session, owner, AnalysisJobStatus.PROCESSING, timedelta(hours=1))
    assert await _ingest(client, owner, owner.repository_id) == 409

    report = await run_maintenance(db_session)

    assert report.processing_jobs_failed == 1
    assert report.storage_dirs_removed == 0
    assert (await _job(db_session, stuck)).status == AnalysisJobStatus.FAILED
    assert await _repository_status(db_session, owner.repository_id) == RepositoryStatus.FAILED
    # The previous successful snapshot (file rows and stored files) is untouched.
    assert await _file_paths(db_session, owner.repository_id) == set(SAMPLE)
    assert (storage / "src" / "app.py").read_bytes() == SAMPLE["src/app.py"]

    assert await _ingest(client, owner, owner.repository_id) == 202
    assert await _repository_status(db_session, owner.repository_id) == RepositoryStatus.READY


@requires_db
async def test_reingestion_possible_after_pending_recovery(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
    owner: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_inline_worker(monkeypatch, db_session)
    await _add_job(db_session, owner, AnalysisJobStatus.PENDING, timedelta(hours=7))
    assert await _ingest(client, owner, owner.repository_id) == 409  # active job exists

    await run_maintenance(db_session)

    assert await _ingest(client, owner, owner.repository_id) == 202
    assert await _repository_status(db_session, owner.repository_id) == RepositoryStatus.READY


@requires_db
async def test_maintenance_task_uses_worker_session_factory(
    db_session: AsyncSession, owner: Owner
) -> None:
    job_id = await _add_job(db_session, owner, AnalysisJobStatus.PENDING, timedelta(hours=7))

    @contextlib.asynccontextmanager
    async def session_factory() -> AsyncIterator[AsyncSession]:
        yield db_session

    assert await maintenance_task({"session_factory": session_factory}) == "ok"
    assert (await _job(db_session, job_id)).status == AnalysisJobStatus.FAILED


# --- Leftover uploads --------------------------------------------------------------------


@requires_db
async def test_upload_cleanup(db_session: AsyncSession, owner: Owner, data_dir: Path) -> None:
    root = _uploads_root(data_dir)
    active_job = await _add_job(db_session, owner, AnalysisJobStatus.PENDING, timedelta(hours=2))
    finished_job = await _add_job(db_session, owner, AnalysisJobStatus.FAILED, timedelta(hours=2))
    active = _make_file(root / f"{active_job}.zip")
    stale = _make_file(root / f"{finished_job}.zip")
    orphan = _make_file(root / f"{uuid.uuid4()}.zip")
    recent = _make_file(root / f"{uuid.uuid4()}.zip", mtime=time.time())

    report = await run_maintenance(db_session)

    assert report.uploads_removed == 2
    assert active.exists()  # the active job still needs its upload, however old
    assert recent.exists()  # may belong to a job created after the active-job snapshot
    assert not stale.exists()
    assert not orphan.exists()


# --- Leftover staging / old directories --------------------------------------------------


@requires_db
async def test_staging_and_old_directory_cleanup(
    db_session: AsyncSession, client: httpx.AsyncClient, owner: Owner, data_dir: Path
) -> None:
    root = _repositories_root(data_dir)
    idle = owner.repository_id
    busy = await _new_repository(client, owner)
    await _add_job(db_session, owner, AnalysisJobStatus.PROCESSING, timedelta(0), busy)
    # Both repositories have a current snapshot, so their ``.old-*`` directories are superseded.
    _make_dir(root / str(idle))
    _make_dir(root / str(busy))
    stale_staging = _make_dir(root / f".staging-{idle}-{HEX}")
    stale_old = _make_dir(root / f".old-{idle}-{HEX}")
    deleted_repo_staging = _make_dir(root / f".staging-{uuid.uuid4()}-{HEX}")
    recent_staging = _make_dir(root / f".staging-{idle}-{'f' * 32}", mtime=time.time())
    busy_staging = _make_dir(root / f".staging-{busy}-{HEX}")
    busy_old = _make_dir(root / f".old-{busy}-{HEX}")

    report = await run_maintenance(db_session)

    assert report.leftover_dirs_removed == 3
    assert not stale_staging.exists()
    assert not stale_old.exists()
    assert not deleted_repo_staging.exists()
    assert recent_staging.exists()
    # A repository with an active job may be extracting or swapping right now.
    assert busy_staging.exists()
    assert busy_old.exists()


@requires_db
async def test_old_directory_without_final_directory_is_preserved(
    db_session: AsyncSession, owner: Owner, data_dir: Path
) -> None:
    """A swap interrupted between its two renames (worker died) leaves only ``.old-*``: it
    is the previous snapshot, so it is kept however old it is, run after run."""
    root = _repositories_root(data_dir)
    old = _make_dir(root / f".old-{owner.repository_id}-{HEX}")

    first = await run_maintenance(db_session)
    second = await run_maintenance(db_session)

    assert first.leftover_dirs_removed == second.leftover_dirs_removed == 0
    assert (old / "inner.txt").exists()


@requires_db
async def test_old_directory_of_deleted_repository_is_removed(
    db_session: AsyncSession, data_dir: Path
) -> None:
    old = _make_dir(_repositories_root(data_dir) / f".old-{uuid.uuid4()}-{HEX}")

    report = await run_maintenance(db_session)

    assert report.leftover_dirs_removed == 1
    assert not old.exists()


def _interleave_swap_with_sweep(
    monkeypatch: pytest.MonkeyPatch, repository_id: uuid.UUID, *, start_at: str, swap_fails: bool
) -> dict[str, Any]:
    """Run a real ``swap_into_place`` of an ingestion that started after maintenance's
    active-job snapshot, paused between its two renames while maintenance sweeps.

    The swap runs in its own thread and is driven by events, not timing: its second rename
    (staging -> final) blocks until maintenance's ``.staging-*``/``.old-*`` sweep has finished.
    At that point the final directory exists only as ``.old-*`` (which keeps its old mtime).
    ``start_at`` picks when the ingestion reaches that point: before maintenance lists the
    storage root ("listing", so ``.old-*`` is a listed candidate) or after the listing but
    before the sweep ("sweep"). Then the rename fails (the swap restores ``.old-*``) or
    succeeds.
    """
    settings = get_settings()
    root = ingestion.repositories_root(settings)
    final = ingestion.repository_storage_dir(settings, repository_id)
    observed: dict[str, Any] = {"calls": 0}
    paused, resume = threading.Event(), threading.Event()
    real_replace, real_sweep, real_children = os.replace, maintenance._sweep, maintenance._children
    swap_thread: threading.Thread | None = None

    def replace(src: Any, dst: Any) -> None:
        if threading.current_thread() is not swap_thread:
            return real_replace(src, dst)
        observed["calls"] += 1
        if observed["calls"] == 2:  # staging -> final
            paused.set()
            assert resume.wait(10), "maintenance never resumed the swap"
            if swap_fails:
                raise OSError("simulated rename failure")
        return real_replace(src, dst)

    def run_swap(staging: Path) -> None:
        try:
            ingestion.swap_into_place(staging, final)
            observed["swap"] = "completed"
        except OSError:
            observed["swap"] = "restored"

    def start_ingestion() -> None:
        nonlocal swap_thread
        if swap_thread is not None:
            return
        staging = ingestion._new_staging_dir(settings, repository_id)
        (staging / "new.txt").write_bytes(b"new")
        observed["staging"] = staging
        swap_thread = threading.Thread(target=run_swap, args=(staging,))
        swap_thread.start()
        assert paused.wait(10), "swap never reached its second rename"

    def children(path: Path) -> list[str]:
        if start_at == "listing" and path == root:
            start_ingestion()
        return real_children(path)

    def sweep(path: Path, pattern: Any, *args: Any) -> int:
        if pattern is not maintenance.LEFTOVER_DIR_NAME:
            return real_sweep(path, pattern, *args)
        start_ingestion()
        old = [p for p in path.iterdir() if p.name.startswith(".old-")]
        observed["old_mtime"] = [p.stat().st_mtime for p in old]
        removed = real_sweep(path, pattern, *args)
        observed["removed"] = removed
        observed["old_after_sweep"] = [p.exists() for p in old]
        observed["staging_after_sweep"] = observed["staging"].exists()
        resume.set()
        assert swap_thread is not None
        swap_thread.join(10)
        return removed

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(maintenance, "_children", children)
    monkeypatch.setattr(maintenance, "_sweep", sweep)
    return observed


@requires_db
@pytest.mark.parametrize("start_at", ["listing", "sweep"])
async def test_sweep_during_swap_cannot_delete_the_previous_snapshot(
    db_session: AsyncSession,
    owner: Owner,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_at: str,
) -> None:
    final = _make_dir(_repositories_root(data_dir) / str(owner.repository_id))  # 2 h old
    observed = _interleave_swap_with_sweep(
        monkeypatch, owner.repository_id, start_at=start_at, swap_fails=True
    )

    report = await run_maintenance(db_session)

    # The race window was really hit: maintenance saw no active job (its snapshot predates
    # this ingestion) and the .old-* directory looked stale by age.
    assert len(observed["old_mtime"]) == 1
    assert observed["old_mtime"][0] < time.time() - maintenance.LEFTOVER_AFTER.total_seconds()
    # ...yet nothing was removed, so the failed swap restored the previous snapshot.
    assert observed["removed"] == 0 == report.leftover_dirs_removed
    assert observed["old_after_sweep"] == [True]
    assert observed["staging_after_sweep"] is True  # new staging is protected by its age
    assert (observed["calls"], observed["swap"]) == (3, "restored")  # final<-old restore
    assert (final / "inner.txt").read_bytes() == b"x"
    assert not (final / "new.txt").exists()


@requires_db
@pytest.mark.parametrize("start_at", ["listing", "sweep"])
async def test_sweep_during_successful_swap_leaves_it_intact(
    db_session: AsyncSession,
    owner: Owner,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_at: str,
) -> None:
    final = _make_dir(_repositories_root(data_dir) / str(owner.repository_id))
    observed = _interleave_swap_with_sweep(
        monkeypatch, owner.repository_id, start_at=start_at, swap_fails=False
    )

    await run_maintenance(db_session)

    assert observed["removed"] == 0
    assert (observed["calls"], observed["swap"]) == (2, "completed")
    assert (final / "new.txt").read_bytes() == b"new"
    # The swap removed its own superseded .old-* directory afterwards.
    assert [p.name for p in final.parent.iterdir() if p.name.startswith(".")] == []


# --- Deleted-repository storage ----------------------------------------------------------


@requires_db
async def test_deleted_repository_storage_is_removed(
    db_session: AsyncSession, client: httpx.AsyncClient, owner: Owner, data_dir: Path
) -> None:
    root = _repositories_root(data_dir)
    existing = _make_dir(root / str(owner.repository_id))
    recent_existing_repo = await _new_repository(client, owner)
    recent_existing = _make_dir(root / str(recent_existing_repo), mtime=time.time())
    deleted = _make_dir(root / str(uuid.uuid4()))
    (deleted / "nested").mkdir()
    (deleted / "nested" / "deep.txt").write_bytes(b"x")

    report = await run_maintenance(db_session)

    assert report.storage_dirs_removed == 1
    assert not deleted.exists()
    assert existing.is_dir() and (existing / "inner.txt").exists()
    assert recent_existing.is_dir()


@requires_db
async def test_storage_of_repository_deleted_via_api_is_removed(
    db_session: AsyncSession,
    client: httpx.AsyncClient,
    owner: Owner,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_inline_worker(monkeypatch, db_session)
    assert await _ingest(client, owner, owner.repository_id) == 202
    storage = data_dir / "repositories" / str(owner.repository_id)
    response = await client.delete(
        f"/api/v1/projects/{owner.project_id}/repositories/{owner.repository_id}",
        headers=owner.headers,
    )
    assert response.status_code == 204

    await run_maintenance(db_session)

    assert not storage.exists()


# --- Filesystem safety (no database) -----------------------------------------------------


def _sweep_all(data_dir: Path, cutoff: float | None = None) -> tuple[int, int, int]:
    """Run the three sweeps directly, treating every id as removable."""
    cutoff = time.time() - 3600 if cutoff is None else cutoff
    uploads = maintenance._sweep(
        data_dir / "ingestion-uploads", maintenance.UPLOAD_NAME, lambda _: True, False, cutoff
    )
    leftovers = maintenance._sweep(
        data_dir / "repositories", maintenance.LEFTOVER_DIR_NAME, lambda _: True, True, cutoff
    )
    storage = maintenance._sweep(
        data_dir / "repositories", maintenance.STORAGE_NAME, lambda _: True, True, None
    )
    return uploads, leftovers, storage


def test_unexpected_names_are_ignored(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    uploads = _uploads_root(data_dir)
    repositories = _repositories_root(data_dir)
    upper = str(uuid.uuid4()).upper()
    kept = [
        _make_file(uploads / "notes.zip"),
        _make_file(uploads / f"{upper}.zip"),
        _make_file(uploads / f"{uuid.uuid4()}.zip.part"),
        _make_file(uploads / f"{uuid.uuid4().hex}.zip"),
        _make_file(uploads / f"x{uuid.uuid4()}.zip"),
        _make_dir(repositories / "not-a-uuid"),
        _make_dir(repositories / upper),
        _make_dir(repositories / f"{uuid.uuid4()}-copy"),
        _make_dir(repositories / f".staging-{uuid.uuid4()}-short"),
        _make_dir(repositories / f".staging-repo-{HEX}"),
        _make_dir(repositories / f".backup-{uuid.uuid4()}-{HEX}"),
        _make_dir(repositories / f"staging-{uuid.uuid4()}-{HEX}"),
    ]

    assert _sweep_all(data_dir) == (0, 0, 0)
    assert all(path.exists() for path in kept)


def test_unexpected_entry_types_are_ignored(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    dir_named_upload = _make_dir(_uploads_root(data_dir) / f"{uuid.uuid4()}.zip")
    file_named_storage = _make_file(_repositories_root(data_dir) / str(uuid.uuid4()))
    file_named_staging = _make_file(_repositories_root(data_dir) / f".old-{uuid.uuid4()}-{HEX}")

    assert _sweep_all(data_dir) == (0, 0, 0)
    assert dir_named_upload.is_dir()
    assert file_named_storage.is_file()
    assert file_named_staging.is_file()


def test_paths_outside_the_storage_roots_are_never_touched(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _uploads_root(data_dir)
    _repositories_root(data_dir)
    in_data_root = _make_file(data_dir / f"{uuid.uuid4()}.zip")
    in_data_root_dir = _make_dir(data_dir / str(uuid.uuid4()))
    beside_data_dir = _make_dir(tmp_path / str(uuid.uuid4()))
    nested = _repositories_root(data_dir) / "nested"
    nested.mkdir()
    nested_storage = _make_dir(nested / str(uuid.uuid4()))

    assert _sweep_all(data_dir) == (0, 0, 0)
    assert in_data_root.exists()
    assert in_data_root_dir.exists()
    assert beside_data_dir.exists()
    assert nested_storage.exists()  # only direct children of a root are considered


def test_missing_roots_are_a_no_op(tmp_path: Path) -> None:
    assert _sweep_all(tmp_path / "missing") == (0, 0, 0)


def test_junction_storage_entry_is_removed_without_following(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    outside = _outside(tmp_path)
    link = _repositories_root(data_dir) / str(uuid.uuid4())
    _junction(link, outside)

    assert _sweep_all(data_dir) == (0, 0, 1)
    assert not os.path.lexists(link)
    assert (outside / "keep.txt").exists()


def test_junction_leftover_entry_is_removed_without_following(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    outside = _outside(tmp_path)
    link = _repositories_root(data_dir) / f".staging-{uuid.uuid4()}-{HEX}"
    _junction(link, outside)

    # A junction's own timestamp cannot be aged here, so use a cutoff in the future.
    assert _sweep_all(data_dir, cutoff=time.time() + 60) == (0, 1, 0)
    assert not os.path.lexists(link)
    assert (outside / "keep.txt").exists()


def test_junction_inside_a_removed_directory_is_not_followed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    outside = _outside(tmp_path)
    storage = _make_dir(_repositories_root(data_dir) / str(uuid.uuid4()))
    _junction(storage / "escape", outside)

    assert _sweep_all(data_dir) == (0, 0, 1)
    assert not storage.exists()
    assert (outside / "keep.txt").exists()


def test_junction_root_is_not_traversed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside = _outside(tmp_path)
    target = _make_dir(outside / str(uuid.uuid4()))
    _junction(data_dir / "repositories", outside)

    assert _sweep_all(data_dir) == (0, 0, 0)
    assert target.is_dir()


def test_symlinked_upload_is_unlinked_not_followed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    outside = _outside(tmp_path)
    link = _uploads_root(data_dir) / f"{uuid.uuid4()}.zip"
    _symlink(link, outside / "keep.txt")

    assert _sweep_all(data_dir, cutoff=time.time() + 60) == (1, 0, 0)
    assert not os.path.lexists(link)
    assert (outside / "keep.txt").exists()


def test_symlinked_storage_directory_is_unlinked_not_followed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    outside = _outside(tmp_path)
    link = _repositories_root(data_dir) / str(uuid.uuid4())
    _symlink(link, outside, directory=True)

    assert _sweep_all(data_dir) == (0, 0, 1)
    assert not os.path.lexists(link)
    assert (outside / "keep.txt").exists()


def test_link_entries_are_never_recursed_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated symlink (works without symlink privilege): an entry reported as a link is
    only ever unlinked, never removed recursively, even when unlinking fails."""
    data_dir = tmp_path / "data"
    storage = _make_dir(_repositories_root(data_dir) / str(uuid.uuid4()))
    unlinked: list[str] = []
    monkeypatch.setattr(maintenance, "_is_link", lambda path, st: True)

    def fake_unlink(path: Any) -> None:
        unlinked.append(Path(path).name)
        raise OSError("simulated")

    def no_rmtree(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("rmtree must not be used on a link")

    monkeypatch.setattr(maintenance.os, "unlink", fake_unlink)
    monkeypatch.setattr(maintenance.shutil, "rmtree", no_rmtree)

    assert (
        maintenance._sweep(
            data_dir / "repositories", maintenance.STORAGE_NAME, lambda _: True, True, None
        )
        == 0
    )
    monkeypatch.undo()
    assert unlinked == [storage.name]
    assert (storage / "inner.txt").exists()
