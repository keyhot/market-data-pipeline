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
    `shade`/`mixHex`, and draw their rim via `rimStroke`/`BODY_RIM_SHADED`.
    Pulled from the real page source, not re-typed, for the reason every
    other driver in this file pulls `snap`/`CAST_SCALE` the same way: a
    second definition drifts from the first and stops proving anything about
    the page that actually ships.
    """
    return (
        _js_block(source, "function mixHex(") + "\n"
        + _js_block(source, "function shade(") + "\n"
        + _js_block(source, "function insetSpan(") + "\n"
        + _js_block(source, "const litRect = ") + "\n"
        + _js_block(source, "const litCircle = ") + "\n"
        + _js_const(source, "BODY_RIM_SHADED")
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


def test_every_screen_rect_lands_on_glass_the_intake_actually_flattened():
    """The rect a candle is drawn in must contain no painted schematic.

    Scope, stated honestly: this rect is derived to sit INSIDE the quad the
    intake flattens, so it can only fail when the manifest is edited to claim
    more glass than the plate has - which is exactly the hand-edit a repaint
    invites. The complementary direction, an intake that under-fills, is
    checked against the frame itself in tests/unit/test_plate_asset.py."""
    manifest = load_manifest()
    with Image.open(PLATE_PNG) as im:
        pixels = im.convert("RGB").load()
        for screen in manifest.screens:
            ink = [
                (x, y)
                for y in range(screen["y"], screen["y"] + screen["h"])
                for x in range(screen["x"], screen["x"] + screen["w"])
                if pixels[x, y][1] > pixels[x, y][0] + 15
                and pixels[x, y][2] > pixels[x, y][0] + 10
                and pixels[x, y][1] > 50
            ]
            assert ink == [], (
                f"{screen['id']}: {len(ink)} painted px inside the chart rect"
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
