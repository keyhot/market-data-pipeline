"""The room's one measured key light, as data.

Docs/room-art-direction.md (vault) — the cast currently ignores the painted
amber desk lamp entirely, which is why every volume reads as a flat sticker
(KI-051). This module turns the lamp into `LightModel`: Python decides the
parameters, the page only draws (Tasks 6-8).

Mirrors `tests/unit/test_plate_manifest.py`'s shape: a broken or missing
`light` block must degrade to a documented neutral default rather than raise
into a page render — the KI-050 lesson, tested here rather than trusted.
"""
import dataclasses
import json

from world.light import DEFAULT_LIGHT, as_json, light_for
from world.plate import load_manifest


def test_the_shipped_manifest_carries_a_light_model():
    model = light_for(load_manifest())
    assert model is not DEFAULT_LIGHT
    # The key is the painted desk lamp. If a repaint moves the lamp, this
    # number is re-measured — that is the whole point of it being data.
    assert 1250 <= model.key[0] <= 1450
    assert 380 <= model.key[1] <= 520


def test_a_missing_light_block_degrades_instead_of_raising():
    # Failure is never a blank canvas: a manifest without the block must give
    # a flat neutral light, not kill the page (the KI-050 lesson).
    stripped = dataclasses.replace(load_manifest(), light={})
    assert light_for(stripped) is DEFAULT_LIGHT


def test_a_missing_manifest_degrades_too():
    assert light_for(None) is DEFAULT_LIGHT


def test_the_ramp_is_monotonic_and_centred_on_base():
    ramp = light_for(load_manifest()).ramp
    assert ramp["shade"] < ramp["base"] < ramp["lit"]
    assert ramp["base"] == 0.0


def test_as_json_round_trips_every_field_the_page_reads():
    payload = json.loads(as_json(light_for(load_manifest())))
    assert set(payload) >= {"key", "warmth", "ambient", "ramp", "contact"}
    assert set(payload["contact"]) >= {"opacity", "widthScale", "throwScale"}


def test_a_non_monotonic_ramp_degrades_instead_of_lighting_the_wrong_side():
    # A ramp that isn't shade < base < lit would darken the LIT side of a
    # figure and brighten the SHADE side — worse than no lighting model at
    # all, so this must degrade exactly like a missing block, not pass a
    # backwards ramp through to the page.
    manifest = load_manifest()
    poisoned = dataclasses.replace(
        manifest,
        light={
            "key": [1339, 455],
            "direction": [-0.55, 0.84],
            "warmth": "#f0a848",
            "ambient": "#1a2030",
            "ramp": {"lit": -0.30, "base": 0.0, "shade": 0.34},
            "contact": {"opacity": 0.38, "widthScale": 1.15, "throwScale": 7},
        },
    )
    assert light_for(poisoned) is DEFAULT_LIGHT


def test_an_explicitly_flat_ramp_still_keeps_its_measured_key():
    # A flat ramp (lit == base == shade == 0) is not malformed — an art
    # direction of "no ramp contribution here" — so it must not be treated
    # the same as a missing/broken block: the measured lamp position and
    # contact shadow survive even though the ramp itself is neutral.
    manifest = load_manifest()
    flattened = dataclasses.replace(
        manifest,
        light={
            "key": [1339, 455],
            "direction": [-0.55, 0.84],
            "warmth": "#f0a848",
            "ambient": "#1a2030",
            "ramp": {"lit": 0.0, "base": 0.0, "shade": 0.0},
            "contact": {"opacity": 0.38, "widthScale": 1.15, "throwScale": 7},
        },
    )
    model = light_for(flattened)
    assert model is not DEFAULT_LIGHT
    assert model.key == (1339, 455)


def test_a_light_block_missing_a_required_field_degrades_rather_than_raising():
    # Exercises the try/except path directly: a `contact` with no `throwScale`
    # must not propagate a KeyError into a page render.
    manifest = load_manifest()
    poisoned = dataclasses.replace(
        manifest,
        light={
            "key": [1339, 455],
            "direction": [-0.55, 0.84],
            "warmth": "#f0a848",
            "ambient": "#1a2030",
            "ramp": {"lit": 0.30, "base": 0.0, "shade": -0.34},
            "contact": {"opacity": 0.38, "widthScale": 1.15},
        },
    )
    assert light_for(poisoned) is DEFAULT_LIGHT


def test_the_neutral_default_withholds_a_direction_but_not_the_shadow():
    """What a light model degrades to still has to put a floor under the cast.

    The flat ramp and `height: 0` are the honest neutral — no measurement, so
    no invented direction, and the page draws the centred contact patch it
    drew before there was a light model at all. `opacity` is deliberately not
    part of that: a contact shadow is the only thing telling a viewer where
    the floor is, and the room that reaches this default is the *procedural
    fallback* — the one that has already lost its painting and can least
    afford a cast floating in front of nothing (KI-045/KI-047).

    A plate that genuinely wants no contact shadow says so by measuring
    `opacity: 0`, which is a statement and reaches the page intact.
    """
    contact = DEFAULT_LIGHT.contact
    assert contact["throwScale"] == 0, (
        "the neutral default measures a throw - it has invented a lamp"
    )
    assert contact["opacity"] > 0, (
        "the neutral default draws no contact shadow at all, so every figure "
        "in the procedural fallback room floats"
    )
