"""Python method override tests (GET .../files/method-overrides?path=&line=). Offline.

Scanner tests use a temporary storage directory and need no database. Every semantic test
also checks an independent oracle: the related classes are exactly Step 51's direct bases
and subclasses of the method's class (frozen ``analyze_type_hierarchy``), and a related
class implements the method exactly when an AST walk of its own body (compound statements
included, nested definitions excluded) finds a def/async def of that name. The oracle never
uses Step 45's ``Resolver.bindings``, so imports are never followed. A duality sweep checks
both directions against each other. API tests run against the already-migrated throwaway
test database (TEST_DATABASE_URL, "_test") inside rolled-back transactions; the rate-limit
test uses a disposable Redis (TEST_REDIS_URL). Junction tests are Windows-only; symlink tests
are skipped where symlinks cannot be created.
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
from app.schemas.code_method_overrides import MethodOverridesSummary
from app.services import (
    code_definition,
    code_method_overrides,
    code_references,
    code_type_hierarchy,
    ingestion,
)
from app.services.code_definition import _cap, _normalize
from app.services.code_method_overrides import (
    MAX_OVERRIDE_CLASSES,
    METHOD_OVERRIDES_LIMIT,
    analyze_method_overrides,
)
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.code_type_hierarchy import analyze_type_hierarchy
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


# --- Fixture repository --------------------------------------------------------------------

BASE = "\n".join(
    [
        "import abc",  # 1
        "from typing import overload",  # 2
        "",  # 3
        "class Shape(abc.ABC):",  # 4
        "    @abc.abstractmethod",  # 5
        "    def area(self):",  # 6
        "        ...",  # 7
        "",  # 8
        "    def name(self):",  # 9
        "        return 'shape'",  # 10
        "",  # 11
        "    @staticmethod",  # 12
        "    def make():",  # 13
        "        return Shape",  # 14
        "",  # 15
        "    @classmethod",  # 16
        "    def build(cls):",  # 17
        "        return cls",  # 18
        "",  # 19
        "    @property",  # 20
        "    def size(self):",  # 21
        "        return 0",  # 22
        "",  # 23
        "    @size.setter",  # 24
        "    def size(self, value):",  # 25
        "        pass",  # 26
        "",  # 27
        "class Mixin:",  # 28
        "    async def area(self):",  # 29
        "        return 1",  # 30
        "",  # 31
        "    def helper(self):",  # 32
        "        def area():",  # 33: a nested function, never a binding of Mixin
        "            return 2",  # 34
        "        return area",  # 35
        "",  # 36
        "def area():",  # 37: module level
        "    return 0",  # 38
        "",
    ]
)

SQUARE = "\n".join(
    [
        "from shapes.base import Shape, Mixin",  # 1
        "from shapes import base",  # 2
        "",  # 3
        "class Square(Shape, Mixin):",  # 4
        "    def area(self):",  # 5
        "        return 4",  # 6
        "",  # 7
        "    @staticmethod",  # 8
        "    def make():",  # 9
        "        return Square",  # 10
        "",  # 11
        "    @classmethod",  # 12
        "    def build(cls):",  # 13
        "        return cls",  # 14
        "",  # 15
        "    @property",  # 16
        "    def size(self):",  # 17
        "        return 4",  # 18
        "",  # 19
        "    @custom",  # 20
        "    def name(self):",  # 21
        "        return 'square'",  # 22
        "",  # 23
        "class Plain(Shape):",  # 24
        "    pass",  # 25
        "",  # 26
        "class Other(Shape):",  # 27
        "    def other(self):",  # 28
        "        return 1",  # 29
        "",  # 30
        "class Imported(Shape):",  # 31
        "    from shapes.base import area",  # 32: an imported name is never an implementation
        "",  # 33
        "class Assigned(Shape):",  # 34
        "    area = Shape.area",  # 35
        "",  # 36
        "class Classy(Shape):",  # 37
        "    class area:",  # 38
        "        pass",  # 39
        "",  # 40
        "class Branchy(Shape):",  # 41
        "    if flag:",  # 42
        "        def area(self):",  # 43
        "            return 1",  # 44
        "    else:",  # 45
        "        async def area(self):",  # 46
        "            return 2",  # 47
        "",  # 48
        "class Odd(base.Shape):",  # 49: Step 51 cannot resolve base.Shape
        "    def area(self):",  # 50
        "        return 0",  # 51
        "",
    ]
)

REPO: dict[str, str | bytes] = {
    "shapes/__init__.py": "",
    "shapes/base.py": BASE,
    "shapes/square.py": SQUARE,
    "shapes/unrelated.py": "class Circle:\n    def area(self):\n        return 3\n",
    "docs/notes.md": "class Shape:\n    def area(self): pass\n",
}

SHAPE = ("shapes/base.py", 4, "class", "Shape", 26)
MIXIN = ("shapes/base.py", 28, "class", "Mixin", 35)
SHAPE_AREA = ("shapes/base.py", 6, "method", "area", 7)
MIXIN_AREA = ("shapes/base.py", 29, "method", "area", 30)
SQUARE_AREA = ("shapes/square.py", 5, "method", "area", 6)
# (site, base_kind, class, ambiguous, status, implementations, implementations_ambiguous)
SQUARE_AREA_OVERRIDDEN = [
    ((4, 14, "Shape"), "resolved", SHAPE, False, "defined", [SHAPE_AREA], False),
    ((4, 21, "Mixin"), "resolved", MIXIN, False, "defined", [MIXIN_AREA], False),  # async
]
# (subclass, site, status, implementations, implementations_ambiguous), Step 51 order.
SHAPE_AREA_OVERRIDING = [
    (
        ("shapes/square.py", 4, "class", "Square", 22),
        (4, 14, "Shape", False),
        "defined",
        [SQUARE_AREA],
        False,
    ),
    (
        ("shapes/square.py", 31, "class", "Imported", 32),
        (31, 16, "Shape", False),
        "unsupported",
        [],
        False,
    ),
    (
        ("shapes/square.py", 34, "class", "Assigned", 35),
        (34, 16, "Shape", False),
        "unsupported",
        [],
        False,
    ),
    (
        ("shapes/square.py", 37, "class", "Classy", 39),
        (37, 14, "Shape", False),
        "not_a_method",
        [],
        False,
    ),
    (
        ("shapes/square.py", 41, "class", "Branchy", 47),
        (41, 15, "Shape", False),
        "defined",
        [
            ("shapes/square.py", 43, "method", "area", 44),
            ("shapes/square.py", 46, "method", "area", 47),
        ],
        True,
    ),
]  # Plain and Other inherit area (counted, never listed); Odd is not a Step 51 subclass


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


def overrides(
    tmp_path: Path, files: dict[str, str | bytes], path: str, line: int, **kw: Any
) -> Any:
    root, rows = _repo(tmp_path, files)
    return analyze_method_overrides(root, _row(rows, path), rows, line, **kw)


def _loc(d: Any) -> tuple[Any, ...] | None:
    return None if d is None else (d.path, d.line, d.kind, d.name, d.end_line)


def _overridden(result: Any) -> list[tuple[Any, ...]]:
    return [
        (
            None if e.site is None else (e.site.line, e.site.column, e.site.name),
            e.base_kind,
            _loc(e.class_),
            e.ambiguous,
            e.status,
            [_loc(d) for d in e.implementations],
            e.implementations_ambiguous,
        )
        for e in result.overridden
    ]


def _overriding(result: Any) -> Any:  # None when subclasses were not searched
    if result.overriding is None:
        return None
    return [
        (
            _loc(e.subclass),
            (e.site.line, e.site.column, e.site.name, e.site.ambiguous),
            e.status,
            [_loc(d) for d in e.implementations],
            e.implementations_ambiguous,
        )
        for e in result.overriding
    ]


def _summary(result: Any, **expected: int) -> None:
    assert result.summary.model_dump() == {
        **dict.fromkeys(MethodOverridesSummary.model_fields, 0),
        **expected,
    }


# --- The independent oracle ------------------------------------------------------------------

_TRANSPARENT = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)


def _names(target: ast.AST) -> list[str]:
    """Names a binding target stores to (Name, tuple/list unpacking, starred)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for element in target.elts for n in _names(element)]
    if isinstance(target, ast.Starred):
        return _names(target.value)
    return []


