"""Python file outline tests (GET .../files/outline?path=...). Fully offline.

Parser tests need no database. API tests run against the already-migrated throwaway test
database (TEST_DATABASE_URL, "_test") inside rolled-back transactions; ingestion runs
inline with the real worker logic and DATA_DIR is a temporary directory. The rate-limit
test uses a disposable Redis (TEST_REDIS_URL). Junction tests are Windows-only; symlink
tests are skipped where symlinks cannot be created.
"""

import ast
import builtins
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
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
from app.services import code_outline, ingestion
from app.services.code_outline import (
    MAX_DEPTH,
    MAX_NAME_CHARS,
    MAX_OUTLINE_BYTES,
    MAX_SYMBOLS,
    OUTLINE_LIMIT,
    outline_source,
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
FILE_NOT_FOUND = {"detail": "File not found"}
UNAVAILABLE = {"detail": "File content is not available; try again later"}

SOURCE = '''import os


@decorator
class Widget(Base):
    """Doc."""

    def __init__(self):
        self.x = 1

    async def fetch(self):
        def helper():
            return 1
        return helper()

    class Inner:
        def method(self):
            pass


async def main():
    pass


def outer():
    class Local:
        pass
    return Local
'''

# (kind, async, name, line, end_line, depth) in source order.
EXPECTED = [
    ("class", False, "Widget", 5, 18, 0),
    ("method", False, "__init__", 8, 9, 1),
    ("method", True, "fetch", 11, 14, 1),
    ("function", False, "helper", 12, 13, 2),
    ("class", False, "Inner", 16, 18, 1),
    ("method", False, "method", 17, 18, 2),
    ("function", True, "main", 21, 22, 0),
    ("function", False, "outer", 25, 28, 0),
    ("class", False, "Local", 26, 27, 1),
]

STUB = b"def f(x: int) -> int: ...\nclass C:\n    def m(self) -> None: ...\n"


def _tuples(symbols: list[Any]) -> list[tuple[Any, ...]]:
    return [(s.kind, s.is_async, s.name, s.line, s.end_line, s.depth) for s in symbols]


def _json_tuples(symbols: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (s["kind"], s["async"], s["name"], s["line"], s["end_line"], s["depth"]) for s in symbols
    ]


# --- Parser (no database) ------------------------------------------------------------------


def test_classes_functions_methods_and_async() -> None:
    status, symbols, truncated = outline_source(SOURCE)
    assert (status, truncated) == ("parsed", False)
    assert _tuples(symbols) == EXPECTED


def test_decorated_definitions_report_the_def_line() -> None:
    source = "@a\n@b(1)\ndef f():\n    pass\n\n\n@dataclass\nclass C:\n    x: int\n"
    _, symbols, _ = outline_source(source)
    assert _tuples(symbols) == [
        ("function", False, "f", 3, 4, 0),
        ("class", False, "C", 8, 9, 0),
    ]


def test_conditional_and_non_definition_statements_are_not_outlined() -> None:
    source = (
        "if TYPE_CHECKING:\n    def hidden(): ...\n"
        "try:\n    class Maybe: ...\nexcept ImportError:\n    pass\n"
        "square = lambda x: x * x\nVALUE = 1\n"
        "def shown():\n    with ctx:\n        def inner_hidden(): ...\n"
    )
    _, symbols, _ = outline_source(source)
    assert _tuples(symbols) == [("function", False, "shown", 9, 11, 0)]


def test_depth_limit() -> None:
    assert MAX_DEPTH == 8
    levels = 12
    source = "".join("    " * i + f"def level{i}():\n" for i in range(levels))
    source += "    " * levels + "pass\n"
    status, symbols, truncated = outline_source(source)
    assert status == "parsed"
    assert [(s.name, s.depth) for s in symbols] == [(f"level{i}", i) for i in range(MAX_DEPTH)]
    assert truncated is True

    exactly = "".join("    " * i + f"def level{i}():\n" for i in range(MAX_DEPTH))
    exactly += "    " * MAX_DEPTH + "pass\n"
    _, symbols, truncated = outline_source(exactly)
    assert (len(symbols), truncated) == (MAX_DEPTH, False)


def test_symbol_limit() -> None:
    assert MAX_SYMBOLS == 2000
    source = "".join(f"def f{i}(): pass\n" for i in range(MAX_SYMBOLS + 1))
    status, symbols, truncated = outline_source(source)
    assert status == "parsed"
    assert len(symbols) == MAX_SYMBOLS
    assert (symbols[0].name, symbols[-1].name) == ("f0", f"f{MAX_SYMBOLS - 1}")
    assert truncated is True

    exactly = "".join(f"def f{i}(): pass\n" for i in range(MAX_SYMBOLS))
    _, symbols, truncated = outline_source(exactly)
    assert (len(symbols), truncated) == (MAX_SYMBOLS, False)


def test_names_are_capped() -> None:
    assert MAX_NAME_CHARS == 200
    _, symbols, _ = outline_source("def " + "a" * 300 + "(): pass\n")
    assert symbols[0].name == "a" * MAX_NAME_CHARS


def test_unicode_identifiers() -> None:
    _, symbols, _ = outline_source(
        "def grüße():\n    pass\nclass Ωmega:\n    def 変換(self): ...\n"
    )
    assert [(s.kind, s.name) for s in symbols] == [
        ("function", "grüße"),
        ("class", "Ωmega"),
        ("method", "変換"),
    ]


def test_empty_and_stub_sources() -> None:
    assert outline_source("") == ("parsed", [], False)
    assert outline_source("# only a comment\n") == ("parsed", [], False)
    _, symbols, _ = outline_source(STUB.decode())
    assert _tuples(symbols) == [
        ("function", False, "f", 1, 1, 0),
        ("class", False, "C", 2, 3, 0),
        ("method", False, "m", 3, 3, 1),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "def (:\n",
        "x = 1\x00\n",
        "(" * 201 + ")" * 201,
        "x = " + "+".join(["1"] * 100_000),
        "".join("    " * i + f"def f{i}():\n" for i in range(101)) + "    " * 101 + "pass\n",
        "def f():\nreturn 1\n",
        "class C:\n  def a(self): pass\n    def b(self): pass\n",
    ],
    ids=["syntax", "null-byte", "nested-parens", "long-chain", "deep-indent", "indent", "dedent"],
)
def test_invalid_and_hostile_sources_are_syntax_errors(source: str) -> None:
    started = time.perf_counter()
    assert outline_source(source) == ("syntax_error", [], False)
    assert time.perf_counter() - started < 5.0


