"""Production exposure tests: API docs/OpenAPI and SQL bound-parameter hiding.

The app is created when app.main is imported, so each environment is checked by importing
it in a fresh Python process (nothing in this test process is reconfigured). Engine tests
rebuild the cached engine with patched settings and restore the caches afterwards; the
real-database tests use the throwaway test database (TEST_DATABASE_URL, "_test").

Scope note: hide_parameters=True prevents SQLAlchemy from rendering bound parameter values
in SQL logs and SQLAlchemy-generated exception formatting. It does not sanitize
database-server-generated error text.
"""

import contextlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import get_settings
from app.database import get_engine
from app.models import User

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)

DOC_PATHS = ["/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"]
ROOT_WITH_DOCS = {
    "name": "AI Software Engineer",
    "version": "1.0.0",
    "status": "running",
    "docs": "/docs",
    "health": "/health",
}
SENTINEL = "sentinel-bound-value-7c41e9"

# Runs in a fresh interpreter: imports the app and reports what it serves.
_PROBE = """
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
result = {"status": {}, "routes": sorted(getattr(r, "path", "") for r in app.routes)}
for path in %(paths)r:
    result["status"][path] = client.get(path).status_code
root = client.get("/")
result["root"] = [root.status_code, root.json()]
health = client.get("/api/v1/health")
result["health"] = [health.status_code, sorted(health.json())]
me = client.get("/api/v1/auth/me")
result["me"] = [me.status_code, me.json(), me.headers.get("www-authenticate")]
print(json.dumps(result))
"""


def _probe(environment: str) -> dict[str, Any]:
    env = {
        **os.environ,
        "ENVIRONMENT": environment,
        "DEBUG": "false",
        # Test-only value; not a published placeholder, so production settings accept it.
        "SECRET_KEY": "test-only-production-probe-key-0123456789abcdef",
        "API_PREFIX": "/api",
        "DATABASE_URL": "postgresql+asyncpg://localhost/ai_se_unit_tests",  # never connected
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", _PROBE % {"paths": DOC_PATHS}],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return dict(json.loads(result.stdout.strip().splitlines()[-1]))


@pytest.fixture(scope="module")
def production() -> dict[str, Any]:
    return _probe("production")


# --- A. Production process ---------------------------------------------------------------


def test_production_does_not_serve_docs_or_openapi(production: dict[str, Any]) -> None:
    assert production["status"] == {path: 404 for path in DOC_PATHS}


def test_production_root_omits_docs(production: dict[str, Any]) -> None:
    status_code, body = production["root"]
    assert status_code == 200
    assert body == {key: value for key, value in ROOT_WITH_DOCS.items() if key != "docs"}


def test_production_api_is_unchanged(production: dict[str, Any]) -> None:
    assert production["health"] == [200, ["app", "message", "status", "timestamp", "version"]]
    assert production["me"] == [
        401,
        {"detail": "Invalid or missing authentication credentials"},
        "Bearer",
    ]


@pytest.mark.parametrize("environment", ["development", "test"])
def test_only_doc_routes_differ_from_production(
    production: dict[str, Any], environment: str
) -> None:
    other = _probe(environment)
    assert set(other["routes"]) - set(production["routes"]) == set(DOC_PATHS)
    assert set(production["routes"]) <= set(other["routes"])
    assert {"/api/v1/auth/login", "/api/v1/auth/register"} <= set(production["routes"])


# --- B. Development / test processes -----------------------------------------------------


@pytest.mark.parametrize("environment", ["development", "test"])
def test_docs_served_outside_production(environment: str) -> None:
    result = _probe(environment)
    assert result["status"] == {path: 200 for path in DOC_PATHS}
    assert result["root"] == [200, ROOT_WITH_DOCS]


# --- C. Engine configuration -------------------------------------------------------------


@pytest.fixture
def fresh_engine_cache() -> Iterator[None]:
    get_engine.cache_clear()
    try:
        yield
    finally:
        get_engine.cache_clear()


@pytest.mark.parametrize(("debug", "hidden"), [(False, True), (True, False)])
def test_engine_hides_parameters_unless_debug(
    debug: bool, hidden: bool, monkeypatch: pytest.MonkeyPatch, fresh_engine_cache: None
) -> None:
    monkeypatch.setattr(get_settings(), "debug", debug)
    engine = get_engine()  # lazy: no connection is opened
    assert engine.sync_engine.hide_parameters is hidden
    assert engine.echo is debug


# --- D. Real database --------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _test_engine(monkeypatch: pytest.MonkeyPatch, debug: bool) -> AsyncIterator[AsyncEngine]:
    """The application's engine (get_engine) for the throwaway test database with DEBUG
    patched; the engine cache is cleared before and afterwards."""
    assert TEST_DATABASE_URL
    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        pytest.fail("refusing to use TEST_DATABASE_URL: database name must end with '_test'")
    monkeypatch.setattr(get_settings(), "database_url", SecretStr(TEST_DATABASE_URL))
    monkeypatch.setattr(get_settings(), "debug", debug)
    get_engine.cache_clear()
    engine = get_engine()
    try:
        yield engine
    finally:
        await engine.dispose()
        get_engine.cache_clear()


async def _sql_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, debug: bool
) -> str:
    """SQLAlchemy's own SQL log output for a statement with a sentinel bound parameter.
    INFO logging is enabled explicitly, so statements are logged even when echo is off."""
    caplog.set_level(logging.INFO, logger="sqlalchemy.engine")
    async with _test_engine(monkeypatch, debug) as engine:
        async with engine.connect() as conn:
            value = await conn.scalar(text("SELECT CAST(:value AS text)"), {"value": SENTINEL})
    assert value == SENTINEL  # the parameter was really bound and sent
    return caplog.text


@requires_db
async def test_sql_log_hides_bound_parameters_without_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log = await _sql_log(monkeypatch, caplog, debug=False)
    assert "SELECT CAST(" in log  # the statement itself is still logged
    assert "[SQL parameters hidden due to hide_parameters=True]" in log
    assert SENTINEL not in log


@requires_db
async def test_sql_log_shows_bound_parameters_in_debug(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log = await _sql_log(monkeypatch, caplog, debug=True)
    assert SENTINEL in log  # development debugging behavior is unchanged


async def _error_text(monkeypatch: pytest.MonkeyPatch, debug: bool) -> str:
    """Text of a database error whose server message does not quote the value (a string too
    long for users.email), so any sentinel in it can only come from SQLAlchemy's parameter
    rendering. Server errors that do quote values (e.g. a unique-violation DETAIL) are not
    covered: hide_parameters does not sanitize database-server-generated text. Nothing is
    committed."""
    async with _test_engine(monkeypatch, debug) as engine:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                with pytest.raises(DBAPIError) as excinfo:
                    await conn.execute(
                        insert(User).values(
                            email=SENTINEL + "x" * 400 + "@example.com", password_hash="x"
                        )
                    )
            finally:
                await transaction.rollback()
    return str(excinfo.value)


@requires_db
async def test_sqlalchemy_error_formatting_hides_bound_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = await _error_text(monkeypatch, debug=False)
    assert "value too long" in error  # the error itself is still reported
    assert "[SQL parameters hidden due to hide_parameters=True]" in error
    assert SENTINEL not in error


@requires_db
async def test_sqlalchemy_error_formatting_shows_parameters_in_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = await _error_text(monkeypatch, debug=True)
    assert SENTINEL in error  # development debugging behavior is unchanged
