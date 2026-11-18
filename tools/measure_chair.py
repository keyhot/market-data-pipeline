#!/usr/bin/env python3
"""Measure the painted chair, and derive the trader's scale from it.

Two questions this answers, both of which the redesign had been answering by
eye or by convention:

1. **How far back does the chair recline?** The reference pose leans at ~50
   degrees from vertical because the reference chair is high-backed with a
   headrest. The plate's chair is not that chair. Its rake is painted into the
   art and can be read off it, so the new pose leans at a MEASURED angle.

2. **How big is the trader?** Not answered here, and deliberately: the sheet
   no longer HAS a `k`. What this prints instead is the one unit the question
   can be asked in without inventing a scale -- the **head diameter** -- and
   the proportion defect stated as a ratio, which needs no scale at all.

**Two dead ends recorded so they are not walked twice.**

*Anthropometry.* Call the seat 45cm, measure px/cm, put a 175cm adult in it.
Tried, and it argued the cast was 2.3x too small. It needs a floor line, and
the floor under this chair is painted in shadow with no clean edge; px/cm
estimates off it ranged 2.7-4.4 and the answer scaled with the guess.

*Chair-to-chair.* Scale the figure by (plate chair span / reference chair
span). Tried, and it gave k = 0.64 against the head's 0.74. It is not a
14%-noisy measurement of one thing, it is a measurement of the **wrong**
thing, and `Docs/character-redesign.md` had already found out why: the
reference chair is high-backed WITH A HEADREST and the plate's is a mid-back
without one. Their spans are not the same anatomical distance, so their ratio
means nothing. The answer moved another 20% depending on which column of the
backrest was called "behind the trader", which is what a meaningless ratio
looks like from the inside.

What survives is the head: `head`->`crown` in the trace is the skull's radius,
and the shipped skull is `litCircle(0, -4, 22)` in `character()` -- a head the
user has already graded and approved at that size. Comparing those two
compares the same anatomical thing in both images and needs no furniture.

Run: poetry run python scripts/measure_chair.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATE_PNG = ROOT / "api" / "static" / "world-plate-btc-eth.png"
PLATE_JSON = ROOT / "api" / "static" / "world-plate-btc-eth.json"
SKELETON = ROOT / "tests" / "fixtures" / "char-redesign" / "skeleton_ref.json"

# The chair's upholstery is the one blue-grey material in this corner: the wall
# is warm (R > B), the desk and floor are darker and less saturated. Sampled,
# not guessed -- backrest (48,58,76), seat (53,68,86), wall (43,35,39),
# desk (27,28,40), floor (22,29,47).
CHAIR_B_FLOOR = 60
CHAIR_B_OVER_R = 18

# The console on the right wall is also blue-grey paneling and would otherwise
# be read as more chair. The backrest ends well before it.
CHAIR_X_LIMIT = 1505

# The camera's elevation, as sin(phi) -- the SAME constant `seatedRig` poses the
# legs with (`api/templates/world.html`), stated once here so the two cannot
# drift. It is only ever used to un-foreshorten a DEPTH, never to derive a
# distance: the plate disagrees with itself about the elevation (castor base 33
# degrees, tube bores 14) and the answers below are checked for insensitivity to
# that rather than resting on it.
SIN_PHI = 0.358


def _chair(px, x: int, y: int) -> bool:
    r, g, b = px[x, y]
    return b >= CHAIR_B_FLOOR and (b - r) >= CHAIR_B_OVER_R and g >= r


# The painted floor, read off the chair's own base.
#
# The anthropometry dead end above needed a floor LINE, and this plate paints
# the floor in shadow with no clean edge. The five-star base does not care: its
# castors are painted, they are the same blue-grey upholstery material as the
# rest of the chair, and every one of them is standing ON the floor. The bottom
# of that silhouette is therefore a set of real floor contact points, which is
# all a bracket needs.
#
# It is a BAND and not a line on purpose, and that is the whole finding. The
# floor is a plane seen in three-quarter view, so contact y depends on depth:
# the far castor touches at ~768 and the nearest at ~844. Converting that into
# a seat-to-floor DISTANCE needs the projection, and the projection is exactly
# what is not recoverable here -- estimates swing by 2x depending on which
# contact point you nominate as "under the chair", which is the same signature
# as the 2.7-4.4 px/cm spread that killed anthropometry. So this deliberately
# does not try. A bracket needs no projection: a seated figure's feet cannot be
# above the furthest painted contact point, nor below the nearest one, whatever
# the plane does in between.
BASE_Y_RANGE = (735, 900)
BASE_X_RANGE = (1240, 1500)


def measure_floor_band() -> tuple[int, int]:
    """(far, near) screen y of the floor, from the chair base's contact points.

    The ONE definition of where the painted floor is. `tests/api/
    test_world_page.py::test_the_seated_figure_puts_its_feet_on_the_painted_
    floor` imports it rather than restating 768/844 as literals -- the same
    discipline `find_episodes` and `is_degraded_stream_event` get in CLAUDE.md,
    and for the same reason: a number typed into a test is a number that stops
    tracking the art the moment the plate is repainted.
    """
    from PIL import Image

    px = Image.open(PLATE_PNG).convert("RGB").load()
    contacts = []
    for x in range(BASE_X_RANGE[0], BASE_X_RANGE[1], 2):
        ys = [y for y in range(BASE_Y_RANGE[0], BASE_Y_RANGE[1]) if _chair(px, x, y)]
        if ys:
            contacts.append(max(ys))
    if not contacts:
        raise SystemExit(
            "no chair-coloured pixels below the seat: the plate was repainted "
            "or the chair predicate no longer matches its upholstery"
        )
    return min(contacts), max(contacts)


# The seat's own top face, which is the one surface on this chair that lies flat
# on the ground plane -- so its edges ARE the room's two ground axes, and the
# direction the chair points can be read off them without a projection.
#
# Only two of the cushion's four edges are painted: the upper-left pair. The
# lower-right pair is occluded by the chair's own backrest and base, so it is
# closed by parallelism (R = T + (B - L)) rather than measured. That occlusion
# is not a nuisance, it is the finding: a backrest standing on the NEAR edge is
# what hides it.
SEAT_TOP_ROWS = (667, 728)          # below the backrest's lit left edge strip
SEAT_X_RANGE = (1230, 1470)


def _is_cushion(px, x: int, y: int) -> bool:
    """The seat's lit top face: the brightest upholstery band on the chair.

    Tighter than `_chair`, deliberately -- the backrest's front face is the
    same material two values darker, and the whole measurement below depends
    on reading the CUSHION's edge rather than the slab standing behind it.
    """
    r, g, b = px[x, y]
    return 44 <= r <= 64 and 58 <= g <= 80 and 74 <= b <= 98


def measure_seat_axes() -> dict:
    """The chair's two ground axes and the direction it faces, off the cushion.

    The question this answers is which way the trader should be pointed, and
    the honest version of it is not "which way does the chair look like it
    points" but "which cushion edge does the BACKREST stand on". The facing is
    perpendicular to that edge, away from it.

    That is measurable, and it does not need the occluded edges: the backrest
    is a slab whose width runs along the edge it stands on, so its CROWN line
    is parallel to that edge. The crown falls 569 -> 533 over x 1388 -> 1490
    (slope -0.35), which is the upper-left edge's slope (-0.32) and not the
    lower-left edge's (+0.31). So the backrest's width axis is the T->L axis,
    it stands on the edge parallel to that one -- the lower-right edge, the
    hidden one -- and the chair faces along the OTHER axis, up-left, out over
    the cushion's front-left corner toward the desk.

    It is worth saying why that is not obvious from looking: the backrest is
    painted up-and-right of the cushion, which reads as "the back is at the
    back" and puts the chair facing down-left, out into the room. It is the
    near-side edge seen over the top of a tall slab from a camera above it.
    The crown line settles it; the eye does not.

    Returned in SCREEN pixels (the vectors) and in ground units (the lengths,
    with depth un-foreshortened through SIN_PHI), because the page needs the
    first and the scale bracket needs the second.
    """
    from PIL import Image

    px = Image.open(PLATE_PNG).convert("RGB").load()

    left_edge = {}
    for y in range(*SEAT_TOP_ROWS):
        xs = [x for x in range(*SEAT_X_RANGE) if _is_cushion(px, x, y)]
        if xs:
            left_edge[y] = min(xs)
    if len(left_edge) < 20:
        raise SystemExit(
            "the seat's top face is not where it was painted: the cushion "
            "predicate found nothing to fit two edges to"
        )

    # The L corner is where the two visible edges meet -- the leftmost point of
    # the silhouette, which is a corner rather than a tangent because both
    # edges are straight.
    apex_y = min(left_edge, key=lambda y: left_edge[y])

    def _fit(rows: list[int]) -> tuple[float, float]:
        """x = a*y + b, least squares. Fitted, not cornered: two single pixels
        would carry the plate's own dithering straight into the angle."""
        mean_y = sum(rows) / len(rows)
        mean_x = sum(left_edge[y] for y in rows) / len(rows)
        cov = sum((y - mean_y) * (left_edge[y] - mean_x) for y in rows)
        var = sum((y - mean_y) ** 2 for y in rows)
        return cov / var, mean_x - (cov / var) * mean_y

    back_rows = [y for y in left_edge if SEAT_TOP_ROWS[0] + 3 <= y <= apex_y - 2]
    front_rows = [y for y in left_edge if apex_y + 2 <= y <= SEAT_TOP_ROWS[1] - 3]
    a_far, b_far = _fit(back_rows)
    a_near, b_near = _fit(front_rows)

    top_y, bottom_y = float(SEAT_TOP_ROWS[0]), float(SEAT_TOP_ROWS[1] - 1)
    T = (a_far * top_y + b_far, top_y)
    L = ((a_far * apex_y + b_far + a_near * apex_y + b_near) / 2, float(apex_y))
    B = (a_near * bottom_y + b_near, bottom_y)
    R = (T[0] + B[0] - L[0], T[1] + B[1] - L[1])

    # The axis the backrest's width runs along (T->L), and the one the chair
    # faces along (B->L). Both point left; they differ in whether they rise.
    across = (L[0] - T[0], L[1] - T[1])
    facing = (L[0] - B[0], L[1] - B[1])

    def _ground(v: tuple[float, float]) -> tuple[float, float]:
        return (v[0], v[1] / SIN_PHI)

    g_across, g_facing = _ground(across), _ground(facing)
    dot = g_across[0] * g_facing[0] + g_across[1] * g_facing[1]
    span_across = math.hypot(*g_across)
    span_facing = math.hypot(*g_facing)

    return {
        "corners": {"T": T, "L": L, "B": B, "R_by_parallelism": R},
        # What the page is given: a measured screen vector, never an angle.
        "facing_screen": facing,
        "across_screen": across,
        # What a scale bracket is allowed to compare a body against: the seat
        # in the same flat-ground metric the rig's own lateral axis lives in.
        "seat_width_u": span_across,
        "seat_depth_u": span_facing,
        # A grade on SIN_PHI rather than a use of it: a real seat is square in
        # plan, so these two axes should come out perpendicular. They do, to
        # within 6 degrees, which is the same "independent agreement" the
        # traced legs gave the floor band.
        "axes_angle_deg": math.degrees(math.acos(dot / (span_across * span_facing))),
        "yaw_deg": math.degrees(math.atan2(g_facing[0], g_facing[1])),
    }


