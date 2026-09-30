"""Background ingestion worker tests.

- Unit tests need neither database nor Redis.
- Worker/lifecycle tests use the throwaway PostgreSQL (TEST_DATABASE_URL, "_test").
- Queue integration tests additionally need a disposable Redis (TEST_REDIS_URL: loopback
  host and a non-zero database index; the database is flushed by these tests).

``install_inline_worker`` is shared with the Step 10-12 API tests: it replaces the Redis
enqueue with the real ``run_ingestion_job`` executed immediately on the test session, so
those tests observe the worker's final state right after the 202 response.
"""

import asyncio
import contextlib
import io
import os
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.worker import Worker, func
from pydantic import SecretStr, ValidationError
from sqlalchemy import func as sql_func
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, get_settings
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
from app.services import github as github_service
from app.services import ingestion as ingestion_service
from app.services.analysis_jobs import claim_pending_job
from app.services.ingestion import ingestion_artifact_path, run_ingestion_job
from app.workers import queue as queue_module
from app.workers.queue import INGESTION_TASK, deserialize_job, serialize_job
from app.workers.worker import WorkerSettings, ingest_repository

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)
requires_redis = pytest.mark.skipif(
    not (TEST_DATABASE_URL and TEST_REDIS_URL),
    reason="set TEST_DATABASE_URL and TEST_REDIS_URL (disposable Redis) for queue tests",
)

PASSWORD = "test-only-Passw0rd!"
ZIP_MARKER = b"UNIQUE-ZIP-CONTENT-MARKER-7f3a"
SAMPLE = {"src/app.py": b"print('hi')\n# " + ZIP_MARKER + b"\n", "README.md": b"# demo\n"}
GITHUB_IP = "140.82.112.3"
CODELOAD_IP = "140.82.112.10"
SOURCE_URL = "https://github.com/octo/demo"
CODELOAD_URL = "https://codeload.github.com/octo/demo/zip/refs/heads/main"


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _unsafe_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo("placeholder")
        info.filename = "../../evil.txt"
        zf.writestr(info, b"x")
    return buffer.getvalue()


# --- Shared helper for API tests ---------------------------------------------------------


