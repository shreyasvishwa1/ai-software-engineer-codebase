"""Python type hierarchy tests (GET .../files/type-hierarchy?path=&line=). Offline.

Scanner tests use a temporary storage directory and need no database. Every semantic test
also checks the Step 45 oracle (Step 45's go-to-definition at every base position, found by
an independent AST walk), Step 47 parity (subclass sites are exactly Step 47's references
that are base positions) and, across the sweep fixtures, the duality of bases and
subclasses. API tests run against the already-migrated throwaway test database
(TEST_DATABASE_URL, "_test") inside rolled-back transactions; ingestion runs inline with the
real worker logic and DATA_DIR is a temporary directory. The rate-limit test uses a
disposable Redis (TEST_REDIS_URL). Junction tests are Windows-only; symlink tests are skipped
where symlinks cannot be created.
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
from collections import Counter
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
from app.schemas.code_type_hierarchy import TypeHierarchySummary
from app.services import code_definition, code_references, code_type_hierarchy, ingestion
from app.services.code_definition import (
    BUILTIN_NAMES,
    _cap,
    _normalize,
    analyze_definition,
    collect_scopes,
    name_tokens,
    resolving_scope,
)
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.code_references import analyze_references
from app.services.code_type_hierarchy import (
    MAX_BASES,
    MAX_SUBCLASSES,
    TYPE_HIERARCHY_LIMIT,
    analyze_type_hierarchy,
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
KINDS = ("resolved", "ambiguous", "not_a_class", "builtin", "unsupported", "unresolved", "dynamic")
SUBCLASS_COUNTS = (
    "subclasses",
    "subclasses_ambiguous",
    "subclass_unsupported",
    "non_base_references",
)


# --- Fixture repository --------------------------------------------------------------------

BASE = "\n".join(
    [
        "class Animal:",  # 1
        "    legs = 4",  # 2
        "",  # 3
        "class Mixin:",  # 4
        "    pass",  # 5
        "",  # 6
        "def factory():",  # 7
        "    return Animal",  # 8
        "",
    ]
)

DOG = "\n".join(
    [
        "from zoo.base import Animal, Mixin, factory",  # 1
        "import zoo.base",  # 2
        "import zoo.base as zb",  # 3
        "from zoo import base",  # 4
        "",  # 5
        "@decorate",  # 6
        "class Dog(Animal, Mixin, metaclass=Meta, flag=True):",  # 7
        "    def bark(self):",  # 8
        "        return Animal()",  # 9
        "",  # 10
        "class Wolf(zoo.base.Animal): pass",  # 11
        "class Fox(zb.Animal, zb.Animal): pass",  # 12
        "class Odd(base.Animal): pass",  # 13
        "class Made(factory): pass",  # 14
        "pet = Animal",  # 15
        "isinstance(pet, Animal)",  # 16
        "",
    ]
)

HORSE = "\n".join(
    [
        "from zoo import Beast",  # 1
        "from typing import Generic",  # 2
        "import abc",  # 3
        "",  # 4
        "class Horse(",  # 5
        "    Beast[int],",  # 6
        "    Generic[T],",  # 7
        "    abc.ABC,",  # 8
        "    object,",  # 9
        "    Exception,",  # 10
        "    *extra,",  # 11
        "    pick(),",  # 12
        "    A if c else B,",  # 13
        "    lambda: 0,",  # 14
        "    1,",  # 15
        "):",  # 16
        "    pass",  # 17
        "",
    ]
)

REPO: dict[str, str | bytes] = {
    "zoo/__init__.py": "from .base import Animal as Beast\n",
    "zoo/base.py": BASE,
    "zoo/cat.py": "from .base import Animal\n\nclass Cat(Animal):\n    pass\n",
    "pets/dog.py": DOG,
    "pets/horse.py": HORSE,
    "pets/puppy.py": "from pets.dog import Dog\nclass Puppy(Dog): pass\n",
    "__init__.py": "from zoo.base import Animal\nclass Root(Animal): pass\n",  # a root __init__
    "docs/notes.md": "class Animal:\n    pass\nclass Dog(Animal): pass\n",
}

ANIMAL = {"path": "zoo/base.py", "kind": "class", "name": "Animal", "line": 1, "end_line": 2}
ANIMAL_KEY = ("zoo/base.py", 1, "class", "Animal", 2)
MIXIN_KEY = ("zoo/base.py", 4, "class", "Mixin", 5)

# ((path, line, kind, name, end_line), (line, column, name, ambiguous)), server order.
ANIMAL_SUBCLASSES = [
    (("__init__.py", 2, "class", "Root", 2), (2, 12, "Animal", False)),
    (("pets/dog.py", 7, "class", "Dog", 9), (7, 11, "Animal", False)),
    (("pets/dog.py", 11, "class", "Wolf", 11), (11, 21, "Animal", False)),
    (("pets/dog.py", 12, "class", "Fox", 12), (12, 14, "Animal", False)),
    (("pets/dog.py", 12, "class", "Fox", 12), (12, 25, "Animal", False)),  # inherited twice
    (("pets/horse.py", 5, "class", "Horse", 17), (6, 5, "Beast", False)),  # re-exported
    (("zoo/cat.py", 3, "class", "Cat", 4), (3, 11, "Animal", False)),  # relative import
]
# (site (line, column, name) or None, kind, [(path, line, kind, name, end_line), ...])
DOG_BASES = [
    ((7, 11, "Animal"), "resolved", [ANIMAL_KEY]),
    ((7, 19, "Mixin"), "resolved", [MIXIN_KEY]),
]
HORSE_BASES = [
    ((6, 5, "Beast"), "resolved", [ANIMAL_KEY]),  # Beast[int]: the subscript's value
    ((7, 5, "Generic"), "unresolved", []),  # typing is outside the repository
    ((8, 9, "ABC"), "unresolved", []),
    ((9, 5, "object"), "builtin", []),
    ((10, 5, "Exception"), "builtin", []),
    (None, "dynamic", []),  # *extra
    (None, "dynamic", []),  # pick()
    (None, "dynamic", []),  # A if c else B
    (None, "dynamic", []),  # lambda: 0
    (None, "dynamic", []),  # 1
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
    return analyze_type_hierarchy(root, _row(rows, path), rows, line, **kw)


def _loc(d: Any) -> tuple[Any, ...]:
    return (d.path, d.line, d.kind, d.name, d.end_line)


def _bases(result: Any) -> list[tuple[Any, ...]]:
    return [
        (
            None if b.site is None else (b.site.line, b.site.column, b.site.name),
            b.kind,
            [_loc(d) for d in b.definitions],
        )
        for b in result.bases
    ]


def _subclasses(result: Any) -> Any:  # None when subclasses were not searched
    if result.subclasses is None:
        return None
    return [
        (_loc(s.subclass), (s.site.line, s.site.column, s.site.name, s.site.ambiguous))
        for s in result.subclasses
    ]


def _kind_counts(result: Any) -> dict[str, int]:
    summary = result.summary.model_dump()
    return {k: summary[f"bases_{k}"] for k in KINDS if summary[f"bases_{k}"]}


def _counts(result: Any, names: tuple[str, ...]) -> dict[str, int]:
    summary = result.summary.model_dump()
    return {name: summary[name] for name in names if summary[name]}


# --- The Step 45 oracle (independent base positions) -----------------------------------------


def _named(base: ast.expr) -> ast.expr | None:
    """The class a base expression names, from Python's rules: a name, an attribute, or
    either subscripted (a generic alias inherits its origin)."""
    value = base.value if isinstance(base, ast.Subscript) else base
    return value if isinstance(value, (ast.Name, ast.Attribute)) else None


def _classes(tree: ast.Module) -> list[tuple[ast.ClassDef, str]]:
    """Every class statement and what it is bound in: module, function or class."""
    out: list[tuple[ast.ClassDef, str]] = []
    stack: list[tuple[ast.AST, str]] = [(n, "module") for n in reversed(tree.body)]
    while stack:
        node, where = stack.pop()
        inner = where
        if isinstance(node, ast.ClassDef):
            out.append((node, where))
            inner = "class"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = "function"
        stack.extend((c, inner) for c in reversed(list(ast.iter_child_nodes(node))))
    return out


def _position(text: str, tokens: list[Any], named: ast.expr) -> tuple[int, int] | None:
    if isinstance(named, ast.Name):
        line = text.split("\n")[named.lineno - 1]
        return named.lineno, len(line.encode()[: named.col_offset].decode()) + 1
    assert isinstance(named, ast.Attribute)
    token = next(
        (t for t in tokens if t.line == named.end_lineno and t.end_byte == named.end_col_offset),
        None,
    )
    if token is None or unicodedata.normalize("NFKC", token.text) != named.attr:
        return None
    return token.line, token.column + 1


def _builtin(table: Any, named: ast.expr) -> bool:
    root = named
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
    """The expected hierarchy of the class on ``line``: Step 45 at every base position."""
    parsed = {}
    for row in rows:
        text = _oracle_text(root, row)
        if text is not None:
            tree = ast.parse(text)
            parsed[row.path] = (text, tree, collect_scopes(tree), name_tokens(text))
    by_path = {row.path: row for row in rows}

    def ask(file: str, named: ast.expr) -> Any:
        position = _position(parsed[file][0], parsed[file][3], named)
        assert position is not None
        return analyze_definition(root, by_path[file], rows, *position)

    node, where = next((c, w) for c, w in _classes(parsed[path][1]) if c.lineno == line)
    location = (path, node.lineno, "class", _cap(node.name), node.end_lineno)
    wanted = location[:4]
    bases: list[tuple[Any, ...]] = []
    truncated = len(node.bases) > MAX_BASES
    for base in node.bases[:MAX_BASES]:
        named = _named(base)
        if named is None:
            bases.append((None, "dynamic", []))
            continue
        result = ask(path, named)
        truncated |= result.truncated
        site = (result.reference.line, result.reference.column, result.reference.name)
        definitions = [_loc(d) for d in result.definitions]
        if result.status == "unsupported":
            kind = "builtin" if _builtin(parsed[path][2], named) else "unsupported"
        elif result.status == "not_found":
            kind = "unresolved"
        else:
            assert result.status in ("resolved", "ambiguous"), result.status
            classes = any(d.kind == "class" for d in result.definitions)
            kind = result.status if classes else "not_a_class"
        bases.append((site, kind, definitions))

    subclasses: list[tuple[Any, ...]] | None = None
    if where != "class":
        subclasses = []
        for file in sorted(parsed) if where == "module" else [path]:
            for cls, _ in _classes(parsed[file][1]):
                for base in cls.bases:
                    named = _named(base)
                    if named is None:
                        continue
                    result = ask(file, named)
                    keys = [(d.path, d.line, d.kind, d.name) for d in result.definitions]
                    if wanted in keys:
                        ref = result.reference
                        subclasses.append(
                            (
                                (file, cls.lineno, "class", _cap(cls.name), cls.end_lineno),
                                (ref.line, ref.column, ref.name, len(keys) > 1),
                            )
                        )
        subclasses.sort(key=lambda entry: (entry[0][:4], entry[1][:2]))
    return {
        "target": location,
        "bases": bases,
        "bases_truncated": truncated,
        "subclasses": subclasses,
        "keywords": len(node.keywords),
        "count": len(node.bases),
    }


def parity(root: Path, rows: list[Any], target: Any, line: int, result: Any) -> None:
    """Step 47: subclass sites are exactly its references that are base positions."""
    refs = analyze_references(root, target, rows, line)
    assert refs.status == "scanned"
    sites = [(s.subclass.path, s.site.line, s.site.column) for s in result.subclasses]
    found = {(r.path, r.line, r.column): r for r in refs.references}
    assert set(sites) <= set(found)
    for entry in result.subclasses:
        ref = found[(entry.subclass.path, entry.site.line, entry.site.column)]
        assert (entry.site.name, entry.site.ambiguous) == (ref.name, ref.ambiguous)
        assert ref.context in ("name", "attribute")
    # Same order: Step 47's order restricted to the base positions (one site may serve once).
    assert sorted(set(sites), key=sites.index) == [
        (r.path, r.line, r.column) for r in refs.references if (r.path, r.line, r.column) in sites
    ]
    summary = result.summary
    assert refs.summary.references == summary.subclasses + summary.non_base_references
    assert refs.summary.ambiguous == (
        summary.subclasses_ambiguous
        + sum(r.ambiguous for r in refs.references if (r.path, r.line, r.column) not in sites)
    )
    assert refs.summary.unsupported == summary.subclass_unsupported
    assert refs.truncated == result.subclasses_truncated


def checked(tmp_path: Path, files: dict[str, str | bytes], path: str, line: int) -> Any:
    """``hierarchy`` plus the Step 45 oracle and Step 47 parity (no limit reached)."""
    root, rows = _repo(tmp_path, files)
    target = _row(rows, path)
    result = analyze_type_hierarchy(root, target, rows, line)
    assert result.status == "scanned", result.status
    expected = oracle(root, rows, path, line)
    assert _loc(result.target) == expected["target"]
    assert _bases(result) == expected["bases"]
    assert _kind_counts(result) == dict(Counter(kind for _, kind, _ in expected["bases"]))
    assert result.summary.bases == expected["count"]
    assert result.summary.keyword_arguments == expected["keywords"]
    assert result.bases_truncated is expected["bases_truncated"]
    if expected["subclasses"] is None:
        assert (result.subclasses_status, result.subclasses) == ("unsupported_target", None)
        assert result.subclasses_truncated is None
        assert _counts(result, SUBCLASS_COUNTS) == {}
    else:
        assert result.subclasses_status == "scanned"
        assert _subclasses(result) == expected["subclasses"]
        assert result.summary.subclasses == len(expected["subclasses"])
        assert result.summary.subclasses_ambiguous == sum(s[1][3] for s in expected["subclasses"])
        parity(root, rows, target, line, result)
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


# --- M, P, Q, R, S, T, U, V, AD, AE, AF, AG, AV: the repository -------------------------------


def test_repository_type_hierarchy(tmp_path: Path) -> None:
    result = checked(tmp_path, REPO, "zoo/base.py", 1)
    assert result.target.model_dump() == ANIMAL
    assert result.bases == [] and result.summary.bases == 0
    assert _subclasses(result) == ANIMAL_SUBCLASSES
    assert result.summary.model_dump() == {
        **dict.fromkeys(TypeHierarchySummary.model_fields, 0),
        "files_scanned": 7,  # docs/notes.md is not Python
        "subclasses": 7,
        "subclass_unsupported": 1,  # base.Animal after "from zoo import base"
        # six import names (zoo/__init__ twice, cat, dog, horse, root __init__), Animal(),
        # pet = Animal, isinstance(pet, Animal), return Animal
        "non_base_references": 10,
    }
    assert (result.subclasses_status, result.bases_truncated, result.subclasses_truncated) == (
        "scanned",
        False,
        False,
    )


def test_direct_multiple_and_keyword_bases(tmp_path: Path) -> None:
    dog = checked(tmp_path, REPO, "pets/dog.py", 7)
    assert dog.target.model_dump() == {
        "path": "pets/dog.py",
        "kind": "class",
        "name": "Dog",
        "line": 7,  # the class line, never the decorator's
        "end_line": 9,
    }
    assert _bases(dog) == DOG_BASES  # source order; @decorate and the keywords are not bases
    assert (dog.summary.bases, dog.summary.keyword_arguments) == (2, 2)
    assert _kind_counts(dog) == {"resolved": 2}
    assert _subclasses(dog) == [(("pets/puppy.py", 2, "class", "Puppy", 2), (2, 13, "Dog", False))]
    assert dog.summary.non_base_references == 1  # puppy's import


def test_base_forms(tmp_path: Path) -> None:
    def bases(line: int) -> list[tuple[Any, ...]]:
        return _bases(checked(tmp_path, REPO, "pets/dog.py", line))

    assert bases(11) == [((11, 21, "Animal"), "resolved", [ANIMAL_KEY])]  # zoo.base.Animal
    assert bases(12) == [  # zb.Animal twice: duplicates kept
        ((12, 14, "Animal"), "resolved", [ANIMAL_KEY]),
        ((12, 25, "Animal"), "resolved", [ANIMAL_KEY]),
    ]
    assert bases(13) == [((13, 16, "Animal"), "unsupported", [])]  # from zoo import base
    assert bases(14) == [
        ((14, 12, "factory"), "not_a_class", [("zoo/base.py", 7, "function", "factory", 8)])
    ]
    cat = checked(tmp_path, REPO, "zoo/cat.py", 3)  # from .base import Animal
    assert _bases(cat) == [((3, 11, "Animal"), "resolved", [ANIMAL_KEY])]
    root = checked(tmp_path, REPO, "__init__.py", 2)  # a root __init__.py
    assert _bases(root) == [((2, 12, "Animal"), "resolved", [ANIMAL_KEY])]
    assert root.subclasses == []


def test_builtin_unresolved_dynamic_and_subscripted_bases(tmp_path: Path) -> None:
    horse = checked(tmp_path, REPO, "pets/horse.py", 5)
    assert (horse.target.line, horse.target.end_line) == (5, 17)
    assert _bases(horse) == HORSE_BASES
    assert _kind_counts(horse) == {"resolved": 1, "unresolved": 2, "builtin": 2, "dynamic": 5}
    assert (horse.summary.bases, horse.summary.keyword_arguments) == (10, 0)
    assert horse.bases_truncated is False


def test_relative_imports_are_relative(tmp_path: Path) -> None:
    """Two packages define base.py: a relative import names its own package's module only,
    where the absolute ``from base import ...`` would match both."""
    files: dict[str, str | bytes] = {
        "a/base.py": "class Animal: pass\n",
        "b/base.py": "class Animal: pass\n",
        "a/cat.py": "from .base import Animal\nclass Cat(Animal): pass\n",
        "c.py": "from base import Animal\nclass D(Animal): pass\n",
    }
    a_animal, b_animal = (
        ("a/base.py", 1, "class", "Animal", 1),
        ("b/base.py", 1, "class", "Animal", 1),
    )
    cat = checked(tmp_path, files, "a/cat.py", 2)
    assert _bases(cat) == [((2, 11, "Animal"), "resolved", [a_animal])]
    d = checked(tmp_path, files, "c.py", 2)
    assert _bases(d) == [((2, 9, "Animal"), "ambiguous", [a_animal, b_animal])]
    b = checked(tmp_path, files, "b/base.py", 1)
    assert _subclasses(b) == [(("c.py", 2, "class", "D", 2), (2, 9, "Animal", True))]  # never Cat


def test_aliased_and_re_exported_bases(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "lib/__init__.py": "from .core import Base as Exported\n",
        "lib/core.py": "class Base: pass\n",
        "a.py": "from lib.core import Base as B\nfrom lib import Exported\nimport lib.core as lc\n"
        "from lib import Exported as E2\n"
        "class One(B): pass\nclass Two(Exported): pass\nclass Three(lc.Base): pass\n"
        "class Four(E2): pass\n",
    }
    base = ("lib/core.py", 1, "class", "Base", 1)
    for line, site in ((5, (5, 11, "B")), (6, (6, 11, "Exported")), (8, (8, 12, "E2"))):
        assert _bases(checked(tmp_path, files, "a.py", line)) == [(site, "resolved", [base])]
    assert _bases(checked(tmp_path, files, "a.py", 7)) == [((7, 16, "Base"), "resolved", [base])]
    result = checked(tmp_path, files, "lib/core.py", 1)
    assert [(s[0][3], s[1][2]) for s in _subclasses(result)] == [
        ("One", "B"),
        ("Two", "Exported"),
        ("Three", "Base"),
        ("Four", "E2"),
    ]


# --- I, J, K, L, M, N, O: targets ------------------------------------------------------------


def test_targets_and_statuses(tmp_path: Path) -> None:
    source = (
        "@decorate\n"  # 1
        "class C(B):\n"  # 2
        "    x = 1\n"  # 3
        "def f(): pass\n"  # 4
        "async def g(): pass\n"  # 5
        "class D:\n"  # 6
        "    def m(self): pass\n"  # 7
        "\n"  # 8
    )
    files: dict[str, str | bytes] = {"m.py": source}
    empty = dict.fromkeys(TypeHierarchySummary.model_fields, 0)
    not_scanned: dict[str, Any] = {
        "target": None,
        "bases": [],
        "subclasses_status": None,
        "subclasses": None,
        "summary": empty,
        "bases_truncated": False,
        "subclasses_truncated": None,
    }
    for line in (1, 3, 8, 9, 999):  # decorator, body, blank, past the end
        result = hierarchy(tmp_path, files, "m.py", line)
        assert result.model_dump() == {"status": "not_a_definition", **not_scanned}
    for line in (4, 5, 7):  # def, async def, method
        result = hierarchy(tmp_path, files, "m.py", line)
        assert result.model_dump() == {"status": "not_a_class", **not_scanned}
    assert hierarchy(tmp_path, {"m.py": ""}, "m.py", 1).status == "not_a_definition"
    assert hierarchy(tmp_path, {"m.py": "x = 1\n"}, "m.py", 1).status == "not_a_definition"
    scanned = checked(tmp_path, files, "m.py", 2)
    assert (scanned.target.name, scanned.target.line) == ("C", 2)
    assert _bases(scanned) == [((2, 9, "B"), "unresolved", [])]
    assert checked(tmp_path, files, "m.py", 6).bases == []


def test_target_file_statuses(tmp_path: Path, read_spy: list[str]) -> None:
    def status(content: str | bytes, line: int = 1) -> str:
        result = hierarchy(tmp_path, {"m.py": content}, "m.py", line)
        if result.status != "scanned":
            assert (result.target, result.bases, result.subclasses) == (None, [], None)
            assert set(result.summary.model_dump().values()) == {0}
        return result.status

    assert status("class (:\n") == "syntax_error"
    assert status("class A:\n    '''\n") == "syntax_error"
    assert status(b"\x00\x01") == "binary"
    assert status(b"\xff\xfe") == "binary"
    body = b"class T: pass\nclass U(T): pass\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    result = hierarchy(tmp_path, {"m.py": exactly}, "m.py", 1)
    assert _subclasses(result) == [(("m.py", 2, "class", "U", 2), (2, 9, "T", False))]
    assert status(exactly + b"#") == "too_large"
    read_spy.clear()
    assert status(os.urandom(MAX_CONTENT_BYTES)) in ("binary", "too_large")
    assert read_spy == ["m.py"]  # exactly 1 MiB is opened
    read_spy.clear()
    assert status(os.urandom(MAX_CONTENT_BYTES) + b"x") == "too_large"
    assert read_spy == []  # over 1 MiB is never opened


SCOPES: dict[str, str | bytes] = {
    "s.py": (
        "class Base: pass\n"  # 1
        "class Outer:\n"  # 2
        "    class Inner(Base): pass\n"  # 3
        "    class Deep(Inner): pass\n"  # 4
        "def build():\n"  # 5
        "    class Base: pass\n"  # 6
        "    class Local(Base): pass\n"  # 7
        "    class Sub(Local): pass\n"  # 8
        "    return Sub\n"  # 9
        "class Leaf(Base): pass\n"  # 10
        "class Nested(Outer.Inner): pass\n"  # 11
        "def f(Base):\n"  # 12
        "    class P(Base): pass\n"  # 13
        "    return P\n"  # 14
    ),
    "t.py": "from s import Base as B, build, Outer\nclass T(B): pass\nclass U(build): pass\n"
    "class V(Outer.Inner): pass\n",
}
S_BASE = ("s.py", 1, "class", "Base", 1)


def test_module_level_target_and_shadowing(tmp_path: Path) -> None:
    result = checked(tmp_path, SCOPES, "s.py", 1)
    assert _subclasses(result) == [
        (("s.py", 3, "class", "Inner", 3), (3, 17, "Base", False)),  # class scope: module Base
        (("s.py", 10, "class", "Leaf", 10), (10, 12, "Base", False)),
        (("t.py", 2, "class", "T", 2), (2, 9, "B", False)),
    ]  # the local Base of build() and the parameter Base of f() are not this class
    assert _bases(checked(tmp_path, SCOPES, "s.py", 13)) == [((13, 13, "Base"), "unsupported", [])]
    assert _bases(checked(tmp_path, SCOPES, "t.py", 3)) == [
        ((3, 9, "build"), "not_a_class", [("s.py", 5, "function", "build", 9)])
    ]


def test_function_local_target_scans_only_its_file(tmp_path: Path, read_spy: list[str]) -> None:
    local = checked(tmp_path, SCOPES, "s.py", 7)
    assert local.target.model_dump() == {
        "path": "s.py",
        "kind": "class",
        "name": "Local",
        "line": 7,
        "end_line": 7,
    }
    assert _bases(local) == [((7, 17, "Base"), "resolved", [("s.py", 6, "class", "Base", 6)])]
    assert local.subclasses_status == "scanned"
    assert _subclasses(local) == [(("s.py", 8, "class", "Sub", 8), (8, 15, "Local", False))]
    assert local.summary.files_scanned == 1
    read_spy.clear()
    hierarchy(tmp_path, SCOPES, "s.py", 7)
    assert read_spy == ["s.py"]


def test_nested_class_target_has_bases_but_unsupported_subclasses(tmp_path: Path) -> None:
    inner = checked(tmp_path, SCOPES, "s.py", 3)
    assert inner.target.model_dump() == {
        "path": "s.py",
        "kind": "class",
        "name": "Inner",
        "line": 3,
        "end_line": 3,
    }
    assert _bases(inner) == [((3, 17, "Base"), "resolved", [S_BASE])]
    assert (inner.subclasses_status, inner.subclasses, inner.subclasses_truncated) == (
        "unsupported_target",
        None,
        None,
    )  # never an empty list: Deep, Nested and V do inherit it
    assert _counts(inner, SUBCLASS_COUNTS) == {}
    deep = checked(tmp_path, SCOPES, "s.py", 4)  # bases evaluated in Outer's class scope
    assert _bases(deep) == [((4, 16, "Inner"), "resolved", [("s.py", 3, "class", "Inner", 3)])]
    nested = checked(tmp_path, SCOPES, "s.py", 11)  # Outer.Inner: no static resolution
    assert _bases(nested) == [((11, 20, "Inner"), "unsupported", [])]


# --- W, AH: ambiguity ------------------------------------------------------------------------


AMBIGUOUS: dict[str, str | bytes] = {
    "zoo_base.py": "class Animal: pass\n",
    "lib/__init__.py": "class thing: pass\n",
    "lib/thing.py": "",
    "amb.py": (
        "try:\n"  # 1
        "    from zoo_base import Animal\n"  # 2
        "except ImportError:\n"  # 3
        "    class Animal: pass\n"  # 4
        "from lib import thing\n"  # 5
        "class Pet(Animal): pass\n"  # 6
        "class Mod(thing): pass\n"  # 7
        "if flag:\n"  # 8
        "    class Base: pass\n"  # 9
        "else:\n"  # 10
        "    class Base: pass\n"  # 11
        "class Both(Base, Base[int]): pass\n"  # 12
    ),
    "both.pyi": "class Stub: pass\n",
    "both.py": "class Stub: pass\n",
    "use.py": "from both import Stub\nclass S(Stub): pass\n",
}


def test_ambiguous_bases_keep_every_candidate(tmp_path: Path) -> None:
    pet = checked(tmp_path, AMBIGUOUS, "amb.py", 6)
    assert _bases(pet) == [
        (
            (6, 11, "Animal"),
            "ambiguous",
            [("amb.py", 4, "class", "Animal", 4), ("zoo_base.py", 1, "class", "Animal", 1)],
        )
    ]
    mod = checked(tmp_path, AMBIGUOUS, "amb.py", 7)  # a module candidate counts (Step 47)
    assert _bases(mod) == [
        (
            (7, 11, "thing"),
            "ambiguous",
            [
                ("lib/__init__.py", 1, "class", "thing", 1),
                ("lib/thing.py", 1, "module", "thing", 1),
            ],
        )
    ]
    both = checked(tmp_path, AMBIGUOUS, "amb.py", 12)
    candidates = [("amb.py", 9, "class", "Base", 9), ("amb.py", 11, "class", "Base", 11)]
    assert _bases(both) == [
        ((12, 12, "Base"), "ambiguous", candidates),
        ((12, 18, "Base"), "ambiguous", candidates),
    ]
    assert _kind_counts(both) == {"ambiguous": 2}
    stub = checked(tmp_path, AMBIGUOUS, "use.py", 2)  # .py and .pyi
    assert _bases(stub) == [
        (
            (2, 9, "Stub"),
            "ambiguous",
            [("both.py", 1, "class", "Stub", 1), ("both.pyi", 1, "class", "Stub", 1)],
        )
    ]


def test_ambiguous_subclasses(tmp_path: Path) -> None:
    for path, line in (("zoo_base.py", 1), ("amb.py", 4)):
        result = checked(tmp_path, AMBIGUOUS, path, line)
        assert _subclasses(result) == [(("amb.py", 6, "class", "Pet", 6), (6, 11, "Animal", True))]
        assert result.summary.subclasses_ambiguous == 1
    thing = checked(tmp_path, AMBIGUOUS, "lib/__init__.py", 1)
    assert _subclasses(thing) == [(("amb.py", 7, "class", "Mod", 7), (7, 11, "thing", True))]
    base = checked(tmp_path, AMBIGUOUS, "amb.py", 9)
    assert _subclasses(base) == [
        (("amb.py", 12, "class", "Both", 12), (12, 12, "Base", True)),
        (("amb.py", 12, "class", "Both", 12), (12, 18, "Base", True)),  # Base[int]
    ]


# --- AB, AC, AU: subscripts, keywords, PEP 695 -------------------------------------------------


def test_subscripted_bases_resolve_their_value(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "g.py": "import g2\nfrom g2 import Box\nclass Base: pass\nT = 1\n"
        "class A(Base[T], g2.Box[int, T], Box[Base]): pass\n",
        "g2.py": "class Box: pass\n",
    }
    result = checked(tmp_path, files, "g.py", 5)
    box = ("g2.py", 1, "class", "Box", 1)
    assert _bases(result) == [
        ((5, 9, "Base"), "resolved", [("g.py", 3, "class", "Base", 3)]),
        ((5, 21, "Box"), "resolved", [box]),
        ((5, 34, "Box"), "resolved", [box]),
    ]
    base = checked(tmp_path, files, "g.py", 3)  # Box[Base]: Base is an argument, not a base
    assert _subclasses(base) == [(("g.py", 5, "class", "A", 5), (5, 9, "Base", False))]
    assert base.summary.non_base_references == 1


def test_metaclass_and_keywords_are_not_bases(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class Meta(type): pass\nclass Base: pass\n"
        "class A(Base, metaclass=Meta, **options): pass\n"
        "class B(metaclass=Meta): pass\n"
    }
    a = checked(tmp_path, files, "m.py", 3)
    assert _bases(a) == [((3, 9, "Base"), "resolved", [("m.py", 2, "class", "Base", 2)])]
    assert (a.summary.bases, a.summary.keyword_arguments) == (1, 2)
    b = checked(tmp_path, files, "m.py", 4)
    assert (b.bases, b.summary.keyword_arguments) == ([], 1)
    meta = checked(tmp_path, files, "m.py", 1)
    assert _bases(meta) == [((1, 12, "type"), "builtin", [])]
    assert meta.subclasses == []  # metaclass=Meta is a reference, never inheritance
    assert meta.summary.non_base_references == 2


def test_pep_695_classes(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "p.py": "class Base[T]: pass\nclass Child[U: Base](Base[U]): pass\n"
        "type Alias = Base\nclass Plain(Base): pass\n"
    }
    child = checked(tmp_path, files, "p.py", 2)
    assert _bases(child) == [((2, 22, "Base"), "resolved", [("p.py", 1, "class", "Base", 1)])]
    base = checked(tmp_path, files, "p.py", 1)
    assert _subclasses(base) == [
        (("p.py", 2, "class", "Child", 2), (2, 22, "Base", False)),
        (("p.py", 4, "class", "Plain", 4), (4, 13, "Base", False)),
    ]  # the type parameter bound is never resolved (Step 45)


def test_dynamic_bases_are_never_resolved(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "d.py": "class B: pass\nclass C: pass\ndef get_base(): return B\n"
        "class X(*[B], get_base(), B if c else C, (lambda: B)(), B.mro()[0], [B][0], 'B'): pass\n"
    }
    result = checked(tmp_path, files, "d.py", 4)
    assert _bases(result) == [(None, "dynamic", [])] * 7
    assert result.summary.bases_dynamic == 7
    b = checked(tmp_path, files, "d.py", 1)
    assert b.subclasses == []  # B inside dynamic bases is never a base position
    assert b.summary.non_base_references == 6


# --- AI: ordering ------------------------------------------------------------------------------


def test_order_is_source_order_and_path_line_kind_name_site(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "z.py": "from a import Root\nclass Z2(Root): pass\nclass Z1(Root): pass\n",
        "a.py": "class Root: pass\nclass B(Root, Root): pass\nclass A(Root): pass\n",
        "m/__init__.py": "from a import Root\nclass M(Root): pass\n",
        "o.py": "from a import Root\nclass C3(Root): pass\nclass C1(C3, Root): pass\n",
    }
    result = checked(tmp_path, files, "a.py", 1)
    assert [(s[0][0], s[0][1], s[0][3], s[1][1]) for s in _subclasses(result)] == [
        ("a.py", 2, "B", 9),
        ("a.py", 2, "B", 15),
        ("a.py", 3, "A", 9),
        ("m/__init__.py", 2, "M", 9),
        ("o.py", 2, "C3", 10),
        ("o.py", 3, "C1", 14),
        ("z.py", 2, "Z2", 10),
        ("z.py", 3, "Z1", 10),
    ]
    c1 = checked(tmp_path, files, "o.py", 3)
    assert [b[0][2] for b in _bases(c1)] == ["C3", "Root"]  # never sorted


# --- AR, AS, AT: text forms ----------------------------------------------------------------


def test_unicode_tabs_crlf_and_nfkc(tmp_path: Path) -> None:
    source = (
        "# é\r\n"  # 1
        "class Base: pass\r\n"  # 2
        "class Kid(\uff22ase): pass\r\n"  # 3: a fullwidth B, NFKC-normalized to Base
        "if True:\r\n"  # 4
        "\tclass Tab(Base): pass\r\n"  # 5
        "class \u00e9t\u00e9(Base): pass\r\n"  # 6
        "class Sub(\u00e9t\u00e9): pass\r\n"  # 7
    )
    files: dict[str, str | bytes] = {"u.py": source.encode()}
    base = checked(tmp_path, files, "u.py", 2)
    assert _subclasses(base) == [
        (("u.py", 3, "class", "Kid", 3), (3, 11, "Base", False)),
        (("u.py", 5, "class", "Tab", 5), (5, 12, "Base", False)),
        (("u.py", 6, "class", "\u00e9t\u00e9", 6), (6, 11, "Base", False)),
    ]
    sub = checked(tmp_path, files, "u.py", 7)  # code-point columns
    assert _bases(sub) == [
        ((7, 11, "\u00e9t\u00e9"), "resolved", [("u.py", 6, "class", "\u00e9t\u00e9", 6)])
    ]


def test_long_names_are_capped(tmp_path: Path) -> None:
    long = "B" * 300
    files: dict[str, str | bytes] = {"m.py": f"class {long}: pass\nclass C({long}): pass\n"}
    base = checked(tmp_path, files, "m.py", 1)
    assert base.target.name == "B" * 200
    assert base.subclasses[0].site.name == "B" * 200
    assert checked(tmp_path, files, "m.py", 2).bases[0].site.name == "B" * 200


# --- AJ, AP, AQ: limits ------------------------------------------------------------------------


def test_base_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class A: pass\nclass B: pass\nclass C(A, B, A, x()): pass\n"
    }
    monkeypatch.setattr(code_type_hierarchy, "MAX_BASES", 4)  # exactly all four
    result = hierarchy(tmp_path, files, "m.py", 3)
    assert (len(result.bases), result.summary.bases, result.bases_truncated) == (4, 4, False)
    monkeypatch.setattr(code_type_hierarchy, "MAX_BASES", 2)
    result = hierarchy(tmp_path, files, "m.py", 3)
    assert [b.site.name for b in result.bases] == ["A", "B"]  # the first, in source order
    assert result.summary.bases == 4  # counted before the cap
    assert _kind_counts(result) == {"resolved": 2}  # only the examined bases are classified
    assert (result.bases_truncated, result.subclasses_truncated) == (True, False)
    assert MAX_BASES == 200


def test_subclass_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class A: pass\nclass X(A): pass\nclass Y(A, A): pass\n",
        "n.py": "from m import A\nclass Z(A): pass\n",
    }
    monkeypatch.setattr(code_type_hierarchy, "MAX_SUBCLASSES", 4)  # exactly all four
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert (len(result.subclasses), result.subclasses_truncated) == (4, False)
    monkeypatch.setattr(code_type_hierarchy, "MAX_SUBCLASSES", 3)
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert [(s.subclass.name, s.site.column) for s in result.subclasses] == [
        ("X", 9),
        ("Y", 9),
        ("Y", 12),
    ]
    assert result.summary.subclasses == 4  # counted before the cap
    assert (result.subclasses_truncated, result.bases_truncated) == (True, False)
    assert MAX_SUBCLASSES == 500


def test_index_truncation_sets_both(tmp_path: Path) -> None:
    result = hierarchy(tmp_path, REPO, "zoo/base.py", 1, index_truncated=True)
    assert (result.bases_truncated, result.subclasses_truncated) == (True, True)
    nested = hierarchy(tmp_path, SCOPES, "s.py", 3, index_truncated=True)
    assert (nested.bases_truncated, nested.subclasses_truncated) == (True, None)


def test_read_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "from b import Base\nclass Kid(Base): pass\n",
        "b.py": "class Base: pass\n",
        "c.py": "from b import Base\nclass Other(Base): pass\n" + "#" * 100 + "\n",
    }
    root, rows = _repo(tmp_path, files)
    a, b = _row(rows, "a.py"), _row(rows, "b.py")
    # The target and the base's module fit; c.py does not: only the subclasses are cut.
    monkeypatch.setattr(code_type_hierarchy, "MAX_SCANNED_BYTES", a.size_bytes + b.size_bytes)
    result = analyze_type_hierarchy(root, a, rows, 2)
    assert _bases(result) == [((2, 11, "Base"), "resolved", [("b.py", 1, "class", "Base", 1)])]
    assert (result.bases_truncated, result.subclasses_truncated) == (False, True)
    assert "c.py" not in read_spy  # checked before the file is opened
    # Only the target fits: the base's module is never read, so the base is cut too.
    monkeypatch.setattr(code_type_hierarchy, "MAX_SCANNED_BYTES", a.size_bytes)
    result = analyze_type_hierarchy(root, a, rows, 2)
    assert result.bases_truncated is True
    assert result.bases[0].kind != "resolved"


def test_parse_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "from b import Base\nclass Kid(Base): pass\n",
        "b.py": "class Base: pass\n" + "#" * 50 + "\n",
    }
    root, rows = _repo(tmp_path, files)
    a = _row(rows, "a.py")
    monkeypatch.setattr(code_type_hierarchy, "MAX_PARSED_BYTES", a.size_bytes)
    result = analyze_type_hierarchy(root, a, rows, 2)  # b.py is never parsed
    assert result.bases_truncated is True
    assert result.bases[0].kind != "resolved"
    monkeypatch.setattr(code_type_hierarchy, "MAX_PARSED_BYTES", 10**9)
    full = analyze_type_hierarchy(root, a, rows, 2)
    assert (full.bases[0].kind, full.bases_truncated) == ("resolved", False)


def test_files_that_cannot_be_analyzed_are_counted(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "class Base: pass\n",
        "big.py": b"from a import Base\nclass K(Base): pass\n" + b"#" * MAX_OUTLINE_BYTES,
        "bad.py": "from a import Base\nclass (Base:\n",
        "bin.py": b"\x00Base",
    }
    result = hierarchy(tmp_path, files, "a.py", 1)
    summary = result.summary
    assert (summary.too_large, summary.syntax_error, summary.binary) == (1, 1, 1)
    assert result.subclasses == []


def test_candidate_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {"m.py": "class A: pass\nclass B(A): pass\nclass C(A): pass\n"}
    monkeypatch.setattr(code_references, "MAX_CANDIDATES", 2)  # "A" of line 1 and line 2
    result = hierarchy(tmp_path, files, "m.py", 1)
    assert [s.subclass.name for s in result.subclasses] == ["B"]
    assert (result.subclasses_truncated, result.bases_truncated) == (True, False)


def test_alias_name_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "class A: pass\n",
        "b.py": "from a import A as B\nclass K(B): pass\n",
    }
    assert [s.subclass.name for s in hierarchy(tmp_path, files, "a.py", 1).subclasses] == ["K"]
    monkeypatch.setattr(code_references, "MAX_ALIAS_NAMES", 1)
    result = hierarchy(tmp_path, files, "a.py", 1)
    assert (result.subclasses, result.subclasses_truncated) == ([], True)


def test_definition_list_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_definition, "MAX_DEFINITIONS", 1)
    result = hierarchy(tmp_path, AMBIGUOUS, "amb.py", 6)
    assert _bases(result) == [
        ((6, 11, "Animal"), "ambiguous", [("amb.py", 4, "class", "Animal", 4)])
    ]  # Step 45's list, cut as Step 45 cuts it; still ambiguous
    assert result.bases_truncated is True
    cut = hierarchy(tmp_path, AMBIGUOUS, "zoo_base.py", 1)  # cut from Step 45's list
    assert (cut.subclasses, cut.subclasses_truncated) == ([], True)
    root, rows = _repo(tmp_path, AMBIGUOUS)
    refs = analyze_references(root, _row(rows, "zoo_base.py"), rows, 1)
    # Step 47 cuts it the same way; only the unambiguous import name remains
    assert ([r.context for r in refs.references], refs.truncated) == (["import"], True)


def test_module_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "class A: pass\n",
        "b.py": "from a import A\nclass K(A): pass\n",
    }
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 0)
    k = hierarchy(tmp_path, files, "b.py", 2)
    assert _bases(k) == [((2, 9, "A"), "unsupported", [])]
    assert k.bases_truncated is True
    a = hierarchy(tmp_path, files, "a.py", 1)
    assert (a.subclasses, a.summary.subclass_unsupported, a.subclasses_truncated) == ([], 2, True)
    root, rows = _repo(tmp_path, files)  # exactly Step 45's answer at the base
    step45 = analyze_definition(root, _row(rows, "b.py"), rows, 2, 9)
    assert (step45.status, step45.truncated) == ("unsupported", True)


def test_import_hops_follow_step_45(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "class A: pass\n",
        "b.py": "from a import A\n",
        "c.py": "from b import A\nclass K(A): pass\n",
    }
    assert hierarchy(tmp_path, files, "c.py", 2).bases[0].kind == "resolved"
    monkeypatch.setattr(code_definition, "MAX_IMPORT_HOPS", 1)
    result = hierarchy(tmp_path, files, "c.py", 2)
    assert result.bases[0].kind != "resolved"
    root, rows = _repo(tmp_path, files)
    assert analyze_definition(root, _row(rows, "c.py"), rows, 2, 9).status != "resolved"


# --- AZ: no execution ------------------------------------------------------------------------


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"  # 1
        "import os, subprocess, socket\n"  # 2
        "os.system('echo')\nsubprocess.run(['echo'])\n"  # 3, 4
        "socket.create_connection(('example.invalid', 80))\n"  # 5
        "__import__('os').system('echo')\neval('1')\nexec('x = 1')\n"  # 6, 7, 8
        "def get_base():\n    open('side-effect', 'w')\n    return object\n"  # 9-11
        "class Base(metaclass=get_base()): pass\n"  # 12
        "class Target(Base, get_base(), *eval('()')): pass\n"  # 13
    )
    files: dict[str, str | bytes] = {
        "m.py": "from evil import Base\nclass Kid(Base): pass\n",
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
    result = analyze_type_hierarchy(root, _row(rows, "evil.py"), rows, 13)
    base = analyze_type_hierarchy(root, _row(rows, "evil.py"), rows, 12)
    monkeypatch.undo()
    assert [b.kind for b in result.bases] == ["resolved", "dynamic", "dynamic"]
    assert base.subclasses is not None
    assert [(s.subclass.path, s.subclass.name) for s in base.subclasses] == [
        ("evil.py", "Target"),
        ("m.py", "Kid"),
    ]
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


# --- AK, AL, AM, AN: snapshot ------------------------------------------------------------------


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class T: pass\n",
        "n.py": "from m import T\nclass K(T): pass\n",
    }
    root, rows = _repo(tmp_path, files)
    (root / "n.py").write_bytes(
        b"from m import T\nclass K(T): pass  # changed\n"
    )  # a subclass file
    with pytest.raises(FileContentError) as raised:
        analyze_type_hierarchy(root, rows[0], rows, 1)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "n.py").unlink()  # missing
    with pytest.raises(FileContentError) as raised:
        analyze_type_hierarchy(root, rows[0], rows, 1)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    root, rows = _repo(tmp_path, files)
    (root / "m.py").write_bytes(b"class T: pass  # changed\n")  # the target
    with pytest.raises(FileContentError) as raised:
        analyze_type_hierarchy(root, rows[0], rows, 1)
    assert raised.value.status_code == 409


def test_a_module_read_only_for_a_base_is_verified(tmp_path: Path, read_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "from lib import Base\nclass Outer:\n    class Inner(Base): pass\n",
        "lib.py": "class Base:\n    pass\n",
    }
    root, rows = _repo(tmp_path, files)
    target = _row(rows, "m.py")
    result = analyze_type_hierarchy(root, target, rows, 3)  # nested: no subclass scan
    assert [d.path for d in result.bases[0].definitions] == ["lib.py"]
    assert read_spy == ["m.py", "lib.py"]
    (root / "lib.py").write_bytes(b"class Base:\n    pass  # changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_type_hierarchy(root, target, rows, 3)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_symlinked_module_fails_the_request(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "from lib import Base\nclass T(Base): pass\n",
        "lib.py": "class Base: pass\n",
    }
    root, rows = _repo(tmp_path, files)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"class Base: pass\n")
    (root / "lib.py").unlink()
    try:
        os.symlink(outside, root / "lib.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    with pytest.raises(FileContentError) as raised:
        analyze_type_hierarchy(root, _row(rows, "m.py"), rows, 2)
    assert raised.value.status_code == 409


def test_junctioned_package_fails_the_request(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    files: dict[str, str | bytes] = {
        "m.py": "class T: pass\n",
        "pkg/use.py": "from m import T\nclass K(T): pass\n",
    }
    root, rows = _repo(tmp_path, files)
    outside = tmp_path / "outside"
    shutil.copytree(root / "pkg", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pkg")
    _winapi.CreateJunction(str(outside), str(root / "pkg"))  # type: ignore[attr-defined]
    with pytest.raises(FileContentError) as raised:
        analyze_type_hierarchy(root, _row(rows, "m.py"), rows, 1)
    assert raised.value.status_code == 409


def test_nothing_is_cached_across_requests(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class T: pass\n",
        "n.py": "from m import T\nclass K(T): pass\n",
    }
    root, rows = _repo(tmp_path, files)
    first = analyze_type_hierarchy(root, rows[0], rows, 1)
    assert first.subclasses is not None
    assert [s.subclass.name for s in first.subclasses] == ["K"]
    changed = b"from m import T\nT = None\nclass K(T): pass\n"  # now a variable
    (root / "n.py").write_bytes(changed)
    rows[1] = SimpleNamespace(
        path="n.py", size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest()
    )
    second = analyze_type_hierarchy(root, rows[0], rows, 1)
    assert second.subclasses == []


def test_memo_keeps_scopes_apart(tmp_path: Path) -> None:
    """One file, one name, two scopes: each base resolves in its own scope."""
    files: dict[str, str | bytes] = {
        "m.py": "class Base: pass\nclass A(Base): pass\ndef f():\n    class Base: pass\n"
        "    class B(Base): pass\nclass C(Base): pass\n"
    }
    module_base = checked(tmp_path, files, "m.py", 1)
    assert [s.subclass.name for s in module_base.subclasses] == ["A", "C"]
    local_base = checked(tmp_path, files, "m.py", 4)
    assert [s.subclass.name for s in local_base.subclasses] == ["B"]


# --- The Step 45 oracle sweep, Step 47 parity and duality -------------------------------------


SWEEP: dict[str, dict[str, str | bytes]] = {
    "repo": REPO,
    "scopes": SCOPES,
    "ambiguous": AMBIGUOUS,
    "mixed": {
        "pkg/__init__.py": "class Thing: pass\nfrom . import util\n",
        "pkg/util.py": "import pkg\nclass U(pkg.Thing): pass\nclass V(pkg.util.U, U): pass\n",
        "m.py": (
            "from pkg import Thing, util\nimport pkg.util as u\n"
            "class C(Thing, u.U, util.U):\n    class D(C if x else Thing): pass\n"
            "    class E(Thing): pass\n"
            "def f():\n    class L(Thing, u.V): pass\n    return L\n"
            "type T = Thing\nclass G[X: Thing](Thing[X]): pass\n"
        ),
        "star.py": "from pkg import *\nclass S(Thing, object): pass\nclass object: pass\n"
        "class O(object): pass\n",
    },
}


def _class_lines(text: str) -> list[int]:
    return [n.lineno for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ClassDef)]


@pytest.mark.parametrize("fixture", sorted(SWEEP))
def test_step_45_oracle_sweep(tmp_path: Path, fixture: str) -> None:
    """Every class of every file: bases, subclasses and counts are exactly Step 45's answers
    at the base positions found by an independent walk; subclasses are Step 47's."""
    files = SWEEP[fixture]
    checked_targets = 0
    for path, content in sorted(files.items()):
        if not path.endswith((".py", ".pyi")):
            continue
        text = content if isinstance(content, str) else content.decode()
        for line in _class_lines(text):
            checked(tmp_path, files, path, line)
            checked_targets += 1
    assert checked_targets >= 4


