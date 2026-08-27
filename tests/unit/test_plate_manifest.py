"""The manifest is the seam between an art step and a code step.

Spec: Docs/world-room-plate.md -> "Symbols and the plate" (vault).

A repaint is a new PNG plus a new manifest - no code change. That only holds if
the loader validates what it is handed, so these are the validations. Two of
them go further and check the manifest against the PNG itself: numbers measured
off an image drift the moment the image is replaced, and a candle drawn 6px off
the painted glass is not something a schema check can see.
"""
import json

from PIL import Image

from world.plate import (
    DEFAULT_MANIFEST_PATH,
    load_manifest,
    watchlist_disagreements,
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
    """The one check that fails if the manifest and the asset ever drift apart:
    the rect a candle is drawn in must contain no painted schematic. The intake
    script flattens quads; the manifest carries the axis-aligned rect inside
    each quad; nothing but this notices if those two stop agreeing."""
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
    for name, anchor in manifest.cast.items():
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
