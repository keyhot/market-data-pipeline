"""Uptime math over stream_* world events — the log built this sprint IS the
measurement tool."""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stream.soak_report import (
    _broadcast_events_for_window,
    _fetch_report_events,
    compute_broadcast_uptime,
    compute_director_activity,
    compute_uptime,
    gap_callouts,
    window_bounds,
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


def test_report_fetch_is_per_type_so_a_busy_window_cant_drop_rows():
    """KI-014: one `since` query capped at N returns the NEWEST N, so a busy
    24h window silently loses the oldest in-window rows — the stream_started
    that opened it, or an early broadcast_live. Both understate uptime on the
    report that is Sprint 11's acceptance evidence. Fetch per type instead."""
    asked = []

    def fetch(limit=50, event_type=None, since=None):
        asked.append(event_type)
        return [_ev(1, event_type)] if event_type == "stream_started" else []

    events, truncated = _fetch_report_events(fetch, *WINDOW)
    assert "stream_started" in asked and "broadcast_live" in asked
    assert "commentary_spoken" in asked  # director activity shares the fetch
    assert [e["event_type"] for e in events] == ["stream_started"]
    assert truncated == []


def test_report_fetch_reports_truncation_instead_of_a_quiet_number():
    """If a type does hit the cap the report must say so — a silently
    understated uptime is worse than a loud 'this number is incomplete'."""

    def fetch(limit=50, event_type=None, since=None):
        return [_ev(1, event_type)] * limit if event_type == "commentary_spoken" else []

    _, truncated = _fetch_report_events(fetch, *WINDOW, per_type_limit=50)
    assert truncated == ["commentary_spoken"]


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


# --- KI-036: `Outages: 6` next to 38 seconds of downtime -------------------
#
# Degraded rows are appended with zero duration on purpose, so their reason
# stays visible in the table. The headline printed len(outages), so the A6
# report read "Outages: 6" beside "99.96% uptime (38s down)": one real outage,
# one network_congested, four reconnect notices.

def test_the_outage_headline_counts_downtime_not_degraded_notices():
    events = [
        _ev(0, "stream_started"),
        _ev(10, "stream_reconnected"),
        _ev(20, "stream_dropped", {"reason": "network_congested"}),
        _ev(30, "stream_dropped", {"reason": "obs_unreachable"}),
        _ev(31, "stream_started"),
    ]
    result = compute_uptime(events, *WINDOW)
    assert result["outage_count"] == 1
    # Every row still reaches the table — the reasons are the evidence.
    assert len(result["outages"]) == 3
    assert result["degraded_count"] == 2


# --- KI-037: 100.0% public uptime across 38 seconds of dark ----------------
#
# YouTube keeps a broadcast in `live` state through a brief ingest loss, so the
# metric the runbook calls "could anyone actually watch" sailed through the
# soak's 38s gap untouched. The report only guarded the reverse direction
# (OBS pushing into a broadcast nobody could watch), so this was structurally
# invisible. The guard is in seconds, not percentage points: 38s over 24h is
# 0.04pp and would clear any pp threshold worth setting.

def test_public_uptime_is_called_out_when_it_ignores_a_dark_gap():
    report = {"uptime_pct": 99.96, "downtime_seconds": 38.0}
    broadcast = {"uptime_pct": 100.0, "measured": True}
    notes = gap_callouts(report, broadcast)
    assert len(notes) == 1
    assert "38s" in notes[0] and "100.0%" in notes[0]


def test_no_dark_gap_callout_when_nothing_went_down():
    report = {"uptime_pct": 100.0, "downtime_seconds": 0.0}
    broadcast = {"uptime_pct": 100.0, "measured": True}
    assert gap_callouts(report, broadcast) == []


def test_the_encoder_up_but_unwatchable_gap_is_still_called_out():
    """The 2026-07-27 failure: ~4h of pushing into no public broadcast."""
    report = {"uptime_pct": 100.0, "downtime_seconds": 0.0}
    broadcast = {"uptime_pct": 83.0, "measured": True}
    notes = gap_callouts(report, broadcast)
    assert len(notes) == 1 and "17.00 percentage points" in notes[0]


def test_unmeasured_broadcast_makes_no_claim_in_either_direction():
    report = {"uptime_pct": 99.96, "downtime_seconds": 38.0}
    broadcast = {"uptime_pct": 0.0, "measured": False}
    assert gap_callouts(report, broadcast) == []


# --- Re-running a report over a window that has already closed -------------
#
# The acceptance evidence for a soak is a report over *the soak's* window. With
# only `--hours N` counting back from now, re-running one hour later silently
# scores a different window and the numbers can't be compared to the ones the
# sprint note quotes.

def test_the_window_ends_now_by_default():
    now = datetime(2026, 8, 22, 3, 43, tzinfo=timezone.utc)
    start, end = window_bounds(24.0, None, now)
    assert end == now and start == now - timedelta(hours=24)


def test_an_explicit_ending_reproduces_a_window_that_has_closed():
    now = datetime(2026, 8, 22, 3, 43, tzinfo=timezone.utc)
    start, end = window_bounds(24.0, "2026-08-22T04:30:00+02:00", now)
    assert end == datetime(2026, 8, 22, 2, 30, tzinfo=timezone.utc)
    assert start == datetime(2026, 8, 21, 2, 30, tzinfo=timezone.utc)


@pytest.fixture
def host_tz():
    """Set the process timezone as the C library sees it, for one test.

    `window_bounds` reads an offset-less `--ending` through the OS (naive
    `.astimezone()`), so the only way to vary what it means is the `TZ`
    variable plus `time.tzset()`. monkeypatch alone restores the variable but
    never re-runs tzset, so the zone would leak into every later test; the
    explicit tzset after the context closes is what puts it back, and the
    final assert makes a leak fail here rather than somewhere unrelated.
    """
    before = (os.environ.get("TZ"), time.tzname)
    with pytest.MonkeyPatch.context() as mp:

        def use(name):
            mp.setenv("TZ", name)
            time.tzset()
            # An OS without that zone's data falls back to UTC silently,
            # which would turn every case below back into the tautology.
            july = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
            assert july.astimezone().utcoffset() == (
                july.astimezone(ZoneInfo(name)).utcoffset()
            ), f"TZ={name} did not take effect"

        yield use
    time.tzset()
    assert (os.environ.get("TZ"), time.tzname) == before


# (host zone, --ending as typed, the UTC instant it must mean). Every
# expectation is a literal: computing it with the same `.astimezone` as the
# code made the old test pass on a UTC machine, where local time *is* UTC, so
# a regression that read the string as UTC went unnoticed on CI.
_LOCAL_WALL_CLOCK = [
    ("Europe/Warsaw", "2026-08-22T04:30:00", (2026, 8, 22, 2, 30)),  # CEST +2
    ("Europe/Warsaw", "2026-12-22T04:30:00", (2026, 12, 22, 3, 30)),  # CET +1
    ("Asia/Kolkata", "2026-08-22T04:30:00", (2026, 8, 21, 23, 0)),  # +5:30
    ("America/Los_Angeles", "2026-08-22T04:30:00", (2026, 8, 22, 11, 30)),  # -7
    ("America/Los_Angeles", "2026-12-22T04:30:00", (2026, 12, 22, 12, 30)),  # -8
]


@pytest.mark.parametrize(
    "zone, ending, utc",
    _LOCAL_WALL_CLOCK,
    ids=[f"{z}-{e[:10]}" for z, e, _ in _LOCAL_WALL_CLOCK],
)
def test_an_ending_without_an_offset_is_local_wall_clock_not_utc(
    host_tz, zone, ending, utc
):
    """A human types the time their clock showed. Reading it as UTC would slide
    the window by the machine's offset — one to eight hours depending on the
    zone and the season — and quietly report on the wrong hours.

    Run under a zone that is not UTC, so it means something on a UTC CI runner
    too, and in both halves of the year so a fixed offset cannot pass."""
    host_tz(zone)
    now = datetime(2026, 8, 22, 3, 43, tzinfo=timezone.utc)
    start, end = window_bounds(24.0, ending, now)
    assert end == datetime(*utc, tzinfo=timezone.utc)
    assert start == end - timedelta(hours=24)


def test_an_ending_with_an_offset_ignores_the_machine_zone(host_tz):
    """The other half of the contract: a stated offset is the whole answer, so
    the same string means the same instant wherever the report is run."""
    host_tz("Asia/Kolkata")
    now = datetime(2026, 8, 22, 3, 43, tzinfo=timezone.utc)
    for ending in ("2026-08-22T04:30:00+02:00", "2026-08-22T02:30:00Z"):
        _, end = window_bounds(24.0, ending, now)
        assert end == datetime(2026, 8, 22, 2, 30, tzinfo=timezone.utc)


# A wall clock that does not exist (the DST gap) or exists twice (the fold)
# looks like any other to a human typing `--ending`, and `fromisoformat` can
# only produce fold=0. Europe/Warsaw springs forward Sun 2026-03-29 02:00 ->
# 03:00 and falls back Sun 2026-10-25 03:00 -> 02:00. Today nothing raises:
# an ambiguous time is its *first* occurrence (CEST +2; the second, 01:30Z,
# cannot be typed at all) on every CPython, but a missing one depends on the
# interpreter. 3.11 (CI) applies the offset from *after* the jump (CEST +2),
# so 02:30 is 00:30Z, an hour that reads 01:30 on the Warsaw clock; 3.12+
# applies the one from *before* it (CET +1), so 02:30 is 01:30Z, which reads
# 03:30. All literals.
# (--ending as typed, the UTC instant it maps to, why)
_DST_EDGE_ENDINGS = [
    ("2026-03-29T01:59:59", (2026, 3, 29, 0, 59, 59), "last second before the gap"),
    ("2026-03-29T03:00:00", (2026, 3, 29, 1, 0, 0), "after the gap, CEST +2"),
    ("2026-10-25T01:59:59", (2026, 10, 24, 23, 59, 59), "before the fold, CEST +2"),
    ("2026-10-25T02:00:00", (2026, 10, 25, 0, 0, 0), "fold start, first, CEST +2"),
    ("2026-10-25T02:30:00", (2026, 10, 25, 0, 30, 0), "inside the fold, first, CEST"),
    ("2026-10-25T02:59:59", (2026, 10, 25, 0, 59, 59), "last of the fold, first, CEST"),
    ("2026-10-25T03:00:00", (2026, 10, 25, 2, 0, 0), "after the fold, CET +1"),
]

# (--ending as typed, UTC instant on CPython 3.11, on CPython 3.12+)
_DST_GAP_ENDINGS = [
    ("2026-03-29T02:00:00", (2026, 3, 29, 0, 0, 0), (2026, 3, 29, 1, 0, 0)),
    ("2026-03-29T02:30:00", (2026, 3, 29, 0, 30, 0), (2026, 3, 29, 1, 30, 0)),
    ("2026-03-29T02:59:59", (2026, 3, 29, 0, 59, 59), (2026, 3, 29, 1, 59, 59)),
]


def _end_under_warsaw(host_tz, ending):
    host_tz("Europe/Warsaw")
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)  # not today
    start, end = window_bounds(24.0, ending, now)
    assert start == end - timedelta(hours=24)
    assert end.utcoffset() == timedelta(0)  # UTC, not just the same instant
    return end