def measure_sit_anchor(back_fraction: float = 0.35) -> dict:
    """Where the hips go, and the cushion's own width there.

    Both fall out of `measure_seat_axes`, and neither is a number anyone should
    type again. The seat's centre line runs from the middle of the front edge
    to the middle of the back edge, and the hips sit `back_fraction` of it in
    front of the back edge.

    The rule is that his BACK rests against the backrest, not inside it. The
    back edge measured here is the cushion's, and the backrest stands forward
    of it, so "a fifth of the depth" (the old default) put the hip joint so far
    back that the painted backrest — drawn over him by the `foreground` mask —
    hid 50 of the torso's 80 px: a stick of sweater standing on a full-width
    seat (increment 21, "I don't like how the torso connects to the ass").
    0.35 shows 45 of 80. 0.4 shows more but his back then overruns the cushion
    chord at that row and his toe's bounding box reaches `desk-face-trader`;
    0.35 clears that plate by 0.07 px, so any further forward needs the name
    surface re-chosen.

    `seat_width` is the cushion's own horizontal chord AT that row, which is
    the budget the rig has to fit inside. The 79px this replaced was measured
    somewhere else on a parallelogram — a chord through a diamond depends
    entirely on which row you take it at, and 79 was narrow enough to forbid
    any figure big enough for the chair (`test_the_seated_figure_is_drawn_to_
    the_chairs_own_proportion`).
    """
    from PIL import Image

    axes = measure_seat_axes()
    corners = axes["corners"]
    front = tuple((a + b) / 2 for a, b in zip(corners["T"], corners["L"]))
    back = tuple((a + b) / 2 for a, b in zip(corners["B"], corners["R_by_parallelism"]))
    hips = tuple(b - back_fraction * (b - f) for f, b in zip(front, back))

    px = Image.open(PLATE_PNG).convert("RGB").load()
    row = round(hips[1])
    xs = [x for x in range(*SEAT_X_RANGE) if _is_cushion(px, x, row)]
    if not xs:
        raise SystemExit(f"no cushion painted at the hip row y={row}")

    return {
        "sit_anchor": {"x": round(hips[0]), "y": row},
        "seat": {"x": min(xs), "y": row, "width": max(xs) - min(xs)},
        "front_mid": front,
        "back_mid": back,
    }



