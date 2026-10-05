"""Python unused local variables tests (GET .../files/unused-variables?path=). Offline.

Analysis tests use a temporary storage directory and need no database. Expected entries are
written out by hand from the Step 60 rules (``at`` only turns "the nth occurrence of this text
in the fixture" into a 1-based line and code-point column). Three independent oracles cross-check
them: CPython's own ``symtable`` (test-only, on fixture text the tests authored) for every listed
variable, the frozen Step 58 ``analyze_occurrences`` for every assignment site in both directions,
and the frozen Step 58 ``_supported`` for the once-per-file declaration rule. API tests run against
the already-migrated throwaway test database (TEST_DATABASE_URL, "_test") inside rolled-back
transactions; the rate-limit test uses a disposable Redis (TEST_REDIS_URL). Junction tests are
Windows-only; symlink tests are skipped where symlinks cannot be created.
"""

import ast
import builtins
import hashlib
import logging
import os
import shutil
import socket
import subprocess
import symtable
import sys
import tokenize
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
from app.services import code_unused_variables, ingestion
from app.services.code_definition import collect_scopes
from app.services.code_occurrences import _supported, analyze_occurrences
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.code_unused_variables import (
    MAX_UNUSED_VARIABLES,
    UNUSED_VARIABLES_LIMIT,
    analyze_unused_variables,
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
ZERO = {"unused": 0, "distinct": 0, "unsupported": 0}


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


def unused(tmp_path: Path, source: str | bytes, path: str = "m.py") -> Any:
    root, rows = _repo(tmp_path, {path: source})
    return analyze_unused_variables(root, _row(rows, path))


def listed(result: Any) -> list[tuple[int, int, str]]:
    return [(v.line, v.column, v.name) for v in result.variables]


def summary(result: Any) -> tuple[int, int, int]:
    return result.summary.unused, result.summary.distinct, result.summary.unsupported


def at(source: str, needle: str, nth: int = 0) -> tuple[int, int]:
    """1-based (line, code-point column) of the ``nth`` occurrence of ``needle``."""
    index = -1
    for _ in range(nth + 1):
        index = source.index(needle, index + 1)
    line = source.count("\n", 0, index) + 1
    return line, index - (source.rfind("\n", 0, index) + 1) + 1


def entry(source: str, name: str, nth: int = 0) -> tuple[int, int, str]:
    return (*at(source, name, nth), name)


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_unused_variables._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_unused_variables, "_read_verified", spy)
    return reads


# --- A. Plain assignments and candidates (U2, U3) ----------------------------------------------

EXAMPLE = (
    "import json\n"  # 1
    "\n"  # 2
    "def build(rows):\n"  # 3
    "    total = 0\n"  # 4
    '    header = "id,name"\n'  # 5
    "    count: int = len(rows)\n"  # 6
    "    a, b = 1, 2\n"  # 7
    "    for row in rows:\n"  # 8
    "        total += row\n"  # 9
    "    _scratch = rows[:1]\n"  # 10
    "    return json.dumps(total)\n"  # 11
)


def test_the_approved_example(tmp_path: Path) -> None:
    result = unused(tmp_path, EXAMPLE)
    assert result.status == "parsed" and result.path == "m.py"
    assert listed(result) == [(5, 5, "header"), (6, 5, "count")]
    assert summary(result) == (2, 2, 0) and result.truncated is False


def test_a_plain_assignment_is_listed(tmp_path: Path) -> None:
    result = unused(tmp_path, "def f():\n    x = 1\n")
    assert listed(result) == [(2, 5, "x")] and summary(result) == (1, 1, 0)


def test_chained_assignments_list_each_target(tmp_path: Path) -> None:
    result = unused(tmp_path, "def f():\n    a = b = c = 0\n    return b\n")
    assert listed(result) == [(2, 5, "a"), (2, 13, "c")] and summary(result) == (2, 2, 0)


def test_annotated_assignments_with_a_value_are_listed(tmp_path: Path) -> None:
    result = unused(tmp_path, "def f():\n    count: int = 0\n    (paren): int = 1\n")
    assert listed(result) == [(2, 5, "count"), (3, 6, "paren")]


