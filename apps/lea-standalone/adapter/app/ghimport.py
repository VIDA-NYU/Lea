"""GitHub import for skills — the "paste a link → Add" path (v2.1.1 W4, D56).

The primary way to add a skill is a GitHub URL. We fetch by **shallow clone**
(no API tokens for public repos, no rate-limited contents API), locate the skill
markdown, and **snapshot** it into the skill's `body` (D45 — the row is the
source of truth, not a live link). `source_url` + `source_ref` are recorded so a
later re-sync is possible.

Supported URL shapes (`normalize_github_url`):
  - repo root:  https://github.com/owner/repo(.git)
  - a subtree:  https://github.com/owner/repo/tree/<ref>/<subdir>
  - a file:     https://github.com/owner/repo/blob/<ref>/<path>.md
  - raw file:   https://raw.githubusercontent.com/owner/repo/<ref>/<path>.md
  - a gist:     https://gist.github.com/<user>/<id>  (or /<id>)

Locating the md when the URL points at a repo/subtree (not a specific file):
`SKILL.md` → `README.md` → the first `*.md` found (D56). Guards: shallow clone
(`--depth 1`), a clone timeout, and a body-size cap; the temp clone dir is always
removed. The token (when configured) is injected into the clone URL for private
repos and scrubbed from any error — public repos need none.
"""

from __future__ import annotations

import logging
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from . import tool_names as _tool_names
from .github_source import GitHubSourceError, clone_repository, head_sha

logger = logging.getLogger("lea-interface.ghimport")

# Guards (D56): a shallow clone shouldn't hang or drag in a giant file.
CLONE_TIMEOUT_SECONDS = 60
MAX_BODY_BYTES = 256 * 1024  # a skill is prose; 256 KB is already generous
# A whole skill DIRECTORY (v2.5 H1/H2): the entry point plus its references. Real ones
# are large — `lean4-skills` ships a 29 KB SKILL.md beside 41 reference files totalling
# ~690 KB — so the cap is generous but finite, and per-file so one runaway asset can't
# consume the whole budget.
MAX_SKILL_BYTES = 4 * 1024 * 1024
MAX_SKILL_FILES = 200

_PRIORITY_MD = ("SKILL.md", "README.md")
# Directories the AgentSkills standard defines beside SKILL.md. Everything else in the
# skill folder is ignored: an import should carry the skill, not the repository.
_RESOURCE_DIRS = ("references", "scripts", "assets")


class GitHubImportError(RuntimeError):
    """The URL was unusable, the clone failed, or no skill markdown was found."""


@dataclass
class ImportTarget:
    """A normalized GitHub reference: what to clone, at which ref, and where to
    look inside it. `explicit_file` (relative to the repo root) is set when the URL
    points at one file; otherwise `subdir` is the directory to search."""

    clone_url: str
    repo_name: str
    ref: str | None = None
    subdir: str = ""
    explicit_file: str | None = None
    source_url: str = ""


@dataclass
class ImportedSkill:
    name: str
    body: str
    source_url: str
    source_ref: str | None
    # H4: from the SKILL.md frontmatter when present — `description` is exactly the
    # "when to use this" the authoring form asks for, already written by the author.
    description: str | None = None
    # H2: (relative path, text) for every reference/script/asset beside the entry point.
    # Empty for a single-file skill, which keeps that path byte-identical to before.
    files: list[tuple[str, str]] = None  # type: ignore[assignment]
    # H5: sub-agent roles bundled beside the skill (`agents/*.md`), already translated to
    # Lea's tool names. H8: MCP servers the skill declares in its `.mcp.json`.
    roles: list[dict] = None  # type: ignore[assignment]
    mcp_servers: dict = None  # type: ignore[assignment]
    # H8: the AgentSkills standard fields worth keeping.
    version: str | None = None
    license: str | None = None
    allowed_tools: list[str] | None = None

    def __post_init__(self):
        if self.files is None:
            self.files = []
        if self.roles is None:
            self.roles = []
        if self.mcp_servers is None:
            self.mcp_servers = {}


