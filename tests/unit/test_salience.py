from datetime import timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd

from world.events import record_salient_events
from world.salience import SalienceConfig, detect_events, salience_enabled

CONFIG = SalienceConfig(vol_window=30, lookback_bars=5, cooldown_minutes=30)


def _calm_bars(n=120, seed=1):
    rng = np.random.default_rng(seed)
    index = pd.date_range("2026-07-19", periods=n, freq="1min", tz="UTC")
    close = 100.0 + np.cumsum(rng.normal(0, 0.01, n))
    return pd.DataFrame(
        {
            "Open": np.concatenate([[100.0], close[:-1]]),
            "High": close + 0.02,
            "Low": close - 0.02,
            "Close": close,
            "Volume": rng.integers(900, 1100, n).astype(float),
        },
        index=index,
    )


def test_salience_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("SALIENCE_ENABLED", raising=False)
    assert salience_enabled() is True
    monkeypatch.setenv("SALIENCE_ENABLED", "0")
    assert salience_enabled() is False


def test_calm_data_fires_nothing():
    assert detect_events("BTCUSDT", _calm_bars(), CONFIG) == []


def test_big_move_detected():
    bars = _calm_bars()
    bars.iloc[-2, bars.columns.get_loc("Close")] += 5.0  # ~5% jump vs 0.01 sigma

    events = detect_events("BTCUSDT", bars, CONFIG)

    types = [e["event_type"] for e in events]
    assert "big_move" in types
    big = next(e for e in events if e["event_type"] == "big_move")
    assert big["severity"] >= CONFIG.big_move_sigmas
    assert big["symbol"] == "BTCUSDT"


def test_gap_open_detected():
    bars = _calm_bars()
    bars.iloc[-1, bars.columns.get_loc("Open")] = (
        bars["Close"].iloc[-2] * 1.01
    )

    events = detect_events("BTCUSDT", bars, CONFIG)

    assert any(e["event_type"] == "gap_open" for e in events)


def test_volume_anomaly_detected():
    bars = _calm_bars()
    bars.iloc[-1, bars.columns.get_loc("Volume")] = 50_000.0

    events = detect_events("BTCUSDT", bars, CONFIG)

    assert any(e["event_type"] == "volume_anomaly" for e in events)


def test_streak_detected():
    bars = _calm_bars()
    closes = bars["Close"].to_numpy().copy()
    for i in range(-8, 0):
        closes[i] = closes[i - 1] + 0.02  # eight rising closes
    bars["Close"] = closes

    events = detect_events("BTCUSDT", bars, CONFIG)

    streaks = [e for e in events if e["event_type"] == "streak"]
    assert streaks and streaks[0]["payload"]["direction"] == "up"
    assert streaks[0]["payload"]["bars"] >= CONFIG.streak_length


def test_short_history_fires_nothing():
    assert detect_events("BTCUSDT", _calm_bars(n=20), CONFIG) == []


@patch("world.events.append_world_events")
@patch("world.events.latest_world_event_time", return_value=None)
def test_record_appends_when_no_cooldown(mock_latest, mock_append):
    bars = _calm_bars()
    bars.iloc[-1, bars.columns.get_loc("Volume")] = 50_000.0

    written = record_salient_events("BTCUSDT", bars, CONFIG)

    assert written
    assert mock_append.call_count == 1


@patch("world.events.append_world_events")
def test_cooldown_suppresses_recent_repeat(mock_append):
    bars = _calm_bars()
    bars.iloc[-1, bars.columns.get_loc("Volume")] = 50_000.0
    recent = bars.index[-1].to_pydatetime() - timedelta(minutes=5)

    with patch("world.events.latest_world_event_time", return_value=recent):
        written = record_salient_events("BTCUSDT", bars, CONFIG)

    assert written == []
    assert mock_append.call_count == 0


@patch("world.events.append_world_events")
@patch("world.events.latest_world_event_time", return_value=None)
def test_batch_dedupes_within_itself(mock_latest, mock_append):
    """Two candidate events of the same type inside one batch: only the
    first passes; the second is inside the first's cooldown."""
    bars = _calm_bars()
    vol_col = bars.columns.get_loc("Volume")
    bars.iloc[-2, vol_col] = 50_000.0
    bars.iloc[-1, vol_col] = 52_000.0

    written = record_salient_events("BTCUSDT", bars, CONFIG)

    volume_events = [e for e in written if e["event_type"] == "volume_anomaly"]
    assert len(volume_events) == 1