# Each name bound by another form (alone or next to a plain assignment) is removed entirely;
# ``keep`` proves the function was analyzed.
EXCLUDED_FORMS: dict[str, str] = {
    "annotation_without_value": "def f():\n    x: int\n    keep = 1\n",
    "augmented_assignment": "def f():\n    x = 0\n    x += 1\n    keep = 1\n",
    "tuple_unpacking": "def f():\n    a, b = 1, 2\n    (c, (d, e)) = 3, (4, 5)\n    keep = 1\n",
    "list_unpacking": "def f(values):\n    [a, b] = values\n    keep = 1\n",
    "starred_unpacking": "def f(values):\n    first, *rest = values\n    *others, = values\n"
    "    keep = 1\n",
    "for_target": "def f(values):\n    for x in values:\n        pass\n    keep = 1\n",
    "async_for_target": "async def f(values):\n    async for x in values:\n        pass\n"
    "    keep = 1\n",
    "with_target": "def f(p):\n    with open(p) as x:\n        pass\n    keep = 1\n",
    "async_with_target": "async def f(p):\n    async with p as x:\n        pass\n    keep = 1\n",
    "walrus": "def f(v):\n    if (x := v):\n        pass\n    keep = 1\n",
    "walrus_in_a_comprehension": "def f(values):\n    [(x := v) for v in values]\n    keep = 1\n",
    "except_name": "def f():\n    try:\n        pass\n    except ValueError as x:\n        pass\n"
    "    keep = 1\n",
    "match_captures": (
        "def f(v):\n"
        "    match v:\n"
        "        case [x]:\n"
        "            pass\n"
        "        case {'k': 1, **rest}:\n"
        "            pass\n"
        "        case [*items]:\n"
        "            pass\n"
        "        case str() as alias:\n"
        "            pass\n"
        "    keep = 1\n"
    ),
    "imports": "def f():\n    import os\n    from os import path\n    import os.path as osp\n"
    "    keep = 1\n",
    "parameters": "def f(x, /, y, *args, kw=1, **kwargs):\n    x = 1\n    keep = 1\n",
    "def_and_class": (
        "def f():\n"
        "    def helper():\n"
        "        pass\n"
        "    class Local:\n"
        "        pass\n"
        "    helper = 1\n"
        "    keep = 1\n"
    ),
    "global_declaration": "def f():\n    global x\n    x = 1\n    keep = 1\n",
    "nonlocal_declaration": (
        "def f():\n"
        "    x = 0\n"
        "    keep = 1\n"
        "    def inner():\n"
        "        nonlocal x\n"
        "        x = 1\n"
        "    return inner\n"
    ),
    "del": "def f():\n    x = 1\n    del x\n    keep = 1\n",
    "type_alias": "def f():\n    type X = int\n    keep = 1\n",
    "plain_and_for_target": "def f(values):\n    x = 0\n    for x in values:\n        pass\n"
    "    keep = 1\n",
    "plain_and_except_name": (
        "def f():\n"
        "    x = None\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as x:\n"
        "        pass\n"
        "    keep = 1\n"
    ),
}


@pytest.mark.parametrize("form", sorted(EXCLUDED_FORMS))
def test_names_with_another_binding_form_are_removed_entirely(tmp_path: Path, form: str) -> None:
    source = EXCLUDED_FORMS[form]
    result = unused(tmp_path, source)
    assert listed(result) == [entry(source, "keep")]
    assert summary(result) == (1, 1, 0)


# --- B. Reads (U4) -----------------------------------------------------------------------------

# Every ``x`` is read somewhere that resolves to f's binding; ``keep`` is never read.
READS: dict[str, str] = {
    "same_function": "def f():\n    x = 1\n    keep = 1\n    return x\n",
    "nested_function": "def f():\n    x = set()\n    keep = 1\n    def inner():\n"
    "        return x\n    return inner\n",
    "deeper_nesting": "def f():\n    x = 1\n    keep = 1\n    def g():\n        def h():\n"
    "            return x\n        return h\n    return g\n",
    "lambda": "def f():\n    x = 1\n    keep = 1\n    return lambda: x\n",
    "list_comprehension": "def f(values):\n    x = 1\n    keep = 1\n"
    "    return [v + x for v in values]\n",
    "generator_condition": "def f(values):\n    x = 1\n    keep = 1\n"
    "    return list(v for v in values if v > x)\n",
    "first_iterable": "def f():\n    x = [1]\n    keep = 1\n    return {v: v for v in x}\n",
    "nested_class_body": "def f():\n    x = 1\n    keep = 1\n    class Local:\n"
    "        attribute = x\n    return Local\n",
    "method_of_a_nested_class": "def f():\n    x = 1\n    keep = 1\n    class Local:\n"
    "        def method(self):\n            return x\n    return Local\n",
    "decorator": "def f(wrap):\n    x = wrap\n    keep = 1\n    @x\n    def g():\n"
    "        pass\n    return g\n",
    "default": "def f():\n    x = 1\n    keep = 1\n    def g(v=x):\n        return v\n"
    "    return g\n",
    "annotation": "def f():\n    x = int\n    keep = 1\n    def g(v: x) -> x:\n"
    "        return v\n    return g\n",
    "f_string": "def f():\n    x = 1\n    keep = 1\n    return f'{x!r:>{x}}'\n",
    "read_before_the_assignment": "def f():\n    print(x)\n    x = 1\n    keep = 1\n",
    "keyword_argument_value": "def f(g):\n    x = 1\n    keep = 1\n    return g(key=x)\n",
}


@pytest.mark.parametrize("case", sorted(READS))
def test_any_read_resolving_to_the_variable_keeps_it(tmp_path: Path, case: str) -> None:
    source = READS[case]
    result = unused(tmp_path, source)
    assert listed(result) == [entry(source, "keep")]
    assert summary(result) == (1, 1, 0)


# The same identifier elsewhere is not a read of f's binding.
NOT_READS: dict[str, tuple[str, list[tuple[int, int, str]]]] = {
    "nested_shadowing": (
        "def f():\n    x = 1\n    def g():\n        x = 2\n        return x\n    return g\n",
        [(2, 5, "x")],
    ),
    "class_attribute": (
        "def f():\n    x = 1\n    class Local:\n        x = 2\n        y = x\n    return Local\n",
        [(2, 5, "x")],
    ),
    "sibling_function": ("def f():\n    x = 1\ndef g():\n    x = 2\n    return x\n", [(2, 5, "x")]),
    "module_variable": ("x = 1\nprint(x)\ndef f():\n    x = 2\n", [(4, 5, "x")]),
    "attribute_name": ("def f(obj):\n    x = 1\n    return obj.x\n", [(2, 5, "x")]),
    "keyword_argument_name": ("def f(g):\n    x = 1\n    return g(x=2)\n", [(2, 5, "x")]),
    "string_and_comment": ("def f():\n    x = 1\n    return 'x'  # x\n", [(2, 5, "x")]),
    "pep_695_type_parameter": (
        "def f():\n    T = 1\n    def g[T](v: T) -> T:\n        return v\n    return g\n",
        [(2, 5, "T")],
    ),
}