def test_parsing_never_compiles_to_bytecode_or_executes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "executed"
    source = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "import os\nos.system('echo hi')\n"
        "__import__('subprocess').run(['echo'])\n"
        "def f():\n    raise SystemExit\n"
    )
    compiled_flags: list[int] = []
    real_compile = builtins.compile

    def watching_compile(*args: Any, **kwargs: Any) -> Any:
        flags = kwargs.get("flags", args[3] if len(args) > 3 else 0)
        compiled_flags.append(flags)
        return real_compile(*args, **kwargs)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("repository code must never run")

    monkeypatch.setattr(builtins, "compile", watching_compile)
    monkeypatch.setattr(builtins, "exec", refuse)
    monkeypatch.setattr(builtins, "eval", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    status, symbols, _ = outline_source(source)
    assert (status, [s.name for s in symbols]) == ("parsed", ["f"])
    assert not marker.exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


def test_outline_is_stateless_across_threads() -> None:
    sources = [SOURCE, STUB.decode(), "def (:\n", "def a(): pass\n" * 50] * 8
    expected = [outline_source(source) for source in sources]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(outline_source, sources)) == expected


# --- API fixtures ----------------------------------------------------------------------------

FILES: dict[str, bytes] = {
    "app/widget.py": SOURCE.encode(),
    "stubs/api.pyi": STUB,
    "SCRIPT.PY": b"def upper_case():\n    pass\n",
    "empty.py": b"",
    "bad.py": b"def (:\n",
    "bin/blob.py": b"\x00\x01\x02 def f(): pass",
    "bin/latin1.py": b"\xff\xfe def f(): pass",
    "README.md": b"# def not_python(): pass\n",
    "web/app.js": b"function f() {}\n",
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
        f"/repositories/{repository_id or owner.repository_id}/files/outline"
    )


async def _get(
    client: httpx.AsyncClient, owner: Owner, path: str | None, **kw: Any
) -> httpx.Response:
    params = {} if path is None else {"path": path}
    url = _url(owner, kw.pop("project_id", None), kw.pop("repository_id", None))
    return await client.get(url, params=params, headers=kw.pop("headers", owner.headers))


async def _outline(client: httpx.AsyncClient, owner: Owner, path: str) -> dict[str, Any]:
    response = await _get(client, owner, path)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_outline.read_file_content

    async def spy(db: AsyncSession, repository: Any, path: str) -> Any:
        reads.append(path)
        return await real(db, repository, path)

    monkeypatch.setattr(code_outline, "read_file_content", spy)
    return reads


