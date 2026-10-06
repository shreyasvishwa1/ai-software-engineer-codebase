from typing import Literal

from pydantic import BaseModel

from app.schemas.code_outline import OutlineStatus

# Unreachable code of one Python file: the statements after a statement that always leaves its
# statement list (return, raise, break, continue, an if whose every branch leaves, an endless while
# loop without a break), under the Step 69 rules. Deliberately excluded: source text, snippets,
# file ids, sha256 checksums, filesystem paths and any AST data.


class UnreachableRegion(BaseModel):
    line: int  # 1-based line where the region's first statement starts (its first decorator)
    end_line: int  # 1-based last line of the region's last statement
    statements: int  # statements in the region, counted in its own list (nested ones excluded)
    cause: Literal["return", "raise", "break", "continue", "if", "while"]  # the leaving statement
    cause_line: int  # 1-based line where the leaving statement starts


class UnreachableCodeSummary(BaseModel):
    # Counted before the response cap; all zero unless the status is "parsed".
    regions: int
    statements: int


class UnreachableCodeResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    status: OutlineStatus
    regions: list[UnreachableRegion]  # source order; at most 500
    summary: UnreachableCodeSummary
    truncated: bool  # more than 500 regions were found
