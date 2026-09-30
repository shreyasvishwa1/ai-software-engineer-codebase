"""Repository analysis job tests.

Model/schema tests need no database. The migration test runs only against a dedicated,
disposable database (TEST_MIGRATION_DATABASE_URL, name ending in "_test") because it
upgrades and downgrades the schema. API tests run against the already-migrated throwaway
test database (TEST_DATABASE_URL, name ending in "_test") inside rolled-back transactions,
with DATA_DIR in a temporary directory and GitHub mocked (respx + mocked DNS).
"""

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import respx
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import (
    AnalysisJob,
    AnalysisJobStatus,
    AnalysisJobType,
    Base,
    Repository,
    RepositoryStatus,
)
from app.schemas.analysis_job import AnalysisJobResponse
from app.services import github as github_service
from app.services import ingestion as ingestion_service
from app.services.analysis_jobs import new_ingestion_job
from tests.test_repository_ingestion import _zip, _zip_raw_names

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_MIGRATION_DATABASE_URL = os.environ.get("TEST_MIGRATION_DATABASE_URL")
BACKEND_DIR = Path(__file__).resolve().parent.parent
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)
requires_migration_db = pytest.mark.skipif(
    not TEST_MIGRATION_DATABASE_URL,
    reason="set TEST_MIGRATION_DATABASE_URL to a dedicated, disposable test database",
)

PASSWORD = "test-only-Passw0rd!"
JOB_FIELDS = {
    "id",
    "project_id",
    "repository_id",
    "job_type",
    "status",
    "progress",
    "error_message",
    "created_at",
    "started_at",
    "completed_at",
}
JOB_NOT_FOUND = {"detail": "Analysis job not found"}
REPOSITORY_NOT_FOUND = {"detail": "Repository not found"}
SAMPLE = {"src/app.py": b"print('hi')\n", "README.md": b"# r\n"}
GITHUB_IP = "140.82.112.3"
CODELOAD_IP = "140.82.112.10"


# --- Model and schema (no database) ------------------------------------------------------


def test_analysis_job_columns_and_foreign_keys() -> None:
    table = Base.metadata.tables["analysis_jobs"]
    assert {"repository_id", "job_type"} <= set(table.c.keys())
    assert table.c.repository_id.nullable  # only for rows created before migration 0002
    fks = {fk.parent.name: (fk.column.table.name, fk.ondelete) for fk in table.foreign_keys}
    assert fks == {
        "project_id": ("projects", "CASCADE"),
        "repository_id": ("repositories", "CASCADE"),
    }
    assert table.c.job_type.type.enums == ["ingestion"]
    assert table.c.status.type.enums == ["pending", "processing", "completed", "failed"]
    assert str(table.c.status.server_default.arg) == "pending"


