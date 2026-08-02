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
    model_losing_streak: int = 3   # consecutive resolved losses → event


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


def detect_model_events(
    symbol: str, accuracy: dict, config: SalienceConfig | None = None
) -> list[dict]:
    """Salience fed by the model's own track record rather than the market —
    the first rule where the world reacts to the inhabitant, not the price.
    `accuracy` is storage.postgres_store.get_signal_accuracy output."""
    from datetime import datetime, timezone

    config = config or SalienceConfig()
    events: list[dict] = []
    if (
        accuracy.get("streak_outcome") == "loss"
        and accuracy.get("current_streak", 0) >= config.model_losing_streak
    ):
        streak = accuracy["current_streak"]
        events.append(
            {
                "occurred_at": datetime.now(timezone.utc),
                "event_type": "model_losing_streak",
                "symbol": symbol.upper(),
                # grows with the streak: 3 losses = 1.0, each extra +0.33
                "severity": round(streak / config.model_losing_streak, 4),
                "payload": {
                    "streak": streak,
                    "hit_rate": accuracy.get("hit_rate"),
                    "window": accuracy.get("window"),
                },
            }
        )
    return events


# Every event type the world can currently emit — the API validates against
# this set, and the renderer/overlays map visuals from it.
KNOWN_EVENT_TYPES = frozenset(
    {
        "big_move",
        "volatility_spike",
        "gap_open",
        "volume_anomaly",
        "streak",
        "signal_resolved",
        "model_losing_streak",
        # stream lifecycle (Sprint 11) — severities in world/stream_events.py
        "stream_started",
        "stream_stopped",
        "stream_dropped",
        # trader inhabitant (Sprint 12) — severities in world/trader_events.py
        "trader_opened",
        "trader_closed",
        "trader_milestone",
        # director actions (Sprint 13) — severities in director/events.py
        "scene_switched",
        "commentary_spoken",
        # YouTube broadcast lifecycle (Sprint 14) — severities in
        # broadcast/events.py; these are what "the stream was actually public"
        # is measured from (scripts/soak_report.compute_broadcast_uptime).
        "broadcast_created",
        "broadcast_live",
        "broadcast_ended",
    }
)