# The cushion as an OCCLUDER (increment 22). Seen from behind, the cushion's
# front edge is its upper-left edge on screen, so a shin hanging below it is
# BEHIND the seat — and the painted seat has to hide it. The lit top face is
# `_is_cushion`; the two side faces are the same upholstery two values down,
# ~(35, 41, 58), which the floor under it (~(24, 27, 39)) never reaches.
OCCLUDER_SEED = (1320, 700)
# rows from the backrest's foot down; columns stop short of the backrest strip,
# which the foreground hides anyway
OCCLUDER_BOX = ((1255, 1431), (666, 765))


def _seat_face(px, x: int, y: int) -> bool:
    r, g, b = px[x, y]
    return b >= 48 and (b - r) >= 18 and g >= r


def _outline(px, x: int, y: int) -> bool:
    r, g, b = px[x, y]
    return r + g + b < 80


def _simplify(points: list, eps: float) -> list:
    """Ramer-Douglas-Peucker over an open polyline."""
    if len(points) < 3:
        return points
    (ax, ay), (bx, by) = points[0], points[-1]
    length = math.hypot(bx - ax, by - ay) or 1.0
    far, index = 0.0, 0
    for i in range(1, len(points) - 1):
        px_, py_ = points[i]
        d = abs((bx - ax) * (ay - py_) - (ax - px_) * (by - ay)) / length
        if d > far:
            far, index = d, i
    if far <= eps:
        return [points[0], points[-1]]
    return _simplify(points[: index + 1], eps)[:-1] + _simplify(points[index:], eps)


