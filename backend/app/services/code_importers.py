"""Request-time reverse import lookup: which Python files import one target Python file.

Resolution is exactly Step 39's: the same module index (built once per request from the
same snapshot rows), the same statement traversal (``import_statements``), the same name
cap and the same ``_lookups``. An importer matches when the target is in the FULL match
set of one of its statements; Step 39's display cap of five targets does not apply here.
So for every importer examined, it is reported for the target exactly when the forward
resolution of that importer matches the target.

Snapshot: a single SELECT returns the target row and every Python row of the repository.
The target's content is never read (only its path matters); candidate importers are read
only through Step 21's verified reader against their snapshot row's SHA-256. If any read
fails (the stored bytes no longer match, e.g. a re-ingestion swapped storage), the whole
request fails with 409: no partial or mixed results are ever returned.

Only text that may name the target is parsed: every import that resolves to the target
spells out the target's last module part, so ASCII text without it is skipped; non-ASCII
text is always parsed because Python normalizes identifiers (NFKC). Parsing uses
``ast.parse`` only: nothing is compiled to bytecode, imported or executed. Work is bounded
by MAX_SCANNED_BYTES read (stored sizes, checked before a file is opened), MAX_PARSED_BYTES
parsed, Step 37's per-file caps and the result caps; ``truncated`` reports any limit.
Nothing is stored, cached or logged.
"""

import ast
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_importers import (
    CodeImportersResponse,
    Importer,
    ImporterImport,
    ImportersSummary,
)

# Step 39's resolver, Step 37's Python rules and Step 21's verified reader, reused unchanged.
from app.services.code_imports import (
    MAX_INDEXED_PATHS,
    MAX_NAMES,
    ModuleIndex,
    _cap,
    _lookups,
    _resolution,
    import_statements,
    module_parts,
)
from app.services.code_outline import (
    _PARSE_ERRORS,
    MAX_OUTLINE_BYTES,
    SUPPORTED_LANGUAGE,
    _is_python,
)
from app.services.file_content import (
    FILE_NOT_FOUND,
    MAX_CONTENT_BYTES,
    FileContentError,
    _decode,
    _read_verified,
    _Unavailable,
)
from app.services.ingestion import repository_storage_dir
from app.services.rate_limit import Limit

MAX_SCANNED_BYTES = 64 * 1024 * 1024  # stored size of the files read
MAX_PARSED_BYTES = 4 * 1024 * 1024  # UTF-8 size of the text parsed
MAX_IMPORTERS = 200
MAX_IMPORTS_PER_IMPORTER = 20
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
IMPORTERS_LIMIT = Limit("importers-user", 30, 600)
REPOSITORY_CHANGED = "Repository changed during analysis; run it again"


def match_imports(
    source: str, importer: str, target: str, index: ModuleIndex
) -> tuple[str, list[ImporterImport], bool]:
    """(parsed | syntax_error, statements of ``importer`` that resolve to ``target``,
    truncated) using Step 39's traversal, name cap and full (uncapped) match sets."""
    try:
        tree = ast.parse(source)  # builds a syntax tree only; nothing is compiled or run
    except _PARSE_ERRORS:
        return "syntax_error", [], False
    nodes, truncated = import_statements(tree)
    found: list[ImporterImport] = []
    for node in nodes:
        names = [alias.name for alias in node.names]
        if len(names) > MAX_NAMES:
            names, truncated = names[:MAX_NAMES], True
        matches = _lookups(node, importer, index, names)
        if not any(target in matched for matched in matches):
            continue
        shown_names = []
        for name in names:
            capped, cut = _cap(name)
            shown_names.append(capped)
            truncated = truncated or cut
        shown_module = None
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            shown_module, cut = _cap(node.module)
            truncated = truncated or cut
        found.append(
            ImporterImport(
                line=node.lineno,
                statement="import" if isinstance(node, ast.Import) else "from",
                module=shown_module,
                level=node.level if isinstance(node, ast.ImportFrom) else 0,
                names=shown_names,
                resolution=_resolution(matches),
            )
        )
    return "parsed", found, truncated


