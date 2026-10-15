"""Password reset by email: token security, email sending, configuration and the API flow.

Token, email and configuration tests run offline (SMTP is replaced by a fake; nothing is
sent). API tests use the already-migrated throwaway test database (TEST_DATABASE_URL,
"_test") inside rolled-back transactions; the email sender is replaced to capture links.
"""

import logging
import os
import smtplib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1.routes import auth as auth_routes
from app.config import ConfigurationError, Settings, get_settings
from app.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.database import get_db
from app.main import app
from app.models import User
from app.services import password_reset
from app.services.password_reset import (
    InvalidResetToken,
    create_reset_token,
    password_fingerprint,
    read_reset_token,
    reset_link,
    send_reset_email,
)
from app.services.rate_limit import (
    RESET_CONFIRM_IP,
    RESET_REQUEST_EMAIL,
    RESET_REQUEST_IP,
    Limit,
    RateLimiter,
    get_rate_limiter,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

PASSWORD = "test-only-Passw0rd!"
NEW_PASSWORD = "test-only-N3w-Passw0rd!"
BASE_URL = "https://app.example.test"
SMTP = {
    "smtp_host": "smtp.example.test",
    "smtp_port": 587,
    "smtp_username": "sender@example.test",
    "smtp_password": SecretStr("test-only-app-password"),
    "smtp_from": "sender@example.test",
    "app_base_url": BASE_URL,
}


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"secret_key": SecretStr("s" * 40), **SMTP, **overrides}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = get_settings()
    for name, value in SMTP.items():
        monkeypatch.setattr(settings, name, value)
    return settings


# --- Tokens ----------------------------------------------------------------------------------


def test_a_token_carries_the_user_and_the_password_fingerprint() -> None:
    settings = _settings()
    user_id = uuid.uuid4()
    token = create_reset_token(settings, user_id, "$argon2id$hash-one")
    assert read_reset_token(settings, token) == (
        user_id,
        password_fingerprint("$argon2id$hash-one"),
    )
    assert "hash-one" not in token  # only a fingerprint, never the hash


def test_the_fingerprint_changes_with_the_password() -> None:
    assert password_fingerprint("$argon2id$a") != password_fingerprint("$argon2id$b")
    assert len(password_fingerprint("$argon2id$a")) == 32


def test_expired_tokens_are_rejected() -> None:
    settings = _settings()
    payload = {
        "sub": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        "purpose": "password-reset",
        "pwd": "x" * 32,
    }
    token = jwt.encode(payload, password_reset._signing_key(settings), algorithm="HS256")
    with pytest.raises(InvalidResetToken):
        read_reset_token(settings, token)


def test_tokens_expire_after_the_configured_minutes() -> None:
    settings = _settings(password_reset_token_minutes=30)
    token = create_reset_token(settings, uuid.uuid4(), "h")
    exp = jwt.decode(token, options={"verify_signature": False})["exp"]
    remaining = exp - datetime.now(timezone.utc).timestamp()
    assert 29 * 60 < remaining <= 30 * 60


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c"])
def test_malformed_tokens_are_rejected(token: str) -> None:
    with pytest.raises(InvalidResetToken):
        read_reset_token(_settings(), token)


def test_tokens_signed_with_another_secret_are_rejected() -> None:
    token = create_reset_token(_settings(secret_key=SecretStr("a" * 40)), uuid.uuid4(), "h")
    with pytest.raises(InvalidResetToken):
        read_reset_token(_settings(secret_key=SecretStr("b" * 40)), token)


def test_a_wrong_purpose_is_rejected() -> None:
    settings = _settings()
    payload = {
        "sub": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "purpose": "something-else",
        "pwd": "x" * 32,
    }
    token = jwt.encode(payload, password_reset._signing_key(settings), algorithm="HS256")
    with pytest.raises(InvalidResetToken):
        read_reset_token(settings, token)


def test_reset_tokens_and_access_tokens_never_substitute_for_each_other() -> None:
    settings = get_settings()
    user_id = uuid.uuid4()
    with pytest.raises(InvalidTokenError):
        decode_access_token(create_reset_token(settings, user_id, "h"))
    with pytest.raises(InvalidResetToken):
        read_reset_token(settings, create_access_token(user_id))


def test_the_reset_link_puts_the_token_in_the_fragment() -> None:
    assert reset_link(_settings(), "TOKEN") == f"{BASE_URL}/reset-password#token=TOKEN"


# --- Configuration ---------------------------------------------------------------------------


