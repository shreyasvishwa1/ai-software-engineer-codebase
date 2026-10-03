"""Python go-to-definition tests (GET .../files/definition?path=&line=&column=). Offline.

Resolver tests use a temporary storage directory and need no database. API tests run
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
from app.services import code_definition, ingestion
from app.services.code_definition import (
    DEFINITION_LIMIT,
    MAX_DEFINITIONS,
    MAX_IMPORT_HOPS,
    MAX_MODULE_FILES,
    analyze_definition,
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

MAIN = "\n".join(
    [
        "from pkg.users import UserService",  # 1
        "from pkg.users import UserService as US",  # 2
        "import pkg.users",  # 3
        "import pkg.users as users",  # 4
        "from pkg import UserService as Exported",  # 5
        "from stubs.typed import stub",  # 6
        "import utils",  # 7
        "import os",  # 8
        "import requests",  # 9
        "from pkg import sub",  # 10
        "from h.one import X as X1",  # 11
        "from h.two import X as X2",  # 12
        "from h.three import X as X3",  # 13
        "from h.four import X as X4",  # 14
        "import app.main",  # 15
        "import xapp.main",  # 16
        "",  # 17
        "UserService()",  # 18
        "US()",  # 19
        "pkg.users.UserService()",  # 20
        "users.UserService()",  # 21
        "Exported()",  # 22
        "stub()",  # 23
        "utils.helper()",  # 24
        "os.path.join()",  # 25
        "requests.get()",  # 26
        "pkg.users.missing()",  # 27
        "sub",  # 28
        "X1, X2, X3, X4",  # 29
        "app.main.run()",  # 30
        "xapp.main.run()",  # 31
        "",
    ]
)

REPO: dict[str, str | bytes] = {
    "main.py": MAIN,
    "pkg/__init__.py": "from .users import UserService\n",
    "pkg/users.py": "class UserService:\n    pass\n\n\ndef make():\n    pass\n",
    "pkg/sub/__init__.py": "",
    "pkg/sub/helpers.py": "",
    "pkg/sub/tools.py": "from ..users import make\nfrom . import helpers\nmake()\nhelpers\n",
    "other/helpers.py": "def x():\n    pass\n",
    "stubs/typed.pyi": "def stub() -> int: ...\n",
    "lib/utils.py": "def helper():\n    pass\n",
    "tools/utils.py": "def helper():\n    pass\n",
    "web/xapp/main.py": "def run():\n    pass\n",
    "h/one.py": "class X:\n    pass\n",
    "h/two.py": "from h.one import X\n",
    "h/three.py": "from h.two import X\n",
    "h/four.py": "from h.three import X\n",
    "docs/notes.md": "def UserService(): pass\n",
}

USER_SERVICE = ("pkg/users.py", "class", "UserService", 1, 2)


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


def define(tmp_path: Path, files: dict[str, str | bytes], path: str, line: int, column: int) -> Any:
    root = tmp_path / "repo"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    rows = _write(root, files)
    return analyze_definition(root, next(r for r in rows if r.path == path), rows, line, column)


def at(source: str, needle: str, nth: int = 0, offset: int = 0) -> tuple[int, int]:
    """1-based (line, column) of the ``nth`` occurrence of ``needle``, plus ``offset``."""
    index = -1
    for _ in range(nth + 1):
        index = source.index(needle, index + 1)
    line = source.count("\n", 0, index) + 1
    column = index - (source.rfind("\n", 0, index) + 1) + 1
    return line, column + offset


def _defs(result: Any) -> list[tuple[Any, ...]]:
    return [(d.path, d.kind, d.name, d.line, d.end_line) for d in result.definitions]


def one(tmp_path: Path, source: str, needle: str, nth: int = 0, offset: int = 0) -> Any:
    line, column = at(source, needle, nth, offset)
    return define(tmp_path, {"m.py": source}, "m.py", line, column)


def main(tmp_path: Path, needle: str, nth: int = 0, offset: int = 0) -> Any:
    line, column = at(MAIN, needle, nth, offset)
    return define(tmp_path, REPO, "main.py", line, column)


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_definition._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_definition, "_read_verified", spy)
    return reads


# --- Positions ---------------------------------------------------------------------------

POSITIONS = (
    "def target(value):\n"  # 1
    "    return value\n"  # 2
    "\n"  # 3
    "result = target(1) + other  # target\n"  # 4
    "label = 'target'\n"  # 5
)


def test_every_character_of_the_identifier(tmp_path: Path) -> None:
    for offset in (0, 3, 5):  # first, middle, last character of "target"
        result = one(tmp_path, POSITIONS, "target(1)", offset=offset)
        assert (result.status, _defs(result)) == (
            "resolved",
            [("m.py", "function", "target", 1, 2)],
        )
        assert result.reference.model_dump() == {
            "path": "m.py",
            "line": 4,
            "column": 10,
            "name": "target",
        }


@pytest.mark.parametrize(
    ("needle", "nth", "offset"),
    [
        ("target(1)", 0, 6),  # immediately after the identifier
        ("result =", 0, 6),  # whitespace
        (" + ", 0, 1),  # punctuation
        ("# target", 0, 3),  # comment
        ("'target'", 0, 2),  # string
        ("def", 0, 0),  # keyword
        ("return", 0, 2),  # keyword
    ],
    ids=["after", "whitespace", "punctuation", "comment", "string", "def", "return"],
)
def test_positions_that_are_not_identifiers(
    tmp_path: Path, needle: str, nth: int, offset: int
) -> None:
    result = one(tmp_path, POSITIONS, needle, nth, offset)
    assert (result.status, result.reference, result.definitions) == ("no_identifier", None, [])


def test_positions_beyond_the_line_or_file(tmp_path: Path) -> None:
    for line, column in ((4, 999), (99, 1), (6, 1)):
        result = define(tmp_path, {"m.py": POSITIONS}, "m.py", line, column)
        assert (result.status, result.reference) == ("no_identifier", None)


def test_non_reference_identifiers(tmp_path: Path) -> None:
    # The def name, a parameter, a parameter reference and an unbound name.
    assert one(tmp_path, POSITIONS, "target").status == "unsupported"
    assert one(tmp_path, POSITIONS, "value").status == "unsupported"
    assert one(tmp_path, POSITIONS, "value", nth=1).status == "unsupported"
    result = one(tmp_path, POSITIONS, "other")
    assert (result.status, result.reference.name) == ("not_found", "other")


def test_unicode_tabs_and_crlf(tmp_path: Path) -> None:
    unicode = "def target():\n    pass\n\u540d\u524d = '\u00e9\u00e9'; x = target()\n"
    result = one(tmp_path, unicode, "target()\n")  # code-point column, multi-byte text before
    assert (result.status, result.reference.column) == ("resolved", 16)
    assert at(unicode, "target()\n") == (3, 16)
    tabs = "def target():\n    pass\nif True:\n\tx = target()\n"
    result = one(tmp_path, tabs, "target()\n")
    assert (result.status, result.reference.line, result.reference.column) == ("resolved", 4, 6)
    crlf = "def target():\r\n    pass\r\n\r\nx = target()\r\n"
    line, column = at(crlf.replace("\r\n", "\n"), "target()\n")
    assert (line, column) == (4, 5)
    result = define(tmp_path, {"m.py": crlf.encode()}, "m.py", line, column)
    assert (result.status, _defs(result)) == ("resolved", [("m.py", "function", "target", 1, 2)])


def test_nfkc_identifiers(tmp_path: Path) -> None:
    source = "def auth():\n    pass\n\uff58 = \uff41\uff55\uff54\uff48()\n"
    result = one(tmp_path, source, "\uff41\uff55\uff54\uff48")
    assert (result.status, result.reference.name) == ("resolved", "auth")
    files: dict[str, str | bytes] = {
        "m.py": "import mod\nmod.\uff46unc()\n",
        "mod.py": "def func():\n    pass\n",
    }
    line, column = at(files["m.py"], "\uff46unc")  # type: ignore[arg-type]
    result = define(tmp_path, files, "m.py", line, column)
    assert (result.status, result.reference.name, _defs(result)) == (
        "resolved",
        "func",
        [("mod.py", "function", "func", 1, 2)],
    )


def test_soft_keywords(tmp_path: Path) -> None:
    source = "match = 1\nmatch command:\n    case 1:\n        pass\n"
    assert one(tmp_path, source, "match", nth=1).status == "no_identifier"
    assert one(tmp_path, source, "case").status == "no_identifier"
    assert one(tmp_path, source, "match").status == "unsupported"  # a variable


# --- Definitions and scopes -------------------------------------------------------------


def test_module_class_and_function(tmp_path: Path) -> None:
    source = "class User:\n    pass\n\n\ndef build():\n    return User()\n\n\nbuild()\n"
    assert _defs(one(tmp_path, source, "User()")) == [("m.py", "class", "User", 1, 2)]
    assert _defs(one(tmp_path, source, "build()\n")) == [("m.py", "function", "build", 5, 6)]


def test_local_and_nested_functions_and_classes(tmp_path: Path) -> None:
    source = (
        "def outer():\n"  # 1
        "    def inner():\n"  # 2
        "        def deepest():\n"  # 3
        "            pass\n"  # 4
        "        return deepest()\n"  # 5
        "    class Local:\n"  # 6
        "        class Nested:\n"  # 7
        "            pass\n"  # 8
        "        ref = Nested\n"  # 9
        "    return inner(), Local\n"  # 10
    )
    assert _defs(one(tmp_path, source, "return deepest()", offset=7)) == [
        ("m.py", "function", "deepest", 3, 4)
    ]
    assert _defs(one(tmp_path, source, "return inner()", offset=7)) == [
        ("m.py", "function", "inner", 2, 5)
    ]
    assert _defs(one(tmp_path, source, "Nested", nth=1)) == [("m.py", "class", "Nested", 7, 8)]
    assert _defs(one(tmp_path, source, "Local", nth=1)) == [("m.py", "class", "Local", 6, 9)]


def test_method_kind_follows_step_37(tmp_path: Path) -> None:
    source = "class A:\n    def save(self):\n        pass\n    alias = save\n"
    assert _defs(one(tmp_path, source, "save", nth=1)) == [("m.py", "method", "save", 2, 3)]


def test_conditional_definitions_are_bindings(tmp_path: Path) -> None:
    source = (
        "if FLAG:\n    def f():\n        pass\n"
        "try:\n    class C:\n        pass\nexcept E:\n    pass\n"
        "f()\nC\n"
    )
    assert _defs(one(tmp_path, source, "f()\nC")) == [("m.py", "function", "f", 2, 3)]
    assert _defs(one(tmp_path, source, "C\n")) == [("m.py", "class", "C", 5, 6)]


def test_redefinitions_are_ambiguous(tmp_path: Path) -> None:
    twice = "def f():\n    pass\ndef f():\n    pass\nf()\n"
    result = one(tmp_path, twice, "f()\n")
    assert (result.status, [d[3] for d in _defs(result)]) == ("ambiguous", [1, 3])
    mixed = "def f():\n    pass\nclass f:\n    pass\nf()\n"
    result = one(tmp_path, mixed, "f()\n")
    assert (result.status, [(d[1], d[3]) for d in _defs(result)]) == (
        "ambiguous",
        [("function", 1), ("class", 3)],
    )


def test_variable_shadowing(tmp_path: Path) -> None:
    spec = "User = OtherUser\n\ndef f():\n    User = LocalUser\n    User()\n"
    assert one(tmp_path, spec, "User()").status == "unsupported"
    after = "def f():\n    pass\nf = 1\nf\n"
    assert one(tmp_path, after, "f\n").status == "unsupported"
    local_def = "def f():\n    pass\ndef g():\n    def f():\n        pass\n    return f()\n"
    assert _defs(one(tmp_path, local_def, "return f()", offset=7)) == [
        ("m.py", "function", "f", 4, 5)
    ]


@pytest.mark.parametrize(
    "binding",
    [
        "name = 1",
        "name += 1",
        "name: int",
        "for name in x:\n        pass",
        "with x as name:\n        pass",
        "try:\n        pass\n    except E as name:\n        pass",
        "del name",
        "match x:\n        case name:\n            pass",
        "(name := 1)",
        "[(name := y) for y in x]",
        "global name",
        "import os as name",
    ],
    ids=[
        "assign",
        "augmented",
        "annotated",
        "for",
        "with",
        "except",
        "del",
        "match",
        "walrus",
        "walrus-in-comprehension",
        "global",
        "import",
    ],
)
def test_variable_like_bindings_shadow_definitions(tmp_path: Path, binding: str) -> None:
    source = f"def name():\n    pass\ndef g():\n    {binding}\n    return name\n"
    result = one(tmp_path, source, "return name", offset=7)
    expected = "not_found" if binding.startswith("import") else "unsupported"
    assert (result.status, result.definitions) == (expected, [])


def test_parameters_and_nonlocal(tmp_path: Path) -> None:
    source = "def name():\n    pass\ndef g(name):\n    return name\n"
    assert one(tmp_path, source, "return name", offset=7).status == "unsupported"
    nested = (
        "def name():\n    pass\n"
        "def g():\n    name = 1\n    def h():\n        nonlocal name\n        return name\n"
    )
    assert one(tmp_path, nested, "return name", offset=7).status == "unsupported"


def test_class_scopes_are_skipped_by_nested_functions(tmp_path: Path) -> None:
    source = (
        "class A:\n"  # 1
        "    helper = 1\n"  # 2
        "    def m(self):\n"  # 3
        "        return helper()\n"  # 4
        "    items = [helper for _ in range(2)]\n"  # 5
        "def helper():\n"  # 6
        "    pass\n"  # 7
    )
    assert _defs(one(tmp_path, source, "helper()")) == [("m.py", "function", "helper", 6, 7)]
    assert _defs(one(tmp_path, source, "helper for")) == [("m.py", "function", "helper", 6, 7)]


def test_builtins_unbound_names_and_star_imports(tmp_path: Path) -> None:
    source = "print(len(x))\nundefined_name\n"
    assert one(tmp_path, source, "print").status == "unsupported"
    assert one(tmp_path, source, "len").status == "unsupported"
    assert one(tmp_path, source, "undefined_name").status == "not_found"
    star = "from os import *\nmystery()\n"
    assert one(tmp_path, star, "mystery").status == "unsupported"


# --- Imports -----------------------------------------------------------------------------


def test_imported_symbols_aliases_and_module_chains(tmp_path: Path) -> None:
    for needle in ("UserService()", "US()", "UserService()\nusers", "Exported()"):
        result = main(tmp_path, needle)
        assert (result.status, _defs(result)) == ("resolved", [USER_SERVICE]), needle
    result = main(tmp_path, "UserService()", nth=1)  # pkg.users.UserService
    assert (result.status, _defs(result)) == ("resolved", [USER_SERVICE])
    result = main(tmp_path, "users.UserService()", nth=1)  # users.UserService (alias)
    assert result.reference.name == "users"
    assert _defs(result) == [("pkg/users.py", "module", "users", 1, 1)]


def test_imported_module_names(tmp_path: Path) -> None:
    assert _defs(main(tmp_path, "pkg.users.UserService()")) == [
        ("pkg/__init__.py", "module", "pkg", 1, 1)
    ]
    assert _defs(main(tmp_path, "users.UserService()")) == [
        ("pkg/users.py", "module", "users", 1, 1)
    ]  # the attribute in pkg.users.UserService
    assert _defs(main(tmp_path, "sub\n", nth=1)) == [("pkg/sub/__init__.py", "module", "sub", 1, 1)]


def test_names_inside_import_statements(tmp_path: Path) -> None:
    assert _defs(main(tmp_path, "UserService")) == [USER_SERVICE]
    assert _defs(main(tmp_path, "US\n")) == [USER_SERVICE]
    assert _defs(main(tmp_path, "pkg.users\n", offset=4)) == [
        ("pkg/users.py", "module", "users", 1, 1)
    ]  # "users" in "import pkg.users"
    assert _defs(main(tmp_path, "pkg.users\n")) == [("pkg/__init__.py", "module", "pkg", 1, 1)]
    assert _defs(main(tmp_path, "as users", offset=3)) == [
        ("pkg/users.py", "module", "users", 1, 1)
    ]
    assert _defs(main(tmp_path, "users import UserService\n", nth=0)) == [
        ("pkg/users.py", "module", "users", 1, 1)
    ]  # the module part of "from pkg.users import ..."


def test_relative_imports_and_packages(tmp_path: Path) -> None:
    source = REPO["pkg/sub/tools.py"]
    assert isinstance(source, str)
    for needle, expected in (
        ("make()", [("pkg/users.py", "function", "make", 5, 6)]),
        ("helpers\n", [("pkg/sub/helpers.py", "module", "helpers", 1, 1)]),
        ("users", [("pkg/users.py", "module", "users", 1, 1)]),
    ):
        line, column = at(source, needle, nth=1 if needle == "helpers\n" else 0)
        result = define(tmp_path, REPO, "pkg/sub/tools.py", line, column)
        assert (result.status, _defs(result)) == ("resolved", expected), needle


def test_pyi_and_unresolved_imports(tmp_path: Path) -> None:
    assert _defs(main(tmp_path, "stub()")) == [("stubs/typed.pyi", "function", "stub", 1, 1)]
    assert main(tmp_path, "os.path").status == "not_found"  # the standard library
    assert main(tmp_path, "requests.get").status == "not_found"  # third party
    assert main(tmp_path, "get()").status == "not_found"
    assert main(tmp_path, "join()").status == "unsupported"  # os.path is no import chain
    assert main(tmp_path, "missing()").status == "not_found"


def test_ambiguous_modules_and_whole_segments(tmp_path: Path) -> None:
    result = main(tmp_path, "helper()")
    assert (result.status, _defs(result)) == (
        "ambiguous",
        [
            ("lib/utils.py", "function", "helper", 1, 2),
            ("tools/utils.py", "function", "helper", 1, 2),
        ],
    )
    assert main(tmp_path, "run()").status == "not_found"  # app.main is not web/xapp/main.py
    assert _defs(main(tmp_path, "run()", nth=1)) == [("web/xapp/main.py", "function", "run", 1, 2)]


def test_import_hops(tmp_path: Path) -> None:
    assert MAX_IMPORT_HOPS == 3
    expected = [("h/one.py", "class", "X", 1, 2)]
    for needle in ("X1", "X2", "X3"):
        result = main(tmp_path, needle, nth=1)
        assert (result.status, _defs(result)) == ("resolved", expected), needle
    assert main(tmp_path, "X4", nth=1).status == "unsupported"  # a fourth import statement


def test_star_import_in_a_module(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "from lib import thing\nthing\n",
        "lib.py": "from os import *\n",
    }
    assert define(tmp_path, files, "m.py", 2, 1).status == "unsupported"


def test_module_level_non_definitions(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "from lib import value, nothing\nvalue\nnothing\n",
        "lib.py": "value = 1\n",
    }
    assert define(tmp_path, files, "m.py", 2, 1).status == "unsupported"
    assert define(tmp_path, files, "m.py", 3, 1).status == "not_found"


# --- Attributes -------------------------------------------------------------------------


def test_attributes_without_type_inference(tmp_path: Path) -> None:
    source = (
        "class User:\n"
        "    def save(self):\n"
        "        self.save()\n"
        "        self.service\n"
        "user = User()\n"
        "user.save()\n"
        "User.save\n"
        "get().save\n"
    )
    for needle, nth in (("save()", 0), ("service", 0), ("save()", 1), ("save\n", 0), ("save\n", 1)):
        result = one(tmp_path, source, needle, nth)
        assert (result.status, result.definitions) == ("unsupported", []), (needle, nth)


# --- File statuses ----------------------------------------------------------------------


def test_file_statuses(tmp_path: Path, read_spy: list[str]) -> None:
    assert define(tmp_path, {"m.py": "def (:\n"}, "m.py", 1, 1).status == "syntax_error"
    assert define(tmp_path, {"m.py": b"\x00\x01"}, "m.py", 1, 1).status == "binary"
    assert define(tmp_path, {"m.py": b"\xff\xfe"}, "m.py", 1, 1).status == "binary"
    assert define(tmp_path, {"m.py": ""}, "m.py", 1, 1).status == "no_identifier"
    body = b"def target():\n    pass\ntarget()\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    assert define(tmp_path, {"m.py": exactly}, "m.py", 3, 1).status == "resolved"
    over = define(tmp_path, {"m.py": exactly + b"#"}, "m.py", 3, 1)
    assert (over.status, over.reference) == ("too_large", None)
    read_spy.clear()
    limit = os.urandom(MAX_CONTENT_BYTES)
    assert define(tmp_path, {"m.py": limit}, "m.py", 1, 1).status in ("binary", "too_large")
    assert read_spy == ["m.py"]  # exactly 1 MiB is opened
    read_spy.clear()
    assert define(tmp_path, {"m.py": limit + b"x"}, "m.py", 1, 1).status == "too_large"
    assert read_spy == []  # over 1 MiB is never opened


def test_unanalyzable_modules_are_unsupported(tmp_path: Path) -> None:
    for module in (b"def (:\n", b"\x00\x01", b"x = 1\n" + b"#" * MAX_OUTLINE_BYTES):
        files: dict[str, str | bytes] = {"m.py": "from lib import thing\nthing\n", "lib.py": module}
        assert define(tmp_path, files, "m.py", 2, 1).status == "unsupported"


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
    root = tmp_path / "repo"
    root.mkdir()
    rows = _write(root, files)
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
    result = analyze_definition(root, next(r for r in rows if r.path == "m.py"), rows, 2, 1)
    monkeypatch.undo()
    assert _defs(result) == [("evil.py", "function", "target", 13, 14)]
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


# --- Snapshot and reads -----------------------------------------------------------------


def test_changed_target_or_module_fails_the_request(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "from lib import thing\nthing\n",
        "lib.py": "def thing():\n    pass\n",
    }
    root = tmp_path / "repo"
    root.mkdir()
    rows = _write(root, files)
    target = next(r for r in rows if r.path == "m.py")
    (root / "lib.py").write_bytes(b"def thing():\n    pass  # changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_definition(root, target, rows, 2, 1)
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "lib.py").unlink()  # missing
    with pytest.raises(FileContentError):
        analyze_definition(root, target, rows, 2, 1)
    (root / "m.py").write_bytes(b"thing\n")  # the target itself changed
    with pytest.raises(FileContentError):
        analyze_definition(root, target, rows, 1, 1)


def test_only_needed_snapshot_files_are_read(tmp_path: Path, read_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "m.py": "from stray import thing\nfrom lib import used\nthing\nused\n",
        "lib.py": "def used():\n    pass\n",
        "unrelated.py": "def other():\n    pass\n",
    }
    root = tmp_path / "repo"
    root.mkdir()
    rows = _write(root, files)
    (root / "stray.py").write_bytes(b"def thing():\n    pass\n")  # on disk, not in the snapshot
    target = next(r for r in rows if r.path == "m.py")
    assert analyze_definition(root, target, rows, 3, 1).status == "not_found"
    assert analyze_definition(root, target, rows, 4, 1).status == "resolved"
    assert "stray.py" not in read_spy and "unrelated.py" not in read_spy
    assert sorted(set(read_spy)) == ["lib.py", "m.py"]


# --- Limits ------------------------------------------------------------------------------


def test_module_file_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_MODULE_FILES == 10
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 3)  # X3 reads three, two, one
    result = main(tmp_path, "X3", nth=1)
    assert (result.status, result.truncated) == ("resolved", False)
    monkeypatch.setattr(code_definition, "MAX_MODULE_FILES", 2)
    result = main(tmp_path, "X3", nth=1)
    assert (result.status, result.definitions, result.truncated) == ("unsupported", [], True)


def test_import_hop_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_definition, "MAX_IMPORT_HOPS", 2)
    assert main(tmp_path, "X2", nth=1).status == "resolved"
    assert main(tmp_path, "X3", nth=1).status == "unsupported"
    monkeypatch.setattr(code_definition, "MAX_IMPORT_HOPS", 4)
    assert main(tmp_path, "X4", nth=1).status == "resolved"


def test_definition_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_DEFINITIONS == 20
    source = "def f():\n    pass\n" * 4 + "f()\n"
    monkeypatch.setattr(code_definition, "MAX_DEFINITIONS", 3)
    result = one(tmp_path, source, "f()\n")
    assert (result.status, [d[3] for d in _defs(result)], result.truncated) == (
        "ambiguous",
        [1, 3, 5],
        True,
    )
    monkeypatch.setattr(code_definition, "MAX_DEFINITIONS", 4)
    result = one(tmp_path, source, "f()\n")
    assert (len(result.definitions), result.truncated) == (4, False)


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
        f"/repositories/{repository_id or owner.repository_id}/files/definition"
    )


async def _get(
    client: httpx.AsyncClient, owner: Owner, params: dict[str, Any], **kw: Any
) -> httpx.Response:
    url = _url(owner, kw.pop("project_id", None), kw.pop("repository_id", None))
    return await client.get(url, params=params, headers=kw.pop("headers", owner.headers))


def _main_params(needle: str, nth: int = 0) -> dict[str, Any]:
    line, column = at(MAIN, needle, nth)
    return {"path": "main.py", "line": line, "column": column}


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_definition_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
    response = await _get(client, alice, _main_params("Exported()"))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "resolved",
        "reference": {"path": "main.py", "line": 22, "column": 1, "name": "Exported"},
        "definitions": [
            {
                "path": "pkg/users.py",
                "kind": "class",
                "name": "UserService",
                "line": 1,
                "end_line": 2,
            }
        ],
        "truncated": False,
    }


@requires_db
async def test_endpoint_statuses(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    body = (await _get(client, alice, {"path": "docs/notes.md", "line": 1, "column": 5})).json()
    assert body == {
        "status": "unsupported_language",
        "reference": None,
        "definitions": [],
        "truncated": False,
    }
    assert read_spy == []  # a non-Python file is never read
    body = (await _get(client, alice, {"path": "main.py", "line": 999, "column": 1})).json()
    assert (body["status"], body["reference"]) == ("no_identifier", None)
    body = (await _get(client, alice, _main_params("helper()"))).json()
    assert body["status"] == "ambiguous" and len(body["definitions"]) == 2


@requires_db
async def test_validation_and_unknown_paths(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    base = {"path": "main.py", "line": 18, "column": 1}
    for change in (
        {"line": 0},
        {"line": -1},
        {"column": 0},
        {"column": -3},
        {"line": "1.5"},
        {"column": "x"},
        {"path": ""},
        {"path": "a" * 4097},
    ):
        response = await _get(client, alice, {**base, **change})
        assert response.status_code == 422, change
    for missing in ("path", "line", "column"):
        params = {k: v for k, v in base.items() if k != missing}
        assert (await _get(client, alice, params)).status_code == 422
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _get(client, alice, base, **kwargs)).status_code == 422
    assert read_spy == []
    for path in ("missing.py", "../main.py", "/main.py", "MAIN.PY"):
        response = await _get(client, alice, {**base, "path": path})
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)


@requires_db
async def test_index_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python rows by path: h/four.py, h/one.py, h/three.py, h/two.py, lib/utils.py, main.py, ...
    monkeypatch.setattr(code_definition, "MAX_INDEXED_PATHS", 5)
    body = (await _get(client, alice, _main_params("UserService()"))).json()
    assert (body["status"], body["truncated"]) == ("not_found", True)  # pkg/users.py unindexed
    monkeypatch.setattr(code_definition, "MAX_INDEXED_PATHS", 15)  # exactly all 15
    body = (await _get(client, alice, _main_params("UserService()"))).json()
    assert (body["status"], body["truncated"]) == ("resolved", False)


@requires_db
async def test_changed_storage_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    (_storage(alice) / "pkg" / "users.py").write_bytes(
        b"class UserService:\n    pass  # tampered\n"
    )
    response = await _get(client, alice, _main_params("UserService()"))
    assert (response.status_code, response.json()) == (409, CHANGED)
    assert "tampered" not in response.text

    await _ingest(client, alice, FILES)
    (_storage(alice) / "main.py").write_bytes(MAIN.encode() + b"# changed\n")
    response = await _get(client, alice, _main_params("UserService()"))
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    root = _storage(alice)
    outside = tmp_path / "outside"
    shutil.copytree(root / "pkg", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pkg")
    _junction(root / "pkg", outside)
    response = await _get(client, alice, _main_params("UserService()"))
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_symlinked_module_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "pkg" / "users.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(FILES["pkg/users.py"])
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    response = await _get(client, alice, _main_params("UserService()"))
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_storage_swap_during_the_request_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the target is read, a module file changes before it is read."""
    real = code_definition.collect_scopes
    root = _storage(alice)

    def collect_then_swap(tree: Any) -> Any:
        (root / "pkg" / "users.py").write_bytes(b"class UserService:\n    pass  # new snapshot\n")
        return real(tree)

    monkeypatch.setattr(code_definition, "collect_scopes", collect_then_swap)
    response = await _get(client, alice, _main_params("UserService()"))
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
        body = (await _get(client, alice, _main_params("Exported()"))).json()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert body["status"] == "resolved"
    assert len([s for s in statements if "FROM files" in s]) == 1


