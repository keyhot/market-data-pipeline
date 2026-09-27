"""E10: v0 with the label moved to a longer horizon, scored in positions
against a drift null (vault `Docs/horizon-experiment.md`).

Everything here takes `h` explicitly. The E7–E9 reports import `HORIZON = 1`
from `live_gate_report`, and reusing their `collect` would silently purge one
bar and score a one-bar return.
"""

import numpy as np
import pandas as pd

from model.backtest import find_episodes, merge_overlapping_holds

GATE_QUANTILE = 0.8
BOOTSTRAP = 1000
SEED = 7


def forward_return(close: pd.Series, h: int) -> pd.Series:
    return close.shift(-h) / close - 1.0


def expanding_oos(X: pd.DataFrame, y: pd.Series, ret: pd.Series, h: int,
                  years, fit) -> pd.DataFrame:
    """One model per test year, trained on every row before it less an h-row
    purge (the last training labels look h bars ahead). `centre` is the
    training rows' up rate: the call centre, known at prediction time, so a
    drifting market's base rate does not turn every call long."""
    chunks = []
    for year in years:
        test = np.flatnonzero(X.index.year == year)
        train_end = test[0] - h
        model = fit(X.iloc[:train_end], y.iloc[:train_end])
        chunks.append(pd.DataFrame({
            "year": year,
            "p": np.asarray(model.predict(X.iloc[test]), dtype=float),
            "y": y.iloc[test].to_numpy(),
            "ret": ret.iloc[test].to_numpy(),
            "centre": float(y.iloc[:train_end].mean()),
        }, index=X.index[test]))
    return pd.concat(chunks)


def gate_top_fifth(oos: pd.DataFrame) -> pd.Series:
    """The top fifth of |p − centre| within each year."""
    conf = (oos.p - oos.centre).abs()
    cutoff = conf.groupby(oos.year).transform(lambda c: c.quantile(GATE_QUANTILE))
    return conf >= cutoff


def drift_per_bar(close: pd.Series) -> dict[int, float]:
    """Each calendar year's mean geometric return per bar."""
    out = {}
    for year, c in close.groupby(close.index.year):
        out[int(year)] = float((c.iloc[-1] / c.iloc[0]) ** (1 / (len(c) - 1)) - 1)
    return out


def positions(side: pd.Series, close: pd.Series, h: int,
              mu: dict[int, float]) -> pd.DataFrame:
    """Gated calls (`side` ∈ {−1, 0, 1} on close's index) as positions:
    consecutive calls on one side merge into one, held to the last call + h,
    by the harness's own definitions. Each is scored against holding the same
    side for the same bars at its entry year's drift: skill = gross − null."""
    closes = close.to_numpy(dtype=float)
    side = side.reindex(close.index, fill_value=0).to_numpy()
    rows = []
    for sign in (1, -1):
        closed, pending = merge_overlapping_holds(find_episodes(side == sign), h)
        for start, end in closed + ([pending] if pending is not None else []):
            exit_ = end + h
            if exit_ >= len(closes):
                continue
            bars = exit_ - start
            year = int(close.index[start].year)
            gross = sign * (closes[exit_] / closes[start] - 1.0)
            null = sign * ((1.0 + mu[year]) ** bars - 1.0)
            rows.append({"side": sign, "entry": close.index[start], "year": year,
                         "bars": bars, "gross": gross, "null": null,
                         "skill": gross - null})
    return pd.DataFrame(rows, columns=["side", "entry", "year", "bars",
                                       "gross", "null", "skill"])


def per_call_proxy_bps(oos: pd.DataFrame, gated: pd.Series) -> float:
    """E9's money proxy with the long bias taken out: mean(s·r) − mean(s)·mean(r)
    over the gated calls, in bps."""
    g = oos[gated]
    s = np.sign(g.p - g.centre)
    return float(((s * g.ret).mean() - s.mean() * g.ret.mean()) * 1e4)


def week_block_ci(values, weeks, n: int = BOOTSTRAP,
                  seed: int = SEED) -> tuple[float, float, float]:
    """Mean and a 95% interval, resampling whole weeks: at h up to a day,
    neighbouring positions share news, so rows are not independent."""
    values = np.asarray(values, dtype=float)
    codes, _ = pd.factorize(np.asarray(weeks))
    rows_of = [np.flatnonzero(codes == k) for k in range(codes.max() + 1)]
    rng = np.random.default_rng(seed)
    draws = [values[np.concatenate([rows_of[i] for i in
                                    rng.integers(0, len(rows_of), len(rows_of))])].mean()
             for _ in range(n)]
    low, high = np.percentile(draws, [2.5, 97.5])
    return float(values.mean()), float(low), float(high)
