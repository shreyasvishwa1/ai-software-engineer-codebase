"""Python unused imports: every import binding, in one Python file, whose bound name nothing in the
file ever reads, under Step 45's scope model and Step 58's occurrence model. Never guesses; an
import kept only for its side effects is listed too (its name is never read).

Scopes (I1): the module scope and def/async def scopes (Step 45's ``module`` and ``function``
scopes). Class bodies are never examined (an import there is a class attribute); lambdas and
comprehensions cannot hold import statements.

Candidate (I2): every Step 45 binding of the name in that scope is an ``import`` or
``from ... import`` binding, none of them ``from __future__ import``. Any other binding of the name
there removes it entirely: an assignment (plain, annotated or augmented), a def/class statement, a
parameter, a for/with/except target, a walrus target, a match capture, ``del``, or a global/nonlocal
declaration. Several imports of one name (a ``try``/``except ImportError`` fallback) are one
candidate; star imports bind nothing and are never listed.

Reads (I3): a Load ``ast.Name`` anywhere in the file that Step 58's ``_use`` resolves to the same
scope object and name: attribute bases (``os.path.join`` reads ``os``), calls, decorators, defaults,
annotations (``TYPE_CHECKING`` blocks included), f-strings, nested functions, comprehensions and
class bodies, before or after the import. PEP 695 type parameters are never reads (Step 55's
``_type_parameter``, applied by ``_use``).

Re-exports (I4), never listed or counted: a redundant alias (``import a as a``,
``from m import a as a``) and, in the module scope only, a name listed in a literal ``__all__``:
every module binding of ``__all__`` is ``=``, ``+=`` or annotated ``=`` with a list or tuple of string
constants (NFKC-normalized, as Python normalizes names).

Unsupported (I5), counted but never listed:

* (a) the module scope of a package or stub interface (``__init__.py``, any ``.pyi``);
* (b) the module scope when ``__all__`` is not literal (any other binding of it, a read such as
  ``__all__.extend(...)``, or a global declaration of it);
* (c) dynamic access: for the module scope, a load of ``globals``, ``locals``, ``vars``, ``eval`` or
  ``exec`` anywhere in the file; for a function, Step 60's rule (a load of ``locals``, ``vars``,
  ``eval`` or ``exec`` lexically inside it, nested scopes included);
* (d) the name is an identifier inside a string annotation evaluated in the scope or in a scope
  nested there (Step 60's rule). Ordinary strings never count.

Declared names (I6): a name declared global or nonlocal anywhere in the file is never listed or
counted (Step 58's rule, evaluated once per file).

Entries (I7): one per import of a listed binding, at its bound name (the name after ``as`` when
aliased, otherwise the first name: ``a`` of ``import a.b.c``), in source order. At most
MAX_UNUSED_IMPORTS are returned; the summary counts them all.

Out of scope by design: ``# noqa`` comments, type comments, doctests, use by other files
(re-exports consumed elsewhere), ``importlib``/``sys.modules`` access and module ``__getattr__``.

Snapshot: one SELECT returns the requested row and its SHA-256; the file is read once through
Step 21's verified reader, so any change answers 409. No other file is read. Parsing is
``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode, imported or executed.
Nothing is stored, cached or logged.
"""

import ast
import tokenize
import unicodedata
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_outline import OutlineStatus
from app.schemas.code_unused_imports import (
    UnusedImport,
    UnusedImportsResponse,
    UnusedImportsSummary,
)

