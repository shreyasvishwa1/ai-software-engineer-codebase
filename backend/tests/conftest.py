import os
from collections.abc import Iterator

import pytest

# Test-only configuration, set before the app (and its settings) is imported.
# Environment variables take precedence over backend/.env, so tests do not
# depend on a developer's local values. This key is not a real secret.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-real-use-000000")
os.environ.setdefault("API_PREFIX", "/api")
# Placeholder URL without credentials; the engine is lazy, so unit tests never connect.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/ai_se_unit_tests")


@pytest.fixture(autouse=True)
def _no_auth_rate_limit() -> Iterator[None]:
    """Disable authentication rate limiting for every test via a dependency override, so
    the suite is neither throttled nor dependent on Redis. Rate limiting itself is tested
    in tests/test_rate_limit.py, which installs the real limiter. The application has no
    test-environment bypass."""
    from app.main import app
    from app.services.rate_limit import RateLimiter, get_rate_limiter

    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(None)
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_rate_limiter, None)