def _own_body(cls: ast.ClassDef, name: str) -> tuple[list[ast.AST], list[str]]:
    """The defs of ``name`` in the class's own body and the kinds of its other bindings, from
    Python's statement rules: compound statements are part of the body, nested functions and
    classes have bodies of their own. Written independently of Step 45's collector."""
    defs: list[ast.AST] = []
    others: list[str] = []
    stack: list[ast.stmt] = list(reversed(cls.body))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                defs.append(node)
        elif isinstance(node, ast.ClassDef):
            if node.name == name:
                others.append("class")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound == name:
                    others.append("import")
        elif isinstance(node, ast.Assign):
            if any(name in _names(t) for t in node.targets):
                others.append("variable")
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if name in _names(node.target):
                others.append("variable")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            if name in node.names:
                others.append("declared")
        if isinstance(node, (ast.For, ast.AsyncFor)) and name in _names(node.target):
            others.append("variable")
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None and name in _names(item.optional_vars):
                    others.append("variable")
        if isinstance(node, _TRANSPARENT):
            blocks: list[list[ast.stmt]] = [getattr(node, "body", [])]
            blocks += [getattr(node, "orelse", []), getattr(node, "finalbody", [])]
            for handler in getattr(node, "handlers", []):
                if handler.name == name:
                    others.append("variable")
                blocks.append(handler.body)
            for block in reversed(blocks):
                stack.extend(reversed(block))
    return defs, others


def _parse(root: Path, row: Any) -> ast.Module | None:
    if row.size_bytes > MAX_CONTENT_BYTES:
        return None
    text = _decode((root / row.path).read_bytes())
    if text is None or len(text.encode()) > MAX_OUTLINE_BYTES:
        return None
    try:
        return ast.parse(_normalize(text))
    except SyntaxError:
        return None


def _enclosing(tree: ast.Module) -> dict[int, ast.AST | None]:
    """id(def or class) -> the nearest enclosing def or class statement (None at module
    level); compound statements are transparent."""
    out: dict[int, ast.AST | None] = {}
    stack: list[tuple[ast.AST, ast.AST | None]] = [(n, None) for n in tree.body]
    while stack:
        node, owner = stack.pop()
        inner = owner
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[id(node)] = owner
            inner = node
        stack.extend((child, inner) for child in ast.iter_child_nodes(node))
    return out


def _member(root: Path, rows: list[Any], related: Any, name: str) -> tuple[Any, ...]:
    """(status, implementations, implementations_ambiguous) of one related class."""
    tree = _parse(root, _row(rows, related.path))
    if tree is None:
        return ("unsupported", [], False)
    cls = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.lineno == related.line
    )
    defs, others = _own_body(cls, name)
    if defs:
        found = sorted(
            ((related.path, d.lineno, "method", _cap(name), d.end_lineno) for d in defs),  # type: ignore[attr-defined]
            key=lambda d: d[1],
        )
        cut = found[: code_definition.MAX_DEFINITIONS]
        return ("defined", cut, len(found) > 1)
    if not others:
        return ("not_defined", [], False)
    if all(kind == "class" for kind in others):
        return ("not_a_method", [], False)
    return ("unsupported", [], False)


def oracle(root: Path, rows: list[Any], path: str, line: int) -> dict[str, Any]:
    """The expected overrides of the method on ``line``: Step 51's direct relationships of its
    class, each related class judged by its own body's defs only."""
    tree = _parse(root, _row(rows, path))
    assert tree is not None
    owners = _enclosing(tree)
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno == line
    )
    owner = owners[id(node)]
    assert isinstance(owner, ast.ClassDef), "not a method"
    name = node.name
    hierarchy = analyze_type_hierarchy(root, _row(rows, path), rows, owner.lineno)
    overridden = []
    for base in hierarchy.bases:
        site = None if base.site is None else (base.site.line, base.site.column, base.site.name)
        classes = [d for d in base.definitions if d.kind == "class"]
        if base.kind not in ("resolved", "ambiguous") or not classes:
            overridden.append(
                (site, base.kind, None, base.kind == "ambiguous", "unavailable", [], False)
            )
            continue
        for related in classes:
            member = _member(root, rows, related, name)
            overridden.append((site, base.kind, _loc(related), base.kind == "ambiguous", *member))
    overriding = None
    inheriting = 0
    if hierarchy.subclasses_status == "scanned":
        overriding = []
        for entry in hierarchy.subclasses or []:
            member = _member(root, rows, entry.subclass, name)
            if member[0] == "not_defined":
                inheriting += 1
                continue
            s = entry.site
            overriding.append(
                (_loc(entry.subclass), (s.line, s.column, s.name, s.ambiguous), *member)
            )
    return {
        "target": (path, node.lineno, "method", _cap(name), node.end_lineno),
        "owner": (path, owner.lineno, "class", _cap(owner.name), owner.end_lineno),
        "overridden": overridden,
        "overriding": overriding,
        "inheriting": inheriting,
        "hierarchy": hierarchy,
    }


