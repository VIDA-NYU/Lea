"""Credential-safe GitHub source acquisition shared by import features."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .gitstore import _inject_token, _scrub


CLONE_TIMEOUT_SECONDS = 60
_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


class GitHubSourceError(RuntimeError):
    def __init__(self, message: str, code: str = "github_source_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str
    source_url: str
    clone_url: str


@dataclass(frozen=True)
class TrackedFile:
    path: str
    mode: str
    object_id: str


def parse_github_repository_url(url: str) -> GitHubRepository:
    """Accept only an HTTPS GitHub root-repository URL."""

    raw = str(url or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "github.com", "www.github.com",
    }:
        raise GitHubSourceError(
            "Enter an https://github.com/owner/repository URL.", "invalid_repository_url"
        )
    if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise GitHubSourceError(
            "The GitHub URL must point to a repository root.", "invalid_repository_url"
        )
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise GitHubSourceError(
            "The GitHub URL must point to a repository root.", "invalid_repository_url"
        )
    owner, name = parts
    if name.endswith(".git"):
        name = name[:-4]
    if not owner or not name or not _REPO_SEGMENT_RE.fullmatch(owner) or not _REPO_SEGMENT_RE.fullmatch(name):
        raise GitHubSourceError(
            "The GitHub owner or repository name is invalid.", "invalid_repository_url"
        )
    canonical = f"https://github.com/{owner}/{name}"
    return GitHubRepository(
        owner=owner,
        name=name,
        source_url=canonical,
        clone_url=f"{canonical}.git",
    )


def clone_repository(
    clone_url: str,
    destination: Path,
    *,
    ref: str | None = None,
    token: str | None = None,
    timeout: int = CLONE_TIMEOUT_SECONDS,
) -> str:
    """Shallow-clone a source and return its checked-out HEAD SHA."""

    credential_url = _inject_token(clone_url, token) if token else clone_url
    attempts: list[list[str]] = []
    if ref:
        attempts.append([
            "clone", "--depth", "1", "--no-tags", "--branch", ref,
            credential_url, str(destination),
        ])
    attempts.append([
        "clone", "--depth", "1", "--no-tags", credential_url, str(destination),
    ])
    env = {
        **os.environ,
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    last_error = "clone failed"
    for args in attempts:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        try:
            process = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise GitHubSourceError(
                "Timed out cloning the repository.", "repository_unavailable"
            ) from None
        if process.returncode == 0:
            # `git clone` persists its source URL as `remote.origin.url`. Replace
            # the credential-bearing one immediately so a private-source token is
            # never retained in preview or durable staging.
            if token:
                sanitized = subprocess.run(
                    ["git", "-C", str(destination), "remote", "set-url", "origin", clone_url],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if sanitized.returncode != 0:
                    detail = _scrub(sanitized.stderr.strip(), token)
                    raise GitHubSourceError(
                        f"Could not sanitize the cloned repository: {detail or 'git failed'}",
                        "repository_unavailable",
                    )
            return head_sha(destination)
        last_error = _scrub(process.stderr.strip() or "clone failed", token)
    raise GitHubSourceError(
        f"Could not clone the repository: {last_error}", "repository_unavailable"
    )


def head_sha(repository: Path) -> str:
    process = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if process.returncode != 0 or not process.stdout.strip():
        raise GitHubSourceError("Could not read the cloned repository revision.")
    return process.stdout.strip()


def tracked_lean_files(repository: Path) -> list[TrackedFile]:
    """List tracked Lean files with their Git modes, without following symlinks."""

    process = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "-s", "-z", "--", "*.lean"],
        capture_output=True,
        timeout=15,
    )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise GitHubSourceError(f"Could not inventory the repository: {detail or 'git failed'}")
    result: list[TrackedFile] = []
    for record in process.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, path_bytes = record.partition(b"\t")
        parts = metadata.decode("ascii", errors="replace").split()
        if not separator or len(parts) < 3:
            continue
        result.append(
            TrackedFile(
                path=path_bytes.decode("utf-8", errors="surrogateescape"),
                mode=parts[0],
                object_id=parts[1],
            )
        )
    return result
