"""Python call hierarchy tests (GET .../files/call-hierarchy?path=&line=). Offline.

Scanner tests use a temporary storage directory and need no database. Every semantic test
also checks the Step 45 oracle (Step 45's go-to-definition at every call site, with call
sites and owning units found by an independent AST walk written from Python's evaluation
rules) and Step 47 parity (callers are exactly Step 47's references that are call sites).
API tests run against the already-migrated throwaway test database (TEST_DATABASE_URL,
"_test") inside rolled-back transactions; ingestion runs inline with the real worker logic
and DATA_DIR is a temporary directory. The rate-limit test uses a disposable Redis
(TEST_REDIS_URL). Junction tests are Windows-only; symlink tests are skipped where symlinks
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
import sys
import unicodedata
import uuid
from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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
from app.schemas.code_call_hierarchy import CallHierarchySummary
from app.services import code_call_hierarchy, code_definition, code_references, ingestion
from app.services.code_call_hierarchy import (
    CALL_HIERARCHY_LIMIT,
    MAX_CALLEE_CALLS,
    MAX_CALLEES,
    MAX_CALLERS,
    MAX_SITES,
    MAX_SITES_PER_GROUP,
    analyze_call_hierarchy,
)
from app.services.code_definition import (
    BUILTIN_NAMES,
    _cap,
    _normalize,
    analyze_definition,
    collect_scopes,
    name_tokens,
    resolving_scope,
)
from app.services.code_importers import MAX_PARSED_BYTES, MAX_SCANNED_BYTES
from app.services.code_imports import module_parts
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.code_references import analyze_references
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
CALLEE_COUNTS = (
    "callee_calls",
    "callee_ambiguous",
    "callee_unsupported",
    "callee_builtin",
    "callee_unresolved",
    "callee_not_callable",
    "callee_dynamic",
)
CALLER_COUNTS = ("caller_calls", "caller_ambiguous", "caller_unsupported", "non_call_references")


# --- Fixture repository --------------------------------------------------------------------

AUTH = "\n".join(
    [
        "def authenticate(user):",  # 1
        "    return check(user)",  # 2
        "",  # 3
        "def check(user):",  # 4
        "    if user:",  # 5
        "        return authenticate(user)",  # 6
        "    return None",  # 7
        "",  # 8
        "class User:",  # 9
        "    def __init__(self, name):",  # 10
        "        self.name = name",  # 11
        "",  # 12
        "    def save(self):",  # 13
        "        return check(self)",  # 14
        "",  # 15
        "authenticate(None)",  # 16
        "",
    ]
)

VIEWS = "\n".join(
    [
        "from auth import authenticate as login, User",  # 1
        "import auth",  # 2
        "import auth as a",  # 3
        "from . import helpers",  # 4
        "from .helpers import helper",  # 5
        "",  # 6
        "@login",  # 7
        "def view(request):",  # 8
        "    login(request)",  # 9
        "    auth.authenticate(request)",  # 10
        "    a.authenticate(request); helper(); helper()",  # 11
        '    user = User("x")',  # 12
        "    helpers.helper()",  # 13
        "    print(user)",  # 14
        "    request.save()",  # 15
        "    f()()",  # 16
        "    def inner():",  # 17
        "        return login(request)",  # 18
        "    return inner()",  # 19
        "",  # 20
        "class Panel:",  # 21
        "    handler = login(None)",  # 22
        "",  # 23
        "    @login",  # 24
        "    def render(self):",  # 25
        "        return [login(x) for x in self.items]",  # 26
        "",
    ]
)

REPO: dict[str, str | bytes] = {
    "auth.py": AUTH,
    "app/__init__.py": "from auth import authenticate\nauthenticate(1)\n",
    "app/views.py": VIEWS,
    "app/helpers.py": "def helper():\n    return 1\n",
    "__init__.py": "from auth import authenticate\nauthenticate(0)\n",  # a root __init__
    "docs/notes.md": "def authenticate():\n    pass\nauthenticate()\n",
}

AUTHENTICATE = {
    "path": "auth.py",
    "kind": "function",
    "name": "authenticate",
    "line": 1,
    "end_line": 2,
}

# ((path, line, kind, name, end_line), [(line, column, name, ambiguous), ...]), server order.
AUTHENTICATE_CALLERS = [
    (("__init__.py", 1, "module", "__init__", 1), [(2, 1, "authenticate", False)]),
    (("app/__init__.py", 1, "module", "app", 1), [(2, 1, "authenticate", False)]),
    (("app/views.py", 1, "module", "views", 1), [(7, 2, "login", False)]),  # @login
    (
        ("app/views.py", 8, "function", "view", 19),
        [(9, 5, "login", False), (10, 10, "authenticate", False), (11, 7, "authenticate", False)],
    ),
    (("app/views.py", 17, "function", "inner", 18), [(18, 16, "login", False)]),
    (
        ("app/views.py", 21, "class", "Panel", 26),
        [(22, 15, "login", False), (24, 6, "login", False)],
    ),
    (("app/views.py", 25, "method", "render", 26), [(26, 17, "login", False)]),
    (("auth.py", 1, "module", "auth", 1), [(16, 1, "authenticate", False)]),
    (("auth.py", 4, "function", "check", 7), [(6, 16, "authenticate", False)]),  # mutual recursion
]
AUTHENTICATE_CALLEES = [(("auth.py", 4, "function", "check", 7), [(2, 12, "check", False)])]
VIEW_CALLEES = [
    (
        ("app/helpers.py", 1, "function", "helper", 2),
        [(11, 30, "helper", False), (11, 40, "helper", False)],
    ),
    (("app/views.py", 17, "function", "inner", 18), [(19, 12, "inner", False)]),
    (
        ("auth.py", 1, "function", "authenticate", 2),
        [(9, 5, "login", False), (10, 10, "authenticate", False), (11, 7, "authenticate", False)],
    ),
    (("auth.py", 9, "class", "User", 14), [(12, 12, "User", False)]),  # never __init__
]


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


def hierarchy(
    tmp_path: Path, files: dict[str, str | bytes], path: str, line: int, **kw: Any
) -> Any:
    root, rows = _repo(tmp_path, files)
    return analyze_call_hierarchy(root, _row(rows, path), rows, line, **kw)


def _view(groups: list[Any] | None) -> list[tuple[Any, ...]] | None:
    if groups is None:
        return None
    out = []
    for group in groups:
        loc = group.caller if hasattr(group, "caller") else group.callee
        sites = [(c.line, c.column, c.name, c.ambiguous) for c in group.calls]
        out.append(((loc.path, loc.line, loc.kind, loc.name, loc.end_line), sites))
    return out


def _sites(groups: list[Any]) -> list[tuple[int, int]]:
    return [(c.line, c.column) for group in groups for c in group.calls]


def _counts(result: Any, names: tuple[str, ...]) -> dict[str, int]:
    summary = result.summary.model_dump()
    return {name: summary[name] for name in names if summary[name]}


# --- The Step 45 oracle (independent call sites and units) ------------------------------------


def _module_name(path: str) -> str:
    parts = module_parts(path)
    return _cap(parts[-1] if parts else PurePosixPath(path).stem)


def _walk(path: str, tree: ast.Module) -> tuple[list[tuple[Any, Any]], dict[int, Any]]:
    """Every call site (callee Name/Attribute or None when dynamic, owning unit or None) and
    every definition (line -> (location, scope kind bound in, unit bound in)). Written from
    Python's evaluation rules, independently of the draft and of Step 45's collector."""
    module = (path, 1, "module", _module_name(path), 1)
    sites: list[tuple[Any, Any]] = []
    definitions: dict[int, Any] = {}
    stack: list[tuple[ast.AST, Any, str]] = [(n, module, "module") for n in reversed(tree.body)]
    while stack:
        node, unit, scope = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(node, ast.ClassDef):
                kind = "class"
                outer: list[ast.AST] = [*node.decorator_list, *node.bases, *node.keywords]
            else:
                kind = "method" if scope == "class" else "function"
                a = node.args
                every = [*a.posonlyargs, *a.args, *a.kwonlyargs]
                every += [x for x in (a.vararg, a.kwarg) if x is not None]
                outer = [*node.decorator_list, *a.defaults]
                outer += [d for d in a.kw_defaults if d is not None]
                outer += [x.annotation for x in every if x.annotation is not None]
                outer += [node.returns] if node.returns is not None else []
            location = (path, node.lineno, kind, _cap(node.name), node.end_lineno)
            definitions[node.lineno] = (location, scope, unit)
            for decorator in node.decorator_list:  # @d applies d to the definition
                plain = isinstance(decorator, (ast.Name, ast.Attribute))
                sites.append((decorator if plain else None, unit))
            inner = "class" if kind == "class" else "function"
            children = [(n, unit, scope) for n in outer]
            children += [(n, None, scope) for n in getattr(node, "type_params", [])]
            children += [(n, location, inner) for n in node.body]
        else:
            if isinstance(node, ast.Call):
                plain = isinstance(node.func, (ast.Name, ast.Attribute))
                sites.append((node.func if plain else None, unit))
            children = [(c, unit, scope) for c in ast.iter_child_nodes(node)]
        stack.extend(reversed(children))
    return sites, definitions


def _position(text: str, tokens: list[Any], callee: ast.expr) -> tuple[int, int] | None:
    if isinstance(callee, ast.Name):
        line = text.split("\n")[callee.lineno - 1]
        return callee.lineno, len(line.encode()[: callee.col_offset].decode()) + 1
    assert isinstance(callee, ast.Attribute)
    token = next(
        (t for t in tokens if t.line == callee.end_lineno and t.end_byte == callee.end_col_offset),
        None,
    )
    if token is None or unicodedata.normalize("NFKC", token.text) != callee.attr:
        return None
    return token.line, token.column + 1


def _builtin(table: Any, callee: ast.expr) -> bool:
    root = callee
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name) or id(root) not in table.scope_of:
        return False
    scope = module = table.scope_of[id(root)]
    while module.parent is not None:
        module = module.parent
    found = resolving_scope(scope, root.id)
    return found is None and not module.star_import and root.id in BUILTIN_NAMES


def _oracle_text(root: Path, row: Any) -> str | None:
    if row.size_bytes > MAX_CONTENT_BYTES:
        return None
    text = _decode((root / row.path).read_bytes())
    if text is None or len(text.encode()) > MAX_OUTLINE_BYTES:
        return None
    text = _normalize(text)
    try:
        ast.parse(text)
        name_tokens(text)
    except Exception:
        return None
    return text


def oracle(root: Path, rows: list[Any], path: str, line: int) -> dict[str, Any]:
    """The expected hierarchy of the definition on ``line``: Step 45 at every call site."""
    parsed = {}
    for row in rows:
        text = _oracle_text(root, row)
        if text is not None:
            tree = ast.parse(text)
            sites, definitions = _walk(row.path, tree)
            parsed[row.path] = (text, sites, definitions, collect_scopes(tree), name_tokens(text))
    text, sites, definitions, table, tokens = parsed[path]
    location, scope, unit = definitions[line]
    wanted = location[:4]
    by_path = {row.path: row for row in rows}

    def ask(file: str, callee: ast.expr) -> Any:
        position = _position(parsed[file][0], parsed[file][4], callee)
        if position is None:
            return None
        return analyze_definition(root, by_path[file], rows, *position)

    counts: Counter[str] = Counter()
    truncated = False
    callees: dict[tuple[Any, ...], list[tuple[Any, ...]]] = defaultdict(list)
    for callee, owner in sites:
        if owner is None or owner[:4] != wanted:
            continue
        if callee is None:
            counts["callee_dynamic"] += 1
            continue
        result = ask(path, callee)
        if result is None or result.status == "unsupported":
            truncated |= bool(result is not None and result.truncated)
            builtin = result is not None and _builtin(table, callee)
            counts["callee_builtin" if builtin else "callee_unsupported"] += 1
            continue
        truncated |= result.truncated
        if result.status == "not_found":
            counts["callee_unresolved"] += 1
            continue
        assert result.status in ("resolved", "ambiguous"), result.status
        callable_ = [d for d in result.definitions if d.kind != "module"]
        if not callable_:
            counts["callee_not_callable"] += 1
            continue
        ambiguous = len(result.definitions) > 1
        counts["callee_calls"] += 1
        counts["callee_ambiguous"] += ambiguous
        ref = result.reference
        for d in callable_:
            callees[(d.path, d.line, d.kind, d.name, d.end_line)].append(
                (ref.line, ref.column, ref.name, ambiguous)
            )
    callers: dict[tuple[Any, ...], list[tuple[Any, ...]]] | None = None
    if scope != "class":
        callers = defaultdict(list)
        files = sorted(parsed) if unit[2] == "module" else [path]
        for file in files:
            for callee, owner in parsed[file][1]:
                if owner is None or callee is None:
                    continue
                result = ask(file, callee)
                if result is None:
                    continue
                keys = [(d.path, d.line, d.kind, d.name) for d in result.definitions]
                if wanted in keys:
                    ref = result.reference
                    callers[owner].append((ref.line, ref.column, ref.name, len(keys) > 1))

    def ordered(groups: dict[tuple[Any, ...], list[tuple[Any, ...]]]) -> list[tuple[Any, ...]]:
        return [(key, sorted(groups[key])) for key in sorted(groups, key=lambda k: k[:4])]

    return {
        "target": location,
        "callees": ordered(callees),
        "callers": None if callers is None else ordered(callers),
        "counts": {k: v for k, v in counts.items() if v},
        "callees_truncated": truncated,
    }


def parity(root: Path, rows: list[Any], target: Any, line: int, result: Any) -> None:
    """Step 47: callers are exactly its references that are call sites."""
    refs = analyze_references(root, target, rows, line)
    assert refs.status == "scanned"
    calls = {(g.caller.path, c.line, c.column): c for g in result.callers for c in g.calls}
    found = {(r.path, r.line, r.column): r for r in refs.references}
    assert set(calls) <= set(found)
    for spot, call in calls.items():
        assert (call.name, call.ambiguous) == (found[spot].name, found[spot].ambiguous)
    summary = result.summary
    assert refs.summary.references == summary.caller_calls + summary.non_call_references
    assert summary.caller_calls == len(calls)
    assert refs.summary.unsupported == summary.caller_unsupported
    assert refs.truncated == result.callers_truncated


def checked(tmp_path: Path, files: dict[str, str | bytes], path: str, line: int) -> Any:
    """``hierarchy`` plus the Step 45 oracle and Step 47 parity (no limit reached)."""
    root, rows = _repo(tmp_path, files)
    target = _row(rows, path)
    result = analyze_call_hierarchy(root, target, rows, line)
    assert result.status == "scanned", result.status
    expected = oracle(root, rows, path, line)
    t = result.target
    assert (t.path, t.line, t.kind, t.name, t.end_line) == expected["target"]
    assert _view(result.callees) == expected["callees"]
    assert all(g.total_calls == len(g.calls) for g in result.callees)
    assert _counts(result, CALLEE_COUNTS) == expected["counts"]
    assert result.callees_truncated is expected["callees_truncated"]
    if expected["callers"] is None:
        assert (result.callers_status, result.callers) == ("unsupported_target", None)
        assert result.callers_truncated is None
        assert _counts(result, CALLER_COUNTS) == {}
    else:
        assert result.callers_status == "scanned"
        assert _view(result.callers) == expected["callers"]
        assert all(g.total_calls == len(g.calls) for g in result.callers)
        sites = [s for _, group in expected["callers"] for s in group]
        assert result.summary.caller_calls == len(sites)
        assert result.summary.caller_ambiguous == sum(s[3] for s in sites)
        parity(root, rows, target, line, result)
    return result


def at(source: str, needle: str, nth: int = 0, offset: int = 0) -> tuple[int, int]:
    """1-based (line, column) of the ``nth`` occurrence of ``needle``, plus ``offset``."""
    index = -1
    for _ in range(nth + 1):
        index = source.index(needle, index + 1)
    line = source.count("\n", 0, index) + 1
    column = index - (source.rfind("\n", 0, index) + 1) + 1
    return line, column + offset


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_references._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_references, "_read_verified", spy)
    return reads


# --- G, R, S, U, AC, AD, AE, AF, AQ, W: the repository ------------------------------------------


def test_repository_call_hierarchy(tmp_path: Path) -> None:
    result = checked(tmp_path, REPO, "auth.py", 1)
    assert result.target.model_dump() == AUTHENTICATE
    assert _view(result.callers) == AUTHENTICATE_CALLERS
    assert _view(result.callees) == AUTHENTICATE_CALLEES
    assert result.summary.model_dump() == {
        "files_scanned": 5,  # docs/notes.md is not Python
        "too_large": 0,
        "binary": 0,
        "syntax_error": 0,
        "caller_calls": 12,
        "caller_ambiguous": 0,
        "caller_unsupported": 0,
        "non_call_references": 4,  # the three import lines name it four times
        "callee_calls": 1,
        "callee_ambiguous": 0,
        "callee_unsupported": 0,
        "callee_builtin": 0,
        "callee_unresolved": 0,
        "callee_not_callable": 0,
        "callee_dynamic": 0,
    }
    assert (result.callers_status, result.callers_truncated, result.callees_truncated) == (
        "scanned",
        False,
        False,
    )


def test_function_callees_and_counts(tmp_path: Path) -> None:
    result = checked(tmp_path, REPO, "app/views.py", 8)  # decorated: the def line is the target
    assert (result.target.name, result.target.line, result.target.end_line) == ("view", 8, 19)
    assert _view(result.callees) == VIEW_CALLEES
    assert [g.total_calls for g in result.callees] == [2, 1, 3, 1]
    assert _counts(result, CALLEE_COUNTS) == {
        "callee_calls": 7,
        "callee_unsupported": 2,  # helpers.helper (from-imported module), request.save
        "callee_builtin": 1,  # print
        "callee_unresolved": 1,  # the unbound f of f()()
        "callee_dynamic": 1,  # the outer call of f()()
    }
    assert (
        result.callers == [] and _counts(result, CALLER_COUNTS) == {}
    )  # @login is not a call of view


# --- H, I, J, K, L, M: targets ----------------------------------------------------------------


def test_async_function_target(tmp_path: Path) -> None:
    source = (
        "async def fetch(url):\n"  # 1
        "    await load(url)\n"  # 2
        "    async with session() as s:\n"  # 3
        "        async for chunk in stream(s):\n"  # 4
        "            yield chunk\n"  # 5
        "def load(u): pass\n"  # 6
        "def session(): pass\n"  # 7
        "def stream(s): pass\n"  # 8
        "async def main():\n"  # 9
        "    await fetch('x')\n"  # 10
        "    [c async for c in fetch('y')]\n"  # 11
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert result.target.model_dump() == {
        "path": "m.py",
        "kind": "function",
        "name": "fetch",
        "line": 1,
        "end_line": 5,
    }
    assert [(g.callee.name, _sites([g])) for g in result.callees] == [
        ("load", [(2, 11)]),
        ("session", [(3, 16)]),
        ("stream", [(4, 28)]),
    ]
    assert _view(result.callers) == [
        (("m.py", 9, "function", "main", 11), [(10, 11, "fetch", False), (11, 23, "fetch", False)])
    ]


def test_method_target_has_callees_but_unsupported_callers(tmp_path: Path) -> None:
    result = checked(tmp_path, REPO, "app/views.py", 25)
    assert result.target.model_dump() == {
        "path": "app/views.py",
        "kind": "method",
        "name": "render",
        "line": 25,
        "end_line": 26,
    }
    assert (result.callers_status, result.callers, result.callers_truncated) == (
        "unsupported_target",
        None,
        None,
    )
    assert _view(result.callees) == [
        (("auth.py", 1, "function", "authenticate", 2), [(26, 17, "login", False)])
    ]  # the @login of line 24 belongs to the class body
    assert _counts(result, CALLER_COUNTS) == {}
    save = checked(tmp_path, REPO, "auth.py", 13)  # a method's module-level callee
    assert _view(save.callees) == [
        (("auth.py", 4, "function", "check", 7), [(14, 16, "check", False)])
    ]
    assert (save.callers_status, save.callers) == ("unsupported_target", None)


def test_nested_function_target_scans_only_its_file(tmp_path: Path, read_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def outer():\n    def inner():\n        return outer()\n    inner()\n    return inner\n",
        "n.py": "from m import inner\ninner()\nfrom m import outer\nouter()\n",
    }
    result = checked(tmp_path, files, "m.py", 2)
    assert (result.target.kind, result.target.name) == ("function", "inner")
    assert _view(result.callers) == [
        (("m.py", 1, "function", "outer", 5), [(4, 5, "inner", False)])
    ]
    assert _view(result.callees) == [
        (("m.py", 1, "function", "outer", 5), [(3, 16, "outer", False)])
    ]
    assert result.summary.non_call_references == 1  # "return inner"
    assert result.summary.files_scanned == 1
    read_spy.clear()
    hierarchy(tmp_path, files, "m.py", 2)
    assert read_spy == ["m.py"]


def test_nested_class_targets(tmp_path: Path) -> None:
    source = (
        "def f(): pass\n"  # 1
        "class Outer:\n"  # 2
        "    class Inner:\n"  # 3
        "        x = f()\n"  # 4
        "    y = Inner()\n"  # 5
        "def build():\n"  # 6
        "    class Local:\n"  # 7
        "        z = f()\n"  # 8
        "    return Local()\n"  # 9
    )
    inner = checked(tmp_path, {"m.py": source}, "m.py", 3)
    assert (inner.target.kind, inner.callers_status, inner.callers) == (
        "class",
        "unsupported_target",
        None,
    )
    assert _view(inner.callees) == [(("m.py", 1, "function", "f", 1), [(4, 13, "f", False)])]
    local = checked(tmp_path, {"m.py": source}, "m.py", 7)  # bound in a function: scanned
    assert _view(local.callers) == [
        (("m.py", 6, "function", "build", 9), [(9, 12, "Local", False)])
    ]
    assert _view(local.callees) == [(("m.py", 1, "function", "f", 1), [(8, 13, "f", False)])]
    outer = checked(tmp_path, {"m.py": source}, "m.py", 2)
    assert _view(outer.callees) == [
        (("m.py", 3, "class", "Inner", 4), [(5, 9, "Inner", False)])  # Inner's body is its own
    ]
    build = checked(tmp_path, {"m.py": source}, "m.py", 6)
    assert _view(build.callees) == [(("m.py", 7, "class", "Local", 8), [(9, 12, "Local", False)])]


def test_class_target(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "models.py": (
            "def register(c): return c\n"  # 1
            "def default(): pass\n"  # 2
            "def column(): pass\n"  # 3
            "class Base: pass\n"  # 4
            "@register\n"  # 5
            "class User(Base, metaclass=type(default())):\n"  # 6
            "    id = column()\n"  # 7
            "    names = [column() for _ in range(2)]\n"  # 8
            "    def save(self):\n"  # 9
            "        return default()\n"  # 10
            "class Admin(User): pass\n"  # 11
        ),
        "use.py": (
            "from models import User\n"  # 1
            "u = User()\n"  # 2
            "isinstance(u, User)\n"  # 3
            "match u:\n"  # 4
            "    case User(): pass\n"  # 5
        ),
    }
    result = checked(tmp_path, files, "models.py", 6)
    assert result.target.model_dump() == {
        "path": "models.py",
        "kind": "class",
        "name": "User",
        "line": 6,
        "end_line": 10,
    }
    assert _view(result.callees) == [
        (
            ("models.py", 3, "function", "column", 3),
            [(7, 10, "column", False), (8, 14, "column", False)],
        )
    ]  # @register, type(default()) and save's default() are not the class body's
    assert _counts(result, CALLEE_COUNTS) == {"callee_calls": 2, "callee_builtin": 1}  # range
    assert _view(result.callers) == [(("use.py", 1, "module", "use", 1), [(2, 5, "User", False)])]
    # use.py's import, isinstance argument and class pattern; Admin's base
    assert result.summary.non_call_references == 4


def test_targets_and_statuses(tmp_path: Path) -> None:
    source = (
        "@decorator\n"  # 1
        "def f():\n"  # 2
        "    pass\n"  # 3
        "x = f()\n"  # 4
        "\n"  # 5
    )
    files: dict[str, str | bytes] = {"m.py": source}
    empty = {name: 0 for name in CallHierarchySummary.model_fields}
    for line in (1, 3, 4, 5, 6, 999):  # decorator, body, module statement, blank, past the end
        result = hierarchy(tmp_path, files, "m.py", line)
        assert result.model_dump() == {
            "status": "not_a_definition",
            "target": None,
            "callers_status": None,
            "callers": None,
            "callees": [],
            "summary": empty,
            "callers_truncated": None,
            "callees_truncated": False,
        }
    assert hierarchy(tmp_path, {"m.py": ""}, "m.py", 1).status == "not_a_definition"
    assert hierarchy(tmp_path, {"m.py": "f()\n"}, "m.py", 1).status == "not_a_definition"  # M
    assert hierarchy(tmp_path, files, "m.py", 2).status == "scanned"


def test_target_file_statuses(tmp_path: Path, read_spy: list[str]) -> None:
    def status(content: str | bytes, line: int = 1) -> str:
        result = hierarchy(tmp_path, {"m.py": content}, "m.py", line)
        if result.status != "scanned":
            assert (result.target, result.callers, result.callees) == (None, None, [])
            assert set(result.summary.model_dump().values()) == {0}
        return result.status

    assert status("def (:\n") == "syntax_error"
    assert status(b"\x00\x01") == "binary"
    assert status(b"\xff\xfe") == "binary"
    body = b"def target():\n    pass\ntarget()\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    result = hierarchy(tmp_path, {"m.py": exactly}, "m.py", 1)
    assert _sites(result.callers) == [(3, 1)]
    assert status(exactly + b"#") == "too_large"
    read_spy.clear()
    assert status(os.urandom(MAX_CONTENT_BYTES)) in ("binary", "too_large")
    assert read_spy == ["m.py"]  # exactly 1 MiB is opened
    read_spy.clear()
    assert status(os.urandom(MAX_CONTENT_BYTES) + b"x") == "too_large"
    assert read_spy == []  # over 1 MiB is never opened


# --- Q, R, S, T, U, V, W, X, Y: resolution ------------------------------------------------------


def test_direct_call_forms(tmp_path: Path) -> None:
    source = (
        "def f(*a, **k): return a\n"  # 1
        "async def caller():\n"  # 2
        "    f(f(1))\n"  # 3
        "    x = (y := f())\n"  # 4
        "    print(f'{f()}')\n"  # 5
        "    await f()\n"  # 6
        "    g(key=f)\n"  # 7 not a call of f
        "    (f)()\n"  # 8
        "    f(\n        1)\n"  # 9
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert _view(result.callers) == [
        (
            ("m.py", 2, "function", "caller", 10),
            [
                (3, 5, "f", False),
                (3, 7, "f", False),
                (4, 15, "f", False),
                (5, 14, "f", False),
                (6, 11, "f", False),
                (8, 6, "f", False),
                (9, 5, "f", False),
            ],
        )
    ]
    assert result.summary.non_call_references == 1  # key=f
    caller = checked(tmp_path, {"m.py": source}, "m.py", 2)
    assert _counts(caller, CALLEE_COUNTS) == {
        "callee_calls": 7,
        "callee_builtin": 1,
        "callee_unresolved": 1,  # print, g
    }


def test_imported_calls_and_aliases(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "lib/core.py": "def run():\n    pass\n",
        "lib/__init__.py": "from .core import run\nfrom .core import run as start\n",
        "app.py": (
            "from lib.core import run\n"  # 1
            "from lib.core import run as go\n"  # 2
            "from lib import start\n"  # 3
            "def main():\n"  # 4
            "    run(); go(); start()\n"  # 5
        ),
        "h/two.py": "from lib.core import run as run2\n",
        "h/three.py": "from h.two import run2 as run3\n",
        "chain.py": "from h.three import run3 as run4\nrun4()\n",
    }
    result = checked(tmp_path, files, "lib/core.py", 1)
    assert _view(result.callers) == [
        (
            ("app.py", 4, "function", "main", 5),
            [(5, 5, "run", False), (5, 12, "go", False), (5, 18, "start", False)],
        ),
        (("chain.py", 1, "module", "chain", 1), [(2, 1, "run4", False)]),  # three renames away
    ]
    main = checked(tmp_path, files, "app.py", 4)
    assert [g.callee.path for g in main.callees] == ["lib/core.py"]
    assert main.callees[0].total_calls == 3


def test_relative_imports(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "users/__init__.py": "",
        "users/service.py": "class User:\n    pass\n",
        "admin/service.py": "class User:\n    pass\n",
        "users/api.py": "from .service import User\ndef make():\n    return User()\n",
        "top.py": "from service import User\nUser()\n",  # absolute: both service modules
    }
    result = checked(tmp_path, files, "users/service.py", 1)
    assert _view(result.callers) == [
        (("top.py", 1, "module", "top", 1), [(2, 1, "User", True)]),
        (("users/api.py", 2, "function", "make", 3), [(3, 12, "User", False)]),
    ]
    make = checked(tmp_path, files, "users/api.py", 2)
    assert [g.callee.path for g in make.callees] == ["users/service.py"]


def test_module_aliases(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "pkg/__init__.py": "",
        "pkg/mod.py": "def f():\n    pass\n",
        "use.py": (
            "import pkg.mod\n"  # 1
            "import pkg.mod as m\n"  # 2
            "from pkg import mod\n"  # 3
            "def g():\n"  # 4
            "    pkg.mod.f()\n"  # 5
            "    m.f()\n"  # 6
            "    mod.f()\n"  # 7 a from-imported module: unsupported
            "    m()\n"  # 8 a module: not callable
            "    pkg.mod()\n"  # 9
        ),
    }
    result = checked(tmp_path, files, "pkg/mod.py", 1)
    assert _view(result.callers) == [
        (("use.py", 4, "function", "g", 9), [(5, 13, "f", False), (6, 7, "f", False)])
    ]
    assert result.summary.caller_unsupported == 1
    g = checked(tmp_path, files, "use.py", 4)
    assert _counts(g, CALLEE_COUNTS) == {
        "callee_calls": 2,
        "callee_unsupported": 1,
        "callee_not_callable": 2,
    }


def test_shadowing(tmp_path: Path) -> None:
    source = (
        "def f(): pass\n"  # 1
        "def a(f):\n"  # 2
        "    return f()\n"  # 3 parameter
        "def b():\n"  # 4
        "    f = g\n"  # 5
        "    return f()\n"  # 6 local variable
        "def c():\n"  # 7
        "    def f(): pass\n"  # 8
        "    return f()\n"  # 9 the nested def
        "class K:\n"  # 10
        "    def f(self): pass\n"  # 11
        "    def m(self):\n"  # 12
        "        return f()\n"  # 13 class scope skipped: the module f
        "def d():\n"  # 14
        "    global f\n"  # 15
        "    return f()\n"  # 16
        "def e():\n"  # 17
        "    return [f() for f in fs]\n"  # 18 comprehension variable
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert _view(result.callers) == [(("m.py", 12, "method", "m", 13), [(13, 16, "f", False)])]
    assert result.summary.caller_unsupported == 6  # a: 1, b: 2, d: 1, e: 2 (as Step 47)
    assert _view(checked(tmp_path, {"m.py": source}, "m.py", 7).callees) == [
        (("m.py", 8, "function", "f", 8), [(9, 12, "f", False)])
    ]
    for line in (2, 4, 14, 17):
        assert _counts(checked(tmp_path, {"m.py": source}, "m.py", line), ("callee_unsupported",))


def test_recursion_appears_in_both_lists(tmp_path: Path) -> None:
    source = "def fact(n):\n    return n * fact(n - 1)\nfact(3)\n"
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert _view(result.callees) == [(("m.py", 1, "function", "fact", 2), [(2, 16, "fact", False)])]
    assert _view(result.callers) == [
        (("m.py", 1, "function", "fact", 2), [(2, 16, "fact", False)]),
        (("m.py", 1, "module", "m", 1), [(3, 1, "fact", False)]),
    ]
    assert "recursive" not in result.model_dump_json()


def test_constructor_calls_resolve_to_the_class(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "models.py": (
            "class User:\n"  # 1
            "    def __init__(self):\n"  # 2
            "        super().__init__()\n"  # 3
            "    @classmethod\n"  # 4
            "    def create(cls):\n"  # 5
            "        return cls()\n"  # 6
        ),
        "use.py": "import models\nfrom models import User\ndef make():\n    User(); models.User(); User.create()\n",
    }
    make = checked(tmp_path, files, "use.py", 3)
    assert _view(make.callees) == [
        (("models.py", 1, "class", "User", 6), [(4, 5, "User", False), (4, 20, "User", False)])
    ]
    assert _counts(make, CALLEE_COUNTS) == {"callee_calls": 2, "callee_unsupported": 1}
    init = checked(tmp_path, files, "models.py", 2)
    assert _counts(init, CALLEE_COUNTS) == {"callee_builtin": 1, "callee_unsupported": 1}
    assert init.callers is None
    user = checked(tmp_path, files, "models.py", 1)
    assert _view(user.callers) == [
        (("use.py", 3, "function", "make", 4), [(4, 5, "User", False), (4, 20, "User", False)])
    ]
    assert _view(user.callees) == []  # @classmethod belongs to the class body: a builtin
    assert _counts(user, CALLEE_COUNTS) == {"callee_builtin": 1}


def test_ambiguous_calls_are_listed_under_every_candidate(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": (
            "if X:\n"  # 1
            "    def f(): pass\n"  # 2
            "else:\n"  # 3
            "    def f(): pass\n"  # 4
            "def g():\n"  # 5
            "    f()\n"  # 6
            "    from pkg import thing\n"  # 7
            "    thing()\n"  # 8 pkg/__init__'s thing and the module pkg/thing.py
        ),
        "pkg/__init__.py": "def thing(): pass\n",
        "pkg/thing.py": "",
    }
    g = checked(tmp_path, files, "m.py", 5)
    assert _view(g.callees) == [
        (("m.py", 2, "function", "f", 2), [(6, 5, "f", True)]),
        (("m.py", 4, "function", "f", 4), [(6, 5, "f", True)]),
        (("pkg/__init__.py", 1, "function", "thing", 1), [(8, 5, "thing", True)]),
    ]
    assert _counts(g, CALLEE_COUNTS) == {"callee_calls": 2, "callee_ambiguous": 2}
    for line in (2, 4):
        result = checked(tmp_path, files, "m.py", line)
        assert _view(result.callers) == [(("m.py", 5, "function", "g", 8), [(6, 5, "f", True)])]
        assert (result.summary.caller_calls, result.summary.caller_ambiguous) == (1, 1)


def test_try_except_import_fallbacks_are_ambiguous(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "def thing():\n    pass\n",
        "b.py": "def thing():\n    pass\n",
        "m.py": "try:\n    from a import thing\nexcept ImportError:\n    from b import thing\ndef use():\n    thing()\n",
    }
    use = checked(tmp_path, files, "m.py", 5)
    assert [(g.callee.path, g.calls[0].ambiguous) for g in use.callees] == [
        ("a.py", True),
        ("b.py", True),
    ]
    assert use.summary.callee_calls == 1  # one site, two groups


# --- Z, AA, AB: counted calls ------------------------------------------------------------------


def test_dynamic_calls_are_counted_never_resolved(tmp_path: Path) -> None:
    source = (
        "from functools import partial\n"  # 1
        "def f(): pass\n"  # 2
        "def d(x): return f\n"  # 3
        "def target(o, n, x):\n"  # 4
        "    f()()\n"  # 5 outer dynamic, f
        "    x[0]()\n"  # 6
        "    getattr(o, n)()\n"  # 7 outer dynamic, getattr builtin
        "    globals()[n]()\n"  # 8 outer dynamic, globals builtin
        "    partial(f)()\n"  # 9 outer dynamic, partial unresolved (external)
        "    (lambda: 0)()\n"  # 10
        "    o.m()()\n"  # 11 outer dynamic, o.m unsupported
        "    @d(1)\n"  # 12 the application is dynamic, d(1) a call of d
        "    @x[0]\n"  # 13
        "    def inner(): pass\n"  # 14
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 4)
    assert _view(result.callees) == [
        (("m.py", 2, "function", "f", 2), [(5, 5, "f", False)]),
        (("m.py", 3, "function", "d", 3), [(12, 6, "d", False)]),
    ]
    assert _counts(result, CALLEE_COUNTS) == {
        "callee_calls": 2,
        "callee_builtin": 2,
        "callee_unresolved": 1,
        "callee_unsupported": 1,
        "callee_dynamic": 9,
    }
    f = checked(tmp_path, {"m.py": source}, "m.py", 2)
    assert _sites(f.callers) == [(5, 5)]  # partial(f) and "return f" are not calls of f
    assert f.summary.non_call_references == 2


def test_builtins(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": (
            "def a(x):\n"  # 1
            "    print(len(x)); str.join(',', x); dict.fromkeys(x)\n"  # 2
            "def b():\n"  # 3
            "    print = log\n"  # 4
            "    print()\n"  # 5 a variable: unsupported, not builtin
            "def len(x): pass\n"  # 6 a module-level def shadows the builtin
            "def c(x):\n"  # 7
            "    return len(x)\n"  # 8
        ),
        "s.py": "from m import *\ndef d():\n    print()\n",
    }
    assert _counts(checked(tmp_path, files, "m.py", 1), CALLEE_COUNTS) == {
        "callee_calls": 1,
        "callee_builtin": 3,  # len here is m.py's len (module scope)
    }
    assert _counts(checked(tmp_path, files, "m.py", 3), CALLEE_COUNTS) == {"callee_unsupported": 1}
    c = checked(tmp_path, files, "m.py", 7)
    assert _view(c.callees) == [(("m.py", 6, "function", "len", 6), [(8, 12, "len", False)])]
    assert _counts(checked(tmp_path, files, "s.py", 2), CALLEE_COUNTS) == {"callee_unsupported": 1}


def test_duplicate_calls_are_one_group(tmp_path: Path) -> None:
    source = "def f(x=None): return x\ndef g():\n    f(); f(f())\n    f()\n"
    result = checked(tmp_path, {"m.py": source}, "m.py", 2)
    assert _view(result.callees) == [
        (
            ("m.py", 1, "function", "f", 1),
            [(3, 5, "f", False), (3, 10, "f", False), (3, 12, "f", False), (4, 5, "f", False)],
        )
    ]
    assert result.callees[0].total_calls == 4 and result.summary.callee_calls == 4
    assert _view(checked(tmp_path, {"m.py": source}, "m.py", 1).callers) == [
        (
            ("m.py", 2, "function", "g", 4),
            [(3, 5, "f", False), (3, 10, "f", False), (3, 12, "f", False), (4, 5, "f", False)],
        )
    ]


# --- AC, AD, AE, AF, AG, AH, AI, AQ: units --------------------------------------------------------


def test_module_callers_and_root_init(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "t.py": "def t(): pass\n",
        "__init__.py": "from t import t\nt()\n",
        "pkg/__init__.py": "from t import t\nt()\n",
        "pkg/sub/mod.pyi": "from t import t\nt()\n",
        "pkg/sub/__init__.py": "",
        "z.py": "import t as m\nm.t()\n",
    }
    result = checked(tmp_path, files, "t.py", 1)
    assert [(loc[0], loc[2], loc[3], loc[1], loc[4]) for loc, _ in _view(result.callers)] == [
        ("__init__.py", "module", "__init__", 1, 1),  # a root __init__ names no module
        ("pkg/__init__.py", "module", "pkg", 1, 1),
        ("pkg/sub/mod.pyi", "module", "mod", 1, 1),
        ("z.py", "module", "z", 1, 1),
    ]


def test_nested_callers(tmp_path: Path) -> None:
    source = (
        "def t(): pass\n"  # 1
        "def outer():\n"  # 2
        "    t()\n"  # 3
        "    def middle():\n"  # 4
        "        def inner():\n"  # 5
        "            return lambda: t()\n"  # 6 the lambda belongs to inner
        "        t()\n"  # 7
        "    class Local:\n"  # 8
        "        x = t()\n"  # 9
        "        def m(self):\n"  # 10
        "            t()\n"  # 11
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert [(loc[1], loc[2], loc[3], sites) for loc, sites in _view(result.callers)] == [
        (2, "function", "outer", [(3, 5, "t", False)]),
        (4, "function", "middle", [(7, 9, "t", False)]),
        (5, "function", "inner", [(6, 28, "t", False)]),
        (8, "class", "Local", [(9, 13, "t", False)]),
        (10, "method", "m", [(11, 13, "t", False)]),
    ]
    assert _view(result.callers)[1][0] == ("m.py", 4, "function", "middle", 7)


def test_class_body_callers(tmp_path: Path) -> None:
    source = (
        "def t(): pass\n"  # 1
        "class A:\n"  # 2
        "    x = t()\n"  # 3
        "    if True:\n"  # 4
        "        y = t()\n"  # 5
        "    class B:\n"  # 6
        "        z = t()\n"  # 7
        "    def m(self, d=t()):\n"  # 8 the default: A's body
        "        t()\n"  # 9
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert [(loc[2], loc[3], _s) for loc, _s in _view(result.callers)] == [
        ("class", "A", [(3, 9, "t", False), (5, 13, "t", False), (8, 19, "t", False)]),
        ("class", "B", [(7, 13, "t", False)]),
        ("method", "m", [(9, 9, "t", False)]),
    ]


def test_decorators(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "deco.py": "def mark(f): return f\ndef factory(n): return mark\n",
        "m.py": (
            "import deco\n"  # 1
            "from deco import mark, factory\n"  # 2
            "@mark\n"  # 3 module
            "def t():\n"  # 4
            "    @deco.mark\n"  # 5 t: the decorator of a nested def is t's
            "    @factory(1)\n"  # 6 t: factory call plus one dynamic application
            "    def inner(): pass\n"  # 7
            "    return inner\n"  # 8
            "@mark\n"  # 9 module
            "class C:\n"  # 10
            "    @mark\n"  # 11 C's body
            "    def m(self): pass\n"  # 12
        ),
    }
    t = checked(tmp_path, files, "m.py", 4)
    assert _view(t.callees) == [
        (("deco.py", 1, "function", "mark", 1), [(5, 11, "mark", False)]),
        (("deco.py", 2, "function", "factory", 2), [(6, 6, "factory", False)]),
    ]
    assert _counts(t, CALLEE_COUNTS) == {"callee_calls": 2, "callee_dynamic": 1}
    mark = checked(tmp_path, files, "deco.py", 1)
    assert [(loc[2], loc[3], s) for loc, s in _view(mark.callers)] == [
        ("module", "m", [(3, 2, "mark", False), (9, 2, "mark", False)]),
        ("function", "t", [(5, 11, "mark", False)]),
        ("class", "C", [(11, 6, "mark", False)]),
    ]
    assert mark.summary.non_call_references == 2  # the import, factory's "return mark"


def test_annotations_and_defaults(tmp_path: Path) -> None:
    source = (
        "def f(): pass\n"  # 1
        "def g(x: f() = f(), *, y: f() = f()) -> f():\n"  # 2 all module's
        "    z: f() = 1\n"  # 3 g's (syntactic, though never evaluated)
        "v: f() = 2\n"  # 4 module
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert [(loc[2], [s[:2] for s in sites]) for loc, sites in _view(result.callers)] == [
        ("module", [(2, 10), (2, 16), (2, 27), (2, 33), (2, 41), (4, 4)]),  # line 1 first
        ("function", [(3, 8)]),
    ]
    g = checked(tmp_path, {"m.py": source}, "m.py", 2)
    assert _sites(g.callees) == [(3, 8)]


def test_lambdas(tmp_path: Path) -> None:
    source = (
        "def f(*a): pass\n"  # 1
        "h = lambda: f()\n"  # 2 module
        "def g():\n"  # 3
        "    return lambda x=f(): f(x, lambda: f())\n"  # 4 all g's
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert [(loc[3], [s[:2] for s in sites]) for loc, sites in _view(result.callers)] == [
        ("m", [(2, 13)]),
        ("g", [(4, 21), (4, 26), (4, 39)]),
    ]


def test_comprehensions(tmp_path: Path) -> None:
    source = (
        "def f(x=0): return [x]\n"  # 1
        "def g():\n"  # 2
        "    a = [f(x) for x in f()]\n"  # 3
        "    b = {f(k): f(v) for k, v in ()}\n"  # 4
        "    c = (f(y) for y in () if f(y))\n"  # 5
        "class K:\n"  # 6
        "    items = [f(x) for x in f()]\n"  # 7 the class body (syntactic), class scope skipped
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert [(loc[3], [s[:2] for s in sites]) for loc, sites in _view(result.callers)] == [
        ("g", [(3, 10), (3, 24), (4, 10), (4, 16), (5, 10), (5, 30)]),
        ("K", [(7, 14), (7, 28)]),
    ]


def test_pep_695_type_parameters(tmp_path: Path) -> None:
    source = (
        "def bound(): return int\n"  # 1
        "def outer():\n"  # 2
        "    def generic[T: bound()](x: T) -> T:\n"  # 3 the bound has no unit
        "        return x\n"  # 4
        "    class Box[U: bound()]:\n"  # 5 no unit either
        "        pass\n"  # 6
        "    type Alias[V: bound()] = bound()\n"  # 7 a type statement: outer's
        "    return generic\n"  # 8
    )
    result = checked(tmp_path, {"m.py": source}, "m.py", 2)
    assert _view(result.callees) == [
        (("m.py", 1, "function", "bound", 1), [(7, 19, "bound", False), (7, 30, "bound", False)])
    ]
    assert _counts(result, CALLEE_COUNTS) == {"callee_calls": 2}
    for line in (3, 5):
        assert _view(checked(tmp_path, {"m.py": source}, "m.py", line).callees) == []
    bound = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert _sites(bound.callers) == [(7, 19), (7, 30)]


# --- AJ: ordering ----------------------------------------------------------------------------------


def test_order_is_path_line_kind_name_and_line_column(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "t.py": "def z(): t()\ndef t(): pass\nt()\n",
        "a.py": "from t import t\n\u00e9 = 1; t(); t()\n",
        "B.py": "from t import t\nt()\n",
        "\u00e9.py": "from t import t\nt()\n",
        "c.py": "from t import t\ndef g():\n    t()\nt()\n",  # g's call is found first
    }
    result = checked(tmp_path, files, "t.py", 2)
    keys = [loc[:4] for loc, _ in _view(result.callers)]
    assert keys == [
        ("B.py", 1, "module", "B"),
        ("a.py", 1, "module", "a"),
        ("c.py", 1, "module", "c"),
        ("c.py", 2, "function", "g"),
        ("t.py", 1, "function", "z"),  # same path and line: "function" < "module"
        ("t.py", 1, "module", "t"),
        ("\u00e9.py", 1, "module", "\u00e9"),
    ]
    assert _sites(result.callers[1:2]) == [(2, 8), (2, 13)]  # code points, not bytes
    callee_files: dict[str, str | bytes] = {
        "z.py": "\n\n\n\ndef late(): pass\n",
        "b.py": "def early(): pass\n",
        "m.py": "from z import late\nfrom b import early\ndef go():\n    late(); early()\n    early()\n",
    }
    go = checked(tmp_path, callee_files, "m.py", 3)
    assert [(g.callee.path, _sites([g])) for g in go.callees] == [
        ("b.py", [(4, 13), (5, 5)]),
        ("z.py", [(4, 5)]),
    ]


def test_unicode_tabs_crlf_and_nfkc(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def User():\n    pass\n",
        "u.py": "from m import \uff35ser\nx = '\U0001d4b3\u00e9'; \uff35ser()\n",
        "t.py": b"from m import User\r\nif 1:\r\n\tUser()\r\n",
    }
    result = checked(tmp_path, files, "m.py", 1)
    assert _view(result.callers) == [
        (("t.py", 1, "module", "t", 1), [(3, 2, "User", False)]),
        (("u.py", 1, "module", "u", 1), [(2, 11, "User", False)]),
    ]


def test_long_names_are_capped(tmp_path: Path) -> None:
    name = "c" * 250
    source = f"def t(): pass\ndef {name}():\n    t()\n"
    result = checked(tmp_path, {"m.py": source}, "m.py", 1)
    assert result.callers[0].caller.name == "c" * 200


# --- AK: limits -----------------------------------------------------------------------------------


GROUPS = "def t(): pass\n" + "".join(f"def c{i}():\n    t(); t(); t()\n" for i in range(3))


def test_caller_group_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_CALLERS == 200
    monkeypatch.setattr(code_call_hierarchy, "MAX_CALLERS", 3)
    result = hierarchy(tmp_path, {"m.py": GROUPS}, "m.py", 1)
    assert (len(result.callers), result.callers_truncated) == (3, False)
    monkeypatch.setattr(code_call_hierarchy, "MAX_CALLERS", 2)
    result = hierarchy(tmp_path, {"m.py": GROUPS}, "m.py", 1)
    assert [g.caller.name for g in result.callers] == ["c0", "c1"]
    assert (result.callers_truncated, result.callees_truncated) == (True, False)
    assert result.summary.caller_calls == 9  # counted before the cap


def test_callee_group_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_CALLEES == 200
    source = "def a(): pass\ndef b(): pass\ndef c(): pass\ndef t():\n    c(); b(); a()\n"
    monkeypatch.setattr(code_call_hierarchy, "MAX_CALLEES", 3)
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 4)
    assert (len(result.callees), result.callees_truncated) == (3, False)
    monkeypatch.setattr(code_call_hierarchy, "MAX_CALLEES", 2)
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 4)
    assert [g.callee.name for g in result.callees] == ["a", "b"]  # by line, not call order
    assert (result.callees_truncated, result.callers_truncated) == (True, False)
    assert result.summary.callee_calls == 3


def test_sites_per_group_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_SITES_PER_GROUP == 50
    monkeypatch.setattr(code_call_hierarchy, "MAX_SITES_PER_GROUP", 3)
    result = hierarchy(tmp_path, {"m.py": GROUPS}, "m.py", 1)
    assert ([len(g.calls) for g in result.callers], result.callers_truncated) == ([3, 3, 3], False)
    monkeypatch.setattr(code_call_hierarchy, "MAX_SITES_PER_GROUP", 2)
    result = hierarchy(tmp_path, {"m.py": GROUPS}, "m.py", 1)
    assert [(g.total_calls, _sites([g])) for g in result.callers] == [
        (3, [(3, 5), (3, 10)]),
        (3, [(5, 5), (5, 10)]),
        (3, [(7, 5), (7, 10)]),
    ]
    assert result.callers_truncated is True
    callee = hierarchy(tmp_path, {"m.py": GROUPS}, "m.py", 2)
    assert (callee.callees[0].total_calls, len(callee.callees[0].calls)) == (3, 2)
    assert (callee.callees_truncated, callee.callers_truncated) == (True, False)


def test_total_sites_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_SITES == 1000
    monkeypatch.setattr(code_call_hierarchy, "MAX_SITES", 9)
    result = hierarchy(tmp_path, {"m.py": GROUPS}, "m.py", 1)
    assert (len(_sites(result.callers)), result.callers_truncated) == (9, False)
    monkeypatch.setattr(code_call_hierarchy, "MAX_SITES", 4)
    result = hierarchy(tmp_path, {"m.py": GROUPS}, "m.py", 1)
    assert [(g.caller.name, g.total_calls, len(g.calls)) for g in result.callers] == [
        ("c0", 3, 3),
        ("c1", 3, 1),
    ]
    assert result.callers_truncated is True
    monkeypatch.setattr(code_call_hierarchy, "MAX_SITES", 3)  # exactly one group
    result = hierarchy(tmp_path, {"m.py": GROUPS}, "m.py", 1)
    assert ([g.caller.name for g in result.callers], result.callers_truncated) == (["c0"], True)


def test_callee_call_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_CALLEE_CALLS == 5000
    source = "def a(): pass\ndef t():\n    a(); x[0](); a()\n"
    monkeypatch.setattr(code_call_hierarchy, "MAX_CALLEE_CALLS", 3)
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 2)
    assert (_sites(result.callees), result.callees_truncated) == ([(3, 5), (3, 18)], False)
    monkeypatch.setattr(code_call_hierarchy, "MAX_CALLEE_CALLS", 2)  # source order
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 2)
    assert (_sites(result.callees), result.callees_truncated) == ([(3, 5)], True)
    assert _counts(result, CALLEE_COUNTS) == {"callee_calls": 1, "callee_dynamic": 1}
    assert result.callers_truncated is False


def test_index_truncation_sets_both(tmp_path: Path) -> None:
    result = hierarchy(tmp_path, REPO, "auth.py", 1, index_truncated=True)
    assert (result.callers_truncated, result.callees_truncated) == (True, True)
    assert _view(result.callers) == AUTHENTICATE_CALLERS
    method = hierarchy(tmp_path, REPO, "app/views.py", 25, index_truncated=True)
    assert (method.callers_truncated, method.callees_truncated) == (None, True)


def test_read_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_spy: list[str]) -> None:
    assert MAX_SCANNED_BYTES == 64 * 1024 * 1024
    files: dict[str, str | bytes] = {
        "m.py": "def t(): pass\n",
        "n.py": "from m import t\nt()\n",
        "o.py": "from m import t\nt()\n",
    }
    total = sum(len(str(text).encode()) for text in files.values())
    monkeypatch.setattr(code_call_hierarchy, "MAX_SCANNED_BYTES", total)
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert (len(result.callers), result.callers_truncated) == (2, False)
    monkeypatch.setattr(code_call_hierarchy, "MAX_SCANNED_BYTES", total - 1)
    read_spy.clear()
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert ([g.caller.path for g in result.callers], result.callers_truncated) == (["n.py"], True)
    assert result.callees_truncated is False
    assert read_spy == ["m.py", "n.py"]  # o.py is never opened


def test_reads_stop_at_the_first_file_over_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_spy: list[str]
) -> None:
    files: dict[str, str | bytes] = {
        "big.py": "from m import t\nt()\n" + "#" * 100,
        "m.py": "def t(): pass\n",
        "z.py": "from m import t\nt()\n",
    }
    monkeypatch.setattr(
        code_call_hierarchy, "MAX_SCANNED_BYTES", len(files["m.py"]) + len(files["z.py"])
    )
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert read_spy == ["m.py"]  # as Step 47: z.py is not read after big.py does not fit
    assert (result.callers, result.callers_truncated) == ([], True)


def test_callee_budget_exhaustion_is_reported_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_spy: list[str]
) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "from z import helper\ndef outer():\n    def t():\n        return helper()\n    return t()\n",
        "z.py": "def helper(): pass\n",
    }
    monkeypatch.setattr(code_call_hierarchy, "MAX_SCANNED_BYTES", len(files["m.py"]))
    result = hierarchy(tmp_path, files, "m.py", 3)  # z.py cannot be read for helper
    assert (result.callees, result.summary.callee_unsupported) == ([], 1)
    assert (result.callees_truncated, result.callers_truncated) == (True, False)
    assert _sites(result.callers) == [(5, 12)]
    assert read_spy == ["m.py"]


def test_parse_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_PARSED_BYTES == 4 * 1024 * 1024
    files: dict[str, str | bytes] = {
        "m.py": "def t(): pass\n",
        "n.py": "from m import t\nt()\n",
        "skipped.py": "x = 1\n" * 50,  # never parsed, so not counted
        "z.py": "from m import t\nt()\n",
    }
    parsed = sum(len(str(files[p]).encode()) for p in ("m.py", "n.py", "z.py"))
    monkeypatch.setattr(code_call_hierarchy, "MAX_PARSED_BYTES", parsed)
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert (len(result.callers), result.callers_truncated) == (2, False)
    monkeypatch.setattr(code_call_hierarchy, "MAX_PARSED_BYTES", parsed - 1)
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert ([g.caller.path for g in result.callers], result.callers_truncated) == (["n.py"], True)


def test_candidate_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "def f():\n    pass\nf()\nf()\n"  # three candidates, the def name included
    monkeypatch.setattr(code_references, "MAX_CANDIDATES", 3)
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 1)
    assert (_sites(result.callers), result.callers_truncated) == ([(3, 1), (4, 1)], False)
    monkeypatch.setattr(code_references, "MAX_CANDIDATES", 2)
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 1)
    assert (_sites(result.callers), result.callers_truncated) == ([(3, 1)], True)
    assert result.callees_truncated is False


def test_alias_name_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def t(): pass\n",
        "n.py": "from m import t as a\nfrom m import t as b\na(); b()\n",
    }
    monkeypatch.setattr(code_references, "MAX_ALIAS_NAMES", 3)
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert (_sites(result.callers), result.callers_truncated) == ([(3, 1), (3, 6)], False)
    monkeypatch.setattr(code_references, "MAX_ALIAS_NAMES", 2)  # b (sorted last) is dropped
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert (_sites(result.callers), result.callers_truncated) == ([(3, 1)], True)


def test_definition_list_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "if X:\n    def f(): pass\nelse:\n    def f(): pass\n" "def g():\n    f()\n"
    monkeypatch.setattr(code_definition, "MAX_DEFINITIONS", 1)
    first = hierarchy(tmp_path, {"m.py": source}, "m.py", 2)
    assert (_view(first.callers), first.callers_truncated) == (
        [(("m.py", 5, "function", "g", 6), [(6, 5, "f", True)])],
        False,
    )
    second = hierarchy(tmp_path, {"m.py": source}, "m.py", 4)  # cut from Step 45's list
    assert (second.callers, second.callers_truncated) == ([], True)
    g = hierarchy(tmp_path, {"m.py": source}, "m.py", 5)
    assert (_view(g.callees), g.callees_truncated) == (
        [(("m.py", 2, "function", "f", 2), [(6, 5, "f", True)])],
        True,
    )


def test_module_budget_is_per_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "def fa(): pass\n",
        "b.py": "def fb(): pass\n",
        "m.py": "def t():\n    from a import fa\n    from b import fb\n    fa(); fb()\n",
    }
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 1)  # each call reads one module
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert [g.callee.path for g in result.callees] == ["a.py", "b.py"]
    assert result.callees_truncated is False
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 0)
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert (result.callees, result.summary.callee_unsupported, result.callees_truncated) == (
        [],
        2,
        True,
    )


CHAIN: dict[str, str | bytes] = {
    "h/one.py": "class X:\n    pass\n",
    "h/two.py": "from h.one import X as X2\n",
    "h/three.py": "from h.two import X2 as X3\n",
    "main.py": "from h.three import X3 as X4\nX4()\ndef make():\n    return X4()\n",
}


def test_module_budget_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 3)  # X4 reads three, two, one
    result = checked(tmp_path, CHAIN, "h/one.py", 1)
    assert _sites(result.callers) == [(2, 1), (4, 12)] and result.callers_truncated is False
    make = checked(tmp_path, CHAIN, "main.py", 3)
    assert [g.callee.path for g in make.callees] == ["h/one.py"]
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 2)
    result = hierarchy(tmp_path, CHAIN, "h/one.py", 1)
    assert (result.callers, result.callers_truncated) == ([], True)
    assert result.summary.caller_unsupported == 4  # main.py's X3, X4 (import) and two X4()
    root, rows = _repo(tmp_path, CHAIN)
    refs = analyze_references(root, rows[0], rows, 1)
    assert (refs.summary.unsupported, refs.truncated) == (result.summary.caller_unsupported, True)
    make = hierarchy(tmp_path, CHAIN, "main.py", 3)
    assert (make.callees, make.summary.callee_unsupported, make.callees_truncated) == ([], 1, True)


def test_import_hops_follow_step_45(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_definition, "MAX_IMPORT_HOPS", 2)
    result = checked(tmp_path, CHAIN, "h/one.py", 1)
    assert result.callers == []
    make = checked(tmp_path, CHAIN, "main.py", 3)
    assert (make.callees, make.summary.callee_unsupported) == ([], 1)
    monkeypatch.setattr(code_definition, "MAX_IMPORT_HOPS", 4)
    assert _sites(checked(tmp_path, CHAIN, "h/one.py", 1).callers) == [(2, 1), (4, 12)]


# --- Reads, phases and counts -------------------------------------------------------------------


def test_phase_order_and_one_scan(tmp_path: Path, read_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "t.py": "from z import helper\ndef t():\n    return helper()\n",
        "a.py": "from t import t\nt()\n",
        "z.py": "def helper(): pass\n",
    }
    result = hierarchy(tmp_path, files, "t.py", 2)
    assert (_sites(result.callees), _sites(result.callers)) == ([(3, 12)], [(2, 1)])
    assert read_spy == ["t.py", "z.py", "a.py"]  # target, callees, then callers; each once


def test_only_snapshot_files_are_read(tmp_path: Path, read_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def t(): pass\n",
        "n.py": "from stray import t as s\nfrom m import t as u\ndef g():\n    s(); u()\n",
    }
    root, rows = _repo(tmp_path, files)
    (root / "stray.py").write_bytes(b"from m import t\nt()\n")  # not in the snapshot
    result = analyze_call_hierarchy(root, rows[0], rows, 1)
    assert _sites(result.callers) == [(4, 10)]
    g = analyze_call_hierarchy(root, rows[1], rows, 3)
    assert ([g_.callee.path for g_ in g.callees], g.summary.callee_unresolved) == (["m.py"], 1)
    assert "stray.py" not in read_spy


def test_files_that_cannot_be_analyzed_are_counted(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def t():\n    pass\n",
        "bad.py": "from m import t\ndef (:\n",
        "bin.py": b"t\x00",
        "big.py": b"t = 1\n" + b"#" * MAX_OUTLINE_BYTES,
        "huge.py": b"#" * (MAX_CONTENT_BYTES + 1),
    }
    result = hierarchy(tmp_path, files, "m.py", 1)
    summary = result.summary.model_dump()
    assert {k: summary[k] for k in ("files_scanned", "too_large", "binary", "syntax_error")} == {
        "files_scanned": 4,  # huge.py is never opened
        "too_large": 2,
        "binary": 1,
        "syntax_error": 1,
    }
    assert (result.callers_truncated, result.callees_truncated) == (False, False)


# --- AS: safety ------------------------------------------------------------------------------------


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "import os, subprocess, socket\n"
        "os.system('echo')\nsubprocess.run(['echo'])\n"
        "socket.create_connection(('example.invalid', 80))\n"
        "__import__('os').system('echo')\neval('1')\nexec('x = 1')\n"
        "def deco(f):\n    open('side-effect', 'w')\n    return f\n"
        "@deco\ndef target():\n    os.system('echo'); eval('2')\n"
    )
    files: dict[str, str | bytes] = {
        "m.py": "from evil import target\ndef main():\n    target()\n",
        "evil.py": evil,
    }
    root, rows = _repo(tmp_path, files)
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
    result = analyze_call_hierarchy(root, _row(rows, "evil.py"), rows, 13)
    main = analyze_call_hierarchy(root, _row(rows, "m.py"), rows, 2)
    monkeypatch.undo()
    assert _sites(result.callers) == [(3, 5)]
    assert _counts(result, CALLEE_COUNTS) == {"callee_unresolved": 1, "callee_builtin": 1}
    assert [g.callee.name for g in main.callees] == ["target"]
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


# --- AL, AM, AN: snapshot ----------------------------------------------------------------------------


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def t(): pass\n",
        "n.py": "from m import t\nt()\n",
    }
    root, rows = _repo(tmp_path, files)
    (root / "n.py").write_bytes(b"from m import t\nt()  # changed\n")  # a scanned file
    with pytest.raises(FileContentError) as raised:
        analyze_call_hierarchy(root, rows[0], rows, 1)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "n.py").unlink()  # missing
    with pytest.raises(FileContentError):
        analyze_call_hierarchy(root, rows[0], rows, 1)
    root, rows = _repo(tmp_path, files)
    (root / "m.py").write_bytes(b"def t(): pass  # changed\n")  # the target
    with pytest.raises(FileContentError) as raised:
        analyze_call_hierarchy(root, rows[0], rows, 1)
    assert raised.value.status_code == 409


def test_a_module_read_only_for_a_callee_is_verified(tmp_path: Path, read_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def outer():\n    from lib import helper\n    def t():\n        return helper()\n    return t\n",
        "lib.py": "def helper():\n    pass\n",
    }
    root, rows = _repo(tmp_path, files)
    target = _row(rows, "m.py")
    result = analyze_call_hierarchy(root, target, rows, 3)
    assert [g.callee.path for g in result.callees] == ["lib.py"]
    assert read_spy == ["m.py", "lib.py"]
    (root / "lib.py").write_bytes(b"def helper():\n    pass  # changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_call_hierarchy(root, target, rows, 3)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_symlinked_module_fails_the_request(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "from lib import helper\ndef t():\n    return helper()\n",
        "lib.py": "def helper():\n    pass\n",
    }
    root, rows = _repo(tmp_path, files)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"def helper():\n    pass\n")
    (root / "lib.py").unlink()
    try:
        os.symlink(outside, root / "lib.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    with pytest.raises(FileContentError) as raised:
        analyze_call_hierarchy(root, _row(rows, "m.py"), rows, 2)
    assert raised.value.status_code == 409


def test_junctioned_package_fails_the_request(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    files: dict[str, str | bytes] = {
        "m.py": "def t(): pass\n",
        "pkg/use.py": "from m import t\nt()\n",
    }
    root, rows = _repo(tmp_path, files)
    outside = tmp_path / "outside"
    shutil.copytree(root / "pkg", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pkg")
    _winapi.CreateJunction(str(outside), str(root / "pkg"))  # type: ignore[attr-defined]
    with pytest.raises(FileContentError) as raised:
        analyze_call_hierarchy(root, _row(rows, "m.py"), rows, 1)
    assert raised.value.status_code == 409


# --- The Step 45 oracle sweep and Step 47 parity -----------------------------------------------------


SWEEP: dict[str, dict[str, str | bytes]] = {
    "repo": REPO,
    "chain": CHAIN,
    "groups": {"m.py": GROUPS},
    "dynamic": {
        "m.py": (
            "from functools import partial\ndef f(): pass\ndef d(x): return f\n"
            "def target(o, n, x):\n    f()()\n    x[0]()\n    getattr(o, n)()\n"
            "    globals()[n]()\n    partial(f)()\n    (lambda: 0)()\n    o.m()()\n"
            "    @d(1)\n    @x[0]\n    def inner(): pass\n"
        )
    },
    "units": {
        "deco.py": "def mark(f): return f\ndef factory(n): return mark\n",
        "m.py": (
            "import deco\nfrom deco import mark, factory\n@mark\ndef t():\n    @deco.mark\n"
            "    @factory(1)\n    def inner(): pass\n    return inner\n@mark\nclass C:\n"
            "    @mark\n    def m(self, d=t()): return [t() for _ in d] + [lambda: t()]\n"
            "    x: t() = t()\n    class D:\n        y = mark(t)\n"
            "def g(x: t() = t(), *, y: t() = t()) -> t():\n    z: t() = 1\n"
            "    def h():\n        return t(), mark(h)\n    return h()\n"
        ),
    },
    "shadow": {
        "m.py": (
            "def f(): pass\ndef a(f):\n    return f()\ndef b():\n    f = g\n    return f()\n"
            "def c():\n    def f(): pass\n    return f()\nclass K:\n    def f(self): pass\n"
            "    def m(self):\n        return f()\ndef d():\n    global f\n    return f()\n"
            "def e():\n    return [f() for f in fs]\ndef print(): pass\ndef p():\n"
            "    print(); len(()); str.join(',', ())\n"
        ),
        "s.py": "from m import *\ndef q():\n    print(); f()\n",
    },
    "pep695": {
        "m.py": (
            "def bound(): return int\ndef outer():\n    def generic[T: bound()](x: T) -> T:\n"
            "        return bound()\n    class Box[U: bound()]:\n        v = bound()\n"
            "    type Alias[V: bound()] = bound()\n    return generic\n"
        )
    },
    "mixed": {
        "pkg/__init__.py": "def thing(): pass\nfrom . import util\n",
        "pkg/thing.py": "",
        "pkg/util.py": "import pkg\ndef go():\n    pkg.thing(); pkg.util.go(); thing()\n",
        "m.py": (
            "from pkg import thing, util\nimport pkg.util as u\n"
            "class C:\n    def m(self):\n        thing(); u.go(); self.m(); C()\n"
            "    @staticmethod\n    def s(): return [thing() for _ in u.go()]\n"
            "def f(x=thing()):\n    async def g(): await f(); return lambda: g()\n    return g\n"
            "type T = f()\n"
        ),
    },
}


def _definition_lines(text: str) -> list[int]:
    tree = ast.parse(text)
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


@pytest.mark.parametrize("fixture", sorted(SWEEP))
def test_step_45_oracle_sweep(tmp_path: Path, fixture: str) -> None:
    """Every definition of every file: callers, callees and counts are exactly Step 45's
    answers at the call sites found by an independent walk; callers are Step 47's."""
    files = SWEEP[fixture]
    checked_targets = 0
    for path, content in sorted(files.items()):
        if not path.endswith(".py"):
            continue
        text = content if isinstance(content, str) else content.decode()
        for line in _definition_lines(text):
            checked(tmp_path, files, path, line)
            checked_targets += 1
    assert checked_targets >= 2


