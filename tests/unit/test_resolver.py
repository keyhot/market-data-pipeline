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


def _severity(signal, entry, realized):
    _, _, mock_append = _run(signal, entry, realized)
    return mock_append.call_args[0][0][0]["severity"]


def test_severity_is_the_realized_move_in_bps():
    # KI-041: |p-0.5| was measured to carry no information about being right
    # (corr -0.0135 with the realized move over 7,078 on-air resolutions), so
    # the room's loudness is keyed to what the market actually did.
    assert _severity(_signal("up", 0.8), 100.0, 100.25) == pytest.approx(25.0)
    assert _severity(_signal("up", 0.8), 100.0, 99.9) == pytest.approx(10.0)


@pytest.mark.parametrize("probability", [0.55, 0.95])
@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("realized", [99.7, 100.3])
def test_severity_ignores_confidence_and_outcome(probability, direction, realized):
    # The same move is the same size whoever called it and however sure the
    # model claimed to be; re-adding the probability term or the old x2 loss
    # multiplier makes this red. Wins and losses measured the same size on air
    # (13.3 vs 13.6 bps), so doubling losses doubled nothing real.
    reference = _severity(_signal("up", 0.75), 100.0, 100.3)
    assert _severity(_signal(direction, probability), 100.0, realized) == (
        pytest.approx(reference)
    )


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
