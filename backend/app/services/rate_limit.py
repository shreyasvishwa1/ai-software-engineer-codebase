"""Rate limiting for the authentication endpoints, backed by the existing Redis.

Fixed windows (code constants, not configuration):

- ``login-ip``: every login attempt per client IP, 10 per 60 s.
- ``login-email``: failed logins per normalized email, 5 per 15 min. Only failed attempts
  count, a successful login does not reset it, and known and unknown emails are treated
  identically. While a login is being checked it holds a short-lived "pending" slot, so
  concurrent attempts cannot get past the limit before their failures are recorded.
- ``register-ip``: every registration attempt per client IP, 5 per hour.

Counters are updated by Lua scripts, so increment and expiry are atomic. Keys contain only
the SHA-256 hex digest of the email or IP (``ratelimit:<scope>:<digest>``). The client IP
is ``request.client.host``; forwarding headers such as X-Forwarded-For are never trusted.

Connections follow the existing queue pattern (app.workers.queue): one arq Redis pool per
request, closed when the request ends; the API has no long-lived Redis pool. If Redis is
unavailable the limiter fails open: requests proceed without rate limiting. Nothing is
logged.
"""

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from arq import create_pool
from arq.connections import ArqRedis
from fastapi import Depends, HTTPException, Request, status
from redis.exceptions import RedisError

from app.schemas.auth import normalize_email
from app.workers.queue import redis_settings


@dataclass(frozen=True)
class Limit:
    scope: str
    max_requests: int
    window_seconds: int

    @property
    def window_ms(self) -> int:
        return self.window_seconds * 1000


LOGIN_IP = Limit("login-ip", 10, 60)
LOGIN_EMAIL = Limit("login-email", 5, 15 * 60)
REGISTER_IP = Limit("register-ip", 5, 60 * 60)
RESET_REQUEST_IP = Limit("reset-request-ip", 5, 60 * 60)
RESET_REQUEST_EMAIL = Limit("reset-request-email", 3, 60 * 60)
RESET_CONFIRM_IP = Limit("reset-confirm-ip", 10, 15 * 60)
# Upper bound for one login check; a slot left behind by a crashed request expires.
PENDING_TTL_MS = 60_000

TOO_MANY_ATTEMPTS = "Too many attempts; please try again later"
_REDIS_ERRORS = (RedisError, OSError, asyncio.TimeoutError)

# KEYS[1] counter; ARGV[1] window (ms). Returns {count, remaining ms}.
_HIT = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('PTTL', KEYS[1])
if count == 1 or ttl < 0 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""

# KEYS[1] failures, KEYS[2] pending; ARGV[1] limit, ARGV[2] pending TTL (ms).
# Returns {1, 0} when a slot was taken, {0, remaining ms} when limited.
_RESERVE = """
local failures = tonumber(redis.call('GET', KEYS[1]) or '0')
local pending = tonumber(redis.call('GET', KEYS[2]) or '0')
if failures + pending >= tonumber(ARGV[1]) then
  local ttl
  if failures >= tonumber(ARGV[1]) then
    ttl = redis.call('PTTL', KEYS[1])
  else
    ttl = redis.call('PTTL', KEYS[2])
  end
  if ttl < 0 then ttl = 1000 end
  return {0, ttl}
end
redis.call('INCR', KEYS[2])
redis.call('PEXPIRE', KEYS[2], ARGV[2])
return {1, 0}
"""

# KEYS[1] failures, KEYS[2] pending; ARGV[1] '1' if the attempt failed, ARGV[2] window (ms).
_FINISH = """
if tonumber(redis.call('GET', KEYS[2]) or '0') > 0 then
  redis.call('DECR', KEYS[2])
end
if ARGV[1] == '1' then
  local count = redis.call('INCR', KEYS[1])
  if count == 1 or redis.call('PTTL', KEYS[1]) < 0 then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
  end
end
return {1}
"""


def rate_limit_key(scope: str, value: str) -> str:
    return f"ratelimit:{scope}:{hashlib.sha256(value.encode()).hexdigest()}"


def email_key_material(email: str) -> str:
    return normalize_email(email)


def client_ip(request: Request) -> str:
    """The direct peer address only; forwarding headers are deliberately ignored."""
    return request.client.host if request.client else ""


def too_many_attempts(retry_after_ms: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=TOO_MANY_ATTEMPTS,
        headers={"Retry-After": str(max(1, math.ceil(retry_after_ms / 1000)))},
    )


class LoginAttempt:
    """Marks whether the credentials were rejected (``failed()``)."""

    def __init__(self) -> None:
        self.rejected = False

    def failed(self) -> None:
        self.rejected = True


class RateLimiter:
    """Per-request limiter. Without a Redis connection every check passes (fail open)."""

    def __init__(self, redis: ArqRedis | None) -> None:
        self._redis = redis

    async def _eval(self, script: str, keys: list[str], args: list[Any]) -> list[int] | None:
        if self._redis is None:
            return None
        try:
            result = await self._redis.eval(script, len(keys), *keys, *args)  # type: ignore[misc]
        except _REDIS_ERRORS:
            return None  # fail open
        return [int(value) for value in result]

    async def hit(self, limit: Limit, value: str) -> None:
        """Count one request; raise 429 once the window's limit is exceeded."""
        result = await self._eval(_HIT, [rate_limit_key(limit.scope, value)], [limit.window_ms])
        if result is not None and result[0] > limit.max_requests:
            raise too_many_attempts(result[1])

    async def check_login_ip(self, ip: str) -> None:
        await self.hit(LOGIN_IP, ip)

    async def check_register_ip(self, ip: str) -> None:
        await self.hit(REGISTER_IP, ip)

    @asynccontextmanager
    async def login_attempt(self, email: str) -> AsyncIterator[LoginAttempt]:
        """Hold a pending slot for ``email`` while the credentials are checked; raise 429
        if recorded failures plus pending attempts already reach the limit. A failure is
        recorded only when the body calls ``failed()``; errors are not counted."""
        material = email_key_material(email)
        keys = [
            rate_limit_key(LOGIN_EMAIL.scope, material),
            rate_limit_key(f"{LOGIN_EMAIL.scope}-pending", material),
        ]
        reserved = await self._eval(_RESERVE, keys, [LOGIN_EMAIL.max_requests, PENDING_TTL_MS])
        if reserved is not None and reserved[0] == 0:
            raise too_many_attempts(reserved[1])
        attempt = LoginAttempt()
        try:
            yield attempt
        finally:
            if reserved is not None:
                failed = "1" if attempt.rejected else "0"
                await self._eval(_FINISH, keys, [failed, LOGIN_EMAIL.window_ms])


async def _connect() -> ArqRedis | None:
    settings = redis_settings()
    settings.conn_retries = 0  # fail open at once instead of arq's retry delay
    try:
        return await create_pool(settings)
    except _REDIS_ERRORS:
        return None


async def get_rate_limiter() -> AsyncIterator[RateLimiter]:
    redis = await _connect()
    try:
        yield RateLimiter(redis)
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except _REDIS_ERRORS:
                pass


RateLimit = Annotated[RateLimiter, Depends(get_rate_limiter)]
