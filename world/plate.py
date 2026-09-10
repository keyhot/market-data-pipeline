"""The plate's anchors, loaded and validated.

Docs/world-room-plate.md (vault) makes the anchors DATA so that repainting the
room for a third symbol is an art step and a measurement step, with no code
change. This module is the only thing that knows the manifest's shape.

Every number in the shipped manifest was measured off
`api/static/world-plate-btc-eth.png` - the tube bores and their housings, the
seat of the painted chair, the clear floor lane, and the two monitor rects the
intake script flattened. `tests/unit/test_plate_manifest.py` checks the ones a
machine can re-derive from the image, so the manifest cannot quietly drift away
from the picture it describes.

`cast.trader.seat` (added Sprint 15, Task 7 review round 1) is the one seat
measurement in that list that did not reach the manifest until this fix:
the seat's material was told apart from the surrounding room by a colour scan
along the row at `base_y` (blue-grey, `b > r + 15`), not by eye - the
contiguous span at that row is `[1299, 1378]`, recorded as the left edge and
width a screen-space rect would use, the same convention `screens` already
uses. It exists so the seated rig's width has something machine-checkable to
fit inside, rather than a number carried only in a test's docstring.

`cast.trader.sit_anchor` (Sprint 16) is the companion measurement `seat` is NOT:
`seat` is the fit budget, `sit_anchor` is where the hips go. On a chair painted
in three-quarter view those are different points, and using one for both is what
put the figure beside the chair (KI-054). At the shipped `cast.scale` the rig
composited on `sit_anchor` clears the seat by 17px on the left and **2px on the
right** - the band is nearly exhausted, so a wider seated rig or a further-right
anchor needs `seat` re-measured off the plate, not nudged.

Pure and DB-free by design: this is read during a page render, and a broken
manifest must degrade to the procedural room rather than raise into it.

`text_surfaces["desk-plate-trader"]` (Sprint 16 Task 9) is real, clean desk
paint - and, at the shipped `cast.scale`/`sit_anchor`, almost entirely
covered by the seated rig's own head (`y=543..570` sits inside the head's
rendered `y=520..705`). It stays in the manifest, unreferenced by any `text`
placement, for the same reason `spare_tubes` and `tube-plinth-*` do: real,
measured paint a re-anchored trader or a repaint could use later - but
anyone reaching for it for a NEW placement should re-check it against the
rig first (`tests/api/test_world_page.py::
test_the_name_trader_surface_does_not_intersect_the_seated_rig` is the
check that caught this the first time). `name-trader` itself now sits on
`desk-plate-trader`'s sibling `desk-face-trader` - the same desk unit's
front panel, well clear of the rig.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "api" / "static" / "world-plate-btc-eth.json"
)


@dataclass(frozen=True)
class PlateManifest:
    plate: str
    canvas: tuple[int, int]
    cell: int
    symbols: tuple[str, ...]
    tubes: tuple[dict, ...]
    cast: dict
    screens: tuple[dict, ...]
    glow: tuple[dict, ...]
    bands: dict
    spare_tubes: tuple[dict, ...] = field(default=())
    light: dict = field(default_factory=dict)
    # Sprint 16 Task 9: where text is allowed to sit. `text_surfaces` is the
    # measured places the plate actually has (a plinth, a desk plate) — NOT
    # the two bands, which `world.text_layout` derives from `bands` + canvas
    # instead of restating. `text` is the placements, each naming one surface.
    text_surfaces: tuple[dict, ...] = field(default=())
    text: tuple[dict, ...] = field(default=())
    # Sprint 16 Task 14 (STRETCH): the painted city's own window lights, each
    # a small rect measured over painted glass with a colour and a blink
    # period. Optional, like `text_surfaces`/`text` above — an older
    # manifest or a repaint that dropped it degrades to a dark, still
    # skyline, never a throw.
    ambient_lights: tuple[dict, ...] = field(default=())

    def as_dict(self) -> dict:
        return {
            "plate": self.plate,
            "canvas": list(self.canvas),
            "cell": self.cell,
            "symbols": list(self.symbols),
            "tubes": [dict(tube) for tube in self.tubes],
            "spare_tubes": [dict(tube) for tube in self.spare_tubes],
            "cast": dict(self.cast),
            "screens": [dict(screen) for screen in self.screens],
            "glow": [dict(glow) for glow in self.glow],
            "ambient_lights": [dict(light) for light in self.ambient_lights],
            "bands": dict(self.bands),
            "light": dict(self.light),
            "text_surfaces": [dict(s) for s in self.text_surfaces],
            "text": [dict(t) for t in self.text],
        }

    def characters(self) -> dict[str, dict]:
        """Only the people in `cast` - not the room-wide numbers beside them.

        Sprint 16 put `scale` and `rig_height` in `cast`, where they belong:
        how big the figures are drawn is a measurement against the painted
        furniture, the same kind of thing as the seat and the tube bores. That
        makes `cast` a mixed block, and "is this entry a character" is the
        question every consumer then has to answer - so it is answered once,
        here, rather than as an `isinstance` restated at each call site.

        A character is an entry carrying an anchor. A future settings key is
        excluded by having no `x`, not by being on a list this has to be kept
        in step with.
        """
        return {
            name: value
            for name, value in self.cast.items()
            if isinstance(value, dict) and "x" in value
        }

    def screen_for(self, symbol: str) -> dict | None:
        """The painted monitor a symbol's candles belong in, if it has one."""
        wanted = symbol.upper()
        for screen in self.screens:
            if str(screen.get("symbol", "")).upper() == wanted:
                return dict(screen)
        return None


