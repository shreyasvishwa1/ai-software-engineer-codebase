"""Python unreachable code tests (GET .../files/unreachable-code?path=). Offline.

Analysis tests use a temporary storage directory and need no database. Expected regions are
written out by hand from the Step 69 rules. Two independent oracles cross-check them on curated and
seeded random programs the tests author themselves: CPython's own compiler (test-only), which
drops the bytecode of every statement it proves unreachable, so no reported line may carry any;
and a plain recursive reading of the rules that shares no code with the service's iterative walk.
API tests run against the already-migrated throwaway test database (TEST_DATABASE_URL, "_test")
inside rolled-back transactions; the rate-limit test uses a disposable Redis (TEST_REDIS_URL).
Junction tests are Windows-only; symlink tests are skipped where symlinks cannot be created.
"""

import ast
import builtins
import hashlib
import logging
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import types
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
from app.services import code_unreachable_code, ingestion
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.code_unreachable_code import (
    MAX_UNREACHABLE_REGIONS,
    UNREACHABLE_CODE_LIMIT,
    analyze_unreachable_code,
)
from app.services.file_content import MAX_CONTENT_BYTES, FileContentError
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
CHANGED = {"detail": "Repository changed during analysis; run it again"}
ZERO = {"regions": 0, "statements": 0}
REGION_FIELDS = {"line", "end_line", "statements", "cause", "cause_line"}

Region = tuple[int, int, int, str, int]  # (line, end_line, statements, cause, cause_line)


# --- Helpers ---------------------------------------------------------------------------------


def _write(root: Path, files: dict[str, str | bytes]) -> list[Any]:
    """Write ``files`` byte-exactly; snapshot-like rows of the Python ones, by path."""
    rows = []
    for path, content in files.items():
        data = content.encode() if isinstance(content, str) else content
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path.endswith((".py", ".pyi")):
            rows.append(
                SimpleNamespace(
                    path=path,
                    language="Python",
                    extension=path.rsplit(".", 1)[1],
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
    return sorted(rows, key=lambda row: row.path)


def _repo(tmp_path: Path, files: dict[str, str | bytes]) -> tuple[Path, list[Any]]:
    root = tmp_path / "repo"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    return root, _write(root, files)


def _row(rows: list[Any], path: str) -> Any:
    return next(r for r in rows if r.path == path)


def unreachable(tmp_path: Path, source: str | bytes, path: str = "m.py") -> Any:
    root, rows = _repo(tmp_path, {path: source})
    return analyze_unreachable_code(root, _row(rows, path))


def regions(result: Any) -> list[Region]:
    return [(r.line, r.end_line, r.statements, r.cause, r.cause_line) for r in result.regions]


def found(tmp_path: Path, source: str) -> list[Region]:
    result = unreachable(tmp_path, source)
    assert result.status == "parsed"
    return regions(result)


def summary(result: Any) -> tuple[int, int]:
    return result.summary.regions, result.summary.statements


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_unreachable_code._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_unreachable_code, "_read_verified", spy)
    return reads


# --- A. Leaving statements (U2) --------------------------------------------------------------


def test_statements_after_a_return_never_run(tmp_path: Path) -> None:
    source = (
        "def f():\n"  # 1
        "    return 1\n"  # 2
        "    a = 2\n"  # 3
        "    b = 3\n"  # 4
    )
    assert found(tmp_path, source) == [(3, 4, 2, "return", 2)]


@pytest.mark.parametrize(
    ("source", "cause"),
    [
        ("def f():\n    return\n    x()\n", "return"),
        ("def f():\n    raise ValueError('no')\n    x()\n", "raise"),
        ("for i in y:\n    break\n    x()\n", "break"),
        ("while c:\n    continue\n    x()\n", "continue"),
    ],
    ids=["return", "raise", "break", "continue"],
)
def test_every_leaving_statement(tmp_path: Path, source: str, cause: str) -> None:
    assert found(tmp_path, source) == [(3, 3, 1, cause, 2)]


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    x()\n    return 1\n",
        "def f():\n    if c:\n        return 1\n    x()\n",
        "for i in y:\n    if i:\n        break\n    x()\n",
        "x = 1\n",
        "",
    ],
    ids=["last-statement", "conditional-return", "conditional-break", "plain", "empty"],
)
def test_nothing_after_a_leaving_statement_is_no_region(tmp_path: Path, source: str) -> None:
    assert found(tmp_path, source) == []


def test_statements_on_the_same_line(tmp_path: Path) -> None:
    assert found(tmp_path, "def f():\n    return 1; x(); y()\n") == [(2, 2, 2, "return", 2)]


def test_module_and_class_bodies(tmp_path: Path) -> None:
    assert found(tmp_path, "import sys\nraise SystemExit(0)\nmain()\n") == [(3, 3, 1, "raise", 2)]
    assert found(tmp_path, "class A:\n    raise TypeError\n    x = 1\n") == [(3, 3, 1, "raise", 2)]


def test_only_the_first_leaving_statement_starts_the_region(tmp_path: Path) -> None:
    source = (
        "def f():\n"  # 1
        "    return 1\n"  # 2
        "    return 2\n"  # 3
        "    x()\n"  # 4
    )
    assert found(tmp_path, source) == [(3, 4, 2, "return", 2)]


# --- B. If statements (U2) -------------------------------------------------------------------


def test_an_if_whose_every_branch_leaves(tmp_path: Path) -> None:
    source = (
        "def f(x):\n"  # 1
        "    if x:\n"  # 2
        "        return 1\n"  # 3
        "    else:\n"  # 4
        "        raise ValueError\n"  # 5
        "    after()\n"  # 6
    )
    assert found(tmp_path, source) == [(6, 6, 1, "if", 2)]


