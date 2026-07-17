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
