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

# "Degraded" is live-but-impaired: recorded and reacted to, but never downtime.
# This predicate is THE definition, and it lives here — in the pure fold module
# — because both folds that need it are pure and `world/stream_events.py` (the
# writer, which owns the vocabulary) imports Postgres. Two copies of this rule
# used to exist, hardcoded as `reason == "dropped_frames"` in this file and in
# scripts/soak_report.py; KI-019 is what happens when two surfaces each carry
# their own copy of "how big is this", and KI-021 was about to add a third.
# Note what is NOT here: `content_unreachable` (KI-024). A stream whose pages
# cannot reach the API is pushing pixels at nobody — that is a content outage
# and it accrues downtime, which is the entire point of recording it.
# KI-032: `dropped_frames` is the HISTORICAL name and must stay accepted
# forever. `world_events` is append-only, so every row written before the
# rename keeps it; dropping it here would reclassify every past degradation as
# downtime and silently restate the uptime history — fixing a truthfulness bug
# by corrupting the record it is measured against. The two names that replace
# it say which fault they are: `encoder_overloaded` is output_skipped_frames
# (the encoder couldn't keep up), `network_congested` is output_congestion
# (what OBS actually derives from RTMP drops).
STREAM_DEGRADED_REASONS = frozenset(
    {"dropped_frames", "encoder_overloaded", "network_congested"}
)


def is_degraded_stream_event(event_type: str, payload: dict | None) -> bool:
    """True when a stream_* event means impaired-but-live, so it accrues no
    downtime. An RTMP reconnect (KI-021) is the type-level case: OBS never
    stopped, it re-dialled the ingest in ~2.5s and kept `outputActive` true.
    """
    if event_type == "stream_reconnected":
        return True
    return (
        event_type == "stream_dropped"
        and (payload or {}).get("reason") in STREAM_DEGRADED_REASONS
    )

# Per-rule cut points onto tiers 0..3. Each rule's own trigger threshold must
# land on tier 0 — a rule firing at its minimum is by definition routine.
_TIER_CUTS: dict[str, tuple[float, float, float]] = {
    "big_move": (5.0, 7.0, 10.0),            # sigmas; fires at 4.0
    "volatility_spike": (4.0, 6.0, 9.0),     # sigma ratio; fires at 3.0
    "gap_open": (1.5, 2.5, 4.0),             # multiples of the gap threshold
    "volume_anomaly": (5.0, 7.0, 10.0),      # volume z-score; fires at 4.0
    "streak": (9.0, 12.0, 15.0),             # consecutive bars; fires at 7
    "signal_resolved": (0.6, 1.2, 1.8),      # confidence, doubled on a loss
    "model_losing_streak": (1.34, 2.0, 3.0), # streak/3; tiers at 5, 6, 9 losses
    "stream_started": (2.0, 3.0, 4.0),
    "stream_stopped": (2.0, 3.0, 4.0),
    "stream_dropped": (2.0, 3.0, 4.0),
    # Its own cuts, not the stream family's shared (2,3,4). Severity 3.0 keeps
    # the log's ordering honest (a reconnect is a bigger deal than a routine
    # start), but on the shared cuts that severity renders **tier 2 — "major"**,
    # i.e. a 2.5s blink that already healed itself reacting LOUDER in the room
    # than the stream actually going down (stream_stopped, tier 1). Per-rule
    # cuts exist for exactly this: severity is what happened, the tier is how
    # hard to react, and they are allowed to disagree. "A blink, not an alarm."
    "stream_reconnected": (3.0, 4.0, 5.0),
}
_GENERIC_CUTS = (2.0, 5.0, 10.0)

# Pressure/agitation decay per event, so the room reflects the recent past
# without a clock. Chosen so ~7 quiet events halve an impression.
_DECAY = 0.9
_DIRECTIONLESS = frozenset({"volatility_spike", "volume_anomaly", "gap_open"})


GENERIC_TIER_CUTS = _GENERIC_CUTS


def tier_cuts() -> dict[str, tuple[float, float, float]]:
    """The per-rule thresholds, for surfaces that compute a tier themselves.

    The renderer and the events rail both need one, and severities are
    rule-specific — a 1.6 ``signal_resolved`` is tier 2 where a 1.6 ``big_move``
    is tier 0. A page carrying its own absolute scale silently pinned the most
    frequent event in the world to tier 0, so the scale is injected from here.
    """
    return dict(_TIER_CUTS)


def severity_tier(event_type: str, severity: float) -> int:
    """Map a rule-specific severity onto the shared 0..3 scale."""
    cuts = _TIER_CUTS.get(event_type, _GENERIC_CUTS)
    return sum(1 for cut in cuts if severity >= cut)


def tier_of_js() -> str:
    """`severity_tier`, as the JavaScript the pages are served with.

    Injecting the *cuts* was only half of KI-019: the three-line function that
    reads them was then written out twice, byte-identical, in `world.html` and
    `overlay_events.html`. Two copies of a rule is how the first version drifted
    from the server, and a page is free to edit its own copy. There is one
    definition now, and it is this one — the same lookup-then-count as above,
    including the fall back to the generic scale for an unlisted rule.
    """
    return (
        "function tierOf(eventType, severity) {\n"
        "      const cuts = TIER_CUTS.cuts[eventType] ?? TIER_CUTS.generic;\n"
        "      return cuts.reduce("
        "(tier, cut) => tier + (severity >= cut ? 1 : 0), 0);\n"
        "    }"
    )


