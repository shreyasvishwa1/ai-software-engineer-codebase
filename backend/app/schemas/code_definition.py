from typing import Literal

from pydantic import BaseModel

# Static go-to-definition for one position in a Python file. Deliberately excluded: source
# text, snippets, file ids, sha256 checksums and filesystem paths.

DefinitionStatus = Literal[
    "resolved",  # exactly one definition
    "ambiguous",  # two or more definitions
    "not_found",  # the name is bound nowhere, or only to code outside the repository
    "unsupported",  # an identifier, but its binding or form is outside static resolution
    "no_identifier",  # the position is not on an identifier
    "syntax_error",
    "too_large",
    "binary",
    "unsupported_language",
]


class DefinitionReference(BaseModel):
    path: str  # the requested file, relative to the repository root
    line: int  # 1-based
    column: int  # 1-based code-point column of the identifier's first character
    name: str  # the identifier as Python sees it (NFKC), at most 200 characters


class DefinitionLocation(BaseModel):
    path: str  # POSIX path relative to the repository root
    kind: Literal["class", "function", "method", "module"]
    name: str  # at most 200 characters
    line: int  # 1-based line of the class/def statement (1 for modules)
    end_line: int


class DefinitionResponse(BaseModel):
    status: DefinitionStatus
    reference: DefinitionReference | None  # None unless an identifier was found
    definitions: list[DefinitionLocation]  # sorted by (path, line); only resolved/ambiguous
    truncated: bool  # an index, module-file or definition limit was reached