def _belongs(root: Path, rows: list[Any], implementation: Any, related: Any, name: str) -> None:
    """The implementation is a def/async def of ``name`` in the related class's own body."""
    tree = _parse(root, _row(rows, implementation.path))
    assert tree is not None
    owners = _enclosing(tree)
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.lineno == implementation.line
    )
    assert node.name == name and implementation.kind == "method"
    owner = owners[id(node)]
    assert isinstance(owner, ast.ClassDef)
    assert (implementation.path, owner.lineno) == (related.path, related.line)


def checked(tmp_path: Path, files: dict[str, str | bytes], path: str, line: int) -> Any:
    """``overrides`` plus the oracle (no limit reached)."""
    root, rows = _repo(tmp_path, files)
    result = analyze_method_overrides(root, _row(rows, path), rows, line)
    assert result.status == "scanned", result.status
    expected = oracle(root, rows, path, line)
    hierarchy = expected["hierarchy"]
    assert _loc(result.target) == expected["target"]
    assert _loc(result.owner) == expected["owner"]
    assert _overridden(result) == expected["overridden"]
    assert _overriding(result) == expected["overriding"]
    assert result.overridden_truncated is hierarchy.bases_truncated is False
    name = result.target.name
    for entry in result.overridden:
        for implementation in entry.implementations:
            _belongs(root, rows, implementation, entry.class_, name)
    for sub in result.overriding or []:
        for implementation in sub.implementations:
            _belongs(root, rows, implementation, sub.subclass, name)
    summary = result.summary
    statuses = [e[4] for e in expected["overridden"]]
    assert summary.bases == hierarchy.summary.bases
    assert summary.base_classes == sum(e[2] is not None for e in expected["overridden"])
    for status in ("defined", "not_defined", "not_a_method", "unavailable", "unsupported"):
        assert getattr(summary, f"overridden_{status}") == statuses.count(status)
    if expected["overriding"] is None:
        assert (result.overriding_status, result.overriding_truncated) == (
            "unsupported_target",
            None,
        )
        assert (summary.overriding, summary.inheriting, summary.subclasses) == (0, 0, 0)
    else:
        assert result.overriding_status == "scanned"
        assert result.overriding_truncated is False
        sub_statuses = [e[2] for e in expected["overriding"]]
        assert summary.subclasses == hierarchy.summary.subclasses
        assert summary.overriding == sub_statuses.count("defined")
        assert summary.inheriting == expected["inheriting"]
        assert summary.overriding_unsupported == sub_statuses.count("unsupported")
        assert summary.overriding_not_a_method == sub_statuses.count("not_a_method")
    file_counts = ("files_scanned", "too_large", "binary", "syntax_error")
    assert {k: getattr(summary, k) for k in file_counts} == {
        k: getattr(hierarchy.summary, k) for k in file_counts
    }
    return result


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_references._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_references, "_read_verified", spy)
    return reads


# --- 1, 2, 8-14, 17-30: the repository ----------------------------------------------------------


def test_parent_implementations_with_multiple_inheritance_and_async(tmp_path: Path) -> None:
    result = checked(tmp_path, REPO, "shapes/square.py", 5)
    assert result.target.model_dump() == {
        "path": "shapes/square.py",
        "kind": "method",
        "name": "area",
        "line": 5,
        "end_line": 6,
    }
    assert _loc(result.owner) == ("shapes/square.py", 4, "class", "Square", 22)
    assert _overridden(result) == SQUARE_AREA_OVERRIDDEN  # both bases, in base order; no MRO
    assert (result.overriding_status, result.overriding) == ("scanned", [])
    _summary(
        result,
        files_scanned=4,
        bases=2,
        base_classes=2,
        overridden_defined=2,
    )
    assert (result.overridden_truncated, result.overriding_truncated) == (False, False)


def test_child_overrides_never_list_inheriting_or_unrelated_classes(tmp_path: Path) -> None:
    result = checked(tmp_path, REPO, "shapes/base.py", 6)  # an abstract method
    assert _overriding(result) == SHAPE_AREA_OVERRIDING
    assert _overridden(result) == [
        ((4, 17, "ABC"), "unresolved", None, False, "unavailable", [], False)
    ]
    _summary(
        result,
        files_scanned=4,
        bases=1,
        overridden_unavailable=1,
        subclasses=7,
        overriding=2,
        inheriting=2,  # Plain (pass) and Other (def other)
        overriding_unsupported=2,  # the imported name and the assignment
        overriding_not_a_method=1,
    )
    names = {e.subclass.name for e in result.overriding}
    assert not names & {"Plain", "Other", "Circle", "Odd"}


def test_async_method_target_matches_sync_overrides(tmp_path: Path) -> None:
    result = checked(tmp_path, REPO, "shapes/base.py", 29)
    assert result.target.name == "area" and _loc(result.owner) == MIXIN
    assert _overriding(result) == [
        (
            ("shapes/square.py", 4, "class", "Square", 22),
            (4, 21, "Mixin", False),
            "defined",
            [SQUARE_AREA],
            False,
        )
    ]
    assert result.overridden == []


@pytest.mark.parametrize(
    ("line", "name", "parent", "child"),
    [
        (9, "make", ("shapes/base.py", 13, "method", "make", 14), None),  # @staticmethod
        (13, "build", ("shapes/base.py", 17, "method", "build", 18), None),  # @classmethod
        (21, "name", ("shapes/base.py", 9, "method", "name", 10), None),  # @custom
    ],
)
def test_decorated_methods_are_implementations(
    tmp_path: Path, line: int, name: str, parent: tuple[Any, ...], child: Any
) -> None:
    result = checked(tmp_path, REPO, "shapes/square.py", line)
    assert result.target.name == name
    assert _overridden(result)[0][4:] == ("defined", [parent], False)
    assert _overridden(result)[1][4] == "not_defined"  # Mixin does not define it


