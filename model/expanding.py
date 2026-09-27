"""The on-air model as a walk-forward predictor (L1, vault
`Docs/pnl-15m-experiment.md`): expanding training and the live gate,
refit once per calendar period.

The harness hands each fold a rolling train slice. This predictor ignores its
start and trains on every row before it, which `run_backtest`'s contract
allows. At each refit it repeats `model/train.py`'s recipe on those rows: fit
on the first 80% less a purge, then gate at `GATE_QUANTILE` of |p - 0.5| on
the last 20%. Ungated rows come back as 0.5, so with `entry_threshold=0.5`
the harness goes long only on a gated call for up.
"""

import lightgbm as lgb
import numpy as np
import pandas as pd

from model.backtest import find_episodes, merge_overlapping_holds
from model.train import _NUM_ROUNDS, _PARAMS, GATE_QUANTILE, HOLDOUT_FRACTION


def _lightgbm_fit(X: pd.DataFrame, y: pd.Series):
    return lgb.train(_PARAMS, lgb.Dataset(X, label=y),
                     num_boost_round=_NUM_ROUNDS)


class ExpandingGatedFold:
    """A `predict_fold` for `run_backtest`. `calls` keeps every test row's raw
    p and whether it cleared the gate, for the signed (long and short) read."""

    def __init__(self, fit=None, period: str = "Q", horizon_bars: int = 1,
                 gate_quantile: float = GATE_QUANTILE):
        self._fit = fit or _lightgbm_fit
        self._period = period
        self._horizon = horizon_bars
        self._quantile = gate_quantile
        self._model = None
        self._fitted_for = None
        self.cutoff: float | None = None
        self._chunks: list[pd.DataFrame] = []

    def _refit(self, X: pd.DataFrame, y: pd.Series, stop: int) -> None:
        split = int(stop * (1 - HOLDOUT_FRACTION))
        # Purge, as train.py does: the last labels before the split look
        # `horizon` bars past it, into the rows the cutoff is taken from.
        fit_end = max(split - self._horizon, 0)
        self._model = self._fit(X.iloc[:fit_end], y.iloc[:fit_end])
        confidence = np.abs(self._model.predict(X.iloc[split:stop]) - 0.5)
        self.cutoff = float(np.quantile(confidence, self._quantile))

    def __call__(self, X, y, train: slice, test: slice) -> np.ndarray:
        period = X.index[test.start].tz_localize(None).to_period(self._period)
        if period != self._fitted_for:
            self._refit(X, y, train.stop)
            self._fitted_for = period
        p = np.asarray(self._model.predict(X.iloc[test]), dtype=float)
        gated = np.abs(p - 0.5) >= self.cutoff
        self._chunks.append(
            pd.DataFrame({"p": p, "gated": gated}, index=X.index[test]))
        return np.where(gated, p, 0.5)

    @property
    def calls(self) -> pd.DataFrame:
        return pd.concat(self._chunks) if self._chunks else pd.DataFrame(
            columns=["p", "gated"])


def signed_gross_returns(calls: pd.DataFrame, close: pd.Series,
                         horizon_bars: int) -> dict[str, list[float]]:
    """Gross return of each merged position if gated calls were traded both
    ways: up opens a long, down a short. Positions come from the harness's own
    `find_episodes` and `merge_overlapping_holds`, one per direction; a
    position whose exit falls off the end is dropped, as the harness does."""
    closes = close.to_numpy(dtype=float)
    row_of = close.index.get_indexer(calls.index)
    out: dict[str, list[float]] = {}
    for side, sign, mask in (("long", 1.0, calls.gated & (calls.p > 0.5)),
                             ("short", -1.0, calls.gated & (calls.p < 0.5))):
        episodes = [(int(row_of[s]), int(row_of[e]))
                    for s, e in find_episodes(mask.to_numpy())]
        closed, pending = merge_overlapping_holds(episodes, horizon_bars)
        held = closed + ([pending] if pending is not None else [])
        out[side] = [
            sign * (closes[end + horizon_bars] / closes[start] - 1.0)
            for start, end in held if end + horizon_bars < len(closes)
        ]
    return out