@pytest.mark.parametrize("case", sorted(NOT_READS))
def test_the_same_identifier_elsewhere_is_not_a_read(tmp_path: Path, case: str) -> None:
    source, expected = NOT_READS[case]
    result = unused(tmp_path, source)
    assert listed(result) == expected
    assert summary(result) == (len(expected), len(expected), 0)


def test_flow_sensitive_dead_stores_are_not_listed(tmp_path: Path) -> None:
    result = unused(tmp_path, "def f():\n    x = 1\n    x = 2\n    return x\n")
    assert listed(result) == [] and summary(result) == (0, 0, 0)


# --- C. Exclusions and suppression (U5) --------------------------------------------------------


def test_leading_underscore_names_are_never_listed_or_counted(tmp_path: Path) -> None:
    for name in ("_", "_temp", "__temp", "_temp_value", "__dunder__"):
        result = unused(tmp_path, f"def f():\n    {name} = 1\n    keep = 1\n")
        assert (listed(result), summary(result)) == ([(3, 5, "keep")], (1, 1, 0)), name
        dynamic = unused(tmp_path, f"def f():\n    {name} = 1\n    return locals()\n")
        assert (listed(dynamic), summary(dynamic)) == ([], (0, 0, 0)), name


GLOBAL_ELSEWHERE = (
    "def f():\n"  # 1
    "    shared = 1\n"  # 2
    "    keep = 1\n"  # 3
    "def g():\n"  # 4
    "    global shared\n"  # 5
    "    shared = 2\n"  # 6
    "def dynamic():\n"  # 7
    "    shared = 3\n"  # 8
    "    return locals()\n"  # 9
)
NONLOCAL_ELSEWHERE = (
    "def f():\n"  # 1
    "    counter = 1\n"  # 2
    "    keep = 1\n"  # 3
    "def outer():\n"  # 4
    "    counter = 0\n"  # 5
    "    def inner():\n"  # 6
    "        nonlocal counter\n"  # 7
    "        counter = 1\n"  # 8
    "    return inner\n"  # 9
)


def test_a_global_declaration_anywhere_removes_the_name(tmp_path: Path) -> None:
    result = unused(tmp_path, GLOBAL_ELSEWHERE)
    assert listed(result) == [(3, 5, "keep")]
    assert summary(result) == (1, 1, 0)  # not counted, not even in the dynamic function


def test_a_nonlocal_declaration_anywhere_removes_the_name(tmp_path: Path) -> None:
    result = unused(tmp_path, NONLOCAL_ELSEWHERE)
    assert listed(result) == [(3, 5, "keep")] and summary(result) == (1, 1, 0)


@pytest.mark.parametrize(
    "call", ["locals()", "vars()", "eval('greeting')", "exec('print(greeting)')"]
)
def test_dynamic_functions_count_never_read_variables_as_unsupported(
    tmp_path: Path, call: str
) -> None:
    source = (
        "def f():\n"  # 1
        "    greeting = 'hi'\n"  # 2
        "    farewell = 'bye'\n"  # 3
        "    used = 1\n"  # 4
        "    print(used)\n"  # 5
        f"    return {call}\n"  # 6
        "def other():\n"  # 7
        "    keep = 1\n"  # 8
    )
    result = unused(tmp_path, source)
    assert listed(result) == [(8, 5, "keep")]
    assert summary(result) == (1, 1, 2)


NESTED_DYNAMIC: dict[str, str] = {
    "lambda": "def f():\n    unused = 1\n    return lambda: locals()\n",
    "nested_function": "def f():\n    unused = 1\n    def g():\n        return eval('1')\n"
    "    return g\n",
    "deeper_function": "def f():\n    unused = 1\n    def g():\n        def h():\n"
    "            exec('pass')\n        return h\n    return g\n",
    "class_body": "def f():\n    unused = 1\n    class Local:\n        snapshot = vars()\n"
    "    return Local\n",
    "comprehension": "def f(values):\n    unused = 1\n    return [locals() for v in values]\n",
    "nested_default": "def f():\n    unused = 1\n    def g(v=locals()):\n        return v\n"
    "    return g\n",
}


def test_dynamic_access_nested_inside_the_function_suppresses(tmp_path: Path) -> None:
    for case, source in NESTED_DYNAMIC.items():
        result = unused(tmp_path, source)
        assert (listed(result), summary(result)) == ([], (0, 0, 1)), case


DYNAMIC_ELSEWHERE = (
    "locals()\n"  # 1: module level
    "def f():\n"  # 2
    "    sibling_kept = 1\n"  # 3
    "def g():\n"  # 4
    "    return locals()\n"  # 5
    "def outer():\n"  # 6
    "    def inner():\n"  # 7
    "        inner_kept = 1\n"  # 8
    "    return vars(), inner\n"  # 9: outer is dynamic, inner is not
    "def h(v=locals()):\n"  # 10: a default is evaluated in the enclosing scope
    "    default_kept = 1\n"  # 11
    "def k(obj):\n"  # 12
    "    attribute_kept = obj.locals()\n"  # 13: an attribute, not a name
)


