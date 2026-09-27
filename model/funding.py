"""Funding and perp-premium features (experiment E9, vault
`Docs/funding-experiment.md`): positioning in the perpetual-futures market,
which a spot model has never seen. Every feature at bar t reads only what
was public when bar t CLOSED.

Sources (Binance USDⓈ-M, verified 2026-09-27): `/fapi/v1/fundingRate` rows
`{fundingTime, fundingRate}`; `/fapi/v1/premiumIndexKlines` and
`/fapi/v1/indexPriceKlines` rows are klines, [0] open time, [4] close,
[6] close time.
"""

import numpy as np
import pandas as pd

FUNDING_FEATURES = (
    "funding_last",
    "funding_change",
    "premium_1",
    "premium_mean_32",
    "premium_change_4",
)

BASELINE_RATE = 0.0001  # the interest-rate floor funding sits at when flat
_BAR = pd.Timedelta(minutes=15)
_KLINE_WIDTH_MS = 15 * 60_000 - 1
_SETTLEMENT = pd.Timedelta(hours=8)
_SETTLEMENT_SLACK = pd.Timedelta(seconds=1)  # settlements land ms after the hour
_PREMIUM_MEAN_WINDOW = 32  # 8h of 15m bars: the accruing next funding
_PREMIUM_CHANGE_BARS = 4
_GRID_MAX_BPS = 3.0
_GRID_MIN_RATIO = 3.0


def funding_series(rows: list[list]) -> pd.Series:
    """Settled rates indexed by settlement time. Refuses anything off the 8h
    schedule: a 4h stretch would silently change what `funding_change` means."""
    index = pd.to_datetime([int(r[0]) for r in rows], unit="ms", utc=True)
    series = pd.Series([float(r[1]) for r in rows], index=index, name="funding")
    if not series.index.is_monotonic_increasing or series.index.has_duplicates:
        raise ValueError("funding settlements are not strictly increasing")
    spacing = series.index.to_series().diff().dropna()
    if ((spacing - _SETTLEMENT).abs() > _SETTLEMENT_SLACK).any():
        raise ValueError("funding settlements are off the 8h schedule")
    return series


def kline_close(rows: list[list]) -> pd.Series:
    """A fapi 15m kline series' close, indexed by open time (the spot grid's
    convention)."""
    opens = np.array([int(k[0]) for k in rows])
    closes_at = np.array([int(k[6]) for k in rows])
    if (closes_at - opens != _KLINE_WIDTH_MS).any():
        raise ValueError("kline is not 15m wide: wrong interval or field map")
    index = pd.to_datetime(opens, unit="ms", utc=True)
    if index.has_duplicates:
        raise ValueError("duplicate kline open times")
    return pd.Series([float(k[4]) for k in rows], index=index).sort_index()


def check_grid(index_close: pd.Series, spot_close: pd.Series) -> dict[int, float]:
    """Median |index/spot − 1| in bps at lags −1, 0, +1. The fapi grid is the
    spot grid only if lag 0 is tiny AND far below its neighbours; a one-bar
    shift would put the next bar into the features and pass as skill."""
    medians = {}
    for lag in (-1, 0, 1):
        shifted = index_close.shift(lag).reindex(spot_close.index)
        medians[lag] = float((shifted / spot_close - 1.0).abs().median() * 1e4)
    neighbours = min(medians[-1], medians[1])
    if medians[0] >= _GRID_MAX_BPS or medians[0] * _GRID_MIN_RATIO >= neighbours:
        raise ValueError(f"index and spot are misaligned: medians {medians} bps")
    return medians


def funding_features(
    funding: pd.Series, premium: pd.Series, bars: pd.DatetimeIndex
) -> pd.DataFrame:
    """The five pre-registered features on the bar grid. A bar opening at T
    closes at T + 15m and sees a settlement only if fundingTime <= T + 15m."""
    last = funding.index.searchsorted(bars + _BAR, side="right") - 1
    rates = funding.to_numpy()
    seen = last >= 0
    has_prev = last >= 1
    features = pd.DataFrame(index=bars)
    features["funding_last"] = np.where(
        seen, rates[np.maximum(last, 0)] - BASELINE_RATE, np.nan)
    features["funding_change"] = np.where(
        has_prev, rates[np.maximum(last, 0)] - rates[np.maximum(last - 1, 0)],
        np.nan)

    p = premium.reindex(bars)
    features["premium_1"] = p
    features["premium_mean_32"] = p.rolling(_PREMIUM_MEAN_WINDOW).mean()
    features["premium_change_4"] = p - p.shift(_PREMIUM_CHANGE_BARS)
    return features


def with_funding_features(
    X: pd.DataFrame, y: pd.Series, feats: pd.DataFrame
) -> tuple[pd.DataFrame, pd.Series]:
    """X plus the funding features. Rows without a complete feature row are
    dropped, never filled."""
    joined = X.join(feats, how="inner").dropna()
    return joined, y.loc[joined.index]


def leaked_control(feats: pd.DataFrame, index_close: pd.Series) -> pd.DataFrame:
    """The positive control: `premium_change_4` replaced by the NEXT bar's
    index log return, carried through the same fapi cache and join. It must
    score far above base, or a null result means nothing. Never an arm."""
    leaked = feats.copy()
    next_return = np.log(index_close.shift(-1) / index_close)
    leaked["premium_change_4"] = next_return.reindex(feats.index)
    return leaked
