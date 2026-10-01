from typing import Literal

from pydantic import BaseModel

# The content of one registered repository file. Deliberately excluded: file ids, sha256
# checksums and filesystem paths.


class FileContentResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    language: str | None
    size_bytes: int
    content: str | None  # None when the file is not displayable as text (see reason)
    reason: Literal["binary", "too_large"] | None