@pytest.fixture
def parse_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    parsed: list[int] = []
    real = code_outline.outline_source

    def spy(source: str) -> Any:
        parsed.append(len(source))
        return real(source)

    monkeypatch.setattr(code_outline, "outline_source", spy)
    return parsed


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_outline_of_a_python_file(client: httpx.AsyncClient, alice: Owner) -> None:
    body = await _outline(client, alice, "app/widget.py")
    assert set(body) == {"path", "status", "symbols", "truncated"}
    assert (body["path"], body["status"], body["truncated"]) == ("app/widget.py", "parsed", False)
    assert _json_tuples(body["symbols"]) == EXPECTED
    for symbol in body["symbols"]:
        assert set(symbol) == {"kind", "async", "name", "line", "end_line", "depth"}
    assert body["symbols"][0] == {
        "kind": "class",
        "async": False,
        "name": "Widget",
        "line": 5,
        "end_line": 18,
        "depth": 0,
    }


@requires_db
async def test_stub_uppercase_and_empty_files(client: httpx.AsyncClient, alice: Owner) -> None:
    stub = await _outline(client, alice, "stubs/api.pyi")
    assert (stub["status"], [s["name"] for s in stub["symbols"]]) == ("parsed", ["f", "C", "m"])
    upper = await _outline(client, alice, "SCRIPT.PY")
    assert (upper["status"], [s["name"] for s in upper["symbols"]]) == ("parsed", ["upper_case"])
    assert await _outline(client, alice, "empty.py") == {
        "path": "empty.py",
        "status": "parsed",
        "symbols": [],
        "truncated": False,
    }


@requires_db
async def test_syntax_error_file(client: httpx.AsyncClient, alice: Owner) -> None:
    assert await _outline(client, alice, "bad.py") == {
        "path": "bad.py",
        "status": "syntax_error",
        "symbols": [],
        "truncated": False,
    }


@requires_db
async def test_binary_files(client: httpx.AsyncClient, alice: Owner, parse_spy: list[int]) -> None:
    for path in ("bin/blob.py", "bin/latin1.py"):
        assert await _outline(client, alice, path) == {
            "path": path,
            "status": "binary",
            "symbols": [],
            "truncated": False,
        }
    assert parse_spy == []


@requires_db
async def test_unsupported_languages_are_never_read(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str], parse_spy: list[int]
) -> None:
    for path in ("README.md", "web/app.js"):
        assert await _outline(client, alice, path) == {
            "path": path,
            "status": "unsupported_language",
            "symbols": [],
            "truncated": False,
        }
    assert read_spy == [] and parse_spy == []
    assert (await _outline(client, alice, "app/widget.py"))["status"] == "parsed"
    assert read_spy == ["app/widget.py"]


