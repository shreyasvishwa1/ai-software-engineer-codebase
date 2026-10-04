from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.code_definition import DefinitionLocation
from app.schemas.code_type_hierarchy import BaseKind, BaseSite, SubclassSite

# Direct method overrides for one Python method: the same-name methods its class's direct
# bases define, and the same-name methods its class's direct subclasses define. Deliberately
# excluded: source text, snippets, decorators, file ids, sha256 checksums, filesystem paths
# and any AST or scope data.

MethodOverridesStatus = Literal[
    "scanned",  # the target is a method; its overrides were found
    "not_a_definition",  # no class/def statement starts on the requested line
    "not_a_method",  # a class, a module-level function or a function nested in a function
    "syntax_error",  # the target file
    "too_large",  # the target file
    "binary",  # the target file
    "unsupported_language",  # the target file
]

OverridingStatus = Literal[
    "scanned",  # the method's class is bound in a module or function scope
    "unsupported_target",  # a class nested in a class: its subclasses are not searched (Step 51)
]

MemberStatus = Literal[
    # The related class's own body binds the name with at least one def/async def (any
    # decorators); those definitions are listed.
    "defined",
    "not_defined",  # the related class's own body does not bind the name
    "not_a_method",  # the name is bound only by class statements
    # The name is bound only by assignments, imports or global/nonlocal declarations (never
    # followed), or the related class's file cannot be analyzed.
    "unsupported",
    "unavailable",  # the base is not a resolvable class (builtin, unresolved, dynamic, ...)
]


class OverriddenEntry(BaseModel):
    site: BaseSite | None  # Step 51's base site, a position in the target's file
    base_kind: BaseKind  # Step 51's kind of that base
    # One Step 51 class candidate of the base; None exactly when the status is unavailable.
    class_: DefinitionLocation | None = Field(serialization_alias="class")  # "class" in JSON
    ambiguous: bool  # Step 51 resolves the base to several definitions
    status: MemberStatus
    # The class's same-name methods, by line and cut at Step 45's MAX_DEFINITIONS; empty
    # unless defined.
    implementations: list[DefinitionLocation]
    implementations_ambiguous: bool  # the class body defines the name more than once


class OverridingEntry(BaseModel):
    subclass: DefinitionLocation  # one Step 51 direct subclass entry
    site: SubclassSite  # where it names the method's class, a position in subclass.path
    status: Literal["defined", "not_a_method", "unsupported"]
    implementations: list[DefinitionLocation]  # as for OverriddenEntry
    implementations_ambiguous: bool


class MethodOverridesSummary(BaseModel):
    # Counted before the response caps; all zero unless the status is scanned, and the
    # overriding counts are zero unless subclasses were searched.
    files_scanned: int  # Step 51's file counts for the repository scan
    too_large: int
    binary: int
    syntax_error: int
    bases: int  # Step 51's base count of the method's class
    base_classes: int  # class candidates examined in the overridden direction
    overridden_defined: int
    overridden_not_defined: int
    overridden_not_a_method: int
    overridden_unavailable: int
    overridden_unsupported: int
    subclasses: int  # Step 51's subclass count of the method's class
    overriding: int  # examined subclass entries defining the name
    inheriting: int  # examined subclass entries not binding the name (never listed)
    overriding_not_a_method: int
    overriding_unsupported: int


class MethodOverridesResponse(BaseModel):
    status: MethodOverridesStatus
    target: DefinitionLocation | None  # None unless scanned
    owner: DefinitionLocation | None  # the method's class; None unless scanned
    overridden: list[
        OverriddenEntry
    ]  # Step 51 base order, then candidate order; empty unless scanned
    overriding_status: OverridingStatus | None  # None unless scanned
    # Step 51 subclass order with duplicates, only entries binding the name; None unless searched.
    overriding: list[OverridingEntry] | None
    summary: MethodOverridesSummary
    # Step 51's bases truncation, an index, read, parse or definition limit, or
    # MAX_OVERRIDE_CLASSES was reached in the overridden direction.
    overridden_truncated: bool
    # None exactly when overriding is None; else the same for the overriding direction.
    overriding_truncated: bool | None
