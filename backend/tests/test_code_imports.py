"""Python import resolution tests (GET .../files/imports?path=...). Fully offline.

Resolver tests need no database. API tests run against the already-migrated throwaway test
database (TEST_DATABASE_URL, "_test") inside rolled-back transactions; ingestion runs
inline with the real worker logic and DATA_DIR is a temporary directory. The rate-limit
test uses a disposable Redis (TEST_REDIS_URL). Junction tests are Windows-only; symlink
tests are skipped where symlinks cannot be created.
"""

import builtins
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
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
from app.services import code_imports, ingestion
from app.services.code_imports import (
    IMPORTS_LIMIT,
    MAX_IMPORTS,
    MAX_INDEXED_PATHS,
    MAX_NAMES,
    MAX_TARGETS,
    MAX_TEXT_CHARS,
    analyze_imports,
    module_parts,
)
from app.services.code_outline import MAX_OUTLINE_BYTES
from app.services.file_content import MAX_CONTENT_BYTES
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
UNAVAILABLE = {"detail": "File content is not available; try again later"}

PATHS = sorted(
    [
        "backend/app/__init__.py",
        "backend/app/main.py",
        "backend/app/services/__init__.py",
        "backend/app/services/search.py",
        "backend/app/services/outline.py",
        "backend/app/models/user.py",  # a package without __init__.py
        "backend/tests/test_main.py",
        "tools/utils.py",
        "lib/utils.py",
        "stubs/typed.pyi",
        "web/xapp/main.py",
        "my-scripts/run.py",
        "__init__.py",
    ]
)
IMPORTER = "backend/app/main.py"


def _entries(source: str, importer: str = IMPORTER, paths: list[str] | None = None) -> list[Any]:
    status, imports, _ = analyze_imports(source, importer, PATHS if paths is None else paths)
    assert status == "parsed"
    return imports


def _one(source: str, importer: str = IMPORTER) -> Any:
    (entry,) = _entries(source, importer)
    return entry


def _summary(entry: Any) -> tuple[Any, ...]:
    return (entry.resolution, entry.targets)


# --- Module index (no database) --------------------------------------------------------------


def test_module_parts() -> None:
    assert module_parts("backend/app/main.py") == ("backend", "app", "main")
    assert module_parts("backend/app/__init__.py") == ("backend", "app")
    assert module_parts("stubs/typed.pyi") == ("stubs", "typed")
    assert module_parts("pkg/__init__.pyi") == ("pkg",)
    assert module_parts("SCRIPT.PY") == ("SCRIPT",)
    assert module_parts("__init__.py") == ()


# --- Absolute imports ------------------------------------------------------------------------


def test_import_module_and_dotted_module() -> None:
    assert _summary(_one("import main\n")) == (
        "ambiguous",
        ["backend/app/main.py", "web/xapp/main.py"],
    )
    assert _summary(_one("import app.main\n")) == ("resolved", ["backend/app/main.py"])
    assert _summary(_one("import backend.app.main\n")) == ("resolved", ["backend/app/main.py"])
    entry = _one("import app.services.search as s\n")
    assert (entry.statement, entry.module, entry.level, entry.names) == (
        "import",
        None,
        0,
        ["app.services.search"],
    )
    assert _summary(entry) == ("resolved", ["backend/app/services/search.py"])


def test_whole_segment_matching_only() -> None:
    # "app.main" must never match web/xapp/main.py, and parts are never substring-matched.
    assert _one("import app.main\n").targets == ["backend/app/main.py"]
    assert _summary(_one("import pp.main\n")) == ("not_found", [])
    assert _summary(_one("import ain\n")) == ("not_found", [])
    assert _summary(_one("import xapp.main\n")) == ("resolved", ["web/xapp/main.py"])
    # A part that is not an identifier is never split or used: only "run" is importable.
    assert _summary(_one("import run\n")) == ("resolved", ["my-scripts/run.py"])
    assert _summary(_one("import scripts.run\n")) == ("not_found", [])


def test_packages_stubs_and_namespace_like_directories() -> None:
    assert _summary(_one("import app\n")) == ("resolved", ["backend/app/__init__.py"])
    assert _summary(_one("import app.services\n")) == (
        "resolved",
        ["backend/app/services/__init__.py"],
    )
    assert _summary(_one("import stubs.typed\n")) == ("resolved", ["stubs/typed.pyi"])
    assert _summary(_one("import app.models.user\n")) == (
        "resolved",
        ["backend/app/models/user.py"],
    )
    assert _summary(_one("import app.models\n")) == ("not_found", [])  # no __init__.py


