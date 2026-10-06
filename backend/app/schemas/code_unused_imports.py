from pydantic import BaseModel

from app.schemas.code_outline import OutlineStatus

# Unused imports of one Python file: import bindings whose bound name nothing in the file reads,
# under the Step 45 scope model, the Step 58 occurrence model and the Step 62 rules (an import kept
# only for its side effects is listed too). Deliberately excluded: source text, snippets, file ids,
# sha256 checksums, filesystem paths and any AST or scope data.


class UnusedImport(BaseModel):
    line: int  # 1-based
    column: int  # 1-based code-point column of the bound name (the name after "as" when aliased)
    name: str  # the bound name as Python sees it (NFKC), at most 200 characters


class UnusedImportsSummary(BaseModel):
    # Counted before the response cap; all zero unless the status is "parsed".
    unused: int  # listed entries: imports whose bound name is never read
    distinct: int  # distinct listed bindings, a binding being (scope, name)
    unsupported: int  # never-read imports of interfaces, dynamic code or string annotations


class UnusedImportsResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    status: OutlineStatus
    imports: list[UnusedImport]  # source order (line, column), every import; at most 500
    summary: UnusedImportsSummary
    truncated: bool  # more than 500 entries were found
