#!/usr/bin/env python3
"""Turn the generated room plate into the shipped asset. Re-runnable, deterministic.

    poetry run python scripts/prepare_plate.py            # the recorded source
    poetry run python scripts/prepare_plate.py other.png  # a different candidate

Pillow is a **dev** dependency (`poetry install --only main` in the Dockerfile
keeps it out of the runtime image), so this runs under poetry, not the system
python3 the plan named — the plan predates pillow existing in the poetry env.
The resample result depends on the Pillow version; this was authored against
12.3.0, and the intake is re-run from the original rather than edited in place.

Three edits, all decided in Docs/world-room-plate.md (vault):

1. **Upscale to 1920x960**, the size of the `world-room` browser source in
   scripts/stream_scene.py::_world_focus. The original is 1456x720 — aspect
   2.0222 against the target's 2.0, so the content is squeezed ~1.1%
   horizontally. Not an integer divisor either, so LANCZOS, and the softening
   of the pixel edges is an accepted cost recorded in the PR.
2. **The generator's watermark is painted out.** It is a bright four-pointed
   star low on the right-hand counter. The plan said to copy the strip directly
   above it; measurement says that strip is the bottom of a plant pot, so each
   repainted pixel is taken from its own side of the counter's edge instead
   (see COUNTER_EDGE).
3. **The two central monitors are flattened to dark glass**, because that is
   where live candles are drawn (Task 11). The peripheral screens keep their
   painted schematic art: abstract circuitry is decor, and nobody mistakes it
   for a market.

Every coordinate below was measured on the upscaled 1920x960 output of THIS
source (sha256 recorded), by locating the monitors' inner frame lines and the
watermark's luminance, not by eye. They are facts about one specific image:
re-measure if the source ever changes.
"""

import argparse
import hashlib
import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
TARGET = (1920, 960)
OUT = REPO / "api" / "static" / "world-plate-btc-eth.png"

# Untracked (`.gitignore:215` excludes data/visual-qa/), and one of four
# candidates generated on 2026-08-25. The hash is the only thing that says
# which one this asset came from.
DEFAULT_SOURCE = (
    REPO / "data" / "visual-qa" / "Generated Image August 25, 2026 - 7_33PM.png"
)
SOURCE_SHA256 = "0f0c26889dcb3a56cc042f10dd17889b230e028508c4fee72cdb2b4cf5993057"

# The watermark, as a shape rather than a rectangle: a rectangle big enough to
# hold the star also holds the plant pot above it, and pasting over the pot is
# a more obvious edit than the watermark was.
WATERMARK_WINDOW = (1795, 838, 1906, 946)  # left, top, right, bottom: search area
WATERMARK_LUMA = 60         # the star's core; nothing cold there is this bright
WATERMARK_GROW = 6          # px; must exceed EDGE_BAND, or the piece of the star lying
                            # across the counter's edge survives in a strip
WATERMARK_TOLERANCE = 8     # per channel, against the flat counter colours

# The star's glow is far wider than its core and nowhere near as bright, so
# brightness alone leaves a halo. Both surfaces in this corner are flat fills,
# so the glow is found as "does not match the surface it is lying on".
COUNTER_FLAT = {True: (1780, 900), False: (1890, 940)}  # above-edge, below-edge samples
EDGE_BAND = 4     # px either side of the counter edge, where a blend is expected
DONOR_REACH = 48  # px; past this a donor is a different part of the room, not this one
POT_SHADOW = (1794, 866, 1842, 886)  # the pot's contact shadow: content, not glow

# The counter's near edge runs diagonally through the watermark, so the patch
# has to know which side of it every repainted pixel is on. Two measured points
# on that edge; the surfaces either side of it are flat, which is why a copied
# donor patch smears and a side-aware fill does not.
COUNTER_EDGE = ((1772, 928), (1826, 910))   # (x, y), (x, y) - slope -1/3

# The painted glass is not an axis-aligned rectangle: both monitors are drawn
# in slight perspective, with vertical left and right edges but top edges that
# rise ~1px per 20px to the right while the bottoms stay flat.
#
# These are the frames themselves - the inner dark line the bezel draws around
# the glass, found per column as the luminance minimum and fitted robustly.
# `top` is (slope, intercept) of that line; every other edge is constant. The
# fill quads are DERIVED from them, so the frame is stated once: P4 and P5 need
# the same numbers, and re-deriving them means redoing the measurement.
SCREEN_FRAMES = {
    "centre-left": {
        "left": 461, "right": 809, "top": (-0.0506, 137.0), "bottom": 352,
    },
    "centre-right": {
        "left": 852, "right": 1152, "top": (-0.0541, 138.8), "bottom": 306,
    },
}
SCREEN_INSET = 2   # px inside the frame line, so a fill never overruns the bezel


def frame_top(frame: dict, x: int) -> int:
    """Where the frame's inner dark line runs at column `x`."""
    slope, intercept = frame["top"]
    return round(slope * x + intercept)


