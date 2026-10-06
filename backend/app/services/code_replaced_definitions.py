"""Python replaced definitions: every def, async def or class statement, in one Python file, whose
binding a later def, async def or class statement of the same name in the same statement list
replaces before anything can have read it, under Step 45's scope model and Step 58's occurrence
model. Never guesses; this is not a control-flow analysis.

Scopes (D1): the module, class and def/async def scopes holding the statements (Step 45's
``module``, ``class`` and ``function`` scopes); lambdas and comprehensions cannot hold statements.

Pair (D2): a def, async def or class statement S1 and the first later def, async def or class
statement S2 of the same statement list (a body, an ``else`` or ``finally`` list, an except
handler's or a match case's body) binding the same name, provided that no other Step 45 binding of
the name in that scope lies between them (an assignment, an import, a def or class in a nested
block, a loop or with target, ``del``, a walrus ...) and no ``return``, ``raise``, ``break`` or
``continue`` statement of that list lies between them (S2 might never run). Statements of different
lists (``if``/``else`` branches, a ``try`` body and its handlers) are never paired.

Reads (D3): a load of the name that Step 58's ``_use`` resolves to the same scope, anywhere in the
file before S2 binds: before S2's ``def``/``class`` keyword (S2's decorators included) or in what S2
evaluates first (a def's defaults and annotations; a class's bases, keywords and body). Loads inside
S1 itself never count: S1's code only runs once something has read S1. A pair with such a read is
never listed or counted: S1 may have been used, possibly by code that runs later than it is
written, which is why every earlier load counts.

Unsupported (D4), counted but never listed: S1 has decorators (a decorator may keep the function or
class alive, e.g. by registering it; ``typing.overload`` stubs are counted here), S1 is a class with
bases or keywords (creating it may run code that keeps it: ``__init_subclass__``, a metaclass), or
the scope has dynamic access (Step 62's rules: for the module scope a load of ``globals``,
``locals``, ``vars``, ``eval`` or ``exec`` anywhere in the file; for any other scope Step 60's rule,
such a load inside it, nested scopes included).

Excluded names (D5), never listed or counted: a name declared global or nonlocal anywhere in the
file (Step 58's rule, evaluated once per file) and ``_`` (a conventional throwaway name).

Entries (D6): one per listed S1, at the NAME token after its ``def``/``class`` keyword (1-based line
and code-point column), with its kind as Step 37 names definitions (``class``; ``method`` for a def
bound in a class scope; otherwise ``function``) and the position of S2's name. Source order; at
most MAX_REPLACED_DEFINITIONS are returned; the summary counts them all.

Out of scope by design: replacement by any other binding (``f = ...``, an import), definitions in
different blocks, reads through attributes or other modules (``self.f``, ``module.f``,
``sys.modules``), frame introspection, ``# noqa`` comments and unreachable code.

Snapshot: one SELECT returns the requested row and its SHA-256; the file is read once through
Step 21's verified reader, so any change answers 409. No other file is read. Parsing is
``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode, imported or executed.
Nothing is stored, cached or logged.
"""

import ast
import tokenize
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_outline import OutlineStatus
from app.schemas.code_replaced_definitions import (
    ReplacedDefinition,
    ReplacedDefinitionsResponse,
    ReplacedDefinitionsSummary,
)
from app.services.code_definition import (
    NameToken,
    Scope,
    _cap,
    _normalize,
    collect_scopes,
    name_tokens,
)
from app.services.code_importers import REPOSITORY_CHANGED
from app.services.code_occurrences import _use
from app.services.code_outline import _PARSE_ERRORS, MAX_OUTLINE_BYTES, _is_python
from app.services.code_unbound_names import _generic_spans
from app.services.code_unused_imports import MODULE_DYNAMIC_NAMES, _dynamic_scopes
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

MAX_REPLACED_DEFINITIONS = 500  # entries returned, the first in source order

REPLACED_DEFINITIONS_LIMIT = Limit("replaced-definitions-user", 120, 600)

Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
Kind = Literal["class", "function", "method"]
Position = tuple[int, int]  # (line, UTF-8 column): the unit of AST positions

_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_LISTS = ("body", "orelse", "finalbody")  # D2: the statement-list fields
_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)  # D2
_THROWAWAY = "_"  # D5


def _start(node: ast.AST) -> Position:
    return node.lineno, node.col_offset  # type: ignore[attr-defined]


def _end(node: ast.AST) -> Position:
    return node.end_lineno, node.end_col_offset  # type: ignore[attr-defined]


def _statement_lists(tree: ast.Module) -> Iterator[list[ast.stmt]]:
    """Every statement list of the file (D2): bodies, else and finally lists, handler and case
    bodies (a lambda's or conditional expression's ``body`` is an expression, never a list)."""
    for node in ast.walk(tree):
        for field in _LISTS:
            statements = getattr(node, field, None)
            if isinstance(statements, list) and statements and isinstance(statements[0], ast.stmt):
                yield statements


def _pairs(statements: list[ast.stmt]) -> Iterator[tuple[Definition, Definition, list[ast.stmt]]]:
    """Each def/class statement of the list with the first later one binding the same name, and
    the statements between them (D2)."""
    for index, first in enumerate(statements):
        if not isinstance(first, _DEFINITIONS):
            continue
        for later in range(index + 1, len(statements)):
            second = statements[later]
            if isinstance(second, _DEFINITIONS) and second.name == first.name:
                yield first, second, statements[index + 1 : later]
                break


