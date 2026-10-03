"""Python find-references tests (GET .../files/references?path=&line=). Offline.

Scanner tests use a temporary storage directory and need no database. Every semantic test
also checks the Step 45 duality against ``analyze_definition``: a returned reference resolves
to the target there, and every token of every file that resolves to the target is returned.
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
from app.services import code_definition, code_references, ingestion
from app.services.code_definition import _normalize, analyze_definition, name_tokens
from app.services.code_importers import MAX_PARSED_BYTES, MAX_SCANNED_BYTES
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.code_references import (
    MAX_ALIAS_NAMES,
    MAX_CANDIDATES,
    MAX_REFERENCES,
    REFERENCES_LIMIT,
    analyze_references,
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

MAIN = "\n".join(
    [
        "from users import User",  # 1
        "from users import Account",  # 2
        "from users.service import User as U",  # 3
        "import users.service",  # 4
        "import users.service as svc",  # 5
        "import requests",  # 6
        "",  # 7
        "User()",  # 8
        "Account()",  # 9
        "U()",  # 10
        "users.service.User()",  # 11
        "svc.User()",  # 12
        "requests.User()",  # 13
        "x: User = U",  # 14
        "'User'  # User",  # 15
        "foo(User=1)",  # 16
        "def f(User):",  # 17
        "    return User()",  # 18
        "def g():",  # 19
        "    User = object",  # 20
        "    return User()",  # 21
        "def h():",  # 22
        "    global User",  # 23
        "    return User",  # 24
        "def k(x=User(), *, y: User) -> User:",  # 25
        "    return [User for _ in x]",  # 26
        "",
    ]
)

REPO: dict[str, str | bytes] = {
    "main.py": MAIN,
    "users/__init__.py": "from .service import User\nfrom .service import User as Account\n",
    "users/service.py": "class User:\n    pass\n\n\ndef make():\n    return User()\n",
    "users/api.py": "from .service import User\nfrom . import service\nUser()\nservice.User()\n",
    "star.py": "from users import *\nUser()\n",
    "ext.py": "from requests import User\nUser()\n",
    "other.py": "class User:\n    pass\nUser()\n",
    "docs/notes.md": "class User: pass\n",
}

USER = {"path": "users/service.py", "kind": "class", "name": "User", "line": 1, "end_line": 2}

# (path, line, column, name, context, ambiguous) for users/service.py::User, server order.
USER_REFERENCES = [
    ("main.py", 1, 19, "User", "import", False),
    ("main.py", 2, 19, "Account", "import", False),  # through the renaming re-export
    ("main.py", 3, 27, "User", "import", False),
    ("main.py", 3, 35, "U", "import", False),  # the alias "as" token (D3)
    ("main.py", 8, 1, "User", "name", False),
    ("main.py", 9, 1, "Account", "name", False),
    ("main.py", 10, 1, "U", "name", False),
    ("main.py", 11, 15, "User", "attribute", False),
    ("main.py", 12, 5, "User", "attribute", False),
    ("main.py", 14, 4, "User", "name", False),  # annotation
    ("main.py", 14, 11, "U", "name", False),
    ("main.py", 25, 9, "User", "name", False),  # default
    ("main.py", 25, 23, "User", "name", False),  # parameter annotation
    ("main.py", 25, 32, "User", "name", False),  # return annotation
    ("main.py", 26, 13, "User", "name", False),  # comprehension
    ("users/__init__.py", 1, 22, "User", "import", False),  # re-export (D4)
    ("users/__init__.py", 2, 22, "User", "import", False),
    ("users/__init__.py", 2, 30, "Account", "import", False),
    ("users/api.py", 1, 22, "User", "import", False),  # relative import
    ("users/api.py", 3, 1, "User", "name", False),
    ("users/service.py", 6, 12, "User", "name", False),
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


def find(tmp_path: Path, files: dict[str, str | bytes], path: str, line: int) -> Any:
    root, rows = _repo(tmp_path, files)
    return analyze_references(root, next(r for r in rows if r.path == path), rows, line)


def _refs(result: Any) -> list[tuple[Any, ...]]:
    return [(r.path, r.line, r.column, r.name, r.context, r.ambiguous) for r in result.references]


def _spots(result: Any) -> list[tuple[str, int, int]]:
    return [(r.path, r.line, r.column) for r in result.references]


def oracle(root: Path, rows: list[Any], target: dict[str, Any]) -> set[tuple[str, int, int]]:
    """Every token of every file whose Step 45 go-to-definition includes ``target``."""
    wanted = (target["path"], target["line"], target["kind"], target["name"])
    found = set()
    for row in rows:
        try:
            text = (root / row.path).read_bytes().decode("utf-8-sig")
            tokens = name_tokens(_normalize(text))
        except Exception:
            continue
        for token in tokens:
            result = analyze_definition(root, row, rows, token.line, token.column + 1)
            if wanted in {(d.path, d.line, d.kind, d.name) for d in result.definitions}:
                found.add((row.path, token.line, token.column + 1))
    return found


def dual(tmp_path: Path, files: dict[str, str | bytes], path: str, line: int) -> Any:
    """``find`` plus the duality check with Step 45 (soundness and completeness)."""
    root, rows = _repo(tmp_path, files)
    result = analyze_references(root, next(r for r in rows if r.path == path), rows, line)
    assert result.status == "scanned", result.status
    assert set(_spots(result)) == oracle(root, rows, result.target.model_dump())
    assert len(_spots(result)) == len(set(_spots(result)))
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


@pytest.fixture
def parse_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The number of statements of every module parsed by the scanner."""
    parsed: list[int] = []
    real = code_references.collect_scopes

    def spy(tree: ast.Module) -> Any:
        parsed.append(len(tree.body))
        return real(tree)

    monkeypatch.setattr(code_references, "collect_scopes", spy)
    return parsed