def test_an_elif_chain_whose_every_branch_leaves(tmp_path: Path) -> None:
    source = (
        "def f(x):\n"  # 1
        "    if x == 1:\n"  # 2
        "        return 1\n"  # 3
        "    elif x == 2:\n"  # 4
        "        return 2\n"  # 5
        "    elif x == 3:\n"  # 6
        "        raise KeyError\n"  # 7
        "    else:\n"  # 8
        "        return 4\n"  # 9
        "    after()\n"  # 10
    )
    assert found(tmp_path, source) == [(10, 10, 1, "if", 2)]


@pytest.mark.parametrize(
    "source",
    [
        "def f(x):\n    if x:\n        return 1\n    after()\n",
        "def f(x):\n    if x:\n        return 1\n    elif y:\n        return 2\n    after()\n",
        "def f(x):\n    if x:\n        return 1\n    else:\n        x()\n    after()\n",
        "def f(x):\n    if x:\n        x()\n    else:\n        return 1\n    after()\n",
        "def f(x):\n    if x:\n        return 1\n    elif y:\n        y()\n    else:\n        return 2\n    after()\n",
    ],
    ids=[
        "no-else",
        "elif-without-else",
        "else-falls-through",
        "body-falls-through",
        "elif-falls-through",
    ],
)
def test_an_if_with_a_branch_that_may_complete_never_leaves(tmp_path: Path, source: str) -> None:
    assert found(tmp_path, source) == []


def test_nested_ifs_decide_their_branches(tmp_path: Path) -> None:
    source = (
        "def f(x, y):\n"  # 1
        "    if x:\n"  # 2
        "        if y:\n"  # 3
        "            return 1\n"  # 4
        "        else:\n"  # 5
        "            return 2\n"  # 6
        "    else:\n"  # 7
        "        if y:\n"  # 8: an else holding an if, not an elif
        "            raise KeyError\n"  # 9
        "        else:\n"  # 10
        "            return 3\n"  # 11
        "    after()\n"  # 12
    )
    assert found(tmp_path, source) == [(12, 12, 1, "if", 2)]


def test_a_leaving_statement_deeper_than_its_branch_does_not_count(tmp_path: Path) -> None:
    source = (
        "def f(x, m):\n"  # 1
        "    if x:\n"  # 2
        "        with m:\n"  # 3: a with statement never leaves (out of scope)
        "            return 1\n"  # 4
        "    else:\n"  # 5
        "        return 2\n"  # 6
        "    after()\n"  # 7
    )
    assert found(tmp_path, source) == []


def test_a_branch_region_and_the_region_after_the_if(tmp_path: Path) -> None:
    source = (
        "def f(x):\n"  # 1
        "    if x:\n"  # 2
        "        return 1\n"  # 3
        "        dead()\n"  # 4
        "    else:\n"  # 5
        "        raise ValueError\n"  # 6
        "    after()\n"  # 7
    )
    assert found(tmp_path, source) == [(4, 4, 1, "return", 3), (7, 7, 1, "if", 2)]


def test_branches_leaving_through_loop_jumps(tmp_path: Path) -> None:
    source = (
        "for x in xs:\n"  # 1
        "    if x:\n"  # 2
        "        break\n"  # 3
        "    else:\n"  # 4
        "        continue\n"  # 5
        "    never()\n"  # 6
        "done()\n"  # 7
    )
    assert found(tmp_path, source) == [(6, 6, 1, "if", 2)]


# --- C. Endless while loops (U2) -------------------------------------------------------------


def test_statements_after_an_endless_loop_never_run(tmp_path: Path) -> None:
    source = (
        "def serve():\n"  # 1
        "    while True:\n"  # 2
        "        handle()\n"  # 3
        "    cleanup()\n"  # 4
        "    log()\n"  # 5
    )
    assert found(tmp_path, source) == [(4, 5, 2, "while", 2)]


@pytest.mark.parametrize("condition", ["True", "1", "'x'", "...", "2.5", "b'x'"])
def test_true_constants_make_a_loop_endless(tmp_path: Path, condition: str) -> None:
    source = f"def f():\n    while {condition}:\n        g()\n    after()\n"
    assert found(tmp_path, source) == [(4, 4, 1, "while", 2)]


@pytest.mark.parametrize(
    "condition", ["False", "0", "None", "''", "x", "-1", "not False", "(1, 2)"]
)
def test_other_conditions_never_make_a_loop_endless(tmp_path: Path, condition: str) -> None:
    source = f"def f():\n    while {condition}:\n        g()\n    after()\n"
    assert found(tmp_path, source) == []


@pytest.mark.parametrize(
    "body",
    [
        "        break\n",
        "        if g():\n            break\n",
        "        if g():\n            pass\n        else:\n            break\n",
        "        with m:\n            break\n",
        "        try:\n            g()\n        except E:\n            break\n",
        "        try:\n            g()\n        finally:\n            break\n",
        "        match g():\n            case 1:\n                break\n",
        "        for x in y:\n            pass\n        else:\n            break\n",
        "        while c:\n            pass\n        else:\n            break\n",
    ],
    ids=["direct", "if", "else", "with", "except", "finally", "match", "for-else", "while-else"],
)
def test_a_break_for_the_loop_lets_it_end(tmp_path: Path, body: str) -> None:
    source = "def f():\n    while True:\n" + body + "    after()\n"
    assert found(tmp_path, source) == []


@pytest.mark.parametrize(
    "body",
    [
        "        for x in y:\n            break\n",
        "        while c:\n            break\n",
        "        def g():\n            for x in y:\n                break\n",
        "        class K:\n            for x in y:\n                break\n",
        "        continue\n",
        "        if c:\n            return 1\n",
        "        raise_later = 1\n",
    ],
    ids=["nested-for", "nested-while", "nested-def", "nested-class", "continue", "return", "plain"],
)
def test_breaks_of_other_loops_never_count(tmp_path: Path, body: str) -> None:
    source = "def f():\n    while True:\n" + body + "    after()\n"
    end = source.count("\n")
    assert found(tmp_path, source) == [(end, end, 1, "while", 2)]


