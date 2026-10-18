"""Shared SQL/admission functions. No workspace or executor dependency."""
from __future__ import annotations
import re
from pathlib import Path
from . import project_coordination
from . import store

class ProjectIdentityError(ValueError):

    def __init__(self, code: str, message: str, *, status: int=400, detail: dict | None=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail or {}

def namespace_segment_from_name(name: str) -> str:
    parts = re.split('[^A-Za-z0-9]+', str(name or '').strip())
    segment = ''.join((part[:1].upper() + part[1:] for part in parts if part))
    segment = re.sub('[^A-Za-z0-9]', '', segment)
    if not segment or not segment[0].isalpha():
        segment = f'P{segment}'
    return segment[:80] or 'OverleafProject'

def namespace_from_project_name(name: str) -> str:
    return f'Lea.{namespace_segment_from_name(name)}'

def normalize_namespace(namespace: str | None, project_name: str) -> str:
    raw = str(namespace or '').strip()
    value = raw if raw else namespace_from_project_name(project_name)
    if not value.startswith('Lea.'):
        value = f'Lea.{value}'
    try:
        return store.validate_project_namespace(value)
    except ValueError as exc:
        raise ProjectIdentityError('invalid_namespace', str(exc), status=422) from None

def namespace_preview(project_name: str, *, namespace: str | None=None, exclude_project_id: str | None=None) -> dict:
    project_title = (project_name or '').strip()
    if not project_title:
        raise ProjectIdentityError('missing_project_name', 'Project name is required.')
    normalized = normalize_namespace(namespace, project_title)
    owner = store.get_project_by_namespace(normalized)
    available = owner is None or owner.get('id') == exclude_project_id
    suggestions: list[str] = []
    if not available:
        base = normalized
        for suffix in ('2', '2026', 'Project'):
            candidate = f'{base}{suffix}'
            if store.get_project_by_namespace(candidate) is None:
                suggestions.append(candidate)
        n = 3
        while len(suggestions) < 3:
            candidate = f'{base}{n}'
            if store.get_project_by_namespace(candidate) is None:
                suggestions.append(candidate)
            n += 1
    return {'project_name': project_title, 'namespace': normalized, 'available': available, 'suggestions': suggestions}

def unique_namespace_for_project_name(name: str, *, conn=None, proofs_root: Path | None=None) -> str:
    base = namespace_from_project_name(name)

    def available(candidate):
        if conn is not None:
            taken = any((row[0].casefold() == candidate.casefold() for row in conn.execute('select namespace from projects')))
        else:
            taken = store.get_project_by_namespace(candidate) is not None
        return not taken
    candidate, n = (base, 2)
    while not available(candidate):
        candidate, n = (f'{base}{n}', n + 1)
    return candidate

def _unique_default_name(conn, title: str, *, exclude_project_id: str | None=None) -> str:
    taken = {row['title'].strip().casefold() for row in conn.execute('select id, title from projects') if row['id'] != exclude_project_id}
    candidate, n = (title, 2)
    while candidate.casefold() in taken:
        candidate, n = (f'{title} ({n})', n + 1)
    return candidate

def _adopt_overleaf_name(conn, project: dict, title: str, proofs_root: Path | None) -> dict:
    title = str(title or '').strip()
    if not title or project['name_source'] != 'generated':
        return project
    title = _unique_default_name(conn, title, exclude_project_id=project['id'])
    from .workspace_commands import authorize
    authorize(conn)
    conn.execute("update projects set title = ?, name_source = 'overleaf', updated_at = ? where id = ?", (title, store.utc_now(), project['id']))
    return store._normalize_project(store.row_to_dict(conn.execute('select * from projects where id = ?', (project['id'],)).fetchone()))

def project_identity(project: dict, *, overleaf_project_id: str | None=None) -> dict:
    from .queries.identity import project_identity as query
    return query(project, overleaf_project_id=overleaf_project_id)

def ensure_project(slug: str, proofs_root: Path | None, title: str | None=None, namespace: str | None=None, *, overleaf_project_title: str | None=None, name_source: str | None=None) -> dict:
    """Get-or-create a project by slug (the Overleaf path) AND ensure its on-disk repo is
    provisioned with the seeded ``.lea/*.md`` docs — the idempotent, slug-keyed analogue
    of :func:`provision_project`, so an Overleaf-originated project is provisioned exactly
    like a UI-created one (D25/D26/D28).

    Seeds each doc only when missing (never clobbers an edited ``memory.md`` /
    ``blueprint.md``) and commits only when something was written, so it is safe to call
    on every run/mirror and backfills projects created tag-only before this existed. Disk
    provisioning is best-effort — the index row is the source of truth and is always
    returned; a missing ``proofs_root`` (lea_root unconfigured) just skips the disk half."""
    existing = store.get_project_by_slug(slug)
    previous_project = None
    with project_coordination.mutation(existing['id'] if existing else None), store.write() as conn:
        row = conn.execute('select * from projects where slug = ?', (slug,)).fetchone()
        if row:
            project = store._normalize_project(store.row_to_dict(row))
            previous_project = project
            if overleaf_project_title is not None:
                project = _adopt_overleaf_name(conn, project, overleaf_project_title, proofs_root)
        else:
            if overleaf_project_title is not None:
                source_title = overleaf_project_title.strip()
                project_title = _unique_default_name(conn, source_title or 'Overleaf Project')
                source = 'overleaf' if source_title else 'generated'
                namespace = None
            else:
                project_title = (title or slug).strip() or slug
                defaults = {slug, store.project_namespace_for_slug(slug)[4:], 'Overleaf Project'}
                source = 'generated' if project_title in defaults else 'custom'
            ns = normalize_namespace(namespace, project_title) if namespace else None
            if ns is None or any((r[0].casefold() == ns.casefold() for r in conn.execute('select namespace from projects'))):
                ns = unique_namespace_for_project_name(project_title, conn=conn, proofs_root=proofs_root)
            project = store.create_project(slug, title=project_title, namespace=ns, repo_path=store.repo_path_for_namespace(ns), name_source=name_source or source, conn=conn)
    return project