@pytest.mark.parametrize("fixture", sorted(SWEEP))
def test_duality(tmp_path: Path, fixture: str) -> None:
    """C has base site S resolving to class D exactly when D's subclasses hold (C, S), for
    every class of the fixture whose subclasses are searched."""
    files = SWEEP[fixture]
    root, rows = _repo(tmp_path, files)
    results: dict[tuple[str, int], Any] = {}
    for row in rows:
        text = _oracle_text(root, row)
        for line in _class_lines(text) if text is not None else []:
            results[(row.path, line)] = analyze_type_hierarchy(root, row, rows, line)
    edges_from_bases = set()
    for (path, line), result in results.items():
        assert result.bases_truncated is False
        for base in result.bases:
            if base.kind not in ("resolved", "ambiguous"):
                continue
            for d in base.definitions:
                if d.kind != "class":
                    continue
                owner = results[(d.path, d.line)]
                if owner.subclasses_status != "scanned":
                    continue
                assert owner.subclasses_truncated is False
                site = base.site
                assert site is not None
                edge = (
                    _loc(result.target),
                    (site.line, site.column, site.name, base.kind == "ambiguous"),
                )
                assert edge in _subclasses(owner), (edge, d)
                edges_from_bases.add((_loc(d), edge))
    edges_from_subclasses = set()
    for result in results.values():
        for entry in result.subclasses or []:
            child = results[(entry.subclass.path, entry.subclass.line)]
            site = (entry.site.line, entry.site.column, entry.site.name)
            matching = [
                b
                for b in child.bases
                if b.site is not None and (b.site.line, b.site.column, b.site.name) == site
            ]
            assert len(matching) == 1
            assert result.target in matching[0].definitions
            assert (matching[0].kind == "ambiguous") is entry.site.ambiguous
            edges_from_subclasses.add(
                (_loc(result.target), (_loc(entry.subclass), (*site, entry.site.ambiguous)))
            )
    assert edges_from_subclasses == edges_from_bases
    assert edges_from_bases


