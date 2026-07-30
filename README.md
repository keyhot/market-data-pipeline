# Bitcoin Room

A 24/7 livestream where real Bitcoin and Ethereum market data drives a small, persistent
world. Two inhabitants share a room and watch the candles. When the market moves, the room
notices. When it doesn't, the room doesn't either — you can leave it on the way you'd leave
a fireplace on.

The inhabitant whose job is to predict the market is, right now, losing. Its walk-forward
backtest returned **−39.8%** against **+1%** buy-and-hold, and after costs it loses money.
That number is published deliberately and is not quietly retuned. The goal is a model that
wins; this is version zero, and version zero's record stays in the log forever either way.

## What you're looking at

A persistent room, rendered in PixiJS, with two inhabitants:

- **MODEL** — a LightGBM classifier that issues directional calls. Every prediction is
  written down *before* the outcome is known and resolved afterwards.
- **TRADER** — an independent freqtrade dry-run sidecar that takes its own positions. It is
  not an executor of MODEL's calls, so when the two disagree the disagreement is real. It's
  an opt-in service; when it isn't running, TRADER renders as dormant and the room is still
  complete.

Around them: a candlestick chart, a live signal strip, and a scrolling world-event feed.

The register is a calm baseline that swells when the market does, then decays back. A
director picks scenes and writes commentary from deterministic phrase banks keyed to event
severity — **no LLM, no API cost**. The phrases match the events, the events come from the
data, and the data is real.

## Why the track record is public

Most "AI trading" content is survivorship: the profitable runs get shown and the losing ones
get quietly retrained into something else. You never see the version that lost, so you can't
tell whether anything was actually learned.

Here the opposite is enforced structurally. Predictions are recorded before their outcome and
resolved against realized bars afterwards. Wins and losses both land permanently in
`world_events`, which is **append-only** — application code never updates or deletes a row
([`docs/world-memory.md`](docs/world-memory.md)). Nothing resets between streams.

**The goal is a model that makes money.** The append-only log isn't a commitment to keep
losing — it's what makes getting better mean something. Every version is measured against a
record that can't be quietly edited afterwards, so when a later model does beat costs, the
improvement is a verifiable step from a published starting point rather than a claim. And if
a change makes things worse, that's in the log too.

The room reacts to whatever the record currently says. Right now that means a model in a
losing stretch, and the world is honest about it.

Downtime is treated the same way. When the stream drops, the watchdog writes the outage
into the log rather than erasing it — and it distinguishes *degraded* from *down*, so the
uptime report can't overstate itself.

## Architecture

```
  Binance websocket (1m klines)  +  Yahoo Finance (equities)
                    │
                    ▼
            Postgres  ──►  price_bars, signals, news, corporate events
                    │
                    ▼
            Salience engine        deterministic rules: volatility spikes,
                    │              gaps, streaks, volume anomalies
                    ▼
         world_events (append-only)  ◄── the world's memory
                    │
      ┌─────────────┼──────────────────────┬─────────────────────┐
      ▼             ▼                      ▼                     ▼
  LightGBM     project_state()         Director            Watchdog
  prediction   (pure fold)          scene + phrase       outages → events
      │             │                      │                     │
      └─► resolver ─┘                      ▼                     │
          win/loss                  PixiJS room  /world          │
                                    overlays, charts             │
                                           │                     │
                                           ▼                     │
                                  OBS (driven over websocket) ◄──┘
                                           │
                                           ▼
                                        stream
```

`world_events` is the single source of truth. Everything the viewer sees is a projection
over it — `project_state()` is a pure fold whose determinism and chunk-invariance are
property-tested, so refreshing the page restores the same world rather than a similar one.

**Adding a data provider** means implementing `MarketDataProvider` from
`ingestion/providers.py` and injecting it into the fetchers. Binance slotted in beside
Yahoo Finance without touching the API or storage layers.

## Running it

Copy `.env.example` to `.env` first, then:

```bash
docker compose up -d --build        # Postgres + API + scheduler/ingester
./scripts/smoke_test.sh             # boots the stack and verifies it end to end
```

The API lands on `http://localhost:8000`. Open `/world` for the room, `/charts` for the
candlestick grid, `/dashboard` for the watchlist.

For development outside Docker:

```bash
poetry install
poetry run uvicorn api.main:app --reload
```

Give the room a past — replays the same salience rules over historical klines, flagging
each event `backfilled: true` so learned history stays distinct from witnessed history:

```bash
poetry run python scripts/backfill_world_events.py --days 60
```

### The model

```bash
python -m model.train    --symbol BTCUSDT --interval 1m   # LightGBM baseline
python -m model.backtest --symbol BTCUSDT --interval 1m   # walk-forward, fees + slippage
python -m model.predict  --symbol BTCUSDT --interval 1m   # one-shot signal write
```

