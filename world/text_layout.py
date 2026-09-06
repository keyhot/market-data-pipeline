"""Where words are allowed to be.

The design rule, from the sprint's own brief: text may sit on a surface the
room actually has — a screen, the painted quiet band, a plinth, a desk plate —
but it may not float in the air above an object. It is diegetic: words belong
to things in the room, not to an invisible layer in front of it.

Making the placements DATA is what makes the rule enforceable. A label's
position is checked against the surface it names, so "no text in the air"
becomes an assertion with a mutation test behind it rather than a matter of
taste that drifts back the next time someone needs to say something.
"""

from __future__ import annotations

import json


def _band_surfaces(manifest) -> dict:
    """`band-top` and `band-bottom`, derived from `bands` + `canvas` rather
    than restated as a second pair of literals.

    The manifest already carries the one true measurement
    (`bands: {top, bottom}`), and both band rectangles are arithmetic on that
    plus `canvas` — `band-bottom.y` is `canvas.height - bands.bottom`. Storing
    them again as `{x, y, w, h}` literals would give this project two
    definitions of the same number, and a repaint that changed `bands.bottom`
    would leave the second one silently wrong. This sprint has already
    deleted a duplicate helper for exactly that reason.
    """
    canvas = getattr(manifest, "canvas", None) if manifest is not None else None
    bands = getattr(manifest, "bands", None) if manifest is not None else None
    if not canvas or not bands:
        return {}
    width, height = canvas
    top = bands.get("top", 0)
    bottom = bands.get("bottom", 0)
    return {
        "band-top": {"id": "band-top", "x": 0, "y": 0, "w": width, "h": top},
        "band-bottom": {
            "id": "band-bottom",
            "x": 0,
            "y": height - bottom,
            "w": width,
            "h": bottom,
        },
    }


def _surfaces(manifest) -> dict:
    """Every surface a placement can name: the two derived bands, plus
    whatever the manifest measured directly (plinths, the desk plate)."""
    if manifest is None:
        return {}
    surfaces = _band_surfaces(manifest)
    surfaces.update(
        {str(s["id"]): s for s in getattr(manifest, "text_surfaces", ()) or ()}
    )
    return surfaces


def _fits(placement: dict, surface: dict) -> bool:
    left, top = placement["x"], placement["y"]
    right, bottom = left + placement["w"], top + placement["h"]
    return 0 <= left and 0 <= top and right <= surface["w"] and bottom <= surface["h"]


def floating_text(manifest) -> list[str]:
    """Every placement that escapes the surface it claims to sit on, or
    names a surface that does not exist.

    Loud on purpose: this is the CI-facing half of the rule, so a placement
    with nowhere real to sit is reported by id rather than silently skipped.
    """
    if manifest is None:
        return []
    surfaces = _surfaces(manifest)
    loose = []
    for placement in getattr(manifest, "text", ()) or ():
        surface = surfaces.get(str(placement.get("surface")))
        if surface is None or not _fits(placement, surface):
            loose.append(str(placement.get("id")))
    return loose


def as_json(manifest) -> str:
    """The page's `__TEXT_JSON__`: placements resolved to absolute canvas
    coordinates, ready to draw.

    Deliberately the opposite failure mode from `floating_text` above: a
    placement whose surface cannot be resolved is silently DROPPED here
    rather than raised. That asymmetry is intentional, not an oversight —
    loud in CI (where `floating_text` is the thing under test and a missing
    surface should fail the build), degrading on air (where a broken
    placement should mean one missing label, not a page that stops
    rendering). Degrading the *page* while a broken manifest fails *tests*
    is the same split `world/plate.py`'s own `load_manifest` makes, and the
    KI-050 lesson behind it: a render path must never raise on bad data it
    can instead just omit.
    """
    if manifest is None:
        return "{}"
    surfaces = _surfaces(manifest)
    resolved = {}
    for placement in getattr(manifest, "text", ()) or ():
        surface = surfaces.get(str(placement.get("surface")))
        if surface is None:
            continue
        resolved[str(placement["id"])] = {
            "x": surface["x"] + placement["x"],
            "y": surface["y"] + placement["y"],
            "w": placement["w"],
            "h": placement["h"],
            "size": placement.get("size", 14),
        }
    return json.dumps(resolved)