def test_step_47_parity(tmp_path: Path) -> None:
    """Callers are exactly Step 47's references whose token is a call site, with the same
    names, ambiguity, unsupported count, file counts and truncation."""
    for files, path, line in ((REPO, "auth.py", 1), (REPO, "auth.py", 9), (CHAIN, "h/one.py", 1)):
        root, rows = _repo(tmp_path, files)
        target = _row(rows, path)
        result = analyze_call_hierarchy(root, target, rows, line)
        refs = analyze_references(root, target, rows, line)
        calls = {(g.caller.path, c.line, c.column) for g in result.callers for c in g.calls}
        assert calls <= {(r.path, r.line, r.column) for r in refs.references}
        assert not any(
            r.context == "import" for r in refs.references if (r.path, r.line, r.column) in calls
        )
        file_counts = ("files_scanned", "too_large", "binary", "syntax_error")
        assert {k: getattr(result.summary, k) for k in file_counts} == {
            k: getattr(refs.summary, k) for k in file_counts
        }
        assert (
            refs.summary.references
            == result.summary.caller_calls + result.summary.non_call_references
        )
        assert refs.summary.unsupported == result.summary.caller_unsupported
        assert refs.truncated == result.callers_truncated


def test_nothing_is_cached_across_requests(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def t(): pass\n",
        "n.py": "from m import t\ndef g():\n    t()\n",
    }
    root, rows = _repo(tmp_path, files)
    first = analyze_call_hierarchy(root, rows[0], rows, 1)
    assert _sites(first.callers) == [(3, 5)]
    changed = b"from m import t\ndef g():\n    t = None\n    t()\n"  # now a local variable
    (root / "n.py").write_bytes(changed)
    rows[1] = SimpleNamespace(
        path="n.py", size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest()
    )
    second = analyze_call_hierarchy(root, rows[0], rows, 1)
    assert (second.callers, second.summary.caller_unsupported) == ([], 2)


