"""The decisions the painted monitors make.

The renderer draws; it does not decide. How many candles fit an irregular
painted frame, whether a frame is big enough to be legible at all, and when
data is too old to be shown as the present are all decided here, under test,
and injected into the page as constants - and as the functions that read them.

Pure and DB-free, the same shape as `world.renderer_health`. Nothing in here
knows where the glass is: the rects come from the plate manifest (P2), which is
measured off the real asset.
"""
from __future__ import annotations

CELL = 4
BODY_CELLS = 3          # candle body width
GAP_CELLS = 1           # gap between candles
PAD_CELLS = 2           # inset from the painted bezel, each side
MIN_WIDTH = 160         # below this a chart is a smear, not a chart
MIN_HEIGHT = 90
MIN_BARS = 8            # a run shorter than this shows nothing worth reading
STALE_AFTER_SECONDS = 180   # 3 missed 1m bars; the ingester writes closed candles
STALE_ALPHA = 0.35          # stale screens dim rather than lie


def bars_that_fit(width_px: int, cell: int = CELL) -> int:
    """Whole candles only - a fractional candle is a sliver at the bezel.

    Never negative: the inset alone can be wider than the glass, and floor
    division of a negative usable width would otherwise hand the page a count
    to iterate backwards over.
    """
    usable = width_px - 2 * PAD_CELLS * cell
    pitch = (BODY_CELLS + GAP_CELLS) * cell
    return max(0, usable // pitch)


def is_drawable(rect: dict, cell: int = CELL) -> bool:
    """Both dimensions and the candle count. `MIN_WIDTH` dominates at the
    default cell; the bar floor is what stops a coarser grid from claiming a
    frame it can only fit four candles in.

    Fails closed on a rect missing its dimensions - an unmeasured manifest
    entry renders dark glass, it does not raise inside a render loop. `or 0`
    rather than a `get` default because these rects are parsed from JSON,
    where unmeasured is `null` as often as it is absent, and `None >= int`
    raises. JS reaches the same answer by coercing null to 0; the node
    cross-check in the tests pins that the two still agree.
    """
    width = rect.get("w") or 0
    return (
        width >= MIN_WIDTH
        and (rect.get("h") or 0) >= MIN_HEIGHT
        and bars_that_fit(width, cell) >= MIN_BARS
    )


def stale_alpha_for(age_seconds: float) -> float:
    """How strongly to draw data of this age.

    A monitor showing three-minute-old candles at full strength is claiming to
    be live. Exactly at the threshold is still fresh; past it, the screen dims
    rather than posing as the present.
    """
    return STALE_ALPHA if age_seconds > STALE_AFTER_SECONDS else 1.0


def monitor_rules() -> dict:
    """The constants, as the JSON the page is served."""
    return {
        "cell": CELL,
        "body_cells": BODY_CELLS,
        "gap_cells": GAP_CELLS,
        "pad_cells": PAD_CELLS,
        "min_width": MIN_WIDTH,
        "min_height": MIN_HEIGHT,
        "min_bars": MIN_BARS,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "stale_alpha": STALE_ALPHA,
    }


def rules_js() -> str:
    """`bars_that_fit`, `is_drawable` and `stale_alpha_for`, as the JavaScript
    the page is served.

    The same lesson as `world.state.tier_of_js`: injecting the *constants* is
    only half of it. A page that re-derives the rule from them owns a second
    copy, and two copies of a rule is how KI-019 happened. The text below is
    generated FROM the constants above, so the tested definition and the
    running definition cannot drift - `test_monitors.py` runs this text in
    node and compares every answer against Python's.

    `isDrawable` is emitted at the default cell only; the page draws on the
    cast's 4px grid and has no reason to ask about another.
    """
    return (
        "function barsThatFit(widthPx) {\n"
        f"      const usable = widthPx - 2 * {PAD_CELLS} * {CELL};\n"
        f"      const pitch = ({BODY_CELLS} + {GAP_CELLS}) * {CELL};\n"
        "      return Math.max(0, Math.floor(usable / pitch));\n"
        "    }\n"
        "    function isDrawable(rect) {\n"
        f"      return rect.w >= {MIN_WIDTH} && rect.h >= {MIN_HEIGHT}\n"
        f"        && barsThatFit(rect.w) >= {MIN_BARS};\n"
        "    }\n"
        "    function staleAlpha(ageSeconds) {\n"
        f"      return ageSeconds > {STALE_AFTER_SECONDS} ? {STALE_ALPHA} : 1;\n"
        "    }"
    )
