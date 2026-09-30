"""Project CRUD and ownership tests against a dedicated, already-migrated throwaway test database.

Set TEST_DATABASE_URL (postgresql+asyncpg, database name ending in "_test") after running
`alembic upgrade head` against that database. Each test runs inside a transaction that is
rolled back afterwards; the fixture never creates or drops tables.
"""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import get_db
from app.main import app
from app.models import Project, User

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
PROJECT_FIELDS = {"id", "name", "description", "created_at", "updated_at"}
NOT_FOUND = {"detail": "Project not found"}


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


async def _auth_headers(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    registered = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert registered.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> dict[str, str]:
    return await _auth_headers(client, "alice@example.com")


@pytest.fixture
async def bob(client: httpx.AsyncClient) -> dict[str, str]:
    return await _auth_headers(client, "bob@example.com")


async def _create(
    client: httpx.AsyncClient, headers: dict[str, str], name: str = "Alpha", **extra: object
) -> httpx.Response:
    return await client.post("/api/v1/projects", json={"name": name, **extra}, headers=headers)


def _assert_safe(response: httpx.Response) -> None:
    for marker in ("password", "$argon2", "secret", "postgresql", "Bearer", "Traceback", "SELECT"):
        assert marker not in response.text


# --- Authentication ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/projects"),
        ("GET", "/api/v1/projects"),
        ("GET", f"/api/v1/projects/{uuid.uuid4()}"),
        ("PATCH", f"/api/v1/projects/{uuid.uuid4()}"),
        ("DELETE", f"/api/v1/projects/{uuid.uuid4()}"),
    ],
)
async def test_unauthenticated_requests_fail(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    body = {"name": "x"} if method in ("POST", "PATCH") else None
    response = await client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_invalid_token_fails(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/v1/projects", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401


# --- Create ------------------------------------------------------------------------------


async def test_create_project(client: httpx.AsyncClient, alice: dict[str, str]) -> None:
    response = await _create(client, alice, "  My Project  ", description="First one")
    assert response.status_code == 201
    body = response.json()
    assert set(body) == PROJECT_FIELDS
    assert body["name"] == "My Project"
    assert body["description"] == "First one"
    uuid.UUID(body["id"])
    _assert_safe(response)


async def test_create_without_description(
    client: httpx.AsyncClient, alice: dict[str, str]
) -> None:
    response = await _create(client, alice)
    assert response.status_code == 201
    assert response.json()["description"] is None


async def test_ownership_comes_from_authenticated_user(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: dict[str, str]
) -> None:
    project_id = (await _create(client, alice)).json()["id"]
    owner_email = await db_session.scalar(
        select(User.email).join(Project).where(Project.id == uuid.UUID(project_id))
    )
    assert owner_email == "alice@example.com"


@pytest.mark.parametrize("field", ["user_id", "owner_id", "owner", "id", "created_at"])
async def test_client_cannot_supply_owner_or_server_fields(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: dict[str, str],
    bob: dict[str, str],
    field: str,
) -> None:
    bob_id = await db_session.scalar(select(User.id).where(User.email == "bob@example.com"))
    response = await _create(client, alice, **{field: str(bob_id)})
    assert response.status_code == 422
    assert await db_session.scalar(select(Project.id)) is None  # nothing was created


# --- List --------------------------------------------------------------------------------


async def test_list_returns_only_own_projects(
    client: httpx.AsyncClient, alice: dict[str, str], bob: dict[str, str]
) -> None:
    alice_ids = {(await _create(client, alice, name)).json()["id"] for name in ("A1", "A2")}
    bob_id = (await _create(client, bob, "B1")).json()["id"]

    alice_list = await client.get("/api/v1/projects", headers=alice)
    assert alice_list.status_code == 200
    assert {p["id"] for p in alice_list.json()} == alice_ids
    assert bob_id not in alice_list.text
    assert all(set(p) == PROJECT_FIELDS for p in alice_list.json())
    _assert_safe(alice_list)

    bob_list = await client.get("/api/v1/projects", headers=bob)
    assert [p["id"] for p in bob_list.json()] == [bob_id]


async def test_list_is_newest_first_and_deterministic(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: dict[str, str]
) -> None:
    ids = [(await _create(client, alice, name)).json()["id"] for name in ("P1", "P2", "P3")]
    # Rows created in one transaction share now(); give them distinct creation times.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for offset, project_id in enumerate(ids):
        await db_session.execute(
            update(Project)
            .where(Project.id == uuid.UUID(project_id))
            .values(created_at=base + timedelta(minutes=offset))
        )
    await db_session.commit()

    first = (await client.get("/api/v1/projects", headers=alice)).json()
    second = (await client.get("/api/v1/projects", headers=alice)).json()
    assert [p["id"] for p in first] == list(reversed(ids))
    assert first == second


async def test_list_empty_for_new_user(client: httpx.AsyncClient, alice: dict[str, str]) -> None:
    response = await client.get("/api/v1/projects", headers=alice)
    assert response.status_code == 200
    assert response.json() == []


# --- Get ---------------------------------------------------------------------------------


async def test_owner_can_get_project(client: httpx.AsyncClient, alice: dict[str, str]) -> None:
    created = (await _create(client, alice, description="d")).json()
    response = await client.get(f"/api/v1/projects/{created['id']}", headers=alice)
    assert response.status_code == 200
    assert response.json() == created
    _assert_safe(response)


async def test_get_nonexistent_project_is_not_found(
    client: httpx.AsyncClient, alice: dict[str, str]
) -> None:
    response = await client.get(f"/api/v1/projects/{uuid.uuid4()}", headers=alice)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_other_users_project_looks_not_found(
    client: httpx.AsyncClient, alice: dict[str, str], bob: dict[str, str]
) -> None:
    bob_project = (await _create(client, bob, "Secret")).json()["id"]
    foreign = await client.get(f"/api/v1/projects/{bob_project}", headers=alice)
    missing = await client.get(f"/api/v1/projects/{uuid.uuid4()}", headers=alice)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == NOT_FOUND
    assert "Secret" not in foreign.text


# --- Update ------------------------------------------------------------------------------


async def test_owner_can_update_name(client: httpx.AsyncClient, alice: dict[str, str]) -> None:
    project = (await _create(client, alice, description="keep me")).json()
    response = await client.patch(
        f"/api/v1/projects/{project['id']}", json={"name": "  Renamed "}, headers=alice
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert response.json()["description"] == "keep me"  # omitted field preserved
    _assert_safe(response)


async def test_owner_can_update_and_clear_description(
    client: httpx.AsyncClient, alice: dict[str, str]
) -> None:
    project = (await _create(client, alice, "Keep Name")).json()
    url = f"/api/v1/projects/{project['id']}"
    updated = await client.patch(url, json={"description": "new"}, headers=alice)
    assert updated.status_code == 200
    assert updated.json()["description"] == "new"
    assert updated.json()["name"] == "Keep Name"  # omitted field preserved
    cleared = await client.patch(url, json={"description": None}, headers=alice)
    assert cleared.json()["description"] is None


async def test_empty_patch_changes_nothing(
    client: httpx.AsyncClient, alice: dict[str, str]
) -> None:
    project = (await _create(client, alice, description="d")).json()
    response = await client.patch(f"/api/v1/projects/{project['id']}", json={}, headers=alice)
    assert response.status_code == 200
    assert response.json() == project


async def test_other_user_cannot_update_project(
    client: httpx.AsyncClient, alice: dict[str, str], bob: dict[str, str]
) -> None:
    bob_project = (await _create(client, bob, "Bob's")).json()
    response = await client.patch(
        f"/api/v1/projects/{bob_project['id']}", json={"name": "Hijacked"}, headers=alice
    )
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    unchanged = await client.get(f"/api/v1/projects/{bob_project['id']}", headers=bob)
    assert unchanged.json()["name"] == "Bob's"


@pytest.mark.parametrize("field", ["user_id", "owner_id", "id", "created_at", "updated_at"])
async def test_owner_cannot_transfer_ownership_or_set_server_fields(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: dict[str, str],
    bob: dict[str, str],
    field: str,
) -> None:
    project_id = (await _create(client, alice)).json()["id"]
    bob_id = await db_session.scalar(select(User.id).where(User.email == "bob@example.com"))
    response = await client.patch(
        f"/api/v1/projects/{project_id}", json={field: str(bob_id)}, headers=alice
    )
    assert response.status_code == 422
    owner_email = await db_session.scalar(
        select(User.email).join(Project).where(Project.id == uuid.UUID(project_id))
    )
    assert owner_email == "alice@example.com"


# --- Delete ------------------------------------------------------------------------------


async def test_owner_can_delete_project(client: httpx.AsyncClient, alice: dict[str, str]) -> None:
    project_id = (await _create(client, alice)).json()["id"]
    response = await client.delete(f"/api/v1/projects/{project_id}", headers=alice)
    assert response.status_code == 204
    assert response.content == b""
    after = await client.get(f"/api/v1/projects/{project_id}", headers=alice)
    assert after.status_code == 404
    assert (await client.get("/api/v1/projects", headers=alice)).json() == []


async def test_other_user_cannot_delete_project(
    client: httpx.AsyncClient, alice: dict[str, str], bob: dict[str, str]
) -> None:
    bob_project = (await _create(client, bob)).json()["id"]
    response = await client.delete(f"/api/v1/projects/{bob_project}", headers=alice)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    still_there = await client.get(f"/api/v1/projects/{bob_project}", headers=bob)
    assert still_there.status_code == 200


# --- Validation --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "x" * 101])
async def test_invalid_names_rejected_on_create(
    client: httpx.AsyncClient, alice: dict[str, str], name: str
) -> None:
    assert (await _create(client, alice, name)).status_code == 422


async def test_max_length_name_accepted(client: httpx.AsyncClient, alice: dict[str, str]) -> None:
    assert (await _create(client, alice, "x" * 100)).status_code == 201


async def test_missing_name_rejected(client: httpx.AsyncClient, alice: dict[str, str]) -> None:
    response = await client.post("/api/v1/projects", json={"description": "d"}, headers=alice)
    assert response.status_code == 422


@pytest.mark.parametrize("name", ["", "   ", "x" * 101, None])
async def test_invalid_names_rejected_on_update(
    client: httpx.AsyncClient, alice: dict[str, str], name: str | None
) -> None:
    project = (await _create(client, alice, "Original")).json()
    url = f"/api/v1/projects/{project['id']}"
    assert (await client.patch(url, json={"name": name}, headers=alice)).status_code == 422
    assert (await client.get(url, headers=alice)).json()["name"] == "Original"


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123", "' OR 1=1 --"])
@pytest.mark.parametrize("method", ["GET", "PATCH", "DELETE"])
async def test_invalid_project_uuid_handled_safely(
    client: httpx.AsyncClient, alice: dict[str, str], bad_id: str, method: str
) -> None:
    body = {"name": "x"} if method == "PATCH" else None
    response = await client.request(method, f"/api/v1/projects/{bad_id}", json=body, headers=alice)
    assert response.status_code == 422
    assert "Traceback" not in response.text
    assert "SELECT" not in response.text
