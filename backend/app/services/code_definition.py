"""Static go-to-definition for one position in a Python file. Never guesses.

Position: the verified text is tokenized (``tokenize``, code-point columns); the NAME token
covering the 1-based line/column (first through last character) is the identifier.
Token columns are converted to UTF-8 byte offsets, the unit of AST ``col_offset``, and the
token is matched to an ``ast.Name`` starting there, an ``ast.Attribute`` whose attribute
ends there, or a name inside an import statement. Keywords, whitespace, punctuation,
strings, comments and positions past the line or file are ``no_identifier``; any other
identifier (a def/class name, a parameter, a keyword argument, ...) is ``unsupported``.

Scopes: a dedicated collector (iterative) records every binding of every lexical scope
(module, def, async def, class, lambda, comprehension) regardless of if/try/with/for/
while/match nesting, as Python does; walrus targets belong to the nearest
non-comprehension scope. This intentionally differs from Step 37's outline, which is
reused nowhere here and left unchanged. A name resolves in its own scope, then enclosing
function scopes (class scopes are skipped, as in Python), then the module. In the
resolving scope: a global/nonlocal declaration or any variable-like binding (assignment,
parameter, loop/with/except target, del, match capture, walrus) is ``unsupported``;
def/class bindings are definitions; import bindings are followed.

Imports: module paths come only from Step 39's ``ModuleIndex``/``_lookups`` (reused
unchanged: whole-segment matching, relative imports, packages, .pyi). ``from M import X``
yields the submodule M.X if it exists plus whatever M's top-level binding of X is (a
definition, or another import, followed up to MAX_IMPORT_HOPS import statements in all).
An attribute resolves only when its whole value chain is a module bound by a plain
``import``; ``self.x``, ``obj.x`` and ``Class.x`` are ``unsupported`` (no type inference).

Snapshot: one SELECT returns the target row and every Python row; the target and at most
MAX_MODULE_FILES module files are read through Step 21's verified reader against their
snapshot SHA-256, so any change answers 409 and results never mix snapshots. Parsing is
``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode, imported or executed.
Nothing is stored, cached across requests or logged.
"""

import ast
import builtins
import io
import keyword
import tokenize
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_definition import (
    DefinitionLocation,
    DefinitionReference,
    DefinitionResponse,
    DefinitionStatus,
)

