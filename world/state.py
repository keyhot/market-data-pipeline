"""World-state projection (Sprint 12): the append-only world_events log
folded into what the room currently looks like. Pure — no database access,
no clock of its own — because "refresh restores the same world" is only true
if the same log always produces the same state.

Severity normalization lives here rather than in the canvas: every salience
rule scores in its own unit (sigmas, z-scores, bar counts, multiples of a
threshold), so a renderer mapping raw severity to visual weight would be
permanently dominated by big_move.
"""

from datetime import datetime, timezone

TIER_NAMES = ("routine", "notable", "major", "dramatic")
RECENT_LIMIT = 12

# Per-rule cut points onto tiers 0..3. Each rule's own trigger threshold must
# land on tier 0 — a rule firing at its minimum is by definition routine.
_TIER_CUTS: dict[str, tuple[float, float, float]] = {
    "big_move": (5.0, 7.0, 10.0),            # sigmas; fires at 4.0
    "volatility_spike": (4.0, 6.0, 9.0),     # sigma ratio; fires at 3.0
    "gap_open": (1.5, 2.5, 4.0),             # multiples of the gap threshold
    "volume_anomaly": (5.0, 7.0, 10.0),      # volume z-score; fires at 4.0
    "streak": (9.0, 12.0, 15.0),             # consecutive bars; fires at 7
    "signal_resolved": (0.6, 1.2, 1.8),      # confidence, doubled on a loss
    "model_losing_streak": (1.34, 2.0, 3.0), # streak/3: 4 losses, 6, 9
    "stream_started": (2.0, 3.0, 4.0),
    "stream_stopped": (2.0, 3.0, 4.0),
    "stream_dropped": (2.0, 3.0, 4.0),
}
_GENERIC_CUTS = (2.0, 5.0, 10.0)

# Pressure/agitation decay per event, so the room reflects the recent past
# without a clock. Chosen so ~7 quiet events halve an impression.
_DECAY = 0.9
_DIRECTIONLESS = frozenset({"volatility_spike", "volume_anomaly", "gap_open"})


def severity_tier(event_type: str, severity: float) -> int:
    """Map a rule-specific severity onto the shared 0..3 scale."""
    cuts = _TIER_CUTS.get(event_type, _GENERIC_CUTS)
    return sum(1 for cut in cuts if severity >= cut)


def empty_state() -> dict:
    return {
        "event_count": 0,
        "symbols": {},
        "model": {
            "wins": 0,
            "losses": 0,
            "resolved": 0,
            "hit_rate": None,
            "current_streak": 0,
            "streak_outcome": None,
        },
        "stream": {"state": "unknown", "drops": 0, "last_transition": None},
        "recent": [],
    }


def _empty_symbol() -> dict:
    return {
        "pressure": 0.0,
        "agitation": 0.0,
        "mood": "calm",
        "tier": 0,
        "event_counts": {},
        "last_event": None,
    }


def _pressure_delta(event_type: str, tier: int, payload: dict) -> float:
    weight = float(tier + 1)
    if event_type == "streak":
        return weight if payload.get("direction") == "up" else -weight
    if event_type == "big_move":
        return weight if float(payload.get("return", 0.0)) >= 0 else -weight
    return 0.0


def _mood(pressure: float, agitation: float) -> str:
    if agitation >= 6.0:
        return "panicked"
    if pressure >= 1.5:
        return "bullish"
    if pressure <= -1.5:
        return "bearish"
    return "calm"


def fold_event(state: dict, event: dict) -> dict:
    """Absorb one event. Pure: returns a new state, mutates nothing."""
    new = {
        "event_count": state["event_count"] + 1,
        "symbols": dict(state["symbols"]),
        "model": dict(state["model"]),
        "stream": dict(state["stream"]),
        "recent": state["recent"],
    }
    etype = event["event_type"]
    payload = event.get("payload") or {}
    tier = severity_tier(etype, float(event["severity"]))

    symbol = event.get("symbol")
    if symbol:
        prev = new["symbols"].get(symbol, _empty_symbol())
        pressure = prev["pressure"] * _DECAY + _pressure_delta(etype, tier, payload)
        agitation = prev["agitation"] * _DECAY + (
            float(tier + 1) if etype in _DIRECTIONLESS or etype == "big_move" else 0.0
        )
        counts = dict(prev["event_counts"])
        counts[etype] = counts.get(etype, 0) + 1
        new["symbols"][symbol] = {
            "pressure": round(pressure, 4),
            "agitation": round(agitation, 4),
            "mood": _mood(pressure, agitation),
            "tier": tier,
            "event_counts": counts,
            "last_event": {
                "event_type": etype,
                "occurred_at": event["occurred_at"],
                "tier": tier,
            },
        }

    if etype == "signal_resolved":
        outcome = payload.get("outcome")
        model = new["model"]
        model["resolved"] += 1
        if outcome == "win":
            model["wins"] += 1
        elif outcome == "loss":
            model["losses"] += 1
        if outcome in ("win", "loss"):
            if model["streak_outcome"] == outcome:
                model["current_streak"] += 1
            else:
                model["streak_outcome"] = outcome
                model["current_streak"] = 1

    if etype in ("stream_started", "stream_stopped", "stream_dropped"):
        stream = new["stream"]
        stream["state"] = "live" if etype == "stream_started" else "down"
        stream["last_transition"] = event["occurred_at"]
        if etype == "stream_dropped":
            stream["drops"] += 1

    # Newest-first, capped. Slicing a fresh list keeps fold_event pure.
    entry = {
        "id": event.get("id"),
        "occurred_at": event["occurred_at"],
        "event_type": etype,
        "symbol": symbol,
        "severity": float(event["severity"]),
        "tier": tier,
        "tier_name": TIER_NAMES[tier],
        "payload": payload,
    }
    new["recent"] = [entry, *state["recent"]][:RECENT_LIMIT]
    return new


def project_state(events: list[dict], now: datetime | None = None) -> dict:
    """Fold a world_events page (any order) into current world state."""
    ordered = sorted(events, key=lambda e: (e["occurred_at"], e.get("id") or 0))
    state = empty_state()
    for event in ordered:
        state = fold_event(state, event)

    resolved = state["model"]["resolved"]
    state["model"]["hit_rate"] = (
        state["model"]["wins"] / resolved if resolved else None
    )
    state["generated_at"] = (now or datetime.now(timezone.utc)).isoformat()
    return state
