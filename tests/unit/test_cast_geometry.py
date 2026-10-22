"""The cast has to belong to the furniture it stands among.

Sprint 15 shipped a room with an atmosphere and a cast that did not read as
part of it: the newcomer's verdict was "it has an atmosphere, but I couldn't
really tell what's going on here". A screenshot at the deployed 1920x960
(2026-09-01) settled what the plan could not settle in advance - the figures
are sized by a page constant rather than by anything measured off the plate,
and the seated one is anchored where a body *fits* rather than where its hips
*go*.

These are the machine-checkable half. The other half is a picture, and this
file deliberately does not pretend otherwise: no assertion here grades whether
the room looks right.
"""

from api.templating import WORLD_MODULE_DIR, world_modules, world_page_source
import dataclasses
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from world.plate import load_manifest
from world_node import run_world

WORLD_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "api" / "templates" / "world.html"
)

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_the_seated_anchor_is_distinct_from_the_seat():
    # KI-054: `x` was derived as the seat's geometric centre, and the figure
    # still landed beside the chair. The two are different questions — where a
    # body fits, and where its hips go — so they are two fields now.
    trader = load_manifest().cast["trader"]
    assert "sit_anchor" in trader
    assert set(trader["sit_anchor"]) >= {"x", "y"}


def test_the_seated_figure_lands_inside_its_seat():
    trader = load_manifest().cast["trader"]
    seat, anchor = trader["seat"], trader["sit_anchor"]
    assert seat["x"] <= anchor["x"] <= seat["x"] + seat["width"]


def test_the_cast_block_tells_its_settings_apart_from_its_people():
    """`cast` now holds room-wide numbers (`scale`, `rig_height`) beside the
    per-character anchors, and two existing checks walked `cast.items()` taking
    every value for a character - they broke the moment a scalar landed there.
    "Which of these is a person" is one question, so it gets one answer here
    rather than an `isinstance` restated at each call site.
    """
    manifest = load_manifest()
    people = manifest.characters()
    assert set(people) == {"model", "trader"}
    assert all("x" in anchor and "base_y" in anchor for anchor in people.values())
    assert "scale" not in people and "rig_height" not in people


def _skull_radius_local() -> int:
    """The skull `character()` draws for every body but the orb - read from
    the page's own `litCircle(0, 0, 28)` call (`api/templates/world.html`)
    rather than copied as a second 28, so a redraw that resizes the head
    moves this number with it instead of silently going stale beside it.

    Sprint 16 (KI-051): the skull used to be a bare `skull.circle(snap(0),
    snap(0), 28).fill(BODY_FILL).stroke(BODY_RIM)`; it is now shaded like
    every other volume via `paint(skull, litCircle(0, 0, 28), BODY_FILL)`.
    The three literal args to `litCircle` are the same (cx, cy, radius) the
    old bare call took, so the radius still lives at one place in the page.
    """
    match = re.search(
        r"litCircle\(0, 0, (\d+)\)", world_page_source()
    )
    assert match, "the page no longer draws the skull via litCircle(0, 0, r)"
    return int(match.group(1))


def test_the_seated_heads_width_fits_the_seat_budget():
    """Review round finding: the old `test_the_cast_fits_under_the_room_
    ceiling` (`rig_height * scale <= 300`) could not fail. At the tallest
    drawn body (180 units) it permits any scale up to 1.667, so it passed at
    the rejected 1.5 (270) as readily as at the shipped 1.25 (225) - it was
    RED only while the manifest keys were absent, never once because a scale
    was actually too big. A bound reverse-engineered to sit between 225 and
    270 (e.g. "260") would be the same defect wearing a tighter number, so
    this closes a gap in a check the manifest already grades instead of
    inventing a new one.

    `cast.trader.seat.width` (79, measured off the plate for the chair's own
    cushion - `world/plate.py`'s docstring) is already the established width
    budget for the seated rig: `test_the_seated_rig_fits_inside_the_manifest_
    seat` composites `seatedRig`'s `roundRect` calls against it. But that
    test can only ever see what `seatedRig` draws, and the skull is drawn
    separately, in `character()` - so the rig's actual widest part has never
    been graded against its own budget. Run for real (not eyeballed): the
    widest `roundRect` `seatedRig` draws is the chest, `snap(-24)` wide 48,
    at local x [-24, 24] - the arms (pivoted at x=±14) land inside it. The
    skull is a circle of radius `_skull_radius_local()` (28 local units, read
    from the page rather than retyped) centred on the same local origin, so
    it spans [-28, 28] - a 56-unit diameter, 17% wider than the chest the
    existing test measures. The head, not the chest, is what a repaint could
    push out of budget, and until now nothing graded it.

    This checks the diameter against the budget, not where it lands - an
    aggregate check, same style as the chest's (Round 2 Finding 1), not a
    positional one (Round 2 Finding 4, an already-covered concern for the
    chest). Deliberately: composited at the shipped `sit_anchor.x` (1346)
    with a 35px screen radius (28 * 1.25), the head's screen span is
    [1311, 1381] against a seat of [1299, 1378] - the right edge already
    overshoots the seat by 3px at the scale that shipped. A positional
    variant would fail on the current manifest, not just at the rejected
    1.5, which is exactly why this grades the size of the thing against its
    budget and leaves where it lands to the test that already owns that
    question.

    Break-even is seat_width / 56 = 79 / 56 ≈ 1.411 - inside the gap the
    whole-pixel test (`test_the_manifest_scale_keeps_the_rendered_cell_a_
    whole_pixel`) leaves between the surviving candidates 1.25 and 1.5, and
    not hard against either: shipped 1.25 clears by 9px (70 vs 79), rejected
    1.5 overshoots by 5px (84 vs 79).
    """
    manifest = load_manifest()
    scale = manifest.cast["scale"]
    seat_width = manifest.cast["trader"]["seat"]["width"]
    head_diameter = 2 * _skull_radius_local() * scale
    assert head_diameter <= seat_width, (
        f"the seated head renders {head_diameter}px across at cast.scale "
        f"{scale} - wider than the {seat_width}px seat "
        "(cast.trader.seat.width) it is drawn sitting on"
    )


