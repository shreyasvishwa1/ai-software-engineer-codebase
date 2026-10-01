"""Cancelling pending ingestion jobs (POST .../jobs/{job_id}/cancel).

API tests run against the already-migrated throwaway test database (TEST_DATABASE_URL, name
ending in "_test") inside rolled-back transactions. Queued jobs are recorded but not run
(they stay pending) unless a test runs the real worker logic; DATA_DIR is a temporary
directory and GitHub is never contacted. Races with the worker are made deterministic by
running the competing operation at a fixed point (hooks), never by sleeping.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import AnalysisJob, AnalysisJobStatus, Repository, RepositoryStatus
from app.services import job_cancellation
from app.services.analysis_jobs import claim_pending_job
from app.services.ingestion import ingestion_artifact_path, run_ingestion_job
from app.services.maintenance import run_maintenance
from tests.test_worker import _unsafe_zip, _zip, install_inline_worker, install_recording_queue

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
SAMPLE = {"src/app.py": b"print('hi')\n", "README.md": b"# demo\n"}
SOURCE_URL = "https://github.com/octo/demo"
CANCELLED = "Ingestion was cancelled"
NOT_FOUND = {"detail": "Analysis job not found"}
NOT_CANCELLABLE = {"detail": "Only pending jobs can be cancelled"}


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
    project_id: str
    repository_id: str


async def _repository(
    client: httpx.AsyncClient, owner_headers: dict[str, str], project_id: str, **fields: Any
) -> str:
    body = {"name": f"repo-{uuid.uuid4().hex[:8]}", "source_type": "zip", **fields}
    response = await client.post(
        f"/api/v1/projects/{project_id}/repositories", json=body, headers=owner_headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _owner(client: httpx.AsyncClient, email: str) -> Owner:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project = (await client.post("/api/v1/projects", json={"name": "P"}, headers=headers)).json()
    repository_id = await _repository(client, headers, project["id"])
    return Owner(headers, project["id"], repository_id)


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com")


@pytest.fixture
async def bob(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "bob@example.com")


def _repo_url(project_id: str, repository_id: str) -> str:
    return f"/api/v1/projects/{project_id}/repositories/{repository_id}"


def _cancel_url(project_id: str, repository_id: str, job_id: str) -> str:
    return f"{_repo_url(project_id, repository_id)}/jobs/{job_id}/cancel"


async def _ingest(
    client: httpx.AsyncClient, owner: Owner, repository_id: str | None = None, data: Any = None
) -> httpx.Response:
    repository_id = repository_id or owner.repository_id
    files = (
        None if data is False else {"file": ("repo.zip", data or _zip(SAMPLE), "application/zip")}
    )
    return await client.post(
        f"{_repo_url(owner.project_id, repository_id)}/ingest", files=files, headers=owner.headers
    )


async def _pending_zip_job(client: httpx.AsyncClient, owner: Owner, monkeypatch: Any) -> str:
    install_recording_queue(monkeypatch)
    response = await _ingest(client, owner)
    assert response.status_code == 202, response.text
    return str(response.json()["job_id"])


async def _cancel(
    client: httpx.AsyncClient, owner: Owner, job_id: str, **kw: Any
) -> httpx.Response:
    project_id = kw.pop("project_id", owner.project_id)
    repository_id = kw.pop("repository_id", owner.repository_id)
    headers = kw.pop("headers", owner.headers)
    return await client.post(_cancel_url(project_id, repository_id, job_id), headers=headers)


async def _job_row(db: AsyncSession, job_id: str) -> dict[str, Any]:
    row = (
        await db.execute(
            select(
                AnalysisJob.status,
                AnalysisJob.error_message,
                AnalysisJob.progress,
                AnalysisJob.started_at,
                AnalysisJob.completed_at,
            ).where(AnalysisJob.id == uuid.UUID(job_id))
        )
    ).one()
    return dict(row._mapping)


async def _repository_status(db: AsyncSession, repository_id: str) -> RepositoryStatus:
    value = await db.scalar(
        select(Repository.status).where(Repository.id == uuid.UUID(repository_id))
    )
    assert value is not None
    return value


def _upload(job_id: str) -> Path:
    return ingestion_artifact_path(get_settings(), uuid.UUID(job_id))


def _assert_cancelled_response(response: httpx.Response, job_id: str) -> None:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == job_id
    assert (body["status"], body["error_message"], body["progress"]) == ("failed", CANCELLED, 0)
    assert body["completed_at"] is not None
    assert body["started_at"] is None  # never started


@dataclass
class FileLock:
    """Simulated lock on job uploads: unlinking one fails while ``locked`` is True."""

    locked: bool = True
    attempts: list[Path] = field(default_factory=list)


@pytest.fixture
def upload_lock(monkeypatch: pytest.MonkeyPatch) -> FileLock:
    lock = FileLock()
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if lock.locked and self.parent.name == "ingestion-uploads":
            lock.attempts.append(self)
            raise PermissionError("simulated file lock")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    return lock


# --- Cancelling pending jobs -------------------------------------------------------------


@requires_db
async def test_cancel_pending_zip_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _pending_zip_job(client, alice, monkeypatch)
    assert _upload(job_id).exists()
    status_before = await _repository_status(db_session, alice.repository_id)

    response = await _cancel(client, alice, job_id)

    _assert_cancelled_response(response, job_id)
    row = await _job_row(db_session, job_id)
    assert (row["status"], row["error_message"], row["progress"]) == (
        AnalysisJobStatus.FAILED,
        CANCELLED,
        0,
    )
    assert row["completed_at"] is not None
    assert not _upload(job_id).exists()
    assert await _repository_status(db_session, alice.repository_id) == status_before
    # The job read endpoint shows the same state.
    listed = await client.get(
        f"{_repo_url(alice.project_id, alice.repository_id)}/jobs/{job_id}", headers=alice.headers
    )
    assert listed.json()["error_message"] == CANCELLED


@requires_db
async def test_cancel_pending_github_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_recording_queue(monkeypatch)
    repository_id = await _repository(
        client, alice.headers, alice.project_id, source_type="github", source_url=SOURCE_URL
    )
    response = await _ingest(client, alice, repository_id, data=False)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert not _upload(job_id).exists()  # GitHub jobs have no upload

    cancelled = await _cancel(client, alice, job_id, repository_id=repository_id)

    _assert_cancelled_response(cancelled, job_id)
    assert await _repository_status(db_session, repository_id) == RepositoryStatus.PENDING


@requires_db
async def test_reingestion_allowed_after_cancel(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _pending_zip_job(client, alice, monkeypatch)
    blocked = await _ingest(client, alice)
    assert blocked.status_code == 409  # the pending job holds the repository

    _assert_cancelled_response(await _cancel(client, alice, job_id), job_id)

    install_inline_worker(monkeypatch, db_session)
    retried = await _ingest(client, alice)
    assert retried.status_code == 202
    new_row = await _job_row(db_session, retried.json()["job_id"])
    assert new_row["status"] == AnalysisJobStatus.COMPLETED
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.READY
    assert (await _job_row(db_session, job_id))["error_message"] == CANCELLED


# --- Jobs that cannot be cancelled -------------------------------------------------------


@requires_db
async def test_processing_job_cannot_be_cancelled(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _pending_zip_job(client, alice, monkeypatch)
    # What a worker does when it starts the job.
    assert await claim_pending_job(db_session, uuid.UUID(job_id))
    await db_session.execute(
        update(Repository)
        .where(Repository.id == uuid.UUID(alice.repository_id))
        .values(status=RepositoryStatus.PROCESSING)
    )
    await db_session.commit()
    before = await _job_row(db_session, job_id)

    response = await _cancel(client, alice, job_id)

    assert (response.status_code, response.json()) == (409, NOT_CANCELLABLE)
    assert await _job_row(db_session, job_id) == before
    assert before["status"] == AnalysisJobStatus.PROCESSING
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.PROCESSING
    assert _upload(job_id).exists()  # the running job still needs its upload


@requires_db
@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_finished_job_cannot_be_cancelled(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    install_inline_worker(monkeypatch, db_session)
    response = await _ingest(
        client, alice, data=_zip(SAMPLE) if outcome == "completed" else _unsafe_zip()
    )
    job_id = response.json()["job_id"]
    before = await _job_row(db_session, job_id)
    assert before["status"] == AnalysisJobStatus(outcome)
    repository_before = await _repository_status(db_session, alice.repository_id)

    cancelled = await _cancel(client, alice, job_id)

    assert (cancelled.status_code, cancelled.json()) == (409, NOT_CANCELLABLE)
    assert await _job_row(db_session, job_id) == before
    assert before["error_message"] != CANCELLED
    assert await _repository_status(db_session, alice.repository_id) == repository_before


# --- Authorization and validation --------------------------------------------------------


@requires_db
async def test_unowned_or_missing_jobs_are_not_found(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    bob: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _pending_zip_job(client, alice, monkeypatch)
    other_repository = await _repository(client, alice.headers, alice.project_id)
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]

    attempts = [
        await _cancel(  # another user, their own project/repository in the path
            client, bob, job_id
        ),
        await _cancel(  # another user, the owner's path
            client, alice, job_id, headers=bob.headers
        ),
        await _cancel(client, alice, job_id, repository_id=other_repository),
        await _cancel(client, alice, job_id, project_id=other_project),
        await _cancel(client, alice, str(uuid.uuid4())),
    ]

    for response in attempts:
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    row = await _job_row(db_session, job_id)
    assert row["status"] == AnalysisJobStatus.PENDING  # untouched
    assert _upload(job_id).exists()


@requires_db
async def test_unauthenticated_cancel_is_rejected(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _pending_zip_job(client, alice, monkeypatch)
    for headers in ({}, {"Authorization": "Bearer not-a-token"}):
        response = await _cancel(client, alice, job_id, headers=headers)
        assert response.status_code == 401
    assert (await _job_row(db_session, job_id))["status"] == AnalysisJobStatus.PENDING


@requires_db
@pytest.mark.parametrize("segment", ["project", "repository", "job"])
async def test_invalid_uuid_is_rejected(
    client: httpx.AsyncClient, alice: Owner, segment: str
) -> None:
    ids = {"project": alice.project_id, "repository": alice.repository_id, "job": str(uuid.uuid4())}
    ids[segment] = "not-a-uuid"
    response = await client.post(
        _cancel_url(ids["project"], ids["repository"], ids["job"]), headers=alice.headers
    )
    assert response.status_code == 422


# --- Races with the worker (deterministic ordering) --------------------------------------


@requires_db
async def test_claim_then_cancel_claim_wins(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker claims the job after cancel's ownership check but before its conditional
    UPDATE (hooked at exactly that point): the claim wins, the cancel gets 409."""
    job_id = await _pending_zip_job(client, alice, monkeypatch)
    real_fail_pending_job = job_cancellation.fail_pending_job
    claimed: list[bool] = []

    async def claim_first(db: AsyncSession, target: uuid.UUID, message: str) -> bool:
        claimed.append(await claim_pending_job(db, target))
        await db.commit()
        return await real_fail_pending_job(db, target, message)

    monkeypatch.setattr(job_cancellation, "fail_pending_job", claim_first)
    response = await _cancel(client, alice, job_id)

    assert claimed == [True]
    assert (response.status_code, response.json()) == (409, NOT_CANCELLABLE)
    row = await _job_row(db_session, job_id)
    assert (row["status"], row["error_message"]) == (AnalysisJobStatus.PROCESSING, None)
    assert row["started_at"] is not None
    assert _upload(job_id).exists()  # the claimed job keeps its upload


