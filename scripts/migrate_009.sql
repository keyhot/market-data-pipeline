-- Sprint 9 migration: add signals + world_events to an EXISTING volume.
-- Idempotent (IF NOT EXISTS everywhere) — safe to re-run. Fresh volumes get
-- these tables from db/init.sql instead; keep both in sync.
-- Apply: docker compose exec -T postgres psql -U market_data -d market_data < scripts/migrate_009.sql

CREATE TABLE IF NOT EXISTS signals (
    symbol           TEXT        NOT NULL,
    interval         TEXT        NOT NULL,
    signal_timestamp TIMESTAMPTZ NOT NULL,
    model_version    TEXT        NOT NULL,
    horizon_bars     INTEGER     NOT NULL,
    direction        TEXT        NOT NULL,
    probability      DOUBLE PRECISION NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    outcome          TEXT,
    PRIMARY KEY (symbol, interval, signal_timestamp, model_version)
);

CREATE INDEX IF NOT EXISTS idx_signals_unresolved ON signals (symbol, signal_timestamp)
    WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS world_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    event_type  TEXT        NOT NULL,
    symbol      TEXT,
    severity    DOUBLE PRECISION NOT NULL,
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_world_events_time ON world_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_world_events_type_time ON world_events (event_type, occurred_at DESC);
