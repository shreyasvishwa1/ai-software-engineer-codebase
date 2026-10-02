"""Python reverse import tests (GET .../files/importers?path=...). Fully offline.

Scanner tests use a temporary storage directory and need no database. API tests run
against the already-migrated throwaway test database (TEST_DATABASE_URL, "_test") inside
rolled-back transactions; ingestion runs inline with the real worker logic and DATA_DIR is
a temporary directory. The rate-limit test uses a disposable Redis (TEST_REDIS_URL).
Junction tests are Windows-only.
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
import time
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
from app.services import code_importers, ingestion
from app.services.code_importers import (
    IMPORTERS_LIMIT,
    MAX_IMPORTERS,
    MAX_IMPORTS_PER_IMPORTER,
    MAX_PARSED_BYTES,
    MAX_SCANNED_BYTES,
    REPOSITORY_CHANGED,
    match_imports,
    scan_importers,
)
from app.services.code_imports import MAX_TARGETS, ModuleIndex, analyze_imports
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

# A small repository: (path -> source). Every .py/.pyi file is part of the module index.
REPO: dict[str, str] = {
    "app/__init__.py": "",
    "app/main.py": (
        "import os\n"
        "from app.services import auth, search\n"
        "def lazy():\n"
        "    from app.services.auth import login\n"
        "import utils\n"
    ),
    "app/services/__init__.py": "from . import auth\n",
    "app/services/auth.py": "import app.services.auth\n",  # a self-import (never read)
    "app/services/search.py": "from .auth import login\nfrom ..models import user\n",
    "app/models/user.py": "if TYPE_CHECKING:\n    from app.services import auth\n",
    "app/api/routes.py": "try:\n    import services.auth as a\nexcept ImportError:\n    pass\n",
    "web/xapp/auth.py": "",
    "web/client.py": "import xapp.auth\nimport app.servicesauth\nimport pp.services.auth\n",
    "tools/utils.py": "",
    "lib/utils.py": "",
    "stubs/typed.pyi": "from app.services import auth as typed_auth\n",
    "my-scripts/run.py": "from . import helper\n",
    "my-scripts/helper.py": "",
    "docs/notes.md": "from app.services import auth\n",
    "__init__.py": "",
}


def _rows(root: Path, files: dict[str, str | bytes]) -> list[Any]:
    """Write ``files`` below ``root`` and return snapshot-like rows for the Python ones."""
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


def _scan(tmp_path: Path, target: str, files: dict[str, str | bytes] | None = None) -> Any:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    rows = _rows(root, REPO if files is None else files)
    return scan_importers(root, rows, target)


def _paths(importers: list[Any]) -> list[str]:
    return [importer.path for importer in importers]


@pytest.fixture
def read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = code_importers._read_verified

    def spy(root: Path, relative: str, expected: str | None) -> bytes:
        reads.append(relative)
        return real(root, relative, expected)

    monkeypatch.setattr(code_importers, "_read_verified", spy)
    return reads


@pytest.fixture
def parse_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    parsed: list[str] = []
    real = code_importers.match_imports

    def spy(source: str, importer: str, target: str, index: Any) -> Any:
        parsed.append(importer)
        return real(source, importer, target, index)

    monkeypatch.setattr(code_importers, "match_imports", spy)
    return parsed


# --- Matching (no database) ------------------------------------------------------------------


def test_importers_of_a_module(tmp_path: Path) -> None:
    importers, summary, truncated = _scan(tmp_path, "app/services/auth.py")
    assert _paths(importers) == [
        "app/api/routes.py",
        "app/main.py",
        "app/models/user.py",
        "app/services/__init__.py",
        "app/services/search.py",
        "stubs/typed.pyi",
    ]
    by_path = {i.path: i.imports for i in importers}
    assert [
        (s.line, s.statement, s.module, s.level, s.names, s.resolution)
        for s in by_path["app/main.py"]
    ] == [
        (2, "from", "app.services", 0, ["auth", "search"], "resolved"),
        (4, "from", "app.services.auth", 0, ["login"], "resolved"),
    ]
    assert [(s.line, s.module, s.level, s.names) for s in by_path["app/services/__init__.py"]] == [
        (1, None, 1, ["auth"])
    ]
    assert [(s.line, s.module, s.level) for s in by_path["app/services/search.py"]] == [
        (1, "auth", 1)
    ]
    assert [(s.line, s.names) for s in by_path["app/models/user.py"]] == [(2, ["auth"])]
    assert [(s.line, s.statement, s.names) for s in by_path["app/api/routes.py"]] == [
        (2, "import", ["services.auth"])
    ]
    assert truncated is False
    assert summary.syntax_error == 0


def test_whole_segment_matching_only(tmp_path: Path) -> None:
    # web/client.py imports xapp.auth, app.servicesauth and pp.services.auth: none of these
    # is app/services/auth.py, and only the first is web/xapp/auth.py.
    importers, _, _ = _scan(tmp_path, "app/services/auth.py")
    assert "web/client.py" not in _paths(importers)
    importers, _, _ = _scan(tmp_path, "web/xapp/auth.py")
    assert _paths(importers) == ["web/client.py"]
    assert [s.names for s in importers[0].imports] == [["xapp.auth"]]


def test_packages_relative_imports_and_invalid_directory_names(tmp_path: Path) -> None:
    importers, _, _ = _scan(tmp_path, "app/services/__init__.py")
    # "from app.services import ..." always resolves the package itself.
    assert _paths(importers) == ["app/main.py", "app/models/user.py", "stubs/typed.pyi"]
    importers, _, _ = _scan(tmp_path, "app/models/user.py")
    assert _paths(importers) == ["app/services/search.py"]  # from ..models import user
    importers, _, _ = _scan(tmp_path, "my-scripts/helper.py")
    assert _paths(importers) == ["my-scripts/run.py"]  # exact relative parts
    assert _scan(tmp_path, "__init__.py")[0] == []  # a root __init__ is never a module


def test_ambiguous_modules_list_the_importer_for_every_match(tmp_path: Path) -> None:
    for target in ("lib/utils.py", "tools/utils.py"):
        importers, _, _ = _scan(tmp_path, target)
        assert _paths(importers) == ["app/main.py"]
        assert [(s.line, s.names, s.resolution) for s in importers[0].imports] == [
            (5, ["utils"], "ambiguous")
        ]


def test_more_than_five_targets_use_the_full_match_set(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {f"pkg{i}/common.py": "" for i in range(7)}
    files["main.py"] = "import common\n"
    status, forward, truncated = analyze_imports("import common\n", "main.py", sorted(files))
    assert len(forward[0].targets) == MAX_TARGETS and truncated is True  # forward display cap
    for i in range(7):
        importers, _, cut = _scan(tmp_path, f"pkg{i}/common.py", files)
        assert _paths(importers) == ["main.py"], i
        assert importers[0].imports[0].resolution == "ambiguous"
        assert cut is False


def test_one_entry_per_importer_with_every_matching_statement_in_order(tmp_path: Path) -> None:
    files: dict[str, str | bytes] = {
        "a/target.py": "",
        "b/user.py": "import a.target\nx = 1\nfrom a import target\nimport a.target as again\n",
    }
    importers, _, _ = _scan(tmp_path, "a/target.py", files)
    assert _paths(importers) == ["b/user.py"]
    assert [s.line for s in importers[0].imports] == [1, 3, 4]


def test_the_target_itself_is_never_read(tmp_path: Path, read_spy: list[str]) -> None:
    importers, _, _ = _scan(tmp_path, "app/services/auth.py")
    assert "app/services/auth.py" not in _paths(importers)  # its self-import is not reported
    assert "app/services/auth.py" not in read_spy


def test_agreement_with_forward_resolution(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rows = _rows(root, REPO)
    paths = [row.path for row in rows]
    sources = {path: REPO[path] for path in paths}
    for target in paths:
        importers, _, _ = scan_importers(root, rows, target)
        expected = []
        for importer in paths:
            if importer == target:
                continue
            status, forward, _ = analyze_imports(sources[importer], importer, paths)
            assert all(len(entry.targets) < MAX_TARGETS for entry in forward)  # uncapped here
            if any(target in entry.targets for entry in forward):
                expected.append(importer)
        assert _paths(importers) == expected, target


def test_names_in_source_order_and_long_strings_capped(tmp_path: Path) -> None:
    long = "x" * 300
    files: dict[str, str | bytes] = {
        "a/target.py": "",
        "b/user.py": f"from a import {long}, target, alpha\nfrom a.target import {long}\n",
    }
    importers, _, truncated = _scan(tmp_path, "a/target.py", files)
    first, second = importers[0].imports
    assert first.names == ["x" * 200, "target", "alpha"]
    assert second.module == "a.target" and second.names == ["x" * 200]
    assert truncated is True


# --- Per-file outcomes and limits ------------------------------------------------------------


def test_per_file_outcomes(tmp_path: Path, read_spy: list[str], parse_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "a/target.py": "",
        "b/binary.py": b"\x00\x01 import a.target",
        "b/latin1.py": b"\xff\xfe import a.target",
        "b/huge.py": b"import a.target\n" + b"#" * MAX_CONTENT_BYTES,
        "b/big.py": b"import a.target\n" + b"#" * MAX_OUTLINE_BYTES,
        "b/bad.py": b"import a.target (\n",
        "b/unrelated.py": b"import os\n",
        "b/user.py": b"import a.target\n",
    }
    importers, summary, truncated = _scan(tmp_path, "a/target.py", files)
    assert _paths(importers) == ["b/user.py"]
    assert summary.model_dump() == {
        "files_scanned": 6,
        "too_large": 2,
        "binary": 2,
        "syntax_error": 1,
    }
    assert "b/huge.py" not in read_spy  # over 1 MiB: never opened
    assert sorted(parse_spy) == ["b/bad.py", "b/user.py"]  # big, binary, unrelated: not parsed
    assert truncated is False


def test_prefilter_keeps_non_ascii_files(tmp_path: Path, parse_spy: list[str]) -> None:
    files: dict[str, str | bytes] = {
        "a/auth.py": "",
        "b/plain.py": "import os\n",
        "b/unicode.py": "# café\nimport os\n",
        "b/nfkc.py": "from a import ａｕｔｈ\n",  # Python normalizes identifiers (NFKC)
    }
    importers, _, _ = _scan(tmp_path, "a/auth.py", files)
    assert _paths(importers) == ["b/nfkc.py"]
    assert importers[0].imports[0].names == ["auth"]
    assert sorted(parse_spy) == ["b/nfkc.py", "b/unicode.py"]


def test_text_size_boundary(tmp_path: Path, parse_spy: list[str]) -> None:
    line = b"import a.target\n"
    exactly = line + b"#" * (MAX_OUTLINE_BYTES - len(line))
    files: dict[str, str | bytes] = {
        "a/target.py": "",
        "b/exact.py": exactly,
        "b/over.py": exactly + b"#",
    }
    importers, summary, _ = _scan(tmp_path, "a/target.py", files)
    assert _paths(importers) == ["b/exact.py"]
    assert summary.too_large == 1
    assert parse_spy == ["b/exact.py"]


def test_read_budget_is_checked_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_spy: list[str]
) -> None:
    assert MAX_SCANNED_BYTES == 64 * 1024 * 1024
    files: dict[str, str | bytes] = {
        "a/target.py": "",
        "b/one.py": "import a.target\n".ljust(40),
        "b/two.py": "import a.target\n".ljust(40),
    }
    monkeypatch.setattr(code_importers, "MAX_SCANNED_BYTES", 60)
    importers, summary, truncated = _scan(tmp_path, "a/target.py", files)
    assert (_paths(importers), summary.files_scanned, truncated) == (["b/one.py"], 1, True)
    assert read_spy == ["b/one.py"]
    read_spy.clear()
    monkeypatch.setattr(code_importers, "MAX_SCANNED_BYTES", 80)  # exactly enough
    importers, _, truncated = _scan(tmp_path, "a/target.py", files)
    assert (_paths(importers), truncated) == (["b/one.py", "b/two.py"], False)


def test_parse_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_PARSED_BYTES == 4 * 1024 * 1024
    source = "import a.target\n"
    files: dict[str, str | bytes] = {"a/target.py": "", "b/one.py": source, "b/two.py": source}
    monkeypatch.setattr(code_importers, "MAX_PARSED_BYTES", len(source) * 2 - 1)
    importers, _, truncated = _scan(tmp_path, "a/target.py", files)
    assert (_paths(importers), truncated) == (["b/one.py"], True)
    monkeypatch.setattr(code_importers, "MAX_PARSED_BYTES", len(source) * 2)
    importers, _, truncated = _scan(tmp_path, "a/target.py", files)
    assert (_paths(importers), truncated) == (["b/one.py", "b/two.py"], False)


def test_result_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (MAX_IMPORTERS, MAX_IMPORTS_PER_IMPORTER) == (200, 20)
    files: dict[str, str | bytes] = {"a/target.py": ""}
    files.update(
        {f"b/u{i}.py": "import a.target\nimport a.target\nimport a.target\n" for i in range(4)}
    )
    monkeypatch.setattr(code_importers, "MAX_IMPORTERS", 3)
    monkeypatch.setattr(code_importers, "MAX_IMPORTS_PER_IMPORTER", 2)
    importers, _, truncated = _scan(tmp_path, "a/target.py", files)
    assert _paths(importers) == ["b/u0.py", "b/u1.py", "b/u2.py"]
    assert all([s.line for s in i.imports] == [1, 2] for i in importers)
    assert truncated is True
    monkeypatch.setattr(code_importers, "MAX_IMPORTERS", 4)
    monkeypatch.setattr(code_importers, "MAX_IMPORTS_PER_IMPORTER", 3)
    importers, _, truncated = _scan(tmp_path, "a/target.py", files)
    assert (len(importers), truncated) == (4, False)


@pytest.mark.parametrize(
    "source",
    [
        "(" * 201 + ")" * 201 + "\nimport a.target\n",
        "import a.target\nx = " + "+".join(["1"] * 100_000),
        "".join("    " * i + f"def f{i}():\n" for i in range(101))
        + "    " * 101
        + "import a.target\n",
    ],
    ids=["nested-parens", "long-chain", "deep-indent"],
)
def test_hostile_sources_are_syntax_errors(tmp_path: Path, source: str) -> None:
    started = time.perf_counter()
    importers, summary, _ = _scan(tmp_path, "a/target.py", {"a/target.py": "", "b/evil.py": source})
    assert (importers, summary.syntax_error) == ([], 1)
    assert time.perf_counter() - started < 5.0


def test_snapshot_mismatch_fails_the_whole_scan(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rows = _rows(
        root, {"a/target.py": "", "b/one.py": "import a.target\n", "b/two.py": "import a.target\n"}
    )
    (root / "b" / "two.py").write_bytes(b"import a.target  # changed\n")  # no longer the snapshot
    with pytest.raises(FileContentError) as raised:
        scan_importers(root, rows, "a/target.py")
    assert (raised.value.status_code, raised.value.detail) == (409, REPOSITORY_CHANGED)
    (root / "b" / "two.py").unlink()  # missing is a mismatch too
    with pytest.raises(FileContentError):
        scan_importers(root, rows, "a/target.py")


def test_files_outside_the_snapshot_are_never_read(tmp_path: Path, read_spy: list[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rows = _rows(root, {"a/target.py": "", "b/user.py": "import a.target\n"})
    (root / "b" / "stray.py").write_bytes(b"import a.target\n")  # on disk, not in the snapshot
    importers, _, _ = scan_importers(root, rows, "a/target.py")
    assert _paths(importers) == ["b/user.py"]
    assert read_spy == ["b/user.py"]


def test_repository_code_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "executed"
    evil = (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "import a.target\n"
        "__import__('os').system('echo pwned')\n"
        "import subprocess; subprocess.run(['echo'])\n"
    )
    files: dict[str, str | bytes] = {"a/target.py": "", "b/evil.py": evil}
    root = tmp_path / "repo"
    root.mkdir()
    rows = _rows(root, files)
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
    ):
        monkeypatch.setattr(target, name, refuse)
    importers, _, _ = scan_importers(root, rows, "a/target.py")
    monkeypatch.undo()
    assert _paths(importers) == ["b/evil.py"]
    assert not marker.exists()
    assert compiled_flags and all(flags & ast.PyCF_ONLY_AST for flags in compiled_flags)


def test_match_imports_is_pure() -> None:
    index = ModuleIndex.build(["a/target.py", "b/user.py"])
    assert match_imports("import (\n", "b/user.py", "a/target.py", index) == (
        "syntax_error",
        [],
        False,
    )
    status, found, _ = match_imports("import a.target\n", "b/user.py", "a/target.py", index)
    assert (status, [s.line for s in found]) == ("parsed", [1])


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


FILES = {path: source.encode() for path, source in REPO.items()}


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
        f"/repositories/{repository_id or owner.repository_id}/files/importers"
    )


async def _get(
    client: httpx.AsyncClient, owner: Owner, path: str | None, **kw: Any
) -> httpx.Response:
    params = {} if path is None else {"path": path}
    url = _url(owner, kw.pop("project_id", None), kw.pop("repository_id", None))
    return await client.get(url, params=params, headers=kw.pop("headers", owner.headers))


async def _importers(client: httpx.AsyncClient, owner: Owner, path: str) -> dict[str, Any]:
    response = await _get(client, owner, path)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _storage(owner: Owner) -> Path:
    return ingestion.repository_storage_dir(get_settings(), uuid.UUID(owner.repository_id))


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("directory junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


EMPTY_SUMMARY = {"files_scanned": 0, "too_large": 0, "binary": 0, "syntax_error": 0}


# --- API -----------------------------------------------------------------------------------


@requires_db
async def test_importers_endpoint(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    body = await _importers(client, alice, "app/services/auth.py")
    assert set(body) == {"path", "status", "importers", "summary", "truncated"}
    assert (body["path"], body["status"], body["truncated"]) == (
        "app/services/auth.py",
        "scanned",
        False,
    )
    assert [i["path"] for i in body["importers"]] == [
        "app/api/routes.py",
        "app/main.py",
        "app/models/user.py",
        "app/services/__init__.py",
        "app/services/search.py",
        "stubs/typed.pyi",
    ]
    main = next(i for i in body["importers"] if i["path"] == "app/main.py")
    assert main == {
        "path": "app/main.py",
        "imports": [
            {
                "line": 2,
                "statement": "from",
                "module": "app.services",
                "level": 0,
                "names": ["auth", "search"],
                "resolution": "resolved",
            },
            {
                "line": 4,
                "statement": "from",
                "module": "app.services.auth",
                "level": 0,
                "names": ["login"],
                "resolution": "resolved",
            },
        ],
    }
    python_files = [p for p in REPO if p.endswith((".py", ".pyi"))]
    assert body["summary"] == {**EMPTY_SUMMARY, "files_scanned": len(python_files) - 1}
    assert "app/services/auth.py" not in read_spy  # the target is never read
    assert "docs/notes.md" not in read_spy  # only Python snapshot rows are read


@requires_db
async def test_unsupported_and_unindexable_targets(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    assert await _importers(client, alice, "docs/notes.md") == {
        "path": "docs/notes.md",
        "status": "unsupported_language",
        "importers": [],
        "summary": EMPTY_SUMMARY,
        "truncated": False,
    }
    assert await _importers(client, alice, "__init__.py") == {
        "path": "__init__.py",
        "status": "scanned",
        "importers": [],
        "summary": EMPTY_SUMMARY,
        "truncated": False,
    }
    assert read_spy == []


@requires_db
async def test_target_content_is_irrelevant(client: httpx.AsyncClient, alice: Owner) -> None:
    files = dict(FILES)
    files["a/target.py"] = os.urandom(MAX_CONTENT_BYTES + 1)  # too large and binary
    files["b/user.py"] = b"import a.target\n"
    await _ingest(client, alice, files)
    body = await _importers(client, alice, "a/target.py")
    assert [i["path"] for i in body["importers"]] == ["b/user.py"]


@requires_db
async def test_index_limit(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_importers, "MAX_INDEXED_PATHS", 3)
    first = await _importers(client, alice, "app/__init__.py")  # among the first 3 paths
    assert first["truncated"] is True
    beyond = await _importers(client, alice, "web/xapp/auth.py")  # not indexed: unresolvable
    assert (beyond["importers"], beyond["truncated"]) == ([], True)


@requires_db
async def test_unknown_paths_and_validation(
    client: httpx.AsyncClient, alice: Owner, read_spy: list[str]
) -> None:
    for path in (
        "missing.py",
        "../etc/passwd",
        "app/../app/main.py",
        "/app/main.py",
        "APP/MAIN.PY",
    ):
        response = await _get(client, alice, path)
        assert (response.status_code, response.json()) == (404, FILE_NOT_FOUND)
    assert (await _get(client, alice, None)).status_code == 422
    assert (await _get(client, alice, "")).status_code == 422
    assert (await _get(client, alice, "a" * 4097)).status_code == 422
    for kwargs in ({"repository_id": "not-a-uuid"}, {"project_id": "not-a-uuid"}):
        assert (await _get(client, alice, "app/main.py", **kwargs)).status_code == 422
    assert read_spy == []


@requires_db
async def test_changed_storage_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, tmp_path: Path
) -> None:
    root = _storage(alice)
    (root / "app" / "main.py").write_bytes(b"import app.services.auth  # tampered\n")
    response = await _get(client, alice, "app/services/auth.py")
    assert (response.status_code, response.json()) == (409, CHANGED)
    assert "tampered" not in response.text

    await _ingest(client, alice, FILES)  # restore a consistent snapshot
    (_storage(alice) / "app" / "main.py").unlink()  # missing
    response = await _get(client, alice, "app/services/auth.py")
    assert (response.status_code, response.json()) == (409, CHANGED)

    await _ingest(client, alice, FILES)
    root = _storage(alice)
    outside = tmp_path / "outside"
    shutil.copytree(root / "stubs", outside)  # identical bytes behind a junction
    shutil.rmtree(root / "stubs")
    _junction(root / "stubs", outside)
    response = await _get(client, alice, "app/services/auth.py")
    assert (response.status_code, response.json()) == (409, CHANGED)


@requires_db
async def test_storage_swap_during_the_request_fails_with_409(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the snapshot query, storage changes before the importers are read."""
    real_build = code_importers.ModuleIndex.build
    root = _storage(alice)

    def build_then_swap(paths: Any) -> Any:
        index = real_build(paths)
        (root / "web" / "client.py").write_bytes(b"import app.services.auth  # new snapshot\n")
        return index

    monkeypatch.setattr(code_importers.ModuleIndex, "build", build_then_swap)
    response = await _get(client, alice, "app/services/auth.py")
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
        body = await _importers(client, alice, "app/services/auth.py")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(body["importers"]) == 6
    assert len([s for s in statements if "FROM files" in s]) == 1