def test_property_and_setter(tmp_path: Path) -> None:
    square = checked(tmp_path, REPO, "shapes/square.py", 17)  # @property size
    assert _overridden(square)[0][4:] == (
        "defined",
        [
            ("shapes/base.py", 21, "method", "size", 22),
            ("shapes/base.py", 25, "method", "size", 26),
        ],
        True,  # the getter and the setter: both listed
    )
    setter = checked(tmp_path, REPO, "shapes/base.py", 25)  # the setter is a method target
    assert [e[0][3] for e in _overriding(setter)] == ["Square"]


def test_statuses_of_related_bindings(tmp_path: Path) -> None:
    result = checked(tmp_path, REPO, "shapes/base.py", 6)
    by_class = {e[0][3]: e[2:] for e in _overriding(result)}
    assert by_class["Imported"] == ("unsupported", [], False)  # from shapes.base import area
    assert by_class["Assigned"] == ("unsupported", [], False)  # area = Shape.area
    assert by_class["Classy"] == ("not_a_method", [], False)  # class area
    assert by_class["Branchy"][0] == "defined"  # defs in if/else of the class body


def test_imported_names_never_count_as_implementations(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "lib.py": "def save(self):\n    pass\nclass Real:\n    def save(self): pass\n",
        "m.py": (
            "class Base:\n"  # 1
            "    from lib import save\n"  # 2
            "class Child(Base):\n"  # 3
            "    def save(self): pass\n"  # 4
            "class Grand(Child):\n"  # 5
            "    import lib as save\n"  # 6
            "class Alias(Child):\n"  # 7
            "    from lib import Real as save\n"  # 8
        ),
    }
    child = checked(tmp_path, files, "m.py", 4)
    assert _overridden(child) == [
        (
            (3, 13, "Base"),
            "resolved",
            ("m.py", 1, "class", "Base", 2),
            False,
            "unsupported",
            [],
            False,
        )
    ]
    assert _overriding(child) == [
        (("m.py", 5, "class", "Grand", 6), (5, 13, "Child", False), "unsupported", [], False),
        (("m.py", 7, "class", "Alias", 8), (7, 13, "Child", False), "unsupported", [], False),
    ]
    assert child.summary.overriding == 0 and child.summary.overridden_defined == 0


def test_non_class_bases_are_unavailable_with_their_kind(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": (
            "import abc\nfrom shapes import base\ndef factory(): pass\n"  # 1-3
            "class K(object, abc.ABC, *extra, factory, base.Shape, get()):\n"  # 4
            "    def save(self): pass\n"  # 5
        ),
        "shapes/__init__.py": "",
        "shapes/base.py": "class Shape:\n    def save(self): pass\n",
    }
    result = checked(tmp_path, files, "m.py", 5)
    assert [(e[1], e[2], e[4]) for e in _overridden(result)] == [
        ("builtin", None, "unavailable"),
        ("unresolved", None, "unavailable"),
        ("dynamic", None, "unavailable"),
        ("not_a_class", None, "unavailable"),
        ("unsupported", None, "unavailable"),  # from shapes import base; base.Shape
        ("dynamic", None, "unavailable"),
    ]
    assert result.summary.overridden_unavailable == 6
    assert result.summary.base_classes == 0


def test_unsupported_base_of_the_repository(tmp_path: Path) -> None:
    result = checked(tmp_path, REPO, "shapes/square.py", 50)  # Odd(base.Shape)
    assert _overridden(result) == [
        ((49, 16, "Shape"), "unsupported", None, False, "unavailable", [], False)
    ]


# --- 3-7: targets -------------------------------------------------------------------------------


def test_targets_and_statuses(tmp_path: Path) -> None:
    empty = dict.fromkeys(MethodOverridesSummary.model_fields, 0)
    not_scanned = {
        "target": None,
        "owner": None,
        "overridden": [],
        "overriding_status": None,
        "overriding": None,
        "summary": empty,
        "overridden_truncated": False,
        "overriding_truncated": None,
    }
    for line in (4, 28):  # class lines
        assert overrides(tmp_path, REPO, "shapes/base.py", line).model_dump() == {
            "status": "not_a_method",
            **not_scanned,
        }
    for path, line in (("n.py", 2), ("n.py", 4), ("n.py", 7)):  # nested and local classes
        assert overrides(tmp_path, SCOPES, path, line).status == "not_a_method"
    for line in (33, 37):  # a function nested in a method, a module-level function
        assert overrides(tmp_path, REPO, "shapes/base.py", line).status == "not_a_method"
    for line in (1, 5, 7, 8, 12, 999):  # import, decorators, body, blank, past the end
        assert overrides(tmp_path, REPO, "shapes/base.py", line).model_dump() == {
            "status": "not_a_definition",
            **not_scanned,
        }
    assert overrides(tmp_path, {"m.py": ""}, "m.py", 1).status == "not_a_definition"
    assert overrides(tmp_path, REPO, "shapes/base.py", 6).status == "scanned"


def test_target_file_statuses(tmp_path: Path, read_spy: list[str]) -> None:
    def status(content: str | bytes, line: int = 2) -> str:
        result = overrides(tmp_path, {"m.py": content}, "m.py", line)
        if result.status != "scanned":
            assert (result.target, result.owner, result.overridden, result.overriding) == (
                None,
                None,
                [],
                None,
            )
            assert set(result.summary.model_dump().values()) == {0}
        return result.status

    assert status("class (:\n") == "syntax_error"
    assert status("class A:\n    def f(self): '''\n") == "syntax_error"
    assert status(b"\x00\x01") == "binary"
    assert status(b"\xff\xfe") == "binary"
    body = b"class T:\n    def f(self): pass\nclass U(T):\n    def f(self): pass\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    assert status(exactly) == "scanned"
    assert status(exactly + b"#") == "too_large"
    read_spy.clear()
    assert status(os.urandom(MAX_CONTENT_BYTES) + b"x") == "too_large"
    assert read_spy == []  # over 1 MiB is never opened


# --- 13, 31-33: ambiguity and duplicates ---------------------------------------------------------


AMBIGUOUS: dict[str, str | bytes] = {
    "shapes/__init__.py": "",
    "shapes/base.py": "class Shape:\n    def area(self): pass\n",
    "amb.py": (
        "try:\n"  # 1
        "    from shapes.base import Shape\n"  # 2
        "except ImportError:\n"  # 3
        "    class Shape:\n"  # 4
        "        def area(self): pass\n"  # 5
        "class Tile(Shape):\n"  # 6
        "    def area(self): pass\n"  # 7
        "class Dup(Tile, Tile):\n"  # 8
        "    def area(self): pass\n"  # 9
    ),
}


