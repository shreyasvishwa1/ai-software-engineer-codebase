"""Static call hierarchy for one Python definition: who calls it and what it calls.

Never guesses.

Target: the def/async def/class statement starting on the requested line (Step 47's
``_target``; Python allows at most one compound statement per line). Decorator and body
lines are ``not_a_definition``; modules are never targets.

Call site: the name in ``f(...)``, the final attribute name in ``a.b.f(...)``, or a bare
decorator ``@f`` / ``@m.f`` (Python calls it with the decorated definition). Its position
is the 1-based line and 1-based code-point column of that name token: exactly where Step
45's go-to-definition resolves it. Any other called expression (``f()()``, ``x[0]()``,
``getattr(o, n)()``, the result of ``@factory(...)``) is dynamic: counted, never resolved.
Call sites are syntactic; when Python evaluates them (annotations, lambdas, generator
expressions) is not modelled.

Unit: every call site belongs to the nearest enclosing def/async def, class body or module,
found from Step 45's scopes: lambda and comprehension scopes belong to their enclosing unit,
and decorators, defaults and annotations to the scope that evaluates them. A call Step 45
never visits (in a def's or class's PEP 695 type-parameter list, which it never resolves)
belongs to no unit.

Resolution: Step 45's own dispatch (Step 47's ``_resolve``) with Step 45's module-file
budget applied afresh to every call, so a site resolves here exactly as go-to-definition
resolves it. Answers are memoized within the request by exactly what Step 45 reads: the
file, the scope the name resolves in and the name or attribute chain (import statements and
chains longer than MAX_MEMO_CHAIN are never memoized). An entry keeps only Step 45's capped
definition list, and an answer degraded by a read or parse budget flags truncation again
whenever it is reused.

Callees: every call site owned by the target, grouped by each callable definition (class,
function, method) Step 45 returns; a site with several definitions (modules included, as
in Step 47) is listed under each with ``ambiguous``. A class call resolves to the class,
never ``__init__``; ``self.m()``, ``obj.m()`` and ``super().m()`` stay unsupported because
Step 45 has no type inference.

Callers: Step 47's references of the target whose token is a call site, grouped by the unit
owning the call; Step 47's loop, run through its own helpers. A target bound in a class
scope (a method or nested class) is not searched (``callers_status`` unsupported_target),
as in Step 47, and a target bound in a function scope can only be called from its own file.

Snapshot: one SELECT returns the target row and every Python row; Step 47's ``_Scan`` reads
each file at most once through Step 21's verified reader against its snapshot SHA-256,
callees first and then callers, so any change answers 409 and results never mix snapshots.
Work is bounded by Step 47's read, parse, candidate and alias budgets, Step 45's per-call
limits, MAX_CALLEE_CALLS and the response caps; each direction reports its own truncation.
Parsing is ``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode, imported or
executed. Nothing is stored, cached across requests or logged.
"""

import ast
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_call_hierarchy import (
    CalleeGroup,
    CallerGroup,
    CallersStatus,
    CallHierarchyResponse,
    CallHierarchyStatus,
    CallHierarchySummary,
    CallSite,
)
from app.schemas.code_definition import DefinitionLocation

# Step 45's resolver and limits (read at call time, so they stay Step 45's), Step 47's
# snapshot scan, closure and dispatch, and Steps 21, 37, 39 and 41's rules, reused unchanged.
from app.services import code_definition, code_references
from app.services.code_definition import (
    _FUNCTIONS,
    BUILTIN_NAMES,
    NameToken,
    Outcome,
    Scope,
    ScopeTable,
    _cap,
    _module_scope,
    _ModuleBudget,
    resolving_scope,
)
from app.services.code_importers import MAX_PARSED_BYTES, MAX_SCANNED_BYTES
from app.services.code_imports import MAX_INDEXED_PATHS, module_parts
from app.services.code_outline import SUPPORTED_LANGUAGE, _is_python
from app.services.code_references import (
    _candidates,
    _closure,
    _key,
    _RequestResolver,
    _resolve,
    _Scan,
    _Source,
    _target,
)
from app.services.file_content import FILE_NOT_FOUND, FileContentError
from app.services.ingestion import repository_storage_dir
from app.services.rate_limit import Limit

