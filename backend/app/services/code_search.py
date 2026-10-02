"""Request-time literal text search over a repository's current snapshot.

Files are the repository's files rows, searched in path order. Each is read only through
the Step 21 verified reader (links refused, containment checked, size bounded, SHA-256
verified) and decoded with its strict UTF-8 decoder. Nothing is executed, stored, cached
or logged, and neither the query nor any result is logged.

Matching is a literal, case-insensitive substring test: the line and the query are both
lowercased with ``str.lower`` and compared with ``str.find``, which runs in linear time.
The query is never interpreted as a regular expression or a glob. Lines are split on
"\\n" (a trailing "\\r" is removed), only their first MAX_LINE_CHARS characters are
searched, and each matching line is reported once with its 1-based number and a preview of
at most PREVIEW_CHARS characters starting about PREVIEW_LEAD characters before the match.

At most MAX_FILES files and MAX_TOTAL_BYTES of stored ``size_bytes`` (checked before a
file is opened) are searched and at most MAX_MATCHES matches are returned; ``truncated``
reports that a limit stopped the search. A file that cannot be searched never fails the
request: it is counted as binary (including invalid UTF-8), too_large or unavailable.
"""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_search import CodeSearchMatch, CodeSearchResponse, CodeSearchSummary

# Step 21's verified reader and decoder, reused unchanged (never duplicated).
from app.services.file_content import MAX_CONTENT_BYTES, _decode, _read_verified, _Unavailable
from app.services.ingestion import repository_storage_dir
from app.services.rate_limit import Limit

MAX_FILES = 5000
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MATCHES = 200
MAX_LINE_CHARS = 4096
PREVIEW_CHARS = 200
PREVIEW_LEAD = 60
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
CODE_SEARCH_LIMIT = Limit("code-search-user", 60, 600)

Hit = tuple[int, str]  # (1-based line number, preview)


def _original_index(segment: str, lowered: str, index: int) -> int:
    """Map an index in ``lowered`` back to ``segment`` (lowercasing a few characters, such as
    U+0130, changes their length)."""
    if len(lowered) == len(segment):
        return index
    total = 0
    for position, char in enumerate(segment):
        if total >= index:
            return position
        total += len(char.lower())
    return len(segment)


def _preview(segment: str, index: int) -> str:
    """At most PREVIEW_CHARS of ``segment``, starting about PREVIEW_LEAD before ``index``."""
    if len(segment) <= PREVIEW_CHARS:
        return segment
    start = min(max(0, index - PREVIEW_LEAD), len(segment) - PREVIEW_CHARS)
    return segment[start : start + PREVIEW_CHARS]


def search_text(text: str, query: str) -> list[Hit]:
    """Every line containing ``query`` (literal, case-insensitive), once per line."""
    needle = query.lower()
    hits: list[Hit] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        segment = raw.removesuffix("\r")[:MAX_LINE_CHARS]
        lowered = segment.lower()
        index = lowered.find(needle)
        if index >= 0:
            hits.append((number, _preview(segment, _original_index(segment, lowered, index))))
    return hits


def search_file(
    root: Path, path: str, size_bytes: int, sha256: str | None, query: str
) -> tuple[str, list[Hit]]:
    """(status, hits) for one stored file; status is scanned, binary, too_large or
    unavailable."""
    if size_bytes > MAX_CONTENT_BYTES:
        return "too_large", []  # never opened
    try:
        data = _read_verified(root, path, sha256)
    except _Unavailable:
        return "unavailable", []
    text = _decode(data)
    if text is None:
        return "binary", []
    return "scanned", search_text(text, query)


async def code_search(db: AsyncSession, repository: Repository, query: str) -> CodeSearchResponse:
    rows = (
        await db.execute(
            select(File.path, File.size_bytes, File.sha256).where(
                File.repository_id == repository.id
            )
        )
    ).all()
    rows = sorted(rows, key=lambda row: row.path)
    truncated = len(rows) > MAX_FILES
    root = repository_storage_dir(get_settings(), repository.id)

    counts = {"scanned": 0, "binary": 0, "too_large": 0, "unavailable": 0}
    matches: list[CodeSearchMatch] = []
    budget = MAX_TOTAL_BYTES
    for row in rows[:MAX_FILES]:
        if row.size_bytes <= MAX_CONTENT_BYTES:  # larger files are never read
            if row.size_bytes > budget:
                truncated = True  # checked before the file is opened
                break
            budget -= row.size_bytes
        status, hits = await run_in_threadpool(
            search_file, root, row.path, row.size_bytes, row.sha256, query
        )
        counts[status] += 1
        overflow = len(matches) + len(hits) > MAX_MATCHES
        if overflow:
            hits = hits[: MAX_MATCHES - len(matches)]
        matches.extend(
            CodeSearchMatch(path=row.path, line=line, preview=preview) for line, preview in hits
        )
        if overflow:
            truncated = True  # more matches existed: stop searching
            break

    matches.sort(key=lambda match: (match.path, match.line))
    summary = CodeSearchSummary(
        files_scanned=counts["scanned"],
        binary=counts["binary"],
        too_large=counts["too_large"],
        unavailable=counts["unavailable"],
        matches=len(matches),
    )
    return CodeSearchResponse(matches=matches, summary=summary, truncated=truncated)
