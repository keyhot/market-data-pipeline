"""Personalities are frozen threshold policies over one event stream: same
events, different thresholds -> genuinely different behaviour."""

import dataclasses
from datetime import datetime, timezone

from director.commentary import lines_for_tick
from director.personalities import PERSONALITIES, Personality, reacts_to
from director.policy import DirectorConfig, DirectorState

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def test_three_frozen_personalities():
    assert len(PERSONALITIES) >= 3
    for p in PERSONALITIES:
        assert isinstance(p, Personality)
        assert dataclasses.is_dataclass(p)
        # frozen -> assignment raises
        try:
            p.min_tier = 99
            raised = False
        except dataclasses.FrozenInstanceError:
            raised = True
        assert raised


def test_statistician_stays_silent_below_its_high_tier():
    stat = next(p for p in PERSONALITIES if p.name == "statistician")
    ev = {"event_type": "signal_resolved", "payload": {"outcome": "loss"}}
    assert reacts_to(stat, ev, 1) is False
    assert reacts_to(stat, ev, 3) is True


def test_optimist_reacts_to_wins_and_shrugs_at_losses():
    opt = next(p for p in PERSONALITIES if p.name == "optimist")
    win = {"event_type": "signal_resolved", "payload": {"outcome": "win"}}
    loss = {"event_type": "signal_resolved", "payload": {"outcome": "loss"}}
    assert reacts_to(opt, win, 1) is True
    assert reacts_to(opt, loss, 1) is False


def test_same_stream_different_behavior():
    """The point of the sprint: identical events, measurably different reactions."""
    events = [
        ({"event_type": "volatility_spike", "payload": {}}, 1),
        ({"event_type": "signal_resolved", "payload": {"outcome": "win"}}, 3),
    ]
    reactions = {
        p.name: tuple(reacts_to(p, e, t) for e, t in events) for p in PERSONALITIES
    }
    # No two personalities react identically to the whole stream.
    assert len(set(reactions.values())) == len(reactions)


def test_lines_for_tick_wires_personalities_to_phrases():
    ds = DirectorState(current_scene="chart-focus", last_switch=NOW)
    state = {
        "recent": [
            {
                "id": 10,
                "event_type": "signal_resolved",
                "tier": 3,
                "symbol": "BTCUSDT",
                "payload": {"outcome": "win"},
            }
        ]
    }
    lines = lines_for_tick(state, ds, NOW, DirectorConfig())
    chars = {ln["character"] for ln in lines}
    assert "optimist" in chars and "statistician" in chars  # both react to a tier-3 win
    assert "anxious" not in chars  # anxious frets over losses, not wins
    # deterministic (seeded per event+personality)
    assert lines_for_tick(state, ds, NOW, DirectorConfig()) == lines


def test_lines_for_tick_silent_on_already_seen_events():
    ds = DirectorState(
        current_scene="chart-focus", last_switch=NOW, last_seen_event_id=10
    )
    state = {
        "recent": [
            {
                "id": 10,
                "event_type": "signal_resolved",
                "tier": 3,
                "symbol": "BTCUSDT",
                "payload": {"outcome": "win"},
            }
        ]
    }
    assert lines_for_tick(state, ds, NOW, DirectorConfig()) == []
