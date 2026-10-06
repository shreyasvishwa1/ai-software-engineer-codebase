"""Python replaced definitions tests (GET .../files/replaced-definitions?path=). Offline.

Analysis tests use a temporary storage directory and need no database. Expected entries are
written out by hand from the Step 66 rules. Two independent oracles cross-check them: CPython's own
``symtable`` (test-only, on fixture text the tests authored), whose symbol for every listed name is
bound to two or more definition namespaces in its scope, and an ``ast`` walk that knows nothing of
the service's scope machinery. API tests run against the already-migrated throwaway test database
(TEST_DATABASE_URL, "_test") inside rolled-back transactions; the rate-limit test uses a disposable
Redis (TEST_REDIS_URL). Junction tests are Windows-only; symlink tests are skipped where symlinks
cannot be created.
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
from app.services import code_replaced_definitions, ingestion
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.code_replaced_definitions import (
    MAX_REPLACED_DEFINITIONS,
    REPLACED_DEFINITIONS_LIMIT,
    analyze_replaced_definitions,
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
ZERO = {"replaced": 0, "distinct": 0, "unsupported": 0}
ENTRY_FIELDS = {"line", "column", "name", "kind", "replaced_line", "replaced_column"}


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


def replaced(tmp_path: Path, source: str | bytes, path: str = "m.py") -> Any:
    root, rows = _repo(tmp_path, {path: source})
    return analyze_replaced_definitions(root, _row(rows, path))


def listed(result: Any) -> list[tuple[int, int, str, str, int, int]]:
    return [
        (d.line, d.column, d.name, d.kind, d.replaced_line, d.replaced_column)
        for d in result.definitions
    ]


def summary(result: Any) -> tuple[int, int, int]:
    return result.summary.replaced, result.summary.distinct, result.summary.unsupported


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_replaced_definitions._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_replaced_definitions, "_read_verified", spy)
    return reads


# --- A. Pairs and scopes (D1, D2) ------------------------------------------------------------


def test_a_module_function_replaced_before_any_read(tmp_path: Path) -> None:
    source = (
        "def f():\n"  # 1
        "    return 1\n"  # 2
        "def f():\n"  # 3
        "    return 2\n"  # 4
        "f()\n"  # 5: reads the second
    )
    result = replaced(tmp_path, source)
    assert listed(result) == [(1, 5, "f", "function", 3, 5)]
    assert summary(result) == (1, 1, 0)


def test_a_duplicate_test_method_never_runs(tmp_path: Path) -> None:
    source = (
        "class TestThing:\n"  # 1
        "    def test_one(self):\n"  # 2
        "        assert 1\n"  # 3
        "    def test_two(self):\n"  # 4
        "        assert 2\n"  # 5
        "    def test_one(self):\n"  # 6
        "        assert 3\n"  # 7
    )
    assert listed(replaced(tmp_path, source)) == [(2, 9, "test_one", "method", 6, 9)]


def test_a_nested_function_replaced(tmp_path: Path) -> None:
    source = (
        "def outer():\n"  # 1
        "    def inner():\n"  # 2
        "        return 1\n"  # 3
        "    def inner():\n"  # 4
        "        return 2\n"  # 5
        "    return inner\n"  # 6: reads the second
    )
    assert listed(replaced(tmp_path, source)) == [(2, 9, "inner", "function", 4, 9)]


def test_a_class_replaced(tmp_path: Path) -> None:
    source = "class A:\n    x = 1\nclass A:\n    x = 2\n"
    assert listed(replaced(tmp_path, source)) == [(1, 7, "A", "class", 3, 7)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def f(): ...\nclass f: ...\n", (1, 5, "f", "function", 2, 7)),
        ("class f: ...\ndef f(): ...\n", (1, 7, "f", "class", 2, 5)),
        ("async def f(): ...\ndef f(): ...\n", (1, 11, "f", "function", 2, 5)),
        ("def f(): ...\nasync def f(): ...\n", (1, 5, "f", "function", 2, 11)),
        (
            "class C:\n    async def m(self): ...\n    def m(self): ...\n",
            (2, 15, "m", "method", 3, 9),
        ),
        ("class C:\n    class N: ...\n    class N: ...\n", (2, 11, "N", "class", 3, 11)),
    ],
)
def test_kinds_and_name_positions(tmp_path: Path, source: str, expected: tuple[Any, ...]) -> None:
    assert listed(replaced(tmp_path, source)) == [expected]


def test_a_chain_lists_every_replaced_link(tmp_path: Path) -> None:
    result = replaced(tmp_path, "def f(): ...\ndef f(): ...\ndef f(): ...\n")
    assert listed(result) == [(1, 5, "f", "function", 2, 5), (2, 5, "f", "function", 3, 5)]
    assert summary(result) == (2, 1, 0)


def test_the_replacement_is_the_first_later_definition_of_the_name(tmp_path: Path) -> None:
    source = (
        "def f(): ...\n"  # 1
        "x = 1\n"  # 2
        "def g(): ...\n"  # 3: another name
        "class h: ...\n"  # 4
        "def f(): ...\n"  # 5
    )
    assert listed(replaced(tmp_path, source)) == [(1, 5, "f", "function", 5, 5)]


@pytest.mark.parametrize(
    ("source", "first", "second"),
    [
        ("if c:\n    def f(): ...\n    def f(): ...\n", (2, 9), (3, 9)),
        ("if c:\n    pass\nelse:\n    def f(): ...\n    def f(): ...\n", (4, 9), (5, 9)),
        ("for x in y:\n    def f(): ...\n    def f(): ...\n", (2, 9), (3, 9)),
        ("for x in y:\n    pass\nelse:\n    def f(): ...\n    def f(): ...\n", (4, 9), (5, 9)),
        ("while c:\n    def f(): ...\n    def f(): ...\n", (2, 9), (3, 9)),
        ("with c:\n    def f(): ...\n    def f(): ...\n", (2, 9), (3, 9)),
        ("try:\n    def f(): ...\n    def f(): ...\nexcept E:\n    pass\n", (2, 9), (3, 9)),
        ("try:\n    pass\nexcept E:\n    def f(): ...\n    def f(): ...\n", (4, 9), (5, 9)),
        (
            "try:\n    pass\nexcept E:\n    pass\nelse:\n    def f(): ...\n    def f(): ...\n",
            (6, 9),
            (7, 9),
        ),
        ("try:\n    pass\nfinally:\n    def f(): ...\n    def f(): ...\n", (4, 9), (5, 9)),
        ("match v:\n    case 1:\n        def f(): ...\n        def f(): ...\n", (3, 13), (4, 13)),
    ],
)
def test_every_kind_of_statement_list(
    tmp_path: Path, source: str, first: tuple[int, int], second: tuple[int, int]
) -> None:
    assert listed(replaced(tmp_path, source)) == [(*first, "f", "function", *second)]


@pytest.mark.parametrize(
    "source",
    [
        "if c:\n    def f(): ...\nelse:\n    def f(): ...\n",
        "if a:\n    def f(): ...\nelif b:\n    def f(): ...\n",
        "try:\n    def f(): ...\nexcept E:\n    def f(): ...\n",
        "if c:\n    def f(): ...\ndef f(): ...\n",  # a nested list, then the outer one
        "def f(): ...\nif c:\n    def f(): ...\n",  # the outer list, then a nested one
        "def f(): ...\nclass C:\n    def f(self): ...\n",  # another scope altogether
    ],
)
def test_definitions_of_different_lists_are_never_paired(tmp_path: Path, source: str) -> None:
    result = replaced(tmp_path, source)
    assert (result.definitions, result.summary.model_dump()) == ([], ZERO)


@pytest.mark.parametrize(
    "between",
    [
        "f = 1",
        "f += 1",
        "f: int = 1",
        "import f",
        "from m import f",
        "for f in x:\n    pass",
        "with x as f:\n    pass",
        "del f",
        "(f := 1)",
        "try:\n    pass\nexcept E as f:\n    pass",
        "if c:\n    def f(): ...",
        "if c:\n    class f: ...",
    ],
)
def test_another_binding_between_skips_the_pair(tmp_path: Path, between: str) -> None:
    result = replaced(tmp_path, f"def f(): ...\n{between}\ndef f(): ...\n")
    assert (result.definitions, result.summary.model_dump()) == ([], ZERO)


@pytest.mark.parametrize(
    "source",
    [
        "def outer():\n    def f(): ...\n    return 1\n    def f(): ...\n",
        "def f(): ...\nraise SystemExit\ndef f(): ...\n",
        "for x in y:\n    def f(): ...\n    break\n    def f(): ...\n",
        "for x in y:\n    def f(): ...\n    continue\n    def f(): ...\n",
    ],
)
def test_a_terminator_between_skips_the_pair(tmp_path: Path, source: str) -> None:
    result = replaced(tmp_path, source)
    assert (result.definitions, result.summary.model_dump()) == ([], ZERO)


def test_only_a_terminator_of_the_same_list_skips_the_pair(tmp_path: Path) -> None:
    source = "def f(): ...\nif c:\n    raise X\ndef f(): ...\n"  # S2 still runs when c is false
    assert listed(replaced(tmp_path, source)) == [(1, 5, "f", "function", 4, 5)]


# --- B. Reads (D3) ---------------------------------------------------------------------------


@pytest.mark.parametrize("between", ["f()", "g = f", "print(f.__name__)", "items = [f]"])
def test_a_read_between_keeps_the_first(tmp_path: Path, between: str) -> None:
    result = replaced(tmp_path, f"def f(): ...\n{between}\ndef f(): ...\n")
    assert (result.definitions, result.summary.model_dump()) == ([], ZERO)


@pytest.mark.parametrize(
    "source",
    [
        "def g():\n    return f()\ndef f(): ...\ndef f(): ...\n",  # deferred, before the first
        "x = [f for _ in ()]\ndef f(): ...\ndef f(): ...\n",  # immediate, before the first
        "def f(): ...\ndef g():\n    return f\ndef f(): ...\n",  # in a function defined between
        "def f(): ...\nclass K:\n    h = f\ndef f(): ...\n",  # a class body between
    ],
)
def test_every_earlier_read_may_be_a_use(tmp_path: Path, source: str) -> None:
    result = replaced(tmp_path, source)
    assert (result.definitions, result.summary.model_dump()) == ([], ZERO)


@pytest.mark.parametrize(
    "replacement",
    [
        "@deco(f)\ndef f(): ...",  # its decorators
        "def f(x=f): ...",  # its defaults
        "def f(*, x=f): ...",  # its keyword-only defaults
        "def f(x: f): ...",  # its annotations
        "def f() -> f: ...",  # its return annotation
        "class f(f): ...",  # a class's bases
        "class f(metaclass=f): ...",  # a class's keywords
        "class f:\n    g = f",  # a class's body
        "class f:\n    def m(self):\n        return f",  # code in a class body counts too
    ],
)
def test_reads_in_what_the_replacement_evaluates_first(tmp_path: Path, replacement: str) -> None:
    result = replaced(tmp_path, f"def f(): ...\n{replacement}\n")
    assert (result.definitions, result.summary.model_dump()) == ([], ZERO)


def test_a_property_setter_reads_the_getter(tmp_path: Path) -> None:
    source = (
        "class C:\n"
        "    @property\n"
        "    def x(self): ...\n"
        "    @x.setter\n"
        "    def x(self, value): ...\n"
    )
    result = replaced(tmp_path, source)
    assert (result.definitions, result.summary.model_dump()) == ([], ZERO)  # used, not unsupported


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "def f(n):\n    return f(n - 1)\ndef f(n):\n    return n\n",
            (1, 5, "f", "function", 3, 5),
        ),
        (
            "class A:\n    def m(self):\n        return A\nclass A: ...\n",
            (1, 7, "A", "class", 4, 7),
        ),
        ("f = 0\ndef f(x=f): ...\ndef f(): ...\n", (2, 5, "f", "function", 3, 5)),
        ("class A:\n    y = A\nclass A: ...\n", (1, 7, "A", "class", 3, 7)),
    ],
)
def test_reads_inside_the_first_never_count(
    tmp_path: Path, source: str, expected: tuple[Any, ...]
) -> None:
    assert listed(replaced(tmp_path, source)) == [expected]


def test_reads_after_the_replacement_never_count(tmp_path: Path) -> None:
    source = (
        "def f(): ...\n"  # 1
        "def f():\n"  # 2
        "    return f\n"  # 3: inside the replacement's own body
        "f()\n"  # 4
        "print(f)\n"  # 5
    )
    assert listed(replaced(tmp_path, source)) == [(1, 5, "f", "function", 2, 5)]


@pytest.mark.parametrize(
    "between",
    [
        "def g():\n    f = 1\n    return f",  # another function's local
        "class C:\n    f = 1\n    y = f",  # a class attribute of that name
        "obj.f()\nself.f",  # attribute names
        "x = 'f'  # f",  # strings and comments
        "g(f=1)",  # keyword-argument names
        "def h(f):\n    return f",  # a parameter of that name
        "y = lambda f: f",  # a lambda parameter
        "z = [f for f in range(3)]",  # a comprehension variable
    ],
)
def test_loads_of_other_bindings_never_count(tmp_path: Path, between: str) -> None:
    source = f"def f(): ...\n{between}\ndef f(): ...\n"
    replacement_line = source.count("\n")
    assert listed(replaced(tmp_path, source)) == [(1, 5, "f", "function", replacement_line, 5)]


# --- C. Unsupported (D4) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "@deco\ndef f(): ...\ndef f(): ...\n",
        "@overload\ndef f(x: int): ...\ndef f(x): ...\n",
        "@app.route('/')\ndef index(): ...\n@app.route('/b')\ndef index(): ...\n",
        "class C:\n    @staticmethod\n    def m(): ...\n    def m(self): ...\n",
        "@dataclass\nclass A: ...\nclass A: ...\n",
    ],
)
def test_decorated_first_definitions_are_counted_never_listed(tmp_path: Path, source: str) -> None:
    result = replaced(tmp_path, source)
    assert (result.definitions, summary(result)) == ([], (0, 0, 1))


def test_a_decorated_replacement_keeps_the_first_listed(tmp_path: Path) -> None:
    source = "def f(): ...\n@deco\ndef f(): ...\n"
    assert listed(replaced(tmp_path, source)) == [(1, 5, "f", "function", 3, 5)]


@pytest.mark.parametrize(
    "first",
    [
        "class A(B): ...",
        "class A(metaclass=M): ...",
        "class A(object): ...",
        "class A(*bases): ...",
    ],
)
def test_classes_with_bases_or_keywords_are_counted(tmp_path: Path, first: str) -> None:
    result = replaced(tmp_path, f"{first}\nclass A: ...\n")
    assert (result.definitions, summary(result)) == ([], (0, 0, 1))


@pytest.mark.parametrize("name", ["globals", "locals", "vars", "eval", "exec"])
def test_dynamic_access_anywhere_counts_module_pairs(tmp_path: Path, name: str) -> None:
    source = f"def f(): ...\ndef f(): ...\ndef g():\n    return {name}\n"
    result = replaced(tmp_path, source)
    assert (result.definitions, summary(result)) == ([], (0, 0, 1))


@pytest.mark.parametrize("name", ["locals", "vars", "eval", "exec"])
def test_dynamic_access_inside_a_function_counts_its_pairs(tmp_path: Path, name: str) -> None:
    source = f"def outer():\n    def f(): ...\n    def f(): ...\n    return {name}()\n"
    result = replaced(tmp_path, source)
    assert (result.definitions, summary(result)) == ([], (0, 0, 1))


def test_dynamic_access_in_a_nested_scope_marks_the_enclosing_function(tmp_path: Path) -> None:
    source = (
        "def outer():\n"
        "    def f(): ...\n"
        "    def f(): ...\n"
        "    def inner():\n"
        "        return locals()\n"
    )
    result = replaced(tmp_path, source)
    assert (result.definitions, summary(result)) == ([], (0, 0, 1))


def test_dynamic_access_in_another_function_leaves_a_function_alone(tmp_path: Path) -> None:
    source = (
        "def a():\n"  # 1
        "    def f(): ...\n"  # 2
        "    def f(): ...\n"  # 3
        "def b():\n"  # 4
        "    return vars()\n"  # 5
    )
    assert listed(replaced(tmp_path, source)) == [(2, 9, "f", "function", 3, 9)]


def test_dynamic_access_in_a_class_body_counts_its_pairs(tmp_path: Path) -> None:
    source = "class C:\n    def m(self): ...\n    def m(self): ...\n    names = vars()\n"
    result = replaced(tmp_path, source)
    assert (result.definitions, summary(result)) == ([], (0, 0, 1))


def test_globals_counts_only_module_pairs(tmp_path: Path) -> None:
    source = (
        "def outer():\n"  # 1
        "    def f(): ...\n"  # 2
        "    def f(): ...\n"  # 3
        "def g(): ...\n"  # 4
        "def g(): ...\n"  # 5
        "print(globals())\n"  # 6
    )
    result = replaced(tmp_path, source)
    assert listed(result) == [(2, 9, "f", "function", 3, 9)]
    assert summary(result) == (1, 1, 1)


# --- D. Excluded names (D5) ------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "def g():\n    global f\ndef f(): ...\ndef f(): ...\n",
        "def outer():\n    def f(): ...\n    def f(): ...\n    def inner():\n        nonlocal f\n",
        "def f(): ...\ndef f(): ...\ndef g():\n    global f\n    f = 1\n",
    ],
)
def test_global_and_nonlocal_names_are_never_listed_or_counted(tmp_path: Path, source: str) -> None:
    result = replaced(tmp_path, source)
    assert (result.definitions, result.summary.model_dump()) == ([], ZERO)


def test_the_throwaway_name_is_never_listed_or_counted(tmp_path: Path) -> None:
    source = "def _(): ...\ndef _(): ...\n@register\ndef _(): ...\nclass _: ...\n"
    result = replaced(tmp_path, source)
    assert (result.definitions, result.summary.model_dump()) == ([], ZERO)
    assert listed(replaced(tmp_path, "def _p(): ...\ndef _p(): ...\n")) == [
        (1, 5, "_p", "function", 2, 5)
    ]


# --- E. Entries, positions and limits (D6) ---------------------------------------------------


def test_names_are_normalized_like_python(tmp_path: Path) -> None:
    source = "def \uff41(): ...\ndef a(): ...\n"  # a fullwidth "a" is "a" for Python (NFKC)
    assert listed(replaced(tmp_path, source)) == [(1, 5, "a", "function", 2, 5)]


def test_columns_count_code_points(tmp_path: Path) -> None:
    source = "class \u00c7:\n    def \u00f1(self): ...\n    def \u00f1(self): ...\n"
    assert listed(replaced(tmp_path, source)) == [(2, 9, "\u00f1", "method", 3, 9)]


def test_a_name_on_a_continuation_line(tmp_path: Path) -> None:
    source = "def \\\n  f(): ...\ndef f(): ...\n"
    assert listed(replaced(tmp_path, source)) == [(2, 3, "f", "function", 3, 5)]


def test_entries_are_in_source_order(tmp_path: Path) -> None:
    source = (
        "class C:\n"  # 1
        "    def m(self): ...\n"  # 2
        "    def m(self): ...\n"  # 3
        "def f(): ...\n"  # 4
        "def f(): ...\n"  # 5
        "if c:\n"  # 6
        "    def g(): ...\n"  # 7
        "    def g(): ...\n"  # 8
    )
    assert listed(replaced(tmp_path, source)) == [
        (2, 9, "m", "method", 3, 9),
        (4, 5, "f", "function", 5, 5),
        (7, 9, "g", "function", 8, 9),
    ]


def test_the_summary_counts_distinct_bindings(tmp_path: Path) -> None:
    source = (
        "def f(): ...\ndef f(): ...\ndef f(): ...\n"  # two entries, one binding
        "def g(): ...\ndef g(): ...\n"
        "class C:\n    def f(self): ...\n    def f(self): ...\n"  # another scope's f
        "@deco\ndef h(): ...\ndef h(): ...\n"  # unsupported
    )
    assert summary(replaced(tmp_path, source)) == (4, 3, 1)


def test_entries_are_capped_but_counted(tmp_path: Path) -> None:
    assert MAX_REPLACED_DEFINITIONS == 500  # the response contract: at most 500 entries
    exactly = "def f(): ...\n" * (MAX_REPLACED_DEFINITIONS + 1)  # 500 replaced links
    result = replaced(tmp_path, exactly)
    assert (len(result.definitions), result.truncated) == (MAX_REPLACED_DEFINITIONS, False)
    over = replaced(tmp_path, "def f(): ...\n" * (MAX_REPLACED_DEFINITIONS + 2))
    assert (len(over.definitions), over.truncated) == (MAX_REPLACED_DEFINITIONS, True)
    assert summary(over) == (MAX_REPLACED_DEFINITIONS + 1, 1, 0)
    assert over.definitions[-1].line == MAX_REPLACED_DEFINITIONS  # the first 500, in order


def test_long_names_are_capped(tmp_path: Path) -> None:
    name = "n" * 250
    entry = replaced(tmp_path, f"def {name}(): ...\ndef {name}(): ...\n").definitions[0]
    assert entry.name == "n" * 200


# --- F. Statuses, snapshot and safety (offline) ----------------------------------------------


def _not_parsed(result: Any, status: str) -> None:
    assert result.model_dump() == {
        "path": "m.py",
        "status": status,
        "definitions": [],
        "summary": ZERO,
        "truncated": False,
    }


def test_empty_file(tmp_path: Path) -> None:
    result = replaced(tmp_path, "")
    assert (result.status, result.definitions, result.summary.model_dump()) == ("parsed", [], ZERO)


def test_syntax_error(tmp_path: Path) -> None:
    _not_parsed(replaced(tmp_path, "def f(:\n"), "syntax_error")
    _not_parsed(replaced(tmp_path, "def f(): ...\nx = '''\n"), "syntax_error")


def test_tokenize_failure_is_a_syntax_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(source: str) -> Any:
        raise tokenize.TokenError("simulated", (1, 0))

    monkeypatch.setattr(code_replaced_definitions, "name_tokens", failing)
    _not_parsed(replaced(tmp_path, "def f(): ...\ndef f(): ...\n"), "syntax_error")


def test_binary(tmp_path: Path) -> None:
    _not_parsed(replaced(tmp_path, b"\x00\x01def f(): ...\n"), "binary")
    _not_parsed(replaced(tmp_path, b"\xff\xfe"), "binary")


def test_decoded_size_boundary(tmp_path: Path) -> None:
    body = b"def f(): ...\ndef f(): ...\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    assert len(exactly) == MAX_OUTLINE_BYTES
    assert listed(replaced(tmp_path, exactly)) == [(1, 5, "f", "function", 2, 5)]
    _not_parsed(replaced(tmp_path, exactly + b"#"), "too_large")  # never parsed


def test_stored_size_limit_is_checked_before_opening(tmp_path: Path, read_spy: list[str]) -> None:
    result = replaced(tmp_path, os.urandom(MAX_CONTENT_BYTES))
    assert result.status in ("binary", "too_large") and read_spy == ["m.py"]  # exactly 1 MiB opens
    read_spy.clear()
    _not_parsed(replaced(tmp_path, os.urandom(MAX_CONTENT_BYTES) + b"x"), "too_large")
    assert read_spy == []  # over 1 MiB is never opened


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "def f(): ...\ndef f(): ...\n"})
    (root / "m.py").write_bytes(b"def f(): ...\ndef f(): ...  # changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_replaced_definitions(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "m.py").unlink()  # missing
    with pytest.raises(FileContentError) as raised:
        analyze_replaced_definitions(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_symlinked_file_fails_the_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "def f(): ...\n"})
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"def f(): ...\n")
    (root / "m.py").unlink()
    try:
        os.symlink(outside, root / "m.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    with pytest.raises(FileContentError) as raised:
        analyze_replaced_definitions(root, rows[0])
    assert raised.value.status_code == 409


def test_junctioned_package_fails_the_request(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    root, rows = _repo(tmp_path, {"pkg/m.py": "def f(): ...\n"})
    outside = tmp_path / "outside"
    shutil.copytree(root / "pkg", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pkg")
    _winapi.CreateJunction(str(outside), str(root / "pkg"))  # type: ignore[attr-defined]
    with pytest.raises(FileContentError) as raised:
        analyze_replaced_definitions(root, rows[0])
    assert raised.value.status_code == 409


def test_nothing_is_cached_across_requests(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "def f(): ...\ndef f(): ...\n"})
    assert listed(analyze_replaced_definitions(root, rows[0])) == [(1, 5, "f", "function", 2, 5)]
    changed = b"def f(): ...\nf()\ndef f(): ...\n"
    (root / "m.py").write_bytes(changed)
    row = SimpleNamespace(
        path="m.py", size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest()
    )
    assert analyze_replaced_definitions(root, row).definitions == []


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"  # 1
        "import os, subprocess, socket\n"  # 2
        "os.system('echo')\nsubprocess.run(['echo'])\n"  # 3, 4
        "socket.create_connection(('example.invalid', 80))\n"  # 5
        "def target():\n    open('side-effect', 'w')\n"  # 6, 7
        "def target():\n    pass\n"  # 8, 9
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
    result = analyze_replaced_definitions(root, rows[0])
    monkeypatch.undo()
    assert listed(result) == [(6, 5, "target", "function", 8, 5)] and summary(result) == (1, 1, 0)
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


def test_responses_carry_no_source_or_internals(tmp_path: Path) -> None:
    marker = "UNIQUE-CONTENT-MARKER-66a1"
    source = (
        "def secret_helper():\n"
        f"    return '{marker}'  # {marker}\n"
        "def secret_helper():\n"
        "    return 2\n"
    )
    result = replaced(tmp_path, source)
    text = result.model_dump_json()
    assert marker not in text
    for word in ("def ", "return", "pass"):
        assert word not in text
    assert hashlib.sha256(source.encode()).hexdigest() not in text
    body = result.model_dump()
    assert set(body) == {"path", "status", "definitions", "summary", "truncated"}
    assert set(body["summary"]) == set(ZERO)
    for item in body["definitions"]:
        assert set(item) == ENTRY_FIELDS


def test_rate_limit_constant() -> None:
    assert REPLACED_DEFINITIONS_LIMIT == Limit("replaced-definitions-user", 120, 600)


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
    root, rows = _repo(tmp_path, {"m.py": ORACLE, "notes.md": "def f(): ...\n"})
    monkeypatch.setattr(
        code_replaced_definitions, "repository_storage_dir", lambda settings, rid: root
    )
    return root, rows


async def _request(db: _Db, path: str) -> Any:
    session: Any = db  # the stand-in session
    repository: Any = SimpleNamespace(id=uuid.uuid4())
    return await code_replaced_definitions.code_replaced_definitions(session, repository, path)


async def test_request_reads_one_row_and_analyzes_the_stored_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    _, rows = storage
    db = _Db(rows[0])
    result = await _request(db, "m.py")
    assert listed(result) == ORACLE_ENTRIES
    assert len(db.statements) == 1 and read_spy == ["m.py"]
    sql = str(db.statements[0])
    assert "FROM files" in sql and "files.sha256" in sql and "files.path" in sql


async def test_request_never_reads_a_non_python_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    row = SimpleNamespace(
        path="notes.md", language="Markdown", extension="md", size_bytes=13, sha256="x"
    )
    result = await _request(_Db(row), "notes.md")
    assert result.model_dump() == {
        "path": "notes.md",
        "status": "unsupported_language",
        "definitions": [],
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
    "import os\n"  # 1
    "\n"  # 2
    "def helper():\n"  # 3: replaced at 6, never read before
    "    return 1\n"  # 4
    "\n"  # 5
    "def helper():\n"  # 6
    "    return 2\n"  # 7
    "\n"  # 8
    "def used():\n"  # 9: read on line 11 before its replacement
    "    return 1\n"  # 10
    "first = used()\n"  # 11
    "def used():\n"  # 12
    "    return 2\n"  # 13
    "\n"  # 14
    "class TestCase:\n"  # 15
    "    def test_a(self):\n"  # 16: replaced at 20
    "        return os.sep\n"  # 17
    "    def test_b(self):\n"  # 18
    "        return 2\n"  # 19
    "    def test_a(self):\n"  # 20
    "        return 3\n"  # 21
    "\n"  # 22
    "if os.name == 'nt':\n"  # 23
    "    def pick(): ...\n"  # 24: another branch, never paired
    "else:\n"  # 25
    "    def pick(): ...\n"  # 26
    "\n"  # 27
    "def outer():\n"  # 28
    "    def inner(): ...\n"  # 29: replaced at 30
    "    def inner(): ...\n"  # 30
    "    return inner\n"  # 31
    "\n"  # 32
    "class Old: ...\n"  # 33: replaced at 34
    "class Old: ...\n"  # 34
)
ORACLE_ENTRIES = [
    (3, 5, "helper", "function", 6, 5),
    (16, 9, "test_a", "method", 20, 9),
    (29, 9, "inner", "function", 30, 9),
    (33, 7, "Old", "class", 34, 7),
]
CHAINS = "def f(): ...\ndef f(): ...\ndef f(): ...\nclass K:\n    def m(self): ...\n    def m(self): ...\n"


def test_the_oracle_fixture(tmp_path: Path) -> None:
    result = replaced(tmp_path, ORACLE)
    assert listed(result) == ORACLE_ENTRIES
    assert summary(result) == (4, 4, 0)


def _owner(tree: ast.Module, line: int) -> ast.AST:
    """The def or class whose body holds the definition statement starting on ``line`` (fixture
    definitions sit directly in their holder's body, or in a module-level block)."""
    holder: ast.AST = tree
    for node in ast.walk(tree):  # outer before inner: the last match is the innermost
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and any(
            isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and s.lineno == line
            for s in node.body
        ):
            holder = node
    return holder


@pytest.mark.parametrize("source", [ORACLE, CHAINS])
def test_cpython_symbol_table_binds_every_listed_name_to_several_definitions(
    tmp_path: Path, source: str
) -> None:
    """For every listed entry, CPython's symbol of that name in its scope is bound to two or more
    function or class namespaces: the scope really defines the name more than once."""
    top = symtable.symtable(source, "<fixture>", "exec")
    tables: dict[tuple[str, int], Any] = {}

    def walk(table: Any) -> None:
        for child in table.get_children():
            tables[(child.get_name(), child.get_lineno())] = child
            walk(child)

    walk(top)
    tree = ast.parse(source)
    result = replaced(tmp_path, source)
    assert result.definitions
    for entry in result.definitions:
        owner = _owner(tree, entry.line)
        table = top if isinstance(owner, ast.Module) else tables[(owner.name, owner.lineno)]
        symbol = table.lookup(entry.name)
        assert symbol.is_assigned() and symbol.is_namespace(), entry
        assert len(symbol.get_namespaces()) >= 2, entry


def _ast_oracle(source: str) -> list[tuple[int, str, int]]:
    """Test-only, independent of the service: (line, name, replacing line) for each def/class
    statement followed in its own statement list by a same-named one, with no Load of the name
    anywhere before the replacement outside the first definition, and no decorators or bases
    (fixtures without dynamic access, declarations, terminators or other bindings of the names)."""
    tree = ast.parse(source)
    loads = [
        (n.lineno, n.col_offset, n.id)
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    ]
    found = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)
            if not isinstance(statements, list):
                continue
            definitions = [
                s
                for s in statements
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ]
            for i, first in enumerate(definitions):
                second = next((d for d in definitions[i + 1 :] if d.name == first.name), None)
                if second is None or first.decorator_list:
                    continue
                if isinstance(first, ast.ClassDef) and (first.bases or first.keywords):
                    continue
                cut = (
                    (second.end_lineno, second.end_col_offset)
                    if isinstance(second, ast.ClassDef)
                    else (second.body[0].lineno, second.body[0].col_offset)
                )
                inside = (
                    (first.lineno, first.col_offset),
                    (first.end_lineno, first.end_col_offset),
                )
                if any(
                    name == first.name
                    and (line, column) < cut
                    and not inside[0] <= (line, column) <= inside[1]
                    for line, column, name in loads
                ):
                    continue
                found.append((first.lineno, first.name, second.lineno))
    return sorted(found)