def test_analysis_job_constraints_and_indexes() -> None:
    table = Base.metadata.tables["analysis_jobs"]
    checks = {c.name: c for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
    required = checks["ck_analysis_jobs_repository_and_type_required"]
    assert "repository_id IS NOT NULL AND job_type IS NOT NULL" in str(required.sqltext)
    assert required.dialect_options["postgresql"]["not_valid"] is True
    indexes = {ix.name: ix for ix in table.indexes}
    assert [c.name for c in indexes["ix_analysis_jobs_repository_id_created_at"].columns] == [
        "repository_id",
        "created_at",
    ]
    active = indexes["uq_analysis_jobs_active_ingestion"]
    assert active.unique is True
    assert "status IN ('pending', 'processing')" in str(active.dialect_options["postgresql"]["where"])


def test_response_schema_exposes_only_job_fields() -> None:
    assert set(AnalysisJobResponse.model_fields) == JOB_FIELDS


def test_new_ingestion_job_takes_associations_from_repository() -> None:
    repository = Repository(id=uuid.uuid4(), project_id=uuid.uuid4())
    job = new_ingestion_job(repository)
    assert (job.repository_id, job.project_id) == (repository.id, repository.project_id)
    assert job.job_type == AnalysisJobType.INGESTION
    assert job.status == AnalysisJobStatus.PENDING
    assert job.progress == 0
    assert job.started_at is None and job.completed_at is None


# --- Migration 0001 -> 0002 -> 0001 -> 0002 (disposable database) ------------------------


def _alembic(url: str, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": url, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@requires_migration_db
async def test_migration_upgrade_downgrade_upgrade() -> None:
    assert TEST_MIGRATION_DATABASE_URL
    url = make_url(TEST_MIGRATION_DATABASE_URL)
    if not (url.database or "").endswith("_test"):
        pytest.fail("refusing: TEST_MIGRATION_DATABASE_URL database name must end with '_test'")
    raw_url = url.set(drivername="postgresql").render_as_string(hide_password=False)

    _alembic(TEST_MIGRATION_DATABASE_URL, "downgrade", "base")  # disposable database only
    _alembic(TEST_MIGRATION_DATABASE_URL, "upgrade", "0001")

    conn = await asyncpg.connect(raw_url)
    try:
        # A job created under 0001 has no repository or type.
        user_id = await conn.fetchval(
            "INSERT INTO users (id, email, password_hash) "
            "VALUES (gen_random_uuid(), 'legacy@example.com', 'x') RETURNING id"
        )
        project_id = await conn.fetchval(
            "INSERT INTO projects (id, user_id, name) VALUES (gen_random_uuid(), $1, 'P') "
            "RETURNING id",
            user_id,
        )
        repository_id = await conn.fetchval(
            "INSERT INTO repositories (id, project_id, name, source_type) "
            "VALUES (gen_random_uuid(), $1, 'r', 'zip') RETURNING id",
            project_id,
        )
        legacy_job = await conn.fetchval(
            "INSERT INTO analysis_jobs (id, project_id) VALUES (gen_random_uuid(), $1) "
            "RETURNING id",
            project_id,
        )
        running_job = await conn.fetchval(
            "INSERT INTO analysis_jobs (id, project_id, status) "
            "VALUES (gen_random_uuid(), $1, 'running') RETURNING id",
            project_id,
        )
    finally:
        await conn.close()

    _alembic(TEST_MIGRATION_DATABASE_URL, "upgrade", "head")
    assert _alembic(TEST_MIGRATION_DATABASE_URL, "current").split() == ["0002", "(head)"]

    conn = await asyncpg.connect(raw_url)
    try:
        # Existing rows: no guessed repository/type; statuses keep their meaning.
        legacy = await conn.fetchrow(
            "SELECT repository_id, job_type, status FROM analysis_jobs WHERE id = $1", legacy_job
        )
        assert (legacy["repository_id"], legacy["job_type"], legacy["status"]) == (
            None,
            None,
            "pending",
        )
        assert await conn.fetchval(
            "SELECT status FROM analysis_jobs WHERE id = $1", running_job
        ) == "processing"

        labels = await conn.fetch(
            "SELECT t.typname, e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname IN ('analysis_job_status', 'analysis_job_type') "
            "ORDER BY t.typname, e.enumsortorder"
        )
        assert [(r["typname"], r["enumlabel"]) for r in labels] == [
            ("analysis_job_status", "pending"),
            ("analysis_job_status", "processing"),
            ("analysis_job_status", "completed"),
            ("analysis_job_status", "failed"),
            ("analysis_job_type", "ingestion"),
        ]
        fk = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'fk_analysis_jobs_repository_id_repositories'"
        )
        assert fk == "FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE CASCADE"
        check = await conn.fetchrow(
            "SELECT convalidated, pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conname = 'ck_analysis_jobs_repository_and_type_required'"
        )
        assert check["convalidated"] is False  # NOT VALID: existing rows are not checked
        indexes = {
            r["indexname"]: r["indexdef"]
            for r in await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'analysis_jobs'"
            )
        }
        assert "(repository_id, created_at)" in indexes["ix_analysis_jobs_repository_id_created_at"]
        assert "UNIQUE" in indexes["uq_analysis_jobs_active_ingestion"]
        assert "WHERE" in indexes["uq_analysis_jobs_active_ingestion"]

        # New rows must name a repository and type.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO analysis_jobs (id, project_id) VALUES (gen_random_uuid(), $1)",
                project_id,
            )
        insert_job = (
            "INSERT INTO analysis_jobs (id, project_id, repository_id, job_type, status) "
            "VALUES (gen_random_uuid(), $1, $2, 'ingestion', $3)"
        )
        await conn.execute(insert_job, project_id, repository_id, "processing")
        with pytest.raises(asyncpg.UniqueViolationError):  # second active ingestion job
            await conn.execute(insert_job, project_id, repository_id, "pending")
        await conn.execute(insert_job, project_id, repository_id, "completed")  # allowed
    finally:
        await conn.close()

    _alembic(TEST_MIGRATION_DATABASE_URL, "downgrade", "0001")
    conn = await asyncpg.connect(raw_url)
    try:
        columns = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'analysis_jobs'"
            )
        }
        assert not {"repository_id", "job_type"} & columns
        assert await conn.fetchval(
            "SELECT status FROM analysis_jobs WHERE id = $1", legacy_job
        ) == "queued"
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_type WHERE typname = 'analysis_job_type'"
        ) == 0
    finally:
        await conn.close()

    _alembic(TEST_MIGRATION_DATABASE_URL, "upgrade", "head")  # upgrade again
    assert _alembic(TEST_MIGRATION_DATABASE_URL, "current").split() == ["0002", "(head)"]
    _alembic(TEST_MIGRATION_DATABASE_URL, "check")


