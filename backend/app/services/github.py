"""Public GitHub repository archive download.

Only ``https://github.com/<owner>/<repository>`` URLs are accepted, validated without any
network or DNS access. The download URL is built from the validated owner/repository
(never taken from the client), redirects are followed manually and only to GitHub hosts,
and the response is streamed with a hard byte limit. No git, subprocess or authentication.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import IO
from urllib.parse import SplitResult, quote, urljoin, urlsplit

import httpx

GITHUB_HOST = "github.com"
# github.com serves /archive/ URLs by redirecting to codeload.github.com.
ALLOWED_DOWNLOAD_HOSTS = frozenset({"github.com", "codeload.github.com"})

CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0
TOTAL_TIMEOUT_SECONDS = 300.0
MAX_REDIRECTS = 5
USER_AGENT = "AI-Software-Engineer-Ingestion"

# GitHub owner names: 1-39 alphanumerics or single hyphens, not starting/ending with "-".
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
# Conservative subset of valid git branch names.
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")


class GitHubError(Exception):
    """A controlled GitHub validation or download failure, safe to return to the client."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str


def _has_unsafe_characters(value: str) -> bool:
    return any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch == "\\" for ch in value)


def _split(url: str) -> tuple[SplitResult, int | None] | None:
    try:
        parts = urlsplit(url)
        return parts, parts.port
    except ValueError:
        return None


def parse_github_repository_url(url: str | None) -> GitHubRepository:
    """Validate a public GitHub repository URL without any network access."""
    if not url:
        raise GitHubError(422, "GitHub repository URL is required")
    invalid = GitHubError(422, "Invalid GitHub repository URL")
    if _has_unsafe_characters(url) or "?" in url or "#" in url or "%" in url:
        raise invalid
    split = _split(url)
    if split is None:
        raise invalid
    parts, port = split
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or "@" in parts.netloc
        or port is not None
        or parts.hostname != GITHUB_HOST
        or parts.netloc.lower() != GITHUB_HOST
    ):
        raise invalid

    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    segments = path.split("/")
    if len(segments) != 3 or segments[0] != "":
        raise invalid
    owner, name = segments[1], segments[2]
    if (
        not _OWNER.match(owner)
        or not _REPOSITORY.match(name)
        or name in (".", "..")
        or name.lower().endswith(".git")
    ):
        raise invalid
    return GitHubRepository(owner=owner, name=name)


def validate_branch(branch: str) -> str:
    invalid = GitHubError(422, "Unsupported default branch name")
    if (
        not _BRANCH.match(branch)
        or branch.startswith(("/", "-"))
        or branch.endswith(("/", ".", ".lock"))
        or "//" in branch
        or ".." in branch
        or any(segment.startswith(".") for segment in branch.split("/"))
    ):
        raise invalid
    return branch


def _redirect_error() -> GitHubError:
    return GitHubError(502, "GitHub redirected to an unsupported location")


def validate_download_url(url: str) -> str:
    """Allow only https URLs on GitHub download hosts, default port, no credentials."""
    if _has_unsafe_characters(url):
        raise _redirect_error()
    split = _split(url)
    if split is None:
        raise _redirect_error()
    parts, port = split
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or "@" in parts.netloc
        or port is not None
        or parts.hostname not in ALLOWED_DOWNLOAD_HOSTS
        or parts.netloc.lower() != parts.hostname
        or parts.fragment
    ):
        raise _redirect_error()
    return url


def archive_download_url(repository: GitHubRepository, default_branch: str | None) -> str:
    """Build the archive URL on github.com from validated parts only."""
    ref = "HEAD" if default_branch is None else "refs/heads/" + validate_branch(default_branch)
    url = (
        f"https://{GITHUB_HOST}/{quote(repository.owner, safe='')}/"
        f"{quote(repository.name, safe='')}/archive/{quote(ref, safe='/')}.zip"
    )
    return validate_download_url(url)


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 200:
        return
    if response.status_code == 404:
        raise GitHubError(404, "GitHub repository not found")
    if response.status_code == 429:
        raise GitHubError(503, "GitHub rate limit exceeded; try again later")
    if response.status_code in (401, 403, 451):
        raise GitHubError(502, "GitHub denied access to the repository")
    raise GitHubError(502, "GitHub returned an unexpected response")


async def _stream_to(response: httpx.Response, dest: IO[bytes], max_bytes: int) -> None:
    too_large = GitHubError(413, "Downloaded archive exceeds the size limit")
    declared = response.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise GitHubError(502, "GitHub returned an unexpected response")
        if int(declared) > max_bytes:
            raise too_large  # rejected before reading any of the body
    written = 0
    # Raw bytes: no transparent decompression, so the limit applies to what is stored.
    async for chunk in response.aiter_raw():
        written += len(chunk)
        if written > max_bytes:
            raise too_large
        dest.write(chunk)


async def download_archive(url: str, dest: IO[bytes], max_bytes: int) -> None:
    """Stream the archive at ``url`` (already validated) into ``dest``, following only
    redirects to allowed GitHub hosts. Error response bodies are never read."""
    timeout = httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            # trust_env=False: ignore proxy environment variables and .netrc credentials.
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=timeout, headers=headers
            ) as client:
                current = validate_download_url(url)
                visited = {current}
                for _ in range(MAX_REDIRECTS + 1):
                    async with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise GitHubError(502, "GitHub returned an unexpected response")
                            target = validate_download_url(urljoin(current, location))
                            if target in visited:
                                raise GitHubError(502, "GitHub redirect loop detected")
                            visited.add(target)
                            current = target
                            continue
                        _raise_for_status(response)
                        await _stream_to(response, dest, max_bytes)
                        return
                raise GitHubError(502, "GitHub redirected too many times")
    except (TimeoutError, httpx.TimeoutException):
        raise GitHubError(504, "GitHub download timed out") from None
    except httpx.HTTPError:
        raise GitHubError(502, "Could not connect to GitHub") from None
