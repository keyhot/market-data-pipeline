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

Fetched data is cached in memory (TTL via `CACHE_TTL_SECONDS`) and persisted as
timestamped CSVs under `data/raw/`.

## Tests

```bash
pytest
```
