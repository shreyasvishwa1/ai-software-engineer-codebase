"""Authentication rate limiting tests.

- Unit tests need neither database nor Redis.
- Limiter tests use a disposable Redis (TEST_REDIS_URL: loopback host and a non-zero
  database index; the database is flushed by these tests).
- API tests additionally use the throwaway PostgreSQL (TEST_DATABASE_URL, "_test") inside
  rolled-back transactions.

tests/conftest.py replaces the limiter with a no-op for the whole suite; the fixtures here
remove that override so the real dependency (and real Redis) is exercised. Window expiry is
made deterministic by shortening a key's TTL in Redis rather than by sleeping for a window.
"""

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import HTTPException
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.api.v1.routes import auth as auth_routes
from app.config import get_settings
from app.database import get_db
from app.main import app
from app.services import rate_limit
from app.services.rate_limit import (
    LOGIN_EMAIL,
    LOGIN_IP,
    REGISTER_IP,
    TOO_MANY_ATTEMPTS,
    RateLimiter,
    client_ip,
    get_rate_limiter,
    rate_limit_key,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(
    not TEST_REDIS_URL, reason="set TEST_REDIS_URL (disposable Redis) for rate-limit tests"
)
requires_db_and_redis = pytest.mark.skipif(
    not (TEST_DATABASE_URL and TEST_REDIS_URL),
    reason="set TEST_DATABASE_URL and TEST_REDIS_URL for rate-limit API tests",
)

PASSWORD = "test-only-Passw0rd!"
WRONG = "wrong-Passw0rd!"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/1"  # nothing listens on port 1
LIMITED = {"detail": TOO_MANY_ATTEMPTS}
KEY_PATTERN = re.compile(
    r"^ratelimit:(login-ip|login-email|login-email-pending|register-ip):[0-9a-f]{64}$"
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _request(client: tuple[str, int] | None, headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": raw, "client": client}
    )


# --- Unit tests (no database, no Redis) --------------------------------------------------


def test_limits_are_the_approved_constants() -> None:
    assert (LOGIN_IP.scope, LOGIN_IP.max_requests, LOGIN_IP.window_seconds) == ("login-ip", 10, 60)
    assert (LOGIN_EMAIL.scope, LOGIN_EMAIL.max_requests, LOGIN_EMAIL.window_seconds) == (
        "login-email",
        5,
        900,
    )
    assert (REGISTER_IP.scope, REGISTER_IP.max_requests, REGISTER_IP.window_seconds) == (
        "register-ip",
        5,
        3600,
    )


def test_keys_contain_only_the_sha256_digest() -> None:
    key = rate_limit_key("login-email", "alice@example.com")
    assert key == f"ratelimit:login-email:{_sha('alice@example.com')}"
    assert "alice" not in key and "example" not in key
    ip_key = rate_limit_key("login-ip", "203.0.113.7")
    assert "203.0.113.7" not in ip_key
    assert KEY_PATTERN.match(key) and KEY_PATTERN.match(ip_key)


def test_email_key_material_is_normalized() -> None:
    variants = ["alice@example.com", "  Alice@Example.COM ", "ALICE@EXAMPLE.COM\t"]
    assert {rate_limit.email_key_material(v) for v in variants} == {"alice@example.com"}


def test_client_ip_ignores_forwarding_headers() -> None:
    forwarded = {
        "X-Forwarded-For": "198.51.100.1",
        "X-Real-IP": "198.51.100.2",
        "Forwarded": "for=1.2.3.4",
    }
    assert client_ip(_request(("10.1.2.3", 5000), forwarded)) == "10.1.2.3"
    assert client_ip(_request(None, forwarded)) == ""


def test_too_many_attempts_response() -> None:
    exc = rate_limit.too_many_attempts(59_001)
    assert (exc.status_code, exc.detail) == (429, TOO_MANY_ATTEMPTS)
    assert exc.headers == {"Retry-After": "60"}  # rounded up to whole seconds
    assert rate_limit.too_many_attempts(0).headers == {"Retry-After": "1"}  # never zero


async def test_limiter_without_redis_fails_open() -> None:
    limiter = RateLimiter(None)
    for _ in range(50):
        await limiter.check_login_ip("10.0.0.1")
        await limiter.check_register_ip("10.0.0.1")
        async with limiter.login_attempt("alice@example.com") as attempt:
            attempt.failed()


async def test_redis_errors_during_a_request_fail_open() -> None:
    class BrokenRedis:
        async def eval(self, *args: Any) -> Any:
            raise RedisConnectionError("simulated outage")

    limiter = RateLimiter(BrokenRedis())  # type: ignore[arg-type]
    for _ in range(20):
        await limiter.check_login_ip("10.0.0.1")
        async with limiter.login_attempt("alice@example.com") as attempt:
            attempt.failed()


async def test_unreachable_redis_connects_to_nothing_quickly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(UNREACHABLE_REDIS))
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await rate_limit._connect() is None
    assert loop.time() - started < 5  # no arq retry delay


# --- Redis fixtures ----------------------------------------------------------------------


@pytest.fixture
async def redis_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ArqRedis]:
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(settings)
    await pool.flushdb()
    # Use the real limiter dependency (conftest installs a no-op override for other tests).
    app.dependency_overrides.pop(get_rate_limiter, None)
    try:
        yield pool
    finally:
        await pool.flushdb()
        await pool.aclose()