@requires_db
async def test_text_size_limit(
    client: httpx.AsyncClient, alice: Owner, parse_spy: list[int]
) -> None:
    assert MAX_OUTLINE_BYTES == 256 * 1024
    # Random names keep the upload under the archive compression-ratio limit.
    lines = [
        f"def f{os.urandom(8).hex()}(): pass\n".encode() for _ in range(MAX_OUTLINE_BYTES // 30)
    ]
    exactly = b"".join(lines) + b"#" * (MAX_OUTLINE_BYTES % 30)
    assert len(exactly) == MAX_OUTLINE_BYTES
    await _ingest(
        client,
        alice,
        {
            "exact.py": exactly,
            "over.py": exactly + b"\n",
            "huge.py": os.urandom(MAX_CONTENT_BYTES + 1),
        },
    )
    over = await _outline(client, alice, "over.py")
    assert over == {"path": "over.py", "status": "too_large", "symbols": [], "truncated": False}
    huge = await _outline(client, alice, "huge.py")
    assert (huge["status"], huge["symbols"]) == ("too_large", [])
    assert parse_spy == []  # neither was parsed
    exact = await _outline(client, alice, "exact.py")
    assert exact["status"] == "parsed"
    assert len(exact["symbols"]) == MAX_SYMBOLS and exact["truncated"] is True
    assert parse_spy == [MAX_OUTLINE_BYTES]


@requires_db
async def test_unknown_and_unavailable_files(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    for path in ("missing.py", "../etc/passwd", "app/../app/widget.py", "APP/WIDGET.PY"):
        response = await _get(client, alice, path)
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)

    root = _storage(alice)
    (root / "bad.py").write_bytes(b"def tampered(): pass\n")  # hash mismatch
    (root / "empty.py").unlink()  # missing
    outside = tmp_path / "outside"
    shutil.copytree(root / "stubs", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "stubs")
    _junction(root / "stubs", outside)
    for path in ("bad.py", "empty.py", "stubs/api.pyi"):
        response = await _get(client, alice, path)
        assert (response.status_code, response.json()) == (409, UNAVAILABLE)
        assert "tampered" not in response.text


@requires_db
async def test_symlinked_file_is_unavailable(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "app" / "widget.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(FILES["app/widget.py"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    response = await _get(client, alice, "app/widget.py")
    assert (response.status_code, response.json()) == (409, UNAVAILABLE)


@requires_db
async def test_validation(client: httpx.AsyncClient, alice: Owner, read_spy: list[str]) -> None:
    assert (await _get(client, alice, None)).status_code == 422  # missing path
    assert (await _get(client, alice, "")).status_code == 422
    assert (await _get(client, alice, "a" * 4097)).status_code == 422
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _get(client, alice, "app/widget.py", **kwargs)).status_code == 422
    assert read_spy == []


@requires_db
async def test_malicious_file_is_parsed_not_executed(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('pwned')\n"
        "import socket; socket.create_connection(('example.invalid', 80))\n"
        "__import__('os').system('echo pwned')\n"
        "class Evil:\n    def __del__(self):\n        raise SystemExit\n"
        "def run():\n    exec('1')\n"
    ).encode()
    await _ingest(client, alice, {"evil.py": evil})

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("repository code must never run")

    for target, name in (
        (builtins, "exec"),
        (builtins, "eval"),
        (os, "system"),
        (subprocess, "Popen"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
    ):
        monkeypatch.setattr(target, name, refuse)
    body = await _outline(client, alice, "evil.py")
    assert [(s["kind"], s["name"]) for s in body["symbols"]] == [
        ("class", "Evil"),
        ("method", "__del__"),
        ("function", "run"),
    ]
    assert not marker.exists()


@requires_db
async def test_no_internals_or_content_are_exposed_or_logged(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    content_marker = "UNIQUE-CONTENT-MARKER-91c2"
    await _ingest(
        client,
        alice,
        {"m.py": f"SECRET = '{content_marker}'\n\ndef visible_name():\n    pass\n".encode()},
    )
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, "m.py")
    assert response.status_code == 200
    assert [s["name"] for s in response.json()["symbols"]] == ["visible_name"]
    assert content_marker not in response.text
    assert content_marker not in caplog.text
    assert "visible_name" not in caplog.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text


@requires_db
async def test_reingestion_shows_the_new_snapshot(client: httpx.AsyncClient, alice: Owner) -> None:
    await _ingest(client, alice, {"app/widget.py": b"def replaced():\n    pass\n"})
    body = await _outline(client, alice, "app/widget.py")
    assert [s["name"] for s in body["symbols"]] == ["replaced"]
    response = await _get(client, alice, "stubs/api.pyi")
    assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"bob.py": b"def bobs(): pass\n"})
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    for path, kwargs in (
        ("bob.py", {"project_id": bob.project_id, "repository_id": bob.repository_id}),
        ("app/widget.py", {"project_id": other_project}),
        ("app/widget.py", {"repository_id": str(uuid.uuid4())}),
    ):
        response = await _get(client, alice, path, **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert (await client.get(_url(alice), params={"path": "app/widget.py"})).status_code == 401
    bad = await _get(client, alice, "app/widget.py", headers={"Authorization": "Bearer nope"})
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
    bob = await _owner(client, "bob@example.com", {"bob.py": b"def bobs(): pass\n"})
    limiter = _AlwaysLimited()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    for kwargs in (
        {"project_id": bob.project_id, "repository_id": bob.repository_id},
        {"repository_id": str(uuid.uuid4())},
    ):
        response = await _get(client, alice, "bob.py", **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert limiter.hits == []

    limited = await _get(client, alice, "app/widget.py")
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts; please try again later"}
    assert limiter.hits == [(OUTLINE_LIMIT, alice.user_id)]
    assert read_spy == []  # the limited request read nothing


@requires_db_and_redis
async def test_rate_limit_is_120_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert OUTLINE_LIMIT == Limit("outline-user", 120, 600)
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(settings)
    await pool.flushdb()
    app.dependency_overrides.pop(get_rate_limiter, None)  # use the real Step 17 limiter
    try:
        for _ in range(120):
            assert (await _get(client, alice, "README.md")).status_code == 200
        limited = await _get(client, alice, "README.md")
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts; please try again later"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 600
        keys = [k.decode() if isinstance(k, bytes) else k for k in await pool.keys("*")]
        assert any(k.startswith("ratelimit:outline-user:") for k in keys)
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
    for _ in range(122):  # beyond the limit
        assert (await _get(client, alice, "README.md")).status_code == 200
