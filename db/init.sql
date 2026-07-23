-- L2 storage schema — applied automatically on first container boot via
-- docker-entrypoint-initdb.d. Source of truth: docs/postgres-schema-spike.md.
-- Re-applying requires an empty data volume (docker compose down -v).

CREATE TABLE price_bars (
    symbol        TEXT        NOT NULL,
    bar_timestamp TIMESTAMPTZ NOT NULL,
    interval      TEXT        NOT NULL,          -- bar granularity: '1d', '1h', ...
    open          NUMERIC(18, 6),
    high          NUMERIC(18, 6),
    low           NUMERIC(18, 6),
    close         NUMERIC(18, 6),
    volume        BIGINT,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, bar_timestamp, interval)
);

-- Training reads: "all 1d bars for AAPL ordered by time" — covered by the PK.
-- Cross-symbol scans by date benefit from the reverse index:
CREATE INDEX idx_price_bars_time ON price_bars (interval, bar_timestamp);

CREATE TABLE corporate_events (
    symbol     TEXT        NOT NULL,
    event_date DATE        NOT NULL,
    event_type TEXT        NOT NULL,             -- 'dividends' | 'splits'
    value      NUMERIC(18, 6) NOT NULL,          -- dividend amount / split ratio
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, event_date, event_type)
);

CREATE TABLE news_items (
    id           TEXT        NOT NULL,           -- provider uuid
    symbol       TEXT        NOT NULL,           -- same story can tag many symbols
    title        TEXT        NOT NULL,
    publisher    TEXT,
    url          TEXT,
    published_at TIMESTAMPTZ,
    summary      TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, symbol)
);

CREATE INDEX idx_news_symbol_time ON news_items (symbol, published_at DESC);

-- Scheduler observability outlives process restarts (replaced
-- data/scheduler_state.json in Sprint 7 — see
-- storage/postgres_store.latest_success_times()).
CREATE TABLE ingestion_runs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      TEXT        NOT NULL,            -- 'ticker:AAPL:1d'
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status      TEXT        NOT NULL,            -- 'success' | 'error' | 'skipped'
    rows_written INTEGER,
    error       TEXT
);

-- Model plane (Sprint 9): one row per prediction. resolved_at/outcome are
-- filled by the Sprint 10 resolver; signals is the only model/world table
-- where UPDATE is permitted (resolution only).
CREATE TABLE signals (
    symbol           TEXT        NOT NULL,
    interval         TEXT        NOT NULL,        -- bar granularity the model ran on
    signal_timestamp TIMESTAMPTZ NOT NULL,        -- the bar the prediction is made AT
    model_version    TEXT        NOT NULL,        -- date + git sha, see model/train.py
    horizon_bars     INTEGER     NOT NULL,
    direction        TEXT        NOT NULL,        -- 'up' | 'down'
    probability      DOUBLE PRECISION NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    outcome          TEXT,                        -- 'win' | 'loss' (NULL = pending)
    PRIMARY KEY (symbol, interval, signal_timestamp, model_version)
);

CREATE INDEX idx_signals_unresolved ON signals (symbol, signal_timestamp)
    WHERE resolved_at IS NULL;

-- The Living World's memory (Sprint 9): append-only — application code has
-- no UPDATE or DELETE path, by design. History (including failures)
-- accumulates forever. See docs/world-memory.md.
CREATE TABLE world_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    event_type  TEXT        NOT NULL,             -- 'volatility_spike' | 'gap_open' | ...
    symbol      TEXT,                             -- NULL for market-wide events
    severity    DOUBLE PRECISION NOT NULL,        -- salience score, higher = more notable
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_world_events_time ON world_events (occurred_at DESC);
CREATE INDEX idx_world_events_type_time ON world_events (event_type, occurred_at DESC);

-- Natural key: the same rule firing for the same symbol at the same instant
-- IS the same event. Makes the Sprint 12 historical backfill re-runnable.
CREATE UNIQUE INDEX uq_world_events_natural
    ON world_events (event_type, occurred_at, symbol) NULLS NOT DISTINCT;
