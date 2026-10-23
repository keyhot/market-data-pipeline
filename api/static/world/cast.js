// /world — The cast's design: reaction/style tables, faces, bodies, heads, gaze,
// soft shadows, depth order.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import "./room.js";
import { WORLD_CONFIG, PAINTED_CAST, LIGHT } from "./config.js";
import { BODY_FILL, HUMAN, MACHINE } from "./palette.js";
import {
  mixHex, shade, rampLevel, KEY_AXIS, CONTACT, SHADOW_THROW, paint, litRect,
  litCircle, litPath,
} from "./shading.js";
import { snap } from "./tiers.js";

    // --- B1: procedural characters ----------------------------------------
    // The inhabitants were colour-swatch circles: the room had a mood but no
    // face, and reactions.py's named animations (jolt/shake/hop/…) existed only
    // as strings. Both are real now. Procedural geometry only — no external
    // assets, so the CSP/SRI rule holds and there is no asset pipeline.
    const REACTIONS = WORLD_CONFIG.reactions;
    // The bodies are built from the market's own glyphs rather than from
    // anatomy. This world is made of price data; its inhabitants should be
    // too — and a candle or a bar cluster still reads at 120px on a stream,
    // where limbs turn to mush. The face is identical across all of them.
    const STYLES = ["bars", "figure", "candle", "monolith", "orb"];
    // The pick (2026-08-12). Not one style for everyone: the MODEL is a
    // machine, the TRADER is a person. Two events land on two silhouettes you
    // can tell apart at a glance and across the room — which is what makes
    // "they disagreed" legible without labels.
    //
    // The MODEL used to be "a machine built out of the market's own glyphs",
    // and increment 11 (2026-09-21) deliberately gave that up: the user's
    // 2026-09-11 call is a plated robot with no candle glyphs, trading the
    // made-of-the-market link for an unambiguous robot read. `bars` is
    // therefore a LEGACY KEY — it names a body that has no bars in it — and
    // it is kept because `STYLES`, `FACE_KIND`, `HEAD_TRAVEL`, `SHADOW_WIDTH`
    // and `STYLE_OVERRIDE` all spell it, so renaming costs five registry
    // edits and buys no picture.
    const CAST = { MODEL: "bars", TRADER: "figure" };
    // `?style=` overrides the whole cast — it exists for evaluation, so it has
    // to be able to put everyone in the same body for comparison.
    const STYLE_OVERRIDE = new URLSearchParams(location.search).get("style");

    // Where the floor is, and how big the cast stands on it. Both were spelled
    // 0.72 in three places, which is how the floor and its inhabitants came
    // apart in the first place.
    const GROUND = 0.66;
    // The pair read as two small icons on a wide dark plane — about a quarter
    // of the frame, which on a phone-sized YouTube window is nothing. The
    // LAYER carries the scale, not the characters: half the named animations
    // set `container.scale` themselves and would clobber a per-character one.
    //
    // 1.5, not 1.35 (review round 1, Finding 1): `positionCharacters` divides
    // every anchor by CAST_SCALE before assigning it, and this layer then
    // re-multiplies by the same factor — rendered position on the painted
    // plate is provably invariant to CAST_SCALE, so changing it moves nothing
    // and only resizes the cast. What it DOES change is the on-screen size of
    // the cast's own 4px local grid: at 1.35 that renders as 5.4 screen px,
    // which `roundPixels` then rounds to 5 or 6 depending on phase — the step
    // JITTERS, worse than a fixed off-grid value. At 1.5 it renders as an
    // exact 6 screen px: integer, uniform, no jitter, and every `snap()` call
    // site in the cast stays correct with the one CELL/snap definition — no
    // second snap function for a second coordinate space, which is a second
    // definition of the same rule that later tickets would have to choose
    // between.
    //
    // Known residual, left deliberate rather than hidden: the cast now renders
    // on a uniform 6px cell while the props (pillars) render on a true 4px
    // cell — a consistent grid PER SURFACE, not one 4px grid across the whole
    // room. Strict single-grid conformance needs CAST_SCALE = 1, a 26% shrink
    // of the only two figures on screen, which trades this sprint's headline
    // goal (watchability) for a parenthetical. One constant to reverse after
    // a visual check, not a redesign.
    //
    // Sprint 16: the visual check happened, at the 1920x960 the browser source
    // actually is. 1.5 puts a 42px-radius head on the model — over half the
    // height of the painted bonsai beside it — and lifts the trader's crown
    // 67px clear of its own chair's backrest, which is what "standing in front
    // of the chair" turned out to mean. So the size of the cast is a
    // MEASUREMENT against the painted furniture now, not a constant: it lives
    // in the manifest next to the seat and the tubes, and a repaint re-measures
    // it instead of re-coding it. The literal is the no-plate fallback, and it
    // has to keep CELL * CAST_SCALE a whole number of screen pixels (see
    // test_animation_displacement_actually_quantises_to_the_grid, which grades
    // exactly this literal): 1.25 renders the 4px cell as an exact 5px.
    const CAST_SCALE = (PAINTED_CAST && PAINTED_CAST.scale) || 1.25;

    function styleFor(name) {
      const style = STYLE_OVERRIDE || CAST[name];
      return BODIES[style] ? style : "bars";
    }

    // Expression as two axes rather than 19 hand-drawn faces: valence bends
    // the mouth, arousal opens the eyes and lifts the brows. Every mood the
    // reactions registry can emit has an entry (page test enforces it); an
    // unknown one lands on neutral rather than a blank stare.
    const FACE = {
      elated:    { valence:  1.0, arousal: 0.9 },
      relieved:  { valence:  0.7, arousal: 0.2 },
      proud:     { valence:  0.8, arousal: 0.5 },
      eager:     { valence:  0.7, arousal: 0.8 },
      decisive:  { valence:  0.4, arousal: 0.7 },
      focused:   { valence:  0.0, arousal: 0.6 },
      attentive: { valence:  0.1, arousal: 0.5 },
      speaking:  { valence:  0.2, arousal: 0.5 },
      weighing:  { valence: -0.1, arousal: 0.3 },
      idle:      { valence:  0.0, arousal: 0.0 },
      neutral:   { valence:  0.0, arousal: 0.3 },
      surprised: { valence:  0.2, arousal: 1.0 },
      startled:  { valence: -0.2, arousal: 1.0 },
      alert:     { valence:  0.0, arousal: 0.9 },
      anxious:   { valence: -0.5, arousal: 0.8 },
      alarmed:   { valence: -0.7, arousal: 1.0 },
      resigned:  { valence: -0.4, arousal: 0.1 },
      dejected:  { valence: -0.9, arousal: 0.2 },
      grim:      { valence: -0.8, arousal: 0.4 },
      calm:      { valence:  0.1, arousal: 0.2 },
      bullish:   { valence:  0.8, arousal: 0.6 },
      bearish:   { valence: -0.8, arousal: 0.6 },
      panicked:  { valence: -1.0, arousal: 1.0 },
    };

    function faceFor(mood) {
      return FACE[mood];
    }

    // Each body is a market glyph. `accents` are the parts that can move
    // independently of the mass — wicks, bars — which is what gives these
    // silhouettes something to act with in place of limbs.
    const BODIES = {
      // A candlestick: real body between two wicks. The project's own glyph,
      // and the one that reads most instantly as "this thing is the market".
      candle(body, accents) {
        paint(body, litRect(-27, -104, 54, 84, 8), BODY_FILL);
        // Wick thick enough to read as a neck at stream distance, and short
        // enough that the head sits ON the candle rather than floating over it.
        const wickTop = new PIXI.Graphics();
        paint(wickTop, litRect(-3.5, -130, 7, 30, 3.5), BODY_FILL);
        const wickLow = new PIXI.Graphics();
        paint(wickLow, litRect(-3.5, -22, 7, 26, 3.5), BODY_FILL);
        // A wick flicks by lengthening. It cannot swing: it's drawn in the
        // character's own space, so rotating it pivots about the feet.
        wickTop.gestureMode = wickLow.gestureMode = "pump";
        accents.push(wickTop, wickLow);
        return -142;
      },
      // A cluster of bars — volume, an equaliser, a heartbeat. The most
      // animatable body: the bars themselves can jump, ripple and settle.
      // The MODEL: a plated machine. The key is a legacy name — this body was
      // five candlestick glyphs until 2026-09-21, and `CAST.MODEL`,
      // `FACE_KIND`, `HEAD_TRAVEL`, `SHADOW_WIDTH` and `STYLE_OVERRIDE` all
      // spell it, so renaming it would be five registry edits for no picture.
      //
      // The reference art builds this robot out of teal candlesticks and the
      // user's 2026-09-11 call overrides that deliberately — "a plated
      // machine: panelled chassis, visible joints, antenna, glowing eyes. No
      // candle glyphs" — trading the made-of-the-market link for an
      // unambiguous robot read.
      //
      // Every origin below is a whole CELL. `litRect` snaps both coordinates
      // to the 4-unit grid and leaves sizes alone, so an origin off the grid
      // moves when the grid rounds it, and two seams authored less than a
      // cell apart land in the same one — increment 10's surviving mutation,
      // one body over.
      bars(body, accents) {
        // A seam's thickness. Sizes are not snapped, so this is free to be
        // whatever reads: 3 units is 3.75px at the MODEL's 1.25 scale, thin
        // enough to be a gap and thick enough to survive the encoder.
        const cut = 2;
        // Bottom-up, and every part MEETS the next. The candle cluster's real
        // defect was never its vocabulary — it was that five columns touched
        // neither each other nor the head above them, so the silhouette was a
        // lollipop over a picket fence and no amount of shading could join it.
        paint(body, litRect(-24, -48, 16, 48, 3), BODY_FILL);    // leg, near
        paint(body, litRect(8, -48, 16, 48, 3), BODY_FILL);      // leg, far
        // Feet, over the legs' ends: a leg that stops as a bare stick stands
        // the machine on stilts rather than on the floor (2026-09-24).
        paint(body, litRect(-28, -8, 24, 8, 3), BODY_FILL);      // foot, near
        paint(body, litRect(4, -8, 24, 8, 3), BODY_FILL);        // foot, far
        paint(body, litRect(-28, -72, 56, 28, 4), BODY_FILL);    // pelvis
        paint(body, litRect(-32, -132, 64, 60, 6), BODY_FILL);   // the chassis
        // Shoulders, which exist so the arms have somewhere to hang FROM: an
        // accent rotates about its own origin, and an arm pivoting inside the
        // chassis tears out of it on the first gesture.
        paint(body, litRect(-48, -120, 16, 20, 4), BODY_FILL);
        paint(body, litRect(32, -120, 16, 20, 4), BODY_FILL);
        // Panelling and joints are CUTS, never separate plates. `paint` lays
        // three banded insets into every volume, and on a small shape those
        // come out as a dark ring round a pale core — the joint cap that made
        // the trader's elbow read as a bolt, which a robot built out of little
        // plates and pins would repeat everywhere. One slab keeps the bands
        // reading as shading; the panels are what is left between the cuts.
        //
        // `{ keepFill, matte }`, the same shape as `shod` and the hand's
        // creases, for the same two reasons: keepFill so a seam is neither
        // recoloured by the mood nor outlined, matte so `shade` does not mix
        // its lit rung 0.3 toward the lamp's hue and turn a gap between two
        // plates into a scratch down the middle of one.
        const seamed = { keepFill: true, matte: true };
        // Kept below y -110, where the skull's own silhouette ends: panelling
        // drawn under the head is panelling nobody sees.
        paint(body, litRect(-32, -104, 64, cut, 1), MACHINE.seam, undefined, seamed);
        paint(body, litRect(0, -104, cut, 32, 1), MACHINE.seam, undefined, seamed);
        // The arms ARE the gesture. `gesture()` only ever moves what is on
        // `accents`; the cluster put five bars there and let the ripple be the
        // animation ("pump"), and a chassis has no ripple. Without these the
        // machine would go inert for every one of `ANIM`'s animations — the
        // detail-versus-animation failure this whole redesign is built to
        // avoid, and the one thing here no screenshot would ever report.
        for (const side of [-1, 1]) {
          const arm = new PIXI.Graphics();
          paint(arm, litRect(-6, -8, 12, 52, 4), BODY_FILL);
          // The visible joints: a shoulder and an elbow, cut into the one
          // slab rather than built as three.
          paint(arm, litRect(-6, 4, 12, cut, 1), MACHINE.seam, undefined, seamed);
          paint(arm, litRect(-6, 24, 12, cut, 1), MACHINE.seam, undefined, seamed);
          arm.x = side * 40;
          arm.y = -108;               // inside the shoulder block, not at the floor
          arm.gestureMode = "swing";  // what hangs from a joint swings
          // Not quite matching, the same two numbers `figure` spends: dead
          // symmetry reads as a mannequin even on a machine.
          arm.restRotation = side < 0 ? -0.06 : 0.10;
          // A lighter value than the chassis, or a resting robot reads as one
          // slab and its arms only appear mid-animation.
          arm.shadeFactor = 1.35;
          accents.push(arm);
        }
        // Unchanged, and deliberately: `HEAD_TRAVEL.bars`, `SHADOW_WIDTH.bars`
        // and the mood tag's own offset are all tuned against this number.
        return -138;
      },
      // A weighted column — deliberate, unhurried, hard to topple. The calm
      // end of the register; a piece that looks like it is thinking.
      monolith(body, accents) {
        paint(body, litRect(-26, -104, 52, 78, 20), BODY_FILL);
        const base = new PIXI.Graphics();
        paint(base, litRect(-38, -30, 76, 30, 14), BODY_FILL);
        base.gestureMode = "shift";   // a weighted column doesn't wave, it leans
        base.restX = 0;
        accents.push(base);
        return -134;
      },
      // A person: torso, two legs, two arms. The one body here that is NOT a
      // market glyph, and that is the point — the trader is the human in the
      // room, so he doesn't get to be made of candles.
      figure(body, accents) {
        // The gap has to be wide enough to survive the stream: at 6 units the
        // two legs merged into one column and he read as a pedestal.
        paint(body, litRect(-19, -56, 13, 56, 6), BODY_FILL);
        paint(body, litRect(6, -56, 13, 56, 6), BODY_FILL);
        // Waist under a broader chest. The taper is what reads as a person —
        // one capsule reads as a pill — and the wider shoulders are what stop
        // the head (28px, shared with every other body) looking oversized.
        paint(body, litRect(-26, -86, 52, 40, 14), BODY_FILL);
        paint(body, litRect(-33, -128, 66, 54, 22), BODY_FILL);
        for (const side of [-1, 1]) {
          const arm = new PIXI.Graphics();
          paint(arm, litRect(-7, -5, 14, 66, 7), BODY_FILL);
          arm.x = side * 36; arm.y = -120;   // pivot at the shoulder
          // Not quite straight and not quite matching: dead-symmetrical arms
          // read as a mannequin, and the asymmetry costs two numbers.
          arm.restRotation = side < 0 ? -0.05 : 0.09;
          // v1's arms were the torso's exact colour, so a resting figure read
          // as a capsule and the arms only appeared mid-animation. A lighter
          // shade separates them from both the body and the dark room.
          arm.shadeFactor = 1.35;
          accents.push(arm);
        }
        // The head overlaps the torso rather than resting on it: a hairline gap
        // at this size reads as a floating head, not as a neck.
        return -152;
      },
      // The original: face on a single disc, no body. Kept as the baseline the
      // others are judged against.
      orb(body) {
        paint(body, litCircle(0, -60, 46), BODY_FILL);
        return -66;
      },
    };

    // The MODEL's head, built where node can run it (increment 13): a
    // style-keyed registry, the shape of `BODIES`, so the one piece of the
    // cast `character()` used to build inline can be graded. Only the MODEL
    // is registered; candle and monolith are gallery bodies and keep the
    // ball. Returns the crown — the highest unit it draws — because the mood
    // tag is placed from it.
    function boxHead(skull, beacon) {
      const { x, y, w, h, depth, radius } = MACHINE.head;
      const cut = 2;
      const seamed = { keepFill: true, matte: true };
      // The far faces first, each a slanted face that recedes from the front
      // face's own corners. Increment 13 drew them as ONE rounded plate
      // offset by the depth, and that ends square a whole depth short of the
      // face's top-right and bottom-left corners — two cards laid askew, not
      // a box (2026-09-24). The side is the one turned away from the lamp,
      // so it sits a shade step down: three faces, three values, one solid.
      const top = [[x - depth, y - depth], [x + w - depth, y - depth], [x + w, y], [x, y]];
      const near = [[x - depth, y - depth], [x, y], [x, y + h], [x - depth, y + h - depth]];
      paint(skull, litPath(top), BODY_FILL);
      paint(skull, litPath(near), BODY_FILL, undefined, { shadowed: true });
      paint(skull, litRect(x, y, w, h, radius), BODY_FILL);
      // Both plates are `BODY_FILL` and `paint` bands both toward the one
      // lamp, so without a cut the side comes out the value of the front and
      // the extrusion reads as a notch. The reference draws a dark gap here.
      paint(skull, litRect(x, y, cut, h, 1), MACHINE.seam, undefined, seamed);
      paint(skull, litRect(x, y, w, cut, 1), MACHINE.seam, undefined, seamed);
      // The antenna, on the top face's centre. The mast is a plate like any
      // other; the beacon is a lit fill on its own Graphics, because `repaint`
      // clears the skull on every mood change and replays only what `paint`
      // recorded.
      // The top face spans `x - depth .. x - depth + w`; its centre, less
      // half the mast.
      const mastX = x - depth + w / 2 - 2;
      paint(skull, litRect(mastX, y - depth - 12, 4, 12, 1), BODY_FILL);
      beacon.roundRect(snap(mastX) - 4, snap(y - depth - 20), 12, 8, 2).fill(0xd1d4dc);
      return y - depth - 20;
    }
    const MACHINE_HEADS = {
      bars: boxHead,
    };

    // THE TRADER IN PROFILE (increment 16). The chair faces up-left, away from
    // the camera, and for two increments that bought the back of a head: a
    // hair disc over a nape, which read as a ball on a collar. The user's call
    // was that he should be LOOKING AT THE CHARTS, which is a side view — so
    // the head turns out of the body's three-quarter-back into profile toward
    // the painted chart screens, and the body keeps the chair's measured yaw
    // (`seatedRig`'s bones, lap bracket and seat budget all hang off it).
    //
    // Authored facing +x and mirrored by `side`; every coordinate is a whole
    // CELL so the mirror lands on the grid `litRect` snaps to. Big masses go
    // through `paint` (hair, face); the small features are FLAT fills on their
    // own Graphics — a 4-unit nose through `paint` is three concentric bands,
    // the joint-cap "dark ring" a fourth time.
    // The seated trader's hand as one outline in its own frame (the
    // forearm's: +y runs heel to fingertip, x across the four fingers), about
    // 16 across by 20 along — `ARM.hand`'s numbers. Authored with the flare on
    // +x; `seatedRig` mirrors it to whichever side faces the floor. The heel
    // is 11, a little inside the sleeve's 12, so the wrist does not step out
    // into a box, and its back corner is rounded where it leaves the cuff.
    // (One line, so the suite's `_js_const` can lift it.)
    // SPLAYED (Sprint 17 #7): four fingers fanning toward the floor side
    // (0, 6, 11, 16 degrees off the forearm's line, the back finger straight so
    // the hand stays flush with the back of the wrist), cut apart by three
    // notches that open from the web at y 8 to 2.2-3.1 units at the tips:
    // at least one keyline block (2 frame px = 1.6 units at CAST_SCALE 1.25),
    // so the filter sees a gap in the ALPHA and draws the keyline into it.
    // Painted crease lines inside a closed outline did not survive the 1:1
    // frame: the rim ran unbroken round the tips (the mitten). Concave, so
    // `litPath` bands its convex hull and cuts each band back to the outline
    // (the light patch by the cuff). Generated by a script (review dir); kept
    // a literal because the suite parses it.
    const HAND_OUTLINE = [[0, 1], [2, 0], [11, 0], [14, 2.6], [16.94, 15.83], [15.99, 17.04], [14.54, 16.52], [11.5, 8], [12.79, 17.87], [11.74, 18.99], [10.34, 18.35], [7.7, 8], [8.19, 18.81], [7.04, 19.83], [5.71, 19.07], [3.9, 8], [3.25, 17.1], [2, 18], [0.75, 17.1], [0, 15.5]];

    // How far down (+y) the outline reaches at x: the lowest crossing of the
    // vertical line through x. -Infinity where x misses the outline.
    function outlineDepthAt(pts, x) {
      let depth = -Infinity;
      for (let i = 0; i < pts.length; i++) {
        const [ax, ay] = pts[i], [bx, by] = pts[(i + 1) % pts.length];
        if (x < Math.min(ax, bx) || x > Math.max(ax, bx)) continue;
        const y = ax === bx ? Math.max(ay, by) : ay + (by - ay) * (x - ax) / (bx - ax);
        depth = Math.max(depth, y);
      }
      return depth;
    }

    const PROFILE_HEAD = {
      // The cranium: hair, back of the head and the crown. Painted first.
      skull: [-4, -8, 18],
      // Skin under the back of the skull: the jaw's back and the nape. Hair
      // hanging to the jaw behind a profile reads as a BEARD, so the hair
      // stops at the ear and this is what shows below it. Painted OVER the
      // skull, from the ear's bottom down: laid under it, the skull's lower
      // arc still hung a tuft of hair below the ear (the user's catch), and
      // no circle ends on a straight line. Square-cornered: its back-top
      // corner sits right under the ear's back edge, and any rounding there
      // lets a speck of the skull through. `test_no_hair_shows_below_the_ear`.
      nape: [-8, 8, 16, 12, 0],
      // The face, as one CONVEX outline (increment 18): forehead rounding
      // into the brow, a straight front for the nose and lips to sit on, a
      // chin, a jaw angle, and the back edge running up the cheek to the
      // temple on a DIAGONAL. That diagonal is the haircut: the old face was
      // a rect whose back edge stopped 8 units short of the ear, so a column
      // of hair ran from the fringe to the jaw in front of it — "too much
      // hair on the left of the ear" (the user's catch). Now the cheek is
      // skin all the way back to the ear's FRONT, and the hair keeps the
      // head above and behind the ear, where a tapered Ivy League cut leaves
      // it. (A first cut carried the cheek back UNDER the ear too; the head
      // read as a bald egg with a cap on, and the ear vanished into skin.)
      face: [[6, -21], [14, -21], [18, -17], [20, -10], [20, 10], [18, 16],
             [13, 20], [2, 20], [-3, 16], [-3, -6], [0, -13]],
      // The top: longer than the sides and swept up and back off the brow,
      // overlapping the face by 2 so no seam of skull shows between them.
      // Its underside is the HAIRLINE, and it sits at -19 where it used to
      // sit at -12 — the forehead the head did not have.
      fringe: [[-6, -20], [-2, -26], [6, -28], [14, -27], [19, -24],
               [20, -21], [15, -19], [4, -19]],
      // Flat features, [x, y, w, h] from the head's origin, facing +x. The
      // face is in thirds — hairline -19 to brow -7, brow to nose base 6,
      // nose base to chin 20 — `test_the_profile_face_is_in_thirds`.
      //
      // The nose is a small rounded wedge, [x, y] points: it used to start at
      // eye level and hang 14 units, a third of the head. Then it was a
      // triangle whose tip pointed straight out (24, 4), and under the keyline
      // that read as a beak ("his nose is weird", 2026-09-25). A nose points
      // DOWN: a sloped bridge to a rounded tip at 5, with a flat underside
      // back to the face. A longer bridge from the brow was tried and was a
      // bigger beak.
      nose: [[20, -2], [21, 0], [22, 3], [23, 5], [22, 7], [20, 7]],
      // The eye is a white with a pupil in it, and WHERE the pupil sits is
      // the other half of the look: the head nods only `GAZE_HEAD_SHARE` of
      // the way, and the pupil rides the front-upper corner for the rest.
      // Drawn as an ellipse inside this box.
      eye: [13, -4, 4, 4],
      // The shadow under it: wider than the eye and hanging 3 units below
      // it, since anything under 2 is lost to the keyline's 2px grid.
      eyeBag: [12, -1, 6, 4],
      // Inside the white's oval, so half a unit in from the box's corner:
      // the box corner is outside an ellipse, and a pupil there sits on skin.
      pupil: [14.5, -3.5, 2, 2],
      brow: [11, -7, 8, 2],
      // The ear, an ellipse in this box from brow to nose base, and its
      // bottom is where the nape starts. It sits ON the cheek now.
      ear: [-8, -6, 6, 14],
      // The short sideburn a taper leaves in front of the ear: from the
      // temple hair down to mid-ear, no further.
      sideburn: [-2, -12, 2, 8],
      // A one-unit line in the crease colour: at the skin's shade it vanished
      // into the face's own shade band, and the face below the nose was a
      // blank wall.
      mouth: [17, 12, 3, 1],
      // The joint the head nods about: the top of the neck, under the jaw.
      pivot: [4, 20],
    };

    // A rect mirrored about x = 0 when the head faces -x.
    function mirrorRect([x, y, w, h, ...rest], side) {
      return [side < 0 ? -(x + w) : x, y, w, h, ...rest];
    }

    function profileHead(skull, features, side) {
      const P = PROFILE_HEAD;
      const [cx, cy, r] = P.skull;
      const hair = { keepFill: true, noRim: true, matte: true };
      const skin = { keepFill: true, noRim: true };
      const mirror = (pts) => pts.map(([x, y]) => [side * x, y]);
      paint(skull, litCircle(side * cx, cy, r), HUMAN.hair, undefined, hair);
      paint(skull, litRect(...mirrorRect(P.nape, side)), HUMAN.skin, undefined, skin);
      paint(skull, litPath(mirror(P.face)), HUMAN.skin, undefined, skin);
      paint(skull, litPath(mirror(P.fringe)), HUMAN.hair, undefined, hair);
      // Not snapped: the pupil is half a CELL by design, and snapping its
      // mirrored x rounds it OUT of the eye on one side — which
      // `test_the_profile_is_one_head_mirrored` caught.
      const flat = (rect, colour) => {
        const [x, y, w, h] = mirrorRect(rect, side);
        features.rect(x, y, w, h).fill(colour);
      };
      // An ellipse inscribed in a feature box: an ear or an eye drawn as a
      // rect is a block, which is the whole of increment 18's brief.
      const oval = (rect, colour) => {
        const [x, y, w, h] = mirrorRect(rect, side);
        features.ellipse(x + w / 2, y + h / 2, w / 2, h / 2).fill(colour);
      };
      const skinBase = shade(HUMAN.skin, rampLevel("base"));
      const skinShade = shade(HUMAN.skin, rampLevel("shade"));
      flat(P.sideburn, HUMAN.hair);
      oval(P.ear, skinBase);
      // The ear's hollow: a shadowed oval set toward the FACE side, so the
      // rim of the ear reads as a C round it rather than as a flat disc.
      const [ex0, ey0, ew, eh] = P.ear;
      oval([ex0 + ew / 2, ey0 + eh / 4, ew / 3, eh / 2], skinShade);
      features.poly(mirror(P.nose).flat()).fill(skinBase);
      flat(P.brow, HUMAN.hair);
      // A tired shadow under the eye (increment 19): an oval the white laps
      // the top of, so what shows is a crescent below it. Halfway between the
      // skin's base and the mouth's shade — at the mouth's own value it reads
      // as a bruise. Tied to the eye box, never the pupil, which moves with
      // the gaze. `test_the_profile_eye_is_tired`.
      oval(P.eyeBag, mixHex(skinBase, skinShade, 0.5));
      oval(P.eye, 0xd8c6b0);
      flat(P.pupil, 0x1b1716);
      flat(P.mouth, HUMAN.crease);
      const eye = mirrorRect(P.eye, side);
      return {
        crown: Math.min(cy - r, ...P.fringe.map((p) => p[1])),
        chin: Math.max(...P.face.map((p) => p[1])),
        eye: [eye[0] + eye[2] / 2, eye[1] + eye[3] / 2],
        pivot: [side * P.pivot[0], P.pivot[1]],
      };
    }

    // Where the charts are: the centre of every painted screen whose role is
    // `chart`, averaged. Null with no plate — a head with nothing to look at
    // looks level.
    function chartTarget(screens) {
      const charts = (screens || []).filter((s) => s.role === "chart" && s.quad);
      if (!charts.length) return null;
      let x = 0, y = 0;
      for (const s of charts) {
        for (const [px, py] of s.quad) { x += px / s.quad.length; y += py / s.quad.length; }
      }
      return { x: x / charts.length, y: y / charts.length };
    }

    // Which way a profile faces: toward the charts, or with no charts to
    // look at, the way the chair points. Never 0 — a profile has a front.
    function gazeSide(target, x, facing) {
      if (target && target.x !== x) return Math.sign(target.x - x);
      return (facing && Math.sign(facing[0])) || -1;
    }

    // How far up a head facing `side` must tilt for its eye to point at
    // `target`, both in screen pixels. Level when there is no target or the
    // target is behind him; never past GAZE_MAX, a neck's comfortable range.
    const GAZE_MAX = 0.6;
    // How much of that the NECK does. People look up at a screen with the
    // eyes as much as with the head; a head nodding the whole 28 degrees to
    // the charts reads as a man studying the ceiling.
    const GAZE_HEAD_SHARE = 0.5;
    function gazePitch(eye, target, side) {
      if (!target) return 0;
      const ahead = side * (target.x - eye.x);
      if (ahead <= 0) return 0;
      return Math.max(0, Math.min(GAZE_MAX, Math.atan2(eye.y - target.y, ahead)));
    }

    // A machine and a person shouldn't wear the same face. The bodies built
    // out of market glyphs get a readout — visor, LED eyes, a stepped mouth,
    // and no brows, because a brow is a muscle. The figure keeps the human
    // one, and so does the orb, which is a face and nothing else.
    const FACE_KIND = {
      bars: "machine", candle: "machine", monolith: "machine",
      figure: "human", orb: "human",
    };

    // One inhabitant: a body whose colour is its mood, a face that reads at a
    // glance, and a label carrying the numbers.
    // How wide a body's contact patch is, per style — the same registry shape
    // as HEAD_TRAVEL, and checked against STYLES for the same reason: a new
    // body that silently inherits someone else's footprint reads as floating.
    //
    // `bars: 34` was tuned against the candle cluster's five splayed columns.
    // Increment 11 replaced those with two legs, and the stance was MEASURED
    // rather than left open: they plant x -24..24, i.e. 48 units apart,
    // against the figure's 38. The 34 was kept — a contact patch narrower
    // than the stance is what a shadow under two feet looks like — but it is
    // now a number with a measurement behind it rather than an inheritance.
    const SHADOW_WIDTH = { bars: 34, figure: 30, candle: 22, monolith: 26, orb: 20 };

    // How tall a contact patch is against its own width: a roughly circular
    // patch on the floor, seen from the room's viewpoint. So this number is
    // not just the ellipse's proportions — it IS this room's foreshortening,
    // the factor by which the floor squashes anything measured across it. The
    // shadow's height and the y half of its throw both read it, because they
    // are the same fact, and writing the throw's squash as its own literal is
    // how the two would silently drift apart.
    //
    // Taken against the patch's own 2:1 base, NOT against its drawn width:
    // `CONTACT.widthScale` widens the blob that is DRAWN — an art call about
    // how far a body's contact spills — while the floor's foreshortening is a
    // property of the room, unchanged by how wide the plate wants the smudge.
    const SHADOW_ASPECT = 0.46;

    // KI-061 — which way ONE figure's shadow falls. The lamp is painted as a
    // point (`LIGHT.key`, plate pixels, which are stage pixels), so a shadow
    // runs from the lamp through the foot and on: away from the key, per
    // figure. `KEY_AXIS` is a single room-wide vector, right for the band
    // lighting's broad "lamp is up and to the right" and wrong for a figure
    // standing under the lamp — the trader sits at the key's own x, and a
    // global axis threw his shadow down-and-left as if the lamp were beside
    // him. No key, or a foot exactly on it, falls back to `KEY_AXIS`.
    function throwAxis(footX, footY) {
      const key = (LIGHT && LIGHT.key) || [];
      const dx = footX - Number(key[0]), dy = footY - Number(key[1]);
      const len = Math.hypot(dx, dy);
      return Number.isFinite(len) && len > 0 ? [dx / len, dy / len] : KEY_AXIS;
    }

    // One soft blob, built once and shared. A hard ellipse reads as a sticker
    // and three stacked ellipses band visibly (the same thing that made the
    // first vignette look like a wireframe) — and `FillGradient` cannot do a
    // radial alpha ramp, which is exactly the limitation the wall port found.
    let shadowTexture = null;
    function softShadow() {
      if (!shadowTexture) {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 128;
        const ctx = canvas.getContext("2d");
        const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
        g.addColorStop(0, "rgba(0,0,0,0.85)");
        g.addColorStop(0.5, "rgba(0,0,0,0.42)");
        g.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, 128, 128);
        shadowTexture = PIXI.Texture.from(canvas);
      }
      const sprite = new PIXI.Sprite(shadowTexture);
      sprite.anchor.set(0.5);
      return sprite;
    }

    // Light is overhead, so height off the floor spreads the shadow and
    // thins it. This is the whole grounding cue: without it a jump is a body
    // sliding up the screen, and with it the floor is somewhere real.
    // A shadow lives on its own layer, so hiding a character's container does
    // *not* hide it. The animation sheet caught two of them hanging in mid-air
    // where MODEL and TRADER had been. Visibility is a property of the
    // character; both eval surfaces go through here.
    function setCharacterVisible(char, visible) {
      char.container.visible = visible;
      if (char.shadow) char.shadow.visible = visible;
    }

    function shadowTick(char) {
      // First, always: `CONTACT` is a top-level read, so a character with no
      // shadow must leave before anything touches it.
      if (!char.shadow) return;
      const lift = Math.max(0, char.baseY - char.container.y);
      const spread = 1 + lift * 0.004;
      // Thrown AWAY from the key — from where the lamp is painted, through
      // this figure's foot (`throwAxis`, KI-061). For the model, far to the
      // lamp's left, that is left and a little down; for the trader, under
      // it, straight down onto the floor in front of him. Centred,
      // which is what this did until Sprint 16, a figure the room lights hard
      // from one side gets a puddle under its feet; a puddle is the loudest
      // "pasted on" cue in the before-frame, and the whole of KI-051's
      // complaint about the cast not belonging to its room.
      //
      // The y half is squashed by `SHADOW_ASPECT / 2` because the throw lies
      // ON the floor, and the floor is exactly that foreshortened — it is the
      // same fact that makes a round contact patch draw as a flat ellipse, so
      // it is read from the same constant rather than restated as a number of
      // its own. Unsquashed, the measured direction (steeply downward) walks
      // the shadow clear of the feet and the figure floats again, having
      // gained a direction and lost its floor.
      // The cast lives in `layers.chars`, scaled by CAST_SCALE; the key is in
      // plate pixels. Compared unscaled, the trader's foot (1373/1.25) reads
      // as 240px LEFT of a lamp he sits beside and the throw goes back to
      // down-and-left — the first cut of this fix did exactly that.
      const axis = throwAxis(char.container.x * CAST_SCALE, char.baseY * CAST_SCALE);
      const throwX = axis[0] * CONTACT.throwScale * SHADOW_THROW;
      const throwY =
        axis[1] * CONTACT.throwScale * SHADOW_THROW * (SHADOW_ASPECT / 2);
      // The throw moves the patch AND grows it by the same amount, so the
      // half-extent gains exactly what the centre gave up: the lamp-side edge
      // stays where the foot is and only the far edge travels. Offsetting
      // alone is the trade this ticket would most easily make by accident —
      // a shadow that no longer touches the body it belongs to reads as a
      // second figure, which is a worse floating than the one being fixed.
      char.shadow.x = char.container.x + throwX;
      char.shadow.y = char.baseY + 3 + throwY;
      char.shadow.width =
        (char.shadowW * 2 * CONTACT.widthScale + 2 * Math.abs(throwX)) * spread;
      char.shadow.height =
        (char.shadowW * SHADOW_ASPECT + 2 * Math.abs(throwY)) * spread;
      char.shadow.alpha = CONTACT.opacity / (1 + lift * 0.014);
    }

    // Which side of the body an accent composites on. `near` means "on the
    // camera side" — a forearm that comes forward over the lap, not one
    // hanging behind a torso that is 48 units wide.
    //
    // KI-084: every accent used to go on before the body, and the seated
    // torso spans ±24, so the seated arms were invisible at rest from the day
    // the seated rig shipped. The fix is deliberately NOT a change to the
    // `accents` array — `restAccents`, `gesture()`, the seat-fit drivers and
    // `character()`'s own `accents[0]`/`accents[1]` arm aliases all index it,
    // and a partition applied in place would have moved all four of them.
    // Only the parenting changes; rotation still comes from `restRotation`
    // regardless of which side of `body` a Graphics is parented on.
    //
    // Untagged is FAR, which is the pre-KI-084 behaviour: every standing
    // `BODIES.*` style tags nothing, and a wick reading as emerging from the
    // candle depends on staying behind it.
    // Is this body turned far enough from the camera to have no face left?
    //
    // The facing is a screen vector measured off the painted furniture, and
    // its y component alone answers this: negative means the body points UP
    // the screen, which in this projection is away from the camera. The 2px
    // deadband is for a re-measured plate — a chair painted in dead profile
    // should not flip the head around on a pixel of dithering.
    function facesAway(facing) {
      return !!facing && facing.length === 2 && facing[1] < -2;
    }

    function depthOrder(accents, which) {
      return accents.filter((a) => (a.depth === "near") === (which === "near"));
    }

export {
  REACTIONS, STYLES, CAST, GROUND, CAST_SCALE, styleFor, faceFor, BODIES,
  MACHINE_HEADS, HAND_OUTLINE, outlineDepthAt, profileHead, chartTarget,
  gazeSide, GAZE_HEAD_SHARE, gazePitch, FACE_KIND, SHADOW_WIDTH, softShadow,
  setCharacterVisible, shadowTick, facesAway, depthOrder,
};