@pytest.mark.parametrize("source", [ORACLE, CHAINS])
def test_an_independent_ast_walk_agrees(tmp_path: Path, source: str) -> None:
    result = replaced(tmp_path, source)
    assert [(d.line, d.name, d.replaced_line) for d in result.definitions] == _ast_oracle(source)


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
    "def helper():\n"  # 1: replaced at 3
    "    return 1\n"  # 2
    "def helper():\n"  # 3
    "    return 2\n"  # 4
    "class TestThing:\n"  # 5
    "    def test_a(self): ...\n"  # 6: replaced at 7
    "    def test_a(self): ...\n"  # 7
    "@register\n"  # 8
    "def hook(): ...\n"  # 9: decorated, unsupported
    "def hook(): ...\n"  # 10
)
FILES: dict[str, bytes] = {
    "app/mod.py": API_SOURCE.encode(),
    "app/bad.py": b"def (:\n",
    "app/clean.py": b"def f():\n    return 1\nf()\ndef f(): ...\n",
    "docs/notes.md": b"def f(): ...\ndef f(): ...\n",
}
TARGET = {"path": "app/mod.py"}
API_RESULT = {
    "path": "app/mod.py",
    "status": "parsed",
    "definitions": [
        {
            "line": 1,
            "column": 5,
            "name": "helper",
            "kind": "function",
            "replaced_line": 3,
            "replaced_column": 5,
        },
        {
            "line": 6,
            "column": 9,
            "name": "test_a",
            "kind": "method",
            "replaced_line": 7,
            "replaced_column": 9,
        },
    ],
    "summary": {"replaced": 2, "distinct": 2, "unsupported": 1},
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
        f"/repositories/{repository_id or owner.repository_id}/files/replaced-definitions"
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
async def test_replaced_definitions_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
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
        "definitions": [],
        "summary": ZERO,
        "truncated": False,
    }
    assert read_spy == []  # a non-Python file is never read
    body = (await _get(client, alice, {"path": "app/bad.py"})).json()
    assert body == {
        "path": "app/bad.py",
        "status": "syntax_error",
        "definitions": [],
        "summary": ZERO,
        "truncated": False,
    }
    body = (await _get(client, alice, {"path": "app/clean.py"})).json()
    assert (body["status"], body["definitions"], body["summary"]) == ("parsed", [], ZERO)


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
    real = code_replaced_definitions.repository_storage_dir

    def swap_then_root(settings: Any, repository_id: Any) -> Path:
        root = real(settings, repository_id)
        (root / "app" / "mod.py").write_bytes(b"def f(): ...  # new snapshot\n")
        return root

    monkeypatch.setattr(code_replaced_definitions, "repository_storage_dir", swap_then_root)
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
    marker = "UNIQUE-CONTENT-MARKER-66b2"
    files = dict(FILES)
    files["app/mod.py"] = (API_SOURCE + f"note = '{marker}'  # {marker}\n").encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "parsed"
    assert marker not in response.text and marker not in caplog.text
    for word in ("def ", "return", "register", "class "):
        assert word not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text
    body = response.json()
    assert set(body) == {"path", "status", "definitions", "summary", "truncated"}
    assert set(body["summary"]) == {"replaced", "distinct", "unsupported"}
    for item in body["definitions"]:
        assert set(item) == ENTRY_FIELDS


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _create_owner(
        client, "bob@example.com", {"b.py": b"def bobs(): ...\ndef bobs(): ...\n"}
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
    assert [d["name"] for d in own["definitions"]] == ["bobs"]


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
        client, "bob@example.com", {"b.py": b"def bobs(): ...\ndef bobs(): ...\n"}
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
    assert limiter.hits == [(REPLACED_DEFINITIONS_LIMIT, alice.user_id)]
    assert read_spy == []  # limited before any snapshot read


@requires_db_and_redis
async def test_rate_limit_is_120_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert REPLACED_DEFINITIONS_LIMIT == Limit("replaced-definitions-user", 120, 600)
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
        assert any(k.startswith("ratelimit:replaced-definitions-user:") for k in keys)
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
