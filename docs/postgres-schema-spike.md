# Postgres Schema Spike — L2 Storage Layer

Design for replacing CSV persistence with Postgres. Model training is a
first-class consumer: the same bar must appear exactly once no matter how many
times it was fetched, so every write path is an idempotent upsert.

## Design principles

1. **One row per `(symbol, bar_timestamp, interval)`** in a single unified
   time-series table. No per-symbol or per-range tables — training reads want
   one query surface.
2. **Idempotent upserts everywhere.** Re-fetching a range must be a no-op for
   unchanged bars (`ON CONFLICT ... DO UPDATE`), never a duplicate.
3. **`interval` is bar granularity** (`1d`, `1h`, ...), not the fetch range.
   The API's `TimeRange` (`5d`, `1mo`, ...) describes how much history was
   requested; all of today's fetches produce `1d` bars. The fetch range stays
   in the ingestion log, not in the bar key.
4. **Raw CSVs remain the landing zone** until the migration completes; a
   backfill script replays `data/raw/` into Postgres using the same upserts.

## Tables

```sql
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

-- Scheduler observability outlives process restarts (replaces
-- data/scheduler_state.json once L2 lands).
CREATE TABLE ingestion_runs (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      TEXT        NOT NULL,            -- 'ticker:AAPL:1d'
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status      TEXT        NOT NULL,            -- 'success' | 'error' | 'skipped'
    rows_written INTEGER,
    error       TEXT
);
```

## Upsert pattern

```sql
INSERT INTO price_bars (symbol, bar_timestamp, interval, open, high, low, close, volume)
VALUES (%(symbol)s, %(ts)s, %(interval)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s)
ON CONFLICT (symbol, bar_timestamp, interval) DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    fetched_at = now();
```

`DO UPDATE` (not `DO NOTHING`) because Yahoo revises recent bars — the latest
fetch wins, and the row count still can't grow.

## Decisions and deferred questions

| Decision | Choice | Why |
|---|---|---|
| ORM vs raw SQL | psycopg + raw SQL | Small schema, upsert-heavy; ORM adds little |
| TimescaleDB | Deferred | Plain Postgres is fine below ~10M rows; revisit when intraday bars land |
| Partitioning | Deferred | Same threshold as above |
| Adjusted prices | Store as fetched (yfinance auto-adjusts) | Revisit if training needs raw + adjusted |

## Migration plan

1. `docker compose up -d` (this spike) → schema applied via an init script.
2. Implement a `PostgresStore` alongside the CSV writer (storage layer already
   abstracts writes; providers/fetchers untouched).
3. Backfill script replays `data/raw/*.csv` through the upserts.
4. Dual-write CSV + Postgres for a sprint, then drop CSV writes.
