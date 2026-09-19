#!/usr/bin/env python3
"""Assemble the model sheet's prelude out of the page that actually ships.

The sheet exists to decide the trader's volumes and proportions before any of
them are placed in the scene. That decision is only worth anything if the
figure on the sheet is lit by the *room's* code — so none of the shading is
retyped here. `tests/api/test_world_page.py::_shading_prelude` already names
exactly the block a figure needs in order to draw (`mixHex`/`shade`/
`rampLevel`/`litRect`/`litCircle`/`paint`/`rimStyle`/`HUMAN`), and it is
already the project's one answer to "which lines are the shading". Restating
that list here would make a second copy that drifts — the failure the test
helper's own docstring warns about — so it is imported and called.

The page carries server-substituted placeholders (`__LIGHT_JSON__`,
`__BODY_BASE_FILL__`, `__BODY_RIM_FILL__`). A static sheet served by
`http.server` has no FastAPI to fill them, so they are filled here from the
same `world.visuals` / `world.light` / `world.plate` the app fills them from,
rather than from literals typed into the sheet.

Output is generated: `tools/charsheet/prelude.js` is gitignored and rebuilt by
`scripts/sheet_shot.py` on every shot.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.measure_chair import measure_plate_chair  # noqa: E402

PAGE = ROOT / "api" / "templates" / "world.html"
TESTS = ROOT / "tests" / "api" / "test_world_page.py"
SKELETON = ROOT / "data" / "visual-qa" / "char-redesign" / "skeleton_ref.json"
OUT = ROOT / "tools" / "charsheet" / "prelude.js"

# The sheet's canvas, defined once. `scripts/sheet_shot.py` sizes its viewport
# from these same two numbers: a viewport narrower than the sheet crops the
# columns the sheet exists to compare, and it does it silently -- the frame
# still looks finished, it is just missing the subject. Three columns is what
# turned a spare 1280 into a real constraint.
SHEET_W, SHEET_H = 1600, 980


def _human_skull(source: str) -> str:
    """`character()`'s human head, extracted rather than retyped.

    The sheet used to carry its own `litCircle(0, -4, 22)` / `litCircle(0, 2,
    19)`, because the skull is built inside a `FACE_KIND` branch in
    `character()` and there is no `_js_block`-able construct to pull. That copy
    was named as a drift point the moment it was written: the redesign's scope
    is rewriting `seatedRig` AND that skull, so the control column would have
    drawn a new body wearing an old head and it would have read as a defect in
    the new work.

    It matters more now than it did, because the head is no longer just a
    shape on the sheet -- it is the UNIT the re-posed figure is authored in.
    A stale copy would not look like a stale head, it would silently rescale
    the whole figure. So the copy is deleted and the lines are sliced out.
    """
    start = source.find('} else if (FACE_KIND[style] === "human") {')
    if start < 0:
        raise SystemExit(
            "REFUSED: could not find the human-head branch in character(). "
            "The sheet authors the whole figure in head diameters, so a head "
            "it had to guess at would rescale everything silently."
        )
    end = source.find("} else {", start)
    lines = [
        line.strip()
        for line in source[start:end].splitlines()
        if line.strip().startswith("paint(skull,")
    ]
    if len(lines) != 2:
        raise SystemExit(
            f"REFUSED: expected 2 paint(skull, ...) calls in the human branch, "
            f"found {len(lines)}. The head changed shape; the sheet's unit "
            "changed with it and the change must be deliberate."
        )
    # The hair circle's radius is the SHEET'S UNIT -- every bone the re-posed
    # figure draws is a ratio of this head, so it is pulled from the same two
    # lines rather than typed a second time next to them.
    radius = re.search(r"litCircle\(\s*0,\s*-?\d+,\s*(\d+)\s*\)", lines[0])
    if not radius:
        raise SystemExit(
            "REFUSED: the human head's first shape is no longer a litCircle "
            "with a literal radius, and that radius is the sheet's unit."
        )
    body = "\n  ".join(lines)
    return (
        f"const HEAD_RADIUS = {radius.group(1)};\n"
        f"function humanSkull(skull) {{\n  {body}\n}}"
    )


def _test_helpers():
    """`_shading_prelude` and friends, loaded from the test module by path.

    Importing a private helper out of a test file is not the shape to reach
    for twice. It is right here because the alternative is worse in the
    specific way this project keeps getting burned by: the helper IS the
    project's list of "what a figure needs to draw", it is kept current by
    181 tests that fail when the page moves, and a second list maintained
    beside it would go stale silently. Promoting it into a shared module is
    the cleaner end state and is deliberately not bundled into this increment.
    """
    spec = importlib.util.spec_from_file_location("_world_page_tests", TESTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build() -> str:
    from world import light, plate, visuals

    helpers = _test_helpers()
    source = PAGE.read_text()

    manifest = plate.load_manifest()
    if manifest is None:
        raise SystemExit(
            "REFUSED: no plate manifest, so the room's real light is unknown. "
            "A sheet lit by a guess grades a figure that will never exist."
        )

    # The painted plate is a THIRD hard input as of increment 2 -- the sheet's
    # lean is measured off it -- so it gets the same loud refusal the manifest
    # and the skeleton already have. Without this the failure arrives as a
    # Pillow traceback from inside `measure_plate_chair()`, which says where it
    # broke but not what the sheet lost.
    plate_png = ROOT / "api" / "static" / "world-plate-btc-eth.png"
    if not plate_png.exists():
        raise SystemExit(
            f"REFUSED: {plate_png} is missing -- the re-posed column leans at "
            "the rake MEASURED off that art. Without it the only lean "
            "available is a guess, which is what this increment removed."
        )

    # Inlined rather than fetched. The page is static, and a `fetch` that 404s
    # leaves a sheet that draws the control column and silently omits the
    # subject — a picture that lies, which is the one thing `shoot.py` exists
    # to prevent. Absent input fails HERE, loudly, before Chrome starts.
    if not SKELETON.exists():
        raise SystemExit(
            f"REFUSED: {SKELETON} is missing — it is the traced pose the sheet "
            "is a picture OF. It is gitignored; recover it before shooting."
        )

    # The globals every extracted block closes over. `PLATE = null` is not a
    # shortcut: it is what makes `CELL` and `CAST_SCALE` fall back to the
    # shipped 4 and 1.25, so the sheet authors on the same grid the room does.
    # The light, by contrast, must be the real one — hence the manifest above.
    return "\n".join(
        [
            "// GENERATED by scripts/sheet_build.py — do not edit.",
            "// Every block below is sliced out of api/templates/world.html.",
            "const PLATE = null;",
            "const PAINTED_CAST = null;",
            f"const SHEET_W = {SHEET_W};",
            f"const SHEET_H = {SHEET_H};",
            f"const LIGHT = {light.as_json(light.light_for(manifest))};",
            f"const BODY_FILL = {hex(visuals.BODY_BASE_FILL)};",
            f"const BODY_RIM = {{ width: 1.5, color: {hex(visuals.BODY_RIM_FILL)},"
            " alignment: 1 };",
            helpers._js_block(source, "function snap("),
            helpers._js_const(source, "CELL"),
            helpers._js_const(source, "CAST_SCALE"),
            helpers._shading_prelude(source),
            f"const SKELETON = {SKELETON.read_text().strip()};",
            # The painted chair, MEASURED. The re-posed column leans at the
            # rake the art actually has; the reference's ~50 degrees belongs
            # to a headrest this chair does not have, and a number typed into
            # the sheet by eye is how that would have been missed again.
            f"const CHAIR = {json.dumps(measure_plate_chair(), indent=2)};",
            _human_skull(source),
            # The control column: the trader exactly as it ships today. The
            # sheet's whole job is a comparison, and a known-good subject is
            # also what separates a harness failure from an authoring one.
            helpers._js_block(source, "function seatedRig("),
        ]
    )


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build() + "\n")
    print("wrote", OUT, f"({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
