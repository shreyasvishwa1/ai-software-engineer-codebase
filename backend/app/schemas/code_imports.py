from typing import Literal

from pydantic import BaseModel

from app.schemas.code_outline import OutlineStatus

# Imports of one Python file, resolved to repository files by their stored paths only.
# Deliberately excluded: file ids, sha256 checksums, filesystem paths and file contents.

ImportResolution = Literal["resolved", "ambiguous", "not_found"]


class ImportEntry(BaseModel):
    line: int  # 1-based line of the import statement
    statement: Literal["import", "from"]
    module: str | None  # "from" module as written (None for "import" and "from . import")
    level: int  # number of leading dots of a relative import; 0 = absolute
    names: list[str]  # imported names in source order (at most 50, each at most 200 chars)
    targets: list[str]  # matching repository paths, sorted (at most 5)
    # not_found: nothing matched; ambiguous: some imported module matched 2+ files;
    # resolved: every module that matched, matched exactly one file.
    resolution: ImportResolution


class CodeImportsResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    status: OutlineStatus
    imports: list[ImportEntry]  # empty unless status is "parsed"
    truncated: bool  # an import, name, target, text-length or path-index limit was reached