def test_step_47_parity(tmp_path: Path) -> None:
    """Subclass sites are exactly Step 47's references whose token is a base position, with
    the same names, ambiguity, unsupported count, file counts and truncation."""
    for files, path, line in (
        (REPO, "zoo/base.py", 1),
        (REPO, "pets/dog.py", 7),
        (AMBIGUOUS, "zoo_base.py", 1),
        (SCOPES, "s.py", 1),
    ):
        root, rows = _repo(tmp_path, files)
        target = _row(rows, path)
        result = analyze_type_hierarchy(root, target, rows, line)
        parity(root, rows, target, line, result)
        refs = analyze_references(root, target, rows, line)
        file_counts = ("files_scanned", "too_large", "binary", "syntax_error")
        assert {k: getattr(result.summary, k) for k in file_counts} == {
            k: getattr(refs.summary, k) for k in file_counts
        }


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
TARGET = {"path": "zoo/base.py", "line": 1}


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
        f"/repositories/{repository_id or owner.repository_id}/files/type-hierarchy"
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


def _json_location(key: tuple[Any, ...]) -> dict[str, Any]:
    path, line, kind, name, end_line = key
    return {"path": path, "kind": kind, "name": name, "line": line, "end_line": end_line}


def _json_subclasses(entries: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "subclass": _json_location(location),
            "site": {"line": s[0], "column": s[1], "name": s[2], "ambiguous": s[3]},
        }
        for location, s in entries
    ]