def test_ambiguous_bases_keep_every_candidate(tmp_path: Path) -> None:
    tile = checked(tmp_path, AMBIGUOUS, "amb.py", 7)
    assert _overridden(tile) == [
        (
            (6, 12, "Shape"),
            "ambiguous",
            ("amb.py", 4, "class", "Shape", 5),
            True,
            "defined",
            [("amb.py", 5, "method", "area", 5)],
            False,
        ),
        (
            (6, 12, "Shape"),
            "ambiguous",
            ("shapes/base.py", 1, "class", "Shape", 2),
            True,
            "defined",
            [("shapes/base.py", 2, "method", "area", 2)],
            False,
        ),
    ]
    assert tile.summary.overridden_defined == 2


def test_ambiguous_subclass_and_duplicates(tmp_path: Path) -> None:
    base = checked(tmp_path, AMBIGUOUS, "shapes/base.py", 2)
    assert _overriding(base) == [
        (
            ("amb.py", 6, "class", "Tile", 7),
            (6, 12, "Shape", True),
            "defined",
            [("amb.py", 7, "method", "area", 7)],
            False,
        )
    ]
    tile = checked(tmp_path, AMBIGUOUS, "amb.py", 7)
    dup = ("amb.py", 8, "class", "Dup", 9)
    dup_area = [("amb.py", 9, "method", "area", 9)]
    assert _overriding(tile) == [  # Dup inherits Tile twice: both entries kept
        (dup, (8, 11, "Tile", False), "defined", dup_area, False),
        (dup, (8, 17, "Tile", False), "defined", dup_area, False),
    ]
    dup_result = checked(tmp_path, AMBIGUOUS, "amb.py", 9)
    assert [e[2] for e in _overridden(dup_result)] == [("amb.py", 6, "class", "Tile", 7)] * 2


def test_unrelated_same_name_methods_are_excluded(tmp_path: Path) -> None:
    circle = checked(tmp_path, REPO, "shapes/unrelated.py", 2)
    assert (circle.overridden, circle.overriding) == ([], [])


# --- 15, 16, 55, 56: scopes, cycles, PEP 695 ------------------------------------------------------


SCOPES: dict[str, str | bytes] = {
    "n.py": (
        "class Outer:\n"  # 1
        "    class Base:\n"  # 2
        "        def save(self): pass\n"  # 3
        "    class Child(Base):\n"  # 4
        "        def save(self): pass\n"  # 5
        "def build():\n"  # 6
        "    class Local:\n"  # 7
        "        def save(self): pass\n"  # 8
        "    class Sub(Local):\n"  # 9
        "        def save(self):\n"  # 10
        "            def save(): pass\n"  # 11: nested in a method
        "            return save\n"  # 12
        "    return Sub\n"  # 13
    ),
    "c.py": "class A(B):\n    def f(self): pass\nclass B(A):\n    def f(self): pass\n",
    "p.py": "class Box[T]:\n    def get(self) -> T: ...\nclass IntBox(Box[int]):\n    def get(self) -> int: ...\n",
}


def test_nested_class_methods(tmp_path: Path) -> None:
    child = checked(tmp_path, SCOPES, "n.py", 5)
    assert _overridden(child) == [
        (
            (4, 17, "Base"),
            "resolved",
            ("n.py", 2, "class", "Base", 3),
            False,
            "defined",
            [("n.py", 3, "method", "save", 3)],
            False,
        )
    ]
    assert (child.overriding_status, child.overriding, child.overriding_truncated) == (
        "unsupported_target",
        None,
        None,
    )
    base = checked(tmp_path, SCOPES, "n.py", 3)
    assert (base.overriding_status, base.overriding) == ("unsupported_target", None)


def test_function_local_classes_and_nested_functions(tmp_path: Path) -> None:
    local = checked(tmp_path, SCOPES, "n.py", 8)
    assert _overriding(local) == [
        (
            ("n.py", 9, "class", "Sub", 12),
            (9, 15, "Local", False),
            "defined",
            [("n.py", 10, "method", "save", 12)],
            False,
        )
    ]
    assert overrides(tmp_path, SCOPES, "n.py", 11).status == "not_a_method"


def test_cyclic_inheritance_terminates_with_direct_entries(tmp_path: Path) -> None:
    a = checked(tmp_path, SCOPES, "c.py", 2)
    b_class = ("c.py", 3, "class", "B", 4)
    assert [e[2] for e in _overridden(a)] == [b_class]
    assert [e[0] for e in _overriding(a)] == [b_class]


def test_pep_695_classes(tmp_path: Path) -> None:
    result = checked(tmp_path, SCOPES, "p.py", 4)
    assert _overridden(result) == [
        (
            (3, 14, "Box"),
            "resolved",
            ("p.py", 1, "class", "Box", 2),
            False,
            "defined",
            [("p.py", 2, "method", "get", 2)],
            False,
        )
    ]


def test_transitive_ancestors_are_never_reported(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class A:\n    def f(self): pass\nclass B(A):\n    pass\nclass C(B):\n    def f(self): pass\n"
    }
    c = checked(tmp_path, files, "m.py", 6)
    assert [e[4] for e in _overridden(c)] == ["not_defined"]  # B; never A
    a = checked(tmp_path, files, "m.py", 2)
    assert (_overriding(a), a.summary.inheriting) == ([], 1)  # B inherits; C is not direct


def test_unicode_tabs_crlf_and_nfkc(tmp_path: Path) -> None:
    source = (
        "# \u00e9\r\n"  # 1
        "class Base:\r\n"  # 2
        "\tdef \uff53ave(self): pass\r\n"  # 3: a fullwidth s, NFKC-normalized to save
        "class Kid(Base):\r\n"  # 4
        "\tdef save(self): pass\r\n"  # 5
    )
    result = checked(tmp_path, {"u.py": source.encode()}, "u.py", 5)
    assert _overridden(result)[0][4:] == ("defined", [("u.py", 3, "method", "save", 3)], False)


# --- 50-54: limits --------------------------------------------------------------------------------