def scan_importers(
    root: Path, rows: Sequence[Any], target: str
) -> tuple[list[Importer], ImportersSummary, bool]:
    """Importers of ``target`` among ``rows`` (the snapshot's indexed Python rows, sorted by
    path). Raises FileContentError(409) when a file no longer matches its snapshot row."""
    counts = {"files_scanned": 0, "too_large": 0, "binary": 0, "syntax_error": 0}
    parts = module_parts(target)
    if not parts:
        return [], ImportersSummary(**counts), False  # a root __init__ names no module
    index = ModuleIndex.build([row.path for row in rows])
    token = parts[-1]
    importers: list[Importer] = []
    read_budget, parse_budget = MAX_SCANNED_BYTES, MAX_PARSED_BYTES
    truncated = False
    for row in rows:
        if row.path == target:
            continue  # the target's content is never read
        if row.size_bytes > MAX_CONTENT_BYTES:
            counts["too_large"] += 1  # never opened
            continue
        if row.size_bytes > read_budget:
            truncated = True  # checked before the file is opened
            break
        read_budget -= row.size_bytes
        try:
            data = _read_verified(root, row.path, row.sha256)
        except _Unavailable:
            raise FileContentError(409, REPOSITORY_CHANGED) from None
        counts["files_scanned"] += 1
        text = _decode(data)
        if text is None:
            counts["binary"] += 1
            continue
        size = len(text.encode("utf-8"))
        if size > MAX_OUTLINE_BYTES:
            counts["too_large"] += 1  # never parsed
            continue
        if text.isascii() and token not in text:
            continue  # cannot name the target, so cannot import it
        if size > parse_budget:
            truncated = True
            break
        parse_budget -= size
        status, statements, cut = match_imports(text, row.path, target, index)
        if status == "syntax_error":
            counts["syntax_error"] += 1
            continue
        truncated = truncated or cut
        if not statements:
            continue
        if len(importers) >= MAX_IMPORTERS:
            truncated = True
            break
        if len(statements) > MAX_IMPORTS_PER_IMPORTER:
            statements, truncated = statements[:MAX_IMPORTS_PER_IMPORTER], True
        importers.append(Importer(path=row.path, imports=statements))
    return importers, ImportersSummary(**counts), truncated


async def code_importers(
    db: AsyncSession, repository: Repository, path: str
) -> CodeImportersResponse:
    # One statement, one snapshot: the target row and every Python row of the repository.
    rows = (
        await db.execute(
            select(File.path, File.language, File.extension, File.size_bytes, File.sha256).where(
                File.repository_id == repository.id,
                or_(File.path == path, File.language == SUPPORTED_LANGUAGE),
            )
        )
    ).all()
    target = next((r for r in rows if r.path == path), None)
    if target is None:
        raise FileContentError(404, FILE_NOT_FOUND)
    empty = ImportersSummary(files_scanned=0, too_large=0, binary=0, syntax_error=0)
    if not _is_python(target.language, target.extension):
        return CodeImportersResponse(
            path=target.path,
            status="unsupported_language",
            importers=[],
            summary=empty,
            truncated=False,
        )
    python_rows = sorted(
        (r for r in rows if _is_python(r.language, r.extension)), key=lambda r: r.path
    )
    indexed = python_rows[:MAX_INDEXED_PATHS]
    truncated = len(python_rows) > MAX_INDEXED_PATHS
    if not module_parts(target.path) or target.path not in {r.path for r in indexed}:
        # A root __init__ names no module; an unindexed target cannot be resolved to.
        return CodeImportersResponse(
            path=target.path, status="scanned", importers=[], summary=empty, truncated=truncated
        )
    root = repository_storage_dir(get_settings(), repository.id)
    importers, summary, cut = await run_in_threadpool(scan_importers, root, indexed, target.path)
    return CodeImportersResponse(
        path=target.path,
        status="scanned",
        importers=importers,
        summary=summary,
        truncated=truncated or cut,
    )
