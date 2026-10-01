"""Repository file tree and language statistics tests.

Tree-building tests need no database. API tests run against the already-migrated throwaway
test database (TEST_DATABASE_URL, name ending in "_test") inside rolled-back transactions;
queued ingestion runs inline with the real worker logic, DATA_DIR is a temporary directory
and GitHub is mocked (respx + mocked DNS).
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from app.schemas.repository_files import TreeDirectory, TreeFile
from app.services import github as github_service
from app.services.repository_files import build_tree
from tests.test_worker import _unsafe_zip, _zip, install_inline_worker

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
CONTENT_MARKER = b"FILE-CONTENT-MUST-NOT-LEAK-91c2"
ARCHIVE = {
    "src/app.py": b"print('app')\n# " + CONTENT_MARKER + b"\n",
    "src/utils/helper.py": b"def helper():\n    return 1\n",
    "src/Zeta.md": b"# Zeta\n",
    "src/alpha.txt": b"alpha\n",
    "README.md": b"# Readme\n",
    "docs/guide.md": b"# Guide\n",
    "Dockerfile": b"FROM scratch\n",
    "Makefile": b"all:\n\ttrue\n",
    "a.unknownext": b"???\n",
}
LANGUAGES = {
    "src/app.py": "Python",
    "src/utils/helper.py": "Python",
    "src/Zeta.md": "Markdown",
    "src/alpha.txt": "Text",
    "README.md": "Markdown",
    "docs/guide.md": "Markdown",
    "Dockerfile": "Dockerfile",
    "Makefile": "Makefile",
    "a.unknownext": None,
}
EXPECTED_SHAPE = (
    "directory",
    "",
    [
        ("directory", "docs", [("file", "docs/guide.md")]),
        (
            "directory",
            "src",
            [
                ("directory", "src/utils", [("file", "src/utils/helper.py")]),
                ("file", "src/alpha.txt"),
                ("file", "src/app.py"),
                ("file", "src/Zeta.md"),
            ],
        ),
        ("file", "a.unknownext"),
        ("file", "Dockerfile"),
        ("file", "Makefile"),
        ("file", "README.md"),
    ],
)
TOP_TREE_FIELDS = {"repository_id", "status", "file_count", "total_bytes", "tree"}
TOP_LANGUAGE_FIELDS = {"repository_id", "status", "total_files", "total_bytes", "languages"}
FILE_FIELDS = {"type", "name", "path", "extension", "language", "size_bytes"}
DIRECTORY_FIELDS = {"type", "name", "path", "children"}
GITHUB_IP = "140.82.112.3"
CODELOAD_IP = "140.82.112.10"


def _shape(node: dict[str, Any]) -> tuple[Any, ...]:
    if node["type"] == "file":
        return ("file", node["path"])
    return ("directory", node["path"], [_shape(child) for child in node["children"]])


def _file_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    if node["type"] == "file":
        return [node]
    return [f for child in node["children"] for f in _file_nodes(child)]


def _directory_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    if node["type"] == "file":
        return []
    return [node] + [d for child in node["children"] for d in _directory_nodes(child)]


def _expected_languages(archive: dict[str, bytes], languages: dict[str, str | None]) -> list[Any]:
    totals: dict[str | None, list[int]] = {}
    for path, data in archive.items():
        bucket = totals.setdefault(languages[path], [0, 0])
        bucket[0] += 1
        bucket[1] += len(data)
    total_bytes = sum(len(data) for data in archive.values())
    rows = [
        {
            "language": language,
            "file_count": count,
            "total_bytes": size,
            "percentage": round(size * 100 / total_bytes, 2) if total_bytes else 0.0,
        }
        for language, (count, size) in totals.items()
    ]
    return sorted(rows, key=lambda r: (-r["total_bytes"], r["language"] is None, r["language"] or ""))


# --- Tree building (no database) ---------------------------------------------------------


def _file(path: str, size: int = 1) -> TreeFile:
    name = path.rsplit("/", 1)[-1]
    return TreeFile(name=name, path=path, extension=None, language=None, size_bytes=size)


def test_build_tree_nests_and_orders_deterministically() -> None:
    paths = list(ARCHIVE)
    forward = build_tree([_file(p) for p in paths]).model_dump()
    backward = build_tree([_file(p) for p in reversed(paths)]).model_dump()
    assert forward == backward  # input order does not matter
    assert _shape(forward) == EXPECTED_SHAPE


def test_build_tree_directories_have_correct_names_and_paths() -> None:
    tree = build_tree([_file("a/b/c/d.txt")]).model_dump()
    assert [(d["name"], d["path"]) for d in _directory_nodes(tree)] == [
        ("", ""),
        ("a", "a"),
        ("b", "a/b"),
        ("c", "a/b/c"),
    ]


def test_build_tree_case_insensitive_with_stable_tie_break() -> None:
    tree = build_tree([_file("b.txt"), _file("B.md"), _file("a.txt"), _file("C/x.py")])
    assert [child.name for child in tree.children] == ["C", "a.txt", "B.md", "b.txt"]


def test_build_tree_empty() -> None:
    assert build_tree([]) == TreeDirectory(name="", path="", children=[])


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

    install_inline_worker(monkeypatch, db_session)
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


async def _owner(client: httpx.AsyncClient, email: str) -> Owner:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project = (await client.post("/api/v1/projects", json={"name": "P"}, headers=headers)).json()
    return Owner(headers, project["id"], await _repository(client, headers, project["id"]))


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com")


@pytest.fixture
async def bob(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "bob@example.com")


def _url(project_id: str, repository_id: str, view: str) -> str:
    return f"/api/v1/projects/{project_id}/repositories/{repository_id}/{view}"


async def _ingest(client: httpx.AsyncClient, owner: Owner, data: bytes) -> None:
    response = await client.post(
        f"/api/v1/projects/{owner.project_id}/repositories/{owner.repository_id}/ingest",
        files={"file": ("repo.zip", data, "application/zip")},
        headers=owner.headers,
    )
    assert response.status_code == 202, response.text


async def _get(client: httpx.AsyncClient, owner: Owner, view: str) -> dict[str, Any]:
    response = await client.get(
        _url(owner.project_id, owner.repository_id, view), headers=owner.headers
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


# --- API: authentication and ownership ---------------------------------------------------


@requires_db
@pytest.mark.parametrize("view", ["tree", "languages"])
async def test_unauthenticated_requests_fail(
    client: httpx.AsyncClient, alice: Owner, view: str
) -> None:
    response = await client.get(_url(alice.project_id, alice.repository_id, view))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@requires_db
@pytest.mark.parametrize("view", ["tree", "languages"])
async def test_other_users_and_missing_repositories_are_not_found(
    client: httpx.AsyncClient, alice: Owner, bob: Owner, view: str
) -> None:
    await _ingest(client, bob, _zip({"secret/bob_only.py": b"x = 1\n"}))
    other = (await client.post("/api/v1/projects", json={"name": "O"}, headers=alice.headers)).json()
    attempts = [
        (bob.project_id, bob.repository_id),  # Bob's project and repository
        (alice.project_id, bob.repository_id),  # Alice's project, Bob's repository
        (other["id"], alice.repository_id),  # Alice's repository under her other project
        (alice.project_id, str(uuid.uuid4())),  # nonexistent repository
        (str(uuid.uuid4()), alice.repository_id),  # nonexistent project
    ]
    for project_id, repository_id in attempts:
        response = await client.get(_url(project_id, repository_id, view), headers=alice.headers)
        assert response.status_code == 404
        assert response.json() == {"detail": "Repository not found"}
        assert "bob_only" not in response.text


@requires_db
@pytest.mark.parametrize("view", ["tree", "languages"])
async def test_invalid_uuid(client: httpx.AsyncClient, alice: Owner, view: str) -> None:
    for project_id, repository_id in (("bad", alice.repository_id), (alice.project_id, "bad")):
        response = await client.get(_url(project_id, repository_id, view), headers=alice.headers)
        assert response.status_code == 422


@requires_db
async def test_read_only_endpoints(client: httpx.AsyncClient, alice: Owner) -> None:
    for view in ("tree", "languages"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = await client.request(
                method, _url(alice.project_id, alice.repository_id, view), headers=alice.headers
            )
            assert response.status_code == 405


# --- API: tree ---------------------------------------------------------------------------


@requires_db
async def test_empty_repository(client: httpx.AsyncClient, alice: Owner) -> None:
    tree = await _get(client, alice, "tree")
    assert tree == {
        "repository_id": alice.repository_id,
        "status": "pending",
        "file_count": 0,
        "total_bytes": 0,
        "tree": {"type": "directory", "name": "", "path": "", "children": []},
    }
    languages = await _get(client, alice, "languages")
    assert languages == {
        "repository_id": alice.repository_id,
        "status": "pending",
        "total_files": 0,
        "total_bytes": 0,
        "languages": [],
    }


@requires_db
async def test_tree_structure_fields_and_ordering(
    client: httpx.AsyncClient, data_dir: Path, alice: Owner
) -> None:
    await _ingest(client, alice, _zip(ARCHIVE))
    body = await _get(client, alice, "tree")
    assert set(body) == TOP_TREE_FIELDS
    assert (body["repository_id"], body["status"]) == (alice.repository_id, "ready")
    assert body["file_count"] == len(ARCHIVE)
    assert body["total_bytes"] == sum(len(data) for data in ARCHIVE.values())
    assert _shape(body["tree"]) == EXPECTED_SHAPE

    files = {node["path"]: node for node in _file_nodes(body["tree"])}
    assert set(files) == set(ARCHIVE)
    for path, node in files.items():
        assert set(node) == FILE_FIELDS
        assert node["name"] == path.rsplit("/", 1)[-1]
        assert node["size_bytes"] == len(ARCHIVE[path])
        assert node["language"] == LANGUAGES[path]
    assert files["src/app.py"]["extension"] == "py"
    assert files["Dockerfile"]["extension"] is None
    for directory in _directory_nodes(body["tree"]):
        assert set(directory) == DIRECTORY_FIELDS

    second = await _get(client, alice, "tree")
    assert second == body  # deterministic


@requires_db
async def test_responses_expose_no_internal_or_sensitive_data(
    client: httpx.AsyncClient, db_session: AsyncSession, data_dir: Path, alice: Owner
) -> None:
    await _ingest(client, alice, _zip(ARCHIVE))
    file_ids = {str(i) for i in await db_session.scalars(select(File.id))}
    checksums = set(await db_session.scalars(select(File.sha256)))
    for view in ("tree", "languages"):
        response = await client.get(
            _url(alice.project_id, alice.repository_id, view), headers=alice.headers
        )
        text = response.text
        assert "sha256" not in text
        assert not any(checksum and checksum in text for checksum in checksums)
        assert not any(file_id in text for file_id in file_ids)
        assert CONTENT_MARKER.decode() not in text  # no file contents
        assert str(data_dir) not in text and str(data_dir.resolve()) not in text
        assert "ingestion-uploads" not in text and "repositories/" not in text


# --- API: language statistics ------------------------------------------------------------


@requires_db
async def test_language_statistics(client: httpx.AsyncClient, alice: Owner) -> None:
    await _ingest(client, alice, _zip(ARCHIVE))
    body = await _get(client, alice, "languages")
    assert set(body) == TOP_LANGUAGE_FIELDS
    assert (body["repository_id"], body["status"]) == (alice.repository_id, "ready")
    assert body["total_files"] == len(ARCHIVE)
    assert body["total_bytes"] == sum(len(data) for data in ARCHIVE.values())
    assert body["languages"] == _expected_languages(ARCHIVE, LANGUAGES)
    assert body["languages"][-1]["language"] is None  # unknown types are reported as null
    assert abs(sum(row["percentage"] for row in body["languages"]) - 100) < 0.1
    assert all(set(row) == {"language", "file_count", "total_bytes", "percentage"}
               for row in body["languages"])


@requires_db
async def test_language_statistics_tie_breaks(client: httpx.AsyncClient, alice: Owner) -> None:
    await _ingest(client, alice, _zip({"b.rs": b"yy", "c.zzz": b"zz", "a.go": b"xx"}))
    body = await _get(client, alice, "languages")
    assert [row["language"] for row in body["languages"]] == ["Go", "Rust", None]
    assert [row["percentage"] for row in body["languages"]] == [33.33, 33.33, 33.33]


@requires_db
async def test_language_statistics_with_only_empty_files(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    await _ingest(client, alice, _zip({"empty.py": b"", "blank.md": b""}))
    body = await _get(client, alice, "languages")
    assert body["total_bytes"] == 0
    assert body["languages"] == [
        {"language": "Markdown", "file_count": 1, "total_bytes": 0, "percentage": 0.0},
        {"language": "Python", "file_count": 1, "total_bytes": 0, "percentage": 0.0},
    ]


# --- Language population at ingestion ----------------------------------------------------


@requires_db
async def test_zip_ingestion_records_languages(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    await _ingest(client, alice, _zip(ARCHIVE))
    rows = dict(
        (await db_session.execute(
            select(File.path, File.language).where(
                File.repository_id == uuid.UUID(alice.repository_id)
            )
        )).all()
    )
    assert rows == LANGUAGES


@requires_db
async def test_github_ingestion_records_languages(
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
        return_value=httpx.Response(
            200, content=_zip({"demo-main/main.go": b"package main\n", "demo-main/LICENSE": b"MIT\n"})
        )
    )
    response = await client.post(
        f"/api/v1/projects/{alice.project_id}/repositories/{repository_id}/ingest",
        headers=alice.headers,
    )
    assert response.status_code == 202
    owner = Owner(alice.headers, alice.project_id, repository_id)
    tree = await _get(client, owner, "tree")
    assert {f["path"]: f["language"] for f in _file_nodes(tree["tree"])} == {
        "demo-main/main.go": "Go",
        "demo-main/LICENSE": None,
    }
    languages = await _get(client, owner, "languages")
    assert [(row["language"], row["file_count"]) for row in languages["languages"]] == [
        ("Go", 1),
        (None, 1),
    ]


# --- Snapshots and status ----------------------------------------------------------------


@requires_db
async def test_failed_reingestion_keeps_previous_snapshot(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    await _ingest(client, alice, _zip(ARCHIVE))
    tree_before = await _get(client, alice, "tree")
    languages_before = await _get(client, alice, "languages")

    await _ingest(client, alice, _unsafe_zip())  # the worker rejects it
    tree_after = await _get(client, alice, "tree")
    languages_after = await _get(client, alice, "languages")
    assert tree_after["status"] == languages_after["status"] == "failed"
    assert {k: v for k, v in tree_after.items() if k != "status"} == {
        k: v for k, v in tree_before.items() if k != "status"
    }
    assert languages_after["languages"] == languages_before["languages"]


@requires_db
async def test_status_reflects_reingestion_in_progress(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    await _ingest(client, alice, _zip(ARCHIVE))
    # Simulate a re-ingestion in progress: the previous snapshot is still served.
    await db_session.execute(
        update(Repository)
        .where(Repository.id == uuid.UUID(alice.repository_id))
        .values(status=RepositoryStatus.PROCESSING)
    )
    await db_session.commit()
    tree = await _get(client, alice, "tree")
    languages = await _get(client, alice, "languages")
    assert tree["status"] == languages["status"] == "processing"
    assert tree["file_count"] == len(ARCHIVE)
    assert languages["total_files"] == len(ARCHIVE)