def measure_seat_occluder(eps: float = 1.5) -> list[list[int]]:
    """The painted cushion's silhouette — top face, both side faces and the
    dark outline round them — as a screen polygon.

    Flood-filled from a seed on the cushion inside `OCCLUDER_BOX`, then read
    PER COLUMN (topmost and bottommost filled pixel, each pushed out across
    the chair's own near-black outline) rather than hulled: the underside is
    curved, and a hull's chords would hide shin the painting shows. Including
    the outline is what makes a shin that re-emerges below the seat meet the
    chair's line instead of painting a pixel over it.
    """
    from PIL import Image

    px = Image.open(PLATE_PNG).convert("RGB").load()
    (x0, x1), (y0, y1) = OCCLUDER_BOX
    seen, stack = set(), [OCCLUDER_SEED]
    while stack:
        x, y = stack.pop()
        if (x, y) in seen or not (x0 <= x < x1 and y0 <= y < y1):
            continue
        if not _seat_face(px, x, y):
            continue
        seen.add((x, y))
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    if not seen:
        raise SystemExit(f"no cushion painted at the seed {OCCLUDER_SEED}")

    top, bottom = [], []
    for x in sorted({x for x, _ in seen}):
        ys = [y for sx, y in seen if sx == x]
        t, b = min(ys), max(ys)
        while t - 1 >= y0 and _outline(px, x, t - 1) and min(ys) - t < 2:
            t -= 1
        while b + 1 < y1 and _outline(px, x, b + 1) and b - max(ys) < 2:
            b += 1
        top.append((x, t))
        bottom.append((x + 1, b + 1))   # pixel (x, b) covers up to x+1, b+1
    top = _simplify(top, eps)
    bottom = _simplify(bottom[::-1], eps)
    return [[int(x), int(y)] for x, y in top + bottom]

