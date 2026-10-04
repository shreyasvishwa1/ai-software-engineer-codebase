"""Python statically unbound names tests (GET .../files/unbound-names?path=). Offline.

Scanner tests use a temporary storage directory and need no database. The central oracle is an
occurrence-level parity check against the frozen Step 45 implementation: every Load
``ast.Name`` found by an independent walk is resolved with Step 45's own ``analyze_definition``
at its exact (line, column), while a test-only spy on the frozen ``resolving_scope`` records
Step 45's own binding decision for that occurrence; Step 55 must list exactly the occurrences
Step 45 finds unbound with ``not_found``, apart from the documented E1/E2 exclusions, compared
as (path, line, column, identifier) tuples. API tests run against the already-migrated
throwaway test database (TEST_DATABASE_URL, "_test") inside rolled-back transactions; the
rate-limit test uses a disposable Redis (TEST_REDIS_URL). Junction tests are Windows-only;
symlink tests are skipped where symlinks cannot be created.
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
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
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
from app.services import code_definition, code_unbound_names, ingestion
from app.services.code_definition import (
    BUILTIN_NAMES,
    _normalize,
    analyze_definition,
    collect_scopes,
)
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.code_unbound_names import (
    MAX_UNBOUND_NAMES,
    UNBOUND_NAMES_LIMIT,
    analyze_unbound_names,
)
from app.services.file_content import MAX_CONTENT_BYTES, FileContentError, _decode
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
ZERO = {"unbound": 0, "distinct": 0, "unsupported": 0, "implicit": 0}
# The approved E1 allowlist, written here from the contract (independently of the service).
ALLOWLIST = {
    "__file__",
    "__cached__",
    "__builtins__",
    "__annotations__",
    "__module__",
    "__qualname__",
    "__class__",
}


# --- Fixtures --------------------------------------------------------------------------------

BASIC = "\n".join(
    [
        "import os",  # 1
        "from typing import List as L",  # 2
        "value = missing_module_name",  # 3
        "result = undefined_function(value)",  # 4
        "def outer(a):",  # 5
        "    local = a + missing_in_function",  # 6
        "    def inner():",  # 7
        "        return local + missing_in_nested",  # 8: local is a closure binding
        "    return inner",  # 9
        "square = lambda q: q * missing_in_lambda",  # 10
        "doubled = [n * 2 for n in range(3) if n > missing_in_comprehension]",  # 11
        "text = f'{missing_in_fstring}'",  # 12
        "match os:",  # 13
        "    case [first, *rest]:",  # 14
        "        found = first + rest + missing_in_case",  # 15
        "print(os, L, value, result, outer, square, doubled, text, found)",  # 16
        "",
    ]
)
BASIC_LISTED = [
    (3, 9, "missing_module_name"),
    (4, 10, "undefined_function"),
    (6, 17, "missing_in_function"),
    (8, 24, "missing_in_nested"),
    (10, 24, "missing_in_lambda"),
    (11, 43, "missing_in_comprehension"),
    (12, 11, "missing_in_fstring"),
    (15, 32, "missing_in_case"),
]

SCOPES = "\n".join(
    [
        "class Config:",  # 1
        "    attr = 1",  # 2
        "    alias = attr",  # 3: a class body sees its own scope
        "    items = [attr for _ in range(2)]",  # 4: a comprehension element does not
        "    pairs = [x for x in attr]",  # 5: the outermost iterable is evaluated in the class
        "    def method(self):",  # 6
        "        return attr",  # 7: methods do not see the class scope
        "    def nested(self):",  # 8
        "        class Inner:",  # 9: a class nested in a method
        "            inner_attr = missing_in_inner",  # 10
        "        return Inner",  # 11
        "    handler = lambda self: attr",  # 12: a lambda in a class body does not either
        "",
    ]
)
SCOPES_LISTED = [(4, 14, "attr"), (7, 16, "attr"), (10, 26, "missing_in_inner"), (12, 28, "attr")]

DECLARATIONS = "\n".join(
    [
        "def uses_global():",  # 1
        "    global declared_only",  # 2
        "    return declared_only",  # 3: a declaration binds (Step 45)
        "def outer():",  # 4
        "    count = 0",  # 5
        "    def bump():",  # 6
        "        nonlocal count",  # 7
        "        count += 1",  # 8
        "        return count",  # 9
        "    return bump",  # 10
        "def broken():",  # 11
        "    return not_declared",  # 12
        "",
    ]
)

BINDINGS = "\n".join(
    [
        "import os.path",  # 1
        "import json as j",  # 2
        "from . import sibling",  # 3
        "from .pkg import thing as other",  # 4
        "from typing import TYPE_CHECKING",  # 5
        "import not_installed_anywhere",  # 6
        "if TYPE_CHECKING:",  # 7
        "    from collections import OrderedDict",  # 8
        "if os.environ:",  # 9
        "    conditional = 1",  # 10
        "try:",  # 11
        "    import ujson as fast",  # 12
        "except ImportError:",  # 13
        "    fast = None",  # 14
        "try:",  # 15
        "    from speedups import accelerate",  # 16
        "except ImportError:",  # 17
        "    pass",  # 18
        "early = later_defined",  # 19: used before its assignment
        "later_defined = 1",  # 20
        "removed = 1",  # 21
        "del removed",  # 22
        "print(os, j, sibling, other, OrderedDict, not_installed_anywhere, conditional, fast,",  # 23
        "      accelerate, removed, early)",  # 24
        "def local_before_assignment():",  # 25
        "    print(local_value)",  # 26
        "    local_value = 1",  # 27
        "",
    ]
)

DYNAMIC = "exec('made_by_exec = 1')\nglobals()['made_by_globals'] = 2\nprint(made_by_exec, made_by_globals)\n"

NEVER = "\n".join(
    [
        "import os",  # 1
        "def defined(param, *args, key=None, **kw):",  # 2
        "    stored = param",  # 3
        "    del stored",  # 4
        "    return os.nowhere(keyword_name=args)",  # 5: an attribute and a keyword argument
        "class Defined:",  # 6
        "    text = 'quoted_name'  # commented_name",  # 7
        "for loop_target in range(2):",  # 8
        "    pass",  # 9
        "with open(os.devnull) as handle:",  # 10
        "    pass",  # 11
        "try:",  # 12
        "    pass",  # 13
        "except Exception as caught:",  # 14
        "    pass",  # 15
        "(walrus := 1)",  # 16
        "global_list = [item for item in range(walrus)]",  # 17
        "class Walrus:",  # 18
        "    xs = [(inside := 1) for _ in range(2)]",  # 19: a store Step 45 leaves unbound
        "",
    ]
)

BUILTIN_SOURCE = (
    "def f(items):\n"
    "    return (len(items), sorted(items), print, __name__, __doc__, __package__, __spec__,\n"
    "            __loader__, __debug__, __import__, __build_class__)\n"
)

CLASS_CELL = "\n".join(
    [
        "class C:",  # 1
        "    direct = __class__",  # 2: listed
        "    xs = [__class__ for _ in range(1)]",  # 3: listed (comprehensions are not functions)
        "    f = lambda self: __class__",  # 4: implicit
        "    def m(self):",  # 5
        "        def g():",  # 6
        "            return __class__",  # 7: implicit
        "        class Inner:",  # 8
        "            seen = __class__",  # 9: implicit (a class nested in a method)
        "        return __class__, [__class__ for _ in range(1)]",  # 10: implicit twice
        "def plain():",  # 11
        "    return __class__",  # 12: listed
        "top = __class__",  # 13: listed
        "",
    ]
)

ARBITRARY = "print(__my_missing_variable__, __version__, __all__, __private_name)\n__bound_dunder__ = 1\n_ = __bound_dunder__\n"
PRIVATE = "__private_name = 1\nprint(__private_name)\nclass K:\n    def m(self):\n        return __private_name\n"

PEP695 = "\n".join(
    [
        "def deco(arg):",  # 1
        "    return lambda f: f",  # 2
        "@deco(T)",  # 3: a decorator lies outside the generic statement
        "def generic[T](value: T, *, default=T) -> T:",  # 4
        "    inner: T = value",  # 5
        "    return inner",  # 6
        "class Box[U](list[U]):",  # 7
        "    item: U",  # 8
        "    def get(self) -> U:",  # 9
        "        return self.item",  # 10
        "type Pair[K, V] = tuple[K, V, missing_alias]",  # 11
        "def bounded[B: MissingBound](x: B) -> B:",  # 12: the parameter list is never visited
        "    return x",  # 13
        "async def agen[A](x: A) -> A:",  # 14
        "    return x",  # 15
        "outside = (T, U, K, A)",  # 16
        "",
    ]
)
PEP695_LISTED = [
    (3, 7, "T"),
    (11, 31, "missing_alias"),
    (16, 12, "T"),
    (16, 15, "U"),
    (16, 18, "K"),
    (16, 21, "A"),
]

STAR = "from os.path import *\nprint(join, missing_from_star, __file__, len)\ndef f():\n    return another_missing\n"

UNICODE = "# \u00e9\r\nval\u00fce = 1\r\nprint(val\u00fce, \uff4dissing_fullwidth, \u00fcnbound)\r\nif True:\r\n\tprint(tab_missing)\r\n"
UNICODE_LISTED = [(3, 14, "missing_fullwidth"), (3, 33, "\u00fcnbound"), (5, 8, "tab_missing")]

DUPLICATES = "print(dup, other_missing, dup)\nprint(dup)\n"
DUPLICATES_LISTED = [(1, 7, "dup"), (1, 12, "other_missing"), (1, 27, "dup"), (2, 7, "dup")]

IMPLICIT_ANY = "print({n})\ndef f():\n    return {n}\nclass C:\n    x = {n}\n    def m(self):\n        return lambda: {n}\n"
CLASS_NAMES = (
    "class C:\n"  # 1
    "    q = {n}\n"  # 2: implicit
    "    xs = [c for c in {n}]\n"  # 3: implicit (outermost iterable, evaluated in the class)
    "    ys = [{n} for _ in range(1)]\n"  # 4: listed (comprehension element)
    "    def m(self, d={n}):\n"  # 5: implicit (a default, evaluated in the class)
    "        return {n}\n"  # 6: listed (method)
    "print({n})\n"  # 7: listed (module)
)


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
                    path=path, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
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


def unbound(tmp_path: Path, source: str | bytes, path: str = "m.py") -> Any:
    root, rows = _repo(tmp_path, {path: source})
    return analyze_unbound_names(root, _row(rows, path))


def _listed(result: Any) -> list[tuple[int, int, str]]:
    return [(n.line, n.column, n.name) for n in result.names]


def at(source: str, needle: str, nth: int = 0) -> tuple[int, int]:
    """1-based (line, code-point column) of the ``nth`` occurrence of ``needle``."""
    index = -1
    for _ in range(nth + 1):
        index = source.index(needle, index + 1)
    line = source.count("\n", 0, index) + 1
    return line, index - (source.rfind("\n", 0, index) + 1) + 1


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_unbound_names._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_unbound_names, "_read_verified", spy)
    return reads


# --- The occurrence-level Step 45 parity oracle (test-only) ------------------------------------


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


def _step45(
    root: Path, row: Any, rows: list[Any], line: int, column: int, name: str
) -> tuple[str, str | None]:
    """Step 45's status at (line, column) and its own binding decision for that occurrence:
    "bound", "unbound", or None when Step 45 never looks the name up."""
    with _binding_spy() as calls:
        status = analyze_definition(root, row, rows, line, column).status
    found = next((result for looked_up, result in calls if looked_up == name), "absent")
    if found == "absent":
        return status, None
    return status, "unbound" if found is None else "bound"


def _oracle_type_parameter(node: ast.Name, tree: ast.Module) -> bool:
    """E2 from the contract text: a type parameter of a generic statement containing the load."""
    for statement in ast.walk(tree):
        if not isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.TypeAlias)
        ):
            continue
        params = [
            p.name
            for p in statement.type_params
            if isinstance(p, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple))
        ]
        if node.id not in params:
            continue
        begins = (statement.lineno, statement.col_offset) <= (node.lineno, node.col_offset)
        ends = (node.end_lineno, node.end_col_offset) <= (
            statement.end_lineno,
            statement.end_col_offset,
        )
        if begins and ends:
            return True
    return False


def _oracle_implicit(name: str, scope: Any) -> bool:
    """E1 from the contract text, on Step 45's scope chain."""
    if name in ("__file__", "__cached__", "__builtins__", "__annotations__"):
        return True
    if name in ("__module__", "__qualname__"):
        return bool(scope.kind == "class")
    if name == "__class__":
        kinds = []
        while scope is not None:
            kinds.append(scope.kind)
            scope = scope.parent
        return any(
            k in ("function", "lambda") and "class" in kinds[i + 1 :] for i, k in enumerate(kinds)
        )
    return False


