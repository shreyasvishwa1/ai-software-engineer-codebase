"""Code-metrics analysis tests (Step 23).

- Line counting is tested without database or Redis.
- API and worker tests run against the already-migrated throwaway test database
  (TEST_DATABASE_URL, "_test") inside rolled-back transactions; ingestion runs inline, and
  code-metrics jobs are either recorded (left pending) or run inline with the real worker
  logic. DATA_DIR is a temporary directory.
- Two lock tests commit their own rows through separate connections (needed to observe
  row locks between sessions) and delete them afterwards.
- The migration test uses the disposable migration database (TEST_MIGRATION_DATABASE_URL).

Races are made deterministic with hooks at the exact competing point, never with sleeps.
"""

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import AnalysisJob, AnalysisJobStatus, AnalysisJobType, File, FileMetric
from app.services import code_metrics, ingestion, job_cancellation
from app.services.analysis_jobs import claim_pending_job
from app.services.code_metrics import (
    CHANGED_MESSAGE,
    STALE_PENDING_MESSAGE,
    STALE_PROCESSING_MESSAGE,
    count_lines,
    run_code_metrics_job,
)
from app.services.file_content import MAX_CONTENT_BYTES
from app.services.maintenance import run_maintenance
from app.workers import queue as queue_module
from app.workers.worker import analyze_code_metrics
from tests.test_worker import _zip, install_inline_worker, install_recording_queue

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_MIGRATION_DATABASE_URL = os.environ.get("TEST_MIGRATION_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)
requires_migration_db = pytest.mark.skipif(
    not TEST_MIGRATION_DATABASE_URL,
    reason="set TEST_MIGRATION_DATABASE_URL to a dedicated, disposable test database",
)
BACKEND_DIR = Path(__file__).resolve().parent.parent

PASSWORD = "test-only-Passw0rd!"
BIG = os.urandom(MAX_CONTENT_BYTES + 1)  # incompressible, so the archive ratio check passes
FILES: dict[str, bytes] = {
    "src/app.py": b"import os\n\ndef main():\n    return 1\n",
    "crlf.txt": b"a\r\nb\r\n\r\nc",
    "cr.txt": b"x\ry\r\r",
    "blank.txt": b"  \n\t\n\n",
    "empty.txt": b"",
    "no_newline.txt": b"one\ntwo",
    "unicode.txt": "héllo\n世界\n\U0001f680".encode(),
    "bom.txt": b"\xef\xbb\xbfline\n",
    "zero-byte.dat": b"a\x00b",
    "latin1.txt": "café".encode("latin-1"),
    "big.bin": BIG,
}
EXPECTED: dict[str, tuple[str, int | None, int | None]] = {
    "src/app.py": ("analyzed", 4, 1),
    "crlf.txt": ("analyzed", 4, 1),
    "cr.txt": ("analyzed", 3, 1),
    "blank.txt": ("analyzed", 3, 3),
    "empty.txt": ("analyzed", 0, 0),
    "no_newline.txt": ("analyzed", 2, 0),
    "unicode.txt": ("analyzed", 3, 0),
    "bom.txt": ("analyzed", 1, 0),
    "zero-byte.dat": ("binary", None, None),
    "latin1.txt": ("binary", None, None),
    "big.bin": ("too_large", None, None),
}
NOT_FOUND = {"detail": "Repository not found"}


# --- Line counting (no database) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text_value", "expected"),
    [
        ("", (0, 0)),
        ("one", (1, 0)),
        ("one\n", (1, 0)),
        ("one\ntwo", (2, 0)),
        ("one\ntwo\n", (2, 0)),
        ("\n", (1, 1)),
        ("\n\n", (2, 2)),
        ("a\r\nb\r\n", (2, 0)),
        ("a\rb\r", (2, 0)),
        ("a\r\n\r\nb", (3, 1)),
        ("a\n\r\nb\rc", (4, 1)),
        ("  \n\t\n x \n", (3, 2)),
        ("héllo\n世界\n\U0001f680", (3, 0)),
        ("a b\x0cc", (1, 0)),  # only \n, \r\n and \r break lines
        ("　\n", (1, 1)),  # Unicode whitespace is blank
    ],
)
def test_count_lines(text_value: str, expected: tuple[int, int]) -> None:
    assert count_lines(text_value) == expected


def test_thresholds_are_code_metrics_specific() -> None:
    assert code_metrics.CODE_METRICS_PROCESSING_STALE_AFTER == timedelta(seconds=2100)
    assert code_metrics.CODE_METRICS_PENDING_STALE_AFTER == timedelta(hours=6)