# --- The reference semantics (cases A-Y) ------------------------------------------------


def test_repository_references(tmp_path: Path) -> None:
    result = dual(tmp_path, REPO, "users/service.py", 1)
    assert result.target.model_dump() == USER
    assert _refs(result) == USER_REFERENCES
    # Excluded and counted: f's parameter use, g's assignment and use, h's global use,
    # the star import's unbound use and api.py's attribute of a from-imported module.
    assert result.summary.model_dump() == {
        "files_scanned": 7,
        "too_large": 0,
        "binary": 0,
        "syntax_error": 0,
        "references": 21,
        "ambiguous": 0,
        "unsupported": 6,
    }
    assert result.truncated is False


def test_function_target_and_other_same_name_definitions(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        **REPO,
        "use.py": "from users.service import make\nmake()\nimport users.service\nusers.service.make\n",
    }
    result = dual(tmp_path, files, "users/service.py", 5)
    assert result.target.model_dump() == {
        "path": "users/service.py",
        "kind": "function",
        "name": "make",
        "line": 5,
        "end_line": 6,
    }
    assert _refs(result) == [
        ("use.py", 1, 27, "make", "import", False),
        ("use.py", 2, 1, "make", "name", False),
        ("use.py", 4, 15, "make", "attribute", False),
    ]
    other = dual(tmp_path, REPO, "other.py", 1)  # the unrelated User of other.py
    assert _refs(other) == [("other.py", 3, 1, "User", "name", False)]


def test_case_a_to_d_uses_and_definition_names(tmp_path: Path) -> None:
    source = "class User:\n    pass\n\nUser()\ndef authenticate():\n    pass\nauthenticate()\n"
    files: dict[str, str | bytes] = {"m.py": source}
    assert _refs(dual(tmp_path, files, "m.py", 1)) == [("m.py", 4, 1, "User", "name", False)]
    assert _refs(dual(tmp_path, files, "m.py", 5)) == [
        ("m.py", 7, 1, "authenticate", "name", False)  # the def name itself is not returned
    ]


def test_case_k_to_o_scopes_shadowing_and_bindings(tmp_path: Path) -> None:
    shadow = "class User:\n    pass\n\ndef f():\n    User = Other\n    User()\n"
    result = dual(tmp_path, {"m.py": shadow}, "m.py", 1)
    assert (_refs(result), result.summary.unsupported) == ([], 2)  # K
    local = "def outer():\n    def inner():\n        pass\n\n    inner()\n"
    assert _refs(dual(tmp_path, {"m.py": local}, "m.py", 2)) == [
        ("m.py", 5, 5, "inner", "name", False)  # L
    ]
    nested = "class User:\n    pass\n\ndef outer():\n    class User:\n        pass\n\n    User()\n"
    assert _refs(dual(tmp_path, {"m.py": nested}, "m.py", 1)) == []  # M: the inner one
    assert _refs(dual(tmp_path, {"m.py": nested}, "m.py", 5)) == [
        ("m.py", 8, 5, "User", "name", False)
    ]
    parameter = "class User:\n    pass\n\ndef f(User):\n    User()\n"
    result = dual(tmp_path, {"m.py": parameter}, "m.py", 1)
    assert (_refs(result), result.summary.unsupported) == ([], 1)  # N
    assigned = "class User:\n    pass\n\nUser = something\nUser()\n"
    result = dual(tmp_path, {"m.py": assigned}, "m.py", 1)
    assert (_refs(result), result.summary.unsupported) == ([], 2)  # O: both names


