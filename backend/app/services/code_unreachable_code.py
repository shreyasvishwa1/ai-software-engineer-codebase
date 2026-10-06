"""Python unreachable code: the statements of one Python file that can never run because an earlier
statement of the same statement list always leaves that list, decided from the syntax tree alone.
Never guesses; this is not a full control-flow analysis.

Statement lists (U1): a module, def or class body; an if, for, while, try or with body; an ``else``
or ``finally`` list; an except handler's or a match case's body.

Leaving statements (U2), after which the rest of their list never runs:
- ``return``, ``raise``, ``break`` and ``continue``;
- an ``if`` statement with an ``else`` (``elif`` chains included) whose every branch holds a
  leaving statement of its own list;
- a ``while`` loop whose condition is a constant that is true (``while True:``, ``while 1:``) and
  whose body holds no ``break`` for that loop. A ``break`` in a nested loop's body belongs to that
  loop and one in a nested loop's ``else`` to this one; nested defs and classes never hold one.

Regions (U3): the statements after the first leaving statement of a list, to the end of that list,
form one region, reported once: the 1-based line where its first statement starts (the first
decorator's line for a decorated def or class), the last line of its last statement, the number of
statements in it, and the leaving statement's kind and line. Nothing inside a region is analyzed
further (none of it runs), nor is the ``else`` of an endless loop. Regions are listed in source
order; at most MAX_UNREACHABLE_REGIONS are returned; the summary counts them all.

Out of scope by design: leaving through ``with`` (a context manager may suppress an exception),
``try`` or ``match`` statements; other constant conditions (``if False:``); reporting the ``else``
of an endless loop; calls that never return (``sys.exit()``, ``os._exit()``, functions annotated
``NoReturn``: names can be rebound); ``assert False`` (removed under ``-O``); anything across
functions or files.

Snapshot: one SELECT returns the requested row and its SHA-256; the file is read once through
Step 21's verified reader, so any change answers 409. No other file is read. Parsing is
``ast.parse`` only, walked with explicit stacks (an ``elif`` chain may nest thousands of levels):
nothing is compiled to bytecode, imported or executed. Nothing is stored, cached or logged.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_outline import OutlineStatus
from app.schemas.code_unreachable_code import (
    UnreachableCodeResponse,
    UnreachableCodeSummary,
    UnreachableRegion,
)
from app.services.code_importers import REPOSITORY_CHANGED
from app.services.code_outline import _PARSE_ERRORS, MAX_OUTLINE_BYTES, _is_python
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

MAX_UNREACHABLE_REGIONS = 500  # regions returned, the first in source order

UNREACHABLE_CODE_LIMIT = Limit("unreachable-code-user", 120, 600)

Cause = Literal["return", "raise", "break", "continue", "if", "while"]

_JUMPS: dict[type[ast.stmt], Cause] = {
    ast.Return: "return",
    ast.Raise: "raise",
    ast.Break: "break",
    ast.Continue: "continue",
}
_LOOPS = (ast.For, ast.AsyncFor, ast.While)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass
class _Region:
    statements: list[ast.stmt]  # the unreachable rest of one list, never empty
    cause: Cause
    cause_line: int


def _lists(statement: ast.stmt) -> Iterator[list[ast.stmt]]:
    """The statement lists directly inside ``statement`` (U1), in source order."""
    body = getattr(statement, "body", None)
    if isinstance(body, list):  # def, class, loop, if, with and try bodies
        yield body
    for handler in getattr(statement, "handlers", ()):  # try and try* handlers
        yield handler.body
    for case in getattr(statement, "cases", ()):  # match cases
        yield case.body
    for field in ("orelse", "finalbody"):
        statements = getattr(statement, field, None)
        if statements:
            yield statements


def _breaks_out(body: list[ast.stmt]) -> bool:
    """Whether a loop body holds a ``break`` for that loop (U2)."""
    pending = [body]
    while pending:
        for statement in pending.pop():
            if isinstance(statement, ast.Break):
                return True
            if isinstance(statement, _SCOPES):
                continue  # a nested def or class never breaks this loop
            if isinstance(statement, _LOOPS):
                if statement.orelse:  # a nested loop's else belongs to this loop
                    pending.append(statement.orelse)
                continue  # its body's breaks are its own
            pending.extend(_lists(statement))
    return False


class _Leaving:
    """Which statements always leave their list (U2), each decided once."""

    def __init__(self) -> None:
        self._known: dict[int, Cause | None] = {}

    def cause(self, statement: ast.stmt) -> Cause | None:
        key = id(statement)
        if key not in self._known:
            if isinstance(statement, ast.If):
                self._decide_chain(statement)
            elif isinstance(statement, ast.While):
                test = statement.test
                endless = isinstance(test, ast.Constant) and bool(test.value)
                self._known[key] = "while" if endless and not _breaks_out(statement.body) else None
            else:
                self._known[key] = _JUMPS.get(type(statement))
        return self._known[key]

    def leaves(self, statements: list[ast.stmt]) -> bool:
        # A plain loop: recursion through nested bodies stays in Python frames (at most 100
        # indentation levels), never in C calls such as any().
        for statement in statements:
            if self.cause(statement) is not None:
                return True
        return False

    def _decide_chain(self, first: ast.If) -> None:
        """An if/elif chain, decided from its last link up: no recursion along the chain."""
        chain = [first]
        while True:
            orelse = chain[-1].orelse
            if (
                len(orelse) != 1
                or not isinstance(orelse[0], ast.If)
                or id(orelse[0]) in self._known
            ):
                break
            chain.append(orelse[0])
        for node in reversed(chain):
            both = bool(node.orelse) and self.leaves(node.body) and self.leaves(node.orelse)
            self._known[id(node)] = "if" if both else None


def _regions(tree: ast.Module) -> list[_Region]:
    """Every region (U3) of reachable code, in source order."""
    leaving = _Leaving()
    found: list[_Region] = []
    pending = [tree.body]
    while pending:
        statements = pending.pop()
        for index, statement in enumerate(statements):
            cause = leaving.cause(statement)
            for nested in _lists(statement):
                if cause == "while" and nested is getattr(statement, "orelse", None):
                    continue  # the else of an endless loop never runs
                pending.append(nested)
            if cause is not None:
                rest = statements[index + 1 :]
                if rest:
                    found.append(_Region(rest, cause, statement.lineno))
                break  # the rest of this list never runs: nothing in it is analyzed
    return sorted(found, key=lambda region: _start(region.statements[0]))


def _start(statement: ast.stmt) -> tuple[int, int]:
    """Where a statement starts: its first decorator, for a decorated def or class."""
    decorators = getattr(statement, "decorator_list", None)
    node = decorators[0] if decorators else statement
    return node.lineno, node.col_offset  # type: ignore[attr-defined]


def _empty(path: str, status: OutlineStatus) -> UnreachableCodeResponse:
    return UnreachableCodeResponse(
        path=path,
        status=status,
        regions=[],
        summary=UnreachableCodeSummary(regions=0, statements=0),
        truncated=False,
    )


def analyze_unreachable_code(root: Path, row: Any) -> UnreachableCodeResponse:
    """Unreachable code of the Python file ``row`` (a snapshot row: path, size_bytes, sha256).
    Raises FileContentError(409) when storage no longer holds that snapshot."""
    if row.size_bytes > MAX_CONTENT_BYTES:
        return _empty(row.path, "too_large")  # never opened
    try:
        data = _read_verified(root, row.path, row.sha256)
    except _Unavailable:
        raise FileContentError(409, REPOSITORY_CHANGED) from None
    text = _decode(data)
    if text is None:
        return _empty(row.path, "binary")
    if len(text.encode("utf-8")) > MAX_OUTLINE_BYTES:
        return _empty(row.path, "too_large")  # never parsed
    try:
        # A syntax tree only; nothing is compiled or run. Python's own line endings (\r\n, \r)
        # are newlines to the parser, and only its line numbers are reported.
        tree = ast.parse(text)
    except _PARSE_ERRORS:
        return _empty(row.path, "syntax_error")
    found = _regions(tree)
    entries = [
        UnreachableRegion(
            line=_start(region.statements[0])[0],
            end_line=region.statements[-1].end_lineno or region.statements[-1].lineno,
            statements=len(region.statements),
            cause=region.cause,
            cause_line=region.cause_line,
        )
        for region in found
    ]
    return UnreachableCodeResponse(
        path=row.path,
        status="parsed",
        regions=entries[:MAX_UNREACHABLE_REGIONS],
        summary=UnreachableCodeSummary(
            regions=len(entries), statements=sum(len(region.statements) for region in found)
        ),
        truncated=len(entries) > MAX_UNREACHABLE_REGIONS,
    )


async def code_unreachable_code(
    db: AsyncSession, repository: Repository, path: str
) -> UnreachableCodeResponse:
    # One statement, one snapshot: the requested row with its stored size and SHA-256.
    row = (
        await db.execute(
            select(File.path, File.language, File.extension, File.size_bytes, File.sha256).where(
                File.repository_id == repository.id, File.path == path
            )
        )
    ).first()
    if row is None:
        raise FileContentError(404, FILE_NOT_FOUND)
    if not _is_python(row.language, row.extension):
        return _empty(row.path, "unsupported_language")  # never read
    root = repository_storage_dir(get_settings(), repository.id)
    return await run_in_threadpool(analyze_unreachable_code, root, row)
