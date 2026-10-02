"""Request-time inventory of the dependencies declared in a repository's manifest files.

Manifests are found among the current snapshot's files rows by exact file name, anywhere
in the repository: ``package.json``, ``pyproject.toml`` (PEP 621 ``[project]`` table only)
and ``requirements*.txt``. Each is read only through the Step 21 verified reader (links
refused, containment checked, size bounded, SHA-256 verified) and decoded with its strict
UTF-8 decoder, then parsed with the standard library (``json``, ``tomllib``) or a strict
line parser. Nothing is executed, installed, resolved, looked up or followed (no network,
no subprocesses, no included requirement files), and nothing is stored or logged.

Values are reported as written: the name must match NAME_PATTERN and the rest of the
requirement is the specifier; both are cut to MAX_VALUE_LENGTH characters. At most
MAX_MANIFESTS manifests (in path order) and MAX_DEPENDENCIES entries in total are
returned; ``truncated`` reports that more existed. A manifest that cannot be used never
fails the request: it is reported as invalid, too_large or unavailable.
"""

import json
import re
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.dependencies import (
    DependencyEntry,
    DependencyInventoryResponse,
    DependencySummary,
    ManifestResponse,
)

# Step 21's verified reader and decoder, reused unchanged (never duplicated).
from app.services.file_content import MAX_CONTENT_BYTES, _decode, _read_verified, _Unavailable
from app.services.ingestion import repository_storage_dir

MAX_MANIFESTS = 50
MAX_DEPENDENCIES = 5000
MAX_VALUE_LENGTH = 200
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_REQUIREMENTS_NAME = re.compile(r"^requirements.*\.txt$")
# pip's comment rule: '#' at the start of a line or after whitespace.
_COMMENT = re.compile(r"(^|\s)#.*$")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_PACKAGE_SCOPES = (
    ("dependencies", "runtime"),
    ("devDependencies", "dev"),
    ("peerDependencies", "peer"),
    ("optionalDependencies", "optional"),
)

Entry = tuple[str, str, str]  # (scope, name, specifier)


def _cap(value: str) -> str:
    return value[:MAX_VALUE_LENGTH]


def split_requirement(requirement: str) -> tuple[str, str] | None:
    """(name, specifier) of a requirement as written, or None without a valid name."""
    text = requirement.strip()
    match = NAME_PATTERN.match(text)
    if match is None:
        return None
    return _cap(match.group(0)), _cap(text[match.end() :].strip())


# --- Parsers (each yields entries in file order; invalid input raises ValueError) --------


def parse_package_json(text: str) -> Iterator[Entry]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("package.json must contain an object")
    for key, scope in _PACKAGE_SCOPES:
        section = data.get(key)
        if not isinstance(section, dict):
            continue
        for name, version in section.items():
            if isinstance(name, str) and isinstance(version, str):
                yield scope, _cap(name), _cap(version)


def _requirement_entries(requirements: Any, scope: str) -> Iterator[Entry]:
    if not isinstance(requirements, list):
        return
    for requirement in requirements:
        if isinstance(requirement, str) and (parsed := split_requirement(requirement)):
            yield scope, *parsed


def parse_pyproject(text: str) -> Iterator[Entry]:
    project = tomllib.loads(text).get("project")
    if not isinstance(project, dict):
        return  # no PEP 621 metadata: no declared dependencies
    yield from _requirement_entries(project.get("dependencies"), "runtime")
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group, requirements in optional.items():
            if isinstance(group, str):
                yield from _requirement_entries(requirements, _cap(f"optional:{group}"))


def parse_requirements(text: str) -> Iterator[Entry]:
    """One requirement per line. Options (-r, -c, -e, --index-url, ...), URLs and local
    paths are skipped; included files are never followed."""
    for raw in text.splitlines():
        line = _COMMENT.sub("", raw).strip()
        if not line or line.startswith("-"):
            continue
        if (
            "://" in line
            or line.startswith((".", "/", "\\", "~", "file:"))
            or _WINDOWS_PATH.match(line)
        ):
            continue
        if parsed := split_requirement(line):
            yield "runtime", *parsed


@dataclass(frozen=True)
class _Kind:
    ecosystem: str
    parse: Callable[[str], Iterator[Entry]]


_PACKAGE_JSON = _Kind("npm", parse_package_json)
_PYPROJECT = _Kind("python", parse_pyproject)
_REQUIREMENTS = _Kind("python", parse_requirements)


def manifest_kind(file_name: str) -> _Kind | None:
    if file_name == "package.json":
        return _PACKAGE_JSON
    if file_name == "pyproject.toml":
        return _PYPROJECT
    if _REQUIREMENTS_NAME.match(file_name):
        return _REQUIREMENTS
    return None


# --- One manifest -------------------------------------------------------------------------


def inventory_manifest(
    root: Path, path: str, size_bytes: int, sha256: str | None, kind: _Kind, limit: int
) -> tuple[str, list[Entry], bool]:
    """(status, at most ``limit`` entries sorted by scope/name, whether more existed)."""
    if size_bytes > MAX_CONTENT_BYTES:
        return "too_large", [], False  # never opened
    try:
        data = _read_verified(root, path, sha256)
    except _Unavailable:
        return "unavailable", [], False
    text = _decode(data)
    if text is None:
        return "invalid", [], False
    entries: list[Entry] = []
    overflow = False
    try:
        for entry in kind.parse(text):
            if len(entries) >= limit:
                overflow = True
                break
            entries.append(entry)
    except (ValueError, RecursionError):  # includes JSON and TOML decode errors
        return "invalid", [], False
    return "parsed", sorted(entries), overflow


# --- Repository inventory -----------------------------------------------------------------


async def dependency_inventory(
    db: AsyncSession, repository: Repository
) -> DependencyInventoryResponse:
    rows = (
        await db.execute(
            select(File.path, File.size_bytes, File.sha256).where(
                File.repository_id == repository.id
            )
        )
    ).all()
    candidates = sorted(
        (
            (row, kind)
            for row in rows
            if (kind := manifest_kind(PurePosixPath(row.path).name)) is not None
        ),
        key=lambda candidate: candidate[0].path,
    )
    truncated = len(candidates) > MAX_MANIFESTS
    root = repository_storage_dir(get_settings(), repository.id)

    manifests: list[ManifestResponse] = []
    counts = {"parsed": 0, "invalid": 0, "too_large": 0, "unavailable": 0}
    remaining = MAX_DEPENDENCIES
    for row, kind in candidates[:MAX_MANIFESTS]:
        status, entries, overflow = await run_in_threadpool(
            inventory_manifest, root, row.path, row.size_bytes, row.sha256, kind, remaining
        )
        truncated = truncated or overflow
        remaining -= len(entries)
        counts[status] += 1
        manifests.append(
            ManifestResponse.model_validate(
                {
                    "path": row.path,
                    "ecosystem": kind.ecosystem,
                    "status": status,
                    "dependencies": [
                        DependencyEntry(name=name, specifier=specifier, scope=scope)
                        for scope, name, specifier in entries
                    ],
                }
            )
        )
    summary = DependencySummary(
        manifests=len(manifests),
        parsed=counts["parsed"],
        invalid=counts["invalid"],
        too_large=counts["too_large"],
        unavailable=counts["unavailable"],
        dependencies=MAX_DEPENDENCIES - remaining,
    )
    return DependencyInventoryResponse(manifests=manifests, summary=summary, truncated=truncated)