@pytest.mark.parametrize(
    "binding",
    [
        "    for User in items:\n        pass",
        "    with open() as User:\n        pass",
        "    try:\n        pass\n    except E as User:\n        pass",
        "    match x:\n        case User:\n            pass",
        "    (User := 1)",
        "    del User",
        "    global User",
        "    User = 1\n    def inner():\n        nonlocal User\n        return User",
    ],
)
def test_variable_like_bindings_shadow(tmp_path: Path, binding: str) -> None:
    source = f"class User:\n    pass\n\ndef f():\n{binding}\n    return User\n"
    result = dual(tmp_path, {"m.py": source}, "m.py", 1)
    assert _refs(result) == []
    assert result.summary.unsupported >= 1


def test_case_p_to_w_contexts(tmp_path: Path) -> None:
    source = (
        "class User:\n"  # 1
        "    pass\n"  # 2
        "x: User\n"  # 3
        "def f(x: User = User()) -> User:\n"  # 4
        "    ...\n"  # 5
        "foo(User=value)\n"  # 6
        "'User'  # User\n"  # 7
        "User.save\n"  # 8
        "obj.User\n"  # 9
        "lambda: User\n"  # 10
        "{k: User for k in ()}\n"  # 11
    )
    assert _refs(dual(tmp_path, {"m.py": source}, "m.py", 1)) == [
        ("m.py", 3, 4, "User", "name", False),
        ("m.py", 4, 10, "User", "name", False),
        ("m.py", 4, 17, "User", "name", False),
        ("m.py", 4, 28, "User", "name", False),
        ("m.py", 8, 1, "User", "name", False),
        ("m.py", 10, 9, "User", "name", False),
        ("m.py", 11, 5, "User", "name", False),
    ]


def test_case_s_attributes_need_no_type_inference(tmp_path: Path) -> None:
    source = (
        "class User:\n"
        "    def save(self):\n"
        "        self.save()\n"
        "def save():\n"
        "    pass\n"
        "obj.save()\n"
        "User.save()\n"
        "User().save()\n"
    )
    result = dual(tmp_path, {"m.py": source}, "m.py", 4)  # the module-level save
    assert _refs(result) == []
    assert result.summary.unsupported == 3  # self.save, User.save, User().save (obj: unbound)


def test_case_w_builtins_x_external_and_y_star_imports(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def print():\n    pass\n",
        "o.py": "print()\nfrom requests import print as p\np()\n",
        "s.py": "from m import *\nprint()\n",
    }
    result = dual(tmp_path, files, "m.py", 1)
    assert _refs(result) == []
    assert result.summary.unsupported == 2  # o.py's builtin print, s.py's star import


def test_conditional_definitions_are_ambiguous(tmp_path: Path) -> None:
    source = "if X:\n    def f():\n        pass\nelse:\n    def f():\n        pass\nf()\n"
    for line in (2, 5):
        result = dual(tmp_path, {"m.py": source}, "m.py", line)
        assert _refs(result) == [("m.py", 7, 1, "f", "name", True)]
        assert (result.summary.references, result.summary.ambiguous) == (1, 1)


def test_try_except_import_fallbacks(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "a.py": "def thing():\n    pass\n",
        "b.py": "def thing():\n    pass\n",
        "m.py": "try:\n    from a import thing\nexcept ImportError:\n    from b import thing\nthing()\n",
    }
    result = dual(tmp_path, files, "a.py", 1)
    assert _refs(result) == [
        ("m.py", 2, 19, "thing", "import", False),  # indented: 4 + 15
        ("m.py", 5, 1, "thing", "name", True),
    ]