@pytest.mark.parametrize("scope", ["def g():", "async def g():", "class K:"])
def test_a_stray_break_in_a_nested_scope_never_counts(tmp_path: Path, scope: str) -> None:
    # Python's compiler rejects this break ("'break' outside loop"), but the syntax tree, which is
    # all the service reads, has it inside the nested scope: it never ends the outer loop.
    source = (
        "def f():\n"  # 1
        "    while True:\n"  # 2
        f"        {scope}\n"  # 3
        "            break\n"  # 4
        "    after()\n"  # 5
    )
    assert found(tmp_path, source) == [(5, 5, 1, "while", 2)]


def test_async_for_breaks_are_their_own(tmp_path: Path) -> None:
    source = (
        "async def f():\n"  # 1
        "    while True:\n"  # 2
        "        async for x in y:\n"  # 3
        "            break\n"  # 4
        "    after()\n"  # 5
    )
    assert found(tmp_path, source) == [(5, 5, 1, "while", 2)]


def test_the_else_of_an_endless_loop_is_never_analyzed(tmp_path: Path) -> None:
    source = (
        "def f():\n"  # 1
        "    while True:\n"  # 2
        "        g()\n"  # 3
        "    else:\n"  # 4
        "        return 1\n"  # 5
        "        dead()\n"  # 6: inside code that never runs: not reported on its own
        "    after()\n"  # 7
    )
    assert found(tmp_path, source) == [(7, 7, 1, "while", 2)]


def test_an_inner_break_ends_only_the_inner_loop(tmp_path: Path) -> None:
    source = (
        "def f():\n"  # 1
        "    while True:\n"  # 2
        "        while True:\n"  # 3
        "            break\n"  # 4
        "        g()\n"  # 5
        "    after()\n"  # 6
    )
    assert found(tmp_path, source) == [(6, 6, 1, "while", 2)]


# --- D. Regions (U1, U3) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("for x in y:\n    break\n    a()\n", (3, 3, 1, "break", 2)),
        ("for x in y:\n    pass\nelse:\n    raise E\n    a()\n", (5, 5, 1, "raise", 4)),
        ("while c:\n    break\n    a()\n", (3, 3, 1, "break", 2)),
        ("while c:\n    pass\nelse:\n    raise E\n    a()\n", (5, 5, 1, "raise", 4)),
        ("if c:\n    raise E\n    a()\n", (3, 3, 1, "raise", 2)),
        ("if c:\n    pass\nelse:\n    raise E\n    a()\n", (5, 5, 1, "raise", 4)),
        ("with m:\n    raise E\n    a()\n", (3, 3, 1, "raise", 2)),
        (
            "async def f():\n    async with m:\n        return 1\n        a()\n",
            (4, 4, 1, "return", 3),
        ),
        (
            "async def f():\n    async for x in y:\n        break\n        a()\n",
            (4, 4, 1, "break", 3),
        ),
        ("try:\n    raise E\n    a()\nexcept E:\n    pass\n", (3, 3, 1, "raise", 2)),
        ("try:\n    pass\nexcept E:\n    raise\n    a()\n", (5, 5, 1, "raise", 4)),
        (
            "try:\n    pass\nexcept E:\n    pass\nelse:\n    raise E\n    a()\n",
            (7, 7, 1, "raise", 6),
        ),
        ("try:\n    pass\nfinally:\n    raise E\n    a()\n", (5, 5, 1, "raise", 4)),
        ("try:\n    pass\nexcept* E:\n    raise\n    a()\n", (5, 5, 1, "raise", 4)),
        ("match c:\n    case 1:\n        raise E\n        a()\n", (4, 4, 1, "raise", 3)),
        ("def f():\n    return\n    a()\n", (3, 3, 1, "return", 2)),
        ("async def f():\n    return\n    a()\n", (3, 3, 1, "return", 2)),
        ("class A:\n    raise E\n    a = 1\n", (3, 3, 1, "raise", 2)),
        ("raise E\na()\n", (2, 2, 1, "raise", 1)),
    ],
    ids=[
        "for",
        "for-else",
        "while",
        "while-else",
        "if",
        "else",
        "with",
        "async-with",
        "async-for",
        "try",
        "except",
        "try-else",
        "finally",
        "except-star",
        "match-case",
        "def",
        "async-def",
        "class",
        "module",
    ],
)
def test_every_kind_of_statement_list(tmp_path: Path, source: str, expected: Region) -> None:
    assert found(tmp_path, source) == [expected]


def test_a_region_spans_its_statements_to_the_last_line(tmp_path: Path) -> None:
    source = (
        "def f():\n"  # 1
        "    return 1\n"  # 2
        "    x = [\n"  # 3
        "        1,\n"  # 4
        "    ]\n"  # 5
        "    def g():\n"  # 6
        "        pass\n"  # 7
        "\n"  # 8: blank and comment lines after the last statement are not part of it
        "# trailing\n"  # 9
    )
    assert found(tmp_path, source) == [(3, 7, 2, "return", 2)]


@pytest.mark.parametrize("keyword", ["def g():\n        pass\n", "class G:\n        pass\n"])
def test_a_decorated_first_statement_starts_at_its_first_decorator(
    tmp_path: Path, keyword: str
) -> None:
    source = (
        "def f():\n"  # 1
        "    return 1\n"  # 2
        "    @deco\n"  # 3
        "    @deco2(x)\n"  # 4
        f"    {keyword}"  # 5, 6
    )
    assert found(tmp_path, source) == [(3, 6, 1, "return", 2)]