def test_class_inspection_limit_cuts_only_its_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_method_overrides, "MAX_OVERRIDE_CLASSES", 1)
    square = overrides(tmp_path, REPO, "shapes/square.py", 5)
    assert [e.class_.name for e in square.overridden] == ["Shape"]  # Mixin not inspected
    assert (square.overridden_truncated, square.overriding_truncated) == (True, False)
    shape = overrides(tmp_path, REPO, "shapes/base.py", 6)  # the unavailable base costs nothing
    assert [e.subclass.name for e in shape.overriding] == ["Square"]
    assert (shape.overridden_truncated, shape.overriding_truncated) == (False, True)
    monkeypatch.setattr(code_method_overrides, "MAX_OVERRIDE_CLASSES", 2)  # exactly enough
    assert overrides(tmp_path, REPO, "shapes/square.py", 5).overridden_truncated is False
    assert MAX_OVERRIDE_CLASSES == 1000


def test_step_51_truncation_is_propagated_per_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_type_hierarchy, "MAX_BASES", 1)
    square = overrides(tmp_path, REPO, "shapes/square.py", 5)
    assert ([e.class_.name for e in square.overridden], square.overridden_truncated) == (
        ["Shape"],
        True,
    )
    assert square.overriding_truncated is False
    monkeypatch.setattr(code_type_hierarchy, "MAX_BASES", 200)
    monkeypatch.setattr(code_type_hierarchy, "MAX_SUBCLASSES", 1)
    shape = overrides(tmp_path, REPO, "shapes/base.py", 6)
    assert ([e.subclass.name for e in shape.overriding], shape.overriding_truncated) == (
        ["Square"],
        True,
    )
    assert shape.overridden_truncated is False


def test_index_truncation_sets_both(tmp_path: Path) -> None:
    result = overrides(tmp_path, REPO, "shapes/base.py", 6, index_truncated=True)
    assert (result.overridden_truncated, result.overriding_truncated) == (True, True)
    nested = overrides(tmp_path, SCOPES, "n.py", 5, index_truncated=True)
    assert (nested.overridden_truncated, nested.overriding_truncated) == (True, None)


def test_phase_2_read_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_spy: list[str]
) -> None:
    root, rows = _repo(tmp_path, REPO)
    square = _row(rows, "shapes/square.py")
    monkeypatch.setattr(code_method_overrides, "MAX_SCANNED_BYTES", square.size_bytes)
    result = analyze_method_overrides(root, square, rows, 5)  # Step 51 keeps its own budget
    assert [(e.class_.name, e.status) for e in result.overridden] == [
        ("Shape", "unsupported"),
        ("Mixin", "unsupported"),
    ]
    assert (result.overridden_truncated, result.overriding_truncated) == (True, False)


def test_phase_2_parse_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, rows = _repo(tmp_path, REPO)
    square = _row(rows, "shapes/square.py")
    monkeypatch.setattr(code_method_overrides, "MAX_PARSED_BYTES", square.size_bytes)
    result = analyze_method_overrides(root, square, rows, 5)
    assert [e.status for e in result.overridden] == ["unsupported", "unsupported"]
    assert result.overridden_truncated is True


def test_definition_list_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_definition, "MAX_DEFINITIONS", 1)
    result = overrides(tmp_path, REPO, "shapes/base.py", 6)
    branchy = next(e for e in result.overriding if e.subclass.name == "Branchy")
    assert ([d.line for d in branchy.implementations], branchy.implementations_ambiguous) == (
        [43],
        True,
    )
    assert (result.overriding_truncated, result.overridden_truncated) == (True, False)


def test_related_files_that_cannot_be_analyzed_are_unsupported(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "class A:\n    def f(self): pass\n",
        "big.py": b"from a import A\nclass K(A):\n    def f(self): pass\n"
        + b"#" * MAX_OUTLINE_BYTES,
    }
    result = overrides(tmp_path, files, "a.py", 2)
    assert result.overriding == [] and result.summary.too_large == 1  # not a Step 51 subclass