async def _keys(redis: ArqRedis) -> list[str]:
    return sorted([k.decode() if isinstance(k, bytes) else k for k in await redis.keys("*")])


async def _int(redis: ArqRedis, key: str) -> int:
    value = await redis.get(key)
    return int(value) if value is not None else 0


async def _expire_now(redis: ArqRedis, key: str) -> None:
    """End a key's window deterministically: shorten its TTL and wait until it is gone."""
    await redis.pexpire(key, 1)
    for _ in range(500):
        if not await redis.exists(key):
            return
        await asyncio.sleep(0.01)
    pytest.fail("key did not expire")


def _email_key(email: str) -> str:
    return rate_limit_key("login-email", rate_limit.email_key_material(email))


def _pending_key(email: str) -> str:
    return rate_limit_key("login-email-pending", rate_limit.email_key_material(email))


# --- Limiter against real Redis ----------------------------------------------------------


@requires_redis
async def test_concurrent_hits_cannot_exceed_the_limit(redis_client: ArqRedis) -> None:
    limiter = RateLimiter(redis_client)
    results = await asyncio.gather(
        *(limiter.check_login_ip("203.0.113.9") for _ in range(50)), return_exceptions=True
    )
    allowed = [r for r in results if r is None]
    limited = [r for r in results if isinstance(r, HTTPException) and r.status_code == 429]
    assert (len(allowed), len(limited)) == (10, 40)
    assert await _int(redis_client, rate_limit_key("login-ip", "203.0.113.9")) == 50


@requires_redis
async def test_concurrent_failed_logins_cannot_exceed_the_email_limit(
    redis_client: ArqRedis,
) -> None:
    """All attempts are in flight at once (none has recorded its failure yet): pending
    slots still cap the number of credential checks at the limit."""
    limiter = RateLimiter(redis_client)
    release = asyncio.Event()
    entered = rejected = 0

    async def attempt() -> None:
        nonlocal entered, rejected
        try:
            async with limiter.login_attempt("victim@example.com") as login:
                entered += 1
                await release.wait()
                login.failed()
        except HTTPException as exc:
            assert exc.status_code == 429
            rejected += 1

    tasks = [asyncio.create_task(attempt()) for _ in range(20)]
    for _ in range(1000):
        if entered + rejected == 20:
            break
        await asyncio.sleep(0.005)
    assert (entered, rejected) == (5, 15)
    release.set()
    await asyncio.gather(*tasks)
    assert await _int(redis_client, _email_key("victim@example.com")) == 5
    assert await _int(redis_client, _pending_key("victim@example.com")) == 0


@requires_redis
async def test_keys_expire_with_their_window(redis_client: ArqRedis) -> None:
    limiter = RateLimiter(redis_client)
    await limiter.check_login_ip("10.0.0.1")
    await limiter.check_register_ip("10.0.0.1")
    async with limiter.login_attempt("a@example.com") as attempt:
        attempt.failed()

    ttl = {key: await redis_client.pttl(key) for key in await _keys(redis_client)}
    assert 0 < ttl[rate_limit_key("login-ip", "10.0.0.1")] <= 60_000
    assert 0 < ttl[rate_limit_key("register-ip", "10.0.0.1")] <= 3_600_000
    assert 0 < ttl[_email_key("a@example.com")] <= 900_000
    pending = _pending_key("a@example.com")
    assert pending not in ttl or 0 < ttl[pending] <= rate_limit.PENDING_TTL_MS
    assert all(value > 0 for value in ttl.values())  # no key without an expiry

    # Later hits in the same window do not extend it.
    before = await redis_client.pttl(rate_limit_key("login-ip", "10.0.0.1"))
    await limiter.check_login_ip("10.0.0.1")
    assert await redis_client.pttl(rate_limit_key("login-ip", "10.0.0.1")) <= before


