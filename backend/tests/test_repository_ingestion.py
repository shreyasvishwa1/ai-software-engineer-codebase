"""Repository ZIP ingestion tests.

Archive-safety and configuration tests need no database. API tests run against a dedicated,
already-migrated throwaway test database (TEST_DATABASE_URL, name ending in "_test"); each
runs inside a rolled-back transaction, and DATA_DIR points at a per-test temporary directory.
All ZIP fixtures are built in memory; nothing is written into the source tree.
"""

import hashlib
import io
import os
import stat
import sys
import uuid
import warnings
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import REPO_ROOT, Settings, get_settings
from app.database import get_db
from app.main import app
from app.models import AnalysisJob, AnalysisJobStatus, File, Repository, RepositoryStatus
from app.services import ingestion as ingestion_service
from app.services.archive import ArchiveError, ArchiveLimits, extract_zip, normalize_member_path
from tests.test_worker import install_inline_worker

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
LIMITS = ArchiveLimits(
    max_entries=100, max_total_bytes=10_000_000, max_file_bytes=1_000_000, max_compression_ratio=100
)

UNSAFE_NAMES = [
    "../../evil.txt",
    "../../../etc/passwd",
    "src/../../evil.txt",
    "src/../evil.txt",
    "/tmp/evil.txt",
    "/absolute/path.txt",
    "C:/evil.txt",
    "C:\\evil.txt",
    "c:evil.txt",
    "\\\\server\\share\\evil.txt",
    "//server/share/evil.txt",
    "..\\..\\evil.txt",
    "src/file.txt:stream",
    "src/con.txt",
    "NUL",
    "bad\x01name.txt",
    "trailing-dot.",
    "",
]


# --- ZIP builders ------------------------------------------------------------------------