def load_manifest(path: Path | None = None) -> PlateManifest | None:
    """Return the manifest, or None if it cannot be read.

    None is a first-class answer: the renderer falls back to the procedural
    room, so a missing or broken manifest must never raise into a page render.
    """
    path = Path(path) if path is not None else DEFAULT_MANIFEST_PATH
    try:
        raw = json.loads(path.read_text())
        canvas = tuple(int(value) for value in raw["canvas"])
        return PlateManifest(
            plate=str(raw["plate"]),
            canvas=(canvas[0], canvas[1]),
            cell=int(raw.get("cell", 4)),
            symbols=tuple(str(symbol).upper() for symbol in raw.get("symbols", ())),
            tubes=tuple(raw.get("tubes", ())),
            spare_tubes=tuple(raw.get("spare_tubes", ())),
            cast=dict(raw.get("cast", {})),
            screens=tuple(raw.get("screens", ())),
            glow=tuple(raw.get("glow", ())),
            ambient_lights=tuple(raw.get("ambient_lights", ())),
            bands=dict(raw.get("bands", {})),
            light=dict(raw.get("light", {})),
            text_surfaces=tuple(raw.get("text_surfaces", ())),
            text=tuple(raw.get("text", ())),
        )
    except FileNotFoundError:
        logger.warning("plate manifest missing", extra={"path": str(path)})
        return None
    except Exception as exc:
        logger.warning(
            "plate manifest unreadable",
            extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
        )
        return None


def _rects_intersect(a: dict, b: dict) -> bool:
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


def glow_chart_overlaps(manifest: PlateManifest | None) -> list[str]:
    """Every glow rect that lands on a screen carrying live content (KI-056).

    The swell's job is to light the surfaces the plate deliberately left
    blank. A monitor with candles in it is no longer blank, and additive amber
    over dark glass turns it olive - worst at high tiers, i.e. exactly when a
    viewer is most likely to be looking.
    """
    if manifest is None:
        return []
    charts = [s for s in manifest.screens if s.get("role") == "chart"]
    return [
        str(glow.get("id"))
        for glow in manifest.glow
        if any(_rects_intersect(glow, chart) for chart in charts)
    ]


def watchlist_disagreements(
    manifest: PlateManifest | None, symbols: list[str]
) -> list[str]:
    """Name every symbol the room cannot draw, and every symbol it draws in vain.

    A startup warning, never a silent mis-render: the plate paints tube bases at
    fixed positions, so a symbol with no painted base has nowhere to stand.

    This plate paints four tubes and assigns two, so an unpainted symbol is
    usually a manifest edit rather than a repaint - which is worth saying in the
    warning itself, where somebody is actually reading it.
    """
    if manifest is None:
        return []
    painted = {symbol.upper() for symbol in manifest.symbols}
    wanted = {symbol.upper() for symbol in symbols}
    spare = len(manifest.spare_tubes)
    plural = "s" if spare != 1 else ""
    hint = (
        f" ({spare} spare tube{plural} painted: assign one in the manifest)"
        if spare
        else " (the plate has no spare tube: this needs a repaint)"
    )
    problems = [
        f"{symbol}: traded but not painted on the plate{hint}"
        for symbol in sorted(wanted - painted)
    ]
    problems += [
        f"{symbol}: painted on the plate but not traded"
        for symbol in sorted(painted - wanted)
    ]
    return problems
