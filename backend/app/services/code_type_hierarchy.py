"""Static type (inheritance) hierarchy for one Python class: its direct bases and its direct
subclasses. Never guesses; transitive chains are not computed.

Target: the class statement starting on the requested line (Step 47's ``_target``; Python
allows at most one compound statement per line). A def/async def there is ``not_a_class``;
decorator and body lines are ``not_a_definition``; modules are never targets.

Base position: an element of a class statement's base list that is a name ``Base``, an
attribute ``pkg.Base`` or one of those subscripted (``Base[T]``, ``pkg.Base[T]``: the class
inherited is the subscript's value). Its site is the 1-based line and 1-based code-point
column of the base name (the final attribute name of ``pkg.Base``): exactly where Step 45's
go-to-definition resolves it, in the scope enclosing the class statement. Any other base
expression (``*bases``, ``B if c else C``, ``get_base()``, a lambda, a literal) is dynamic:
listed and counted, never evaluated or resolved. ``metaclass=`` and other class keywords are
not bases; they are only counted.

Bases: every base of the target, in source order with duplicates kept, resolved with Step
45's own dispatch (Step 47's ``_resolve``) through Step 49's per-request ``_Resolutions``
memo, reused read-only: Step 45's module-file budget applies afresh to every base. A base
whose definitions include a class is resolved (exactly one definition) or ambiguous (two or
more, modules included, as in Step 47; all are listed); one resolving only to functions or
modules is not_a_class; Step 45's unsupported answers are builtin (an unbound Python
builtin) or unsupported; no definition at all is unresolved.

Subclasses: Step 47's references of the target whose token is a base position, one entry per
(subclass, base position); Step 47's loop, run through its own helpers. A target bound in a
class scope (a nested class) is not searched (``subclasses_status`` unsupported_target), as
in Step 47, and a target bound in a function scope can only be inherited in its own file.

Snapshot: one SELECT returns the target row and every Python row; Step 47's ``_Scan`` reads
each file at most once through Step 21's verified reader against its snapshot SHA-256,
bases first and then subclasses, so any change answers 409 and results never mix
snapshots. Work is bounded by Step 47's read, parse, candidate and alias budgets, Step 45's
per-site limits, MAX_BASES and MAX_SUBCLASSES; each direction reports its own truncation.
Parsing is ``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode, imported or
executed. Nothing is stored, cached across requests or logged.
"""

import ast
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_definition import DefinitionLocation
from app.schemas.code_type_hierarchy import (
    BaseEntry,
    BaseKind,
    BaseSite,
    SubclassEntry,
    SubclassesStatus,
    SubclassSite,
    TypeHierarchyResponse,
    TypeHierarchyStatus,
    TypeHierarchySummary,
)

# Step 45's resolver and limits (read at call time, so they stay Step 45's), Step 47's
# snapshot scan, closure and dispatch, Step 49's resolution memo, and Steps 21, 37, 39 and
# 41's rules, all reused unchanged.
from app.services import code_references
from app.services.code_call_hierarchy import _is_builtin, _Resolutions, _Site
from app.services.code_definition import NameToken, _cap, _ModuleBudget
from app.services.code_importers import MAX_PARSED_BYTES, MAX_SCANNED_BYTES
from app.services.code_imports import MAX_INDEXED_PATHS
from app.services.code_outline import SUPPORTED_LANGUAGE, _is_python
from app.services.code_references import (
    _candidates,
    _closure,
    _key,
    _RequestResolver,
    _Scan,
    _Source,
    _target,
)
from app.services.file_content import FILE_NOT_FOUND, FileContentError
from app.services.ingestion import repository_storage_dir
from app.services.rate_limit import Limit

MAX_BASES = 200  # base expressions of the target that are examined, in source order
MAX_SUBCLASSES = 500  # subclass entries returned
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
TYPE_HIERARCHY_LIMIT = Limit("type-hierarchy-user", 20, 600)


# --- Base positions --------------------------------------------------------------------------


def _base_name(base: ast.expr) -> ast.Name | ast.Attribute | None:
    """The Name/Attribute naming the class a base expression inherits; None when dynamic."""
    if isinstance(base, ast.Subscript):  # Base[T]: the class inherited is the value
        base = base.value
    return base if isinstance(base, (ast.Name, ast.Attribute)) else None