def test_nothing_inside_a_region_is_analyzed(tmp_path: Path) -> None:
    source = (
        "def f():\n"  # 1
        "    return 1\n"  # 2
        "    if x:\n"  # 3
        "        return 2\n"  # 4
        "        dead()\n"  # 5
        "    def g():\n"  # 6
        "        return 3\n"  # 7
        "        dead()\n"  # 8
    )
    assert found(tmp_path, source) == [(3, 8, 2, "return", 2)]


def test_reachable_definitions_are_analyzed(tmp_path: Path) -> None:
    source = (
        "class A:\n"  # 1
        "    def m(self):\n"  # 2
        "        return 1\n"  # 3
        "        self.dead()\n"  # 4
        "    def n(self):\n"  # 5
        "        def inner():\n"  # 6
        "            raise ValueError\n"  # 7
        "            dead()\n"  # 8
        "        return inner\n"  # 9
        "    value = [x for x in range(3)]\n"  # 10: expressions hold no statements
    )
    assert found(tmp_path, source) == [(4, 4, 1, "return", 3), (8, 8, 1, "raise", 7)]


def test_a_long_elif_chain_is_walked_without_recursion(tmp_path: Path) -> None:
    links = 1000
    source = (
        "def f(x):\n    if x == 0:\n        return 0\n"
        + "".join(f"    elif x == {i}:\n        return {i}\n" for i in range(1, links))
        + "    else:\n        return -1\n    dead()\n"
    )
    assert sys.getrecursionlimit() < 2 * links  # deep enough to fail a naive recursive walk
    end = source.count("\n")
    assert found(tmp_path, source) == [(end, end, 1, "if", 2)]


def test_the_deepest_indentation_in_a_worker_thread(tmp_path: Path) -> None:
    depth = 99  # Python allows at most 100 indentation levels
    lines = [("    " * i) + f"if c{i}:" for i in range(depth)]
    lines += [("    " * depth) + "raise E", ("    " * depth) + "x = 1"]
    for i in reversed(range(depth)):
        lines += [("    " * i) + "else:", ("    " * i) + "    raise E"]
    lines.append("tail = 1")
    source = "\n".join(lines) + "\n"
    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            outcome["regions"] = found(tmp_path, source)
        except BaseException as exc:  # reported below, never swallowed
            outcome["error"] = exc

    worker = threading.Thread(target=work)  # the service runs in a threadpool thread
    worker.start()
    worker.join()
    assert "error" not in outcome, outcome.get("error")
    last = source.count("\n")
    assert outcome["regions"] == [
        (depth + 2, depth + 2, 1, "raise", depth + 1),
        (last, last, 1, "if", 1),
    ]


# --- E. Order, summary and limits (U3) -------------------------------------------------------


def test_regions_are_in_source_order(tmp_path: Path) -> None:
    source = (
        "def late():\n"  # 1
        "    if c:\n"  # 2
        "        return 1\n"  # 3
        "        a()\n"  # 4: inner region, found after the outer one's statement
        "    else:\n"  # 5
        "        return 2\n"  # 6
        "    b()\n"  # 7
        "def early():\n"  # 8
        "    raise E\n"  # 9
        "    c()\n"  # 10
    )
    result = unreachable(tmp_path, source)
    assert regions(result) == [(4, 4, 1, "return", 3), (7, 7, 1, "if", 2), (10, 10, 1, "raise", 9)]
    assert summary(result) == (3, 3)


def test_the_summary_counts_every_statement(tmp_path: Path) -> None:
    source = "def f():\n    return\n    a()\n    b()\n    c()\ndef g():\n    raise E\n    d()\n"
    result = unreachable(tmp_path, source)
    assert regions(result) == [(3, 5, 3, "return", 2), (8, 8, 1, "raise", 7)]
    assert summary(result) == (2, 4)


def test_regions_are_capped_but_counted(tmp_path: Path) -> None:
    assert MAX_UNREACHABLE_REGIONS == 500
    source = "".join(f"def f{i}():\n    return\n    a()\n    b()\n" for i in range(501))
    result = unreachable(tmp_path, source)
    assert len(result.regions) == 500 and result.truncated is True
    assert summary(result) == (501, 1002)
    assert regions(result)[-1] == (4 * 499 + 3, 4 * 499 + 4, 2, "return", 4 * 499 + 2)
    exactly = "".join(f"def f{i}():\n    return\n    a()\n" for i in range(500))
    capped = unreachable(tmp_path, exactly)
    assert (len(capped.regions), capped.truncated, summary(capped)) == (500, False, (500, 500))


# --- F. Statuses, snapshot and safety (offline) ----------------------------------------------


def _not_parsed(result: Any, status: str) -> None:
    assert result.model_dump() == {
        "path": "m.py",
        "status": status,
        "regions": [],
        "summary": ZERO,
        "truncated": False,
    }


def test_empty_file(tmp_path: Path) -> None:
    result = unreachable(tmp_path, "")
    assert (result.status, result.regions, result.summary.model_dump()) == ("parsed", [], ZERO)


def test_syntax_error(tmp_path: Path) -> None:
    _not_parsed(unreachable(tmp_path, "def f(:\n"), "syntax_error")
    _not_parsed(unreachable(tmp_path, "def f():\n    return\n    x = '''\n"), "syntax_error")
    deep = "".join("    " * i + f"if c{i}:\n" for i in range(100)) + "    " * 100 + "pass\n"
    _not_parsed(unreachable(tmp_path, deep), "syntax_error")  # 101 indentation levels


def test_line_endings_are_python_newlines(tmp_path: Path) -> None:
    assert found(tmp_path, "def f():\r\n    return\r\n    a()\r\n") == [(3, 3, 1, "return", 2)]
    assert found(tmp_path, "def f():\r    return\r    a()\r") == [(3, 3, 1, "return", 2)]


