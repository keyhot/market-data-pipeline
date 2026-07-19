"""Feature pipeline (Sprint 9): the single source of features for both
training and live inference — no train/serve skew by construction.

Rules from docs/freqai-takeaways.md: features at time t use only bars <= t;
the label looks forward explicitly and nowhere else.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

DEFAULT_HORIZON_BARS = 15

_RETURN_WINDOWS = (1, 5, 15, 60)
_VOL_WINDOWS = (15, 60)
_MOMENTUM_WINDOW = 20
_VOLUME_WINDOW = 60

# Extreme feature values are clipped, not dropped (freqai-takeaways rule 7,
# light version) — a flash spike should register, not explode the tree splits.
_CLIP_SIGMA = 8.0


@dataclass
class FeatureResult:
    X: pd.DataFrame
    y: pd.Series
    feature_names: list[str] = field(default_factory=list)


def build_features(
    bars: pd.DataFrame, horizon_bars: int = DEFAULT_HORIZON_BARS
) -> FeatureResult:
    """bars: OHLCV frame with a UTC DatetimeIndex (provider- or store-shaped).
    Returns aligned X and y where y[t] = 1 if close[t + horizon] > close[t]."""
    frame = _normalize(bars)
    features = _compute_features(frame)
    close = frame["close"]

    # The one deliberate look into the future — and the rows where it looks
    # past the end of the data are dropped, never filled.
    forward_return = close.shift(-horizon_bars) / close - 1.0
    label = (forward_return > 0).astype(int)

    valid = features.notna().all(axis=1) & forward_return.notna()
    return FeatureResult(
        X=features[valid], y=label[valid], feature_names=list(features.columns)
    )


def build_latest_features(bars: pd.DataFrame) -> pd.DataFrame | None:
    """Features for the newest complete bar only (live inference path).
    Same computation as build_features with the label step skipped — no
    future data is required or touched. None when history is too short."""
    frame = _normalize(bars)
    features = _compute_features(frame)
    if features.empty:
        return None
    last = features.iloc[[-1]]
    if last.isna().any(axis=None):
        return None
    return last


def _compute_features(frame: pd.DataFrame) -> pd.DataFrame:
    close = frame["close"]
    features = pd.DataFrame(index=frame.index)
    for window in _RETURN_WINDOWS:
        features[f"log_return_{window}"] = np.log(close / close.shift(window))
    returns_1 = features["log_return_1"]
    for window in _VOL_WINDOWS:
        features[f"volatility_{window}"] = returns_1.rolling(window).std()
    features["momentum"] = close / close.rolling(_MOMENTUM_WINDOW).mean() - 1.0
    features["hl_range"] = (frame["high"] - frame["low"]) / close
    volume = frame["volume"].astype(float)
    vol_mean = volume.rolling(_VOLUME_WINDOW).mean()
    vol_std = volume.rolling(_VOLUME_WINDOW).std()
    features["volume_zscore"] = (volume - vol_mean) / vol_std.replace(0.0, np.nan)
    return features.clip(lower=-_CLIP_SIGMA, upper=_CLIP_SIGMA)


def _normalize(bars: pd.DataFrame) -> pd.DataFrame:
    """Accept provider frames (Open/High/Low/Close/Volume columns) and store
    reader shapes (open/high/low/close/volume with a timestamp column)."""
    frame = bars.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index(pd.to_datetime(frame["timestamp"], utc=True))
    frame.columns = [str(c).lower() for c in frame.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"bars frame missing columns: {sorted(missing)}")
    frame = frame[sorted(required)].astype(float)
    if not frame.index.is_monotonic_increasing:
        frame = frame.sort_index()
    return frame
