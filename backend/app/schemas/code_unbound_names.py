from pydantic import BaseModel

from app.schemas.code_outline import OutlineStatus

# Statically unbound names of one Python file: identifier loads with no statically known
# binding under the Step 45 scope model and the Step 55 rules. This is not a runtime NameError
# detector. Deliberately excluded: source text, snippets, file ids, sha256 checksums,
# filesystem paths and any AST or scope data.


class UnboundName(BaseModel):
    line: int  # 1-based
    column: int  # 1-based code-point column of the identifier (where go-to-definition resolves)
    name: str  # the identifier as Python sees it (NFKC), at most 200 characters


class UnboundNamesSummary(BaseModel):
    # Counted before the response cap; all zero unless the status is "parsed".
    unbound: int  # listed occurrences: no statically known binding
    distinct: int  # distinct identifiers among them
    unsupported: int  # would-be-unbound loads in a module with a star import (never listed)
    implicit: int  # loads of interpreter-provided names, e.g. __file__ (never listed)


class UnboundNamesResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    status: OutlineStatus
    names: list[UnboundName]  # source order (line, column), every occurrence; at most 500
    summary: UnboundNamesSummary
    truncated: bool  # more than 500 occurrences were found