def test_relative_imports_follow_step_39(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "users/__init__.py": "",
        "users/service.py": "class User:\n    pass\n",
        "admin/service.py": "class User:\n    pass\n",
        "users/api.py": "from .service import User\nUser()\nfrom . import service\n",
        "top.py": "from service import User\nUser()\n",  # absolute: both service modules
    }
    assert _refs(dual(tmp_path, files, "users/service.py", 1)) == [
        ("top.py", 1, 21, "User", "import", True),
        ("top.py", 2, 1, "User", "name", True),
        ("users/api.py", 1, 22, "User", "import", False),  # relative: users/service.py only
        ("users/api.py", 2, 1, "User", "name", False),
    ]


def test_class_scopes_are_skipped(tmp_path: Path) -> None:
    source = (
        "def User():\n    pass\n"
        "class C:\n"
        "    def User(self):\n        pass\n"
        "    alias = User\n"
        "    def m(self):\n        return User()\n"
    )
    result = dual(tmp_path, {"m.py": source}, "m.py", 1)
    assert _refs(result) == [("m.py", 8, 16, "User", "name", False)]


def test_pyi_ambiguous_modules_and_whole_segments(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "stubs/typed.pyi": "def stub() -> int: ...\n",
        "lib/utils.py": "def helper():\n    pass\n",
        "tools/utils.py": "def helper():\n    pass\n",
        "xutils.py": "def helper():\n    pass\n",
        "m.py": "from stubs.typed import stub\nstub()\nimport utils\nutils.helper()\n",
    }
    assert _refs(dual(tmp_path, files, "stubs/typed.pyi", 1)) == [
        ("m.py", 1, 25, "stub", "import", False),
        ("m.py", 2, 1, "stub", "name", False),
    ]
    assert _refs(dual(tmp_path, files, "lib/utils.py", 1)) == [
        ("m.py", 4, 7, "helper", "attribute", True)
    ]
    assert _refs(dual(tmp_path, files, "xutils.py", 1)) == []


CHAIN: dict[str, str | bytes] = {
    "h/one.py": "class X:\n    pass\n",
    "h/two.py": "from h.one import X as X2\n",
    "h/three.py": "from h.two import X2 as X3\n",
    "main.py": "from h.three import X3 as X4\nX4()\n",
}


def test_renaming_import_chains_need_the_name_closure(tmp_path: Path) -> None:
    result = dual(tmp_path, CHAIN, "h/one.py", 1)
    assert _refs(result) == [
        ("h/three.py", 1, 19, "X2", "import", False),
        ("h/three.py", 1, 25, "X3", "import", False),
        ("h/two.py", 1, 19, "X", "import", False),
        ("h/two.py", 1, 24, "X2", "import", False),
        ("main.py", 1, 21, "X3", "import", False),
        ("main.py", 1, 27, "X4", "import", False),
        ("main.py", 2, 1, "X4", "name", False),  # three renames away from X
    ]


def test_import_hops_follow_step_45(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_definition, "MAX_IMPORT_HOPS", 2)
    result = dual(tmp_path, CHAIN, "h/one.py", 1)
    assert ("main.py", 2, 1) not in _spots(result)
    assert not any(path == "main.py" for path, _, _ in _spots(result))
    monkeypatch.setattr(code_definition, "MAX_IMPORT_HOPS", 4)
    assert ("main.py", 2, 1) in _spots(dual(tmp_path, CHAIN, "h/one.py", 1))


def test_unicode_columns_nfkc_tabs_and_crlf(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class User:\n    pass\n",
        "u.py": "from m import \uff35ser\nx = '\U0001d4b3\u00e9'; \uff35ser()\n",
        "t.py": b"from m import User\r\nif 1:\r\n\tUser()\r\n",
    }
    result = dual(tmp_path, files, "m.py", 1)
    assert _refs(result) == [
        ("t.py", 1, 15, "User", "import", False),
        ("t.py", 3, 2, "User", "name", False),
        ("u.py", 1, 15, "User", "import", False),
        ("u.py", 2, 11, "User", "name", False),
    ]


def test_order_is_path_line_column_whatever_the_discovery_order(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class User:\n    pass\n",
        "n.py": "from m import User as Renamed\n",
        "A.py": "from n import Renamed\nRenamed(); Renamed()\n",  # found in the second round
        "z.py": "from m import User\nUser()\n",
        "\u00e9.py": "from m import User\nUser()\n",
    }
    result = dual(tmp_path, files, "m.py", 1)
    paths = [path for path, _, _ in _spots(result)]
    assert paths == sorted(paths) and paths[0] == "A.py" and paths[-1] == "\u00e9.py"
    assert _spots(result)[:3] == [("A.py", 1, 15), ("A.py", 2, 1), ("A.py", 2, 12)]


