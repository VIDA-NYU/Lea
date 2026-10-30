"""Generate blueprint nodes from formalized artifacts (FEATURE-overleaf-blueprint-view).

The blueprint graph is derived from ``.lea/blueprint.md`` (:mod:`blueprint` parses it,
:mod:`graph` enriches it). Nothing writes that file automatically — so a project that
was *formalized* (real ``.lean`` proofs, recorded in the ``artifacts`` index) but never
*decomposed* has an empty graph. This module closes that gap on demand: it reads the
project's recorded artifacts and appends a ``## node`` section per formalized
declaration, so the graph populates from what actually exists.

Design:

- **Source of truth is the artifacts index** (``store.list_artifacts_for_scope``) — the
  decls the run finalizer recorded, i.e. "what you formalized". One node per artifact.
- **Additive + idempotent.** A node is emitted only for an artifact not already
  represented (matched by ``lean:`` FQN), and existing nodes are never modified or
  removed (the document is re-rendered canonically, :func:`blueprint.render`). Safe
  to re-run after formalizing more theorems; never clobbers hand/agent authoring
  done in the standalone UI.
- **Edges use compiler-resolved constants**, including namespace and binder scope.
  Incomplete inspection leaves the blueprint unchanged and exposes diagnostics.

The status/color of the generated nodes is *not* written here — it stays derived from
live Lean state by :mod:`graph`, exactly as for hand-authored nodes.
"""

from __future__ import annotations

import re
import json
import subprocess

from . import blueprint
from . import graph
from . import projects as project_service
from . import store
from .artifacts import scan_lean_declarations

# Lean decl keyword → blueprint kind (definition = box, lemma/theorem = ellipse).
_KEYWORD_KIND = {
    "theorem": "theorem",
    "lemma": "lemma",
    "def": "definition",
    "abbrev": "definition",
    "instance": "definition",
    "structure": "definition",
    "inductive": "definition",
    "class": "definition",
}

# Cap the auto-extracted signature used as node statement prose.
_SIGNATURE_CAP = 240
_SIGNATURE_DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?"
    r"(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+|"
    r"scoped\s+|local\s+)*"
    r"(?:theorem|lemma|def|abbrev|structure|class|inductive|coinductive|instance|opaque)\s+"
    r"[A-Za-z_][A-Za-z0-9_'.]*"
)


def _unique_key(base: str, taken: set[str]) -> str:
    """A blueprint key that doesn't collide with one already in the file/batch."""
    key = base or "node"
    n = 2
    while key in taken:
        key = f"{base}_{n}"
        n += 1
    taken.add(key)
    return key


