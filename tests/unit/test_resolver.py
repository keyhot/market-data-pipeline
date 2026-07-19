from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from world.resolver import resolve_pending

SIGNAL_TS = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
TARGET_TS = SIGNAL_TS + timedelta(minutes=15)
NOW = TARGET_TS + timedelta(minutes=1)


def _signal(direction="up", probability=0.8):
    return {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "signal_timestamp": SIGNAL_TS,
        "model_version": "test-v0",
        "horizon_bars": 15,
        "direction": direction,
        "probability": probability,
    }


def _run(signal, entry_close, realized_close, resolve_returns=1):
    closes = {SIGNAL_TS: entry_close, TARGET_TS: realized_close}

    with (
        patch("world.resolver.get_unresolved_signals", return_value=[signal]),
        patch(
            "world.resolver.get_bar_close",
            side_effect=lambda s, i, ts: closes.get(ts),
        ),
        patch(
            "world.resolver.resolve_signal", return_value=resolve_returns
        ) as mock_resolve,
        patch("world.resolver.append_world_events") as mock_append,
    ):
        resolutions = resolve_pending(now=NOW)
    return resolutions, mock_resolve, mock_append


@pytest.mark.parametrize(
    "direction,went_up,expected",
    [
        ("up", True, "win"),
        ("up", False, "loss"),
        ("down", False, "win"),
        ("down", True, "loss"),
    ],
)
def test_all_direction_outcome_combos(direction, went_up, expected):
    realized = 110.0 if went_up else 90.0

    resolutions, _, mock_append = _run(_signal(direction), 100.0, realized)

    assert resolutions[0]["outcome"] == expected
    event = mock_append.call_args[0][0][0]
    assert event["event_type"] == "signal_resolved"
    assert event["payload"]["outcome"] == expected


def test_confident_loss_more_severe_than_hesitant_loss_and_any_win():
    _, _, confident_loss = _run(_signal("up", probability=0.9), 100.0, 90.0)
    _, _, hesitant_loss = _run(_signal("up", probability=0.55), 100.0, 90.0)
    _, _, confident_win = _run(_signal("up", probability=0.9), 100.0, 110.0)

    sev_confident_loss = confident_loss.call_args[0][0][0]["severity"]
    sev_hesitant_loss = hesitant_loss.call_args[0][0][0]["severity"]
    sev_confident_win = confident_win.call_args[0][0][0]["severity"]

    assert sev_confident_loss > sev_hesitant_loss
    assert sev_confident_loss > sev_confident_win  # losses weighted double


def test_horizon_not_reached_leaves_pending():
    with (
        patch("world.resolver.get_unresolved_signals", return_value=[_signal()]),
        patch("world.resolver.resolve_signal") as mock_resolve,
        patch("world.resolver.append_world_events") as mock_append,
    ):
        resolutions = resolve_pending(now=SIGNAL_TS + timedelta(minutes=5))

    assert resolutions == []
    mock_resolve.assert_not_called()
    mock_append.assert_not_called()


def test_missing_realized_bar_leaves_pending():
    resolutions, mock_resolve, mock_append = _run(_signal(), 100.0, None)

    assert resolutions == []
    mock_resolve.assert_not_called()
    mock_append.assert_not_called()


def test_concurrent_resolution_emits_no_second_event():
    resolutions, _, mock_append = _run(
        _signal(), 100.0, 110.0, resolve_returns=0
    )

    assert resolutions == []
    mock_append.assert_not_called()


def test_unknown_interval_left_pending():
    signal = {**_signal(), "interval": "3h"}
    with (
        patch("world.resolver.get_unresolved_signals", return_value=[signal]),
        patch("world.resolver.resolve_signal") as mock_resolve,
        patch("world.resolver.append_world_events") as mock_append,
    ):
        resolutions = resolve_pending(now=NOW)

    assert resolutions == []
    mock_resolve.assert_not_called()
    mock_append.assert_not_called()
