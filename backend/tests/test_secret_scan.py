"""Secret scan tests (GET .../analysis/secrets). Fully offline.

Rule tests need no database. API tests run against the already-migrated throwaway test
database (TEST_DATABASE_URL, "_test") inside rolled-back transactions; ingestion runs
inline with the real worker logic and DATA_DIR is a temporary directory. The rate-limit
test uses a disposable Redis (TEST_REDIS_URL). Junction tests are Windows-only; symlink
tests are skipped where symlinks cannot be created.

Every credential-shaped value is assembled at runtime from fragments, so this file never
contains a literal that matches a scanner rule.
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from arq import create_pool
from arq.connections import RedisSettings
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import File
from app.services import ingestion, secret_scan
from app.services.file_content import MAX_CONTENT_BYTES
from app.services.rate_limit import Limit, RateLimiter, get_rate_limiter, too_many_attempts
from app.services.secret_scan import (
    MAX_FILES,
    MAX_FINDINGS,
    MAX_LINE_CHARS,
    MAX_TOTAL_BYTES,
    RULES,
    SECRET_SCAN_LIMIT,
    scan_text,
)
from tests.test_worker import _zip, install_inline_worker

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)
requires_db_and_redis = pytest.mark.skipif(
    not (TEST_DATABASE_URL and TEST_REDIS_URL),
    reason="set TEST_DATABASE_URL and TEST_REDIS_URL for the rate-limit test",
)

PASSWORD = "test-only-Passw0rd!"
NOT_FOUND = {"detail": "Repository not found"}

# Synthetic, structurally valid values (never real credentials), built from fragments.
AWS = "AK" + "IA" + "Q" * 16
AWS_TEMP = "AS" + "IA" + "Z9" * 8
GITHUB = "gh" + "p_" + "a1" * 18
GITHUB_PAT = "github" + "_pat_" + "b2" * 41
SLACK = "xo" + "xb-" + "1234567890-" + "c" * 10
STRIPE = "sk" + "_live_" + "d4" * 12
GOOGLE = "AI" + "za" + "e5" * 17 + "f"
ANTHROPIC = "sk-" + "ant-" + "g6" * 10
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
PGP_PRIVATE_KEY = "-----BEGIN " + "PGP PRIVATE" + " KEY BLOCK-----"
ALL_SECRETS = (
    AWS,
    AWS_TEMP,
    GITHUB,
    GITHUB_PAT,
    SLACK,
    STRIPE,
    GOOGLE,
    ANTHROPIC,
    PRIVATE_KEY,
    PGP_PRIVATE_KEY,
)
# The distinctive bodies of the values above (what must never be echoed).
SECRET_BODIES = ("Q" * 16, "Z9" * 8, "a1" * 18, "b2" * 41, "c" * 10, "d4" * 12, "e5" * 17)

FILES: dict[str, bytes] = {
    "README.md": b"# Project\nNothing to see here.\n",
    "src/config.py": f'REGION = "us-east-1"\n\nKEY_ID = "{AWS}"\n'.encode(),
    "deploy/id_rsa": f"{PRIVATE_KEY}\nMIIEowIBAAKCAQEA\n".encode(),
    "app/.env": f"# tokens\nGITHUB={GITHUB} SLACK={SLACK}\r\nOTHER=1\r\n".encode(),
}


# --- Rules (no database) -------------------------------------------------------------------


def test_rule_set_is_exactly_the_approved_seven() -> None:
    assert [rule.id for rule in RULES] == [
        "aws-access-key-id",
        "github-token",
        "slack-token",
        "stripe-live-key",
        "google-api-key",
        "anthropic-api-key",
        "private-key",
    ]
    assert all(rule.description for rule in RULES)


@pytest.mark.parametrize(
    ("rule", "line"),
    [
        ("aws-access-key-id", f'key = "{AWS}"'),
        ("aws-access-key-id", f"{AWS_TEMP}"),
        ("github-token", f"token: {GITHUB}"),
        ("github-token", f"token={'gh' + 'o_' + 'h' * 40}"),
        ("github-token", f"{GITHUB_PAT}"),
        ("slack-token", f"SLACK={SLACK}"),
        ("stripe-live-key", f"stripe = '{STRIPE}'"),
        ("stripe-live-key", "rk" + "_live_" + "x" * 24),
        ("google-api-key", f"maps({GOOGLE})"),
        ("anthropic-api-key", f"ANTHROPIC_API_KEY={ANTHROPIC}"),
        ("private-key", PRIVATE_KEY),
        ("private-key", "-----BEGIN " + "PRIVATE" + " KEY-----"),
        ("private-key", "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----"),
        ("private-key", "-----BEGIN " + "ENCRYPTED PRIVATE" + " KEY-----"),
        ("private-key", PGP_PRIVATE_KEY),
    ],
)
def test_each_rule_matches(rule: str, line: str) -> None:
    assert scan_text(line) == [(1, rule)]


@pytest.mark.parametrize(
    "line",
    [
        "AK" + "IA" + "Q" * 15,  # one character short
        "AK" + "IA" + "Q" * 17,  # too long: part of a longer identifier
        "AK" + "IA" + "q" * 16,  # lowercase
        "XAK" + "IA" + "Q" * 16,  # inside a word
        "gh" + "x_" + "a" * 36,  # unknown GitHub prefix
        "gh" + "p_" + "a" * 35,  # one character short
        "github" + "_pat_" + "b" * 81,
        "xo" + "xq-" + "1234567890",  # unknown Slack prefix
        "xo" + "xb-" + "123456789",  # too short
        "sk" + "_test_" + "d" * 24,  # test-mode keys are not reported
        "sk" + "_live_" + "d" * 23,
        "AI" + "za" + "e" * 34,  # one character short
        "AI" + "za" + "e" * 36,  # too long
        "sk-" + "ant-" + "g" * 19,
        "sk-" + "ant" + "_" + "g" * 30,
        "-----BEGIN " + "PUBLIC" + " KEY-----",
        "-----BEGIN " + "CERTIFICATE" + "-----",
        "-----BEGIN " + "rsa private" + " key-----",
    ],
)
def test_near_misses_do_not_match(line: str) -> None:
    assert scan_text(line) == []


def test_two_rules_on_one_line_are_reported_separately() -> None:
    assert scan_text(f"{GITHUB} {AWS}") == [(1, "aws-access-key-id"), (1, "github-token")]


def test_a_rule_is_reported_once_per_line() -> None:
    assert scan_text(f"{AWS} {AWS} {AWS}") == [(1, "aws-access-key-id")]


def test_line_numbers_are_one_based() -> None:
    text = f"first\nsecond\n{STRIPE}\n\n{ANTHROPIC}\n"
    assert scan_text(text) == [(3, "stripe-live-key"), (5, "anthropic-api-key")]
    assert scan_text(f"{STRIPE}") == [(1, "stripe-live-key")]


def test_crlf_line_endings() -> None:
    text = f"a\r\n{AWS}\r\nb\r\n{GOOGLE}\r\n"
    assert scan_text(text) == [(2, "aws-access-key-id"), (4, "google-api-key")]


def test_only_the_first_4096_characters_of_a_line_are_scanned() -> None:
    assert MAX_LINE_CHARS == 4096
    fits = " " * (MAX_LINE_CHARS - len(AWS)) + AWS
    cut = " " * (MAX_LINE_CHARS - len(AWS) + 1) + AWS  # last character falls outside
    after = "x " * MAX_LINE_CHARS + AWS
    assert scan_text(fits) == [(1, "aws-access-key-id")]
    assert scan_text(cut) == []
    assert scan_text(after) == []
    # Later lines are unaffected by a long line before them.
    assert scan_text(f"{after}\n{AWS}") == [(2, "aws-access-key-id")]


def test_pathological_lines_are_bounded() -> None:
    lines = [
        ("gh" + "p_") * 2000,
        "AK" + "IA" * 2000,
        "-----BEGIN " + "A " * 2000,
        "xo" + "xb" * 2000,
        "sk-" + "ant" * 2000,
        ("AI" + "za") * 2000,
    ]
    assert scan_text("\n".join(lines)) == []


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
    user_id: str
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
    owner = Owner(registered.json()["id"], headers, project["id"], repository.json()["id"])
    await _ingest(client, owner, files)
    return owner


@pytest.fixture
async def alice(client: httpx.AsyncClient) -> Owner:
    return await _owner(client, "alice@example.com", FILES)


def _url(owner: Owner, project_id: str | None = None, repository_id: str | None = None) -> str:
    return (
        f"/api/v1/projects/{project_id or owner.project_id}"
        f"/repositories/{repository_id or owner.repository_id}/analysis/secrets"
    )


async def _scan(client: httpx.AsyncClient, owner: Owner) -> dict[str, Any]:
    response = await client.get(_url(owner), headers=owner.headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = secret_scan._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(secret_scan, "_read_verified", spy)
    return reads


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


def _assert_no_secret_values(text: str) -> None:
    for value in (*ALL_SECRETS, *SECRET_BODIES, "MIIEowIBAAKCAQEA"):
        assert value not in text


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_scan_across_files(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    body = await _scan(client, alice)
    assert body["findings"] == [
        {"path": "app/.env", "line": 2, "rule": "github-token", "description": "GitHub token"},
        {"path": "app/.env", "line": 2, "rule": "slack-token", "description": "Slack token"},
        {"path": "deploy/id_rsa", "line": 1, "rule": "private-key", "description": "Private key"},
        {
            "path": "src/config.py",
            "line": 3,
            "rule": "aws-access-key-id",
            "description": "AWS access key ID",
        },
    ]
    assert body["summary"] == {
        "files_scanned": 4,
        "binary": 0,
        "too_large": 0,
        "unavailable": 0,
        "findings": 4,
    }
    assert body["truncated"] is False
    assert sorted(read_spy) == sorted(FILES)  # every file went through the verified reader
    for finding in body["findings"]:
        assert set(finding) == {"path", "line", "rule", "description"}


@requires_db
async def test_clean_repository(client: httpx.AsyncClient, alice: Owner) -> None:
    await _ingest(client, alice, {"README.md": b"hello\n", "src/a.py": b"x = 1\n"})
    body = await _scan(client, alice)
    assert body == {
        "findings": [],
        "summary": {
            "files_scanned": 2,
            "binary": 0,
            "too_large": 0,
            "unavailable": 0,
            "findings": 0,
        },
        "truncated": False,
    }


@requires_db
async def test_findings_are_sorted_by_path_line_and_rule(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    files = {
        "z.txt": f"{AWS}\n".encode(),
        "a/b.txt": f"x\n{STRIPE} {GOOGLE}\n{ANTHROPIC}\n".encode(),
        "a.txt": f"{SLACK}\n".encode(),
        "B.txt": f"{GITHUB}\n".encode(),
    }
    await _ingest(client, alice, files)
    first = await client.get(_url(alice), headers=alice.headers)
    second = await client.get(_url(alice), headers=alice.headers)
    assert first.json() == second.json()
    assert [(f["path"], f["line"], f["rule"]) for f in first.json()["findings"]] == [
        ("B.txt", 1, "github-token"),
        ("a.txt", 1, "slack-token"),
        ("a/b.txt", 2, "google-api-key"),
        ("a/b.txt", 2, "stripe-live-key"),
        ("a/b.txt", 3, "anthropic-api-key"),
        ("z.txt", 1, "aws-access-key-id"),
    ]


@requires_db
async def test_no_secret_values_or_internals_are_exposed_or_logged(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    every = "\n".join(ALL_SECRETS) + "\n"
    await _ingest(client, alice, {"all.txt": every.encode(), "id_rsa": FILES["deploy/id_rsa"]})
    caplog.set_level(logging.DEBUG)
    response = await client.get(_url(alice), headers=alice.headers)
    assert response.status_code == 200
    assert len(response.json()["findings"]) == len(ALL_SECRETS) + 1
    _assert_no_secret_values(response.text)
    _assert_no_secret_values(caplog.text)
    assert "column" not in response.text and "snippet" not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text


@requires_db
async def test_scan_makes_no_outbound_connections_or_subprocesses(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def refuse(name: str) -> Any:
        def blocked(*args: Any, **kwargs: Any) -> Any:
            attempts.append(name)
            raise AssertionError(f"unexpected {name}")

        return blocked

    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))
    monkeypatch.setattr(subprocess, "Popen", refuse("subprocess.Popen"))
    body = await _scan(client, alice)
    assert body["summary"]["findings"] == 4
    assert attempts == []


@requires_db
async def test_unscannable_files_are_counted_not_failed(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    files = {
        "bin/blob.dat": b"\x00\x01" + AWS.encode() + b"\x00",
        "bin/latin1.txt": b"\xff\xfe" + AWS.encode(),
        "big/huge.txt": AWS.encode() + b"\n" + os.urandom(MAX_CONTENT_BYTES),
        "ok.txt": f"{AWS}\n".encode(),
    }
    await _ingest(client, alice, files)
    body = await _scan(client, alice)
    assert [(f["path"], f["rule"]) for f in body["findings"]] == [("ok.txt", "aws-access-key-id")]
    assert body["summary"] == {
        "files_scanned": 1,
        "binary": 2,
        "too_large": 1,
        "unavailable": 0,
        "findings": 1,
    }
    assert body["truncated"] is False
    assert "big/huge.txt" not in read_spy  # over 1 MiB: never opened


@requires_db
async def test_unavailable_files(client: httpx.AsyncClient, alice: Owner, tmp_path: Path) -> None:
    root = _storage(alice)
    (root / "README.md").write_bytes(f"{ANTHROPIC}\n".encode())  # hash mismatch
    (root / "src" / "config.py").unlink()  # missing
    outside = tmp_path / "outside"
    shutil.copytree(root / "deploy", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "deploy")
    _junction(root / "deploy", outside)

    body = await _scan(client, alice)
    assert [(f["path"], f["rule"]) for f in body["findings"]] == [
        ("app/.env", "github-token"),
        ("app/.env", "slack-token"),
    ]
    assert body["summary"] == {
        "files_scanned": 1,
        "binary": 0,
        "too_large": 0,
        "unavailable": 3,
        "findings": 2,
    }
    _assert_no_secret_values(json.dumps(body))


@requires_db
async def test_symlinked_file_is_unavailable(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "src" / "config.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(FILES["src/config.py"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    body = await _scan(client, alice)
    assert "src/config.py" not in [f["path"] for f in body["findings"]]
    assert body["summary"]["unavailable"] == 1


@requires_db
async def test_file_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_FILES == 5000
    monkeypatch.setattr(secret_scan, "MAX_FILES", 3)
    files = {f"f{i}.txt": f"{AWS}\n".encode() for i in range(5)}
    await _ingest(client, alice, files)
    body = await _scan(client, alice)
    assert [f["path"] for f in body["findings"]] == ["f0.txt", "f1.txt", "f2.txt"]
    assert body["summary"]["files_scanned"] == 3
    assert body["truncated"] is True

    monkeypatch.setattr(secret_scan, "MAX_FILES", 5)  # exactly at the limit
    assert (await _scan(client, alice))["truncated"] is False


@requires_db
async def test_byte_budget_is_checked_before_reading(
    client: httpx.AsyncClient,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    read_spy: list[str],
) -> None:
    assert MAX_TOTAL_BYTES == 64 * 1024 * 1024
    a = f"{AWS}\n".encode().ljust(60, b" ")
    b = f"{GITHUB}\n".encode().ljust(60, b" ")
    huge = os.urandom(MAX_CONTENT_BYTES + 1)  # never read, so it uses no budget
    await _ingest(client, alice, {"a.txt": a, "b.txt": b, "big.txt": huge})
    monkeypatch.setattr(secret_scan, "MAX_TOTAL_BYTES", 100)
    body = await _scan(client, alice)
    assert [f["path"] for f in body["findings"]] == ["a.txt"]
    assert body["summary"] == {
        "files_scanned": 1,
        "binary": 0,
        "too_large": 0,
        "unavailable": 0,
        "findings": 1,
    }
    assert body["truncated"] is True
    assert read_spy == ["a.txt"]  # b.txt was never opened

    read_spy.clear()
    monkeypatch.setattr(secret_scan, "MAX_TOTAL_BYTES", 120)  # exactly enough
    body = await _scan(client, alice)
    assert [f["path"] for f in body["findings"]] == ["a.txt", "b.txt"]
    assert body["summary"]["too_large"] == 1
    assert body["truncated"] is False
    assert read_spy == ["a.txt", "b.txt"]


@requires_db
async def test_findings_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_FINDINGS == 500
    monkeypatch.setattr(secret_scan, "MAX_FINDINGS", 3)
    await _ingest(
        client,
        alice,
        {"a.txt": f"{AWS}\n{AWS}\n".encode(), "b.txt": f"{AWS}\n{AWS}\n".encode()},
    )
    body = await _scan(client, alice)
    assert [(f["path"], f["line"]) for f in body["findings"]] == [
        ("a.txt", 1),
        ("a.txt", 2),
        ("b.txt", 1),
    ]
    assert body["summary"]["findings"] == 3
    assert body["truncated"] is True

    monkeypatch.setattr(secret_scan, "MAX_FINDINGS", 4)  # exactly at the limit
    body = await _scan(client, alice)
    assert (len(body["findings"]), body["truncated"]) == (4, False)


@requires_db
async def test_reingestion_shows_the_new_snapshot(client: httpx.AsyncClient, alice: Owner) -> None:
    await _ingest(client, alice, {"only.txt": f"x\n{STRIPE}\n".encode()})
    body = await _scan(client, alice)
    assert [(f["path"], f["line"]) for f in body["findings"]] == [("only.txt", 2)]


@requires_db
async def test_ownership_and_validation(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"bob.txt": f"{AWS}\n".encode()})
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
        await client.get(_url(alice), headers={"Authorization": "Bearer not-a-token"})
    ).status_code == 401
    assert (
        await client.get(_url(alice, repository_id="not-a-uuid"), headers=alice.headers)
    ).status_code == 422
    assert (
        await client.get(_url(alice, project_id="not-a-uuid"), headers=alice.headers)
    ).status_code == 422


# --- Rate limiting -------------------------------------------------------------------------


class _AlwaysLimited(RateLimiter):
    def __init__(self) -> None:
        super().__init__(None)
        self.hits: list[tuple[Limit, str]] = []

    async def hit(self, limit: Limit, value: str) -> None:
        self.hits.append((limit, value))
        raise too_many_attempts(1000)


@requires_db
async def test_ownership_is_checked_before_rate_limiting(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    bob = await _owner(client, "bob@example.com", {"bob.txt": b"x\n"})
    limiter = _AlwaysLimited()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    for url in (_url(bob), _url(alice, repository_id=str(uuid.uuid4()))):
        response = await client.get(url, headers=alice.headers)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert limiter.hits == []

    limited = await client.get(_url(alice), headers=alice.headers)
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts; please try again later"}
    assert limiter.hits == [(SECRET_SCAN_LIMIT, alice.user_id)]
    assert read_spy == []  # the limited request scanned nothing


@requires_db_and_redis
async def test_rate_limit_is_30_per_user_per_hour(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert SECRET_SCAN_LIMIT == Limit("secret-scan-user", 30, 3600)
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(settings)
    await pool.flushdb()
    app.dependency_overrides.pop(get_rate_limiter, None)  # use the real Step 17 limiter
    try:
        for _ in range(30):
            assert (await client.get(_url(alice), headers=alice.headers)).status_code == 200
        limited = await client.get(_url(alice), headers=alice.headers)
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts; please try again later"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 3600
        keys = [k.decode() if isinstance(k, bytes) else k for k in await pool.keys("*")]
        assert any(k.startswith("ratelimit:secret-scan-user:") for k in keys)
        assert not any(alice.user_id in k for k in keys)  # hashed, never the raw user id
    finally:
        await pool.flushdb()
        await pool.aclose()


@requires_db
async def test_redis_outage_fails_open(client: httpx.AsyncClient, alice: Owner) -> None:
    class BrokenRedis:
        async def eval(self, *args: Any) -> Any:
            raise RedisConnectionError("simulated outage")

    app.dependency_overrides[get_rate_limiter] = lambda: RateLimiter(BrokenRedis())  # type: ignore[arg-type]
    for _ in range(32):  # beyond the limit
        assert (await client.get(_url(alice), headers=alice.headers)).status_code == 200
