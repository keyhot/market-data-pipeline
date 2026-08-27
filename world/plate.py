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

Pure and DB-free by design: this is read during a page render, and a broken
manifest must degrade to the procedural room rather than raise into it.
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
            "bands": dict(self.bands),
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
            bands=dict(raw.get("bands", {})),
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