def _signature(span_text: str) -> str:
    """The decl header from after its name up to ``:=`` (binders + type), as the node's
    statement. The source's own line breaks are **preserved** so a multi-line signature
    reads the same here as in the Lean pane; only trailing per-line spaces, blank outer
    lines, and the gap after the decl name are trimmed. Capped. ``""`` when unusable."""
    if not span_text:
        return ""
    head = span_text.split(":=", 1)[0]
    decl = _SIGNATURE_DECL_RE.match(head)
    if decl:
        head = head[decl.end():]
    lines = [line.rstrip() for line in head.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if lines:
        lines[0] = lines[0].lstrip()  # drop the space between the decl name and its binders
    return "\n".join(lines)[:_SIGNATURE_CAP].rstrip()


def _kind_for(keyword: str | None, artifact_kind: str | None) -> str:
    """Blueprint kind from the Lean keyword, falling back to the artifact's kind."""
    if keyword and keyword in _KEYWORD_KIND:
        return _KEYWORD_KIND[keyword]
    return "definition" if artifact_kind == "definition" else "lemma"


def _node_block(candidate: dict) -> dict:
    """A synthesized candidate as a parsed node block (rendered by :func:`blueprint.render`)."""
    return {"type": "node", "key": candidate["key"], "kind": candidate["kind"],
            "lean": candidate["lean"], "file": candidate.get("path") or None,
            "uses": list(candidate["uses"]), "statement": candidate["statement"]}


def _compiler_references(project: dict, repo, relative: str, *, required=()) -> dict[str, list[str]]:
    """Use current compiler-selected constants, never source token guesses.

    Final checks normally already contain the selected declaration's references.
    Generation may inspect other declarations explicitly on the worker, but a
    graph read never invokes the compiler.
    """
    steps = store.code_steps_for_project_path(project['id'], relative)
    step = steps[0] if steps else None
    if step and step.get('check_current') and step.get('code') == (repo / relative).read_text():
        from .db import connect
        with connect() as conn:
            row = conn.execute('select evidence_json from proof_check_evidence where id=?',
                               (step.get('check_evidence_id'),)).fetchone()
        selected = json.loads(row[0]).get('selected_result') if row else None
        if (selected and isinstance(selected.get('dependencies'), list)
                and set(required) <= {selected['user_name']}):
            return {selected['user_name']: selected['dependencies']}
    from .project_library import lake_root
    if lake_root(repo) is None:
        raise ValueError('No compatible Lake workspace is available for dependency inspection.')
    from lea.result_selection import inspect_file
    report = inspect_file(repo / relative)
    if not report.get('complete'):
        raise ValueError('Compiler inspection was incomplete.')
    return {d['user_name']: d['dependencies'] for d in report['declarations']}


def generate(project: dict, proofs_root) -> dict:
    """Append blueprint nodes for the project's formalized artifacts (additive,
    idempotent). Writes + commits ``.lea/blueprint.md`` when anything is added.

    Returns ``{"added": int, "skipped": int, "warnings": [str], "graph": {...}}`` —
    ``skipped`` counts artifacts already represented; ``graph`` is the freshly derived
    graph so a caller can render in one round-trip."""
    repo = project_service.project_repo_dir(project, proofs_root)
    artifacts = store.list_artifacts_for_scope(project["id"])

    existing_text = project_service.read_doc(project, proofs_root, "blueprint.md")
    parsed = blueprint.parse(existing_text)
    existing_nodes = parsed["nodes"]
    taken_keys = {n["key"] for n in existing_nodes}

    fqn_to_file, file_to_text = graph._scan_lean_decls(repo)
    existing_leans = {graph._resolve_name(n['lean'], fqn_to_file) or n['lean']
                      for n in existing_nodes if n['lean']}

    warnings: list[str] = []
    candidates: list[dict] = []
    skipped = 0
    for art in artifacts:
        recorded_name = art["declaration_name"]
        # Ordinary target artifacts historically record a short name. Resolve
        # it only when unique, using the same identity rules as graph reads.
        fqn = graph._resolve_name(recorded_name, fqn_to_file) if recorded_name else None
        if not recorded_name or fqn in existing_leans:
            skipped += 1
            continue
        if not fqn:
            warnings.append(f"`{recorded_name}` is recorded but its exact or unambiguous declaration was not found; no node was added.")
            skipped += 1
            continue
        short = graph._short(fqn)
        text = file_to_text.get(art.get("path") or "", "")
        declaration = next((d for d in scan_lean_declarations(text) if d.full_name == fqn), None)
        keyword, span = (declaration.keyword, declaration.span) if declaration else (None, '')
        if keyword is None:
            warnings.append(f"`{recorded_name}` is recorded but its exact or unambiguous declaration was not found; no node was added.")
            skipped += 1
            continue
        candidates.append({
            "key": _unique_key(short, taken_keys),
            "lean": fqn,
            "path": art["path"],
            "kind": _kind_for(keyword, art.get("kind")),
            "statement": _signature(span) or f"Formalized as `{fqn}`.",
        })

    keys_by_name = {graph._resolve_name(node['lean'], fqn_to_file) or node['lean']: node['key']
                    for node in existing_nodes if node['lean']}
    keys_by_name.update({cand['lean']: cand['key'] for cand in candidates})
    references = {}
    by_path = {}
    for candidate in candidates:
        by_path.setdefault(candidate['path'], set()).add(candidate['lean'])
    for relative, required in by_path.items():
        try:
            inspected = _compiler_references(project, repo, relative, required=required)
            if required - inspected.keys():
                raise ValueError('A recorded declaration is missing from the compiled module.')
            references.update(inspected)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f'Dependencies for {relative} could not be resolved by Lean: {exc}')
    for cand in candidates:
        # Missing inspection is visible and cannot manufacture a dependency.
        cand['uses'] = list(dict.fromkeys(keys_by_name[name]
            for name in references.get(cand['lean'], [])
            if name in keys_by_name and keys_by_name[name] != cand['key']))

    # A partial generation must not persist incomplete edges and then call itself
    # idempotently complete on the next attempt. Keep the existing document until
    # all new declarations have compiler references; retry remains safe.
    complete = all(c['lean'] in references for c in candidates)
    if candidates and complete:
        unchanged = (project_service.read_doc(project, proofs_root, 'blueprint.md') == existing_text
                     and all((repo / path).read_text() == file_to_text[path] for path in by_path))
        if not unchanged:
            complete = False
            warnings.append('Project source or Blueprint changed during inspection. Refresh and generate again.')
    if candidates and complete:
        # Append through the canonical renderer: the whole document comes out in
        # canonical form, and `render` closes a dangling ``` fence before the next
        # section, so appended nodes never land inside a fence (which would parse as
        # inert and re-add them on every run, breaking idempotency).
        document = dict(parsed)
        if not (document.get("preamble") or "").strip():
            document["preamble"] = blueprint.parse(blueprint.empty_document(project["title"]))["preamble"]
        document["blocks"] = [*parsed["blocks"], *(_node_block(c) for c in candidates)]
        new_text = blueprint.render(document)
        project_service.write_doc(project, proofs_root, "blueprint.md", new_text)
        store.note_blueprint_change(project["id"])

    return {
        "added": len(candidates) if complete else 0,
        "skipped": skipped,
        "warnings": warnings,
        "graph": graph.build_graph(project, proofs_root),
    }
