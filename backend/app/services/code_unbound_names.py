"""Statically unbound names of one Python file: every identifier load with no statically known
binding. Never guesses; this is not a runtime NameError detector.

Candidate: an ``ast.Name`` in Load context, at its NAME token: the exact position Step 45's
go-to-definition resolves. Stores, deletions, attribute names, keyword-argument names,
definition and parameter names, strings and comments are never candidates.

Classification, in this order (first match wins):

* R0 bound: Step 45's ``resolving_scope`` finds a binding (class scopes are visible only to
  their own body; ``global`` and ``nonlocal`` declarations bind). A name bound by an import
  is bound wherever the import points, even when Step 45's go-to-definition answers
  ``not_found`` because the target lies outside the repository. Bindings are never followed.
* R1 builtin: the name is in Step 45's ``BUILTIN_NAMES`` (which already holds ``__name__``,
  ``__doc__``, ``__package__``, ``__loader__`` and ``__spec__``).
* E2 PEP 695 type parameter: the name is a type parameter of an enclosing generic
  ``def``/``async def``/``class``/``type`` statement and the load lies within that statement
  (from its ``def``/``class``/``type`` keyword, so never in its decorators). Step 45 does not
  model PEP 695 scopes. Names inside the type-parameter list itself are never visited by
  Step 45, so they are never candidates.
* E1 interpreter-provided (counted as ``implicit``): exactly ``__file__``, ``__cached__``,
  ``__builtins__`` and ``__annotations__`` in any scope; ``__module__`` and ``__qualname__``
  only in a class body (Step 45 evaluates a class-body comprehension's outermost iterable
  there too); ``__class__`` only where, walking up from the load's scope, a function or
  lambda scope comes before an enclosing class scope (comprehensions do not count), as in
  CPython 3.12. Any other dunder-looking name follows the ordinary rules.
* R2 star import (counted as ``unsupported``): the module has a star import, so the name may
  come from it (Step 45 answers ``unsupported``).
* L listed: no statically known binding.

Out of scope by design: control flow, definite assignment and path sensitivity (a name used
before its assignment, defined conditionally or in a try/except is bound), name mangling,
import resolution, dynamic globals (``exec``/``globals()`` definitions are listed) and runtime
mutation.

Snapshot: one SELECT returns the requested row and its SHA-256; the file is read once through
Step 21's verified reader, so any change answers 409. No other file is read. Parsing is
``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode, imported or executed.
Nothing is stored, cached or logged.
"""

import ast
import tokenize
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_outline import OutlineStatus
from app.schemas.code_unbound_names import UnboundName, UnboundNamesResponse, UnboundNamesSummary

# Step 45's scope model and positions, Steps 21, 37 and 41's readers, rules and limits, all
# reused unchanged.
from app.services.code_definition import (
    BUILTIN_NAMES,
    Scope,
    _cap,
    _module_scope,
    _normalize,
    collect_scopes,
    name_tokens,
    resolving_scope,
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

MAX_UNBOUND_NAMES = 500  # occurrences returned, the first in source order
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
UNBOUND_NAMES_LIMIT = Limit("unbound-names-user", 120, 600)

# E1: interpreter-provided names that Step 45's BUILTIN_NAMES does not cover.
ANY_SCOPE_NAMES = frozenset({"__file__", "__cached__", "__builtins__", "__annotations__"})
CLASS_BODY_NAMES = frozenset({"__module__", "__qualname__"})
CLASS_CELL_NAME = "__class__"

Position = tuple[int, int]  # (line, UTF-8 column): the unit of AST positions


def _generic_spans(tree: ast.Module) -> list[tuple[frozenset[str], Position, Position]]:
    """(type parameter names, start, end) of every PEP 695 generic statement."""
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.TypeAlias)):
            if node.type_params and node.end_lineno is not None and node.end_col_offset is not None:
                names = frozenset(
                    param.name
                    for param in node.type_params
                    if isinstance(param, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple))
                )
                start = (node.lineno, node.col_offset)  # the def/class/type keyword
                spans.append((names, start, (node.end_lineno, node.end_col_offset)))
    return spans


def _type_parameter(node: ast.Name, spans: list[tuple[frozenset[str], Position, Position]]) -> bool:
    """E2: ``node`` names a type parameter of a generic statement that contains it."""
    position = (node.lineno, node.col_offset)
    return any(node.id in names and start <= position < end for names, start, end in spans)


def _implicit(name: str, scope: Scope) -> bool:
    """E1: an interpreter-provided name, in a scope where Python provides it."""
    if name in ANY_SCOPE_NAMES:
        return True
    if name in CLASS_BODY_NAMES:
        return scope.kind == "class"
    if name == CLASS_CELL_NAME:
        function_seen = False
        current: Scope | None = scope
        while current is not None:
            if current.kind in ("function", "lambda"):
                function_seen = True
            elif current.kind == "class" and function_seen:
                return True
            current = current.parent
    return False


def _empty(path: str, status: OutlineStatus) -> UnboundNamesResponse:
    return UnboundNamesResponse(
        path=path,
        status=status,
        names=[],
        summary=UnboundNamesSummary(unbound=0, distinct=0, unsupported=0, implicit=0),
        truncated=False,
    )


def analyze_unbound_names(root: Path, row: Any) -> UnboundNamesResponse:
    """Statically unbound names of the Python file ``row`` (a snapshot row: path, size_bytes,
    sha256). Raises FileContentError(409) when storage no longer holds that snapshot."""
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

    listed: list[UnboundName] = []
    identifiers: set[str] = set()
    unsupported = implicit = 0
    for token in tokens:
        node = table.names.get((token.line, token.start_byte))
        if node is None or not isinstance(node.ctx, ast.Load):
            continue  # not an identifier load
        scope = table.scope_of[id(node)]
        if resolving_scope(scope, node.id) is not None:
            continue  # R0: bound (wherever the binding leads)
        if node.id in BUILTIN_NAMES:
            continue  # R1
        if _type_parameter(node, spans):
            continue  # E2
        if _implicit(node.id, scope):
            implicit += 1  # E1
            continue
        if _module_scope(scope).star_import:
            unsupported += 1  # R2: it may come from the star import
            continue
        identifiers.add(node.id)  # L
        listed.append(UnboundName(line=token.line, column=token.column + 1, name=_cap(node.id)))

    return UnboundNamesResponse(
        path=row.path,
        status="parsed",
        names=listed[:MAX_UNBOUND_NAMES],
        summary=UnboundNamesSummary(
            unbound=len(listed),
            distinct=len(identifiers),
            unsupported=unsupported,
            implicit=implicit,
        ),
        truncated=len(listed) > MAX_UNBOUND_NAMES,
    )


async def code_unbound_names(
    db: AsyncSession, repository: Repository, path: str
) -> UnboundNamesResponse:
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
    return await run_in_threadpool(analyze_unbound_names, root, row)