def test_from_import_with_submodules_and_symbols() -> None:
    entry = _one("from app.services import search, outline, helper\n")
    assert (entry.statement, entry.module, entry.level, entry.names) == (
        "from",
        "app.services",
        0,
        ["search", "outline", "helper"],
    )
    # The package plus the names that are modules; "helper" is a symbol, not a file.
    assert _summary(entry) == (
        "resolved",
        [
            "backend/app/services/__init__.py",
            "backend/app/services/outline.py",
            "backend/app/services/search.py",
        ],
    )
    assert _summary(_one("from app.services.search import run\n")) == (
        "resolved",
        ["backend/app/services/search.py"],
    )
    star = _one("from app.services import *\n")
    assert (star.names, _summary(star)) == (
        ["*"],
        ("resolved", ["backend/app/services/__init__.py"]),
    )


def test_ambiguity_is_per_module_and_never_picks_one() -> None:
    entry = _one("import utils\n")
    assert _summary(entry) == ("ambiguous", ["lib/utils.py", "tools/utils.py"])
    # Two different modules, each matching exactly one file, are not ambiguous.
    assert _summary(_one("import app.main, stubs.typed\n")) == (
        "resolved",
        ["backend/app/main.py", "stubs/typed.pyi"],
    )
    # One ambiguous module makes the statement ambiguous.
    assert _one("import app.main, utils\n").resolution == "ambiguous"


def test_targets_are_sorted_and_capped_at_five() -> None:
    assert MAX_TARGETS == 5
    paths = sorted(f"pkg{i}/common.py" for i in range(7))
    status, imports, truncated = analyze_imports("import common\n", "main.py", paths)
    assert status == "parsed"
    assert imports[0].targets == [f"pkg{i}/common.py" for i in range(5)]
    assert (imports[0].resolution, truncated) == ("ambiguous", True)
    status, imports, truncated = analyze_imports("import common\n", "main.py", paths[:5])
    assert (len(imports[0].targets), truncated) == (5, False)


def test_standard_and_third_party_modules_are_not_found() -> None:
    for source in (
        "import os\n",
        "import os.path\n",
        "from requests import get\n",
        "import __future__\n",
    ):
        assert _summary(_one(source)) == ("not_found", [])


# --- Relative imports ------------------------------------------------------------------------


def test_relative_imports_from_a_module() -> None:
    importer = "backend/app/services/search.py"  # package: backend/app/services
    assert _summary(_one("from . import outline\n", importer)) == (
        "resolved",
        ["backend/app/services/outline.py"],
    )
    assert _summary(_one("from .outline import build\n", importer)) == (
        "resolved",
        ["backend/app/services/outline.py"],
    )
    assert _summary(_one("from .. import main\n", importer)) == (
        "resolved",
        ["backend/app/main.py"],
    )
    assert _summary(_one("from ..models import user\n", importer)) == (
        "resolved",
        ["backend/app/models/user.py"],
    )
    assert _summary(_one("from ..services import search\n", importer)) == (
        "resolved",
        ["backend/app/services/__init__.py", "backend/app/services/search.py"],
    )
    entry = _one("from ...tests import test_main\n", importer)
    assert (entry.module, entry.level, _summary(entry)) == (
        "tests",
        3,
        ("resolved", ["backend/tests/test_main.py"]),
    )
    # Relative imports use exact parts: the other utils.py files are never considered.
    assert _summary(_one("from . import utils\n", importer)) == ("not_found", [])


def test_relative_imports_from_a_package_init() -> None:
    importer = "backend/app/services/__init__.py"  # its own package: backend/app/services
    assert _summary(_one("from .search import x\n", importer)) == (
        "resolved",
        ["backend/app/services/search.py"],
    )
    assert _summary(_one("from .. import main\n", importer)) == (
        "resolved",
        ["backend/app/main.py"],
    )


def test_relative_imports_cannot_climb_above_the_root() -> None:
    importer = "backend/app/main.py"  # package: backend/app (2 parts)
    assert _summary(_one("from ... import utils\n", importer)) == ("not_found", [])  # root
    entry = _one("from .... import anything\n", importer)
    assert (entry.level, _summary(entry)) == (4, ("not_found", []))
    assert _summary(_one("from . import x\n", "top.py")) == ("not_found", [])
    assert _summary(_one("from .. import x\n", "top.py")) == ("not_found", [])


