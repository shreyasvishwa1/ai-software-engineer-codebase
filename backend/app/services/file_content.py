"""Reading the content of one registered file of an (already authorized) repository.

The file is identified by an exact match on its ``files`` row; the client's path is only a
lookup value and is never used to build a filesystem path. The disk path comes from the
repository storage directory and the stored ``File.path`` (validated at extraction).

Reads are defensive: the storage root and every path component are checked with lstat
(no symlinks or junctions), the resolved path must stay inside the storage root, the
target must be a regular file, and at most MAX_CONTENT_BYTES + 1 bytes are read. The bytes
must match ``File.sha256``; otherwise (missing file, a storage swap in progress, a failed
swap, tampering) the content is reported as unavailable rather than returning bytes of
another snapshot. The hash check also means that even a path replaced between the checks
and the open can only ever yield the recorded content. Nothing is logged.
"""

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.file_content import FileContentResponse
from app.services.ingestion import repository_storage_dir

MAX_CONTENT_BYTES = 1 * 1024 * 1024

FILE_NOT_FOUND = "File not found"
CONTENT_UNAVAILABLE = "File content is not available; try again later"


class FileContentError(Exception):
    """A controlled failure, safe to return to the client."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _Unavailable(Exception):
    pass


def _is_link(path: Path, st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or os.path.isjunction(path)


def _read_verified(root: Path, relative: str, expected_sha256: str | None) -> bytes:
    """Read ``relative`` (a stored File.path) below ``root`` and verify its hash.

    Raises _Unavailable for anything unexpected; never follows links."""
    try:
        parts = PurePosixPath(relative).parts
        if not parts or PurePosixPath(relative).is_absolute() or ".." in parts:
            raise _Unavailable
        st = os.lstat(root)
        if _is_link(root, st) or not stat.S_ISDIR(st.st_mode):
            raise _Unavailable
        current = root
        for index, part in enumerate(parts):
            current = current / part
            st = os.lstat(current)
            if _is_link(current, st):
                raise _Unavailable
            if index < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
                raise _Unavailable
        if not stat.S_ISREG(st.st_mode):
            raise _Unavailable
        resolved_root = root.resolve(strict=True)
        resolved = current.resolve(strict=True)
        if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
            raise _Unavailable
        with open(current, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _Unavailable
            data = handle.read(MAX_CONTENT_BYTES + 1)
    except OSError:
        raise _Unavailable from None
    if len(data) > MAX_CONTENT_BYTES:
        raise _Unavailable
    if expected_sha256 is None or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise _Unavailable
    return data


def _decode(data: bytes) -> str | None:
    """Strict UTF-8 (a BOM is removed); None for anything that is not text."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


async def read_file_content(
    db: AsyncSession, repository: Repository, path: str
) -> FileContentResponse:
    file = await db.scalar(
        select(File).where(File.repository_id == repository.id, File.path == path)
    )
    if file is None:
        raise FileContentError(404, FILE_NOT_FOUND)

    def response(content: str | None, reason: str | None) -> FileContentResponse:
        return FileContentResponse.model_validate(
            {
                "path": file.path,
                "language": file.language,
                "size_bytes": file.size_bytes,
                "content": content,
                "reason": reason,
            }
        )

    if file.size_bytes > MAX_CONTENT_BYTES:
        return response(None, "too_large")  # the file is never opened
    root = repository_storage_dir(get_settings(), repository.id)
    try:
        data = await run_in_threadpool(_read_verified, root, file.path, file.sha256)
    except _Unavailable:
        raise FileContentError(409, CONTENT_UNAVAILABLE) from None
    text = _decode(data)
    return response(text, None if text is not None else "binary")
