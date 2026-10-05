"""Python unused local variables: every assignment, in one Python file, to a def/async def local
variable that nothing in the file ever reads, under Step 45's scope model and Step 58's
occurrence model. Never guesses; this is not a dead-store (data-flow) analysis.

Variable (U1): a name bound in a def/async def scope (Step 45's ``function`` scopes), identified
by that scope object and the name, as Step 58 identifies a binding. Module, class, lambda and
comprehension scopes are never examined.

Candidate (U2, U3): every Step 45 binding of the name in that scope is a plain assignment, an
``ast.Name`` that is a whole target of ``=`` (``x = ...``; each name of ``a = x = ...``) or the
target of an annotated assignment with a value (``x: T = ...``). Any other binding of the name
there removes the variable entirely: a parameter, a def/class statement, an import, a global or
nonlocal declaration, an augmented assignment, a for/with target, an element of an unpacking
target, a walrus target, an except-handler name, a match capture, ``del``, an annotation without
a value or a ``type`` alias.

Reads (U4): a Load ``ast.Name`` anywhere in the file that Step 58's ``_use`` resolves to the same
scope object and name: in the function itself, in nested functions, lambdas, comprehensions and
class bodies, and in the decorators, defaults and annotations evaluated there. PEP 695 type
parameters are never reads (Step 55's ``_type_parameter``, applied by ``_use``).

Listed (U5): a candidate without reads, unless

* U5a its name starts with "_" (intentionally unused): never listed or counted;
* U5b its name is declared global or nonlocal anywhere in the file (Step 58's rule, evaluated
  once per file instead of through ``_supported``, which rescans the file on every call): never
  listed or counted;
* U5c its function is dynamic, a load of ``locals``, ``vars``, ``eval`` or ``exec`` appearing
  anywhere inside it (nested scopes included): counted as ``unsupported``;
* U5d its name is an identifier inside a string constant within a parameter, return or
  annotated-assignment annotation evaluated in its function or in a scope nested there: counted
  as ``unsupported``. Any other string (a docstring, an ordinary literal) never counts.

Entries (U6, U7): one per assignment site of a listed variable (a NAME token that ``_use``
resolves to a write of it), in source order. At most MAX_UNUSED_VARIABLES are returned; the
summary counts them all.

Out of scope by design: control flow (``x = 1; x = 2; return x`` lists nothing, although the first
assignment is a dead store), unused parameters, imports, loop variables and except-handler names,
class and module attributes, frame introspection (``sys._getframe().f_locals``), type comments
and ``# noqa``.

Snapshot: one SELECT returns the requested row and its SHA-256; the file is read once through
Step 21's verified reader, so any change answers 409. No other file is read. Parsing is
``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode, imported or executed.
Nothing is stored, cached or logged.
"""

import ast
import tokenize
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_outline import OutlineStatus
from app.schemas.code_unused_variables import (
    UnusedVariable,
    UnusedVariablesResponse,
    UnusedVariablesSummary,
)

# Steps 21, 37, 41, 45, 55 and 58's readers, rules, scope model and helpers, all reused unchanged
# (Step 55's _type_parameter through Step 58's _use, which applies it with these spans).
from app.services.code_definition import (
    _FUNCTIONS,
    NameToken,
    Scope,
    ScopeTable,
    _arguments,
    _cap,
    _normalize,
    collect_scopes,
    name_tokens,
)
from app.services.code_importers import REPOSITORY_CHANGED
from app.services.code_occurrences import _use
from app.services.code_outline import _PARSE_ERRORS, MAX_OUTLINE_BYTES, _is_python
from app.services.code_unbound_names import _generic_spans
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

MAX_UNUSED_VARIABLES = 500  # entries returned, the first in source order

UNUSED_VARIABLES_LIMIT = Limit("unused-variables-user", 120, 600)

# The names through which code reaches a function's locals without naming them (U5c).
DYNAMIC_NAMES = frozenset({"locals", "vars", "eval", "exec"})

Variable = tuple[int, str]  # (id of the function scope, name): Step 58's binding identity


def _chain(scope: Scope | None) -> Iterator[Scope]:
    """``scope`` and every scope enclosing it."""
    while scope is not None:
        yield scope
        scope = scope.parent


