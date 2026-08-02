"""Uptime math over stream_* world events — the log built this sprint IS the
measurement tool."""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.soak_report import (
    _broadcast_events_for_window,
    compute_broadcast_uptime,
    compute_director_activity,
    compute_uptime,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_soak_report_runs_as_the_documented_script(tmp_path):
    """KI-007: running `scripts/soak_report.py` must put the repo root on
    sys.path so `storage` imports. Execute the module's top level exactly as a
    script does — its sys.path insert runs — but skip main() (and thus the DB)
    via run_name, then prove `storage` is now importable.

    Run from a *neutral* cwd with empty PYTHONPATH: otherwise `python -c` puts
    cwd (the repo root) on the path as sys.path[0] and `storage` imports for the
    wrong reason. From tmp_path, only soak_report's own insert can add the repo
    root — run_path adds scripts/, never the repo root — so this stays RED
    without the fix (proven: a no-op script in its place raises ModuleNotFound)."""
    env = {**os.environ, "PYTHONPATH": ""}
    script = _REPO_ROOT / "scripts" / "soak_report.py"
    probe = (
        "import runpy; "
        f"runpy.run_path({str(script)!r}, run_name='_probe'); "
        "import storage.postgres_store  # importable only if repo root was added"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


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


def test_stream_stopped_counts_as_downtime_until_restart():
    # KI-008: the watchdog now records an unexpected live->inactive transition as
    # stream_stopped. That span is downtime just like a drop — otherwise the
    # soak report would overstate uptime (state.py already counts it; this keeps
    # the two reports honest together).
    events = [
        _ev(0, "stream_started"),
        _ev(10, "stream_stopped", {"reason": "output_inactive"}),
        _ev(25, "stream_started", {"recovery_seconds": 900}),
    ]
    result = compute_uptime(events, *WINDOW)
    assert result["downtime_seconds"] == 900.0
    assert result["uptime_pct"] == 75.0
    assert [o["reason"] for o in result["outages"]] == ["output_inactive"]


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


def _ev_at(when: datetime, event_type, payload=None):
    """An event at an arbitrary instant — `_ev` can only place events inside the
    report window's hour, and public-broadcast uptime turns on events *outside*
    it."""
    return {
        "occurred_at": when.isoformat(),
        "event_type": event_type,
        "payload": payload or {},
    }


def test_broadcast_uptime_counts_live_to_ended():
    events = [_ev(10, "broadcast_live"), _ev(40, "broadcast_ended")]
    result = compute_broadcast_uptime(events, *WINDOW)
    assert result["live_seconds"] == 30 * 60
    assert result["uptime_pct"] == 50.0


def test_broadcast_live_to_window_end_when_unclosed():
    result = compute_broadcast_uptime([_ev(30, "broadcast_live")], *WINDOW)
    assert result["live_seconds"] == 30 * 60  # still live -> runs to window_end
    assert result["uptime_pct"] == 50.0


def test_broadcast_live_before_the_window_covers_the_whole_window():
    """The sprint's own design decision is ONE long-lived broadcast
    (enableAutoStop=false), so during a 24h soak the `broadcast_live` that took
    the stream public can sit days behind window_start and NOTHING lands inside
    the window. A fold that only sees in-window events reports 0% public uptime
    next to ~100% OBS uptime — the exact inversion of the truth this report
    exists to tell, and the number that closes Sprint 11."""
    window_start, window_end = WINDOW
    events = [_ev_at(window_start - timedelta(days=2), "broadcast_live")]
    result = compute_broadcast_uptime(events, window_start, window_end)
    assert result["live_seconds"] == 60 * 60
    assert result["uptime_pct"] == 100.0


def test_broadcast_ended_before_the_window_is_not_uptime():
    """The mirror of the case above: a broadcast that ended before the window
    must not leak live time into it."""
    window_start, window_end = WINDOW
    events = [
        _ev_at(window_start - timedelta(hours=3), "broadcast_live"),
        _ev_at(window_start - timedelta(hours=1), "broadcast_ended"),
    ]
    result = compute_broadcast_uptime(events, window_start, window_end)
    assert result["live_seconds"] == 0
    assert result["uptime_pct"] == 0.0


def test_no_broadcast_events_reports_zero_not_full():
    """Opposite default from compute_uptime on purpose: OBS downtime is proved
    by events, public uptime is proved by events. No evidence of being live is
    not evidence of being live — truthfulness over a flattering number."""
    result = compute_broadcast_uptime([], *WINDOW)
    assert result["live_seconds"] == 0
    assert result["uptime_pct"] == 0.0


def test_zero_uptime_is_flagged_unmeasured_when_nothing_was_observed():
    """0% has two very different causes: the broadcast really wasn't public,
    or nothing is writing broadcast_* events (broadcast_manager not running,
    OAuth missing). Reporting the second as 'encoder up, stream not watchable'
    is a diagnosis the data doesn't support — and a warning that cries wolf
    gets ignored by the time it's real."""
    assert compute_broadcast_uptime([], *WINDOW)["measured"] is False
    observed = compute_broadcast_uptime([_ev(10, "broadcast_ended")], *WINDOW)
    assert observed["measured"] is True  # events seen; genuinely 0% live
    assert observed["uptime_pct"] == 0.0


def test_broadcast_events_for_window_supplies_the_prior_live_event():
    """The helper that keeps the acceptance number from inverting: the last
    live/ended BEFORE the window has to reach the fold."""
    window_start, window_end = WINDOW
    prior_live = _ev_at(window_start - timedelta(days=3), "broadcast_live")

    def fetch(limit=50, event_type=None):
        return {"broadcast_live": [prior_live], "broadcast_ended": []}[event_type]

    events = _broadcast_events_for_window(
        fetch, [_ev(5, "stream_started")], window_start
    )
    assert [e["event_type"] for e in events] == ["broadcast_live"]
    assert compute_broadcast_uptime(events, window_start, window_end)["uptime_pct"] == (
        100.0
    )


def test_broadcast_events_for_window_ignores_events_inside_the_window_as_prior():
    """`fetch` is newest-first and unfiltered by time, so its newest row may be
    INSIDE the window — it's already in the window events and must not be
    double-counted as prior state."""
    window_start, _ = WINDOW
    inside = _ev(20, "broadcast_live")

    def fetch(limit=50, event_type=None):
        return {"broadcast_live": [inside], "broadcast_ended": []}[event_type]

    events = _broadcast_events_for_window(fetch, [inside], window_start)
    assert events == [inside]  # once, not twice


def test_broadcast_restart_sums_both_live_spans():
    events = [
        _ev(0, "broadcast_live"),
        _ev(15, "broadcast_ended"),
        _ev(45, "broadcast_created"),  # created is not live time
        _ev(50, "broadcast_live"),
    ]
    result = compute_broadcast_uptime(events, *WINDOW)
    assert result["live_seconds"] == (15 + 10) * 60
    assert len(result["spans"]) == 2


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
