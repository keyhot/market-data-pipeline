"""Scene selection (Sprint 13): a pure policy over the world state.

Two register-critical pieces: the **dwell guard** (a burst of events must not
flap scenes — mutation-checked) and **decay-to-home** (when the world goes
quiet, settle back to the calm home scene rather than sticking wherever the last
event left the camera). Keyed on the normalized severity *tier*, not raw
severity, so `gap_open` and `big_move` are comparable.
"""

# Which scene each event class foregrounds.
_MARKET_TYPES = frozenset(
    {"big_move", "volatility_spike", "gap_open", "volume_anomaly", "streak"}
)
_MODEL_TYPES = frozenset(
    {
        "signal_resolved",
        "model_losing_streak",
        "trader_opened",
        "trader_closed",
        "trader_milestone",
    }
)

_SCENE_FOR_INTENT = {
    "market": "chart-focus",
    "model": "world-focus",
    "event": "event-focus",
}
_SWITCH_TIER = 2  # only tier >= this is worth interrupting the current scene


def _desired_scene(state):
    """The scene the most salient recent event points to, or None if quiet."""
    for event in state.get("recent", []):
        if event.get("tier", 0) < _SWITCH_TIER:
            continue
        if event["event_type"] in _MARKET_TYPES:
            intent = "market"
        elif event["event_type"] in _MODEL_TYPES:
            intent = "model"
        else:
            intent = "event"
        # recent is newest-first, so the first qualifying event wins.
        return _SCENE_FOR_INTENT[intent]
    return None


def choose_scene(state, dir_state, now, config):
    desired = _desired_scene(state)
    dwell = (now - dir_state.last_switch).total_seconds()
    if desired is None:
        # Nothing salient. Decay back to the calm home scene once we've lingered
        # away from it long enough — the "swell, then settle" register. Don't
        # snap home instantly, or a lull right after a swell looks twitchy.
        if (
            dir_state.current_scene != config.home_scene
            and dwell >= config.return_to_home_seconds
        ):
            return config.home_scene
        return dir_state.current_scene
    if desired == dir_state.current_scene:
        return dir_state.current_scene
    if dwell < config.min_dwell_seconds:
        return dir_state.current_scene  # hold — dwell not elapsed (no flapping)
    return desired
