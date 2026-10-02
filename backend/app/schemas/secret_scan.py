from typing import Literal

from pydantic import BaseModel

# Likely committed credentials, reported by location only. Deliberately excluded: the
# matched text, snippets, masked previews, columns, file ids, sha256 checksums and
# filesystem paths.

SecretRule = Literal[
    "aws-access-key-id",
    "github-token",
    "slack-token",
    "stripe-live-key",
    "google-api-key",
    "anthropic-api-key",
    "private-key",
]


class SecretFinding(BaseModel):
    path: str  # POSIX path relative to the repository root
    line: int  # 1-based
    rule: SecretRule
    description: str


class SecretScanSummary(BaseModel):
    files_scanned: int
    binary: int  # includes content that is not valid UTF-8
    too_large: int
    unavailable: int
    findings: int


class SecretScanResponse(BaseModel):
    findings: list[SecretFinding]
    summary: SecretScanSummary
    truncated: bool  # the file, byte or findings limit stopped the scan
