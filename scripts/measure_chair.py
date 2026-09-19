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
SKELETON = ROOT / "data" / "visual-qa" / "char-redesign" / "skeleton_ref.json"

# The chair's upholstery is the one blue-grey material in this corner: the wall
# is warm (R > B), the desk and floor are darker and less saturated. Sampled,
# not guessed -- backrest (48,58,76), seat (53,68,86), wall (43,35,39),
# desk (27,28,40), floor (22,29,47).
CHAIR_B_FLOOR = 60
CHAIR_B_OVER_R = 18

# The console on the right wall is also blue-grey paneling and would otherwise
# be read as more chair. The backrest ends well before it.
CHAIR_X_LIMIT = 1505


def _chair(px, x: int, y: int) -> bool:
    r, g, b = px[x, y]
    return b >= CHAIR_B_FLOOR and (b - r) >= CHAIR_B_OVER_R and g >= r


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


# The shipped seated trader, read off `seatedRig` and `character()` so the
# comparison below is against what actually goes out. Crown is the hair
# circle's top: `headY` -108, `litCircle(0, -4, 22)` -> -134. The ankle is
# where the shin rect `litRect(-20, 6, 15, 20, 5)` meets the shoe
# `litRect(-23, 24, 18, 8, 3)` -> +24. Crown-to-ankle, so it is the same
# anatomical span as the reference chain and not a foot-thickness longer.
SHIPPED_CROWN_U = -134
SHIPPED_ANKLE_U = 24
SHIPPED_HEAD_D_U = 44          # litCircle(0, -4, 22) silhouette


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
    shipped_crown_to_ankle = SHIPPED_ANKLE_U - SHIPPED_CROWN_U

    # Where the length actually went missing, limb by limb, in head-diameters.
    parts = {
        "spine pelvis->neck": (dist("pelvis", "chest") + dist("chest", "neck"),
                               72),        # litRect(-24, -78, 48, 72)
        "neck->head centre": (dist("neck", "head"), 20),   # -88 collar to -108
        "thigh hip->knee": (dist("hip_near", "knee_near"), 16),   # y -6..10
        "shin knee->ankle": (dist("knee_near", "ankle_near"), 18),  # y 6..24
    }

    return {
        "ref_head_diameter_px": ref_head_d,
        "head_unit_u": SHIPPED_HEAD_D_U,
        "px_per_head": ref_head_d,
        "heads_tall_reference": ref_crown_to_ankle / ref_head_d,
        "heads_tall_shipped": shipped_crown_to_ankle / SHIPPED_HEAD_D_U,
        "parts": {
            name: {
                "ref_heads": ref_px / ref_head_d,
                "should_be_u": ref_px / ref_head_d * SHIPPED_HEAD_D_U,
                "shipped_u": shipped_u,
            }
            for name, (ref_px, shipped_u) in parts.items()
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

    s = head_unit(chair)
    print(f"\nTHE UNIT: 1 head = {s['px_per_head']:.1f} reference px"
          f" = {s['head_unit_u']} rig units (the approved litCircle(0,-4,22))")

    print("\nTHE PROPORTION DEFECT -- a ratio, so no scale is involved")
    print(f"  reference figure  {s['heads_tall_reference']:.2f} heads, crown to ankle")
    print(f"  shipped figure    {s['heads_tall_shipped']:.2f} heads, crown to ankle")
    print("\n  where the length went missing:")
    print(f"    {'part':<22}{'ref':>7}{'should be':>12}{'ships as':>10}{'':>3}")
    for name, p in s["parts"].items():
        ratio = p["should_be_u"] / p["shipped_u"]
        print(f"    {name:<22}{p['ref_heads']:>6.2f}h{p['should_be_u']:>11.0f}u"
              f"{p['shipped_u']:>9.0f}u   {ratio:>4.1f}x")
    print("\n  The head is right and the TORSO is very nearly right. The defect")
    print("  is concentrated in the legs, which is why the figure reads as a")
    print("  doll rather than as a small person: dolls are short-legged.")
    print("  A redistribution, not the wholesale rescale anthropometry argued for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
