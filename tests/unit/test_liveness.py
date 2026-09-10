from datetime import datetime, timedelta, timezone

from world.liveness import world_liveness

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def test_a_running_world_is_not_stale():
    out = world_liveness(NOW - timedelta(minutes=3), NOW - timedelta(minutes=1), NOW)
    assert out["stale"] is False
    assert out["signal_age_s"] == 180


def test_the_three_day_silence_is_stale():
    # The KI-057 window, to the day. `price_bars` was healthy throughout, so
    # this is the only field that would have said anything.
    out = world_liveness(NOW - timedelta(days=3), NOW - timedelta(days=3), NOW)
    assert out["stale"] is True


def test_a_world_that_has_never_run_is_stale_rather_than_an_error():
    # A fresh database is not a crash. It is also not healthy.
    out = world_liveness(None, None, NOW)
    assert out["stale"] is True
    assert out["signal_age_s"] is None


def test_a_live_event_stream_does_not_excuse_a_dead_model():
    # The exact shape of KI-057: stream_dropped events kept arriving while
    # the model was silent, so ANY-event freshness would have read healthy.
    out = world_liveness(NOW - timedelta(days=2), NOW - timedelta(seconds=30), NOW)
    assert out["stale"] is True