def test_binary(tmp_path: Path) -> None:
    _not_parsed(unreachable(tmp_path, b"\x00\x01def f(): return\n"), "binary")
    _not_parsed(unreachable(tmp_path, b"\xff\xfe"), "binary")


def test_decoded_size_boundary(tmp_path: Path) -> None:
    body = b"def f():\n    return\n    a()\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    assert len(exactly) == MAX_OUTLINE_BYTES
    assert regions(unreachable(tmp_path, exactly)) == [(3, 3, 1, "return", 2)]
    _not_parsed(unreachable(tmp_path, exactly + b"#"), "too_large")  # never parsed


def test_stored_size_limit_is_checked_before_opening(tmp_path: Path, read_spy: list[str]) -> None:
    result = unreachable(tmp_path, os.urandom(MAX_CONTENT_BYTES))
    assert result.status in ("binary", "too_large") and read_spy == ["m.py"]  # exactly 1 MiB opens
    read_spy.clear()
    _not_parsed(unreachable(tmp_path, os.urandom(MAX_CONTENT_BYTES) + b"x"), "too_large")
    assert read_spy == []  # over 1 MiB is never opened


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "def f():\n    return\n    a()\n"})
    (root / "m.py").write_bytes(b"def f():\n    return\n    a()  # changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_unreachable_code(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "m.py").unlink()  # missing
    with pytest.raises(FileContentError) as raised:
        analyze_unreachable_code(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_symlinked_file_fails_the_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "def f():\n    return\n"})
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"def f():\n    return\n")
    (root / "m.py").unlink()
    try:
        os.symlink(outside, root / "m.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    with pytest.raises(FileContentError) as raised:
        analyze_unreachable_code(root, rows[0])
    assert raised.value.status_code == 409


def test_junctioned_package_fails_the_request(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    root, rows = _repo(tmp_path, {"pkg/m.py": "def f():\n    return\n"})
    outside = tmp_path / "outside"
    shutil.copytree(root / "pkg", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pkg")
    _winapi.CreateJunction(str(outside), str(root / "pkg"))  # type: ignore[attr-defined]
    with pytest.raises(FileContentError) as raised:
        analyze_unreachable_code(root, rows[0])
    assert raised.value.status_code == 409


def test_nothing_is_cached_across_requests(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "def f():\n    return\n    a()\n"})
    assert regions(analyze_unreachable_code(root, rows[0])) == [(3, 3, 1, "return", 2)]
    changed = b"def f():\n    if c:\n        return\n    a()\n"
    (root / "m.py").write_bytes(changed)
    row = SimpleNamespace(
        path="m.py", size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest()
    )
    assert analyze_unreachable_code(root, row).regions == []


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"  # 1
        "import os, subprocess, socket\n"  # 2
        "os.system('echo')\nsubprocess.run(['echo'])\n"  # 3, 4
        "socket.create_connection(('example.invalid', 80))\n"  # 5
        "def target():\n    raise SystemExit\n"  # 6, 7
        "    open('side-effect', 'w')\n"  # 8
    )
    root, rows = _repo(tmp_path, {"m.py": evil})
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
    result = analyze_unreachable_code(root, rows[0])
    monkeypatch.undo()
    assert regions(result) == [(8, 8, 1, "raise", 7)] and summary(result) == (1, 1)
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


def test_responses_carry_no_source_or_internals(tmp_path: Path) -> None:
    marker = "UNIQUE-CONTENT-MARKER-69a1"
    source = (
        "def secret_helper():\n"  # 1
        f"    return '{marker}'  # {marker}\n"  # 2
        f"    print('{marker}')\n"  # 3: after return
    )
    result = unreachable(tmp_path, source)
    assert regions(result) == [(3, 3, 1, "return", 2)]
    text = result.model_dump_json()
    assert marker not in text and "secret_helper" not in text and "print" not in text
    for word in ("def ", "pass", "'"):
        assert word not in text
    assert hashlib.sha256(source.encode()).hexdigest() not in text
    body = result.model_dump()
    assert set(body) == {"path", "status", "regions", "summary", "truncated"}
    assert set(body["summary"]) == set(ZERO)
    for item in body["regions"]:
        assert set(item) == REGION_FIELDS


def test_rate_limit_constant() -> None:
    assert UNREACHABLE_CODE_LIMIT == Limit("unreachable-code-user", 120, 600)


class _Rows:
    def __init__(self, row: Any) -> None:
        self.row = row

    def first(self) -> Any:
        return self.row


class _Db:
    """Stands in for the session: records every statement, answers with one row (or none)."""

    def __init__(self, row: Any) -> None:
        self.row = row
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(self.row)


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[Any]]:
    root, rows = _repo(tmp_path, {"m.py": ORACLE, "notes.md": "return\nx\n"})
    monkeypatch.setattr(code_unreachable_code, "repository_storage_dir", lambda settings, rid: root)
    return root, rows


async def _request(db: _Db, path: str) -> Any:
    session: Any = db  # the stand-in session
    repository: Any = SimpleNamespace(id=uuid.uuid4())
    return await code_unreachable_code.code_unreachable_code(session, repository, path)


async def test_request_reads_one_row_and_analyzes_the_stored_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    _, rows = storage
    db = _Db(rows[0])
    result = await _request(db, "m.py")
    assert regions(result) == ORACLE_REGIONS
    assert len(db.statements) == 1 and read_spy == ["m.py"]
    sql = str(db.statements[0])
    assert "FROM files" in sql and "files.sha256" in sql and "files.path" in sql


