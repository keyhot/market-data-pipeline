# Sprint 8 — Live Data & Always-On Ops

Goal: the first **ready product** — 24/7 crypto data (Binance REST + websocket
into Postgres), live-updating charts, and the whole stack booting with one
`docker compose up`. Notion sprint page `3a089097-00af-816a-a8b7-d72c28be9809`.

Decisions (agreed 2026-07-18):
- **Direct `httpx` + `websockets`** against Binance public API — no ccxt. The
  `MarketDataProvider` seam contains a future swap; we must own the
  reconnect/gap-backfill logic either way.
- **SSE + DB polling** for live charts — works across containers (ingester and
  API are separate processes), EventSource auto-reconnects, zero new infra.

## Tickets → implementation

1. **Binance REST provider** — `ingestion/binance_provider.py`:
   `BinanceProvider(MarketDataProvider)` over `GET /api/v3/klines` (public, no
   key). `TimeRange` → interval/limit mapping; returns a yfinance-shaped
   DataFrame (Open/High/Low/Close/Volume, UTC index) so `write_price_bars`
   works unchanged. `get_events` → `UnsupportedEventTypeError`.
2. **Crypto watchlist + market hours** — `market: crypto|equity` (default
   equity) on watchlist entries; BTCUSDT/ETHUSDT added.
   `scheduler/market_hours.py` (`zoneinfo` America/New_York, Mon–Fri
   9:30–16:00); equity jobs skip with a log line when closed; crypto jobs
   route to `ingestion/factory.get_crypto_provider()`.
3. **Websocket kline ingester** — `ingestion/binance_ws.py`: combined
   `@kline_1m` streams for watchlist crypto symbols; closed candles only
   (`k.x == true`) → `write_price_bars(symbol, df, interval="1m")` (interval
   param threaded through `storage/writes.py`, default `1d` keeps existing
   callers unchanged). Exponential-backoff reconnect; REST gap backfill since
   last stored 1m bar. Runs in FastAPI lifespan behind `WS_INGEST_ENABLED`.
4. **SSE live chart push** — `GET /stream/bars/{symbol}` polling Postgres
   (~3s) for bars newer than last sent; `chart.html` opens an `EventSource`
   and `series.update()`s; `/bars/{symbol}` gains `?interval=`.
5. **TimescaleDB spike** — `docs/timescale-spike.md`, adopt-or-defer with the
   adoption trigger written down.
6. **Dockerize** — `Dockerfile` (python:3.11-slim); compose services `api` and
   `scheduler` (same image, flags differ), `restart: unless-stopped`,
   healthchecks, `depends_on: postgres: service_healthy`.
7. **One-command boot** — `docker compose up -d` from a clean volume brings up
   db + api + scheduler; `.env.example` wired.
8. **E2E smoke test** — `scripts/smoke_test.sh`: boot → `/health` green →
   a stored bar exists → `/bars` + `/chart` serve it → down.
9. **Tests** — Binance provider (mocked transport), ingester (fake kline
   messages, reconnect/backfill), market hours, watchlist parsing, SSE first
   event.
10. **Docs** — README flags/endpoints/boot, architecture-vision data-plane
    status, CLAUDE.md (local).

## Verification

Full suite + ruff clean; clean-volume `docker compose up -d` → BTCUSDT 1m bars
within ~2 min → `/chart/BTCUSDT` ticks live; `docker compose restart
scheduler` → gap backfilled; equity jobs skip outside market hours.
