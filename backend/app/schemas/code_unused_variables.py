from pydantic import BaseModel

from app.schemas.code_outline import OutlineStatus

# Unused local variables of one Python file: assignments to def/async def locals that nothing in
# the file reads, under the Step 45 scope model, the Step 58 occurrence model and the Step 60
# rules. This is not a dead-store (data-flow) analysis. Deliberately excluded: source text,
# snippets, file ids, sha256 checksums, filesystem paths and any AST or scope data.


class UnusedVariable(BaseModel):
    line: int  # 1-based
    column: int  # 1-based code-point column of the assigned name
    name: str  # the identifier as Python sees it (NFKC), at most 200 characters


class UnusedVariablesSummary(BaseModel):
    # Counted before the response cap; all zero unless the status is "parsed".
    unused: int  # listed entries: assignments to variables that are never read
    distinct: int  # distinct listed variables, a variable being (function scope, name)
    unsupported: int  # never-read variables of dynamic functions or named in string annotations


class UnusedVariablesResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    status: OutlineStatus
    variables: list[UnusedVariable]  # source order (line, column), every assignment; at most 500
    summary: UnusedVariablesSummary
    truncated: bool  # more than 500 entries were found
