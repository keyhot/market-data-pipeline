-- Hosting-migration guard: let the database say which deployment it is.
-- Idempotent — safe to re-run. Fresh volumes get the same table from
-- db/init.sql instead; keep both in sync.
-- Apply: docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_014.sql
--
-- Why: after the move to stream-a1 there are two databases and one of them is
-- the world's memory. `world_events` is append-only (docs/world-memory.md), so
-- a stray write from a dev session cannot be deleted — and with an SSH tunnel
-- open for visual QA, both databases answer on localhost:5432. The connection
-- URL cannot tell them apart. This row can. storage/db.py compares it against
-- DEPLOY_ROLE and opens the pool read-only when they disagree, which makes
-- every write path fail at Postgres rather than at a call site someone forgot.
--
-- Seeded 'dev' deliberately: the safe default is the one that cannot be the
-- world's memory. Stamp production explicitly, after the restore:
--   UPDATE deployment_identity SET role = 'prod';

CREATE TABLE IF NOT EXISTS deployment_identity (
    -- one row, enforced: a second role would make the guard ambiguous
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    role      TEXT NOT NULL CHECK (role IN ('dev', 'prod')),
    stamped_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO deployment_identity (singleton, role)
VALUES (TRUE, 'dev')
ON CONFLICT (singleton) DO NOTHING;
