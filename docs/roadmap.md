# Roadmap — from data pipeline to Living World

Written 2026-07-18 (during Sprint 8). Companion to `architecture-vision.md`:
that doc says *what* the planes are; this one says *when* and *why*, and what
lies beyond the committed path. Revisit at each sprint planning.

## Where we are

| Layer | Status |
|---|---|
| L1 Ingestion (API, providers, scheduler, news) | Done — Sprints 1–5 |
| L2 Storage (Postgres source of truth, mandatory writes) | Done — Sprints 6–7 |
| L3 Visualization (chart + dashboard pages) | Started — Sprint 7 |
| L4 Living World (stream, characters, world engine) | Vision defined 2026-07-18 |

Sprint 8 (in flight, 2026-07-18 → 07-25) delivers the **first "ready product"**:
24/7 crypto data via Binance websocket, live-updating charts, one-command
`docker compose up` boot. Everything after runs on that always-on foundation.

The through-line: **truthful data → salient events → a world that remembers.**
The salience engine and the world event log are the project's real IP; charts
and dashboards are commodity.

## Committed path (Sprints 9–14, ~Aug–Oct 2026)

One-week sprints, solo. Each sprint ships something independently useful, so
the project is never more than a week from a demoable state.

### Sprint 9 — Model v0 + World Memory
- LightGBM direction-probability baseline; features from Postgres bars;
  walk-forward backtest with fees/slippage. Study FreqAI's design first
  (never copy code — GPL); MLflow if experiment count justifies it.
- `signals` table: the model writes, everyone else reads.
- **Salience engine v0**: deterministic rules (volatility z-scores, gap opens,
  streaks, volume anomalies) over the live bar stream.
- **`world_events` log**: append-only table, recording from day one — the
  world's memory starts accumulating months before the world renders.

### Sprint 10 — Accountability + Live Overlays
- Prediction-vs-outcome resolution: every signal resolves into a world event
  (right or wrong — "failure is content" starts here, automatically).
- Paper trading: Alpaca paper API for equities; evaluate freqtrade dry-run as
  a sidecar container for crypto (its real trades = a future world inhabitant).
- Overlay-grade pages: live P&L strip, signal ticker, event feed — the same
  pages OBS will load later.

### Sprint 11 — Stream MVP
- OBS + obs-websocket + RTMP to YouTube/Twitch; one scene, boring and
  reliable. Uptime is the entire feature. NVENC/QuickSync encoder.
- Existing chart/overlay pages as Browser Sources. DMCA-safe audio bed.

### Sprint 12 — World Renderer v0
- One room, one character, a handful of world-state variables — a PixiJS (or
  Phaser) canvas page driven by `world_events` over WebSocket, loaded as a
  Browser Source. World state = projection over the event log.
- First personality: one policy over the salience stream, with visible
  reactions. The world already has ~2 months of history to display.

### Sprint 13 — Director & Personalities
- Director service: salience-driven scene switching, in-character LLM
  commentary (Claude API) + TTS. The salience engine doubles as the LLM cost
  gate — commentary only on events worth talking about.
- 2–3 personalities as *policies with different thresholds* over the same
  event stream. Stretch: ground each character in a genuinely different
  strategy (momentum vs mean-reversion) so on-screen arguments are real model
  disagreement — truthful by construction.

### Sprint 14+ — Emergence & Progression
- Small interacting systems (weather-from-volatility, office economy from
  P&L, achievements, locked/unlocked rooms) instead of scripted reactions.
- Progression mechanics: earned, environmental, slow. The month-away test
  becomes the acceptance criterion for every feature here.

## Expansion tracks (unscheduled — pull in when the base is stable)

**Audience interactivity.** Twitch/YouTube chat as a world *input* — naming
characters, votes, chat-sentiment as ambient weather. Hard rule: chat can
touch presentation, never analytics.

**Data breadth.** Each new source is new salience rules, i.e. new content:
macro calendar (FRED), options flow, orderbook depth, on-chain metrics. Add a
source only when the world can *react* to it.

**Model depth.** Personality-grounded model ensemble (each character backed by
a real, different strategy with its own public track record). PyTorch sequence
models only after boosted trees are beaten honestly.

**Derived content.** The `world_events` log doubles as an editorial source:
auto-generated daily recaps ("what happened in the office today"), clip
markers at high-salience moments, a newsletter written from the log. Cheap to
build once the log is rich.

**Public surface.** Read-only API / embeddable widgets from the same Postgres;
the stream is the marketing for the data product, not the other way around.

**Infra evolution.** TimescaleDB when intraday volume demands it; Prometheus +
Grafana when `/metrics` JSON stops being enough; VPS + GPU stream box when
leaving the home server. All deferred until they hurt.

## Risks & standing decisions

- **Solo bandwidth vs game-dev scope.** Mitigation: ruthless v0 slices, one
  new system per sprint, emergence over authored content.
- **World memory is precious.** Once `world_events` accumulates, losing it
  kills the product's soul. Nightly `pg_dump` from Sprint 9, offsite copy
  before Sprint 12.
- **Data licensing.** yfinance is unofficial — fine for a prototype, not for a
  public product. Binance covers crypto legitimately; a paid equities feed
  (Polygon/Alpaca/Databento) is a known future cost. Revisit before the
  stream goes public.
- **LLM/TTS cost.** Salience-gated; local Piper TTS as the free fallback.
- **GPL boundary.** freqtrade only ever as a separate service; ccxt (MIT) is
  the safe dependency.
- **Truthfulness invariant.** Every expansion is tested against one rule:
  visual exaggeration welcome, fabricated data never.

## Success metrics by phase

- **Now (S8):** stack uptime; bar freshness lag (websocket → Postgres → page).
- **S9–10:** backtest honesty (fees modeled, no lookahead); % of signals
  resolved into world events; world_events/day.
- **S11–13:** stream uptime; cost/day; salient-events surfaced vs missed.
- **S14+:** the month-away test — does a returning viewer immediately see
  that the world moved on? Viewer questions in chat ("why is the office
  empty?") are the KPI curiosity was designed for.