def _json_bases(entries: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "site": None if site is None else {"line": site[0], "column": site[1], "name": site[2]},
            "kind": kind,
            "definitions": [_json_location(d) for d in definitions],
        }
        for site, kind, definitions in entries
    ]


EMPTY_SUMMARY = dict.fromkeys(TypeHierarchySummary.model_fields, 0)
NOT_SCANNED: dict[str, Any] = {
    "target": None,
    "bases": [],
    "subclasses_status": None,
    "subclasses": None,
    "summary": EMPTY_SUMMARY,
    "bases_truncated": False,
    "subclasses_truncated": None,
}


@requires_db
async def test_type_hierarchy_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "scanned",
        "target": ANIMAL,
        "bases": [],
        "subclasses_status": "scanned",
        "subclasses": _json_subclasses(ANIMAL_SUBCLASSES),
        "summary": {
            **EMPTY_SUMMARY,
            "files_scanned": 7,
            "subclasses": 7,
            "subclass_unsupported": 1,
            "non_base_references": 10,
        },
        "bases_truncated": False,
        "subclasses_truncated": False,
    }
    horse = (await _get(client, alice, {"path": "pets/horse.py", "line": 5})).json()
    assert horse["bases"] == _json_bases(HORSE_BASES)
    assert (horse["summary"]["bases"], horse["summary"]["bases_dynamic"]) == (10, 5)


