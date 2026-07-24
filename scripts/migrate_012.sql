-- Sprint 12 migration: a natural-key unique index on world_events so the
-- historical backfill is re-runnable. Idempotent — safe to re-run.
-- Applies to EXISTING volumes; fresh volumes get this from db/init.sql.
-- Apply: docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_012.sql
--
-- This constrains INSERTs only. It does NOT weaken the append-only contract:
-- no row is ever updated or deleted (docs/world-memory.md).
--
-- NULLS NOT DISTINCT (Postgres 15+) matters: stream_* events carry symbol
-- NULL, and default NULL semantics would let them duplicate freely.

CREATE UNIQUE INDEX IF NOT EXISTS uq_world_events_natural
    ON world_events (event_type, occurred_at, symbol) NULLS NOT DISTINCT;
