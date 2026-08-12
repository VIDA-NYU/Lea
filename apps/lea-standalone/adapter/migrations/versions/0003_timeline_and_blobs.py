"""artifact_blobs + timeline — SQL takes ownership of proof content

Revision ID: 0003_timeline_and_blobs
Revises: 0002_reconcile_legacy_drift

**Expand step only.** This adds the new tables; nothing reads or writes them yet,
`messages`/`code_steps` are untouched, and no behaviour changes. Backfill is 0004;
switching the code over and dropping the old tables come after that. (expand →
migrate → contract, so a failure at any point is recoverable.)

Two tables, not one, and NOT because of dedup — measured on this repo's real data
the dedup ratio is 0.83 (18% saved), which would not justify a join on its own.
The reasons that do:
  - Access pattern. `timeline` is scanned per session, ordered, for the chat view;
    content is cold, fetched one step at a time when the canvas opens. Inlining
    multi-KB proof text in the hot table is the wrong shape.
  - It is the seam for moving binaries (uploads/PDFs) to object storage later
    without touching call sites. Inline content has nowhere to move to.
  - Many-to-one is real: a cascade step reuses content, and saying so once is more
    truthful than copying it.

## `timeline` merges `messages` + `code_steps`

`id integer primary key autoincrement` **is** the ordering key — there is no `seq`
column. That is what makes the shared-counter bug unrepresentable rather than
guarded: the old `seq` was `max()+1` across *two* tables, so `UNIQUE(session_id,
seq)` could not be expressed (no SQL constraint spans two tables) and the guarantee
rested on transaction discipline. One table + a primary key = the DB enforces it and
no caller can forget.

Measured (8 threads x 25 inserts): autoincrement with *no* transaction gives 200/200
unique ids, 0 lastrowid mismatches. The `AUTOINCREMENT` keyword is required — a
plain `integer primary key` reuses ids after a delete, so a later row would inherit
a dead row's timeline position.

Gaps are fine and always were: `useProofStream.ts` already invents fractional seqs
(+0.5, +1e-4) to interleave approvals, so contiguity is deliberately violated
client-side today. Every consumer only sorts or compares. (Note SQLite does *not*
gap on rollback — `sqlite_sequence` is transactional — but Postgres sequences do,
so nothing may rely on density.)

## Typed columns vs `data`

Anything carrying a guarantee is a real column; `data` (JSON) is only for what
genuinely varies per tool (args, previews, diagnostics). JSON is a docstring in data
form — once `after_blob_id` is the only pointer to the only copy of a proof, a
dangling reference is a silently lost proof, and `json_extract` can't be NOT NULL or
CHECKed. opencode does the same thing (typed `summary_*` columns beside JSON part
data). This threads `docs/STORAGE-code-steps-vs-timeline.md` rather than
contradicting it: that doc rejected a *fully generic* `timeline_events(type,
data_json)` because Lea's core queries would go through JSON parsing — with typed
columns they stay simple.

## `kind` / `author`

Follows OpenHands, explicitly not opencode. OpenHands separates
`SourceType = ["agent","user","environment","hook"]` (who) from the event class
(what). opencode's message `type` is `user | synthetic | system | shell |
agent-switched | ...`, which *conflates* who and what — structurally the same
mistake we're fixing here: today `messages.kind` defaults to `'assistant'` (a role
value used as a kind default), so 26 rows in the live DB have `role='user'` with
`kind='assistant'`. `kind` is really just "is this an edit_note".

`role` and `author` were the same concept spelled twice ('assistant' == 'agent'), so
they collapse into one column; the LLM role needed for transcript replay stays
derivable. `environment` is OpenHands' name for "the world changed it, not an
actor" — that's where bash-written files land, since `FileChanged` only fires for
write_file/edit_file on .lean. `cascade` is dropped as an author (it had **zero**
rows — aspirational in a comment): it's a trigger, not an identity, so it belongs in
`data`.

## `content_lost`

Not defensive programming — 20 of the 32 existing `code_steps` point at git repos
that no longer exist (`reset-local-state.mjs` deletes the proofs dir while the rows
survive). Their bytes are **already unrecoverable**, which is exactly the
two-sources-of-truth drift that motivated moving content into SQL. 0004 cannot
restore them. The marker records the loss instead of deleting the user's history,
and it earns its place: it's what lets the CHECK below stay strict for every row
whose content we *do* have.
"""

from alembic import op

revision = "0003_timeline_and_blobs"
down_revision = "0002_reconcile_legacy_drift"
branch_labels = None
depends_on = None

STATEMENTS = [
    # Content-addressed store. `sha256` is UNIQUE, so dedup is the schema's job, not
    # the caller's: put_blob is "insert or find". `content` is TEXT — proof files are
    # UTF-8. Binaries (uploads) stay in git for now and move to object storage later
    # behind this table's interface, which is one reason it's a separate table.
    """
    create table if not exists artifact_blobs (
        id text primary key,
        sha256 text not null unique,          -- dedup key; content addressing
        content text not null,
        created_at text not null
    )
    """,
    """
    create table if not exists timeline (
        -- IS the ordering key. No `seq` column: the DB allocates, so the
        -- read-modify-write that produced duplicate seqs cannot be written.
        -- AUTOINCREMENT (not bare `integer primary key`) so ids are never reused
        -- after a delete.
        id integer primary key autoincrement,

        session_id text not null references sessions(id),
        run_id text references runs(id),      -- NULL for user edits (no run)

        kind text not null,                   -- 'message' | 'code' | 'edit_note'
        author text not null,                 -- 'user' | 'agent' | 'environment'

        -- message / edit_note
        content text,                         -- the prose

        -- code
        turn integer,                         -- agent turn; NULL for user edits
        path text,
        after_blob_id text references artifact_blobs(id),   -- the ONLY content pointer
        summary text,
        check_status text,                    -- 'ok' | 'error' | NULL (unchecked)
        check_detail text,
        artifact_kind text,                   -- set only when check_status='ok'

        -- 1 = the bytes are known-unrecoverable (a legacy row whose git repo was
        -- deleted). Lets the CHECK below stay strict for every row we can back.
        content_lost integer not null default 0,

        data text,                            -- JSON: per-tool specifics only
        created_at text not null,

        -- Enforcement, not convention. These are the guarantees that used to live in
        -- docstrings and were therefore false.
        check (kind in ('message', 'code', 'edit_note')),
        check (author in ('user', 'agent', 'environment')),
        check (content_lost in (0, 1)),
        -- A code row must point at content, or say why it can't.
        check (kind <> 'code' or after_blob_id is not null or content_lost = 1),
        -- Prose rows must have prose.
        check (kind = 'code' or content is not null),
        -- artifact_kind is meaningful only for a passing check (mirrors the old
        -- code_steps rule, which was enforced in Python and so could drift).
        check (artifact_kind is null or check_status = 'ok')
    )
    """,
    # The hot query: one session's thread, in order.
    "create index if not exists ix_timeline_session on timeline(session_id, id)",
    # Proof history / "latest step for this path" — partial, so it stays small and
    # skips the prose rows entirely.
    """
    create index if not exists ix_timeline_code on timeline(session_id, path, id)
    where after_blob_id is not null
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("drop index if exists ix_timeline_code")
    op.execute("drop index if exists ix_timeline_session")
    op.execute("drop table if exists timeline")
    op.execute("drop table if exists artifact_blobs")