# --- Review regressions: memo keys, memo contents, orders and caps ------------------------------


def test_memo_keys_tell_chains_and_names_apart(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "pkg/__init__.py": "def f(): pass\n",
        "pkg/mod.py": "def f(): pass\n",
        "pkg/a.py": "def f(): pass\n",
        "lib.py": "def f(): pass\ndef g(): pass\n",
        "use.py": (
            "import pkg.mod\n"  # 1
            "import pkg.a\n"  # 2
            "import lib\n"  # 3
            "def t():\n"  # 4
            "    pkg.f(); pkg.mod.f(); pkg.a.f()\n"  # 5 one root, three chains
            "    lib.f(); lib.g()\n"  # 6 one chain, two names
        ),
    }
    result = checked(tmp_path, files, "use.py", 4)
    assert [(g.callee.path, g.callee.name, _sites([g])) for g in result.callees] == [
        ("lib.py", "f", [(6, 9)]),
        ("lib.py", "g", [(6, 18)]),
        ("pkg/__init__.py", "f", [(5, 9)]),
        ("pkg/a.py", "f", [(5, 33)]),
        ("pkg/mod.py", "f", [(5, 22)]),
    ]


def test_memo_keys_tell_root_bindings_apart(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "def f(): pass\n",
        "use.py": "import a as x\ndef g():\n    x.f()\n    return [x.f() for x in ()]\n",
    }
    result = checked(tmp_path, files, "use.py", 2)
    assert _view(result.callees) == [(("a.py", 1, "function", "f", 1), [(3, 7, "f", False)])]
    assert _counts(result, CALLEE_COUNTS) == {"callee_calls": 1, "callee_unsupported": 1}