# --- Statements, order and limits ------------------------------------------------------------


def test_all_imports_including_nested_ones_in_source_order() -> None:
    source = (
        "from __future__ import annotations\n"
        "import os, sys\n"
        "if TYPE_CHECKING:\n"
        "    from app.services import search\n"
        "try:\n"
        "    import ujson as json\n"
        "except ImportError:\n"
        "    import json\n"
        "def lazy():\n"
        "    from app import main\n"
        "    class Inner:\n"
        "        import stubs.typed\n"
        "x = 1; import utils\n"
    )
    entries = _entries(source)
    assert [(e.line, e.statement, e.module, e.names) for e in entries] == [
        (1, "from", "__future__", ["annotations"]),
        (2, "import", None, ["os", "sys"]),
        (4, "from", "app.services", ["search"]),
        (6, "import", None, ["ujson"]),
        (8, "import", None, ["json"]),
        (10, "from", "app", ["main"]),
        (12, "import", None, ["stubs.typed"]),
        (13, "import", None, ["utils"]),
    ]
    assert entries[5].targets == ["backend/app/__init__.py", "backend/app/main.py"]


def test_names_keep_source_order() -> None:
    assert _one("from app.services import zeta, alpha, mid\n").names == ["zeta", "alpha", "mid"]


def test_import_limit() -> None:
    assert MAX_IMPORTS == 500
    source = "".join(f"import m{i}\n" for i in range(MAX_IMPORTS + 1))
    status, imports, truncated = analyze_imports(source, IMPORTER, PATHS)
    assert (status, len(imports), truncated) == ("parsed", MAX_IMPORTS, True)
    assert imports[-1].names == [f"m{MAX_IMPORTS - 1}"]
    exactly = "".join(f"import m{i}\n" for i in range(MAX_IMPORTS))
    assert analyze_imports(exactly, IMPORTER, PATHS)[2] is False


def test_name_limit() -> None:
    assert MAX_NAMES == 50
    names = ", ".join(f"n{i}" for i in range(MAX_NAMES + 1))
    status, imports, truncated = analyze_imports(f"from app import {names}\n", IMPORTER, PATHS)
    assert (len(imports[0].names), imports[0].names[-1], truncated) == (MAX_NAMES, "n49", True)
    exactly = ", ".join(f"n{i}" for i in range(MAX_NAMES))
    assert analyze_imports(f"from app import {exactly}\n", IMPORTER, PATHS)[2] is False


def test_long_strings_are_capped() -> None:
    assert MAX_TEXT_CHARS == 200
    long = "a" * 300
    status, imports, truncated = analyze_imports(f"from {long}.b import {long}\n", IMPORTER, PATHS)
    assert imports[0].module == f"{long}.b"[:MAX_TEXT_CHARS]
    assert imports[0].names == [long[:MAX_TEXT_CHARS]]
    assert truncated is True
    assert analyze_imports("from " + "a" * 200 + " import b\n", IMPORTER, PATHS)[2] is False


def test_path_index_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_INDEXED_PATHS == 10_000
    monkeypatch.setattr(code_imports, "MAX_INDEXED_PATHS", 2)
    paths = ["a.py", "b.py", "c.py"]
    status, imports, truncated = analyze_imports("import a\nimport c\n", "x.py", paths)
    assert [(e.names[0], e.resolution) for e in imports] == [("a", "resolved"), ("c", "not_found")]
    assert truncated is True
    assert analyze_imports("import a\n", "x.py", paths[:2])[2] is False


# --- Parsing ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import (:\n",
        "import os\x00\n",
        "(" * 201 + ")" * 201,
        "x = " + "+".join(["1"] * 100_000),
        "".join("    " * i + f"def f{i}():\n" for i in range(101)) + "    " * 101 + "pass\n",
    ],
    ids=["syntax", "null-byte", "nested-parens", "long-chain", "deep-indent"],
)
def test_invalid_and_hostile_sources_are_syntax_errors(source: str) -> None:
    started = time.perf_counter()
    assert analyze_imports(source, IMPORTER, PATHS) == ("syntax_error", [], False)
    assert time.perf_counter() - started < 5.0


def test_empty_source() -> None:
    assert analyze_imports("", IMPORTER, PATHS) == ("parsed", [], False)
    assert analyze_imports("x = 1\n", IMPORTER, PATHS) == ("parsed", [], False)