The feature pipeline is shared between training and inference and is property-tested for
no-lookahead. The backtest applies a purge gap and real costs, and the test suite includes
an oracle leak detector.

### The stream

```bash
poetry run python scripts/stream_ctl.py build              # scene from SCENE_SPEC, idempotent
poetry run python scripts/stream_ctl.py configure-output   # RTMP from OBS_STREAM_KEY
poetry run python scripts/stream_ctl.py start              # records a stream_started event
poetry run python scripts/stream_watchdog.py               # or the systemd unit
```

Layout constants live in `scripts/stream_scene.py`; every source is a browser source, so
**the pages are the scene**. `scripts/soak_report.py` computes uptime from the recorded
events.

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness, including Postgres connectivity |
| `GET /metrics` | Per-route request counts, latency, status codes, write counters |
| `GET /ticker/{symbol}/{range}` | OHLCV history for one symbol |
| `GET /tickers/{range}?symbols=A,B,C` | Concurrent batch fetch, up to 10 symbols |
| `GET /events/{symbol}/{type}` | Dividends, splits, actions |
| `GET /news/{symbol}` | Latest news |
| `GET /bars/{symbol}` | Stored price bars from Postgres |
| `GET /stream/bars/{symbol}` | SSE: bars stored after connect |
| `GET /stored/events/{symbol}/{type}`, `GET /stored/news/{symbol}` | Stored events and news |
| `GET /world/events` | Stored world events (`limit`, `event_type`, `symbol`, `since`) |
| `GET /world/state` | The log folded into current state — moods, tiers, reactions |
| `GET /stream/world/events` | SSE: world events stored after connect |
| `GET /signals/{symbol}` | Model signals with outcomes and rolling accuracy |
| `GET /world` | The room: PixiJS canvas, SSE-updated |
| `GET /chart/{symbol}`, `GET /charts`, `GET /dashboard` | Candlestick pages and watchlist |
| `GET /overlay/signals`, `GET /overlay/events` | OBS browser sources |

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `POSTGRES_WRITE_ENABLED` | on | Postgres is the source of truth; an uncached fetch fails with 503 if the write fails. Set `0` only for offline dev. |
| `CSV_WRITE_ENABLED` | on | Also snapshot fetches to `data/raw/*.csv`. |
| `SCHEDULER_ENABLED` | off | Background watchlist ingestion; skip-logic reads the `ingestion_runs` table. |
| `WS_INGEST_ENABLED` | off | Binance websocket 1m-kline ingester for crypto symbols. |
| `SALIENCE_ENABLED` | on | Turn the bar stream into world events. |
| `DIRECTOR_ENABLED` | off | Scene switching and commentary. |
| `DIRECTOR_MUTED` | off | Global mute — suppressed lines are counted, never logged. |

Watchlist entries take an optional `market: crypto` (default `equity`). Equity jobs skip
outside Mon–Fri 9:30–16:00 ET; crypto jobs run 24/7. Crypto uses public Binance REST and
websocket endpoints — no API key. Every write is an idempotent upsert, and the ingester
REST-backfills the gap since the last stored bar on each reconnect, so restarts never lose
candles.

## Testing

```bash
poetry run pytest
```

439 tests, written test-first, including property-based tests on the state projection and
mutation checks on the watchdog backoff and the director's rate limits. **No test touches
the network** — providers, the OBS client, and the store are all injected seams. Integration
tests under `tests/integration/` need the compose Postgres and auto-skip without it.

> Use `poetry run pytest`, not bare `pytest` — the latter resolves to the Anaconda base
> environment and is missing dependencies.

## What's not done yet

Being candid:

- **The 24-hour soak has not run.** The stream has gone live and the watchdog has recovered
  real failures, but the system has never been left unattended for a full day. Every uptime
  claim here is a design property, not a measured record.
- **The stream is silent.** Piper TTS is wired in behind an injected runner and degrades to
  silence when the binary is absent — which it currently is. No music bed either.
- **There is no clip pipeline.** The world produces small moments worth cutting — a streak
  resolving, a volatility spike decaying — and nothing turns them into shorts.
- **The model doesn't win yet.** In walk-forward backtest, version zero called direction
  correctly 55.6% of the time and still returned −39.8%: round-trip costs of roughly 0.22%
  exceed the per-trade edge at 15-bar holds on 1-minute data. The live resolved record has
  so far run below the backtest. Trading less often — a higher threshold, a longer horizon,
  or a coarser interval — is the first lever; a model that clears its own fees is on the
  roadmap, and the starting point is published so the progress is checkable.

## Where the design docs live

Planning, design records, and spikes live in an Obsidian vault outside this repo. Three
code-coupled docs stay here: [`docs/world-memory.md`](docs/world-memory.md) (the append-only
contract), [`docs/postgres-schema-spike.md`](docs/postgres-schema-spike.md), and
[`docs/freqai-takeaways.md`](docs/freqai-takeaways.md).