def install_inline_worker(monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> list[str]:
    """Run queued ingestion jobs immediately with the real worker logic on ``session``.

    Returns the list of queued job ids (in order).
    """
    queued: list[str] = []

    async def enqueue_inline(job_id: uuid.UUID) -> None:
        queued.append(str(job_id))
        await run_ingestion_job(session, job_id)

    monkeypatch.setattr(queue_module, "enqueue_ingestion_job", enqueue_inline)
    return queued


def install_recording_queue(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record queued job ids without running them (jobs stay pending)."""
    queued: list[str] = []

    async def enqueue_only(job_id: uuid.UUID) -> None:
        queued.append(str(job_id))

    monkeypatch.setattr(queue_module, "enqueue_ingestion_job", enqueue_only)
    return queued


# --- Unit tests (no database, no Redis) --------------------------------------------------


def test_job_payload_serialization_is_json() -> None:
    data = {"t": 1, "f": INGESTION_TASK, "a": [str(uuid.uuid4())], "k": {}, "et": 1}
    raw = serialize_job(data)
    assert raw.startswith(b"{")  # JSON, not pickle
    assert deserialize_job(raw) == data
    with pytest.raises(ValueError):
        deserialize_job(b"[1, 2]")


def test_worker_settings() -> None:
    assert [f.name for f in WorkerSettings.functions] == [INGESTION_TASK]
    assert WorkerSettings.max_tries == 1  # no automatic retries
    assert WorkerSettings.keep_result == 0  # no stored results
    assert WorkerSettings.job_serializer is serialize_job
    assert WorkerSettings.job_deserializer is deserialize_job
    assert WorkerSettings.job_timeout == 1800


async def test_task_rejects_non_uuid_payload() -> None:
    assert await ingest_repository({"session_factory": None}, "../../etc/passwd") == "invalid"


def test_redis_url_setting() -> None:
    default = Settings(_env_file=None, secret_key="x" * 40)  # type: ignore[call-arg]
    assert default.redis_url.get_secret_value() == "redis://localhost:6379/0"
    assert "localhost" not in repr(default.redis_url)  # SecretStr: not printed
    for good in ("redis://127.0.0.1:6380/2", "rediss://user:pw@cache.example:6380", "redis://h"):
        Settings(_env_file=None, secret_key="x" * 40, redis_url=good)  # type: ignore[call-arg, arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        "http://localhost:6379/0",
        "localhost:6379",
        "redis://",
        "redis://localhost:99999/0",
        "redis://localhost:6379/db",
        "redis://localhost:6379/0#frag",
        "",
    ],
)
def test_invalid_redis_url_rejected(bad: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, secret_key="x" * 40, redis_url=bad)  # type: ignore[call-arg, arg-type]
    assert "localhost" not in str(excinfo.value.errors(include_input=False))  # value not echoed


# --- Fixtures ----------------------------------------------------------------------------


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


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    answers = {"github.com": [GITHUB_IP], "codeload.github.com": [CODELOAD_IP]}

    async def fake_resolve_host(host: str) -> list[str]:
        return answers[host]

    monkeypatch.setattr(github_service, "resolve_host", fake_resolve_host)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def _serve_github(router: respx.MockRouter, response: httpx.Response) -> None:
    router.get(f"https://{GITHUB_IP}/octo/demo/archive/HEAD.zip").mock(
        return_value=httpx.Response(302, headers={"Location": CODELOAD_URL})
    )
    router.get(f"https://{CODELOAD_IP}/octo/demo/zip/refs/heads/main").mock(
        return_value=response
    )


@dataclass
class Owner:
    headers: dict[str, str]
    token: str
    project_id: str
    repository_id: str


async def _repository(
    client: httpx.AsyncClient, headers: dict[str, str], project_id: str, **fields: object
) -> str:
    body = {"name": "repo", "source_type": "zip", **fields}
    response = await client.post(
        f"/api/v1/projects/{project_id}/repositories", json=body, headers=headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _owner(client: httpx.AsyncClient, email: str) -> Owner:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    project = (await client.post("/api/v1/projects", json={"name": "P"}, headers=headers)).json()
    repository_id = await _repository(client, headers, project["id"])
    return Owner(headers, token, project["id"], repository_id)


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com")


@pytest.fixture
async def bob(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "bob@example.com")


def _ingest_url(project_id: str, repository_id: str) -> str:
    return f"/api/v1/projects/{project_id}/repositories/{repository_id}/ingest"


async def _ingest_zip(
    client: httpx.AsyncClient, owner: Owner, data: bytes, repository_id: str | None = None
) -> httpx.Response:
    return await client.post(
        _ingest_url(owner.project_id, repository_id or owner.repository_id),
        files={"file": ("repo.zip", data, "application/zip")},
        headers=owner.headers,
    )


async def _job(db: AsyncSession, job_id: str) -> AnalysisJob:
    db.expire_all()
    job = await db.get(AnalysisJob, uuid.UUID(job_id))
    assert job is not None
    return job


async def _repository_status(db: AsyncSession, repository_id: str) -> RepositoryStatus:
    value = await db.scalar(
        select(Repository.status).where(Repository.id == uuid.UUID(repository_id))
    )
    assert value is not None
    return value


async def _file_paths(db: AsyncSession, repository_id: str) -> set[str]:
    rows = await db.scalars(select(File.path).where(File.repository_id == uuid.UUID(repository_id)))
    return set(rows)


def _uploads(data_dir: Path) -> list[str]:
    root = data_dir / "ingestion-uploads"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def _leftovers(data_dir: Path) -> list[str]:
    root = data_dir / "repositories"
    return sorted(p.name for p in root.iterdir() if p.name.startswith(".")) if root.exists() else []


# --- Queueing (API side) -----------------------------------------------------------------


@requires_db
async def test_zip_ingestion_queues_pending_job_and_stores_upload(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = install_recording_queue(monkeypatch)
    archive = _zip(SAMPLE)
    response = await _ingest_zip(client, alice, archive)
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id", "repository_id", "status"}
    assert (body["repository_id"], body["status"]) == (alice.repository_id, "pending")
    assert queued == [body["job_id"]]  # the returned id is the one queued

    job = await _job(db_session, body["job_id"])
    assert (job.status, job.job_type, job.progress) == (
        AnalysisJobStatus.PENDING,
        AnalysisJobType.INGESTION,
        0,
    )
    assert job.started_at is None
    # Nothing was ingested in the request; the upload waits under DATA_DIR by job id only.
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.PENDING
    assert await _file_paths(db_session, alice.repository_id) == set()
    artifact = ingestion_artifact_path(get_settings(), uuid.UUID(body["job_id"]))
    assert artifact.read_bytes() == archive
    assert _uploads(data_dir) == [f"{body['job_id']}.zip"]
    assert "repo.zip" not in str(artifact)  # the client filename is never used


@requires_db
async def test_github_ingestion_queues_pending_job_without_network(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    github: respx.MockRouter,
) -> None:
    queued = install_recording_queue(monkeypatch)
    repository_id = await _repository(
        client, alice.headers, alice.project_id, source_type="github", source_url=SOURCE_URL
    )
    response = await client.post(
        _ingest_url(alice.project_id, repository_id), headers=alice.headers
    )
    assert response.status_code == 202
    assert queued == [response.json()["job_id"]]
    assert github.calls.call_count == 0  # the request itself never contacts GitHub
    assert (await _job(db_session, queued[0])).status == AnalysisJobStatus.PENDING
    assert _uploads(data_dir) == []  # GitHub jobs have no uploaded artifact


@requires_db
async def test_request_level_rejections_queue_nothing(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    bob: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = install_recording_queue(monkeypatch)
    url = _ingest_url(alice.project_id, alice.repository_id)
    anonymous = await client.post(url, files={"file": ("r.zip", _zip(SAMPLE), "x/y")})
    assert anonymous.status_code == 401
    cross_user = await _ingest_zip(client, alice, _zip(SAMPLE), repository_id=bob.repository_id)
    assert cross_user.status_code == 404
    other = (await client.post("/api/v1/projects", json={"name": "O"}, headers=alice.headers)).json()
    wrong_project = await client.post(
        _ingest_url(other["id"], alice.repository_id),
        files={"file": ("r.zip", _zip(SAMPLE), "application/zip")},
        headers=alice.headers,
    )
    assert wrong_project.status_code == 404
    missing = await _ingest_zip(client, alice, _zip(SAMPLE), repository_id=str(uuid.uuid4()))
    assert missing.status_code == 404
    ssrf = await _repository(
        client, alice.headers, alice.project_id, source_type="github",
        source_url="https://169.254.169.254/latest/meta-data",
    )
    rejected = await client.post(_ingest_url(alice.project_id, ssrf), headers=alice.headers)
    assert (rejected.status_code, rejected.json()) == (
        422,
        {"detail": "Invalid GitHub repository URL"},
    )
    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 1)
    too_big = await _ingest_zip(client, alice, os.urandom(2 * 1024 * 1024))
    assert too_big.status_code == 413
    assert queued == []
    assert await db_session.scalar(select(sql_func.count()).select_from(AnalysisJob)) == 0
    assert _uploads(data_dir) == []


@requires_db
async def test_second_enqueue_while_active_is_rejected(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = install_recording_queue(monkeypatch)
    assert (await _ingest_zip(client, alice, _zip(SAMPLE))).status_code == 202
    second = await _ingest_zip(client, alice, _zip({"other.txt": b"o"}))
    assert (second.status_code, second.json()) == (
        409,
        {"detail": "Repository ingestion is already in progress"},
    )
    assert len(queued) == 1
    count = select(sql_func.count()).select_from(AnalysisJob)
    assert await db_session.scalar(count) == 1
    assert len(_uploads(data_dir)) == 1  # the rejected upload was not kept


@requires_db
async def test_queue_failure_fails_job_and_removes_upload(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_enqueue(job_id: uuid.UUID) -> None:
        raise ConnectionError("redis://:secret@10.0.0.1:6379 unreachable")

    monkeypatch.setattr(queue_module, "enqueue_ingestion_job", broken_enqueue)
    response = await _ingest_zip(client, alice, _zip(SAMPLE))
    assert (response.status_code, response.json()) == (
        503,
        {"detail": "Ingestion could not be queued; try again later"},
    )
    assert "secret" not in response.text
    jobs = (await db_session.scalars(select(AnalysisJob))).all()
    assert [(j.status, j.error_message) for j in jobs] == [
        (AnalysisJobStatus.FAILED, "Ingestion could not be queued; try again later")
    ]
    assert _uploads(data_dir) == []
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.PENDING


# --- Worker lifecycle --------------------------------------------------------------------


async def _queue_zip(
    client: httpx.AsyncClient, owner: Owner, monkeypatch: pytest.MonkeyPatch, data: bytes
) -> str:
    install_recording_queue(monkeypatch)
    response = await _ingest_zip(client, owner, data)
    assert response.status_code == 202
    return str(response.json()["job_id"])


@requires_db
async def test_worker_claims_and_completes_zip_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _queue_zip(client, alice, monkeypatch, _zip(SAMPLE))
    observed: dict[str, Any] = {}
    real_pipeline = ingestion_service._run_pipeline

    async def observing_pipeline(db: AsyncSession, *args: Any) -> None:
        # Plain column queries: expiring the session here would disturb the pipeline.
        observed["job"] = await db.scalar(
            select(AnalysisJob.status).where(AnalysisJob.id == uuid.UUID(job_id))
        )
        observed["repository"] = await _repository_status(db, alice.repository_id)
        await real_pipeline(db, *args)

    monkeypatch.setattr(ingestion_service, "_run_pipeline", observing_pipeline)
    assert await run_ingestion_job(db_session, uuid.UUID(job_id)) == "completed"

    assert observed == {"job": AnalysisJobStatus.PROCESSING, "repository": RepositoryStatus.PROCESSING}
    job = await _job(db_session, job_id)
    assert (job.status, job.progress, job.error_message) == (AnalysisJobStatus.COMPLETED, 100, None)
    assert job.started_at is not None and job.completed_at is not None
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.READY
    assert await _file_paths(db_session, alice.repository_id) == set(SAMPLE)
    assert _uploads(data_dir) == []  # artifact removed after success
    assert _leftovers(data_dir) == []


@requires_db
async def test_worker_completes_github_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    github: respx.MockRouter,
) -> None:
    queued = install_recording_queue(monkeypatch)
    repository_id = await _repository(
        client, alice.headers, alice.project_id, source_type="github", source_url=SOURCE_URL
    )
    assert (
        await client.post(_ingest_url(alice.project_id, repository_id), headers=alice.headers)
    ).status_code == 202
    _serve_github(github, httpx.Response(200, content=_zip({"demo-main/a.py": b"a"})))
    assert await run_ingestion_job(db_session, uuid.UUID(queued[0])) == "completed"
    job = await _job(db_session, queued[0])
    assert (job.status, job.progress) == (AnalysisJobStatus.COMPLETED, 100)
    assert job.completed_at is not None
    assert await _repository_status(db_session, repository_id) == RepositoryStatus.READY
    assert await _file_paths(db_session, repository_id) == {"demo-main/a.py"}


@requires_db
@pytest.mark.parametrize(
    ("archive", "error_message"),
    [
        (b"PK\x03\x04 not a zip", "Invalid ZIP archive"),
        (_unsafe_zip(), "Archive contains an unsafe path"),
    ],
)
async def test_worker_fails_bad_zip_and_cleans_up(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    archive: bytes,
    error_message: str,
) -> None:
    job_id = await _queue_zip(client, alice, monkeypatch, archive)
    assert await run_ingestion_job(db_session, uuid.UUID(job_id)) == "failed"
    job = await _job(db_session, job_id)
    assert (job.status, job.error_message, job.progress) == (
        AnalysisJobStatus.FAILED,
        error_message,
        0,
    )
    assert job.completed_at is not None
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.FAILED
    assert await _file_paths(db_session, alice.repository_id) == set()
    assert _uploads(data_dir) == []  # artifact removed after failure
    assert _leftovers(data_dir) == []
    assert not (tmp_path / "evil.txt").exists() and not (data_dir / "evil.txt").exists()


@requires_db
@pytest.mark.parametrize(
    ("mock", "error_message"),
    [
        ({"return_value": httpx.Response(404, text="SECRET body")}, "GitHub repository not found"),
        ({"side_effect": httpx.ReadTimeout("timed out")}, "GitHub download timed out"),
    ],
)
async def test_worker_fails_github_errors(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    github: respx.MockRouter,
    mock: dict[str, Any],
    error_message: str,
) -> None:
    queued = install_recording_queue(monkeypatch)
    repository_id = await _repository(
        client, alice.headers, alice.project_id, source_type="github", source_url=SOURCE_URL
    )
    await client.post(_ingest_url(alice.project_id, repository_id), headers=alice.headers)
    github.get(f"https://{GITHUB_IP}/octo/demo/archive/HEAD.zip").mock(**mock)
    assert await run_ingestion_job(db_session, uuid.UUID(queued[0])) == "failed"
    job = await _job(db_session, queued[0])
    assert (job.status, job.error_message) == (AnalysisJobStatus.FAILED, error_message)
    assert await _repository_status(db_session, repository_id) == RepositoryStatus.FAILED


@requires_db
async def test_worker_unexpected_error_is_recorded_safely(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _queue_zip(client, alice, monkeypatch, _zip(SAMPLE))

    async def exploding(db: AsyncSession, job_id: uuid.UUID) -> None:
        raise RuntimeError("SECRET /srv/internal traceback detail")

    monkeypatch.setattr(ingestion_service, "mark_completed", exploding)
    assert await run_ingestion_job(db_session, uuid.UUID(job_id)) == "failed"  # never raises
    job = await _job(db_session, job_id)
    assert (job.status, job.error_message) == (
        AnalysisJobStatus.FAILED,
        "Ingestion failed due to an internal error",
    )
    assert await _file_paths(db_session, alice.repository_id) == set()
    assert _uploads(data_dir) == []


@requires_db
async def test_job_can_only_be_claimed_once(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid.UUID(await _queue_zip(client, alice, monkeypatch, _zip(SAMPLE)))
    assert await claim_pending_job(db_session, job_id) is True
    assert await claim_pending_job(db_session, job_id) is False  # conditional UPDATE
    await db_session.commit()
    # A second worker receiving a job that is already processing does nothing, and must
    # not delete the upload the first worker is using.
    assert await run_ingestion_job(db_session, job_id) == "skipped"
    assert _uploads(data_dir) == [f"{job_id}.zip"]
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.PENDING


@requires_db
async def test_duplicate_delivery_runs_ingestion_once(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid.UUID(await _queue_zip(client, alice, monkeypatch, _zip(SAMPLE)))
    runs: list[uuid.UUID] = []
    real_pipeline = ingestion_service._run_pipeline

    async def counting_pipeline(db: AsyncSession, repository: Any, jid: uuid.UUID, p: Any) -> None:
        runs.append(jid)
        await real_pipeline(db, repository, jid, p)

    monkeypatch.setattr(ingestion_service, "_run_pipeline", counting_pipeline)
    assert await run_ingestion_job(db_session, job_id) == "completed"
    assert await run_ingestion_job(db_session, job_id) == "skipped"
    assert runs == [job_id]
    assert (await _job(db_session, str(job_id))).status == AnalysisJobStatus.COMPLETED


@requires_db
async def test_worker_does_not_trust_payload_or_mismatched_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    bob: Owner,
) -> None:
    # A job whose repository is not in its project (never produced by the app).
    forged = AnalysisJob(
        project_id=uuid.UUID(bob.project_id),
        repository_id=uuid.UUID(alice.repository_id),
        job_type=AnalysisJobType.INGESTION,
        status=AnalysisJobStatus.PENDING,
        progress=0,
    )
    db_session.add(forged)
    await db_session.commit()
    assert await run_ingestion_job(db_session, forged.id) == "invalid"
    job = await _job(db_session, str(forged.id))
    assert (job.status, job.error_message) == (AnalysisJobStatus.FAILED, "Invalid ingestion job")
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.PENDING

    # An unknown job id: nothing to run; a stray upload for it is removed.
    ghost = uuid.uuid4()
    stray = ingestion_artifact_path(get_settings(), ghost)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"leftover")
    assert await run_ingestion_job(db_session, ghost) == "missing"
    assert not stray.exists()


@requires_db
async def test_reingestion_cleans_old_storage_and_uploads(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_inline_worker(monkeypatch, db_session)
    assert (await _ingest_zip(client, alice, _zip({"a.txt": b"1"}))).status_code == 202
    assert (await _ingest_zip(client, alice, _zip({"b.txt": b"2"}))).status_code == 202
    storage = data_dir / "repositories" / alice.repository_id
    assert sorted(p.name for p in storage.iterdir()) == ["b.txt"]
    assert _leftovers(data_dir) == []  # no .old-/.staging- directories
    assert _uploads(data_dir) == []


@requires_db
async def test_jobs_endpoint_shows_worker_state(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _queue_zip(client, alice, monkeypatch, _zip(SAMPLE))
    url = f"/api/v1/projects/{alice.project_id}/repositories/{alice.repository_id}/jobs/{job_id}"
    assert (await client.get(url, headers=alice.headers)).json()["status"] == "pending"
    await run_ingestion_job(db_session, uuid.UUID(job_id))
    db_session.expire_all()
    done = (await client.get(url, headers=alice.headers)).json()
    assert (done["status"], done["progress"]) == ("completed", 100)


# --- Real Redis + arq worker -------------------------------------------------------------


@pytest.fixture
async def redis_pool(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ArqRedis]:
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(
        settings, job_serializer=serialize_job, job_deserializer=deserialize_job
    )
    await pool.flushdb()
    try:
        yield pool
    finally:
        await pool.flushdb()
        await pool.aclose()


def _worker(pool: ArqRedis, session: AsyncSession) -> Worker:
    """A burst-mode arq worker on the test Redis, sharing the test's database session."""

    @contextlib.asynccontextmanager
    async def shared_session() -> AsyncIterator[AsyncSession]:
        yield session

    assert TEST_REDIS_URL
    # Its own connection pool (closed by worker.close()), and arq's default signal handling
    # (handle_signals=False makes close() reference SIGUSR1, which Windows lacks).
    return Worker(
        functions=[func(ingest_repository, name=INGESTION_TASK)],
        redis_settings=RedisSettings.from_dsn(TEST_REDIS_URL),
        burst=True,
        ctx={"session_factory": shared_session},
        job_serializer=serialize_job,
        job_deserializer=deserialize_job,
        keep_result=0,
        max_tries=1,
        poll_delay=0.05,
    )


async def _redis_dump(pool: ArqRedis) -> bytes:
    """Every key and value in the (disposable) Redis database."""
    dump = b""
    for key in await pool.keys("*"):
        kind = (await pool.type(key)).decode()
        dump += key + b"="
        if kind == "string":
            dump += await pool.get(key) or b""
        elif kind == "zset":
            dump += b",".join(await pool.zrange(key, 0, -1))
        dump += b"\n"
    return dump


@requires_redis
async def test_redis_payload_contains_only_the_job_id(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    redis_pool: ArqRedis,
) -> None:
    response = await _ingest_zip(client, alice, _zip(SAMPLE))
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    raw = await redis_pool.get(f"arq:job:{job_id}")
    assert raw is not None
    payload = deserialize_job(raw)
    assert payload["f"] == INGESTION_TASK
    assert payload["a"] == [job_id]  # the AnalysisJob id, nothing else
    assert payload["k"] == {}
    dump = await _redis_dump(redis_pool)
    for forbidden in (
        ZIP_MARKER,
        b"PK\x03\x04",
        alice.token.encode(),
        PASSWORD.encode(),
        get_settings().secret_key.get_secret_value().encode(),
        str(data_dir).encode(),
        b"alice@example.com",
    ):
        assert forbidden not in dump


@requires_redis
async def test_arq_worker_processes_zip_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    redis_pool: ArqRedis,
) -> None:
    response = await _ingest_zip(client, alice, _zip(SAMPLE))
    job_id = response.json()["job_id"]
    assert (await _job(db_session, job_id)).status == AnalysisJobStatus.PENDING

    worker = _worker(redis_pool, db_session)
    try:
        await worker.main()
    finally:
        await worker.close()
    assert worker.jobs_complete == 1 and worker.jobs_failed == 0
    job = await _job(db_session, job_id)
    assert (job.status, job.progress) == (AnalysisJobStatus.COMPLETED, 100)
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.READY
    assert await _file_paths(db_session, alice.repository_id) == set(SAMPLE)
    assert _uploads(data_dir) == []
    assert await redis_pool.keys("arq:result:*") == []  # keep_result=0


@requires_redis
async def test_arq_worker_processes_github_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    redis_pool: ArqRedis,
    github: respx.MockRouter,
) -> None:
    repository_id = await _repository(
        client, alice.headers, alice.project_id, source_type="github", source_url=SOURCE_URL
    )
    response = await client.post(
        _ingest_url(alice.project_id, repository_id), headers=alice.headers
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert SOURCE_URL.encode() not in await _redis_dump(redis_pool)  # URL loaded from the DB
    _serve_github(github, httpx.Response(200, content=_zip({"demo-main/x.py": b"x"})))

    worker = _worker(redis_pool, db_session)
    try:
        await worker.main()
    finally:
        await worker.close()
    job = await _job(db_session, job_id)
    assert job.status == AnalysisJobStatus.COMPLETED
    assert await _repository_status(db_session, repository_id) == RepositoryStatus.READY


@requires_redis
async def test_only_one_arq_worker_runs_a_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    redis_pool: ArqRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = await _ingest_zip(client, alice, _zip(SAMPLE))
    job_id = response.json()["job_id"]
    # Enqueueing the same job id again is a no-op (arq job id == AnalysisJob id).
    assert await redis_pool.enqueue_job(INGESTION_TASK, job_id, _job_id=job_id) is None

    runs: list[Any] = []
    real_pipeline = ingestion_service._run_pipeline

    async def counting_pipeline(*args: Any) -> None:
        runs.append(args[2])
        await real_pipeline(*args)

    monkeypatch.setattr(ingestion_service, "_run_pipeline", counting_pipeline)
    first, second = _worker(redis_pool, db_session), _worker(redis_pool, db_session)
    try:
        await asyncio.gather(first.main(), second.main())
    finally:
        await first.close()
        await second.close()
    assert len(runs) == 1
    assert first.jobs_complete + second.jobs_complete == 1
    assert (await _job(db_session, job_id)).status == AnalysisJobStatus.COMPLETED


@requires_redis
async def test_active_job_blocks_second_enqueue_with_real_queue(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    redis_pool: ArqRedis,
) -> None:
    assert (await _ingest_zip(client, alice, _zip(SAMPLE))).status_code == 202
    assert (await _ingest_zip(client, alice, _zip(SAMPLE))).status_code == 409
    assert await redis_pool.zcard("arq:queue") == 1
    await db_session.execute(update(Repository).values(name="unchanged"))  # session still usable
