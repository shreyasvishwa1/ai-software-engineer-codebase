"""Python in-file name occurrences: every occurrence, in one Python file, of the binding that the
identifier at a position refers to, under Step 45's scope model. Never guesses.

Identifier: the NAME token covering the 1-based line and code-point column (Step 45's
``token_at``). Keywords, whitespace, punctuation, strings, comments and positions past the line
or file are ``no_identifier``; so is a soft keyword (``match``, ``case``, ``type``, ``_``) used
as a keyword.

Binding: an ``ast.Name`` (load, store or delete) resolves with Step 45's ``resolving_scope``
from the scope that evaluates it, as Python looks names up (nested functions see enclosing
function bindings, class bodies are visible only to themselves, comprehensions have their own
scope, defaults and decorators belong to the enclosing scope, a walrus binds outside the
comprehension). No binding is ``not_bound``: a builtin, a name bound nowhere in the file, or one
a star import may provide. Three binding sites without an ``ast.Name`` are occurrences too: a
parameter (any kind, lambdas included) in its function's own scope, a def/class name in the
scope enclosing the statement, and an except-handler name. Every other identifier is
``unsupported``: attribute names, keyword-argument names, import statements, ``global`` and
``nonlocal`` statements, match patterns and PEP 695 type parameters, with every reference to
one (Step 45 does not model them).

Unsupported bindings (v1, conservative): a name declared ``global`` or ``nonlocal`` anywhere in
the file (no alias tracking), and a name the binding's own scope binds with an import or a
match capture.

Occurrences: every NAME token of the file that resolves to the same binding (the same scope
object and name), in source order, as ``read`` (a load), ``write`` (a store: assignment,
augmented and annotated assignment, for/with/comprehension targets, walrus, except-handler
name), ``delete``, ``parameter`` or ``definition`` (a def/class name). Names reached only
dynamically (``locals()``, ``exec``) or written inside string annotations are not occurrences.
At most MAX_OCCURRENCES are returned, the first in source order; the summary counts them all.

Snapshot: one SELECT returns the requested row and its SHA-256; the file is read once through
Step 21's verified reader, so any change answers 409. No other file is read. Parsing is
``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode, imported or executed.
Nothing is stored, cached or logged.
"""

import ast
import keyword
import tokenize
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_definition import DefinitionReference
from app.schemas.code_occurrences import (
    Occurrence,
    OccurrenceKind,
    OccurrencesResponse,
    OccurrencesStatus,
    OccurrencesSummary,
)

# Steps 21, 37, 41, 45 and 55's readers, rules, scope model and helpers, all reused unchanged.
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
    resolving_scope,
    token_at,
)
from app.services.code_importers import REPOSITORY_CHANGED
from app.services.code_outline import _PARSE_ERRORS, MAX_OUTLINE_BYTES, _is_python
from app.services.code_unbound_names import _generic_spans, _type_parameter
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

MAX_OCCURRENCES = 1000  # occurrences returned, the first in source order
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
OCCURRENCES_LIMIT = Limit("occurrences-user", 120, 600)

_CONTEXT_KINDS: dict[type[ast.expr_context], OccurrenceKind] = {
    ast.Load: "read",
    ast.Store: "write",
    ast.Del: "delete",
}
_IMPORT_KINDS = frozenset({"import", "import_from"})
_CAPTURES = (ast.MatchAs, ast.MatchStar, ast.MatchMapping)

Position = tuple[int, int]  # (line, UTF-8 column): the unit of AST positions


@dataclass
class _Use:
    """One identifier occurrence: the scope holding its binding (None: bound nowhere in the
    file), the identifier as Python sees it (NFKC) and the occurrence kind."""

    scope: Scope | None
    name: str
    kind: OccurrenceKind


def _sites(
    tree: ast.Module, table: ScopeTable, tokens: Sequence[NameToken]
) -> dict[Position, _Use]:
    """Binding sites that are not ``ast.Name`` nodes, by their NAME token's (line, start byte):
    parameters, def/class names and except-handler names."""
    index = {(token.line, token.start_byte): i for i, token in enumerate(tokens)}

    def name_after(node: ast.stmt | ast.excepthandler, word: str) -> NameToken | None:
        """The NAME token right after the first ``word`` token from the node's own start."""
        position = index.get((node.lineno, node.col_offset))
        if position is None:
            return None
        while position + 1 < len(tokens):
            if tokens[position].text == word:
                return tokens[position + 1]
            position += 1
        return None

    sites: dict[Position, _Use] = {}
    for node in ast.walk(tree):
        if isinstance(node, (*_FUNCTIONS, ast.Lambda)):
            # The function's own scope evaluates its body (None: Step 45 never visits it).
            body: ast.AST = node.body if isinstance(node, ast.Lambda) else node.body[0]
            inner = table.scope_of.get(id(body))
            if inner is not None:
                for arg in _arguments(node.args):
                    sites[(arg.lineno, arg.col_offset)] = _Use(inner, arg.arg, "parameter")
        if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            name = name_after(node, "class" if isinstance(node, ast.ClassDef) else "def")
            outer = table.scope_of.get(id(node))
            if name is not None and outer is not None:
                sites[(name.line, name.start_byte)] = _Use(outer, node.name, "definition")
        elif isinstance(node, ast.ExceptHandler) and node.name:
            name = name_after(node, "as")
            outer = table.scope_of.get(id(node))
            if name is not None and outer is not None:
                sites[(name.line, name.start_byte)] = _Use(outer, node.name, "write")
    return sites