# --- API fixtures ------------------------------------------------------------------------


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
    """Mocked GitHub: fixed public DNS answers; unmatched requests fail."""
    answers = {"github.com": [GITHUB_IP], "codeload.github.com": [CODELOAD_IP]}

    async def fake_resolve_host(host: str) -> list[str]:
        return answers[host]

    monkeypatch.setattr(github_service, "resolve_host", fake_resolve_host)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@dataclass
class Owner:
    headers: dict[str, str]
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


async def _project(client: httpx.AsyncClient, headers: dict[str, str], name: str) -> str:
    response = await client.post("/api/v1/projects", json={"name": name}, headers=headers)
    return str(response.json()["id"])


async def _owner(client: httpx.AsyncClient, email: str) -> Owner:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project_id = await _project(client, headers, "P")
    return Owner(headers, project_id, await _repository(client, headers, project_id))


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com")


@pytest.fixture
async def bob(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "bob@example.com")


def _jobs_url(project_id: str, repository_id: str, job_id: str | None = None) -> str:
    base = f"/api/v1/projects/{project_id}/repositories/{repository_id}/jobs"
    return f"{base}/{job_id}" if job_id else base


async def _ingest_zip(
    client: httpx.AsyncClient, owner: Owner, data: bytes, repository_id: str | None = None
) -> httpx.Response:
    return await client.post(
        f"/api/v1/projects/{owner.project_id}/repositories/"
        f"{repository_id or owner.repository_id}/ingest",
        files={"file": ("repo.zip", data, "application/zip")},
        headers=owner.headers,
    )


async def _jobs(client: httpx.AsyncClient, owner: Owner, repository_id: str | None = None) -> Any:
    response = await client.get(
        _jobs_url(owner.project_id, repository_id or owner.repository_id), headers=owner.headers
    )
    assert response.status_code == 200
    return response.json()


async def _job_count(db: AsyncSession, **filters: str) -> int:
    query = select(func.count()).select_from(AnalysisJob)
    for column, value in filters.items():
        query = query.where(getattr(AnalysisJob, column) == uuid.UUID(value))
    return int(await db.scalar(query) or 0)


async def _repository_status(db: AsyncSession, repository_id: str) -> RepositoryStatus:
    value = await db.scalar(
        select(Repository.status).where(Repository.id == uuid.UUID(repository_id))
    )
    assert value is not None
    return value


def _assert_failed_job(job: dict[str, Any], owner: Owner, error_message: str) -> None:
    assert job["status"] == "failed"
    assert job["error_message"] == error_message
    assert job["progress"] == 0
    assert job["started_at"] is not None  # it was processing before it failed
    assert job["completed_at"] is not None
    assert (job["repository_id"], job["project_id"]) == (owner.repository_id, owner.project_id)


# --- API: authentication -----------------------------------------------------------------


@requires_db
async def test_unauthenticated_requests_fail(client: httpx.AsyncClient, alice: Owner) -> None:
    for url in (
        _jobs_url(alice.project_id, alice.repository_id),
        _jobs_url(alice.project_id, alice.repository_id, str(uuid.uuid4())),
    ):
        response = await client.get(url)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


# --- API: jobs created by ingestion ------------------------------------------------------