def test_memo_keys_tell_files_apart(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "t.py": "def t(): pass\n",
        "a.py": "from os import *\nt()\nx.t()\n",  # a star import: unsupported
        "b.py": "t()\nx.t()\n",  # unbound, no star import: not found
    }
    result = checked(tmp_path, files, "t.py", 1)
    assert (result.callers, result.summary.caller_unsupported) == ([], 2)


def test_resolutions_are_memoized_per_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def g(): pass\ndef h(): pass\n",
        "t.py": "import m\nfrom m import g\ndef t():\n    g(); g(); m.h(); m.h()\n",
        "c.py": "from t import t\ndef c1():\n    t(); t()\ndef c2():\n    t()\n",
    }
    resolved: list[tuple[str, int, int]] = []
    real = code_call_hierarchy._resolve

    def counting(resolver: Any, source: Any, path: str, token: Any) -> Any:
        resolved.append((path, token.line, token.column + 1))
        return real(resolver, source, path, token)

    monkeypatch.setattr(code_call_hierarchy, "_resolve", counting)
    result = hierarchy(tmp_path, files, "t.py", 3)
    assert [g.total_calls for g in result.callees] == [2, 2]
    assert _sites(result.callers) == [(3, 5), (3, 10), (5, 5)]
    calls = [r for r in resolved if (r[0], r[1]) in (("t.py", 4), ("c.py", 3), ("c.py", 5))]
    assert calls == [("t.py", 4, 5), ("t.py", 4, 17), ("c.py", 3, 5)]  # once per memo key
    imports = [r for r in resolved if (r[0], r[1]) == ("c.py", 1)]
    assert imports == [("c.py", 1, 6), ("c.py", 1, 15)]  # import tokens: every time
    resolved.clear()
    hierarchy(tmp_path, files, "t.py", 3)  # a new request resolves again
    assert ("t.py", 4, 5) in resolved and ("c.py", 3, 5) in resolved