# Steps 21, 37, 39 and 41's rules, resolver and readers, reused unchanged.
from app.services.code_importers import REPOSITORY_CHANGED
from app.services.code_imports import MAX_INDEXED_PATHS, ModuleIndex, _lookups, module_parts
from app.services.code_outline import (
    _PARSE_ERRORS,
    MAX_NAME_CHARS,
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

MAX_MODULE_FILES = 10  # module files read per request, besides the target
MAX_IMPORT_HOPS = 3  # import statements followed from the reference to a definition
MAX_DEFINITIONS = 20
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
DEFINITION_LIMIT = Limit("definition-user", 120, 600)

ScopeKind = Literal["module", "function", "class", "lambda", "comprehension"]
BindingKind = Literal["def", "class", "import", "import_from", "variable", "declared"]

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


# --- Lexical scopes ---------------------------------------------------------------------


@dataclass
class Binding:
    kind: BindingKind
    node: ast.AST
    alias: ast.alias | None = None


@dataclass
class Scope:
    kind: ScopeKind
    parent: "Scope | None"
    bindings: dict[str, list[Binding]] = field(default_factory=dict)
    star_import: bool = False

    def bind(self, name: str, binding: Binding) -> None:
        self.bindings.setdefault(name, []).append(binding)


@dataclass
class ScopeTable:
    module: Scope
    scope_of: dict[int, Scope]  # id(node) -> the scope in which the node is evaluated
    imports: list[ast.Import | ast.ImportFrom]
    names: dict[tuple[int, int], ast.Name]  # (line, start byte) -> Name
    attributes: dict[tuple[int, int], ast.Attribute]  # (line, end byte) -> Attribute


def _arguments(args: ast.arguments) -> list[ast.arg]:
    found = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return found + [a for a in (args.vararg, args.kwarg) if a is not None]


def _non_comprehension(scope: Scope) -> Scope:
    while scope.kind == "comprehension" and scope.parent is not None:
        scope = scope.parent
    return scope


def collect_scopes(tree: ast.Module) -> ScopeTable:
    """Every binding of every lexical scope, collected iteratively (no recursion)."""
    module = Scope("module", None)
    table = ScopeTable(module, {}, [], {}, {})
    stack: list[tuple[ast.AST, Scope]] = [(node, module) for node in reversed(tree.body)]
    while stack:
        node, scope = stack.pop()
        table.scope_of[id(node)] = scope
        children: list[tuple[ast.AST, Scope]] | None = None
        if isinstance(node, _FUNCTIONS):
            scope.bind(node.name, Binding("def", node))
            inner = Scope("function", scope)
            for arg in _arguments(node.args):
                inner.bind(arg.arg, Binding("variable", arg))
            outer = [*node.decorator_list, *node.args.defaults]
            outer += [d for d in node.args.kw_defaults if d is not None]
            outer += [a.annotation for a in _arguments(node.args) if a.annotation is not None]
            outer += [node.returns] if node.returns is not None else []
            children = [(n, scope) for n in outer] + [(n, inner) for n in node.body]
        elif isinstance(node, ast.ClassDef):
            scope.bind(node.name, Binding("class", node))
            inner = Scope("class", scope)
            class_outer: list[ast.AST] = [*node.decorator_list, *node.bases, *node.keywords]
            children = [(n, scope) for n in class_outer] + [(n, inner) for n in node.body]
        elif isinstance(node, ast.Lambda):
            inner = Scope("lambda", scope)
            for arg in _arguments(node.args):
                inner.bind(arg.arg, Binding("variable", arg))
            lambda_outer: list[ast.AST] = [
                *node.args.defaults,
                *[d for d in node.args.kw_defaults if d is not None],
            ]
            children = [(n, scope) for n in lambda_outer] + [(node.body, inner)]
        elif isinstance(node, _COMPREHENSIONS):
            inner = Scope("comprehension", scope)
            generators = node.generators
            children = [(generators[0].iter, scope)]  # evaluated in the enclosing scope
            for index, generator in enumerate(generators):
                children.append((generator.target, inner))
                if index:
                    children.append((generator.iter, inner))
                children += [(condition, inner) for condition in generator.ifs]
            results = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            children += [(result, inner) for result in results]
        elif isinstance(node, ast.NamedExpr):
            target = node.target
            if isinstance(target, ast.Name):
                _non_comprehension(scope).bind(target.id, Binding("variable", target))
                table.scope_of[id(target)] = scope
                table.names[(target.lineno, target.col_offset)] = target
            children = [(node.value, scope)]
        elif isinstance(node, ast.Import):
            table.imports.append(node)
            for alias in node.names:
                scope.bind(alias.asname or alias.name.split(".")[0], Binding("import", node, alias))
        elif isinstance(node, ast.ImportFrom):
            table.imports.append(node)
            for alias in node.names:
                if alias.name == "*":
                    scope.star_import = True
                else:
                    scope.bind(alias.asname or alias.name, Binding("import_from", node, alias))
        elif isinstance(node, ast.Global | ast.Nonlocal):
            for name in node.names:
                scope.bind(name, Binding("declared", node))
        elif isinstance(node, ast.Name):
            table.names[(node.lineno, node.col_offset)] = node
            if not isinstance(node.ctx, ast.Load):
                scope.bind(node.id, Binding("variable", node))
        elif isinstance(node, ast.Attribute):
            if node.end_lineno is not None and node.end_col_offset is not None:
                table.attributes[(node.end_lineno, node.end_col_offset)] = node
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                scope.bind(node.name, Binding("variable", node))
        elif isinstance(node, ast.MatchAs | ast.MatchStar):
            if node.name:
                scope.bind(node.name, Binding("variable", node))
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                scope.bind(node.rest, Binding("variable", node))
        if children is None:
            children = [(child, scope) for child in ast.iter_child_nodes(node)]
        stack.extend(reversed(children))
    return table


def resolving_scope(scope: Scope, name: str) -> Scope | None:
    """The innermost scope binding ``name``, as Python looks it up (class scopes are only
    visible to their own body)."""
    current: Scope | None = scope
    own = True
    while current is not None:
        if (own or current.kind != "class") and name in current.bindings:
            return current
        own = False
        current = current.parent
    return None


# --- Tokens and positions ----------------------------------------------------------------


@dataclass(frozen=True)
class NameToken:
    line: int  # 1-based
    column: int  # 0-based code points
    end_column: int
    start_byte: int  # UTF-8 offsets on the line, the unit of AST col_offset
    end_byte: int
    text: str


def name_tokens(source: str) -> list[NameToken]:
    """Every NAME token (keywords included) with code-point and byte positions."""
    lines = source.split("\n")
    found: list[NameToken] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.NAME:
            continue
        line, column = token.start
        text = lines[line - 1]
        start_byte = len(text[:column].encode("utf-8"))
        found.append(
            NameToken(
                line,
                column,
                token.end[1],
                start_byte,
                start_byte + len(token.string.encode("utf-8")),
                token.string,
            )
        )
    return found


def token_at(tokens: Sequence[NameToken], line: int, column: int) -> NameToken | None:
    """The NAME token whose characters include the 1-based ``column`` of ``line``."""
    for token in tokens:
        if token.line == line and token.column <= column - 1 < token.end_column:
            return token
    return None


def _within(token: NameToken, node: ast.AST) -> bool:
    start = (node.lineno, node.col_offset)  # type: ignore[attr-defined]
    end = (node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]
    return start <= (token.line, token.start_byte) and (token.line, token.end_byte) <= end


# --- Resolution ---------------------------------------------------------------------------


class _ModuleBudget(Exception):
    """Resolving needs more than MAX_MODULE_FILES module files."""


@dataclass
class Outcome:
    definitions: list[DefinitionLocation] = field(default_factory=list)
    unsupported: bool = False

    def add(self, other: "Outcome") -> "Outcome":
        return Outcome(self.definitions + other.definitions, self.unsupported or other.unsupported)


def _unsupported() -> Outcome:
    return Outcome([], True)


# Python's builtin names (a static set from the interpreter's own builtins module; no
# repository code is involved). An unbound builtin is defined outside the repository.
BUILTIN_NAMES = frozenset(dir(builtins))


def _cap(name: str) -> str:
    return name[:MAX_NAME_CHARS]


def _module_definitions(paths: set[str]) -> Outcome:
    """Module files as definitions, named after the module itself (its last part)."""
    return Outcome(
        [
            DefinitionLocation(
                path=path, kind="module", name=_cap(module_parts(path)[-1]), line=1, end_line=1
            )
            for path in sorted(paths)
        ]
    )


class Resolver:
    def __init__(
        self, root: Path, rows: Sequence[Any], target_path: str, target: ScopeTable
    ) -> None:
        self.root = root
        self.rows = {row.path: row for row in rows}
        self.index = ModuleIndex.build([row.path for row in rows])
        self.tables: dict[str, ScopeTable | None] = {target_path: target}
        self.files_read = 0

    def module(self, path: str) -> ScopeTable | None:
        """A module file's scopes, read from the snapshot (None: it cannot be analyzed)."""
        if path in self.tables:
            return self.tables[path]
        row = self.rows[path]
        table: ScopeTable | None = None
        if row.size_bytes <= MAX_CONTENT_BYTES:  # larger files are never opened
            if self.files_read >= MAX_MODULE_FILES:
                raise _ModuleBudget
            self.files_read += 1
            try:
                data = _read_verified(self.root, path, row.sha256)
            except _Unavailable:
                raise FileContentError(409, REPOSITORY_CHANGED) from None
            text = _decode(data)
            if text is not None and len(text.encode("utf-8")) <= MAX_OUTLINE_BYTES:
                try:
                    table = collect_scopes(ast.parse(_normalize(text)))
                except _PARSE_ERRORS:
                    table = None
        self.tables[path] = table
        return table

    def bindings(self, bindings: list[Binding], scope: Scope, path: str, hops: int) -> Outcome:
        kinds = {binding.kind for binding in bindings}
        if "declared" in kinds or "variable" in kinds:
            return _unsupported()
        outcome = Outcome()
        for binding in bindings:
            node = binding.node
            if isinstance(node, ast.ClassDef):
                outcome.definitions.append(
                    DefinitionLocation(
                        path=path,
                        kind="class",
                        name=_cap(node.name),
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                    )
                )
            elif isinstance(node, _FUNCTIONS):
                outcome.definitions.append(
                    DefinitionLocation(
                        path=path,
                        kind="method" if scope.kind == "class" else "function",
                        name=_cap(node.name),
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                    )
                )
            else:
                outcome = outcome.add(self.follow(binding, path, hops + 1))
        return outcome

    def follow(self, binding: Binding, importer: str, hop: int) -> Outcome:
        """Follow one import statement (the ``hop``-th on this path)."""
        if hop > MAX_IMPORT_HOPS:
            return _unsupported()
        alias = binding.alias
        assert alias is not None
        if isinstance(binding.node, ast.Import):
            dotted = alias.name if alias.asname else alias.name.split(".")[0]
            return _module_definitions(_lookups(binding.node, importer, self.index, [dotted])[0])
        assert isinstance(binding.node, ast.ImportFrom)
        return self.from_import(binding.node, importer, alias.name, hop)

    def from_import(self, node: ast.ImportFrom, importer: str, name: str, hop: int) -> Outcome:
        """``from M import name``: the submodule M.name and M's top-level ``name``."""
        matches = _lookups(node, importer, self.index, [name])
        modules, submodules = (matches[0], matches[1]) if len(matches) == 2 else (set(), matches[0])
        outcome = _module_definitions(submodules)
        for path in sorted(modules):
            outcome = outcome.add(self.top_level(path, name, hop))
        return outcome

    def top_level(self, path: str, name: str, hop: int) -> Outcome:
        table = self.module(path)
        if table is None:
            return _unsupported()
        scope = table.module
        if name in scope.bindings:
            return self.bindings(scope.bindings[name], scope, path, hop)
        return _unsupported() if scope.star_import else Outcome()

    def name(self, node: ast.Name, scope: Scope, path: str) -> Outcome:
        found = resolving_scope(scope, node.id)
        if found is None:
            module = _module_scope(scope)
            if module.star_import or node.id in BUILTIN_NAMES:
                return _unsupported()
            return Outcome()
        return self.bindings(found.bindings[node.id], found, path, 0)

    def attribute(self, node: ast.Attribute, scope: Scope, path: str) -> Outcome:
        """``a.b.X`` where ``a.b`` is a module bound by a plain ``import``; else unsupported."""
        chain: list[str] = []
        value: ast.expr = node.value
        while isinstance(value, ast.Attribute):
            chain.insert(0, value.attr)
            value = value.value
        if not isinstance(value, ast.Name):
            return _unsupported()
        chain.insert(0, value.id)
        found = resolving_scope(scope, value.id)
        if found is None:
            if _module_scope(scope).star_import or value.id in BUILTIN_NAMES:
                return _unsupported()
            return Outcome()
        bindings = found.bindings[value.id]
        if any(binding.kind != "import" for binding in bindings):
            return _unsupported()
        dotted = ".".join(chain)
        modules: set[str] = set()
        for binding in bindings:
            alias = binding.alias
            assert alias is not None
            if alias.asname:
                if len(chain) == 1:
                    modules.add(alias.name)
            elif alias.name == dotted or alias.name.startswith(dotted + "."):
                modules.add(dotted)
        if not modules:
            return _unsupported()
        outcome = Outcome()
        for module in sorted(modules):
            synthetic = ast.ImportFrom(module=module, names=[], level=0)
            outcome = outcome.add(self.from_import(synthetic, path, node.attr, 1))
        return outcome

    def import_name(
        self,
        node: ast.Import | ast.ImportFrom,
        token: NameToken,
        tokens: Sequence[NameToken],
        path: str,
    ) -> tuple[str, Outcome] | None:
        """A name inside an import statement: (identifier, outcome), or None."""
        inside = [t for t in tokens if _within(t, node)]
        if isinstance(node, ast.ImportFrom):
            keyword_import = next((t for t in inside if t.text == "import"), None)
            if keyword_import is not None and (token.line, token.start_byte) < (
                keyword_import.line,
                keyword_import.start_byte,
            ):
                parts = [
                    t
                    for t in inside
                    if t.text != "from"
                    and (t.line, t.start_byte) < (keyword_import.line, keyword_import.start_byte)
                ]
                if token not in parts or node.module is None:
                    return None
                segments = node.module.split(".")
                prefix = ".".join(segments[: parts.index(token) + 1])
                synthetic = ast.ImportFrom(module=prefix, names=[], level=node.level)
                return segments[parts.index(token)], _module_definitions(
                    _lookups(synthetic, path, self.index, [])[0]
                )
        for alias in node.names:
            if not _within(token, alias):
                continue
            alias_tokens = [t for t in inside if _within(t, alias)]
            as_token = next((t for t in alias_tokens if t.text == "as"), None)
            if isinstance(node, ast.ImportFrom):
                identifier = (
                    alias.asname
                    if as_token is not None and token != alias_tokens[0]
                    else alias.name
                )
                return identifier or alias.name, self.from_import(node, path, alias.name, 1)
            segments = alias.name.split(".")
            if as_token is not None and (token.line, token.start_byte) > (
                as_token.line,
                as_token.start_byte,
            ):
                return alias.asname or alias.name, _module_definitions(
                    _lookups(node, path, self.index, [alias.name])[0]
                )
            parts = [t for t in alias_tokens if t.text != "as"]
            position = parts.index(token)
            prefix = ".".join(segments[: position + 1])
            return segments[position], _module_definitions(
                _lookups(node, path, self.index, [prefix])[0]
            )
        return None


def _module_scope(scope: Scope) -> Scope:
    while scope.parent is not None:
        scope = scope.parent
    return scope


def _normalize(text: str) -> str:
    """Python's own line endings (\\r\\n and \\r are newlines); columns are unaffected."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --- Request ------------------------------------------------------------------------------


def _response(
    status: DefinitionStatus,
    reference: DefinitionReference | None = None,
    definitions: list[DefinitionLocation] | None = None,
    truncated: bool = False,
) -> DefinitionResponse:
    return DefinitionResponse(
        status=status, reference=reference, definitions=definitions or [], truncated=truncated
    )


def analyze_definition(
    root: Path,
    target: Any,
    rows: Sequence[Any],
    line: int,
    column: int,
    index_truncated: bool = False,
) -> DefinitionResponse:
    """Resolve the identifier at (line, column) of ``target``; ``rows`` are the snapshot's
    indexed Python rows sorted by path. Raises FileContentError(409) on any change."""
    if target.size_bytes > MAX_CONTENT_BYTES:
        return _response("too_large")  # never opened
    try:
        data = _read_verified(root, target.path, target.sha256)
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
    token = token_at(tokens, line, column)
    if token is None or keyword.iskeyword(token.text):
        return _response("no_identifier")

    table = collect_scopes(tree)
    resolver = Resolver(root, rows, target.path, table)
    key = (token.line, token.start_byte)
    name_node = table.names.get(key)
    attribute = table.attributes.get((token.line, token.end_byte))
    if attribute is not None and attribute.attr != unicodedata.normalize("NFKC", token.text):
        attribute = None
    identifier = unicodedata.normalize("NFKC", token.text)
    outcome: Outcome | None = None
    try:
        if name_node is not None:
            identifier = name_node.id
            outcome = resolver.name(name_node, table.scope_of[id(name_node)], target.path)
        elif attribute is not None:
            identifier = attribute.attr
            outcome = resolver.attribute(attribute, table.scope_of[id(attribute)], target.path)
        else:
            for statement in table.imports:
                if _within(token, statement):
                    found = resolver.import_name(statement, token, tokens, target.path)
                    if found is not None:
                        identifier, outcome = found
                    break
    except _ModuleBudget:
        outcome, index_truncated = _unsupported(), True

    if outcome is None and keyword.issoftkeyword(token.text):
        return _response("no_identifier")  # e.g. "match" used as a keyword
    reference = DefinitionReference(
        path=target.path, line=token.line, column=token.column + 1, name=_cap(identifier)
    )
    if outcome is None or outcome.unsupported:
        return _response("unsupported", reference, truncated=index_truncated)
    unique = {(d.path, d.line, d.kind, d.name): d for d in outcome.definitions}
    definitions = [unique[key] for key in sorted(unique)]
    truncated = index_truncated or len(definitions) > MAX_DEFINITIONS
    definitions = definitions[:MAX_DEFINITIONS]
    if not definitions:
        return _response("not_found", reference, truncated=truncated)
    status: DefinitionStatus = "resolved" if len(definitions) == 1 else "ambiguous"
    return _response(status, reference, definitions, truncated)


async def code_definition(
    db: AsyncSession, repository: Repository, path: str, line: int, column: int
) -> DefinitionResponse:
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
    if not _is_python(target.language, target.extension):
        return _response("unsupported_language")  # never read
    python_rows = sorted(
        (r for r in rows if _is_python(r.language, r.extension)), key=lambda r: r.path
    )
    indexed = python_rows[:MAX_INDEXED_PATHS]
    root = repository_storage_dir(get_settings(), repository.id)
    return await run_in_threadpool(
        analyze_definition,
        root,
        target,
        indexed,
        line,
        column,
        len(python_rows) > MAX_INDEXED_PATHS,
    )
