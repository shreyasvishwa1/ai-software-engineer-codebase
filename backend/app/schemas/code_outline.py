from typing import Literal

from pydantic import BaseModel, Field

# Structural outline of one Python file. Deliberately excluded: file ids, sha256 checksums,
# filesystem paths and any file content other than definition names.

OutlineStatus = Literal["parsed", "syntax_error", "too_large", "binary", "unsupported_language"]


class OutlineSymbol(BaseModel):
    kind: Literal["class", "function", "method"]
    is_async: bool = Field(serialization_alias="async")  # "async" is a Python keyword
    name: str  # a Python identifier, at most 200 characters; untrusted text
    line: int  # 1-based line of the class/def statement (not its decorators)
    end_line: int  # 1-based last line of the definition
    depth: int  # 0 = top level


class CodeOutlineResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    status: OutlineStatus
    symbols: list[OutlineSymbol]  # empty unless status is "parsed"
    truncated: bool  # the symbol or depth limit left definitions out