# --- 63: no execution -----------------------------------------------------------------------------


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"  # 1
        "import os, subprocess, socket\n"  # 2
        "os.system('echo')\nsubprocess.run(['echo'])\n"  # 3, 4
        "socket.create_connection(('example.invalid', 80))\n"  # 5
        "eval('1')\nexec('x = 1')\n"  # 6, 7
        "def deco(f):\n    open('side-effect', 'w')\n    return f\n"  # 8-10
        "class Meta(type):\n    def __new__(m, *a): os.system('echo')\n"  # 11, 12
        "class Base(metaclass=Meta):\n    @deco\n    def save(self): eval('2')\n"  # 13-15
    )
    files: dict[str, str | bytes] = {
        "m.py": "from evil import Base\nclass Kid(Base):\n    @deco\n    def save(self): pass\n",
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
    kid = analyze_method_overrides(root, _row(rows, "m.py"), rows, 4)
    base = analyze_method_overrides(root, _row(rows, "evil.py"), rows, 15)
    monkeypatch.undo()
    assert [(e.class_.name, e.status) for e in kid.overridden] == [("Base", "defined")]
    assert [e.subclass.name for e in base.overriding] == ["Kid"]
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


# --- 38-44: snapshot ------------------------------------------------------------------------------


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    for changed in ("shapes/square.py", "shapes/base.py"):  # the target, a base's file
        root, rows = _repo(tmp_path, REPO)
        (root / changed).write_bytes((root / changed).read_bytes() + b"# changed\n")
        with pytest.raises(FileContentError) as raised:
            analyze_method_overrides(root, _row(rows, "shapes/square.py"), rows, 5)
        assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    root, rows = _repo(tmp_path, REPO)
    (root / "shapes/square.py").write_bytes(SQUARE.encode() + b"# changed\n")  # a subclass file
    with pytest.raises(FileContentError) as raised:
        analyze_method_overrides(root, _row(rows, "shapes/base.py"), rows, 6)
    assert raised.value.status_code == 409
    root, rows = _repo(tmp_path, REPO)
    (root / "shapes/base.py").unlink()  # missing
    with pytest.raises(FileContentError) as raised:
        analyze_method_overrides(root, _row(rows, "shapes/square.py"), rows, 5)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_a_change_between_the_phases_fails_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 51 read the base's file; it changes before phase 2 inspects that class."""
    root, rows = _repo(tmp_path, REPO)
    real = code_method_overrides.analyze_type_hierarchy

    def hierarchy_then_change(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        (root / "shapes/base.py").write_bytes(BASE.encode() + b"# new snapshot\n")
        return result

    monkeypatch.setattr(code_method_overrides, "analyze_type_hierarchy", hierarchy_then_change)
    with pytest.raises(FileContentError) as raised:
        analyze_method_overrides(root, _row(rows, "shapes/square.py"), rows, 5)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_phase_2_reads_only_related_files(tmp_path: Path, read_spy: list[str]) -> None:
    root, rows = _repo(tmp_path, SCOPES)
    analyze_method_overrides(root, _row(rows, "n.py"), rows, 5)  # nested: no subclass scan
    assert sorted(set(read_spy)) == ["n.py"]


def test_symlinked_base_fails_the_request(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "from lib import Base\nclass T(Base):\n    def f(self): pass\n",
        "lib.py": "class Base:\n    def f(self): pass\n",
    }
    root, rows = _repo(tmp_path, files)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"class Base:\n    def f(self): pass\n")
    (root / "lib.py").unlink()
    try:
        os.symlink(outside, root / "lib.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    with pytest.raises(FileContentError) as raised:
        analyze_method_overrides(root, _row(rows, "m.py"), rows, 3)
    assert raised.value.status_code == 409


def test_junctioned_package_fails_the_request(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    root, rows = _repo(tmp_path, REPO)
    outside = tmp_path / "outside"
    shutil.copytree(root / "shapes", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "shapes")
    _winapi.CreateJunction(str(outside), str(root / "shapes"))  # type: ignore[attr-defined]
    with pytest.raises(FileContentError) as raised:
        analyze_method_overrides(root, _row(rows, "shapes/square.py"), rows, 5)
    assert raised.value.status_code == 409


def test_nothing_is_cached_across_requests(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class T:\n    def f(self): pass\n",
        "n.py": "from m import T\nclass K(T):\n    def f(self): pass\n",
    }
    root, rows = _repo(tmp_path, files)
    first = analyze_method_overrides(root, rows[0], rows, 2)
    assert [e.status for e in first.overriding] == ["defined"]
    changed = b"from m import T\nclass K(T):\n    f = None\n"
    (root / "n.py").write_bytes(changed)
    rows[1] = SimpleNamespace(
        path="n.py", size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest()
    )
    second = analyze_method_overrides(root, rows[0], rows, 2)
    assert [e.status for e in second.overriding] == ["unsupported"]


# --- The oracle sweep and duality -----------------------------------------------------------------


SWEEP: dict[str, dict[str, str | bytes]] = {"repo": REPO, "ambiguous": AMBIGUOUS, "scopes": SCOPES}


def _method_lines(text: str) -> list[int]:
    tree = ast.parse(text)
    owners = _enclosing(tree)
    return [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and isinstance(owners[id(n)], ast.ClassDef)
    ]


@pytest.mark.parametrize("fixture", sorted(SWEEP))
def test_oracle_sweep(tmp_path: Path, fixture: str) -> None:
    """Every method of every file matches the independent oracle."""
    files = SWEEP[fixture]
    count = 0
    for path, content in sorted(files.items()):
        if path.endswith(".py"):
            text = content if isinstance(content, str) else content.decode()
            for line in _method_lines(text):
                checked(tmp_path, files, path, line)
                count += 1
    assert count >= 4


@pytest.mark.parametrize("fixture", sorted(SWEEP))
def test_duality(tmp_path: Path, fixture: str) -> None:
    """m is defined in overridden(t) exactly when t is defined in overriding(m), for the same
    direct relationship, whenever both directions are supported and untruncated."""
    files = SWEEP[fixture]
    root, rows = _repo(tmp_path, files)
    results = {}
    for row in rows:
        if _parse(root, row) is None:
            continue
        for line in _method_lines((root / row.path).read_bytes().decode()):
            results[(row.path, line)] = analyze_method_overrides(root, row, rows, line)
    up = set()
    for (path, line), result in results.items():
        assert result.overridden_truncated is False
        for entry in result.overridden:
            if entry.status != "defined":
                continue
            for m in entry.implementations:
                other = results[(m.path, m.line)]
                if other.overriding_status != "scanned":
                    continue
                assert other.overriding_truncated is False
                assert any(
                    sub.status == "defined"
                    and (sub.subclass.path, sub.subclass.line)
                    == (result.owner.path, result.owner.line)
                    and any((d.path, d.line) == (path, line) for d in sub.implementations)
                    for sub in other.overriding
                ), ((path, line), (m.path, m.line))
                up.add(((path, line), (m.path, m.line)))
    down = set()
    for (path, line), result in results.items():
        for sub in result.overriding or []:
            if sub.status != "defined":
                continue
            for d in sub.implementations:
                child = results[(d.path, d.line)]
                assert any(
                    e.status == "defined"
                    and e.class_ is not None
                    and (e.class_.path, e.class_.line) == (result.owner.path, result.owner.line)
                    and any((m.path, m.line) == (path, line) for m in e.implementations)
                    for e in child.overridden
                )
                down.add(((d.path, d.line), (path, line)))
    assert up == down and up


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


FILES: dict[str, bytes] = {
    path: content.encode() if isinstance(content, str) else content
    for path, content in REPO.items()
}
TARGET = {"path": "shapes/base.py", "line": 6}


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
        f"/repositories/{repository_id or owner.repository_id}/files/method-overrides"
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


def _json_location(key: Any) -> dict[str, Any] | None:
    if key is None:
        return None
    path, line, kind, name, end_line = key
    return {"path": path, "kind": kind, "name": name, "line": line, "end_line": end_line}


def _json_overriding(entries: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "subclass": _json_location(subclass),
            "site": {"line": s[0], "column": s[1], "name": s[2], "ambiguous": s[3]},
            "status": status,
            "implementations": [_json_location(d) for d in implementations],
            "implementations_ambiguous": several,
        }
        for subclass, s, status, implementations, several in entries
    ]


def _json_overridden(entries: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "site": None if site is None else {"line": site[0], "column": site[1], "name": site[2]},
            "base_kind": kind,
            "class": _json_location(cls),
            "ambiguous": ambiguous,
            "status": status,
            "implementations": [_json_location(d) for d in implementations],
            "implementations_ambiguous": several,
        }
        for site, kind, cls, ambiguous, status, implementations, several in entries
    ]


EMPTY_SUMMARY = dict.fromkeys(MethodOverridesSummary.model_fields, 0)
NOT_SCANNED: dict[str, Any] = {
    "target": None,
    "owner": None,
    "overridden": [],
    "overriding_status": None,
    "overriding": None,
    "summary": EMPTY_SUMMARY,
    "overridden_truncated": False,
    "overriding_truncated": None,
}


@requires_db
async def test_method_overrides_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "scanned",
        "target": _json_location(SHAPE_AREA),
        "owner": _json_location(SHAPE),
        "overridden": _json_overridden(
            [((4, 17, "ABC"), "unresolved", None, False, "unavailable", [], False)]
        ),
        "overriding_status": "scanned",
        "overriding": _json_overriding(SHAPE_AREA_OVERRIDING),
        "summary": {
            **EMPTY_SUMMARY,
            "files_scanned": 4,
            "bases": 1,
            "overridden_unavailable": 1,
            "subclasses": 7,
            "overriding": 2,
            "inheriting": 2,
            "overriding_unsupported": 2,
            "overriding_not_a_method": 1,
        },
        "overridden_truncated": False,
        "overriding_truncated": False,
    }
    square = (await _get(client, alice, {"path": "shapes/square.py", "line": 5})).json()
    assert square["overridden"] == _json_overridden(SQUARE_AREA_OVERRIDDEN)