async def test_request_never_reads_a_non_python_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    row = SimpleNamespace(
        path="notes.md", language="Markdown", extension="md", size_bytes=9, sha256="x"
    )
    result = await _request(_Db(row), "notes.md")
    assert result.model_dump() == {
        "path": "notes.md",
        "status": "unsupported_language",
        "regions": [],
        "summary": ZERO,
        "truncated": False,
    }
    assert read_spy == []


async def test_request_for_an_unknown_file_is_404(read_spy: list[str]) -> None:
    with pytest.raises(FileContentError) as raised:
        await _request(_Db(None), "missing.py")
    assert (raised.value.status_code, raised.value.detail) == (404, FILE_NOT_FOUND["detail"])
    assert read_spy == []


# --- G. Oracles ------------------------------------------------------------------------------

ORACLE = (
    "import sys\n"  # 1
    "\n"  # 2
    "def parse(text):\n"  # 3
    "    if not text:\n"  # 4
    "        raise ValueError('empty')\n"  # 5
    "        log('never')\n"  # 6: after raise
    "    return text.split()\n"  # 7
    "    cleanup()\n"  # 8: after return
    "\n"  # 9
    "def pick(x):\n"  # 10
    "    if x > 0:\n"  # 11
    "        return 'positive'\n"  # 12
    "    elif x < 0:\n"  # 13
    "        return 'negative'\n"  # 14
    "    else:\n"  # 15
    "        return 'zero'\n"  # 16
    "    unreachable_tail()\n"  # 17: after an if whose every branch returns
    "\n"  # 18
    "def serve(queue):\n"  # 19
    "    while True:\n"  # 20
    "        item = queue.get()\n"  # 21
    "        if item is None:\n"  # 22
    "            continue\n"  # 23
    "        handle(item)\n"  # 24
    "    shutdown()\n"  # 25: after an endless loop without break
    "\n"  # 26
    "def drain(queue):\n"  # 27
    "    while True:\n"  # 28
    "        if queue.empty():\n"  # 29
    "            break\n"  # 30
    "    return 'drained'\n"  # 31: reachable through the break
    "\n"  # 32
    "for arg in sys.argv:\n"  # 33
    "    if arg == '--':\n"  # 34
    "        break\n"  # 35
    "        print('never')\n"  # 36: after break
    "    try:\n"  # 37
    "        open(arg)\n"  # 38
    "    except OSError:\n"  # 39
    "        continue\n"  # 40
    "        print('never')\n"  # 41: after continue
    "    finally:\n"  # 42
    "        pass\n"  # 43
)
ORACLE_REGIONS: list[Region] = [
    (6, 6, 1, "raise", 5),
    (8, 8, 1, "return", 7),
    (17, 17, 1, "if", 11),
    (25, 25, 1, "while", 20),
    (36, 36, 1, "break", 35),
    (41, 41, 1, "continue", 40),
]


def _bytecode_lines(source: str) -> set[int]:
    """Every line that still carries bytecode once CPython compiled ``source`` (test-authored text,
    never repository code): CPython drops the instructions of statements it proves unreachable."""
    lines: set[int] = set()
    pending = [compile(source, "<fixture>", "exec")]
    while pending:
        code = pending.pop()
        lines.update(line for _, _, line in code.co_lines() if line is not None)
        pending.extend(const for const in code.co_consts if isinstance(const, types.CodeType))
    return lines


def _reference(source: str) -> list[Region]:
    """The Step 69 rules read plainly and recursively (fine for small fixtures), sharing no code
    with the service's iterative walk."""
    tree = ast.parse(source)
    out: list[Region] = []

    def has_break(statements: list[ast.stmt]) -> bool:
        for s in statements:
            if isinstance(s, ast.Break):
                return True
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
                if has_break(s.orelse):
                    return True
                continue
            for block in blocks(s):
                if has_break(block):
                    return True
        return False

    def blocks(s: ast.stmt) -> list[list[ast.stmt]]:
        found: list[list[ast.stmt]] = []
        if isinstance(s, ast.Match):
            found.extend(case.body for case in s.cases)
        elif isinstance(s, (ast.Try, ast.TryStar)):
            found.append(s.body)
            found.extend(handler.body for handler in s.handlers)
            found.extend(b for b in (s.orelse, s.finalbody) if b)
        elif hasattr(s, "body") and isinstance(s.body, list):
            found.append(s.body)
            orelse = getattr(s, "orelse", [])
            if orelse:
                found.append(orelse)
        return found

    def kind(s: ast.stmt) -> str | None:
        simple = {
            ast.Return: "return",
            ast.Raise: "raise",
            ast.Break: "break",
            ast.Continue: "continue",
        }
        if type(s) in simple:
            return simple[type(s)]
        if isinstance(s, ast.If) and s.orelse:
            if any(kind(x) for x in s.body) and any(kind(x) for x in s.orelse):
                return "if"
        if isinstance(s, ast.While) and isinstance(s.test, ast.Constant) and s.test.value:
            if not has_break(s.body):
                return "while"
        return None

    def walk(statements: list[ast.stmt]) -> None:
        for i, s in enumerate(statements):
            k = kind(s)
            for block in blocks(s):
                if not (k == "while" and block is s.orelse):
                    walk(block)
            if k:
                rest = statements[i + 1 :]
                if rest:
                    first = rest[0]
                    decorators = getattr(first, "decorator_list", [])
                    start = decorators[0].lineno if decorators else first.lineno
                    out.append((start, rest[-1].end_lineno or 0, len(rest), k, s.lineno))
                return

    walk(tree.body)
    return sorted(out)