MAX_CALLEE_CALLS = 5000  # call sites owned by the target that are examined, in source order
MAX_CALLERS = 200  # caller groups returned
MAX_CALLEES = 200  # callee groups returned
MAX_SITES_PER_GROUP = 50  # call sites returned per group
MAX_SITES = 1000  # call sites returned per direction
MAX_MEMO_CHAIN = 32  # longer attribute chains are resolved afresh, never memoized
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
CALL_HIERARCHY_LIMIT = Limit("call-hierarchy-user", 20, 600)

Key = tuple[str, int, str, str]  # Step 47's definition key: (path, line, kind, name)
Order = tuple[int, int]  # (line, UTF-8 start byte): source order within one file


# --- Call sites and units --------------------------------------------------------------------


def _module_unit(path: str) -> DefinitionLocation:
    """Module-level code as a unit, named like Step 45's module definitions."""
    parts = module_parts(path)
    name = parts[-1] if parts else PurePosixPath(path).stem  # a root __init__ names no module
    return DefinitionLocation(path=path, kind="module", name=_cap(name), line=1, end_line=1)


class _Calls:
    """The call sites of one parsed file and the unit owning each, from one tree walk."""

    def __init__(self, path: str, source: _Source) -> None:
        tree, table = source.tree, source.table
        assert tree is not None and table is not None
        self.table: ScopeTable = table
        module = _module_unit(path)
        self._locations: dict[Key, DefinitionLocation] = {_key(module): module}
        self._kinds: dict[Key, tuple[Literal["class", "function", "method"], int]] = {}
        # id(scope) -> owning unit; lambda/comprehension scopes are added as they are met.
        self._units: dict[int, Key | None] = {id(table.module): _key(module)}
        self.callees: set[int] = set()  # ids of the Name/Attribute nodes naming a callee
        self.calls: list[tuple[ast.expr, ast.AST]] = []  # (called expression, its anchor)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                self._add(node.func, node)
            elif isinstance(node, (ast.ClassDef, *_FUNCTIONS)):
                self._define(path, node)
                for decorator in node.decorator_list:  # @d applies d to the definition
                    self._add(decorator, decorator)

    def _add(self, called: ast.expr, anchor: ast.AST) -> None:
        if isinstance(called, (ast.Name, ast.Attribute)):
            self.callees.add(id(called))
        self.calls.append((called, anchor))

    def _define(
        self, path: str, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        inner = self.table.scope_of.get(id(node.body[0]))  # the definition's own scope
        bound = self.table.scope_of.get(id(node))  # the scope it is bound in
        if inner is None or bound is None:
            return
        kind: Literal["class", "function", "method"] = (
            "class"
            if isinstance(node, ast.ClassDef)
            else "method"
            if bound.kind == "class"
            else "function"
        )
        key: Key = (path, node.lineno, kind, _cap(node.name))  # Step 47's _key of its location
        self._units[id(inner)] = key
        self._kinds[key] = (kind, node.end_lineno or node.lineno)

    def location(self, unit: Key) -> DefinitionLocation:
        """The unit's location, exactly Step 45's for a definition; built on first use."""
        if unit not in self._locations:
            path, line, _, name = unit
            kind, end_line = self._kinds[unit]
            self._locations[unit] = DefinitionLocation(
                path=path, kind=kind, name=name, line=line, end_line=end_line
            )
        return self._locations[unit]

    def unit_of(self, node: ast.AST) -> Key | None:
        """The unit owning ``node``: its scope, past any lambda or comprehension scopes.
        Memoized per scope, so a file costs O(scopes + sites) whatever the nesting."""
        scope: Scope | None = self.table.scope_of.get(id(node))
        passed: list[Scope] = []
        unit: Key | None = None
        while scope is not None:
            if id(scope) in self._units:
                unit = self._units[id(scope)]
                break
            if scope.kind not in ("lambda", "comprehension"):
                break  # a def or class scope Step 45 has but this walk has not: no unit
            passed.append(scope)
            scope = scope.parent
        for skipped in passed:
            self._units[id(skipped)] = unit
        return unit


@dataclass
class _Site:
    order: Order
    node: ast.expr | None  # the Name/Attribute naming the callee; None when dynamic
    token: NameToken | None  # the token Step 45 resolves at; None when dynamic


def _owned_sites(calls: _Calls, source: _Source, unit: Key) -> list[_Site]:
    """The call sites owned by ``unit``, in source order."""
    tokens = source.tokens
    assert tokens is not None
    by_start = {(t.line, t.start_byte): t for t in tokens}
    by_end = {(t.line, t.end_byte): t for t in tokens}
    sites: list[_Site] = []
    for called, anchor in calls.calls:
        named = isinstance(called, (ast.Name, ast.Attribute))
        if calls.unit_of(called if named else anchor) != unit:
            continue
        token: NameToken | None = None
        order: Order = (called.lineno, called.col_offset)
        if isinstance(called, ast.Name):
            token = by_start.get((called.lineno, called.col_offset))
        elif isinstance(called, ast.Attribute) and called.end_lineno is not None:
            token = by_end.get((called.end_lineno, called.end_col_offset or 0))
            if token is not None and unicodedata.normalize("NFKC", token.text) != called.attr:
                token = None
            if token is not None:
                order = (token.line, token.start_byte)
        sites.append(_Site(order, called if named else None, token))
    sites.sort(key=lambda site: site.order)  # stable: equal positions keep the walk order
    return sites


# --- Resolution ------------------------------------------------------------------------------


@dataclass
class _Answer:
    """Step 45's answer at one token, kept small: its size never depends on how many
    definitions Step 45 found."""

    identifier: str
    context: str  # 'name', 'attribute' or 'import' (Step 47's dispatch)
    unsupported: bool
    total: int  # distinct definitions, before Step 45's MAX_DEFINITIONS cut
    listed: list[Key]  # Step 45's list: sorted (path, line, kind, name), then cut
    definitions: dict[Key, DefinitionLocation]  # the listed ones
    has_target: bool  # the request's target is among all the definitions


_BUDGET = object()  # memo entry: Step 45's module-file budget was exceeded


class _Resolutions:
    """Step 45 answers for one request, each computed once per (file, resolving scope, name
    or attribute chain): exactly what Step 45 reads, since every call starts a fresh module
    budget and reads and parses are final within the request."""

    def __init__(self, resolver: _RequestResolver, target: Key) -> None:
        self.resolver = resolver
        self.target = target
        # key -> (answer, None or _BUDGET; whether a read or parse budget degraded it)
        self.memo: dict[tuple[Any, ...], tuple[_Answer | None | object, bool]] = {}

    @staticmethod
    def _memo_key(path: str, source: _Source, token: NameToken) -> tuple[Any, ...] | None:
        table = source.table
        assert table is not None
        name_node = table.names.get((token.line, token.start_byte))
        if name_node is not None:  # as _resolve: a Name first
            scope = table.scope_of[id(name_node)]
            return (path, "name", id(resolving_scope(scope, name_node.id)), name_node.id)
        attribute = table.attributes.get((token.line, token.end_byte))
        if attribute is None or attribute.attr != unicodedata.normalize("NFKC", token.text):
            return None  # an import name or no reference: resolved every time
        chain: list[str] = []
        value: ast.expr = attribute.value
        while isinstance(value, ast.Attribute):
            if len(chain) >= MAX_MEMO_CHAIN:
                return None  # bounded key size: resolved afresh
            chain.append(value.attr)
            value = value.value
        if not isinstance(value, ast.Name):
            return None  # Step 45 answers unsupported at once
        scope = table.scope_of[id(attribute)]
        found = resolving_scope(scope, value.id)
        return (path, "attribute", id(found), value.id, tuple(chain), attribute.attr)

    def resolve(self, path: str, source: _Source, token: NameToken) -> _Answer | None:
        """Step 45 at ``token`` of ``path``; raises _ModuleBudget as Step 45 would."""
        scan = self.resolver.scan
        key = self._memo_key(path, source, token)
        if key is not None and key in self.memo:
            known, degraded = self.memo[key]
            if degraded:
                scan.truncated = True  # as the budget would refuse the same read again
            if known is _BUDGET:
                raise _ModuleBudget
            assert known is None or isinstance(known, _Answer)
            return known
        before, scan.truncated = scan.truncated, False
        self.resolver.begin(path)
        try:
            resolved = _resolve(self.resolver, source, path, token)
        except _ModuleBudget:
            degraded = scan.truncated
            scan.truncated = before or degraded
            if key is not None:
                self.memo[key] = (_BUDGET, degraded)
            raise
        degraded = scan.truncated
        scan.truncated = before or degraded
        answer = None if resolved is None else self._answer(*resolved)
        if key is not None:
            self.memo[key] = (answer, degraded)
        return answer

    def _answer(self, identifier: str, context: str, outcome: Outcome) -> _Answer:
        unique = {_key(d): d for d in outcome.definitions}
        listed = sorted(unique)[: code_definition.MAX_DEFINITIONS]
        return _Answer(
            identifier=identifier,
            context=context,
            unsupported=outcome.unsupported,
            total=len(unique),
            listed=listed,
            definitions={key: unique[key] for key in listed},
            has_target=self.target in unique,
        )


def _is_builtin(site: _Site, table: ScopeTable) -> bool:
    """Step 45 answers unsupported because the called (root) name is an unbound builtin."""
    root: ast.expr | None = site.node
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name) or site.node is None:
        return False
    scope = table.scope_of.get(id(site.node))
    if scope is None:
        return False
    return (
        resolving_scope(scope, root.id) is None
        and not _module_scope(scope).star_import
        and root.id in BUILTIN_NAMES
    )