def test_dynamic_access_elsewhere_never_suppresses(tmp_path: Path) -> None:
    result = unused(tmp_path, DYNAMIC_ELSEWHERE)
    assert listed(result) == [
        (3, 5, "sibling_kept"),
        (8, 9, "inner_kept"),
        (11, 5, "default_kept"),
        (13, 5, "attribute_kept"),
    ]
    assert summary(result) == (4, 4, 0)


STRING_ANNOTATIONS: dict[str, str] = {
    "parameter": "def f():\n    Alias = int\n    def g(v: 'Alias'):\n        return v\n"
    "    return g\n",
    "return": "def f():\n    Alias = int\n    def g() -> 'Alias':\n        pass\n    return g\n",
    "varargs_and_kwargs": "def f():\n    Alias = int\n"
    "    def g(*args: 'Alias', **kwargs: 'Alias'):\n        return args, kwargs\n    return g\n",
    "inside_a_subscript": "def f(Optional):\n    Alias = int\n"
    "    def g(v: Optional['Alias']):\n        return v\n    return g\n",
    "inside_a_string_expression": "def f():\n    Alias = int\n"
    "    def g(v: 'dict[str, Alias]'):\n        return v\n    return g\n",
    "evaluated_in_a_nested_scope": "def f():\n    Alias = int\n    def g():\n"
    "        def h(v: 'Alias'):\n            return v\n        return h\n    return g\n",
    "annotated_assignment_in_a_nested_scope": "def f():\n    Alias = int\n    def g():\n"
    "        local: 'Alias' = 0\n        return local\n    return g\n",
    "class_body_annotation": "def f():\n    Alias = int\n    class Local:\n"
    "        field: 'Alias'\n    return Local\n",
    "nfkc": "def f():\n    Alias = int\n    def g(v: '\uff21lias'):\n        return v\n"
    "    return g\n",
    "identifier_characters": "def f():\n    l\u00b7l = int\n    def g(v: 'l\u00b7l'):\n"
    "        return v\n    return g\n",
}


def test_names_inside_string_annotations_are_unsupported(tmp_path: Path) -> None:
    for case, source in STRING_ANNOTATIONS.items():
        result = unused(tmp_path, source)
        assert (listed(result), summary(result)) == ([], (0, 0, 1)), case


def test_a_string_annotated_assignment_suppresses_the_named_variable(tmp_path: Path) -> None:
    result = unused(tmp_path, "def f():\n    Alias = int\n    value: 'Alias' = 1\n")
    assert listed(result) == [(3, 5, "value")] and summary(result) == (1, 1, 1)


ORDINARY_STRINGS = (
    "def g(v: 'Alias', w: 'Mine'):\n"  # 1: evaluated in the module, not in f or h
    "    return v, w\n"  # 2
    "def f():\n"  # 3
    "    '''Alias is documented here.'''\n"  # 4
    "    Alias = int\n"  # 5
    "    print('Alias', f'{0}Alias')\n"  # 6
    "    note: str = 'Alias'\n"  # 7
    "    return cast('Alias', note)\n"  # 8
    "def h(v: 'Mine'):\n"  # 9: h's own annotation is evaluated in the module too
    "    Mine = 1\n"  # 10
    "    return v\n"  # 11
)


def test_docstrings_and_other_strings_never_suppress(tmp_path: Path) -> None:
    result = unused(tmp_path, ORDINARY_STRINGS)
    assert listed(result) == [(5, 5, "Alias"), (10, 5, "Mine")]
    assert summary(result) == (2, 2, 0)


def test_only_never_read_candidates_are_counted_unsupported(tmp_path: Path) -> None:
    source = (
        "def f():\n"
        "    used = 1\n"
        "    print(used)\n"
        "    Used = int\n"
        "    def g(v: 'Used'):\n"
        "        return Used\n"
        "    _hidden = 1\n"
        "    for loop in range(3):\n"
        "        pass\n"
        "    return locals(), g\n"
        "def both():\n"
        "    Both = int\n"
        "    def p(v: 'Both'):\n"
        "        return v\n"
        "    return locals(), p\n"
    )
    result = unused(tmp_path, source)
    assert listed(result) == [] and summary(result) == (0, 0, 1)  # Both, once


# --- D. Entries, positions and limits (U6, U7) -------------------------------------------------


def test_the_approved_special_cases(tmp_path: Path) -> None:
    closure = "def outer():\n    seen = set()\n\n    def inner():\n        return seen\n"
    assert (listed(c := unused(tmp_path, closure)), summary(c)) == ([], (0, 0, 0))
    repeated = unused(tmp_path, "def f():\n    x = 1\n    x = 2\n")
    assert listed(repeated) == [(2, 5, "x"), (3, 5, "x")] and summary(repeated) == (2, 1, 0)
    dynamic = unused(tmp_path, "def f():\n    greeting = 'hi'\n    return locals()\n")
    assert listed(dynamic) == [] and summary(dynamic) == (0, 0, 1)
    for source in ("x = 1\n", "class A:\n    x = 1\n"):
        result = unused(tmp_path, source)
        assert (result.status, listed(result), summary(result)) == ("parsed", [], (0, 0, 0))


def test_every_assignment_of_a_listed_variable_is_an_entry(tmp_path: Path) -> None:
    source = (
        "def f(flag):\n"
        "    x = 1\n"
        "    if flag:\n"
        "        x = 2\n"
        "    else:\n"
        "        x: int = 3\n"
    )
    result = unused(tmp_path, source)
    assert listed(result) == [(2, 5, "x"), (4, 9, "x"), (6, 9, "x")]
    assert summary(result) == (3, 1, 0)


