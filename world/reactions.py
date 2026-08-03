"""Character reactions v0 (Sprint 12): the mapping from "what happened" to
"what the room shows". Deliberately a data table, not logic — Sprint 13's
personalities differ by *thresholds* over these same reactions, so the
descriptors stay neutral about who is reacting.

The registry invariant (every KNOWN_EVENT_TYPES member has an entry) is
enforced by test, mirroring the subset check in tests/unit/test_stream_events.py.
"""

_FALLBACK = ("neutral", "idle")

# event_type -> (mood, animation)
REACTIONS: dict[str, tuple[str, str]] = {
    "big_move": ("startled", "jolt"),
    "volatility_spike": ("anxious", "shake"),
    "gap_open": ("surprised", "hop"),
    "volume_anomaly": ("alert", "pulse"),
    "streak": ("focused", "lean"),
    "signal_resolved": ("resigned", "shrug"),
    "model_losing_streak": ("dejected", "slump"),
    "stream_started": ("relieved", "wave"),
    "stream_stopped": ("idle", "sleep"),
    "stream_dropped": ("alarmed", "flicker"),
    "trader_opened": ("decisive", "step"),
    "trader_closed": ("weighing", "turn"),
    "trader_milestone": ("proud", "cheer"),
    # director actions (Sprint 13) — the room acknowledges the camera + voice
    "scene_switched": ("attentive", "pan"),
    "commentary_spoken": ("speaking", "talk"),
    # broadcast lifecycle (Sprint 14) — "are we public?" is the stream's own
    # heartbeat, so it reads like the stream_* pair it parallels.
    "broadcast_created": ("attentive", "pan"),
    "broadcast_live": ("relieved", "wave"),
    "broadcast_ended": ("idle", "sleep"),
}

# Types whose meaning genuinely flips on payload content.
_SIGNAL_OUTCOMES = {
    "win": ("elated", "cheer"),
    "loss": ("dejected", "slump"),
}
_STREAK_DIRECTIONS = {
    "up": ("eager", "lean"),
    "down": ("grim", "lean"),
}


# Every animation name the registry can emit (Sprint 14, B1). The renderer
# implements one behaviour per name; this set is what the page is held to, so a
# new reaction can't silently fall back to standing still on a 24/7 stream.
ANIMATIONS: frozenset[str] = frozenset(
    animation
    for _mood, animation in (
        *REACTIONS.values(),
        *_SIGNAL_OUTCOMES.values(),
        *_STREAK_DIRECTIONS.values(),
        _FALLBACK,
    )
)

# Likewise for moods: the face has an expression per mood, and the fallback
# keeps an unknown mood renderable rather than blank.
MOODS: frozenset[str] = frozenset(
    mood
    for mood, _animation in (
        *REACTIONS.values(),
        *_SIGNAL_OUTCOMES.values(),
        *_STREAK_DIRECTIONS.values(),
        _FALLBACK,
    )
)


def reaction_for(event_type: str, tier: int, payload: dict | None = None) -> dict:
    """Mood/animation descriptor for one event at one severity tier."""
    payload = payload or {}
    mood, animation = REACTIONS.get(event_type, _FALLBACK)

    if event_type == "signal_resolved":
        mood, animation = _SIGNAL_OUTCOMES.get(
            payload.get("outcome"), (mood, animation)
        )
    elif event_type == "streak":
        mood, animation = _STREAK_DIRECTIONS.get(
            payload.get("direction"), (mood, animation)
        )

    tier = max(0, min(int(tier), 3))
    return {
        "mood": mood,
        "animation": animation,
        "intensity": round((tier + 1) / 4, 2),
        "duration_ms": 800 + 400 * tier,
    }


def attach_reactions(state: dict) -> dict:
    """Enrich a projected state with reaction descriptors. Returns a new dict;
    the canvas should never have to compute any of this itself."""
    enriched = dict(state)
    enriched["recent"] = [
        {
            **event,
            "reaction": reaction_for(
                event["event_type"], event.get("tier", 0), event.get("payload")
            ),
        }
        for event in state.get("recent", [])
    ]

    model = dict(state.get("model", {}))
    if model:
        outcome = model.get("streak_outcome")
        model["reaction"] = reaction_for(
            "signal_resolved",
            min(model.get("current_streak", 0), 3),
            {"outcome": outcome} if outcome else None,
        )
    enriched["model"] = model

    enriched["symbols"] = {
        symbol: {**data, "reaction": reaction_for("streak", data.get("tier", 0),
                                                  {"direction": _direction(data)})}
        for symbol, data in state.get("symbols", {}).items()
    }
    return enriched


def _direction(symbol_state: dict) -> str:
    return "up" if symbol_state.get("pressure", 0.0) >= 0 else "down"
