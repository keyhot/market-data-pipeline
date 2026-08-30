"""KI-019's family: two surfaces reading one ramp.

world/visuals.py is injected into /world AND both overlays. If Sprint 15
retunes it to make the plate look right, the overlays change on air with no
one having asked. These values are pinned so that edit fails loudly here
instead of quietly on the stream.

The plan's own draft of this file assumed `room_light(tier)` returns
`{"intensity": ..., "color": ...}` and sorted on `light["intensity"]`. It
doesn't, and sorting on a key it doesn't have raises `KeyError` rather than
failing an assertion. Verified against `world/visuals.py`: the real ramp is
`{"vignette", "warmth", "lift"}` (see `_ROOM_LIGHT_RAMP`, ~line 120, and
`room_light`, ~line 128). This file pins THAT shape, not the guess -- the
mismatch is exactly the failure mode this regression test exists to catch,
so changing `world/visuals.py` to match the brief instead would have been
the ticket's own defect.
"""

from world import visuals


def test_the_tier_ramp_is_monotonic_and_four_deep():
    lights = [visuals.room_light(tier) for tier in range(4)]
    assert len(lights) == 4
    # The real shape. A field renamed or dropped here is exactly the kind of
    # edit that would silently retune both overlays.
    assert {"vignette", "warmth", "lift"} == set(lights[0])
    # Retuning for the plate must not flatten or invert the ramp the overlays
    # read: vignette (how closed-in the room reads) falls with tier, warmth
    # and lift rise. A single `sorted(..., key=...)` can't express "one field
    # falls while two rise", so each direction is checked on its own key.
    vignettes = [light["vignette"] for light in lights]
    assert vignettes == sorted(vignettes, reverse=True), vignettes
    for key in ("warmth", "lift"):
        values = [light[key] for light in lights]
        assert values == sorted(values), (key, values)
        assert len(set(values)) == len(values), f"{key} is flat somewhere"


def test_the_tier_ramp_values_are_pinned():
    """A numeric pin, not just an ordering check.

    A retune that shaves tier 3's lift from 0.22 to, say, 0.13 keeps every
    ordering invariant above intact (0.13 is still > tier 2's 0.12) while
    still measurably dimming what both overlays render -- the exact "nobody
    asked" edit Override 1 is about. Pinned against `world/visuals.py`'s own
    `_ROOM_LIGHT_RAMP`, so a deliberate retune is a one-line, visible diff
    here instead of a silent pass.
    """
    assert [visuals.room_light(tier) for tier in range(4)] == [
        {"vignette": 0.55, "warmth": 0.00, "lift": 0.00},
        {"vignette": 0.44, "warmth": 0.10, "lift": 0.05},
        {"vignette": 0.30, "warmth": 0.26, "lift": 0.12},
        {"vignette": 0.16, "warmth": 0.45, "lift": 0.22},
    ]


def test_the_overlays_and_the_room_read_the_same_mood_colours():
    for mood in ("calm", "bullish", "bearish", "panicked"):
        assert mood in visuals.MOOD_COLORS
