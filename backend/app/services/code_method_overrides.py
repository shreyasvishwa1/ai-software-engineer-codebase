"""Static direct method overrides for one Python method. Never guesses; no MRO, no transitive
chains.

Target: the def/async def statement starting on the requested line (Step 47's ``_target``)
when it is bound in a class scope: a method, as Step 45 calls it, whatever its decorators.
A class, a module-level function or a function nested in a function is ``not_a_method``;
decorator, body and blank lines are ``not_a_definition``. Its owner is the class whose own
scope binds it.

Relationships: Step 51's direct bases and direct subclasses of the owner, from the frozen
``analyze_type_hierarchy`` called unchanged (phase 1): its base order, candidates, ambiguity,
subclass order, duplicates, nested-class rule and truncation.

Implementation identity: a related class implements the method exactly when its own class
scope (Step 45's bindings, which include defs inside if/try/with blocks of the class body)
binds the method's name with an actual ``def``/``async def``. Decorators are never
interpreted; imports, assignments and declarations are never followed or counted, and a
class statement of that name is not a method. Nothing else of the class (its bases, its
instances, ``self.x = ...``) is considered.

Snapshot: one SELECT returns the target row and every Python row. Phase 1 reads through
Step 51's own scan; phase 2 reads only the target file and the related classes' files
through a second Step 47 ``_Scan`` over the same rows. Both verify every read against the
same snapshot SHA-256, so any change answers 409 and results never mix snapshots. Work is
bounded by Step 51's limits, the phase-2 read and parse budgets, MAX_OVERRIDE_CLASSES and
Step 45's MAX_DEFINITIONS; each direction reports its own truncation. Parsing is
``ast.parse`` and ``tokenize`` only: nothing is compiled to bytecode, imported or executed.
Nothing is stored, cached across requests or logged.
"""

import ast
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.code_definition import DefinitionLocation
from app.schemas.code_method_overrides import (
    MemberStatus,
    MethodOverridesResponse,
    MethodOverridesStatus,
    MethodOverridesSummary,
    OverriddenEntry,
    OverridingEntry,
    OverridingStatus,
)

# Step 45's scopes and limits (read at call time), Step 47's snapshot scan and target, Step
# 51's type hierarchy and Steps 21, 37, 39 and 41's rules, all reused unchanged.
from app.services import code_definition
from app.services.code_definition import _FUNCTIONS, ScopeTable, _cap
from app.services.code_importers import MAX_PARSED_BYTES, MAX_SCANNED_BYTES
from app.services.code_imports import MAX_INDEXED_PATHS
from app.services.code_outline import SUPPORTED_LANGUAGE, _is_python
from app.services.code_references import _key, _Scan, _Source, _target
from app.services.code_type_hierarchy import analyze_type_hierarchy
from app.services.file_content import FILE_NOT_FOUND, FileContentError
from app.services.ingestion import repository_storage_dir
from app.services.rate_limit import Limit

MAX_OVERRIDE_CLASSES = 1000  # related classes inspected per request, overridden direction first
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
METHOD_OVERRIDES_LIMIT = Limit("method-overrides-user", 20, 600)


# --- Classes and their own bindings -------------------------------------------------------


def _own_scope(table: ScopeTable, node: ast.ClassDef) -> Any:
    """The class's own scope (the scope its body statements are evaluated in)."""
    return table.scope_of.get(id(node.body[0]))


def _location(
    path: str, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
) -> DefinitionLocation:
    """Exactly Step 45's location for a class or a def bound in a class scope."""
    return DefinitionLocation(
        path=path,
        kind="class" if isinstance(node, ast.ClassDef) else "method",
        name=_cap(node.name),
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
    )


class _Members:
    """Same-name method definitions of related classes, from phase 2's verified reads."""

    def __init__(self, scan: _Scan, name: str) -> None:
        self.scan = scan
        self.name = name
        self.classes: dict[str, dict[int, ast.ClassDef]] = {}  # path -> class line -> node
        self.truncated = False  # a read, parse or definition limit was reached

    def _class(self, path: str, line: int) -> tuple[ast.ClassDef, _Source] | None:
        if path not in self.scan.rows:
            return None
        if not self.scan.read(path):
            self.truncated = True  # the read budget is exhausted
            return None
        source = self.scan.parse(path)
        if source is None or source.tree is None:
            if self.scan.parse_exhausted:
                self.truncated = True  # the parse budget is exhausted
            return None  # too large, binary or a syntax error: cannot be inspected
        if path not in self.classes:  # indexed once per file and request
            self.classes[path] = {
                n.lineno: n for n in ast.walk(source.tree) if isinstance(n, ast.ClassDef)
            }
        node = self.classes[path].get(line)
        return (node, source) if node is not None else None

    def of(
        self, related: DefinitionLocation
    ) -> tuple[MemberStatus, list[DefinitionLocation], bool]:
        """(status, implementations, implementations_ambiguous) of one related class."""
        found = self._class(related.path, related.line)
        if found is None:
            return "unsupported", [], False
        node, source = found
        assert source.table is not None
        scope = _own_scope(source.table, node)
        bindings = scope.bindings.get(self.name, []) if scope is not None else []
        if not bindings:
            return "not_defined", [], False
        defs = [b.node for b in bindings if b.kind == "def" and isinstance(b.node, _FUNCTIONS)]
        if defs:
            implementations = sorted(
                (_location(related.path, d) for d in defs), key=lambda d: d.line
            )
            if len(implementations) > code_definition.MAX_DEFINITIONS:
                self.truncated = True  # cut as Step 45 cuts its list
            return (
                "defined",
                implementations[: code_definition.MAX_DEFINITIONS],
                len(implementations) > 1,
            )
        if all(b.kind == "class" for b in bindings):
            return "not_a_method", [], False
        return "unsupported", [], False  # assignments, imports, declarations: never followed


