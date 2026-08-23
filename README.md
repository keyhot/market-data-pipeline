# Bitcoin Room

A 24/7 livestream where real Bitcoin and Ethereum market data drives a small, persistent
world. Two inhabitants share a room and watch the candles. When the market moves, the room
notices. When it doesn't, the room doesn't either — you can leave it on the way you'd leave
a fireplace on.

The inhabitant whose job is to predict the market is, right now, losing. Its walk-forward
backtest returned **−63.3%** against **+18.4%** buy-and-hold, and after costs it loses money.
That number is published deliberately and is not quietly retuned — it got *worse* when the
harness's own trade accounting was fixed (KI-001/KI-040, 2026-08-23), and the worse number
replaced the flattering one. The goal is a model that
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

The first unattended 24-hour soak (2026-08-21 → 08-22) measured **99.96% uptime** — 38
seconds down — with the public broadcast live for all 86,400 seconds. The report counts
**6 outages, of which 5 were zero-duration notices**, and **4 ingest reconnects, of which
1 was the watchdog's own relaunch**. Those two artifacts are named in the report (KI-036,
KI-035) rather than rounded into a nicer number.

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

# is the probability worth gating on? (walk-forward OOS, cached per artifact)
poetry run python scripts/confidence_report.py --symbol BTCUSDT [--refresh]
```

The feature pipeline is shared between training and inference and is property-tested for
no-lookahead. The backtest applies a purge gap and real costs, and the test suite includes
an oracle leak detector.

#### Reading the numbers

Every figure in this README is one of these. They're defined here so the honest ones stay
legible rather than impressive.

| Term | What it means here |
|---|---|
| **bps** | Basis point — one hundredth of a percent. 22 bps = 0.22%. Small moves are easier to compare in bps than in decimals. |
| **round trip** | The total cost of opening *and* closing one position: 0.1% Binance taker fee + 1 bp slippage, both sides = **22 bps**. Paid whether the call was right or wrong. |
| **position** | One entry and one exit. *Not* one bar: a signal that stays on for 20 consecutive minutes is one position, not twenty. Charging the round trip per bar instead was a real bug here (KI-040), and merging holds that overlap into the single position one unit of capital actually held was its sibling (KI-001). |
| **horizon** | How far ahead the model is asked to predict — currently 15 bars, i.e. 15 minutes. The label is "is the close 15 bars from now higher than the close now?" |
| **hit rate** | Share of positions where the direction was right. **49.9%** here. On its own it says almost nothing — see the next row. |
| **edge** | Average profit per position *after* costs: **−19.2 bps**. This is the number that matters. A hit rate can sit at 50% while the edge is deeply negative, because being right by 6 bps and wrong by 6 bps both pay the same 22 bps toll. |
| **walk-forward / fold** | Train on a window, predict the window immediately after it, slide forward, repeat — 253 times here. It's the honest alternative to training and testing on the same period, because the model only ever sees the past. |
| **purge gap** | Rows skipped between a train window and its test window. Without it the last training labels — which look 15 bars ahead — would peek into the test set. |
| **buy-and-hold** | The do-nothing benchmark: buy at the start, sell at the end, pay the toll once. **+18.4%**. Any strategy has to beat this, not merely make money. |
| **exposure** (time in market) | Fraction of the bars over which capital was actually committed — a position is held from entry until `horizon` bars after its signal drops. The published BTC run is **82.0%** in the market. That reframes it: this is not a selective strategy, it's a near-permanent long that keeps paying to re-enter a position it barely leaves. |
| **exposure-matched null** | What a strategy with **no forecasting skill at all** would have returned at the same exposure and the same turnover: each fold's benchmark return scaled by that fold's time in market, charged one round trip per position, compounded across folds. This is what `run_backtest` reports as `null_total_return`. Beating buy-and-hold is not the bar; beating this is. An earlier *linear* form — total benchmark return scaled by overall exposure — reproduced the −63.3% headline to 0.0 points on the 36-day window it was written for, but overstates an 87%-exposed path by ~3.5x once the benchmark compounds 10x; it is superseded, and the correction is in the vault plan. |
| **calibration** | Whether a stated probability matches reality — of all the bars where the model says 0.9, do ~90% go up? Here they don't; realized frequency is flat near 0.50 at every stated probability, so `p` is a score, not a probability. |
| **Brier skill score** | Calibration in one number, against the baseline of always predicting the base rate. Positive = better than that baseline. **−0.35** = worse. |
| **ROC AUC** | Ranking quality, ignoring calibration: the chance a randomly chosen up-bar is scored above a randomly chosen down-bar. 0.5 is a coin flip. Pooled: **0.4965**. |
| **drawdown** | Worst peak-to-trough fall of the equity curve — how bad it got on the way, not just where it ended. |

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
| `GET /standby` | The procedural card the watchdog cuts to on a real drop — makes no network requests, by design and by test |

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

798 tests, written test-first, including property-based tests on the state projection and
mutation checks on the watchdog backoff and the director's rate limits. **No test touches
the network** — providers, the OBS client, and the store are all injected seams. Integration
tests under `tests/integration/` need the compose Postgres and auto-skip without it.

> Use `poetry run pytest`, not bare `pytest` — the latter resolves to the Anaconda base
> environment and is missing dependencies.

## What's not done yet

Being candid:

- **The stream is silent.** Piper TTS is wired in behind an injected runner and degrades to
  silence when the binary is absent — which it currently is. No music bed either.
- **There is no clip pipeline.** The world produces small moments worth cutting — a streak
  resolving, a volatility spike decaying — and nothing turns them into shorts.
- **The model doesn't win yet.** The walk-forward backtest (253 folds, 2026-07-18 →
  2026-08-22) took **519 positions**, called direction correctly on **49.9%** of them, and
  returned **−63.3%** against +18.4% buy-and-hold. ETHUSDT replicates: 46.8% over 485
  positions, −56.6% against +28.6%. The average position earns **−19.2 bps** against a
  22 bps round trip.
  A separate, wider sweep — `scripts/confidence_report.py`, 251 folds and **50,200
  out-of-sample predictions per symbol** — says there is nothing to select from either:
  pooled ROC AUC **0.4965** (BTC) / 0.5123 (ETH), and gating on p > 0.8 buys +0.15 bps of
  selection value against that same 22 bps toll. The binding constraint is arithmetic, not
  tuning: the median 15-bar move is **6.5 bps** and the round trip costs 22.
  And on that window the loss is **turnover, not forecasting**: a null with no skill in it
  reproduces −63.3% to 0.0 points (ETH: 0.7), because 519 round trips at 22 bps compound to
  a −68.1% drag on a strategy that was long 82% of the time.
  `run_backtest` now reports `time_in_market`, a per-fold exposure-matched `null_return`,
  and `excess_vs_null`, so the strategy is scored against *its own* exposure and turnover
  instead of against a 100%-invested benchmark. Measured that way over 6.6 years of 15m
  bars (1,159 folds, three down years), the strategy still loses to buy-and-hold by two
  orders of magnitude at every exposure setting, and the share of folds beating their own
  null falls through chance — 613 → 571 → 548 of 1,159 — as capital is forced genuinely
  flat by a re-entry cooldown. Full write-up in the vault plan.
  Earlier revisions of this README quoted 55.6% and −39.8%, from 3 folds and 264 trades on
  a harness that billed fees per in-market bar rather than per position; both figures are
  superseded, and the record of that is in the vault rather than deleted.

- **The room's loudest reaction is keyed to an uncalibrated number.** A resolved signal's
  severity is `|p − 0.5| × 2`, doubled on a loss — and severity drives reaction amplitude,
  rail colour, the tier swell, and the director's scene choice. But across those 50,200
  predictions, realized P(up) is flat at **0.47–0.50 in every predicted bucket** and the
  Brier skill score is **−0.35** — worse than predicting the base rate. So `p` carries no
  information about being right, and the show currently reacts hardest to a confidence that
  means nothing. Tracked as KI-041; the fix is to calibrate `p` or read it as a rank, not to
  quietly turn the reaction down.

## Where the design docs live

Planning, design records, and spikes live in an Obsidian vault outside this repo. Three
code-coupled docs stay here: [`docs/world-memory.md`](docs/world-memory.md) (the append-only
contract), [`docs/postgres-schema-spike.md`](docs/postgres-schema-spike.md), and
[`docs/freqai-takeaways.md`](docs/freqai-takeaways.md).
