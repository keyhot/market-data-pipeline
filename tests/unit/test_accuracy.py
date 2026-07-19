from unittest.mock import patch

from world.salience import SalienceConfig, detect_model_events


def _accuracy(outcomes, window=50):
    """Build a get_signal_accuracy-shaped dict from newest-first outcomes."""
    wins = outcomes.count("win")
    streak = 0
    for outcome in outcomes:
        if outcome != outcomes[0]:
            break
        streak += 1
    return {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "window": window,
        "resolved": len(outcomes),
        "wins": wins,
        "losses": len(outcomes) - wins,
        "hit_rate": wins / len(outcomes) if outcomes else None,
        "current_streak": streak,
        "streak_outcome": outcomes[0] if outcomes else None,
    }


def test_accuracy_reader_math():
    """Pin the streak/hit-rate math via the store function itself (mocked
    connection would duplicate SQL; instead verify against seeded rows in
    the integration suite — here we pin the derived-shape contract)."""
    acc = _accuracy(["loss", "loss", "loss", "win", "loss"])
    assert acc["hit_rate"] == 0.2
    assert acc["current_streak"] == 3
    assert acc["streak_outcome"] == "loss"


def test_losing_streak_fires_at_threshold():
    config = SalienceConfig(model_losing_streak=3)

    events = detect_model_events("btcusdt", _accuracy(["loss"] * 3), config)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "model_losing_streak"
    assert event["symbol"] == "BTCUSDT"
    assert event["severity"] == 1.0
    assert event["payload"]["streak"] == 3


def test_longer_streak_more_severe():
    config = SalienceConfig(model_losing_streak=3)

    three = detect_model_events("BTCUSDT", _accuracy(["loss"] * 3), config)
    six = detect_model_events("BTCUSDT", _accuracy(["loss"] * 6), config)

    assert six[0]["severity"] > three[0]["severity"]


def test_short_streak_or_winning_fires_nothing():
    config = SalienceConfig(model_losing_streak=3)

    assert detect_model_events("BTCUSDT", _accuracy(["loss"] * 2), config) == []
    assert detect_model_events("BTCUSDT", _accuracy(["win"] * 5), config) == []
    assert detect_model_events("BTCUSDT", _accuracy([]), config) == []


@patch("world.events.append_world_events")
@patch("world.events.latest_world_event_time", return_value=None)
def test_record_model_events_appends(mock_latest, mock_append):
    from world.events import record_model_events

    with patch(
        "storage.postgres_store.get_signal_accuracy",
        return_value=_accuracy(["loss"] * 4),
    ):
        written = record_model_events("BTCUSDT")

    assert len(written) == 1
    mock_append.assert_called_once()