# --- Groups ----------------------------------------------------------------------------------


@dataclass
class _Groups:
    locations: dict[Key, DefinitionLocation] = field(default_factory=dict)
    calls: dict[Key, list[tuple[Order, CallSite]]] = field(default_factory=dict)

    def add(self, location: DefinitionLocation, order: Order, call: CallSite) -> None:
        key = _key(location)
        self.locations.setdefault(key, location)
        self.calls.setdefault(key, []).append((order, call))

    def cut(
        self, max_groups: int
    ) -> tuple[list[tuple[DefinitionLocation, int, list[CallSite]]], bool]:
        """Groups by (path, line, kind, name) and their sites by position, then the response
        caps: (location, total calls, listed calls) per group, and whether anything was cut."""
        out: list[tuple[DefinitionLocation, int, list[CallSite]]] = []
        truncated = len(self.calls) > max_groups
        remaining = MAX_SITES
        for key in sorted(self.calls)[:max_groups]:
            calls = [call for _, call in sorted(self.calls[key], key=lambda item: item[0])]
            listed = calls[:MAX_SITES_PER_GROUP]
            if len(listed) > remaining:
                listed = listed[:remaining]
            if len(listed) < len(calls):
                truncated = True
            if not listed:
                truncated = True
                break
            remaining -= len(listed)
            out.append((self.locations[key], len(calls), listed))
        return out, truncated


