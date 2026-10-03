"""Python symbol search tests (POST .../symbols/search). Fully offline.

Scanner tests use a temporary storage directory and need no database. API tests run
against the already-migrated throwaway test database (TEST_DATABASE_URL, "_test") inside
rolled-back transactions; ingestion runs inline with the real worker logic and DATA_DIR is
a temporary directory. The rate-limit test uses a disposable Redis (TEST_REDIS_URL).
Junction tests are Windows-only; symlink tests are skipped where symlinks cannot be created.
"""

import ast
import builtins
import hashlib
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
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from arq import create_pool
from arq.connections import RedisSettings
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import event, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import get_db
from app.main import app
from app.models import File
from app.services import ingestion, symbol_search
from app.services.code_outline import MAX_DEPTH, MAX_NAME_CHARS, MAX_OUTLINE_BYTES, MAX_SYMBOLS
from app.services.file_content import MAX_CONTENT_BYTES, FileContentError
from app.services.rate_limit import Limit, RateLimiter, get_rate_limiter, too_many_attempts
from app.services.symbol_search import (
    MAX_RESULTS,
    SYMBOL_SEARCH_LIMIT,
    match_symbols,
    normalize_query,
    scan_symbols,
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
CHANGED = {"detail": "Repository changed during analysis; run it again"}

AUTH = """import os
from app import authenticate as login  # authenticate in a comment


@decorator(authenticate)
class Service:
    authenticate = 1

    def authenticate(self):
        return "authenticate"

    async def refresh(self):
        def authenticate():
            pass
        return authenticate


async def authenticate(user):
    handler = lambda authenticate: authenticate
    obj.authenticate = None
    return user


def authenticate_user():
    pass


class authenticate:
    pass


if TYPE_CHECKING:
    def authenticate():
        pass
"""

# (kind, async, line, end_line, depth) of every "authenticate" definition, in source order.
AUTH_MATCHES = [
    ("method", False, 9, 10, 1),
    ("function", False, 13, 14, 2),
    ("function", True, 18, 21, 0),
    ("class", False, 28, 29, 0),
]


def _write(root: Path, files: dict[str, str | bytes]) -> list[Any]:
    """Write ``files`` below ``root``; snapshot-like rows of the Python ones, by path."""
    rows = []
    for path, content in files.items():
        data = content.encode() if isinstance(content, str) else content
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path.lower().endswith((".py", ".pyi")):
            rows.append(
                SimpleNamespace(
                    path=path, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
                )
            )
    return sorted(rows, key=lambda row: row.path)


def _scan(tmp_path: Path, query: str, files: dict[str, str | bytes]) -> Any:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    return scan_symbols(root, _write(root, files), normalize_query(query))


def _hits(results: list[Any]) -> list[tuple[Any, ...]]:
    return [(r.path, r.kind, r.is_async, r.line, r.end_line, r.depth) for r in results]


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = symbol_search._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(symbol_search, "_read_verified", spy)
    return reads


@pytest.fixture
def parse_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    parsed: list[int] = []
    real = symbol_search.match_symbols

    def spy(source: str, query: str) -> Any:
        parsed.append(len(source))
        return real(source, query)

    monkeypatch.setattr(symbol_search, "match_symbols", spy)
    return parsed


# --- Matching (no database) ------------------------------------------------------------------


def test_every_definition_with_the_exact_name(tmp_path: Path) -> None:
    results, summary, truncated = _scan(tmp_path, "authenticate", {"auth.py": AUTH})
    assert [(k, a, line, end, d) for _, k, a, line, end, d in _hits(results)] == AUTH_MATCHES
    assert all(r.name == "authenticate" and r.path == "auth.py" for r in results)
    assert (summary.files_scanned, truncated) == (1, False)


def test_non_definitions_never_match(tmp_path: Path) -> None:
    # Variables, imports, aliases, strings, comments, attributes, lambdas, decorators and
    # conditional definitions (Step 37 does not walk if/try/with/for/while blocks).
    source = (
        "import login\nfrom x import y as login\nlogin = 1\nobj.login = 2\n"
        "f = lambda login: login\n# def login(): pass\ns = 'def login(): pass'\n"
        "@login\ndef other():\n    pass\n"
        "if True:\n    def login(): pass\ntry:\n    class login: pass\nexcept E:\n    pass\n"
        "with ctx:\n    def login(): pass\nfor i in x:\n    def login(): pass\n"
        "while 0:\n    def login(): pass\n"
    )
    assert _scan(tmp_path, "login", {"a.py": source})[0] == []


def test_case_sensitive_exact_names_only(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "class User:\n    pass\nclass user:\n    pass\nclass USER:\n    pass\n",
        "b.py": "def authenticate_user():\n    pass\ndef auth():\n    pass\n",
    }
    assert [(r.path, r.line) for r in _scan(tmp_path, "User", files)[0]] == [("a.py", 1)]
    assert [(r.path, r.line) for r in _scan(tmp_path, "user", files)[0]] == [("a.py", 3)]
    assert [(r.path, r.line) for r in _scan(tmp_path, "auth", files)[0]] == [("b.py", 3)]
    assert _scan(tmp_path, "authenticate", files)[0] == []  # prefix
    assert _scan(tmp_path, "uth", files)[0] == []  # substring
    assert _scan(tmp_path, "Auth", files)[0] == []


def test_same_name_definitions_are_never_merged(tmp_path: Path) -> None:
    source = (
        "def foo():\n    pass\nclass foo:\n    pass\n"
        "@overload\ndef foo(x: int) -> int: ...\n@overload\ndef foo(x: str) -> str: ...\n"
    )
    results, _, _ = _scan(tmp_path, "foo", {"b.py": source, "a.py": "def foo(): pass\n"})
    assert [(r.path, r.kind, r.line) for r in results] == [
        ("a.py", "function", 1),
        ("b.py", "function", 1),
        ("b.py", "class", 3),
        ("b.py", "function", 6),
        ("b.py", "function", 8),
    ]


def test_symbol_kinds_async_and_nesting(tmp_path: Path) -> None:
    source = (
        "class run:\n"  # 1
        "    def run(self): pass\n"  # 2
        "    async def run(self): pass\n"  # 3
        "    class run:\n"  # 4
        "        def run(self): pass\n"  # 5
        "def outer():\n"  # 6
        "    async def run(): pass\n"  # 7
        "    class run: pass\n"  # 8
    )
    results, _, _ = _scan(tmp_path, "run", {"a.py": source})
    assert [(r.kind, r.is_async, r.line, r.depth) for r in results] == [
        ("class", False, 1, 0),
        ("method", False, 2, 1),
        ("method", True, 3, 1),
        ("class", False, 4, 1),
        ("method", False, 5, 2),
        ("function", True, 7, 1),
        ("class", False, 8, 1),
    ]


def test_depth_boundary_follows_step_37(tmp_path: Path) -> None:
    assert MAX_DEPTH == 8
    deep = (
        "".join("    " * i + "def deep():\n" for i in range(MAX_DEPTH + 2))
        + "    " * (MAX_DEPTH + 2)
        + "pass\n"
    )
    results, _, truncated = _scan(tmp_path, "deep", {"a.py": deep})
    assert [r.depth for r in results] == list(range(MAX_DEPTH))
    assert truncated is True
    exact = (
        "".join("    " * i + "def deep():\n" for i in range(MAX_DEPTH))
        + "    " * MAX_DEPTH
        + "pass\n"
    )
    results, _, truncated = _scan(tmp_path, "deep", {"a.py": exact})
    assert (len(results), truncated) == (MAX_DEPTH, False)


# --- Unicode, NFKC and long names --------------------------------------------------------


def test_parser_normalizes_identifiers_with_nfkc() -> None:
    # The actual parser behavior the search relies on.
    assert ast.parse("def \uff41\uff55\uff54\uff48(): pass").body[0].name == "auth"  # type: ignore[attr-defined]
    assert ast.parse("def \ufb01le(): pass").body[0].name == "file"  # type: ignore[attr-defined]
    assert normalize_query("\uff41\uff55\uff54\uff48") == "auth"
    assert normalize_query("stra\u00dfe") == "stra\u00dfe"  # NFKC is not case folding


def test_nfkc_equivalent_spellings_match(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "ascii.py": "def auth():\n    pass\n",
        "wide.py": "# caf\u00e9\ndef \uff41\uff55\uff54\uff48():\n    pass\n",  # defines "auth"
        "lig.py": "def \ufb01le():\n    pass\n",  # defines "file"
    }
    assert [r.path for r in _scan(tmp_path, "auth", files)[0]] == ["ascii.py", "wide.py"]
    assert [r.path for r in _scan(tmp_path, "\uff41\uff55\uff54\uff48", files)[0]] == [
        "ascii.py",
        "wide.py",
    ]
    assert [(r.path, r.name) for r in _scan(tmp_path, "file", files)[0]] == [("lig.py", "file")]
    assert [r.path for r in _scan(tmp_path, "\ufb01le", files)[0]] == ["lig.py"]


def test_normalization_never_folds_case_or_letters(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "def strasse():\n    pass\ndef Auth():\n    pass\ndef \u00c4pfel():\n    pass\n",
    }
    assert _scan(tmp_path, "stra\u00dfe", files)[0] == []  # not "strasse"
    assert _scan(tmp_path, "\uff41uth", files)[0] == []  # NFKC gives "auth", not "Auth"
    assert [r.line for r in _scan(tmp_path, "\uff21uth", files)[0]] == [3]  # fullwidth A -> "Auth"
    assert _scan(tmp_path, "\u00e4pfel", files)[0] == []
    assert [r.line for r in _scan(tmp_path, "\u00c4pfel", files)[0]] == [5]
    assert [r.line for r in _scan(tmp_path, "A\u0308pfel", files)[0]] == [5]  # NFKC composes


def test_a_longer_identifier_never_matches_its_200_character_prefix(tmp_path: Path) -> None:
    assert MAX_NAME_CHARS == 200
    query = "a" * 200
    files: dict[str, str | bytes] = {
        "long.py": f"def {'a' * 250}():\n    pass\nclass {'a' * 201}:\n    pass\n",
        "exact.py": f"def {query}():\n    pass\n",
    }
    # Step 37 itself caps the longer names to the same 200 characters.
    from app.services.code_outline import outline_source

    capped = outline_source(files["long.py"])[1]  # type: ignore[arg-type]
    assert [s.name for s in capped] == [query, query]
    results, _, _ = _scan(tmp_path, query, files)
    assert [(r.path, r.line) for r in results] == [("exact.py", 1)]
    assert results[0].name == query


def test_queries_that_normalize_beyond_200_characters(tmp_path: Path) -> None:
    query = "\ufb03" * 200  # each "ffi" ligature normalizes to three letters
    full = "ffi" * 200
    files: dict[str, str | bytes] = {
        "a.py": f"def {full}():\n    pass\ndef {full[:200]}():\n    pass\ndef {full}x():\n    pass\n",
    }
    results, _, _ = _scan(tmp_path, query, files)
    assert [(r.line, r.name) for r in results] == [(1, full[:200])]  # Step 37's capped name


def test_match_symbols_is_pure() -> None:
    assert match_symbols("def (:\n", "x") == ("syntax_error", [], False, False)
    status, found, _, confirmed = match_symbols("def x(): pass\n", "x")
    assert (status, [s.line for s in found], confirmed) == ("parsed", [1], False)


# --- Per-file outcomes and limits ------------------------------------------------------------


def test_per_file_outcomes(tmp_path: Path, read_spy: list[str], parse_spy: list[int]) -> None:
    files: dict[str, str | bytes] = {
        "a/binary.py": b"\x00\x01 def target(): pass",
        "a/latin1.py": b"\xff\xfe def target(): pass",
        "a/huge.py": b"def target(): pass\n" + b"#" * MAX_CONTENT_BYTES,
        "a/big.py": b"def target(): pass\n" + b"#" * MAX_OUTLINE_BYTES,
        "a/bad.py": b"def target(:\n",
        "a/empty.py": b"",
        "a/other.py": b"def unrelated(): pass\n",
        "a/ok.py": b"def target(): pass\n",
    }
    results, summary, truncated = _scan(tmp_path, "target", files)
    assert [r.path for r in results] == ["a/ok.py"]
    assert summary.model_dump() == {
        "files_scanned": 7,
        "too_large": 2,
        "binary": 2,
        "syntax_error": 1,
    }
    assert "a/huge.py" not in read_spy  # over 1 MiB: never opened
    assert sorted(parse_spy) == [len(b"def target(:\n"), len(b"def target(): pass\n")]
    assert truncated is False


def test_prefilter_skips_only_files_that_cannot_match(tmp_path: Path, parse_spy: list[int]) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "def other(): pass\n",  # ASCII without the name: skipped
        "b.py": "x = 'target'\n",  # ASCII with the name: parsed, no match
        "c.py": "# na\u00efve\ndef other(): pass\n",  # non-ASCII: always parsed
    }
    assert _scan(tmp_path, "target", files)[0] == []
    assert len(parse_spy) == 2