def normalize_github_url(url: str) -> ImportTarget:
    """Parse a GitHub-flavored URL into an `ImportTarget`. Raises on anything that
    isn't a github.com / raw.githubusercontent.com / gist.github.com URL."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ("https", "http"):
        raise GitHubImportError("Enter an https GitHub URL (repo, file, or gist).")
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]

    if host in ("github.com", "www.github.com"):
        return _target_from_github(parts, raw)
    if host == "raw.githubusercontent.com":
        return _target_from_raw(parts, raw)
    if host == "gist.github.com":
        return _target_from_gist(parts, raw)
    raise GitHubImportError(
        "Not a GitHub URL. Use github.com, raw.githubusercontent.com, or gist.github.com."
    )


def _target_from_github(parts: list[str], source_url: str) -> ImportTarget:
    if len(parts) < 2:
        raise GitHubImportError("That GitHub URL is missing an owner/repo.")
    owner, repo = parts[0], parts[1]
    repo = repo[:-4] if repo.endswith(".git") else repo
    clone_url = f"https://github.com/{owner}/{repo}.git"
    target = ImportTarget(clone_url=clone_url, repo_name=repo, source_url=source_url)
    # …/tree/<ref>/<subdir…>  or  …/blob/<ref>/<path…>
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        target.ref = parts[3]
        rest = "/".join(parts[4:])
        if parts[2] == "blob":
            if not rest.endswith(".md"):
                raise GitHubImportError("A GitHub file link must point at a .md file.")
            target.explicit_file = rest
        else:
            target.subdir = rest
    return target


def _target_from_raw(parts: list[str], source_url: str) -> ImportTarget:
    # raw.githubusercontent.com/owner/repo/<ref>/<path…>
    if len(parts) < 4:
        raise GitHubImportError("That raw URL is missing owner/repo/ref/path.")
    owner, repo, ref = parts[0], parts[1], parts[2]
    path = "/".join(parts[3:])
    if not path.endswith(".md"):
        raise GitHubImportError("A raw file link must point at a .md file.")
    return ImportTarget(
        clone_url=f"https://github.com/{owner}/{repo}.git",
        repo_name=repo, ref=ref, explicit_file=path, source_url=source_url,
    )


def _target_from_gist(parts: list[str], source_url: str) -> ImportTarget:
    # gist.github.com/<user>/<id>  or  gist.github.com/<id>
    if not parts:
        raise GitHubImportError("That gist URL is missing an id.")
    gist_id = parts[-1]
    return ImportTarget(
        clone_url=f"https://gist.github.com/{gist_id}.git",
        repo_name=gist_id, source_url=source_url,
    )


def fetch_skill(url: str, token: str | None = None) -> ImportedSkill:
    """Import a skill from a GitHub URL: normalize → shallow-clone → locate the md →
    snapshot into `body`. Returns the raw material (name/body/provenance); the caller
    persists + assigns. Raises `GitHubImportError` on any failure (message scrubbed
    of the token)."""
    target = normalize_github_url(url)
    dest = Path(tempfile.mkdtemp(prefix="lea-ghimport-"))
    try:
        _clone(target, dest, token)
        md_path = _locate_md(dest, target)
        if md_path.stat().st_size > MAX_BODY_BYTES:
            raise GitHubImportError("That markdown file is too large to import as a skill.")
        raw = md_path.read_text(encoding="utf-8", errors="replace")
        meta, body = split_frontmatter(raw)
        body = body.strip()
        if not body:
            raise GitHubImportError("The skill markdown file is empty.")
        # A SKILL.md carries its own name/description; prefer them over anything derived
        # from the path, because the author wrote them for exactly this purpose.
        name = str(meta.get("name") or "").strip() or _derive_name(md_path, dest, target)
        is_skill_md = md_path.name.lower() == "skill.md"
        files = _collect_resources(md_path.parent) if is_skill_md else []
        # Roles live beside the *plugin*, not inside the skill folder
        # (`plugins/lean4/agents/` next to `plugins/lean4/skills/lean4/`), so search from
        # the repo root — bounded by the clone, which is already shallow.
        roles = _collect_roles(dest) if is_skill_md else []
        allowed = meta.get("allowed_tools")
        if isinstance(allowed, str):
            allowed = allowed.split()
        return ImportedSkill(
            name=name,
            body=body,
            description=(str(meta.get("description")).strip() if meta.get("description") else None),
            files=files,
            roles=roles,
            mcp_servers=_collect_mcp(md_path.parent) if is_skill_md else {},
            version=(str(meta.get("version")).strip() if meta.get("version") else None),
            license=(str(meta.get("license")).strip() if meta.get("license") else None),
            allowed_tools=[str(a) for a in allowed] if isinstance(allowed, list) else None,
            source_url=target.source_url,
            source_ref=target.ref or _head_sha(dest),
        )
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _clone(target: ImportTarget, dest: Path, token: str | None) -> None:
    """Shallow-clone `target` into `dest`. Tries the pinned ref first (branch/tag),
    then falls back to the default branch (a commit-SHA ref can't be `--branch`ed on
    a shallow clone). The token is injected for the clone only; errors are scrubbed."""
    try:
        clone_repository(
            target.clone_url,
            dest,
            ref=target.ref,
            token=token,
            timeout=CLONE_TIMEOUT_SECONDS,
        )
    except GitHubSourceError as exc:
        raise GitHubImportError(str(exc)) from None


def _locate_md(dest: Path, target: ImportTarget) -> Path:
    """Find the skill markdown (D56, fixed in v2.5 H1).

    An explicit file wins. Otherwise look for **SKILL.md anywhere in the tree**,
    shallowest first — that is where the AgentSkills standard puts a skill, and it is
    routinely several levels down (`plugins/lean4/skills/lean4/SKILL.md`). Only if there
    is none do we fall back to a root README, then to the first markdown found.

    This ordering is the fix: the old code looked for SKILL.md among the *direct children*
    only, so any repo with a root README silently imported its README — documentation
    ABOUT the skill — instead of the skill. Observed on `cameronfreer/lean4-skills`:
    8.9 KB of repo docs imported, a 29 KB SKILL.md and 41 references ignored.
    """
    if target.explicit_file:
        f = dest / target.explicit_file
        if not f.is_file():
            raise GitHubImportError(f"File not found in the repo: {target.explicit_file}")
        return f
    base = dest / target.subdir if target.subdir else dest
    if not base.is_dir():
        raise GitHubImportError(f"Path not found in the repo: {target.subdir}")

    skills = [p for p in base.rglob("*") if _in_repo(p) and p.is_file()
              and p.name.lower() == "skill.md"]
    if skills:
        # Shallowest wins, then alphabetical, so the choice is stable across clones.
        return sorted(skills, key=lambda p: (len(p.relative_to(base).parts), str(p)))[0]

    readme = _find_ci(base, "README.md")
    if readme:
        return readme
    mds = sorted(p for p in base.rglob("*.md") if _in_repo(p))
    if mds:
        return mds[0]
    raise GitHubImportError("No markdown (.md) file found to import as a skill.")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """`(metadata, body)` for a markdown file that may open with YAML frontmatter (H4).

    Without this the `---` block lands verbatim in the system prompt as literal YAML —
    and its `description:` is precisely the "when to use this" the authoring form asks
    for, already written by the skill's author. Malformed frontmatter is treated as
    ordinary text rather than failing the import.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            try:
                meta = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}, text
            if not isinstance(meta, dict):
                return {}, text
            return meta, "\n".join(lines[i + 1:]).lstrip("\n")
    return {}, text