@requires_db
async def test_reingestion_moves_the_definition(client: httpx.AsyncClient, alice: Owner) -> None:
    files = dict(FILES)
    files["pkg/users.py"] = b"\n\n\nclass UserService:\n    pass\n"
    await _ingest(client, alice, files)
    body = (await _get(client, alice, _main_params("UserService()"))).json()
    assert [(d["path"], d["line"]) for d in body["definitions"]] == [("pkg/users.py", 4)]
    del files["pkg/users.py"]
    await _ingest(client, alice, files)
    body = (await _get(client, alice, _main_params("UserService()"))).json()
    assert body["status"] == "not_found"


@requires_db
async def test_no_source_internals_or_logged_content(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-6f1d"
    files = dict(FILES)
    files["pkg/users.py"] = f"class UserService:  # {marker}\n    value = '{marker}'\n".encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, _main_params("UserService()"))
    assert response.status_code == 200 and response.json()["status"] == "resolved"
    assert marker not in response.text and marker not in caplog.text
    assert "import" not in response.text and "pass" not in response.text
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
    params = {"path": "b.py", "line": 3, "column": 1}
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
    bob = await _owner(client, "bob@example.com", {"b.py": b"x\n"})
    limiter = _AlwaysLimited()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    params = {"path": "b.py", "line": 1, "column": 1}
    for kwargs in (
        {"project_id": bob.project_id, "repository_id": bob.repository_id},
        {"repository_id": str(uuid.uuid4())},
    ):
        response = await _get(client, alice, params, **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert limiter.hits == []
    limited = await _get(client, alice, _main_params("Exported()"))
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts; please try again later"}
    assert limiter.hits == [(DEFINITION_LIMIT, alice.user_id)]
    assert read_spy == []


@requires_db_and_redis
async def test_rate_limit_is_120_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert DEFINITION_LIMIT == Limit("definition-user", 120, 600)
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
        assert any(k.startswith("ratelimit:definition-user:") for k in keys)
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