@pytest.mark.parametrize(
    "source",
    [
        "(" * 201 + ")" * 201 + "\ndef target(): pass\n",
        "def target(): pass\nx = " + "+".join(["1"] * 100_000),
        "".join("    " * i + f"def target{i}():\n" for i in range(101)) + "    " * 101 + "pass\n",
    ],
    ids=["nested-parens", "long-chain", "deep-indent"],
)
def test_hostile_sources_are_syntax_errors(tmp_path: Path, source: str) -> None:
    started = time.perf_counter()
    results, summary, _ = _scan(tmp_path, "target", {"evil.py": source})
    assert (results, summary.syntax_error) == ([], 1)
    assert time.perf_counter() - started < 5.0


def test_text_size_boundary(tmp_path: Path, parse_spy: list[int]) -> None:
    line = b"def target(): pass\n"
    exactly = line + b"#" * (MAX_OUTLINE_BYTES - len(line))
    files: dict[str, str | bytes] = {"exact.py": exactly, "over.py": exactly + b"#"}
    results, summary, _ = _scan(tmp_path, "target", files)
    assert [r.path for r in results] == ["exact.py"]
    assert summary.too_large == 1
    assert parse_spy == [MAX_OUTLINE_BYTES]


def test_read_budget_is_checked_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_spy: list[str]
) -> None:
    assert symbol_search.MAX_SCANNED_BYTES == 64 * 1024 * 1024
    files: dict[str, str | bytes] = {
        "a.py": "def target(): pass\n".ljust(40),
        "b.py": "def target(): pass\n".ljust(40),
    }
    monkeypatch.setattr(symbol_search, "MAX_SCANNED_BYTES", 79)
    results, summary, truncated = _scan(tmp_path, "target", files)
    assert ([r.path for r in results], summary.files_scanned, truncated) == (["a.py"], 1, True)
    assert read_spy == ["a.py"]  # b.py was never opened
    monkeypatch.setattr(symbol_search, "MAX_SCANNED_BYTES", 80)  # exactly enough
    results, _, truncated = _scan(tmp_path, "target", files)
    assert ([r.path for r in results], truncated) == (["a.py", "b.py"], False)


