"""Reactions are the only place event semantics become visuals. The registry
invariant is the point: adding a salience rule without a reaction must fail a
test rather than silently render nothing on a 24/7 stream."""

import pytest

from world.reactions import REACTIONS, attach_reactions, reaction_for
from world.salience import KNOWN_EVENT_TYPES


def test_every_known_event_type_has_a_reaction():
    missing = sorted(KNOWN_EVENT_TYPES - set(REACTIONS))
    assert missing == [], f"event types with no reaction: {missing}"


def test_unknown_event_type_falls_back_instead_of_raising():
    reaction = reaction_for("some_future_rule", 2)
    assert reaction["mood"] == "neutral"
    assert reaction["animation"] == "idle"


def test_intensity_and_duration_scale_with_tier():
    low = reaction_for("big_move", 0)
    high = reaction_for("big_move", 3)
    assert high["intensity"] > low["intensity"]
    assert high["duration_ms"] > low["duration_ms"]


def test_signal_resolved_splits_on_outcome():
    win = reaction_for("signal_resolved", 2, {"outcome": "win"})
    loss = reaction_for("signal_resolved", 2, {"outcome": "loss"})
    assert win["mood"] != loss["mood"]
    assert win["mood"] == "elated"
    assert loss["mood"] == "dejected"


def test_streak_splits_on_direction():
    up = reaction_for("streak", 1, {"direction": "up"})
    down = reaction_for("streak", 1, {"direction": "down"})
    assert up["mood"] == "eager"
    assert down["mood"] == "grim"


def test_missing_payload_never_raises():
    for event_type in sorted(KNOWN_EVENT_TYPES):
        assert reaction_for(event_type, 0) is not None


@pytest.mark.parametrize("tier", [0, 1, 2, 3])
def test_intensity_is_bounded(tier):
    assert 0.0 < reaction_for("big_move", tier)["intensity"] <= 1.0


def test_attach_reactions_enriches_recent_without_mutating_input():
    state = {
        "recent": [
            {
                "id": 1,
                "event_type": "signal_resolved",
                "tier": 3,
                "payload": {"outcome": "loss"},
            }
        ],
        "symbols": {"BTCUSDT": {"mood": "bearish", "tier": 2}},
        "model": {"streak_outcome": "loss", "current_streak": 4},
    }
    enriched = attach_reactions(state)
    assert enriched["recent"][0]["reaction"]["mood"] == "dejected"
    assert "reaction" not in state["recent"][0]
    assert enriched["model"]["reaction"]["animation"] == "slump"


# --- B1: the animation vocabulary the renderer must implement ---


def test_animations_covers_every_animation_the_registry_can_emit():
    """B1 makes the named animations real. The renderer implements one
    behaviour per name, so this set is the contract between reactions.py and
    the canvas — a reaction whose animation isn't here would render as standing
    still, on a stream nobody is watching at 3am."""
    from world.reactions import ANIMATIONS

    emitted = set()
    for event_type in KNOWN_EVENT_TYPES:
        for tier in range(4):
            for payload in ({}, {"outcome": "win"}, {"outcome": "loss"},
                            {"direction": "up"}, {"direction": "down"}):
                emitted.add(reaction_for(event_type, tier, payload)["animation"])
    emitted.add(reaction_for("some_future_rule", 0)["animation"])  # fallback
    assert emitted <= ANIMATIONS, f"animations with no entry: {emitted - ANIMATIONS}"


def test_moods_covers_every_mood_the_registry_can_emit():
    from world.reactions import MOODS

    emitted = set()
    for event_type in KNOWN_EVENT_TYPES:
        for payload in ({}, {"outcome": "win"}, {"outcome": "loss"},
                        {"direction": "up"}, {"direction": "down"}):
            emitted.add(reaction_for(event_type, 1, payload)["mood"])
    emitted.add(reaction_for("some_future_rule", 0)["mood"])
    assert emitted <= MOODS, f"moods with no entry: {emitted - MOODS}"
