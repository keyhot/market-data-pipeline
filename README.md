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

Fetched data is cached in memory (TTL via `CACHE_TTL_SECONDS`) and persisted as
timestamped CSVs under `data/raw/`.

## Continuous ingestion

Set `SCHEDULER_ENABLED=true` to run background fetches for everything in
`config/watchlist.yaml` on an interval. Last-run state is persisted to
`data/scheduler_state.json`, so restarts don't re-fetch fresh data. Scheduler
health shows up on `/health` and `/metrics`.

## Postgres (upcoming L2 storage)

`docker compose up -d` starts a local Postgres 16 (copy `.env.example` to
`.env` first). The schema design lives in `docs/postgres-schema-spike.md`.

## Tests

```bash
pytest
```
