"""Uptime math over stream_* world events — the log built this sprint IS the
measurement tool."""

from datetime import datetime, timezone

import pytest

from scripts.soak_report import compute_director_activity, compute_uptime


def _ev(minute, event_type, payload=None):
    return {
        "occurred_at": datetime(
            2026, 7, 21, 0, minute, tzinfo=timezone.utc
        ).isoformat(),
        "event_type": event_type,
        "payload": payload or {},
    }


WINDOW = (
    datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 7, 21, 1, 0, tzinfo=timezone.utc),
)


def test_no_events_means_unknown_full_uptime():
    result = compute_uptime([], *WINDOW)
    assert result["uptime_pct"] == 100.0 and result["outages"] == []


def test_single_outage_and_recovery():
    events = [
        _ev(0, "stream_started"),
        _ev(30, "stream_dropped", {"reason": "obs_unreachable"}),
        _ev(36, "stream_started", {"recovery_seconds": 360}),
    ]
    result = compute_uptime(events, *WINDOW)
    assert result["downtime_seconds"] == 360.0
    assert result["uptime_pct"] == 90.0
    assert len(result["outages"]) == 1
    assert result["outages"][0]["reason"] == "obs_unreachable"


def test_unrecovered_outage_runs_to_window_end():
    events = [_ev(0, "stream_started"), _ev(50, "stream_dropped")]
    result = compute_uptime(events, *WINDOW)
    assert result["downtime_seconds"] == 600.0
    assert result["uptime_pct"] == pytest.approx(83.33, abs=0.01)


def test_dropped_frames_reason_is_not_downtime():
    # dropped_frames means degraded, not down — the stream stayed live
    events = [
        _ev(0, "stream_started"),
        _ev(30, "stream_dropped", {"reason": "dropped_frames"}),
    ]
    result = compute_uptime(events, *WINDOW)
    assert result["uptime_pct"] == 100.0
    assert result["outages"][0]["duration_seconds"] == 0


def test_degraded_event_does_not_hide_a_real_outage_that_follows():
    """A dropped_frames degradation must not block tracking of a real outage
    that fires before the next stream_started — otherwise the real downtime is
    silently deleted and uptime is overstated (a sacred number)."""
    events = [
        _ev(0, "stream_started"),
        _ev(20, "stream_dropped", {"reason": "dropped_frames"}),  # degraded, live
        _ev(30, "stream_dropped", {"reason": "obs_unreachable"}),  # real outage
        _ev(42, "stream_started", {"recovery_seconds": 720}),
    ]
    result = compute_uptime(events, *WINDOW)
    assert result["downtime_seconds"] == 720.0  # the real 30->42 outage, not 0
    assert result["uptime_pct"] == 80.0
    reasons = {o["reason"] for o in result["outages"]}
    assert reasons == {"dropped_frames", "obs_unreachable"}


def test_director_activity_counts_and_rates():
    events = [
        _ev(5, "scene_switched", {"scene": "world-focus"}),
        _ev(10, "commentary_spoken", {"character": "optimist", "text": "!"}),
        _ev(20, "commentary_spoken", {"character": "optimist", "text": "!!"}),
        _ev(30, "commentary_spoken", {"character": "anxious", "text": "?"}),
    ]
    a = compute_director_activity(events, window_hours=1.0)
    assert a["switches"] == 1 and a["lines"] == 3
    assert a["lines_per_hour"] == 3.0 and a["switches_per_hour"] == 1.0
    assert a["by_character"] == {"optimist": 2, "anxious": 1}


def test_director_activity_ignores_non_director_events():
    events = [_ev(1, "big_move", {}), _ev(2, "stream_started", {})]
    a = compute_director_activity(events, window_hours=2.0)
    assert a["lines"] == 0 and a["switches"] == 0
    assert a["by_character"] == {}