def _token(
    by_start: dict[tuple[int, int], NameToken],
    by_end: dict[tuple[int, int], NameToken],
    named: ast.Name | ast.Attribute,
) -> NameToken | None:
    """The token Step 45 resolves at: the name, or the final attribute name."""
    if isinstance(named, ast.Name):
        return by_start.get((named.lineno, named.col_offset))
    token = by_end.get((named.end_lineno or named.lineno, named.end_col_offset or 0))
    if token is not None and unicodedata.normalize("NFKC", token.text) != named.attr:
        return None
    return token


def _base_positions(source: _Source) -> dict[int, ast.ClassDef]:
    """id of every base position's Name/Attribute -> the class statement it belongs to."""
    assert source.tree is not None
    positions: dict[int, ast.ClassDef] = {}
    for node in ast.walk(source.tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                named = _base_name(base)
                if named is not None:
                    positions[id(named)] = node
    return positions


def _class_location(path: str, node: ast.ClassDef) -> DefinitionLocation:
    """The class's location, exactly Step 45's."""
    return DefinitionLocation(
        path=path,
        kind="class",
        name=_cap(node.name),
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
    )


# --- Request ---------------------------------------------------------------------------------


def _empty(status: TypeHierarchyStatus) -> TypeHierarchyResponse:
    zero = dict.fromkeys(TypeHierarchySummary.model_fields, 0)
    return TypeHierarchyResponse(
        status=status,
        target=None,
        bases=[],
        subclasses_status=None,
        subclasses=None,
        summary=TypeHierarchySummary(**zero),
        bases_truncated=False,
        subclasses_truncated=None,
    )


def _bases(
    node: ast.ClassDef,
    path: str,
    source: _Source,
    resolutions: _Resolutions,
    counts: dict[str, int],
) -> tuple[list[BaseEntry], bool]:
    """The target's bases in source order and whether a limit was reached."""
    assert source.table is not None and source.tokens is not None
    by_start = {(t.line, t.start_byte): t for t in source.tokens}
    by_end = {(t.line, t.end_byte): t for t in source.tokens}
    truncated = len(node.bases) > MAX_BASES
    entries: list[BaseEntry] = []
    for base in node.bases[:MAX_BASES]:
        named = _base_name(base)
        token = _token(by_start, by_end, named) if named is not None else None
        kind: BaseKind
        definitions: list[DefinitionLocation] = []
        if named is None or token is None:
            kind = "dynamic" if named is None else "unsupported"  # no token to resolve at
            site = None
        else:
            identifier = named.id if isinstance(named, ast.Name) else named.attr
            site = BaseSite(line=token.line, column=token.column + 1, name=_cap(identifier))
            try:
                answer = resolutions.resolve(path, source, token)
            except _ModuleBudget:
                answer, truncated = None, True  # Step 45: unsupported and truncated
            if answer is None:
                kind = "unsupported"
            elif answer.unsupported:
                order = (token.line, token.start_byte)
                builtin = _is_builtin(_Site(order, named, token), source.table)
                kind = "builtin" if builtin else "unsupported"
            elif not answer.total:
                kind = "unresolved"
            else:
                if answer.total > len(answer.listed):
                    truncated = True  # Step 45 cuts its list the same way
                definitions = [answer.definitions[key] for key in answer.listed]
                if not any(key[2] == "class" for key in answer.listed):
                    kind = "not_a_class"
                else:
                    kind = "resolved" if answer.total == 1 else "ambiguous"
        counts[f"bases_{kind}"] += 1
        entries.append(BaseEntry(site=site, kind=kind, definitions=definitions))
    return entries, truncated


def analyze_type_hierarchy(
    root: Path,
    target_row: Any,
    rows: Sequence[Any],
    line: int,
    index_truncated: bool = False,
) -> TypeHierarchyResponse:
    """Bases and subclasses of the class starting on ``line`` of ``target_row``; ``rows`` are
    the snapshot's indexed Python rows sorted by path. Raises FileContentError(409) on any
    change."""
    scan = _Scan(
        root,
        {row.path: row for row in [*rows, target_row]},
        read_budget=MAX_SCANNED_BYTES,
        parse_budget=MAX_PARSED_BYTES,
    )
    scan.read(target_row.path)  # over 1 MiB it is never opened
    if scan.sources[target_row.path] is None:
        return _empty("binary" if scan.counts["binary"] else "too_large")
    source = scan.parse(target_row.path)
    if source is None or source.tokens is None:  # Step 45: tokenize failures are syntax errors
        return _empty("syntax_error")
    found = _target(source, target_row.path, line)
    if found is None:
        return _empty("not_a_definition")
    target, node, in_class = found
    if not isinstance(node, ast.ClassDef):
        return _empty("not_a_class")
    assert source.table is not None
    wanted = _key(target)
    resolutions = _Resolutions(
        _RequestResolver(root, rows, scan, target_row.path, source.table), wanted
    )
    counts = dict.fromkeys(TypeHierarchySummary.model_fields, 0)
    counts["bases"] = len(node.bases)
    counts["keyword_arguments"] = len(node.keywords)

    # Bases first (bounded by MAX_BASES): every base expression of the target.
    bases, cut = _bases(node, target_row.path, source, resolutions, counts)
    bases_truncated = index_truncated or cut or scan.truncated
    scan.truncated = False  # from here on, it reports the subclasses' own budget exhaustion

    # Subclasses: Step 47's references of the target whose token is a base position.
    subclasses_status: SubclassesStatus = "unsupported_target" if in_class else "scanned"
    subclasses_out: list[SubclassEntry] | None = None
    subclasses_truncated: bool | None = None
    if not in_class:
        module_level = source.table.scope_of[id(node)].parent is None
        paths = [target_row.path]
        if module_level:  # only a module-level binding can be imported elsewhere
            for row in rows:
                if not scan.read(row.path):
                    break
            paths = sorted(p for p in {*scan.sources, target_row.path} if scan.sources.get(p))
        names, selected = _closure(scan, paths, node.name) if module_level else ({node.name}, paths)
        positions: dict[str, dict[int, ast.ClassDef]] = {}
        found_entries: list[tuple[tuple[Any, ...], SubclassEntry]] = []
        candidates = 0
        for path, token, parsed in _candidates(scan, selected, names):
            if candidates >= code_references.MAX_CANDIDATES:
                scan.truncated = True
                break
            candidates += 1
            try:
                answer = resolutions.resolve(path, parsed, token)
            except _ModuleBudget:
                counts["subclass_unsupported"] += 1
                scan.truncated = True
                continue
            if answer is None:
                continue
            if answer.unsupported:
                counts["subclass_unsupported"] += 1
                continue
            if not answer.has_target:
                continue
            if wanted not in answer.listed:
                scan.truncated = True  # Step 45 would cut the target from its list
                continue
            if path not in positions:  # indexed only once it holds a reference to the target
                positions[path] = _base_positions(parsed)
            assert parsed.table is not None
            reference = (
                parsed.table.names.get((token.line, token.start_byte))
                if answer.context == "name"
                else parsed.table.attributes.get((token.line, token.end_byte))
                if answer.context == "attribute"
                else None
            )
            subclass = positions[path].get(id(reference)) if reference is not None else None
            if subclass is None:
                counts["non_base_references"] += 1  # an import, a call, a value, an argument
                continue
            ambiguous = answer.total > 1
            counts["subclasses"] += 1
            counts["subclasses_ambiguous"] += ambiguous
            location = _class_location(path, subclass)
            site = SubclassSite(
                line=token.line,
                column=token.column + 1,
                name=_cap(answer.identifier),
                ambiguous=ambiguous,
            )
            order = (*_key(location), site.line, site.column)
            found_entries.append((order, SubclassEntry(subclass=location, site=site)))
        found_entries.sort(key=lambda item: item[0])
        subclasses_out = [entry for _, entry in found_entries[:MAX_SUBCLASSES]]
        subclasses_truncated = (
            index_truncated or scan.truncated or len(found_entries) > MAX_SUBCLASSES
        )

    return TypeHierarchyResponse(
        status="scanned",
        target=target,
        bases=bases,
        subclasses_status=subclasses_status,
        subclasses=subclasses_out,
        summary=TypeHierarchySummary(**{**counts, **scan.counts}),
        bases_truncated=bases_truncated,
        subclasses_truncated=subclasses_truncated,
    )


async def code_type_hierarchy(
    db: AsyncSession, repository: Repository, path: str, line: int
) -> TypeHierarchyResponse:
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
        analyze_type_hierarchy, root, target, indexed, line, len(python_rows) > MAX_INDEXED_PATHS
    )