# Steps 21, 37, 41, 45, 55, 58 and 60's readers, rules, scope model and helpers, all reused
# unchanged (Step 55's _type_parameter through Step 58's _use, which applies it with these spans).
from app.services.code_definition import (
    Binding,
    NameToken,
    ScopeTable,
    _cap,
    _normalize,
    _within,
    collect_scopes,
    name_tokens,
)
from app.services.code_importers import REPOSITORY_CHANGED
from app.services.code_occurrences import _use
from app.services.code_outline import _PARSE_ERRORS, MAX_OUTLINE_BYTES, _is_python
from app.services.code_unbound_names import _generic_spans
from app.services.code_unused_variables import (
    DYNAMIC_NAMES,
    Variable,
    _annotation_mentions,
    _chain,
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

MAX_UNUSED_IMPORTS = 500  # entries returned, the first in source order

UNUSED_IMPORTS_LIMIT = Limit("unused-imports-user", 120, 600)

# Module-scope names are also reachable through globals() from anywhere in the file (I5c).
MODULE_DYNAMIC_NAMES = DYNAMIC_NAMES | {"globals"}

_EXAMINED = frozenset({"module", "function"})  # I1
_IMPORT_KINDS = frozenset({"import", "import_from"})


def _future(binding: Binding) -> bool:
    """A ``from __future__ import ...`` binding: a compiler directive, never an unused import."""
    node = binding.node
    return isinstance(node, ast.ImportFrom) and node.module == "__future__" and node.level == 0


def _redundant(binding: Binding) -> bool:
    """A redundant alias (``import a as a``, ``from m import a as a``): an explicit re-export."""
    return binding.alias is not None and binding.alias.asname == binding.alias.name


def _strings(value: ast.expr | None) -> list[str] | None:
    """The strings of a list or tuple display of string constants (NFKC), otherwise None."""
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    strings = []
    for element in value.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        strings.append(unicodedata.normalize("NFKC", element.value))
    return strings


def _literal_all(tree: ast.Module, table: ScopeTable) -> tuple[set[int], set[str]]:
    """The module-scope ``__all__`` targets bound in an approved literal form (I4), by id, with the
    names they list: ``=``, ``+=`` or annotated ``=`` with a list or tuple of string constants."""
    targets: set[int] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if table.scope_of.get(id(node)) is not table.module:
            continue
        pairs: list[tuple[ast.expr, ast.expr | None]]
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            pairs = [(node.target, node.value)]
        elif isinstance(node, ast.AnnAssign):
            pairs = [(node.target, node.value)]
        else:
            continue
        for target, value in pairs:
            strings = _strings(value)
            if isinstance(target, ast.Name) and target.id == "__all__" and strings is not None:
                targets.add(id(target))
                names.update(strings)
    return targets, names


def _dynamic_scopes(table: ScopeTable) -> set[int]:
    """Step 60's dynamic-function rule: the scopes evaluating a load of ``locals``, ``vars``,
    ``eval`` or ``exec``, with every scope enclosing them."""
    dynamic: set[int] = set()
    for node in table.names.values():
        if isinstance(node.ctx, ast.Load) and node.id in DYNAMIC_NAMES:
            dynamic.update(id(scope) for scope in _chain(table.scope_of[id(node)]))
    return dynamic


def _bound_token(alias: ast.alias, lines: dict[int, list[NameToken]]) -> NameToken:
    """The NAME token of the name an import binds (I7): the one after ``as`` when aliased,
    otherwise the first (``a`` of ``import a.b.c``)."""
    end = alias.end_lineno if alias.end_lineno is not None else alias.lineno
    inside = [
        token
        for line in range(alias.lineno, end + 1)
        for token in lines.get(line, [])
        if _within(token, alias)
    ]
    return inside[-1] if any(token.text == "as" for token in inside) else inside[0]


def _empty(path: str, status: OutlineStatus) -> UnusedImportsResponse:
    return UnusedImportsResponse(
        path=path,
        status=status,
        imports=[],
        summary=UnusedImportsSummary(unused=0, distinct=0, unsupported=0),
        truncated=False,
    )


def analyze_unused_imports(root: Path, row: Any) -> UnusedImportsResponse:
    """Unused imports of the Python file ``row`` (a snapshot row: path, size_bytes, sha256).
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
    mentions = _annotation_mentions(tree, table)
    all_targets, exported = _literal_all(tree, table)
    dynamic = _dynamic_scopes(table)
    module_dynamic = any(
        isinstance(node.ctx, ast.Load) and node.id in MODULE_DYNAMIC_NAMES
        for node in table.names.values()
    )

    # Every scope once, and the names any of them declares global or nonlocal (I6).
    scopes = list({id(scope): scope for scope in table.scope_of.values()}.values())
    declared = {
        name
        for scope in scopes
        for name, bindings in scope.bindings.items()
        if any(binding.kind == "declared" for binding in bindings)
    }

    # Every read of every binding (I3), and the NAME tokens by line for the bound names (I7).
    read: set[Variable] = set()
    lines: dict[int, list[NameToken]] = {}
    for token in tokens:
        lines.setdefault(token.line, []).append(token)
        use = _use(token, table, {}, spans)
        if use is not None and use.scope is not None and use.kind == "read":
            read.add((id(use.scope), use.name))

    module = table.module
    literal = (
        "__all__" not in declared
        and (id(module), "__all__") not in read
        and all(id(binding.node) in all_targets for binding in module.bindings.get("__all__", []))
    )
    interface = row.path.endswith(".pyi") or row.path.rsplit("/", 1)[-1] == "__init__.py"

    listed: list[tuple[str, list[Binding]]] = []
    unsupported = 0
    for scope in scopes:
        if scope.kind not in _EXAMINED:
            continue  # I1
        in_module = scope is module
        for name, bindings in scope.bindings.items():
            if not all(binding.kind in _IMPORT_KINDS for binding in bindings):
                continue  # I2: some other binding form
            if any(_future(binding) for binding in bindings):
                continue  # I2: a compiler directive
            if (id(scope), name) in read or name in declared:
                continue  # I3, I6
            if any(_redundant(binding) for binding in bindings) or (
                in_module and literal and name in exported
            ):
                continue  # I4: an explicit re-export
            if (
                (in_module and (interface or not literal or module_dynamic))
                or (not in_module and id(scope) in dynamic)
                or name in mentions.get(id(scope), ())
            ):
                unsupported += 1  # I5
                continue
            listed.append((name, bindings))

    found: list[tuple[NameToken, str]] = []
    for name, bindings in listed:
        for binding in bindings:
            assert binding.alias is not None  # every import binding carries its alias
            found.append((_bound_token(binding.alias, lines), name))
    found.sort(key=lambda item: (item[0].line, item[0].column))
    entries = [
        UnusedImport(line=token.line, column=token.column + 1, name=_cap(name))
        for token, name in found
    ]
    return UnusedImportsResponse(
        path=row.path,
        status="parsed",
        imports=entries[:MAX_UNUSED_IMPORTS],
        summary=UnusedImportsSummary(
            unused=len(entries), distinct=len(listed), unsupported=unsupported
        ),
        truncated=len(entries) > MAX_UNUSED_IMPORTS,
    )


async def code_unused_imports(
    db: AsyncSession, repository: Repository, path: str
) -> UnusedImportsResponse:
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
    return await run_in_threadpool(analyze_unused_imports, root, row)
