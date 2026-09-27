"""Order-flow features (experiment E8, vault `Docs/order-flow-experiment.md`):
the kline's own taker-buy volume and trade count, which `_klines_to_frame`
drops and v0 has never seen. Every feature at t reads bars <= t only.

Field map, verified against a live /api/v3/klines row on 2026-09-27:
[5] volume, [7] quote volume, [8] trades, [9] taker-buy base volume.
"""

import numpy as np
import pandas as pd

from model.features import _normalize

FLOW_FEATURES = (
    "flow_imbalance_1",
    "flow_imbalance_5",
    "flow_resid_1",
    "trades_zscore",
    "trade_size_zscore",
)

_IMBALANCE_WINDOW = 5
_RESID_WINDOW = 60
_ZSCORE_WINDOW = 60
_TOLERANCE = 1e-9


def flow_frame(raw: list[list]) -> pd.DataFrame:
    """volume / trades / taker_buy indexed by kline open time. Refuses rows
    that could only come from a wrong field map, since a misread column
    would be a silent new feature rather than an error."""
    index = pd.to_datetime([k[0] for k in raw], unit="ms", utc=True)
    low = np.array([float(k[3]) for k in raw])
    high = np.array([float(k[2]) for k in raw])
    volume = np.array([float(k[5]) for k in raw])
    quote = np.array([float(k[7]) for k in raw])
    trades = np.array([float(k[8]) for k in raw])
    taker_buy = np.array([float(k[9]) for k in raw])

    if ((taker_buy < 0) | (taker_buy > volume * (1 + _TOLERANCE))).any():
        raise ValueError("taker-buy volume outside [0, volume]: wrong field map")
    if ((trades < 0) | (trades != np.floor(trades))).any():
        raise ValueError("trades is not a non-negative integer: wrong field map")
    traded = volume > 0
    vwap = quote[traded] / volume[traded]
    if ((vwap < low[traded] * (1 - _TOLERANCE))
            | (vwap > high[traded] * (1 + _TOLERANCE))).any():
        raise ValueError("quote volume / volume outside [low, high]: wrong field map")

    frame = pd.DataFrame(
        {"volume": volume, "trades": trades, "taker_buy": taker_buy}, index=index
    )
    frame.index.name = "Date"
    return frame


def align_flow(flow: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """Flow restricted to the stored bars' timestamps. The volumes must agree
    on every shared bar: a one-bar shift would put the next bar's flow into
    the features and pass as skill."""
    stored = _normalize(bars)["volume"]
    shared = flow.index.intersection(stored.index)
    if shared.empty:
        raise ValueError("flow and stored bars share no timestamps")
    # Half a unit: the live price_bars.volume is still BIGINT (migrate_013
    # unapplied, KI-090), so the store holds volume rounded to whole units.
    mismatch = ~np.isclose(flow.loc[shared, "volume"], stored.loc[shared],
                           rtol=0.0, atol=0.5 + 1e-9)
    if mismatch.any():
        raise ValueError(
            f"flow volume disagrees with stored volume on {int(mismatch.sum())} "
            f"of {len(shared)} bars: misaligned"
        )
    return flow.loc[shared]


def flow_features(flow: pd.DataFrame, close: pd.Series) -> pd.DataFrame:
    """The five pre-registered features. Zero-volume bars give NaN, which the
    join drops rather than fills."""
    volume = flow["volume"].where(flow["volume"] > 0)
    signed = 2.0 * flow["taker_buy"] - volume
    features = pd.DataFrame(index=flow.index)
    features["flow_imbalance_1"] = signed / volume
    features["flow_imbalance_5"] = (
        signed.rolling(_IMBALANCE_WINDOW).sum()
        / volume.rolling(_IMBALANCE_WINDOW).sum()
    )

    r = np.log(close / close.shift(1)).reindex(flow.index)
    imb = features["flow_imbalance_1"]
    slope = imb.rolling(_RESID_WINDOW).cov(r) / r.rolling(_RESID_WINDOW).var()
    intercept = (imb.rolling(_RESID_WINDOW).mean()
                 - slope * r.rolling(_RESID_WINDOW).mean())
    features["flow_resid_1"] = imb - intercept - slope * r

    trades = flow["trades"].astype(float)
    features["trades_zscore"] = _zscore(trades)
    size = np.log(volume / trades.where(trades > 0))
    features["trade_size_zscore"] = _zscore(size)
    return features.replace([np.inf, -np.inf], np.nan)


def with_flow_features(
    X: pd.DataFrame, y: pd.Series, flow_feats: pd.DataFrame
) -> tuple[pd.DataFrame, pd.Series]:
    """X plus the flow features. Rows without a complete flow row are
    dropped, never filled."""
    joined = X.join(flow_feats, how="inner").dropna()
    return joined, y.loc[joined.index]


def leaked_control(flow_feats: pd.DataFrame) -> pd.DataFrame:
    """The positive control: `flow_imbalance_1` replaced by the NEXT bar's.
    It must score far above base, or the plumbing is broken and a null
    result means nothing. Never an arm."""
    leaked = flow_feats.copy()
    leaked["flow_imbalance_1"] = flow_feats["flow_imbalance_1"].shift(-1)
    return leaked


def _zscore(series: pd.Series) -> pd.Series:
    mean = series.rolling(_ZSCORE_WINDOW).mean()
    std = series.rolling(_ZSCORE_WINDOW).std()
    return (series - mean) / std.replace(0.0, np.nan)
