"""Python in-file name occurrences tests (GET .../files/occurrences?path=&line=&column=). Offline.

Analysis tests use a temporary storage directory and need no database. Expected occurrences are
written out by hand from Python's scoping rules (``pos`` only turns "the nth occurrence of this
identifier in the fixture text" into a 1-based line and code-point column). Two independent
oracles cross-check them: CPython's own ``symtable`` (test-only, on fixture text the tests
authored) for the local/free/global classification of every name, and the frozen Step 45
implementation for each name's boundness and for def/class bindings. API tests run against the
already-migrated throwaway test database (TEST_DATABASE_URL, "_test") inside rolled-back
transactions; the rate-limit test uses a disposable Redis (TEST_REDIS_URL). Junction tests are
Windows-only; symlink tests are skipped where symlinks cannot be created.
"""

import ast
import builtins
import hashlib
import logging
import os
import re
import shutil
import socket
import subprocess
import symtable
import sys
import tokenize
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
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
from app.services import code_definition, code_occurrences, ingestion
from app.services.code_definition import analyze_definition
from app.services.code_occurrences import (
    MAX_OCCURRENCES,
    OCCURRENCES_LIMIT,
    analyze_occurrences,
)
from app.services.code_outline import MAX_OUTLINE_BYTES
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
ZERO = {"occurrences": 0, "reads": 0, "writes": 0, "deletes": 0, "bindings": 0}


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


def analyze(tmp_path: Path, source: str | bytes, line: int, column: int) -> Any:
    root, rows = _repo(tmp_path, {"m.py": source})
    return analyze_occurrences(root, rows[0], line, column)


