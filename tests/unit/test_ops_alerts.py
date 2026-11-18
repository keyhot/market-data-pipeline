"""The alert path (KI-049 / KI-057 / KI-086): five durable failure records had
no path to a human. These pin the pure half — what counts as failing, and when
an email goes out — with no network, no clock and no mail server."""

from datetime import datetime, timedelta, timezone

import pytest

from alerts.rules import (
    Check,
    check_backup,
    check_broadcast,
    check_world,
    decide,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


# --- backup (KI-049) --------------------------------------------------------

def _backup(stamp="2026-09-26T01-10-01Z", ok=True, error=None):
    # The exact shape backup_postgres.sh writes — stamp uses dashes in the time.
    return {
        "stamp": stamp,
        "ok": ok,
        "bytes": 18996233,
        "path": f"backups/market_data_{stamp}.dump",
        "trigger": "cron",
        "error": error,
    }


def test_a_verified_backup_from_last_night_is_healthy():
    assert check_backup(_backup(), NOW).failing is False


def test_a_failed_backup_is_failing_and_says_why():
    check = check_backup(
        _backup(ok=False, error="service postgres is not running"), NOW
    )
    assert check.failing is True
    assert "postgres is not running" in check.summary


def test_no_status_file_is_failing():
    # A backup that never ran leaves no status at all — the 2026-08-18 night.
    assert check_backup(None, NOW).failing is True


def test_an_ok_backup_that_is_too_old_is_failing():
    # Nightly at 01:10Z: 26h is a missed night, not a late one.
    taken = datetime(2026, 9, 25, 1, 10, 1, tzinfo=timezone.utc)
    status = _backup(stamp="2026-09-25T01-10-01Z")
    assert check_backup(status, taken + timedelta(hours=26)).failing is False
    assert check_backup(status, taken + timedelta(hours=28)).failing is True


def test_an_unparseable_stamp_is_failing_not_a_crash():
    assert check_backup(_backup(stamp="garbage"), NOW).failing is True


# --- world (KI-057) ---------------------------------------------------------

def _health(world, reason=None):
    return {
        "status": 200,
        "data": {"world": world, "world_unavailable_reason": reason},
    }


def test_a_fresh_world_is_healthy():
    world = {"signal_age_s": 120.0, "event_age_s": 60.0, "stale": False}
    assert check_world(_health(world)).failing is False


def test_a_stale_world_is_failing_with_its_ages():
    world = {"signal_age_s": 3 * 86400.0, "event_age_s": 7200.0, "stale": True}
    check = check_world(_health(world))
    assert check.failing is True
    assert "72.0h" in check.summary


def test_a_world_nobody_could_read_is_failing_and_names_the_reason():
    # "we could not look" is not "we looked and it was fine" (KI-057's pair).
    check = check_world(_health(None, "not_connected"))
    assert check.failing is True
    assert "not_connected" in check.summary


def test_an_unreachable_api_is_failing():
    assert check_world(None, error="Connection refused").failing is True


# --- broadcast (KI-086) -----------------------------------------------------

def _bstatus(updated=NOW, refused=0, tick_failures=0):
    return {
        "updated_at": updated.isoformat(),
        "refused_transitions": refused,
        "consecutive_tick_failures": tick_failures,
        "lifecycle": "live",
        "broadcast_id": "abc",
    }


def test_a_quiet_live_broadcast_is_healthy():
    assert check_broadcast(_bstatus(), NOW).failing is False


def test_a_refusal_streak_at_the_log_threshold_is_failing():
    from broadcast.service import REFUSED_TRANSITION_ALERT_AT

    n = REFUSED_TRANSITION_ALERT_AT
    assert check_broadcast(_bstatus(refused=n), NOW).failing is True
    # The benign settle (51 on 08-18 and 09-15) must not email anyone.
    assert check_broadcast(_bstatus(refused=n - 1), NOW).failing is False


def test_a_manager_that_stopped_ticking_is_failing():
    # Crashed, fail-closed on a missing OAuth secret, or hung.
    old = _bstatus(updated=NOW - timedelta(minutes=11))
    assert check_broadcast(old, NOW).failing is True
    assert check_broadcast(None, NOW).failing is True


def test_a_manager_whose_every_tick_fails_is_failing():
    assert check_broadcast(_bstatus(tick_failures=20), NOW).failing is True
    assert check_broadcast(_bstatus(tick_failures=19), NOW).failing is False


# --- decide: when mail goes out --------------------------------------------

def _run(ticks, state=None, start=NOW, step=timedelta(minutes=5), **kw):
    """Feed a sequence of per-tick check lists; return every tick's alerts."""
    state = state or {}
    out = []
    for i, checks in enumerate(ticks):
        alerts, state = decide(checks, state, start + i * step, **kw)
        out.append(alerts)
    return out, state


BAD = [Check("world", True, "world stale")]
GOOD = [Check("world", False, "ok")]


def test_one_failing_tick_sends_nothing():
    # Every `up -d --build api` makes /health unreachable for a tick or two.
    out, _ = _run([BAD, BAD, GOOD])
    assert out == [[], [], []]


def test_a_confirmed_failure_fires_once_then_stays_quiet():
    out, _ = _run([BAD] * 6)
    kinds = [[a.kind for a in tick] for tick in out]
    assert kinds == [[], [], ["firing"], [], [], []]


def test_a_firing_check_is_reminded_daily():
    out, _ = _run([BAD] * 3 + [BAD] * 300)  # 5-min ticks, > 24h
    kinds = [a.kind for tick in out for a in tick]
    assert kinds.count("firing") == 1
    assert kinds.count("reminder") == 1


def test_recovery_sends_resolved_once():
    out, _ = _run([BAD] * 3 + [GOOD] * 3)
    kinds = [[a.kind for a in tick] for tick in out]
    assert kinds[3] == ["resolved"]
    assert kinds[4:] == [[], []]


def test_a_check_that_never_fired_resolves_silently():
    out, _ = _run([BAD, BAD, GOOD])
    assert all(tick == [] for tick in out)


def test_a_gap_between_runs_is_reported_after_the_fact():
    # KI-088: the host slept and so did every alerter on it. The first run
    # after waking says how long nobody was looking.
    _, state = _run([GOOD])
    alerts, _ = decide(GOOD, state, NOW + timedelta(hours=60))
    assert [a.kind for a in alerts] == ["gap"]
    assert "60.0h" in alerts[0].summary


def test_the_first_run_ever_is_not_a_gap():
    alerts, _ = decide(GOOD, {}, NOW)
    assert alerts == []


def test_decide_does_not_mutate_the_state_it_was_given():
    # The runner keeps the OLD state when delivery fails, so the same alert is
    # retried next tick — that only works if decide never touched it.
    _, state = _run([BAD, BAD])
    snapshot = repr(state)
    decide(BAD, state, NOW + timedelta(minutes=15))
    assert repr(state) == snapshot


@pytest.mark.parametrize("name", ["backup", "world", "broadcast"])
def test_checks_are_tracked_independently(name):
    other = "world" if name != "world" else "backup"
    ticks = [[Check(name, True, "x"), Check(other, False, "ok")]] * 3
    out, _ = _run(ticks)
    assert [(a.kind, a.check) for a in out[2]] == [("firing", name)]
