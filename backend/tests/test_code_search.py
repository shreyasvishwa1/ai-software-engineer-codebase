"""Code search tests (POST .../search). Fully offline.

Matcher tests need no database. API tests run against the already-migrated throwaway test
database (TEST_DATABASE_URL, "_test") inside rolled-back transactions; ingestion runs
inline with the real worker logic and DATA_DIR is a temporary directory. The rate-limit
test uses a disposable Redis (TEST_REDIS_URL). Junction tests are Windows-only; symlink
tests are skipped where symlinks cannot be created.
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
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
from app.services import code_search, ingestion
from app.services.code_search import (
    CODE_SEARCH_LIMIT,
    MAX_FILES,
    MAX_LINE_CHARS,
    MAX_MATCHES,
    MAX_TOTAL_BYTES,
    PREVIEW_CHARS,
    search_text,
)
from app.services.file_content import MAX_CONTENT_BYTES
from app.services.rate_limit import Limit, RateLimiter, get_rate_limiter, too_many_attempts
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

FILES: dict[str, bytes] = {
    "README.md": b"# Demo\nLoad the config with load_config().\n",
    "src/app.py": b"import os\n\n\ndef load_config(path):\n    return LOAD_CONFIG_DEFAULT\n",
    "src/util.py": b"def helper():\r\n    pass\r\n# see load_config\r\n",
    "docs/notes.txt": b"nothing relevant here\n",
}


# --- Matcher (no database) -----------------------------------------------------------------


def test_case_insensitive_literal_matching() -> None:
    text = "Load_Config here\nload_config there\nLOAD_CONFIG everywhere\nloadconfig no"
    assert [line for line, _ in search_text(text, "load_config")] == [1, 2, 3]
    assert [line for line, _ in search_text(text, "LOAD_config")] == [1, 2, 3]
    assert search_text("ab", "abc") == []


def test_one_result_per_line() -> None:
    assert search_text("foo foo FOO foo", "foo") == [(1, "foo foo FOO foo")]


def test_line_numbers_are_one_based() -> None:
    assert search_text("needle", "needle") == [(1, "needle")]
    assert [line for line, _ in search_text("a\n\nb needle\n\nneedle c\n", "needle")] == [3, 5]


def test_crlf_lines_and_previews() -> None:
    hits = search_text("first\r\nthe needle\r\n\r\nNEEDLE again\r\n", "needle")
    assert hits == [(2, "the needle"), (4, "NEEDLE again")]
    assert all("\r" not in preview for _, preview in hits)


@pytest.mark.parametrize("query", [".*", "(", "[", r"\d", "a+", "a|b", "^x$", "(?i)"])
def test_regex_metacharacters_are_literal(query: str) -> None:
    text = f"plain text 123 aaa b\nliteral {query} here\nx"
    assert search_text(text, query) == [(2, f"literal {query} here")]


def test_glob_characters_are_literal() -> None:
    assert search_text("file.py\n*.py here\nfile?py", "*.py") == [(2, "*.py here")]
    assert search_text("file.py\nfile?py", "file?py") == [(2, "file?py")]


def test_only_the_first_4096_characters_are_searched() -> None:
    assert MAX_LINE_CHARS == 4096
    fits = "x" * (MAX_LINE_CHARS - 6) + "needle"
    cut = "x" * (MAX_LINE_CHARS - 5) + "needle"  # last character falls outside
    assert [line for line, _ in search_text(fits, "needle")] == [1]
    assert search_text(cut, "needle") == []
    assert search_text("x" * MAX_LINE_CHARS + "needle", "needle") == []
    assert [line for line, _ in search_text(f"{cut}\nneedle", "needle")] == [2]


def test_preview_window_and_cap() -> None:
    assert PREVIEW_CHARS == 200
    short = "    return needle  # trailing"
    assert search_text(short, "needle") == [(1, short)]

    line = "".join(chr(ord("a") + i % 26) for i in range(1000))
    middle = "a" * 500 + "NEEDLE" + "b" * 500
    ((_, preview),) = search_text(middle, "needle")
    assert len(preview) == PREVIEW_CHARS
    assert preview.startswith("a" * 60 + "NEEDLE")  # starts 60 characters before the match
    assert preview == middle[440:640]

    start = "NEEDLE" + "c" * 1000
    assert search_text(start, "needle") == [(1, start[:PREVIEW_CHARS])]
    end = "d" * 1000 + "NEEDLE"
    assert search_text(end, "needle") == [(1, end[-PREVIEW_CHARS:])]
    for _, preview in search_text(f"{line}\n{middle}\n{start}\n{end}", "e"):
        assert len(preview) <= PREVIEW_CHARS


def test_preview_window_near_the_line_limit() -> None:
    line = "e" * (MAX_LINE_CHARS - 10) + "needle" + "f" * 5000
    ((_, preview),) = search_text(line, "needle")
    assert len(preview) == PREVIEW_CHARS
    assert preview.endswith("needle" + "f" * 4)  # never beyond the searched segment


def test_unicode_text() -> None:
    text = "Grüße aus München\nΣΊΣΥΦΟΣ\nnaïve café\n日本語のテキスト"
    assert search_text(text, "MÜNCHEN") == [(1, "Grüße aus München")]
    assert search_text(text, "σίσυφος") == [(2, "ΣΊΣΥΦΟΣ")]
    assert search_text(text, "CAFÉ") == [(3, "naïve café")]
    assert search_text(text, "テキスト") == [(4, "日本語のテキスト")]
    # Lowercasing U+0130 yields two characters; the preview still centers on the match.
    long = "İ" * 300 + "needle" + "z" * 300
    ((_, preview),) = search_text(long, "needle")
    assert len(preview) == PREVIEW_CHARS
    assert preview == long[240:440]
    assert preview.startswith("İ" * 60 + "needle")


def test_pathological_lines_stay_fast() -> None:
    text = "\n".join(["a" * MAX_LINE_CHARS] * 1000)
    started = time.perf_counter()
    assert search_text(text, "a" * 199 + "b") == []
    assert search_text(text, "a" * 199 + "B") == []
    assert time.perf_counter() - started < 2.0  # linear: a backtracking matcher takes ~10 s


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
        f"/repositories/{repository_id or owner.repository_id}/search"
    )


async def _post(client: httpx.AsyncClient, owner: Owner, query: str, **kw: Any) -> httpx.Response:
    url = _url(owner, kw.pop("project_id", None), kw.pop("repository_id", None))
    return await client.post(url, json={"query": query}, headers=kw.pop("headers", owner.headers))


async def _search(client: httpx.AsyncClient, owner: Owner, query: str) -> dict[str, Any]:
    response = await _post(client, owner, query)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_search._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_search, "_read_verified", spy)
    return reads


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


def _summary(scanned: int, binary: int, too_large: int, unavailable: int, matches: int) -> dict:
    return {
        "files_scanned": scanned,
        "binary": binary,
        "too_large": too_large,
        "unavailable": unavailable,
        "matches": matches,
    }


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_search_across_files(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    body = await _search(client, alice, "load_config")
    assert body == {
        "matches": [
            {"path": "README.md", "line": 2, "preview": "Load the config with load_config()."},
            {"path": "src/app.py", "line": 4, "preview": "def load_config(path):"},
            {"path": "src/app.py", "line": 5, "preview": "    return LOAD_CONFIG_DEFAULT"},
            {"path": "src/util.py", "line": 3, "preview": "# see load_config"},
        ],
        "summary": _summary(4, 0, 0, 0, 4),
        "truncated": False,
    }
    assert sorted(read_spy) == sorted(FILES)  # every file went through the verified reader


@requires_db
async def test_results_are_sorted_by_path_and_line(client: httpx.AsyncClient, alice: Owner) -> None:
    files = {
        "z.txt": b"hit\n",
        "a/b.txt": b"x\nhit\nx\nhit\n",
        "a.txt": b"hit\n",
        "B.txt": b"x\nx\nhit\n",
    }
    await _ingest(client, alice, files)
    first = await _post(client, alice, "HIT")
    second = await _post(client, alice, "hit")
    assert first.json() == second.json()
    assert [(m["path"], m["line"]) for m in first.json()["matches"]] == [
        ("B.txt", 3),
        ("a.txt", 1),
        ("a/b.txt", 2),
        ("a/b.txt", 4),
        ("z.txt", 1),
    ]


@requires_db
async def test_no_matches(client: httpx.AsyncClient, alice: Owner) -> None:
    body = await _search(client, alice, "definitely absent")
    assert body == {"matches": [], "summary": _summary(4, 0, 0, 0, 0), "truncated": False}


@requires_db
async def test_regex_queries_are_literal_end_to_end(
    client: httpx.AsyncClient, alice: Owner
) -> None:
    await _ingest(client, alice, {"a.txt": b"aaa\nx = re.compile(r'.*')\n123\n"})
    body = await _search(client, alice, ".*")
    assert [(m["path"], m["line"]) for m in body["matches"]] == [("a.txt", 2)]
    assert (await _search(client, alice, r"\d"))["matches"] == []


@requires_db
@pytest.mark.parametrize(
    "body",
    [
        {"query": "a"},  # too short
        {"query": "x" * 201},  # too long
        {"query": "   "},  # whitespace only
        {"query": "\t \t"},
        {"query": "two\nlines"},
        {"query": "carriage\rreturn"},
        {"query": "nul\x00byte"},
        {},  # missing
        {"query": "valid", "extra": True},  # extra field
        {"query": 42},  # not a string
        {"query": None},
        ["query"],
    ],
    ids=[
        "short",
        "long",
        "spaces",
        "tabs",
        "newline",
        "carriage-return",
        "nul",
        "missing",
        "extra-field",
        "number",
        "null",
        "array",
    ],
)
async def test_invalid_bodies_are_rejected(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str], body: Any
) -> None:
    response = await client.post(_url(alice), json=body, headers=alice.headers)
    assert response.status_code == 422, response.text
    assert read_spy == []


@requires_db
async def test_query_length_boundaries(client: httpx.AsyncClient, alice: Owner) -> None:
    assert (await _post(client, alice, "ab")).status_code == 200
    assert (await _post(client, alice, "x" * 200)).status_code == 200
    assert (await _post(client, alice, " a")).status_code == 200  # spaces are meaningful


@requires_db
async def test_malformed_requests(client: httpx.AsyncClient, alice: Owner) -> None:
    raw = await client.post(
        _url(alice),
        content=b"not json",
        headers={**alice.headers, "Content-Type": "application/json"},
    )
    assert raw.status_code == 422
    empty = await client.post(_url(alice), headers=alice.headers)
    assert empty.status_code == 422
    for url in (_url(alice, repository_id="not-a-uuid"), _url(alice, project_id="not-a-uuid")):
        assert (
            await client.post(url, json={"query": "ok"}, headers=alice.headers)
        ).status_code == 422
    # Query parameters are not an alternative way to search.
    query_param = await client.post(f"{_url(alice)}?query=load", headers=alice.headers)
    assert query_param.status_code == 422
    assert (await client.get(_url(alice), headers=alice.headers)).status_code == 405


@requires_db
async def test_unscannable_files_are_counted_not_failed(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    files = {
        "bin/blob.dat": b"\x00\x01needle\x00",
        "bin/latin1.txt": b"\xff\xfe needle",
        "big/huge.txt": b"needle\n" + os.urandom(MAX_CONTENT_BYTES),
        "ok.txt": b"needle\n",
    }
    await _ingest(client, alice, files)
    body = await _search(client, alice, "needle")
    assert [(m["path"], m["line"]) for m in body["matches"]] == [("ok.txt", 1)]
    assert body["summary"] == _summary(1, 2, 1, 0, 1)
    assert body["truncated"] is False
    assert "big/huge.txt" not in read_spy  # over 1 MiB: never opened


@requires_db
async def test_unavailable_files(client: httpx.AsyncClient, alice: Owner, tmp_path: Path) -> None:
    root = _storage(alice)
    (root / "README.md").write_bytes(b"tampered load_config\n")  # hash mismatch
    (root / "src" / "util.py").unlink()  # missing
    outside = tmp_path / "outside"
    shutil.copytree(root / "docs", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "docs")
    _junction(root / "docs", outside)

    body = await _search(client, alice, "load_config")
    assert [(m["path"], m["line"]) for m in body["matches"]] == [
        ("src/app.py", 4),
        ("src/app.py", 5),
    ]
    assert body["summary"] == _summary(1, 0, 0, 3, 2)
    assert "tampered" not in json.dumps(body)


@requires_db
async def test_symlinked_file_is_unavailable(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "src" / "app.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(FILES["src/app.py"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    body = await _search(client, alice, "load_config")
    assert "src/app.py" not in [m["path"] for m in body["matches"]]
    assert body["summary"]["unavailable"] == 1


@requires_db
async def test_file_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_FILES == 5000
    monkeypatch.setattr(code_search, "MAX_FILES", 3)
    await _ingest(client, alice, {f"f{i}.txt": b"hit\n" for i in range(5)})
    body = await _search(client, alice, "hit")
    assert [m["path"] for m in body["matches"]] == ["f0.txt", "f1.txt", "f2.txt"]
    assert body["summary"]["files_scanned"] == 3
    assert body["truncated"] is True

    monkeypatch.setattr(code_search, "MAX_FILES", 5)  # exactly at the limit
    assert (await _search(client, alice, "hit"))["truncated"] is False


@requires_db
async def test_byte_budget_is_checked_before_reading(
    client: httpx.AsyncClient,
    alice: Owner,
    monkeypatch: pytest.MonkeyPatch,
    read_spy: list[str],
) -> None:
    assert MAX_TOTAL_BYTES == 64 * 1024 * 1024
    a = b"hit in a\n".ljust(60, b" ")
    b = b"hit in b\n".ljust(60, b" ")
    huge = os.urandom(MAX_CONTENT_BYTES + 1)  # never read, so it uses no budget
    await _ingest(client, alice, {"a.txt": a, "b.txt": b, "big.txt": huge})
    monkeypatch.setattr(code_search, "MAX_TOTAL_BYTES", 100)
    body = await _search(client, alice, "hit")
    assert [m["path"] for m in body["matches"]] == ["a.txt"]
    assert body["summary"] == _summary(1, 0, 0, 0, 1)
    assert body["truncated"] is True
    assert read_spy == ["a.txt"]  # b.txt was never opened

    read_spy.clear()
    monkeypatch.setattr(code_search, "MAX_TOTAL_BYTES", 120)  # exactly enough
    body = await _search(client, alice, "hit")
    assert [m["path"] for m in body["matches"]] == ["a.txt", "b.txt"]
    assert body["summary"]["too_large"] == 1
    assert body["truncated"] is False
    assert read_spy == ["a.txt", "b.txt"]


@requires_db
async def test_match_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_MATCHES == 200
    monkeypatch.setattr(code_search, "MAX_MATCHES", 3)
    await _ingest(client, alice, {"a.txt": b"hit\nhit\n", "b.txt": b"hit\nhit\n"})
    body = await _search(client, alice, "hit")
    assert [(m["path"], m["line"]) for m in body["matches"]] == [
        ("a.txt", 1),
        ("a.txt", 2),
        ("b.txt", 1),
    ]
    assert body["summary"]["matches"] == 3
    assert body["truncated"] is True

    monkeypatch.setattr(code_search, "MAX_MATCHES", 4)  # exactly at the limit
    body = await _search(client, alice, "hit")
    assert (len(body["matches"]), body["truncated"]) == (4, False)


@requires_db
async def test_no_internals_or_query_are_exposed_or_logged(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = "UNIQUE-QUERY-MARKER-7f3a"
    await _ingest(client, alice, {"q.txt": f"has {query} inside\n".encode()})
    caplog.set_level(logging.DEBUG)
    response = await _post(client, alice, query)
    invalid = await _post(client, alice, f"{query}\n")
    assert response.status_code == 200 and invalid.status_code == 422
    assert response.json()["matches"] == [
        {"path": "q.txt", "line": 1, "preview": f"has {query} inside"}
    ]
    assert query not in caplog.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text
    for match in response.json()["matches"]:
        assert set(match) == {"path", "line", "preview"}


@requires_db
async def test_search_makes_no_outbound_connections_or_subprocesses(
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
    body = await _search(client, alice, "load_config")
    assert body["summary"]["matches"] == 4
    assert attempts == []


@requires_db
async def test_reingestion_shows_the_new_snapshot(client: httpx.AsyncClient, alice: Owner) -> None:
    await _ingest(client, alice, {"only.txt": b"x\nload_config again\n"})
    body = await _search(client, alice, "load_config")
    assert [(m["path"], m["line"]) for m in body["matches"]] == [("only.txt", 2)]
    assert body["summary"]["files_scanned"] == 1


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"bob.txt": b"load_config\n"})
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    for kwargs in (
        {"project_id": bob.project_id, "repository_id": bob.repository_id},  # alice's token
        {"project_id": other_project},
        {"repository_id": str(uuid.uuid4())},
    ):
        response = await _post(client, alice, "load_config", **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert (await client.post(_url(alice), json={"query": "load_config"})).status_code == 401
    bad = await _post(client, alice, "load_config", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


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
    for kwargs in (
        {"project_id": bob.project_id, "repository_id": bob.repository_id},
        {"repository_id": str(uuid.uuid4())},
    ):
        response = await _post(client, alice, "load_config", **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert limiter.hits == []

    limited = await _post(client, alice, "load_config")
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts; please try again later"}
    assert limiter.hits == [(CODE_SEARCH_LIMIT, alice.user_id)]
    assert read_spy == []  # the limited request searched nothing


@requires_db_and_redis
async def test_rate_limit_is_60_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert CODE_SEARCH_LIMIT == Limit("code-search-user", 60, 600)
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(settings)
    await pool.flushdb()
    app.dependency_overrides.pop(get_rate_limiter, None)  # use the real Step 17 limiter
    try:
        for _ in range(60):
            assert (await _post(client, alice, "load_config")).status_code == 200
        limited = await _post(client, alice, "load_config")
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts; please try again later"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 600
        keys = [k.decode() if isinstance(k, bytes) else k for k in await pool.keys("*")]
        assert any(k.startswith("ratelimit:code-search-user:") for k in keys)
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
    for _ in range(62):  # beyond the limit
        assert (await _post(client, alice, "load_config")).status_code == 200
