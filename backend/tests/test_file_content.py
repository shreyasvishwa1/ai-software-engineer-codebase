"""Repository file content API tests (GET .../files/content?path=...).

API tests run against the already-migrated throwaway test database (TEST_DATABASE_URL, name
ending in "_test") inside rolled-back transactions; ingestion runs inline with the real
worker logic and DATA_DIR is a temporary directory. Filesystem access by the service is
observed with spies, so tests can prove that a request never touched the disk. Storage
swaps are controlled deterministically (no sleeps). Junction tests are Windows-only;
symlink tests are skipped where symlinks cannot be created.
"""

import os
import shutil
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.services import file_content, ingestion
from app.services.file_content import MAX_CONTENT_BYTES
from tests.test_worker import _zip, install_inline_worker

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
REPO_NOT_FOUND = {"detail": "Repository not found"}
FILE_NOT_FOUND = {"detail": "File not found"}
UNAVAILABLE = {"detail": "File content is not available; try again later"}
BIG = os.urandom(MAX_CONTENT_BYTES + 1)  # incompressible, so the archive ratio check passes
FILES: dict[str, bytes] = {
    "src/app.py": b"print('hello')\n",
    "src/utils/deep/helper.py": b"def helper():\n    return 42\n",
    "unicode.txt": "h\u00e9llo \u2014 \u4e16\u754c \U0001f680\n".encode(),
    "bom.txt": b"\xef\xbb\xbfwith a byte-order mark\n",
    "empty.txt": b"",
    "page.html": b"<script>alert('x')</script><img src=x onerror=alert(1)>",
    "zero-byte.dat": b"abc\x00def",
    "latin1.txt": "caf\u00e9".encode("latin-1"),
    "big.bin": BIG,
    "exact.txt": b"x" * 10,
}


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
async def client(
    db_session: AsyncSession, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    install_inline_worker(monkeypatch, db_session)

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


async def _owner(client: httpx.AsyncClient, email: str, files: dict[str, bytes]) -> Owner:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project = (await client.post("/api/v1/projects", json={"name": "P"}, headers=headers)).json()
    repository = await client.post(
        f"/api/v1/projects/{project['id']}/repositories",
        json={"name": "repo", "source_type": "zip"},
        headers=headers,
    )
    owner = Owner(headers, project["id"], repository.json()["id"])
    assert (await _ingest(client, owner, files)).status_code == 202
    return owner


async def _ingest(
    client: httpx.AsyncClient, owner: Owner, files: dict[str, bytes]
) -> httpx.Response:
    return await client.post(
        f"/api/v1/projects/{owner.project_id}/repositories/{owner.repository_id}/ingest",
        files={"file": ("repo.zip", _zip(files), "application/zip")},
        headers=owner.headers,
    )


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com", FILES)


@pytest.fixture
async def bob(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "bob@example.com", {"bob.txt": b"bob's file\n"})


def _content_url(project_id: str, repository_id: str) -> str:
    return f"/api/v1/projects/{project_id}/repositories/{repository_id}/files/content"


async def _get(
    client: httpx.AsyncClient, owner: Owner, path: str | None, **kw: Any
) -> httpx.Response:
    url = _content_url(
        kw.pop("project_id", owner.project_id), kw.pop("repository_id", owner.repository_id)
    )
    params = {} if path is None else {"path": path}
    return await client.get(url, params=params, headers=kw.pop("headers", owner.headers))


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


@dataclass
class FsSpy:
    reads: list[str]
    opened: list[str]


@pytest.fixture
def fs_spy(monkeypatch: pytest.MonkeyPatch) -> FsSpy:
    """Records every filesystem read attempt and every file opened by the service."""
    spy = FsSpy(reads=[], opened=[])
    real_read = file_content._read_verified

    def read(root: Path, relative: str, expected: str | None) -> bytes:
        spy.reads.append(relative)
        return real_read(root, relative, expected)

    def tracking_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        spy.opened.append(str(file))
        return open(file, *args, **kwargs)

    monkeypatch.setattr(file_content, "_read_verified", read)
    monkeypatch.setattr(file_content, "open", tracking_open, raising=False)
    return spy


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
    assert os.path.isjunction(link)


# --- Text content ------------------------------------------------------------------------


@requires_db
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app.py", "print('hello')\n"),
        ("unicode.txt", "h\u00e9llo \u2014 \u4e16\u754c \U0001f680\n"),
        ("bom.txt", "with a byte-order mark\n"),  # BOM removed
        ("empty.txt", ""),
        ("src/utils/deep/helper.py", "def helper():\n    return 42\n"),
    ],
)
async def test_text_content(
    client: httpx.AsyncClient, alice: Owner, path: str, expected: str
) -> None:
    response = await _get(client, alice, path)

    assert response.status_code == 200
    assert response.json() == {
        "path": path,
        "language": response.json()["language"],
        "size_bytes": len(FILES[path]),
        "content": expected,
        "reason": None,
    }
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"


