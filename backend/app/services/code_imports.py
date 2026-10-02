"""Request-time resolution of one Python file's imports to repository files.

Snapshot: a single SELECT returns the requested files row together with the repository's
Python files rows, so the path index and the requested file always come from one database
snapshot. Only the requested file is read, through Step 21's verified reader against that
row's SHA-256: if storage no longer holds that snapshot (e.g. a concurrent re-ingestion
swapped it) the read fails and the request answers 409 instead of mixing snapshots. Other
files are matched by their stored paths only and are never opened.

Parsing reuses Step 37's rules and limits (Python by stored language and .py/.pyi
extension, text of at most MAX_OUTLINE_BYTES, ``ast.parse`` only: nothing is compiled to
bytecode, imported or executed). Every import statement in the file is listed, nested ones
included, ordered by source position.

Module index: each indexed Python path maps to its module parts (extension removed,
``pkg/__init__`` -> ``pkg``). Absolute imports match any suffix of complete parts, built
only from parts that are valid identifiers (a part is never split or substring-matched).
``import a.b`` looks up ``a.b``; ``from a.b import x, y`` looks up ``a.b``, ``a.b.x`` and
``a.b.y``. Relative imports are resolved from the importing file's package by exact
module parts; climbing above the repository root matches nothing. Python's import system
(sys.path, source roots, namespace packages, installed packages) is not modelled.

Nothing is stored, cached or logged.
"""

import ast
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_imports import CodeImportsResponse, ImportEntry, ImportResolution
from app.schemas.code_outline import OutlineStatus

# Step 37's Python rules and limits, and Step 21's verified reader, reused unchanged.
from app.services.code_outline import (
    _PARSE_ERRORS,
    MAX_OUTLINE_BYTES,
    SUPPORTED_LANGUAGE,
    _is_python,
)
from app.services.file_content import (
    CONTENT_UNAVAILABLE,
    FILE_NOT_FOUND,
    MAX_CONTENT_BYTES,
    FileContentError,
    _decode,
    _read_verified,
    _Unavailable,
)
from app.services.ingestion import repository_storage_dir
from app.services.rate_limit import Limit

MAX_IMPORTS = 500
MAX_NAMES = 50
MAX_TARGETS = 5
MAX_TEXT_CHARS = 200
MAX_INDEXED_PATHS = 10_000
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
IMPORTS_LIMIT = Limit("imports-user", 120, 600)

Parts = tuple[str, ...]


def module_parts(path: str) -> Parts:
    """Module parts of a stored Python path: extension removed, ``pkg/__init__`` -> pkg."""
    *directories, name = path.split("/")
    stem = name.rsplit(".", 1)[0]
    parts = (*directories, stem)
    return parts[:-1] if stem == "__init__" else parts


@dataclass
class ModuleIndex:
    """Repository Python paths by module name (stored paths only; nothing is read)."""

    by_suffix: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_parts: dict[Parts, set[str]] = field(default_factory=lambda: defaultdict(set))

    @classmethod
    def build(cls, paths: Iterable[str]) -> "ModuleIndex":
        index = cls()
        for path in paths:
            parts = module_parts(path)
            if not parts:
                continue  # a root __init__ names no module
            index.by_parts[parts].add(path)
            # Suffixes of complete parts, each part a valid identifier (importable).
            for start in range(len(parts) - 1, -1, -1):
                if not parts[start].isidentifier():
                    break
                index.by_suffix[".".join(parts[start:])].add(path)
        return index

    def absolute(self, dotted: str) -> set[str]:
        return self.by_suffix.get(dotted, set())

    def exact(self, parts: Parts) -> set[str]:
        return self.by_parts.get(parts, set()) if parts else set()


def _package_of(importer: str) -> Parts:
    parts = module_parts(importer)
    is_package = importer.split("/")[-1].rsplit(".", 1)[0] == "__init__"
    return parts if is_package else parts[:-1]


def _lookups(
    node: ast.Import | ast.ImportFrom, importer: str, index: ModuleIndex, names: Sequence[str]
) -> list[set[str]]:
    """The files matched by each module this statement imports."""
    if isinstance(node, ast.Import):
        return [index.absolute(name) for name in names]
    submodules = [name for name in names if name != "*"]
    if node.level == 0:
        module = node.module or ""
        return [index.absolute(module)] + [index.absolute(f"{module}.{n}") for n in submodules]
    package = _package_of(importer)
    up = node.level - 1
    if up > len(package):
        return [set()]  # climbs above the repository root
    base = package[: len(package) - up]
    relative: Parts = tuple(node.module.split(".")) if node.module else ()
    found = [index.exact(base + relative)] if relative else []
    return found + [index.exact(base + relative + (n,)) for n in submodules]


