from typing import Literal

from pydantic import BaseModel

from app.schemas.code_definition import DefinitionLocation

# Static find-references for one Python definition. Deliberately excluded: source text,
# snippets, file ids, sha256 checksums and filesystem paths.

ReferencesStatus = Literal[
    "scanned",  # the target is a supported definition and the repository was scanned
    "not_a_definition",  # no class/def statement starts on the requested line
    "unsupported_target",  # a method or nested class: its uses are attribute accesses
    "syntax_error",  # the target file
    "too_large",  # the target file
    "binary",  # the target file
    "unsupported_language",  # the target file
]


class ReferenceLocation(BaseModel):
    path: str  # POSIX path relative to the repository root
    line: int  # 1-based
    column: int  # 1-based code-point column of the identifier's first character
    name: str  # the identifier as Python sees it (NFKC), at most 200 characters
    context: Literal["name", "attribute", "import"]
    ambiguous: bool  # go-to-definition here also returns other definitions


class ReferencesSummary(BaseModel):
    files_scanned: int  # files read
    too_large: int
    binary: int
    syntax_error: int
    references: int  # before the MAX_REFERENCES cap
    ambiguous: int  # before the MAX_REFERENCES cap
    unsupported: int  # same-name uses whose resolution is unsupported


class ReferencesResponse(BaseModel):
    status: ReferencesStatus
    target: DefinitionLocation | None  # None unless scanned or unsupported_target
    references: list[ReferenceLocation]  # sorted by (path, line, column)
    summary: ReferencesSummary  # all zero unless scanned
    truncated: bool  # an index, read, parse, candidate, name or reference limit was reached