ORDERED = (
    "def first():\n"  # 1
    "    b = 1\n"  # 2
    "    def inner():\n"  # 3
    "        c = 2\n"  # 4
    "    a = 3\n"  # 5
    "    return inner\n"  # 6
    "class K:\n"  # 7
    "    def method(self):\n"  # 8
    "        d = 4\n"  # 9
    "async def later():\n"  # 10
    "    e = 5\n"  # 11
)


def test_entries_are_in_source_order_across_functions_methods_and_async(tmp_path: Path) -> None:
    result = unused(tmp_path, ORDERED)
    assert listed(result) == [(2, 5, "b"), (4, 9, "c"), (5, 5, "a"), (9, 9, "d"), (11, 5, "e")]
    assert summary(result) == (5, 5, 0)


def test_distinct_counts_variables_not_names(tmp_path: Path) -> None:
    result = unused(tmp_path, "def f():\n    tmp = 1\ndef g():\n    tmp = 2\n    tmp = 3\n")
    assert listed(result) == [(2, 5, "tmp"), (4, 5, "tmp"), (5, 5, "tmp")]
    assert summary(result) == (3, 2, 0)


def test_columns_count_code_points_and_names_are_nfkc(tmp_path: Path) -> None:
    source = (
        "def f():\n    s = '\u00e9'; v\u00e5lue = 1\n    \uff56\uff41\uff4c = 2\n    return s\n"
    )
    assert listed(unused(tmp_path, source.encode())) == [(2, 14, "v\u00e5lue"), (3, 5, "val")]


def test_tabs_line_endings_and_bom(tmp_path: Path) -> None:
    assert listed(unused(tmp_path, "def g():\n\ttabbed = 1\n")) == [(2, 2, "tabbed")]
    for source in (
        b"def f():\r\n    x = 1\r\n",
        b"def f():\r    x = 1\r",
        b"\xef\xbb\xbfdef f():\n    x = 1\n",
    ):
        assert listed(unused(tmp_path, source)) == [(2, 5, "x")], source


def test_long_names_are_capped_after_normalization(tmp_path: Path) -> None:
    for name in ("n" * 200, "n" * 201, "n" * 300, "\uff4e" * 201):
        result = unused(tmp_path, f"def f():\n    {name} = 1\n".encode())
        assert listed(result) == [(2, 5, "n" * 200)] and result.truncated is False


def _many(count: int, distinct: bool = True) -> str:
    return "def f():\n" + "".join(f"    v{i if distinct else ''} = {i}\n" for i in range(count))


def test_exactly_the_cap_is_not_truncated(tmp_path: Path) -> None:
    assert MAX_UNUSED_VARIABLES == 500
    result = unused(tmp_path, _many(500))
    assert (len(result.variables), result.truncated) == (500, False)
    assert summary(result) == (500, 500, 0)
    assert listed(result)[0] == (2, 5, "v0") and listed(result)[-1] == (501, 5, "v499")


def test_one_over_the_cap_is_truncated_and_counted_before_the_cap(tmp_path: Path) -> None:
    result = unused(tmp_path, _many(501))
    assert (len(result.variables), result.truncated) == (500, True)
    assert listed(result)[-1] == (501, 5, "v499")
    assert summary(result) == (501, 501, 0)
    same = unused(tmp_path, _many(501, distinct=False))
    assert (len(same.variables), same.truncated, summary(same)) == (500, True, (501, 1, 0))


def test_module_and_class_scopes_are_never_examined(tmp_path: Path) -> None:
    source = (
        "x = 1\n"
        "y: int = 2\n"
        "class A:\n"
        "    z = 1\n"
        "    w: int = 2\n"
        "    def m(self):\n"
        "        return self\n"
    )
    result = unused(tmp_path, source)
    assert (result.status, listed(result), summary(result)) == ("parsed", [], (0, 0, 0))


def test_lambda_and_comprehension_scopes_are_never_examined(tmp_path: Path) -> None:
    source = (
        "f = lambda: (w := 1)\n"
        "def h():\n"
        "    return lambda v=1: (z := v), [j for j in range(3)]\n"
    )
    assert summary(unused(tmp_path, source)) == (0, 0, 0)


# --- E. Statuses, snapshot and safety (offline) ------------------------------------------------


def _not_parsed(result: Any, status: str) -> None:
    assert result.model_dump() == {
        "path": "m.py",
        "status": status,
        "variables": [],
        "summary": ZERO,
        "truncated": False,
    }


def test_empty_file(tmp_path: Path) -> None:
    result = unused(tmp_path, "")
    assert (result.status, result.variables, result.summary.model_dump()) == ("parsed", [], ZERO)


def test_syntax_error(tmp_path: Path) -> None:
    _not_parsed(unused(tmp_path, "def f(:\n    x = 1\n"), "syntax_error")
    _not_parsed(unused(tmp_path, "def f():\n    x = '''\n"), "syntax_error")


def test_tokenize_failure_is_a_syntax_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(source: str) -> Any:
        raise tokenize.TokenError("simulated", (1, 0))

    monkeypatch.setattr(code_unused_variables, "name_tokens", failing)
    _not_parsed(unused(tmp_path, "def f():\n    x = 1\n"), "syntax_error")


