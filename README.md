# Market Data Pipeline

**Automated market data ingestion, processing, and visualization platform.**  
Designed with modular architecture to allow future microservices and streaming integration.

## Quickstart

One command boots the whole stack — Postgres, API, and the scheduler/ingester
(copy `.env.example` to `.env` first):

```bash
docker compose up -d --build
```

The API lands on `http://localhost:8000` (see `API_PORT`), the scheduler
container ingests the watchlist, and the websocket ingester streams live
crypto 1m bars into Postgres. `./scripts/smoke_test.sh` boots and verifies
the stack end to end.

For local development outside Docker:

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
| `GET /stream/bars/{symbol}` | Server-Sent Events: bars stored after connect (`interval`, `poll_seconds`) |
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

Watchlist entries take an optional `market: crypto` (default `equity`).
Equity jobs skip outside regular US market hours (Mon–Fri 9:30–16:00 ET);
crypto jobs run 24/7 against Binance.

## Live crypto data (Sprint 8)

Crypto symbols are served by `ingestion/binance_provider.py` (public Binance
REST, no API key) behind the same `MarketDataProvider` abstraction. With
`WS_INGEST_ENABLED=true`, `ingestion/binance_ws.py` streams Binance 1m klines
for the watchlist's crypto symbols and writes each **closed** candle into
Postgres (`interval='1m'`). On every (re)connect it REST-backfills the gap
since the last stored 1m bar, so restarts never lose candles — all writes are
idempotent upserts. TimescaleDB was evaluated and deferred with explicit
adoption triggers (`docs/timescale-spike.md`).

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
| `WS_INGEST_ENABLED` | off | Binance websocket 1m-kline ingester for the watchlist's crypto symbols. |

## First charts (L3)

`GET /chart/{symbol}` renders a candlestick page for one symbol and
`GET /dashboard` lists the watchlist with latest closes, linking to each
chart. Both are server-rendered HTML from `api/templates/`, fed by the
existing `/bars/{symbol}` JSON endpoint. Charts update live: the page
subscribes to `GET /stream/bars/{symbol}` (SSE) and appends bars as they are
stored — try `GET /chart/BTCUSDT?interval=1m` with the websocket ingester
running. Stack choice and constraints (CDN script, SRI-pinned version,
attribution requirement) are in `docs/charting-stack-decision.md`.

## Model plane (Sprint 9)

The first trade predictor: shared feature pipeline (`model/features.py`,
no-lookahead guaranteed by tests), LightGBM direction baseline
(`python -m model.train --symbol BTCUSDT --interval 1m`), walk-forward
backtest with fees + slippage (`python -m model.backtest ...`), and one-shot
inference writing idempotent rows to the `signals` table
(`python -m model.predict ...`). Design rules and the honest (currently
losing) backtest numbers: `docs/model-plane.md` and
`docs/freqai-takeaways.md`.

## World memory (Sprint 9)

The Living World's memory started recording: deterministic salience rules
(`world/salience.py` — volatility spikes, gaps, streaks, volume anomalies)
turn the live bar stream into append-only `world_events` rows via scheduler
jobs (flag `SALIENCE_ENABLED`, default on). Nothing ever updates or deletes
a world event — see `docs/world-memory.md`. Nightly backups:
`scripts/backup_postgres.sh` (cron 03:10, 14-day retention, restore drill in
the script header).

## Tests

```bash
pytest
```

Integration tests in `tests/integration/` need the docker-compose Postgres
and auto-skip when it's unreachable.