def _generate(seed: int) -> str:
    """A random, compilable program of nested statements (each on its own lines)."""
    rng = random.Random(seed)
    lines: list[str] = []

    def simple(indent: str) -> None:
        lines.append(indent + rng.choice(["x = 1", "f()", "y += 2", "pass", "g(x)"]))

    def block(indent: str, depth: int, in_loop: bool, in_def: bool) -> None:
        for _ in range(rng.randint(1, 4)):
            statement(indent, depth, in_loop, in_def)

    def statement(indent: str, depth: int, in_loop: bool, in_def: bool) -> None:
        choices = ["simple", "simple", "raise"]
        if in_def:
            choices.append("return")
        if in_loop:
            choices += ["break", "continue"]
        if depth < 4:
            choices += [
                "if",
                "ifelse",
                "elif",
                "while_true",
                "while_c",
                "for",
                "try",
                "with",
                "def",
            ]
        pick = rng.choice(choices)
        inner = indent + "    "
        if pick == "simple":
            simple(indent)
        elif pick in ("raise", "return", "break", "continue"):
            lines.append(indent + {"raise": "raise E", "return": "return 1"}.get(pick, pick))
        elif pick == "if":
            lines.append(indent + "if c:")
            block(inner, depth + 1, in_loop, in_def)
        elif pick == "ifelse":
            lines.append(indent + "if c:")
            block(inner, depth + 1, in_loop, in_def)
            lines.append(indent + "else:")
            block(inner, depth + 1, in_loop, in_def)
        elif pick == "elif":
            lines.append(indent + "if c:")
            block(inner, depth + 1, in_loop, in_def)
            lines.append(indent + "elif d:")
            block(inner, depth + 1, in_loop, in_def)
            lines.append(indent + "else:")
            block(inner, depth + 1, in_loop, in_def)
        elif pick in ("while_true", "while_c", "for"):
            header = {"while_true": "while True:", "while_c": "while c:", "for": "for i in r:"}[
                pick
            ]
            lines.append(indent + header)
            block(inner, depth + 1, True, in_def)
            if rng.random() < 0.3:
                lines.append(indent + "else:")
                block(inner, depth + 1, in_loop, in_def)
        elif pick == "try":
            lines.append(indent + "try:")
            block(inner, depth + 1, in_loop, in_def)
            lines.append(indent + "except E:")
            block(inner, depth + 1, in_loop, in_def)
            if rng.random() < 0.5:
                lines.append(indent + "finally:")
                block(inner, depth + 1, in_loop, in_def)
        elif pick == "with":
            lines.append(indent + "with m:")
            block(inner, depth + 1, in_loop, in_def)
        else:
            lines.append(indent + "def h():")
            block(inner, depth + 1, False, True)

    lines.append("def main():")
    block("    ", 1, False, True)
    block("", 1, False, False)
    return "\n".join(lines) + "\n"


def test_the_oracle_fixture(tmp_path: Path) -> None:
    result = unreachable(tmp_path, ORACLE)
    assert regions(result) == ORACLE_REGIONS
    assert summary(result) == (6, 6)
    assert _reference(ORACLE) == ORACLE_REGIONS


@pytest.mark.parametrize("seed", range(200))
def test_cpython_keeps_no_bytecode_on_any_reported_line(tmp_path: Path, seed: int) -> None:
    source = ORACLE if seed == 0 else _generate(seed)
    reported = found(tmp_path, source)
    carrying = _bytecode_lines(source)
    for line, end_line, *_ in reported:
        assert not carrying & set(range(line, end_line + 1)), (seed, line, end_line)


@pytest.mark.parametrize("seed", range(200))
def test_a_plain_recursive_reading_of_the_rules_agrees(tmp_path: Path, seed: int) -> None:
    source = ORACLE if seed == 0 else _generate(seed)
    assert found(tmp_path, source) == _reference(source)


def test_the_random_programs_exercise_every_cause() -> None:
    causes = {region[3] for seed in range(1, 200) for region in _reference(_generate(seed))}
    assert causes == {"return", "raise", "break", "continue", "if", "while"}


# --- API -------------------------------------------------------------------------------------


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


API_SOURCE = (
    "def load(path):\n"  # 1
    "    return open(path).read()\n"  # 2
    "    print('loaded')\n"  # 3: after return
    "def loop():\n"  # 4
    "    while True:\n"  # 5
    "        step()\n"  # 6
    "    finish()\n"  # 7: after an endless loop
    "    report()\n"  # 8
)
FILES: dict[str, bytes] = {
    "app/mod.py": API_SOURCE.encode(),
    "app/bad.py": b"def (:\n",
    "app/clean.py": b"def f():\n    if c:\n        return 1\n    return 2\n",
    "docs/notes.md": b"return\nx\n",
}
TARGET = {"path": "app/mod.py"}
API_RESULT = {
    "path": "app/mod.py",
    "status": "parsed",
    "regions": [
        {"line": 3, "end_line": 3, "statements": 1, "cause": "return", "cause_line": 2},
        {"line": 7, "end_line": 8, "statements": 2, "cause": "while", "cause_line": 5},
    ],
    "summary": {"regions": 2, "statements": 3},
    "truncated": False,
}


async def _ingest(client: httpx.AsyncClient, owner: Owner, files: dict[str, bytes]) -> None:
    response = await client.post(
        f"/api/v1/projects/{owner.project_id}/repositories/{owner.repository_id}/ingest",
        files={"file": ("repo.zip", _zip(files), "application/zip")},
        headers=owner.headers,
    )
    assert response.status_code == 202, response.text


async def _create_owner(client: httpx.AsyncClient, email: str, files: dict[str, bytes]) -> Owner:
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
    return await _create_owner(client, "alice@example.com", FILES)


def _url(owner: Owner, project_id: str | None = None, repository_id: str | None = None) -> str:
    return (
        f"/api/v1/projects/{project_id or owner.project_id}"
        f"/repositories/{repository_id or owner.repository_id}/files/unreachable-code"
    )


