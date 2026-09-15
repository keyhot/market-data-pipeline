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
    DEFAULT_SOURCE,
    SCREEN_FRAMES,
    SOURCE_SHA256,
    TARGET,
    WATERMARK_LUMA,
    WATERMARK_WINDOW,
    _is_terracotta,
    frame_bottom,
    frame_top,
    resample_mode,
    sha256,
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
# again, to 79391 / 60301 px - a second legitimate, measured narrowing. KI-068
# then moved `centre-right`'s right edge 12px in, off the frame's OUTER line
# and onto its inner one, for 79391 / 58004 - a third. The floors are LEFT
# where they were (7.3% / 1.8% headroom now, not 10%): a floor that follows
# every shrink downward guards nothing, and the drift they were standing in
# for is pinned directly now by
# `test_the_shipped_plates_fill_matches_the_frames_it_was_generated_from`
# and, for the vertical edges those two KIs never checked, by
# `test_the_frames_vertical_edges_are_the_lines_they_are_painted_on`.
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


# KI-068: the two horizontal frame edges are MEASURED off the source art and
# recorded as fitted lines; the two vertical ones are hand-entered scalars, and
# nothing checked them. Three of the four named the frame's inner dark line -
# the boundary the fill is inset from - and `centre-right`'s `right` named the
# OUTER one, 12px further out, so the glass fill was painted straight over that
# monitor's right-hand bezel and the screen ran off the edge of its own frame.
#
# Not findable in the shipped PNG: the fill erases the bezel it overran, which
# is KI-067's lesson repeated ("the pixels it wanted were the ones the bug had
# erased"). So this reads the SOURCE, and it is the same measurement `top` and
# `bottom` come from, turned 90 degrees: per row now instead of per column, the
# frame's inner dark line found immediately against the bezel's lit face, with
# the schematic's cyan masked out first because it is brighter than the bezel.
#
# Threshold-free on purpose. "Immediately against the lit face" is a walk from
# the bezel's brightest column toward the glass until the luminance stops
# falling, so nothing here is tuned to a luminance value that a repaint would
# invalidate - the one number, CYAN_INK, is the intake's own.
SOURCE = DEFAULT_SOURCE  # the intake's own path, so the two cannot drift
CYAN_INK = 28           # blue-minus-red; the intake's own mask for schematic ink
EDGE_TOLERANCE = 1      # px; `centre-left`'s left line lands on 461 or 462 by row


def _inner_frame_line(lum, cyan, y, nominal, step):
    """Where the frame's inner dark line runs at row `y`, on one vertical edge.

    `step` is +1 for a left-hand edge (the glass lies to the RIGHT of the bezel)
    and -1 for a right-hand one. The window is anchored on the recorded scalar
    but reaches much further toward the glass than away from it: a scalar that
    is wrong is wrong outward, and reaching outward instead would eventually
    find the coolant tube standing beside the right-hand monitor, which is far
    brighter than any bezel.
    """
    lo, hi = sorted((nominal + step * 16, nominal - step * 4))
    window = [
        (x, None if cyan[y, x] else lum[y, x]) for x in range(lo, hi + 1)
    ]
    lit = max((v, x) for x, v in window if v is not None)[1]
    x = lit
    while True:
        nxt = x + step
        if not (lo <= nxt <= hi) or cyan[y, nxt] or lum[y, nxt] > lum[y, x]:
            return x
        x = nxt


def test_the_frames_vertical_edges_are_the_lines_they_are_painted_on():
    """Every edge of `SCREEN_FRAMES` names the frame's inner dark line - the
    two fitted ones by construction, and these two because this says so.

    Skips when the source art is absent rather than failing: it is gitignored,
    and KI-067 established that "gone" is usually a worktree artifact (it is in
    every worktree and in Downloads). The hash is asserted, not skipped on, so
    a DIFFERENT image in that path is a failure and not a silent pass.
    """
    import numpy as np

    if not SOURCE.exists():
        import pytest

        pytest.skip(f"source art not in this worktree: {SOURCE.name}")
    assert sha256(SOURCE) == SOURCE_SHA256, (
        f"{SOURCE.name} is not the image every coordinate here was measured on"
    )
    with Image.open(SOURCE) as raw:
        im = raw.convert("RGB").resize(TARGET, resample_mode(raw.size))
    pixels = np.asarray(im, dtype=int)
    lum = pixels.sum(axis=2) / 3.0
    cyan = (pixels[:, :, 2] - pixels[:, :, 0]) >= CYAN_INK

    for name, frame in SCREEN_FRAMES.items():
        for edge, step in (("left", 1), ("right", -1)):
            nominal = frame[edge]
            rows = range(
                frame_top(frame, nominal) + 12, frame_bottom(frame, nominal) - 12
            )
            found = sorted(
                _inner_frame_line(lum, cyan, y, nominal, step) for y in rows
            )
            median = found[len(found) // 2]
            assert abs(median - nominal) <= EDGE_TOLERANCE, (
                f"{name} {edge}: the painted frame's inner line runs at "
                f"x={median} over {len(found)} rows, but SCREEN_FRAMES says "
                f"{nominal} - the fill is inset from the wrong line, so it "
                "overruns the bezel by the difference"
            )
