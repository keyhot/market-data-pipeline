"""The plate's acceptance criteria are checkable, so they are a test.

Spec: Docs/world-room-plate.md -> "The plate - asset contract" (vault).

The three criteria a machine can judge are here. The two it cannot — "no text,
no numbers, no glyphs anywhere" and "no characters" — were signed off by eye
against the shipped asset and recorded in the commit message; a test that
claimed to check them would be a test that lies.

The screen quads are imported from the intake script rather than restated,
because two hand-kept copies of one rect is how a candle ends up drawn 6px off
the painted glass.
"""
from pathlib import Path

from PIL import Image

from scripts.prepare_plate import (
    SCREEN_QUADS,
    WATERMARK_LUMA,
    WATERMARK_WINDOW,
    _is_terracotta,
)

PLATE = (
    Path(__file__).resolve().parents[2] / "api" / "static" / "world-plate-btc-eth.png"
)
BYTE_BUDGET = 3_000_000  # on-disk PNG; the ~7MB figure in the spec is decoded RGBA


def _interior(quad):
    """The pixels strictly inside a quad with vertical left and right edges.

    Inset by one pixel so the polygon's own rasterised boundary is not what is
    being measured.
    """
    (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = quad
    span = trx - tlx
    for x in range(tlx + 1, trx):
        ratio = (x - tlx) / span
        top = tly + (try_ - tly) * ratio
        bottom = bly + (bry - bly) * ratio
        for y in range(int(top) + 2, int(bottom) - 1):
            yield x, y


def _is_schematic_ink(pixel):
    """The teal the generator drew its circuitry in. The dark glass is not teal:
    (26,32,46) and its sheen (34,41,57) both fail the first clause."""
    red, green, blue = pixel
    return green > red + 15 and blue > red + 10 and green > 50


def test_the_plate_is_exactly_the_browser_source_size():
    """1920x960 matches `_world_focus` in scripts/stream_scene.py. A plate of
    any other size makes every measured anchor in the manifest a lie."""
    with Image.open(PLATE) as im:
        assert im.size == (1920, 960)


def test_the_plate_stays_inside_its_byte_budget():
    assert PLATE.stat().st_size <= BYTE_BUDGET


def test_the_generators_watermark_is_gone():
    """Nothing cold in that corner of the room is as bright as the star was.

    The plant pot the star sat under is warm and is deliberately left alone, so
    it is exempted here by the intake's own rule rather than by a second copy
    of it - if that rule ever stops telling clay from starlight, this fails.
    """
    with Image.open(PLATE) as im:
        pixels = im.convert("RGB").load()
        left, top, right, bottom = WATERMARK_WINDOW
        survivors = [
            (x, y, pixels[x, y])
            for y in range(top, bottom)
            for x in range(left, right)
            if sum(pixels[x, y]) / 3 > WATERMARK_LUMA
            and not _is_terracotta(pixels[x, y])
        ]
    assert survivors == []


def test_no_painted_schematic_survives_where_live_candles_go():
    """The central monitors carry live data (Task 11), so a painted circuit
    left in a corner of the glass would be a baked claim under a live chart.
    Under-filling the quad is invisible at any zoom a human reviews at, which
    is exactly why this is measured and not eyeballed."""
    with Image.open(PLATE) as im:
        pixels = im.convert("RGB").load()
        for name, quad in SCREEN_QUADS.items():
            ink = [
                (x, y) for x, y in _interior(quad) if _is_schematic_ink(pixels[x, y])
            ]
            assert ink == [], (
                f"{name}: {len(ink)} schematic pixels survived, e.g. {ink[:5]}"
            )


def test_the_peripheral_screens_keep_their_art():
    """The other half of the decision: only the two central monitors are
    flattened. A blanket fill would leave a room with nothing on in it."""
    with Image.open(PLATE) as im:
        pixels = im.convert("RGB").load()
        ink = sum(
            1
            for y in range(0, 500, 2)
            for x in range(0, 460, 2)
            if _is_schematic_ink(pixels[x, y])
        )
    assert ink > 500, "the left-hand wall screens lost their schematic art"