# --- Targets ---------------------------------------------------------------------------


def test_targets_and_statuses(tmp_path: Path) -> None:
    source = (
        "@decorator\n"  # 1
        "def f():\n"  # 2
        "    pass\n"  # 3
        "async def g():\n"  # 4
        "    pass\n"  # 5
        "if X:\n"  # 6
        "    class C:\n"  # 7
        "        def method(self):\n"  # 8
        "            pass\n"  # 9
        "        class Inner:\n"  # 10
        "            pass\n"  # 11
        "def outer():\n"  # 12
        "    class Local:\n"  # 13
        "        pass\n"  # 14
        "    return Local()\n"  # 15
        "f(); g(); C()\n"  # 16
    )
    files: dict[str, str | bytes] = {"m.py": source}
    for line in (1, 3, 6, 16, 17, 999):
        result = find(tmp_path, files, "m.py", line)
        assert (result.status, result.target, result.references) == ("not_a_definition", None, [])
    expected = {
        2: ("function", "f"),
        4: ("function", "g"),
        7: ("class", "C"),
        12: ("function", "outer"),
    }
    for line, (kind, name) in expected.items():
        result = dual(tmp_path, files, "m.py", line)
        assert (result.target.kind, result.target.name, result.target.line) == (kind, name, line)
    assert _refs(dual(tmp_path, files, "m.py", 13)) == [("m.py", 15, 12, "Local", "name", False)]
    for line, kind in ((8, "method"), (10, "class")):
        result = find(tmp_path, files, "m.py", line)
        assert result.status == "unsupported_target"
        assert result.target.kind == kind and result.references == []
        assert set(result.summary.model_dump().values()) == {0}


def test_target_file_statuses(tmp_path: Path, read_spy: list[str]) -> None:
    assert find(tmp_path, {"m.py": "def (:\n"}, "m.py", 1).status == "syntax_error"
    assert find(tmp_path, {"m.py": b"\x00\x01"}, "m.py", 1).status == "binary"
    assert find(tmp_path, {"m.py": b"\xff\xfe"}, "m.py", 1).status == "binary"
    assert find(tmp_path, {"m.py": ""}, "m.py", 1).status == "not_a_definition"
    body = b"def target():\n    pass\ntarget()\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    assert _refs(find(tmp_path, {"m.py": exactly}, "m.py", 1)) == [
        ("m.py", 3, 1, "target", "name", False)
    ]
    over = find(tmp_path, {"m.py": exactly + b"#"}, "m.py", 1)
    assert (over.status, over.target) == ("too_large", None)
    read_spy.clear()
    limit = os.urandom(MAX_CONTENT_BYTES)
    assert find(tmp_path, {"m.py": limit}, "m.py", 1).status in ("binary", "too_large")
    assert read_spy == ["m.py"]  # exactly 1 MiB is opened
    read_spy.clear()
    assert find(tmp_path, {"m.py": limit + b"x"}, "m.py", 1).status == "too_large"
    assert read_spy == []  # over 1 MiB is never opened


def test_local_targets_scan_only_their_file(tmp_path: Path, read_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "def outer():\n    def inner():\n        pass\n    inner()\n",
        "n.py": "from m import inner\ninner()\n",
    }
    result = dual(tmp_path, files, "m.py", 2)
    assert _refs(result) == [("m.py", 4, 5, "inner", "name", False)]
    assert result.summary.files_scanned == 1
    read_spy.clear()
    find(tmp_path, files, "m.py", 2)
    assert read_spy == ["m.py"]


# --- Candidates, counts and reads ----------------------------------------------------------


def test_only_text_naming_the_closure_is_parsed(tmp_path: Path, parse_spy: list[int]) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class User:\n    pass\n",
        "a.py": "x = 1\n" * 3,  # ASCII without the name: read, never parsed
        "b.py": "y = '\u00e9'\n" * 2,  # non-ASCII: always parsed (NFKC)
        "c.py": "from m import User\n",
    }
    result = dual(tmp_path, files, "m.py", 1)
    parse_spy.clear()
    find(tmp_path, files, "m.py", 1)
    assert sorted(parse_spy) == [1, 1, 2]  # m.py, c.py and b.py; never a.py's 3 statements
    assert result.summary.files_scanned == 4