def test_parse_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert symbol_search.MAX_PARSED_BYTES == 4 * 1024 * 1024
    source = "def target(): pass\n"
    files: dict[str, str | bytes] = {"a.py": source, "b.py": source}
    monkeypatch.setattr(symbol_search, "MAX_PARSED_BYTES", len(source) * 2 - 1)
    results, _, truncated = _scan(tmp_path, "target", files)
    assert ([r.path for r in results], truncated) == (["a.py"], True)
    monkeypatch.setattr(symbol_search, "MAX_PARSED_BYTES", len(source) * 2)
    results, _, truncated = _scan(tmp_path, "target", files)
    assert ([r.path for r in results], truncated) == (["a.py", "b.py"], False)


def test_per_file_symbol_limit(tmp_path: Path) -> None:
    assert MAX_SYMBOLS == 2000
    filler = "".join(f"def f{i}(): pass\n" for i in range(MAX_SYMBOLS - 1))
    exact = filler + "def target(): pass\n"  # the 2000th symbol
    over = exact + "def target(): pass\n"  # the 2001st is dropped by Step 37
    results, _, truncated = _scan(tmp_path, "target", {"a.py": exact})
    assert (len(results), truncated) == (1, False)
    results, _, truncated = _scan(tmp_path, "target", {"a.py": over})
    assert (len(results), truncated) == (1, True)


