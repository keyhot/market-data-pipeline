# Architecture Vision — Final Product

Target: a 24/7 autonomous livestream where truthful market data drives a
persistent, evolving **living world** — plus an AI model that predicts trades.
Built solo, layer by layer. Five planes, each independently useful.

## Living World (north star for L3/L4)

The stream should not feel like a dashboard; it should feel like a living world
whose inhabitants react to the market. Data stays truthful — presentation
entertains. The principles (agreed 2026-07-18):

- **The market is invisible; its consequences are visible.** Show how the world
  reacts before showing numbers. Viewers should read the mood before the stats.
- **The world remembers.** Nothing resets between streams. Characters, rooms,
  achievements, and mistakes accumulate; the environment tells the system's
  history without narration.
- **Data creates events, not updates.** Don't announce every price change.
  Detect moments worth attention — ask "why is this moment interesting?" before
  presenting it.
- **Every component has personality.** Different systems react differently
  (optimistic, cautious, statistical, emotional, procedural). Personality
  emerges from consistent reactions, not scripted jokes.
- **Failure is content.** Wrong predictions and losing trades are never hidden
  — they become world history. A system that visibly learns beats one
  pretending to be perfect.
- **Visual storytelling before narration.** Animation, environment, lighting,
  objects first; narration only for what can't be inferred.
- **Persistent progression.** The world slowly evolves; growth is earned and
  environmental, not just numerical.
- **Build curiosity.** The best moments make viewers ask questions ("why is
  everyone celebrating?", "why is that room locked?").
- **Allow emergence.** Small systems that interact beat scripted reactions;
  simultaneous events should combine in unexpected ways.
- **Entertainment never replaces truth.** Characters may misread, panic, or
  celebrate too early — the underlying analytics stay accurate.

Success test: someone opens the stream after a month away and immediately sees
the office changed, characters have new habits, old mistakes are still visible,
and today's market already altered the environment before any chart appears.

Architecturally this adds two components (both live in the broadcast plane's
director, backed by Postgres):

- **Salience engine** — turns the bar/news/signal stream into discrete world
  events. Starts as deterministic rules (volatility z-scores, gap opens,
  streaks, volume anomalies, prediction-vs-outcome resolution); no LLM needed
  for v0. Personalities are policies over this event stream (different
  thresholds → different characters).
- **World event log** — append-only `world_events` table; current world state
  is a projection over it. Immutable by design, so history (including failures)
  accumulates for free. Should start recording *before* the world renders, so
  the world is born with a past.

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

## Data plane (L1 done, L2 done — Sprint 7; live crypto — Sprint 8)

- What exists: FastAPI + provider abstraction + APScheduler watchlist ingester;
  Postgres is now the primary store (mandatory writes, CSV snapshots optional
  — see `postgres-schema-spike.md` for the schema and the cutover rationale).
- Real-time (Sprint 8, done for crypto): `BinanceProvider` (public REST) plus
  a websocket 1m-kline ingester (`ingestion/binance_ws.py`) with reconnect
  gap-backfill; equity jobs are market-hours aware. A paid equities websocket
  (Polygon.io, Alpaca, Finnhub, Databento) remains a later swap via the same
  `MarketDataProvider` abstraction.
- 24/7 constraint: equity markets close nights/weekends — crypto websockets
  (Binance) are the free, truly-24/7 filler. Live since Sprint 8.
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

## Presentation plane (L3 — started)

- Started Sprint 7: `GET /chart/{symbol}` and `GET /dashboard` are
  server-rendered pages fed by the stored bars (`docs/charting-stack-decision.md`).
  Everything beyond that is still a web page: small React/Next.js (or vanilla
  JS) frontend fed by FastAPI over WebSockets/SSE.
- Charts: TradingView Lightweight Charts (free, canvas, terminal aesthetic).
- Internal monitoring: Grafana straight on Postgres.
- Stream overlays are the same pages, loaded as OBS Browser Sources — and the
  living-world renderer is just another such page (PixiJS/Phaser canvas
  driven by the world event stream over WebSocket). Charts become objects
  *inside* the world rather than the product.

## Broadcast plane (L4)

- OBS Studio + obs-websocket v5 (`obsws-python`) for autonomous scene/source
  control; RTMP out to YouTube/Twitch. 24/7 encoding wants NVENC/QuickSync.
- Director service: Python process housing the salience engine and world
  engine (see Living World above) — watches bars, news, and model signals,
  appends world events, and drives what the stream shows. LLM-generated
  commentary (Claude API) spoken via TTS (ElevenLabs, or local Piper for
  free) — voiced *in character*, per personality.
- Characters: personalities as policies over the world event stream (same
  events, different thresholds and reactions). Rendered in the world page
  (PixiJS/Three.js browser source; VTube Studio + Live2D an alternative for a
  single host character).
- The model's public track record is a character trait: wrong calls resolve
  into visible world events, not hidden metrics ("failure is content").
- Music: DMCA-safe only (StreamBeats, YouTube Audio Library, generated) —
  legal trap, not a technical one.

## Ops plane

- One box (home server / small VPS; the streaming machine needs the GPU
  encoder). Docker Compose: postgres, api, scheduler, director, frontend.
- Restart policies + `/health` for resilience; Uptime Kuma for alerting.
- Switch `/metrics` JSON → Prometheus format only when Grafana is adopted.

## Sequencing

1. Postgres migration (Sprint 6) — completed Sprint 7 (Postgres primary, mandatory writes)
2. Intraday / crypto data source — Sprint 8 (Binance + websocket, always-on stack)
3. Model v0: baseline + backtest + paper trading
   — plus salience rules v0 + `world_events` log (start the world's memory
   before the world renders)
4. Overlay pages (L3) — started Sprint 7 (`/chart/{symbol}`, `/dashboard`)
5. Stream MVP — one scene, uptime boring first
6. World renderer v0 + first personality — one room, one character, a handful
   of world-state variables; then grow by adding small interacting systems

After step 2, the model track and the stream track are independent and can be
interleaved. The world event log (step 3) is deliberately early: history only
accumulates in real time.