# --- Request ---------------------------------------------------------------------------------


def _empty(status: MethodOverridesStatus) -> MethodOverridesResponse:
    zero = dict.fromkeys(MethodOverridesSummary.model_fields, 0)
    return MethodOverridesResponse(
        status=status,
        target=None,
        owner=None,
        overridden=[],
        overriding_status=None,
        overriding=None,
        summary=MethodOverridesSummary(**zero),
        overridden_truncated=False,
        overriding_truncated=None,
    )


def analyze_method_overrides(
    root: Path,
    target_row: Any,
    rows: Sequence[Any],
    line: int,
    index_truncated: bool = False,
) -> MethodOverridesResponse:
    """Direct overrides of the method starting on ``line`` of ``target_row``; ``rows`` are the
    snapshot's indexed Python rows sorted by path. Raises FileContentError(409) on any change."""
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
    if not isinstance(node, _FUNCTIONS) or not in_class:
        return _empty("not_a_method")
    assert source.tree is not None and source.table is not None
    scope = source.table.scope_of[id(node)]  # the class scope binding the method
    owner_node = next(
        n
        for n in ast.walk(source.tree)
        if isinstance(n, ast.ClassDef) and _own_scope(source.table, n) is scope
    )
    owner = _location(target_row.path, owner_node)

    # Phase 1: Step 51's direct bases and subclasses of the owner, unchanged.
    hierarchy = analyze_type_hierarchy(root, target_row, rows, owner.line, index_truncated)
    assert hierarchy.status == "scanned" and hierarchy.target is not None
    assert _key(hierarchy.target) == _key(owner)

    members = _Members(scan, node.name)
    counts = dict.fromkeys(MethodOverridesSummary.model_fields, 0)
    for name in ("files_scanned", "too_large", "binary", "syntax_error", "bases", "subclasses"):
        counts[name] = getattr(hierarchy.summary, name)
    inspected = 0

    # Phase 2, overridden direction: every Step 51 base, each class candidate inspected.
    overridden: list[OverriddenEntry] = []
    overridden_cut = False
    for base in hierarchy.bases:
        classes = [d for d in base.definitions if d.kind == "class"]
        if base.kind not in ("resolved", "ambiguous") or not classes:
            counts["overridden_unavailable"] += 1
            overridden.append(
                OverriddenEntry(
                    site=base.site,
                    base_kind=base.kind,
                    class_=None,
                    ambiguous=base.kind == "ambiguous",
                    status="unavailable",
                    implementations=[],
                    implementations_ambiguous=False,
                )
            )
            continue
        for related in classes:
            if inspected >= MAX_OVERRIDE_CLASSES:
                overridden_cut = True
                break
            inspected += 1
            status, implementations, several = members.of(related)
            counts["base_classes"] += 1
            counts[f"overridden_{status}"] += 1
            overridden.append(
                OverriddenEntry(
                    site=base.site,
                    base_kind=base.kind,
                    class_=related,
                    ambiguous=base.kind == "ambiguous",
                    status=status,
                    implementations=implementations,
                    implementations_ambiguous=several,
                )
            )
        if overridden_cut:
            break
    overridden_truncated = hierarchy.bases_truncated or overridden_cut or members.truncated
    members.truncated = False  # from here on, it reports the overriding direction's limits

    # Phase 2, overriding direction: every Step 51 subclass entry defining the name.
    overriding_status: OverridingStatus = (
        "scanned" if hierarchy.subclasses_status == "scanned" else "unsupported_target"
    )
    overriding: list[OverridingEntry] | None = None
    overriding_truncated: bool | None = None
    if overriding_status == "scanned" and hierarchy.subclasses is not None:
        overriding = []
        overriding_cut = False
        for entry in hierarchy.subclasses:
            if inspected >= MAX_OVERRIDE_CLASSES:
                overriding_cut = True
                break
            inspected += 1
            status, implementations, several = members.of(entry.subclass)
            if status == "not_defined":
                counts["inheriting"] += 1  # inherits the method: never an override
                continue
            assert status in ("defined", "not_a_method", "unsupported")
            listed = cast(Literal["defined", "not_a_method", "unsupported"], status)
            counts["overriding" if status == "defined" else f"overriding_{status}"] += 1
            overriding.append(
                OverridingEntry(
                    subclass=entry.subclass,
                    site=entry.site,
                    status=listed,
                    implementations=implementations,
                    implementations_ambiguous=several,
                )
            )
        overriding_truncated = bool(
            hierarchy.subclasses_truncated or overriding_cut or members.truncated
        )

    return MethodOverridesResponse(
        status="scanned",
        target=target,
        owner=owner,
        overridden=overridden,
        overriding_status=overriding_status,
        overriding=overriding,
        summary=MethodOverridesSummary(**counts),
        overridden_truncated=overridden_truncated,
        overriding_truncated=overriding_truncated,
    )


async def code_method_overrides(
    db: AsyncSession, repository: Repository, path: str, line: int
) -> MethodOverridesResponse:
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
        analyze_method_overrides,
        root,
        target,
        indexed,
        line,
        len(python_rows) > MAX_INDEXED_PATHS,
    )
