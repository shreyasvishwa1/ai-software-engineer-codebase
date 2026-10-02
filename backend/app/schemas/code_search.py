from pydantic import BaseModel, ConfigDict, Field, field_validator

# Literal text search over a repository's current snapshot. Deliberately excluded: file ids,
# sha256 checksums, filesystem paths and anything beyond the matched line's preview.

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 200


class CodeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=MIN_QUERY_LENGTH, max_length=MAX_QUERY_LENGTH)

    @field_validator("query")
    @classmethod
    def single_line_text(cls, value: str) -> str:
        if any(char in value for char in ("\n", "\r", "\x00")):
            raise ValueError("query must be a single line of text")
        if not value.strip():
            raise ValueError("query must not be only whitespace")
        return value


class CodeSearchMatch(BaseModel):
    path: str  # POSIX path relative to the repository root
    line: int  # 1-based
    preview: str  # at most 200 characters of the matching line; untrusted text


class CodeSearchSummary(BaseModel):
    files_scanned: int
    binary: int  # includes content that is not valid UTF-8
    too_large: int
    unavailable: int
    matches: int


class CodeSearchResponse(BaseModel):
    matches: list[CodeSearchMatch]
    summary: CodeSearchSummary
    truncated: bool  # the file, byte or match limit stopped the search