def screen_quad(frame: dict, inset: int = SCREEN_INSET) -> tuple:
    """The glass inside a frame, as corners clockwise from top-left."""
    left = frame["left"] + inset
    right = frame["right"] - inset
    return (
        (left, frame_top(frame, left) + inset),
        (right, frame_top(frame, right) + inset),
        (right, frame["bottom"] - inset),
        (left, frame["bottom"] - inset),
    )


SCREEN_QUADS = {name: screen_quad(frame) for name, frame in SCREEN_FRAMES.items()}
GLASS = (26, 32, 46)  # between the plate's wall (21,26,40) and its lit glass (33,40,54)
SHEEN = (34, 41, 57)      # one band along the top edge, so the glass reads as glass
SHEEN_HEIGHT = 10


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resample_mode(size: tuple[int, int]) -> int:
    """Integer upscales keep pixel art crisp; anything else has to be resampled."""
    width, height = size
    if TARGET[0] % width == 0 and TARGET[1] % height == 0:
        return Image.NEAREST
    return Image.LANCZOS


def _is_terracotta(pixel) -> bool:
    """The plant pot sits directly above the star and is warm where the whole
    rest of the corner is cold. Never repaint it: a notch in the pot is a more
    obvious edit than the watermark was.

    The margins are tight on purpose: the star's topmost pixels sit against the
    pot and pick up a little of its warmth (r-g about 11), while the pot's own
    clay is warmer than that (r-g 17 and up). A looser rule protects the tip of
    the watermark from being painted out at all.
    """
    red, green, blue = pixel
    return red > green + 14 and red > blue + 8


def _edge_offset(x: int, y: int) -> float:
    """Signed distance in y from the counter's near edge. Negative is above it."""
    (x0, y0), (x1, y1) = COUNTER_EDGE
    slope = (y1 - y0) / (x1 - x0)
    return y - (y0 + slope * (x - x0))


def _protected(px, x: int, y: int, counter_luma: int) -> bool:
    """Real content inside the search window that must survive the repaint.

    The pot's contact shadow reaches into the star's own glow, and no rectangle
    separates them - by y=883 they touch. What does separate them is direction:
    a shadow is darker than the counter it falls on, and a watermark is
    brighter. So the shadow is protected where it is dark, and the star lying
    across the same rows is not.
    """
    left, top, right, bottom = POT_SHADOW
    if left <= x < right and top <= y < bottom and sum(px[x, y]) < counter_luma:
        return True
    return _is_terracotta(px[x, y])


def watermark_mask(im: Image.Image) -> set[tuple[int, int]]:
    """Every pixel the generator's star touched: its core and its glow.

    Seeded on "does not match the flat surface it lies on", which is what finds
    the glow, then grown a few pixels. The blend along the counter's own edge is
    excluded from the seed - it is a real gradient, not glow - but the grow step
    still reaches the piece of that edge the star is lying across.
    """
    left, top, right, bottom = WATERMARK_WINDOW
    px = im.load()
    flat = {side: px[x, y] for side, (x, y) in COUNTER_FLAT.items()}
    counter_luma = sum(flat[True])
    core = set()
    for y in range(top, bottom):
        for x in range(left, right):
            if _protected(px, x, y, counter_luma):
                continue
            offset = _edge_offset(x, y)
            if abs(offset) <= EDGE_BAND:
                continue
            reference = flat[offset < 0]
            deviation = max(abs(a - b) for a, b in zip(px[x, y], reference))
            if deviation > WATERMARK_TOLERANCE:
                core.add((x, y))
    grown = set()
    for x, y in core:
        for dy in range(-WATERMARK_GROW, WATERMARK_GROW + 1):
            for dx in range(-WATERMARK_GROW, WATERMARK_GROW + 1):
                if not _protected(px, x + dx, y + dy, counter_luma):
                    grown.add((x + dx, y + dy))
    return grown