@requires_redis
async def test_window_expiry_allows_requests_again(redis_client: ArqRedis) -> None:
    limiter = RateLimiter(redis_client)
    for _ in range(10):
        await limiter.check_login_ip("10.0.0.2")
    with pytest.raises(HTTPException):
        await limiter.check_login_ip("10.0.0.2")
    await _expire_now(redis_client, rate_limit_key("login-ip", "10.0.0.2"))
    await limiter.check_login_ip("10.0.0.2")  # a new window

    for _ in range(5):
        async with limiter.login_attempt("b@example.com") as attempt:
            attempt.failed()
    with pytest.raises(HTTPException):
        async with limiter.login_attempt("b@example.com"):
            pass
    await _expire_now(redis_client, _email_key("b@example.com"))
    async with limiter.login_attempt("b@example.com"):
        pass


@requires_redis
async def test_errors_inside_an_attempt_are_not_counted(redis_client: ArqRedis) -> None:
    limiter = RateLimiter(redis_client)
    with pytest.raises(RuntimeError):
        async with limiter.login_attempt("c@example.com"):
            raise RuntimeError("database unavailable")
    assert await _int(redis_client, _email_key("c@example.com")) == 0
    assert await _int(redis_client, _pending_key("c@example.com")) == 0


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


ClientFactory = Callable[[str], httpx.AsyncClient]


@pytest.fixture
async def clients(db_session: AsyncSession) -> AsyncIterator[ClientFactory]:
    """Returns a factory for API clients whose requests come from a given IP address."""

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    opened: list[httpx.AsyncClient] = []

    def make(ip: str) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app, client=(ip, 40000))
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        opened.append(client)
        return client

    try:
        yield make
    finally:
        for client in opened:
            await client.aclose()
        app.dependency_overrides.pop(get_db, None)


async def _login(client: httpx.AsyncClient, email: str, password: str, **kw: Any) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, **kw
    )


async def _register(
    client: httpx.AsyncClient, email: str, password: str = PASSWORD
) -> httpx.Response:
    return await client.post("/api/v1/auth/register", json={"email": email, "password": password})


def _assert_limited(response: httpx.Response, max_retry_after: int) -> int:
    assert response.status_code == 429
    assert response.json() == LIMITED
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= max_retry_after
    return retry_after


