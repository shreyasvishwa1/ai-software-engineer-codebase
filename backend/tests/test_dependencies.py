"""Dependency inventory tests (GET .../analysis/dependencies).

Parser tests need no database. API tests run against the already-migrated throwaway test
database (TEST_DATABASE_URL, "_test") inside rolled-back transactions; ingestion runs
inline with the real worker logic and DATA_DIR is a temporary directory. Filesystem access
is observed with spies. Junction tests are Windows-only; symlink tests are skipped where
symlinks cannot be created.
"""

import json
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
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import File
from app.services import dependencies, ingestion
from app.services.dependencies import (
    MAX_DEPENDENCIES,
    MAX_MANIFESTS,
    manifest_kind,
    parse_package_json,
    parse_pyproject,
    parse_requirements,
    split_requirement,
)
from app.services.file_content import MAX_CONTENT_BYTES
from tests.test_worker import _zip, install_inline_worker

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
NOT_FOUND = {"detail": "Repository not found"}
PACKAGE_JSON = json.dumps(
    {
        "name": "web",
        "dependencies": {"react": "^18.2.0", "axios": "1.6.7"},
        "devDependencies": {"vitest": "^3.2.7"},
        "peerDependencies": {"react-dom": ">=18"},
        "optionalDependencies": {"fsevents": "*"},
    }
).encode()
PYPROJECT = b"""
[project]
name = "svc"
dependencies = ["fastapi==0.110.0", "pydantic>=2,<3", "uvicorn[standard]~=0.29"]

[project.optional-dependencies]
dev = ["pytest>=8"]
docs = ["mkdocs"]
"""
REQUIREMENTS = b"""# pinned
requests==2.31.0
-r other.txt
flask  # web
"""
FILES: dict[str, bytes] = {
    "frontend/package.json": PACKAGE_JSON,
    "backend/pyproject.toml": PYPROJECT,
    "backend/requirements.txt": REQUIREMENTS,
    "backend/requirements-dev.txt": b"pytest==8.3.4\n",
    "broken/package.json": b"{not json",
    "src/app.py": b"print('hi')\n",
    # Look-alikes that are not manifests.
    "frontend/package.json.bak": PACKAGE_JSON,
    "docs/my-requirements.txt": b"evil==1\n",
    "Requirements.txt": b"evil==1\n",
    "pyproject.toml.old": PYPROJECT,
    "requirements.txt.d/x": b"evil==1\n",
}


# --- Parsers (no database) -----------------------------------------------------------------


def test_package_json_scopes() -> None:
    assert sorted(parse_package_json(PACKAGE_JSON.decode())) == [
        ("dev", "vitest", "^3.2.7"),
        ("optional", "fsevents", "*"),
        ("peer", "react-dom", ">=18"),
        ("runtime", "axios", "1.6.7"),
        ("runtime", "react", "^18.2.0"),
    ]


def test_package_json_skips_non_string_values() -> None:
    text = json.dumps(
        {
            "dependencies": {"ok": "1.0", "nested": {"version": "1"}, "num": 3, "nul": None},
            "devDependencies": ["not", "an", "object"],
            "peerDependencies": "nope",
        }
    )
    assert list(parse_package_json(text)) == [("runtime", "ok", "1.0")]
    with pytest.raises(ValueError):
        list(parse_package_json("[1, 2]"))  # not an object


def test_pyproject_runtime_and_optional_groups() -> None:
    assert list(parse_pyproject(PYPROJECT.decode())) == [
        ("runtime", "fastapi", "==0.110.0"),
        ("runtime", "pydantic", ">=2,<3"),
        ("runtime", "uvicorn", "[standard]~=0.29"),
        ("optional:dev", "pytest", ">=8"),
        ("optional:docs", "mkdocs", ""),
    ]


