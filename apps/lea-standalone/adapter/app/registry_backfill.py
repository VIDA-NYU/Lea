"""One-time artifact backfill from a legacy registry markdown (PLAN 4.3).

Before the structured artifact index (4.1), the only record of "declaration X
lives at path Y" was the per-project registry markdown the prover agent wrote
(``workspace/projects/<slug>.md``, ``<!-- lea:theorem ... -->`` markers). This
importer parses those markers into the artifacts table once per project, so
the index is comprehensive for pre-index artifacts too — after which the
markdown is write-only from the machines' point of view (agent prose only).

Guarded by "the project has no artifact rows yet": a project touched by any
post-4.1 run never backfills, and the import can never clobber run-recorded
rows.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import store
from .artifacts import classify_lean_artifact
from .projects import project_repo_dir

_MARKER_RE = re.compile(r"<!--\s*lea:theorem\s+([^>]*?)-->")
_ATTR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)="([^"]*)"')


def _unescape(value: str) -> str:
    # Reverse of the companion's escapeHtmlAttribute (& last, like any
    # HTML-entity unescape).
    return (
        value.replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )


def backfill_artifacts_from_registry(project: dict, proofs_root: Path, registry_path: Path) -> int:
    """Import the registry's structured entries into the artifacts table.
    Returns the number of rows written; 0 when already indexed or no registry."""
    if store.list_artifacts_for_scope(project["id"]):
        return 0
    try:
        markdown = registry_path.read_text()
    except OSError:
        return 0

    namespace = str(project.get("namespace") or "")
    if not namespace:
        return 0
    prefix = "workspace/proofs/" + namespace.replace(".", "/") + "/"
    repo = project_repo_dir(project, proofs_root)

    written = 0
    for match in _MARKER_RE.finditer(markdown):
        attrs = {key: _unescape(value) for key, value in _ATTR_RE.findall(match.group(1))}
        name = attrs.get("name")
        proof = str(attrs.get("proof") or "").replace("\\", "/")
        if not name or not proof.startswith(prefix):
            continue
        rel = proof[len(prefix):]
        content = ""
        candidate = repo / rel
        if candidate.is_file():
            try:
                content = candidate.read_text()
            except OSError:
                content = ""
        store.upsert_artifact(
            project_id=project["id"],
            session_id=None,
            run_id=None,
            declaration_name=name,
            kind=classify_lean_artifact(content) if content else None,
            path=rel,
            module_name=attrs.get("module") or None,
        )
        written += 1
    return written