@requires_db
async def test_successful_zip_ingestion_records_completed_job(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    assert (await _jobs(client, alice)) == []
    response = await _ingest_zip(client, alice, _zip(SAMPLE))
    assert response.status_code == 200

    jobs = await _jobs(client, alice)
    assert len(jobs) == 1
    job = jobs[0]
    assert set(job) == JOB_FIELDS
    assert (job["repository_id"], job["project_id"]) == (alice.repository_id, alice.project_id)
    assert job["job_type"] == "ingestion"
    assert job["status"] == "completed"
    assert job["progress"] == 100
    assert job["error_message"] is None
    assert job["started_at"] is not None and job["completed_at"] is not None
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.READY

    single = await client.get(
        _jobs_url(alice.project_id, alice.repository_id, job["id"]), headers=alice.headers
    )
    assert single.status_code == 200
    assert single.json() == job


@requires_db
async def test_successful_github_ingestion_records_completed_job(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, github: respx.MockRouter
) -> None:
    repository_id = await _repository(
        client,
        alice.headers,
        alice.project_id,
        source_type="github",
        source_url="https://github.com/octo/demo",
    )
    codeload = "https://codeload.github.com/octo/demo/zip/refs/heads/main"
    github.get(f"https://{GITHUB_IP}/octo/demo/archive/HEAD.zip").mock(
        return_value=httpx.Response(302, headers={"Location": codeload})
    )
    github.get(f"https://{CODELOAD_IP}/octo/demo/zip/refs/heads/main").mock(
        return_value=httpx.Response(200, content=_zip({"demo-main/a.py": b"a"}))
    )
    response = await client.post(
        f"/api/v1/projects/{alice.project_id}/repositories/{repository_id}/ingest",
        headers=alice.headers,
    )
    assert response.status_code == 200
    jobs = await _jobs(client, alice, repository_id)
    assert [(j["job_type"], j["status"], j["progress"]) for j in jobs] == [
        ("ingestion", "completed", 100)
    ]
    assert jobs[0]["repository_id"] == repository_id
    assert await _repository_status(db_session, repository_id) == RepositoryStatus.READY


@requires_db
@pytest.mark.parametrize(
    ("archive", "expected_status", "error_message"),
    [
        (b"PK\x03\x04 not really a zip", 400, "Invalid ZIP archive"),
        (_zip_raw_names(["ok.txt", "../../evil.txt"]), 400, "Archive contains an unsafe path"),
    ],
)
async def test_failed_zip_ingestion_records_failed_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    archive: bytes,
    expected_status: int,
    error_message: str,
) -> None:
    response = await _ingest_zip(client, alice, archive)
    assert response.status_code == expected_status
    jobs = await _jobs(client, alice)
    assert len(jobs) == 1
    _assert_failed_job(jobs[0], alice, error_message)
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.FAILED


@requires_db
async def test_github_download_failure_records_failed_job(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, github: respx.MockRouter
) -> None:
    repository_id = await _repository(
        client,
        alice.headers,
        alice.project_id,
        source_type="github",
        source_url="https://github.com/octo/demo",
    )
    github.get(f"https://{GITHUB_IP}/octo/demo/archive/HEAD.zip").mock(
        return_value=httpx.Response(404, text="SECRET upstream body")
    )
    response = await client.post(
        f"/api/v1/projects/{alice.project_id}/repositories/{repository_id}/ingest",
        headers=alice.headers,
    )
    assert response.status_code == 404
    jobs = await _jobs(client, alice, repository_id)
    owner = Owner(alice.headers, alice.project_id, repository_id)
    _assert_failed_job(jobs[0], owner, "GitHub repository not found")
    assert "SECRET" not in str(jobs)


@requires_db
async def test_unexpected_failure_records_generic_error(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exploding_mark_completed(db: AsyncSession, job_id: uuid.UUID) -> None:
        raise RuntimeError("SECRET internal detail /srv/data")

    monkeypatch.setattr(ingestion_service, "mark_completed", exploding_mark_completed)
    # Starlette re-raises unhandled errors after sending the 500; a real server logs them.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as server:
        response = await server.post(
            f"/api/v1/projects/{alice.project_id}/repositories/{alice.repository_id}/ingest",
            files={"file": ("repo.zip", _zip(SAMPLE), "application/zip")},
            headers=alice.headers,
        )
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected error occurred. Please try again later.",
        }
    }
    assert "SECRET" not in response.text
    jobs = await _jobs(client, alice)
    _assert_failed_job(jobs[0], alice, "Ingestion failed due to an internal error")
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.FAILED


