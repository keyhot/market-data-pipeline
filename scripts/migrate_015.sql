-- Sprint 11 close-out: the music bed's now-playing row.
-- Idempotent — safe to re-run. Fresh volumes get the same table from
-- db/init.sql instead; keep both in sync.
-- Apply: docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_015.sql
--
-- Why a table and not a world_event: the bed changes track every ~90 seconds,
-- and world_events is append-only and feeds salience, mood and the director's
-- scene choice. Writing a track change there would add ~950 rows a day of
-- pure noise to the log the world's behaviour is computed from. This is
-- current state, not history, so it is one row that gets UPDATEd — which is
-- also why it is NOT in world_events, where updating anything is forbidden
-- (docs/world-memory.md).
--
-- Single row by construction: `id smallint PRIMARY KEY CHECK (id = 1)` makes
-- "there is exactly one now-playing" a schema fact rather than a convention
-- the writer is trusted to keep.

CREATE TABLE IF NOT EXISTS music_now_playing (
    id                smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    track_file        text        NOT NULL,
    title             text        NOT NULL,
    artist            text        NOT NULL,
    source            text        NOT NULL,
    source_url        text        NOT NULL,
    license           text        NOT NULL,
    duration_seconds  numeric(8, 1),
    started_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