def test_only_snapshot_files_are_read(tmp_path: Path, read_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class User:\n    pass\n",
        "n.py": "from stray import User\nfrom m import User as U\nU()\n",
    }
    root, rows = _repo(tmp_path, files)
    (root / "stray.py").write_bytes(b"from m import User\nUser()\n")  # not in the snapshot
    result = analyze_references(root, rows[0], rows, 1)
    assert [path for path, _, _ in _spots(result)] == ["n.py", "n.py", "n.py"]
    assert "stray.py" not in read_spy and sorted(read_spy) == ["m.py", "n.py"]


def test_files_that_cannot_be_analyzed_are_counted(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class User:\n    pass\n",
        "bad.py": "from m import User\ndef (:\n",
        "bin.py": b"User\x00",
        "big.py": b"User = 1\n" + b"#" * MAX_OUTLINE_BYTES,
        "huge.py": b"#" * (MAX_CONTENT_BYTES + 1),
    }
    result = find(tmp_path, files, "m.py", 1)
    assert result.summary.model_dump() == {
        "files_scanned": 4,  # huge.py is never opened
        "too_large": 2,
        "binary": 1,
        "syntax_error": 1,
        "references": 0,
        "ambiguous": 0,
        "unsupported": 0,
    }
    assert result.truncated is False


# --- Limits ----------------------------------------------------------------------------------


def test_read_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_spy: list[str]) -> None:
    assert MAX_SCANNED_BYTES == 64 * 1024 * 1024
    files: dict[str, str | bytes] = {
        "m.py": "class User:\n    pass\n",
        "n.py": "from m import User\n",
        "o.py": "from m import User\nUser()\n",
    }
    total = sum(len(str(text).encode()) for text in files.values())
    monkeypatch.setattr(code_references, "MAX_SCANNED_BYTES", total)
    result = find(tmp_path, files, "m.py", 1)
    assert (len(result.references), result.truncated) == (3, False)
    monkeypatch.setattr(code_references, "MAX_SCANNED_BYTES", total - 1)
    read_spy.clear()
    result = find(tmp_path, files, "m.py", 1)
    assert (_spots(result), result.truncated) == ([("n.py", 1, 15)], True)
    assert read_spy == ["m.py", "n.py"]  # o.py is never opened


def test_parse_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_PARSED_BYTES == 4 * 1024 * 1024
    files: dict[str, str | bytes] = {
        "m.py": "class User:\n    pass\n",
        "n.py": "from m import User\n",
        "skipped.py": "x = 1\n" * 50,  # never parsed, so not counted
        "z.py": "from m import User\nUser()\n",
    }
    parsed = sum(len(str(files[p]).encode()) for p in ("m.py", "n.py", "z.py"))
    monkeypatch.setattr(code_references, "MAX_PARSED_BYTES", parsed)
    result = find(tmp_path, files, "m.py", 1)
    assert (len(result.references), result.truncated) == (3, False)
    monkeypatch.setattr(code_references, "MAX_PARSED_BYTES", parsed - 1)
    result = find(tmp_path, files, "m.py", 1)
    assert (_spots(result), result.truncated) == ([("n.py", 1, 15)], True)


def test_reference_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_REFERENCES == 500
    source = "def f():\n    pass\nf()\nf()\nf()\n"
    monkeypatch.setattr(code_references, "MAX_REFERENCES", 3)
    result = find(tmp_path, {"m.py": source}, "m.py", 1)
    assert (len(result.references), result.truncated) == (3, False)
    monkeypatch.setattr(code_references, "MAX_REFERENCES", 2)
    result = find(tmp_path, {"m.py": source}, "m.py", 1)
    assert (_spots(result), result.truncated) == ([("m.py", 3, 1), ("m.py", 4, 1)], True)
    assert result.summary.references == 3  # counted before the cap


def test_candidate_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_CANDIDATES == 10_000
    source = "def f():\n    pass\nf()\nf()\n"  # three candidates, the def name included
    monkeypatch.setattr(code_references, "MAX_CANDIDATES", 3)
    result = find(tmp_path, {"m.py": source}, "m.py", 1)
    assert (len(result.references), result.truncated) == (2, False)
    monkeypatch.setattr(code_references, "MAX_CANDIDATES", 2)
    result = find(tmp_path, {"m.py": source}, "m.py", 1)
    assert (_spots(result), result.truncated) == ([("m.py", 3, 1)], True)


