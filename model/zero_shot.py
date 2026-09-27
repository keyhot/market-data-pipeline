"""Zero-shot forecaster plumbing (Chronos spike, step A) — pure and torch-free.

A pretrained forecaster (amazon/chronos-2) runs in its own environment
(`scripts/chronos_collect.py`, on Kaggle or `.venv-forecast`) and writes raw
quantile forecasts to CSV. Everything that decides what those forecasts mean
lives here, in numpy, under test: which closes a forecast may see, how a
quantile forecast becomes P(up), and how it lines up with the LightGBM
walk-forward rows so the two are scored on identical bars.

Vault: `Spikes/chronos-forecaster-spike.md`.
"""

import numpy as np
import pandas as pd


def p_up_from_quantiles(levels, values, close: float) -> float:
    """P(close[t+h] > close[t]) from one step's quantile forecast.

    The quantiles define a piecewise-linear CDF through (value, level). The
    label is strict, so the CDF is read right-continuous: forecast mass sitting
    exactly on the current close is not "up". Outside the outermost quantiles
    the forecast says nothing more, so the answer clamps to the extreme levels
    rather than reaching 0 or 1.
    """
    levels = np.asarray(levels, dtype=float)
    values = np.asarray(values, dtype=float)
    if np.any(np.diff(values) < 0):
        raise ValueError("quantile values must be non-decreasing")
    below = int(np.searchsorted(values, close, side="right"))  # values <= close
    if below == 0:
        cdf = levels[0]
    elif below == len(values):
        cdf = levels[-1]
    else:
        lo, hi = below - 1, below  # values[lo] <= close < values[hi]
        frac = (close - values[lo]) / (values[hi] - values[lo])
        cdf = levels[lo] + frac * (levels[hi] - levels[lo])
    return float(1.0 - cdf)


def context_window(closes: np.ndarray, end_pos: int, context_len: int) -> np.ndarray:
    """The closes a forecast made at bar `end_pos` may see: the last
    `context_len` of them, ending at — and including — that bar. A short
    history raises instead of forecasting from less, which would make the
    early rows a different model from the rest."""
    if end_pos >= len(closes):
        raise ValueError(f"end_pos {end_pos} is past the series ({len(closes)})")
    start = end_pos - context_len + 1
    if start < 0:
        raise ValueError(
            f"bar {end_pos} has {end_pos + 1} closes of history, needs {context_len}"
        )
    return closes[start : end_pos + 1]


def forecast_positions(index: pd.DatetimeIndex, wanted: pd.DatetimeIndex) -> np.ndarray:
    """Bar positions of the timestamps to forecast at; a timestamp with no
    stored bar is an error, never a silent skip."""
    positions = index.get_indexer(wanted)
    if (positions < 0).any():
        missing = wanted[positions < 0]
        raise KeyError(f"{len(missing)} timestamps have no bar, first {missing[0]}")
    return positions


def with_forecaster_p(oos: pd.DataFrame, forecasts: pd.DataFrame) -> pd.DataFrame:
    """The LightGBM walk-forward rows with `p` swapped for the forecaster's.

    Same bars, same folds, same labels and forward returns — so every number
    `scripts/confidence_report.py` prints is a like-for-like comparison. A row
    without a forecast raises: scoring a subset would compare different bars.
    """
    lookup = forecasts.set_index("ts")["p"]
    p = lookup.reindex(oos.ts)
    if p.isna().any():
        raise ValueError(f"{int(p.isna().sum())} OOS rows have no forecast")
    out = oos.copy()
    out["p"] = p.to_numpy()
    return out


def p_up_frame(forecasts: pd.DataFrame) -> pd.DataFrame:
    """`ts, p` from the collector's `ts, close, q<level>...` rows.

    Quantile heads can cross slightly (BTC: 20 of 50,200 rows, by < 0.2 bps,
    all between the 0.75 and 0.8 heads). Each row's values are sorted first:
    monotone rearrangement (Chernozhukov et al., 2010), which never moves a
    forecast further from the truth. `p_up_from_quantiles` itself stays
    strict, so an unsorted input anywhere else is still an error."""
    columns = [c for c in forecasts.columns if c.startswith("q")]
    levels = np.array([float(c[1:]) for c in columns])
    order = np.argsort(levels)
    levels, columns = levels[order], [columns[i] for i in order]
    quantiles = np.sort(forecasts[columns].to_numpy(dtype=float), axis=1)
    closes = forecasts["close"].to_numpy(dtype=float)
    p = [p_up_from_quantiles(levels, q, c) for q, c in zip(quantiles, closes)]
    return pd.DataFrame({"ts": forecasts["ts"].to_numpy(), "p": p})


def cached_predictor(p_by_ts: pd.Series):
    """A `run_backtest(predict_fold=...)` that serves stored P(up) for each
    fold's test bars and ignores the train slice — a zero-shot forecaster has
    nothing to fit. A test bar without a stored prediction raises."""
    def predict_fold(X, y, train, test):
        wanted = X.index[test]
        missing = ~wanted.isin(p_by_ts.index)
        if missing.any():
            raise KeyError(f"{int(missing.sum())} test bars have no prediction")
        return p_by_ts.loc[wanted].to_numpy()
    return predict_fold
