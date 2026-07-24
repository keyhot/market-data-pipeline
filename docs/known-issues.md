# Known issues / tracked tech debt

From the 2026-07-24 whole-codebase bug audit (Sprints 1–11). The audit's
**confirmed, bounded bugs were fixed** on branch `fix/pre-sprint12-audit`
(binance_ws pagination + clean-close backoff, `get_pool()` race, resolver
`rows_written`, soak_report degraded-outage masking, `price_bars.volume`
NUMERIC). The items below are **deliberately deferred** — each is either latent
under the current config or needs a design decision, not a mechanical fix.

## Model plane — needs a design decision

### 1. Backtest compounds overlapping-horizon trades as if sequential (affects the headline −39.8%)
`model/backtest.py` marks every bar with `prob > entry_threshold` as `in_market`
and multiplies each bar's forward `[i, i+horizon)` return into the equity curve.
With `horizon_bars=15` and a 0.55 threshold, many in-market bars overlap in real
time, so their return windows are compounded as if they were sequential,
fully-capitalized, non-overlapping trades — a curve no capital-constrained
strategy could realize. **The `strategy_total_return` / `equity` / drawdown
figures (including the −39.8% quoted in `docs/model-plane.md` and elsewhere) are
therefore not a realizable P&L.** The purge gap between train/test is correct;
the bug is the within-fold per-bar overlap. `avg_trade_return` (arithmetic mean)
is undistorted, so the two headline numbers are mutually inconsistent.
*Fix direction:* cap concurrent exposure (skip/size-down overlapping entries) or
report a position-sized, non-overlapping equity curve. The *direction* ("loses
money after costs") is likely robust; the *magnitude* is suspect. Until fixed,
treat −39.8% as indicative, not exact.

### 2. `resolver.py` uses calendar-day steps for equity "1d" signals (latent)
`INTERVAL_DELTAS["1d"] = timedelta(days=1)`; `target_ts = signal_timestamp +
delta * horizon_bars` advances by calendar days. Correct for 24/7 crypto, wrong
for equities, whose "1d" bars are *trading* days (labels in `features.py` are
computed by row-shift, not calendar days). **Latent:** only crypto has `predict:
true` in `config/watchlist.yaml` today. The moment an equity gets `predict: true`,
its signals resolve against the wrong bar (or land on a weekend and never
resolve). *Fix direction:* for "1d", advance by counting stored trading-day bars
after `signal_timestamp`, not calendar days.

### 3. Horizon/interval contract is uncoupled train → predict → resolve (latent)
`predict()` stamps `horizon_bars` from a caller default, not from the trained
artifact (the artifact doesn't persist horizon), and its CLI has no `--horizon`.
`run_resolver_job` calls `record_model_events(symbol)` with no interval, so the
`interval="1m"` default is always used — for the equity-1d path, accuracy lookup
finds zero rows and `model_losing_streak` can never fire. **Latent** for the same
reason as #2. *Fix direction:* persist the trained horizon in the artifact and
read it back in `predict()`; thread the real interval through `run_resolver_job →
record_model_events`.

### 4. `train.py` holdout split has no purge gap (low impact)
`backtest.py` purges `horizon_bars` rows at every fold boundary, but the
train/holdout split in `train.py` does not, so the last ~`horizon_bars−1`
training rows have labels that peek a sliver past the split. Numerically
negligible on a normal dataset, but inconsistent with the project's own stated
purge rule for a metric billed as "honest, printed win or lose." *Fix:* insert a
`horizon_bars` gap at the split.

## Ops / API — accepted or edge

### 5. `stream_events._safe_latest` fails open on DB-down (by design)
The 5-minute `stream_dropped` cooldown is skipped when Postgres is unreachable
("don't let the cooldown check kill recording"). During simultaneous flapping +
Postgres outage, repeated `stream_dropped` events spool individually rather than
being deduped. Deliberate, commented tradeoff — noted, not a bug.

### 6. SSE poll uses a fixed lookback window (edge)
`api/main.py` SSE generators re-read a fixed window (10 bars / 20 events) per
poll; a burst larger than that between two polls could be missed *on the live
feed only* (Postgres and REST re-fetch still have everything). Plausible only
right after scheduler startup or a bulk backfill. Low priority.

## Verified clean by the audit
`model/features.py` no-lookahead property holds; `resolver.py`'s
`UPDATE ... WHERE resolved_at IS NULL` + rowcount check is race-safe; the
severity formula is correct; `backtest.py`'s train/test purge gap is correctly
sized; `salience.py` rolling-window rules avoid lookahead; the market-event
DB-backed cooldown defeats restart races.