def test_binary(tmp_path: Path) -> None:
    _not_parsed(unused(tmp_path, b"\x00\x01def f():\n    x = 1\n"), "binary")
    _not_parsed(unused(tmp_path, b"\xff\xfe"), "binary")


def test_decoded_size_boundary(tmp_path: Path) -> None:
    body = b"def f():\n    x = 1\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    assert len(exactly) == MAX_OUTLINE_BYTES
    assert listed(unused(tmp_path, exactly)) == [(2, 5, "x")]
    _not_parsed(unused(tmp_path, exactly + b"#"), "too_large")  # never parsed


def test_stored_size_limit_is_checked_before_opening(tmp_path: Path, read_spy: list[str]) -> None:
    result = unused(tmp_path, os.urandom(MAX_CONTENT_BYTES))
    assert result.status in ("binary", "too_large") and read_spy == ["m.py"]  # exactly 1 MiB opens
    read_spy.clear()
    _not_parsed(unused(tmp_path, os.urandom(MAX_CONTENT_BYTES) + b"x"), "too_large")
    assert read_spy == []  # over 1 MiB is never opened


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "def f():\n    x = 1\n"})
    (root / "m.py").write_bytes(b"def f():\n    x = 1  # changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_unused_variables(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "m.py").unlink()  # missing
    with pytest.raises(FileContentError) as raised:
        analyze_unused_variables(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_symlinked_file_fails_the_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "def f():\n    x = 1\n"})
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"def f():\n    x = 1\n")
    (root / "m.py").unlink()
    try:
        os.symlink(outside, root / "m.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    with pytest.raises(FileContentError) as raised:
        analyze_unused_variables(root, rows[0])
    assert raised.value.status_code == 409


def test_junctioned_package_fails_the_request(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    root, rows = _repo(tmp_path, {"pkg/m.py": "def f():\n    x = 1\n"})
    outside = tmp_path / "outside"
    shutil.copytree(root / "pkg", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pkg")
    _winapi.CreateJunction(str(outside), str(root / "pkg"))  # type: ignore[attr-defined]
    with pytest.raises(FileContentError) as raised:
        analyze_unused_variables(root, rows[0])
    assert raised.value.status_code == 409


def test_nothing_is_cached_across_requests(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "def f():\n    x = 1\n"})
    assert listed(analyze_unused_variables(root, rows[0])) == [(2, 5, "x")]
    changed = b"def f():\n    x = 1\n    return x\n"
    (root / "m.py").write_bytes(changed)
    row = SimpleNamespace(
        path="m.py", size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest()
    )
    assert analyze_unused_variables(root, row).variables == []


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"  # 1
        "import os, subprocess, socket\n"  # 2
        "os.system('echo')\nsubprocess.run(['echo'])\n"  # 3, 4
        "socket.create_connection(('example.invalid', 80))\n"  # 5
        "def deco(f):\n    open('side-effect', 'w')\n    return f\n"  # 6-8
        "@deco\ndef target():\n    leftover = os.getcwd()\n"  # 9-11
        "def runner():\n    eval('1')\n    exec('made = 1')\n"  # 12-14
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
    result = analyze_unused_variables(root, rows[0])
    monkeypatch.undo()
    assert listed(result) == [(11, 5, "leftover")] and summary(result) == (1, 1, 0)
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


def test_responses_carry_no_source_or_internals(tmp_path: Path) -> None:
    marker = "UNIQUE-CONTENT-MARKER-60a7"
    source = EXAMPLE + f"note = '{marker}'  # {marker}\n"
    result = unused(tmp_path, source)
    text = result.model_dump_json()
    assert marker not in text
    for word in ("def ", "return", "import", "json", "len("):
        assert word not in text
    assert hashlib.sha256(source.encode()).hexdigest() not in text
    body = result.model_dump()
    assert set(body) == {"path", "status", "variables", "summary", "truncated"}
    assert set(body["summary"]) == set(ZERO)
    for item in body["variables"]:
        assert set(item) == {"line", "column", "name"}


def test_rate_limit_constant() -> None:
    assert UNUSED_VARIABLES_LIMIT == Limit("unused-variables-user", 120, 600)


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
    root, rows = _repo(tmp_path, {"m.py": EXAMPLE, "notes.md": "header = 1\n"})
    monkeypatch.setattr(code_unused_variables, "repository_storage_dir", lambda settings, rid: root)
    return root, rows


async def _request(db: _Db, path: str) -> Any:
    session: Any = db  # the stand-in session
    repository: Any = SimpleNamespace(id=uuid.uuid4())
    return await code_unused_variables.code_unused_variables(session, repository, path)


async def test_request_reads_one_row_and_analyzes_the_stored_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    _, rows = storage
    db = _Db(rows[0])
    result = await _request(db, "m.py")
    assert listed(result) == [(5, 5, "header"), (6, 5, "count")]
    assert len(db.statements) == 1 and read_spy == ["m.py"]
    sql = str(db.statements[0])
    assert "FROM files" in sql and "files.sha256" in sql and "files.path" in sql


async def test_request_never_reads_a_non_python_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    row = SimpleNamespace(
        path="notes.md", language="Markdown", extension="md", size_bytes=11, sha256="x"
    )
    result = await _request(_Db(row), "notes.md")
    assert result.model_dump() == {
        "path": "notes.md",
        "status": "unsupported_language",
        "variables": [],
        "summary": ZERO,
        "truncated": False,
    }
    assert read_spy == []


async def test_request_for_an_unknown_file_is_404(read_spy: list[str]) -> None:
    with pytest.raises(FileContentError) as raised:
        await _request(_Db(None), "missing.py")
    assert (raised.value.status_code, raised.value.detail) == (404, FILE_NOT_FOUND["detail"])
    assert read_spy == []


# --- F. Oracles --------------------------------------------------------------------------------

ORACLE = (
    "import os\n"  # 1
    "limit = 10\n"  # 2
    "\n"  # 3
    "def outer(a, b=limit, *rest, key=None, **extra):\n"  # 4
    "    plain = 1\n"  # 5
    "    chained = again = 2\n"  # 6
    "    annotated: int = 3\n"  # 7
    "    unpack_a, unpack_b = 4, 5\n"  # 8
    "    for loop_var in rest:\n"  # 9
    "        pass\n"  # 10
    "    with open(a) as handle:\n"  # 11
    "        pass\n"  # 12
    "    total = 0\n"  # 13
    "    total += 1\n"  # 14
    "    captured = 6\n"  # 15
    "    shadowed = 7\n"  # 16
    "    def inner(value=captured):\n"  # 17
    "        shadowed = value\n"  # 18
    "        return shadowed + used_later\n"  # 19
    "    used_later = 8\n"  # 20
    "    class Local:\n"  # 21
    "        attribute = again\n"  # 22
    "        def method(self):\n"  # 23
    "            method_local = 9\n"  # 24
    "            return self\n"  # 25
    "    lam = lambda: plain\n"  # 26
    "    comp = [item for item in rest if item > annotated]\n"  # 27
    "    try:\n"  # 28
    "        pass\n"  # 29
    "    except ValueError as error:\n"  # 30
    "        pass\n"  # 31
    "    del unpack_a\n"  # 32
    "    return inner, Local, os, key, extra, b\n"  # 33
    "\n"  # 34
    "def other():\n"  # 35
    "    lonely = 1\n"  # 36
    "    other_unused = lonely\n"  # 37
)


def test_the_oracle_fixture(tmp_path: Path) -> None:
    result = unused(tmp_path, ORACLE)
    assert listed(result) == [
        (6, 5, "chained"),
        (16, 5, "shadowed"),
        (24, 13, "method_local"),
        (26, 5, "lam"),
        (27, 5, "comp"),
        (37, 5, "other_unused"),
    ]
    assert summary(result) == (6, 6, 0)


def _function_tables(source: str) -> dict[tuple[str, int], Any]:
    tables: dict[tuple[str, int], Any] = {}

    def walk(table: Any) -> None:
        for child in table.get_children():
            if child.get_type() == "function":
                tables[(child.get_name(), child.get_lineno())] = child
            walk(child)

    walk(symtable.symtable(source, "<fixture>", "exec"))
    return tables


def _free_in_a_child(table: Any, name: str) -> bool:
    for child in table.get_children():
        if name in child.get_identifiers() and child.lookup(name).is_free():
            return True
        if _free_in_a_child(child, name):
            return True
    return False


@pytest.mark.parametrize("source", [ORACLE, EXAMPLE, ORDERED, GLOBAL_ELSEWHERE])
def test_cpython_symbol_table_agrees_with_every_listed_variable(
    tmp_path: Path, source: str
) -> None:
    """Every listed variable is, for CPython, a local of its function that is assigned, never
    referenced there and never free in a nested scope (ASCII fixtures: columns are bytes)."""
    functions = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    tables = _function_tables(source)
    result = unused(tmp_path, source)
    assert result.variables
    for variable in result.variables:
        where = (variable.line, variable.column - 1)
        owner = max(
            (
                f
                for f in functions
                if (f.lineno, f.col_offset) <= where <= (f.end_lineno, f.end_col_offset)
            ),
            key=lambda f: (f.lineno, f.col_offset),
        )
        table = tables[(owner.name, owner.lineno)]
        symbol = table.lookup(variable.name)
        assert symbol.is_local() and symbol.is_assigned(), variable
        assert not (symbol.is_referenced() or symbol.is_parameter()), variable
        assert not (symbol.is_imported() or symbol.is_global() or symbol.is_nonlocal()), variable
        assert not _free_in_a_child(table, variable.name), variable


def _plain_positions(source: str) -> set[tuple[int, int]]:
    """U2 written from the contract (test-only): 1-based positions of whole ``=`` targets and of
    annotated-assignment targets with a value (ASCII fixtures: columns are bytes)."""
    found: set[tuple[int, int]] = set()
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr] = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        found |= {(t.lineno, t.col_offset + 1) for t in targets if isinstance(t, ast.Name)}
    return found


PARITY: dict[str, str] = {
    "oracle": ORACLE,
    "example": EXAMPLE,
    "ordered": ORDERED,
    "excluded_forms": "".join(
        source
        for form, source in sorted(EXCLUDED_FORMS.items())
        if form not in ("global_declaration", "nonlocal_declaration")
    ),
    "reads": "".join(source for _, source in sorted(READS.items())),
    "not_reads": "".join(source for _, (source, _) in sorted(NOT_READS.items())),
    "declarations": GLOBAL_ELSEWHERE.replace("    return locals()\n", "") + NONLOCAL_ELSEWHERE,
}


@pytest.mark.parametrize("fixture", sorted(PARITY))
def test_step_58_occurrence_parity(tmp_path: Path, fixture: str) -> None:
    """Both directions, site by site: an assignment is listed exactly when the frozen Step 58
    model finds its binding in a function, with only write occurrences, all of them plain, and a
    name without a leading underscore (the fixtures have no dynamic names or string annotations).
    """
    source = PARITY[fixture]
    root, rows = _repo(tmp_path, {"m.py": source})
    result = analyze_unused_variables(root, rows[0])
    plain = _plain_positions(source)
    expected: set[tuple[int, int, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)):
            continue
        found = analyze_occurrences(root, rows[0], node.lineno, node.col_offset + 1)
        if found.status != "found" or found.scope != "function" or node.id.startswith("_"):
            continue
        writes = {(o.line, o.column) for o in found.occurrences if o.kind == "write"}
        others = len(found.occurrences) - len(writes)
        if others == 0 and writes <= plain:
            expected |= {(line, column, node.id) for line, column in writes}
    assert set(listed(result)) == expected and len(listed(result)) == len(expected)
    for line, column, name in listed(result):
        found = analyze_occurrences(root, rows[0], line, column)
        assert (found.status, found.summary.reads, found.reference.name) == ("found", 0, name)
        assert (line, column, "write") in {(o.line, o.column, o.kind) for o in found.occurrences}


@pytest.mark.parametrize("source", [GLOBAL_ELSEWHERE, NONLOCAL_ELSEWHERE, ORACLE])
def test_the_declaration_rule_agrees_with_step_58_supported(tmp_path: Path, source: str) -> None:
    """U5b is evaluated once per file; for every candidate it must agree with the frozen
    ``_supported``, and an unsupported candidate is never listed."""
    tree = ast.parse(source)
    table = collect_scopes(tree)
    plain = _plain_positions(source)
    declared = {
        name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Global, ast.Nonlocal))
        for name in node.names
    }
    names = {name for _, _, name in listed(unused(tmp_path, source))}
    checked = 0
    for scope in {id(s): s for s in table.scope_of.values()}.values():
        if scope.kind != "function":
            continue
        for name, bindings in scope.bindings.items():
            if all(
                isinstance(b.node, ast.Name) and (b.node.lineno, b.node.col_offset + 1) in plain
                for b in bindings
            ):
                supported = _supported(scope, name, table)
                assert supported == (name not in declared), name
                assert supported or name not in names, name
                checked += 1
    assert checked >= 3


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
    "def build(rows):\n"  # 2
    "    total = 0\n"  # 3
    "    header = 'id'\n"  # 4
    "    for row in rows:\n"  # 5
    "        total += row\n"  # 6
    "    return total, os\n"  # 7
    "def render():\n"  # 8
    "    greeting = 'hi'\n"  # 9
    "    return locals()\n"  # 10
)
FILES: dict[str, bytes] = {
    "app/mod.py": API_SOURCE.encode(),
    "app/bad.py": b"def (:\n",
    "pkg/__init__.py": b"",
    "docs/notes.md": b"header = 1\n",
}
TARGET = {"path": "app/mod.py"}
API_RESULT = {
    "path": "app/mod.py",
    "status": "parsed",
    "variables": [{"line": 4, "column": 5, "name": "header"}],
    "summary": {"unused": 1, "distinct": 1, "unsupported": 1},
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
        f"/repositories/{repository_id or owner.repository_id}/files/unused-variables"
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
async def test_unused_variables_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
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
        "variables": [],
        "summary": ZERO,
        "truncated": False,
    }
    assert read_spy == []  # a non-Python file is never read
    body = (await _get(client, alice, {"path": "app/bad.py"})).json()
    assert body == {
        "path": "app/bad.py",
        "status": "syntax_error",
        "variables": [],
        "summary": ZERO,
        "truncated": False,
    }
    body = (await _get(client, alice, {"path": "pkg/__init__.py"})).json()
    assert (body["status"], body["variables"], body["summary"]) == ("parsed", [], ZERO)


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
    real = code_unused_variables.repository_storage_dir

    def swap_then_root(settings: Any, repository_id: Any) -> Path:
        root = real(settings, repository_id)
        (root / "app" / "mod.py").write_bytes(b"def f():\n    header = 1  # new snapshot\n")
        return root

    monkeypatch.setattr(code_unused_variables, "repository_storage_dir", swap_then_root)
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
    marker = "UNIQUE-CONTENT-MARKER-60b2"
    files = dict(FILES)
    files["app/mod.py"] = (API_SOURCE + f"note = '{marker}'  # {marker}\n").encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "parsed"
    assert marker not in response.text and marker not in caplog.text
    for word in ("import", "return", "locals", "def "):
        assert word not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text
    body = response.json()
    assert set(body) == {"path", "status", "variables", "summary", "truncated"}
    assert set(body["summary"]) == {"unused", "distinct", "unsupported"}
    for item in body["variables"]:
        assert set(item) == {"line", "column", "name"}


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"b.py": b"def f():\n    bobs_unused = 1\n"})
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
    assert [v["name"] for v in own["variables"]] == ["bobs_unused"]


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
    bob = await _owner(client, "bob@example.com", {"b.py": b"def f():\n    bobs_unused = 1\n"})
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
    assert limiter.hits == [(UNUSED_VARIABLES_LIMIT, alice.user_id)]
    assert read_spy == []  # limited before any snapshot read


@requires_db_and_redis
async def test_rate_limit_is_120_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert UNUSED_VARIABLES_LIMIT == Limit("unused-variables-user", 120, 600)
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
        assert any(k.startswith("ratelimit:unused-variables-user:") for k in keys)
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
