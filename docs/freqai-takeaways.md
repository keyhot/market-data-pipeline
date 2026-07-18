# FreqAI takeaways — design rules for our model plane

Spike output (Sprint 9, 2026-07-19). Source: freqtrade.io FreqAI docs +
architecture review. **freqtrade/FreqAI is GPL-3.0 — everything below is
design ideas re-expressed in our own terms; no code was or may be copied.**

## Rules we adopt

1. **Labels look into the future explicitly, features never do.** FreqAI has
   users define targets that "intentionally look into the future"
   (`label_period_candles`) while features come only from completed candles.
   Ours: label = sign of forward `horizon_bars` return, computed with an
   explicit shift; a property test asserts features at time t are identical
   when computed on data truncated at t (`model/features.py`).
2. **Walk-forward is the only honest evaluation.** FreqAI retrains on a
   sliding window (`train_period_days`) and evaluates strictly forward
   (`backtest_period_days`), emulating live retraining in backtests. Ours:
   rolling train→predict folds in `model/backtest.py`, never shuffled.
3. **Purge the train/test boundary.** A label with an N-bar horizon leaks
   into the next fold's first N bars unless a gap is purged. Ours: purge
   `horizon_bars` between train and test windows.
4. **One feature function for train and live.** FreqAI builds features
   through the same strategy code path in backtest and dry-run, killing
   train/serve skew. Ours: `build_features()` is the single source for both
   `train.py` and `predict.py`.
5. **Boosted trees first.** FreqAI's stock examples are LightGBM/CatBoost
   regressors and classifiers; neural nets are opt-in extras. Confirms our
   plan: LightGBM baseline, deep learning only after it's beaten honestly.
6. **Retraining is routine, not an event.** Models go stale; FreqAI treats
   periodic refit as part of normal operation. Ours: `model_version` stamped
   on every signal so mixed-version histories stay interpretable; retrain is
   a one-line CLI (`python -m model.train`).
7. **Outliers are handled deliberately.** FreqAI ships outlier removal
   (SVM/DBSCAN-style) and a dissimilarity index that suppresses predictions
   on data unlike the training set. Ours (v0, lighter): clip/flag extreme
   feature z-scores; revisit a dissimilarity guard when signals go live in
   Sprint 10 — a model asked about a regime it never saw should abstain.
8. **Fees are not optional.** freqtrade backtests always model exchange fees;
   dry-run and backtest are kept distinct concepts. Ours: fees + slippage
   are constructor arguments of the backtest, never zero by default.

## What we consciously skip (v0)

- **Their 10k-feature expansion** (auto-generated indicator × timeframe ×
  shift grids). We start with ~10 hand-picked features — interpretability
  over coverage while the dataset is small.
- **Hyperparameter optimization** (their hyperopt integration). Fixed modest
  params until the walk-forward harness says tuning is the bottleneck.
- **Their adaptive live-retrain loop.** We retrain manually per runbook
  until Sprint 10's cadence job makes staleness observable.

## Implications for this sprint's tickets

- Feature pipeline (T3): rules 1, 4, 7. Baseline (T4): rules 5, 6.
- Backtest (T5): rules 2, 3, 8. Signals writer (T6): rule 6
  (`model_version` column). Sprint 10 resolver inherits rule 7's abstain
  idea as a candidate ticket.