def test_imports_are_data_and_nothing_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    source = (
        "import os, subprocess\n"
        "from os import system\n"
        f"open({str(marker)!r}, 'w').write('x')\n"
        "__import__('subprocess').run(['echo'])\n"
        "importlib = __import__('importlib')\n"
        "os.system('echo pwned')\n"
    )

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("repository code must never run")

    for target, name in (
        (builtins, "exec"),
        (builtins, "eval"),
        (os, "system"),
        (subprocess, "Popen"),
        (socket.socket, "connect"),
    ):
        monkeypatch.setattr(target, name, refuse)
    imports = _entries(source)
    monkeypatch.undo()
    assert [(e.statement, e.module, e.names) for e in imports] == [
        ("import", None, ["os", "subprocess"]),
        ("from", "os", ["system"]),
    ]
    assert not marker.exists()


# --- API fixtures ----------------------------------------------------------------------------

MAIN = b"""from __future__ import annotations

import os
from app.services import search, helper
from .services.outline import build

if TYPE_CHECKING:
    import utils


def run():
    from app import main
    return main
"""

FILES: dict[str, bytes] = {
    "backend/app/__init__.py": b"",
    "backend/app/main.py": MAIN,
    "backend/app/services/__init__.py": b"",
    "backend/app/services/search.py": b"def search_code(): ...\n",
    "backend/app/services/outline.py": b"def build(): ...\n",
    "tools/utils.py": b"",
    "lib/utils.py": b"",
    "bad.py": b"import (:\n",
    "empty.py": b"",
    "bin/blob.py": b"\x00\x01 import os",
    "bin/latin1.py": b"\xff\xfe import os",
    "README.md": b"import os\n",
}

MAIN_IMPORTS = [
    {
        "line": 1,
        "statement": "from",
        "module": "__future__",
        "level": 0,
        "names": ["annotations"],
        "targets": [],
        "resolution": "not_found",
    },
    {
        "line": 3,
        "statement": "import",
        "module": None,
        "level": 0,
        "names": ["os"],
        "targets": [],
        "resolution": "not_found",
    },
    {
        "line": 4,
        "statement": "from",
        "module": "app.services",
        "level": 0,
        "names": ["search", "helper"],
        "targets": ["backend/app/services/__init__.py", "backend/app/services/search.py"],
        "resolution": "resolved",
    },
    {
        "line": 5,
        "statement": "from",
        "module": "services.outline",
        "level": 1,
        "names": ["build"],
        "targets": ["backend/app/services/outline.py"],
        "resolution": "resolved",
    },
    {
        "line": 8,
        "statement": "import",
        "module": None,
        "level": 0,
        "names": ["utils"],
        "targets": ["lib/utils.py", "tools/utils.py"],
        "resolution": "ambiguous",
    },
    {
        "line": 12,
        "statement": "from",
        "module": "app",
        "level": 0,
        "names": ["main"],
        "targets": ["backend/app/__init__.py", "backend/app/main.py"],
        "resolution": "resolved",
    },
]


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
        f"/repositories/{repository_id or owner.repository_id}/files/imports"
    )


async def _get(
    client: httpx.AsyncClient, owner: Owner, path: str | None, **kw: Any
) -> httpx.Response:
    params = {} if path is None else {"path": path}
    url = _url(owner, kw.pop("project_id", None), kw.pop("repository_id", None))
    return await client.get(url, params=params, headers=kw.pop("headers", owner.headers))


async def _imports(client: httpx.AsyncClient, owner: Owner, path: str) -> dict[str, Any]:
    response = await _get(client, owner, path)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_imports._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_imports, "_read_verified", spy)
    return reads


@pytest.fixture
def parse_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    parsed: list[int] = []
    real = code_imports.analyze_imports

    def spy(source: str, importer: str, paths: Any) -> Any:
        parsed.append(len(source))
        return real(source, importer, paths)

    monkeypatch.setattr(code_imports, "analyze_imports", spy)
    return parsed


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


def _empty(path: str, status: str) -> dict[str, Any]:
    return {"path": path, "status": status, "imports": [], "truncated": False}


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_imports_of_a_python_file(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    body = await _imports(client, alice, "backend/app/main.py")
    assert body == {
        "path": "backend/app/main.py",
        "status": "parsed",
        "imports": MAIN_IMPORTS,
        "truncated": False,
    }
    assert read_spy == ["backend/app/main.py"]  # targets are matched by stored paths only


@requires_db
async def test_unparsed_statuses(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str], parse_spy: list[int]
) -> None:
    assert await _imports(client, alice, "bad.py") == _empty("bad.py", "syntax_error")
    assert await _imports(client, alice, "empty.py") == _empty("empty.py", "parsed")
    for path in ("bin/blob.py", "bin/latin1.py"):
        assert await _imports(client, alice, path) == _empty(path, "binary")
    read_spy.clear()
    assert await _imports(client, alice, "README.md") == _empty("README.md", "unsupported_language")
    assert read_spy == []  # an unsupported file is never read
    assert parse_spy == [len("import (:\n"), 0]  # bad.py and empty.py only