@requires_db
async def test_endpoint_statuses(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    body = (await _get(client, alice, {"path": "docs/notes.md", "line": 1})).json()
    assert body == {"status": "unsupported_language", **NOT_SCANNED}
    assert read_spy == []  # a non-Python file is never read
    for line in (999, 5, 6, 9):  # past the end, a blank line, a decorator line, a body line
        body = (await _get(client, alice, {"path": "pets/dog.py", "line": line})).json()
        assert body == {"status": "not_a_definition", **NOT_SCANNED}
    for path, line in (("zoo/base.py", 7), ("pets/dog.py", 8)):  # a function, a method
        body = (await _get(client, alice, {"path": path, "line": line})).json()
        assert body == {"status": "not_a_class", **NOT_SCANNED}
    body = (await _get(client, alice, {"path": "pets/dog.py", "line": 7})).json()
    assert (body["status"], body["target"]["name"], body["subclasses_status"]) == (
        "scanned",
        "Dog",
        "scanned",
    )
    assert body["bases"] == _json_bases(DOG_BASES)


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
    for path in ("missing.py", "../zoo/base.py", "/zoo/base.py", "ZOO/BASE.PY"):
        response = await _get(client, alice, {**TARGET, "path": path})
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)


@requires_db
async def test_index_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python rows by path: __init__.py, pets/dog.py, pets/horse.py, pets/puppy.py,
    # zoo/__init__.py, zoo/base.py, zoo/cat.py.
    monkeypatch.setattr(code_type_hierarchy, "MAX_INDEXED_PATHS", 6)
    body = (await _get(client, alice, TARGET)).json()  # zoo/cat.py is unindexed
    assert (body["bases_truncated"], body["subclasses_truncated"]) == (True, True)
    assert body["subclasses"] == _json_subclasses(ANIMAL_SUBCLASSES[:-1])
    monkeypatch.setattr(code_type_hierarchy, "MAX_INDEXED_PATHS", 7)  # exactly all seven
    body = (await _get(client, alice, TARGET)).json()
    assert (body["bases_truncated"], body["subclasses_truncated"]) == (False, False)
    assert body["subclasses"] == _json_subclasses(ANIMAL_SUBCLASSES)


