from typing import Literal

from pydantic import BaseModel

# Dependencies declared in a repository's manifest files, as written (never resolved).
# Deliberately excluded: file ids, sha256 checksums, file contents and filesystem paths.

ManifestStatus = Literal["parsed", "invalid", "too_large", "unavailable"]


class DependencyEntry(BaseModel):
    name: str
    specifier: str  # extras, version constraints and markers exactly as written
    scope: str  # runtime | dev | peer | optional | optional:<group>


class ManifestResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    ecosystem: Literal["npm", "python"]
    status: ManifestStatus
    dependencies: list[DependencyEntry]


class DependencySummary(BaseModel):
    manifests: int
    parsed: int
    invalid: int
    too_large: int
    unavailable: int
    dependencies: int


class DependencyInventoryResponse(BaseModel):
    manifests: list[ManifestResponse]
    summary: DependencySummary
    truncated: bool  # more manifests or dependencies existed than the limits allow