def _zip(entries: dict[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _zip_raw_names(names: list[str], data: bytes = b"x") -> bytes:
    """Write entries with exact names (bypassing ZipInfo's separator conversion)."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names are intentional in some tests
        with zipfile.ZipFile(buffer, "w") as zf:
            for name in names:
                info = zipfile.ZipInfo("placeholder")
                info.filename = name
                zf.writestr(info, data)
    return buffer.getvalue()


def _zip_symlink() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("src/ok.py", b"print('ok')\n")
        link = zipfile.ZipInfo("src/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "/etc/passwd")
    return buffer.getvalue()


def _zip_special(entry_type: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo("device")
        info.create_system = 3
        info.external_attr = (entry_type | 0o644) << 16
        zf.writestr(info, b"")
    return buffer.getvalue()


def _zip_encrypted_flag() -> bytes:
    """Set the "encrypted" bit in the local and central headers (zipfile's writer clears it)."""
    data = bytearray(_zip({"secret.txt": b"data"}, zipfile.ZIP_STORED))
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        index = data.find(signature)
        data[index + flag_offset] |= 0x1
    return bytes(data)


def _bomb() -> bytes:
    return _zip({"zeros.bin": b"\0" * 5_000_000})  # ratio ~1000:1


def _files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _extract(data: bytes, dest: Path, limits: ArchiveLimits = LIMITS) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    return [f.path for f in extract_zip(io.BytesIO(data), dest, limits)]


# --- Path normalization (no database) ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/main.py", "src/main.py"),
        ("src/utils/helper.py", "src/utils/helper.py"),
        ("./src/a.py", "src/a.py"),
        ("a//b.txt", "a/b.txt"),
        ("dir/", "dir"),
        ("src\\win\\style.py", "src/win/style.py"),
        (".gitignore", ".gitignore"),
    ],
)
def test_safe_paths_are_normalized(raw: str, expected: str) -> None:
    assert normalize_member_path(raw) == expected


@pytest.mark.parametrize("raw", UNSAFE_NAMES)
def test_unsafe_paths_are_rejected(raw: str) -> None:
    with pytest.raises(ArchiveError) as excinfo:
        normalize_member_path(raw)
    assert excinfo.value.status_code == 400


# --- Extraction security (no database) ---------------------------------------------------


def test_extracts_nested_files_with_hashes(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    content = {"src/main.py": b"print('hi')\n", "src/utils/helper.py": b"x = 1\n", "README": b"r"}
    files = extract_zip(io.BytesIO(_zip(content)), dest, LIMITS)
    by_path = {f.path: f for f in files}
    assert set(by_path) == set(content)
    for path, data in content.items():
        assert (dest / path).read_bytes() == data
        assert by_path[path].size_bytes == len(data)
        assert by_path[path].sha256 == hashlib.sha256(data).hexdigest()
    assert by_path["src/main.py"].extension == "py"
    assert by_path["README"].extension is None


@pytest.mark.parametrize("name", UNSAFE_NAMES[:-1])  # "" cannot be written as an entry name
def test_unsafe_entries_rejected_and_nothing_written(tmp_path: Path, name: str) -> None:
    dest = tmp_path / "dest"
    with pytest.raises(ArchiveError) as excinfo:
        _extract(_zip_raw_names(["src/ok.py", name]), dest)
    assert excinfo.value.status_code == 400
    assert _files_under(tmp_path) == set()  # validation happens before any write


@pytest.mark.parametrize(
    ("builder", "detail"),
    [
        (_zip_symlink, "Archive contains a symbolic link"),
        (lambda: _zip_special(stat.S_IFCHR), "Archive contains an unsupported entry type"),
        (lambda: _zip_special(stat.S_IFIFO), "Archive contains an unsupported entry type"),
        (_zip_encrypted_flag, "Encrypted archives are not supported"),
    ],
)
def test_special_entries_rejected(tmp_path: Path, builder: object, detail: str) -> None:
    with pytest.raises(ArchiveError) as excinfo:
        _extract(builder(), tmp_path / "dest")  # type: ignore[operator]
    assert (excinfo.value.status_code, excinfo.value.detail) == (400, detail)
    assert _files_under(tmp_path) == set()


@pytest.mark.parametrize(
    "names",
    [
        ["src/a.py", "src/a.py"],  # exact duplicate
        ["README.md", "readme.md"],  # collides on case-insensitive filesystems
        ["a", "a/b.txt"],  # file used as a directory
        ["a/b.txt", "a"],
        ["a/", "a"],
        ["src/./a.py", "src/a.py"],  # duplicate after normalization
    ],
)
def test_duplicate_or_conflicting_paths_rejected(tmp_path: Path, names: list[str]) -> None:
    with pytest.raises(ArchiveError) as excinfo:
        _extract(_zip_raw_names(names), tmp_path / "dest")
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "Archive contains duplicate or conflicting paths"


def test_too_many_entries_rejected(tmp_path: Path) -> None:
    limits = ArchiveLimits(3, 10_000_000, 1_000_000, 100)
    with pytest.raises(ArchiveError) as excinfo:
        _extract(_zip({f"f{i}.txt": b"x" for i in range(4)}), tmp_path / "dest", limits)
    assert (excinfo.value.status_code, excinfo.value.detail) == (
        413,
        "Archive contains too many entries",
    )


def test_total_uncompressed_size_limit(tmp_path: Path) -> None:
    limits = ArchiveLimits(100, 1500, 1000, 100)
    data = _zip({"a.bin": os.urandom(800), "b.bin": os.urandom(800)}, zipfile.ZIP_STORED)
    with pytest.raises(ArchiveError) as excinfo:
        _extract(data, tmp_path / "dest", limits)
    assert (excinfo.value.status_code, excinfo.value.detail) == (
        413,
        "Archive exceeds the extracted size limit",
    )
    assert _files_under(tmp_path) == set()


def test_single_file_size_limit(tmp_path: Path) -> None:
    limits = ArchiveLimits(100, 10_000, 1000, 100)
    data = _zip({"big.bin": os.urandom(1001)}, zipfile.ZIP_STORED)
    with pytest.raises(ArchiveError) as excinfo:
        _extract(data, tmp_path / "dest", limits)
    assert (excinfo.value.status_code, excinfo.value.detail) == (
        413,
        "Archive contains a file that exceeds the size limit",
    )


def test_compression_ratio_limit_blocks_zip_bomb(tmp_path: Path) -> None:
    # Size limits large enough that only the compression ratio can stop this archive.
    limits = ArchiveLimits(100, 100_000_000, 100_000_000, 100)
    with pytest.raises(ArchiveError) as excinfo:
        _extract(_bomb(), tmp_path / "dest", limits)
    assert (excinfo.value.status_code, excinfo.value.detail) == (
        413,
        "Archive entry exceeds the compression ratio limit",
    )
    assert _files_under(tmp_path) == set()


@pytest.mark.parametrize("data", [b"", b"not a zip", b"PK\x03\x04garbage", _zip({"a": b"b"})[:-30]])
def test_malformed_archives_rejected(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(ArchiveError) as excinfo:
        _extract(data, tmp_path / "dest")
    assert (excinfo.value.status_code, excinfo.value.detail) == (400, "Invalid ZIP archive")


def test_empty_archive_extracts_nothing(tmp_path: Path) -> None:
    assert _extract(_zip({}), tmp_path / "dest") == []


def test_directory_entries_are_not_registered(tmp_path: Path) -> None:
    assert _extract(_zip({"src/": b"", "src/a.py": b"a"}), tmp_path / "dest") == ["src/a.py"]


# --- Configuration (no database) ---------------------------------------------------------


def test_ingestion_settings_defaults() -> None:
    settings = Settings(_env_file=None, secret_key="x" * 40)  # type: ignore[call-arg]
    assert settings.max_archive_file_count == 10000
    assert settings.max_extracted_size_mb == 500
    assert settings.max_extracted_file_size_mb == 25
    assert settings.max_compression_ratio == 100
    assert settings.data_dir == (REPO_ROOT / "data").resolve()


def test_relative_data_dir_resolves_against_repo_root() -> None:
    settings = Settings(_env_file=None, secret_key="x" * 40, data_dir="storage/x")  # type: ignore[call-arg, arg-type]
    assert settings.data_dir == (REPO_ROOT / "storage" / "x").resolve()


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_archive_file_count": 0},
        {"max_extracted_size_mb": 0},
        {"max_extracted_file_size_mb": -1},
        {"max_compression_ratio": 0},
        {"max_extracted_file_size_mb": 600, "max_extracted_size_mb": 500},
    ],
)
def test_invalid_ingestion_settings_rejected(overrides: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, secret_key="x" * 40, **overrides)  # type: ignore[arg-type]


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
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "data"
    monkeypatch.setattr(get_settings(), "data_dir", path)
    yield path


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


@dataclass
class Owner:
    headers: dict[str, str]
    project_id: str
    repository_id: str


async def _owner(client: httpx.AsyncClient, email: str, source_type: str = "zip") -> Owner:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project = (await client.post("/api/v1/projects", json={"name": "P"}, headers=headers)).json()
    repository = await client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"name": "repo", "source_type": source_type},
        headers=headers,
    )
    return Owner(headers, project["id"], repository.json()["id"])


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com")


