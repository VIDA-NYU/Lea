"""Skill resolution seam (v2.1.1 W3, D48).

A skill is a DB row (markdown `body` in a column, D45); the prover consumes
`cfg.skills` as a list of file *paths* it reads and injects under `## Skill:
<stem>`. This module bridges the two: at run start it materializes the skills
that resolve for a project (global ∪ assigned, D47) to per-run temp `.md`
files — one `<slug>.md` per skill, so the prover's header reads `## Skill:
<slug>` cleanly — and hands `bridge.py` the paths to set on `cfg.skills`.

The temp dir lives in the system temp area, deliberately **not** inside any
project repo, so materialized skills never pollute the git-owned proof tree
(D7/D8). The caller owns cleanup (a run's `finally`). A loose (project-less)
session never calls this — it resolves to no skills by definition (D47).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from . import store


def materialize_run_skills(
    project_id: str | None, session_id: str | None = None, task: str | None = None
) -> tuple[list[str], str | None]:
    """Materialize the skills a RUN resolves to: (global ∪ assigned) ± the session's diff
    (E0e). Supersedes `materialize_project_skills`, which could only answer the project
    half — so a loose session got nothing and had no way to opt a skill in."""
    return _materialize(store.skills_for_run(project_id, session_id, task))


def materialize_project_skills(project_id: str) -> tuple[list[str], str | None]:
    """Write each skill that resolves for the project to `<tempdir>/<slug>.md`.

    Returns `(paths, tempdir)` in resolution order (so the prompt injection order
    is stable). When no skills resolve, returns `([], None)` and creates no dir —
    so the common (no-skills) path allocates nothing. The caller sets `paths` on
    `cfg.skills` and passes `tempdir` to `cleanup` in its `finally`.
    """
    return _materialize(store.skills_for_project(project_id))


def _materialize(skills: list[dict]) -> tuple[list[str], str | None]:
    """Write each resolved skill under `<tempdir>/`, returning its entry-point paths.

    Two layouts, matching the two injection modes (H2/H3):

      * single-file  -> `<tempdir>/<slug>.md`, exactly as before;
      * multi-file   -> `<tempdir>/<slug>/SKILL.md` plus its `references/` etc., so the
        relative links inside SKILL.md resolve when the agent opens them. This is why the
        tree shape matters and a flat dump of files would not do: the entry point says
        `see [x](references/x.md)`, and that only means anything if the tree is intact.

    The temp dir is handed to the run as `cfg.skills_root` (H7) so those reads are
    permitted; it stays outside every project repo, so materialized skills can never
    pollute the git-owned proof tree.
    """
    if not skills:
        return [], None
    tempdir = tempfile.mkdtemp(prefix="lea-skills-")
    paths: list[str] = []
    for skill in skills:
        files = store.skill_files(skill["id"])
        if not files:
            path = Path(tempdir) / f"{skill['slug']}.md"
            path.write_text(skill.get("body") or "")
            paths.append(str(path))
            continue
        root = Path(tempdir) / skill["slug"]
        root.mkdir(parents=True, exist_ok=True)
        entry = root / "SKILL.md"
        entry.write_text(skill.get("body") or "")
        skipped_files = 0
        for row in files:
            # `..` in a stored path would escape the skill root; the importer never
            # produces one, but a hand-edited row must not be able to write outside.
            rel = Path(row["path"])
            if rel.is_absolute() or ".." in rel.parts:
                skipped_files += 1
                continue
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_text(row["content"])
            except OSError:
                skipped_files += 1
        if skipped_files:
            # G5: a reference the entry point links but that never reached disk is a
            # `read_file` that will fail mid-proof for no visible reason. Report it.
            _warn_skipped(skill["slug"], skipped_files)
        paths.append(str(entry))
    return paths, tempdir


# G5: materialization problems are collected rather than swallowed. A loader that
# quietly returns fewer files than the DB holds makes its own failure undetectable —
# the same trap `load_overrides_checked` was written to escape.
_SKIPPED: list[tuple[str, int]] = []


def _warn_skipped(slug: str, count: int) -> None:
    _SKIPPED.append((slug, count))


def drain_skipped() -> list[tuple[str, int]]:
    out = list(_SKIPPED)
    _SKIPPED.clear()
    return out


def cleanup(tempdir: str | None) -> None:
    """Remove a materialized-skills temp dir (best effort). No-op for None."""
    if tempdir:
        shutil.rmtree(tempdir, ignore_errors=True)