def test_alias_name_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_ALIAS_NAMES == 50
    monkeypatch.setattr(code_references, "MAX_ALIAS_NAMES", 3)  # User, Account and U
    result = find(tmp_path, REPO, "users/service.py", 1)
    assert (_refs(result), result.truncated) == (USER_REFERENCES, False)
    monkeypatch.setattr(code_references, "MAX_ALIAS_NAMES", 2)  # U (sorted last) is dropped
    result = find(tmp_path, REPO, "users/service.py", 1)
    assert result.truncated is True
    assert _refs(result) == [r for r in USER_REFERENCES if r[3] != "U"]


def test_module_budget_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 3)  # X4 reads three, two, one
    result = dual(tmp_path, CHAIN, "h/one.py", 1)
    assert ("main.py", 2, 1) in _spots(result) and result.truncated is False
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 2)
    result = dual(tmp_path, CHAIN, "h/one.py", 1)
    assert not any(path == "main.py" for path, _, _ in _spots(result))
    assert result.truncated is True and result.summary.unsupported == 3
    root, rows = _repo(tmp_path, CHAIN)
    main_row = next(r for r in rows if r.path == "main.py")
    step_45 = analyze_definition(root, main_row, rows, 2, 1)
    assert (step_45.status, step_45.truncated) == ("unsupported", True)


def test_definition_list_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = "if X:\n    def f():\n        pass\nelse:\n    def f():\n        pass\nf()\n"
    monkeypatch.setattr(code_definition, "MAX_DEFINITIONS", 1)
    first = find(tmp_path, {"m.py": source}, "m.py", 2)
    assert (_refs(first), first.truncated) == ([("m.py", 7, 1, "f", "name", True)], False)
    second = find(tmp_path, {"m.py": source}, "m.py", 5)  # cut from Step 45's list
    assert (_refs(second), second.truncated) == ([], True)


# --- Safety ------------------------------------------------------------------------------


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "import os, subprocess, socket\n"
        "os.system('echo')\nsubprocess.run(['echo'])\n"
        "socket.create_connection(('example.invalid', 80))\n"
        "__import__('os').system('echo')\neval('1')\nexec('x = 1')\n"
        "def deco(f):\n    open('side-effect', 'w')\n    return f\n"
        "@deco\ndef target():\n    pass\n"
    )
    files: dict[str, str | bytes] = {"m.py": "from evil import target\ntarget()\n", "evil.py": evil}
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
    result = analyze_references(root, rows[0], rows, 13)
    monkeypatch.undo()
    assert _refs(result) == [
        ("m.py", 1, 18, "target", "import", False),
        ("m.py", 2, 1, "target", "name", False),
    ]
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


# --- Snapshot ----------------------------------------------------------------------------


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "class User:\n    pass\n",
        "n.py": "from m import User\nUser()\n",
    }
    root, rows = _repo(tmp_path, files)
    target = rows[0]
    (root / "n.py").write_bytes(b"from m import User\nUser()  # changed\n")  # a scanned file
    with pytest.raises(FileContentError) as raised:
        analyze_references(root, target, rows, 1)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "n.py").unlink()  # missing
    with pytest.raises(FileContentError):
        analyze_references(root, target, rows, 1)
    root, rows = _repo(tmp_path, files)
    (root / "m.py").write_bytes(b"class User:\n    pass  # changed\n")  # the target
    with pytest.raises(FileContentError) as raised:
        analyze_references(root, rows[0], rows, 1)
    assert raised.value.status_code == 409


def test_a_lazily_read_module_is_verified(tmp_path: Path, read_spy: list[str]) -> None:
    source = (
        "def outer():\n    def inner():\n        pass\n    inner()\n"
        "def g():\n    from lib import inner\n    return inner()\n"
    )
    files: dict[str, str | bytes] = {"m.py": source, "lib.py": "def inner():\n    pass\n"}
    result = dual(tmp_path, files, "m.py", 2)
    assert _refs(result) == [("m.py", 4, 5, "inner", "name", False)]
    root, rows = _repo(tmp_path, files)
    target = next(r for r in rows if r.path == "m.py")
    read_spy.clear()
    analyze_references(root, target, rows, 2)
    assert read_spy == ["m.py", "lib.py"]  # lib.py only to resolve g's import
    (root / "lib.py").write_bytes(b"def inner():\n    pass  # changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_references(root, target, rows, 2)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


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
TARGET = {"path": "users/service.py", "line": 1}


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
        f"/repositories/{repository_id or owner.repository_id}/files/references"
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


def _as_json(reference: tuple[Any, ...]) -> dict[str, Any]:
    keys = ("path", "line", "column", "name", "context", "ambiguous")
    return dict(zip(keys, reference, strict=True))


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_references_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "scanned",
        "target": USER,
        "references": [_as_json(r) for r in USER_REFERENCES],
        "summary": {
            "files_scanned": 7,
            "too_large": 0,
            "binary": 0,
            "syntax_error": 0,
            "references": 21,
            "ambiguous": 0,
            "unsupported": 6,
        },
        "truncated": False,
    }