@pytest.fixture
async def bob(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "bob@example.com")


def _ingest_url(project_id: str, repository_id: str) -> str:
    return f"/api/v1/projects/{project_id}/repositories/{repository_id}/ingest"


async def _ingest(
    client: httpx.AsyncClient,
    owner: Owner,
    data: bytes,
    project_id: str | None = None,
    repository_id: str | None = None,
    **kwargs: object,
) -> httpx.Response:
    url = _ingest_url(project_id or owner.project_id, repository_id or owner.repository_id)
    return await client.post(
        url,
        files={"file": ("repo.zip", data, "application/zip")},
        headers=owner.headers,
        **kwargs,  # type: ignore[arg-type]
    )


async def _status(db: AsyncSession, repository_id: str) -> RepositoryStatus:
    value = await db.scalar(
        select(Repository.status).where(Repository.id == uuid.UUID(repository_id))
    )
    assert value is not None
    return value


async def _file_rows(db: AsyncSession, repository_id: str) -> dict[str, File]:
    rows = await db.scalars(select(File).where(File.repository_id == uuid.UUID(repository_id)))
    return {f.path: f for f in rows}


def _storage(data_dir: Path, repository_id: str) -> Path:
    return data_dir / "repositories" / repository_id


def _leftovers(data_dir: Path) -> list[str]:
    """Staging/old directories and queued uploads that should not remain."""
    root = data_dir / "repositories"
    uploads = data_dir / "ingestion-uploads"
    found = [p.name for p in root.iterdir() if p.name.startswith(".")] if root.exists() else []
    found += [p.name for p in uploads.iterdir()] if uploads.exists() else []
    return sorted(found)


