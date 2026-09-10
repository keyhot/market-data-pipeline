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


def test_a_future_timestamp_is_stale_not_brand_new():
    # A DB clock ahead of the API's would otherwise clamp to age 0.0 and read
    # healthy — the one false-healthy path in this module (MINOR-6), and
    # KI-057 recreated inside the field built to catch it.
    out = world_liveness(NOW + timedelta(hours=2), NOW + timedelta(hours=2), NOW)
    assert out["stale"] is True
    # Not hidden behind a 0.0 clamp — a legible negative number instead.
    assert out["signal_age_s"] == -7200
    assert out["event_age_s"] == -7200


def test_a_future_signal_alone_is_stale_even_with_a_fresh_event():
    # Pins signal_age < 0 independently: with the event well inside the
    # threshold, only the signal's own future stamp can be what trips
    # `stale` here. Deleting just this OR-term (leaving event_age < 0 in
    # place) would let this case slip through as stale: False, since
    # nothing else in the predicate fires for it.
    out = world_liveness(NOW + timedelta(hours=1), NOW - timedelta(seconds=10), NOW)
    assert out["stale"] is True


def test_a_future_event_alone_is_stale_even_with_a_fresh_signal():
    # The mirror of the test above: pins event_age < 0 independently.
    out = world_liveness(NOW - timedelta(seconds=10), NOW + timedelta(hours=1), NOW)
    assert out["stale"] is True


def test_just_under_the_default_threshold_is_not_stale():
    # Brackets stale_after_s from below: a 16x-looser default (86400) would
    # still pass this, but it pins the boundary tightly together with the
    # test below (MINOR-1 — nothing previously pinned 5400 to within two
    # days of itself).
    out = world_liveness(NOW - timedelta(minutes=89), NOW - timedelta(minutes=89), NOW)
    assert out["stale"] is False


def test_just_over_the_default_threshold_is_stale():
    out = world_liveness(NOW - timedelta(minutes=91), NOW - timedelta(minutes=91), NOW)
    assert out["stale"] is True