def test_result_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_RESULTS == 200
    files: dict[str, str | bytes] = {f"m{i}.py": "def target(): pass\n" * 2 for i in range(3)}
    monkeypatch.setattr(symbol_search, "MAX_RESULTS", 5)
    results, _, truncated = _scan(tmp_path, "target", files)
    assert ([(r.path, r.line) for r in results], truncated) == (
        [("m0.py", 1), ("m0.py", 2), ("m1.py", 1), ("m1.py", 2), ("m2.py", 1)],
        True,
    )
    monkeypatch.setattr(symbol_search, "MAX_RESULTS", 6)  # exactly enough
    results, _, truncated = _scan(tmp_path, "target", files)
    assert (len(results), truncated) == (6, False)


def test_path_then_source_order(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "z.py": "def t(): pass\n",
        "a/b.py": "class t:\n    def t(self): pass\n\ndef t(): pass\n",
        "B.py": "def t(): pass\n",
        "a.py": "def t(): pass\n",
    }
    results, _, _ = _scan(tmp_path, "t", files)
    assert [(r.path, r.line) for r in results] == [
        ("B.py", 1),
        ("a.py", 1),
        ("a/b.py", 1),
        ("a/b.py", 2),
        ("a/b.py", 4),
        ("z.py", 1),
    ]