def test_pyproject_without_project_table() -> None:
    assert list(parse_pyproject('[tool.poetry.dependencies]\npython = "^3.12"\n')) == []
    assert list(parse_pyproject('project = "not a table"\n')) == []
    assert list(parse_pyproject('[project]\ndependencies = "fastapi"\n')) == []
    assert list(parse_pyproject("[project]\ndependencies = [1, true, 'ok']\n")) == [
        ("runtime", "ok", "")
    ]


def test_requirements_lines() -> None:
    text = "\r\n".join(
        [
            "# a comment",
            "",
            "   ",
            "requests==2.31.0  # trailing comment",
            "Django[argon2]>=4.2,<5 ; python_version >= '3.10'",
            "pkg#notacomment==1",
            "-r base.txt",
            "--requirement=other.txt",
            "-c constraints.txt",
            "-e git+https://github.com/x/y.git#egg=y",
            "--index-url https://pypi.example/simple",
            "https://example.com/pkg-1.0.tar.gz",
            "git+ssh://git@github.com/x/y.git",
            "./local/pkg",
            "../sibling",
            "/abs/path",
            "~/home/pkg",
            "C:\\pkgs\\x.whl",
            "file:pkgs/x",
            "!!!bad name",
            "name @ https://example.com/name.zip",
        ]
    )
    assert list(parse_requirements(text)) == [
        ("runtime", "requests", "==2.31.0"),
        ("runtime", "Django", "[argon2]>=4.2,<5 ; python_version >= '3.10'"),
        ("runtime", "pkg", "#notacomment==1"),
    ]


def test_requirements_never_follow_includes(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def no_open(*args: Any, **kwargs: Any) -> None:
        opened.append(str(args[0]) if args else "?")
        raise AssertionError("parsing must not open files")

    with monkeypatch.context() as patch:
        patch.setattr(dependencies, "open", no_open, raising=False)
        patch.setattr(Path, "open", no_open)
        patch.setattr(Path, "read_text", no_open)
        entries = list(parse_requirements("-r other.txt\n-c c.txt\nflask\n"))
    assert entries == [("runtime", "flask", "")]
    assert opened == []


def test_values_are_capped_at_200_characters() -> None:
    name, specifier = split_requirement("a" * 300 + "==" + "1" * 300) or ("", "")
    assert (len(name), len(specifier)) == (200, 200)
    assert list(parse_package_json(json.dumps({"dependencies": {"b" * 300: "c" * 300}}))) == [
        ("runtime", "b" * 200, "c" * 200)
    ]


def test_manifest_names_are_exact() -> None:
    for name in ("package.json", "pyproject.toml", "requirements.txt", "requirements-dev.txt"):
        assert manifest_kind(name) is not None
    for name in (
        "package.json.bak",
        "Package.json",
        "pyproject.toml.old",
        "my-requirements.txt",
        "Requirements.txt",
        "requirements.txt.bak",
        "requirements.in",
    ):
        assert manifest_kind(name) is None


def _inventory(tmp_path: Path, name: str, data: bytes) -> tuple[str, list[Any], bool]:
    import hashlib

    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / name).write_bytes(data)
    kind = manifest_kind(name)
    assert kind is not None
    return dependencies.inventory_manifest(
        root, name, len(data), hashlib.sha256(data).hexdigest(), kind, MAX_DEPENDENCIES
    )


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("package.json", b"{not json"),
        ("package.json", b"[" * 100_000 + b"]" * 100_000),  # deeply nested
        ("pyproject.toml", b"[project\ndependencies = ["),
        ("requirements.txt", b"\xff\xfe invalid utf-8"),
    ],
    ids=["invalid-json", "deeply-nested-json", "invalid-toml", "invalid-utf-8"],
)
def test_unusable_manifests_are_invalid(tmp_path: Path, name: str, data: bytes) -> None:
    assert _inventory(tmp_path, name, data) == ("invalid", [], False)


