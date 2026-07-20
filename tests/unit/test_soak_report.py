"""Uptime math over stream_* world events — the log built this sprint IS the
measurement tool."""

from datetime import datetime, timezone

import pytest

from scripts.soak_report import compute_uptime


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