async def _job_error(db: AsyncSession, repository_id: str) -> str | None:
    """The latest ingestion job's error (asserting it failed)."""
    db.expire_all()
    job = await db.scalar(
        select(AnalysisJob)
        .where(AnalysisJob.repository_id == uuid.UUID(repository_id))
        .order_by(AnalysisJob.created_at.desc(), AnalysisJob.completed_at.desc())
        .limit(1)
    )
    assert job is not None
    assert job.status == AnalysisJobStatus.FAILED
    return job.error_message


def _assert_accepted(response: httpx.Response, repository_id: str) -> None:
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id", "repository_id", "status"}
    assert (body["repository_id"], body["status"]) == (repository_id, "pending")


SAMPLE = {
    "src/main.py": b"def main():\n    return 1\n",
    "src/utils/helper.py": b"def helper():\n    return 2\n",
    "README.md": b"# Sample\n",
}


# --- API: authentication and ownership ---------------------------------------------------


@requires_db
async def test_unauthenticated_ingestion_fails(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await client.post(
        _ingest_url(alice.project_id, alice.repository_id),
        files={"file": ("repo.zip", _zip(SAMPLE), "application/zip")},
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@requires_db
async def test_cross_user_ingestion_is_not_found(
    client: httpx.AsyncClient, db_session: AsyncSession, data_dir: Path, alice: Owner, bob: Owner
) -> None:
    for project_id in (bob.project_id, alice.project_id):
        response = await _ingest(
            client, alice, _zip(SAMPLE), project_id=project_id, repository_id=bob.repository_id
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Repository not found"}
    assert await _status(db_session, bob.repository_id) == RepositoryStatus.PENDING
    assert await _file_rows(db_session, bob.repository_id) == {}
    assert not _storage(data_dir, bob.repository_id).exists()


@requires_db
async def test_wrong_project_ingestion_is_not_found(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    other = await client.post("/api/v1/projects", json={"name": "Other"}, headers=alice.headers)
    response = await _ingest(client, alice, _zip(SAMPLE), project_id=other.json()["id"])
    assert response.status_code == 404
    assert response.json() == {"detail": "Repository not found"}
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.PENDING


@requires_db
@pytest.mark.parametrize("missing", ["repository", "project"])
async def test_nonexistent_resources_are_not_found(
    client: httpx.AsyncClient, alice: Owner, missing: str
) -> None:
    ids = {"repository_id": str(uuid.uuid4())} if missing == "repository" else {
        "project_id": str(uuid.uuid4())
    }
    response = await _ingest(client, alice, _zip(SAMPLE), **ids)  # type: ignore[arg-type]
    assert response.status_code == 404
    assert response.json() == {"detail": "Repository not found"}


@requires_db
@pytest.mark.parametrize("bad", ["not-a-uuid", "123"])
async def test_invalid_uuid(client: httpx.AsyncClient, alice: Owner, bad: str) -> None:
    for ids in ({"project_id": bad}, {"repository_id": bad}):
        response = await _ingest(client, alice, _zip(SAMPLE), **ids)  # type: ignore[arg-type]
        assert response.status_code == 422


@requires_db
async def test_github_repository_is_rejected_without_network(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    github = await client.post(
        f"/api/v1/projects/{alice.project_id}/repositories",
        json={"name": "gh", "source_type": "github", "source_url": "https://evil.example/a/b"},
        headers=alice.headers,
    )
    repository_id = github.json()["id"]
    # A GitHub repository never falls back to the uploaded ZIP; its URL is validated first.
    response = await _ingest(client, alice, _zip(SAMPLE), repository_id=repository_id)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid GitHub repository URL"}
    assert await _status(db_session, repository_id) == RepositoryStatus.PENDING


@requires_db
async def test_client_cannot_supply_ownership_fields(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, bob: Owner
) -> None:
    response = await client.post(
        _ingest_url(alice.project_id, alice.repository_id),
        files={"file": ("repo.zip", _zip(SAMPLE), "application/zip")},
        data={"project_id": bob.project_id, "repository_id": bob.repository_id},
        headers=alice.headers,
    )
    assert response.status_code == 400  # extra form fields are not accepted
    assert await _file_rows(db_session, alice.repository_id) == {}
    assert await _file_rows(db_session, bob.repository_id) == {}
    assert await _status(db_session, bob.repository_id) == RepositoryStatus.PENDING


# --- API: successful ingestion -----------------------------------------------------------


@requires_db
async def test_valid_zip_ingestion_registers_files(
    client: httpx.AsyncClient, db_session: AsyncSession, data_dir: Path, alice: Owner
) -> None:
    response = await _ingest(client, alice, _zip({**SAMPLE, "src/": b""}))
    _assert_accepted(response, alice.repository_id)

    rows = await _file_rows(db_session, alice.repository_id)
    assert set(rows) == set(SAMPLE)  # directories are not registered
    for path, data in SAMPLE.items():
        row = rows[path]
        assert not path.startswith("/") and "\\" not in path and ".." not in path
        assert row.name == path.rsplit("/", 1)[-1]
        assert row.size_bytes == len(data)
        assert row.sha256 == hashlib.sha256(data).hexdigest()
        assert row.language is None  # language detection is out of scope
    assert rows["src/main.py"].extension == "py"
    assert rows["README.md"].extension == "md"
    assert str(data_dir) not in "".join(rows)  # no absolute machine paths stored

    storage = _storage(data_dir, alice.repository_id)
    assert _files_under(storage) == set(SAMPLE)
    for path, data in SAMPLE.items():
        assert (storage / path).read_bytes() == data
    assert _leftovers(data_dir) == []

    repository = await client.get(
        f"/api/v1/projects/{alice.project_id}/repositories/{alice.repository_id}",
        headers=alice.headers,
    )
    assert repository.json()["status"] == "ready"


@requires_db
async def test_empty_zip_is_ingested_with_no_files(
    client: httpx.AsyncClient, db_session: AsyncSession, data_dir: Path, alice: Owner
) -> None:
    response = await _ingest(client, alice, _zip({}))
    _assert_accepted(response, alice.repository_id)
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.READY
    assert await _file_rows(db_session, alice.repository_id) == {}
    assert _files_under(_storage(data_dir, alice.repository_id)) == set()


@requires_db
async def test_ingestion_does_not_execute_uploaded_code(
    client: httpx.AsyncClient, tmp_path: Path, data_dir: Path, alice: Owner
) -> None:
    marker = tmp_path / "executed.txt"
    payload = f"open({str(marker)!r}, 'w').write('pwned')\n".encode()
    archive = {
        "evil_module_x1.py": payload,
        "__init__.py": payload,
        "setup.py": payload,
        "conftest.py": payload,
        "run.sh": b"#!/bin/sh\ntouch " + str(marker).encode() + b"\n",
        "sitecustomize.py": payload,
    }
    response = await _ingest(client, alice, _zip(archive))
    _assert_accepted(response, alice.repository_id)
    assert not marker.exists()
    assert "evil_module_x1" not in sys.modules
    storage = _storage(data_dir, alice.repository_id)
    assert (storage / "setup.py").read_bytes() == payload  # stored as inert bytes


@requires_db
async def test_reingestion_replaces_files_atomically(
    client: httpx.AsyncClient, db_session: AsyncSession, data_dir: Path, alice: Owner
) -> None:
    first = await _ingest(client, alice, _zip({"a.txt": b"a", "b.txt": b"old"}))
    _assert_accepted(first, alice.repository_id)
    assert set(await _file_rows(db_session, alice.repository_id)) == {"a.txt", "b.txt"}
    second = await _ingest(client, alice, _zip({"b.txt": b"new", "c/d.txt": b"d"}))
    _assert_accepted(second, alice.repository_id)

    rows = await _file_rows(db_session, alice.repository_id)
    assert set(rows) == {"b.txt", "c/d.txt"}
    assert rows["b.txt"].sha256 == hashlib.sha256(b"new").hexdigest()
    storage = _storage(data_dir, alice.repository_id)
    assert _files_under(storage) == {"b.txt", "c/d.txt"}
    assert (storage / "b.txt").read_bytes() == b"new"
    assert _leftovers(data_dir) == []


# --- API: rejected uploads (request errors, status unchanged) ----------------------------


@requires_db
async def test_missing_or_misnamed_file_field(client: httpx.AsyncClient, alice: Owner) -> None:
    url = _ingest_url(alice.project_id, alice.repository_id)
    wrong_name = await client.post(
        url, files={"upload": ("repo.zip", _zip(SAMPLE), "application/zip")}, headers=alice.headers
    )
    assert wrong_name.status_code == 422
    not_multipart = await client.post(url, content=_zip(SAMPLE), headers=alice.headers)
    assert not_multipart.status_code == 415
    two_files = await client.post(
        url,
        files=[("file", ("a.zip", b"a", "application/zip")), ("file", ("b.zip", b"b", "x/y"))],
        headers=alice.headers,
    )
    assert two_files.status_code == 400


@requires_db
async def test_upload_size_limit(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 1)
    big = _zip({"blob.bin": os.urandom(2 * 1024 * 1024)}, zipfile.ZIP_STORED)
    response = await _ingest(client, alice, big)
    assert response.status_code == 413
    assert response.json() == {"detail": "Upload exceeds the size limit"}
    # Slightly over the limit but within the multipart allowance: caught after parsing.
    near = _zip({"blob.bin": os.urandom(1024 * 1024 + 1000)}, zipfile.ZIP_STORED)
    assert (await _ingest(client, alice, near)).status_code == 413
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.PENDING


@requires_db
async def test_concurrent_ingestion_is_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    await db_session.execute(
        update(Repository)
        .where(Repository.id == uuid.UUID(alice.repository_id))
        .values(status=RepositoryStatus.PROCESSING)
    )
    await db_session.commit()
    response = await _ingest(client, alice, _zip(SAMPLE))
    assert response.status_code == 409
    assert response.json() == {"detail": "Repository ingestion is already in progress"}
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.PROCESSING
    assert await _file_rows(db_session, alice.repository_id) == {}


# --- API: failed ingestion ---------------------------------------------------------------

UNSAFE_PATH = "Archive contains an unsafe path"
# Archive problems are found by the worker: the request is accepted (202) and the job fails.
FAILING_ARCHIVES = {
    "malformed": (b"PK\x03\x04 definitely not a zip", "Invalid ZIP archive"),
    "traversal": (_zip_raw_names(["ok.txt", "../../evil.txt"]), UNSAFE_PATH),
    "nested traversal": (_zip_raw_names(["ok.txt", "src/../../evil.txt"]), UNSAFE_PATH),
    "absolute unix": (_zip_raw_names(["ok.txt", "/tmp/evil.txt"]), UNSAFE_PATH),
    "windows drive slash": (_zip_raw_names(["ok.txt", "C:/evil.txt"]), UNSAFE_PATH),
    "windows drive backslash": (_zip_raw_names(["ok.txt", "C:\\evil.txt"]), UNSAFE_PATH),
    "unc": (_zip_raw_names(["ok.txt", "\\\\server\\share\\evil.txt"]), UNSAFE_PATH),
    "symlink": (_zip_symlink(), "Archive contains a symbolic link"),
    "duplicate": (
        _zip_raw_names(["ok.txt", "dup.txt", "dup.txt"]),
        "Archive contains duplicate or conflicting paths",
    ),
    "zip bomb": (_bomb(), "Archive entry exceeds the compression ratio limit"),
}


@requires_db
@pytest.mark.parametrize("case", list(FAILING_ARCHIVES))
async def test_failed_ingestion_is_controlled_and_leaves_no_trace(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    data_dir: Path,
    alice: Owner,
    case: str,
) -> None:
    data, detail = FAILING_ARCHIVES[case]
    response = await _ingest(client, alice, data)
    _assert_accepted(response, alice.repository_id)
    error = await _job_error(db_session, alice.repository_id)
    assert error == detail
    assert str(tmp_path) not in str(error) and "Traceback" not in str(error)

    assert await _status(db_session, alice.repository_id) == RepositoryStatus.FAILED
    assert await _file_rows(db_session, alice.repository_id) == {}
    assert not _storage(data_dir, alice.repository_id).exists()
    assert _leftovers(data_dir) == []
    # Nothing was written anywhere in the temporary area (no evil.txt, no stray files).
    assert _files_under(tmp_path) == set()


@requires_db
@pytest.mark.parametrize(
    ("setting", "value", "archive", "detail"),
    [
        (
            "max_archive_file_count",
            3,
            {f"f{i}.txt": b"x" for i in range(4)},
            "Archive contains too many entries",
        ),
        (
            "max_extracted_size_mb",
            1,
            {"a.bin": b"a" * 600_000, "b.bin": b"b" * 600_000},
            "Archive exceeds the extracted size limit",
        ),
        (
            "max_extracted_file_size_mb",
            1,
            {"big.bin": b"c" * (1024 * 1024 + 1)},
            "Archive contains a file that exceeds the size limit",
        ),
    ],
)
async def test_configured_limits_are_enforced(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: int,
    archive: dict[str, bytes],
    detail: str,
) -> None:
    monkeypatch.setattr(get_settings(), setting, value)
    monkeypatch.setattr(get_settings(), "max_compression_ratio", 10_000)
    response = await _ingest(client, alice, _zip(archive, zipfile.ZIP_STORED))
    _assert_accepted(response, alice.repository_id)
    assert await _job_error(db_session, alice.repository_id) == detail
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.FAILED
    assert await _file_rows(db_session, alice.repository_id) == {}
    assert _leftovers(data_dir) == []


@requires_db
async def test_failed_reingestion_preserves_previous_files(
    client: httpx.AsyncClient, db_session: AsyncSession, data_dir: Path, alice: Owner
) -> None:
    assert (await _ingest(client, alice, _zip(SAMPLE))).status_code == 202
    before = {p: f.sha256 for p, f in (await _file_rows(db_session, alice.repository_id)).items()}

    response = await _ingest(client, alice, _zip_raw_names(["new.txt", "../../evil.txt"]))
    _assert_accepted(response, alice.repository_id)
    assert await _job_error(db_session, alice.repository_id) == UNSAFE_PATH
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.FAILED
    after = {p: f.sha256 for p, f in (await _file_rows(db_session, alice.repository_id)).items()}
    assert after == before  # no partial rows, previous rows intact
    assert _files_under(_storage(data_dir, alice.repository_id)) == set(SAMPLE)
    assert _leftovers(data_dir) == []


@requires_db
async def test_storage_swap_failure_is_controlled(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    data_dir: Path,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_swap(staging: Path, final: Path) -> None:
        raise OSError(f"simulated failure for {final}")

    monkeypatch.setattr(ingestion_service, "swap_into_place", failing_swap)
    response = await _ingest(client, alice, _zip(SAMPLE))
    _assert_accepted(response, alice.repository_id)
    error = await _job_error(db_session, alice.repository_id)
    assert error == "Repository storage could not be updated"
    assert str(tmp_path) not in str(error)
    assert await _status(db_session, alice.repository_id) == RepositoryStatus.FAILED
    assert _leftovers(data_dir) == []


@requires_db
async def test_storage_stays_inside_data_dir(
    client: httpx.AsyncClient, tmp_path: Path, data_dir: Path, alice: Owner
) -> None:
    assert (await _ingest(client, alice, _zip(SAMPLE))).status_code == 202
    storage_root = (data_dir / "repositories").resolve()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            resolved = path.resolve()
            assert storage_root in resolved.parents
            assert not path.is_symlink()
