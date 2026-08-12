"""timeline: allow kind='compaction' (G1/G3 context-compaction marker)

Revision ID: 0006_timeline_compaction_kind
Revises: 0005_session_parent

A context compaction — the condenser pruning/summarizing a run's history, whether at
the automatic threshold (G1) or via the user's `/compact` (G3) — is surfaced as a
durable timeline marker so it survives a reload (the box used to vanish on refresh,
because it lived only in ephemeral client state). The marker rides the SAME channel as
an `edit_note`: an ordinary timeline row, `kind='compaction'`, whose `content` is a JSON
payload the UI renders as the "Compacted — freed ~N tokens" card. It is a marker, never
proof content, so it never becomes a code_step.

The `timeline.kind` CHECK from 0003 (`kind in ('message','code','edit_note')`) has to
learn the new value. SQLite can't ALTER a CHECK in place, so this rebuilds the table:
create the new shape, copy every row (ids preserved — `id` IS the ordering key), drop
the old, rename, recreate the two indexes. Only the CHECK's allowed-kinds set changes;
every other column, constraint, and FK is byte-identical to 0003.

Runs automatically on startup (db.init_db → upgrade_to_head). Nothing to run by hand.
"""

from alembic import op

revision = "0006_timeline_compaction_kind"
down_revision = "0005_session_parent"
branch_labels = None
depends_on = None

# The 0003 timeline shape, with 'compaction' added to the kind CHECK. Everything else is
# identical — a rebuild only because SQLite can't alter a CHECK in place.
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
        check (kind in ('message', 'code', 'edit_note', 'compaction')),
        check (author in ('user', 'agent', 'environment')),
        check (content_lost in (0, 1)),
        check (kind <> 'code' or after_blob_id is not null or content_lost = 1),
        check (kind = 'code' or content is not null),
        check (artifact_kind is null or check_status = 'ok')
    )
"""

_COLUMNS = (
    "id, session_id, run_id, kind, author, content, turn, path, after_blob_id, "
    "summary, check_status, check_detail, artifact_kind, content_lost, data, created_at"
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
    # Not reversible: a row with kind='compaction' would violate the old CHECK, so a
    # blind downgrade could fail on real data. (The rebuild itself is reversible; the
    # data it now permits is not.)
    raise NotImplementedError("cannot narrow the timeline kind CHECK once compaction rows exist")