def test_budget_answers_are_memoized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def g(): pass\n",
        "t.py": "from m import g\ndef t():\n    g(); g()\n",
    }
    resolved: list[int] = []
    real = code_call_hierarchy._resolve

    def counting(resolver: Any, source: Any, path: str, token: Any) -> Any:
        resolved.append(token.line)
        return real(resolver, source, path, token)

    monkeypatch.setattr(code_call_hierarchy, "_resolve", counting)
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 0)
    result = hierarchy(tmp_path, files, "t.py", 2)
    assert (result.callees, result.summary.callee_unsupported) == ([], 2)
    assert result.callees_truncated is True
    assert resolved.count(3) == 1  # the second g() replays the budget answer


def test_a_reused_answer_keeps_its_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "import mod\ndef authenticate():\n    mod.authenticate()\nmod.authenticate()\n"
    files: dict[str, str | bytes] = {"t.py": target, "mod.py": "x = 1\n"}
    monkeypatch.setattr(code_call_hierarchy, "MAX_PARSED_BYTES", len(target))
    result = hierarchy(tmp_path, files, "t.py", 2)  # mod.py cannot be parsed
    assert (result.callees, result.callees_truncated) == ([], True)
    assert (result.callers, result.callers_truncated) == ([], True)
    root, rows = _repo(tmp_path, files)
    monkeypatch.setattr(code_references, "MAX_PARSED_BYTES", len(target))
    refs = analyze_references(root, _row(rows, "t.py"), rows, 2)
    assert refs.truncated is True
    assert refs.summary.unsupported == result.summary.caller_unsupported