@requires_db
async def test_reingestion_changes_the_importers(client: httpx.AsyncClient, alice: Owner) -> None:
    files = dict(FILES)
    del files["app/main.py"]
    files["app/new.py"] = b"from app.services import auth\n"
    await _ingest(client, alice, files)
    body = await _importers(client, alice, "app/services/auth.py")
    paths = [i["path"] for i in body["importers"]]
    assert "app/new.py" in paths and "app/main.py" not in paths


@requires_db
async def test_no_internals_or_content_are_exposed_or_logged(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    alice: Owner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "UNIQUE-CONTENT-MARKER-7d42"
    await _ingest(
        client,
        alice,
        {**FILES, "m.py": f"SECRET = '{marker}'\nimport app.services.auth\n".encode()},
    )
    caplog.set_level(logging.DEBUG)
    response = await _get(client, alice, "app/services/auth.py")
    assert response.status_code == 200
    assert "m.py" in [i["path"] for i in response.json()["importers"]]
    assert marker not in response.text and marker not in caplog.text
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
        ("app/main.py", {"project_id": other_project}),
        ("app/main.py", {"repository_id": str(uuid.uuid4())}),
    ):
        response = await _get(client, alice, path, **kwargs)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert (await client.get(_url(alice), params={"path": "app/main.py"})).status_code == 401
    bad = await _get(client, alice, "app/main.py", headers={"Authorization": "Bearer nope"})
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
    limited = await _get(client, alice, "app/services/auth.py")
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Too many attempts; please try again later"}
    assert limiter.hits == [(IMPORTERS_LIMIT, alice.user_id)]
    assert read_spy == []


