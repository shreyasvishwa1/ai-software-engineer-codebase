"""Read-only views over an (already authorized) repository's registered files.

Both views read only ``files`` rows (never DATA_DIR) and reflect the last successfully
registered snapshot, which ingestion replaces atomically.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import File, Repository
from app.schemas.repository_files import (
    LanguageStatistic,
    RepositoryLanguagesResponse,
    RepositoryTreeResponse,
    TreeDirectory,
    TreeFile,
)


@dataclass
class _Directory:
    name: str
    path: str
    directories: dict[str, "_Directory"] = field(default_factory=dict)
    files: list[TreeFile] = field(default_factory=list)


def _sort_key(name: str) -> tuple[str, str]:
    return (name.casefold(), name)  # case-insensitive, with a deterministic tie-break


def _to_schema(directory: _Directory) -> TreeDirectory:
    children: list[TreeDirectory | TreeFile] = [
        _to_schema(directory.directories[name])
        for name in sorted(directory.directories, key=_sort_key)
    ]
    children.extend(sorted(directory.files, key=lambda f: _sort_key(f.name)))
    return TreeDirectory(name=directory.name, path=directory.path, children=children)


def build_tree(files: Iterable[TreeFile]) -> TreeDirectory:
    """Nest files by their relative POSIX paths: directories first, then files, each sorted
    case-insensitively by name."""
    root = _Directory(name="", path="")
    for file in files:
        parent = root
        for part in file.path.split("/")[:-1]:
            child_path = f"{parent.path}/{part}" if parent.path else part
            parent = parent.directories.setdefault(part, _Directory(name=part, path=child_path))
        parent.files.append(file)
    return _to_schema(root)


async def repository_tree(db: AsyncSession, repository: Repository) -> RepositoryTreeResponse:
    rows = await db.execute(
        select(File.path, File.name, File.extension, File.language, File.size_bytes).where(
            File.repository_id == repository.id
        )
    )
    files = [
        TreeFile(
            name=name, path=path, extension=extension, language=language, size_bytes=size_bytes
        )
        for path, name, extension, language, size_bytes in rows
    ]
    return RepositoryTreeResponse(
        repository_id=repository.id,
        status=repository.status,
        file_count=len(files),
        total_bytes=sum(f.size_bytes for f in files),
        tree=build_tree(files),
    )


async def language_statistics(
    db: AsyncSession, repository: Repository
) -> RepositoryLanguagesResponse:
    """Per-language file counts and bytes, aggregated in SQL."""
    rows = await db.execute(
        select(
            File.language,
            func.count(File.id),
            func.coalesce(func.sum(File.size_bytes), 0),
        )
        .where(File.repository_id == repository.id)
        .group_by(File.language)
    )
    groups = [(language, int(count), int(total)) for language, count, total in rows]
    total_files = sum(count for _, count, _ in groups)
    total_bytes = sum(total for _, _, total in groups)
    # Largest first; ties by name, with files of unknown language last.
    groups.sort(key=lambda g: (-g[2], g[0] is None, g[0] or ""))
    languages = [
        LanguageStatistic(
            language=language,
            file_count=count,
            total_bytes=total,
            percentage=round(total * 100 / total_bytes, 2) if total_bytes else 0.0,
        )
        for language, count, total in groups
    ]
    return RepositoryLanguagesResponse(
        repository_id=repository.id,
        status=repository.status,
        total_files=total_files,
        total_bytes=total_bytes,
        languages=languages,
    )
