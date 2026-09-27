"""Entry gates that compose onto a `run_backtest(predict_fold=...)` predictor.

The 2026-09-27 Chronos spike found that the size of the next 15-bar move is
forecastable — trailing volatility ranks it as well as a 120M-parameter
forecaster (Spearman ~0.44) — and that the top volatility decile clears the
22 bps round trip 2.7-3.5x as often as the average bar. A gate asks whether
v0 is worth listening to only when a move could pay for itself.

Cutoffs come from each fold's TRAIN window only: a cutoff from the whole
series would leak future volatility into the rule. Vault:
`Spikes/chronos-forecaster-spike.md`, `Docs/vol-gate-experiment.md`.
"""

import numpy as np


def volatility_gate(inner, column: str, top_fraction: float):
    """Pass `inner`'s P(up) through on test bars whose `column` is in the top
    `top_fraction` of the train window; P(up) = 0 (flat) elsewhere."""
    def predict_fold(X, y, train, test):
        p = np.asarray(inner(X, y, train, test), dtype=float)
        if top_fraction >= 1.0:
            return p
        cutoff = np.quantile(X[column].iloc[train].to_numpy(), 1.0 - top_fraction)
        open_ = X[column].iloc[test].to_numpy() >= cutoff
        return np.where(open_, p, 0.0)
    return predict_fold


def always_long(X, y, train, test):
    """The no-model control: certain of "up" on every bar. Behind a gate, it
    isolates what the gate is worth from what the model adds inside it."""
    return np.ones(test.stop - test.start)