def measure_plate_chair() -> dict:
    from PIL import Image

    px = Image.open(PLATE_PNG).convert("RGB").load()

    # Backrest crown, per column, capped before the wall console.
    tops = {}
    for x in range(1360, CHAIR_X_LIMIT, 2):
        ys = [y for y in range(480, 700) if _chair(px, x, y)]
        if ys:
            tops[x] = min(ys)
    crown_y = min(tops.values())

    # The rake, as a least-squares fit down the backrest's FRONT edge.
    # Bounded deliberately: above y=566 is the crown's rounded corner and
    # below y=662 the seat slab juts out to the left, and either one dragged
    # into the fit turns a straight edge into a slope that is not the rake.
    pts = [
        (y, min(xs))
        for y in range(566, 662)
        if (xs := [x for x in range(1360, 1460) if _chair(px, x, y)])
    ]
    my = sum(p[0] for p in pts) / len(pts)
    mx = sum(p[1] for p in pts) / len(pts)
    cov = sum((y - my) * (x - mx) for y, x in pts)
    var = sum((y - my) ** 2 for y, _ in pts)
    slope = cov / var
    rake = math.degrees(math.atan(-slope))

    # The seat surface under the trader's own anchor, which is NOT the
    # manifest's `seat.y`: the seat recedes up-and-right in this three-quarter
    # view, so it is 683 at the seat's front-left corner and 673 under x=1339.
    manifest = json.loads(PLATE_JSON.read_text())
    trader_x = manifest["cast"]["trader"]["x"]
    seat_ys = [y for y in range(640, 760) if _chair(px, trader_x, y)]

    # The backrest crown BEHIND THE TRADER, not the chair's highest painted
    # pixel -- the far side of the backrest sits higher on screen purely
    # because it is further away.
    behind = min(tops[x] for x in tops if trader_x <= x <= trader_x + 70)

    return {
        "crown_y": crown_y,
        "crown_behind_trader_y": behind,
        "rake_deg": rake,
        "seat_y_under_trader": min(seat_ys),
        "trader_x": trader_x,
        "manifest_seat_y": manifest["cast"]["trader"]["seat"]["y"],
        "cast_scale": manifest["cast"]["scale"],
        "rig_height": manifest["cast"]["rig_height"],
    }


# The head is the unit, and it is the one number the user already graded:
# `character()` paints the trader's hair as `litCircle(0, -4, 22)`, so the
# silhouette is 44 units across and every reference length below is expressed
# as a multiple of it. There is no px/cm anywhere in that sentence.
SHIPPED_HEAD_D_U = 44          # litCircle(0, -4, 22) silhouette

# This script measures the ART. It deliberately does NOT carry a copy of what
# the page currently draws.
#
# It used to. `SHIPPED_CROWN_U`/`SHIPPED_ANKLE_U` and a per-part `shipped_u`
# column were hand-typed literals, and they froze at the pre-increment-3
# figure: for three increments after the legs were rebuilt to their traced
# lengths this script went on reporting "thigh ships as 16u, 3.6x short" and
# "the shipped figure is 3.59 heads" against a page that had shipped 57.5u
# and 5.50 heads since `6d79698`. A measuring tool that reports a fixed defect
# is worse than one that reports nothing: the next session re-fixes the legs.
#
# So the split is now explicit. This script says what the REFERENCE asks for;
# `tests/api/test_world_page.py` runs the real `seatedRig` in node and grades
# what the PAGE does against these targets. Only one of those two can go
# stale without a test going red, and it is no longer this one.


