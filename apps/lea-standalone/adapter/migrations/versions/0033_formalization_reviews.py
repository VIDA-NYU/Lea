"""Record user decisions separately from prover and compiler history."""
from alembic import op

revision = '0033_formalization_reviews'
down_revision = '0032_proof_check_evidence'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''create table if not exists formalization_review_events (
        id text primary key,
        formalization_id text not null references formalizations(id),
        project_id text references projects(id),
        session_id text references sessions(id),
        run_id text references runs(id),
        action text not null check(action in ('approve','withdraw')),
        outcome text check(outcome in ('proved','defined','disproved')),
        revision_token text not null,
        source_hash text,
        candidate_declaration text not null,
        candidate_path text not null,
        evidence_json text not null,
        approval_id text references formalization_review_events(id),
        idempotency_key text not null,
        request_digest text not null,
        actor text not null default 'user' check(actor = 'user'),
        created_at text not null,
        check((action = 'approve' and outcome is not null and approval_id is null)
           or (action = 'withdraw' and outcome is null and approval_id is not null)),
        unique(formalization_id, idempotency_key)
    )''')
    op.execute('create index if not exists ix_review_formalization on formalization_review_events(formalization_id, created_at)')
    # No-op retries can use a new key without creating another approval. Keep
    # their keys append-only too, so they cannot later be reused differently.
    op.execute('''create table if not exists formalization_review_requests (
        formalization_id text not null references formalizations(id),
        idempotency_key text not null,
        request_digest text not null,
        event_id text not null references formalization_review_events(id),
        primary key(formalization_id, idempotency_key)
    )''')


def downgrade():
    op.drop_table('formalization_review_requests')
    op.drop_table('formalization_review_events')
