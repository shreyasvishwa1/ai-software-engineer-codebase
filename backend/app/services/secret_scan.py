"""Request-time scan of a repository's current snapshot for likely committed credentials.

Files are the repository's files rows, scanned in path order. Each is read only through
the Step 21 verified reader (links refused, containment checked, size bounded, SHA-256
verified) and decoded with its strict UTF-8 decoder; content is only matched against the
fixed RULES below, line by line, and never executed, parsed, sent, stored or logged.

Findings carry the path, the 1-based line number and the rule, never the matched text or
its position. Lines are split on "\\n" (a trailing "\\r" is removed) and only their first
MAX_LINE_CHARS characters are scanned; each rule is reported at most once per line. All
patterns are linear (bounded character classes, no nested quantifiers).

At most MAX_FILES files and MAX_TOTAL_BYTES of stored ``size_bytes`` (checked before a
file is opened) are scanned and at most MAX_FINDINGS findings are returned; ``truncated``
reports that a limit stopped the scan. A file that cannot be scanned never fails the
request: it is counted as binary (including invalid UTF-8), too_large or unavailable.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import File, Repository
from app.schemas.secret_scan import SecretFinding, SecretScanResponse, SecretScanSummary

# Step 21's verified reader and decoder, reused unchanged (never duplicated).
from app.services.file_content import MAX_CONTENT_BYTES, _decode, _read_verified, _Unavailable
from app.services.ingestion import repository_storage_dir
from app.services.rate_limit import Limit

MAX_FILES = 5000
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FINDINGS = 500
MAX_LINE_CHARS = 4096
# Per user; enforced through the existing Step 17 limiter (fail open without Redis).
SECRET_SCAN_LIMIT = Limit("secret-scan-user", 30, 3600)


@dataclass(frozen=True)
class _Rule:
    id: str
    description: str
    pattern: re.Pattern[str]


def _rule(rule_id: str, description: str, pattern: str) -> _Rule:
    return _Rule(rule_id, description, re.compile(pattern, re.ASCII))


RULES: tuple[_Rule, ...] = (
    _rule(
        "aws-access-key-id",
        "AWS access key ID",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Za-z])",
    ),
    _rule(
        "github-token",
        "GitHub token",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{82,255})",
    ),
    _rule("slack-token", "Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,255}"),
    _rule("stripe-live-key", "Stripe live secret key", r"\b[rs]k_live_[0-9A-Za-z]{24,255}"),
    _rule("google-api-key", "Google API key", r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])"),
    _rule("anthropic-api-key", "Anthropic API key", r"\bsk-ant-[A-Za-z0-9_-]{20,255}"),
    _rule(
        "private-key",
        "Private key",
        r"-----BEGIN (?:[A-Z0-9]+ ){0,3}PRIVATE KEY(?: BLOCK)?-----",
    ),
)
_DESCRIPTIONS = {rule.id: rule.description for rule in RULES}

Hit = tuple[int, str]  # (1-based line number, rule id)


def scan_text(text: str) -> list[Hit]:
    """Every (line, rule) match in ``text``, sorted; never the matched text itself."""
    hits: list[Hit] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.removesuffix("\r")[:MAX_LINE_CHARS]
        hits.extend((number, rule.id) for rule in RULES if rule.pattern.search(line))
    return sorted(hits)


def scan_file(root: Path, path: str, size_bytes: int, sha256: str | None) -> tuple[str, list[Hit]]:
    """(status, hits) for one stored file; status is scanned, binary, too_large or
    unavailable."""
    if size_bytes > MAX_CONTENT_BYTES:
        return "too_large", []  # never opened
    try:
        data = _read_verified(root, path, sha256)
    except _Unavailable:
        return "unavailable", []
    text = _decode(data)
    if text is None:
        return "binary", []
    return "scanned", scan_text(text)


async def secret_scan(db: AsyncSession, repository: Repository) -> SecretScanResponse:
    rows = (
        await db.execute(
            select(File.path, File.size_bytes, File.sha256).where(
                File.repository_id == repository.id
            )
        )
    ).all()
    rows = sorted(rows, key=lambda row: row.path)
    truncated = len(rows) > MAX_FILES
    root = repository_storage_dir(get_settings(), repository.id)

    counts = {"scanned": 0, "binary": 0, "too_large": 0, "unavailable": 0}
    findings: list[SecretFinding] = []
    budget = MAX_TOTAL_BYTES
    for row in rows[:MAX_FILES]:
        if row.size_bytes <= MAX_CONTENT_BYTES:  # larger files are never read
            if row.size_bytes > budget:
                truncated = True  # checked before the file is opened
                break
            budget -= row.size_bytes
        status, hits = await run_in_threadpool(
            scan_file, root, row.path, row.size_bytes, row.sha256
        )
        counts[status] += 1
        overflow = len(findings) + len(hits) > MAX_FINDINGS
        if overflow:
            hits = hits[: MAX_FINDINGS - len(findings)]
        for line, rule_id in hits:
            findings.append(
                SecretFinding.model_validate(
                    {
                        "path": row.path,
                        "line": line,
                        "rule": rule_id,
                        "description": _DESCRIPTIONS[rule_id],
                    }
                )
            )
        if overflow:
            truncated = True  # more findings existed: stop scanning
            break

    findings.sort(key=lambda finding: (finding.path, finding.line, finding.rule))
    summary = SecretScanSummary(
        files_scanned=counts["scanned"],
        binary=counts["binary"],
        too_large=counts["too_large"],
        unavailable=counts["unavailable"],
        findings=len(findings),
    )
    return SecretScanResponse(findings=findings, summary=summary, truncated=truncated)
