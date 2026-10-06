from typing import Literal

from pydantic import BaseModel

from app.schemas.code_outline import OutlineStatus

# Replaced definitions of one Python file: def, async def and class statements whose binding a later
# def, async def or class statement of the same name in the same statement list replaces before
# anything can have read it, under the Step 45 scope model, the Step 58 occurrence model and the
# Step 66 rules. Deliberately excluded: source text, snippets, file ids, sha256 checksums,
# filesystem paths and any AST or scope data.


class ReplacedDefinition(BaseModel):
    line: int  # 1-based line of the replaced definition's name
    column: int  # 1-based code-point column of that name
    name: str  # the name as Python sees it (NFKC), at most 200 characters
    kind: Literal["class", "function", "method"]  # as Step 37 names definitions
    replaced_line: int  # 1-based line of the replacing definition's name
    replaced_column: int  # 1-based code-point column of that name


class ReplacedDefinitionsSummary(BaseModel):
    # Counted before the response cap; all zero unless the status is "parsed".
    replaced: int  # listed entries: definitions replaced before anything read them
    distinct: int  # distinct listed bindings, a binding being (scope, name)
    unsupported: int  # never-read replaced definitions that decorators, class creation or
    # dynamic access may keep in use


class ReplacedDefinitionsResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    status: OutlineStatus
    definitions: list[ReplacedDefinition]  # source order (line, column); at most 500
    summary: ReplacedDefinitionsSummary
    truncated: bool  # more than 500 entries were found