class Spy:
    """Counts database lookups and password verifications made by the login route."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, db: AsyncSession) -> None:
        self.db_calls = 0
        self.verify_calls = 0
        original_scalar = db.scalar
        original_verify = auth_routes.verify_password

        async def scalar(*args: Any, **kwargs: Any) -> Any:
            self.db_calls += 1
            return await original_scalar(*args, **kwargs)

        def verify(*args: Any, **kwargs: Any) -> bool:
            self.verify_calls += 1
            return original_verify(*args, **kwargs)

        monkeypatch.setattr(db, "scalar", scalar)
        monkeypatch.setattr(auth_routes, "verify_password", verify)

    def reset(self) -> None:
        self.db_calls = self.verify_calls = 0


# --- API: login IP limit -----------------------------------------------------------------


@requires_db_and_redis
async def test_login_ip_limit(
    redis_client: ArqRedis, clients: ClientFactory, db_session: AsyncSession
) -> None:
    client = clients("192.0.2.10")
    assert (await _register(clients("192.0.2.99"), "known@example.com")).status_code == 201
    # Ten attempts proceed normally (distinct emails, so the email limit is not involved).
    for i in range(9):
        assert (await _login(client, f"user{i}@example.com", WRONG)).status_code == 401
    assert (await _login(client, "known@example.com", PASSWORD)).status_code == 200  # 10th
    # The 11th is limited, even with valid credentials.
    _assert_limited(await _login(client, "known@example.com", PASSWORD), 60)
    # Another address is unaffected.
    assert (await _login(clients("192.0.2.11"), "known@example.com", PASSWORD)).status_code == 200


@requires_db_and_redis
async def test_ip_limited_login_does_no_lookup_or_hashing(
    redis_client: ArqRedis,
    clients: ClientFactory,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = clients("192.0.2.20")
    for i in range(10):
        await _login(client, f"user{i}@example.com", WRONG)
    spy = Spy(monkeypatch, db_session)
    _assert_limited(await _login(client, "someone@example.com", WRONG), 60)
    assert (spy.db_calls, spy.verify_calls) == (0, 0)
    # The email of the limited request was not touched either.
    assert await _int(redis_client, _email_key("someone@example.com")) == 0


@requires_db_and_redis
async def test_x_forwarded_for_is_ignored(redis_client: ArqRedis, clients: ClientFactory) -> None:
    client = clients("192.0.2.30")
    for i in range(10):
        headers = {"X-Forwarded-For": f"198.51.100.{i}"}
        assert (
            await _login(client, f"u{i}@example.com", WRONG, headers=headers)
        ).status_code == 401
    spoofed = {"X-Forwarded-For": "198.51.100.200"}
    _assert_limited(await _login(client, "x@example.com", WRONG, headers=spoofed), 60)
    # Claiming the limited address from elsewhere does not inherit its limit.
    other = {"X-Forwarded-For": "192.0.2.30"}
    assert (
        await _login(clients("192.0.2.31"), "y@example.com", WRONG, headers=other)
    ).status_code == 401
    keys = await _keys(redis_client)
    assert rate_limit_key("login-ip", "192.0.2.30") in keys
    assert not any(_sha(f"198.51.100.{i}") in k for i in (0, 200) for k in keys)


# --- API: login email limit --------------------------------------------------------------


@requires_db_and_redis
async def test_login_email_limit_across_ips(
    redis_client: ArqRedis,
    clients: ClientFactory,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (await _register(clients("192.0.2.98"), "target@example.com")).status_code == 201
    for i in range(5):  # five failures, each from a different address
        response = await _login(clients(f"198.18.0.{i}"), "target@example.com", WRONG)
        assert response.status_code == 401
        assert await _int(redis_client, _email_key("target@example.com")) == i + 1
    spy = Spy(monkeypatch, db_session)
    retry_after = _assert_limited(
        await _login(clients("198.18.0.50"), "target@example.com", PASSWORD), 900
    )
    assert retry_after > 840  # most of the 15-minute window remains
    assert (spy.db_calls, spy.verify_calls) == (0, 0)  # no lookup, no Argon2
    # Other emails from the same address are unaffected.
    assert (await _login(clients("198.18.0.50"), "other@example.com", WRONG)).status_code == 401


@requires_db_and_redis
async def test_known_and_unknown_emails_are_limited_identically(
    redis_client: ArqRedis, clients: ClientFactory
) -> None:
    assert (await _register(clients("192.0.2.97"), "exists@example.com")).status_code == 201
    outcomes: dict[str, list[tuple[int, Any, str | None, str | None]]] = {}
    for n, email in enumerate(["exists@example.com", "missing@example.com"]):
        outcomes[email] = []
        for i in range(6):
            response = await _login(clients(f"198.19.{n}.{i}"), email, WRONG)
            outcomes[email].append(
                (
                    response.status_code,
                    response.json(),
                    response.headers.get("WWW-Authenticate"),
                    "set" if "Retry-After" in response.headers else None,
                )
            )
    assert outcomes["exists@example.com"] == outcomes["missing@example.com"]
    assert [o[0] for o in outcomes["missing@example.com"]] == [401] * 5 + [429]


@requires_db_and_redis
async def test_successful_login_does_not_reset_failures(
    redis_client: ArqRedis, clients: ClientFactory
) -> None:
    client = clients("192.0.2.40")
    assert (await _register(clients("192.0.2.96"), "carol@example.com")).status_code == 201
    for _ in range(4):
        assert (await _login(client, "carol@example.com", WRONG)).status_code == 401
    assert (await _login(client, "carol@example.com", PASSWORD)).status_code == 200
    assert await _int(redis_client, _email_key("carol@example.com")) == 4  # not reset
    assert (await _login(client, "carol@example.com", WRONG)).status_code == 401  # 5th failure
    _assert_limited(await _login(client, "carol@example.com", PASSWORD), 900)


@requires_db_and_redis
async def test_email_normalization_shares_one_counter(
    redis_client: ArqRedis, clients: ClientFactory
) -> None:
    variants = [
        "dave@example.com",
        "  Dave@Example.com",
        "DAVE@EXAMPLE.COM ",
        "dave@EXAMPLE.com",
        "\tdave@example.com",
    ]
    for i, email in enumerate(variants):
        assert (await _login(clients(f"198.20.0.{i}"), email, WRONG)).status_code == 401
    _assert_limited(await _login(clients("198.20.0.9"), "DaVe@example.COM", WRONG), 900)
    assert await _int(redis_client, _email_key("dave@example.com")) == 5


# --- API: registration IP limit ----------------------------------------------------------


@requires_db_and_redis
async def test_register_ip_limit_counts_every_attempt(
    redis_client: ArqRedis, clients: ClientFactory
) -> None:
    client = clients("192.0.2.50")
    assert (await _register(client, "r1@example.com")).status_code == 201
    assert (await _register(client, "r2@example.com")).status_code == 201
    assert (await _register(client, "r1@example.com")).status_code == 409  # duplicate
    assert (await _register(client, "r3@example.com", "short")).status_code == 422  # too short
    assert (await _register(client, "r4@example.com")).status_code == 201  # 5th attempt
    retry_after = _assert_limited(await _register(client, "r5@example.com"), 3600)
    assert retry_after > 3500
    assert (await _register(clients("192.0.2.51"), "r5@example.com")).status_code == 201


@requires_db_and_redis
async def test_register_window_expiry(redis_client: ArqRedis, clients: ClientFactory) -> None:
    client = clients("192.0.2.60")
    for i in range(5):
        await _register(client, f"w{i}@example.com")
    _assert_limited(await _register(client, "w5@example.com"), 3600)
    await _expire_now(redis_client, rate_limit_key("register-ip", "192.0.2.60"))
    assert (await _register(client, "w5@example.com")).status_code == 201


@requires_db_and_redis
async def test_login_window_expiry(redis_client: ArqRedis, clients: ClientFactory) -> None:
    client = clients("192.0.2.61")
    for i in range(10):
        await _login(client, f"e{i}@example.com", WRONG)
    _assert_limited(await _login(client, "e10@example.com", WRONG), 60)
    await _expire_now(redis_client, rate_limit_key("login-ip", "192.0.2.61"))
    assert (await _login(client, "e10@example.com", WRONG)).status_code == 401


# --- API: keys, behavior under limits, outage --------------------------------------------


@requires_db_and_redis
async def test_redis_keys_never_contain_raw_email_or_ip(
    redis_client: ArqRedis, clients: ClientFactory
) -> None:
    client = clients("192.0.2.70")
    await _register(client, "Private.Person@Example.com")
    await _login(client, "Private.Person@Example.com", WRONG)
    await _login(client, "Private.Person@Example.com", PASSWORD)

    keys = await _keys(redis_client)
    assert keys
    for key in keys:
        assert KEY_PATTERN.match(key), key
        lowered = key.lower()
        assert "private" not in lowered and "example" not in lowered and "@" not in key
        assert "192.0.2.70" not in key
    values = [await redis_client.get(key) for key in keys]
    assert all(re.fullmatch(rb"-?\d+", v or b"0") for v in values)  # counters only


@requires_db_and_redis
async def test_authentication_unchanged_under_limits(
    redis_client: ArqRedis, clients: ClientFactory
) -> None:
    client = clients("192.0.2.80")
    registered = await _register(client, "  Erin@Example.com ")
    assert registered.status_code == 201
    assert registered.json()["email"] == "erin@example.com"
    assert (await _register(client, "erin@example.com")).json() == {
        "detail": "An account with this email already exists"
    }
    ok = await _login(client, "erin@example.com", PASSWORD)
    assert ok.status_code == 200
    assert ok.json()["token_type"] == "bearer"
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {ok.json()['access_token']}"}
    )
    assert me.json()["email"] == "erin@example.com"
    for email in ("erin@example.com", "nobody@example.com"):
        bad = await _login(client, email, WRONG)
        assert bad.status_code == 401
        assert bad.json() == {"detail": "Invalid email or password"}
        assert bad.headers["WWW-Authenticate"] == "Bearer"
        assert "Retry-After" not in bad.headers


@requires_db_and_redis
async def test_redis_outage_fails_open(
    redis_client: ArqRedis, clients: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(UNREACHABLE_REDIS))
    client = clients("192.0.2.90")
    assert (await _register(client, "frank@example.com")).status_code == 201
    for i in range(6):  # beyond the registration limit
        assert (await _register(client, f"f{i}@example.com")).status_code == 201
    for _ in range(12):  # beyond both login limits
        assert (await _login(client, "frank@example.com", WRONG)).status_code == 401
    assert (await _login(client, "frank@example.com", PASSWORD)).status_code == 200
    assert await _keys(redis_client) == []


@requires_db_and_redis
async def test_redis_failing_mid_request_fails_open(
    redis_client: ArqRedis, clients: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_eval(self: RateLimiter, *args: Any) -> None:
        return None  # what _eval returns after a Redis error

    monkeypatch.setattr(RateLimiter, "_eval", broken_eval)
    client = clients("192.0.2.91")
    for _ in range(12):
        assert (await _login(client, "gina@example.com", WRONG)).status_code == 401


def test_conftest_override_is_active_by_default() -> None:
    override: Callable[[], Awaitable[Any] | Any] | None = app.dependency_overrides.get(
        get_rate_limiter
    )
    assert override is not None
    limiter = override()
    assert isinstance(limiter, RateLimiter) and limiter._redis is None