def test_snapshot_mismatch_fails_the_whole_scan(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rows = _write(root, {"a.py": "def target(): pass\n", "b.py": "def target(): pass\n"})
    (root / "b.py").write_bytes(b"def target(): pass  # changed\n")
    with pytest.raises(FileContentError) as raised:
        scan_symbols(root, rows, "target")
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "b.py").unlink()  # missing
    with pytest.raises(FileContentError):
        scan_symbols(root, rows, "target")


def test_files_outside_the_snapshot_are_never_read(tmp_path: Path, read_spy: list[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rows = _write(root, {"a.py": "def target(): pass\n"})
    (root / "stray.py").write_bytes(b"def target(): pass\n")  # on disk, not in the snapshot
    results, _, _ = scan_symbols(root, rows, "target")
    assert [r.path for r in results] == ["a.py"]
    assert read_spy == ["a.py"]


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "import os, subprocess, socket\n"
        "os.system('echo pwned')\nsubprocess.run(['echo'])\n"
        "socket.create_connection(('example.invalid', 80))\n"
        "__import__('os').system('echo')\neval('1')\nexec('x = 1')\n"
        "def deco(f):\n    open('side-effect', 'w')\n    return f\n"
        "@deco\ndef target():\n    pass\n"
    )
    root = tmp_path / "repo"
    root.mkdir()
    rows = _write(root, {"evil.py": evil})
    compiled_flags: list[int] = []
    real_compile = builtins.compile

    def watching_compile(*args: Any, **kwargs: Any) -> Any:
        compiled_flags.append(kwargs.get("flags", args[3] if len(args) > 3 else 0))
        return real_compile(*args, **kwargs)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("repository code must never run")

    monkeypatch.setattr(builtins, "compile", watching_compile)
    for target, name in (
        (builtins, "exec"),
        (builtins, "eval"),
        (os, "system"),
        (subprocess, "Popen"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
    ):
        monkeypatch.setattr(target, name, refuse)
    results, _, _ = scan_symbols(root, rows, "target")
    monkeypatch.undo()
    assert [(r.kind, r.line) for r in results] == [("function", 13)]
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


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


FILES: dict[str, bytes] = {
    "backend/app/services/auth.py": AUTH.encode(),
    "backend/app/main.py": b"from app.services.auth import authenticate\n\nauthenticate()\n",
    "backend/Tools.py": b"async def authenticate():\n    pass\n",
    "stubs/auth.pyi": b"def authenticate(user: str) -> bool: ...\n",
    "docs/notes.md": b"def authenticate(): pass\n",
    "web/app.js": b"function authenticate() {}\n",
}


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
        f"/repositories/{repository_id or owner.repository_id}/symbols/search"
    )


async def _post(client: httpx.AsyncClient, owner: Owner, query: Any, **kw: Any) -> httpx.Response:
    url = _url(owner, kw.pop("project_id", None), kw.pop("repository_id", None))
    return await client.post(url, json={"query": query}, headers=kw.pop("headers", owner.headers))


async def _search(client: httpx.AsyncClient, owner: Owner, query: str) -> dict[str, Any]:
    response = await _post(client, owner, query)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_search_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
    body = await _search(client, alice, "authenticate")
    assert set(body) == {"results", "summary", "truncated"}
    assert body["results"][0] == {
        "path": "backend/Tools.py",
        "kind": "function",
        "async": True,
        "name": "authenticate",
        "line": 1,
        "end_line": 2,
        "depth": 0,
    }
    assert [(r["path"], r["kind"], r["line"]) for r in body["results"]] == [
        ("backend/Tools.py", "function", 1),
        ("backend/app/services/auth.py", "method", 9),
        ("backend/app/services/auth.py", "function", 13),
        ("backend/app/services/auth.py", "function", 18),
        ("backend/app/services/auth.py", "class", 28),
        ("stubs/auth.pyi", "function", 1),
    ]
    for result in body["results"]:
        assert set(result) == {"path", "kind", "async", "name", "line", "end_line", "depth"}
    assert body["summary"] == {"files_scanned": 4, "too_large": 0, "binary": 0, "syntax_error": 0}
    assert body["truncated"] is False


@requires_db
async def test_no_matches(client: httpx.AsyncClient, alice: Owner) -> None:
    assert (await _search(client, alice, "missing_symbol"))["results"] == []
    assert (await _search(client, alice, "authenticate_user"))["results"] == [
        {
            "path": "backend/app/services/auth.py",
            "kind": "function",
            "async": False,
            "name": "authenticate_user",
            "line": 24,
            "end_line": 25,
            "depth": 0,
        }
    ]


@requires_db
@pytest.mark.parametrize(
    "query",
    ["f", "_", "\u00e9t\u00e9", "a" * 200, "\uff41\uff55\uff54\uff48"],
    ids=["one-char", "underscore", "unicode", "200-chars", "fullwidth"],
)
async def test_valid_queries(client: httpx.AsyncClient, alice: Owner, query: str) -> None:
    assert (await _post(client, alice, query)).status_code == 200


@requires_db
@pytest.mark.parametrize(
    "body",
    [
        {"query": "a" * 201},
        {"query": ""},
        {"query": "   "},
        {"query": " auth"},
        {"query": "auth "},
        {"query": "au th"},
        {"query": "au\nth"},
        {"query": "au\rth"},
        {"query": "au\x00th"},
        {"query": "auth!"},
        {"query": "app.auth"},
        {"query": "a+b"},
        {"query": "1auth"},
        {},
        {"query": "auth", "extra": True},
        {"query": 42},
        {"query": None},
        ["auth"],
    ],
    ids=[
        "201-chars",
        "empty",
        "whitespace",
        "leading-space",
        "trailing-space",
        "inner-space",
        "newline",
        "carriage-return",
        "nul",
        "punctuation",
        "dotted",
        "operator",
        "digit-first",
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
async def test_malformed_requests(client: httpx.AsyncClient, alice: Owner) -> None:
    raw = await client.post(
        _url(alice),
        content=b"not json",
        headers={**alice.headers, "Content-Type": "application/json"},
    )
    assert raw.status_code == 422
    assert (await client.post(_url(alice), headers=alice.headers)).status_code == 422
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _post(client, alice, "auth", **kwargs)).status_code == 422
    assert (
        await client.post(f"{_url(alice)}?query=auth", headers=alice.headers)
    ).status_code == 422
    assert (await client.get(_url(alice), headers=alice.headers)).status_code == 405


@requires_db
async def test_index_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python rows by path: backend/Tools.py, backend/app/main.py, backend/app/services/auth.py,
    # stubs/auth.pyi
    monkeypatch.setattr(symbol_search, "MAX_INDEXED_PATHS", 3)
    body = await _search(client, alice, "authenticate")
    assert {r["path"] for r in body["results"]} == {
        "backend/Tools.py",
        "backend/app/services/auth.py",
    }
    assert body["truncated"] is True
    monkeypatch.setattr(symbol_search, "MAX_INDEXED_PATHS", 4)  # exactly enough
    body = await _search(client, alice, "authenticate")
    assert ("stubs/auth.pyi" in {r["path"] for r in body["results"]}, body["truncated"]) == (
        True,
        False,
    )


@requires_db
async def test_changed_storage_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    root = _storage(alice)
    (root / "backend" / "app" / "main.py").write_bytes(b"def authenticate(): pass  # tampered\n")
    response = await _post(client, alice, "authenticate")
    assert (response.status_code, response.json()) == (409, CHANGED)
    assert "tampered" not in response.text

    await _ingest(client, alice, FILES)
    (_storage(alice) / "stubs" / "auth.pyi").unlink()  # missing
    response = await _post(client, alice, "authenticate")
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    root = _storage(alice)
    outside = tmp_path / "outside"
    shutil.copytree(root / "stubs", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "stubs")
    _junction(root / "stubs", outside)
    response = await _post(client, alice, "authenticate")
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_symlinked_file_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "stubs" / "auth.pyi"
    outside = tmp_path / "outside.pyi"
    outside.write_bytes(FILES["stubs/auth.pyi"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    response = await _post(client, alice, "authenticate")
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_storage_swap_during_the_request_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the snapshot query, storage changes before the later files are read."""
    real = symbol_search.match_symbols
    root = _storage(alice)

    def match_then_swap(source: str, query: str) -> Any:
        (root / "stubs" / "auth.pyi").write_bytes(b"def authenticate(): ...  # new snapshot\n")
        return real(source, query)

    monkeypatch.setattr(symbol_search, "match_symbols", match_then_swap)
    response = await _post(client, alice, "authenticate")
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_one_statement_provides_the_snapshot(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner
) -> None:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    engine = db_session.bind.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", record)
    try:
        body = await _search(client, alice, "authenticate")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(body["results"]) == 6
    assert len([s for s in statements if "FROM files" in s]) == 1


@requires_db
async def test_reingestion_changes_the_results(client: httpx.AsyncClient, alice: Owner) -> None:
    files = dict(FILES)
    del files["backend/Tools.py"]
    files["new/module.py"] = b"class authenticate:\n    pass\n"
    await _ingest(client, alice, files)
    paths = [r["path"] for r in (await _search(client, alice, "authenticate"))["results"]]
    assert "backend/Tools.py" not in paths and "new/module.py" in paths


@requires_db
async def test_no_snippets_internals_or_logged_content(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-3c9a"
    await _ingest(
        client,
        alice,
        {**FILES, "m.py": f"def authenticate():  # {marker}\n    return '{marker}'\n".encode()},
    )
    caplog.set_level(logging.DEBUG)
    response = await _post(client, alice, "authenticate")
    assert response.status_code == 200
    assert "m.py" in [r["path"] for r in response.json()["results"]]
    assert marker not in response.text and marker not in caplog.text
    assert "return" not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"bob.py": b"def authenticate(): pass\n"})
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    for kwargs in (
        {"project_id": bob.project_id, "repository_id": bob.repository_id},  # alice's token
        {"project_id": other_project},
        {"repository_id": str(uuid.uuid4())},
    ):
        response = await _post(client, alice, "authenticate", **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert (await client.post(_url(alice), json={"query": "authenticate"})).status_code == 401
    bad = await _post(client, alice, "authenticate", headers={"Authorization": "Bearer nope"})
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
    bob = await _owner(client, "bob@example.com", {"bob.py": b"def authenticate(): pass\n"})
    limiter = _AlwaysLimited()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    for kwargs in (
        {"project_id": bob.project_id, "repository_id": bob.repository_id},
        {"repository_id": str(uuid.uuid4())},
    ):
        response = await _post(client, alice, "authenticate", **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert limiter.hits == []
    limited = await _post(client, alice, "authenticate")
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts; please try again later"}
    assert limiter.hits == [(SYMBOL_SEARCH_LIMIT, alice.user_id)]
    assert read_spy == []


@requires_db_and_redis
async def test_rate_limit_is_30_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert SYMBOL_SEARCH_LIMIT == Limit("symbol-search-user", 30, 600)
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
            assert (await _post(client, alice, "nothing_here")).status_code == 200
        limited = await _post(client, alice, "nothing_here")
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts; please try again later"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 600
        keys = [k.decode() if isinstance(k, bytes) else k for k in await pool.keys("*")]
        assert any(k.startswith("ratelimit:symbol-search-user:") for k in keys)
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
        assert (await _post(client, alice, "nothing_here")).status_code == 200
