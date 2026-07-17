# Market Data Pipeline

**Automated market data ingestion, processing, and visualization platform.**  
Designed with modular architecture to allow future microservices and streaming integration.

## Quickstart

```bash
poetry install
uvicorn api.main:app --reload
```

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness check |
| `GET /metrics` | Per-route request counts, latency, and status codes |
| `GET /ticker/{symbol}/{range}` | Fetch OHLCV history for one symbol (e.g. `AAPL/5d`) |
| `GET /tickers/{range}?symbols=A,B,C` | Concurrent batch fetch for up to 10 symbols |
| `GET /events/{symbol}/{type}` | Dividends, splits, or actions, with optional `start`/`end` filters |
| `GET /news/{symbol}` | Latest news with `limit` and `since` filters |
| `GET /bars/{symbol}` | Stored price bars from Postgres, oldest first (`interval`, `limit` params) |
| `GET /stored/events/{symbol}/{type}` | Stored corporate events from Postgres (`limit` 1-1000; `actions` returns all types) |
| `GET /stored/news/{symbol}` | Stored news from Postgres (`limit` 1-100) |
| `GET /chart/{symbol}` | HTML candlestick page for one symbol |
| `GET /dashboard` | HTML watchlist table with latest closes, linking to each chart |

Fetched data is cached in memory (TTL via `CACHE_TTL_SECONDS`) and persisted as
timestamped CSVs under `data/raw/`.

## Continuous ingestion

Set `SCHEDULER_ENABLED=true` to run background fetches for everything in
`config/watchlist.yaml` on an interval. Skip-logic seeds last-success times
from the `ingestion_runs` table in Postgres, so restarts don't re-fetch fresh
data. Scheduler health shows up on `/health` and `/metrics`.

## Postgres (L2 storage — primary as of Sprint 7)

`docker compose up -d` starts a local Postgres 16 (copy `.env.example` to
`.env` first) and applies `db/init.sql` on first boot. The schema design
lives in `docs/postgres-schema-spike.md`.

Writes go through the idempotent upserts in `storage/postgres_store.py`
(connection pool in `storage/db.py`, configured via `DATABASE_URL`). Replay
the existing CSV snapshots into Postgres with:

```bash
python scripts/backfill_postgres.py
```

The script is rerunnable — a second run changes no row counts. Before
flipping storage defaults in a new environment, verify the two stores agree:

```bash
python scripts/check_parity.py
```

Postgres is now the source of truth: every uncached fetch (API endpoints and
scheduler jobs) writes to Postgres first, and a write failure raises a 503
(`StorageWriteError`) rather than being swallowed. CSV snapshots are a
separate, independently toggleable copy. `/health` reports Postgres
connectivity, `/metrics` exposes write counts (`postgres_writes`), scheduler
runs are recorded in the `ingestion_runs` table, and `GET /bars/{symbol}`
serves the stored bars.

| Env var | Default | Meaning |
| --- | --- | --- |
| `POSTGRES_WRITE_ENABLED` | on | Postgres is the source of truth; uncached fetches fail with 503 if the write fails. Set to `0` only for offline dev. |
| `CSV_WRITE_ENABLED` | on | Also snapshot fetches to `data/raw/*.csv`. Set to `0` to run Postgres-only. |
| `SCHEDULER_ENABLED` | off | Background watchlist ingestion. Skip-logic now reads the `ingestion_runs` table (the old `data/scheduler_state.json` is gone). |

## First charts (L3)

`GET /chart/{symbol}` renders a candlestick page for one symbol and
`GET /dashboard` lists the watchlist with latest closes, linking to each
chart. Both are server-rendered HTML from `api/templates/`, fed by the
existing `/bars/{symbol}` JSON endpoint. Stack choice and constraints (CDN
script, SRI-pinned version, attribution requirement) are in
`docs/charting-stack-decision.md`.

## Tests

```bash
pytest
```

Integration tests in `tests/integration/` need the docker-compose Postgres
and auto-skip when it's unreachable.
