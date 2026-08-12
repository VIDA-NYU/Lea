"""backfill timeline + artifact_blobs from messages/code_steps (+ git)

Revision ID: 0004_backfill_timeline
Revises: 0003_timeline_and_blobs

**The migrate step.** Copies every existing row into `timeline`, pulling proof
content out of git into `artifact_blobs`. Old tables are left completely intact —
dropping them is the contract step, deliberately a separate revision, so this can
be inspected (and re-run after a restore) before anything is destroyed.

Runs automatically on startup, so an older database upgrades by launching the app.
Nothing to run by hand.

## No data is dropped, and losses are recorded rather than hidden

Every message and code_step becomes a timeline row. Where content cannot be pulled
out of git the row still lands, with `content_lost = 1`. Deleting history because
the bytes are gone would compound the failure instead of recording it.

On this repo's real database: 31 of 32 code_steps recover; **1 does not**, and it
is worth knowing why, because it is not an accident of housekeeping. That row says
`RealLeAbsSelf.lean @ 51b6adf` — but 51b6adf is a commit whose subject is *"edit
.lea/memory.md"* and whose tree does not contain the file at all. It is
`commit_write`'s early return:

    staged = git diff --cached --name-only
    if not staged: return git rev-parse HEAD    # <- an unrelated commit's sha

i.e. the pointer was **wrong the moment it was written**, and nothing ever verified
that the sha contained the path. No amount of locking would have caught it. That is
the sharpest argument for this whole migration: after it, content is reachable by a
constraint-checked column instead of an unverified pointer into a second store.

## Ordering

Rows are inserted ordered by `(session_id, seq)`, so within a session the new
autoincrement `id` preserves the old `seq` order exactly — including the
message/code_step interleave, which is the entire point of the shared counter.
Order *across* sessions is meaningless (every read filters by session_id), and
global ids are allowed to interleave.

## Idempotency

Guarded on `timeline` being empty. A partial run cannot happen — 0003's
`transactional_ddl` work makes each revision all-or-nothing — but a *re-run* after
a manual restore would otherwise double every row.

## Repo resolution (D24)

A loose session's content is at `proofs/<session-id>`; a project session's is in the
**shared** repo at `proofs/Lea/<Project>`. Getting this wrong doesn't fail loudly —
it silently reports content as lost. (It fooled me: measuring only
`proofs/<session-id>` said 20 of 32 were unrecoverable, when the true number is 1.)
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid

from alembic import op

revision = "0004_backfill_timeline"
down_revision = "0003_timeline_and_blobs"
branch_labels = None
depends_on = None


def _proofs_root():
    """The proofs dir, via the app's own config so LEA_ROOT overrides are honoured."""
    from app.config import load_config

    return load_config().lea_root / "workspace" / "proofs"