@requires_db
async def test_language_and_no_internal_fields(client: httpx.AsyncClient, alice: Owner) -> None:
    body = (await _get(client, alice, "src/app.py")).json()
    assert body["language"] == "Python"
    assert set(body) == {"path", "language", "size_bytes", "content", "reason"}
    text = str(body)
    assert alice.repository_id not in text and str(get_settings().data_dir) not in text


@requires_db
async def test_html_is_returned_as_a_json_string(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _get(client, alice, "page.html")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json()["content"] == FILES["page.html"].decode()
    assert response.text.startswith("{")  # a JSON document, never raw HTML


# --- Not displayable ---------------------------------------------------------------------


@requires_db
@pytest.mark.parametrize("path", ["zero-byte.dat", "latin1.txt"])
async def test_binary_content(client: httpx.AsyncClient, alice: Owner, path: str) -> None:
    response = await _get(client, alice, path)
    assert response.status_code == 200
    assert response.json() == {
        "path": path,
        "language": response.json()["language"],
        "size_bytes": len(FILES[path]),
        "content": None,
        "reason": "binary",
    }
    assert response.headers["cache-control"] == "no-store"


@requires_db
async def test_too_large_file_is_never_opened(
    client: httpx.AsyncClient, alice: Owner, fs_spy: FsSpy
) -> None:
    response = await _get(client, alice, "big.bin")

    assert response.status_code == 200
    assert response.json() == {
        "path": "big.bin",
        "language": None,
        "size_bytes": MAX_CONTENT_BYTES + 1,
        "content": None,
        "reason": "too_large",
    }
    assert (fs_spy.reads, fs_spy.opened) == ([], [])
    assert MAX_CONTENT_BYTES == 1024 * 1024


# --- Ownership and validation ------------------------------------------------------------


@requires_db
async def test_unowned_or_missing_repository_is_not_found(
    client: httpx.AsyncClient, alice: Owner, bob: Owner, fs_spy: FsSpy
) -> None:
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    attempts = [
        await _get(
            client, alice, "bob.txt", project_id=bob.project_id, repository_id=bob.repository_id
        ),
        await _get(
            client,
            bob,
            "src/app.py",
            project_id=alice.project_id,
            repository_id=alice.repository_id,
        ),
        await _get(client, alice, "src/app.py", project_id=other_project),
        await _get(client, alice, "src/app.py", repository_id=str(uuid.uuid4())),
    ]
    for response in attempts:
        assert (response.status_code, response.json()) == (404, REPO_NOT_FOUND)
    assert fs_spy.reads == []


@requires_db
async def test_unauthenticated_is_rejected(client: httpx.AsyncClient, alice: Owner) -> None:
    for headers in ({}, {"Authorization": "Bearer not-a-token"}):
        assert (await _get(client, alice, "src/app.py", headers=headers)).status_code == 401


@requires_db
async def test_invalid_input_is_rejected(
    client: httpx.AsyncClient, alice: Owner, fs_spy: FsSpy
) -> None:
    assert (await _get(client, alice, "src/app.py", repository_id="not-a-uuid")).status_code == 422
    assert (await _get(client, alice, "src/app.py", project_id="not-a-uuid")).status_code == 422
    assert (await _get(client, alice, None)).status_code == 422  # missing
    assert (await _get(client, alice, "")).status_code == 422  # empty
    assert (await _get(client, alice, "a" * 4097)).status_code == 422  # too long
    assert (await _get(client, alice, "a" * 4096)).status_code == 404  # at the limit: lookup only
    assert fs_spy.reads == []


# --- Traversal input never reaches the filesystem ----------------------------------------


@requires_db
@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "src/../../secret.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "/src/app.py",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "src\\app.py",
        "..\\..\\secret.txt",
        "%2e%2e%2fsecret.txt",
        "..%2f..%2fsecret.txt",
        "./src/app.py",
        "src//app.py",
        "SRC/APP.PY",
        "src/App.py",
        "does/not/exist.py",
    ],
)
async def test_unknown_or_traversal_paths_are_not_found(
    client: httpx.AsyncClient, alice: Owner, data_dir: Path, fs_spy: FsSpy, path: str
) -> None:
    (data_dir / "secret.txt").write_text("outside the repository")
    response = await _get(client, alice, path)
    assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)
    assert (fs_spy.reads, fs_spy.opened) == ([], [])


@requires_db
async def test_raw_encoded_traversal_is_not_found(
    client: httpx.AsyncClient, alice: Owner, fs_spy: FsSpy
) -> None:
    url = _content_url(alice.project_id, alice.repository_id)
    for query in ("%2e%2e%2f%2e%2e%2fetc%2fpasswd", "..%2F..%2Fsecret.txt", "%2Fetc%2Fpasswd"):
        response = await client.get(f"{url}?path={query}", headers=alice.headers)
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)
    assert (fs_spy.reads, fs_spy.opened) == ([], [])


