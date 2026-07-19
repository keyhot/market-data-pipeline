"""Salience engine v0 (Sprint 9): deterministic rules that answer
"why is this moment interesting?" over the bar stream. No LLM anywhere in
this path — personalities later become policies with different thresholds
over the same rules, so every threshold lives in SalienceConfig.
"""

import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

SALIENCE_ENABLED_ENV = "SALIENCE_ENABLED"


def salience_enabled() -> bool:
    raw = os.environ.get(SALIENCE_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no"}


@dataclass(frozen=True)
class SalienceConfig:
    vol_window: int = 60           # bars for rolling stats
    vol_zscore_threshold: float = 3.0
    gap_threshold: float = 0.004   # open vs previous close, fraction
    streak_length: int = 7         # consecutive same-direction closes
    volume_zscore_threshold: float = 4.0
    big_move_sigmas: float = 4.0   # single-bar return vs rolling sigma
    lookback_bars: int = 10        # evaluate this many recent bars per run
    cooldown_minutes: int = 30     # same type+symbol suppressed within this


def detect_events(
    symbol: str, bars: pd.DataFrame, config: SalienceConfig | None = None
) -> list[dict]:
    """Pure rule evaluation over the most recent bars. Returns candidate
    events (the cooldown/dedupe guard lives in world/events.py, backed by
    the database, not process memory)."""
    config = config or SalienceConfig()
    frame = _normalize(bars)
    if len(frame) < config.vol_window + config.lookback_bars:
        return []

    close = frame["close"]
    returns = close.pct_change()
    vol = returns.rolling(config.vol_window).std()
    vol_z = (returns.abs() / vol.shift(1)).replace([np.inf, -np.inf], np.nan)
    volume = frame["volume"].astype(float)
    volume_mean = volume.rolling(config.vol_window).mean()
    volume_std = volume.rolling(config.vol_window).std()
    volume_z = ((volume - volume_mean) / volume_std.replace(0.0, np.nan)).replace(
        [np.inf, -np.inf], np.nan
    )
    gap = (frame["open"] / close.shift(1) - 1.0).abs()
    direction = np.sign(close.diff())

    events: list[dict] = []
    recent = frame.index[-config.lookback_bars:]
    for ts in recent:
        row_return = returns.loc[ts]
        row_vol_z = vol_z.loc[ts]
        if pd.notna(row_vol_z) and row_vol_z >= config.big_move_sigmas:
            events.append(_event(
                ts, "big_move", symbol, float(row_vol_z),
                {"return": float(row_return), "sigmas": float(row_vol_z)},
            ))
        rolling_sigma = vol.loc[ts]
        window_sigma_z = (
            vol.loc[ts] / vol.shift(config.vol_window).loc[ts]
            if pd.notna(vol.shift(config.vol_window).loc[ts])
            and vol.shift(config.vol_window).loc[ts] > 0
            else np.nan
        )
        if pd.notna(window_sigma_z) and window_sigma_z >= config.vol_zscore_threshold:
            events.append(_event(
                ts, "volatility_spike", symbol, float(window_sigma_z),
                {"sigma_now": float(rolling_sigma), "ratio": float(window_sigma_z)},
            ))
        row_gap = gap.loc[ts]
        if pd.notna(row_gap) and row_gap >= config.gap_threshold:
            events.append(_event(
                ts, "gap_open", symbol, float(row_gap / config.gap_threshold),
                {"gap": float(row_gap)},
            ))
        row_volume_z = volume_z.loc[ts]
        if pd.notna(row_volume_z) and row_volume_z >= config.volume_zscore_threshold:
            events.append(_event(
                ts, "volume_anomaly", symbol, float(row_volume_z),
                {"volume_zscore": float(row_volume_z)},
            ))

    streak = _current_streak(direction)
    if streak >= config.streak_length:
        up = bool(direction.iloc[-1] > 0)
        events.append(_event(
            frame.index[-1], "streak", symbol, float(streak),
            {"bars": int(streak), "direction": "up" if up else "down"},
        ))
    return events


def _event(ts, event_type, symbol, severity, payload) -> dict:
    return {
        "occurred_at": ts.to_pydatetime(),
        "event_type": event_type,
        "symbol": symbol.upper(),
        "severity": round(severity, 4),
        "payload": payload,
    }


def _current_streak(direction: pd.Series) -> int:
    count = 0
    last = 0.0
    for value in reversed(direction.dropna().tolist()):
        if value == 0:
            break
        if last == 0.0:
            last = value
            count = 1
        elif value == last:
            count += 1
        else:
            break
    return count


def _normalize(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index(pd.to_datetime(frame["timestamp"], utc=True))
    frame.columns = [str(c).lower() for c in frame.columns]
    if not frame.index.is_monotonic_increasing:
        frame = frame.sort_index()
    return frame