async def test_worker_task_rejects_non_uuid_payload() -> None:
    assert await analyze_code_metrics({"session_factory": None}, "../../etc/passwd") == "invalid"


# --- Fixtures and helpers ------------------------------------------------------------------


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


async def _new_repository(client: httpx.AsyncClient, owner: Owner) -> str:
    response = await client.post(
        f"/api/v1/projects/{owner.project_id}/repositories",
        json={"name": f"repo-{uuid.uuid4().hex[:8]}", "source_type": "zip"},
        headers=owner.headers,
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _repo_url(owner: Owner, repository_id: str | None = None) -> str:
    return (
        f"/api/v1/projects/{owner.project_id}/repositories/{repository_id or owner.repository_id}"
    )


async def _ingest(
    client: httpx.AsyncClient,
    owner: Owner,
    files: dict[str, bytes],
    repository_id: str | None = None,
) -> httpx.Response:
    return await client.post(
        f"{_repo_url(owner, repository_id)}/ingest",
        files={"file": ("repo.zip", _zip(files), "application/zip")},
        headers=owner.headers,
    )


async def _make_owner(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, session: AsyncSession, email: str
) -> Owner:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project = (await client.post("/api/v1/projects", json={"name": "P"}, headers=headers)).json()
    owner = Owner(headers, project["id"], "")
    owner.repository_id = await _new_repository(client, owner)
    install_inline_worker(monkeypatch, session)
    assert (await _ingest(client, owner, FILES)).status_code == 202
    return owner


@pytest.fixture
async def alice(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> Owner:
    return await _make_owner(client, monkeypatch, db_session, "alice@example.com")


@pytest.fixture
async def bob(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> Owner:
    return await _make_owner(client, monkeypatch, db_session, "bob@example.com")


def record_code_metrics_queue(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    queued: list[str] = []

    async def enqueue_only(job_id: uuid.UUID) -> None:
        queued.append(str(job_id))

    monkeypatch.setattr(queue_module, "enqueue_code_metrics_job", enqueue_only)
    return queued


def inline_code_metrics_queue(monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> list[str]:
    outcomes: list[str] = []

    async def enqueue_inline(job_id: uuid.UUID) -> None:
        outcomes.append(await run_code_metrics_job(session, job_id))

    monkeypatch.setattr(queue_module, "enqueue_code_metrics_job", enqueue_inline)
    return outcomes


def _metrics_url(owner: Owner, repository_id: str | None = None) -> str:
    return f"{_repo_url(owner, repository_id)}/analysis/code-metrics"


async def _start(client: httpx.AsyncClient, owner: Owner, **kw: Any) -> httpx.Response:
    return await client.post(
        _metrics_url(owner, kw.get("repository_id")), headers=kw.get("headers", owner.headers)
    )


async def _results(client: httpx.AsyncClient, owner: Owner) -> dict[str, Any]:
    response = await client.get(_metrics_url(owner), headers=owner.headers)
    assert response.status_code == 200
    return dict(response.json())


async def _job(db: AsyncSession, job_id: str | uuid.UUID) -> AnalysisJob:
    db.expire_all()
    job = await db.get(AnalysisJob, uuid.UUID(str(job_id)))
    assert job is not None
    return job


async def _pending_job(
    client: httpx.AsyncClient, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> str:
    record_code_metrics_queue(monkeypatch)
    response = await _start(client, owner)
    assert response.status_code == 202, response.text
    return str(response.json()["job_id"])


async def _metric_rows(db: AsyncSession, repository_id: str) -> list[FileMetric]:
    db.expire_all()
    rows = await db.scalars(
        select(FileMetric).where(FileMetric.repository_id == uuid.UUID(repository_id))
    )
    return list(rows)


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


# --- Results and successful analysis -------------------------------------------------------


@requires_db
async def test_results_before_any_analysis(client: httpx.AsyncClient, alice: Owner) -> None:
    assert await _results(client, alice) == {"job": None, "summary": None, "files": []}


@requires_db
async def test_successful_analysis(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = record_code_metrics_queue(monkeypatch)
    started = await _start(client, alice)
    assert started.status_code == 202
    body = started.json()
    assert set(body) == {"job_id", "repository_id", "status"}
    assert (body["repository_id"], body["status"]) == (alice.repository_id, "pending")
    assert queued == [body["job_id"]]  # the queue payload is the job id only
    job = await _job(db_session, body["job_id"])
    assert (job.job_type, job.status) == (AnalysisJobType.CODE_METRICS, AnalysisJobStatus.PENDING)

    assert await run_code_metrics_job(db_session, uuid.UUID(body["job_id"])) == "completed"

    results = await _results(client, alice)
    assert results["job"]["status"] == "completed"
    assert results["job"]["job_type"] == "code_metrics"
    assert results["job"]["progress"] == 100
    by_path = {f["path"]: f for f in results["files"]}
    assert {
        p: (f["status"], f["line_count"], f["blank_line_count"]) for p, f in by_path.items()
    } == EXPECTED
    assert [f["path"] for f in results["files"]] == sorted(FILES)
    assert by_path["src/app.py"]["language"] == "Python"
    analyzed = [v for v in EXPECTED.values() if v[0] == "analyzed"]
    assert results["summary"] == {
        "files": len(FILES),
        "analyzed": len(analyzed),
        "binary": 2,
        "too_large": 1,
        "unavailable": 0,
        "total_lines": sum(v[1] or 0 for v in analyzed),
        "blank_lines": sum(v[2] or 0 for v in analyzed),
    }


@requires_db
async def test_results_expose_no_internal_ids_hashes_or_paths(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inline_code_metrics_queue(monkeypatch, db_session)
    assert (await _start(client, alice)).status_code == 202
    response = await client.get(_metrics_url(alice), headers=alice.headers)
    body = response.json()
    assert set(body) == {"job", "summary", "files"}
    assert all(
        set(f) == {"path", "language", "status", "line_count", "blank_line_count"}
        for f in body["files"]
    )
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text
        assert sha not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text


@requires_db
async def test_unavailable_files(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_id = await _pending_job(client, alice, monkeypatch)
    root = _storage(alice)
    (root / "crlf.txt").unlink()  # missing
    (root / "cr.txt").write_bytes(b"tampered\n")  # hash mismatch
    shutil.rmtree(root / "src")  # replaced by a junction to an identical outside copy
    outside = tmp_path / "outside-src"
    outside.mkdir()
    (outside / "app.py").write_bytes(FILES["src/app.py"])
    _junction(root / "src", outside)
    spied: list[str] = []
    real_read = code_metrics._read_verified

    def read(root_dir: Path, relative: str, expected: str | None) -> bytes:
        spied.append(relative)
        return real_read(root_dir, relative, expected)

    monkeypatch.setattr(code_metrics, "_read_verified", read)

    assert await run_code_metrics_job(db_session, uuid.UUID(job_id)) == "completed"

    by_path = {f["path"]: f for f in (await _results(client, alice))["files"]}
    for path in ("crlf.txt", "cr.txt", "src/app.py"):
        assert (by_path[path]["status"], by_path[path]["line_count"]) == ("unavailable", None)
    assert by_path["no_newline.txt"]["status"] == "analyzed"
    assert "big.bin" not in spied  # too-large files are never read
    assert set(spied) == set(FILES) - {"big.bin"}


@requires_db
async def test_symlinked_file_is_unavailable(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    job_id = await _pending_job(client, alice, monkeypatch)
    stored = _storage(alice) / "no_newline.txt"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(FILES["no_newline.txt"])  # identical bytes
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    assert await run_code_metrics_job(db_session, uuid.UUID(job_id)) == "completed"
    by_path = {f["path"]: f for f in (await _results(client, alice))["files"]}
    assert by_path["no_newline.txt"]["status"] == "unavailable"


# --- Starting: ownership, readiness, conflicts, queue failure -----------------------------


@requires_db
async def test_ownership_and_validation(
    client: httpx.AsyncClient, alice: Owner, bob: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_code_metrics_queue(monkeypatch)
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    urls = [
        _metrics_url(bob),  # with alice's token below
        f"/api/v1/projects/{other_project}/repositories/{alice.repository_id}/analysis/code-metrics",
        f"/api/v1/projects/{alice.project_id}/repositories/{uuid.uuid4()}/analysis/code-metrics",
    ]
    for url in urls:
        for method in ("POST", "GET"):
            response = await client.request(method, url, headers=alice.headers)
            assert (response.status_code, response.json()) == (404, NOT_FOUND)
    for method in ("POST", "GET"):
        assert (await client.request(method, _metrics_url(alice))).status_code == 401
        bad = f"/api/v1/projects/{alice.project_id}/repositories/not-a-uuid/analysis/code-metrics"
        assert (await client.request(method, bad, headers=alice.headers)).status_code == 422


@requires_db
async def test_repository_must_be_ready(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = record_code_metrics_queue(monkeypatch)
    never_ingested = await _new_repository(client, alice)
    response = await _start(client, alice, repository_id=never_ingested)
    assert (response.status_code, response.json()) == (
        409,
        {"detail": "Repository must be ingested before analysis"},
    )
    install_inline_worker(monkeypatch, db_session)
    failed_repo = await _new_repository(client, alice)
    await _ingest(client, alice, {"../evil.txt": b"x"}, repository_id=failed_repo)  # rejected
    response = await _start(client, alice, repository_id=failed_repo)
    assert response.json() == {"detail": "Repository must be ingested before analysis"}
    assert queued == []


@requires_db
async def test_ingestion_in_progress_conflicts(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = record_code_metrics_queue(monkeypatch)
    install_recording_queue(monkeypatch)  # the re-ingestion job stays pending
    assert (await _ingest(client, alice, FILES)).status_code == 202
    response = await _start(client, alice)
    assert (response.status_code, response.json()) == (
        409,
        {"detail": "Repository ingestion is in progress"},
    )
    # Repository marked processing (a worker running ingestion) is rejected too.
    other = await _new_repository(client, alice)
    await db_session.execute(
        text("UPDATE repositories SET status = 'processing' WHERE id = :id"), {"id": other}
    )
    await db_session.commit()
    response = await _start(client, alice, repository_id=other)
    assert response.json() == {"detail": "Repository ingestion is in progress"}
    assert queued == []


@requires_db
async def test_duplicate_code_metrics_job_conflicts(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _pending_job(client, alice, monkeypatch)
    second = await _start(client, alice)
    assert (second.status_code, second.json()) == (
        409,
        {"detail": "Code analysis is already in progress"},
    )
    assert (await _job(db_session, first)).status == AnalysisJobStatus.PENDING


@requires_db
async def test_queue_failure_fails_the_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(job_id: uuid.UUID) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(queue_module, "enqueue_code_metrics_job", broken)
    response = await _start(client, alice)
    assert (response.status_code, response.json()) == (
        503,
        {"detail": "Analysis could not be queued; try again later"},
    )
    job = (await _results(client, alice))["job"]
    assert (job["status"], job["error_message"]) == (
        "failed",
        "Analysis could not be queued; try again later",
    )
    record_code_metrics_queue(monkeypatch)
    assert (await _start(client, alice)).status_code == 202  # the active slot was freed


# --- Worker delivery -----------------------------------------------------------------------


@requires_db
async def test_duplicate_delivery_is_skipped(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid.UUID(await _pending_job(client, alice, monkeypatch))
    assert await run_code_metrics_job(db_session, job_id) == "completed"
    before = [
        (m.file_id, m.analysis_job_id) for m in await _metric_rows(db_session, alice.repository_id)
    ]
    assert await run_code_metrics_job(db_session, job_id) == "skipped"
    after = [
        (m.file_id, m.analysis_job_id) for m in await _metric_rows(db_session, alice.repository_id)
    ]
    assert sorted(before) == sorted(after)
    assert await run_code_metrics_job(db_session, uuid.uuid4()) == "missing"


@requires_db
async def test_wrong_job_type_is_never_touched(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_recording_queue(monkeypatch)
    pending_ingestion = (await _ingest(client, alice, FILES)).json()["job_id"]
    assert await run_code_metrics_job(db_session, uuid.UUID(pending_ingestion)) == "invalid"
    job = await _job(db_session, pending_ingestion)
    assert (job.job_type, job.status, job.error_message) == (
        AnalysisJobType.INGESTION,
        AnalysisJobStatus.PENDING,
        None,
    )


@requires_db
async def test_worker_task_runs_with_session_factory(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    job_id = await _pending_job(client, alice, monkeypatch)

    @contextlib.asynccontextmanager
    async def session_factory() -> AsyncIterator[AsyncSession]:
        yield db_session

    assert await analyze_code_metrics({"session_factory": session_factory}, job_id) == "completed"


# --- Snapshot consistency ------------------------------------------------------------------


@requires_db
async def test_reingestion_during_analysis_fails_it(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-ingestion commits a new snapshot after the files were measured and before the
    results are stored: the analysis fails and no metrics are attached to either snapshot."""
    job_id = uuid.UUID(await _pending_job(client, alice, monkeypatch))
    real_store = code_metrics._store_results
    install_inline_worker(monkeypatch, db_session)

    async def reingest_first(*args: Any) -> str:
        assert (await _ingest(client, alice, {"new.py": b"x = 1\n"})).status_code == 202
        return await real_store(*args)

    monkeypatch.setattr(code_metrics, "_store_results", reingest_first)
    assert await run_code_metrics_job(db_session, job_id) == "failed"

    job = await _job(db_session, job_id)
    assert (job.status, job.error_message) == (AnalysisJobStatus.FAILED, CHANGED_MESSAGE)
    assert await _metric_rows(db_session, alice.repository_id) == []
    results = await _results(client, alice)
    assert (results["summary"], results["files"]) == (None, [])
    # Ingestion was not blocked: the new snapshot is in place.
    assert (
        await db_session.scalars(
            select(File.path).where(File.repository_id == uuid.UUID(alice.repository_id))
        )
    ).all() == ["new.py"]


@requires_db
async def test_reingestion_clears_stale_metrics(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inline_code_metrics_queue(monkeypatch, db_session)
    assert (await _start(client, alice)).status_code == 202
    assert len(await _metric_rows(db_session, alice.repository_id)) == len(FILES)

    install_inline_worker(monkeypatch, db_session)
    assert (await _ingest(client, alice, FILES)).status_code == 202  # same content, new rows

    assert await _metric_rows(db_session, alice.repository_id) == []
    results = await _results(client, alice)
    assert results["job"]["status"] == "completed"  # the job is still listed
    assert (results["summary"], results["files"]) == (None, [])


@requires_db
async def test_replacement_analysis(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = inline_code_metrics_queue(monkeypatch, db_session)
    first = (await _start(client, alice)).json()["job_id"]
    (_storage(alice) / "src" / "app.py").write_bytes(b"changed\n")  # now unavailable
    second = (await _start(client, alice)).json()["job_id"]
    assert outcomes == ["completed", "completed"]
    rows = await _metric_rows(db_session, alice.repository_id)
    assert len(rows) == len(FILES)
    assert {r.analysis_job_id for r in rows} == {uuid.UUID(second)}
    assert first != second
    by_path = {f["path"]: f for f in (await _results(client, alice))["files"]}
    assert by_path["src/app.py"]["status"] == "unavailable"


@dataclass
class Committed:
    """Rows committed through a separate connection (visible to other sessions)."""

    url: str
    user_id: uuid.UUID
    repository_id: uuid.UUID
    file_ids: set[uuid.UUID]


@pytest.fixture
async def committed_repository() -> AsyncIterator[Committed]:
    assert TEST_DATABASE_URL
    url = make_url(TEST_DATABASE_URL)
    if not (url.database or "").endswith("_test"):
        pytest.fail("refusing to use TEST_DATABASE_URL: database name must end with '_test'")
    raw = url.set(drivername="postgresql").render_as_string(hide_password=False)
    conn = await asyncpg.connect(raw)
    user_id = uuid.uuid4()
    try:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x')",
            user_id,
            f"lock-{user_id.hex}@example.com",
        )
        project_id = await conn.fetchval(
            "INSERT INTO projects (id, user_id, name) VALUES (gen_random_uuid(), $1, 'P') RETURNING id",
            user_id,
        )
        repository_id = await conn.fetchval(
            "INSERT INTO repositories (id, project_id, name, source_type, status) "
            "VALUES (gen_random_uuid(), $1, 'r', 'zip', 'ready') RETURNING id",
            project_id,
        )
        file_ids = set()
        for name in ("a.py", "b.py"):
            file_ids.add(
                await conn.fetchval(
                    "INSERT INTO files (id, repository_id, path, name, size_bytes) "
                    "VALUES (gen_random_uuid(), $1, $2, $3, 1) RETURNING id",
                    repository_id,
                    name,
                    name,
                )
            )
        yield Committed(raw, user_id, repository_id, file_ids)
    finally:
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)  # cascades
        await conn.close()


async def _separate_session() -> tuple[Any, AsyncSession]:
    """A session on its own connection (outside the rolled-back test transaction)."""
    engine = create_async_engine(TEST_DATABASE_URL or "", poolclass=NullPool)
    return engine, AsyncSession(engine, expire_on_commit=False)


@requires_db
async def test_snapshot_lock_never_waits_for_a_reingestion(
    committed_repository: Committed,
) -> None:
    """A re-ingestion transaction that has deleted the files rows (not yet committed): the
    analysis' NOWAIT lock fails at once instead of waiting, so it cannot deadlock or delay
    the ingestion."""
    other = await asyncpg.connect(committed_repository.url)
    engine, session = await _separate_session()
    try:
        transaction = other.transaction()
        await transaction.start()
        await other.execute(
            "DELETE FROM files WHERE repository_id = $1", committed_repository.repository_id
        )
        assert (
            await code_metrics._lock_current_snapshot(session, committed_repository.repository_id)
            is None
        )
        await session.rollback()
        await transaction.rollback()  # the re-ingestion is abandoned
        locked = await code_metrics._lock_current_snapshot(
            session, committed_repository.repository_id
        )
        assert locked == committed_repository.file_ids
        await session.rollback()
    finally:
        await session.close()
        await engine.dispose()
        await other.close()


@requires_db
async def test_snapshot_lock_holds_off_reingestion_until_stored(
    committed_repository: Committed,
) -> None:
    """While the analysis holds its snapshot locks, a re-ingestion cannot delete the files
    rows (it waits; here a short lock_timeout shows that), so the stored results always
    belong to the snapshot that was checked."""
    other = await asyncpg.connect(committed_repository.url)
    engine, session = await _separate_session()
    try:
        locked = await code_metrics._lock_current_snapshot(
            session, committed_repository.repository_id
        )
        assert locked == committed_repository.file_ids
        transaction = other.transaction()
        await transaction.start()
        await other.execute("SET LOCAL lock_timeout = '200ms'")
        with pytest.raises(asyncpg.LockNotAvailableError):
            await other.execute(
                "DELETE FROM files WHERE repository_id = $1", committed_repository.repository_id
            )
        await transaction.rollback()
        await session.rollback()  # analysis transaction ends; deletion is possible again
        async with other.transaction():
            deleted = await other.execute(
                "DELETE FROM files WHERE repository_id = $1", committed_repository.repository_id
            )
        assert deleted == "DELETE 2"
    finally:
        await session.close()
        await engine.dispose()
        await other.close()


# --- Stale jobs and recovery races ---------------------------------------------------------


async def _age(db: AsyncSession, job_id: str, **values: Any) -> None:
    await db.execute(
        update(AnalysisJob).where(AnalysisJob.id == uuid.UUID(job_id)).values(**values)
    )
    await db.commit()


@requires_db
async def test_stale_pending_job_is_recovered_on_start(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await _pending_job(client, alice, monkeypatch)
    await _age(db_session, old, created_at=func.now() - timedelta(hours=6, seconds=1))
    new = await _start(client, alice)
    assert new.status_code == 202
    job = await _job(db_session, old)
    assert (job.status, job.error_message) == (AnalysisJobStatus.FAILED, STALE_PENDING_MESSAGE)
    assert job.completed_at is not None


@requires_db
async def test_stale_processing_job_is_recovered_on_start(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = await _pending_job(client, alice, monkeypatch)
    assert await claim_pending_job(db_session, uuid.UUID(old))
    await _age(db_session, old, started_at=func.now() - timedelta(seconds=2101))
    assert (await _start(client, alice)).status_code == 202
    job = await _job(db_session, old)
    assert (job.status, job.error_message) == (
        AnalysisJobStatus.FAILED,
        STALE_PROCESSING_MESSAGE,
    )


@requires_db
async def test_recent_active_jobs_are_not_recovered(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = await _pending_job(client, alice, monkeypatch)
    await _age(db_session, pending, created_at=func.now() - timedelta(hours=5))
    assert (await _start(client, alice)).status_code == 409
    assert (await _job(db_session, pending)).status == AnalysisJobStatus.PENDING
    assert await claim_pending_job(db_session, uuid.UUID(pending))
    await _age(db_session, pending, started_at=func.now() - timedelta(seconds=2000))
    assert (await _start(client, alice)).status_code == 409
    assert (await _job(db_session, pending)).status == AnalysisJobStatus.PROCESSING


@requires_db
async def test_recovery_racing_a_worker_claim_never_overwrites_it(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale pending job is claimed by a worker right before the recovery UPDATE: the
    conditional recovery no longer matches it, and the new start is rejected (409)."""
    old = await _pending_job(client, alice, monkeypatch)
    await _age(db_session, old, created_at=func.now() - timedelta(hours=7))
    real_recover = code_metrics.recover_stale_code_metrics_jobs

    async def claim_first(db: AsyncSession, repository_id: uuid.UUID) -> int:
        assert await claim_pending_job(db, uuid.UUID(old))
        await db.commit()
        return await real_recover(db, repository_id)

    monkeypatch.setattr(code_metrics, "recover_stale_code_metrics_jobs", claim_first)
    response = await _start(client, alice)
    assert (response.status_code, response.json()["detail"]) == (
        409,
        "Code analysis is already in progress",
    )
    job = await _job(db_session, old)
    assert (job.status, job.error_message) == (AnalysisJobStatus.PROCESSING, None)


@requires_db
async def test_worker_finishing_a_recovered_job_stores_nothing(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job is recovered as stale while its worker is still measuring: the worker's
    conditional completion no longer matches, so no metrics are stored."""
    job_id = await _pending_job(client, alice, monkeypatch)
    real_store = code_metrics._store_results

    async def recover_first(*args: Any) -> str:
        await _age(db_session, job_id, started_at=func.now() - timedelta(seconds=2101))
        assert (
            await code_metrics.recover_stale_code_metrics_jobs(
                db_session, uuid.UUID(alice.repository_id)
            )
            == 1
        )
        return await real_store(*args)

    monkeypatch.setattr(code_metrics, "_store_results", recover_first)
    assert await run_code_metrics_job(db_session, uuid.UUID(job_id)) == "skipped"
    job = await _job(db_session, job_id)
    assert (job.status, job.error_message) == (AnalysisJobStatus.FAILED, STALE_PROCESSING_MESSAGE)
    assert await _metric_rows(db_session, alice.repository_id) == []


@requires_db
async def test_maintenance_leaves_code_metrics_jobs_alone(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = await _pending_job(client, alice, monkeypatch)
    await _age(db_session, job_id, created_at=func.now() - timedelta(days=2))
    report = await run_maintenance(db_session)
    assert (report.processing_jobs_failed, report.pending_jobs_failed) == (0, 0)
    assert (await _job(db_session, job_id)).status == AnalysisJobStatus.PENDING


# --- Cancellation --------------------------------------------------------------------------


def _cancel_url(owner: Owner, job_id: str) -> str:
    return f"{_repo_url(owner)}/jobs/{job_id}/cancel"


@requires_db
async def test_cancel_messages_depend_on_job_type(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = await _pending_job(client, alice, monkeypatch)
    cancelled = await client.post(_cancel_url(alice, analysis), headers=alice.headers)
    assert cancelled.status_code == 200
    assert (cancelled.json()["status"], cancelled.json()["error_message"]) == (
        "failed",
        "Analysis was cancelled",
    )
    assert cancelled.json()["job_type"] == "code_metrics"

    install_recording_queue(monkeypatch)
    ingest_job = (await _ingest(client, alice, FILES)).json()["job_id"]
    response = await client.post(_cancel_url(alice, ingest_job), headers=alice.headers)
    assert (response.status_code, response.json()["error_message"]) == (
        200,
        "Ingestion was cancelled",
    )


@requires_db
async def test_cancel_rejects_non_pending_code_metrics_jobs(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing = await _pending_job(client, alice, monkeypatch)
    assert await claim_pending_job(db_session, uuid.UUID(processing))
    await db_session.commit()
    response = await client.post(_cancel_url(alice, processing), headers=alice.headers)
    assert (response.status_code, response.json()) == (
        409,
        {"detail": "Only pending jobs can be cancelled"},
    )
    await _age(db_session, processing, status=AnalysisJobStatus.FAILED)  # finished (failed)
    completed = await _pending_job(client, alice, monkeypatch)
    assert await run_code_metrics_job(db_session, uuid.UUID(completed)) == "completed"
    for job_id, expected in (
        (processing, AnalysisJobStatus.FAILED),
        (completed, AnalysisJobStatus.COMPLETED),
    ):
        before = await _job(db_session, job_id)
        state = (before.status, before.error_message, before.completed_at)
        response = await client.post(_cancel_url(alice, job_id), headers=alice.headers)
        assert response.status_code == 409
        after = await _job(db_session, job_id)
        assert (after.status, after.error_message, after.completed_at) == state
        assert after.status == expected


@requires_db
async def test_cancel_racing_worker_claim(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claim wins: the worker claims between cancel's ownership check and its UPDATE.
    job_id = await _pending_job(client, alice, monkeypatch)
    real_fail = job_cancellation.fail_pending_job

    async def claim_first(db: AsyncSession, target: uuid.UUID, message: str) -> bool:
        assert await claim_pending_job(db, target)
        await db.commit()
        return await real_fail(db, target, message)

    monkeypatch.setattr(job_cancellation, "fail_pending_job", claim_first)
    response = await client.post(_cancel_url(alice, job_id), headers=alice.headers)
    assert response.status_code == 409
    assert (await _job(db_session, job_id)).status == AnalysisJobStatus.PROCESSING
    monkeypatch.setattr(job_cancellation, "fail_pending_job", real_fail)
    await _age(db_session, job_id, status=AnalysisJobStatus.FAILED)

    # Cancel wins: the later worker delivery is skipped and leaves the cancellation.
    other = await _pending_job(client, alice, monkeypatch)
    assert (await client.post(_cancel_url(alice, other), headers=alice.headers)).status_code == 200
    assert await run_code_metrics_job(db_session, uuid.UUID(other)) == "skipped"
    job = await _job(db_session, other)
    assert (job.status, job.error_message) == (AnalysisJobStatus.FAILED, "Analysis was cancelled")
    assert await _metric_rows(db_session, alice.repository_id) == []


# --- Migration -----------------------------------------------------------------------------


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
async def test_migration_0003_upgrade_downgrade_upgrade() -> None:
    assert TEST_MIGRATION_DATABASE_URL
    url = make_url(TEST_MIGRATION_DATABASE_URL)
    if not (url.database or "").endswith("_test"):
        pytest.fail("refusing: TEST_MIGRATION_DATABASE_URL database name must end with '_test'")
    raw = url.set(drivername="postgresql").render_as_string(hide_password=False)

    _alembic(TEST_MIGRATION_DATABASE_URL, "downgrade", "base")  # disposable database only
    _alembic(TEST_MIGRATION_DATABASE_URL, "upgrade", "head")
    assert _alembic(TEST_MIGRATION_DATABASE_URL, "current").split() == ["0003", "(head)"]

    conn = await asyncpg.connect(raw)
    try:
        user_id = await conn.fetchval(
            "INSERT INTO users (id, email, password_hash) "
            "VALUES (gen_random_uuid(), 'm@example.com', 'x') RETURNING id"
        )
        project_id = await conn.fetchval(
            "INSERT INTO projects (id, user_id, name) VALUES (gen_random_uuid(), $1, 'P') RETURNING id",
            user_id,
        )
        repository_id = await conn.fetchval(
            "INSERT INTO repositories (id, project_id, name, source_type) "
            "VALUES (gen_random_uuid(), $1, 'r', 'zip') RETURNING id",
            project_id,
        )
        file_id = await conn.fetchval(
            "INSERT INTO files (id, repository_id, path, name, size_bytes) "
            "VALUES (gen_random_uuid(), $1, 'a.py', 'a.py', 1) RETURNING id",
            repository_id,
        )
        insert_job = (
            "INSERT INTO analysis_jobs (id, project_id, repository_id, job_type, status) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4) RETURNING id"
        )
        ingestion_job = await conn.fetchval(
            insert_job, project_id, repository_id, "ingestion", "processing"
        )
        metrics_job = await conn.fetchval(
            insert_job, project_id, repository_id, "code_metrics", "pending"
        )
        with pytest.raises(asyncpg.UniqueViolationError):  # one active code-metrics job
            await conn.fetchval(insert_job, project_id, repository_id, "code_metrics", "processing")
        with pytest.raises(asyncpg.UniqueViolationError):  # ingestion index unchanged
            await conn.fetchval(insert_job, project_id, repository_id, "ingestion", "pending")
        await conn.fetchval(insert_job, project_id, repository_id, "code_metrics", "completed")

        insert_metric = (
            "INSERT INTO file_metrics (file_id, repository_id, analysis_job_id, status, "
            "line_count, blank_line_count) VALUES ($1, $2, $3, $4, $5, $6)"
        )
        for bad in (
            ("analyzed", None, None),
            ("binary", 3, 0),
            ("analyzed", 2, 3),
            ("analyzed", -1, 0),
            ("unknown", None, None),
        ):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(insert_metric, file_id, repository_id, metrics_job, *bad)
        await conn.execute(insert_metric, file_id, repository_id, metrics_job, "analyzed", 3, 1)
        await conn.execute("DELETE FROM analysis_jobs WHERE id = $1", metrics_job)
        assert await conn.fetchval("SELECT analysis_job_id FROM file_metrics") is None  # SET NULL
        await conn.execute("DELETE FROM files WHERE id = $1", file_id)
        assert await conn.fetchval("SELECT count(*) FROM file_metrics") == 0  # CASCADE
    finally:
        await conn.close()

    _alembic(TEST_MIGRATION_DATABASE_URL, "downgrade", "0002")
    conn = await asyncpg.connect(raw)
    try:
        labels = await conn.fetch(
            "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'analysis_job_type' ORDER BY e.enumsortorder"
        )
        assert [r["enumlabel"] for r in labels] == ["ingestion"]
        assert await conn.fetchval("SELECT to_regclass('file_metrics')") is None
        jobs = await conn.fetch("SELECT id, job_type FROM analysis_jobs")
        assert [(r["id"], r["job_type"]) for r in jobs] == [(ingestion_job, "ingestion")]
        indexes = {
            r["indexname"]: r["indexdef"]
            for r in await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'analysis_jobs'"
            )
        }
        assert "uq_analysis_jobs_active_code_metrics" not in indexes
        assert "UNIQUE" in indexes["uq_analysis_jobs_active_ingestion"]
        assert "WHERE" in indexes["uq_analysis_jobs_active_ingestion"]
    finally:
        await conn.close()

    _alembic(TEST_MIGRATION_DATABASE_URL, "upgrade", "head")
    assert _alembic(TEST_MIGRATION_DATABASE_URL, "current").split() == ["0003", "(head)"]
    _alembic(TEST_MIGRATION_DATABASE_URL, "check")  # models and migrations agree
