# Model plane v0 (Sprint 9)

Design rules inherited from `docs/freqai-takeaways.md`. Everything below is
deliberately boring and replaceable — the baseline exists to be beaten.

## Pipeline

```
stored bars → model/features.py → model/train.py → artifact (versioned)
                     ↓                                   ↓
             model/predict.py  ──────────────────→  signals table
                     ↑
        model/backtest.py (walk-forward evaluation)
```

- **Features** (`model/features.py`): log returns (1/5/15/60 bars), rolling
  volatility (15/60), momentum vs 20-bar mean, high-low range, volume
  z-score. Clipped at ±8σ. One function serves training and live inference —
  no train/serve skew by construction.
- **Label**: sign of the forward `horizon_bars` (default 15) return,
  explicitly shifted; rows whose label would look past the end of data are
  dropped. A property test asserts features at time t are identical when
  computed on data truncated at t.
- **Model** (`model/train.py`): LightGBM binary classifier, fixed modest
  params, deterministic seed. Artifacts land in `model/artifacts/`
  (gitignored) as `SYMBOL_interval_version.txt` + `.metrics.json`, where
  `version = YYYYMMDD-<git sha>` — stamped on every signal row so
  mixed-version history stays interpretable.
- **Inference** (`model/predict.py`): newest complete bar → one signal row,
  idempotent upsert keyed `(symbol, interval, signal_timestamp,
  model_version)`. `resolved_at`/`outcome` stay NULL until the Sprint 10
  resolver fills them.

## Honest numbers (2026-07-19, ~1.6k usable BTCUSDT 1m rows)

- Holdout: **62.1% accuracy vs 56.7% always-up baseline** — above baseline,
  but logloss 0.767 is *worse* than a 0.693 coin flip: probabilities are
  poorly calibrated on this small dataset. Treat with suspicion until weeks
  of bars accumulate.
- Walk-forward with costs (0.1% fee + 1bp slippage per side, threshold 0.55):
  **−39.8% strategy vs +1.0% buy-and-hold** over 3 folds, 264 trades,
  55.6% hit rate. **The v0 strategy loses money after costs.** Round-trip
  costs (~0.22%) dwarf the per-trade edge at 15-bar holds on 1m data. This
  is the number that matters, published per the truth-over-vanity rule.
- Implications for v1: trade less often (higher threshold / longer horizon /
  larger interval), or the edge must grow. Cost-aware thresholding is the
  first lever to try.
- The oracle-leak regression test (`test_backtest.py`) proves the harness
  detects leaks: injecting the answer into a feature makes hit rate >90%;
  the honest pipeline sits near 55% — that gap is the evidence of honesty.

## Runbook

```bash
# retrain (do this after significant new data accumulates; ~weekly for now)
python -m model.train --symbol BTCUSDT --interval 1m
# evaluate honestly (writes model/artifacts/backtest_SYMBOL_interval.json)
python -m model.backtest --symbol BTCUSDT --interval 1m
# one-shot live prediction (Sprint 10 schedules this)
python -m model.predict --symbol BTCUSDT --interval 1m
```

Verify a new artifact before trusting it: check `.metrics.json` holdout
numbers against the previous artifact's, and re-run the backtest. Artifacts
are additive — old versions stay on disk; `latest_artifact()` picks by
mtime.