def paint_out_watermark(im: Image.Image) -> int:
    """Repaint the star from the counter either side of it, on its own side of the edge.

    The plan said to copy the strip directly above the watermark. Measured, that
    strip is the bottom of a plant pot and its shadow, and copying it hangs a
    dark smear in mid-air where no pot is.

    So each repainted pixel is interpolated between the nearest untouched pixel
    to its left and to its right that lies on the same side of the counter's
    edge. Nearest-single-pixel was tried first and left a visibly flat block:
    the plate is vignetted, so the counter is not one colour, it is a gradient,
    and a patch that ignores that reads as a patch.
    """
    mask = watermark_mask(im)
    if not mask:
        raise SystemExit(
            "no watermark found in WATERMARK_WINDOW - re-measure before shipping"
        )
    px = im.load()
    width, height = im.size

    shadow_left, shadow_top, shadow_right, shadow_bottom = POT_SHADOW

    def clean(nx: int, ny: int, above: bool) -> bool:
        """A donor has to be counter. The pot, and the pot's shadow on the
        counter, are kept in the image but are not counter: interpolating from
        the shadow drags a dark streak out sideways from under the pot."""
        if not (0 <= nx < width and 0 <= ny < height):
            return False
        if (nx, ny) in mask or _is_terracotta(px[nx, ny]):
            return False
        if shadow_left <= nx < shadow_right and shadow_top <= ny < shadow_bottom:
            return False
        offset = _edge_offset(nx, ny)
        return abs(offset) > EDGE_BAND and (offset < 0) == above

    def scan(x: int, y: int, step: int, above: bool):
        """Along the row: the counter's gradient runs horizontally here."""
        for distance in range(1, 240):
            if clean(x + step * distance, y, above):
                return distance, px[x + step * distance, y]
        return None

    def scan_column(x: int, y: int, above: bool):
        """The fallback for pixels sitting on the edge near the frame's right
        border, where one side of the row runs out of image before it runs out
        of edge band."""
        step = -1 if above else 1
        for distance in range(1, 240):
            if clean(x, y + step * distance, above):
                return distance, px[x, y + step * distance]
        return None

    patched = {}
    for x, y in mask:
        above = _edge_offset(x, y) < 0
        left = scan(x, y, -1, above)
        right = scan(x, y, 1, above)
        if not (left or right):
            left = scan_column(x, y, above)
        near = [side for side in (left, right) if side and side[0] <= DONOR_REACH]
        if len(near) == 2:
            (dl, cl), (dr, cr) = near
            weight = dl / (dl + dr)
            colour = tuple(round(a + (b - a) * weight) for a, b in zip(cl, cr))
        elif near:
            colour = near[0][1]
        elif left or right:
            # Both donors are far away - past the pot, or past the plant beside
            # it. Blending two distant samples makes each row pick a different
            # pair and streaks the patch sideways, so take the nearer one.
            colour = min([side for side in (left, right) if side])[1]
        else:
            raise SystemExit(f"no clean counter either side of ({x}, {y}) - re-measure")
        patched[(x, y)] = colour
    for (x, y), colour in patched.items():
        px[x, y] = colour
    return len(mask)


def darken_screens(im: Image.Image) -> None:
    """Flatten the two central monitors to dark glass with a single sheen band."""
    draw = ImageDraw.Draw(im)
    for corners in SCREEN_QUADS.values():
        (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = corners
        draw.polygon([(tlx, tly), (trx, try_), (brx, bry), (blx, bly)], fill=GLASS)
        draw.polygon(
            [
                (tlx, tly),
                (trx, try_),
                (trx, try_ + SHEEN_HEIGHT),
                (tlx, tly + SHEEN_HEIGHT),
            ],
            fill=SHEEN,
        )


def main(source: Path) -> None:
    if not source.exists():
        raise SystemExit(f"source not found: {source}")
    digest = sha256(source)
    if digest != SOURCE_SHA256:
        print(f"WARNING: source sha256 {digest}")
        print(f"         expected      {SOURCE_SHA256}")
        print("         every coordinate here was measured on the expected image.")

    with Image.open(source) as raw:
        im = raw.convert("RGB")
        print(f"source {source.name} {im.size} sha256={digest[:12]}")
        if im.size != TARGET:
            mode = resample_mode(im.size)
            name = "NEAREST" if mode == Image.NEAREST else "LANCZOS"
            src_aspect = im.size[0] / im.size[1]
            aspect = TARGET[0] / TARGET[1]
            print(
                f"resizing {im.size} -> {TARGET} with {name} "
                f"(aspect {src_aspect:.4f} -> {aspect:.4f}, "
                f"{100 * (src_aspect / aspect - 1):+.1f}% horizontal squeeze)"
            )
            im = im.resize(TARGET, mode)
        painted = paint_out_watermark(im)
        darken_screens(im)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        im.save(OUT, format="PNG", optimize=True)
    print(f"watermark: {painted} px repainted around the counter edge {COUNTER_EDGE}")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes)")
    # Emit the screens in manifest form, so Task 2 copies them rather than
    # re-measuring by hand. Two hand-kept copies of one rect is how a candle
    # ends up drawn 6px off the painted glass.
    print("screens for the manifest (rect = the axis-aligned rect inside the quad):")
    for name, corners in SCREEN_QUADS.items():
        xs = [c[0] for c in corners]
        x, w = min(xs), max(xs) - min(xs)
        y = max(corners[0][1], corners[1][1])      # the lower of the two top corners
        h = min(corners[2][1], corners[3][1]) - y  # the higher of the two bottom ones
        print(
            f'  {{"id": "{name}", "x": {x}, "y": {y}, "w": {w}, "h": {h}, '
            f'"quad": {[list(c) for c in corners]}, "role": "chart"}}'
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", nargs="?", default=str(DEFAULT_SOURCE))
    args = parser.parse_args()
    sys.exit(main(Path(args.source)))
