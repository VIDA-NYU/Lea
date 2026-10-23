"""Project filesystem service — view / read / edit / export the project repo (D34).

A project *is* a git repo on disk (D21/D22), so the Filesystem tab is mostly
*exposure*, not new storage: a tree of the repo's source + assets (hiding git and
Lake build internals), a text read of any file, an edit that funnels through the
same commit-on-write primitive as the v2 canvas (path-escape guarded, D3), and a
zip export of the whole thing. No new content store — git already owns the bytes.

The proofs root is passed in by callers (like the other project services) so this
stays testable against a scratch dir; it never imports config itself.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from .gitstore import GitStore
from .projects import project_repo_dir

# Directories never shown or written through the Filesystem tab: git internals and
# the Lake build tree (oleans live under .lake). `.lea/` IS shown — it holds the
# project docs + uploads, which are assets the user should see (D34, Open Q-7
# resolved: source + assets only).
IGNORED_DIRS = {".git", ".lake"}


class FilesystemError(ValueError):
    """A path escaped the repo, addressed a hidden dir, or a file isn't text. Carries
    a machine ``code`` so the route can map it to the right HTTP status."""

    def __init__(self, message: str, code: str = "bad_path"):
        super().__init__(message)
        self.code = code


def safe_abs(repo: Path, rel: str) -> Path:
    """Resolve a repo-relative path to an absolute path under ``repo``, rejecting
    escapes (``..``, absolute paths) and any path into a hidden dir. Mirrors the v2
    canvas-edit guard in ``sessions.write_file_session``.

    Public (it was ``_safe_abs``) because it is the one containment check the whole
    adapter should share. It had been re-implemented per call site, and the copy that
    was simply *missing* — ``routes/sessions._resolve_proof_path`` — was
    AUDIT-2026-07-24 S3. New code that joins caller-supplied path onto a repo should
    call this rather than write a fourth version of it."""
    if not rel or not str(rel).strip():
        raise FilesystemError("path is required", code="bad_path")
    abs_path = (repo / rel).resolve()
    try:
        relative = abs_path.relative_to(repo.resolve())
    except ValueError as exc:
        raise FilesystemError("path escapes the project repo", code="bad_path") from exc
    if relative.parts and relative.parts[0] in IGNORED_DIRS:
        raise FilesystemError(
            f"path is in a hidden directory: {relative.parts[0]}", code="bad_path"
        )
    return abs_path


def build_tree(repo: Path) -> list[dict]:
    """The repo's file tree as nested dicts (dirs first, then files; each alphabetical),
    hiding :data:`IGNORED_DIRS`. A dir is ``{name, path, type:"dir", children}``; a file
    is ``{name, path, type:"file", size}``. ``path`` is repo-relative POSIX. A missing
    repo → ``[]``. Hidden dirs are pruned before recursion, so ``.git`` is never walked."""
    if not repo.is_dir():
        return []

    def walk(directory: Path) -> list[dict]:
        entries: list[dict] = []
        for child in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if child.is_dir():
                if child.name in IGNORED_DIRS:
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "path": child.relative_to(repo).as_posix(),
                        "type": "dir",
                        "children": walk(child),
                    }
                )
            elif child.is_file():
                entries.append(
                    {
                        "name": child.name,
                        "path": child.relative_to(repo).as_posix(),
                        "type": "file",
                        "size": child.stat().st_size,
                    }
                )
        return entries

    return walk(repo)


def read_text_file(repo: Path, rel: str) -> str:
    """The text of one repo file (path-guarded). A missing file raises
    ``FilesystemError(code="not_found")``; a binary/undecodable file raises
    ``code="binary"`` so the route can steer the user to download instead."""
    abs_path = safe_abs(repo, rel)
    if not abs_path.is_file():
        raise FilesystemError("file not found", code="not_found")
    try:
        return abs_path.read_text()
    except UnicodeDecodeError as exc:
        raise FilesystemError("file is not text — use download", code="binary") from exc


def write_text_file(project: dict, proofs_root: Path, rel: str, content: str) -> str:
    """Write ``content`` to a repo file and commit it (D8, author=user), returning the
    new SHA. The project-wide generalization of the v2 canvas write — same path-guard
    + commit-on-write, any file in the repo. Parent dirs are created as needed."""
    repo = project_repo_dir(project, proofs_root)
    abs_path = safe_abs(repo, rel)
    from . import workspace_commands, workspace_files
    if workspace_commands.current() is not None:
        rel = abs_path.relative_to(repo.resolve()).as_posix()
        saved = workspace_files.save(None, rel, abs_path, content, project_id=project['id'])
        try:
            commit = GitStore(proofs_root).commit_all(repo, f'edit {rel}', paths=[rel])
        except Exception as exc:
            workspace_files.provenance(saved['effect_id'], detail=str(exc))
            from fastapi import HTTPException
            raise HTTPException(500, detail={'code': 'content_saved_commit_failed', 'message': 'Content is saved in the database; Git provenance could not be recorded.', 'effect_id': saved['effect_id']}) from exc
        workspace_files.provenance(saved['effect_id'], commit)
        return commit
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content)
    # Scoped to the edited file (X2) — this repo is shared across a project's sessions.
    return GitStore(proofs_root).commit_all(repo, f"edit {rel}", paths=[rel])


def export_zip(repo: Path) -> bytes:
    """A bounded, stable archive. External replacement cannot redirect a read."""
    limit=64*1024*1024;total=0;entries=0
    buf=io.BytesIO();top=repo.name;manifest=[]
    if repo.is_symlink():raise FilesystemError('Repository symlinks cannot be exported','bad_path')
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as archive:
        for directory,dirs,names in os.walk(repo,followlinks=False):
            dirs[:]=sorted(d for d in dirs if d not in IGNORED_DIRS|{'.lea-trash','displaced'} and not (Path(directory)/d).is_symlink())
            for name in sorted(names):
                entries+=1
                if entries>10000:raise FilesystemError('Export exceeds 10000 files','too_large')
                relative=(Path(directory)/name).relative_to(repo)
                descriptor=os.open(repo,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:
                    for part in relative.parts[:-1]:
                        child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor);os.close(descriptor);descriptor=child
                    file=os.open(relative.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor)
                    with os.fdopen(file,'rb') as source:
                        import stat
                        info=os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode):raise FilesystemError('Unsupported export entry','bad_path')
                        if total+info.st_size>limit:raise FilesystemError('Export input exceeds 64 MiB','too_large')
                        with archive.open(str(Path(top)/relative),'w') as destination:
                            while True:
                                chunk=source.read(min(65536,limit-total+1))
                                if not chunk:break
                                total+=len(chunk)
                                if total>limit:raise FilesystemError('Export input exceeds 64 MiB','too_large')
                                destination.write(chunk)
                                if buf.tell()>limit:raise FilesystemError('Export result exceeds 64 MiB','too_large')
                        after=os.fstat(source.fileno())
                        signature=lambda st:(st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
                        if signature(info)!=signature(after):raise FilesystemError('Workspace changed during export; request it again','changed')
                        manifest.append((repo/relative,signature(after)))
                except OSError as exc:raise FilesystemError('Export destination changed or is unavailable','changed') from exc
                finally:os.close(descriptor)
    if buf.tell()>limit:raise FilesystemError('Export result exceeds 64 MiB','too_large')
    for path,expected in manifest:
        current=path.stat(follow_symlinks=False)
        if (current.st_ino,current.st_size,current.st_mtime_ns,current.st_ctime_ns)!=expected:raise FilesystemError('Workspace changed during export; request it again','changed')
    return buf.getvalue()