def head_unit(chair: dict) -> dict:
    """The head diameter as the sheet's unit, and the scale-free defect.

    Every bone the re-posed figure draws is authored as a RATIO of the head
    diameter, measured off the trace, times the approved 44 units. There is no
    `k` anywhere in that sentence -- which is the point. The one number that
    could have been wrong is the head, and it is the one number already graded.
    """
    ref = json.loads(SKELETON.read_text())
    J = ref["joints"]

    def dist(a: str, b: str) -> float:
        return math.hypot(J[a]["x"] - J[b]["x"], J[a]["y"] - J[b]["y"])

    ref_head_d = dist("head", "crown") * 2

    # Crown to ankle. Standing chain, because the reference pose is reclined
    # and its crown-to-toe SPAN is a diagonal, not a height; and stopping at
    # the ankle because the trace's `ankle`->`toe` runs mostly sideways and
    # would add length the figure does not have vertically.
    ref_crown_to_ankle = (
        dist("pelvis", "chest") + dist("chest", "neck")
        + dist("neck", "head") + dist("head", "crown")
        + dist("hip_near", "knee_near") + dist("knee_near", "ankle_near")
    )

    # The VISIBLE neck, which is not `neck`->`head` and this is the whole
    # trick. `head` is traced at the head's CENTRE, so `neck`->`head` is a
    # neck plus a head radius bundled together; subtracting the radius leaves
    # the bare span of skin between the shoulder line and the chin. It comes
    # out about a sixth of a head -- small, which is exactly why a rig can
    # bury it and still look "nearly right" on every total-height check.
    ref_head_r = dist("head", "crown")
    ref_neck = dist("neck", "head") - ref_head_r

    # Where the length goes, limb by limb, in head-diameters. Reference only:
    # what the page does with these is the suite's business, not this file's.
    parts = {
        "spine pelvis->neck": dist("pelvis", "chest") + dist("chest", "neck"),
        "neck->head centre": dist("neck", "head"),
        "visible neck (shoulder->chin)": ref_neck,
        "thigh hip->knee": dist("hip_near", "knee_near"),
        "shin knee->ankle": dist("knee_near", "ankle_near"),
    }

    return {
        "ref_head_diameter_px": ref_head_d,
        "head_unit_u": SHIPPED_HEAD_D_U,
        "px_per_head": ref_head_d,
        "heads_tall_reference": ref_crown_to_ankle / ref_head_d,
        "parts": {
            name: {
                "ref_heads": ref_px / ref_head_d,
                "should_be_u": ref_px / ref_head_d * SHIPPED_HEAD_D_U,
            }
            for name, ref_px in parts.items()
        },
    }


def main() -> int:
    chair = measure_plate_chair()
    print("PLATE CHAIR (pixel predicate over the painted art)")
    print(f"  backrest crown, highest painted   y = {chair['crown_y']}")
    print("  backrest crown behind the trader  y = "
          f"{chair['crown_behind_trader_y']}")
    print(f"  seat surface under x={chair['trader_x']}          y = "
          f"{chair['seat_y_under_trader']}"
          f"  (manifest seat.y = {chair['manifest_seat_y']})")
    print(f"  RAKE = {chair['rake_deg']:.1f} deg back from vertical")
    print("    -> the recline the painted chair supports. The reference pose")
    print("       leans ~50 deg; that angle belongs to a headrest this chair")
    print("       does not have.")

    far, near = measure_floor_band()
    print(f"  FLOOR BAND = y {far}..{near}, from the base's castor contacts")
    print("    -> far castor to nearest castor. Not a seat-to-floor distance:")
    print("       that needs the projection, and the projection is the thing")
    print("       this room does not give up. It is a bracket, and a bracket")
    print("       is enough to catch a figure floating or sunk into the floor.")

    s = head_unit(chair)
    print(f"\nTHE UNIT: 1 head = {s['px_per_head']:.1f} reference px"
          f" = {s['head_unit_u']} rig units (the approved litCircle(0,-4,22))")

    print("\nWHAT THE REFERENCE ASKS FOR -- ratios, so no scale is involved")
    print(f"  reference figure  {s['heads_tall_reference']:.2f} heads, crown to ankle")
    print(f"\n    {'part':<32}{'ref':>7}{'should be':>12}")
    for name, p in s["parts"].items():
        print(f"    {name:<32}{p['ref_heads']:>6.2f}h{p['should_be_u']:>11.0f}u")
    print("\n  These are TARGETS, not a diff. What the page actually draws is")
    print("  graded by tests/api/test_world_page.py, which runs the real")
    print("  seatedRig in node -- see the note above SHIPPED_HEAD_D_U for why")
    print("  this script no longer keeps its own copy of the shipped numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