def _memo(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture every request's _Resolutions, to look at what its memo keeps."""
    seen: list[Any] = []
    real_init = code_call_hierarchy._Resolutions.__init__

    def init(self: Any, *args: Any) -> None:
        real_init(self, *args)
        seen.append(self)

    monkeypatch.setattr(code_call_hierarchy._Resolutions, "__init__", init)
    return seen


def test_memo_entries_keep_only_step_45s_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def f(): pass\n" * 30,
        "t.py": "from m import f\ndef t():\n    f(); f()\n",
    }
    seen = _memo(monkeypatch)
    result = hierarchy(tmp_path, files, "t.py", 2)
    assert len(result.callees) == 20 and result.callees_truncated is True
    answers = [entry[0] for r in seen for entry in r.memo.values()]
    assert answers and all(len(a.definitions) <= 20 and len(a.listed) <= 20 for a in answers)
    assert any(a.total == 30 for a in answers)


def test_long_attribute_chains_are_not_memoized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep = "a" + ".x" * 40
    files: dict[str, str | bytes] = {
        "a.py": "def f(): pass\n",
        "t.py": f"import a\ndef t():\n    {deep}.f(); {deep}.f(); a.x.f()\n",
    }
    seen = _memo(monkeypatch)
    result = hierarchy(tmp_path, files, "t.py", 2)
    assert result.summary.callee_unsupported == 3  # the chains are not modules
    keys = [key for r in seen for key in r.memo if key[1] == "attribute"]
    assert keys and all(len(key[4]) <= code_call_hierarchy.MAX_MEMO_CHAIN for key in keys)


