"""The manifest is the seam between an art step and a code step.

Spec: Docs/world-room-plate.md -> "Symbols and the plate" (vault).

A repaint is a new PNG plus a new manifest - no code change. That only holds if
the loader validates what it is handed, so these are the validations. Two of
them go further and check the manifest against the PNG itself: numbers measured
off an image drift the moment the image is replaced, and a candle drawn 6px off
the painted glass is not something a schema check can see.
"""
import dataclasses
import json
import math
import re
import shutil
import subprocess

import pytest
from PIL import Image

from world import monitors
from world.light import as_json, light_for
from world.plate import (
    DEFAULT_MANIFEST_PATH,
    glow_chart_overlaps,
    load_manifest,
    watchlist_disagreements,
)

WORLD_TEMPLATE = (
    DEFAULT_MANIFEST_PATH.resolve().parents[2] / "api" / "templates" / "world.html"
)
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _js_block(source: str, opening: str) -> str:
    """The brace-matched source of one JS construct - mirrors
    tests/api/test_world_page.py's helper of the same name and purpose
    (kept local rather than imported: that module builds a TestClient at
    import time, which a plate-manifest unit test has no business pulling
    in)."""
    start = source.index(opening)
    depth = 0
    for j in range(source.index("{", start), len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[start : j + 1]
    raise AssertionError(f"unbalanced braces after {opening!r}")


def _js_const(source: str, name: str) -> str:
    """One `const NAME = ...;` line - mirrors tests/api/test_world_page.py's
    helper of the same name (kept local for the same reason as `_js_block`
    above)."""
    match = re.search(rf"^\s*const {name} = .*;$", source, re.M)
    assert match, f"the page no longer declares a one-line const {name}"
    return match.group(0).strip() + "\n"


def _shading_prelude(source: str) -> str:
    """Everything `seatedRig`/`BODIES.*` need to draw since KI-051
    (world.html, Sprint 16 Task 6): they now call `paint`, which calls
    `shade`/`mixHex`, and draw their rim via
    `rimStyle`/`rimStroke`/`BODY_RIM_SHADED` (Task 8 put `rimStyle` between
    `paint` and the stroke, so a mood can reach the rim's own albedo).
    Pulled from the real page source, not re-typed, for the reason every
    other driver in this file pulls `snap`/`CAST_SCALE` the same way: a
    second definition drifts from the first and stops proving anything about
    the page that actually ships.
    """
    return (
        _js_block(source, "function mixHex(") + "\n"
        + _js_block(source, "function shade(") + "\n"
        + _js_block(source, "function rampLevel(") + "\n"
        + _js_block(source, "function keyAxis(") + "\n"
        + _js_const(source, "KEY_AXIS")
        + _js_const(source, "BAND_INSET")
        + _js_const(source, "BAND_MAX")
        + _js_block(source, "function circleBand(") + "\n"
        + _js_block(source, "function bandExtent(") + "\n"
        + _js_block(source, "function bandInside(") + "\n"
        + _js_block(source, "function slideToKey(") + "\n"
        + _js_const(source, "quant")
        + _js_const(source, "MIN_BAND")
        + _js_block(source, "const litRect = ") + "\n"
        + _js_block(source, "const litCircle = ") + "\n"
        + _js_const(source, "BODY_RIM_SHADED")
        + _js_block(source, "function rimStyle(") + "\n"
        + _js_block(source, "function paint(") + "\n"
        + _js_block(source, "function rimStroke(") + "\n"
        + _js_block(source, "function rimStrokeCircle(") + "\n"
    )


PLATE_PNG = DEFAULT_MANIFEST_PATH.with_suffix(".png")


def _minimal(**overrides):
    manifest = {
        "plate": "p.png",
        "canvas": [1920, 960],
        "cell": 4,
        "symbols": ["BTCUSDT"],
        "tubes": [{"symbol": "BTCUSDT", "x": 1, "base_y": 2, "height": 3, "width": 4}],
        "cast": {},
        "screens": [],
        "glow": [],
        "bands": {"top": 0, "bottom": 0},
    }
    manifest.update(overrides)
    return manifest


def test_the_shipped_manifest_loads():
    manifest = load_manifest()
    assert manifest is not None
    assert manifest.canvas == (1920, 960)
    assert manifest.cell == 4


def test_the_manifests_cell_agrees_with_the_one_python_bakes_into_the_js():
    """`CELL` has three definitions that all have to agree: `monitors.CELL`
    (baked as a literal into the `barsThatFit`/`isDrawable` JS `rules_js()`
    generates), `MONITOR_RULES.cell` (the same constant, injected as data),
    and the page's own `const CELL = (PLATE && PLATE.cell) || 4;` (read from
    THIS manifest). `drawCandles` mixes sources - pitch/pad/`yOf` use the
    page's `CELL` (so the manifest's value), `barsThatFit`/`isDrawable` use
    Python's baked literal - so a repaint that changed `cell` without a
    matching code change would double the layout pitch without changing how
    many bars the fit-count rule believes fit. A schema check on the
    manifest alone can't see that; this pins the one thing that keeps it from
    happening silently.
    """
    manifest = load_manifest()
    assert manifest.cell == monitors.CELL


def test_the_manifest_names_the_plate_beside_it():
    """A manifest pointing at a different PNG would lay the room out against an
    image it is not measured on."""
    manifest = load_manifest()
    assert manifest.plate == PLATE_PNG.name
    assert PLATE_PNG.exists()
    with Image.open(PLATE_PNG) as im:
        assert im.size == manifest.canvas


def test_every_tube_names_a_symbol_and_sits_inside_the_canvas():
    manifest = load_manifest()
    width, height = manifest.canvas
    assert manifest.tubes, "a plate with no tubes has nowhere to draw pressure"
    for tube in manifest.tubes:
        assert tube["symbol"] in manifest.symbols
        assert 0 <= tube["x"] <= width
        assert 0 < tube["base_y"] <= height
        assert tube["height"] > 0 and tube["width"] > 0


def test_a_full_tube_stays_inside_its_painted_housing():
    """`height` is the fill's ceiling. If base_y - height went above the painted
    cap, a symbol at maximum pressure would draw light out of the top of a glass
    tube that visibly ends."""
    manifest = load_manifest()
    for tube in manifest.tubes:
        assert tube["base_y"] - tube["height"] > 0


def test_every_chart_screen_is_a_rect_inside_the_canvas():
    manifest = load_manifest()
    width, height = manifest.canvas
    for screen in manifest.screens:
        assert screen["x"] >= 0 and screen["y"] >= 0
        assert screen["x"] + screen["w"] <= width
        assert screen["y"] + screen["h"] <= height


def test_each_screen_quad_slants_at_both_edges():
    """KI-052: the top edge was measured and the bottom was left flat, so
    candles (laid out in the axis-aligned rect, clipped to the quad) spilled
    off the bottom-right of the painted bezel."""
    manifest = load_manifest()
    charts = [s for s in manifest.screens if s.get("role") == "chart"]
    assert charts, "no chart screens to check"
    for screen in charts:
        (_, ty0), (_, ty1) = screen["quad"][0], screen["quad"][1]
        (_, by0), (_, by1) = screen["quad"][3], screen["quad"][2]
        assert ty0 != ty1, f"{screen['id']} top is flat"
        assert by0 != by1, f"{screen['id']} bottom is flat"
        # Both edges recede the same way or the screen is not a plane.
        assert (ty1 - ty0) * (by1 - by0) > 0


def _quad_pixels(quad):
    """Rasterize the quad as a trapezoid: vertical left/right sides, top and
    bottom edges each linearly interpolated between their own two corners.
    Pure geometry over `quad` itself - no dependency on `x/y/w/h`, which is
    exactly what makes this independent of the rect the old version of this
    test scanned."""
    (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = quad
    assert tlx == blx and trx == brx, "quad sides are not vertical"
    left, right = tlx, trx
    for x in range(left, right):
        frac = (x - left) / (right - left)
        top_y = tly + (try_ - tly) * frac
        bottom_y = bly + (bry - bly) * frac
        for y in range(math.ceil(top_y), math.floor(bottom_y) + 1):
            yield x, y


def test_every_screen_quad_lands_on_glass_the_intake_actually_flattened():
    """The glass a candle is clipped to (the quad, KI-052) must contain no
    painted schematic.

    This replaces a rect-scoped version of the same check. Scope, stated
    honestly, same as before but sharper: `quad` is exactly the polygon the
    intake fills flat and the page now masks candles to, so this can only
    fail when the manifest is hand-edited to claim more glass than the plate
    has. It is green both before and after KI-052's re-measurement (a smaller
    quad is still a subset of what was flattened) - the TDD evidence for the
    KI-052 fix itself is `test_each_screen_quad_slants_at_both_edges` above,
    which is genuinely red on the bug and genuinely green after. The
    complementary direction, an intake that under-fills, is checked against
    the frame itself in tests/unit/test_plate_asset.py."""
    manifest = load_manifest()
    charts = [s for s in manifest.screens if s.get("role") == "chart"]
    assert charts, "no chart screens to check"
    with Image.open(PLATE_PNG) as im:
        pixels = im.convert("RGB").load()
        for screen in charts:
            ink = [
                (x, y)
                for x, y in _quad_pixels(screen["quad"])
                if pixels[x, y][1] > pixels[x, y][0] + 15
                and pixels[x, y][2] > pixels[x, y][0] + 10
                and pixels[x, y][1] > 50
            ]
            assert ink == [], (
                f"{screen['id']}: {len(ink)} painted px inside the chart quad"
            )


def test_the_manifests_chart_screens_match_the_intakes_own_derivation():
    """The manifest's `quad`/`x`/`y`/`w`/`h` are a COPY of what
    scripts/prepare_plate.py's SCREEN_FRAMES + screen_quad() emit - the
    intake's own `main()` prints exactly this so Task 2 could paste it
    rather than re-measure by hand (see its docstring: "two hand-kept copies
    of one rect is how a candle ends up drawn 6px off the painted glass").
    KI-052 was exactly that: the manifest and the intake agreed with each
    other, both flat-bottomed, because both were hand-derived from the same
    wrong assumption. This pins the two sources together so a future
    edit to one without the other - including a repaint that re-runs the
    intake and regenerates the old flat bottom - fails here instead of
    shipping quietly."""
    from scripts.prepare_plate import SCREEN_FRAMES, screen_quad

    manifest = load_manifest()
    charts = [s for s in manifest.screens if s.get("role") == "chart"]
    assert charts, "no chart screens to check"
    for screen in charts:
        assert screen["id"] in SCREEN_FRAMES, (
            f"{screen['id']}: no matching frame in scripts.prepare_plate"
        )
        quad = screen_quad(SCREEN_FRAMES[screen["id"]])
        assert [list(corner) for corner in quad] == screen["quad"], (
            f"{screen['id']}: manifest quad disagrees with the intake's own "
            "screen_quad(SCREEN_FRAMES[...])"
        )
        xs = [c[0] for c in quad]
        x, w = min(xs), max(xs) - min(xs)
        y = max(quad[0][1], quad[1][1])
        h = min(quad[2][1], quad[3][1]) - y
        assert (screen["x"], screen["y"], screen["w"], screen["h"]) == (x, y, w, h), (
            f"{screen['id']}: manifest rect disagrees with the intake's own "
            "rect-from-quad formula"
        )


def test_the_cast_stands_clear_of_the_bands():
    """The banner and the price band are drawn over the room. A cast anchor
    inside either one puts a character behind text for the whole broadcast."""
    manifest = load_manifest()
    _, height = manifest.canvas
    # `cast` also carries room-wide numbers (`scale`, `rig_height`) since
    # Sprint 16; `characters()` is the one place that tells them apart.
    for name, anchor in manifest.characters().items():
        assert anchor["base_y"] > manifest.bands["top"], name
        assert anchor["base_y"] < height - manifest.bands["bottom"], name


def test_a_missing_manifest_is_absence_not_an_exception(tmp_path):
    """The renderer degrades to the procedural room when the plate is absent.
    A loader that raised would turn a soft failure into a hard one."""
    assert load_manifest(tmp_path / "nope.json") is None


def test_a_manifest_that_disagrees_with_the_watchlist_is_reported(tmp_path):
    """Adding a symbol acquires an art step. Silence here would render a room
    that quietly omits a symbol the pipeline is trading."""
    path = tmp_path / "plate.json"
    path.write_text(json.dumps(_minimal()))
    manifest = load_manifest(path)
    problems = watchlist_disagreements(manifest, ["BTCUSDT", "ETHUSDT"])
    assert any("ETHUSDT" in problem for problem in problems)


def test_an_unpainted_symbol_is_told_about_the_spare_tubes(tmp_path):
    """This plate paints four tubes and only two are assigned, so the art step
    the spec warned about is not always an art step. Saying so where the
    warning is read is the difference between a repaint and a manifest edit."""
    path = tmp_path / "plate.json"
    path.write_text(json.dumps(_minimal(spare_tubes=[{"x": 1, "base_y": 2}])))
    manifest = load_manifest(path)
    problems = watchlist_disagreements(manifest, ["BTCUSDT", "SOLUSDT"])
    assert any("SOLUSDT" in problem and "spare" in problem for problem in problems)


def test_agreement_is_silent():
    manifest = load_manifest()
    assert watchlist_disagreements(manifest, list(manifest.symbols)) == []


def test_a_malformed_manifest_is_absence_too(tmp_path):
    path = tmp_path / "plate.json"
    path.write_text("{ not json")
    assert load_manifest(path) is None


def test_a_manifest_missing_its_canvas_is_absence_too(tmp_path):
    """Absence, not a half-built manifest: every anchor is expressed in canvas
    pixels, so a manifest that cannot say how big the canvas is cannot place
    anything."""
    path = tmp_path / "plate.json"
    broken = _minimal()
    del broken["canvas"]
    path.write_text(json.dumps(broken))
    assert load_manifest(path) is None


# P5 adds the detector the ticket's own Step 7 asks a human for. "Confirm the
# pillars sit IN the painted tubes" needs eyes on a live OBS scene, which is
# X2; a bad measurement between now and then would otherwise reach air with
# nothing standing in its way. These are the parts arithmetic can check.

# Where drawPillars writes a tube's two labels, relative to its painted base
# (api/templates/world.html, the label() calls in drawPillars).
LABEL_ROWS = (12, 30)


def test_a_tubes_labels_stay_clear_of_the_bottom_band():
    """A tube measured low enough puts its own name under the price band —
    legible in the browser, covered on the stream."""
    manifest = load_manifest()
    _, height = manifest.canvas
    floor = height - manifest.bands["bottom"]
    for tube in manifest.tubes:
        for row in LABEL_ROWS:
            assert tube["base_y"] + row < floor, f"{tube['symbol']} label at +{row}"


def test_every_cast_anchor_stands_inside_the_frame():
    """`base_y` is already bounded against the bands; `x` was not bounded at
    all, and a character anchored off-canvas is invisible rather than wrong."""
    manifest = load_manifest()
    width, _ = manifest.canvas
    for name, anchor in manifest.characters().items():
        assert 0 < anchor["x"] < width, name


def test_a_pillar_centred_in_its_bore_stays_inside_the_frame():
    """drawPillars centres the bar on `tube.x` and draws it `tube.width` wide,
    so a bore measured near an edge clips."""
    manifest = load_manifest()
    width, _ = manifest.canvas
    for tube in manifest.tubes:
        left = tube["x"] - tube["width"] / 2
        assert left >= 0 and left + tube["width"] <= width, tube["symbol"]


# C2 review round 1, Finding 1 correction: the reviewer's first instruction
# ("read the seat width from the plate manifest") named a field that did not
# exist yet - `cast.trader` carried only x/base_y/pose. This is that field.


def test_the_seat_is_a_rect_inside_the_canvas():
    """Same shape of check as `test_every_chart_screen_is_a_rect_inside_the_
    canvas` - a seat measured off the edge of the frame is not a seat the
    trader can be drawn inside of."""
    manifest = load_manifest()
    width, height = manifest.canvas
    seat = manifest.cast["trader"]["seat"]
    assert seat["x"] >= 0 and seat["y"] >= 0
    assert seat["x"] + seat["width"] <= width
    assert seat["y"] <= height


@needs_node
def test_the_seated_rig_fits_inside_the_manifest_seat():
    """The check `test_the_seated_rig_fits_inside_the_painted_seat_not_just_
    the_backrest` (tests/api/test_world_page.py) runs, except the seat comes
    from THIS manifest instead of a constant duplicated in that test file -
    so a repaint that moves or narrows the chair has exactly one number to
    update, and a stale one is what this test catches.

    Review round 2, Finding 4: an aggregate width check (`rig width <= seat
    width`) is position-blind - it stayed green while the rig, composited on
    the anchor (`cast.trader.x`), sat measurably off the seat this file
    measured. Composites the rig at the manifest's own anchor the same way
    `positionCharacters` does (`container.x` = the anchor, every local
    coordinate scaled by CAST_SCALE from there) and checks both edges
    against the seat rect, not just their difference.

    Mutation-checked against a deliberately mis-measured manifest: narrowing
    `seat.width` below the rig's real extent, and shifting the anchor
    off-centre, must each fail this test, or the check is decorative.
    (Verified by hand during review, not asserted here - asserting a
    specific mis-measured value would just be a second magic number
    standing in for the first one review round 1 objected to.)

    Sprint 16: the anchor in question is `sit_anchor.x`, not `cast.trader.x`.
    `positionCharacters` stopped using `x` for a seated pose when the plate
    started measuring the hips separately from the seat, so shifting `x` now
    moves nothing and mutating it would prove nothing. At the shipped scale
    the fit is 17px of slack on the left and **2px on the right** - the band
    is nearly exhausted, so a wider rig or a further-right anchor needs the
    seat re-measured rather than nudged.
    """
    manifest = load_manifest()
    # Sprint 16: the rig is composited on `sit_anchor` - where the hips go -
    # not on `x`, which `positionCharacters` stopped using for a seated pose.
    # Grading the coordinate that no longer renders is grading nothing.
    anchor_x = manifest.cast["trader"]["sit_anchor"]["x"]
    seat = manifest.cast["trader"]["seat"]
    source = WORLD_TEMPLATE.read_text()

    driver = (
        # The real manifest, not `null`: `CAST_SCALE` reads `cast.scale` now,
        # and the fallback literal is a different number from the shipped one.
        # This test is about the rig the room actually draws, so it has to run
        # the scale the room actually uses.
        f"const PLATE = {json.dumps(manifest.as_dict())};\n"
        "const plateReady = true;\n"
        "const CELL = 4;\n"
        # KI-051 (Task 6): `BODY_RIM` needs a real `.color` now - it feeds
        # `BODY_RIM_SHADED` via `shade()`. A plain `{}` rim (this driver's
        # value before Task 6) would still run, since `shade()`'s own NaN
        # guard falls back rather than throwing - but it would silently
        # exercise nothing of the new rim colour path.
        "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        # `LIGHT` computed the same way `api/main.py` computes it for the
        # real page (`light_for(manifest)`), not re-typed as a literal - so
        # this driver runs the same measured direction the shipped room does.
        f"const LIGHT = {as_json(light_for(manifest))};\n"
        + """
        class FakeGraphics {
          constructor() { this.calls = []; this.x = 0; }
          roundRect(x, y, w, h, r) { this.calls.push([x, w]); return this; }
          circle(x, y, r) { this.calls.push([x - r, 2 * r]); return this; }
          fill() { return this; }
          stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const bodyCalls = [];
        const fakeGfx = {
          roundRect(x, y, w, h, r) { bodyCalls.push([x, w]); return this; },
          circle(x, y, r) { bodyCalls.push([x - r, 2 * r]); return this; },
          fill() { return this; },
          stroke() { return this; },
        };
        """
    )
    driver += _js_block(source, "function snap(") + "\n"
    driver += _shading_prelude(source)
    driver += _js_block(source, "function seatedRig(") + "\n"
    cast_scale_line = re.search(r"^\s*const CAST_SCALE = .*;$", source, re.M)
    assert cast_scale_line, "the page no longer declares CAST_SCALE as a one-line const"
    driver += cast_scale_line.group(0).strip() + "\n"
    driver += """
        const accents = [];
        seatedRig(fakeGfx, accents);
        function range(calls, offset) {
          let lo = Infinity, hi = -Infinity;
          for (const [x, w] of calls) {
            lo = Math.min(lo, offset + x);
            hi = Math.max(hi, offset + x + w);
          }
          return [lo, hi];
        }
        let [lo, hi] = range(bodyCalls, 0);
        for (const accent of accents) {
          const [alo, ahi] = range(accent.calls, accent.x);
          lo = Math.min(lo, alo);
          hi = Math.max(hi, ahi);
        }
        console.log(JSON.stringify({ lo, hi, castScale: CAST_SCALE }));
    """

    result = subprocess.run(
        [NODE, "-e", driver], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    emitted = json.loads(result.stdout)
    left_screen = anchor_x + emitted["lo"] * emitted["castScale"]
    right_screen = anchor_x + emitted["hi"] * emitted["castScale"]
    assert left_screen >= seat["x"], (
        f"the seated rig's left edge is at screen x={left_screen} - "
        f"{seat['x'] - left_screen}px past the seat's left edge ({seat['x']})"
    )
    assert right_screen <= seat["x"] + seat["width"], (
        f"the seated rig's right edge is at screen x={right_screen} - "
        f"{right_screen - seat['x'] - seat['width']}px past the seat's "
        f"right edge ({seat['x'] + seat['width']})"
    )


def test_no_glow_rect_lands_on_a_chart_screen():
    # KI-056: the glow rects were byte-identical to the two chart screens, so
    # the additive swell painted amber over the live candles exactly when
    # something was happening. The swell lights what is BLANK.
    assert glow_chart_overlaps(load_manifest()) == []


def test_the_overlap_rule_actually_catches_an_overlap():
    # Mutation check. Without this the assertion above passes on an empty
    # glow list, an empty screen list, and a broken intersection test alike.
    #
    # Review round 1: an exact-copy poison rect let an equality-only stand-in
    # for `_rects_intersect` (`a["x"] == b["x"] and ...`) pass every test in
    # this file, including this one - because the only overlap this test
    # exercised was identity. Shifted a few pixels off the chart rect instead:
    # still unmistakably overlapping, but no longer identical, so a
    # comparison that only recognises exact copies now has something to fail
    # on. This is also the shape a real collision actually takes - a glow
    # rect landing partway across a chart, not painted pixel-for-pixel over
    # it.
    manifest = load_manifest()
    chart = next(s for s in manifest.screens if s.get("role") == "chart")
    poisoned = dataclasses.replace(
        manifest,
        glow=manifest.glow + ({
            "id": "poison",
            "x": chart["x"] + 20,
            "y": chart["y"] + 20,
            "w": chart["w"],
            "h": chart["h"],
        },),
    )
    assert glow_chart_overlaps(poisoned) == ["poison"]


def test_the_tube_housings_still_glow():
    # The fix removes the monitors, not the swell. A sprint that quietly
    # deleted the tier swell would also pass the test above.
    assert {g["id"] for g in load_manifest().glow} == {"tubes-floor", "tubes-desk"}


# --- Task 9 review, Important 1: text_surfaces had no re-derivation check ---
#
# Sibling in shape to `test_every_screen_rect_lands_on_glass_the_intake_
# actually_flattened` above: the two colour-scan methods `text_surfaces`
# were measured with, re-run here against the plate PNG itself, so a repaint
# that moves a plinth or the desk lamp's pool of light leaves something to
# catch the drift instead of only a script pasted into a task report.
#
# Same honest scope as that sibling: each check re-derives whether the
# DECLARED rect is still valid material by the rule that measured it. It can
# fail if a repaint shrinks the surface, or opens a seam inside it — the
# failure modes that actually matter, since either one would put a caption on
# top of the wrong thing. It does not prove the declared rect is the largest
# rect the plate could support (the plate might now support more); that is a
# left-on-the-table case, not a wrong one.


def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _clean_rect(pixels, x, y, w, h, ok):
    return all(ok(pixels[i, j]) for j in range(y, y + h) for i in range(x, x + w))


def _plinth_face_ok(rgb):
    """The plinths' matte cylinder-wall colour, as an absolute RGB box —
    the same rule `world/text_layout.py`'s task-9 measurement script used.
    Distinct from the disc's backlit glow (much brighter) and the floor
    beyond the plinth (darker)."""
    r, g, b = rgb
    return 35 <= r <= 55 and 50 <= g <= 72 and 62 <= b <= 90


def test_the_tube_plinth_text_surfaces_are_the_plates_own_clean_faces():
    """Both `tube-plinth-*` `text_surfaces` must still be a fully clean
    rectangle of matte plinth colour — zero stray pixels — on the plate this
    manifest names. A repaint that moves either tube (and so its plinth)
    without re-measuring this block would leave a rect that is no longer the
    plinth at all, and this is what would catch it."""
    manifest = load_manifest()
    with Image.open(PLATE_PNG) as im:
        pixels = im.convert("RGB").load()
        plinths = [
            s for s in manifest.text_surfaces if s["id"].startswith("tube-plinth-")
        ]
        assert plinths, "no tube-plinth-* text_surfaces to check"
        for surface in plinths:
            assert _clean_rect(
                pixels, surface["x"], surface["y"], surface["w"], surface["h"],
                _plinth_face_ok,
            ), f"{surface['id']}: not a clean plinth-face rect on the plate"


def _smoothed_column(pixels, height, x, y0, y1, win=3):
    out = []
    for y in range(y0, y1):
        lo, hi = max(0, y - win // 2), min(height, y + win // 2 + 1)
        rows = range(lo, hi)
        r = sum(pixels[x, yy][0] for yy in rows) / len(rows)
        g = sum(pixels[x, yy][1] for yy in rows) / len(rows)
        b = sum(pixels[x, yy][2] for yy in rows) / len(rows)
        out.append((r, g, b))
    return out


def _smoothed_down_run(pixels, size, x, y0, max_delta=6, win=3, sustain=2):
    """How far a locally-smoothed walk gets below `y0` at column `x` before
    two CONSECUTIVE smoothed steps exceed `max_delta` — gradient-tolerant
    (a slow lamp falloff never trips it) but still edge-sensitive (a real
    seam does), and resistant to the plate's own dithering (a single
    speckled pixel needs a second bad step right behind it to count,
    since dithering alone rarely produces two in a row)."""
    width, height = size
    col = _smoothed_column(pixels, height, x, y0, min(y0 + 120, height), win=win)
    bad_streak = 0
    extend = 0
    for i in range(1, len(col)):
        if _dist(col[i], col[i - 1]) > max_delta:
            bad_streak += 1
            if bad_streak >= sustain:
                return extend - (sustain - 1)
        else:
            bad_streak = 0
            extend = i
    return extend


def test_the_desk_plate_text_surface_is_the_plates_own_smooth_desktop():
    """Task 9 review, Important 2: the first `desk-plate-trader` measurement
    used the plinths' absolute-RGB method, which is blind to the desk lamp's
    own lighting gradient across what is actually one continuous flat
    desktop — it measured a threshold artifact (9px), not the surface's real
    ceiling. This re-derives the surface with the gradient-tolerant method
    that replaced it: at every column the declared rect spans, a smoothed
    downward walk from its own top edge must reach at least its own height
    before hitting a sustained edge. A repaint that opened a seam through the
    middle of the desk, or shrank the lit pool the rect sits in, would leave
    some column short of that and fail here.
    """
    manifest = load_manifest()
    surface = next(
        s for s in manifest.text_surfaces if s["id"] == "desk-plate-trader"
    )
    with Image.open(PLATE_PNG) as im:
        pixels = im.convert("RGB").load()
        size = im.size
        shortfalls = [
            (x, run)
            for x in range(surface["x"], surface["x"] + surface["w"])
            for run in [_smoothed_down_run(pixels, size, x, surface["y"])]
            if run < surface["h"]
        ]
        assert shortfalls == [], (
            f"desk-plate-trader: {len(shortfalls)} column(s) fall short of "
            f"the declared height {surface['h']}, e.g. {shortfalls[:3]}"
        )


# --- Task 10: the model gets a name surface too -----------------------------
#
# The trader sits at a painted desk, which has a plate; the model stands on
# open floor, which the plate never singled out as anything. "no name surface
# for the model" was an artifact of what got painted, not a decision — so
# this measures a floor rect directly under the model's own feet
# (`cast.model`: x=360, base_y=830), the same gradient-tolerant method the
# desk plate above was re-measured with. Full measurement + evidence that no
# smaller, tighter rect was needed is in task-10-report.md.


def test_the_model_floor_text_surface_is_the_plates_own_clean_floor():
    """`floor-model` must still be real, seamless floor on the plate this
    manifest names — the same one-directional shape as the desk-plate
    sibling above: this can fail if a repaint opens a seam through the
    surface or shrinks the clear floor lane below the model's feet, not if
    the plate could now support something bigger than currently claimed.
    """
    manifest = load_manifest()
    surface = next(s for s in manifest.text_surfaces if s["id"] == "floor-model")
    with Image.open(PLATE_PNG) as im:
        pixels = im.convert("RGB").load()
        size = im.size
        shortfalls = [
            (x, run)
            for x in range(surface["x"], surface["x"] + surface["w"])
            for run in [_smoothed_down_run(pixels, size, x, surface["y"])]
            if run < surface["h"]
        ]
        assert shortfalls == [], (
            f"floor-model: {len(shortfalls)} column(s) fall short of the "
            f"declared height {surface['h']}, e.g. {shortfalls[:3]}"
        )


def test_the_model_floor_surface_does_not_collide_with_the_tube_glow():
    """The one piece of context Task 9's own report flagged forward for
    whoever adds the next surface: it must not land on `tubes-floor`, the
    additive glow rect the swell paints over the open floor lane by the
    tubes (KI-056's sibling risk - amber light under a name tag would be the
    same washed-out-content failure, one surface over)."""

    def _intersects(a, b):
        return (
            a["x"] < b["x"] + b["w"]
            and b["x"] < a["x"] + a["w"]
            and a["y"] < b["y"] + b["h"]
            and b["y"] < a["y"] + a["h"]
        )

    manifest = load_manifest()
    floor_model = next(s for s in manifest.text_surfaces if s["id"] == "floor-model")
    tubes_floor = next(g for g in manifest.glow if g["id"] == "tubes-floor")
    assert not _intersects(floor_model, tubes_floor)


# --- Task 10 review round 2: the trader's name comes off the trader's own
# face --------------------------------------------------------------------
#
# `desk-plate-trader` (x=1298, y=543, w=80, h=27) is real, clean desk paint —
# verified above — but the seated rig's own head renders almost entirely in
# front of it at the shipped cast scale/anchor (only ~15px of the 80px width
# is ever clear of the head; see task-10-report.md's "Two more defects"
# section). A surface that is correct-per-manifest and invisible-behind-the-
# figure is still "text where it is not necessary." `desk-plate-trader`
# stays in `text_surfaces`, unreferenced now, for the same reason
# `tube-plinth-*` do: real, verified, measured paint a future task could
# still use (a re-anchored trader, a repaint) — see task-10-report.md.
#
# `desk-face-trader` (x=1109, y=655, w=100, h=30) is the desk's own front
# panel — the same continuous piece of furniture, a different face of it —
# well clear of the rig on the X axis (the rig's own rendered bounds start
# at x≈1298; this surface ends at x=1209, 89px of margin) as well as
# measured clean by the same gradient-tolerant method. The literal "does
# not land under the figure" claim is checked from the rendering side, not
# here — `tests/api/test_world_page.py::
# test_the_name_trader_surface_does_not_intersect_the_seated_rig` — since
# that requires the real seated-rig geometry and CAST_SCALE, which only the
# page source carries.


def test_the_desk_face_trader_text_surface_is_the_plates_own_clean_panel():
    """Same one-directional shape as every sibling in this file: this can
    fail if a repaint opens a seam through the desk's front panel or shrinks
    the clean run below the declared height, not if the plate could now
    support something bigger than currently claimed."""
    manifest = load_manifest()
    surface = next(s for s in manifest.text_surfaces if s["id"] == "desk-face-trader")
    with Image.open(PLATE_PNG) as im:
        pixels = im.convert("RGB").load()
        size = im.size
        shortfalls = [
            (x, run)
            for x in range(surface["x"], surface["x"] + surface["w"])
            for run in [_smoothed_down_run(pixels, size, x, surface["y"])]
            if run < surface["h"]
        ]
        assert shortfalls == [], (
            f"desk-face-trader: {len(shortfalls)} column(s) fall short of "
            f"the declared height {surface['h']}, e.g. {shortfalls[:3]}"
        )


def test_the_desk_face_trader_surface_does_not_collide_with_the_tube_glow():
    """Sibling of the floor-model check above — `desk-face-trader` sits far
    from `tubes-floor` (x:515-690 vs x:1109-1209), but the rule is checked,
    not assumed, the same way every new surface in this sprint has been."""

    def _intersects(a, b):
        return (
            a["x"] < b["x"] + b["w"]
            and b["x"] < a["x"] + a["w"]
            and a["y"] < b["y"] + b["h"]
            and b["y"] < a["y"] + a["h"]
        )

    manifest = load_manifest()
    desk_face = next(s for s in manifest.text_surfaces if s["id"] == "desk-face-trader")
    tubes_floor = next(g for g in manifest.glow if g["id"] == "tubes-floor")
    assert not _intersects(desk_face, tubes_floor)


# --- Task 11: KI-055, the tube fill gets a round top ------------------------
#
# The tubes are painted cylinders seen slightly from above, so the surface of
# whatever fills them is an ellipse - a `roundRect` cap is why they read as
# progress bars. `bore_ry` is the bore's perspective squash: half the vertical
# span between the cap's back-rim and front-rim highlight peaks, the same
# measurement `scan_bore_ry.py` (task-11-12-report.md) makes off the plate.
# Every entry that can stand a pillar needs one - `tubes` AND `spare_tubes`,
# since a symbol promoted from a spare inherits whatever it was given.


def test_every_tube_carries_its_bore_foreshortening():
    # The cap's height is the bore's perspective squash. Measured, not
    # guessed — a repaint at a different camera angle changes it.
    manifest = load_manifest()
    for tube in list(manifest.tubes) + list(manifest.spare_tubes):
        assert "bore_ry" in tube, tube
        assert 0 < tube["bore_ry"] < tube["width"] / 2, tube


def _luminance(rgb):
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _smoothed_column_luminance(pixels, height, x, y0, y1, win=3):
    """Same 3-row smoothing `_smoothed_column` above uses, but returning
    luminance rather than a colour triple: gradient-tolerant, so the desk
    lamp's own falloff across the frame does not bias which row reads as the
    peak."""
    out = []
    for y in range(y0, y1):
        lo, hi = max(0, y - win // 2), min(height, y + win // 2 + 1)
        rows = range(lo, hi)
        out.append(sum(_luminance(pixels[x, yy]) for yy in rows) / len(rows))
    return out


def _measured_bore_ry(pixels, height, tube):
    """Re-derive `bore_ry` from the plate itself.

    The painted cap is a flattened ellipse: a bright rim highlight traces its
    far (back) edge and its near (front) edge, with the lit disc FACE between
    them sitting at a lower, flatter luminance than either rim, and the
    vertical tube wall below the front rim sitting lower still. `bore_ry` is
    half the vertical distance between those two rim peaks - the same span
    PIXI's `ellipse(cx, cy, rx, ry)` draws when `cy` sits between them.
    """
    top = tube["base_y"] - tube["height"]
    x = tube["x"]
    y0, y1 = top - 20, top + 40
    col = _smoothed_column_luminance(pixels, height, x, y0, y1)
    ys = list(range(y0, y1))
    background = sum(col[:8]) / 8
    back_i = next(
        (
            i
            for i in range(1, len(col) - 1)
            if col[i] - background > 40
            and col[i] >= col[i - 1]
            and col[i] >= col[i + 1]
        ),
        None,
    )
    assert back_i is not None, (
        f"no back-rim peak found for tube at x={x} in rows {y0}..{top}"
    )
    front_i = next(
        (
            i
            for i in range(back_i + 2, len(col) - 1)
            if col[i] >= col[i - 1]
            and col[i] >= col[i + 1]
            and min(col[back_i:i]) < col[i] - 15
        ),
        None,
    )
    assert front_i is not None, (
        f"no front-rim peak found for tube at x={x} in rows {ys[back_i]}..{top + 40}"
    )
    return (ys[front_i] - ys[back_i]) / 2


def test_every_tubes_bore_ry_matches_the_plates_own_cap_highlight():
    """KI-055: `bore_ry` is a measurement, not a guess. Re-derive it from the
    plate's own painted cap (the back-rim/front-rim highlight peaks) for
    every tube AND spare_tubes entry, so a repaint that moves a tube without
    re-measuring this leaves a cap that no longer matches its own housing."""
    manifest = load_manifest()
    with Image.open(PLATE_PNG) as im:
        pixels = im.convert("RGB").load()
        _, height = im.size
        for tube in list(manifest.tubes) + list(manifest.spare_tubes):
            measured = _measured_bore_ry(pixels, height, tube)
            assert abs(tube["bore_ry"] - measured) <= 0.6, (
                f"{tube.get('symbol', tube)}: manifest bore_ry="
                f"{tube['bore_ry']} but the plate measures {measured}"
            )
