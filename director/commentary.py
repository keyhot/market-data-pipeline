"""Phrase-bank commentary (Sprint 13): deterministic line selection with
anti-repetition. No LLM. ``line_for`` picks one line for a character reacting to
one event at one tier; ``lines_for_tick`` (wired to the personalities in Task 5)
turns a world-state tick into the lines to speak.

Truthfulness: numbers come only from the event payload, via ``str.format`` over
``payload`` + ``symbol``. A template that references a missing key degrades to
its literal text rather than raising.
"""

from director.phrases import PHRASES


def _render(template: str, event: dict) -> str:
    try:
        return template.format(
            symbol=event.get("symbol", "the market"),
            **(event.get("payload") or {}),
        )
    except (KeyError, ValueError, IndexError):
        return template  # missing key / bad spec -> show the text, never crash


def line_for(character, event, tier, rng, recent):
    """One line for this character reacting to this event at this tier, or None.

    RNG is injected for reproducibility; ``recent`` is the character's recently
    spoken lines, avoided for anti-repetition. Falls back to the highest tier
    bank <= ``tier`` so contiguous banks have no gaps."""
    banks = PHRASES.get(character, {}).get(event["event_type"], {})
    templates = banks.get(tier) or banks.get(min(tier, max(banks) if banks else 0), [])
    if not templates:
        return None
    # Prefer a template whose rendered line isn't a recent repeat; sorted() keeps
    # the seeded RNG deterministic regardless of dict/list ordering.
    choices = [t for t in templates if _render(t, event) not in recent] or templates
    template = rng.choice(sorted(choices))
    return _render(template, event)


def lines_for_tick(state, dir_state, now, config):
    """STUB (Task 4). Task 5 wires this to iterate PERSONALITIES over the new
    events in state['recent'], gate each on reacts_to, and draw a line via
    line_for — with the anti-repetition window from dir_state."""
    return []
