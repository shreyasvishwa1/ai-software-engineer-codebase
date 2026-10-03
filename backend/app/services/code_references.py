"""Static find-references for one Python definition. Never guesses.

Target: the class/def statement starting on the requested line of a Python file (Python
allows at most one compound statement per line, so it is unique). A definition bound in a
class scope (a method or nested class) is ``unsupported_target``: its uses are attribute
accesses, which have no static resolution without type inference. Modules are never
targets (Step 41 lists a module's importers).

Reference: a token is a reference to the target exactly when Step 45's go-to-definition at
that token returns the target among its definitions (``ambiguous`` when it returns others
too). Every token is confirmed by Step 45's own ``Resolver`` and dispatch, reused unchanged:
the same scopes, shadowing, imports, re-exports, attribute rule and limits. Definition
names, parameters, keyword arguments, strings and comments are therefore never references,
and same-name uses that resolve as ``unsupported`` are only counted.

Candidates: a reference's identifier is the target's name or an ``as`` name of an import
chain reaching it, so only NAME tokens in that name closure are resolved. The closure grows
from the files that name its members, for at most MAX_IMPORT_HOPS rounds (no resolvable
chain renames more often). ASCII text without any member is never parsed; non-ASCII text
always is, because Python normalizes identifiers (NFKC). A target not bound in module scope
can only be referenced from its own file, so no other file is scanned for it.

Parity: each candidate is resolved with Step 45's per-request budget, MAX_MODULE_FILES
module files besides its own file, counted exactly as Step 45 counts its reads; a candidate
over the budget is unsupported and sets ``truncated``, as in Step 45.

Snapshot: one SELECT returns the target row and every Python row; every file (the target,
scanned files and modules needed for resolution) is read once through Step 21's verified
reader against its snapshot SHA-256, so any change answers 409 and results never mix
snapshots. Work is bounded by Step 41's MAX_SCANNED_BYTES read and MAX_PARSED_BYTES parsed,
the per-file caps, MAX_CANDIDATES, MAX_ALIAS_NAMES and MAX_REFERENCES; ``truncated`` reports
any limit. Parsing is ``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode,
imported or executed. Nothing is stored, cached across requests or logged.
"""

import ast
import keyword
import tokenize
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_definition import DefinitionLocation
from app.schemas.code_references import (
    ReferenceLocation,
    ReferencesResponse,
    ReferencesStatus,
    ReferencesSummary,
)

