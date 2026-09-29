"""Auth API tests against a dedicated, already-migrated throwaway test database.

Set TEST_DATABASE_URL (postgresql+asyncpg, database name ending in "_test") after running
`alembic upgrade head` against that database. Each test runs inside a transaction that is
rolled back afterwards; the fixture never creates or drops tables.
"""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import create_access_token, verify_password
from app.database import get_db
from app.main import app
from app.models import User

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
USER_FIELDS = {"id", "email", "created_at", "updated_at"}


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


async def _register(client: httpx.AsyncClient, email: str = "alice@example.com") -> httpx.Response:
    return await client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})


async def _login(
    client: httpx.AsyncClient, email: str = "alice@example.com", password: str = PASSWORD
) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


def _assert_no_secrets(response: httpx.Response) -> None:
    assert PASSWORD not in response.text
    assert '"password":' not in response.text  # no password field
    assert "password_hash" not in response.text
    assert "$argon2" not in response.text


# --- Registration ------------------------------------------------------------------------


async def test_register_succeeds_and_returns_only_public_fields(client: httpx.AsyncClient) -> None:
    response = await _register(client)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == USER_FIELDS
    assert body["email"] == "alice@example.com"
    uuid.UUID(body["id"])
    _assert_no_secrets(response)


async def test_register_normalizes_email(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await _register(client, "  Alice@Example.COM ")
    assert response.status_code == 201
    assert response.json()["email"] == "alice@example.com"
    stored = await db_session.scalar(select(User.email))
    assert stored == "alice@example.com"
    assert (await _login(client, "ALICE@example.com")).status_code == 200


async def test_duplicate_registration_fails_safely(client: httpx.AsyncClient) -> None:
    assert (await _register(client)).status_code == 201
    response = await _register(client, "ALICE@EXAMPLE.COM")
    assert response.status_code == 409
    assert response.json() == {"detail": "An account with this email already exists"}
    _assert_no_secrets(response)


async def test_password_is_stored_only_as_argon2_hash(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await _register(client)
    user = await db_session.scalar(select(User).where(User.email == "alice@example.com"))
    assert user is not None
    assert user.password_hash != PASSWORD
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password(PASSWORD, user.password_hash)


async def test_short_password_rejected_without_echoing_it(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"email": "bob@example.com", "password": "Sh0rt!"}
    )
    assert response.status_code == 422
    assert "Sh0rt!" not in response.text


async def test_invalid_email_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"email": "not-an-email", "password": PASSWORD}
    )
    assert response.status_code == 422
    assert PASSWORD not in response.text


# --- Login -------------------------------------------------------------------------------


async def test_valid_login_returns_bearer_token(client: httpx.AsyncClient) -> None:
    registered = (await _register(client)).json()
    response = await _login(client)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "token_type"}
    assert body["token_type"] == "bearer"
    claims = jwt.decode(
        body["access_token"],
        get_settings().secret_key.get_secret_value(),
        algorithms=[get_settings().jwt_algorithm],
    )
    assert claims["sub"] == registered["id"]
    _assert_no_secrets(response)


async def test_wrong_password_and_unknown_email_fail_identically(
    client: httpx.AsyncClient,
) -> None:
    await _register(client)
    wrong_password = await _login(client, password="wrong-password-123")
    unknown_email = await _login(client, email="nobody@example.com")
    for response in (wrong_password, unknown_email):
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid email or password"}
        _assert_no_secrets(response)
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.headers.get("www-authenticate") == unknown_email.headers.get(
        "www-authenticate"
    )


# --- Current user ------------------------------------------------------------------------


def _expired_token(user_id: str) -> str:
    settings = get_settings()
    payload = {"sub": user_id, "exp": datetime.now(timezone.utc) - timedelta(minutes=1)}
    return jwt.encode(
        payload, settings.secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )


async def _me(client: httpx.AsyncClient, authorization: str | None) -> httpx.Response:
    headers = {"Authorization": authorization} if authorization is not None else {}
    return await client.get("/api/v1/auth/me", headers=headers)


async def test_me_without_token_fails(client: httpx.AsyncClient) -> None:
    response = await _me(client, None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("authorization", ["Bearer not-a-jwt", "Bearer ", "Basic abc", "abc"])
async def test_me_with_malformed_token_fails(
    client: httpx.AsyncClient, authorization: str
) -> None:
    assert (await _me(client, authorization)).status_code == 401


async def test_me_with_expired_token_fails(client: httpx.AsyncClient) -> None:
    user_id = (await _register(client)).json()["id"]
    assert (await _me(client, f"Bearer {_expired_token(user_id)}")).status_code == 401


async def test_me_with_token_for_unknown_user_fails(client: httpx.AsyncClient) -> None:
    token = create_access_token(uuid.uuid4())
    assert (await _me(client, f"Bearer {token}")).status_code == 401


async def test_me_with_valid_token_returns_user(client: httpx.AsyncClient) -> None:
    registered = (await _register(client)).json()
    token = (await _login(client)).json()["access_token"]
    response = await _me(client, f"Bearer {token}")
    assert response.status_code == 200
    assert response.json() == registered
    assert set(response.json()) == USER_FIELDS
    _assert_no_secrets(response)