def test_an_unindexed_target_has_callers_from_its_own_file(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, REPO)
    target = _row(rows, "auth.py")
    indexed = [r for r in rows if r.path != "auth.py"]
    result = analyze_call_hierarchy(root, target, indexed, 1, index_truncated=True)
    assert _view(result.callers) == AUTHENTICATE_CALLERS[-2:]
    assert (result.callers_truncated, result.callees_truncated) == (True, True)


def test_callee_side_unicode(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "lib.py": "def f(): pass\n",
        "m.py": "def f(): pass\n",
        "u.py": (
            "import lib\nimport m\ndef f(): pass\ndef foo(): pass\ndef t():\n"
            "    \u00e9 = 1; f(); lib.f()\n    m.\uff46(); \uff46oo()\n"
        ),
    }
    result = checked(tmp_path, files, "u.py", 5)
    assert _view(result.callees) == [
        (("lib.py", 1, "function", "f", 1), [(6, 21, "f", False)]),
        (("m.py", 1, "function", "f", 1), [(7, 7, "f", False)]),
        (("u.py", 3, "function", "f", 3), [(6, 12, "f", False)]),
        (("u.py", 4, "function", "foo", 4), [(7, 12, "foo", False)]),
    ]


def test_unsupported_wins_over_definitions(tmp_path: Path) -> None:
    variable = {
        "pkg/__init__.py": "thing = 1\n",
        "pkg/thing.py": "",
        "m.py": "from pkg import thing\ndef t():\n    thing()\n",
    }
    t = checked(tmp_path, variable, "m.py", 2)
    assert (t.callees, _counts(t, CALLEE_COUNTS)) == ([], {"callee_unsupported": 1})
    fallback: dict[str, str | bytes] = {
        "a.py": "def t():\n    pass\n",
        "b.py": "t = 1\n",
        "m.py": "try:\n    from a import t\nexcept ImportError:\n    from b import t\nt()\n",
    }
    a = checked(tmp_path, fallback, "a.py", 1)
    assert (a.callers, a.summary.caller_unsupported) == ([], 3)


def test_callee_cut_is_in_source_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = (
        "def a(): pass\ndef b(): pass\ndef foo(x): pass\ndef t():\n"
        "    x = [a()]; b(); foo(x).bar()\n"  # ast.walk meets b and .bar before a and foo
    )
    monkeypatch.setattr(code_call_hierarchy, "MAX_CALLEE_CALLS", 1)
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 4)
    assert (_sites(result.callees), result.callees_truncated) == ([(5, 10)], True)
    monkeypatch.setattr(code_call_hierarchy, "MAX_CALLEE_CALLS", 3)
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 4)
    assert [(g.callee.name, _sites([g])) for g in result.callees] == [
        ("a", [(5, 10)]),
        ("b", [(5, 16)]),
        ("foo", [(5, 21)]),
    ]
    assert (_counts(result, CALLEE_COUNTS), result.callees_truncated) == ({"callee_calls": 3}, True)


