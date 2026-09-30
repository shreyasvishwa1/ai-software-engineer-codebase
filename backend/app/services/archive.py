"""Safe ZIP inspection and extraction.

Archive contents are only copied as bytes: nothing is executed, imported or interpreted.
Every entry is validated before any byte is written, and extraction re-checks the
actual sizes and the resolved destination of every file.
"""

import hashlib
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

MIB = 1024 * 1024
_CHUNK_SIZE = 64 * 1024

# File.name and File.extension column sizes; 255 is also the common filesystem limit.
MAX_PATH_COMPONENT_LENGTH = 255
MAX_EXTENSION_LENGTH = 32

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
# Characters that are invalid in Windows file names (":" also blocks NTFS streams).
_FORBIDDEN_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class ArchiveError(Exception):
    """A controlled rejection of an archive, safe to return to the client."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int
    max_total_bytes: int
    max_file_bytes: int
    max_compression_ratio: int


@dataclass(frozen=True)
class ExtractedFile:
    path: str  # POSIX path relative to the repository root
    name: str
    extension: str | None
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class _Member:
    info: zipfile.ZipInfo
    path: str


def _unsafe_path() -> ArchiveError:
    return ArchiveError(400, "Archive contains an unsafe path")


def normalize_member_path(raw: str) -> str:
    """Return a safe relative POSIX path for an archive entry, or raise ArchiveError.

    Backslashes are treated as separators so Windows-style names are checked the same
    way on every platform. Absolute, drive, UNC and any ``..`` paths are rejected.
    """
    if not raw or "\x00" in raw:
        raise _unsafe_path()
    name = raw.replace("\\", "/")
    if name.startswith("/") or _WINDOWS_DRIVE.match(name):  # "/x", "//server/share", "C:"
        raise _unsafe_path()
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts:
        raise _unsafe_path()
    for part in parts:
        if (
            part == ".."
            or len(part) > MAX_PATH_COMPONENT_LENGTH
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in part)
            or any(ch in _FORBIDDEN_CHARACTERS for ch in part)
            or part.endswith((".", " "))
            or part.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise _unsafe_path()
    return "/".join(parts)


def _inspect(zf: zipfile.ZipFile, limits: ArchiveLimits) -> list[_Member]:
    """Validate every entry from metadata alone and return the regular files to extract."""
    infos = zf.infolist()
    if len(infos) > limits.max_entries:
        raise ArchiveError(413, "Archive contains too many entries")

    members: list[_Member] = []
    file_keys: set[str] = set()
    dir_keys: set[str] = set()
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise ArchiveError(400, "Encrypted archives are not supported")
        entry_type = stat.S_IFMT(info.external_attr >> 16)
        if entry_type == stat.S_IFLNK:
            raise ArchiveError(400, "Archive contains a symbolic link")
        if entry_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ArchiveError(400, "Archive contains an unsupported entry type")

        path = normalize_member_path(info.filename)
        key = path.casefold()  # case-insensitive filesystems would merge these
        if info.filename.replace("\\", "/").endswith("/") or entry_type == stat.S_IFDIR:
            if key in file_keys:
                raise ArchiveError(400, "Archive contains duplicate or conflicting paths")
            dir_keys.add(key)
            continue
        if key in file_keys or key in dir_keys:
            raise ArchiveError(400, "Archive contains duplicate or conflicting paths")
        file_keys.add(key)

        if info.file_size > limits.max_file_bytes:
            raise ArchiveError(413, "Archive contains a file that exceeds the size limit")
        total += info.file_size
        if total > limits.max_total_bytes:
            raise ArchiveError(413, "Archive exceeds the extracted size limit")
        if info.file_size > 0 and (
            info.compress_size == 0
            or info.file_size > info.compress_size * limits.max_compression_ratio
        ):
            raise ArchiveError(413, "Archive entry exceeds the compression ratio limit")
        members.append(_Member(info=info, path=path))

    # A file may not also be used as a directory by another entry ("a" and "a/b").
    parents = {
        "/".join(parts[:i])
        for key in file_keys | dir_keys
        for parts in [key.split("/")]
        for i in range(1, len(parts))
    }
    if file_keys & parents:
        raise ArchiveError(400, "Archive contains duplicate or conflicting paths")
    return members


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _extract(
    zf: zipfile.ZipFile, members: list[_Member], dest: Path, limits: ArchiveLimits
) -> list[ExtractedFile]:
    root = dest.resolve(strict=True)
    extracted: list[ExtractedFile] = []
    total = 0
    for member in members:
        parts = member.path.split("/")
        target = root.joinpath(*parts)
        if not _is_within(target.parent.resolve(), root):
            raise _unsafe_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _is_within(target.parent.resolve(strict=True), root):
            raise _unsafe_path()

        digest = hashlib.sha256()
        written = 0
        try:
            # "xb" never overwrites: an existing target means a conflicting entry.
            with zf.open(member.info) as source, open(target, "xb") as out:
                while chunk := source.read(_CHUNK_SIZE):
                    written += len(chunk)
                    total += len(chunk)
                    # Actual bytes are checked too, so lying size headers cannot bypass limits.
                    if written > member.info.file_size or written > limits.max_file_bytes:
                        raise ArchiveError(
                            413, "Archive contains a file that exceeds the size limit"
                        )
                    if total > limits.max_total_bytes:
                        raise ArchiveError(413, "Archive exceeds the extracted size limit")
                    digest.update(chunk)
                    out.write(chunk)
        except FileExistsError:
            raise ArchiveError(400, "Archive contains duplicate or conflicting paths") from None

        name = parts[-1]
        extension = PurePosixPath(name).suffix[1:].lower() or None
        if extension is not None and len(extension) > MAX_EXTENSION_LENGTH:
            extension = None
        extracted.append(
            ExtractedFile(
                path=member.path,
                name=name,
                extension=extension,
                size_bytes=written,
                sha256=digest.hexdigest(),
            )
        )
    return extracted


def extract_zip(archive: IO[bytes], dest: Path, limits: ArchiveLimits) -> list[ExtractedFile]:
    """Validate the whole archive, then extract its regular files into ``dest``.

    ``dest`` must be an existing, empty directory. Directory entries are validated but not
    registered. Raises ArchiveError for any unsafe, malformed or over-limit archive.
    """
    try:
        archive.seek(0)
        with zipfile.ZipFile(archive) as zf:
            members = _inspect(zf, limits)
            return _extract(zf, members, dest, limits)
    except (zipfile.BadZipFile, EOFError, NotImplementedError, zlib.error):
        raise ArchiveError(400, "Invalid ZIP archive") from None