def test_the_feature_is_enabled_only_when_fully_configured() -> None:
    assert _settings().password_reset_enabled is True
    for missing in ("smtp_host", "smtp_username", "smtp_password", "smtp_from", "app_base_url"):
        assert _settings(**{missing: None}).password_reset_enabled is False, missing
    assert _settings(smtp_password=SecretStr("  ")).password_reset_enabled is False


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("https://app.example.test/", "https://app.example.test"), ("  ", None), ("", None)],
)
def test_app_base_url_is_normalized(raw: str, stored: str | None) -> None:
    assert _settings(app_base_url=raw).app_base_url == stored


@pytest.mark.parametrize("raw", ["ftp://x.test", "app.example.test", "https://x.test/path"])
def test_invalid_app_base_urls_are_rejected(raw: str) -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic's ValidationError
        _settings(app_base_url=raw)


def test_get_settings_errors_never_include_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "ftp://SECRET-MARKER.test")
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as raised:
            get_settings()
        assert "APP_BASE_URL" in str(raised.value)
        assert "SECRET-MARKER" not in str(raised.value)
    finally:
        monkeypatch.delenv("APP_BASE_URL")
        get_settings.cache_clear()


# --- Email -----------------------------------------------------------------------------------


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []
    fail_with: Exception | None = None

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list[Any] = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *args: Any) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any) -> None:
        self.calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(f"login:{username}:{password}")
        if _FakeSMTP.fail_with:
            raise _FakeSMTP.fail_with

    def send_message(self, message: Any) -> None:
        self.calls.append("send")
        self.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSMTP]:
    _FakeSMTP.instances = []
    _FakeSMTP.fail_with = None
    monkeypatch.setattr(password_reset.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def test_the_email_is_sent_over_starttls_with_the_link(fake_smtp: type[_FakeSMTP]) -> None:
    link = f"{BASE_URL}/reset-password#token=TOKEN"
    send_reset_email(_settings(), "user@example.test", link)
    (smtp,) = fake_smtp.instances
    assert (smtp.host, smtp.port) == ("smtp.example.test", 587)
    assert smtp.calls == [
        "starttls",
        "login:sender@example.test:test-only-app-password",
        "send",
        "quit",
    ]
    (message,) = smtp.sent
    assert message["To"] == "user@example.test"
    assert message["From"] == "AI Software Engineer <sender@example.test>"
    assert "Reset your" in message["Subject"]
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    assert link in plain and link in html
    assert "30 minutes" in plain


def test_nothing_is_sent_when_the_feature_is_disabled(fake_smtp: type[_FakeSMTP]) -> None:
    send_reset_email(_settings(smtp_host=None), "user@example.test", "link")
    assert fake_smtp.instances == []


def test_smtp_failures_are_logged_without_details(
    fake_smtp: type[_FakeSMTP], caplog: pytest.LogCaptureFixture
) -> None:
    fake_smtp.fail_with = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with caplog.at_level(logging.WARNING):
        send_reset_email(_settings(), "user@example.test", "https://x.test/reset#token=SECRETTOKEN")
    assert "could not be sent" in caplog.text
    for secret in ("user@example.test", "SECRETTOKEN", "test-only-app-password"):
        assert secret not in caplog.text


# --- API ---------------------------------------------------------------------------------------


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


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        auth_routes, "send_reset_email", lambda settings, to, link: sent.append((to, link))
    )
    return sent


async def _register(client: httpx.AsyncClient, email: str = "alice@example.com") -> None:
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201


async def _login_status(client: httpx.AsyncClient, password: str) -> int:
    response = await client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": password}
    )
    return response.status_code


