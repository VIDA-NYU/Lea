"""timeline: allow kind='diagnostic' (v2.4 error transparency)

Revision ID: 0009_timeline_diagnostic_kind
Revises: 0008_multi_formalization_sessions

A failure the human needs to see is a timeline row like any other. It rides the same
channel as `compaction` (0006): `kind='diagnostic'`, `content` = the human message so
the existing "prose rows must have prose" CHECK still holds, and the structured
payload (severity, code, title, remedy, source, context) in the `data` JSON column
that 0003 created for exactly this and that nothing had used since.

Why the timeline and not a side table: a diagnostic's POSITION is most of its
meaning. "The tool failed" is only useful next to the step it failed on, in the turn
it happened in. A separate errors table would have to reconstruct that ordering, and
`timeline.id` already IS the ordering key.

`author` is 'environment' — the third value the 0003 CHECK already allows, and the
honest one: a diagnostic is neither the user's nor the agent's speech.

SQLite can't ALTER a CHECK in place, so this rebuilds the table exactly as 0006 did:
create the new shape, copy every row (ids preserved), drop, rename, recreate indexes.
Only the allowed-kinds set changes.

Rebased onto 0008 during the v2.4 merge. It originally sat at 0007 off
`0006_timeline_compaction_kind`; upstream meanwhile added `0006_artifact_index`,
merged the two 0006 heads in its own `0007`, and added `0008`. Left where it was,
Alembic would have had two heads and `upgrade head` would fail outright on startup —
a break git reports as no conflict at all, since the filenames differ.

Runs automatically on startup (db.init_db -> upgrade_to_head). Nothing to run by hand.
"""

from alembic import op

revision = "0009_timeline_diagnostic_kind"
down_revision = "0008_multi_formalization_sessions"
branch_labels = None
depends_on = None

# The 0006 shape with 'diagnostic' added to the kind CHECK. Everything else identical.
_TIMELINE_NEW = """
    create table timeline_new (
        id integer primary key autoincrement,
        session_id text not null references sessions(id),
        run_id text references runs(id),
        kind text not null,
        author text not null,
        content text,
        turn integer,
        path text,
        after_blob_id text references artifact_blobs(id),
        summary text,
        check_status text,
        check_detail text,
        artifact_kind text,
        content_lost integer not null default 0,
        data text,
        created_at text not null,
        -- Added by 0008_multi_formalization_sessions. A rebuild that omits a column
        -- DROPS it, silently, along with every value in it.
        formalization_id text references formalizations(id),
        check (kind in ('message', 'code', 'edit_note', 'compaction', 'diagnostic')),
        check (author in ('user', 'agent', 'environment')),
        check (content_lost in (0, 1)),
        check (kind <> 'code' or after_blob_id is not null or content_lost = 1),
        check (kind = 'code' or content is not null),
        check (artifact_kind is null or check_status = 'ok')
    )
"""

_COLUMNS = (
    "id, session_id, run_id, kind, author, content, turn, path, after_blob_id, "
    "summary, check_status, check_detail, artifact_kind, content_lost, data, created_at, "
    "formalization_id"
)


def upgrade() -> None:
    op.execute(_TIMELINE_NEW)
    op.execute(f"insert into timeline_new ({_COLUMNS}) select {_COLUMNS} from timeline")
    op.execute("drop table timeline")
    op.execute("alter table timeline_new rename to timeline")
    op.execute("create index if not exists ix_timeline_session on timeline(session_id, id)")
    op.execute(
        "create index if not exists ix_timeline_code on timeline(session_id, path, id) "
        "where after_blob_id is not null"
    )


def downgrade() -> None:
    # Same reason as 0006: the rebuild is reversible, the data it now permits is not.
    raise NotImplementedError("cannot narrow the timeline kind CHECK once diagnostic rows exist")