@requires_db
async def test_endpoint_statuses(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    empty = {
        "files_scanned": 0,
        "too_large": 0,
        "binary": 0,
        "syntax_error": 0,
        "references": 0,
        "ambiguous": 0,
        "unsupported": 0,
    }
    body = (await _get(client, alice, {"path": "docs/notes.md", "line": 1})).json()
    assert body == {
        "status": "unsupported_language",
        "target": None,
        "references": [],
        "summary": empty,
        "truncated": False,
    }
    assert read_spy == []  # a non-Python file is never read
    body = (await _get(client, alice, {"path": "main.py", "line": 999})).json()
    assert (body["status"], body["target"], body["summary"]) == ("not_a_definition", None, empty)
    body = (await _get(client, alice, {"path": "main.py", "line": 17})).json()
    assert (body["status"], body["target"]["name"]) == ("scanned", "f")


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
    for path in ("missing.py", "../users/service.py", "/users/service.py", "USERS/SERVICE.PY"):
        response = await _get(client, alice, {**TARGET, "path": path})
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)


@requires_db
async def test_index_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python rows by path: ext, main, other, star, users/__init__, users/api, users/service.
    monkeypatch.setattr(code_references, "MAX_INDEXED_PATHS", 6)
    body = (await _get(client, alice, TARGET)).json()  # the target itself is unindexed
    assert body["truncated"] is True
    assert [(r["path"], r["line"]) for r in body["references"]] == [("users/service.py", 6)]
    monkeypatch.setattr(code_references, "MAX_INDEXED_PATHS", 7)  # exactly all seven
    body = (await _get(client, alice, TARGET)).json()
    assert (len(body["references"]), body["truncated"]) == (21, False)


@requires_db
async def test_changed_storage_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    (_storage(alice) / "main.py").write_bytes(MAIN.encode() + b"# tampered\n")  # scanned
    response = await _get(client, alice, TARGET)
    assert (response.status_code, response.json()) == (409, CHANGED)
    assert "tampered" not in response.text

    await _ingest(client, alice, FILES)
    (_storage(alice) / "users" / "service.py").write_bytes(b"class User:\n    pass  # new\n")
    response = await _get(client, alice, TARGET)  # the target
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    root = _storage(alice)
    outside = tmp_path / "outside"
    shutil.copytree(root / "users", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "users")
    _junction(root / "users", outside)
    response = await _get(client, alice, TARGET)
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_symlinked_file_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "main.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(FILES["main.py"])
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
    """After the target is parsed, a file changes before it is read."""
    real = code_references.collect_scopes
    root = _storage(alice)

    def collect_then_swap(tree: Any) -> Any:
        (root / "users" / "api.py").write_bytes(b"from .service import User  # new snapshot\n")
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
    files["users/service.py"] = b"\n\n\nclass User:\n    pass\n"
    await _ingest(client, alice, files)
    assert (await _get(client, alice, TARGET)).json()["status"] == "not_a_definition"
    body = (await _get(client, alice, {**TARGET, "line": 4})).json()
    assert (body["target"]["line"], len(body["references"])) == (4, 20)  # no make() any more


@requires_db
async def test_no_source_internals_or_logged_content(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-3c9a"
    files = dict(FILES)
    files["users/service.py"] = f"class User:  # {marker}\n    value = '{marker}'\n".encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "scanned"
    assert marker not in response.text and marker not in caplog.text
    assert "pass" not in response.text and "from users" not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text


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
    own = await _get(client, bob, params)
    assert [r["line"] for r in own.json()["references"]] == [3]


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
    assert limiter.hits == [(REFERENCES_LIMIT, alice.user_id)]
    assert read_spy == []


@requires_db_and_redis
async def test_rate_limit_is_20_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert REFERENCES_LIMIT == Limit("references-user", 20, 600)
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
        assert any(k.startswith("ratelimit:references-user:") for k in keys)
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
