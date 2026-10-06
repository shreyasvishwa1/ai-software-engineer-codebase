"""Python unused imports tests (GET .../files/unused-imports?path=). Offline.

Analysis tests use a temporary storage directory and need no database. Expected entries are
written out by hand from the Step 62 rules. Two independent oracles cross-check them: CPython's own
``symtable`` (test-only, on fixture text the tests authored) for every listed import, and an ``ast``
walk that knows nothing of the service's scope machinery. API tests run against the
already-migrated throwaway test database (TEST_DATABASE_URL, "_test") inside rolled-back
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
from app.services import code_unused_imports, ingestion
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.code_unused_imports import (
    MAX_UNUSED_IMPORTS,
    UNUSED_IMPORTS_LIMIT,
    analyze_unused_imports,
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
    return analyze_unused_imports(root, _row(rows, path))


def listed(result: Any) -> list[tuple[int, int, str]]:
    return [(i.line, i.column, i.name) for i in result.imports]


def summary(result: Any) -> tuple[int, int, int]:
    return result.summary.unused, result.summary.distinct, result.summary.unsupported


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_unused_imports._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_unused_imports, "_read_verified", spy)
    return reads


# --- A. Import forms and candidates (I1, I2, I7) -----------------------------------------------

IMPORT_FORMS = (
    "import alpha\n"  # 1
    "import beta.gamma.delta\n"  # 2: binds beta
    "import epsilon.zeta as eta\n"  # 3: binds eta
    "from theta import iota\n"  # 4
    "from kappa import lam as mu\n"  # 5: binds mu
    "from . import nu\n"  # 6
    "from ..xi import omicron\n"  # 7
    "from pi import (\n"  # 8
    "    rho,\n"  # 9
    "    sigma as tau,\n"  # 10: binds tau
    ")\n"  # 11
)


def test_every_import_form_lists_its_bound_name(tmp_path: Path) -> None:
    result = unused(tmp_path, IMPORT_FORMS)
    assert result.status == "parsed" and result.path == "m.py"
    assert listed(result) == [
        (1, 8, "alpha"),
        (2, 8, "beta"),
        (3, 24, "eta"),
        (4, 19, "iota"),
        (5, 26, "mu"),
        (6, 15, "nu"),
        (7, 18, "omicron"),
        (9, 5, "rho"),
        (10, 14, "tau"),
    ]
    assert summary(result) == (9, 9, 0) and result.truncated is False


def test_an_alias_spanning_lines_lists_the_name_after_as(tmp_path: Path) -> None:
    assert listed(unused(tmp_path, "from upsilon import (phi\n    as chi)\n")) == [(2, 8, "chi")]


def test_star_imports_are_never_listed(tmp_path: Path) -> None:
    result = unused(tmp_path, "from os import *\nfrom os.path import join\n")
    assert listed(result) == [(2, 21, "join")] and summary(result) == (1, 1, 0)


def test_future_imports_are_directives_but_a_relative_future_module_is_not(tmp_path: Path) -> None:
    source = "from __future__ import annotations\nimport os\nfrom .__future__ import x\n"
    result = unused(tmp_path, source)
    assert listed(result) == [(2, 8, "os"), (3, 25, "x")] and summary(result) == (2, 2, 0)


# Each name bound by any other form (next to its import) is removed entirely; ``keep`` proves the
# file was analyzed.
MIXED: dict[str, str] = {
    "assignment": "import os\nos = 1\n",
    "annotated_assignment": "import os\nos: int = 1\n",
    "augmented_assignment": "import os\nos += 1\n",
    "def": "import os\ndef os():\n    pass\n",
    "class": "import os\nclass os:\n    pass\n",
    "parameter": "def f(os):\n    import os\n",
    "for_target": "import os\nfor os in []:\n    pass\n",
    "with_target": "import os\nwith open('x') as os:\n    pass\n",
    "except_target": "import os\ntry:\n    pass\nexcept Exception as os:\n    pass\n",
    "walrus": "import os\n(os := 1)\n",
    "match_capture": "import os\nmatch 1:\n    case os:\n        pass\n",
    "del": "import os\ndel os\n",
    "global": "def f():\n    global os\n    import os\n",
    "nonlocal": "def outer():\n    import os\n    def inner():\n        nonlocal os\n"
    "        os = 1\n    return inner\n",
    "fallback_assignment": "try:\n    import json\nexcept ImportError:\n    json = None\n",
}


@pytest.mark.parametrize("form", sorted(MIXED))
def test_names_with_any_other_binding_form_are_removed_entirely(tmp_path: Path, form: str) -> None:
    result = unused(tmp_path, "import keep\n" + MIXED[form])
    assert listed(result) == [(1, 8, "keep")]
    assert summary(result) == (1, 1, 0)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os\nimport os\n", [(1, 8, "os"), (2, 8, "os")]),
        (
            "try:\n    import json\nexcept ImportError:\n    import simplejson as json\n",
            [(2, 12, "json"), (4, 26, "json")],
        ),
        ("import a.b\nimport a.c\n", [(1, 8, "a"), (2, 8, "a")]),
    ],
)
def test_several_imports_of_one_name_are_one_binding(
    tmp_path: Path, source: str, expected: list[tuple[int, int, str]]
) -> None:
    result = unused(tmp_path, source)
    assert listed(result) == expected
    assert summary(result) == (2, 1, 0)


def test_class_bodies_are_never_examined(tmp_path: Path) -> None:
    result = unused(tmp_path, "class C:\n    import os\n    def m(self):\n        import json\n")
    assert listed(result) == [(4, 16, "json")] and summary(result) == (1, 1, 0)


def test_mixed_bindings_are_removed_before_any_later_rule(tmp_path: Path) -> None:
    result = unused(tmp_path, "import keep\nimport os\nos = 1\n", "pkg/__init__.py")
    assert listed(result) == [] and summary(result) == (0, 0, 1)  # only keep; os is no candidate


# --- B. Reads (I3) -----------------------------------------------------------------------------

# Every import below is read somewhere that resolves to its binding; ``keep`` is never read.
READS: dict[str, str] = {
    "name": "import os\nprint(os)\n",
    "attribute_base": "import os\nos.path.join('a')\n",
    "call": "from m import run\nrun()\n",
    "decorator": "from m import deco\n@deco\ndef f():\n    pass\n",
    "default": "from m import DEFAULT\ndef f(x=DEFAULT):\n    return x\n",
    "annotation": "from m import Thing\ndef f(x: Thing) -> Thing:\n    return x\n",
    "type_checking": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
    "    from m import Thing\ndef f(x: Thing) -> None:\n    pass\n",
    "f_string": "import os\nprint(f'{os.sep}')\n",
    "closure": "def f():\n    import os\n    def g():\n        return os\n    return g\n",
    "comprehension": "import os\nprint([os for _ in range(3)])\n",
    "lambda": "import os\nhandler = lambda: os.sep\n",
    "nested_class_body": "def f():\n    import os\n    class C:\n        sep = os.sep\n    return C\n",
    "read_before_the_import": "print(os)\nimport os\n",
    "function_scope": "def f():\n    import json\n    return json.dumps(1)\n",
}


@pytest.mark.parametrize("case", sorted(READS))
def test_any_read_resolving_to_the_binding_keeps_the_import(tmp_path: Path, case: str) -> None:
    result = unused(tmp_path, "import keep\n" + READS[case])
    assert listed(result) == [(1, 8, "keep")]
    assert summary(result) == (1, 1, 0)


# The same identifier elsewhere is not a read of the import.
NOT_READS: dict[str, tuple[str, list[tuple[int, int, str]]]] = {
    "same_name_in_another_function": (
        "def f():\n    import os\ndef g():\n    return os\n",
        [(2, 12, "os")],
    ),
    "attribute_name": ("from os import path\nimport sys\nprint(sys.path)\n", [(1, 16, "path")]),
    "keyword_argument_name": ("import os\nprint(os=1)\n", [(1, 8, "os")]),
    "string_and_comment": ("import os\nprint('os')  # os\n", [(1, 8, "os")]),
    "pep_695_type_parameter": (
        "from m import T\ndef f[T](x: T) -> T:\n    return x\n",
        [(1, 15, "T")],
    ),
    "local_shadowing": ("import os\ndef f():\n    os = 1\n    return os\n", [(1, 8, "os")]),
}


@pytest.mark.parametrize("case", sorted(NOT_READS))
def test_the_same_identifier_elsewhere_is_not_a_read(tmp_path: Path, case: str) -> None:
    source, expected = NOT_READS[case]
    result = unused(tmp_path, source)
    assert listed(result) == expected
    assert summary(result) == (len(expected), len(expected), 0)


# --- C. Re-exports (I4) ------------------------------------------------------------------------


def test_redundant_aliases_are_explicit_re_exports(tmp_path: Path) -> None:
    source = (
        "import keep\n"  # 1
        "import alpha as alpha\n"  # 2
        "from m import beta as beta\n"  # 3
        "import gamma as delta\n"  # 4: not redundant
        "def f():\n"  # 5
        "    import zeta as zeta\n"  # 6
    )
    result = unused(tmp_path, source)
    assert listed(result) == [(1, 8, "keep"), (4, 17, "delta")]
    assert summary(result) == (2, 2, 0)  # re-exports are never counted


LITERAL_ALL: dict[str, str] = {
    "list": "__all__ = ['alpha']\n",
    "tuple": "__all__ = ('alpha',)\n",
    "augmented_list": "__all__ = []\n__all__ += ['alpha']\n",
    "augmented_tuple": "__all__ = ()\n__all__ += ('alpha',)\n",
    "annotated": "__all__: list[str] = ['alpha']\n",
    "nfkc": "__all__ = ['\uff41lpha']\n",
}


@pytest.mark.parametrize("case", sorted(LITERAL_ALL))
def test_names_in_a_literal_all_are_re_exports(tmp_path: Path, case: str) -> None:
    result = unused(tmp_path, "import alpha\nimport beta\n" + LITERAL_ALL[case])
    assert listed(result) == [(2, 8, "beta")]
    assert summary(result) == (1, 1, 0)


def test_the_module_all_never_covers_function_imports(tmp_path: Path) -> None:
    result = unused(tmp_path, "__all__ = ['os']\ndef f():\n    import os\n")
    assert listed(result) == [(3, 12, "os")] and summary(result) == (1, 1, 0)


def test_an_all_inside_a_function_is_not_the_module_interface(tmp_path: Path) -> None:
    source = "import alpha\ndef f():\n    __all__ = ['alpha']\n    return __all__\n"
    result = unused(tmp_path, source)
    assert listed(result) == [(1, 8, "alpha")] and summary(result) == (1, 1, 0)


# --- D. Unsupported (I5) -----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["pkg/__init__.py", "pkg/__init__.pyi", "stubs/m.pyi"])
def test_module_imports_of_package_and_stub_interfaces_are_unsupported(
    tmp_path: Path, path: str
) -> None:
    result = unused(tmp_path, "import alpha\ndef f():\n    import beta\n", path)
    assert listed(result) == [(3, 12, "beta")]  # function scopes are still examined
    assert summary(result) == (1, 1, 1)


def test_similar_file_names_are_ordinary_modules(tmp_path: Path) -> None:
    for path in ("pkg/not__init__.py", "pkg/__init__x.py", "pkg/init.py"):
        result = unused(tmp_path, "import alpha\n", path)
        assert (listed(result), summary(result)) == ([(1, 8, "alpha")], (1, 1, 0)), path


NON_LITERAL_ALL: dict[str, str] = {
    "computed": "__all__ = ['alpha'] + []\n",
    "extend": "__all__ = ['alpha']\n__all__.extend(['beta'])\n",
    "read": "__all__ = ['alpha']\nprint(__all__)\n",
    "global": "__all__ = ['alpha']\ndef f():\n    global __all__\n    __all__ = ['beta']\n",
    "annotation_only": "__all__: list[str]\n",
    "del": "__all__ = ['alpha']\ndel __all__\n",
    "non_string": "__all__ = ['alpha', 1]\n",
    "augmented_multiply": "__all__ = ['alpha']\n__all__ *= ['beta']\n",  # a literal, but not +=
}


@pytest.mark.parametrize("case", sorted(NON_LITERAL_ALL))
def test_a_non_literal_all_makes_module_imports_unsupported(tmp_path: Path, case: str) -> None:
    result = unused(tmp_path, "import alpha\nimport beta\n" + NON_LITERAL_ALL[case])
    assert listed(result) == []
    assert summary(result) == (0, 0, 2)


@pytest.mark.parametrize(
    "call", ["globals()", "locals()", "vars()", "eval('alpha')", "exec('alpha')"]
)
def test_dynamic_module_access_makes_module_imports_unsupported(tmp_path: Path, call: str) -> None:
    result = unused(tmp_path, f"import alpha\ndef f():\n    import beta\nprint({call})\n")
    assert listed(result) == [(3, 12, "beta")]
    assert summary(result) == (1, 1, 1)


def test_dynamic_access_anywhere_in_the_file_reaches_module_imports(tmp_path: Path) -> None:
    result = unused(tmp_path, "import alpha\ndef f():\n    return globals()\n")
    assert listed(result) == [] and summary(result) == (0, 0, 1)


def test_function_imports_follow_the_dynamic_function_rule(tmp_path: Path) -> None:
    for source in (
        "def f():\n    import json\n    return locals()\n",
        "def f():\n    import json\n    return lambda: eval('json')\n",
        "def f():\n    import json\n    def g():\n        return vars()\n    return g\n",
    ):
        result = unused(tmp_path, source)
        assert (listed(result), summary(result)) == ([], (0, 0, 1)), source


def test_globals_never_reads_function_imports(tmp_path: Path) -> None:
    result = unused(tmp_path, "def f():\n    import json\n    return globals()\n")
    assert listed(result) == [(2, 12, "json")] and summary(result) == (1, 1, 0)


def test_dynamic_access_in_a_sibling_function_never_suppresses(tmp_path: Path) -> None:
    result = unused(tmp_path, "def f():\n    import json\ndef g():\n    return locals()\n")
    assert listed(result) == [(2, 12, "json")] and summary(result) == (1, 1, 0)


def test_names_inside_string_annotations_are_unsupported(tmp_path: Path) -> None:
    for source in (
        "from m import Thing\ndef f(x: 'Thing'):\n    return x\n",
        "def f():\n    from m import Thing\n    def g(x: 'Thing'):\n        return x\n    return g\n",
        "from m import Thing\nvalue: 'list[Thing]' = []\n",
    ):
        result = unused(tmp_path, source)
        assert (listed(result), summary(result)) == ([], (0, 0, 1)), source


def test_ordinary_strings_never_suppress(tmp_path: Path) -> None:
    result = unused(tmp_path, "from m import Thing\nprint('Thing')\n'''Thing is documented.'''\n")
    assert listed(result) == [(1, 15, "Thing")] and summary(result) == (1, 1, 0)


# --- E. Declared names (I6) --------------------------------------------------------------------


def test_a_global_declaration_anywhere_removes_the_name(tmp_path: Path) -> None:
    result = unused(tmp_path, "import os\nimport keep\ndef f():\n    global os\n")
    assert listed(result) == [(2, 8, "keep")] and summary(result) == (1, 1, 0)


def test_a_nonlocal_declaration_anywhere_removes_the_name(tmp_path: Path) -> None:
    source = (
        "def f():\n"  # 1
        "    import json\n"  # 2
        "    import keep\n"  # 3
        "def outer():\n"  # 4
        "    json = 1\n"  # 5
        "    def inner():\n"  # 6
        "        nonlocal json\n"  # 7
        "    return inner\n"  # 8
    )
    result = unused(tmp_path, source)
    assert listed(result) == [(3, 12, "keep")] and summary(result) == (1, 1, 0)


# --- F. Entries, positions and limits (I7) -----------------------------------------------------

ORDERED = "import zeta\ndef f():\n    import alpha\nimport beta\n"


def test_entries_follow_source_order_across_scopes(tmp_path: Path) -> None:
    result = unused(tmp_path, ORDERED)
    assert listed(result) == [(1, 8, "zeta"), (3, 12, "alpha"), (4, 8, "beta")]
    assert summary(result) == (3, 3, 0)


def test_distinct_counts_bindings_not_names(tmp_path: Path) -> None:
    result = unused(tmp_path, "import os\ndef f():\n    import os\n")
    assert listed(result) == [(1, 8, "os"), (3, 12, "os")] and summary(result) == (2, 2, 0)


def test_columns_count_code_points_and_names_are_nfkc(tmp_path: Path) -> None:
    source = (
        "s = '\u00e9'; import os\nimport \uff4dath\nfrom m import \uff56\uff41\uff4c as \uff58\n"
    )
    assert listed(unused(tmp_path, source.encode())) == [
        (1, 17, "os"),
        (2, 8, "math"),
        (3, 22, "x"),
    ]


def test_tabs_line_endings_and_bom(tmp_path: Path) -> None:
    assert listed(unused(tmp_path, "def f():\n\timport os\n")) == [(2, 9, "os")]
    for source in (
        b"import os\r\nimport sys\r\n",
        b"import os\rimport sys\r",
        b"\xef\xbb\xbfimport os\nimport sys\n",
    ):
        assert listed(unused(tmp_path, source)) == [(1, 8, "os"), (2, 8, "sys")], source


def test_long_names_are_capped_after_normalization(tmp_path: Path) -> None:
    for name in ("n" * 200, "n" * 201, "n" * 300, "\uff4e" * 201):
        result = unused(tmp_path, f"import {name}\n".encode())
        assert listed(result) == [(1, 8, "n" * 200)] and result.truncated is False


def _many(count: int, distinct: bool = True) -> str:
    return "".join(f"import m{i if distinct else ''}\n" for i in range(count))


def test_exactly_the_cap_is_not_truncated(tmp_path: Path) -> None:
    assert MAX_UNUSED_IMPORTS == 500
    result = unused(tmp_path, _many(500))
    assert (len(result.imports), result.truncated) == (500, False)
    assert summary(result) == (500, 500, 0)
    assert listed(result)[0] == (1, 8, "m0") and listed(result)[-1] == (500, 8, "m499")


def test_one_over_the_cap_is_truncated_and_counted_before_the_cap(tmp_path: Path) -> None:
    result = unused(tmp_path, _many(501))
    assert (len(result.imports), result.truncated) == (500, True)
    assert listed(result)[-1] == (500, 8, "m499")
    assert summary(result) == (501, 501, 0)
    same = unused(tmp_path, _many(501, distinct=False))
    assert (len(same.imports), same.truncated, summary(same)) == (500, True, (501, 1, 0))


# --- G. Statuses, snapshot and safety (offline) ------------------------------------------------


def _not_parsed(result: Any, status: str) -> None:
    assert result.model_dump() == {
        "path": "m.py",
        "status": status,
        "imports": [],
        "summary": ZERO,
        "truncated": False,
    }


def test_empty_file(tmp_path: Path) -> None:
    result = unused(tmp_path, "")
    assert (result.status, result.imports, result.summary.model_dump()) == ("parsed", [], ZERO)


def test_syntax_error(tmp_path: Path) -> None:
    _not_parsed(unused(tmp_path, "import (\n"), "syntax_error")
    _not_parsed(unused(tmp_path, "import os\nx = '''\n"), "syntax_error")


def test_tokenize_failure_is_a_syntax_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(source: str) -> Any:
        raise tokenize.TokenError("simulated", (1, 0))

    monkeypatch.setattr(code_unused_imports, "name_tokens", failing)
    _not_parsed(unused(tmp_path, "import os\n"), "syntax_error")


def test_binary(tmp_path: Path) -> None:
    _not_parsed(unused(tmp_path, b"\x00\x01import os\n"), "binary")
    _not_parsed(unused(tmp_path, b"\xff\xfe"), "binary")


def test_decoded_size_boundary(tmp_path: Path) -> None:
    body = b"import os\n"
    exactly = body + b"#" * (MAX_OUTLINE_BYTES - len(body))
    assert len(exactly) == MAX_OUTLINE_BYTES
    assert listed(unused(tmp_path, exactly)) == [(1, 8, "os")]
    _not_parsed(unused(tmp_path, exactly + b"#"), "too_large")  # never parsed


def test_stored_size_limit_is_checked_before_opening(tmp_path: Path, read_spy: list[str]) -> None:
    result = unused(tmp_path, os.urandom(MAX_CONTENT_BYTES))
    assert result.status in ("binary", "too_large") and read_spy == ["m.py"]  # exactly 1 MiB opens
    read_spy.clear()
    _not_parsed(unused(tmp_path, os.urandom(MAX_CONTENT_BYTES) + b"x"), "too_large")
    assert read_spy == []  # over 1 MiB is never opened


def test_changes_fail_the_whole_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "import os\n"})
    (root / "m.py").write_bytes(b"import os  # changed\n")
    with pytest.raises(FileContentError) as raised:
        analyze_unused_imports(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])
    (root / "m.py").unlink()  # missing
    with pytest.raises(FileContentError) as raised:
        analyze_unused_imports(root, rows[0])
    assert (raised.value.status_code, raised.value.detail) == (409, CHANGED["detail"])


def test_symlinked_file_fails_the_request(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "import os\n"})
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"import os\n")
    (root / "m.py").unlink()
    try:
        os.symlink(outside, root / "m.py")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    with pytest.raises(FileContentError) as raised:
        analyze_unused_imports(root, rows[0])
    assert raised.value.status_code == 409


def test_junctioned_package_fails_the_request(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    root, rows = _repo(tmp_path, {"pkg/m.py": "import os\n"})
    outside = tmp_path / "outside"
    shutil.copytree(root / "pkg", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "pkg")
    _winapi.CreateJunction(str(outside), str(root / "pkg"))  # type: ignore[attr-defined]
    with pytest.raises(FileContentError) as raised:
        analyze_unused_imports(root, rows[0])
    assert raised.value.status_code == 409


def test_nothing_is_cached_across_requests(tmp_path: Path) -> None:
    root, rows = _repo(tmp_path, {"m.py": "import os\n"})
    assert listed(analyze_unused_imports(root, rows[0])) == [(1, 8, "os")]
    changed = b"import os\nprint(os)\n"
    (root / "m.py").write_bytes(changed)
    row = SimpleNamespace(
        path="m.py", size_bytes=len(changed), sha256=hashlib.sha256(changed).hexdigest()
    )
    assert analyze_unused_imports(root, row).imports == []


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"  # 1
        "import os, subprocess, socket\n"  # 2
        "os.system('echo')\nsubprocess.run(['echo'])\n"  # 3, 4
        "socket.create_connection(('example.invalid', 80))\n"  # 5
        "def deco(f):\n    open('side-effect', 'w')\n    return f\n"  # 6-8
        "@deco\ndef target():\n    import json\n"  # 9-11
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
    result = analyze_unused_imports(root, rows[0])
    monkeypatch.undo()
    assert listed(result) == [(11, 12, "json")] and summary(result) == (1, 1, 0)
    assert not marker.exists()
    assert not (tmp_path / "side-effect").exists() and not Path("side-effect").exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


def test_responses_carry_no_source_or_internals(tmp_path: Path) -> None:
    marker = "UNIQUE-CONTENT-MARKER-62d4"
    source = IMPORT_FORMS + f"note = '{marker}'  # {marker}\n"
    result = unused(tmp_path, source)
    text = result.model_dump_json()
    assert marker not in text
    for word in ("import ", "from ", "gamma", "theta", "sigma"):
        assert word not in text
    assert hashlib.sha256(source.encode()).hexdigest() not in text
    body = result.model_dump()
    assert set(body) == {"path", "status", "imports", "summary", "truncated"}
    assert set(body["summary"]) == set(ZERO)
    for item in body["imports"]:
        assert set(item) == {"line", "column", "name"}


def test_rate_limit_constant() -> None:
    assert UNUSED_IMPORTS_LIMIT == Limit("unused-imports-user", 120, 600)


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
    root, rows = _repo(tmp_path, {"m.py": ORDERED, "notes.md": "import os\n"})
    monkeypatch.setattr(code_unused_imports, "repository_storage_dir", lambda settings, rid: root)
    return root, rows


async def _request(db: _Db, path: str) -> Any:
    session: Any = db  # the stand-in session
    repository: Any = SimpleNamespace(id=uuid.uuid4())
    return await code_unused_imports.code_unused_imports(session, repository, path)


async def test_request_reads_one_row_and_analyzes_the_stored_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    _, rows = storage
    db = _Db(rows[0])
    result = await _request(db, "m.py")
    assert listed(result) == [(1, 8, "zeta"), (3, 12, "alpha"), (4, 8, "beta")]
    assert len(db.statements) == 1 and read_spy == ["m.py"]
    sql = str(db.statements[0])
    assert "FROM files" in sql and "files.sha256" in sql and "files.path" in sql


async def test_request_never_reads_a_non_python_file(
    storage: tuple[Path, list[Any]], read_spy: list[str]
) -> None:
    row = SimpleNamespace(
        path="notes.md", language="Markdown", extension="md", size_bytes=10, sha256="x"
    )
    result = await _request(_Db(row), "notes.md")
    assert result.model_dump() == {
        "path": "notes.md",
        "status": "unsupported_language",
        "imports": [],
        "summary": ZERO,
        "truncated": False,
    }
    assert read_spy == []


async def test_request_for_an_unknown_file_is_404(read_spy: list[str]) -> None:
    with pytest.raises(FileContentError) as raised:
        await _request(_Db(None), "missing.py")
    assert (raised.value.status_code, raised.value.detail) == (404, FILE_NOT_FOUND["detail"])
    assert read_spy == []


# --- H. Oracles --------------------------------------------------------------------------------

ORACLE = (
    "import os\n"  # 1: read in f
    "import sys\n"  # 2: never read
    "from collections import OrderedDict, defaultdict\n"  # 3: defaultdict never read
    "from typing import TYPE_CHECKING\n"  # 4
    "if TYPE_CHECKING:\n"  # 5
    "    from pathlib import Path\n"  # 6: read in an annotation
    "\n"  # 7
    "def f(p: Path) -> str:\n"  # 8
    "    import json\n"  # 9: never read
    "    import re\n"  # 10: read in a closure
    "    def g():\n"  # 11
    "        return re.compile('x')\n"  # 12
    "    return os.sep + str(OrderedDict()) + str(g)\n"  # 13
    "\n"  # 14
    "class C:\n"  # 15
    "    import math\n"  # 16: a class body, never examined
    "    def m(self):\n"  # 17
    "        import textwrap\n"  # 18: never read
    "        return self\n"  # 19
)


def test_the_oracle_fixture(tmp_path: Path) -> None:
    result = unused(tmp_path, ORACLE)
    assert listed(result) == [
        (2, 8, "sys"),
        (3, 38, "defaultdict"),
        (9, 12, "json"),
        (18, 16, "textwrap"),
    ]
    assert summary(result) == (4, 4, 0)


def _free_in_a_child(table: Any, name: str) -> bool:
    for child in table.get_children():
        if name in child.get_identifiers() and child.lookup(name).is_free():
            return True
        if _free_in_a_child(child, name):
            return True
    return False


@pytest.mark.parametrize("source", [ORACLE, IMPORT_FORMS, ORDERED])
def test_cpython_symbol_table_agrees_with_every_listed_import(tmp_path: Path, source: str) -> None:
    """Every listed import is, for CPython, an imported name of its scope that is never referenced
    there and never free in a nested scope (ASCII fixtures: columns are bytes)."""
    top = symtable.symtable(source, "<fixture>", "exec")
    tables: dict[tuple[str, int], Any] = {}

    def walk(table: Any) -> None:
        for child in table.get_children():
            if child.get_type() == "function":
                tables[(child.get_name(), child.get_lineno())] = child
            walk(child)

    walk(top)
    functions = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    result = unused(tmp_path, source)
    assert result.imports
    for entry in result.imports:
        where = (entry.line, entry.column - 1)
        owners = [
            f
            for f in functions
            if (f.lineno, f.col_offset) <= where <= (f.end_lineno, f.end_col_offset)
        ]
        owner = max(owners, key=lambda f: (f.lineno, f.col_offset), default=None)
        table = top if owner is None else tables[(owner.name, owner.lineno)]
        symbol = table.lookup(entry.name)
        assert symbol.is_imported() and not symbol.is_referenced(), entry
        assert not _free_in_a_child(table, entry.name), entry


def _ast_oracle(source: str) -> list[str]:
    """Test-only, independent of the service: the bound names of every import outside class bodies
    that no Load name of the file mentions (fixtures whose names are all distinct)."""
    tree = ast.parse(source)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    loads = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__" and node.level == 0:
            continue
        owner = parents.get(id(node))
        while not isinstance(
            owner, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            owner = parents.get(id(owner))
        if isinstance(owner, ast.ClassDef):
            continue
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            if alias.name != "*" and alias.asname != alias.name and name not in loads:
                found.append(name)
    return sorted(found)


@pytest.mark.parametrize("source", [ORACLE, IMPORT_FORMS, ORDERED])
def test_an_independent_ast_walk_agrees(tmp_path: Path, source: str) -> None:
    assert sorted(name for _, _, name in listed(unused(tmp_path, source))) == _ast_oracle(source)


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
    "import sys\n"  # 2: never read
    "from typing import Any\n"  # 3: never read
    "def f():\n"  # 4
    "    import json\n"  # 5: never read
    "    return os.sep\n"  # 6
)
FILES: dict[str, bytes] = {
    "app/mod.py": API_SOURCE.encode(),
    "app/bad.py": b"def (:\n",
    "pkg/__init__.py": b"import os\n",
    "docs/notes.md": b"import os\n",
}
TARGET = {"path": "app/mod.py"}
API_RESULT = {
    "path": "app/mod.py",
    "status": "parsed",
    "imports": [
        {"line": 2, "column": 8, "name": "sys"},
        {"line": 3, "column": 20, "name": "Any"},
        {"line": 5, "column": 12, "name": "json"},
    ],
    "summary": {"unused": 3, "distinct": 3, "unsupported": 0},
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
        f"/repositories/{repository_id or owner.repository_id}/files/unused-imports"
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
async def test_unused_imports_endpoint(client: httpx.AsyncClient, alice: Owner) -> None:
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
        "imports": [],
        "summary": ZERO,
        "truncated": False,
    }
    assert read_spy == []  # a non-Python file is never read
    body = (await _get(client, alice, {"path": "app/bad.py"})).json()
    assert body == {
        "path": "app/bad.py",
        "status": "syntax_error",
        "imports": [],
        "summary": ZERO,
        "truncated": False,
    }
    body = (await _get(client, alice, {"path": "pkg/__init__.py"})).json()
    assert (body["status"], body["imports"], body["summary"]) == (
        "parsed",
        [],
        {"unused": 0, "distinct": 0, "unsupported": 1},
    )


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
    real = code_unused_imports.repository_storage_dir

    def swap_then_root(settings: Any, repository_id: Any) -> Path:
        root = real(settings, repository_id)
        (root / "app" / "mod.py").write_bytes(b"import os  # new snapshot\n")
        return root

    monkeypatch.setattr(code_unused_imports, "repository_storage_dir", swap_then_root)
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
    marker = "UNIQUE-CONTENT-MARKER-62e5"
    files = dict(FILES)
    files["app/mod.py"] = (API_SOURCE + f"note = '{marker}'  # {marker}\n").encode()
    await _ingest(client, alice, files)
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, TARGET)
    assert response.status_code == 200 and response.json()["status"] == "parsed"
    assert marker not in response.text and marker not in caplog.text
    for word in ("import ", "from ", "typing", "return", "def "):
        assert word not in response.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text
    body = response.json()
    assert set(body) == {"path", "status", "imports", "summary", "truncated"}
    assert set(body["summary"]) == {"unused", "distinct", "unsupported"}
    for item in body["imports"]:
        assert set(item) == {"line", "column", "name"}


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"b.py": b"import bobs_unused\n"})
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
    assert [i["name"] for i in own["imports"]] == ["bobs_unused"]


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
    bob = await _owner(client, "bob@example.com", {"b.py": b"import bobs_unused\n"})
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
    assert limiter.hits == [(UNUSED_IMPORTS_LIMIT, alice.user_id)]
    assert read_spy == []  # limited before any snapshot read


@requires_db_and_redis
async def test_rate_limit_is_120_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert UNUSED_IMPORTS_LIMIT == Limit("unused-imports-user", 120, 600)
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
        assert any(k.startswith("ratelimit:unused-imports-user:") for k in keys)
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
