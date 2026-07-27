"""Phrase-bank commentary: deterministic (seeded RNG), anti-repeating, and
truthful (any number quoted comes from the event payload). The registry
invariant keeps every character covered for every event type it might react to."""

import random

from director.commentary import line_for
from director.phrases import PHRASES
from world.salience import KNOWN_EVENT_TYPES

# The director's own actions (Task 7) are not things characters comment on —
# exclude them from the coverage invariant so registering them can't demand
# recursive phrases.
DIRECTOR_META = {"scene_switched", "commentary_spoken"}


def test_every_commentable_event_type_has_phrases_for_every_character():
    covered = set(KNOWN_EVENT_TYPES) - DIRECTOR_META
    for character, banks in PHRASES.items():
        missing = sorted(covered - set(banks))
        assert missing == [], f"{character} missing phrases for {missing}"


def test_line_is_deterministic_under_a_seeded_rng():
    ev = {"event_type": "big_move", "symbol": "BTCUSDT", "payload": {"sigmas": 8.0}}
    a = line_for("statistician", ev, 3, random.Random(1), [])
    b = line_for("statistician", ev, 3, random.Random(1), [])
    assert a == b and a is not None


def test_quoted_numbers_come_from_the_payload():
    ev = {"event_type": "big_move", "symbol": "BTCUSDT", "payload": {"sigmas": 8.0}}
    line = line_for("statistician", ev, 3, random.Random(0), [])
    assert "8.0" in line or "8" in line  # the real sigma, never a made-up one


def test_anti_repetition_avoids_recent_lines():
    ev = {
        "event_type": "streak",
        "symbol": "BTCUSDT",
        "payload": {"bars": 9, "direction": "up"},
    }
    rng = random.Random(3)
    first = line_for("optimist", ev, 1, rng, [])
    second = line_for("optimist", ev, 1, rng, [first])
    assert first is not None and second != first  # not an immediate repeat


def test_returns_none_when_the_character_has_no_line_for_the_tier():
    # A character that only speaks above a high tier stays silent below it.
    ev = {
        "event_type": "signal_resolved",
        "symbol": "BTCUSDT",
        "payload": {"outcome": "win"},
    }
    assert line_for("statistician", ev, 0, random.Random(0), []) is None


def test_missing_payload_key_degrades_to_text_not_a_crash():
    # A template that references a key the payload lacks must not raise.
    ev = {"event_type": "big_move", "symbol": "BTCUSDT", "payload": {}}
    line = line_for("statistician", ev, 3, random.Random(0), [])
    assert line is not None  # renders (even with a missing key), never crashes