async def _request(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post("/api/v1/auth/password-reset/request", json={"email": email})


async def _confirm(
    client: httpx.AsyncClient, token: str, password: str = NEW_PASSWORD
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": token, "password": password}
    )


def _token(link: str) -> str:
    assert link.startswith(f"{BASE_URL}/reset-password#token=")
    return link.split("#token=", 1)[1]


@requires_db
async def test_requests_answer_503_when_email_is_not_configured(
    client: httpx.AsyncClient, outbox: list[tuple[str, str]]
) -> None:
    response = await _request(client, "alice@example.com")
    assert (response.status_code, response.json()) == (
        503,
        {"detail": "Password reset by email is not available"},
    )
    assert outbox == []


@requires_db
async def test_known_and_unknown_emails_get_the_same_answer(
    client: httpx.AsyncClient, enabled: Settings, outbox: list[tuple[str, str]]
) -> None:
    await _register(client)
    known = await _request(client, "  Alice@Example.com ")
    unknown = await _request(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 202
    assert (
        known.json()
        == unknown.json()
        == {"detail": "If an account exists for that email, a reset link is on its way"}
    )
    assert [to for to, _ in outbox] == ["alice@example.com"]  # only the real account is emailed


@requires_db
async def test_the_full_reset_flow(
    client: httpx.AsyncClient, enabled: Settings, outbox: list[tuple[str, str]]
) -> None:
    await _register(client)
    await _request(client, "alice@example.com")
    token = _token(outbox[0][1])
    response = await _confirm(client, token)
    assert (response.status_code, response.json()) == (
        200,
        {"detail": "Your password has been reset; you can now sign in"},
    )
    assert await _login_status(client, NEW_PASSWORD) == 200
    assert await _login_status(client, PASSWORD) == 401
    assert NEW_PASSWORD not in response.text


@requires_db
async def test_a_link_works_only_once(
    client: httpx.AsyncClient, enabled: Settings, outbox: list[tuple[str, str]]
) -> None:
    await _register(client)
    await _request(client, "alice@example.com")
    await _request(client, "alice@example.com")
    first, second = (_token(link) for _, link in outbox)
    assert (await _confirm(client, first)).status_code == 200
    for token in (first, second):  # the password changed, so every earlier link is dead
        response = await _confirm(client, token, "another-Passw0rd!")
        assert (response.status_code, response.json()) == (
            400,
            {"detail": "This reset link is invalid or has expired; request a new one"},
        )
    assert await _login_status(client, NEW_PASSWORD) == 200


@requires_db
async def test_invalid_and_foreign_tokens_are_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession, enabled: Settings
) -> None:
    await _register(client)
    user = await db_session.scalar(select(User).where(User.email == "alice@example.com"))
    assert user is not None
    for token in (
        "garbage",
        create_access_token(user.id),  # a login token is not a reset token
        create_reset_token(enabled, uuid.uuid4(), user.password_hash),  # unknown user
    ):
        assert (await _confirm(client, token)).status_code == 400
    assert await _login_status(client, PASSWORD) == 200


@requires_db
async def test_short_passwords_are_rejected_without_echo(
    client: httpx.AsyncClient, db_session: AsyncSession, enabled: Settings
) -> None:
    await _register(client)
    user = await db_session.scalar(select(User).where(User.email == "alice@example.com"))
    assert user is not None
    token = create_reset_token(enabled, user.id, user.password_hash)
    response = await _confirm(client, token, "Sh0rt!")
    assert response.status_code == 422
    assert "Sh0rt!" not in response.text
    assert verify_password(PASSWORD, user.password_hash)


@requires_db
async def test_validation_errors(client: httpx.AsyncClient, enabled: Settings) -> None:
    assert (await _request(client, "not-an-email")).status_code == 422
    for body in ({"token": "x"}, {}):
        response = await client.post("/api/v1/auth/password-reset/confirm", json=body)
        assert response.status_code == 422, body
        assert NEW_PASSWORD not in response.text


class _RecordingLimiter(RateLimiter):
    def __init__(self) -> None:
        super().__init__(None)
        self.hits: list[tuple[Limit, str]] = []

    async def hit(self, limit: Limit, value: str) -> None:
        self.hits.append((limit, value))


@requires_db
async def test_rate_limits_cover_requests_and_confirmations(
    client: httpx.AsyncClient, enabled: Settings, outbox: list[tuple[str, str]]
) -> None:
    limiter = _RecordingLimiter()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    await _request(client, "Bob@Example.com")
    await _confirm(client, "garbage")
    assert [(limit, value) for limit, value in limiter.hits if limit is not RESET_REQUEST_IP] == [
        (RESET_REQUEST_EMAIL, "bob@example.com"),
        (RESET_CONFIRM_IP, "127.0.0.1"),
    ]
    assert [limit for limit, _ in limiter.hits][0] is RESET_REQUEST_IP
    assert RESET_REQUEST_IP == Limit("reset-request-ip", 5, 3600)
    assert RESET_REQUEST_EMAIL == Limit("reset-request-email", 3, 3600)
    assert RESET_CONFIRM_IP == Limit("reset-confirm-ip", 10, 900)


def test_hash_password_produces_argon2_hashes() -> None:  # the reset path reuses it unchanged
    assert hash_password(NEW_PASSWORD).startswith("$argon2id$")
