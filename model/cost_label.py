"""E1 — a cost-aware 3-class label, scored through `run_backtest(predict_fold=...)`.

v0 labels `close[t+h] > close[t]`, so a +1 bp move and a +50 bp move are the
same positive and the model cannot see whether a move pays the round trip.
Here the label is `up` / `flat` / `down` beyond that round trip, read from
`BacktestConfig`, and the strategy goes long where the model's argmax is `up`
— "not worth trading" is a prediction, not a threshold bolted on afterwards.

Backtest-only by design: `build_features`, `predict.py`, the `signals` table
and the on-air severity mapping are untouched. Vault:
`Docs/cost-label-experiment.md` (pre-registration and results).
"""

import lightgbm as lgb
import numpy as np
import pandas as pd

from model.backtest import BacktestConfig, _close_series
from model.train import _NUM_ROUNDS, _PARAMS

DOWN, FLAT, UP = 0, 1, 2

_MULTICLASS_PARAMS = {
    **_PARAMS,
    "objective": "multiclass",
    "metric": "multi_logloss",
    # Explicit: a calm train window can hold no `down` at all, and the
    # booster must still know there are three classes.
    "num_class": 3,
}


def round_trip_cost(config: BacktestConfig) -> float:
    """The toll a move must clear — the same one `position_returns` charges."""
    return 2 * (config.fee_per_side + config.slippage)


def forward_returns(
    bars: pd.DataFrame, horizon_bars: int, index: pd.Index
) -> pd.Series:
    """`close[t+h] / close[t] - 1` in bar space, on the rows of `index`.

    The same definition `build_features` labels from, so its sign agrees
    with v0's `y` row for row."""
    close = _close_series(bars)
    return (close.shift(-horizon_bars) / close - 1.0).reindex(index)


def cost_class(forward_return: pd.Series, cost: float) -> np.ndarray:
    """UP beyond +cost, DOWN beyond -cost, FLAT between. Strict bands: a move
    of exactly the cost only pays for itself and is not worth taking."""
    values = np.asarray(forward_return, dtype=float)
    if np.isnan(values).any():
        raise ValueError("forward returns contain NaN; misaligned with X?")
    return np.select([values > cost, values < -cost], [UP, DOWN], FLAT)


def cost_label_fold(forward_return: pd.Series, cost: float):
    """Fit a multiclass booster on the train slice's cost classes; 1.0 on test
    bars whose argmax is UP, 0.0 elsewhere. Labels are read for the train
    rows only — the harness purges `horizon_bars` so they cannot reach test."""
    def predict_fold(X, y, train, test):
        labels = cost_class(forward_return.reindex(X.index[train]), cost)
        booster = lgb.train(
            _MULTICLASS_PARAMS,
            lgb.Dataset(X.iloc[train], label=labels),
            num_boost_round=_NUM_ROUNDS,
        )
        proba = booster.predict(X.iloc[test])
        return (proba.argmax(axis=1) == UP).astype(float)
    return predict_fold


def oracle_fold(forward_return: pd.Series, cost: float):
    """The control that must differ: the TRUE future class through the same
    seam. If it fails to beat its null, the alignment is broken."""
    def predict_fold(X, y, train, test):
        labels = cost_class(forward_return.reindex(X.index[test]), cost)
        return (labels == UP).astype(float)
    return predict_fold
