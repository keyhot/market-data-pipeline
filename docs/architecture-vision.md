# Architecture Vision — Final Product

Target: a 24/7 autonomous market-data livestream plus an AI model that predicts
trades, built solo, layer by layer. Five planes, each independently useful.

```
┌─ Data plane ──────────┐   ┌─ Model plane ────────────┐
│ providers → scheduler │──▶│ features → training →     │
│ → Postgres (+ ticks)  │   │ backtest → signals table  │
└──────────┬────────────┘   └────────────┬─────────────┘
           ▼                             ▼
┌─ Presentation plane ─────────────────────────────────┐
│ dashboards + stream overlays (browser pages)         │
└──────────┬───────────────────────────────────────────┘
           ▼
┌─ Broadcast plane ────────────┐   ┌─ Ops plane ───────┐
│ director service → OBS →     │   │ Docker, healthchecks│
│ YouTube/Twitch RTMP          │   │ monitoring, restarts│
└──────────────────────────────┘   └───────────────────┘
```

## Data plane (L1 + L2 — current focus)

- What exists: FastAPI + provider abstraction + APScheduler watchlist ingester,
  CSV landing zone, Postgres migration planned (`postgres-schema-spike.md`).
- Real-time upgrade (later): swap yfinance for a websocket source via the
  existing `MarketDataProvider` abstraction. Candidates: Polygon.io, Alpaca
  Market Data, Finnhub, Databento.
- 24/7 constraint: equity markets close nights/weekends — crypto websockets
  (Binance/Coinbase) are the free, truly-24/7 filler.
- When intraday ticks land: add the TimescaleDB extension (hypertables,
  compression, continuous aggregates). Keep APScheduler; no Kafka/Celery at
  solo scale.

## Model plane (trade predictor)

- Features from Postgres via pandas/Polars; baselines with LightGBM/XGBoost
  (direction probability), PyTorch sequence models only after a boosted-tree
  baseline exists.
- Backtesting is the real product: vectorbt or a custom walk-forward harness,
  always modeling fees and slippage. Experiments tracked in MLflow.
- Serving = a `signals` table in Postgres the model writes into; API and
  stream just read it. No dedicated ML serving infra.
- Paper trading via Alpaca's paper API before any real money. Frame the model
  as a publicly accountable experiment (live P&L is stream content).

## Presentation plane (L3)

- Everything is a web page: small React/Next.js (or vanilla JS) frontend fed
  by FastAPI over WebSockets/SSE.
- Charts: TradingView Lightweight Charts (free, canvas, terminal aesthetic).
- Internal monitoring: Grafana straight on Postgres.
- Stream overlays are the same pages, loaded as OBS Browser Sources.

## Broadcast plane (L4)

- OBS Studio + obs-websocket v5 (`obsws-python`) for autonomous scene/source
  control; RTMP out to YouTube/Twitch. 24/7 encoding wants NVENC/QuickSync.
- Director service: Python process watching data (price spikes, news, model
  signals) and driving what the stream shows. LLM-generated market commentary
  (Claude API) spoken via TTS (ElevenLabs, or local Piper for free).
- Character layer: VTube Studio + Live2D, or an audio-reactive PixiJS/Three.js
  character in a browser source (simpler to automate).
- Music: DMCA-safe only (StreamBeats, YouTube Audio Library, generated) —
  legal trap, not a technical one.

## Ops plane

- One box (home server / small VPS; the streaming machine needs the GPU
  encoder). Docker Compose: postgres, api, scheduler, director, frontend.
- Restart policies + `/health` for resilience; Uptime Kuma for alerting.
- Switch `/metrics` JSON → Prometheus format only when Grafana is adopted.

## Sequencing

1. Postgres migration (Sprint 6)
2. Intraday / crypto data source
3. Model v0: baseline + backtest + paper trading
4. Overlay pages (L3)
5. Stream MVP — one scene (chart + ticker + music), uptime boring first
6. Director, commentary, character — content upgrades

After step 2, the model track and the stream track are independent and can be
interleaved.