@requires_db_and_redis
async def test_rate_limit_is_30_per_user_per_10_minutes(
    client: httpx.AsyncClient, alice: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert IMPORTERS_LIMIT == Limit("importers-user", 30, 600)
    assert TEST_REDIS_URL
    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    if settings.host not in ("127.0.0.1", "localhost") or settings.database == 0:
        pytest.fail("TEST_REDIS_URL must be a loopback, non-default (db != 0) disposable Redis")
    monkeypatch.setattr(get_settings(), "redis_url", SecretStr(TEST_REDIS_URL))
    pool = await create_pool(settings)
    await pool.flushdb()
    app.dependency_overrides.pop(get_rate_limiter, None)  # use the real Step 17 limiter
    try:
        for _ in range(30):
            assert (await _get(client, alice, "docs/notes.md")).status_code == 200
        limited = await _get(client, alice, "docs/notes.md")
        assert limited.status_code == 429
        assert limited.json() == {"detail": "Too many attempts; please try again later"}
        assert 1 <= int(limited.headers["Retry-After"]) <= 600
        keys = [k.decode() if isinstance(k, bytes) else k for k in await pool.keys("*")]
        assert any(k.startswith("ratelimit:importers-user:") for k in keys)
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
    for _ in range(32):  # beyond the limit
        assert (await _get(client, alice, "docs/notes.md")).status_code == 200
