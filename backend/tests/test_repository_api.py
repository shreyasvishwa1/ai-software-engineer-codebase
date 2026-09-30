"""Repository CRUD and ownership tests against a dedicated, already-migrated throwaway test database.

Set TEST_DATABASE_URL (postgresql+asyncpg, database name ending in "_test") after running
`alembic upgrade head` against that database. Each test runs inside a transaction that is
rolled back afterwards; the fixture never creates or drops tables.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import get_db
from app.main import app
from app.models import Repository

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
REPOSITORY_FIELDS = {
    "id",
    "project_id",
    "name",
    "source_type",
    "source_url",
    "default_branch",
    "status",
    "created_at",
    "updated_at",
}
REPOSITORY_NOT_FOUND = {"detail": "Repository not found"}
PROJECT_NOT_FOUND = {"detail": "Project not found"}
GITHUB_REPO = {
    "name": "backend",
    "source_type": "github",
    "source_url": "https://github.com/example/backend",
    "default_branch": "main",
}


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
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
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


async def _owner(client: httpx.AsyncClient, email: str) -> Owner:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project = await client.post("/api/v1/projects", json={"name": "Main"}, headers=headers)
    return Owner(headers=headers, project_id=project.json()["id"])


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com")


@pytest.fixture
async def bob(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "bob@example.com")


@pytest.fixture
async def alice_other_project(client: httpx.AsyncClient, alice: Owner) -> str:
    response = await client.post(
        "/api/v1/projects", json={"name": "Second"}, headers=alice.headers
    )
    return str(response.json()["id"])


def _url(project_id: str, repository_id: str | None = None) -> str:
    base = f"/api/v1/projects/{project_id}/repositories"
    return f"{base}/{repository_id}" if repository_id else base


async def _create(
    client: httpx.AsyncClient, owner: Owner, in_project: str | None = None, **fields: object
) -> httpx.Response:
    body = {**GITHUB_REPO, **fields}
    return await client.post(_url(in_project or owner.project_id), json=body, headers=owner.headers)


def _assert_safe(response: httpx.Response) -> None:
    for marker in ("password", "$argon2", "secret", "postgresql", "Bearer", "Traceback", "SELECT"):
        assert marker not in response.text
    assert "user_id" not in response.text


# --- Authentication ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "has_repository_id"),
    [("POST", False), ("GET", False), ("GET", True), ("PATCH", True), ("DELETE", True)],
)
async def test_unauthenticated_requests_fail(
    client: httpx.AsyncClient, alice: Owner, method: str, has_repository_id: bool
) -> None:
    created = (await _create(client, alice)).json()["id"]
    url = _url(alice.project_id, created if has_repository_id else None)
    body = GITHUB_REPO if method == "POST" else ({"name": "x"} if method == "PATCH" else None)
    response = await client.request(method, url, json=body)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


# --- Create ------------------------------------------------------------------------------


async def test_create_repository(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _create(client, alice, name="  backend  ", default_branch=" main ")
    assert response.status_code == 201
    body = response.json()
    assert set(body) == REPOSITORY_FIELDS
    assert body["project_id"] == alice.project_id
    assert body["name"] == "backend"
    assert body["default_branch"] == "main"
    assert body["source_type"] == "github"
    assert body["source_url"] == GITHUB_REPO["source_url"]
    assert body["status"] == "pending"  # server-managed default
    uuid.UUID(body["id"])
    _assert_safe(response)


async def test_create_zip_repository_with_only_required_fields(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    response = await client.post(
        _url(alice.project_id),
        json={"name": "upload", "source_type": "zip"},
        headers=alice.headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["source_type"], body["source_url"], body["default_branch"]) == ("zip", None, None)


async def test_create_for_nonexistent_project(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _create(client, alice, in_project=str(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json() == PROJECT_NOT_FOUND


async def test_create_for_other_users_project(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, bob: Owner
) -> None:
    response = await _create(client, alice, in_project=bob.project_id)
    assert response.status_code == 404
    assert response.json() == PROJECT_NOT_FOUND
    assert await db_session.scalar(select(Repository.id)) is None


# --- List --------------------------------------------------------------------------------


async def test_list_repositories(client: httpx.AsyncClient, alice: Owner) -> None:
    created = {(await _create(client, alice, name=n)).json()["id"] for n in ("r1", "r2")}
    response = await client.get(_url(alice.project_id), headers=alice.headers)
    assert response.status_code == 200
    assert {r["id"] for r in response.json()} == created
    assert all(set(r) == REPOSITORY_FIELDS for r in response.json())
    _assert_safe(response)


async def test_list_empty(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await client.get(_url(alice.project_id), headers=alice.headers)
    assert response.status_code == 200
    assert response.json() == []


async def test_list_only_own_project_repositories(
    client: httpx.AsyncClient, alice: Owner, bob: Owner, alice_other_project: str
) -> None:
    mine = (await _create(client, alice, name="mine")).json()["id"]
    await _create(client, alice, in_project=alice_other_project, name="other project")
    bobs = (await _create(client, bob, name="bobs")).json()["id"]

    response = await client.get(_url(alice.project_id), headers=alice.headers)
    assert [r["id"] for r in response.json()] == [mine]
    assert bobs not in response.text


async def test_list_other_users_project_is_not_found(
    client: httpx.AsyncClient, alice: Owner, bob: Owner
) -> None:
    await _create(client, bob, name="bobs")
    foreign = await client.get(_url(bob.project_id), headers=alice.headers)
    missing = await client.get(_url(str(uuid.uuid4())), headers=alice.headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == PROJECT_NOT_FOUND


async def test_list_ordering_is_newest_first_and_deterministic(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    ids = [(await _create(client, alice, name=n)).json()["id"] for n in ("r1", "r2", "r3")]
    # Rows created in one transaction share now(); give them distinct creation times.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for offset, repository_id in enumerate(ids):
        await db_session.execute(
            update(Repository)
            .where(Repository.id == uuid.UUID(repository_id))
            .values(created_at=base + timedelta(minutes=offset))
        )
    await db_session.commit()

    first = (await client.get(_url(alice.project_id), headers=alice.headers)).json()
    second = (await client.get(_url(alice.project_id), headers=alice.headers)).json()
    assert [r["id"] for r in first] == list(reversed(ids))
    assert first == second


# --- Get ---------------------------------------------------------------------------------


async def test_get_repository(client: httpx.AsyncClient, alice: Owner) -> None:
    created = (await _create(client, alice)).json()
    response = await client.get(_url(alice.project_id, created["id"]), headers=alice.headers)
    assert response.status_code == 200
    assert response.json() == created
    _assert_safe(response)


async def test_get_nonexistent_repository(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await client.get(_url(alice.project_id, str(uuid.uuid4())), headers=alice.headers)
    assert response.status_code == 404
    assert response.json() == REPOSITORY_NOT_FOUND


async def test_get_other_users_repository_looks_missing(
    client: httpx.AsyncClient, alice: Owner, bob: Owner
) -> None:
    bobs = (await _create(client, bob, name="bob-secret-repo")).json()["id"]
    attempts = [
        _url(bob.project_id, bobs),  # Bob's project + Bob's repository
        _url(alice.project_id, bobs),  # Alice's project + Bob's repository
    ]
    for url in attempts:
        response = await client.get(url, headers=alice.headers)
        assert response.status_code == 404
        assert response.json() == REPOSITORY_NOT_FOUND
        assert "bob-secret-repo" not in response.text


async def test_get_repository_under_wrong_project(
    client: httpx.AsyncClient, alice: Owner, alice_other_project: str
) -> None:
    repository_id = (await _create(client, alice)).json()["id"]
    response = await client.get(_url(alice_other_project, repository_id), headers=alice.headers)
    assert response.status_code == 404
    assert response.json() == REPOSITORY_NOT_FOUND


async def test_get_with_nonexistent_project(client: httpx.AsyncClient, alice: Owner) -> None:
    repository_id = (await _create(client, alice)).json()["id"]
    response = await client.get(_url(str(uuid.uuid4()), repository_id), headers=alice.headers)
    assert response.status_code == 404
    assert response.json() == REPOSITORY_NOT_FOUND


# --- Update ------------------------------------------------------------------------------


async def test_update_repository(client: httpx.AsyncClient, alice: Owner) -> None:
    created = (await _create(client, alice)).json()
    response = await client.patch(
        _url(alice.project_id, created["id"]),
        json={
            "name": " api ",
            "source_url": "https://github.com/example/api",
            "default_branch": "develop",
        },
        headers=alice.headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["source_url"], body["default_branch"]) == (
        "api",
        "https://github.com/example/api",
        "develop",
    )
    assert body["source_type"] == created["source_type"]
    assert body["status"] == created["status"]
    _assert_safe(response)


async def test_partial_update_preserves_omitted_fields(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    created = (await _create(client, alice)).json()
    response = await client.patch(
        _url(alice.project_id, created["id"]), json={"name": "renamed"}, headers=alice.headers
    )
    body = response.json()
    assert body["name"] == "renamed"
    for field in ("source_type", "source_url", "default_branch", "status", "project_id", "id"):
        assert body[field] == created[field]


async def test_update_can_clear_optional_fields(client: httpx.AsyncClient, alice: Owner) -> None:
    created = (await _create(client, alice)).json()
    response = await client.patch(
        _url(alice.project_id, created["id"]),
        json={"source_url": None, "default_branch": None},
        headers=alice.headers,
    )
    assert response.status_code == 200
    assert (response.json()["source_url"], response.json()["default_branch"]) == (None, None)
    assert response.json()["name"] == created["name"]


async def test_empty_patch_changes_nothing(client: httpx.AsyncClient, alice: Owner) -> None:
    created = (await _create(client, alice)).json()
    response = await client.patch(
        _url(alice.project_id, created["id"]), json={}, headers=alice.headers
    )
    assert response.status_code == 200
    assert response.json() == created


async def test_update_nonexistent_repository(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await client.patch(
        _url(alice.project_id, str(uuid.uuid4())), json={"name": "x"}, headers=alice.headers
    )
    assert response.status_code == 404
    assert response.json() == REPOSITORY_NOT_FOUND


async def test_update_other_users_repository(
    client: httpx.AsyncClient, alice: Owner, bob: Owner
) -> None:
    bobs = (await _create(client, bob, name="bobs")).json()
    for project_id in (bob.project_id, alice.project_id):
        response = await client.patch(
            _url(project_id, bobs["id"]), json={"name": "hijacked"}, headers=alice.headers
        )
        assert response.status_code == 404
        assert response.json() == REPOSITORY_NOT_FOUND
    unchanged = await client.get(_url(bob.project_id, bobs["id"]), headers=bob.headers)
    assert unchanged.json() == bobs


async def test_update_through_wrong_project(
    client: httpx.AsyncClient, alice: Owner, alice_other_project: str
) -> None:
    created = (await _create(client, alice)).json()
    response = await client.patch(
        _url(alice_other_project, created["id"]), json={"name": "moved"}, headers=alice.headers
    )
    assert response.status_code == 404
    assert response.json() == REPOSITORY_NOT_FOUND
    still = await client.get(_url(alice.project_id, created["id"]), headers=alice.headers)
    assert still.json() == created


# --- Delete ------------------------------------------------------------------------------


async def test_delete_repository(client: httpx.AsyncClient, alice: Owner) -> None:
    repository_id = (await _create(client, alice)).json()["id"]
    response = await client.delete(_url(alice.project_id, repository_id), headers=alice.headers)
    assert response.status_code == 204
    assert response.content == b""
    after = await client.get(_url(alice.project_id, repository_id), headers=alice.headers)
    assert after.status_code == 404
    assert after.json() == REPOSITORY_NOT_FOUND
    assert (await client.get(_url(alice.project_id), headers=alice.headers)).json() == []


async def test_delete_other_users_repository(
    client: httpx.AsyncClient, alice: Owner, bob: Owner
) -> None:
    bobs = (await _create(client, bob)).json()
    for project_id in (bob.project_id, alice.project_id):
        response = await client.delete(_url(project_id, bobs["id"]), headers=alice.headers)
        assert response.status_code == 404
        assert response.json() == REPOSITORY_NOT_FOUND
    still = await client.get(_url(bob.project_id, bobs["id"]), headers=bob.headers)
    assert still.status_code == 200
    assert still.json() == bobs


async def test_delete_through_wrong_project(
    client: httpx.AsyncClient, alice: Owner, alice_other_project: str
) -> None:
    repository_id = (await _create(client, alice)).json()["id"]
    response = await client.delete(_url(alice_other_project, repository_id), headers=alice.headers)
    assert response.status_code == 404
    assert response.json() == REPOSITORY_NOT_FOUND
    still = await client.get(_url(alice.project_id, repository_id), headers=alice.headers)
    assert still.status_code == 200


# --- Validation / mass assignment --------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"unexpected": "x"},
        {"project_id": "PROJECT"},
        {"user_id": "USER"},
        {"owner_id": "USER"},
        {"id": "REPO"},
        {"status": "ready"},
        {"created_at": "2020-01-01T00:00:00Z"},
        {"updated_at": "2020-01-01T00:00:00Z"},
    ],
)
async def test_create_rejects_unknown_and_server_fields(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    bob: Owner,
    extra: dict[str, str],
) -> None:
    substitutions = {"PROJECT": bob.project_id, "USER": str(uuid.uuid4()), "REPO": str(uuid.uuid4())}
    field, value = next(iter(extra.items()))
    response = await _create(client, alice, **{field: substitutions.get(value, value)})
    assert response.status_code == 422
    assert await db_session.scalar(select(Repository.id)) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"unexpected": "x"},
        {"project_id": "PROJECT"},
        {"user_id": "USER"},
        {"id": "REPO"},
        {"status": "ready"},
        {"source_type": "zip"},
        {"created_at": "2020-01-01T00:00:00Z"},
    ],
)
async def test_update_rejects_unknown_immutable_and_server_fields(
    client: httpx.AsyncClient, alice: Owner, alice_other_project: str, extra: dict[str, str]
) -> None:
    created = (await _create(client, alice)).json()
    substitutions = {"PROJECT": alice_other_project, "USER": str(uuid.uuid4()), "REPO": str(uuid.uuid4())}
    field, value = next(iter(extra.items()))
    url = _url(alice.project_id, created["id"])
    response = await client.patch(
        url, json={field: substitutions.get(value, value)}, headers=alice.headers
    )
    assert response.status_code == 422
    assert (await client.get(url, headers=alice.headers)).json() == created


@pytest.mark.parametrize(
    "fields",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 256},
        {"default_branch": "  "},
        {"default_branch": "b" * 256},
        {"source_url": ""},
        {"source_url": "h" * 2049},
        {"source_type": "svn"},
        {"source_type": None},
    ],
)
async def test_create_field_validation(
    client: httpx.AsyncClient, alice: Owner, fields: dict[str, object]
) -> None:
    assert (await _create(client, alice, **fields)).status_code == 422


async def test_create_requires_name_and_source_type(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    for body in ({"source_type": "zip"}, {"name": "repo"}):
        response = await client.post(_url(alice.project_id), json=body, headers=alice.headers)
        assert response.status_code == 422


async def test_max_length_values_accepted(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _create(
        client, alice, name="n" * 255, default_branch="b" * 255, source_url="h" * 2048
    )
    assert response.status_code == 201


@pytest.mark.parametrize("fields", [{"name": None}, {"name": " "}, {"default_branch": "b" * 256}])
async def test_update_field_validation(
    client: httpx.AsyncClient, alice: Owner, fields: dict[str, object]
) -> None:
    created = (await _create(client, alice)).json()
    url = _url(alice.project_id, created["id"])
    assert (await client.patch(url, json=fields, headers=alice.headers)).status_code == 422
    assert (await client.get(url, headers=alice.headers)).json() == created


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123", "' OR 1=1 --"])
@pytest.mark.parametrize("method", ["GET", "PATCH", "DELETE"])
async def test_invalid_repository_uuid(
    client: httpx.AsyncClient, alice: Owner, bad_id: str, method: str
) -> None:
    body = {"name": "x"} if method == "PATCH" else None
    response = await client.request(
        method, _url(alice.project_id, bad_id), json=body, headers=alice.headers
    )
    assert response.status_code == 422
    assert "Traceback" not in response.text
    assert "SELECT" not in response.text


async def test_invalid_project_uuid(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await client.get(_url("not-a-uuid"), headers=alice.headers)
    assert response.status_code == 422
