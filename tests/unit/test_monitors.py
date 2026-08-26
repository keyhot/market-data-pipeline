"""What the painted monitors are allowed to claim.

Spec: Docs/world-room-plate.md -> "Live charts in the painted monitors" (vault).

The renderer draws; it does not decide. How many candles fit a painted frame,
whether a frame is big enough to be worth drawing at all, and when data is too
old to be shown as the present are decided here, so they can be tested here.

Widths below are chosen to exercise the arithmetic, NOT copied from the plate:
the plan's monitor rects were written before anyone had measured the real
image, and P2 measures them off the asset. Nothing in this module knows or
needs to know where the glass actually is.
"""
import json
import shutil
import subprocess

import pytest

from world.monitors import (
    BODY_CELLS,
    CELL,
    GAP_CELLS,
    MIN_BARS,
    MIN_HEIGHT,
    MIN_WIDTH,
    PAD_CELLS,
    STALE_AFTER_SECONDS,
    STALE_ALPHA,
    bars_that_fit,
    is_drawable,
    monitor_rules,
    rules_js,
    stale_alpha_for,
)

NODE = shutil.which("node")


def test_candles_are_counted_in_whole_grid_cells():
    """A candle is 3 cells wide with a 1-cell gap: 16px of pitch at cell=4,
    inside an 8px inset each side. 320px of glass therefore holds 19 candles,
    not 19.5 - a fractional candle is a sliver against a painted bezel."""
    assert bars_that_fit(320) == 19


def test_the_drawn_row_never_overflows_the_painted_frame():
    """The reason whole candles matter. Whatever the width, the candles plus
    both insets have to fit inside the glass the plate painted."""
    pitch = (BODY_CELLS + GAP_CELLS) * CELL
    inset = 2 * PAD_CELLS * CELL
    for width in range(0, 1200, 7):
        assert bars_that_fit(width) * pitch + inset <= max(width, inset)


def test_a_wider_screen_fits_more_candles():
    assert bars_that_fit(640) > bars_that_fit(320)


def test_a_screen_with_no_room_for_a_single_candle_fits_none():
    """Not a negative count: the inset alone can be wider than the glass."""
    assert bars_that_fit(2 * PAD_CELLS * CELL) == 0
    assert bars_that_fit(4) == 0
    assert bars_that_fit(0) == 0


def test_a_screen_too_small_for_a_legible_candle_is_not_drawable():
    """The spec's rule: too small renders dark glass, never a smear."""
    assert is_drawable({"w": MIN_WIDTH, "h": MIN_HEIGHT}) is True
    assert is_drawable({"w": 40, "h": 30}) is False


def test_a_screen_below_either_dimension_is_not_drawable():
    """Both axes gate it. A letterbox strip of glass is as unreadable as a
    postage stamp, and the plate paints irregular frames."""
    assert is_drawable({"w": MIN_WIDTH - 1, "h": MIN_HEIGHT}) is False
    assert is_drawable({"w": MIN_WIDTH, "h": MIN_HEIGHT - 1}) is False


def test_a_rect_that_never_got_measured_is_not_drawable():
    """A manifest entry missing its dimensions must fail closed to dark glass,
    not raise inside a render loop."""
    assert is_drawable({}) is False
    assert is_drawable({"w": 400}) is False


def test_a_coarser_grid_can_make_a_wide_enough_screen_undrawable():
    """MIN_WIDTH alone is not the whole gate, and this is the case that proves
    the bar floor earns its place. Width is measured in pixels but candles are
    counted in cells, so doubling the cell halves the run a frame can hold: at
    cell=8 a 160px frame is wide enough by the pixel rule and holds four
    candles by the one that counts."""
    assert is_drawable({"w": MIN_WIDTH, "h": MIN_HEIGHT}) is True
    assert bars_that_fit(MIN_WIDTH, cell=2 * CELL) < MIN_BARS
    assert is_drawable({"w": MIN_WIDTH, "h": MIN_HEIGHT}, cell=2 * CELL) is False


def test_fresh_data_is_drawn_at_full_opacity():
    assert stale_alpha_for(0.0) == 1.0
    assert stale_alpha_for(STALE_AFTER_SECONDS) == 1.0


def test_stale_data_dims_rather_than_posing_as_the_present():
    """A monitor showing three-minute-old candles at full strength is claiming
    to be live. Dimming is the honest version, and it is decided here so the
    page cannot own a second copy of the threshold."""
    assert stale_alpha_for(STALE_AFTER_SECONDS + 0.1) == STALE_ALPHA
    assert stale_alpha_for(86_400.0) == STALE_ALPHA


def test_the_rules_reach_the_page_as_data():
    rules = monitor_rules()
    assert rules["cell"] == CELL
    assert rules["stale_after_seconds"] > 0
    assert 0 < rules["stale_alpha"] < 1
    assert rules["min_width"] > 0 and rules["min_height"] > 0
    assert rules["min_bars"] == MIN_BARS


def test_the_rules_survive_the_trip_through_json():
    """They are injected into a template as a JSON literal, so anything in
    here that json cannot carry breaks the page, not the test suite."""
    assert json.loads(json.dumps(monitor_rules())) == monitor_rules()


def test_the_page_is_served_the_rule_itself_not_just_its_numbers():
    """KI-019's actual lesson: injecting the CUTS was half the fix, because
    the three-line function reading them was then written out twice and
    drifted. `world.state.tier_of_js` is the precedent. The monitors get the
    same treatment - the JS is generated from these constants, so a page
    cannot own a second copy of the rule."""
    js = rules_js()
    assert "function barsThatFit(" in js
    assert "function isDrawable(" in js
    assert "function staleAlpha(" in js
    assert str(MIN_WIDTH) in js
    assert str(MIN_HEIGHT) in js
    assert str(MIN_BARS) in js
    assert str(STALE_AFTER_SECONDS) in js
    assert str(STALE_ALPHA) in js


@pytest.mark.skipif(NODE is None, reason="node is not installed on this host")
def test_the_emitted_javascript_agrees_with_python():
    """The claim `rules_js` makes is that the page and the server apply the
    same rule. A substring check cannot see that. This runs the emitted text
    in a real JS engine and compares every answer against Python's.

    It skips where node is absent, which is why the constants are also pinned
    structurally above - that check still runs everywhere.
    """
    widths = [0, 4, 16, 40, 159, 160, 320, 321, 640, 1000]
    rects = [
        {"w": 40, "h": 30},
        {"w": MIN_WIDTH, "h": MIN_HEIGHT},
        {"w": MIN_WIDTH - 1, "h": MIN_HEIGHT},
        {"w": MIN_WIDTH, "h": MIN_HEIGHT - 1},
        {"w": 640, "h": 226},
    ]
    ages = [0, 1.5, STALE_AFTER_SECONDS, STALE_AFTER_SECONDS + 0.1, 86_400]

    driver = (
        f"{rules_js()}\n"
        f"const widths = {json.dumps(widths)};\n"
        f"const rects = {json.dumps(rects)};\n"
        f"const ages = {json.dumps(ages)};\n"
        "console.log(JSON.stringify({\n"
        "  fits: widths.map(barsThatFit),\n"
        "  drawable: rects.map(isDrawable),\n"
        "  alpha: ages.map(staleAlpha),\n"
        "}));\n"
    )
    result = subprocess.run(
        [NODE, "-e", driver], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    emitted = json.loads(result.stdout)

    assert emitted["fits"] == [bars_that_fit(w) for w in widths]
    assert emitted["drawable"] == [is_drawable(r) for r in rects]
    assert emitted["alpha"] == [stale_alpha_for(a) for a in ages]