def _parse(timestamp: str | datetime) -> datetime:
    if isinstance(timestamp, datetime):
        return timestamp
    return datetime.fromisoformat(timestamp)


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
        "stream": {"state": "unknown", "drops": 0, "reconnects": 0,
                   "last_transition": None},
        "trader": None,
        "recent": [],
        "history": {
            "total_events": 0,
            "first_seen": None,
            "worst_loss": None,
            "longest_streak": None,
            "biggest_move": None,
            "downtime_seconds": 0.0,
            "outages": 0,
        },
        "_down_since": None,
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
        "trader": dict(state["trader"]) if state["trader"] else None,
        "recent": state["recent"],
        "history": dict(state["history"]),
        "_down_since": state["_down_since"],
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

    if etype in ("stream_started", "stream_stopped", "stream_dropped",
                 "stream_reconnected"):
        stream = new["stream"]
        # KI-021: a reconnect is not a transition to "down". OBS kept
        # `outputActive` true throughout, so no stream_started follows to clear
        # it — putting it in the "down" branch would leave /world showing a dead
        # stream forever, on a stream that never stopped.
        if etype != "stream_reconnected":
            stream["state"] = "live" if etype == "stream_started" else "down"
            stream["last_transition"] = event["occurred_at"]
        if etype == "stream_dropped":
            stream["drops"] += 1
        elif etype == "stream_reconnected":
            stream["reconnects"] += 1

    if etype in ("trader_opened", "trader_closed", "trader_milestone"):
        trader = new["trader"] or {
            "open_trades": 0, "profit_pct": 0.0, "mood": "decisive",
            "last_event": None,
        }
        if etype == "trader_opened":
            trader["open_trades"] += 1
        elif etype == "trader_closed":
            trader["open_trades"] = max(0, trader["open_trades"] - 1)
        if "profit_pct" in payload:
            trader["profit_pct"] = round(float(payload["profit_pct"]), 4)
        # Mood comes from the reaction table's own vocabulary so the canvas
        # palette needs no trader-specific colours beyond those in Step 4.
        trader["mood"] = (
            "proud" if trader["profit_pct"] > 0
            else "weighing" if trader["profit_pct"] < 0
            else "decisive"
        )
        trader["last_event"] = {
            "event_type": etype, "occurred_at": event["occurred_at"]
        }
        new["trader"] = trader

    history = new["history"]
    history["total_events"] += 1
    if history["first_seen"] is None:
        history["first_seen"] = event["occurred_at"]

    if etype == "signal_resolved" and payload.get("outcome") == "loss":
        realized = float(payload.get("realized_return", 0.0))
        worst = history["worst_loss"]
        if worst is None or realized < worst["realized_return"]:
            history["worst_loss"] = {
                "symbol": symbol,
                "realized_return": realized,
                "occurred_at": event["occurred_at"],
            }
    elif etype == "streak":
        bars = int(payload.get("bars", 0))
        longest = history["longest_streak"]
        if longest is None or bars > longest["bars"]:
            history["longest_streak"] = {
                "symbol": symbol,
                "bars": bars,
                "direction": payload.get("direction"),
                "occurred_at": event["occurred_at"],
            }
    elif etype == "big_move":
        sigmas = float(payload.get("sigmas", event["severity"]))
        biggest = history["biggest_move"]
        if biggest is None or sigmas > biggest["sigmas"]:
            history["biggest_move"] = {
                "symbol": symbol,
                "sigmas": sigmas,
                "occurred_at": event["occurred_at"],
            }

    # Downtime accrues between a stop/drop and the next start. An unclosed
    # outage stays open rather than being guessed at — the log is the record.
    if etype in ("stream_stopped", "stream_dropped", "stream_reconnected"):
        # Degraded means the stream stayed LIVE — the watchdog records it and
        # deliberately does not restart, so no stream_started follows. Booking
        # that live span as downtime would fabricate an outage. One definition,
        # shared with scripts/soak_report.py: is_degraded_stream_event.
        if not is_degraded_stream_event(etype, payload):
            opening = new["_down_since"] is None
            if opening:
                new["_down_since"] = event["occurred_at"]
            # Count one outage per downtime period opened by a drop, deduped —
            # consecutive drops before recovery are one outage, matching
            # soak_report.py's `if open_outage is None`.
            if etype == "stream_dropped" and opening:
                history["outages"] += 1
    elif etype == "stream_started" and new["_down_since"] is not None:
        down = _parse(new["_down_since"])
        history["downtime_seconds"] = round(
            history["downtime_seconds"] + (_parse(event["occurred_at"]) - down)
            .total_seconds(),
            3,
        )
        new["_down_since"] = None

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
    state.pop("_down_since", None)
    # History is folded over the events the CALLER supplied, not the whole
    # table — /world/state passes limit=500. Recording the window keeps
    # "worst loss" honest: it means worst-in-window, not worst-ever.
    state["history"]["window"] = len(ordered)
    state["generated_at"] = (now or datetime.now(timezone.utc)).isoformat()
    return state