def test_definition_cut_sorts_step_45s_list_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files: dict[str, str | bytes] = {
        "pkg/__init__.py": "def thing():\n    pass\n",
        "pkg/thing.py": "x = 1\n",  # Step 45 lists this submodule first
        "m.py": "from pkg import thing\ndef caller():\n    thing()\n",
    }
    monkeypatch.setattr(code_definition, "MAX_DEFINITIONS", 1)
    caller = hierarchy(tmp_path, files, "m.py", 2)
    assert _view(caller.callees) == [
        (("pkg/__init__.py", 1, "function", "thing", 2), [(3, 5, "thing", True)])
    ]
    assert (caller.callees_truncated, caller.summary.callee_not_callable) == (True, 0)
    thing = hierarchy(tmp_path, files, "pkg/__init__.py", 1)
    assert _view(thing.callers) == [(("m.py", 2, "function", "caller", 3), [(3, 5, "thing", True)])]
    assert thing.callers_truncated is False


def test_method_callees_in_class_bodies(tmp_path: Path) -> None:
    source = (
        "def outer():\n"  # 1
        "    class K:\n"  # 2
        "        def deco(f): return f\n"  # 3
        "        @deco\n"  # 4
        "        def m(self): pass\n"  # 5
        "        y = deco(1)\n"  # 6
        "    return K\n"  # 7
        "class C:\n"  # 8
        "    def h(): return 1\n"  # 9
        "    z = h()\n"  # 10
    )
    k = checked(tmp_path, {"m.py": source}, "m.py", 2)
    assert _view(k.callees) == [
        (("m.py", 3, "method", "deco", 3), [(4, 10, "deco", False), (6, 13, "deco", False)])
    ]
    c = checked(tmp_path, {"m.py": source}, "m.py", 8)
    assert _view(c.callees) == [(("m.py", 9, "method", "h", 9), [(10, 9, "h", False)])]


def test_long_names_are_capped_everywhere(tmp_path: Path) -> None:
    long = "q" * 250
    files: dict[str, str | bytes] = {
        "t.py": f"def {long}(): pass\ndef caller():\n    {long}()\n",
        "n.py": f"from t import {long}\n{long}()\n",
        f"{'m' * 250}.py": f"from t import {long}\n{long}()\n",
    }
    target = checked(tmp_path, files, "t.py", 1)
    assert target.target.name == "q" * 200
    names = [
        (g.caller.path[:2], g.caller.kind, g.caller.name, g.calls[0].name) for g in target.callers
    ]
    assert names == [
        ("mm", "module", "m" * 200, "q" * 200),  # module names are capped too
        ("n.", "module", "n", "q" * 200),
        ("t.", "function", "caller", "q" * 200),
    ]
    caller = checked(tmp_path, files, "t.py", 2)
    assert [(g.callee.name, g.calls[0].name) for g in caller.callees] == [("q" * 200, "q" * 200)]


def test_total_sites_limit_for_callees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "def a(): pass\ndef b(): pass\ndef t():\n    a(); a(); b(); b()\n"
    monkeypatch.setattr(code_call_hierarchy, "MAX_SITES", 4)
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 3)
    assert (len(_sites(result.callees)), result.callees_truncated) == (4, False)
    monkeypatch.setattr(code_call_hierarchy, "MAX_SITES", 3)
    result = hierarchy(tmp_path, {"m.py": source}, "m.py", 3)
    assert [(g.callee.name, g.total_calls, len(g.calls)) for g in result.callees] == [
        ("a", 2, 2),
        ("b", 2, 1),
    ]
    assert (result.callees_truncated, result.callers_truncated) == (True, False)


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
    path: content.encode() if isinstance(content, str) else content
    for path, content in REPO.items()
}
TARGET = {"path": "auth.py", "line": 1}


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
        f"/repositories/{repository_id or owner.repository_id}/files/call-hierarchy"
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


def _json_groups(groups: list[tuple[Any, ...]], role: str) -> list[dict[str, Any]]:
    out = []
    for (path, line, kind, name, end_line), sites in groups:
        location = {"path": path, "kind": kind, "name": name, "line": line, "end_line": end_line}
        out.append(
            {
                role: location,
                "total_calls": len(sites),
                "calls": [
                    {"line": s[0], "column": s[1], "name": s[2], "ambiguous": s[3]} for s in sites
                ],
            }
        )
    return out


EMPTY_SUMMARY = dict.fromkeys(CallHierarchySummary.model_fields, 0)


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_call_hierarchy_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "scanned",
        "target": AUTHENTICATE,
        "callers_status": "scanned",
        "callers": _json_groups(AUTHENTICATE_CALLERS, "caller"),
        "callees": _json_groups(AUTHENTICATE_CALLEES, "callee"),
        "summary": {
            **EMPTY_SUMMARY,
            "files_scanned": 5,
            "caller_calls": 12,
            "non_call_references": 4,
            "callee_calls": 1,
        },
        "callers_truncated": False,
        "callees_truncated": False,
    }


@requires_db
async def test_endpoint_statuses(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    not_scanned = {
        "target": None,
        "callers_status": None,
        "callers": None,
        "callees": [],
        "summary": EMPTY_SUMMARY,
        "callers_truncated": None,
        "callees_truncated": False,
    }
    body = (await _get(client, alice, {"path": "docs/notes.md", "line": 1})).json()
    assert body == {"status": "unsupported_language", **not_scanned}
    assert read_spy == []  # a non-Python file is never read
    for line in (999, 6, 7):  # past the end, a blank line, a decorator line
        body = (await _get(client, alice, {"path": "app/views.py", "line": line})).json()
        assert body == {"status": "not_a_definition", **not_scanned}
    body = (await _get(client, alice, {"path": "app/views.py", "line": 25})).json()
    assert (body["status"], body["target"]["kind"], body["callers_status"]) == (
        "scanned",
        "method",
        "unsupported_target",
    )
    assert (body["callers"], body["callers_truncated"]) == (None, None)
    assert [g["callee"]["name"] for g in body["callees"]] == ["authenticate"]
    body = (await _get(client, alice, {"path": "auth.py", "line": 9})).json()
    assert (body["target"]["kind"], body["callers_status"]) == ("class", "scanned")


@requires_db
async def test_validation_and_unknown_paths(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    for change in (
        {"line": 0},
        {"line": -1},
        {"line": "1.5"},
        {"line": "x"},
        {"path": ""},
        {"path": "a" * 4097},
    ):
        response = await _get(client, alice, {**TARGET, **change})
        assert response.status_code == 422, change
    for missing in ("path", "line"):
        params = {k: v for k, v in TARGET.items() if k != missing}
        assert (await _get(client, alice, params)).status_code == 422
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _get(client, alice, TARGET, **kwargs)).status_code == 422
    assert read_spy == []
    for path in ("missing.py", "../auth.py", "/auth.py", "AUTH.PY"):
        response = await _get(client, alice, {**TARGET, "path": path})
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)


@requires_db
async def test_index_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python rows by path: __init__.py, app/__init__.py, app/helpers.py, app/views.py, auth.py.
    monkeypatch.setattr(code_call_hierarchy, "MAX_INDEXED_PATHS", 4)
    body = (await _get(client, alice, TARGET)).json()  # the target itself is unindexed
    assert (body["callers_truncated"], body["callees_truncated"]) == (True, True)
    # Only the first four rows by path are indexed, so auth.py cannot be imported: its
    # callers come from its own file only.
    assert body["callers"] == _json_groups(AUTHENTICATE_CALLERS[-2:], "caller")
    assert (body["summary"]["caller_calls"], body["summary"]["non_call_references"]) == (2, 0)
    monkeypatch.setattr(code_call_hierarchy, "MAX_INDEXED_PATHS", 5)  # exactly all five
    body = (await _get(client, alice, TARGET)).json()
    assert (body["callers_truncated"], body["callees_truncated"]) == (False, False)
    assert body["callers"] == _json_groups(AUTHENTICATE_CALLERS, "caller")


@requires_db
async def test_changed_storage_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    (_storage(alice) / "app" / "views.py").write_bytes(VIEWS.encode() + b"# tampered\n")
    response = await _get(client, alice, TARGET)  # a scanned caller file
    assert (response.status_code, response.json()) == (409, CHANGED)
    assert "tampered" not in response.text

    await _ingest(client, alice, FILES)
    (_storage(alice) / "auth.py").write_bytes(AUTH.encode() + b"# new\n")
    response = await _get(client, alice, TARGET)  # the target
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    (_storage(alice) / "app" / "helpers.py").unlink()  # missing: read only for view's callees
    response = await _get(client, alice, {"path": "app/views.py", "line": 8})
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
    stored = _storage(alice) / "app" / "helpers.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(FILES["app/helpers.py"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    response = await _get(client, alice, {"path": "app/views.py", "line": 8})
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_storage_swap_during_the_request_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the target is parsed, a caller file changes before it is read."""
    real = code_references.collect_scopes
    root = _storage(alice)

    def collect_then_swap(tree: Any) -> Any:
        (root / "app" / "views.py").write_bytes(b"from auth import authenticate  # new snapshot\n")
        return real(tree)

    monkeypatch.setattr(code_references, "collect_scopes", collect_then_swap)
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
    assert body["status"] == "scanned"
    assert len([s for s in statements if "FROM files" in s]) == 1


@requires_db
async def test_reingestion_moves_the_target(client: httpx.AsyncClient, alice: Owner) -> None:
    files = dict(FILES)
    files["auth.py"] = b"\n\n" + AUTH.encode()
    await _ingest(client, alice, files)
    assert (await _get(client, alice, TARGET)).json()["status"] == "not_a_definition"
    body = (await _get(client, alice, {**TARGET, "line": 3})).json()
    assert (body["target"]["line"], body["summary"]["caller_calls"]) == (3, 12)


@requires_db
async def test_no_source_internals_or_logged_content(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-49c1"
    files = dict(FILES)
    files["auth.py"] = AUTH.replace(
        "    return None", f"    return '{marker}'  # {marker}"
    ).encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "scanned"
    assert marker not in response.text and marker not in caplog.text
    assert "return" not in response.text and "import" not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text
    body = response.json()
    assert set(body) == {
        "status",
        "target",
        "callers_status",
        "callers",
        "callees",
        "summary",
        "callers_truncated",
        "callees_truncated",
    }
    for group in body["callers"] + body["callees"]:
        assert set(group) in (
            {"caller", "total_calls", "calls"},
            {"callee", "total_calls", "calls"},
        )
        for call in group["calls"]:
            assert set(call) == {"line", "column", "name", "ambiguous"}


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"b.py": b"def f():\n    pass\nf()\n"})
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    params = {"path": "b.py", "line": 1}
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
    assert [c["line"] for g in own["callers"] for c in g["calls"]] == [3]


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
    bob = await _owner(client, "bob@example.com", {"b.py": b"def f():\n    pass\n"})
    limiter = _AlwaysLimited()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    params = {"path": "b.py", "line": 1}
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
    assert limiter.hits == [(CALL_HIERARCHY_LIMIT, alice.user_id)]
    assert read_spy == []  # limited before any snapshot read


@requires_db_and_redis
async def test_rate_limit_is_20_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert CALL_HIERARCHY_LIMIT == Limit("call-hierarchy-user", 20, 600)
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(settings)
    await pool.flushdb()
    app.dependency_overrides.pop(get_rate_limiter, None)  # use the real Step 17 limiter
    params = {"path": "docs/notes.md", "line": 1}
    try:
        for _ in range(20):
            assert (await _get(client, alice, params)).status_code == 200
        limited = await _get(client, alice, params)
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts; please try again later"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 600
        keys = [k.decode() if isinstance(k, bytes) else k for k in await pool.keys("*")]
        assert any(k.startswith("ratelimit:call-hierarchy-user:") for k in keys)
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
    params = {"path": "docs/notes.md", "line": 1}
    for _ in range(22):  # beyond the limit
        assert (await _get(client, alice, params)).status_code == 200
