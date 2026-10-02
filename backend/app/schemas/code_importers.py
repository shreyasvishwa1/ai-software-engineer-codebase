from typing import Literal

from pydantic import BaseModel

from app.schemas.code_imports import ImportResolution

# Python files whose imports resolve to one target file (reverse of Step 39). Deliberately
# excluded: file ids, sha256 checksums, filesystem paths and file contents.


class ImporterImport(BaseModel):
    """One import statement of an importer that resolves to the target (Step 39 fields,
    without the forward targets)."""

    line: int  # 1-based line of the import statement
    statement: Literal["import", "from"]
    module: str | None  # "from" module as written (None for "import" and "from . import")
    level: int  # number of leading dots of a relative import; 0 = absolute
    names: list[str]  # imported names in source order (at most 50, each at most 200 chars)
    resolution: ImportResolution  # the statement's forward (Step 39) resolution


class Importer(BaseModel):
    path: str  # POSIX path relative to the repository root
    imports: list[ImporterImport]  # in source order, at most 20


class ImportersSummary(BaseModel):
    files_scanned: int  # candidate importers read (and hash-verified)
    too_large: int  # over 1 MiB (never opened) or over 256 KiB of text (never parsed)
    binary: int  # not valid UTF-8 text
    syntax_error: int


class CodeImportersResponse(BaseModel):
    path: str  # the target's POSIX path relative to the repository root
    status: Literal["scanned", "unsupported_language"]
    importers: list[Importer]  # sorted by path; empty unless status is "scanned"
    summary: ImportersSummary
    truncated: bool  # a scan, parse, result or Step 39 limit was reached
