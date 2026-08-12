# Database migrations

The standalone app keeps your sessions, runs, proof timeline, and usage in a local
**SQLite** database at `apps/lea-standalone/data/lea-interface.sqlite3` (gitignored).
The schema is versioned with **Alembic** migrations in
`apps/lea-standalone/adapter/migrations/versions/`.

Since v2.3 the database is **no longer disposable** — SQL owns your proof content, so
"just delete it and start over" means "delete your proofs." That's why schema changes
go through real, ordered migrations instead of a reset.

## TL;DR — upgrading an existing database (for a collaborator pulling new code)

**You don't run migrations by hand. Starting the adapter does it for you.**

```sh
git pull                       # get the new code (and any new migrations)
npm run setup                  # only if dependencies changed; safe to run anyway
npm run start:adapter          # (or: npm run dev:ui) — migrations apply on startup
```

On startup the adapter calls `init_db() → upgrade_to_head()`, which:

1. checks the DB's current revision against the newest migration on disk;
2. if — and only if — a migration is pending, **takes a snapshot of your DB first**
   into `apps/lea-standalone/data/backups/` (if the snapshot fails, the migration
   does **not** run and the adapter refuses to start — refusing is recoverable,
   migrating your only copy without a fallback is not);
3. applies every pending migration in order, then serves.

It's **idempotent**: if your DB is already current, startup is a no-op (no backup, no
change). So this is safe to run repeatedly, and safe for a friend whose DB is several
revisions behind — every pending step is applied in one pass.

Your data is preserved; the migrations transform it in place.

## Verifying

```sh
cd apps/lea-standalone/adapter
./.venv/bin/python -m alembic current   # the revision your DB is stamped at
./.venv/bin/python -m alembic heads     # the newest revision available in the code
```

When `current` == `heads`, you're up to date.

## Manual upgrade (advanced)

Normally unnecessary — prefer starting the adapter, because that path snapshots your
DB first. If you must run it directly:

```sh
cd apps/lea-standalone/adapter
cp ../data/lea-interface.sqlite3 ../data/lea-interface.sqlite3.bak   # back up yourself —
                                                                     # raw alembic does NOT auto-snapshot
./.venv/bin/python -m alembic upgrade head
```

## Rollback / recovery

- **Restore a snapshot:** copy the newest file from `apps/lea-standalone/data/backups/`
  back over `apps/lea-standalone/data/lea-interface.sqlite3` (adapter stopped).
- **Downgrades are not supported** for data-bearing revisions — some migrations
  deliberately raise on `downgrade` because reversing them could lose data (e.g. a
  row using a value a narrower constraint would reject). Recover by restoring a
  snapshot, not by downgrading.

## Revision history

| Revision | What it does |
|----------|--------------|
| `0001_baseline` | Initial schema (sessions, runs, messages, usage, code steps). |
| `0002_reconcile_legacy_drift` | Reconcile columns that drifted on pre-migration databases. |
| `0003_timeline_and_blobs` | One `timeline` table (messages + code steps) + content-addressed `artifact_blobs`; SQL owns proof content. |
| `0004_backfill_timeline` | Backfill the timeline/blobs from the old message/code-step tables + git. |
| `0005_session_parent` | Sub-agent session tree: `parent_id` / `role` / `spawned_at_turn` on `sessions`. |
| `0006_timeline_compaction_kind` | Allow `kind='compaction'` on `timeline` — the durable context-compaction (`/compact`) marker. |

## Adding a new migration (for maintainers)

Never edit an applied revision and never hand-`ALTER` at startup. Add a new revision
file under `migrations/versions/` (increment the number, set `down_revision` to the
current head), and it runs automatically on the next adapter start. See the existing
files for the SQLite table-rebuild pattern used when a `CHECK` constraint changes.