def _resolution(matches: list[set[str]]) -> ImportResolution:
    if not any(matches):
        return "not_found"
    return "ambiguous" if any(len(found) > 1 for found in matches) else "resolved"


def _cap(text: str) -> tuple[str, bool]:
    return text[:MAX_TEXT_CHARS], len(text) > MAX_TEXT_CHARS


def analyze_imports(
    source: str, importer: str, python_paths: Sequence[str]
) -> tuple[OutlineStatus, list[ImportEntry], bool]:
    """(status, imports in source order, truncated) for the Python ``source`` of
    ``importer``, resolved against ``python_paths`` (sorted stored paths)."""
    try:
        tree = ast.parse(source)  # builds a syntax tree only; nothing is compiled or run
    except _PARSE_ERRORS:
        return "syntax_error", [], False

    truncated = len(python_paths) > MAX_INDEXED_PATHS
    index = ModuleIndex.build(python_paths[:MAX_INDEXED_PATHS])
    # ast.walk is iterative; every import statement, nested ones included.
    nodes = sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)),
        key=lambda n: (n.lineno, n.col_offset),
    )
    if len(nodes) > MAX_IMPORTS:
        nodes, truncated = nodes[:MAX_IMPORTS], True

    imports: list[ImportEntry] = []
    for node in nodes:
        names = [alias.name for alias in node.names]
        if len(names) > MAX_NAMES:
            names, truncated = names[:MAX_NAMES], True
        matches = _lookups(node, importer, index, names)
        found = sorted(set().union(*matches))
        if len(found) > MAX_TARGETS:
            truncated = True
        module = node.module if isinstance(node, ast.ImportFrom) else None
        shown_names = []
        for name in names:
            capped, cut = _cap(name)
            shown_names.append(capped)
            truncated = truncated or cut
        shown_module = None
        if module is not None:
            shown_module, cut = _cap(module)
            truncated = truncated or cut
        imports.append(
            ImportEntry(
                line=node.lineno,
                statement="import" if isinstance(node, ast.Import) else "from",
                module=shown_module,
                level=node.level if isinstance(node, ast.ImportFrom) else 0,
                names=shown_names,
                targets=found[:MAX_TARGETS],
                resolution=_resolution(matches),
            )
        )
    return "parsed", imports, truncated


async def code_imports(db: AsyncSession, repository: Repository, path: str) -> CodeImportsResponse:
    # One statement, one snapshot: the requested row and every Python row of the repository.
    rows = (
        await db.execute(
            select(File.path, File.language, File.extension, File.size_bytes, File.sha256).where(
                File.repository_id == repository.id,
                or_(File.path == path, File.language == SUPPORTED_LANGUAGE),
            )
        )
    ).all()
    row = next((r for r in rows if r.path == path), None)
    if row is None:
        raise FileContentError(404, FILE_NOT_FOUND)

    def response(
        status: OutlineStatus, imports: list[ImportEntry] | None = None, truncated: bool = False
    ) -> CodeImportsResponse:
        return CodeImportsResponse(
            path=row.path, status=status, imports=imports or [], truncated=truncated
        )

    if not _is_python(row.language, row.extension):
        return response("unsupported_language")  # never read
    if row.size_bytes > MAX_CONTENT_BYTES:
        return response("too_large")  # never opened
    root = repository_storage_dir(get_settings(), repository.id)
    try:
        # Verified against this snapshot's hash: a swapped snapshot can only yield 409.
        data = await run_in_threadpool(_read_verified, root, row.path, row.sha256)
    except _Unavailable:
        raise FileContentError(409, CONTENT_UNAVAILABLE) from None
    text = _decode(data)
    if text is None:
        return response("binary")
    if len(text.encode("utf-8")) > MAX_OUTLINE_BYTES:
        return response("too_large")  # never parsed
    python_paths = sorted(r.path for r in rows if _is_python(r.language, r.extension))
    status, imports, truncated = await run_in_threadpool(
        analyze_imports, text, row.path, python_paths
    )
    return response(status, imports, truncated)
