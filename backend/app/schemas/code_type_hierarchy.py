from typing import Literal

from pydantic import BaseModel

from app.schemas.code_definition import DefinitionLocation

# Static type (inheritance) hierarchy for one Python class: its direct bases and its direct
# subclasses. Deliberately excluded: source text, snippets, file ids, sha256 checksums,
# filesystem paths and any AST or scope data.

TypeHierarchyStatus = Literal[
    "scanned",  # the target is a class; its bases (and subclasses, see below) were found
    "not_a_definition",  # no class/def statement starts on the requested line
    "not_a_class",  # a def/async def starts on the requested line
    "syntax_error",  # the target file
    "too_large",  # the target file
    "binary",  # the target file
    "unsupported_language",  # the target file
]

SubclassesStatus = Literal[
    "scanned",  # the target is bound in a module or function scope: subclasses were searched
    "unsupported_target",  # a class nested in a class: its subclasses name it as Outer.Inner
]

BaseKind = Literal[
    "resolved",  # Step 45 returns exactly one definition, a class
    "ambiguous",  # Step 45 returns two or more definitions (modules included), a class among
    # them; every one is listed and each is a candidate, none is chosen
    "not_a_class",  # Step 45 returns only functions or modules
    "builtin",  # an unbound Python builtin (object, Exception, ...)
    "unsupported",  # Step 45 answers unsupported (variables, parameters, from pkg import mod)
    "unresolved",  # bound nowhere, or only to code outside the repository (typing.Generic)
    "dynamic",  # not a name, attribute or subscripted one: *bases, B if c else C, get_base()
]


class BaseSite(BaseModel):
    # A position in the target's file (the response's target.path).
    line: int  # 1-based
    column: int  # 1-based code-point column of the base name (where Step 45 resolves it)
    name: str  # the base identifier as Python sees it (NFKC), at most 200 characters


class BaseEntry(BaseModel):
    site: BaseSite | None  # None exactly when the kind is dynamic
    kind: BaseKind
    # Step 45's definitions at the site, in its order (path, line, kind, name) and cut at its
    # MAX_DEFINITIONS; empty unless resolved, ambiguous or not_a_class.
    definitions: list[DefinitionLocation]


class SubclassSite(BaseModel):
    # A position in the subclass's file (subclass.path).
    line: int  # 1-based
    column: int  # 1-based code-point column of the base name naming the target
    name: str  # the identifier as Python sees it (NFKC), at most 200 characters
    ambiguous: bool  # go-to-definition here also returns other definitions


class SubclassEntry(BaseModel):
    subclass: DefinitionLocation  # the class whose base list names the target
    site: SubclassSite


class TypeHierarchySummary(BaseModel):
    # Distinct bases, sites or files, counted before the response caps; all zero unless the
    # status is scanned, and the subclass counts are zero unless subclasses were searched.
    files_scanned: int  # files read
    too_large: int
    binary: int
    syntax_error: int
    bases: int  # base expressions of the target, duplicates included
    bases_resolved: int  # of the examined ones (the first MAX_BASES), by kind
    bases_ambiguous: int
    bases_builtin: int
    bases_unresolved: int
    bases_unsupported: int
    bases_dynamic: int
    bases_not_a_class: int
    keyword_arguments: int  # metaclass= and other class keywords: never bases
    subclasses: int  # base positions naming the target
    subclasses_ambiguous: int  # of which go-to-definition also returns other definitions
    subclass_unsupported: int  # same-name tokens whose resolution is unsupported (as Step 47)
    non_base_references: int  # references of the target that are not base positions


class TypeHierarchyResponse(BaseModel):
    status: TypeHierarchyStatus
    target: DefinitionLocation | None  # None unless scanned
    bases: list[BaseEntry]  # source order, duplicates kept; empty unless scanned
    subclasses_status: SubclassesStatus | None  # None unless scanned
    # By subclass (path, line, kind, name), then site (line, column); None unless searched.
    subclasses: list[SubclassEntry] | None
    summary: TypeHierarchySummary
    # An index, read, parse, module-file or definition limit, or MAX_BASES, was reached while
    # resolving the bases.
    bases_truncated: bool
    # None exactly when subclasses is None; else an index, read, parse, candidate, alias,
    # module-file, definition or MAX_SUBCLASSES limit was reached while finding subclasses.
    subclasses_truncated: bool | None
