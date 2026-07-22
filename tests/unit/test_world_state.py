"""The projection is the world's single source of truth for what the room
shows. Two properties matter more than any individual field: identical input
gives byte-identical output, and folding a log in chunks equals folding it
whole — that is what makes "refresh restores the same world" true."""

import json
from datetime import datetime, timedelta, timezone
from functools import reduce

import pytest

from world.state import (
    RECENT_LIMIT,
    empty_state,
    fold_event,
    project_state,
    severity_tier,
)

BASE = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _event(event_id, event_type, severity, payload=None, symbol="BTCUSDT", minute=0):
    return {
        "id": event_id,
        "occurred_at": (BASE + timedelta(minutes=minute)).isoformat(),
        "event_type": event_type,
        "symbol": symbol,
        "severity": severity,
        "payload": payload or {},
    }


@pytest.mark.parametrize(
    "event_type,severity,expected",
    [
        # Each rule's own trigger value must land on tier 0, or the room shows
        # "dramatic" for every routine firing.
        ("big_move", 4.0, 0),
        ("big_move", 13.92, 3),
        ("volatility_spike", 3.0, 0),
        ("gap_open", 1.0, 0),
        ("gap_open", 2.18, 1),
        ("volume_anomaly", 4.0, 0),
        ("volume_anomaly", 7.23, 2),
        ("streak", 7.0, 0),
        ("streak", 11.0, 1),
        ("signal_resolved", 0.2, 0),
        ("signal_resolved", 1.93, 3),
        ("model_losing_streak", 1.0, 0),
        ("stream_started", 1.0, 0),
        ("stream_stopped", 2.0, 1),
        ("stream_dropped", 5.0, 3),
    ],
)
def test_severity_tier_is_comparable_across_rules(event_type, severity, expected):
    assert severity_tier(event_type, severity) == expected


def test_severity_tier_unknown_type_uses_generic_scale():
    # A type with no cut points of its own (including trader_* in Task 9) falls
    # back rather than raising — an unrendered event is worse than a rough tier.
    assert severity_tier("unregistered_rule", 0.5) == 0
    assert severity_tier("unregistered_rule", 11.0) == 3


def test_tier_is_monotonic_in_severity():
    tiers = [severity_tier("big_move", s) for s in (4.0, 6.0, 8.0, 12.0)]
    assert tiers == sorted(tiers)


def test_projection_is_deterministic():
    events = [_event(2, "big_move", 6.0, {"return": 0.03}, minute=1),
              _event(1, "streak", 9.0, {"bars": 9, "direction": "up"})]
    first = project_state(events, now=BASE)
    second = project_state(events, now=BASE)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_projection_ignores_input_ordering():
    events = [_event(1, "big_move", 6.0, {"return": 0.03}),
              _event(2, "streak", 9.0, {"bars": 9, "direction": "up"}, minute=1)]
    forward = project_state(events, now=BASE)
    backward = project_state(list(reversed(events)), now=BASE)
    assert forward == backward


def test_fold_event_does_not_mutate_its_input():
    """Purity, asserted directly. The chunk test below CANNOT check this —
    reduce(f, xs+ys) == reduce(f, ys, reduce(f, xs)) is the left-fold law and
    holds for impure f too, as long as f is deterministic."""
    before = empty_state()
    snapshot = json.dumps(before, sort_keys=True)
    fold_event(before, _event(1, "big_move", 6.0, {"return": 0.03}))
    assert json.dumps(before, sort_keys=True) == snapshot


def test_folding_in_halves_equals_folding_whole():
    events = [
        _event(i, "big_move", 5.0 + i * 0.1, {"return": 0.01 * (-1) ** i}, minute=i)
        for i in range(1, 11)
    ]
    whole = reduce(fold_event, events, empty_state())
    halves = reduce(
        fold_event, events[5:], reduce(fold_event, events[:5], empty_state())
    )
    assert whole == halves


def test_model_record_counts_wins_and_losses():
    events = [
        _event(1, "signal_resolved", 0.8, {"outcome": "win", "realized_return": 0.01}),
        _event(2, "signal_resolved", 1.2, {"outcome": "loss", "realized_return": -0.02},
               minute=1),
        _event(3, "signal_resolved", 1.4, {"outcome": "loss", "realized_return": -0.03},
               minute=2),
    ]
    model = project_state(events, now=BASE)["model"]
    assert model["wins"] == 1
    assert model["losses"] == 2
    assert model["hit_rate"] == pytest.approx(1 / 3)
    assert model["current_streak"] == 2
    assert model["streak_outcome"] == "loss"