def _binding_cut(definition: Definition) -> Position:
    """Where ``definition`` binds its name (D3): a def after its header (decorators, defaults and
    annotations come first, its body runs only when called), a class after its whole body."""
    if isinstance(definition, ast.ClassDef):
        return _end(definition)
    return _start(definition.body[0])


def _kind(definition: Definition, scope: Scope) -> Kind:
    if isinstance(definition, ast.ClassDef):
        return "class"
    return "method" if scope.kind == "class" else "function"


def _name_token(
    definition: Definition, tokens: Sequence[NameToken], index: dict[Position, int]
) -> NameToken:
    """The NAME token right after the ``def``/``class`` keyword (D6); the statement itself starts
    at that keyword or at ``async``."""
    word = "class" if isinstance(definition, ast.ClassDef) else "def"
    position = index[_start(definition)]
    while tokens[position].text != word:
        position += 1
    return tokens[position + 1]


def _empty(path: str, status: OutlineStatus) -> ReplacedDefinitionsResponse:
    return ReplacedDefinitionsResponse(
        path=path,
        status=status,
        definitions=[],
        summary=ReplacedDefinitionsSummary(replaced=0, distinct=0, unsupported=0),
        truncated=False,
    )


def analyze_replaced_definitions(root: Path, row: Any) -> ReplacedDefinitionsResponse:
    """Replaced definitions of the Python file ``row`` (a snapshot row: path, size_bytes, sha256).
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
    source = _normalize(text)
    try:
        tree = ast.parse(source)  # builds a syntax tree only; nothing is compiled or run
        tokens = name_tokens(source)
    except (*_PARSE_ERRORS, tokenize.TokenError):
        return _empty(row.path, "syntax_error")
    table = collect_scopes(tree)
    spans = _generic_spans(tree)
    dynamic = _dynamic_scopes(table)
    module_dynamic = any(
        isinstance(node.ctx, ast.Load) and node.id in MODULE_DYNAMIC_NAMES
        for node in table.names.values()
    )

    # The names any scope declares global or nonlocal (D5).
    scopes = {id(scope): scope for scope in table.scope_of.values()}.values()
    declared = {
        name
        for scope in scopes
        for name, bindings in scope.bindings.items()
        if any(binding.kind == "declared" for binding in bindings)
    }

    # Every read of every binding with its position (D3), and the NAME tokens by position (D6).
    reads: dict[tuple[int, str], list[Position]] = {}
    index: dict[Position, int] = {}
    for number, token in enumerate(tokens):
        index[(token.line, token.start_byte)] = number
        use = _use(token, table, {}, spans)
        if use is not None and use.scope is not None and use.kind == "read":
            reads.setdefault((id(use.scope), use.name), []).append((token.line, token.start_byte))

    listed: list[tuple[Definition, Definition, Scope]] = []
    unsupported = 0
    for statements in _statement_lists(tree):
        for first, second, between in _pairs(statements):
            name = first.name
            if name == _THROWAWAY or name in declared:
                continue  # D5
            if any(isinstance(statement, _TERMINATORS) for statement in between):
                continue  # D2: the second definition might never run
            scope = table.scope_of[id(first)]
            if any(
                _start(first) < _start(binding.node) < _start(second)
                for binding in scope.bindings[name]
                if binding.node is not first and binding.node is not second
            ):
                continue  # D2: another binding of the name in between
            cut = _binding_cut(second)
            if any(
                position < cut and not _start(first) <= position <= _end(first)
                for position in reads.get((id(scope), name), [])
            ):
                continue  # D3: possibly used
            if (
                first.decorator_list
                or (isinstance(first, ast.ClassDef) and (first.bases or first.keywords))
                or (module_dynamic if scope is table.module else id(scope) in dynamic)
            ):
                unsupported += 1  # D4
                continue
            listed.append((first, second, scope))

    found = sorted(
        (
            (_name_token(first, tokens, index), _name_token(second, tokens, index), first, scope)
            for first, second, scope in listed
        ),
        key=lambda item: (item[0].line, item[0].column),
    )
    entries = [
        ReplacedDefinition(
            line=token.line,
            column=token.column + 1,
            name=_cap(first.name),
            kind=_kind(first, scope),
            replaced_line=replacing.line,
            replaced_column=replacing.column + 1,
        )
        for token, replacing, first, scope in found
    ]
    return ReplacedDefinitionsResponse(
        path=row.path,
        status="parsed",
        definitions=entries[:MAX_REPLACED_DEFINITIONS],
        summary=ReplacedDefinitionsSummary(
            replaced=len(entries),
            distinct=len({(id(scope), first.name) for first, _, scope in listed}),
            unsupported=unsupported,
        ),
        truncated=len(entries) > MAX_REPLACED_DEFINITIONS,
    )


async def code_replaced_definitions(
    db: AsyncSession, repository: Repository, path: str
) -> ReplacedDefinitionsResponse:
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
    return await run_in_threadpool(analyze_replaced_definitions, root, row)