def test_the_scale_is_manifest_data_not_a_page_constant():
    # The whole point: a repaint re-measures rather than re-codes.
    assert isinstance(load_manifest().cast["scale"], (int, float))


def test_the_manifest_scale_keeps_the_rendered_cell_a_whole_pixel():
    """The jitter guard, moved to where the number now lives.

    `test_animation_displacement_actually_quantises_to_the_grid` and
    `test_the_idle_glance_is_snapped_too` (tests/api/test_world_page.py) both
    assert `CELL * CAST_SCALE` is an integer, but their drivers run with
    `PLATE = null`, so they only ever grade the page's *fallback* literal.
    The scale that actually renders is this one, and it is data now - so a
    repaint that picks 1.35 (a 5.4px rendered cell, which `roundPixels` rounds
    to 5 or 6 by phase: a step that jitters) has to fail somewhere.
    """
    manifest = load_manifest()
    rendered_cell = manifest.cell * manifest.cast["scale"]
    assert rendered_cell == int(rendered_cell), (
        f"cell {manifest.cell} * cast.scale {manifest.cast['scale']} = "
        f"{rendered_cell} screen px - not a whole pixel, so the snapped step "
        "PixiJS draws will jitter between two integers by phase"
    )


def _stripped_manifest(*keys: str) -> dict:
    """The shipped manifest with `cast` keys removed - an older plate, or a
    repaint caught mid-edit. Neither may take the room down with it."""
    raw = load_manifest().as_dict()
    raw["cast"] = {k: v for k, v in raw["cast"].items() if k not in keys}
    for key in ("sit_anchor", "seat_occluder"):
        if key in keys:
            raw["cast"]["trader"] = {
                k: v for k, v in raw["cast"]["trader"].items() if k != key
            }
    return raw


def _painted_cast(manifest: dict | None) -> dict | None:
    """What `/world` injects for the cast (KI-076): people and settings split
    by `PlateManifest.cast_payload()` server-side, so the page cannot index a
    settings key by name. Taken from `world.plate` rather than restated, so a
    change to that split reaches these drivers."""
    if manifest is None:
        return None
    return dataclasses.replace(load_manifest(), cast=manifest["cast"]).cast_payload()


