"""Request-time structural outline of one Python file of an (already authorized) repository.

Only files whose stored language is Python (``.py``/``.pyi``) are outlined; any other file
is reported as unsupported without being read. Python files are read only through Step 21's
``read_file_content`` (exact files row match, link and containment checks, SHA-256
verification, strict UTF-8), reused unchanged; its 404/409 errors propagate.

The text is parsed with ``ast.parse`` and nothing else: the repository's code is never
compiled to bytecode, imported or executed, and no subprocess or network access is used.
Parsing happens in the threadpool and only for text of at most MAX_OUTLINE_BYTES; anything
the parser rejects (syntax errors, null bytes, nesting or recursion limits) is reported as
``syntax_error``. The tree is walked iteratively (never recursively), visiting only the
bodies of the module, classes and functions, in source order. At most MAX_SYMBOLS symbols
and MAX_DEPTH nesting levels are reported; ``truncated`` says that definitions were left
out. Nothing is stored, cached or logged.
"""

import ast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.models import File, Repository
from app.schemas.code_outline import CodeOutlineResponse, OutlineStatus, OutlineSymbol
from app.services.file_content import FILE_NOT_FOUND, FileContentError, read_file_content
from app.services.rate_limit import Limit

SUPPORTED_LANGUAGE = "Python"
SUPPORTED_EXTENSIONS = frozenset({"py", "pyi"})
MAX_OUTLINE_BYTES = 256 * 1024  # of decoded text, measured as UTF-8
MAX_SYMBOLS = 2000
MAX_DEPTH = 8  # nesting levels reported: depth 0 (top level) to MAX_DEPTH - 1
MAX_NAME_CHARS = 200
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
OUTLINE_LIMIT = Limit("outline-user", 120, 600)

_Definition = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
# Everything the parser may raise for hostile or invalid input (SyntaxError includes
# IndentationError; ValueError covers null bytes on older interpreters).
_PARSE_ERRORS = (SyntaxError, ValueError, RecursionError, MemoryError)


def outline_source(source: str) -> tuple[OutlineStatus, list[OutlineSymbol], bool]:
    """(status, symbols in source order, truncated) for Python ``source``."""
    try:
        tree = ast.parse(source)  # builds a syntax tree only; nothing is compiled or run
    except _PARSE_ERRORS:
        return "syntax_error", [], False

    symbols: list[OutlineSymbol] = []
    truncated = False
    # Explicit stack of (node, depth, defined directly in a class); children are pushed in
    # reverse so they pop in source order (pre-order).
    stack: list[tuple[ast.stmt, int, bool]] = [(node, 0, False) for node in reversed(tree.body)]
    while stack:
        node, depth, in_class = stack.pop()
        if not isinstance(node, _Definition):
            continue
        if depth >= MAX_DEPTH:
            truncated = True
            continue
        if len(symbols) >= MAX_SYMBOLS:
            truncated = True
            break
        is_class = isinstance(node, ast.ClassDef)
        symbols.append(
            OutlineSymbol(
                kind="class" if is_class else "method" if in_class else "function",
                is_async=isinstance(node, ast.AsyncFunctionDef),
                name=node.name[:MAX_NAME_CHARS],
                line=node.lineno,
                end_line=node.end_lineno if node.end_lineno is not None else node.lineno,
                depth=depth,
            )
        )
        stack.extend((child, depth + 1, is_class) for child in reversed(node.body))
    return "parsed", symbols, truncated


def _is_python(file_language: str | None, extension: str | None) -> bool:
    return file_language == SUPPORTED_LANGUAGE and (extension or "").lower() in SUPPORTED_EXTENSIONS


async def code_outline(db: AsyncSession, repository: Repository, path: str) -> CodeOutlineResponse:
    row = (
        await db.execute(
            select(File.path, File.language, File.extension).where(
                File.repository_id == repository.id, File.path == path
            )
        )
    ).first()
    if row is None:
        raise FileContentError(404, FILE_NOT_FOUND)

    def response(
        status: OutlineStatus, symbols: list[OutlineSymbol] | None = None, truncated: bool = False
    ) -> CodeOutlineResponse:
        return CodeOutlineResponse(
            path=row.path, status=status, symbols=symbols or [], truncated=truncated
        )

    if not _is_python(row.language, row.extension):
        return response("unsupported_language")  # never read
    file = await read_file_content(db, repository, row.path)  # FileContentError: 404 / 409
    if file.reason == "binary":
        return response("binary")
    if file.reason == "too_large" or file.content is None:
        return response("too_large")
    if len(file.content.encode("utf-8")) > MAX_OUTLINE_BYTES:
        return response("too_large")  # never parsed
    status, symbols, truncated = await run_in_threadpool(outline_source, file.content)
    if status != "parsed":
        return response(status)
    return response("parsed", symbols, truncated)