def _call(token: NameToken, identifier: str, ambiguous: bool) -> CallSite:
    return CallSite(
        line=token.line, column=token.column + 1, name=_cap(identifier), ambiguous=ambiguous
    )


# --- Request ---------------------------------------------------------------------------------


def _empty(status: CallHierarchyStatus) -> CallHierarchyResponse:
    zero = dict.fromkeys(CallHierarchySummary.model_fields, 0)
    return CallHierarchyResponse(
        status=status,
        target=None,
        callers_status=None,
        callers=None,
        callees=[],
        summary=CallHierarchySummary(**zero),
        callers_truncated=None,
        callees_truncated=False,
    )


def analyze_call_hierarchy(
    root: Path,
    target_row: Any,
    rows: Sequence[Any],
    line: int,
    index_truncated: bool = False,
) -> CallHierarchyResponse:
    """Callers and callees of the definition starting on ``line`` of ``target_row``; ``rows``
    are the snapshot's indexed Python rows sorted by path. Raises FileContentError(409) on
    any change."""
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
    assert source.table is not None
    wanted = _key(target)
    resolutions = _Resolutions(
        _RequestResolver(root, rows, scan, target_row.path, source.table), wanted
    )
    counts = dict.fromkeys(CallHierarchySummary.model_fields, 0)
    files: dict[str, _Calls] = {target_row.path: _Calls(target_row.path, source)}

    # Callees first (bounded by MAX_CALLEE_CALLS): every call site the target owns.
    callees_truncated = index_truncated
    own = _owned_sites(files[target_row.path], source, wanted)
    if len(own) > MAX_CALLEE_CALLS:
        own, callees_truncated = own[:MAX_CALLEE_CALLS], True
    callees = _Groups()
    for site in own:
        if site.node is None:
            counts["callee_dynamic"] += 1
            continue
        if site.token is None:
            counts["callee_unsupported"] += 1  # no token to resolve at
            continue
        try:
            answer = resolutions.resolve(target_row.path, source, site.token)
        except _ModuleBudget:
            counts["callee_unsupported"] += 1
            callees_truncated = True  # Step 45: unsupported and truncated
            continue
        if answer is None:
            counts["callee_unsupported"] += 1
            continue
        if answer.unsupported:
            counts[
                "callee_builtin" if _is_builtin(site, source.table) else "callee_unsupported"
            ] += 1
            continue
        if not answer.total:
            counts["callee_unresolved"] += 1
            continue
        if answer.total > len(answer.listed):
            callees_truncated = True  # Step 45 cuts its list the same way
        callable_keys = [key for key in answer.listed if key[2] != "module"]
        if not callable_keys:
            counts["callee_not_callable"] += 1
            continue
        ambiguous = answer.total > 1
        counts["callee_calls"] += 1
        counts["callee_ambiguous"] += ambiguous
        call = _call(site.token, answer.identifier, ambiguous)
        for key in callable_keys:
            callees.add(answer.definitions[key], site.order, call)
    callees_truncated = callees_truncated or scan.truncated
    scan.truncated = False  # from here on, it reports the callers' own budget exhaustion

    # Callers: Step 47's references of the target whose token is a call site.
    callers_status: CallersStatus = "unsupported_target" if in_class else "scanned"
    callers_out: list[CallerGroup] | None = None
    callers_truncated: bool | None = None
    if not in_class:
        assert isinstance(node, (ast.ClassDef, *_FUNCTIONS))
        module_level = source.table.scope_of[id(node)].parent is None
        paths = [target_row.path]
        if module_level:  # only a module-level binding can be imported elsewhere
            for row in rows:
                if not scan.read(row.path):
                    break
            paths = sorted(p for p in {*scan.sources, target_row.path} if scan.sources.get(p))
        names, selected = _closure(scan, paths, node.name) if module_level else ({node.name}, paths)
        callers = _Groups()
        candidates = 0
        for path, token, parsed in _candidates(scan, selected, names):
            if candidates >= code_references.MAX_CANDIDATES:
                scan.truncated = True
                break
            candidates += 1
            try:
                answer = resolutions.resolve(path, parsed, token)
            except _ModuleBudget:
                counts["caller_unsupported"] += 1
                scan.truncated = True
                continue
            if answer is None:
                continue
            if answer.unsupported:
                counts["caller_unsupported"] += 1
                continue
            if not answer.has_target:
                continue
            if wanted not in answer.listed:
                scan.truncated = True  # Step 45 would cut the target from its list
                continue
            if path not in files:  # indexed only once it holds a reference to the target
                files[path] = _Calls(path, parsed)
            calls = files[path]
            assert parsed.table is not None
            reference = (
                parsed.table.names.get((token.line, token.start_byte))
                if answer.context == "name"
                else parsed.table.attributes.get((token.line, token.end_byte))
                if answer.context == "attribute"
                else None
            )
            unit = (
                calls.unit_of(reference)
                if reference is not None and id(reference) in calls.callees
                else None
            )
            if unit is None:
                counts["non_call_references"] += 1  # an import, a value, an argument
                continue
            ambiguous = answer.total > 1
            counts["caller_calls"] += 1
            counts["caller_ambiguous"] += ambiguous
            callers.add(
                calls.location(unit),
                (token.line, token.start_byte),
                _call(token, answer.identifier, ambiguous),
            )
        caller_groups, cut = callers.cut(MAX_CALLERS)
        callers_out = [
            CallerGroup(caller=location, total_calls=total, calls=listed)
            for location, total, listed in caller_groups
        ]
        callers_truncated = index_truncated or scan.truncated or cut

    callee_groups, cut = callees.cut(MAX_CALLEES)
    return CallHierarchyResponse(
        status="scanned",
        target=target,
        callers_status=callers_status,
        callers=callers_out,
        callees=[
            CalleeGroup(callee=location, total_calls=total, calls=listed)
            for location, total, listed in callee_groups
        ],
        summary=CallHierarchySummary(**{**counts, **scan.counts}),
        callers_truncated=callers_truncated,
        callees_truncated=callees_truncated or cut,
    )


async def code_call_hierarchy(
    db: AsyncSession, repository: Repository, path: str, line: int
) -> CallHierarchyResponse:
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
        analyze_call_hierarchy, root, target, indexed, line, len(python_rows) > MAX_INDEXED_PATHS
    )