def test_bom_and_crlf_are_handled(tmp_path: Path) -> None:
    status, entries, _ = _inventory(
        tmp_path, "requirements.txt", b"\xef\xbb\xbfrequests==2\r\nflask\r\n"
    )
    assert (status, entries) == (
        "parsed",
        [("runtime", "flask", ""), ("runtime", "requests", "==2")],
    )


# --- API fixtures ----------------------------------------------------------------------------


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


async def _ingest(client: httpx.AsyncClient, owner: Owner, files: dict[str, bytes]) -> None:
    response = await client.post(
        f"/api/v1/projects/{owner.project_id}/repositories/{owner.repository_id}/ingest",
        files={"file": ("repo.zip", _zip(files), "application/zip")},
        headers=owner.headers,
    )
    assert response.status_code == 202, response.text


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
    await _ingest(client, owner, files)
    return owner


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com", FILES)


def _url(owner: Owner, project_id: str | None = None, repository_id: str | None = None) -> str:
    return (
        f"/api/v1/projects/{project_id or owner.project_id}"
        f"/repositories/{repository_id or owner.repository_id}/analysis/dependencies"
    )


async def _inventory_of(client: httpx.AsyncClient, owner: Owner) -> dict[str, Any]:
    response = await client.get(_url(owner), headers=owner.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = dependencies._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(dependencies, "_read_verified", spy)
    return reads


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_inventory(client: httpx.AsyncClient, alice: Owner, read_spy: list[str]) -> None:
    body = await _inventory_of(client, alice)

    assert [m["path"] for m in body["manifests"]] == [
        "backend/pyproject.toml",
        "backend/requirements-dev.txt",
        "backend/requirements.txt",
        "broken/package.json",
        "frontend/package.json",
    ]
    by_path = {m["path"]: m for m in body["manifests"]}
    assert {p: (m["ecosystem"], m["status"]) for p, m in by_path.items()} == {
        "backend/pyproject.toml": ("python", "parsed"),
        "backend/requirements-dev.txt": ("python", "parsed"),
        "backend/requirements.txt": ("python", "parsed"),
        "broken/package.json": ("npm", "invalid"),
        "frontend/package.json": ("npm", "parsed"),
    }
    pyproject = by_path["backend/pyproject.toml"]["dependencies"]
    assert [(d["scope"], d["name"]) for d in pyproject] == [
        ("optional:dev", "pytest"),
        ("optional:docs", "mkdocs"),
        ("runtime", "fastapi"),
        ("runtime", "pydantic"),
        ("runtime", "uvicorn"),
    ]
    assert {"name": "fastapi", "specifier": "==0.110.0", "scope": "runtime"} in pyproject
    assert [(d["scope"], d["name"]) for d in by_path["frontend/package.json"]["dependencies"]] == [
        ("dev", "vitest"),
        ("optional", "fsevents"),
        ("peer", "react-dom"),
        ("runtime", "axios"),
        ("runtime", "react"),
    ]
    assert [d["name"] for d in by_path["backend/requirements.txt"]["dependencies"]] == [
        "flask",
        "requests",
    ]
    assert by_path["broken/package.json"]["dependencies"] == []
    assert body["summary"] == {
        "manifests": 5,
        "parsed": 4,
        "invalid": 1,
        "too_large": 0,
        "unavailable": 0,
        "dependencies": 5 + 5 + 1 + 2,
    }
    assert body["truncated"] is False
    # Look-alikes were never read; every manifest went through the verified reader.
    assert sorted(read_spy) == sorted(by_path)


@requires_db
async def test_output_is_deterministic_and_exposes_no_internals(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    first = await client.get(_url(alice), headers=alice.headers)
    second = await client.get(_url(alice), headers=alice.headers)
    assert first.json() == second.json()
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in first.text and sha not in first.text
    assert alice.repository_id not in first.text
    assert str(get_settings().data_dir) not in first.text
    assert str(_storage(alice)) not in first.text


@requires_db
async def test_oversized_manifest_is_never_opened(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    await _ingest(client, alice, {"big/requirements.txt": os.urandom(MAX_CONTENT_BYTES + 1)})
    body = await _inventory_of(client, alice)
    assert [(m["path"], m["status"]) for m in body["manifests"]] == [
        ("big/requirements.txt", "too_large")
    ]
    assert read_spy == []
    assert body["summary"]["too_large"] == 1


@requires_db
async def test_unavailable_manifests(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    root = _storage(alice)
    (root / "backend" / "requirements.txt").write_bytes(b"tampered==1\n")  # hash mismatch
    (root / "backend" / "requirements-dev.txt").unlink()  # missing
    outside = tmp_path / "outside"
    shutil.copytree(root / "frontend", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "frontend")
    _junction(root / "frontend", outside)

    body = await _inventory_of(client, alice)
    status = {m["path"]: m["status"] for m in body["manifests"]}
    assert status["backend/requirements.txt"] == "unavailable"
    assert status["backend/requirements-dev.txt"] == "unavailable"
    assert status["frontend/package.json"] == "unavailable"
    assert status["backend/pyproject.toml"] == "parsed"
    assert "tampered" not in json.dumps(body)
    assert body["summary"]["unavailable"] == 3


@requires_db
async def test_symlinked_manifest_is_unavailable(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "backend" / "requirements-dev.txt"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(FILES["backend/requirements-dev.txt"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    body = await _inventory_of(client, alice)
    status = {m["path"]: m["status"] for m in body["manifests"]}
    assert status["backend/requirements-dev.txt"] == "unavailable"


@requires_db
async def test_manifest_limit(client: httpx.AsyncClient, alice: Owner) -> None:
    files = {
        f"pkg{i:03d}/requirements.txt": f"dep{i}==1\n".encode() for i in range(MAX_MANIFESTS + 1)
    }
    await _ingest(client, alice, files)
    body = await _inventory_of(client, alice)
    assert len(body["manifests"]) == MAX_MANIFESTS
    assert body["manifests"][-1]["path"] == f"pkg{MAX_MANIFESTS - 1:03d}/requirements.txt"
    assert body["truncated"] is True
    assert body["summary"]["manifests"] == MAX_MANIFESTS


@requires_db
async def test_dependency_limit(client: httpx.AsyncClient, alice: Owner) -> None:
    lines = "".join(f"dep{i:05d}==1\n" for i in range(MAX_DEPENDENCIES + 10))
    await _ingest(
        client,
        alice,
        {"a/requirements.txt": lines.encode(), "b/requirements.txt": b"later==1\n"},
    )
    body = await _inventory_of(client, alice)
    a, b = body["manifests"]
    assert (a["status"], len(a["dependencies"])) == ("parsed", MAX_DEPENDENCIES)
    assert (b["status"], b["dependencies"]) == ("parsed", [])  # still read and accounted for
    assert body["summary"]["dependencies"] == MAX_DEPENDENCIES
    assert sum(len(m["dependencies"]) for m in body["manifests"]) == MAX_DEPENDENCIES
    assert body["truncated"] is True


@requires_db
async def test_reingestion_shows_the_new_snapshot(client: httpx.AsyncClient, alice: Owner) -> None:
    await _ingest(client, alice, {"requirements.txt": b"only==1\n"})
    body = await _inventory_of(client, alice)
    assert [(m["path"], [d["name"] for d in m["dependencies"]]) for m in body["manifests"]] == [
        ("requirements.txt", ["only"])
    ]


@requires_db
async def test_ownership_and_validation(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"requirements.txt": b"bobs==1\n"})
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    for url in (
        _url(bob),  # with alice's token
        _url(alice, project_id=other_project),
        _url(alice, repository_id=str(uuid.uuid4())),
    ):
        response = await client.get(url, headers=alice.headers)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert (await client.get(_url(alice))).status_code == 401
    assert (
        await client.get(_url(alice, repository_id="not-a-uuid"), headers=alice.headers)
    ).status_code == 422