@pytest.mark.parametrize(
    "ending, utc, why",
    _DST_EDGE_ENDINGS,
    ids=[e for e, _, _ in _DST_EDGE_ENDINGS],
)
def test_an_ending_next_to_or_in_a_dst_fold_maps_to_a_fixed_instant(
    host_tz, ending, utc, why
):
    """Pins current behaviour, not a judgement that it is right: 02:30 on
    2026-10-25 happens twice and is silently the first (CEST) one."""
    end = _end_under_warsaw(host_tz, ending)
    assert end == datetime(*utc, tzinfo=timezone.utc), why


@pytest.mark.parametrize(
    "ending, py311, py312",
    _DST_GAP_ENDINGS,
    ids=[e for e, _, _ in _DST_GAP_ENDINGS],
)
def test_an_ending_in_a_dst_gap_is_accepted_and_mapped_by_the_interpreter(
    host_tz, ending, py311, py312
):
    """Pins current behaviour, not a judgement that it is right: 02:30 on
    2026-03-29 does not exist on a Warsaw clock, nothing raises, and the answer
    is an hour off the neighbouring valid times in a direction that depends on
    the Python minor version (see the table above)."""
    end = _end_under_warsaw(host_tz, ending)
    utc = py312 if sys.version_info >= (3, 12) else py311
    assert end == datetime(*utc, tzinfo=timezone.utc)


def test_report_fetch_drops_events_from_after_the_window_closed():
    """`since` has no upper bound, so a window that has already closed also
    pulls in everything that happened *since* it. Found by re-running A6's
    window: the report said 143 director lines where the database, asked for
    the same 24 hours, said 140 — the extra three were spoken after it."""
    inside = _ev(30, "commentary_spoken")
    after = _ev_at(WINDOW[1] + timedelta(minutes=5), "commentary_spoken")

    def fetch(limit=50, event_type=None, since=None):
        return [after, inside] if event_type == "commentary_spoken" else []

    events, _ = _fetch_report_events(fetch, *WINDOW)
    assert events == [inside]
