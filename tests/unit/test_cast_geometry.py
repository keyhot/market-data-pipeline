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

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from world.plate import load_manifest

# The painted furniture the cast has to belong to, measured off the plate.
# A figure taller than the desk-to-ceiling run is not standing in this room.
ROOM_FIGURE_CEILING = 300

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


def test_the_cast_fits_under_the_room_ceiling():
    # Observed 2026-09-01: at CAST_SCALE 1.5 the model's body ran off the
    # bottom of the frame and its head rivalled the bonsai. Whatever scale
    # ships, the drawn figure has to fit the furniture.
    manifest = load_manifest()
    scale = manifest.cast["scale"]
    assert manifest.cast["rig_height"] * scale <= ROOM_FIGURE_CEILING


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
    if "sit_anchor" in keys:
        raw["cast"]["trader"] = {
            k: v for k, v in raw["cast"]["trader"].items() if k != "sit_anchor"
        }
    return raw


def _node(driver: str):
    result = subprocess.run(
        [NODE, "-e", driver], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _page_const(name: str) -> str:
    source = WORLD_TEMPLATE.read_text()
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
    stripped = json.dumps(_stripped_manifest("scale"))
    emitted = _node(
        f"const PLATE = {stripped};\n{line}\n"
        "console.log(JSON.stringify({scale: CAST_SCALE}));"
    )
    assert emitted["scale"] > 0
    assert 4 * emitted["scale"] == int(4 * emitted["scale"]), (
        "the no-scale fallback has to keep the rendered cell a whole pixel too"
    )

    # The mutation: drop the literal. `PLATE.cast.scale` is undefined, so the
    # layer would be scaled by undefined and the cast would vanish.
    mutated = re.sub(r"\|\|.*;$", ";", line)
    broke = _node(
        f"const PLATE = {stripped};\n{mutated}\n"
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
    source = WORLD_TEMPLATE.read_text()
    block = re.search(
        r"^    function sitAnchorFor\(name\) \{.*?^    \}$", source, re.S | re.M
    )
    assert block, "the page no longer declares sitAnchorFor at one indent level"
    prelude = "const plateReady = true;\n"

    present = _node(
        f"const PLATE = {json.dumps(load_manifest().as_dict())};\n"
        + prelude
        + block.group(0)
        + '\nconsole.log(JSON.stringify(sitAnchorFor("trader")));'
    )
    sit = load_manifest().cast["trader"]["sit_anchor"]
    assert present == {"x": sit["x"], "baseY": sit["y"]}

    for stripped in (_stripped_manifest("sit_anchor"), None):
        emitted = _node(
            f"const PLATE = {json.dumps(stripped)};\n"
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
    source = WORLD_TEMPLATE.read_text()
    body_block = re.search(
        r"^    const BODIES = \{.*?^    \};$", source, re.S | re.M
    )
    assert body_block, "the page no longer declares BODIES as one const block"
    snap_block = re.search(
        r"^    function snap\(.*?^    \}$", source, re.S | re.M
    )
    assert snap_block, "the page no longer declares snap() at one indent level"

    driver = (
        "const CELL = 4;\n"
        "const BODY_FILL = 0xffffff, BODY_RIM = {};\n"
        """
        class FakeGraphics {
          constructor() { this.calls = []; this.x = 0; this.y = 0; }
          roundRect(x, y, w, h) { this.calls.push([y, h]); return this; }
          circle(x, y, r) { this.calls.push([y - r, 2 * r]); return this; }
          fill() { return this; }
          stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        """
        + snap_block.group(0)
        + "\n"
        + body_block.group(0)
        + "\n"
        """
        const heights = {};
        for (const style of Object.keys(BODIES)) {
          const calls = [];
          const gfx = {
            roundRect(x, y, w, h) { calls.push([y, h]); return this; },
            circle(x, y, r) { calls.push([y - r, 2 * r]); return this; },
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
        """
    )
    result = subprocess.run(
        [NODE, "-e", driver], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    drawn = json.loads(result.stdout)
    assert load_manifest().cast["rig_height"] == max(drawn.values()), (
        f"rig_height disagrees with what the page draws: {drawn}"
    )
