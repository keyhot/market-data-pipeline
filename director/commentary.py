"""Phrase-bank commentary (Sprint 13): deterministic line selection with
anti-repetition. No LLM. ``line_for`` picks one line for a character reacting to
one event at one tier; ``lines_for_tick`` (wired to the personalities in Task 5)
turns a world-state tick into the lines to speak.

Truthfulness: numbers come only from the event payload, via ``str.format`` over
``payload`` + ``symbol``. A template that references a missing key degrades to
its literal text rather than raising.
"""

import random

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
    """Turn a world-state tick into the lines to speak: each personality reacts
    to the *new* events in state['recent'] (id > last_seen), drawing a
    non-repeating line via line_for. Deterministic — the RNG is seeded per
    (event, personality), so the same event always yields the same line and no
    external RNG needs injecting. Pure: reads dir_state, never writes it."""
    from director.personalities import PERSONALITIES, reacts_to

    last_seen = dir_state.last_seen_event_id or 0
    new_events = [e for e in state.get("recent", []) if (e.get("id") or 0) > last_seen]
    lines = []
    for event in sorted(new_events, key=lambda e: e.get("id") or 0):
        tier = event.get("tier", 0)
        for personality in PERSONALITIES:
            if not reacts_to(personality, event, tier):
                continue
            window = dir_state.recent_lines_by_character.get(
                personality.phrase_character, []
            )[-config.anti_repeat_window :]
            rng = random.Random(f"{event.get('id')}:{personality.name}")
            line = line_for(personality.phrase_character, event, tier, rng, window)
            if line is not None:
                lines.append(
                    {
                        "character": personality.name,
                        "voice": personality.voice,
                        "text": line,
                        "symbol": event.get("symbol"),
                        "event_id": event.get("id"),
                    }
                )
    return lines