async def _get(
    client: httpx.AsyncClient, owner: Owner, params: dict[str, Any], **kw: Any
) -> httpx.Response:
    url = _url(owner, kw.pop("project_id", None), kw.pop("repository_id", None))
    return await client.get(url, params=params, headers=kw.pop("headers", owner.headers))


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


@requires_db
async def test_unreachable_code_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200, response.text
    assert response.json() == API_RESULT


@requires_db
async def test_endpoint_statuses(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    body = (await _get(client, alice, {"path": "docs/notes.md"})).json()
    assert body == {
        "path": "docs/notes.md",
        "status": "unsupported_language",
        "regions": [],
        "summary": ZERO,
        "truncated": False,
    }
    assert read_spy == []  # a non-Python file is never read
    body = (await _get(client, alice, {"path": "app/bad.py"})).json()
    assert body == {
        "path": "app/bad.py",
        "status": "syntax_error",
        "regions": [],
        "summary": ZERO,
        "truncated": False,
    }
    body = (await _get(client, alice, {"path": "app/clean.py"})).json()
    assert (body["status"], body["regions"], body["summary"]) == ("parsed", [], ZERO)


@requires_db
async def test_validation_and_unknown_paths(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    for params in ({"path": ""}, {"path": "a" * 4097}, {}):
        assert (await _get(client, alice, params)).status_code == 422, params
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _get(client, alice, TARGET, **kwargs)).status_code == 422
    assert read_spy == []
    for path in ("a" * 4096, "missing.py", "../app/mod.py", "/app/mod.py", "APP/MOD.PY"):
        response = await _get(client, alice, {"path": path})
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)


@requires_db
async def test_changed_storage_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    (_storage(alice) / "app" / "mod.py").write_bytes(API_SOURCE.encode() + b"# tampered\n")
    response = await _get(client, alice, TARGET)
    assert (response.status_code, response.json()) == (409, CHANGED)
    assert "tampered" not in response.text

    await _ingest(client, alice, FILES)
    (_storage(alice) / "app" / "mod.py").unlink()  # missing
    response = await _get(client, alice, TARGET)
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    root = _storage(alice)
    outside = tmp_path / "outside"
    shutil.copytree(root / "app", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "app")
    _junction(root / "app", outside)
    response = await _get(client, alice, TARGET)
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_symlinked_file_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "app" / "mod.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(FILES["app/mod.py"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    response = await _get(client, alice, TARGET)
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_storage_swap_during_the_request_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is selected, then storage changes before the file is read."""
    real = code_unreachable_code.repository_storage_dir

    def swap_then_root(settings: Any, repository_id: Any) -> Path:
        root = real(settings, repository_id)
        (root / "app" / "mod.py").write_bytes(b"def f():\n    return  # new snapshot\n")
        return root

    monkeypatch.setattr(code_unreachable_code, "repository_storage_dir", swap_then_root)
    response = await _get(client, alice, TARGET)
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
        body = (await _get(client, alice, TARGET)).json()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert body["status"] == "parsed"
    assert len([s for s in statements if "FROM files" in s]) == 1


@requires_db
async def test_no_source_internals_or_logged_content(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-69b2"
    files = dict(FILES)
    files["app/mod.py"] = (API_SOURCE + f"note = '{marker}'  # {marker}\n").encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "parsed"
    assert marker not in response.text and marker not in caplog.text
    for word in ("def ", "print", "loaded", "step", "finish"):
        assert word not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text
    body = response.json()
    assert set(body) == {"path", "status", "regions", "summary", "truncated"}
    assert set(body["summary"]) == {"regions", "statements"}
    for item in body["regions"]:
        assert set(item) == REGION_FIELDS


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _create_owner(
        client, "bob@example.com", {"b.py": b"def f():\n    return\n    a()\n"}
    )
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    params = {"path": "b.py"}
    for kwargs in (
        {"project_id": bob.project_id, "repository_id": bob.repository_id},
        {"project_id": other_project},
        {"repository_id": str(uuid.uuid4())},
    ):
        response = await _get(client, alice, params, **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert (await client.get(_url(alice), params=params)).status_code == 401
    bad = await _get(client, alice, params, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    own = (await _get(client, bob, params)).json()
    assert own["regions"] == [
        {"line": 3, "end_line": 3, "statements": 1, "cause": "return", "cause_line": 2}
    ]


# --- Rate limiting ---------------------------------------------------------------------------


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
    bob = await _create_owner(
        client, "bob@example.com", {"b.py": b"def f():\n    return\n    a()\n"}
    )
    limiter = _AlwaysLimited()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    params = {"path": "b.py"}
    for kwargs in (
        {"project_id": bob.project_id, "repository_id": bob.repository_id},
        {"repository_id": str(uuid.uuid4())},
    ):
        response = await _get(client, alice, params, **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert limiter.hits == []
    limited = await _get(client, alice, TARGET)
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts; please try again later"}
    assert limiter.hits == [(UNREACHABLE_CODE_LIMIT, alice.user_id)]
    assert read_spy == []  # limited before any snapshot read


@requires_db_and_redis
async def test_rate_limit_is_120_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert UNREACHABLE_CODE_LIMIT == Limit("unreachable-code-user", 120, 600)
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(settings)
    await pool.flushdb()
    app.dependency_overrides.pop(get_rate_limiter, None)  # use the real Step 17 limiter
    params = {"path": "docs/notes.md"}
    try:
        for _ in range(120):
            assert (await _get(client, alice, params)).status_code == 200
        limited = await _get(client, alice, params)
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts; please try again later"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 600
        keys = [k.decode() if isinstance(k, bytes) else k for k in await pool.keys("*")]
        assert any(k.startswith("ratelimit:unreachable-code-user:") for k in keys)
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
    params = {"path": "docs/notes.md"}
    for _ in range(122):  # beyond the limit
        assert (await _get(client, alice, params)).status_code == 200
