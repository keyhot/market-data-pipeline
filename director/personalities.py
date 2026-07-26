"""Personalities as threshold policies (Sprint 13): frozen dataclasses shaped
like world.salience.SalienceConfig. Same event stream, different thresholds ->
genuinely different behaviour. No scripted dialogue trees.

- **optimist** — cheerful, low bar; celebrates wins and shrugs off losses.
- **statistician** — dry; only speaks at a high tier, neutral on outcome.
- **anxious** — frets over volatility and losses; kept *sparingly* loud via a
  high min_tier (the register: escalation spice, not the baseline voice).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Personality:
    name: str
    voice: str  # Piper voice id (Task 6)
    min_tier: int
    event_types: frozenset
    phrase_character: str
    # signal_resolved outcomes this character reacts to; None = both.
    outcomes: frozenset | None = None


def reacts_to(personality: Personality, event: dict, tier: int) -> bool:
    """Whether this personality reacts to this event at this tier — pure."""
    if event["event_type"] not in personality.event_types:
        return False
    if tier < personality.min_tier:
        return False
    if event["event_type"] == "signal_resolved" and personality.outcomes is not None:
        outcome = (event.get("payload") or {}).get("outcome")
        if outcome not in personality.outcomes:
            return False
    return True


PERSONALITIES: tuple[Personality, ...] = (
    Personality(
        name="optimist",
        voice="en_US-amy-medium",
        min_tier=1,
        event_types=frozenset(
            {
                "big_move",
                "volatility_spike",
                "gap_open",
                "volume_anomaly",
                "streak",
                "signal_resolved",
                "stream_started",
                "stream_stopped",
                "trader_opened",
                "trader_closed",
                "trader_milestone",
            }
        ),
        phrase_character="optimist",
        outcomes=frozenset({"win"}),  # shrugs off losses
    ),
    Personality(
        name="statistician",
        voice="en_US-ryan-medium",
        min_tier=2,  # only speaks when it's genuinely significant
        event_types=frozenset(
            {
                "big_move",
                "volatility_spike",
                "volume_anomaly",
                "streak",
                "signal_resolved",
                "model_losing_streak",
                "trader_closed",
            }
        ),
        phrase_character="statistician",
        outcomes=None,  # neutral: win or loss, it's a data point
    ),
    Personality(
        name="anxious",
        voice="en_US-lessac-medium",
        min_tier=2,  # register: escalation spice, sparingly — not the baseline
        event_types=frozenset(
            {
                "volatility_spike",
                "gap_open",
                "streak",
                "signal_resolved",
                "model_losing_streak",
                "stream_dropped",
                "stream_stopped",
            }
        ),
        phrase_character="anxious",
        outcomes=frozenset({"loss"}),  # frets over losses, not wins
    ),
)
