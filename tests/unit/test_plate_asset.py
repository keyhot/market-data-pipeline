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
    SCREEN_FRAMES,
    WATERMARK_LUMA,
    WATERMARK_WINDOW,
    _is_terracotta,
    frame_bottom,
    frame_top,
)

PLATE = (
    Path(__file__).resolve().parents[2] / "api" / "static" / "world-plate-btc-eth.png"
)
BYTE_BUDGET = 3_000_000  # on-disk PNG; the ~7MB figure in the spec is decoded RGBA


def _glass(frame):
    """Every pixel of painted glass a frame encloses, one pixel inside its own
    dark line.

    Deliberately the FRAME and not the fill quad: the intake insets its fill by
    two pixels, so a test scoped to the fill would only ever re-read the colour
    it had just painted and would pass with the quad 50px too small. The two
    pixel ring between the fill and the frame is the only part of this that can
    fail, so it is the part that has to be measured.
    """
    for x in range(frame["left"] + 1, frame["right"]):
        for y in range(frame_top(frame, x) + 1, frame_bottom(frame, x)):
            yield x, y


# Measured off schematic that is still on the plate (the left wall screen,
# x 60-200, y 330-500): green-minus-red runs p5=19, p25=24, median=28. The
# bezel's own cyan inner reflection - which is bezel, not screen, and sits in
# the ring this test scans - runs 16-19. The threshold goes between them, and
# `test_the_peripheral_screens_keep_their_art` is the control proving the rule
# still finds circuitry in bulk where circuitry was deliberately kept.
INK_TEAL = 20


def _is_schematic_ink(pixel):
    """The teal the generator drew its circuitry in. The dark glass is not teal:
    (26,32,46) and its sheen (34,41,57) both fail the first clause."""
    red, green, blue = pixel
    return green > red + INK_TEAL and blue > red + 10 and green > 50


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


# KI-052 review round 1, MINOR 3: `_glass()` narrows silently otherwise. This
# ticket itself narrowed its yield 3.5% (centre-left) and 3.6% (centre-right)
# by fixing the flat-bottom bug (`frame_bottom` now recedes on the right
# instead of running level) - a legitimate, measured shrink, but nothing
# caught it as anything other than "still zero ink," which is exactly the
# coverage gap that let the two derivations drift into KI-066. KI-067 then
# re-measured `frame_bottom` off the recovered source art and the yield shrank
# again, to 79391 / 60301 px - a second legitimate, measured narrowing. The
# floors are LEFT where they were (7.3% / 5.8% headroom now, not 10%): a floor
# that follows every shrink downward guards nothing, and the drift they were
# standing in for is pinned directly now by
# `test_the_shipped_plates_fill_matches_the_frames_it_was_generated_from`.
MIN_GLASS_PIXELS = {"centre-left": 74000, "centre-right": 57000}


def test_no_painted_schematic_survives_where_live_candles_go():
    """The central monitors carry live data (Task 11), so a painted circuit
    left in a corner of the glass would be a baked claim under a live chart.
    Under-filling is invisible at any zoom a human reviews at, which is exactly
    why this is measured and not eyeballed."""
    with Image.open(PLATE) as im:
        pixels = im.convert("RGB").load()
        for name, frame in SCREEN_FRAMES.items():
            glass_pixels = list(_glass(frame))
            assert len(glass_pixels) >= MIN_GLASS_PIXELS[name], (
                f"{name}: _glass() yielded only {len(glass_pixels)} px, "
                f"below the floor of {MIN_GLASS_PIXELS[name]} - narrowed too "
                "far, or a frame edge regressed"
            )
            ink = [
                (x, y) for x, y in glass_pixels if _is_schematic_ink(pixels[x, y])
            ]
            assert ink == [], (
                f"{name}: {len(ink)} schematic pixels survived, e.g. {ink[:5]}"
            )


def test_the_peripheral_screens_keep_their_art():
    """The other half of the decision: only the two central monitors are
    flattened. A blanket fill would leave a room with nothing on in it.

    This is also the control for the test above. That one asserts an absence,
    and an absence is what a mis-aimed scan reports too - so the same predicate
    has to find circuitry in bulk somewhere circuitry is known to be."""
    with Image.open(PLATE) as im:
        pixels = im.convert("RGB").load()
        ink = sum(
            1
            for y in range(0, 500, 2)
            for x in range(0, 460, 2)
            if _is_schematic_ink(pixels[x, y])
        )
    assert ink > 500, "the left-hand wall screens lost their schematic art"


# KI-066/KI-067: the shipped PNG and the intake that generates it drifted, and
# nothing compared them. `SCREEN_FRAMES` moved twice (KI-052, then KI-067) while
# `api/static/world-plate-btc-eth.png` kept exactly one revision, so the asset
# on air carried a flat-bottomed fill the derivation had already disowned - and
# every test here passed, because they all read the PNG *through* the frames
# rather than checking the PNG against them.
FILL_EDGE_TOLERANCE = 1  # px; `ImageDraw.polygon` rounds a slanted edge


def _fill_span(pixels, frame, x, colours):
    """The extent of the painted fill at column `x`: the CONTIGUOUS run of
    `colours` containing the screen's own mid-row, grown outward.

    Contiguous and seeded from the middle, not "first and last match in a
    window" - the room is painted in the same family of dark blues, and a
    single pixel of wall art that happens to land on the exact fill colour
    would otherwise be read as the fill's edge (it does: centre-left x=494
    carries one 11px above the real top edge).
    """
    mid = (frame_top(frame, x) + frame_bottom(frame, x)) // 2
    if pixels[x, mid] not in colours:
        return None, None
    top = bottom = mid
    while top - 1 >= 0 and pixels[x, top - 1] in colours:
        top -= 1
    while bottom + 1 < 960 and pixels[x, bottom + 1] in colours:
        bottom += 1
    return top, bottom


def test_the_shipped_plates_fill_matches_the_frames_it_was_generated_from():
    """The painted fill's own edges, read off the asset, against the frame the
    intake would fill today. This is the only check here that can fail when the
    PNG is stale: it reads the boundary as painted rather than assuming it.

    Both edges, not just the bottom - a top that regressed would be the same
    defect pointed the other way, and KI-052/KI-067 were both found by looking
    at one edge and not the other.
    """
    from scripts.prepare_plate import GLASS, SCREEN_INSET, SHEEN

    colours = {GLASS, SHEEN}
    with Image.open(PLATE) as im:
        pixels = im.convert("RGB").load()
        for name, frame in SCREEN_FRAMES.items():
            lo = frame["left"] + SCREEN_INSET + 1
            hi = frame["right"] - SCREEN_INSET
            for x in range(lo, hi):
                top, bottom = _fill_span(pixels, frame, x, colours)
                assert top is not None, f"{name}: no fill at all at x={x}"
                want_top = frame_top(frame, x) + SCREEN_INSET
                want_bottom = frame_bottom(frame, x) - SCREEN_INSET
                assert abs(top - want_top) <= FILL_EDGE_TOLERANCE, (
                    f"{name} x={x}: fill starts at y={top}, frames say {want_top}"
                )
                assert abs(bottom - want_bottom) <= FILL_EDGE_TOLERANCE, (
                    f"{name} x={x}: fill ends at y={bottom}, frames say "
                    f"{want_bottom} - the shipped PNG is stale, or a frame moved"
                )