@requires_db
async def test_changed_storage_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    (_storage(alice) / "pets" / "dog.py").write_bytes(DOG.encode() + b"# tampered\n")
    response = await _get(client, alice, TARGET)  # a scanned subclass file
    assert (response.status_code, response.json()) == (409, CHANGED)
    assert "tampered" not in response.text

    await _ingest(client, alice, FILES)
    (_storage(alice) / "zoo" / "base.py").write_bytes(BASE.encode() + b"# new\n")
    response = await _get(client, alice, TARGET)  # the target
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    (_storage(alice) / "zoo" / "base.py").unlink()  # missing: a base's module
    response = await _get(client, alice, {"path": "zoo/cat.py", "line": 3})
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    root = _storage(alice)
    outside = tmp_path / "outside"
    shutil.copytree(root / "pets", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pets")
    _junction(root / "pets", outside)
    response = await _get(client, alice, TARGET)
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_symlinked_file_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "zoo" / "base.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(FILES["zoo/base.py"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    response = await _get(client, alice, {"path": "zoo/cat.py", "line": 3})
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_storage_swap_during_the_request_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the target is parsed, a subclass file changes before it is read."""
    real = code_references.collect_scopes
    root = _storage(alice)

    def collect_then_swap(tree: Any) -> Any:
        (root / "pets" / "dog.py").write_bytes(b"from zoo.base import Animal  # new snapshot\n")
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
    files["zoo/base.py"] = b"\n\n" + BASE.encode()
    await _ingest(client, alice, files)
    assert (await _get(client, alice, TARGET)).json()["status"] == "not_a_definition"
    body = (await _get(client, alice, {**TARGET, "line": 3})).json()
    assert (body["target"]["line"], body["summary"]["subclasses"]) == (3, 7)


@requires_db
async def test_no_source_internals_or_logged_content(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-51d7"
    files = dict(FILES)
    files["zoo/base.py"] = BASE.replace(
        "    legs = 4", f"    legs = '{marker}'  # {marker}"
    ).encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "scanned"
    assert marker not in response.text and marker not in caplog.text
    assert "legs" not in response.text and "import" not in response.text
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
        "bases",
        "subclasses_status",
        "subclasses",
        "summary",
        "bases_truncated",
        "subclasses_truncated",
    }
    assert set(body["summary"]) == set(EMPTY_SUMMARY) and len(EMPTY_SUMMARY) == 17
    for entry in body["subclasses"]:
        assert set(entry) == {"subclass", "site"}
        assert set(entry["site"]) == {"line", "column", "name", "ambiguous"}
        assert set(entry["subclass"]) == {"path", "kind", "name", "line", "end_line"}
    horse = (await _get(client, alice, {"path": "pets/horse.py", "line": 5})).json()
    for entry in horse["bases"]:
        assert set(entry) == {"site", "kind", "definitions"}
        assert entry["site"] is None or set(entry["site"]) == {"line", "column", "name"}
    assert "pick" not in str(horse) and "lambda" not in str(horse)


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"b.py": b"class A: pass\nclass B(A): pass\n"})
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
    assert [s["subclass"]["name"] for s in own["subclasses"]] == ["B"]


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
    bob = await _owner(client, "bob@example.com", {"b.py": b"class A: pass\n"})
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
    assert limiter.hits == [(TYPE_HIERARCHY_LIMIT, alice.user_id)]
    assert read_spy == []  # limited before any snapshot read


@requires_db_and_redis
async def test_rate_limit_is_20_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert TYPE_HIERARCHY_LIMIT == Limit("type-hierarchy-user", 20, 600)
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
        assert any(k.startswith("ratelimit:type-hierarchy-user:") for k in keys)
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