@requires_db
async def test_text_size_limits(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str], parse_spy: list[int]
) -> None:
    assert MAX_OUTLINE_BYTES == 256 * 1024
    # Random names keep the upload under the archive compression-ratio limit.
    lines = [f"import m{os.urandom(8).hex()}\n".encode() for _ in range(MAX_OUTLINE_BYTES // 25)]
    exactly = b"".join(lines) + b"#" * (MAX_OUTLINE_BYTES % 25)
    assert len(exactly) == MAX_OUTLINE_BYTES
    await _ingest(
        client,
        alice,
        {
            "exact.py": exactly,
            "over.py": exactly + b"\n",
            "huge.py": os.urandom(MAX_CONTENT_BYTES + 1),
            "limit.py": os.urandom(MAX_CONTENT_BYTES),
        },
    )
    assert await _imports(client, alice, "over.py") == _empty("over.py", "too_large")
    assert parse_spy == []  # over the parse cap: never parsed
    assert await _imports(client, alice, "huge.py") == _empty("huge.py", "too_large")
    assert "huge.py" not in read_spy  # over 1 MiB: never opened
    assert (await _imports(client, alice, "limit.py"))["status"] == "binary"  # exactly 1 MiB: read
    assert "limit.py" in read_spy
    exact = await _imports(client, alice, "exact.py")
    assert exact["status"] == "parsed"
    assert (len(exact["imports"]), exact["truncated"]) == (MAX_IMPORTS, True)
    assert parse_spy == [MAX_OUTLINE_BYTES]


@requires_db
async def test_unknown_and_unavailable_files(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    for path in (
        "missing.py",
        "../etc/passwd",
        "backend/app/../app/main.py",
        "/backend/app/main.py",
        "BACKEND/APP/MAIN.PY",
    ):
        response = await _get(client, alice, path)
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)

    root = _storage(alice)
    (root / "bad.py").write_bytes(b"import tampered\n")  # hash mismatch
    (root / "empty.py").unlink()  # missing
    outside = tmp_path / "outside"
    shutil.copytree(root / "backend", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "backend")
    _junction(root / "backend", outside)
    for path in ("bad.py", "empty.py", "backend/app/main.py"):
        response = await _get(client, alice, path)
        assert (response.status_code, response.json()) == (409, UNAVAILABLE)
        assert "tampered" not in response.text


@requires_db
async def test_symlinked_file_is_unavailable(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    stored = _storage(alice) / "backend" / "app" / "main.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(MAIN)
    stored.unlink()
    try:
        os.symlink(outside, stored)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks cannot be created here: {exc}")
    response = await _get(client, alice, "backend/app/main.py")
    assert (response.status_code, response.json()) == (409, UNAVAILABLE)


@requires_db
async def test_validation(client: httpx.AsyncClient, alice: Owner, read_spy: list[str]) -> None:
    assert (await _get(client, alice, None)).status_code == 422
    assert (await _get(client, alice, "")).status_code == 422
    assert (await _get(client, alice, "a" * 4097)).status_code == 422
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _get(client, alice, "backend/app/main.py", **kwargs)).status_code == 422
    assert read_spy == []


@requires_db
async def test_snapshot_change_before_the_read_returns_409(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the snapshot query, storage is swapped to new content: never mixed results."""
    real = code_imports._read_verified

    def swap_then_read(root: Path, relative: str, expected: str | None) -> bytes:
        (root / "backend" / "app" / "main.py").write_bytes(b"import lib.utils\n")
        (root / "new_module.py").write_bytes(b"")
        return real(root, relative, expected)

    monkeypatch.setattr(code_imports, "_read_verified", swap_then_read)
    response = await _get(client, alice, "backend/app/main.py")
    assert (response.status_code, response.json()) == (409, UNAVAILABLE)


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
        body = await _imports(client, alice, "backend/app/main.py")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert body["imports"] == MAIN_IMPORTS
    files_queries = [s for s in statements if "FROM files" in s]
    assert len(files_queries) == 1, files_queries


@requires_db
async def test_reingestion_changes_the_targets(client: httpx.AsyncClient, alice: Owner) -> None:
    files = dict(FILES)
    del files["backend/app/services/search.py"]
    files["backend/app/services/extra.py"] = b""
    files["backend/app/main.py"] = b"from app.services import search, extra\n"
    await _ingest(client, alice, files)
    body = await _imports(client, alice, "backend/app/main.py")
    assert body["imports"][0]["targets"] == [
        "backend/app/services/__init__.py",
        "backend/app/services/extra.py",
    ]


@requires_db
async def test_malicious_imports_are_only_data(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch, read_spy: list[str]
) -> None:
    evil = (
        b"import os, subprocess, socket\n"
        b"__import__('os').system('echo pwned')\n"
        b"from importlib import import_module\n"
    )
    await _ingest(client, alice, {**FILES, "evil.py": evil})
    read_spy.clear()

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("repository code must never run")

    for target, name in (
        (builtins, "exec"),
        (builtins, "eval"),
        (os, "system"),
        (subprocess, "Popen"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
    ):
        monkeypatch.setattr(target, name, refuse)
    body = await _imports(client, alice, "evil.py")
    assert [(e["statement"], e["module"], e["names"]) for e in body["imports"]] == [
        ("import", None, ["os", "subprocess", "socket"]),
        ("from", "importlib", ["import_module"]),
    ]
    assert read_spy == ["evil.py"]


@requires_db
async def test_no_internals_or_content_are_exposed_or_logged(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-5b1e"
    await _ingest(
        client, alice, {**FILES, "m.py": f"SECRET = '{marker}'\nimport app.main\n".encode()}
    )
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, "m.py")
    assert response.status_code == 200
    assert response.json()["imports"][0]["targets"] == ["backend/app/main.py"]
    assert marker not in response.text and marker not in caplog.text
    assert "app.main" not in caplog.text
    rows = (await db_session.execute(select(File.id, File.sha256))).all()
    for file_id, sha in rows:
        assert str(file_id) not in response.text and sha not in response.text
    assert alice.repository_id not in response.text
    assert str(get_settings().data_dir) not in response.text
    assert str(_storage(alice)) not in response.text


@requires_db
async def test_ownership_and_authentication(client: httpx.AsyncClient, alice: Owner) -> None:
    bob = await _owner(client, "bob@example.com", {"bob.py": b"import os\n"})
    other_project = (
        await client.post("/api/v1/projects", json={"name": "Q"}, headers=alice.headers)
    ).json()["id"]
    for path, kwargs in (
        ("bob.py", {"project_id": bob.project_id, "repository_id": bob.repository_id}),
        ("backend/app/main.py", {"project_id": other_project}),
        ("backend/app/main.py", {"repository_id": str(uuid.uuid4())}),
    ):
        response = await _get(client, alice, path, **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    unauthenticated = await client.get(_url(alice), params={"path": "backend/app/main.py"})
    assert unauthenticated.status_code == 401
    bad = await _get(client, alice, "backend/app/main.py", headers={"Authorization": "Bearer nope"})
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
    bob = await _owner(client, "bob@example.com", {"bob.py": b"import os\n"})
    limiter = _AlwaysLimited()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    for kwargs in (
        {"project_id": bob.project_id, "repository_id": bob.repository_id},
        {"repository_id": str(uuid.uuid4())},
    ):
        response = await _get(client, alice, "bob.py", **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert limiter.hits == []

    limited = await _get(client, alice, "backend/app/main.py")
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts; please try again later"}
    assert limiter.hits == [(IMPORTS_LIMIT, alice.user_id)]
    assert read_spy == []


@requires_db_and_redis
async def test_rate_limit_is_120_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert IMPORTS_LIMIT == Limit("imports-user", 120, 600)
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(settings)
    await pool.flushdb()
    app.dependency_overrides.pop(get_rate_limiter, None)  # use the real Step 17 limiter
    try:
        for _ in range(120):
            assert (await _get(client, alice, "README.md")).status_code == 200
        limited = await _get(client, alice, "README.md")
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts; please try again later"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 600
        keys = [k.decode() if isinstance(k, bytes) else k for k in await pool.keys("*")]
        assert any(k.startswith("ratelimit:imports-user:") for k in keys)
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
    for _ in range(122):  # beyond the limit
        assert (await _get(client, alice, "README.md")).status_code == 200