def _plain_targets(tree: ast.Module) -> set[int]:
    """ids of the plain assignment targets (U2): every ``ast.Name`` that is a whole target of an
    ``=`` assignment or the target of an annotated assignment with a value."""
    plain: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            plain.update(id(target) for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None and isinstance(node.target, ast.Name):
                plain.add(id(node.target))
    return plain


def _identifiers(text: str) -> set[str]:
    """Every identifier in ``text``: the maximal runs of characters that may continue a Python
    identifier, after NFKC normalization (as Python normalizes names)."""
    text = unicodedata.normalize("NFKC", text)
    return set("".join(ch if ("_" + ch).isidentifier() else " " for ch in text).split())


def _annotation_mentions(tree: ast.Module, table: ScopeTable) -> dict[int, set[str]]:
    """Identifiers inside string annotations (U5d), by the id of every scope containing the scope
    that evaluates the annotation (that scope included). Annotations are parameter, return and
    annotated-assignment annotations; no other string ever counts."""
    annotations: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, _FUNCTIONS):
            arguments = _arguments(node.args)
            annotations += [arg.annotation for arg in arguments if arg.annotation is not None]
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    mentions: dict[int, set[str]] = {}
    for annotation in annotations:
        words: set[str] = set()
        for node in ast.walk(annotation):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                words |= _identifiers(node.value)
        for scope in _chain(table.scope_of.get(id(annotation))):
            mentions.setdefault(id(scope), set()).update(words)
    return mentions


def _empty(path: str, status: OutlineStatus) -> UnusedVariablesResponse:
    return UnusedVariablesResponse(
        path=path,
        status=status,
        variables=[],
        summary=UnusedVariablesSummary(unused=0, distinct=0, unsupported=0),
        truncated=False,
    )


def analyze_unused_variables(root: Path, row: Any) -> UnusedVariablesResponse:
    """Unused local variables of the Python file ``row`` (a snapshot row: path, size_bytes,
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
    plain = _plain_targets(tree)
    mentions = _annotation_mentions(tree, table)

    # Every scope once, and the names any of them declares global or nonlocal (U5b).
    scopes = list({id(scope): scope for scope in table.scope_of.values()}.values())
    declared = {
        name
        for scope in scopes
        for name, bindings in scope.bindings.items()
        if any(binding.kind == "declared" for binding in bindings)
    }
    # The scopes evaluating a dynamic name, with every scope enclosing them (U5c).
    dynamic: set[int] = set()
    for node in table.names.values():
        if isinstance(node.ctx, ast.Load) and node.id in DYNAMIC_NAMES:
            dynamic.update(id(scope) for scope in _chain(table.scope_of[id(node)]))

    # Every read and write of every binding, in source order (U4, U6).
    read: set[Variable] = set()
    writes: list[tuple[NameToken, Variable]] = []
    for token in tokens:
        use = _use(token, table, {}, spans)
        if use is None or use.scope is None:
            continue
        variable = (id(use.scope), use.name)
        if use.kind == "read":
            read.add(variable)
        elif use.kind == "write":
            writes.append((token, variable))

    listed: set[Variable] = set()
    unsupported = 0
    for scope in scopes:
        if scope.kind != "function":
            continue  # U1
        for name, bindings in scope.bindings.items():
            variable = (id(scope), name)
            if not all(id(binding.node) in plain for binding in bindings):
                continue  # U3: some other binding form
            if variable in read or name.startswith("_") or name in declared:
                continue  # U4, U5a, U5b
            if id(scope) in dynamic or name in mentions.get(id(scope), ()):
                unsupported += 1  # U5c, U5d
                continue
            listed.add(variable)

    entries = [
        UnusedVariable(line=token.line, column=token.column + 1, name=_cap(variable[1]))
        for token, variable in writes
        if variable in listed
    ]
    return UnusedVariablesResponse(
        path=row.path,
        status="parsed",
        variables=entries[:MAX_UNUSED_VARIABLES],
        summary=UnusedVariablesSummary(
            unused=len(entries), distinct=len(listed), unsupported=unsupported
        ),
        truncated=len(entries) > MAX_UNUSED_VARIABLES,
    )


async def code_unused_variables(
    db: AsyncSession, repository: Repository, path: str
) -> UnusedVariablesResponse:
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
    return await run_in_threadpool(analyze_unused_variables, root, row)