def test_stream_state_tracks_last_lifecycle_event():
    events = [
        _event(1, "stream_started", 1.0, symbol=None),
        _event(2, "stream_dropped", 5.0, symbol=None, minute=5),
    ]
    stream = project_state(events, now=BASE)["stream"]
    assert stream["state"] == "down"
    assert stream["drops"] == 1


def test_symbol_mood_reflects_direction_and_agitation():
    up = project_state(
        [_event(1, "streak", 10.0, {"bars": 10, "direction": "up"})], now=BASE
    )["symbols"]["BTCUSDT"]
    down = project_state(
        [_event(1, "streak", 10.0, {"bars": 10, "direction": "down"})], now=BASE
    )["symbols"]["BTCUSDT"]
    assert up["pressure"] > 0 > down["pressure"]
    assert up["mood"] == "bullish"
    assert down["mood"] == "bearish"


def test_recent_events_are_newest_first_and_capped():
    events = [_event(i, "big_move", 5.0, {"return": 0.01}, minute=i) for i in range(30)]
    recent = project_state(events, now=BASE)["recent"]
    assert len(recent) == RECENT_LIMIT
    assert recent[0]["id"] == 29
    assert all("tier" in e for e in recent)


def test_empty_log_projects_an_empty_but_valid_state():
    state = project_state([], now=BASE)
    assert state["event_count"] == 0
    assert state["symbols"] == {}
    assert state["model"]["resolved"] == 0


def test_history_records_worst_loss_and_longest_streak():
    events = [
        _event(1, "signal_resolved", 1.2,
               {"outcome": "loss", "realized_return": -0.02}),
        _event(2, "signal_resolved", 1.9,
               {"outcome": "loss", "realized_return": -0.08}, minute=1),
        _event(3, "streak", 9.0, {"bars": 9, "direction": "up"}, minute=2),
        _event(4, "streak", 14.0, {"bars": 14, "direction": "down"}, minute=3),
    ]
    history = project_state(events, now=BASE)["history"]
    assert history["worst_loss"]["realized_return"] == -0.08
    assert history["longest_streak"]["bars"] == 14
    assert history["total_events"] == 4


def test_history_accumulates_downtime_across_outages():
    events = [
        _event(1, "stream_started", 1.0, symbol=None),
        _event(2, "stream_dropped", 5.0, symbol=None, minute=10),
        _event(3, "stream_started", 1.0, symbol=None, minute=25),
        _event(4, "stream_stopped", 2.0, symbol=None, minute=40),
    ]
    history = project_state(events, now=BASE)["history"]
    assert history["downtime_seconds"] == pytest.approx(15 * 60)
    assert history["outages"] == 1


def test_history_first_seen_is_the_oldest_event():
    events = [_event(2, "big_move", 5.0, {"return": 0.01}, minute=30),
              _event(1, "big_move", 5.0, {"return": 0.01})]
    assert project_state(events, now=BASE)["history"]["first_seen"] == BASE.isoformat()


def test_a_month_of_history_differs_materially_from_a_fresh_log():
    """The month-away property: a world that has lived is not a world that
    just booted, even when the recent window looks the same."""
    aged = [
        _event(i, "signal_resolved", 1.5,
               {"outcome": "loss", "realized_return": -0.01}, minute=i * 60)
        for i in range(1, 200)
    ]
    # One bad day early on, long since out of the recent window. A world that
    # has lived still carries it; a freshly-booted one has never seen it.
    aged[10]["payload"]["realized_return"] = -0.35
    fresh = aged[-3:]
    aged_state = project_state(aged, now=BASE)
    fresh_state = project_state(fresh, now=BASE)

    assert (
        aged_state["history"]["total_events"] > fresh_state["history"]["total_events"]
    )
    assert (
        aged_state["history"]["worst_loss"]["realized_return"]
        < fresh_state["history"]["worst_loss"]["realized_return"]
    )
    assert aged_state["recent"] != []