@dataclass
class Expected:
    listed: list[tuple[str, int, int, str]] = field(default_factory=list)
    implicit: int = 0
    unsupported: int = 0
    bound_not_found: list[tuple[str, int, int, str]] = field(default_factory=list)
    candidates: int = 0


def oracle(root: Path, rows: list[Any], path: str) -> Expected:
    """Step 55's expected output for ``path``, decided occurrence by occurrence by Step 45."""
    row = _row(rows, path)
    text = _decode((root / path).read_bytes())
    assert text is not None
    text = _normalize(text)
    tree = ast.parse(text)
    table = collect_scopes(tree)  # Step 45's scopes, the authority for E1's context
    lines = text.split("\n")
    candidates = sorted(
        (
            (
                node.lineno,
                len(lines[node.lineno - 1].encode()[: node.col_offset].decode()) + 1,
                node,
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        ),
        key=lambda c: (c[0], c[1]),
    )
    expected = Expected(candidates=len(candidates))
    for line, column, node in candidates:
        occurrence = (path, line, column, node.id)
        status, decision = _step45(root, row, rows, line, column, node.id)
        if decision is None:
            continue  # Step 45 never looks it up (a PEP 695 type-parameter list)
        if decision == "bound":
            if status == "not_found":
                expected.bound_not_found.append(occurrence)  # R0: bound outside the repository
            continue
        if status == "unsupported" and node.id in BUILTIN_NAMES:
            continue  # R1
        if _oracle_type_parameter(node, tree):
            continue  # E2
        if _oracle_implicit(node.id, table.scope_of[id(node)]):
            expected.implicit += 1  # E1
            continue
        if status == "unsupported":
            expected.unsupported += 1  # R2: unbound, not a builtin, so a star import
            continue
        assert status == "not_found", (occurrence, status)
        expected.listed.append(occurrence)
    return expected


def _occurrences(result: Any) -> list[tuple[str, int, int, str]]:
    return [(result.path, n.line, n.column, n.name) for n in result.names]


def _occurrences_match(
    actual: list[tuple[str, int, int, str]], expected: list[tuple[str, int, int, str]]
) -> bool:
    """Exact occurrence-level comparison: path, line, column and identifier, in source order,
    duplicates included. Never only counts, names or sets."""
    return actual == expected


def assert_parity(root: Path, rows: list[Any], path: str) -> tuple[Any, Expected]:
    result = analyze_unbound_names(root, _row(rows, path))
    assert result.status == "parsed"
    expected = oracle(root, rows, path)
    actual = _occurrences(result)
    # 1. Every Step 55 occurrence is a Step 45 unbound not_found occurrence outside E1/E2.
    for occurrence in actual:
        assert occurrence in expected.listed, ("listed without Step 45 justification", occurrence)
    # 2. Every applicable Step 45 unbound not_found Load occurrence is listed by Step 55.
    for occurrence in expected.listed:
        assert occurrence in actual, ("Step 45 occurrence missing from Step 55", occurrence)
    assert _occurrences_match(actual, expected.listed)
    assert result.summary.model_dump() == {
        "unbound": len(expected.listed),
        "distinct": len({o[3] for o in expected.listed}),
        "unsupported": expected.unsupported,
        "implicit": expected.implicit,
    }
    assert result.truncated is False
    return result, expected


# --- A, B: basic names and scopes ------------------------------------------------------------


def test_basic_statically_unbound_names(tmp_path: Path) -> None:
    result = unbound(tmp_path, BASIC)
    assert result.path == "m.py" and result.status == "parsed"
    assert _listed(result) == BASIC_LISTED
    assert result.summary.model_dump() == {
        "unbound": 8,
        "distinct": 8,
        "unsupported": 0,
        "implicit": 0,
    }
    assert result.truncated is False


def test_scope_rules(tmp_path: Path) -> None:
    assert _listed(unbound(tmp_path, SCOPES)) == SCOPES_LISTED


def test_global_and_nonlocal_declarations_bind(tmp_path: Path) -> None:
    assert _listed(unbound(tmp_path, DECLARATIONS)) == [(12, 12, "not_declared")]


def test_one_based_positions(tmp_path: Path) -> None:
    assert _listed(unbound(tmp_path, "missing\n")) == [(1, 1, "missing")]


# --- C: bindings (the declared static-binding contract) ----------------------------------------


def test_imports_aliases_and_conservative_bindings_are_never_listed(tmp_path: Path) -> None:
    result = unbound(tmp_path, BINDINGS)
    assert (result.names, result.summary.model_dump()) == ([], ZERO)


def test_use_before_assignment_is_not_listed(tmp_path: Path) -> None:
    """Not a runtime NameError detector: the frozen scope model binds the name in its scope."""
    source = "print(x)\nx = 10\ndef f():\n    print(v)\n    v = 1\n"
    assert unbound(tmp_path, source).names == []


def test_conditional_definitions_are_not_listed(tmp_path: Path) -> None:
    source = "import os\nif os.environ:\n    y = 1\nprint(y)\nwhile False:\n    z = 2\nprint(z)\n"
    assert unbound(tmp_path, source).names == []


def test_try_except_and_del_bindings_are_not_listed(tmp_path: Path) -> None:
    source = (
        "try:\n    import ujson as fast\nexcept ImportError:\n    fast = None\n"
        "try:\n    from speedups import accelerate\nexcept ImportError:\n    pass\n"
        "gone = 1\ndel gone\nprint(fast, accelerate, gone)\n"
    )
    assert unbound(tmp_path, source).names == []


def test_dynamic_definitions_have_no_statically_known_binding(tmp_path: Path) -> None:
    assert _listed(unbound(tmp_path, DYNAMIC)) == [
        (3, 7, "made_by_exec"),
        (3, 21, "made_by_globals"),
    ]


# --- D: exclusions -----------------------------------------------------------------------------


def test_never_listed_forms(tmp_path: Path) -> None:
    """Stores, deletions, attributes, keyword arguments, definitions, parameters, strings and
    comments are not identifier loads; the class-body walrus is a store Step 45 leaves unbound."""
    result = unbound(tmp_path, NEVER)
    assert (result.names, result.summary.model_dump()) == ([], ZERO)


def test_builtins_and_frozen_builtin_dunders_are_never_listed(tmp_path: Path) -> None:
    result = unbound(tmp_path, BUILTIN_SOURCE)
    assert (result.names, result.summary.model_dump()) == ([], ZERO)  # R1: not even implicit


# --- E, F: interpreter-provided names and arbitrary dunders ------------------------------------


@pytest.mark.parametrize("name", ["__file__", "__cached__", "__builtins__", "__annotations__"])
def test_any_scope_interpreter_names_are_implicit(tmp_path: Path, name: str) -> None:
    result = unbound(tmp_path, IMPLICIT_ANY.format(n=name))
    assert result.names == []
    assert result.summary.model_dump() == {**ZERO, "implicit": 4}  # module, function, class, lambda


@pytest.mark.parametrize("name", ["__module__", "__qualname__"])
def test_class_namespace_names_are_implicit_only_in_class_bodies(tmp_path: Path, name: str) -> None:
    source = CLASS_NAMES.format(n=name)
    result = unbound(tmp_path, source)
    assert _listed(result) == [(4, 11, name), (6, 16, name), (7, 7, name)]
    assert (result.summary.implicit, result.summary.unbound) == (3, 3)


def test_class_cell_context(tmp_path: Path) -> None:
    result = unbound(tmp_path, CLASS_CELL)
    assert _listed(result) == [
        (2, 14, "__class__"),
        (3, 11, "__class__"),
        (12, 12, "__class__"),
        (13, 7, "__class__"),
    ]
    assert result.summary.model_dump() == {
        "unbound": 4,
        "distinct": 1,
        "unsupported": 0,
        "implicit": 5,
    }


def test_arbitrary_dunders_are_listed(tmp_path: Path) -> None:
    result = unbound(tmp_path, ARBITRARY)
    assert _listed(result) == [
        (1, 7, "__my_missing_variable__"),
        (1, 32, "__version__"),
        (1, 45, "__all__"),
        (1, 54, "__private_name"),
    ]  # __bound_dunder__ is bound
    assert result.summary.implicit == 0


def test_private_names_follow_ordinary_binding_rules(tmp_path: Path) -> None:
    assert unbound(tmp_path, PRIVATE).names == []  # bound; name mangling is not modelled
    assert _listed(unbound(tmp_path, "def f():\n    return __private_name\n")) == [
        (2, 12, "__private_name")
    ]


def test_interpreter_name_bound_by_the_module_is_bound(tmp_path: Path) -> None:
    result = unbound(tmp_path, "__file__ = 'x'\nprint(__file__)\n")
    assert (result.names, result.summary.implicit) == ([], 0)  # R0 comes first


# --- G: PEP 695 --------------------------------------------------------------------------------


def test_pep_695_type_parameters(tmp_path: Path) -> None:
    result = unbound(tmp_path, PEP695)
    assert _listed(result) == PEP695_LISTED
    assert result.summary.model_dump() == {
        "unbound": 6,
        "distinct": 5,
        "unsupported": 0,
        "implicit": 0,
    }


def test_pep_695_nested_generics_keep_their_own_spans(tmp_path: Path) -> None:
    source = "def outer[T](x: T):\n    def inner[S](y: S) -> T:\n        return y\n    return S\n"
    assert _listed(unbound(tmp_path, source)) == [(4, 12, "S")]


# --- H: star imports ---------------------------------------------------------------------------


def test_star_import_counts_unsupported(tmp_path: Path) -> None:
    result = unbound(tmp_path, STAR)
    assert result.names == []
    # join, missing_from_star and another_missing may come from the star import; __file__ is
    # E1 implicit (E1 comes first); len is a builtin and is not counted.
    assert result.summary.model_dump() == {
        "unbound": 0,
        "distinct": 0,
        "unsupported": 3,
        "implicit": 1,
    }


# --- I: positions ------------------------------------------------------------------------------


def test_nfkc_non_ascii_tabs_and_crlf(tmp_path: Path) -> None:
    assert _listed(unbound(tmp_path, UNICODE.encode())) == UNICODE_LISTED


def test_duplicates_in_source_order(tmp_path: Path) -> None:
    result = unbound(tmp_path, DUPLICATES)
    assert _listed(result) == DUPLICATES_LISTED
    assert (result.summary.unbound, result.summary.distinct) == (4, 2)


# --- J: limits ---------------------------------------------------------------------------------


def test_long_names_are_capped_without_truncation(tmp_path: Path) -> None:
    result = unbound(tmp_path, "print(" + "n" * 300 + ")\n")
    assert _listed(result) == [(1, 7, "n" * 200)]
    assert result.truncated is False


def _many(count: int, distinct: bool = True) -> str:
    return "".join(f"print(missing_{i if distinct else 0})\n" for i in range(count))


def test_exactly_the_cap_is_not_truncated(tmp_path: Path) -> None:
    result = unbound(tmp_path, _many(MAX_UNBOUND_NAMES))
    assert (len(result.names), result.truncated) == (500, False)
    assert (result.summary.unbound, result.summary.distinct) == (500, 500)
    assert MAX_UNBOUND_NAMES == 500


def test_one_over_the_cap_is_truncated_and_counted_before_the_cap(tmp_path: Path) -> None:
    result = unbound(tmp_path, _many(501))
    assert (len(result.names), result.truncated) == (500, True)
    assert _listed(result)[0] == (1, 7, "missing_0") and _listed(result)[-1] == (
        500,
        7,
        "missing_499",
    )
    assert (result.summary.unbound, result.summary.distinct) == (501, 501)
    same = unbound(tmp_path, _many(501, distinct=False))
    assert (same.summary.unbound, same.summary.distinct, same.truncated) == (501, 1, True)


# --- K: statuses -------------------------------------------------------------------------------


def _not_parsed(result: Any, status: str) -> None:
    assert result.model_dump() == {
        "path": "m.py",
        "status": status,
        "names": [],
        "summary": ZERO,
        "truncated": False,
    }


def test_empty_file(tmp_path: Path) -> None:
    result = unbound(tmp_path, "")
    assert (result.status, result.names, result.summary.model_dump()) == ("parsed", [], ZERO)


def test_syntax_error(tmp_path: Path) -> None:
    _not_parsed(unbound(tmp_path, "def (:\n    missing\n"), "syntax_error")
    _not_parsed(unbound(tmp_path, "x = '''\nmissing\n"), "syntax_error")


def test_tokenize_failure_is_a_syntax_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(source: str) -> Any:
        raise tokenize.TokenError("simulated", (1, 0))

    monkeypatch.setattr(code_unbound_names, "name_tokens", failing)
    _not_parsed(unbound(tmp_path, "print(missing)\n"), "syntax_error")


def test_binary(tmp_path: Path) -> None:
    _not_parsed(unbound(tmp_path, b"\x00\x01missing"), "binary")
    _not_parsed(unbound(tmp_path, b"\xff\xfe"), "binary")


def test_decoded_size_boundary(tmp_path: Path) -> None:
    body = b"print(missing)\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    assert len(exactly) == MAX_OUTLINE_BYTES
    assert _listed(unbound(tmp_path, exactly)) == [(1, 7, "missing")]
    _not_parsed(unbound(tmp_path, exactly + b"#"), "too_large")  # never parsed


def test_stored_size_limit_is_checked_before_opening(tmp_path: Path, read_spy: list[str]) -> None:
    result = unbound(tmp_path, os.urandom(MAX_CONTENT_BYTES))
    assert result.status in ("binary", "too_large") and read_spy == ["m.py"]  # exactly 1 MiB opens
    read_spy.clear()
    _not_parsed(unbound(tmp_path, os.urandom(MAX_CONTENT_BYTES) + b"x"), "too_large")
    assert read_spy == []  # over 1 MiB is never opened


# --- L: snapshot -------------------------------------------------------------------------------


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "print(missing)\n"})
    (root / "m.py").write_bytes(b"print(missing)  # changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_unbound_names(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "m.py").unlink()  # missing
    with pytest.raises(FileContentError) as raised:
        analyze_unbound_names(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_symlinked_file_fails_the_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "print(missing)\n"})
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"print(missing)\n")
    (root / "m.py").unlink()
    try:
        os.symlink(outside, root / "m.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    with pytest.raises(FileContentError) as raised:
        analyze_unbound_names(root, rows[0])
    assert raised.value.status_code == 409


def test_junctioned_package_fails_the_request(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    root, rows = _repo(tmp_path, {"pkg/m.py": "print(missing)\n"})
    outside = tmp_path / "outside"
    shutil.copytree(root / "pkg", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pkg")
    _winapi.CreateJunction(str(outside), str(root / "pkg"))  # type: ignore[attr-defined]
    with pytest.raises(FileContentError) as raised:
        analyze_unbound_names(root, rows[0])
    assert raised.value.status_code == 409


def test_nothing_is_cached_across_requests(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "print(missing)\n"})
    assert _listed(analyze_unbound_names(root, rows[0])) == [(1, 7, "missing")]
    changed = b"missing = 1\nprint(missing)\n"
    (root / "m.py").write_bytes(changed)
    row = SimpleNamespace(
        path="m.py", size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest()
    )
    assert analyze_unbound_names(root, row).names == []


# --- M (offline part): no execution ------------------------------------------------------------


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"  # 1
        "import os, subprocess, socket\n"  # 2
        "os.system('echo')\nsubprocess.run(['echo'])\n"  # 3, 4
        "socket.create_connection(('example.invalid', 80))\n"  # 5
        "eval('1')\nexec('made = 1')\n"  # 6, 7
        "def deco(f):\n    open('side-effect', 'w')\n    return f\n"  # 8-10
        "@deco\ndef target():\n    return made\n"  # 11-13
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
    result = analyze_unbound_names(root, rows[0])
    monkeypatch.undo()
    assert _listed(result) == [(13, 12, "made")]
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


# --- N: the Step 45 parity oracle --------------------------------------------------------------


PARITY: dict[str, str | bytes] = {
    "basic": BASIC,
    "scopes": SCOPES,
    "declarations": DECLARATIONS,
    "bindings": BINDINGS,
    "never": NEVER,
    "builtins": BUILTIN_SOURCE,
    "class_cell": CLASS_CELL,
    "class_names": CLASS_NAMES.format(n="__qualname__") + CLASS_NAMES.format(n="__module__"),
    "implicit_any": "".join(
        IMPLICIT_ANY.format(n=n)
        for n in ("__file__", "__cached__", "__builtins__", "__annotations__")
    ),
    "arbitrary": ARBITRARY + PRIVATE,
    "pep695": PEP695,
    "star": STAR,
    "unicode": UNICODE.encode(),
    "duplicates": DUPLICATES,
    "dynamic": DYNAMIC,
}


@pytest.mark.parametrize("fixture", sorted(PARITY))
def test_step_45_occurrence_parity(tmp_path: Path, fixture: str) -> None:
    """Exactly the occurrences Step 45's own resolver finds unbound with not_found, apart from
    E1/E2, compared as (path, line, column, identifier) in source order."""
    root, rows = _repo(tmp_path, {"m.py": PARITY[fixture]})
    _, expected = assert_parity(root, rows, "m.py")
    assert expected.candidates > 0


def test_parity_keeps_external_import_bindings_bound(tmp_path: Path) -> None:
    """Step 45 answers not_found for names bound to imports outside the repository; its own
    binding decision says bound, so Step 55 never lists them (R0)."""
    root, rows = _repo(tmp_path, {"m.py": BINDINGS})
    result, expected = assert_parity(root, rows, "m.py")
    names = {o[3] for o in expected.bound_not_found}
    # fast is also bound by an assignment, so Step 45 answers unsupported rather than not_found.
    assert {"os", "j", "OrderedDict", "not_installed_anywhere", "accelerate"} <= names
    assert "fast" not in names
    assert result.names == []
    basic_root, basic_rows = _repo(tmp_path, {"m.py": BASIC})
    _, basic = assert_parity(basic_root, basic_rows, "m.py")
    assert {o[3] for o in basic.bound_not_found} >= {"os", "L"}


def test_parity_distinguishes_duplicate_positions(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": DUPLICATES})
    result, expected = assert_parity(root, rows, "m.py")
    assert expected.listed == [("m.py", *o) for o in DUPLICATES_LISTED]
    assert len({o[3] for o in expected.listed}) == 2 and len(expected.listed) == 4


def test_parity_oracle_rejects_position_only_differences(tmp_path: Path) -> None:
    """The comparison is occurrence-level: equal names and counts never suffice."""
    root, rows = _repo(tmp_path, {"m.py": DUPLICATES})
    result = analyze_unbound_names(root, rows[0])
    expected = oracle(root, rows, "m.py").listed
    actual = _occurrences(result)
    assert _occurrences_match(actual, expected)

    def perturbed(index: int, line: int = 0, column: int = 0) -> list[tuple[str, int, int, str]]:
        copy = list(actual)
        path, old_line, old_column, name = copy[index]
        copy[index] = (path, old_line + line, old_column + column, name)
        return copy

    collapsed = list(actual)
    collapsed[2] = collapsed[0]  # the second "dup" reported at the first one's position
    for wrong in (perturbed(1, column=1), perturbed(3, line=-1), collapsed):
        assert sorted(o[3] for o in wrong) == sorted(o[3] for o in expected)  # same names
        assert len(wrong) == len(expected)  # same count
        assert not _occurrences_match(wrong, expected)


def _cpython_unbound(text: str) -> set[str]:
    """CPython's own symbol table (test-only, on fixture text the tests authored): implicitly
    global references never bound at module level, outside builtins and the allowlist."""
    top = symtable.symtable(text, "<fixture>", "exec")
    module_bound = {
        s.get_name()
        for s in top.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace()
    }
    found: set[str] = set()

    def walk(table: Any) -> None:
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if not symbol.is_referenced() or symbol.is_declared_global():
                continue
            if table.get_type() == "module" or (symbol.is_global() and not symbol.is_local()):
                if name not in module_bound and name not in BUILTIN_NAMES and name not in ALLOWLIST:
                    found.add(name)
        for child in table.get_children():
            walk(child)

    walk(top)
    return found


@pytest.mark.parametrize("source", [BASIC, SCOPES, DECLARATIONS, DYNAMIC, ARBITRARY, DUPLICATES])
def test_auxiliary_cpython_symbol_table_agreement(tmp_path: Path, source: str) -> None:
    """Auxiliary, identifier-level only: the Step 45 parity oracle remains the authority."""
    assert {n.name for n in unbound(tmp_path, source).names} == _cpython_unbound(source)


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
    "import os\nprint(os, missing_one)\ndef f():\n    return __file__, missing_two, missing_one\n"
)
FILES: dict[str, bytes] = {
    "app/mod.py": API_SOURCE.encode(),
    "app/bad.py": b"def (:\n",
    "pkg/__init__.py": b"",
    "docs/notes.md": b"missing names\n",
}
TARGET = {"path": "app/mod.py"}
API_RESULT = {
    "path": "app/mod.py",
    "status": "parsed",
    "names": [
        {"line": 2, "column": 11, "name": "missing_one"},
        {"line": 4, "column": 22, "name": "missing_two"},
        {"line": 4, "column": 35, "name": "missing_one"},
    ],
    "summary": {"unbound": 3, "distinct": 2, "unsupported": 0, "implicit": 1},
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
        f"/repositories/{repository_id or owner.repository_id}/files/unbound-names"
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
async def test_unbound_names_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
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
        "names": [],
        "summary": ZERO,
        "truncated": False,
    }
    assert read_spy == []  # a non-Python file is never read
    body = (await _get(client, alice, {"path": "app/bad.py"})).json()
    assert body == {
        "path": "app/bad.py",
        "status": "syntax_error",
        "names": [],
        "summary": ZERO,
        "truncated": False,
    }
    body = (await _get(client, alice, {"path": "pkg/__init__.py"})).json()
    assert (body["status"], body["names"]) == ("parsed", [])


@requires_db
async def test_validation_and_unknown_paths(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    for params in ({"path": ""}, {"path": "a" * 4097}, {}):
        assert (await _get(client, alice, params)).status_code == 422, params
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _get(client, alice, TARGET, **kwargs)).status_code == 422
    assert read_spy == []
    for path in ("missing.py", "../app/mod.py", "/app/mod.py", "APP/MOD.PY"):
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
    real = code_unbound_names.repository_storage_dir

    def swap_then_root(settings: Any, repository_id: Any) -> Path:
        root = real(settings, repository_id)
        (root / "app" / "mod.py").write_bytes(b"missing_one = 1  # new snapshot\n")
        return root

    monkeypatch.setattr(code_unbound_names, "repository_storage_dir", swap_then_root)
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
    marker = "UNIQUE-CONTENT-MARKER-55f1"
    files = dict(FILES)
    files["app/mod.py"] = (API_SOURCE + f"note = '{marker}'  # {marker}\n").encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "parsed"
    assert marker not in response.text and marker not in caplog.text
    for word in ("import", "print", "return", "def "):
        assert word not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text
    body = response.json()
    assert set(body) == {"path", "status", "names", "summary", "truncated"}
    assert set(body["summary"]) == {"unbound", "distinct", "unsupported", "implicit"}
    for entry in body["names"]:
        assert set(entry) == {"line", "column", "name"}


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"b.py": b"print(bobs_missing)\n"})
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
    assert [n["name"] for n in own["names"]] == ["bobs_missing"]


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
    bob = await _owner(client, "bob@example.com", {"b.py": b"print(bobs_missing)\n"})
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
    assert limiter.hits == [(UNBOUND_NAMES_LIMIT, alice.user_id)]
    assert read_spy == []  # limited before any snapshot read


@requires_db_and_redis
async def test_rate_limit_is_120_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert UNBOUND_NAMES_LIMIT == Limit("unbound-names-user", 120, 600)
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
        assert any(k.startswith("ratelimit:unbound-names-user:") for k in keys)
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