def pos(source: str, name: str, nth: int = 0) -> tuple[int, int]:
    """1-based (line, code-point column) of the nth whole-identifier occurrence of ``name`` in
    the fixture text (strings and comments included: the tests choose ``nth``)."""
    starts = [m.start() for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", source)]
    index = starts[nth]
    line = source.count("\n", 0, index) + 1
    return line, index - (source.rfind("\n", 0, index) + 1) + 1


def at(tmp_path: Path, source: str, name: str, nth: int = 0) -> Any:
    return analyze(tmp_path, source, *pos(source, name, nth))


def expect(source: str, name: str, kinds: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """(line, column, kind) of the given nth occurrences of ``name``."""
    return [(*pos(source, name, nth), kind) for nth, kind in kinds]


def listed(result: Any) -> list[tuple[int, int, str]]:
    return [(o.line, o.column, o.kind) for o in result.occurrences]


def assert_found(result: Any, scope: str, occurrences: list[tuple[int, int, str]]) -> None:
    assert (result.status, result.scope) == ("found", scope)
    assert listed(result) == occurrences
    kinds = [kind for _, _, kind in occurrences]
    assert result.summary.model_dump() == {
        "occurrences": len(kinds),
        "reads": kinds.count("read"),
        "writes": kinds.count("write"),
        "deletes": kinds.count("delete"),
        "bindings": kinds.count("parameter") + kinds.count("definition"),
    }
    assert result.truncated is False


def assert_status(result: Any, status: str, name: str | None) -> None:
    """A non-found status: no scope, no occurrences, all counts zero."""
    assert result.status == status
    assert (result.reference.name if result.reference else None) == name
    assert (result.scope, result.occurrences, result.truncated) == (None, [], False)
    assert result.summary.model_dump() == ZERO


def every(source: str, name: str) -> list[tuple[int, int]]:
    return [
        pos(source, name, nth) for nth in range(len(re.findall(rf"(?<!\w){name}(?!\w)", source)))
    ]


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_occurrences._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_occurrences, "_read_verified", spy)
    return reads


# --- A. Local bindings ---------------------------------------------------------------------------

SIMPLE = (
    "def calculate(items):\n"  # 1
    "    acc = 0\n"  # 2
    "    for item in items:\n"  # 3
    "        acc += item\n"  # 4
    "    return acc\n"  # 5
)


def test_simple_local_assignment(tmp_path: Path) -> None:
    result = at(tmp_path, SIMPLE, "acc", 2)
    assert_found(
        result, "function", expect(SIMPLE, "acc", [(0, "write"), (1, "write"), (2, "read")])
    )
    assert result.reference.model_dump() == {"path": "m.py", "line": 5, "column": 12, "name": "acc"}


def test_multiple_reads(tmp_path: Path) -> None:
    source = "def f(n):\n    total = n\n    print(total, total)\n    return total + n\n"
    occurrences = expect(source, "total", [(0, "write"), (1, "read"), (2, "read"), (3, "read")])
    assert_found(at(tmp_path, source, "total", 1), "function", occurrences)


def test_multiple_writes(tmp_path: Path) -> None:
    source = "def f():\n    x = 1\n    x = 2\n    x = x + 1\n    return x\n"
    kinds = [(0, "write"), (1, "write"), (2, "write"), (3, "read"), (4, "read")]
    assert_found(at(tmp_path, source, "x", 4), "function", expect(source, "x", kinds))


def test_delete(tmp_path: Path) -> None:
    source = "def f():\n    tmp = []\n    del tmp\n    tmp = None\n    return tmp\n"
    kinds = [(0, "write"), (1, "delete"), (2, "write"), (3, "read")]
    assert_found(at(tmp_path, source, "tmp", 1), "function", expect(source, "tmp", kinds))


def test_augmented_assignment_is_a_write(tmp_path: Path) -> None:
    source = "def f():\n    count = 0\n    count += 1\n    count -= 2\n    count **= 3\n    return count\n"
    kinds = [(0, "write"), (1, "write"), (2, "write"), (3, "write"), (4, "read")]
    assert_found(at(tmp_path, source, "count", 2), "function", expect(source, "count", kinds))


def test_annotated_assignment(tmp_path: Path) -> None:
    source = (
        "def f():\n"
        "    value: int = 3\n"
        "    label: str\n"  # an annotation without a value still binds the local
        "    label = str(value)\n"
        "    return label, value\n"
    )
    value = expect(source, "value", [(0, "write"), (1, "read"), (2, "read")])
    assert_found(at(tmp_path, source, "value"), "function", value)
    label = expect(source, "label", [(0, "write"), (1, "write"), (2, "read")])
    assert_found(at(tmp_path, source, "label", 2), "function", label)


def test_tuple_and_list_unpacking(tmp_path: Path) -> None:
    source = (
        "def f(pair):\n    a, b = pair\n    (c, d) = pair\n    [e, g] = pair\n"
        "    return a, b, c, d, e, g\n"
    )
    for name in ("a", "b", "c", "d", "e", "g"):
        result = at(tmp_path, source, name, 1)
        assert_found(result, "function", expect(source, name, [(0, "write"), (1, "read")]))


def test_nested_destructuring(tmp_path: Path) -> None:
    source = (
        "def f(data):\n"
        "    (a, (b, [c, *rest])) = data\n"
        "    for x, (y, z) in data:\n"
        "        print(x, y, z)\n"
        "    return a, b, c, rest\n"
    )
    for name in ("a", "b", "c", "rest", "x", "y", "z"):
        result = at(tmp_path, source, name, 1)
        assert_found(result, "function", expect(source, name, [(0, "write"), (1, "read")]))


def test_for_target(tmp_path: Path) -> None:
    source = "def f(items):\n    for item in items:\n        print(item)\n    return item\n"
    kinds = [(0, "write"), (1, "read"), (2, "read")]
    assert_found(at(tmp_path, source, "item"), "function", expect(source, "item", kinds))


def test_nested_for_targets(tmp_path: Path) -> None:
    source = "def f(grid):\n    for row in grid:\n        for cell in row:\n            print(row, cell)\n"
    row = expect(source, "row", [(0, "write"), (1, "read"), (2, "read")])
    assert_found(at(tmp_path, source, "row", 1), "function", row)
    cell = expect(source, "cell", [(0, "write"), (1, "read")])
    assert_found(at(tmp_path, source, "cell", 1), "function", cell)


def test_with_targets(tmp_path: Path) -> None:
    source = (
        "def f(path, lock):\n"
        "    with open(path) as handle, lock as (first, second):\n"
        "        return handle.read(), first, second\n"
    )
    for name in ("handle", "first", "second"):
        result = at(tmp_path, source, name, 1)
        assert_found(result, "function", expect(source, name, [(0, "write"), (1, "read")]))


def test_async_for_targets(tmp_path: Path) -> None:
    source = (
        "async def f(stream):\n"
        "    async for chunk in stream:\n"
        "        print(chunk)\n"
        "    return [part async for part in stream]\n"
    )
    chunk = expect(source, "chunk", [(0, "write"), (1, "read")])
    assert_found(at(tmp_path, source, "chunk", 1), "function", chunk)
    part = expect(source, "part", [(0, "read"), (1, "write")])
    assert_found(at(tmp_path, source, "part"), "comprehension", part)


def test_async_with_target(tmp_path: Path) -> None:
    source = "async def f(session):\n    async with session as conn:\n        return conn\n"
    conn = expect(source, "conn", [(0, "write"), (1, "read")])
    assert_found(at(tmp_path, source, "conn", 1), "function", conn)


def test_walrus(tmp_path: Path) -> None:
    source = "def f(items):\n    if (n := len(items)) > 3:\n        return n\n    return 0\n"
    assert_found(
        at(tmp_path, source, "n", 1), "function", expect(source, "n", [(0, "write"), (1, "read")])
    )


def test_nested_function_locals_are_separate(tmp_path: Path) -> None:
    source = (
        "def outer():\n"
        "    x = 1\n"  # outer's x
        "    def inner():\n"
        "        x = 2\n"  # inner's own x
        "        return x\n"
        "    return x + inner()\n"
    )
    assert_found(
        at(tmp_path, source, "x", 3), "function", expect(source, "x", [(0, "write"), (3, "read")])
    )
    assert_found(
        at(tmp_path, source, "x", 2), "function", expect(source, "x", [(1, "write"), (2, "read")])
    )


def test_closure_read_resolves_to_the_enclosing_binding(tmp_path: Path) -> None:
    source = (
        "def outer():\n"
        "    seen = set()\n"
        "    def add(value):\n"
        "        seen.add(value)\n"  # a closure read (seen is a name, add an attribute)
        "    add(1)\n"
        "    return seen\n"
    )
    seen = expect(source, "seen", [(0, "write"), (1, "read"), (2, "read")])
    assert_found(at(tmp_path, source, "seen", 1), "function", seen)


def test_closure_shadowing_creates_a_new_binding(tmp_path: Path) -> None:
    source = (
        "def outer():\n"
        "    level = 1\n"
        "    def inner():\n"
        "        level = 2\n"
        "        return level\n"
        "    def reader():\n"
        "        return level\n"
        "    return inner, reader\n"
    )
    outer = expect(source, "level", [(0, "write"), (3, "read")])
    assert_found(at(tmp_path, source, "level", 3), "function", outer)
    inner = expect(source, "level", [(1, "write"), (2, "read")])
    assert_found(at(tmp_path, source, "level", 2), "function", inner)


def test_lambda_parameters_have_their_own_scope(tmp_path: Path) -> None:
    source = "scale = 2\ndouble = lambda scale: scale * 2\nprint(scale, double)\n"
    module = expect(source, "scale", [(0, "write"), (3, "read")])
    assert_found(at(tmp_path, source, "scale", 3), "module", module)
    own = expect(source, "scale", [(1, "parameter"), (2, "read")])
    assert_found(at(tmp_path, source, "scale", 2), "lambda", own)


def test_lambda_defaults_belong_to_the_enclosing_scope(tmp_path: Path) -> None:
    source = "bias = 1\nshift = lambda v, bias=bias: v + bias\nprint(bias)\n"
    module = expect(source, "bias", [(0, "write"), (2, "read"), (4, "read")])
    assert_found(at(tmp_path, source, "bias", 2), "module", module)
    own = expect(source, "bias", [(1, "parameter"), (3, "read")])
    assert_found(at(tmp_path, source, "bias", 3), "lambda", own)


PARAMETERS = "def f(po, /, pos, *args, kw, **kwargs):\n    return po + pos, args, kw, kwargs\n"


@pytest.mark.parametrize(
    "name",
    [
        "pos",  # positional
        "po",  # positional-only
        "kw",  # keyword-only
        "args",  # *args
        "kwargs",  # **kwargs
    ],
)
def test_parameter_kinds(tmp_path: Path, name: str) -> None:
    occurrences = expect(PARAMETERS, name, [(0, "parameter"), (1, "read")])
    assert_found(at(tmp_path, PARAMETERS, name, 1), "function", occurrences)
    assert_found(at(tmp_path, PARAMETERS, name, 0), "function", occurrences)  # the site itself


DEFAULTS = (
    "limit = 10\n"  # 1
    "kind = int\n"  # 2
    "def f(limit: kind = limit) -> kind:\n"  # 3: annotations and the default are evaluated outside
    "    kind = limit\n"  # 4: the function's own kind; limit is the parameter
    "    return kind\n"  # 5
)


def test_defaults_and_annotations_belong_to_the_enclosing_scope(tmp_path: Path) -> None:
    module_limit = expect(DEFAULTS, "limit", [(0, "write"), (2, "read")])
    assert_found(at(tmp_path, DEFAULTS, "limit", 2), "module", module_limit)
    parameter = expect(DEFAULTS, "limit", [(1, "parameter"), (3, "read")])
    assert_found(at(tmp_path, DEFAULTS, "limit", 3), "function", parameter)
    module_kind = expect(DEFAULTS, "kind", [(0, "write"), (1, "read"), (2, "read")])
    assert_found(at(tmp_path, DEFAULTS, "kind", 1), "module", module_kind)
    local_kind = expect(DEFAULTS, "kind", [(3, "write"), (4, "read")])
    assert_found(at(tmp_path, DEFAULTS, "kind", 4), "function", local_kind)


def test_except_names_bind_in_their_scope(tmp_path: Path) -> None:
    source = (
        "def f(action):\n"
        "    try:\n"
        "        action()\n"
        "    except (KeyError, ValueError) as err:\n"
        "        print(err)\n"
        "        raise\n"
        "try:\n"
        "    pass\n"
        "except* OSError as err:\n"  # a module-level, ExceptionGroup handler: its own binding
        "    print(err)\n"
    )
    local = expect(source, "err", [(0, "write"), (1, "read")])
    assert_found(at(tmp_path, source, "err", 1), "function", local)
    assert_found(at(tmp_path, source, "err", 0), "function", local)  # the handler's name
    module = expect(source, "err", [(2, "write"), (3, "read")])
    assert_found(at(tmp_path, source, "err", 2), "module", module)


# --- B. Comprehensions ------------------------------------------------------------------------


def test_list_comprehension_has_its_own_scope(tmp_path: Path) -> None:
    source = "def f(xs):\n    x = 0\n    ys = [x * 2 for x in xs]\n    return x, ys\n"
    local = expect(source, "x", [(0, "write"), (3, "read")])
    assert_found(at(tmp_path, source, "x", 3), "function", local)
    own = expect(source, "x", [(1, "read"), (2, "write")])
    assert_found(at(tmp_path, source, "x", 1), "comprehension", own)
    assert_found(at(tmp_path, source, "x", 2), "comprehension", own)  # the target itself


def test_set_comprehension(tmp_path: Path) -> None:
    source = "def f(xs):\n    return {v for v in xs if v}\n"
    occurrences = expect(source, "v", [(0, "read"), (1, "write"), (2, "read")])
    assert_found(at(tmp_path, source, "v"), "comprehension", occurrences)


def test_dict_comprehension(tmp_path: Path) -> None:
    source = "def f(pairs):\n    return {k: v for k, v in pairs}\n"
    for name in ("k", "v"):
        result = at(tmp_path, source, name)
        assert_found(result, "comprehension", expect(source, name, [(0, "read"), (1, "write")]))


def test_generator_expression(tmp_path: Path) -> None:
    source = "def f(xs):\n    return sum(n * n for n in xs)\n"
    occurrences = expect(source, "n", [(0, "read"), (1, "read"), (2, "write")])
    assert_found(at(tmp_path, source, "n", 2), "comprehension", occurrences)


def test_nested_comprehensions(tmp_path: Path) -> None:
    source = "def f(grid):\n    return [[cell for cell in row] for row in grid]\n"
    # The inner comprehension's first iterable is evaluated in the outer comprehension.
    row = expect(source, "row", [(0, "read"), (1, "write")])
    assert_found(at(tmp_path, source, "row"), "comprehension", row)
    cell = expect(source, "cell", [(0, "read"), (1, "write")])
    assert_found(at(tmp_path, source, "cell"), "comprehension", cell)
    reads_outer = "def f(grid):\n    return [[row for _ in range(2)] for row in grid]\n"
    outer = expect(reads_outer, "row", [(0, "read"), (1, "write")])
    assert_found(at(tmp_path, reads_outer, "row"), "comprehension", outer)


def test_first_iterable_is_evaluated_in_the_enclosing_scope(tmp_path: Path) -> None:
    source = "def f(x):\n    return [x for x in x]\n"
    parameter = expect(source, "x", [(0, "parameter"), (3, "read")])
    assert_found(at(tmp_path, source, "x", 3), "function", parameter)
    own = expect(source, "x", [(1, "read"), (2, "write")])
    assert_found(at(tmp_path, source, "x", 1), "comprehension", own)
    in_class = "class C:\n    names = ['a']\n    upper = [n.upper() for n in names]\n"
    names = expect(in_class, "names", [(0, "write"), (1, "read")])
    assert_found(at(tmp_path, in_class, "names", 1), "class", names)


def test_walrus_inside_a_comprehension_binds_the_enclosing_function(tmp_path: Path) -> None:
    source = (
        "def f(xs):\n    if any((hit := v) > 2 for v in xs):\n        return hit\n    return None\n"
    )
    hit = expect(source, "hit", [(0, "write"), (1, "read")])
    assert_found(at(tmp_path, source, "hit", 1), "function", hit)
    assert_found(at(tmp_path, source, "hit", 0), "function", hit)


# --- C. Class and function scopes -----------------------------------------------------------


def test_function_local_shadows_a_module_name(tmp_path: Path) -> None:
    source = "name = 'module'\ndef f():\n    name = 'local'\n    return name\nprint(name)\n"
    module = expect(source, "name", [(0, "write"), (3, "read")])
    assert_found(at(tmp_path, source, "name", 3), "module", module)
    local = expect(source, "name", [(1, "write"), (2, "read")])
    assert_found(at(tmp_path, source, "name", 2), "function", local)


def test_nested_functions_read_a_grandparent_binding(tmp_path: Path) -> None:
    source = (
        "def a():\n    v = 1\n    def b():\n        def c():\n            return v\n"
        "        return c\n    return b\n"
    )
    assert_found(
        at(tmp_path, source, "v", 1), "function", expect(source, "v", [(0, "write"), (1, "read")])
    )


def test_a_nested_assignment_shadows_the_outer_binding(tmp_path: Path) -> None:
    source = (
        "def a():\n"
        "    v = 1\n"
        "    def b():\n"
        "        v = 2\n"
        "        def c():\n"
        "            return v\n"  # b's v
        "        return c\n"
        "    return b, v\n"  # a's v
    )
    assert_found(
        at(tmp_path, source, "v", 3), "function", expect(source, "v", [(0, "write"), (3, "read")])
    )
    assert_found(
        at(tmp_path, source, "v", 2), "function", expect(source, "v", [(1, "write"), (2, "read")])
    )


def test_class_body_scope(tmp_path: Path) -> None:
    source = "class Config:\n    size = 3\n    double = size * 2\n    del double\n"
    assert_found(
        at(tmp_path, source, "size", 1),
        "class",
        expect(source, "size", [(0, "write"), (1, "read")]),
    )
    double = expect(source, "double", [(0, "write"), (1, "delete")])
    assert_found(at(tmp_path, source, "double"), "class", double)


def test_methods_do_not_see_class_body_names(tmp_path: Path) -> None:
    source = (
        "size = 10\n"
        "class Config:\n"
        "    size = 3\n"
        "    def area(self):\n"
        "        return size * self.size\n"  # the module's size; self.size is an attribute
    )
    module = expect(source, "size", [(0, "write"), (2, "read")])
    assert_found(at(tmp_path, source, "size", 2), "module", module)
    assert_found(at(tmp_path, source, "size", 1), "class", expect(source, "size", [(1, "write")]))


def test_a_class_inside_a_function_sees_its_locals(tmp_path: Path) -> None:
    source = "def make(unit):\n    class Box:\n        label = unit\n    return Box\n"
    unit = expect(source, "unit", [(0, "parameter"), (1, "read")])
    assert_found(at(tmp_path, source, "unit", 1), "function", unit)
    box = expect(source, "Box", [(0, "definition"), (1, "read")])
    assert_found(at(tmp_path, source, "Box", 1), "function", box)


def test_a_lambda_inside_a_function_reads_its_locals(tmp_path: Path) -> None:
    source = "def f():\n    factor = 3\n    return lambda v: v * factor\n"
    factor = expect(source, "factor", [(0, "write"), (1, "read")])
    assert_found(at(tmp_path, source, "factor", 1), "function", factor)


def test_class_comprehension_conditions_do_not_see_class_names(tmp_path: Path) -> None:
    source = "class C:\n    limit = 3\n    big = [v for v in range(5) if v > limit]\n"
    assert_status(at(tmp_path, source, "limit", 1), "not_bound", "limit")
    assert_found(at(tmp_path, source, "limit", 0), "class", expect(source, "limit", [(0, "write")]))


# --- D. Identifier statuses -----------------------------------------------------------------


def test_builtins_and_never_bound_names_are_not_bound(tmp_path: Path) -> None:
    source = "def f(items):\n    return len(items), missing\n"
    assert_status(at(tmp_path, source, "len"), "not_bound", "len")
    result = at(tmp_path, source, "missing")
    assert_status(result, "not_bound", "missing")
    assert result.reference.model_dump() == {
        "path": "m.py",
        "line": 2,
        "column": 24,
        "name": "missing",
    }
    star = "from helpers import *\nprint(maybe)\n"
    assert_status(at(tmp_path, star, "maybe"), "not_bound", "maybe")


def test_attribute_names_are_never_occurrences(tmp_path: Path) -> None:
    source = "def f(obj):\n    total = 0\n    method = obj.method\n    return obj.total + total\n"
    assert_status(at(tmp_path, source, "total", 1), "unsupported", "total")  # obj.total
    assert_status(at(tmp_path, source, "method", 1), "unsupported", "method")  # obj.method
    local = expect(source, "total", [(0, "write"), (2, "read")])
    assert_found(at(tmp_path, source, "total", 2), "function", local)
    assert_found(
        at(tmp_path, source, "method", 0), "function", expect(source, "method", [(0, "write")])
    )


def test_keyword_argument_names_are_never_occurrences(tmp_path: Path) -> None:
    source = "def f(acc):\n    return calculate(value=acc)\n"
    assert_status(at(tmp_path, source, "value"), "unsupported", "value")
    acc = expect(source, "acc", [(0, "parameter"), (1, "read")])
    assert_found(at(tmp_path, source, "acc", 1), "function", acc)


def test_strings_and_fstring_text_are_not_identifiers(tmp_path: Path) -> None:
    source = "def f():\n    s = 'acc'\n    t = f\"acc {s}\"\n    return s, t\n"
    assert_status(at(tmp_path, source, "acc", 0), "no_identifier", None)  # in 'acc'
    assert_status(at(tmp_path, source, "acc", 1), "no_identifier", None)  # f-string text
    occurrences = expect(source, "s", [(0, "write"), (1, "read"), (2, "read")])
    assert_found(at(tmp_path, source, "s", 1), "function", occurrences)  # {s} is a name


def test_comments_are_not_identifiers(tmp_path: Path) -> None:
    source = "def f():\n    x = 1  # x is local\n    return x\n"
    assert_status(at(tmp_path, source, "x", 1), "no_identifier", None)
    assert_found(
        at(tmp_path, source, "x", 2), "function", expect(source, "x", [(0, "write"), (2, "read")])
    )


@pytest.mark.parametrize("position", [(2, 1), (2, 4), (2, 8), (4, 13)])  # (4, 13): "+="
def test_whitespace_and_punctuation_are_not_identifiers(
    tmp_path: Path, position: tuple[int, int]
) -> None:
    assert_status(analyze(tmp_path, SIMPLE, *position), "no_identifier", None)


@pytest.mark.parametrize("word", ["def", "for", "in", "return"])
def test_keywords_are_not_identifiers(tmp_path: Path, word: str) -> None:
    assert_status(at(tmp_path, SIMPLE, word), "no_identifier", None)


def test_constants_and_other_keywords_are_not_identifiers(tmp_path: Path) -> None:
    source = "def f(a=None):\n    g = lambda: True\n    return a if a else g\n"
    for word in ("None", "lambda", "True", "if", "else"):
        assert_status(at(tmp_path, source, word), "no_identifier", None)


@pytest.mark.parametrize("position", [(1, 100), (5, 30), (6, 1), (99, 1)])
def test_positions_outside_the_source_are_not_identifiers(
    tmp_path: Path, position: tuple[int, int]
) -> None:
    assert_status(analyze(tmp_path, SIMPLE, *position), "no_identifier", None)


GLOBAL = (
    "count = 0\n"
    "def bump():\n"
    "    global count\n"
    "    count += 1\n"
    "def other(count):\n"  # an unrelated parameter: still unsupported (no alias tracking)
    "    return count\n"
)


def test_global_declarations_are_unsupported(tmp_path: Path) -> None:
    for position in every(GLOBAL, "count"):
        assert_status(analyze(tmp_path, GLOBAL, *position), "unsupported", "count")


def test_nonlocal_declarations_are_unsupported(tmp_path: Path) -> None:
    source = (
        "def outer():\n    n = 0\n    def inner():\n        nonlocal n\n        n += 1\n"
        "    return n\n"
    )
    for position in every(source, "n"):
        assert_status(analyze(tmp_path, source, *position), "unsupported", "n")


PEP695 = (
    "T = int\n"  # 1
    "def first[T](items: list[T]) -> T:\n"  # 2: every T here is the type parameter
    "    value: T = items[0]\n"  # 3
    "    return value\n"  # 4
    "class Box[T]:\n"  # 5
    "    item: T\n"  # 6
    "print(T)\n"  # 7
)


def test_type_parameters_are_unsupported(tmp_path: Path) -> None:
    for nth in range(1, 7):
        assert_status(at(tmp_path, PEP695, "T", nth), "unsupported", "T")
    # The module's T: its type-parameter namesakes are never its occurrences.
    module = expect(PEP695, "T", [(0, "write"), (7, "read")])
    assert_found(at(tmp_path, PEP695, "T", 7), "module", module)
    value = expect(PEP695, "value", [(0, "write"), (1, "read")])
    assert_found(at(tmp_path, PEP695, "value", 1), "function", value)


IMPORTS = (
    "import os.path\n"
    "from json import loads as parse\n"
    "import sys as system\n"
    "def f():\n"
    "    return os.getcwd(), parse('1'), system\n"
)


def test_imports_are_unsupported(tmp_path: Path) -> None:
    for name in ("os", "path", "json", "loads", "parse", "sys", "system"):
        for position in every(IMPORTS, name):
            assert_status(analyze(tmp_path, IMPORTS, *position), "unsupported", name)
    mixed = "def f():\n    import json\n    json = 1\n    return json\n"
    for position in every(mixed, "json"):
        assert_status(analyze(tmp_path, mixed, *position), "unsupported", "json")


MATCH = (
    "def f(command):\n"
    "    match command:\n"
    "        case [name, *rest]:\n"
    "            return name, rest\n"
    "        case {'k': value, **others}:\n"
    "            return value, others\n"
    "        case _:\n"
    "            return None\n"
)


def test_match_captures_are_unsupported(tmp_path: Path) -> None:
    for name in ("name", "rest", "value", "others"):
        for position in every(MATCH, name):
            assert_status(analyze(tmp_path, MATCH, *position), "unsupported", name)
    for word in ("match", "case", "_"):
        assert_status(at(tmp_path, MATCH, word), "no_identifier", None)  # soft keywords
    mixed = "def f(v):\n    x = 1\n    match v:\n        case x:\n            pass\n    return x\n"
    for position in every(mixed, "x"):
        assert_status(analyze(tmp_path, mixed, *position), "unsupported", "x")


def test_soft_keywords_used_as_names_are_supported(tmp_path: Path) -> None:
    source = "match = 1\ncase = match + 1\ntype = case\n_ = type\nprint(match, case, type, _)\n"
    expected = {
        "match": [(0, "write"), (1, "read"), (2, "read")],
        "case": [(0, "write"), (1, "read"), (2, "read")],
        "type": [(0, "write"), (1, "read"), (2, "read")],
        "_": [(0, "write"), (1, "read")],
    }
    for name, kinds in expected.items():
        assert_found(at(tmp_path, source, name), "module", expect(source, name, kinds))


# --- E. Occurrence kinds --------------------------------------------------------------------

MIXED = "def f(a):\n" "    a = a + 1\n" "    del a\n" "    a = 2\n" "    print(a, a)\n"
MIXED_KINDS = [
    (0, "parameter"),
    (1, "write"),
    (2, "read"),
    (3, "delete"),
    (4, "write"),
    (5, "read"),
    (6, "read"),
]


def test_mixed_kinds_and_summary(tmp_path: Path) -> None:
    result = at(tmp_path, MIXED, "a", 5)
    assert_found(result, "function", expect(MIXED, "a", MIXED_KINDS))
    assert result.summary.model_dump() == {
        "occurrences": 7,
        "reads": 3,
        "writes": 2,
        "deletes": 1,
        "bindings": 1,
    }


def test_every_occurrence_selects_the_same_binding(tmp_path: Path) -> None:
    expected = expect(MIXED, "a", MIXED_KINDS)
    for line, column, _ in expected:
        result = analyze(tmp_path, MIXED, line, column)
        assert_found(result, "function", expected)
        assert (result.reference.line, result.reference.column) == (line, column)


DEFINITIONS = (
    "def helper():\n"
    "    return 1\n"
    "class Model:\n"
    "    pass\n"
    "async def fetch():\n"
    "    return helper(), Model()\n"
    "def outer():\n"
    "    @staticmethod\n"
    "    def inner():\n"
    "        return 2\n"
    "    return inner()\n"
)


def test_definitions(tmp_path: Path) -> None:
    helper = expect(DEFINITIONS, "helper", [(0, "definition"), (1, "read")])
    assert_found(at(tmp_path, DEFINITIONS, "helper", 1), "module", helper)
    assert_found(at(tmp_path, DEFINITIONS, "helper", 0), "module", helper)  # the def's name
    model = expect(DEFINITIONS, "Model", [(0, "definition"), (1, "read")])
    assert_found(at(tmp_path, DEFINITIONS, "Model"), "module", model)
    fetch = expect(DEFINITIONS, "fetch", [(0, "definition")])
    assert_found(at(tmp_path, DEFINITIONS, "fetch"), "module", fetch)  # async def
    inner = expect(DEFINITIONS, "inner", [(0, "definition"), (1, "read")])
    assert_found(at(tmp_path, DEFINITIONS, "inner"), "function", inner)  # decorated, nested


def test_property_setter_definitions_share_the_class_binding(tmp_path: Path) -> None:
    source = (
        "class C:\n"
        "    @property\n"
        "    def x(self):\n"
        "        return 1\n"
        "    @x.setter\n"  # a class-scope read of the first x
        "    def x(self, value):\n"
        "        pass\n"
    )
    occurrences = expect(source, "x", [(0, "definition"), (1, "read"), (2, "definition")])
    assert_found(at(tmp_path, source, "x", 1), "class", occurrences)
    value = expect(source, "value", [(0, "parameter")])
    assert_found(at(tmp_path, source, "value"), "function", value)


def test_occurrences_are_in_source_order(tmp_path: Path) -> None:
    result = at(tmp_path, SIMPLE, "items")
    assert listed(result) == sorted(listed(result))
    assert listed(result) == expect(SIMPLE, "items", [(0, "parameter"), (1, "read")])


def test_duplicates_are_kept(tmp_path: Path) -> None:
    source = "def f():\n    x, x = 1, 2\n    print(x, x)\n    return x\n"
    kinds = [(0, "write"), (1, "write"), (2, "read"), (3, "read"), (4, "read")]
    assert_found(at(tmp_path, source, "x"), "function", expect(source, "x", kinds))


# --- F. Positions ---------------------------------------------------------------------------


def test_lines_are_one_based_across_blank_lines_and_comments(tmp_path: Path) -> None:
    source = "\n\n# a comment\ndef f(v):\n\n    return v\n"
    assert_found(
        analyze(tmp_path, source, 6, 12), "function", [(4, 7, "parameter"), (6, 12, "read")]
    )


def test_columns_count_code_points(tmp_path: Path) -> None:
    source = "def f(v):\n    s = 'ééé'; t = v\n    return t\n"
    t = [(2, 16, "write"), (3, 12, "read")]  # 19 if counted in UTF-8 bytes
    assert_found(analyze(tmp_path, source, 3, 12), "function", t)
    assert_found(
        analyze(tmp_path, source, 2, 20), "function", [(1, 7, "parameter"), (2, 20, "read")]
    )


def test_non_ascii_identifiers(tmp_path: Path) -> None:
    source = "def f(größe):\n    maß = größe * 2\n    return maß\n"
    assert_found(
        analyze(tmp_path, source, 2, 11), "function", [(1, 7, "parameter"), (2, 11, "read")]
    )
    result = analyze(tmp_path, source, 3, 12)
    assert_found(result, "function", [(2, 5, "write"), (3, 12, "read")])
    assert result.reference.name == "maß"


def test_tabs_count_as_one_column(tmp_path: Path) -> None:
    source = "def f():\n\tvalue = 1\n\treturn value\n"
    assert_found(analyze(tmp_path, source, 3, 9), "function", [(2, 2, "write"), (3, 9, "read")])


@pytest.mark.parametrize(
    "source",
    [
        "def f(a):\r\n    return a\r\n",  # CRLF
        "def f(a):\r    return a\r",  # CR
        b"\xef\xbb\xbfdef f(a):\n    return a\n",  # a BOM is not a column
    ],
)
def test_line_endings_and_bom(tmp_path: Path, source: str | bytes) -> None:
    assert_found(
        analyze(tmp_path, source, 2, 12), "function", [(1, 7, "parameter"), (2, 12, "read")]
    )


def test_nfkc_normalized_identifiers_share_a_binding(tmp_path: Path) -> None:
    source = "def f(ｘ):\n    return x + ｘ\n"  # fullwidth x is x for Python
    result = analyze(tmp_path, source, 2, 16)
    assert_found(result, "function", [(1, 7, "parameter"), (2, 12, "read"), (2, 16, "read")])
    assert result.reference.name == "x"
    ligature = "ﬁle = 1\nprint(file)\n"
    assert_found(analyze(tmp_path, ligature, 2, 7), "module", [(1, 1, "write"), (2, 7, "read")])


# --- G. Limits ------------------------------------------------------------------------------


def _many(count: int) -> str:
    """One write and ``count - 1`` reads of x, one per line."""
    return "x = 0\n" + "x\n" * (count - 1)


def test_exactly_the_cap_is_not_truncated(tmp_path: Path) -> None:
    result = analyze(tmp_path, _many(MAX_OCCURRENCES), 1, 1)
    assert MAX_OCCURRENCES == 1000
    assert (result.status, len(result.occurrences), result.truncated) == ("found", 1000, False)
    assert result.summary.model_dump() == {**ZERO, "occurrences": 1000, "reads": 999, "writes": 1}


def test_over_the_cap_is_truncated_and_counted_before_the_cap(tmp_path: Path) -> None:
    result = analyze(tmp_path, _many(1001), 2, 1)
    assert (result.status, result.truncated) == ("found", True)
    assert listed(result) == [(1, 1, "write")] + [(n, 1, "read") for n in range(2, 1001)]
    assert result.summary.model_dump() == {**ZERO, "occurrences": 1001, "reads": 1000, "writes": 1}
    last = analyze(tmp_path, _many(1001), 1001, 1)  # beyond the cap: still the first 1000
    assert (last.reference.line, last.summary.occurrences) == (1001, 1001)
    assert listed(last) == listed(result)


def test_long_names_are_capped_in_the_reference(tmp_path: Path) -> None:
    name = "v" * 250
    result = analyze(tmp_path, f"{name} = 1\nprint({name})\n", 2, 7)
    assert_found(result, "module", [(1, 1, "write"), (2, 7, "read")])
    assert result.reference.name == "v" * 200


def test_stored_size_limit_is_checked_before_opening(tmp_path: Path, read_spy: list[str]) -> None:
    result = analyze(tmp_path, os.urandom(MAX_CONTENT_BYTES), 1, 1)
    assert result.status in ("binary", "too_large") and read_spy == ["m.py"]  # 1 MiB opens
    read_spy.clear()
    assert_status(analyze(tmp_path, os.urandom(MAX_CONTENT_BYTES) + b"x", 1, 1), "too_large", None)
    assert read_spy == []  # over 1 MiB is never opened


def test_decoded_size_boundary(tmp_path: Path) -> None:
    body = b"def f(a):\n    return a\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    assert len(exactly) == MAX_OUTLINE_BYTES
    assert_found(
        analyze(tmp_path, exactly, 2, 12), "function", [(1, 7, "parameter"), (2, 12, "read")]
    )
    assert_status(analyze(tmp_path, exactly + b"#", 2, 12), "too_large", None)  # never parsed


# --- H (offline). Files, snapshot and safety ------------------------------------------------


def test_binary_files(tmp_path: Path) -> None:
    assert_status(analyze(tmp_path, b"\x00\x01acc", 1, 3), "binary", None)
    assert_status(analyze(tmp_path, b"\xff\xfe", 1, 1), "binary", None)


def test_syntax_errors(tmp_path: Path) -> None:
    assert_status(analyze(tmp_path, "def (:\n    acc\n", 2, 5), "syntax_error", None)
    assert_status(analyze(tmp_path, "acc = '''\nacc\n", 1, 1), "syntax_error", None)


def test_tokenize_failure_is_a_syntax_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(source: str) -> Any:
        raise tokenize.TokenError("simulated", (1, 0))

    monkeypatch.setattr(code_occurrences, "name_tokens", failing)
    assert_status(analyze(tmp_path, SIMPLE, 5, 12), "syntax_error", None)


def test_empty_file_has_no_identifier(tmp_path: Path) -> None:
    assert_status(analyze(tmp_path, "", 1, 1), "no_identifier", None)


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": SIMPLE})
    (root / "m.py").write_bytes(SIMPLE.encode() + b"# changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_occurrences(root, rows[0], 5, 12)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "m.py").unlink()  # missing
    with pytest.raises(FileContentError) as raised:
        analyze_occurrences(root, rows[0], 5, 12)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_symlinked_file_fails_the_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": SIMPLE})
    outside = tmp_path / "outside.py"
    outside.write_bytes(SIMPLE.encode())
    (root / "m.py").unlink()
    try:
        os.symlink(outside, root / "m.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    with pytest.raises(FileContentError) as raised:
        analyze_occurrences(root, rows[0], 5, 12)
    assert raised.value.status_code == 409


def test_junctioned_package_fails_the_request(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    root, rows = _repo(tmp_path, {"pkg/m.py": SIMPLE})
    outside = tmp_path / "outside"
    shutil.copytree(root / "pkg", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pkg")
    _winapi.CreateJunction(str(outside), str(root / "pkg"))  # type: ignore[attr-defined]
    with pytest.raises(FileContentError) as raised:
        analyze_occurrences(root, rows[0], 5, 12)
    assert raised.value.status_code == 409


def test_nothing_is_cached_across_requests(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": SIMPLE})
    assert len(analyze_occurrences(root, rows[0], 5, 12).occurrences) == 3
    changed = SIMPLE.replace("    return acc\n", "    acc = acc\n    return acc\n").encode()
    (root / "m.py").write_bytes(changed)
    row = SimpleNamespace(
        path="m.py", size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest()
    )
    assert len(analyze_occurrences(root, row, 6, 12).occurrences) == 5


def test_only_the_requested_file_is_read(tmp_path: Path, read_spy: list[str]) -> None:
    root, rows = _repo(
        tmp_path,
        {"m.py": "from other import helper\nx = 1\nprint(x)\n", "other.py": "helper = 1\n"},
    )
    result = analyze_occurrences(root, next(r for r in rows if r.path == "m.py"), 3, 7)
    assert_found(result, "module", [(2, 1, "write"), (3, 7, "read")])
    assert read_spy == ["m.py"]


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"  # 1
        "import evil_module_q58, subprocess\n"  # 2
        "loaded = __import__('evil_dynamic_q58')\n"  # 3
        "os_value = subprocess.run(['echo'])\n"  # 4
        "eval('1')\nexec('made = 1')\n"  # 5, 6
        "def deco(f):\n    open('side-effect', 'w')\n    return f\n"  # 7-9
        "@deco\ndef target(arg):\n    return arg + made\n"  # 10-12
    )
    root, rows = _repo(tmp_path, {"m.py": evil})
    compiled_flags: list[int] = []
    opened: list[str] = []
    imported: list[str] = []
    real_compile, real_open, real_import = builtins.compile, builtins.open, builtins.__import__

    def watching_compile(*args: Any, **kwargs: Any) -> Any:
        compiled_flags.append(kwargs.get("flags", args[3] if len(args) > 3 else 0))
        return real_compile(*args, **kwargs)

    def watching_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def watching_import(name: str, *args: Any, **kwargs: Any) -> Any:
        imported.append(name)
        return real_import(name, *args, **kwargs)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("repository code must never run")

    monkeypatch.setattr(builtins, "compile", watching_compile)
    monkeypatch.setattr(builtins, "open", watching_open)
    monkeypatch.setattr(builtins, "__import__", watching_import)
    for target, name in (
        (builtins, "exec"),
        (builtins, "eval"),
        (os, "system"),
        (subprocess, "Popen"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
    ):
        monkeypatch.setattr(target, name, refuse)
    line, column = pos(evil, "arg", 1)
    result = analyze_occurrences(root, rows[0], line, column)
    monkeypatch.undo()
    assert listed(result) == [(*pos(evil, "arg", 0), "parameter"), (line, column, "read")]
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)
    assert opened == [str(root / "m.py")]  # no other file is ever opened
    assert not any(name.startswith("evil_") for name in imported)  # nor imported


def test_responses_carry_no_source_or_internals(tmp_path: Path) -> None:
    marker = "UNIQUE-CONTENT-MARKER-58c3"
    source = SIMPLE + f"note = '{marker}'  # {marker}\n"
    result = analyze(tmp_path, source, 5, 12)
    text = result.model_dump_json()
    assert marker not in text
    for word in ("def ", "return", "items:", "+="):
        assert word not in text
    assert hashlib.sha256(source.encode()).hexdigest() not in text
    body = result.model_dump()
    assert set(body) == {"status", "reference", "scope", "occurrences", "summary", "truncated"}
    assert set(body["reference"]) == {"path", "line", "column", "name"}
    assert set(body["summary"]) == set(ZERO)
    for entry in body["occurrences"]:
        assert set(entry) == {"line", "column", "kind"}


def test_rate_limit_constant() -> None:
    assert OCCURRENCES_LIMIT == Limit("occurrences-user", 120, 600)


# --- H (offline). The request: one SELECT, language before any read --------------------------


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
    root, rows = _repo(tmp_path, {"m.py": SIMPLE, "notes.md": "acc\n"})
    monkeypatch.setattr(code_occurrences, "repository_storage_dir", lambda settings, rid: root)
    return root, rows


async def _request(db: _Db, path: str, line: int, column: int) -> Any:
    session: Any = db  # the stand-in session
    repository: Any = SimpleNamespace(id=uuid.uuid4())
    return await code_occurrences.code_occurrences(session, repository, path, line, column)


async def test_request_reads_one_row_and_analyzes_the_stored_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    _, rows = storage
    db = _Db(rows[0])
    result = await _request(db, "m.py", 5, 12)
    assert_found(
        result, "function", expect(SIMPLE, "acc", [(0, "write"), (1, "write"), (2, "read")])
    )
    assert len(db.statements) == 1 and read_spy == ["m.py"]
    sql = str(db.statements[0])
    assert "FROM files" in sql and "files.sha256" in sql and "files.path" in sql


async def test_request_never_reads_a_non_python_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    row = SimpleNamespace(
        path="notes.md", language="Markdown", extension="md", size_bytes=4, sha256="x"
    )
    assert_status(await _request(_Db(row), "notes.md", 1, 1), "unsupported_language", None)
    assert read_spy == []


async def test_request_for_an_unknown_file_is_404(read_spy: list[str]) -> None:
    with pytest.raises(FileContentError) as raised:
        await _request(_Db(None), "missing.py", 1, 1)
    assert (raised.value.status_code, raised.value.detail) == (404, FILE_NOT_FOUND["detail"])
    assert read_spy == []


# --- Oracle 1: CPython's symbol table (test-only) --------------------------------------------

ORACLE = """\
import json
limit = 10
def outer(a, limit=limit, *rest, key=None, **extra):
    acc = a
    def inner(b):
        total = acc + b
        def deepest():
            return total + acc + limit
        return deepest
    shift = lambda q, w=acc: q + w + acc
    try:
        json.loads('{}')
    except ValueError as err:
        acc = err
    del rest
    return inner, shift, key, extra, missing, len
def other():
    acc = 1
    return acc, limit, outer
"""

_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _own_region(node: Any) -> tuple[tuple[int, int], tuple[int, int]]:
    """Where a scope node's own scope evaluates names: a def/class body, a lambda body, or (for
    comprehensions, which the oracle skips) the whole expression."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start = (node.body[0].lineno, node.body[0].col_offset)
    elif isinstance(node, ast.Lambda):
        start = (node.body.lineno, node.body.col_offset)
    else:
        start = (node.lineno, node.col_offset)
    return start, (node.end_lineno, node.end_col_offset)


def _symtable_cases(source: str) -> Iterator[tuple[ast.Name, str, Any]]:
    """(name node, expectation, own scope node or None for the module) for every Name whose
    innermost own scope is the module, a def or a lambda. Class bodies and comprehensions are
    skipped (3.12's symtable inlines comprehensions); explicit tests cover them."""
    tree = ast.parse(source)
    top = symtable.symtable(source, "<fixture>", "exec")
    tables: dict[tuple[str, int], Any] = {}
    declared: set[str] = set()

    def walk(table: Any) -> None:
        for symbol in table.get_symbols():
            if symbol.is_declared_global() or symbol.is_nonlocal():
                declared.add(symbol.get_name())
        for child in table.get_children():
            if child.get_type() == "function":
                tables[(child.get_name(), child.get_lineno())] = child
            walk(child)

    walk(top)
    module = {symbol.get_name(): symbol for symbol in top.get_symbols()}
    scopes = [node for node in ast.walk(tree) if isinstance(node, _SCOPE_NODES)]
    for name in (node for node in ast.walk(tree) if isinstance(node, ast.Name)):
        where = (name.lineno, name.col_offset)
        owners = [s for s in scopes if _own_region(s)[0] <= where < _own_region(s)[1]]
        owner = max(owners, key=lambda s: _own_region(s)[0], default=None)
        if owner is not None and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            continue
        table = top if owner is None else tables[(getattr(owner, "name", "lambda"), owner.lineno)]
        symbol = table.lookup(name.id)
        imported = name.id in module and module[name.id].is_imported()
        if name.id in declared:
            yield name, "unsupported", owner
        elif symbol.is_imported() or (symbol.is_global() and imported):
            yield name, "unsupported", owner
        elif table is top or symbol.is_global():
            bound = name.id in module and module[name.id].is_assigned()
            yield name, "module" if bound else "not_bound", owner
        elif symbol.is_local():
            yield name, "local", owner
        else:
            assert symbol.is_free(), name.id
            yield name, "free", owner


@pytest.mark.parametrize("source", [ORACLE, SIMPLE, DEFAULTS, MIXED])
def test_cpython_symbol_table_agreement(tmp_path: Path, source: str) -> None:
    root, rows = _repo(tmp_path, {"m.py": source})
    checked = 0
    for name, expectation, owner in _symtable_cases(source):
        result = analyze_occurrences(root, rows[0], name.lineno, name.col_offset + 1)
        label = f"{name.id} at {name.lineno}:{name.col_offset + 1}"
        if expectation in ("unsupported", "not_bound"):
            assert result.status == expectation, label
        elif expectation == "module":
            assert (result.status, result.scope) == ("found", "module"), label
        else:
            kind = "lambda" if isinstance(owner, ast.Lambda) else "function"
            start = (owner.lineno, owner.col_offset + 1)
            end = (owner.end_lineno, owner.end_col_offset + 1)
            inside = [start <= (o.line, o.column) < end for o in result.occurrences]
            if expectation == "local":  # bound by this very def or lambda: every use is inside it
                assert (result.status, result.scope, all(inside)) == ("found", kind, True), label
            else:  # free here: bound by an enclosing function, so some occurrence lies outside
                assert result.status == "found" and result.scope in ("function", "lambda"), label
                assert not all(inside), label
        checked += 1
    assert checked >= 4


# --- Oracle 2: the frozen Step 45 implementation ---------------------------------------------


@contextmanager
def _binding_spy() -> Iterator[list[tuple[str, Any]]]:
    """Records every lookup the frozen Step 45 resolver makes; frozen code is unmodified."""
    calls: list[tuple[str, Any]] = []
    real = code_definition.resolving_scope

    def spy(scope: Any, name: str) -> Any:
        found = real(scope, name)
        calls.append((name, found))
        return found

    code_definition.resolving_scope = spy  # type: ignore[assignment]
    try:
        yield calls
    finally:
        code_definition.resolving_scope = real  # type: ignore[assignment]


def _column(source: str, node: Any) -> int:
    """1-based code-point column of an AST node (whose col_offset counts UTF-8 bytes)."""
    text = source.split("\n")[node.lineno - 1]
    return len(text.encode("utf-8")[: node.col_offset].decode("utf-8")) + 1


@pytest.mark.parametrize("source", [ORACLE, SIMPLE, DEFINITIONS, DEFAULTS])
def test_step_45_boundness_parity(tmp_path: Path, source: str) -> None:
    """Every Name is not_bound here exactly when Step 45's own resolver finds no binding, and
    every occurrence of a parameter is a variable-like binding Step 45 never resolves."""
    root, rows = _repo(tmp_path, {"m.py": source})
    for name in (node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)):
        line, column = name.lineno, _column(source, name)
        with _binding_spy() as calls:
            step45 = analyze_definition(root, rows[0], rows, line, column)
        bound = next(found for looked_up, found in calls if looked_up == name.id) is not None
        result = analyze_occurrences(root, rows[0], line, column)
        assert (result.status != "not_bound") == bound, f"{name.id} at {line}:{column}"
        if any(o.kind == "parameter" for o in result.occurrences):
            assert step45.status == "unsupported", f"{name.id} at {line}:{column}"


@pytest.mark.parametrize("source", [DEFINITIONS, ORACLE])
def test_step_45_definition_parity(tmp_path: Path, source: str) -> None:
    """For every def/class, its reads here are exactly the loads Step 45 resolves to it."""
    root, rows = _repo(tmp_path, {"m.py": source})
    tree = ast.parse(source)
    loads = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)]
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        line_text = source.split("\n")[node.lineno - 1]
        match = re.search(rf"\b(?:def|class)\s+({re.escape(node.name)})\b", line_text)
        assert match is not None
        result = analyze_occurrences(root, rows[0], node.lineno, match.start(1) + 1)
        assert result.status == "found"
        ours = {(o.line, o.column) for o in result.occurrences if o.kind == "read"}
        resolved = set()
        for load in (n for n in loads if n.id == node.name):
            position = (load.lineno, _column(source, load))
            step45 = analyze_definition(root, rows[0], rows, *position)
            if any(d.path == "m.py" and d.line == node.lineno for d in step45.definitions):
                resolved.add(position)
        assert ours == resolved, node.name
        checked += 1
    assert checked >= 4


# --- API -----------------------------------------------------------------------------------


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
    "import os\n"  # 1
    "def total(items):\n"  # 2
    "    acc = 0\n"  # 3
    "    for item in items:\n"  # 4
    "        acc += item\n"  # 5
    "    return acc, os, len(items)\n"  # 6
)
FILES: dict[str, bytes] = {
    "app/mod.py": API_SOURCE.encode(),
    "app/bad.py": b"def (:\n",
    "docs/notes.md": b"acc = 1\n",
}
TARGET = {"path": "app/mod.py", "line": 6, "column": 12}  # the returned acc
API_RESULT = {
    "status": "found",
    "reference": {"path": "app/mod.py", "line": 6, "column": 12, "name": "acc"},
    "scope": "function",
    "occurrences": [
        {"line": 3, "column": 5, "kind": "write"},
        {"line": 5, "column": 9, "kind": "write"},
        {"line": 6, "column": 12, "kind": "read"},
    ],
    "summary": {"occurrences": 3, "reads": 1, "writes": 2, "deletes": 0, "bindings": 0},
    "truncated": False,
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
        f"/repositories/{repository_id or owner.repository_id}/files/occurrences"
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


def _non_found(status: str, reference: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reference": reference,
        "scope": None,
        "occurrences": [],
        "summary": ZERO,
        "truncated": False,
    }


@requires_db
async def test_occurrences_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200, response.text
    assert response.json() == API_RESULT


@requires_db
async def test_endpoint_statuses(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    body = (await _get(client, alice, {"path": "docs/notes.md", "line": 1, "column": 1})).json()
    assert body == _non_found("unsupported_language")
    assert read_spy == []  # a non-Python file is never read
    body = (await _get(client, alice, {"path": "app/bad.py", "line": 1, "column": 1})).json()
    assert body == _non_found("syntax_error")
    os_use = {"path": "app/mod.py", "line": 6, "column": 17, "name": "os"}
    body = (await _get(client, alice, {"path": "app/mod.py", "line": 6, "column": 17})).json()
    assert body == _non_found("unsupported", os_use)  # an import binding
    builtin = {"path": "app/mod.py", "line": 6, "column": 21, "name": "len"}
    body = (await _get(client, alice, {"path": "app/mod.py", "line": 6, "column": 21})).json()
    assert body == _non_found("not_bound", builtin)
    body = (await _get(client, alice, {"path": "app/mod.py", "line": 1, "column": 1})).json()
    assert body == _non_found("no_identifier")  # the import keyword


@requires_db
async def test_validation_and_unknown_paths(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    for params in (
        {"path": "app/mod.py", "line": 0, "column": 1},
        {"path": "app/mod.py", "line": 1, "column": 0},
        {"path": "app/mod.py", "line": "x", "column": 1},
        {"path": "app/mod.py", "column": 1},
        {"path": "app/mod.py", "line": 1},
        {"path": "", "line": 1, "column": 1},
        {"path": "a" * 4097, "line": 1, "column": 1},
        {"line": 1, "column": 1},
    ):
        assert (await _get(client, alice, params)).status_code == 422, params
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _get(client, alice, TARGET, **kwargs)).status_code == 422
    assert read_spy == []
    for path in ("missing.py", "../app/mod.py", "/app/mod.py", "APP/MOD.PY"):
        response = await _get(client, alice, {"path": path, "line": 1, "column": 1})
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
    real = code_occurrences.repository_storage_dir

    def swap_then_root(settings: Any, repository_id: Any) -> Path:
        root = real(settings, repository_id)
        (root / "app" / "mod.py").write_bytes(b"acc = 1  # new snapshot\n")
        return root

    monkeypatch.setattr(code_occurrences, "repository_storage_dir", swap_then_root)
    response = await _get(client, alice, TARGET)
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_one_statement_provides_the_snapshot(
    client: httpx.AsyncClient, db_session: AsyncSession, alice: Owner, read_spy: list[str]
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
    assert body["status"] == "found"
    assert len([s for s in statements if "FROM files" in s]) == 1
    assert read_spy == ["app/mod.py"]  # only the target file is read


@requires_db
async def test_no_source_internals_or_logged_content(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-58f1"
    files = dict(FILES)
    files["app/mod.py"] = (API_SOURCE + f"note = '{marker}'  # {marker}\n").encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "found"
    assert marker not in response.text and marker not in caplog.text
    for word in ("import", "return", "def ", "+="):
        assert word not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text and alice.project_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text
    body = response.json()
    assert set(body) == {"status", "reference", "scope", "occurrences", "summary", "truncated"}
    assert set(body["summary"]) == set(ZERO)
    for entry in body["occurrences"]:
        assert set(entry) == {"line", "column", "kind"}


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"b.py": b"def f(bobs):\n    return bobs\n"})
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    params: dict[str, Any] = {"path": "b.py", "line": 2, "column": 12}
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
    assert own["reference"]["name"] == "bobs" and len(own["occurrences"]) == 2


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
    bob = await _owner(client, "bob@example.com", {"b.py": b"def f(bobs):\n    return bobs\n"})
    limiter = _AlwaysLimited()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    params: dict[str, Any] = {"path": "b.py", "line": 2, "column": 12}
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
    assert limiter.hits == [(OCCURRENCES_LIMIT, alice.user_id)]
    assert read_spy == []  # limited before any snapshot read


@requires_db_and_redis
async def test_rate_limit_is_120_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert OCCURRENCES_LIMIT == Limit("occurrences-user", 120, 600)
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(settings)
    await pool.flushdb()
    app.dependency_overrides.pop(get_rate_limiter, None)  # use the real Step 17 limiter
    params = {"path": "docs/notes.md", "line": 1, "column": 1}
    try:
        for _ in range(120):
            assert (await _get(client, alice, params)).status_code == 200
        limited = await _get(client, alice, params)
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts; please try again later"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 600
        keys = [k.decode() if isinstance(k, bytes) else k for k in await pool.keys("*")]
        assert any(k.startswith("ratelimit:occurrences-user:") for k in keys)
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
    params = {"path": "docs/notes.md", "line": 1, "column": 1}
    for _ in range(122):  # beyond the limit
        assert (await _get(client, alice, params)).status_code == 200