def _collect_roles(plugin_dir: Path) -> list[dict]:
    """Sub-agent roles bundled beside a skill (H5).

    The AgentSkills/plugin layout puts them in `agents/*.md`, each a markdown body under
    frontmatter whose `name`/`description`/`tools`/`model` map almost field-for-field onto
    Lea's `AgentProfile` — the body IS the role's instructions. `lean4-skills` ships four
    (`proof-repair`, `proof-golfer`, `axiom-eliminator`, `sorry-filler-deep`).

    Tool lists are translated here (H6) rather than at spawn: the author wrote them for a
    different harness, and a name that survives to run time is silently dropped by B4
    instead of being visible to whoever is importing.
    """
    roles: list[dict] = []
    for agents_dir in sorted(p for p in plugin_dir.rglob("agents") if p.is_dir() and _in_repo(p)):
        for path in sorted(p for p in agents_dir.glob("*.md") if p.is_file()):
            try:
                meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if not body.strip():
                continue
            tools, unmapped = _tool_names.translate(meta.get("tools") if isinstance(
                meta.get("tools"), list) else str(meta.get("tools") or "").split(","))
            roles.append({
                "name": str(meta.get("name") or path.stem).strip(),
                "description": (str(meta.get("description")).strip()
                                if meta.get("description") else None),
                "system_prompt": body.strip(),
                "tools": tools or None,
                "unmapped_tools": unmapped,
            })
    return roles


