// /world — Animation: easing, the ANIM table, the seated rig and its animations,
// rest poses, idle/blink/gaze/watch.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import { gesture, restAccents, drawEyes } from "./character.js";
import { LIGHT } from "./config.js";
import { HUMAN } from "./palette.js";
import { mixHex, paint, litRect, litPath } from "./shading.js";
import { CELL, snap } from "./tiers.js";
import { HAND_OUTLINE } from "./cast.js";

    // How far `slump` may drop a head, per body. It isn't a style choice: it's
    // how much room the body leaves. The figure's head already overlaps its
    // chest at rest, so the shared 10 buried a third of it and read blobby
    // instead of dejected.
    //
    // `bars` keeps 12 through increment 11, but NOT for the reason it had:
    // the neck it used to have room to spend is gone, and the machine's head
    // is now mounted INTO the chassis. What justifies 12 today is the 34
    // units of skull standing clear above the chassis top (crown -166 against
    // a chassis at -132) — dropping 12 still leaves two thirds of the head
    // showing, where the figure has only 4 to give. Increment 13's box head
    // stands taller still (box top -174, beacon -194), so 12 is only safer.
    const HEAD_TRAVEL = { bars: 12, figure: 4, candle: 8, monolith: 8, orb: 5 };

    // Each named animation from reactions.py, made real. `phase` runs 0->1;
    // every one returns to rest so nothing is ever left sticky-amped.
    // Motion curves. `Math.sin(p * PI)` — a symmetric bump — is what every
    // animation here used, and a symmetric curve is most of what makes
    // procedural motion read as procedural: bodies gather before they move and
    // settle after they arrive. These are applied **per animation**, not as a
    // blanket substitution: `anticipate` and `snap` deliberately return
    // negative values, and several animations feed the curve straight into a
    // scale, where a big enough negative would drive it through zero — the bug
    // `turn` already shipped once. Ranges are noted for that reason.
    const EASE = {
      // -0.18 .. 1 — gathers downward before it rises.
      anticipate: (p) => (p < 0.22
        ? -0.18 * Math.sin((p / 0.22) * Math.PI)
        : Math.sin(((p - 0.22) / 0.78) * Math.PI)),
      // -0.15 .. 1 — hits hard in the first sixth, then rings down to nothing.
      snap: (p) => (p < 0.16
        ? Math.sin((p / 0.16) * (Math.PI / 2))
        : Math.cos(((p - 0.16) / 0.84) * Math.PI * 1.5) * Math.exp(-(p - 0.16) * 3.4)),
      // 0 .. 1 — arrives fast and hangs there. A body that drops doesn't bounce.
      settle: (p) => 1 - Math.pow(1 - Math.sin(p * Math.PI), 2),
    };

    const ANIM = {
      // Breathing, not bobbing. The inhale takes ~40% of the cycle and the
      // exhale the rest, because an even in-and-out is the tell that nothing
      // is alive in there. The chest rises and widens fractionally; at this
      // amplitude it is felt rather than seen, which is the point — it runs
      // 24/7 behind everything else.
      idle:    (c, p) => { const u = p < 0.4
                              ? Math.sin((p / 0.4) * Math.PI - Math.PI / 2)
                              : Math.sin(((p - 0.4) / 0.6) * Math.PI + Math.PI / 2);
                           c.container.y = snap(c.baseY - u * 1.6);
                           c.container.scale.set(1 - u * 0.006, 1 + u * 0.012); },
      jolt:    (c, p, a) => { const u = EASE.snap(p);
                              c.container.y = snap(c.baseY - u * 26 * a);
                              c.container.scale.set(1 + u * 0.08 * a); },
      shake:   (c, p, a) => { c.container.x = snap(c.baseX + Math.sin(p * 42) * 9 * a * (1 - p)); },
      // Crouches before it leaves the floor, and the squash was inverted: it
      // went wide-and-short at the apex and tall-and-thin in the crouch, which
      // is backwards on both. A body in the air stretches along its travel.
      hop:     (c, p, a) => { const j = EASE.anticipate(p);
                              c.container.y = snap(c.baseY - j * 40 * a);
                              c.container.scale.set(1 - j * 0.05 * a, 1 + j * 0.05 * a); },
      pulse:   (c, p, a) => { c.container.scale.set(1 + Math.sin(p * Math.PI * 2) * 0.11 * a); },
      // 0.09, not 0.16: the container pivots at the feet, so a tall body's
      // head swings the full lever arm and 0.16 read as toppling, not leaning.
      lean:    (c, p, a) => { c.container.rotation = Math.sin(p * Math.PI) * 0.09 * a; },
      shrug:   (c, p, a) => { const u = Math.sin(p * Math.PI);
                              gesture(c, u * a, false);
                              c.container.y = snap(c.baseY - u * 6 * a); },
      slump:   (c, p, a) => { const u = EASE.settle(p);
                              c.container.y = snap(c.baseY + u * 12 * a);
                              c.head.y = snap(c.headBaseY
                                + u * HEAD_TRAVEL[c.style] * a);
                              c.container.scale.set(1, 1 - u * 0.07 * a); },
      cheer:   (c, p, a) => { const u = Math.abs(Math.sin(p * Math.PI * 2));
                              c.container.y = snap(c.baseY - u * 30 * a);
                              gesture(c, u * a, true); },
      wave:    (c, p, a) => { gesture(c, Math.sin(p * Math.PI * 6) * 0.6 * a, false);
                              c.head.rotation = Math.sin(p * Math.PI * 6) * 0.10 * a; },
      sleep:   (c, p) => { const b = Math.sin(p * Math.PI * 2) * 0.02;
                           c.container.scale.set(1 + b, 1 - b); },
      flicker: (c, p) => { c.container.alpha = p % 0.25 < 0.125 ? 0.35 : 1; },
      step:    (c, p, a) => { c.container.x = snap(c.baseX + Math.sin(p * Math.PI) * 26 * a); },
      // A flat character can't rotate away, but it must not vanish trying: the
      // old cos() passed through zero twice a cycle and collapsed to a sliver.
      // Squash to 40% and turn the head — that reads as looking round.
      turn:    (c, p, a) => { const u = Math.cos(p * Math.PI * 2);
                              c.container.scale.x = 0.4 + 0.6 * Math.abs(u);
                              c.head.rotation = -u * 0.22 * a; },
      pan:     (c, p, a) => { c.container.x = snap(c.baseX + Math.sin(p * Math.PI) * 10 * a); },
      talk:    (c, p) => { const o = Math.abs(Math.sin(p * Math.PI * 8));
                           c.mouth.scale.y = 1 + o * 1.6;
                           gesture(c, o * 0.25, true); },
    };

    function playAnimation(char, animation, tier) {
      // A reaction takes the body back off idle: a half-finished weight shift
      // left under an animation is a character reacting from a crooked stance.
      resetIdle(char);
      char.anim = animation;
      char.phase = 0;
      // Tier scales amplitude, not choice: the same reaction reads bigger when
      // the event is bigger — calm that swells, on the character itself.
      char.amp = 0.6 + Math.max(0, Math.min(tier, 3)) * 0.25;
      char.duration = (ANIM_DURATION[animation] ?? 0.9);
    }

    const ANIM_DURATION = {
      sleep: 4.0, idle: 3.2, talk: 1.6, wave: 1.4, flicker: 1.0, cheer: 1.2,
    };

    // --- C2: TRADER, seated -------------------------------------------------
    // Everything from the neck up is untouched — `character()` still builds
    // the head, the face and the blink the same way for every body. Only
    // what's below the neck is re-drawn here, hips at the container's own
    // origin: `positionCharacters` puts that on the seat of the painted
    // chair, not on a floor 148px below it (Task 7's Override 2).
    function seatedRig(body, accents, facing) {
      // Which way the painted chair points, and so which way the figure does.
      //
      // Measured, never typed: `facing` is the screen vector the cushion's own
      // edges give (`scripts/measure_chair.py::measure_seat_axes`), handed down
      // from the manifest by `character()`. The literal is that same
      // measurement, so a page with no plate draws the figure the tests
      // describe — the `CAST_SCALE` pattern, and for the same reason.
      //
      // The finding behind the number matters, because the eye gets it wrong.
      // The backrest is painted up-and-RIGHT of the cushion, which reads as
      // "the back is at the back" and points the chair down-left, out into the
      // room. It is the near-side edge, seen over the top of a tall slab by a
      // camera above it. What settles it is the backrest's own crown line: it
      // runs parallel to the cushion's upper-left edge, so the slab's width
      // lies on that axis and it stands on the edge parallel to it — the
      // lower-right one, the one the slab itself hides. The chair therefore
      // faces UP-LEFT, over the cushion's front corner toward the desk, and
      // the camera gets his back. (Which is why the face below is allowed to
      // go: at this yaw there is no face to read.)
      const FACE_DIR = facing || [-100.5, -32.0];
      // The camera looks DOWN on this room, so a bone pointing at it is
      // foreshortened and a bone pointing up the wall is not.
      //
      // `SIN_PHI`/`COS_PHI` are one elevation, and the honest thing to say
      // about it is that the plate does not agree with itself: the castor
      // base — a circle in plan, so its projected aspect IS sin(phi) — reads
      // 33 degrees, while the tube bores (`bore_ry` over half `width`, the
      // same construction) read 14. Hand-painted art is allowed that.
      //
      // It does not matter here, and that is the point: the pose is graded
      // against the painted floor BAND (768, 845) that
      // `scripts/measure_chair.py::measure_floor_band` measures off the
      // chair's own castors, and the band is wide enough to swallow the
      // disagreement. The floor band is the constraint; the elevation is only
      // how the pose reaches it. The seat's two axes come out 84 degrees apart
      // under this value — a real seat is square in plan, so that is a grade
      // on the number rather than another use of it.
      const SIN_PHI = 0.358, COS_PHI = 0.934;   // phi = 21 degrees
      // A ground direction is [lateral, towardCamera]. The rig is authored
      // facing the camera, so yaw 0 is [0, +1] and every direction below is
      // turned through `turn()` rather than re-typed at a new angle: the bones
      // keep their TRUE traced lengths, which is what increment 3 bought, and
      // the turn stays one number that can be re-measured off a repaint.
      const YAW = Math.atan2(FACE_DIR[0], FACE_DIR[1] / SIN_PHI);
      const COS_Y = Math.cos(YAW), SIN_Y = Math.sin(YAW);
      const turn = (lat, tow) => [
        lat * COS_Y + tow * SIN_Y,
        tow * COS_Y - lat * SIN_Y,
      ];
      // Where an offset from the hips lands, and how deep it ended up. `depth`
      // is what decides which arm composites in front of the torso and which
      // leg is drawn first — derived from the turn, never hand-assigned, so a
      // re-measured facing cannot leave the near arm behind the body.
      const plant = (lat, tow) => {
        const [l, t] = turn(lat, tow);
        return { x: l, y: t * SIN_PHI, depth: t };
      };
      // Thighs: short and low, at the front edge of the seat rather than
      // dropping to a floor this plate doesn't give the chair.
      // Jeans. The thigh rects keep the exact geometry the seat-fit tests were
      // written against (KI-051 Task 5 / review round 2) — only the colour
      // moves, and it moves to an authored one. Shins and shoes are new and
      // sit BELOW them: the reference's trader has its legs out in front of
      // the chair, and a seated figure that stops at the hips is most of why
      // the old rig read as a stack of blobs rather than a person.
      const inner = { keepFill: true, noRim: true };
      // CLOTHES ARE MATTE (increment 15). Increment 9 scoped `matte` to the
      // shoe and let the sweater and jeans keep the lamp's band on the
      // argument that "a warm sheen at roughly their own brightness reads".
      // At 1:1 on the frame it did not: the lit rung mixes 0.45 toward
      // `#f0a848`, which turns grey-blue cloth tan, and the band lands as a
      // skin-coloured ellipse on the sleeve, the thigh and the shin — knee
      // pads and holes, not light. The reference lights every garment within
      // its own hue and keeps the warm band for skin and hair, and so does
      // this now. Still scoped to the PERSON: `shade` itself is untouched.
      const cloth = { keepFill: true, matte: true };
      const clothInner = { keepFill: true, noRim: true, matte: true };
      // The shoe is the one volume of the trader that takes no lamp band.
      //
      // `shade` mixes toward `LIGHT.warmth` by an ABSOLUTE 0.3 on the lit
      // rung, which lands EVERY cool volume's lit band within 5° of the
      // lamp's own hue (34°) — the sweater drifts 180°, the jeans 169°, the
      // shoes 164°. (Increment 9 judged the sweater and jeans' warm sheen to
      // read; increment 15 refused it off the frame — see `cloth` above.)
      // For the shoes, authored
      // near-black at hue 227°, it is a lift to hue 31° — four degrees off
      // skin's OWN authored 27° — so the darkest thing on the figure comes
      // out a dark flesh tone and the trader appears to be in slippers. The
      // near foot measured 327px of that wash against 150px of authored
      // `#2a2e3c`. Vault `Docs/character-redesign.md`, increment 9;
      // `scripts/measure_lit_band.py` recomputes the table.
      //
      // The fault is the value lift landing on a hue the volume never
      // authored, NOT the lift itself — the hair lifts proportionally more
      // (0.29 → 0.49) and reads fine, because it drifts 8° rather than 164°.
      // So a darker `HUMAN.shoes` is not the fix: it lowers the band and
      // leaves it at hue 31°, which the A/B frame refused exactly as the
      // table predicted.
      //
      // Scoped to the shoe on purpose. `shade` is shared with every market
      // glyph, the model robot and `world/visuals.py`; re-lighting the room
      // to fix a foot is the mistake increment 8 avoided by answering the
      // aesthetic question at the smallest scope that answers it. The
      // reference agrees: it gives the shoes no warm band at all and lets
      // them read on silhouette against the floor.
      const shod = { keepFill: true, matte: true };
      // The legs are built at the BOTTOM of this function, after the arms, so
      // that `accents[0]`/`accents[1]` stay the two arms `character()` aliases
      // by index. They are no longer drawn onto `body` at all: a posed limb is
      // a rotated capsule, and `litRect` snaps to the 4-unit CELL, so a bone
      // has to be its own Graphics with its own rotation.
      // Waist and chest, stacked straight up off the hips rather than off a
      // standing pair of legs. Review round 1, Finding 1: the seat measures
      // 79 screen px wide (`PLATE.cast.trader.seat.width`), and 58 units of
      // chest was 87px at CAST_SCALE 1.5 (still 78.3px at the pre-ticket
      // 1.35 - the rig's own width was the overflow, not the scale). Chest
      // is 48 (not the naive 50 - snap(-24)=-24 exactly, snap(-25) rounds to
      // -24 too but off-centre, pushing one edge to 26; picking start/width
      // that land clean on the 4px grid keeps the shape actually symmetric)
      // - 48*1.5=72px, inside the seat with margin.
      // `test_the_seated_rig_fits_inside_the_painted_seat_not_just_the_
      // backrest` (page) and `test_the_seated_rig_fits_inside_the_manifest_
      // seat` (manifest) both compute this from the real roundRect calls
      // against the manifest's measurement, rather than trusting this
      // comment's arithmetic.
      // Sweater. Same waist and chest rects as before — these are the two the
      // seat-fit tests measure the rig's width from, and the redesign has no
      // business widening them. A collar on top gives the neck somewhere to
      // come out of, which is the difference between a torso and a bolster.
      // Sweater, as ONE volume. It used to be a chest rect stacked on a waist
      // rect, and at four flat mood-coloured shapes that read as a torso; the
      // moment each carried its own lit band and its own rim it read as two
      // balls with a seam between them. The silhouette a person has here is one
      // shape from shoulder to hip, so it is drawn as one. Left and right
      // extents are unchanged at ±24 — which is what the two seat-fit tests
      // measure, and why merging them is safe.
      // r12, not r18: a 48-wide rect rounded at 18 is only 12 units wide where
      // it meets the thighs, so the legs hung off a tapering point instead of
      // out of a body. Shoulders stay at ±24 — the extent the seat-fit tests
      // measure.
      // A turned torso is NARROWER on screen, and that narrowing is most of
      // what sells the turn: a full-width slab at any yaw is a cardboard
      // cut-out on a turntable. The width is this body's own cross-section as
      // the camera sees it — half-axes 24 across the shoulders and 13 through
      // the chest — so it is hypot(24 cos, 13 sin), never 24 cos, which would
      // flatten him to a plank edge-on. Shoulders are still ±24 in the room;
      // only their projection moved, which is why the seat budget below is
      // measured off THIS rect rather than off the authored 48.
      // Where a limb's next joint lands, given a body-space direction and a
      // TRUE bone length. Declared up here rather than beside the legs that
      // used to be its only caller: the arms below articulate now, and a
      // `const` is in its temporal dead zone until its own line runs.
      const reach = (from, len, dir) => {
        const [lat, tow] = turn(dir[0], dir[1]);
        return {
          x: from.x + lat * len,
          y: from.y + (tow * SIN_PHI - dir[2] * COS_PHI) * len,
        };
      };
      // One bone as a lit capsule laid along a->b. Local to `seatedRig`
      // because the node drivers in the suite extract this one function by
      // name; a helper at module scope would be undefined inside them.
      // A LIMB, not a capsule (increment 20 — "the knee, the forearm and
      // cuff, the flat top of the thigh: make them not blocky"). A capsule is
      // one width from end to end, so the wide thigh stepped down onto the
      // narrower shin in a square notch, the thigh's top was a ruler line and
      // the forearm was a slab. The outline is the convex hull of a disc at
      // each joint — `end` sets the far one's width, and a parent's end
      // equals its child's start, which is what makes a joint a curve rather
      // than a step — plus one muscle point `bulge` units proud of that line,
      // `at` of the way along, on whichever side faces `toward` on screen:
      // the quad bulges UP, the calf BACK, and the undersides that rest on
      // the cushion and the lap stay flat. Convex by construction, so its
      // bands answer to `litPath`'s containment rule.
      // `test_a_limb_is_an_outline_that_tapers_into_the_next_one`.
      const limbOutline = (len, w0, shape, rotation) => {
        const w1 = shape.end, c = w0 / 2;
        const pts = [];
        // `cap` flattens the far end along the bone (1 = a round end): a
        // sleeve's cuff is a shallow curve over the wrist, not a half-disc.
        for (const [cy, r, k] of [[c, w0 / 2, 1], [c + len, w1 / 2, shape.cap || 1]]) {
          for (let i = 0; i < 24; i++) {
            const t = (i / 24) * 2 * Math.PI;
            const s = Math.sin(t);
            pts.push([c + r * Math.cos(t), cy + r * s * (s > 0 ? k : 1)]);
          }
        }
        if (shape.bulge) {
          const [amount, at, toward] = shape.bulge;
          // Local +x lands on screen at (cos r, sin r).
          const dot = Math.cos(rotation) * toward[0] + Math.sin(rotation) * toward[1];
          const side = dot >= 0 ? 1 : -1;
          // An ARCH, not a vertex: one point proud of the line hulls into
          // two straight facets meeting at a kink, which is a slab with a
          // corner. A half-sine peaking at `at` curves off each tangent.
          for (let i = 1; i < 12; i++) {
            const s = i / 12;
            const t = s < 0.5 ? 2 * s * at : at + (2 * s - 1) * (1 - at);
            const half = (w0 + (w1 - w0) * t) / 2;
            pts.push([c + side * (half + amount * Math.sin(Math.PI * s)), c + len * t]);
          }
        }
        // Andrew's monotone chain, strict: a collinear or repeated point is
        // dropped, so every turn of the result is a real one.
        // `quant`'s 1/64, restated: the suite runs this function without the
        // module scope `quant` lives in.
        const quantPt = (v) => Math.round(v * 64) / 64;
        const q = pts.map(([x, y]) => [quantPt(x), quantPt(y)])
          .sort((p, r) => p[0] - r[0] || p[1] - r[1]);
        const cross = (o, p, r) => (p[0] - o[0]) * (r[1] - o[1]) - (p[1] - o[1]) * (r[0] - o[0]);
        const half = (list) => {
          const out = [];
          for (const p of list) {
            while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], p) <= 1e-9) out.pop();
            out.push(p);
          }
          return out.slice(0, -1);
        };
        return half(q).concat(half(q.slice().reverse()));
      };
      const boneGfx = (a, b, width, fill, opts, shape) => {
        const dx = b.x - a.x, dy = b.y - a.y;
        const len = Math.hypot(dx, dy);
        const g = new PIXI.Graphics();
        // `restAccents` rewrites every accent's rotation from `restRotation`
        // on each tick, so a bone whose entire pose IS its rotation must carry
        // the angle THERE. Setting only `.rotation` renders correctly once and
        // then snaps bolt upright on the first frame.
        g.restRotation = Math.atan2(dy, dx) - Math.PI / 2;
        // Authored from its own corner and centred with `pivot`, never with a
        // negative x: `litRect` snaps x to CELL, so `litRect(-w/2, ...)` with
        // an odd half-width lands off-centre and thickens the limb on one
        // side. The rect also runs half a width PAST the joint at each end and
        // rounds to w/2, so consecutive bones interlock instead of leaving a
        // notch on the outside of every bend. A shaped limb keeps the same
        // frame — pivot on the START disc — so every joint reads as before.
        if (shape) {
          g.outline = limbOutline(len, width, shape, g.restRotation);
          // `later`: the caller paints it, after whatever must lie UNDER it.
          if (!shape.later) paint(g, litPath(g.outline), fill, undefined, opts);
        } else {
          paint(g, litRect(0, 0, width, len + width, width / 2), fill, undefined, opts);
        }
        g.pivot.x = width / 2;
        g.pivot.y = width / 2;
        g.x = a.x; g.y = a.y;
        g.rotation = g.restRotation;
        // The PROJECTED length, which is what the capsule actually measures on
        // screen and what the attachment maths below needs. It is not the
        // bone's true length: a limb pointing toward the camera foreshortens,
        // which is why one thigh draws at 27 units and its twin at 54. The
        // true length is tagged separately by the caller that knows it.
        g.boneLength = len;
        g.boneWidth = width;
        return g;
      };
      const bone = (a, b, width, fill, opts, shape) => {
        const g = boneGfx(a, b, width, fill, opts, shape);
        // Legs are not arms. `gesture()` swings every accent it is handed, and
        // swinging each bone of a leg about its own joint takes the leg apart
        // — the same "felled tree" failure the GESTURE table already names for
        // limb-less bodies.
        g.gestureMode = "still";
        // The seat measures what SITS on it. Legs overhang the cushion by
        // design, so the two seat-fit checks skip anything tagged a limb.
        g.limb = "leg";
        accents.push(g);
        return g;
      };
      // The spine, pelvis to shoulder line. 68 units from the hips at 0 to a
      // shoulder line at -72, against the trace's 1.58 heads = 70: it used to
      // be 76, and the 6 units it gives back are what let the head rise into
      // a neck below without the crown outgrowing the chair.
      const HALF_W = snap(Math.hypot(24 * COS_Y, 13 * SIN_Y));
      // The torso as one CONVEX outline (increment 19), not a rounded slab:
      // "natural, without blocks". Authored in `u` — +1 is the side his chest
      // faces, -1 his back — and in fractions of HALF_W, because the width is
      // the turn's. Min y stays -72 (the neck test reads the sweater's top)
      // and |u| never passes 1 (the seat budget). The top runs flat across
      // the collar before the shoulders fall away — a slope starting at the
      // neck would leave the collar's corners standing above it — and the
      // back rounds over at the top: he has been at this desk a while.
      // `test_the_torso_is_an_outline_not_a_slab`.
      const front = FACE_DIR[0] < 0 ? -1 : 1;
      const TORSO = [[-0.45, -72], [0.45, -72], [0.72, -69], [0.92, -62], [1, -50],
                     [0.97, -34], [0.86, -14], [0.76, -4], [-0.72, -4], [-0.86, -10],
                     [-0.97, -26], [-1, -52], [-0.92, -64], [-0.72, -70]];
      paint(body, litPath(TORSO.map(([u, y]) => [front * u * HALF_W, y])),
            HUMAN.sweater, undefined, cloth);
      // THE HEM: one CELL of ribbing along the torso's bottom, on `body` so it
      // breathes with the sweater. Between the forearm and the backrest it is
      // the one stretch of waist on screen (~40x12 px), and without it the
      // sweater ran into the jeans as one mass — the torso read as a column
      // stuck into the seat. Its corners sit on the torso's own edges.
      const HEM = [[0.8, -8], [0.76, -4], [-0.72, -4], [-0.81, -8]];
      paint(body, litPath(HEM.map(([u, y]) => [front * u * HALF_W, y])),
            HUMAN.hem, undefined, clothInner);
      // THE NECK. Drawn between the torso and the collar so the collar laps
      // over its base and the head covers its top, which is what makes a
      // cylinder read as a neck rather than as a peg.
      //
      // It is skin, and that is the whole point: a sweater-coloured collar
      // climbing to the jaw is what the figure had for five increments, and
      // it reads as a head balanced on a bolster. The trace asks for 0.17
      // heads of bare skin between the shoulder line and the chin — about 7
      // units — and `test_the_seated_figure_has_a_neck` measures exactly that
      // gap, from the topmost SWEATER volume rather than from the torso,
      // because the collar is what would cover it.
      //
      // Width 12 is the trace's own neck bone (16 reference px / 59.5 px per
      // head x 44), and it has to stay narrower than the collar or the collar
      // stops reading as clothing over it.
      //
      // A COLUMN, not a pill (2026-09-25, the user's catch). It was
      // `litRect(-6, -82, 12, 14, 6)`: radius 6 on width 12 is a dome, so
      // right under the jaw the room showed on both sides of a round knob,
      // the silhouette keyline ran along the head's whole underside, and the
      // head read as a lollipop. Now its top starts INSIDE the head, under
      // the nape (headY + 8), so there is no top edge to see; its back edge
      // continues the nape's back edge, so the back of the head runs down
      // into the back of the neck; its front edge sits just in front of the
      // nod pivot, so the throat stays joined at any gaze. It stops 2 units
      // into the collar, and its back edge holds at 8, the collar's own
      // half-width: any wider and a sliver of skin lands on the sweater
      // beside the collar.
      // `test_the_neck_joins_the_head_across_its_whole_underside`.
      const NECK = [[5, -92], [-8, -92], [-8, -70], [4, -70]];
      paint(body, litPath(NECK.map(([u, y]) => [front * u, y])), HUMAN.skin, undefined, inner);
      // A collar, inside the shoulders, sitting AT the shoulder line rather
      // than above it. `inner`: it is shading on the torso, not a separate
      // object. It narrows with the shoulders — 22 wide inside a turned
      // 40-wide body reads as a bib, not a collar.
      //
      // It used to top out at -88, thirteen units ABOVE the chin, which is
      // how a figure ends up with no neck while every height check passes.
      // Anything that raises this above the torso top re-buries the jaw, so
      // the neck test grades it from here and fails from both ends.
      const HALF_C = snap(HALF_W * 11 / 24);
      paint(body, litRect(-HALF_C, -72, HALF_C * 2, 10, 5), HUMAN.sweater, undefined, clothInner);
      // THE ARMS BEND AT AN ELBOW, and the comment that used to stand here
      // said they could not.
      //
      // It claimed the reference "paints an arm as ONE tapering sleeve from
      // shoulder to a chunky hand with no articulated elbow". That is wrong,
      // and the trace this redesign runs on had already recorded it wrong:
      // `skeleton_ref.json` carries `elbow_near` and `elbow_far` as joints,
      // and the near arm turns a near right angle at one —
      // shoulder (1068,462) -> elbow (1072,520) -> wrist (1014,524), i.e.
      // straight DOWN the side of the chair and then FORWARD along the
      // armrest to a hand with drawn fingers. Cropping the art at 4x shows
      // the sleeve make that turn plainly. What the reference has no *seam*
      // at is the elbow; it is one continuous sleeve, which is what
      // overlapping capsules give anyway.
      //
      // Lengths are the trace's, converted through the one unit this redesign
      // uses (59.5 reference px per head, 44 units per head): 58.1 px of
      // upper arm and 58.1 px of forearm both come out at 43 units.
      //
      // The arm's HEIGHT is not the trace's to settle: the reference figure is
      // slumped, and a recline throws the shoulder back and down relative to
      // the hip, so every hip-relative arm number off it carries the recline
      // inside it. This rig sits upright, which pins the elbow near -20 at
      // any upper-arm angle a person could hold — measured, not assumed.
      //
      // THE ARM RESTS ON THE LAP (2026-09-23, the user's call). The old pose
      // ran the forearm "forward along the armrest", nearly level, and hung it
      // six units of clear room over the thigh — the plate paints no armrest
      // on the near side at all (closed 2026-09-21). Now the upper arm swings
      // a little further BACK, tucking the elbow beside the hip, and since the
      // elbow cannot come lower the forearm slopes DOWN onto the thigh.
      // `test_the_near_forearm_rests_on_the_lap` brackets the wrist and the
      // fingertips between the thigh's top edge and its centreline, and the
      // window is narrow for a geometric reason: the forearm descends while
      // the thigh rises toward the knee, so a straight hand gains ~7 units of
      // sink over its 20. Only about -0.30 to -0.33 of (unit) forearm drop
      // passes, and at the shipped -0.312 both ends clear by under 1.3
      // units — so a re-measured chair failing that test is expected, not a
      // regression: re-solve the drop against it.
      // Both directions are unit vectors, because `reach` does not normalise
      // and a longer one would draw a bone longer than itself.
      const ARM = {
        upper: [43, [0.199, -0.398, -0.896]],  // down the side, back a little
        // Lateral is POSITIVE, i.e. the forearms splay very slightly OUT. At
        // -0.08 they both angled inward and, seen from behind, converged into
        // one horizontal bar across the chest — two arms reading as a single
        // object, which is the same failure the thighs have below.
        fore:  [43, [0.101, 0.945, -0.312]],   // forward and down onto the thigh
        width: [14, 12],
        // The forearm is not a slab: it leaves the elbow at the upper arm's
        // end width, carries its muscle on the upper side a third of the way
        // down, and narrows to a wrist the hand's heel (x 2..11 of
        // HAND_OUTLINE) sits across. The underside, lying on the lap, is flat.
        wrist: 10, foreBulge: [1.5, 0.3],
        // THE CUFF: the sleeve is painted AFTER the hand, so its end laps over
        // the heel of the palm in a shallow curve (`cap`, the end flattened to
        // half its round) — the hand comes OUT of the sleeve instead of being
        // pasted on the end of a slab, which is what the straight heel edge
        // over the sleeve read as. A separate cuff volume would be the joint
        // cap again (a banded ring = a bracelet); this is the sleeve's own end.
        // `test_the_sleeve_laps_over_the_wrist_as_a_cuff` bounds the overlap.
        cuff: 0.5,
        // The hand, traced off `ref-trader.png` and converted through the one
        // unit this redesign uses. The hand is STRAIGHT — it carries on the
        // forearm's line, fingers pointing where the arm points — so `along`
        // is heel to fingertip down the bone and `across` the palm's span;
        // `HAND_OUTLINE` (cast.js) is the shape, these the box it is placed by.
        //
        // Until 2026-09-23 the hand hung off the wrist at a right angle with
        // its fingers aimed at the floor, which over the knee read as a claw.
        // The numbers survived the turn with their axes swapped: 20 along,
        // split 8 palm / 12 finger (the prong rule, increment 12; the webs sit
        // at y 8), and 16 across. Since Sprint 17 #7 the fingers are the
        // outline's own notches, splayed, not separations cut into a slab.
        // Measurements beside HAND_ALONG_U in tests/api/test_world_page.py.
        hand: { along: 20, across: 16 },
      };
      for (const side of [-1, 1]) {
        // x ±16, and the two halves of that number are the KI-084 fix.
        //
        // It used to be ±25, pushed OUT there because `character()` added every
        // accent before the body and the torso spans ±24 — an arm inside that
        // was simply invisible, which is why the seated arms have never been on
        // screen. Now that `depth: "near"` composites them in FRONT of the
        // torso, an arm no longer has to clear it to be seen, so it comes back
        // inside where a person's arms actually are.
        //
        // And coming back inside is what retires the seat exemption below:
        // at ±16 the sleeve spans 12..24, inside the ±24 the painted cushion
        // budgets at `sit_anchor.x`. The old comment promised exactly this
        // ("when KI-084 lands the arm comes back inside ±24 and this exemption
        // should be revisited, not inherited") — so `arm.limb` is gone, and
        // the arms answer to the seat-fit tests again like the torso does.
        // The shoulder an arm hangs from is a point in the ROOM, so it turns
        // with the body: ±16 lateral becomes a screen offset AND a depth.
        const shoulder = plant(side * 16, 0);
        const root = { x: snap(shoulder.x), y: -66 + shoulder.y };
        // Both joints come out of `reach`, exactly as the legs' do, so the
        // arm's POSE is authored in body space and the turn projects it. The
        // lateral component flips with the side; the other two do not.
        const elbow = reach(root, ARM.upper[0],
                            [side * ARM.upper[1][0], ARM.upper[1][1], ARM.upper[1][2]]);
        const wrist = reach(elbow, ARM.fore[0],
                            [side * ARM.fore[1][0], ARM.fore[1][1], ARM.fore[1][2]]);
        const arm = boneGfx(root, elbow, ARM.width[0], HUMAN.sweater, cloth,
                            { end: ARM.width[1] });
        // `inner`, for the reason the shin is: the forearm is the MIDDLE
        // bone of its chain. An upper arm, a forearm and a hand is the same
        // three links a thigh, a shin and a shoe are, and only the ends make
        // the silhouette — a cream keyline on the one in between cuts the arm
        // into two outlined sausages meeting at a point, which is the
        // "die-cut sticker" read the redesign exists to remove. It shipped
        // rimmed because increment 6 built the arms after the legs and
        // restated the option rather than the rule.
        const fore = boneGfx(elbow, wrist, ARM.width[1], HUMAN.sweater, clothInner,
                             { end: ARM.wrist, bulge: [...ARM.foreBulge, [0, -1]], cap: ARM.cuff, later: true });
        // The lengths BEFORE the turn foreshortened them, so the suite can
        // hold them against the trace rather than against a projection that
        // legitimately changes whenever the chair is re-measured.
        arm.boneTrue = ARM.upper[0];
        fore.boneTrue = ARM.fore[0];
        // The hand, at the far end of the FOREARM's own local frame: the bone
        // runs from (w/2, w/2) along +y to (w/2, len + w/2), so the wrist is
        // the second of those and the hand sits just past it. Skin and
        // authored, so a hand stays a hand in every mood.
        // A flat hand, not a ball. The reference's hands are broad slabs with
        // drawn fingers resting on the armrest and the desk; a circle at this
        // size reads as a knob on the end of a stick, which is what the figure
        // had. A rounded rect carrying the forearm's own width keeps the
        // sleeve-to-skin transition reading as a wrist.
        //
        // Placed at the forearm's DRAWN length, never its authored one. The
        // sleeve is a projection of a 43-unit bone and comes out as little as
        // 28 on screen at this yaw, so a hand parked at 43 floats free of the
        // arm it belongs to with a gap of clear room between them — which is
        // exactly what the first version of this did.
        const H = ARM.hand;
        // Which local side faces the floor is a consequence of the pose, never
        // a literal: `litRect` draws in the forearm's own frame, and PIXI maps
        // that frame's +x to `(cos r, sin r)`. Screen y grows downward, so a
        // positive sine means +x is the floor-facing side. Both arms read it
        // from their own bone.
        //
        // `fore.restRotation` is still ABSOLUTE here — the reparenting below
        // makes it relative to the upper arm, and does so after this line.
        const floorOnPlusX = Math.sin(fore.restRotation) > 0;
        // The hand is flush with the BACK of the wrist, and its extra width
        // goes to the floor side rather than being centred on the bone: a palm
        // that swells above the arm it hangs from reads as a broken joint,
        // while the extra below is the edge of a hand spreading on the thigh.
        const handX = floorOnPlusX ? 0 : ARM.width[1] - H.across;
        const handY = fore.boneLength + ARM.width[1] / 2 - 2;
        // The OUTLINE, not a rect (increment 18 — "the palm looks like a
        // square"). The heel is the forearm's own width where it leaves the
        // sleeve, and the hand flares toward the FLOOR side only (the edge
        // spreading on the thigh; the back of the hand carries on the line
        // of the wrist). The fingers fan apart and their tips stagger, middle
        // fingers longest (Sprint 17 #7): a path's vertices do not snap, only
        // its origin does, so an outline can draw what the CELL grid refused a
        // rect. Concave, so `litPath` bands its convex hull and cuts each band
        // back to the outline, and the hand keeps its light patch by the cuff.
        const handOutline = HAND_OUTLINE.map(([x, y]) =>
          [floorOnPlusX ? x : H.across - x, y]);
        // THE CONTACT SHADOW (2026-09-24): the arm's own silhouette — sleeve
        // and hand — shifted toward the floor side and painted FIRST, so the
        // hand and sleeve cover all of it but the sliver on the thigh below
        // them. That sliver is the edge between arm and leg the 1:1 frame
        // lacked. Points are shifted rather than the origin, because
        // `litPath` snaps its origin to the CELL and 3 units would round away.
        const drop = (floorOnPlusX ? 1 : -1) * 3;
        const cast = (pts) => pts.map(([x, y]) => [x + drop, y]);
        paint(fore, litPath(cast(fore.outline)), HUMAN.contact, undefined, clothInner);
        paint(fore, litPath(cast(handOutline), handX, handY), HUMAN.contact, undefined, clothInner);
        // The hand is `shadowed`: out of the lamp in the lap, one rung down.
        paint(fore, litPath(handOutline, handX, handY),
              HUMAN.skin, undefined, { keepFill: true, shadowed: true });
        // The sleeve, now that the hand is under it (see `ARM.cuff`).
        paint(fore, litPath(fore.outline), HUMAN.sweater, undefined, clothInner);
        // THE FINGERS ARE THE OUTLINE'S OWN NOTCHES (Sprint 17 #7). Until then
        // they were three crease-coloured rects cut into a convex slab, the
        // tips ending on one flat arc; at 1:1 the keyline's light rim ran
        // unbroken round that arc and the hand read as a mitten with stitching.
        // `HAND_OUTLINE` now splays four fingers apart with real gaps at the
        // tips, so the keyline itself draws between them, and the contact
        // shadow above (the same outline, shifted) shows dark in the gaps
        // where the hand lies on the thigh.
        // The forearm becomes a CHILD of the upper arm, which is the only
        // arrangement where a shoulder swing carries the whole limb. `gesture`
        // and `cheer` write `armNear.rotation`/`armFar.rotation`; a forearm
        // left as its own accent would stay behind and tear the arm in half.
        //
        // Re-expressing the elbow in the parent's frame: PIXI maps a child by
        // `world = parentPos + R(parentRot) * (local - parentPivot)`, so the
        // inverse puts the elbow at `R(-parentRot) * (elbow - root) + pivot`.
        // The rotation is likewise relative, or the parent's angle counts twice.
        const ca = Math.cos(-arm.restRotation), sa = Math.sin(-arm.restRotation);
        const ex = elbow.x - root.x, ey = elbow.y - root.y;
        fore.x = ex * ca - ey * sa + ARM.width[0] / 2;
        fore.y = ex * sa + ey * ca + ARM.width[0] / 2;
        fore.restRotation -= arm.restRotation;
        fore.rotation = fore.restRotation;
        arm.addChild(fore);
        arm.forearm = fore;
        arm.shadeFactor = 1.35;
        // Which side of the torso this arm composites on is now a consequence
        // of the turn, not a decision: the shoulder that ended up nearer the
        // camera carries the arm that crosses in front (KI-084's `depthOrder`
        // still does the parenting). Both arms were hand-tagged "near" while
        // the figure faced the camera, which was right then and would put the
        // far forearm over his own back now.
        arm.depth = shoulder.depth > 0 ? "near" : "far";
        accents.push(arm);
      }
      // ---------------------------------------------------------------------
      // The legs, posed off the traced skeleton instead of stacked as upright
      // rects under the hips.
      //
      // The defect this fixes is a RATIO, which is why it needed no scale
      // factor to find: measured against its own head the shipped figure was
      // 3.59 heads crown-to-ankle where the reference is 5.47, and the whole
      // shortfall sat in the legs — a 16-unit thigh where the trace says 57,
      // an 18-unit shin where it says 62. The head was already right and the
      // torso within 3%. That is why the old rig read as a doll rather than as
      // a small person: dolls are short-legged.
      //
      // Lengths and widths are the trace's own, converted through the HEAD and
      // nothing else (1 head = 44 units = the approved `litCircle(0, -4, 22)`
      // in `character()`), so there is no `k` anywhere in this block.
      //
      // A bone is a direction in the ROOM (lateral, toward-camera, up) at its
      // TRUE traced length; `reach` is what turns that into screen pixels.
      // That keeps the ratio finding — legs 3.5x short, the whole point of
      // increment 3 — legible in the source, instead of burying it in twelve
      // hand-typed screen lengths that have been silently foreshortened. It is
      // also what lets the whole figure turn with the chair for one constant:
      // every direction here goes through `turn()` before it is projected.
      const SEATED_LEGS = {
        // hip LATERAL offset (a room offset, not a screen one — it turns), then
        // [trueLength, [lateral, towardCamera, up]] per bone, then the three
        // widths. Widths do NOT foreshorten — a limb's diameter is the same
        // however it is pointed — so they are the trace's own. Knees apart and
        // shins angled back INWARD under the body, which is how a person
        // actually sits.
        //
        // The thighs run very nearly LEVEL (`up: -0.12`), which is what
        // sitting anatomically is — and increment 4 had to slope them at -0.40
        // to get a lap at all. That slope was a workaround for a pose this
        // increment no longer has: pointed straight at the camera a level
        // thigh projects to a ~20-unit stub behind a 19-unit knee, so there
        // was nothing to see. Turned 132 degrees the same thigh lies ACROSS
        // the screen at nearly its full 57 units, so the lap reads from the
        // length rather than from a slope — and the drop it no longer spends
        // on the way to the knee is what leaves room to grow the figure
        // without putting its feet through the painted floor.
        // THE THIGHS ARE NEARLY PARALLEL, and the trace is what says so.
        //
        // They used to splay +/-0.40 laterally — 47 degrees apart. The
        // reference's two thighs are 8 degrees apart and project to 77.8 and
        // 77.7 px, within a pixel of each other, because a man slumped in a
        // task chair puts his legs out in front with his knees together, not
        // astride. Turned 132 degrees, a 47-degree splay aimed one thigh into
        // the screen: it drew 26.6 units long against its twin's 54.2, and at
        // only 1.3x its own width it read as a square lump at the hip. That
        // disparity is most of what "the legs read as one mass" was, and
        // `test_the_two_thighs_read_as_two_legs_not_one_mass` now measures
        // both halves of it.
        //
        // The knees still part slightly — a dead-parallel pair reads as one
        // leg with a seam — but by a tenth, not by four.
        left: {
          hipLat: 11,
          thigh: [57.5, [0.12, 0.985, -0.12]], shin: [62.5, [-0.18, 0.1, -0.98]],
          foot: [24.5, [0.25, 0.97, 0]], width: [19.2, 14.8, 15],
        },
        right: {
          hipLat: -11,
          thigh: [57.6, [-0.12, 0.985, -0.12]], shin: [62.4, [0.18, 0.1, -0.98]],
          foot: [29.8, [-0.25, 0.97, 0]], width: [20.7, 16.3, 16],
        },
      };
      // The limbs' flesh, in units (see `limbOutline`): how far a muscle
      // stands proud of the line between two joints, and where along the
      // bone it peaks. `hem` is the jeans' width at the ankle over the knee's.
      const LIMB = { quad: 3, quadAt: 0.65, calf: 2, calfAt: 0.3, hem: 0.8 };
      // Far side first so it renders behind the near one. The far leg is also
      // mixed toward the room's own ambient rather than given a second
      // darkness constant: the far side of a body catches less of the key
      // light, and without that a correct depth ORDER still reads as a flat
      // person.
      //
      // WHICH leg is the far one is a consequence of the turn now, not a key
      // in the table: the hip that ended up deeper is drawn first. Hand-naming
      // them `far`/`near` was true only while the figure faced the camera, and
      // would have silently swapped the lighting the moment he turned.
      const legOrder = ["left", "right"]
        .map((side) => ({ L: SEATED_LEGS[side], seated: plant(SEATED_LEGS[side].hipLat, 0) }))
        .sort((a, b) => a.seated.depth - b.seated.depth);
      legOrder.forEach(({ L, seated }, index) => {
        const isFar = index === 0;
        const hip = { x: seated.x, y: seated.y };
        const knee = reach(hip, L.thigh[0], L.thigh[1]);
        const ankle = reach(knee, L.shin[0], L.shin[1]);
        const toe = reach(ankle, L.foot[0], L.foot[1]);
        const jeans = isFar
          ? mixHex(HUMAN.jeans, LIGHT.ambient, 0.3) : HUMAN.jeans;
        // The far shoe takes only a tenth of the ambient the far jeans take:
        // mixed 0.3 it came back up to the floor's own value and vanished.
        const shoes = isFar
          ? mixHex(HUMAN.shoes, LIGHT.ambient, 0.1) : HUMAN.shoes;
        // The shin is `inner`: it runs between a thigh and a shoe, so a cream
        // keyline on it cuts the leg into three outlined links of a chain —
        // the "die-cut sticker" read this redesign exists to remove. Only the
        // parts that make the silhouette keep their edge.
        // The thigh narrows to exactly the shin's width at the knee, so the
        // two discs there coincide and the knee is one round; its quad bows
        // UP off the chord, and the side sitting on the cushion stays flat.
        // The shin tapers toward the hem and carries its calf at the BACK —
        // the way the thigh came from, since the toes point the way it went.
        bone(hip, knee, L.width[0], jeans, cloth,
             { end: L.width[1], bulge: [LIMB.quad, LIMB.quadAt, [0, -1]] });
        const shin = bone(knee, ankle, L.width[1], jeans, clothInner,
             { end: L.width[1] * LIMB.hem,
               bulge: [LIMB.calf, LIMB.calfAt, [hip.x - knee.x, hip.y - knee.y]] });
        const shoe = bone(ankle, toe, L.width[2], shoes, shod);
        // The far knee lands ON the cushion's front edge, and from behind the
        // chair that edge is the far one: the painted seat is in front of the
        // shin below it, so `occludeBehindSeat` hides it there (increment
        // 22). The thigh lies ON the cushion and stays in front. The near knee
        // is at the cushion's corner, beside it, not behind — masked too, its
        // shin lost a 32 px notch to the corner's tip, shot and refused.
        if (isFar) shin.behindSeat = shoe.behindSeat = true;
      });
      // THE SEAT: jeans from the waist to the cushion, under the sweater's
      // hem. Without it the hips were whatever the thighs' round hip ends
      // made of them — the sweater's r12 corners cut away the bottom of the
      // back, and the far thigh's cap filled the notch as a ball bulging out
      // under the chair back (the user's "fix his butt"). A seated pelvis is
      // FLAT underneath, because it is sitting on something; bottom
      // `SEAT_DEPTH` below the hips, enough to swallow the rear hip cap (12
      // swallows both and was shot as a box sticking out behind the chair).
      //
      // And it is a BUTTOCK, not a box (increment 21). As a r4 rect it came
      // out from under the backrest as a plank with a square rear corner and
      // the torso a stick planted on it — "I don't like how the torso
      // connects to the ass". Only rig y -7…+8 is on screen below the
      // backrest, so the shaping lives there: the back swells out past the
      // sweater's tucked-in waist (-0.72 at the hem), is fullest well above
      // the cushion, and rounds under onto a flat underside that stops short
      // of the rear. The front leans in off the waist so its edge stays
      // inside the lap instead of drawing a seam down the near thigh. Same
      // `u` convention and HALF_W fractions as `TORSO`; |u| ≤ 1 keeps it in
      // the rect's old footprint, so every seat-fit check holds as it did.
      // `test_the_seated_figure_sits_on_a_seat_not_on_the_ball_of_a_thigh`.
      //
      // Its own still accent, NOT a volume on `body`: `breathe` moves
      // `body.y` a whole CELL, and a seat that breathes lifts off the thighs
      // and hands the ball back for half of every breath. Pushed after the
      // legs, so it covers their hip ends; no `depth`, so `depthOrder` puts
      // it behind the torso and the sweater's hem laps over it.
      const SEAT_DEPTH = 8;
      const SEAT = [[0.82, -16], [-0.92, -16], [-1, -6], [-0.99, -2], [-0.92, 2.5],
                    [-0.775, 5.6], [-0.58, 7.4], [-0.35, SEAT_DEPTH], [0.6, SEAT_DEPTH],
                    [0.78, -4]];
      const seat = new PIXI.Graphics();
      paint(seat, litPath(SEAT.map(([u, y]) => [front * u * HALF_W, y])),
            HUMAN.jeans, undefined, cloth);
      seat.gestureMode = "still";
      // Tagged so nothing counts it as a third arm: an untagged accent IS an
      // arm to every reader of `accents`.
      seat.limb = "seat";
      seat.restRotation = 0;
      accents.push(seat);
      // -100, and the 4 units it moved up are a NECK.
      //
      // The chin sat 21 below whatever this returns when `character()`
      // painted the head as two circles; the profile head (increment 16)
      // puts its chin 20 below, and `profileHead` reports it. At -96 the chin landed at
      // -75, one unit BELOW the shoulder line, and the collar then climbed to
      // -88 on top of that: the head was not sitting on the shoulders, it was
      // sunk into them. At -100 the chin is at -79 and the shoulder line is
      // at -72, which is the trace's 0.17 heads of bare skin.
      //
      // Three numbers move together here and only together — this return, the
      // torso's top, and the collar's top. `test_the_seated_figure_has_a_neck`
      // is the one that grades the span they produce.
      //
      // It used to be kept low enough to stay INSIDE the painted backrest, and
      // that constraint is gone: at the scale the chair actually asks for, a
      // seated person's crown clears the back of an office chair, and the
      // figure that satisfied the old rule was the doll-sized one. What the
      // head answers to now is the chair's own proportion — crown-above-hips
      // against backrest-above-seat — which is a bracket a too-small figure
      // fails from below and a giant fails from above.
      return -100;
    }

    // Every write below moves the torso (`body`), the head, or an arm —
    // never `container`, the hips, fixed to the chair by
    // `positionCharacters`. A standing animation moves the whole body, which
    // is how a seated one sinks through the cushion or slides out of it.
    const SEATED_ANIMATIONS = {
      // The rotation leads on purpose: `HEAD_TRAVEL.figure` is exactly one
      // CELL, so the snapped head-drop alone can round back to nothing for
      // most of the phase at tier 0. `body.rotation` isn't quantised.
      slump:   (c, p, a) => { const u = EASE.settle(p);
                              c.body.rotation = u * 0.16 * a;
                              c.head.rotation = u * 0.10 * a;
                              c.head.y = snap(c.headBaseY + u * HEAD_TRAVEL[c.style] * a); },
      lean:    (c, p, a) => { const u = Math.sin(p * Math.PI) * a;
                              c.body.x = snap(u * 4);
                              c.head.x = snap(u * 6); },
      // No shoulder swing — the desk is in the way. One forearm lifts at the
      // ELBOW instead: the gesture a seated person has room to make. It used
      // to write `armFar.rotation`, which swung the whole near arm about the
      // shoulder and dragged the hand BACK along the lap (Sprint 17 #3).
      // Positive: the forearm points forward-and-down, so turning it that
      // way raises the hand off the thigh. A rig with no forearm child (the
      // model) keeps the old whole-arm lift.
      // `test_the_seated_shrug_lifts_the_forearm_at_the_elbow`.
      gesture: (c, p, a) => { const u = Math.sin(p * Math.PI) * a;
                              const fore = c.armFar.forearm;
                              if (fore) fore.rotation = (fore.restRotation ?? 0) + u * 0.6;
                              else c.armFar.rotation = (c.armFar.restRotation ?? 0) - u * 0.6; },
      // The seated ambient loop. Full CELL, not half: `body.y` rests at 0,
      // already on the grid, so a smaller delta snaps straight back to it.
      //
      // The head rides the same delta (2026-09-25, the user's catch). The
      // neck is painted on `body` and the head is a sibling, so a body that
      // breathed alone carried the collar a CELL down away from a head that
      // stayed put: the neck grew and shrank by a CELL every breath.
      // `test_the_neck_holds_its_length_through_a_breath`.
      //
      // One art pixel, and only UP (the user's second catch, same day): a
      // whole CELL is 4 units, 8 screen px, and a sine carried the bust a
      // step up, back, and a step DOWN into the cushion — a block bobbing on
      // a spring. A chest lifts on the inhale and settles back to rest; it
      // never sinks below it. The seated rig is always under the keyline
      // filter, whose grid is one unit (`KEYLINE.block` px), so a single
      // unit is on the grid this rig is actually drawn to.
      // `test_a_seated_breath_is_one_pixel_and_only_up`.
      breathe: (c, p) => { const dy = -Math.round((1 - Math.cos(p * Math.PI * 2)) / 2);
                           c.body.y = dy;
                           // Not `snap`: that rounds to the CELL, and ate the unit.
                           if (c.headBaseY != null) c.head.y = c.headBaseY + dy; },
      // Head drops, eyes close via the same closed-lid draw the idle blink
      // uses (`setBlink`) — not `visible = false`, which reads as empty
      // sockets rather than a person asleep.
      asleep:  (c, p, a) => { const u = EASE.settle(p);
                              c.head.rotation = 0.22 * u;
                              c.head.y = snap(c.headBaseY + CELL * 3 * u);
                              setBlink(c, u > 0.5); },
      // `trader_milestone`'s own beat, not borrowed: both forearms pump up
      // together, twice within the phase — the double-lift standing `cheer`
      // uses — kept off `container` so the trader stays seated at the desk
      // it just closed a profitable position at. `trader_opened` fires on
      // every cold sidecar start, so unlike the other trader_* reactions
      // this one is guaranteed to run the moment the opt-in profile is ever
      // enabled, and "proud" deserves to read as distinct from "resigned".
      cheer:   (c, p, a) => { const u = Math.abs(Math.sin(p * Math.PI * 2)) * a;
                              c.armNear.rotation = (c.armNear.restRotation ?? 0) - u * 0.5;
                              c.armFar.rotation = (c.armFar.restRotation ?? 0) + u * 0.5;
                              c.body.y = snap(-u * CELL);
                              // The head rides the lift, as in `breathe`.
                              if (c.headBaseY != null) c.head.y = snap(c.headBaseY + c.body.y); },
    };
    // The registry (world/reactions.py) dispatches `sleep`/`shrug`, never
    // the literal `asleep`/`gesture` — aliased onto the names that actually
    // arrive so the re-author is live on real events, not just declared.
    // `shrug` especially: standing writes `container.y`, the hip-lift this
    // table exists to stop.
    SEATED_ANIMATIONS.sleep = SEATED_ANIMATIONS.asleep;
    SEATED_ANIMATIONS.shrug = SEATED_ANIMATIONS.gesture;
    // `trader_*` (world/reactions.py: trader_opened -> step, trader_closed ->
    // turn, trader_milestone -> cheer) is the vocabulary EVERY event routed
    // to the trader can carry — `react()` sends every trader_* type there,
    // never through `model`. None of `step`/`turn` had a seated match: a
    // standing `step` slides the body 26px off the cushion, and `turn`
    // squashes `scale.x` to 0.4 inside a full-width chair — both landed on
    // `container`, the exact write this table exists to stop. `step`
    // ("decisive") reuses the seated lean; `turn` ("weighing") reuses the
    // seated gesture — a considering forearm motion reads as weighing a
    // decision at least as well as it reads as a shrug. `cheer` is defined
    // directly above rather than aliased: milestones are the one distinctly
    // celebratory trader reaction and deserve their own beat.
    SEATED_ANIMATIONS.step = SEATED_ANIMATIONS.lean;
    SEATED_ANIMATIONS.turn = SEATED_ANIMATIONS.gesture;

    function restCharacter(char) {
      if (char.container.pose === "seated") {
        restSeatedCharacter(char);
        return;
      }
      char.container.y = snap(char.baseY);
      char.container.x = snap(char.baseX);
      char.container.rotation = 0;
      char.container.alpha = 1;
      char.container.scale.set(1, 1);
      setGaze(char, 0, 0, 0);
      char.mouth.scale.y = 1;
      restAccents(char);
      if (char.headBaseY != null) char.head.y = snap(char.headBaseY);
    }

    // The seated counterpart: puts back everything SEATED_ANIMATIONS can
    // touch (body, head, the arms via restAccents, blink), and leaves
    // `container` alone — it's the chair, not the body, and the standing
    // reset above would yank the trader back to a floor this pose doesn't
    // stand on.
    function restSeatedCharacter(char) {
      char.body.rotation = 0;
      char.body.x = 0;
      char.body.y = 0;
      char.head.rotation = 0;
      setGaze(char, 0, 0, 0);
      char.mouth.scale.y = 1;
      restAccents(char);
      if (char.headBaseY != null) char.head.y = snap(char.headBaseY);
      setBlink(char, false);
      char.container.alpha = 1;
    }

    // --- B7: idle life, and attention -------------------------------------
    // Seeded, not ad-hoc: the eval gate for calm is one quiet frame compared
    // against another, and unseeded drift makes two calm frames incomparable.
    // The director already treats RNG as something you inject; so does the room.
    function rng32(seed) {
      let s = seed >>> 0;
      return () => {
        s = (s + 0x6d2b79f5) >>> 0;
        let t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
      };
    }

    // Each behaviour owns exactly one property and eases 0->1->0, so two can
    // overlap the way they do in life (you shift your weight while looking
    // somewhere) without fighting over the same transform.
    const IDLE_ACTIONS = {
      blink: {
        every: [2.4, 6.5], dur: 0.16,
        run: (c, u) => setBlink(c, u > 0.45),
      },
      glance: {
        every: [5.0, 13.0], dur: 1.8,
        run: (c, u, dir) => setGaze(c, dir * 2.5 * u, dir * 0.05 * u, 0),
      },
      shift: {
        every: [7.0, 17.0], dur: 2.6,
        run: (c, u, dir) => {
          // Task 7's Override 3: for a seated character `container` IS the
          // chair (`positionCharacters` anchors it to the plate's seat), so
          // the weight-shift this idle action stages moves the torso
          // instead — same amplitude, same "under one CELL" reasoning C1
          // used to leave it unsnapped, applied to the write this ticket's
          // whole table exists to keep off the hips. The head goes with it
          // (the neck is on `body`).
          if (c.container.pose === "seated") { c.body.x = c.head.x = dir * 3 * u; return; }
          c.container.x = c.baseX + dir * 3 * u;
        },
      },
    };

    // Looking is done with the eyes, not by sliding the skull sideways: a head
    // that translates reads as a part coming loose, on the machine especially,
    // where it sits on a visible stem. The head only rotates.
    //
    // Review round 1, Finding 2: the idle glance (IDLE_ACTIONS.glance) and the
    // B7 deliberate look (watchTick) both drive `dx`/`headShift` through a
    // continuous sin() ease, and watchTick's amplitude (4) is exactly one
    // CELL — unsnapped, that is the sub-pixel crawl this ticket exists to
    // remove. Not literally named in the brief's Step 4 list, but the same
    // reasoning that put `restCharacter` and the `ANIM` table under `snap()`
    // reaches every displacement write, and this is one: wrapped once here so
    // every caller (glance, watchTick, resetIdle) is covered without each of
    // them having to remember to.
    function setGaze(char, dx, rot, headShift) {
      char.eyeL.x = char.eyeR.x = snap(dx);
      // A profile has no eyeL/eyeR to slide (they are hidden), and `head`'s
      // origin is mid-skull, so `head.rotation` swung the chin across the
      // collar and `head.x` slid the head off the neck. It looks by NODDING
      // about the top of the neck instead — `aim`, which carries that pivot —
      // on top of its resting look at the charts, and the head stays on the
      // torso wherever the torso has shifted to.
      if (char.profile) {
        char.head.rotation = 0;
        char.head.x = char.body.x;
        char.aim.rotation = (char.aimRest ?? 0) + rot;
        return;
      }
      char.head.rotation = rot;
      char.head.x = snap(headShift);
    }

    function setBlink(char, closed) {
      if (char.blinkClosed === closed || !char.face) return;   // edges only
      char.blinkClosed = closed;
      drawEyes(char, closed ? 0 : char.face.arousal);
    }

    // Hoisted: this runs per character per frame, and OBS renders in software.
    const IDLE_ENTRIES = Object.entries(IDLE_ACTIONS);

    function idleTick(char, dt) {
      char.idle ??= {};
      for (const [name, spec] of IDLE_ENTRIES) {
        const st = (char.idle[name] ??= { wait: spec.every[0] * char.rand(), t: 0 });
        if (!st.active) {
          st.wait -= dt;
          if (st.wait > 0) continue;
          st.active = true;
          st.t = 0;
          st.dir = char.rand() < 0.5 ? -1 : 1;
        }
        st.t += dt;
        const done = st.t >= spec.dur;
        spec.run(char, done ? 0 : Math.sin((st.t / spec.dur) * Math.PI), st.dir);
        if (done) {
          st.active = false;
          const [lo, hi] = spec.every;
          st.wait = lo + char.rand() * (hi - lo);
        }
      }
    }

    // "Disagreement is the content" — but the trader is a dry-run sidecar that
    // stays dormant until its own events exist, so handing it a reaction to an
    // event it had no part in would be inventing activity, which is the one
    // thing this room must never do. It looks. It does not act: no mood, no
    // animation, no numbers. The delay is what makes it read as *noticing*.
    function lookAt(observer, subject, delay) {
      if (!observer || !subject || observer === subject || observer.anim) return;
      observer.watch = {
        t: -delay, dur: 2.8,
        dir: Math.sign(subject.container.x - observer.container.x) || 1,
      };
    }

    function watchTick(char, dt) {
      const w = char.watch;
      if (!w) return;
      w.t += dt;
      if (w.t < 0) return;                       // still the stagger
      const u = Math.sin(Math.min(w.t / w.dur, 1) * Math.PI);
      // A deliberate look is bigger than an idle glance and does carry the
      // head a little — that difference is what makes it read as directed.
      setGaze(char, w.dir * 4 * u, w.dir * 0.12 * u, w.dir * 3 * u);
      if (w.t >= w.dur) { char.watch = null; setGaze(char, 0, 0, 0); }
    }

    function resetIdle(char) {
      setGaze(char, 0, 0, 0);
      char.watch = null;
      setBlink(char, false);
      for (const st of Object.values(char.idle ?? {})) st.active = false;
    }

export {
  ANIM, playAnimation, ANIM_DURATION, seatedRig, SEATED_ANIMATIONS,
  restCharacter, rng32, idleTick, lookAt, watchTick,
  restSeatedCharacter, setBlink, setGaze,
};
