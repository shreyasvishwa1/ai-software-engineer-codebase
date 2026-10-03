from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.code_outline import OutlineSymbol

# Python definitions whose name equals the query, across one repository snapshot. Results
# reuse Step 37's outline fields. Deliberately excluded: file ids, sha256 checksums,
# filesystem paths, source contents and snippets.

MIN_QUERY_LENGTH = 1
MAX_QUERY_LENGTH = 200  # Step 37's MAX_NAME_CHARS


class SymbolSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=MIN_QUERY_LENGTH, max_length=MAX_QUERY_LENGTH)

    @field_validator("query")
    @classmethod
    def python_identifier(cls, value: str) -> str:
        if not value.isidentifier():
            raise ValueError("query must be a Python identifier")
        return value


class SymbolResult(OutlineSymbol):
    """One definition: Step 37's symbol fields (``async`` included) plus its file."""

    path: str  # POSIX path relative to the repository root


class SymbolSearchSummary(BaseModel):
    files_scanned: int  # Python files read (and hash-verified)
    too_large: int  # over 1 MiB (never opened) or over 256 KiB of text (never parsed)
    binary: int  # not valid UTF-8 text
    syntax_error: int


class SymbolSearchResponse(BaseModel):
    results: list[SymbolResult]  # by path, then source order within a file
    summary: SymbolSearchSummary
    truncated: bool  # an index, read, parse, per-file or result limit was reached