def _collect_mcp(skill_dir: Path) -> dict:
    """MCP servers a skill declares in its own `.mcp.json` (H8).

    This is the AgentSkills standard's own bundling mechanism, and the reason "one-click
    MCP" is really "install the skill". Parsed here; the caller decides what to do with
    it — and creates the servers DISABLED, because starting third-party commands must
    stay an explicit human act.
    """
    for candidate in (skill_dir / ".mcp.json", skill_dir.parent / ".mcp.json"):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if isinstance(servers, dict):
            return servers
    return {}


def _collect_resources(skill_dir: Path) -> list[tuple[str, str]]:
    """Every text file under the skill's `references/`, `scripts/`, `assets/` (H2).

    These are the substance of a real skill — the entry point links them and expects them
    to be readable on demand. Binary/unreadable files are skipped rather than failing the
    import; a skill missing one asset is far better than no skill.
    """
    out: list[tuple[str, str]] = []
    budget = MAX_SKILL_BYTES
    for sub in _RESOURCE_DIRS:
        root = skill_dir / sub
        if not root.is_dir():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file() and _in_repo(p)):
            if len(out) >= MAX_SKILL_FILES or budget <= 0:
                return out
            try:
                if path.stat().st_size > MAX_BODY_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            budget -= len(text)
            out.append((str(path.relative_to(skill_dir)), text))
    return out


def _find_ci(base: Path, name: str) -> Path | None:
    """A direct child of `base` whose name matches `name` case-insensitively."""
    target = name.lower()
    for child in base.iterdir():
        if child.is_file() and child.name.lower() == target:
            return child
    return None


def _in_repo(path: Path) -> bool:
    """Skip anything inside a `.git` dir when scanning for markdown."""
    return ".git" not in path.parts


def _derive_name(md_path: Path, dest: Path, target: ImportTarget) -> str:
    """A friendly default name (user-editable later). A generic README/SKILL file
    takes its containing directory's name (or the repo name at the root); a named
    file uses its stem. Separators become spaces."""
    stem = md_path.stem
    if stem.lower() in ("readme", "skill"):
        parent = md_path.parent
        raw = target.repo_name if parent == dest else parent.name
    else:
        raw = stem
    name = raw.replace("-", " ").replace("_", " ").strip()
    return name or target.repo_name


def _head_sha(dest: Path) -> str | None:
    try:
        return head_sha(dest)
    except (subprocess.SubprocessError, GitHubSourceError):
        # F1: the import still succeeded — this is only the provenance sha recorded
        # against it. Logged rather than silently None so "imported from an unknown
        # commit" is diagnosable instead of looking like it was never recorded.
        logger.warning("Could not read HEAD sha of %s", dest, exc_info=True)
        return None
