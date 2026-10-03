"""Request-time Python symbol search: definitions whose name equals the query.

Symbols are exactly Step 37's: ``outline_source`` (reused unchanged) decides which
classes, functions and methods exist, with their kind, async flag, lines and depth.
Matching is exact and case-sensitive against the NFKC-normalized query; Python itself
NFKC-normalizes identifiers, so ``def ａｕｔｈ`` defines ``auth``.

Step 37 caps names at MAX_NAME_CHARS. A capped name is exactly MAX_NAME_CHARS long, so a
shorter query is compared directly. For a query of MAX_NAME_CHARS or more, a candidate
whose capped name equals the query's prefix is confirmed against its full identifier,
looked up by line in the same ``ast.parse`` tree; a longer identifier therefore never
matches merely because its first MAX_NAME_CHARS characters do.

Snapshot: a single SELECT returns the repository's Python rows. Each file is read only
through Step 21's verified reader against its row's SHA-256; if any read fails (the
stored bytes no longer match, e.g. a re-ingestion swapped storage) the whole request
fails with 409. No partial or mixed results are ever returned.

Pre-filter (safe): in ASCII text every identifier is ASCII and NFKC leaves ASCII
unchanged, so a definition named by the query must appear verbatim; ASCII text without
it is skipped. Non-ASCII text is always parsed. Parsing is ``ast.parse`` only: nothing is
compiled to bytecode, imported or executed. Work is bounded by the index, read, parse,
per-file and result limits; ``truncated`` reports that one was reached. Nothing is
stored, cached or logged.
"""

import ast
import unicodedata
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_outline import OutlineStatus, OutlineSymbol
from app.schemas.symbol_search import SymbolResult, SymbolSearchResponse, SymbolSearchSummary

# Steps 37, 39 and 41's rules and limits and Step 21's verified reader, reused unchanged.
from app.services.code_importers import MAX_PARSED_BYTES, MAX_SCANNED_BYTES, REPOSITORY_CHANGED
from app.services.code_imports import MAX_INDEXED_PATHS
from app.services.code_outline import (
    _PARSE_ERRORS,
    MAX_NAME_CHARS,
    MAX_OUTLINE_BYTES,
    SUPPORTED_LANGUAGE,
    _is_python,
    outline_source,
)
from app.services.file_content import (
    MAX_CONTENT_BYTES,
    FileContentError,
    _decode,
    _read_verified,
    _Unavailable,
)
from app.services.ingestion import repository_storage_dir
from app.services.rate_limit import Limit

MAX_RESULTS = 200
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
SYMBOL_SEARCH_LIMIT = Limit("symbol-search-user", 30, 600)

_DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def normalize_query(query: str) -> str:
    """The query as Python would spell the identifier (NFKC)."""
    return unicodedata.normalize("NFKC", query)


def _full_names(source: str) -> dict[int, list[str]]:
    """Full (uncapped) names of the definitions in ``source``, by line."""
    try:
        tree = ast.parse(source)  # the same parse Step 37 performs; nothing is run
    except _PARSE_ERRORS:
        return {}
    names: dict[int, list[str]] = {}
    for node in ast.walk(tree):  # iterative
        if isinstance(node, _DEFINITIONS):
            names.setdefault(node.lineno, []).append(node.name)
    return names


def match_symbols(source: str, query: str) -> tuple[OutlineStatus, list[OutlineSymbol], bool, bool]:
    """(status, Step 37 symbols named exactly ``query`` in source order, truncated, whether
    full names had to be confirmed). ``query`` must already be NFKC-normalized."""
    status, symbols, truncated = outline_source(source)
    if status != "parsed":
        return status, [], False, False
    if len(query) < MAX_NAME_CHARS:
        return status, [s for s in symbols if s.name == query], truncated, False
    # A capped name equals the query's prefix: confirm the full identifier.
    candidates = [s for s in symbols if s.name == query[:MAX_NAME_CHARS]]
    if not candidates:
        return status, [], truncated, False
    full = _full_names(source)
    return status, [s for s in candidates if query in full.get(s.line, [])], truncated, True


def scan_symbols(
    root: Any, rows: Sequence[Any], query: str
) -> tuple[list[SymbolResult], SymbolSearchSummary, bool]:
    """Definitions named ``query`` (NFKC-normalized) among ``rows`` (the snapshot's indexed
    Python rows, sorted by path). Raises FileContentError(409) when a file no longer
    matches its snapshot row."""
    results: list[SymbolResult] = []
    counts = {"files_scanned": 0, "too_large": 0, "binary": 0, "syntax_error": 0}
    read_budget, parse_budget = MAX_SCANNED_BYTES, MAX_PARSED_BYTES
    truncated = False
    for row in rows:
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
        if text.isascii() and query not in text:
            continue  # cannot define the query (see the module docstring)
        if size > parse_budget:
            truncated = True
            break
        parse_budget -= size
        status, matches, cut, confirmed = match_symbols(text, query)
        if confirmed:
            parse_budget -= size  # the confirming parse counts too
        if status == "syntax_error":
            counts["syntax_error"] += 1
            continue
        truncated = truncated or cut
        for symbol in matches:
            if len(results) >= MAX_RESULTS:
                truncated = True
                break
            results.append(SymbolResult(path=row.path, **symbol.model_dump()))
        if len(results) >= MAX_RESULTS and truncated:
            break
    return results, SymbolSearchSummary(**counts), truncated


async def symbol_search(
    db: AsyncSession, repository: Repository, query: str
) -> SymbolSearchResponse:
    normalized = normalize_query(query)
    # One statement, one snapshot: every Python row of the repository.
    rows = (
        await db.execute(
            select(File.path, File.language, File.extension, File.size_bytes, File.sha256).where(
                File.repository_id == repository.id, File.language == SUPPORTED_LANGUAGE
            )
        )
    ).all()
    python_rows = sorted(
        (r for r in rows if _is_python(r.language, r.extension)), key=lambda r: r.path
    )
    indexed = python_rows[:MAX_INDEXED_PATHS]
    root = repository_storage_dir(get_settings(), repository.id)
    results, summary, truncated = await run_in_threadpool(scan_symbols, root, indexed, normalized)
    return SymbolSearchResponse(
        results=results,
        summary=summary,
        truncated=truncated or len(python_rows) > MAX_INDEXED_PATHS,
    )