# --- Integrity ---------------------------------------------------------------------------


@requires_db
async def test_modified_file_is_unavailable(client: httpx.AsyncClient, alice: Owner) -> None:
    (_storage(alice) / "src" / "app.py").write_bytes(b"print('tampered')\n")
    response = await _get(client, alice, "src/app.py")
    assert (response.status_code, response.json()) == (409, UNAVAILABLE)
    assert "tampered" not in response.text


@requires_db
async def test_deleted_file_is_unavailable(client: httpx.AsyncClient, alice: Owner) -> None:
    (_storage(alice) / "src" / "app.py").unlink()
    response = await _get(client, alice, "src/app.py")
    assert (response.status_code, response.json()) == (409, UNAVAILABLE)
    assert str(_storage(alice)) not in response.text


@requires_db
async def test_replaced_by_directory_is_unavailable(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    target = _storage(alice) / "exact.txt"
    target.unlink()
    target.mkdir()
    assert (await _get(client, alice, "exact.txt")).status_code == 409


@requires_db
async def test_storage_swap_window_never_serves_the_wrong_snapshot(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-ingestion commits the new files rows before swapping storage. The swap is held
    back deterministically: until it runs, changed files are unavailable (never the old
    bytes); afterwards the new content is served."""
    deferred: list[tuple[Path, Path]] = []
    real_swap = ingestion.swap_into_place
    monkeypatch.setattr(ingestion, "swap_into_place", lambda s, f: deferred.append((s, f)))
    updated = {**FILES, "src/app.py": b"print('version 2')\n"}
    assert (await _ingest(client, alice, updated)).status_code == 202
    assert len(deferred) == 1  # rows committed, storage still holds the old snapshot

    in_window = await _get(client, alice, "src/app.py")
    assert (in_window.status_code, in_window.json()) == (409, UNAVAILABLE)
    unchanged = await _get(client, alice, "unicode.txt")  # same bytes in both snapshots
    assert unchanged.json()["content"] == FILES["unicode.txt"].decode()

    real_swap(*deferred[0])
    after = await _get(client, alice, "src/app.py")
    assert (after.status_code, after.json()["content"]) == (200, "print('version 2')\n")


@requires_db
async def test_failed_swap_leaves_changed_files_unavailable(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_swap(staging: Path, final: Path) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(ingestion, "swap_into_place", failing_swap)
    updated = {**FILES, "src/app.py": b"print('never stored')\n"}
    assert (await _ingest(client, alice, updated)).status_code == 202
    response = await _get(client, alice, "src/app.py")
    assert (response.status_code, response.json()) == (409, UNAVAILABLE)


@requires_db
async def test_reingestion_serves_new_content(client: httpx.AsyncClient, alice: Owner) -> None:
    updated = {"src/app.py": b"print('second snapshot')\n"}
    assert (await _ingest(client, alice, updated)).status_code == 202
    assert (await _get(client, alice, "src/app.py")).json()[
        "content"
    ] == "print('second snapshot')\n"
    assert (await _get(client, alice, "unicode.txt")).status_code == 404  # not in the new snapshot


# --- Links -------------------------------------------------------------------------------


@requires_db
async def test_junction_in_path_is_refused(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path, fs_spy: FsSpy
) -> None:
    """A stored directory is replaced by a junction to an outside copy with identical bytes,
    so the hash check cannot refuse it; the link and containment checks do."""
    utils = _storage(alice) / "src" / "utils"
    outside = tmp_path / "outside"
    shutil.copytree(utils, outside)
    shutil.rmtree(utils)
    _junction(utils, outside)

    response = await _get(client, alice, "src/utils/deep/helper.py")

    assert (response.status_code, response.json()) == (409, UNAVAILABLE)
    assert fs_spy.opened == []  # the outside file was never opened
    assert (outside / "deep" / "helper.py").exists()


@requires_db
async def test_repository_root_junction_is_refused(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path, fs_spy: FsSpy
) -> None:
    root = _storage(alice)
    moved = tmp_path / "moved-storage"
    shutil.move(str(root), str(moved))
    _junction(root, moved)

    response = await _get(client, alice, "src/app.py")

    assert (response.status_code, response.json()) == (409, UNAVAILABLE)
    assert fs_spy.opened == []


@requires_db
async def test_symlinked_file_is_refused(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path, fs_spy: FsSpy
) -> None:
    stored = _storage(alice) / "src" / "app.py"
    outside = tmp_path / "outside-app.py"
    outside.write_bytes(FILES["src/app.py"])  # identical bytes: the hash check cannot refuse
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")

    response = await _get(client, alice, "src/app.py")

    assert (response.status_code, response.json()) == (409, UNAVAILABLE)
    assert fs_spy.opened == []
