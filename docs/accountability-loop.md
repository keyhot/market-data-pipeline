# The accountability loop (Sprint 10)

"Failure is content" as infrastructure: every model prediction becomes a
public, resolved world event. Nothing is hidden; a confident wrong call is
the most salient thing the model can produce.

## Data flow

```
scheduler (interval cadence)
  ├─ inference:{SYMBOL}:{interval} → model/predict.py → signals row (pending)
  ├─ resolver:signals → world/resolver.py:
  │     signal past horizon + bars exist →
  │       UPDATE signals.resolved_at/outcome   (the one sanctioned UPDATE)
  │       APPEND world_events(signal_resolved) (severity ∝ |p−0.5|, ×2 on loss)
  │     then world/events.record_model_events → model_losing_streak events
  └─ salience:{SYMBOL}:1m → market-driven events (Sprint 9)

API reads:  /signals/{symbol} (+accuracy) · /world/events · SSE /stream/world/events
Overlays:   /overlay/signals (strip) · /overlay/events (feed) — OBS Browser Sources
```

## Resolution semantics

- A signal resolves when `signal_timestamp + horizon_bars × interval` has
  passed AND both the entry bar and the realized bar exist in `price_bars`.
  Missing bars leave it pending — retried every resolver run.
- Outcome: predicted direction vs `realized_close > entry_close`. Resolution
  uses bars **as stored at resolution time** (a later bar revision does not
  reopen an outcome).
- Idempotent by the `resolved_at IS NULL` guard: restarts and concurrent
  runs cannot double-resolve or double-emit events.
- Severity: `(probability − 0.5) × 2`, doubled for losses. Pinned by tests.
- Losing streaks (≥3 consecutive resolved losses per symbol) emit
  `model_losing_streak` events — the first salience rule fed by the model's
  own track record rather than the market.

## Truthfulness caveats (displayed, not buried)

- Overlay "P&L" and hit rates are **signal-based simulation** (resolver
  outcomes over stored bars) — no broker, no fills, no fees at this layer.
  The overlay footer says so on-screen. Fee-aware truth lives in
  `model/backtest.py` (which currently says v0 loses after costs —
  `docs/model-plane.md`).
- Both paper-trading spikes concluded **defer** with written triggers:
  `docs/alpaca-paper-spike.md` (design-only) and
  `docs/freqtrade-sidecar-spike.md` (hands-on; REST-mirror design proven,
  adoption parked for Sprint 12's world renderer).

## Operations

- Inference requires a trained artifact (`python -m model.train ...`);
  the scheduler container reads artifacts from the compose volume mount, so
  retraining needs no rebuild. Missing artifact = logged skip.
- Container image needs `libgomp1` (LightGBM); baked into the Dockerfile.
- Watchlist `predict: true` per ticker enables the cadence; equity inference
  respects market hours.
