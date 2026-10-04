from typing import Literal

from pydantic import BaseModel

from app.schemas.code_definition import DefinitionLocation

# Static call hierarchy for one Python definition. Deliberately excluded: source text,
# snippets, file ids, sha256 checksums, filesystem paths and any AST or scope data.

CallHierarchyStatus = Literal[
    "scanned",  # the target is a definition; its callees (and callers, see below) were found
    "not_a_definition",  # no class/def statement starts on the requested line
    "syntax_error",  # the target file
    "too_large",  # the target file
    "binary",  # the target file
    "unsupported_language",  # the target file
]

CallersStatus = Literal[
    "scanned",  # the target is bound in a module or function scope: callers were searched
    "unsupported_target",  # a method or nested class: its callers are attribute accesses
]


class CallSite(BaseModel):
    # Positions in the file that contains the call: caller.path for a caller group's calls,
    # the response's target.path for a callee group's calls (never callee.path).
    line: int  # 1-based
    column: int  # 1-based code-point column of the called name (where Step 45 resolves it)
    name: str  # the called identifier as Python sees it (NFKC), at most 200 characters
    ambiguous: bool  # go-to-definition here also returns other definitions


class CallerGroup(BaseModel):
    caller: DefinitionLocation  # the unit that makes the calls: a def, a class body or a module
    total_calls: int  # this unit's call sites of the target, before the response caps
    calls: list[CallSite]  # positions in caller.path, by (line, column)


class CalleeGroup(BaseModel):
    callee: DefinitionLocation  # a called class, function or method (never a module)
    total_calls: int  # the target's call sites resolving here, before the response caps
    calls: list[CallSite]  # positions in the target's file, by (line, column)


class CallHierarchySummary(BaseModel):
    # Distinct call sites or files, counted before the response caps; all zero unless the
    # status is scanned, and caller_* / non_call_references are zero unless callers were
    # searched.
    files_scanned: int  # files read
    too_large: int
    binary: int
    syntax_error: int
    caller_calls: int  # call sites of the target
    caller_ambiguous: int  # of which go-to-definition also returns other definitions
    caller_unsupported: int  # same-name tokens whose resolution is unsupported (as Step 47)
    non_call_references: int  # references of the target that are not calls (imports, values)
    callee_calls: int  # the target's call sites resolving to at least one callable definition
    callee_ambiguous: int  # of which go-to-definition also returns other definitions
    callee_unsupported: int  # unsupported resolution (variables, parameters, self.x, ...)
    callee_builtin: int  # calls of Python builtins (print, len, eval, ...)
    callee_unresolved: int  # defined outside the repository or nowhere
    callee_not_callable: int  # resolving only to modules
    callee_dynamic: int  # not a name call: f()(), x[0](), getattr(o, n)(), @factory(...)


class CallHierarchyResponse(BaseModel):
    status: CallHierarchyStatus
    target: DefinitionLocation | None  # None unless scanned
    callers_status: CallersStatus | None  # None unless scanned
    callers: list[CallerGroup] | None  # by (path, line, kind, name); None unless searched
    callees: list[CalleeGroup]  # by (path, line, kind, name); empty unless scanned
    summary: CallHierarchySummary
    # None exactly when callers is None; else an index, read, parse, candidate, alias,
    # module-file, definition or response limit was reached while finding callers.
    callers_truncated: bool | None
    # An index, read, parse, module-file or definition limit, MAX_CALLEE_CALLS or a response
    # cap was reached while finding callees.
    callees_truncated: bool