def _use(
    token: NameToken,
    table: ScopeTable,
    sites: dict[Position, _Use],
    spans: list[tuple[frozenset[str], Position, Position]],
) -> _Use | None:
    """The occurrence ``token`` is, or None when it is not a name occurrence at all (attribute
    and keyword-argument names, import, global/nonlocal and match statements, type parameters).
    """
    node = table.names.get((token.line, token.start_byte))
    if node is None:
        return sites.get((token.line, token.start_byte))
    if _type_parameter(node, spans):
        return None  # a PEP 695 type parameter: outside v1, never another binding's occurrence
    scope = resolving_scope(table.scope_of[id(node)], node.id)
    return _Use(scope, node.id, _CONTEXT_KINDS[type(node.ctx)])


def _supported(scope: Scope, name: str, table: ScopeTable) -> bool:
    """v1's conservative rules: no global/nonlocal declaration of ``name`` anywhere in the file,
    and no import or match-capture binding of it in its own scope."""
    scopes = {id(each): each for each in table.scope_of.values()}.values()
    if any(b.kind == "declared" for each in scopes for b in each.bindings.get(name, [])):
        return False
    return not any(
        b.kind in _IMPORT_KINDS or isinstance(b.node, _CAPTURES) for b in scope.bindings[name]
    )


def _response(
    status: OccurrencesStatus, reference: DefinitionReference | None = None
) -> OccurrencesResponse:
    return OccurrencesResponse(
        status=status,
        reference=reference,
        scope=None,
        occurrences=[],
        summary=OccurrencesSummary(occurrences=0, reads=0, writes=0, deletes=0, bindings=0),
        truncated=False,
    )


def analyze_occurrences(root: Path, row: Any, line: int, column: int) -> OccurrencesResponse:
    """Occurrences of the binding at (line, column) of the Python file ``row`` (a snapshot row:
    path, size_bytes, sha256). Raises FileContentError(409) when storage no longer holds it."""
    if row.size_bytes > MAX_CONTENT_BYTES:
        return _response("too_large")  # never opened
    try:
        data = _read_verified(root, row.path, row.sha256)
    except _Unavailable:
        raise FileContentError(409, REPOSITORY_CHANGED) from None
    text = _decode(data)
    if text is None:
        return _response("binary")
    if len(text.encode("utf-8")) > MAX_OUTLINE_BYTES:
        return _response("too_large")  # never parsed
    source = _normalize(text)
    try:
        tree = ast.parse(source)  # builds a syntax tree only; nothing is compiled or run
        tokens = name_tokens(source)
    except (*_PARSE_ERRORS, tokenize.TokenError):
        return _response("syntax_error")
    selected = token_at(tokens, line, column)
    if selected is None or keyword.iskeyword(selected.text):
        return _response("no_identifier")

    table = collect_scopes(tree)
    spans = _generic_spans(tree)
    sites = _sites(tree, table, tokens)
    target = _use(selected, table, sites, spans)
    if target is None and keyword.issoftkeyword(selected.text):
        return _response("no_identifier")  # e.g. "match" or "case" used as a keyword
    identifier = target.name if target is not None else unicodedata.normalize("NFKC", selected.text)
    reference = DefinitionReference(
        path=row.path, line=selected.line, column=selected.column + 1, name=_cap(identifier)
    )
    if target is None:
        return _response("unsupported", reference)
    scope = target.scope
    if scope is None:
        return _response("not_bound", reference)
    if not _supported(scope, target.name, table):
        return _response("unsupported", reference)

    found: list[Occurrence] = []
    for token in tokens:  # source order
        use = _use(token, table, sites, spans)
        if use is not None and use.scope is scope and use.name == target.name:
            found.append(Occurrence(line=token.line, column=token.column + 1, kind=use.kind))
    kinds = Counter(occurrence.kind for occurrence in found)
    return OccurrencesResponse(
        status="found",
        reference=reference,
        scope=scope.kind,
        occurrences=found[:MAX_OCCURRENCES],
        summary=OccurrencesSummary(
            occurrences=len(found),
            reads=kinds["read"],
            writes=kinds["write"],
            deletes=kinds["delete"],
            bindings=kinds["parameter"] + kinds["definition"],
        ),
        truncated=len(found) > MAX_OCCURRENCES,
    )


async def code_occurrences(
    db: AsyncSession, repository: Repository, path: str, line: int, column: int
) -> OccurrencesResponse:
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
        return _response("unsupported_language")  # never read
    root = repository_storage_dir(get_settings(), repository.id)
    return await run_in_threadpool(analyze_occurrences, root, row, line, column)