# Step 45's resolver (limits read at call time, so they stay Step 45's) and Steps 21, 37, 39
# and 41's readers, rules and budgets, all reused unchanged.
from app.services import code_definition
from app.services.code_definition import (
    _FUNCTIONS,
    NameToken,
    Outcome,
    Resolver,
    ScopeTable,
    _cap,
    _ModuleBudget,
    _normalize,
    _within,
    collect_scopes,
    name_tokens,
)
from app.services.code_importers import MAX_PARSED_BYTES, MAX_SCANNED_BYTES, REPOSITORY_CHANGED
from app.services.code_imports import MAX_INDEXED_PATHS
from app.services.code_outline import (
    _PARSE_ERRORS,
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

MAX_REFERENCES = 500
MAX_CANDIDATES = 10_000  # same-name tokens resolved per request
MAX_ALIAS_NAMES = 50  # the target's name and the import "as" names reaching it
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
REFERENCES_LIMIT = Limit("references-user", 20, 600)

Context = Literal["name", "attribute", "import"]


# --- Snapshot reads -----------------------------------------------------------------------


@dataclass
class _Source:
    text: str  # normalized line endings
    size: int  # UTF-8 bytes of the decoded text
    parsed: bool = False
    tree: ast.Module | None = None
    table: ScopeTable | None = None
    tokens: list[NameToken] | None = None


@dataclass
class _Scan:
    """Verified, budgeted reads and parses of snapshot rows; each file at most once."""

    root: Path
    rows: dict[str, Any]
    read_budget: int  # stored sizes, checked before a file is opened
    parse_budget: int  # UTF-8 size of the text parsed
    counts: dict[str, int] = field(
        default_factory=lambda: {"files_scanned": 0, "too_large": 0, "binary": 0, "syntax_error": 0}
    )
    sources: dict[str, _Source | None] = field(default_factory=dict)
    parse_exhausted: bool = False
    truncated: bool = False

    def read(self, path: str) -> bool:
        """Read ``path`` once; False (and truncated) when the read budget is exhausted."""
        if path in self.sources:
            return True
        row = self.rows[path]
        if row.size_bytes > MAX_CONTENT_BYTES:
            self.counts["too_large"] += 1  # never opened
            self.sources[path] = None
            return True
        if row.size_bytes > self.read_budget:
            self.truncated = True  # checked before the file is opened
            return False
        self.read_budget -= row.size_bytes
        try:
            data = _read_verified(self.root, path, row.sha256)
        except _Unavailable:
            raise FileContentError(409, REPOSITORY_CHANGED) from None
        self.counts["files_scanned"] += 1
        text = _decode(data)
        if text is None:
            self.counts["binary"] += 1
            self.sources[path] = None
            return True
        size = len(text.encode("utf-8"))
        if size > MAX_OUTLINE_BYTES:
            self.counts["too_large"] += 1  # never parsed
            self.sources[path] = None
            return True
        self.sources[path] = _Source(_normalize(text), size)
        return True

    def parse(self, path: str) -> _Source | None:
        """The parsed source of a read file (None: it cannot be analyzed or the parse budget
        is exhausted, which stops all further parsing)."""
        source = self.sources.get(path)
        if source is None:
            return None
        if not source.parsed:
            if self.parse_exhausted or source.size > self.parse_budget:
                self.parse_exhausted = self.truncated = True
                return None
            self.parse_budget -= source.size
            source.parsed = True
            try:
                source.tree = ast.parse(source.text)  # a syntax tree only; nothing is run
                source.table = collect_scopes(source.tree)
                source.tokens = name_tokens(source.text)
            except (*_PARSE_ERRORS, tokenize.TokenError):
                self.counts["syntax_error"] += 1
        return source if source.table is not None else None


class _RequestResolver(Resolver):
    """Step 45's resolver over this request's verified reads, with Step 45's per-request
    module-file budget applied to each candidate (its own file is free)."""

    def __init__(
        self, root: Path, rows: Sequence[Any], scan: _Scan, target: str, table: ScopeTable
    ) -> None:
        super().__init__(root, rows, target, table)
        self.scan = scan
        self.current = target
        self.counted: set[str] = set()

    def begin(self, path: str) -> None:
        """Start one candidate's resolution, as a fresh Step 45 request for ``path``."""
        self.current = path
        self.counted = set()

    def module(self, path: str) -> ScopeTable | None:
        if path != self.current and path not in self.counted:
            if self.rows[path].size_bytes <= MAX_CONTENT_BYTES:  # larger files are never read
                if len(self.counted) >= code_definition.MAX_MODULE_FILES:
                    raise _ModuleBudget
                self.counted.add(path)
        if not self.scan.read(path):
            return None
        source = self.scan.parse(path)
        return source.table if source is not None else None


# --- Target ----------------------------------------------------------------------------------


def _target(
    source: _Source, path: str, line: int
) -> tuple[DefinitionLocation, ast.AST, bool] | None:
    """The class/def statement starting on ``line``: (location, node, bound in a class)."""
    assert source.tree is not None and source.table is not None
    for node in ast.walk(source.tree):
        if isinstance(node, (ast.ClassDef, *_FUNCTIONS)) and node.lineno == line:
            in_class = source.table.scope_of[id(node)].kind == "class"
            kind: Literal["class", "function", "method"] = (
                "class" if isinstance(node, ast.ClassDef) else "method" if in_class else "function"
            )
            location = DefinitionLocation(
                path=path,
                kind=kind,
                name=_cap(node.name),
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
            )
            return location, node, in_class
    return None


def _key(definition: DefinitionLocation) -> tuple[str, int, str, str]:
    return (definition.path, definition.line, definition.kind, definition.name)


# --- References ------------------------------------------------------------------------------


def _resolve(
    resolver: _RequestResolver, source: _Source, path: str, token: NameToken
) -> tuple[str, Context, Outcome] | None:
    """Step 45's dispatch for one token: (identifier, context, outcome), or None when the
    token is not a reference at all (a definition name, a parameter, a keyword argument)."""
    table, tokens = source.table, source.tokens
    assert table is not None and tokens is not None
    name_node = table.names.get((token.line, token.start_byte))
    attribute = table.attributes.get((token.line, token.end_byte))
    if attribute is not None and attribute.attr != unicodedata.normalize("NFKC", token.text):
        attribute = None
    if name_node is not None:
        outcome = resolver.name(name_node, table.scope_of[id(name_node)], path)
        return name_node.id, "name", outcome
    if attribute is not None:
        outcome = resolver.attribute(attribute, table.scope_of[id(attribute)], path)
        return attribute.attr, "attribute", outcome
    for statement in table.imports:
        if _within(token, statement):
            found = resolver.import_name(statement, token, tokens, path)
            if found is None:
                return None
            return found[0], "import", found[1]
    return None


def _closure(scan: _Scan, paths: list[str], name: str) -> tuple[set[str], list[str]]:
    """The name closure and the files that may contain references, in path order."""
    names = {name}
    selected: dict[str, None] = {}
    rounds = code_definition.MAX_IMPORT_HOPS
    for round_ in range(rounds + 1):
        for path in paths:
            source = scan.sources.get(path)
            if path in selected or source is None:
                continue
            if source.text.isascii() and not any(n in source.text for n in names):
                continue  # names no member of the closure, so cannot refer to the target
            selected[path] = None
            scan.parse(path)
        if round_ == rounds:
            break
        found: set[str] = set()
        for path in selected:
            parsed = scan.parse(path)
            if parsed is None or parsed.table is None:
                continue
            for statement in parsed.table.imports:
                if isinstance(statement, ast.ImportFrom):
                    for alias in statement.names:
                        if alias.asname and alias.name in names:
                            found.add(alias.asname)
        new = sorted(found - names)
        if not new:
            break
        for alias_name in new:
            if len(names) >= MAX_ALIAS_NAMES:
                scan.truncated = True
                break
            names.add(alias_name)
    return names, sorted(selected)


def _candidates(
    scan: _Scan, selected: list[str], names: set[str]
) -> Iterator[tuple[str, NameToken, _Source]]:
    """Every non-keyword NAME token in the closure, in (path, source) order."""
    for path in selected:
        parsed = scan.parse(path)
        if parsed is None or parsed.tokens is None:
            continue
        for token in parsed.tokens:
            if keyword.iskeyword(token.text):
                continue
            if unicodedata.normalize("NFKC", token.text) in names:
                yield path, token, parsed


def _empty(
    status: ReferencesStatus, target: DefinitionLocation | None = None
) -> ReferencesResponse:
    summary = ReferencesSummary(
        files_scanned=0,
        too_large=0,
        binary=0,
        syntax_error=0,
        references=0,
        ambiguous=0,
        unsupported=0,
    )
    return ReferencesResponse(
        status=status, target=target, references=[], summary=summary, truncated=False
    )


def analyze_references(
    root: Path,
    target_row: Any,
    rows: Sequence[Any],
    line: int,
    index_truncated: bool = False,
) -> ReferencesResponse:
    """References to the definition starting on ``line`` of ``target_row``; ``rows`` are the
    snapshot's indexed Python rows sorted by path. Raises FileContentError(409) on any change."""
    scan = _Scan(
        root,
        {row.path: row for row in [*rows, target_row]},
        read_budget=MAX_SCANNED_BYTES,
        parse_budget=MAX_PARSED_BYTES,
    )
    if target_row.size_bytes > MAX_CONTENT_BYTES:
        return _empty("too_large")  # never opened
    scan.read(target_row.path)
    if scan.sources[target_row.path] is None:
        return _empty("binary" if scan.counts["binary"] else "too_large")
    source = scan.parse(target_row.path)
    if source is None:
        return _empty("syntax_error")
    found = _target(source, target_row.path, line)
    if found is None:
        return _empty("not_a_definition")
    target, node, in_class = found
    if in_class:
        return _empty("unsupported_target", target)
    assert isinstance(node, (ast.ClassDef, *_FUNCTIONS))
    assert source.table is not None
    module_level = source.table.scope_of[id(node)].parent is None

    paths = [target_row.path]
    if module_level:  # only a module-level binding can be imported elsewhere
        for row in rows:
            if not scan.read(row.path):
                break
        paths = sorted(p for p in {*scan.sources, target_row.path} if scan.sources.get(p))
    names, selected = _closure(scan, paths, node.name) if module_level else ({node.name}, paths)

    resolver = _RequestResolver(root, rows, scan, target_row.path, source.table)
    wanted = _key(target)
    references: list[ReferenceLocation] = []
    candidates = unsupported = 0
    for path, token, parsed in _candidates(scan, selected, names):
        if candidates >= MAX_CANDIDATES:
            scan.truncated = True
            break
        candidates += 1
        resolver.begin(path)
        try:
            resolved = _resolve(resolver, parsed, path, token)
        except _ModuleBudget:
            unsupported, scan.truncated = unsupported + 1, True
            continue
        if resolved is None:
            continue
        identifier, context, outcome = resolved
        if outcome.unsupported:
            unsupported += 1
            continue
        keys = sorted({_key(d) for d in outcome.definitions})
        if wanted not in keys:
            continue
        if wanted not in keys[: code_definition.MAX_DEFINITIONS]:
            scan.truncated = True  # Step 45 would cut the target from its list
            continue
        references.append(
            ReferenceLocation(
                path=path,
                line=token.line,
                column=token.column + 1,
                name=_cap(identifier),
                context=context,
                ambiguous=len(keys) > 1,
            )
        )

    references.sort(key=lambda r: (r.path, r.line, r.column))
    summary = ReferencesSummary(
        **scan.counts,
        references=len(references),
        ambiguous=sum(r.ambiguous for r in references),
        unsupported=unsupported,
    )
    truncated = index_truncated or scan.truncated or len(references) > MAX_REFERENCES
    return ReferencesResponse(
        status="scanned",
        target=target,
        references=references[:MAX_REFERENCES],
        summary=summary,
        truncated=truncated,
    )


async def code_references(
    db: AsyncSession, repository: Repository, path: str, line: int
) -> ReferencesResponse:
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
        return _empty("unsupported_language")  # never read
    python_rows = sorted(
        (r for r in rows if _is_python(r.language, r.extension)), key=lambda r: r.path
    )
    indexed = python_rows[:MAX_INDEXED_PATHS]
    root = repository_storage_dir(get_settings(), repository.id)
    return await run_in_threadpool(
        analyze_references, root, target, indexed, line, len(python_rows) > MAX_INDEXED_PATHS
    )