@requires_db
async def test_storage_failure_records_failed_job(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_swap(staging: Path, final: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ingestion_service, "swap_into_place", failing_swap)
    response = await _ingest_zip(client, alice, _zip(SAMPLE))
    assert response.status_code == 500
    jobs = await _jobs(client, alice)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "failed"  # not left completed
    assert jobs[0]["error_message"] == "Repository storage could not be updated"


@requires_db
async def test_requests_rejected_before_ingestion_create_no_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_bad = await _repository(
        client, alice.headers, alice.project_id, source_type="github", source_url="http://x"
    )
    invalid = await client.post(
        f"/api/v1/projects/{alice.project_id}/repositories/{github_bad}/ingest",
        headers=alice.headers,
    )
    assert invalid.status_code == 422
    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 1)
    too_big = await _ingest_zip(client, alice, os.urandom(2 * 1024 * 1024))
    assert too_big.status_code == 413
    assert await _job_count(db_session, project_id=alice.project_id) == 0


# --- API: ownership ----------------------------------------------------------------------


@requires_db
async def test_cross_user_job_access_is_not_found(
    client: httpx.AsyncClient, alice: Owner, bob: Owner
) -> None:
    assert (await _ingest_zip(client, bob, _zip(SAMPLE))).status_code == 200
    bob_job = (await _jobs(client, bob))[0]["id"]

    for project_id, repository_id in (
        (bob.project_id, bob.repository_id),
        (alice.project_id, bob.repository_id),
    ):
        listed = await client.get(_jobs_url(project_id, repository_id), headers=alice.headers)
        assert listed.status_code == 404
        assert listed.json() == REPOSITORY_NOT_FOUND

    for project_id, repository_id in (
        (bob.project_id, bob.repository_id),  # Bob's project/repository
        (alice.project_id, bob.repository_id),  # Alice's project, Bob's repository
        (alice.project_id, alice.repository_id),  # Alice's own repository, Bob's job
    ):
        response = await client.get(
            _jobs_url(project_id, repository_id, bob_job), headers=alice.headers
        )
        assert response.status_code == 404
        assert response.json() == JOB_NOT_FOUND
        assert bob.repository_id not in response.text


