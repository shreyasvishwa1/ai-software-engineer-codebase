from typing import Literal

from pydantic import BaseModel

from app.schemas.code_definition import DefinitionReference

# In-file name occurrences for one position of a Python file: every occurrence of the binding
# the identifier there refers to, under the Step 45 scope model. Deliberately excluded: source
# text, snippets, file ids, sha256 checksums, filesystem paths and any AST or scope data.

OccurrencesStatus = Literal[
    "found",  # the identifier's binding is supported; its occurrences are listed
    "not_bound",  # no binding in the file (a builtin, bound nowhere, or from a star import)
    "unsupported",  # an identifier or binding outside v1 (attribute, import, global, ...)
    "no_identifier",  # the position is not on an identifier
    "syntax_error",
    "too_large",
    "binary",
    "unsupported_language",
]

OccurrenceKind = Literal[
    "read",  # a load
    "write",  # a store: assignment targets, augmented/annotated assignment, walrus, except name
    "delete",  # a del target
    "parameter",  # a parameter of the binding's function or lambda
    "definition",  # a def/class name
]

# Step 45's scope kinds: the scope that holds the binding.
OccurrenceScope = Literal["module", "function", "class", "lambda", "comprehension"]


class Occurrence(BaseModel):
    line: int  # 1-based
    column: int  # 1-based code-point column of the identifier's first character
    kind: OccurrenceKind


class OccurrencesSummary(BaseModel):
    # Counted before the response cap; all zero unless the status is "found".
    occurrences: int
    reads: int
    writes: int
    deletes: int
    bindings: int  # parameter and definition sites


class OccurrencesResponse(BaseModel):
    status: OccurrencesStatus
    reference: DefinitionReference | None  # None unless an identifier was found
    scope: OccurrenceScope | None  # None unless the status is "found"
    occurrences: list[Occurrence]  # source order, at most 1000; empty unless "found"
    summary: OccurrencesSummary
    truncated: bool  # more than 1000 occurrences were found