def _node(driver: str):
    result = subprocess.run(
        [NODE, "-e", driver], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _js_const(source: str, name: str) -> str:
    """One `const NAME = ...;` line - mirrors tests/api/test_world_page.py's
    helper of the same name."""
    match = re.search(rf"^\s*const {name} = .*;$", source, re.M)
    assert match, f"the page no longer declares a one-line const {name}"
    return match.group(0).strip() + "\n"


_BODY_HEIGHTS_JS = '''
class FakeGraphics {
  constructor() { this.calls = []; this.x = 0; this.y = 0; }
  roundRect(x, y, w, h) { this.calls.push([y, h]); return this; }
  circle(x, y, r) { this.calls.push([y - r, 2 * r]); return this; }
  poly(f) { const ys = f.filter((_, i) => i % 2 === 1); this.calls.push([Math.min(...ys), Math.max(...ys) - Math.min(...ys)]); return this; }
  fill() { return this; }
  stroke() { return this; }
}
setPixi({ Graphics: FakeGraphics });
const { BODIES } = need(await W("cast.js"), "BODIES");
const heights = {};
for (const style of Object.keys(BODIES)) {
  const calls = [];
  const gfx = {
    roundRect(x, y, w, h) { calls.push([y, h]); return this; },
    circle(x, y, r) { calls.push([y - r, 2 * r]); return this; },
    poly(f) { const ys = f.filter((_, i) => i % 2 === 1); calls.push([Math.min(...ys), Math.max(...ys) - Math.min(...ys)]); return this; },
    fill() { return this; },
    stroke() { return this; },
  };
  const accents = [];
  const headY = BODIES[style](gfx, accents);
  let top = Infinity, bottom = -Infinity;
  const eat = (list, offset) => {
    for (const [y, h] of list) {
      top = Math.min(top, offset + y);
      bottom = Math.max(bottom, offset + y + h);
    }
  };
  eat(calls, 0);
  for (const accent of accents) eat(accent.calls, accent.y);
  // Every body but the orb carries a 28-radius skull at `headY`.
  if (style !== "orb") { top = Math.min(top, headY - 28); }
  heights[style] = bottom - top;
}
console.log(JSON.stringify(heights));
'''


def _page_const(name: str) -> str:
    source = world_page_source()
    line = re.search(rf"^\s*const {name} = .*;$", source, re.M)
    assert line, f"the page no longer declares {name} as a one-line const"
    return line.group(0).strip()


@needs_node
def test_a_plate_without_a_cast_scale_still_draws_at_the_literal():
    """The degrade rule: a new draw path never ends in a blank canvas.

    `cast.scale` is data, so a plate can be missing it - an older manifest, a
    repaint half-written. The page must fall back to a literal rather than
    scaling the whole cast layer by `undefined`, which is NaN, which is an
    invisible cast on a painted room nobody can explain.

    Mutation-checked in the same run: with the `|| <literal>` removed the
    fallback yields something unusable, so this test is not decorative.
    """
    line = _page_const("CAST_SCALE")
    cast = json.dumps(_painted_cast(_stripped_manifest("scale")))
    emitted = _node(
        f"const PAINTED_CAST = {cast};\n{line}\n"
        "console.log(JSON.stringify({scale: CAST_SCALE}));"
    )
    assert emitted["scale"] > 0
    assert 4 * emitted["scale"] == int(4 * emitted["scale"]), (
        "the no-scale fallback has to keep the rendered cell a whole pixel too"
    )

    # The mutation: drop the literal. The payload's `scale` is null, so the
    # layer would be scaled by null and the cast would vanish.
    mutated = re.sub(r"\|\|.*;$", ";", line)
    broke = _node(
        f"const PAINTED_CAST = {cast};\n{mutated}\n"
        "console.log(JSON.stringify({scale: CAST_SCALE ?? null}));"
    )
    assert broke["scale"] is None, (
        "without the literal the fallback still produced a usable scale, so "
        "this test would pass with the degrade path deleted"
    )


@needs_node
def test_a_plate_without_a_sit_anchor_keeps_the_standing_anchor():
    """`sitAnchorFor` answers "where do the hips go" with null when the plate
    does not say - and null means `positionCharacters` keeps the anchor that
    shipped before this field existed. A degrade to the previous look, which
    is the rule, rather than to a character at (undefined, undefined).
    """
    source = world_page_source()
    block = re.search(
        r"^    function sitAnchorFor\(name\) \{.*?^    \}$", source, re.S | re.M
    )
    assert block, "the page no longer declares sitAnchorFor at one indent level"
    prelude = "const plateReady = true;\n"

    present = _node(
        f"const PAINTED_CAST = {json.dumps(load_manifest().cast_payload())};\n"
        + prelude
        + block.group(0)
        + '\nconsole.log(JSON.stringify(sitAnchorFor("trader")));'
    )
    sit = load_manifest().cast["trader"]["sit_anchor"]
    assert present == {"x": sit["x"], "baseY": sit["y"]}

    for stripped in (_stripped_manifest("sit_anchor"), None):
        emitted = _node(
            f"const PAINTED_CAST = {json.dumps(_painted_cast(stripped))};\n"
            + prelude
            + block.group(0)
            + '\nconsole.log(JSON.stringify(sitAnchorFor("trader")));'
        )
        assert emitted is None


@needs_node
def test_the_rig_height_is_the_tallest_body_the_page_can_actually_draw():
    """`rig_height` is a measurement of the page, not a wish about it.

    A number typed into the manifest and never checked against the roundRects
    is the same defect as the seat width review round 1 caught: the ceiling
    test above would grade a fiction. This runs the real `BODIES` table and
    takes the true crown-to-feet extent of the tallest standing body (the head
    is a 28-radius skull hung at the y each body returns), because `?style=`
    can put either character in any of them.
    """
    # #42: the real `BODIES` table imported from cast.js (and, through it, the
    # real paint/shade chain, palette, config and LIGHT the page serves) and run
    # under node; only PIXI is faked. Nothing is sliced out of the page text.
    manifest = load_manifest()
    drawn = run_world(_BODY_HEIGHTS_JS)
    assert manifest.cast["rig_height"] == max(drawn.values()), (
        f"rig_height disagrees with what the page draws: {drawn}"
    )


def _occlude_run(painted_cast, plate_ready: bool = True, throw: bool = False):
    source = world_page_source()
    blocks = [
        re.search(rf"^    function {name}\(.*?^    \}}$", source, re.S | re.M)
        for name in ("seatOccluderFor", "occludeBehindSeat")
    ]
    assert all(blocks), "the page no longer declares the seat occluder at one indent level"
    return _node(
        f"const PAINTED_CAST = {json.dumps(painted_cast)};\n"
        f"const plateReady = {json.dumps(plate_ready)};\n"
        + """
        // The mask is drawn on a 2D canvas and handed to PIXI as a Sprite
        // texture; these record the path and where the sprite lands.
        const path = [];
        const ctx = { beginPath() {}, closePath() {}, fill() { path.push("fill"); },
                      moveTo(x, y) { path.push([x, y]); }, lineTo(x, y) { path.push([x, y]); } };
        const document = { createElement: () => ({ getContext: () => ctx }) };
        class FakeGraphics {}
        class FakeSprite { constructor(texture) { this.texture = texture; } }
        const PIXI = {
          Graphics: FakeGraphics, Sprite: FakeSprite,
          Texture: { from: (canvas) => ({ canvas, source: {} }) },
        };
        const added = [];
        const layers = { foreground: { addChild(...k) { added.push(...k); } } };
        const bone = (name, behindSeat) => ({
          name, behindSeat, masked: null,
          setMask(opts) { if (""" + json.dumps(throw) + """) throw new Error("x"); this.masked = opts; },
        });
        const accents = [bone("thigh", false), bone("shin", true),
                         bone("shoe", true), bone("arm", undefined)];
        """
        + "\n".join(b.group(0) for b in blocks)
        + """
        const count = occludeBehindSeat("trader", accents);
        console.log(JSON.stringify({
          count,
          masked: accents.filter((a) => a.masked).map((a) => a.name),
          inverse: accents.filter((a) => a.masked).map((a) => a.masked.inverse),
          ownMasks: new Set(accents.filter((a) => a.masked).map((a) => a.masked.mask)).size,
          inForeground: accents.filter((a) => a.masked).every((a) => added.includes(a.masked.mask)),
          sprites: added.every((m) => m instanceof FakeSprite),
          textures: new Set(added.map((m) => m.texture)).size,
          at: added.map((m) => [m.x, m.y]),
          size: added.length ? [added[0].texture.canvas.width, added[0].texture.canvas.height] : null,
          path,
        }));
        """
    )


@needs_node
def test_the_seat_hides_the_shins_and_nothing_else():
    """Each bone tagged `behindSeat` gets its own INVERSE mask of the measured cushion,
    parented to `layers.foreground` - plate space, so the cut stays on the
    painted seat whatever the figure's container is doing. Nothing above the
    knee is masked: a thigh lies on the cushion, in front of it.
    """
    poly = load_manifest().cast["trader"]["seat_occluder"]
    got = _occlude_run(load_manifest().cast_payload())
    assert got["masked"] == ["shin", "shoe"]
    assert got["count"] == 2
    assert got["inverse"] == [True, True]
    # One mask per bone: a Graphics masking two containers is not something
    # PIXI v8 promises to render.
    assert got["ownMasks"] == 2
    assert got["inForeground"]
    # A SPRITE, never a Graphics: PIXI 8.19's stencil mask (what a Graphics
    # becomes) clips the masked bounds even when inverse, and the keyline
    # filter then cut both shoes off the frame. One texture, drawn once.
    assert got["sprites"] and got["textures"] == 1
    x0 = min(x for x, _ in poly)
    y0 = min(y for _, y in poly)
    assert got["at"] == [[x0, y0]] * 2
    assert got["size"] == [max(x for x, _ in poly) - x0, max(y for _, y in poly) - y0]
    assert got["path"] == [[x - x0, y - y0] for x, y in poly] + ["fill"]


@needs_node
def test_a_plate_without_a_seat_occluder_masks_nothing():
    """No `seat_occluder` (an older plate), no plate, or a mask that throws:
    the legs draw exactly as they did before increment 22 - over the seat,
    never missing - and the throw does not escape into `character()`."""
    for painted, ready in ((_painted_cast(_stripped_manifest("seat_occluder")), True),
                           (load_manifest().cast_payload(), False),
                           (None, True)):
        got = _occlude_run(painted, plate_ready=ready)
        assert got["masked"] == [] and got["count"] == 0 and got["path"] == []
    thrown = _occlude_run(load_manifest().cast_payload(), throw=True)
    assert thrown["masked"] == []
