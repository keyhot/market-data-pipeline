"""The room's light, as data.

The plate is painted with one dominant key — the amber desk lamp — and until
this sprint the cast ignored it entirely: every volume was one flat fill with a
hard rim, which is why the figures read as stickers on a painting (KI-051).

The parameters live in the manifest for the same reason every other
measurement does: a repaint is an art step, and re-measuring the lamp must not
be a code change.

Pure and DB-free. Read during a page render, so a broken block degrades to a
flat neutral light rather than raising into the render — the KI-050 lesson,
written first.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class LightModel:
    key: tuple[int, int]
    direction: tuple[float, float]
    warmth: str
    ambient: str
    ramp: dict
    contact: dict


DEFAULT_LIGHT = LightModel(
    key=(960, 0),
    direction=(0.0, 1.0),
    warmth="#ffffff",
    ambient="#1a2030",
    # A flat ramp is the honest neutral: no block means no measured light, and
    # inventing a direction would put shadows on the wrong side of the room.
    ramp={"lit": 0.0, "base": 0.0, "shade": 0.0},
    contact={"opacity": 0.0, "widthScale": 1.0, "height": 0},
)


def light_for(manifest) -> LightModel:
    """The manifest's light model, or the documented neutral default.

    Never raises: a missing manifest, a missing `light` block, a malformed
    one (wrong shape, missing key), or a ramp that isn't monotonic all
    degrade to `DEFAULT_LIGHT` rather than reaching a page render broken
    (KI-050) — a backwards ramp would light the wrong side of a figure,
    which is worse than no lighting model at all.
    """
    block = getattr(manifest, "light", None) if manifest is not None else None
    if not block:
        return DEFAULT_LIGHT
    try:
        key = tuple(int(v) for v in block["key"])
        direction = tuple(float(v) for v in block["direction"])
        ramp = {k: float(block["ramp"][k]) for k in ("lit", "base", "shade")}
        contact = {
            "opacity": float(block["contact"]["opacity"]),
            "widthScale": float(block["contact"]["widthScale"]),
            "height": float(block["contact"]["height"]),
        }
        # A ramp must be monotonic to describe a real light direction — a
        # backwards ramp would light the wrong side of a figure, worse than
        # no lighting model at all. The one exception is an EXPLICITLY flat
        # ramp ({0,0,0}): that isn't malformed, it's an art direction of "no
        # ramp contribution here" — the measured key/warmth/contact still
        # apply, only the shading gradient is neutral.
        flat = ramp == DEFAULT_LIGHT.ramp
        if not (ramp["shade"] < ramp["base"] < ramp["lit"] or flat):
            return DEFAULT_LIGHT
        return LightModel(
            key=(key[0], key[1]),
            direction=(direction[0], direction[1]),
            warmth=str(block["warmth"]),
            ambient=str(block["ambient"]),
            ramp=ramp,
            contact=contact,
        )
    except Exception:
        return DEFAULT_LIGHT


def as_json(model: LightModel) -> str:
    """The page's `__LIGHT_JSON__`. camelCase inside `contact` matches JS."""
    payload = asdict(model)
    payload["key"] = list(model.key)
    payload["direction"] = list(model.direction)
    return json.dumps(payload)