@requires_db
async def test_endpoint_statuses(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    body = (await _get(client, alice, {"path": "docs/notes.md", "line": 2})).json()
    assert body == {"status": "unsupported_language", **NOT_SCANNED}
    assert read_spy == []  # a non-Python file is never read
    for line in (999, 5, 7):  # past the end, a decorator line, a body line
        body = (await _get(client, alice, {"path": "shapes/base.py", "line": line})).json()
        assert body == {"status": "not_a_definition", **NOT_SCANNED}
    for line in (4, 33, 37):  # a class, a nested function, a module function
        body = (await _get(client, alice, {"path": "shapes/base.py", "line": line})).json()
        assert body == {"status": "not_a_method", **NOT_SCANNED}


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
        assert (await _get(client, alice, {**TARGET, **change})).status_code == 422, change
    for missing in ("path", "line"):
        params = {k: v for k, v in TARGET.items() if k != missing}
        assert (await _get(client, alice, params)).status_code == 422
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _get(client, alice, TARGET, **kwargs)).status_code == 422
    assert read_spy == []
    for path in ("missing.py", "../shapes/base.py", "/shapes/base.py", "SHAPES/BASE.PY"):
        response = await _get(client, alice, {**TARGET, "path": path})
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)


@requires_db
async def test_index_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python rows by path: shapes/__init__.py, shapes/base.py, shapes/square.py, shapes/unrelated.py.
    monkeypatch.setattr(code_method_overrides, "MAX_INDEXED_PATHS", 3)
    body = (await _get(client, alice, TARGET)).json()
    assert (body["overridden_truncated"], body["overriding_truncated"]) == (True, True)
    monkeypatch.setattr(code_method_overrides, "MAX_INDEXED_PATHS", 4)  # exactly all four
    body = (await _get(client, alice, TARGET)).json()
    assert (body["overridden_truncated"], body["overriding_truncated"]) == (False, False)
    assert body["overriding"] == _json_overriding(SHAPE_AREA_OVERRIDING)


@requires_db
async def test_changed_storage_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    (_storage(alice) / "shapes" / "square.py").write_bytes(SQUARE.encode() + b"# tampered\n")
    response = await _get(client, alice, TARGET)  # a subclass file
    assert (response.status_code, response.json()) == (409, CHANGED)
    assert "tampered" not in response.text

    await _ingest(client, alice, FILES)
    (_storage(alice) / "shapes" / "base.py").write_bytes(BASE.encode() + b"# new\n")
    response = await _get(client, alice, TARGET)  # the target
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    (_storage(alice) / "shapes" / "base.py").unlink()  # missing: the bases' file
    response = await _get(client, alice, {"path": "shapes/square.py", "line": 5})
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    root = _storage(alice)
    outside = tmp_path / "outside"
    shutil.copytree(root / "shapes", outside)
    shutil.rmtree(root / "shapes")
    _junction(root / "shapes", outside)
    response = await _get(client, alice, TARGET)
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_symlinked_file_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "shapes" / "base.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(FILES["shapes/base.py"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    response = await _get(client, alice, {"path": "shapes/square.py", "line": 5})
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_storage_swap_between_the_phases_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = code_method_overrides.analyze_type_hierarchy
    root = _storage(alice)

    def hierarchy_then_swap(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        (root / "shapes" / "square.py").write_bytes(b"class Square: pass  # new snapshot\n")
        return result

    monkeypatch.setattr(code_method_overrides, "analyze_type_hierarchy", hierarchy_then_swap)
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
async def test_no_source_internals_or_logged_content(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-53e2"
    files = dict(FILES)
    files["shapes/square.py"] = SQUARE.replace(
        "return 4", f"return '{marker}'  # {marker}"
    ).encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "scanned"
    assert marker not in response.text and marker not in caplog.text
    for word in ("return", "import", "abstractmethod", "staticmethod", "property", "setter"):
        assert word not in response.text
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
        "owner",
        "overridden",
        "overriding_status",
        "overriding",
        "summary",
        "overridden_truncated",
        "overriding_truncated",
    }
    assert set(body["summary"]) == set(EMPTY_SUMMARY) and len(EMPTY_SUMMARY) == 16
    for entry in body["overridden"]:
        assert set(entry) == {
            "site",
            "base_kind",
            "class",
            "ambiguous",
            "status",
            "implementations",
            "implementations_ambiguous",
        }
    for entry in body["overriding"]:
        assert set(entry) == {
            "subclass",
            "site",
            "status",
            "implementations",
            "implementations_ambiguous",
        }
        assert set(entry["site"]) == {"line", "column", "name", "ambiguous"}
        for implementation in entry["implementations"]:
            assert set(implementation) == {"path", "kind", "name", "line", "end_line"}


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(
        client,
        "bob@example.com",
        {"b.py": b"class A:\n    def f(self): pass\nclass B(A):\n    def f(self): pass\n"},
    )
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    params = {"path": "b.py", "line": 2}
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
    assert [e["subclass"]["name"] for e in own["overriding"]] == ["B"]


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
    bob = await _owner(client, "bob@example.com", {"b.py": b"class A:\n    def f(self): pass\n"})
    limiter = _AlwaysLimited()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    params = {"path": "b.py", "line": 2}
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
    assert limiter.hits == [(METHOD_OVERRIDES_LIMIT, alice.user_id)]
    assert read_spy == []  # limited before any snapshot read


@requires_db_and_redis
async def test_rate_limit_is_20_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert METHOD_OVERRIDES_LIMIT == Limit("method-overrides-user", 20, 600)
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
        assert any(k.startswith("ratelimit:method-overrides-user:") for k in keys)
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