@requires_db
async def test_cancel_then_claim_cancel_wins(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    upload_lock: FileLock,
) -> None:
    """Cancel wins; the late queue delivery is skipped by the real worker logic, which then
    removes the upload that the cancel could not delete (simulated file lock)."""
    job_id = await _pending_zip_job(client, alice, monkeypatch)

    _assert_cancelled_response(await _cancel(client, alice, job_id), job_id)
    assert upload_lock.attempts == [_upload(job_id)]
    assert _upload(job_id).exists()

    upload_lock.locked = False  # released; the worker's own cleanup can now succeed
    assert not await claim_pending_job(db_session, uuid.UUID(job_id))  # nothing to claim
    await db_session.rollback()
    assert await run_ingestion_job(db_session, uuid.UUID(job_id)) == "skipped"

    row = await _job_row(db_session, job_id)
    assert (row["status"], row["error_message"]) == (AnalysisJobStatus.FAILED, CANCELLED)
    assert row["started_at"] is None
    assert not _upload(job_id).exists()  # removed by the worker's existing cleanup


@requires_db
async def test_second_cancel_is_rejected_without_changes(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _pending_zip_job(client, alice, monkeypatch)
    first = await _cancel(client, alice, job_id)
    _assert_cancelled_response(first, job_id)
    after_first = await _job_row(db_session, job_id)

    second = await _cancel(client, alice, job_id)

    assert (second.status_code, second.json()) == (409, NOT_CANCELLABLE)
    assert await _job_row(db_session, job_id) == after_first
    # The service itself reports exactly one winner.
    assert (
        await job_cancellation.cancel_pending_ingestion_job(db_session, uuid.UUID(job_id)) is False
    )


@requires_db
async def test_job_deleted_during_cancel_is_not_found(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job disappears (e.g. its repository is deleted) after the ownership check and
    before the conditional UPDATE."""
    job_id = await _pending_zip_job(client, alice, monkeypatch)
    real_fail_pending_job = job_cancellation.fail_pending_job

    async def delete_first(db: AsyncSession, target: uuid.UUID, message: str) -> bool:
        await db.execute(delete(AnalysisJob).where(AnalysisJob.id == target))
        await db.commit()
        return await real_fail_pending_job(db, target, message)

    monkeypatch.setattr(job_cancellation, "fail_pending_job", delete_first)
    response = await _cancel(client, alice, job_id)

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    count = await db_session.scalar(
        select(func.count()).select_from(AnalysisJob).where(AnalysisJob.id == uuid.UUID(job_id))
    )
    assert count == 0


# --- Upload cleanup failure and maintenance ----------------------------------------------


@requires_db
async def test_upload_removal_failure_does_not_undo_cancel(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    upload_lock: FileLock,
) -> None:
    job_id = await _pending_zip_job(client, alice, monkeypatch)

    response = await _cancel(client, alice, job_id)

    _assert_cancelled_response(response, job_id)  # not a 500
    assert upload_lock.attempts == [_upload(job_id)]  # removal was attempted
    assert _upload(job_id).exists()  # left for maintenance
    row = await _job_row(db_session, job_id)
    assert (row["status"], row["error_message"]) == (AnalysisJobStatus.FAILED, CANCELLED)
    # Re-ingestion is possible even though the old upload is still on disk.
    assert (await _ingest(client, alice)).status_code == 202


@requires_db
async def test_maintenance_ignores_cancelled_jobs(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _pending_zip_job(client, alice, monkeypatch)
    _assert_cancelled_response(await _cancel(client, alice, job_id), job_id)
    # Age the job far beyond every maintenance threshold.
    await db_session.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == uuid.UUID(job_id))
        .values(created_at=func.now() - timedelta(days=2))
    )
    await db_session.commit()
    before = await _job_row(db_session, job_id)

    report = await run_maintenance(db_session)

    assert (report.processing_jobs_failed, report.pending_jobs_failed) == (0, 0)
    assert await _job_row(db_session, job_id) == before