def _repo_for(conn, session_id: str, proofs_root):
    """Resolve a session's repo. Mirrors projects.repo_for_session (D24): project
    sessions share `proofs/Lea/<Project>`, loose sessions get `proofs/<id>`."""
    row = conn.exec_driver_sql(
        "select project_id from sessions where id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    project_id = row[0]
    if project_id:
        prow = conn.exec_driver_sql(
            "select repo_path from projects where id = ?", (project_id,)
        ).fetchone()
        if prow and prow[0]:
            # repo_path is 'proofs/Lea/<Project>' — relative to the workspace, and
            # proofs_root already ends in 'proofs'.
            return proofs_root.parent / prow[0]
    return proofs_root / session_id


def _content_at(repo, sha: str, path: str) -> str | None:
    """`git show <sha>:<path>`, or None if unreachable (missing repo, unknown rev, or
    -- see the module docstring -- a sha whose tree never contained the path)."""
    if repo is None or not (repo / ".git").is_dir():
        return None
    proc = subprocess.run(
        ["git", "show", f"{sha}:{path}"], cwd=repo, capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else None


def _put_blob(conn, cache: dict[str, str], content: str, created_at: str) -> str:
    """Insert-or-find by sha256. Dedup is the schema's job (`sha256` is UNIQUE), so
    this stays a dumb upsert."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if digest in cache:
        return cache[digest]
    row = conn.exec_driver_sql(
        "select id from artifact_blobs where sha256 = ?", (digest,)
    ).fetchone()
    if row:
        cache[digest] = row[0]
        return row[0]
    blob_id = str(uuid.uuid4())
    conn.exec_driver_sql(
        "insert into artifact_blobs (id, sha256, content, created_at) values (?, ?, ?, ?)",
        (blob_id, digest, content, created_at),
    )
    cache[digest] = blob_id
    return blob_id


def upgrade() -> None:
    conn = op.get_bind()

    if conn.exec_driver_sql("select count(*) from timeline").fetchone()[0]:
        return  # already backfilled; never double-insert

    # One ordered stream per session, merging both tables on the shared counter —
    # exactly what `seq` existed for. Insert order defines the new ids.
    rows = conn.exec_driver_sql(
        """
        select session_id, seq, created_at, 'message' as src, run_id, role, content, kind,
               commit_sha, null as turn, null as author, null as path, null as summary,
               null as check_status, null as check_detail, null as artifact_kind
          from messages
        union all
        select session_id, seq, created_at, 'code' as src, run_id, null, null, null,
               commit_sha, turn, author, path, summary,
               check_status, check_detail, artifact_kind
          from code_steps
        order by session_id, seq
        """
    ).fetchall()

    if not rows:
        return  # a fresh database has nothing to carry forward

    # Resolved only once we know there is content to find, so a brand-new database
    # never depends on config or the filesystem just to start up.
    proofs_root = _proofs_root()
    blob_cache: dict[str, str] = {}
    repo_cache: dict[str, object] = {}
    recovered = lost = 0

    for r in rows:
        (session_id, _seq, created_at, src, run_id, role, content, kind, commit_sha,
         turn, author, path, summary, check_status, check_detail, artifact_kind) = r

        if src == "message":
            # role and kind were the same concept spelled twice, and `kind` defaulted
            # to 'assistant' (a role value used as a kind default) so it lies for any
            # row nobody set explicitly. `role` is the trustworthy one; `kind` is only
            # meaningful when it says 'edit_note'.
            new_kind = "edit_note" if kind == "edit_note" else "message"
            new_author = "user" if role == "user" else "agent"
            data = f'{{"legacy_commit_sha": "{commit_sha}"}}' if commit_sha else None
            conn.exec_driver_sql(
                "insert into timeline (session_id, run_id, kind, author, content, data,"
                " created_at) values (?, ?, ?, ?, ?, ?, ?)",
                (session_id, run_id, new_kind, new_author, content, data, created_at),
            )
            continue

        if session_id not in repo_cache:
            repo_cache[session_id] = _repo_for(conn, session_id, proofs_root)
        text = _content_at(repo_cache[session_id], commit_sha, path)

        if text is None:
            lost += 1
            blob_id, content_lost = None, 1
        else:
            recovered += 1
            blob_id, content_lost = _put_blob(conn, blob_cache, text, created_at), 0

        conn.exec_driver_sql(
            "insert into timeline (session_id, run_id, kind, author, turn, path,"
            " after_blob_id, summary, check_status, check_detail, artifact_kind,"
            " content_lost, created_at)"
            " values (?, ?, 'code', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, run_id, author if author in ("user", "agent") else "agent",
             turn, path, blob_id, summary, check_status, check_detail,
             artifact_kind if check_status == "ok" else None, content_lost, created_at),
        )

    print(f"[0004] backfilled {len(rows)} rows; content recovered {recovered}, "
          f"lost {lost}, distinct blobs {len(blob_cache)}")


def downgrade() -> None:
    op.execute("delete from timeline")
    op.execute("delete from artifact_blobs")