@requires_db
async def test_job_through_wrong_project_or_repository_is_not_found(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    sibling = await _repository(client, alice.headers, alice.project_id, name="sibling")
    other_project = await _project(client, alice.headers, "Other")
    assert (await _ingest_zip(client, alice, _zip(SAMPLE))).status_code == 200
    job_id = (await _jobs(client, alice))[0]["id"]

    for project_id, repository_id in (
        (alice.project_id, sibling),  # another repository in the same project
        (other_project, alice.repository_id),  # the right repository under another project
    ):
        response = await client.get(
            _jobs_url(project_id, repository_id, job_id), headers=alice.headers
        )
        assert response.status_code == 404
        assert response.json() == JOB_NOT_FOUND
    assert (await _jobs(client, alice, sibling)) == []  # only repository-specific jobs


@requires_db
async def test_nonexistent_resources_and_invalid_ids(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    missing = str(uuid.uuid4())
    assert (
        await client.get(_jobs_url(missing, alice.repository_id), headers=alice.headers)
    ).json() == REPOSITORY_NOT_FOUND
    assert (
        await client.get(_jobs_url(alice.project_id, missing), headers=alice.headers)
    ).status_code == 404
    job = await client.get(
        _jobs_url(alice.project_id, alice.repository_id, missing), headers=alice.headers
    )
    assert (job.status_code, job.json()) == (404, JOB_NOT_FOUND)
    for url in (
        _jobs_url("not-a-uuid", alice.repository_id),
        _jobs_url(alice.project_id, "not-a-uuid"),
        _jobs_url(alice.project_id, alice.repository_id, "not-a-uuid"),
    ):
        assert (await client.get(url, headers=alice.headers)).status_code == 422


# --- API: listing ------------------------------------------------------------------------


@requires_db
async def test_list_multiple_jobs_newest_first(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    assert (await _ingest_zip(client, alice, _zip(SAMPLE))).status_code == 200
    assert (await _ingest_zip(client, alice, b"not a zip")).status_code == 400
    assert (await _ingest_zip(client, alice, _zip({"b.txt": b"b"}))).status_code == 200
    jobs = await _jobs(client, alice)
    assert sorted(j["status"] for j in jobs) == ["completed", "completed", "failed"]

    # Jobs created in one test transaction share now(); give them distinct creation times.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = [j["id"] for j in jobs]
    for offset, job_id in enumerate(ids):
        await db_session.execute(
            update(AnalysisJob)
            .where(AnalysisJob.id == uuid.UUID(job_id))
            .values(created_at=base + timedelta(minutes=offset))
        )
    await db_session.commit()
    first = await _jobs(client, alice)
    assert [j["id"] for j in first] == list(reversed(ids))
    assert first == await _jobs(client, alice)


# --- API: no client-side job creation ----------------------------------------------------


@requires_db
async def test_clients_cannot_create_or_modify_jobs(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    list_url = _jobs_url(alice.project_id, alice.repository_id)
    job_url = _jobs_url(alice.project_id, alice.repository_id, str(uuid.uuid4()))
    body = {
        "project_id": alice.project_id,
        "repository_id": alice.repository_id,
        "job_type": "ingestion",
        "status": "completed",
        "progress": 100,
    }
    for method, url in (("POST", list_url), ("PUT", job_url), ("PATCH", job_url), ("DELETE", job_url)):
        response = await client.request(method, url, json=body, headers=alice.headers)
        assert response.status_code == 405
    assert await _job_count(db_session, repository_id=alice.repository_id) == 0


# --- Cascades ----------------------------------------------------------------------------


@requires_db
async def test_deleting_repository_or_project_removes_jobs(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    second = await _repository(client, alice.headers, alice.project_id, name="second")
    assert (await _ingest_zip(client, alice, _zip(SAMPLE))).status_code == 200
    assert (await _ingest_zip(client, alice, _zip(SAMPLE), repository_id=second)).status_code == 200
    assert await _job_count(db_session, project_id=alice.project_id) == 2

    deleted = await client.delete(
        f"/api/v1/projects/{alice.project_id}/repositories/{alice.repository_id}",
        headers=alice.headers,
    )
    assert deleted.status_code == 204
    assert await _job_count(db_session, repository_id=alice.repository_id) == 0
    assert await _job_count(db_session, repository_id=second) == 1

    removed = await client.delete(f"/api/v1/projects/{alice.project_id}", headers=alice.headers)
    assert removed.status_code == 204
    assert await _job_count(db_session, project_id=alice.project_id) == 0


# --- Concurrency -------------------------------------------------------------------------


@requires_db
async def test_concurrent_ingestion_creates_no_second_job(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    await db_session.execute(
        update(Repository)
        .where(Repository.id == uuid.UUID(alice.repository_id))
        .values(status=RepositoryStatus.PROCESSING)
    )
    await db_session.commit()
    response = await _ingest_zip(client, alice, _zip(SAMPLE))
    assert response.status_code == 409
    assert response.json() == {"detail": "Repository ingestion is already in progress"}
    assert await _job_count(db_session, repository_id=alice.repository_id) == 0
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.PROCESSING


@requires_db
async def test_database_allows_one_active_ingestion_job_per_repository(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    repository = await db_session.get(Repository, uuid.UUID(alice.repository_id))
    assert repository is not None
    active = new_ingestion_job(repository)
    active.status = AnalysisJobStatus.PROCESSING
    db_session.add(active)
    await db_session.commit()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(new_ingestion_job(repository))  # a second active (pending) job
            await db_session.flush()

    finished = new_ingestion_job(repository)
    finished.status = AnalysisJobStatus.COMPLETED
    db_session.add(finished)
    await db_session.commit()  # finished jobs are not limited
    assert await _job_count(db_session, repository_id=alice.repository_id) == 2


@requires_db
async def test_active_job_conflict_is_reported_as_409(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    # Repository not marked processing, but an active job exists (e.g. inconsistent state):
    # the unique index still prevents a second active job and the API returns 409.
    repository = await db_session.get(Repository, uuid.UUID(alice.repository_id))
    assert repository is not None
    db_session.add(new_ingestion_job(repository))
    await db_session.commit()
    response = await _ingest_zip(client, alice, _zip(SAMPLE))
    assert response.status_code == 409
    assert await _job_count(db_session, repository_id=alice.repository_id) == 1
    assert await _repository_status(db_session, alice.repository_id) == RepositoryStatus.PENDING
