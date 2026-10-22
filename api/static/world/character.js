// /world — Building a character and its expression: gestures, accents, faces, eyes,
// mood.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import { keylineFilter } from "./keyline.js";
import { PLATE } from "./config.js";
import { NEUTRAL, BODY_FILL, HUMAN, modelBadge } from "./palette.js";
import {
  paint, repaint, moodBlindParts, litCircle, moodFill, moodRim,
} from "./shading.js";
import { snap } from "./tiers.js";
import { app, layers, label } from "./boot.js";
import { anchorFor } from "./room.js";
import {
  GROUND, styleFor, faceFor, BODIES, MACHINE_HEADS, profileHead, chartTarget,
  gazeSide, FACE_KIND, SHADOW_WIDTH, softShadow, facesAway, depthOrder,
} from "./cast.js";
import { seatedRig, rng32 } from "./animation.js";

    function character(name, style) {
      style = BODIES[style] ? style : styleFor(name);
      const container = new PIXI.Container();
      container.y = app.screen.height * GROUND;
      // C2: which anchor this name paints against decides the rig. Only the
      // trader is ever seated — the plate's `pose` is the one thing that says
      // so, not a second decision made here. Every other `name` this function
      // is ever called with (moods and styles, from the gallery/sheet eval
      // surfaces) misses PLATE.cast entirely and gets "standing" back.
      const anchor = anchorFor(name.toLowerCase());
      container.pose = anchor.pose;
      // How big this figure is drawn against the furniture it uses, and which
      // way that furniture points. Both are measurements off the plate
      // (`scripts/measure_chair.py`), so both arrive as manifest data rather
      // than as page constants — the same rule the seat rect and the tube
      // bores already follow. A body with no painted furniture keeps 1 and a
      // camera-facing pose, which is every standing style.
      const figureScale = anchor.figureScale || 1;

      const body = new PIXI.Graphics();
      const head = new PIXI.Container();
      // The head's resting nod, on its own node: `head.rotation` belongs to
      // the animations (`setGaze`, `restSeatedCharacter` zero it every tick),
      // so a gaze parked there would render one frame and snap level.
      const aim = new PIXI.Container();
      const skull = new PIXI.Graphics();
      // A profile's small features: flat fills `repaint` never touches.
      const features = new PIXI.Graphics();
      let profile = null;
      const eyeL = new PIXI.Graphics();
      const eyeR = new PIXI.Graphics();
      const mouth = new PIXI.Graphics();
      const browL = new PIXI.Graphics();
      const browR = new PIXI.Graphics();
      const visor = new PIXI.Graphics();
      const beacon = new PIXI.Graphics();
      const accents = [];

      const headY = container.pose === "seated"
        ? seatedRig(body, accents, anchor.facing)
        : BODIES[style](body, accents);
      // The orb *is* the face; every other body carries a head above it.
      // Not one of BODIES.*: the orb IS its own face and skips a head
      // entirely, but every other style's head is built here rather than in
      // its body method. Shaded the same way as the rest of the cast — the
      // head is the single largest surface carrying mood (`FACE`/`setExpression`
      // paint eyes, brows and mouth onto it), and a flat head on an otherwise
      // lit body is the one inconsistency a gallery grid would show first.
      // The orb IS its own face and takes no head at all. Everything else gets
      // one here — but a machine's head and a person's are no longer the same
      // circle in two colours. `FACE_KIND` already decides which FACE the style
      // wears (`machineFace` vs `humanFace`); the head under that face now
      // follows the same decision instead of being uniform beneath it, which is
      // what made the trader read as a bauble with features painted on.
      //
      // The human head is two authored volumes, not one: hair sitting high and
      // a slightly smaller face dropped below it, so the crown reads as hair
      // rather than as a hairline drawn on a ball. Both `keepFill`, so the mood
      // reaches them as a rim and leaves skin looking like skin. The face
      // circle is r24 at y+2 — every feature `humanEyes`/`humanFace` draws
      // (eyes at y-4 ±10, brows above them, mouth at y+12) stays well inside
      // it, and they are separate Graphics added after the skull, so the hair
      // can never cover them.
      // The crown every head but the box keeps: the ball's r28.
      let crown = -28;
      if (style === "orb") {
        // no head
      } else if (MACHINE_HEADS[style]) {
        crown = MACHINE_HEADS[style](skull, beacon);
      } else if (FACE_KIND[style] === "human") {
        // r22/r19, not the machine's r28. The first frame of the redesign had
        // a head 56 units across on a torso 48 across — a head wider than the
        // shoulders under it, which is the single loudest "this is not a
        // person" cue a figure can have, and one no test was ever going to
        // report. The machine keeps r28: a blocky chassis carries a big head,
        // and `SHADOW_WIDTH`/`HEAD_TRAVEL` are tuned to it.
        //
        // Turned away from the camera, the same two circles go on in the
        // opposite order: hair OVER skin is the back of a head, and hair under
        // skin is a face. It is two lines rather than a second head because
        // that is genuinely all the difference there is at this size — and the
        // features below are hidden for the same reason, not tuned. (The user
        // asked for the turn knowing the face goes with it: "I do not care
        // about his mimics and that face is not visible".)
        const away = facesAway(anchor.facing);
        if (away) {
          // Turned from the camera, the head turns to the CHARTS instead of
          // showing its back (increment 16, `profileHead`). The side is the
          // one the charts are on; with no plate, the chair's own.
          const side = gazeSide(chartTarget(PLATE && PLATE.screens), anchor.x, anchor.facing);
          profile = profileHead(skull, features, side);
          profile.side = side;
          crown = profile.crown;
          for (const part of [eyeL, eyeR, mouth, browL, browR]) part.visible = false;
        } else {
          // Facing the camera: hair behind, face in front. Hair is matte
          // (increment 15) — a lamp band on a dark disc reads as a bald spot.
          paint(skull, litCircle(0, -4, 22), HUMAN.hair, undefined,
                { keepFill: true, noRim: false, matte: true });
          paint(skull, litCircle(0, 2, 19), HUMAN.skin, undefined,
                { keepFill: true, noRim: true, matte: false });
        }
      } else {
        paint(skull, litCircle(0, 0, 28), BODY_FILL);
      }
      head.y = headY;
      aim.addChild(skull, features, beacon, visor, browL, browR, eyeL, eyeR, mouth);
      if (profile) {
        // Nod about the top of the neck, so the chin stays on it.
        aim.pivot.set(...profile.pivot);
        aim.position.set(...profile.pivot);
      }
      head.addChild(aim);

      const nameTag = label(name, -40, 22, 18);
      const stat = label("", -80, 46, 14, NEUTRAL);
      // The mood tag rides above the head, so it has to follow the SCALED
      // head: `headY` is in rig units and the rig is drawn `figureScale`
      // bigger than them.
      // Lifted by however far a head stands above the ball's crown, so the
      // box head's antenna does not run through its own tag.
      const moodTag = label("", -40, (headY + crown + 28) * figureScale - 56, 16, NEUTRAL);
      // Accents go behind the mass, so a wick reads as emerging from it —
      // all but the ones tagged `near`, which are on the camera side and
      // composite in front of it (KI-084, see `depthOrder`). Labels stay
      // last: a name tag a forearm can cover is the same defect as an arm
      // the torso covers.
      //
      // The body is scaled as ONE node, with the labels deliberately outside
      // it — type at 1.7x is a separate decision from a figure at 1.7x. It
      // cannot be `container.scale`: `playAnimation`'s pulse/cheer/flicker and
      // `resetIdle` write that every tick, so a per-figure scale parked there
      // would render for one frame and then be overwritten — the same failure
      // `bone()`'s `restRotation` comment describes. One container in between
      // survives both, and every animation below keeps writing rig-local units
      // to `body`/`head`/the accents exactly as it did.
      const rig = new PIXI.Container();
      rig.scale.set(figureScale);
      const badge = style === "bars" ? modelBadge(body) : null;
      rig.addChild(...depthOrder(accents, "far"), body, ...(badge ? [badge] : []), head, ...depthOrder(accents, "near"));
      // The person wears the keyline; the labels beside him do not, and
      // neither does his shadow (it lives on `layers.shadows`). Only the
      // seated figure: the machine keeps its rim, a glyph's own edge.
      const keyline = container.pose === "seated" ? keylineFilter() : null;
      if (keyline) rig.filters = [keyline];
      container.addChild(rig, nameTag, stat, moodTag);
      layers.chars.addChild(container);

      // Seeded off the name, so MODEL and TRADER don't blink in unison — two
      // characters sharing a timer read as one animation on two puppets.
      let seed = 0;
      for (const ch of name) seed = (seed * 31 + ch.charCodeAt(0)) >>> 0;

      const shadow = softShadow();
      layers.shadows.addChild(shadow);

      const char = {
        container, body, head, skull, eyeL, eyeR, mouth, browL, browR, visor,
        accents, style, stat, moodTag, nameTag, faceKind: FACE_KIND[style],
        aim, profile, figureScale,
        baseY: container.y, mood: "neutral", anim: null, phase: 0,
        rand: rng32(seed || 1),
        // The shadow is cast by the figure, so it grows with it — a 1.7x body
        // over a 1x smudge is a figure standing on someone else's shadow.
        shadow, shadowW: SHADOW_WIDTH[style] * figureScale,
      };
      // The two arms `seatedRig` pushed onto `accents` are real Graphics,
      // already children of `container` — aliased under their own names so
      // the seated animations below have one to reach for by name instead of
      // indexing into the array.
      if (container.pose === "seated") {
        char.armNear = accents[0];
        char.armFar = accents[1];
      }
      // Two characters breathing on one clock read as one animation played on
      // two puppets — the exact thing the blink seed already avoids. Offset
      // the phase *and* vary the period, so they never drift into step either.
      char.phaseOffset = char.rand();
      char.breath = 3.2 * (0.85 + char.rand() * 0.3);
      setExpression(char, "neutral");
      restAccents(char);   // arms hang from their resting angle, not from zero
      // Loud, not fatal: a grey limb is a wrong pixel, a throw here is a
      // missing character on air. The suite runs every builder through the
      // same check and fails there instead.
      const blind = moodBlindParts(accents);
      if (blind.length) {
        console.error(`KI-073: ${style} draws ${blind.join(", ")} outside paint(); `
          + "it will never take a mood");
      }
      return char;
    }

    // Limb-less bodies still need a gesture — but not the same one. Rotating a
    // bar about its own base swings it like a felled tree, and at tier-3
    // amplitude the cluster came apart on cheer/wave/shrug; the candle's wicks
    // were worse, pivoting about the character's *feet*. Only what hangs from
    // a joint swings. Everything else pumps or shifts its weight.
    const GESTURE = {
      swing: (a, amt) => { a.rotation = (a.restRotation ?? 0) + amt * 0.35; },
      pump: (a, amt) => { a.scale.y = 1 + Math.abs(amt) * 0.55; },
      shift: (a, amt) => { a.x = snap((a.restX ?? 0) + amt * 6); },
      // A seated figure gestures with its arms. The leg bones are accents too
      // — they have to be, since each carries its own rotation — but swinging
      // a thigh, a shin and a shoe independently about their own joints does
      // not animate a leg, it dismantles one. Opting out by name beats
      // special-casing `limb` here: `gesture()` already dispatches on
      // `gestureMode`, so this needs no new branch.
      still: () => {},
    };

    function gesture(char, amount, stagger) {
      char.accents.forEach((accent, i) => {
        const offset = stagger ? Math.sin(amount * Math.PI * 2 + i * 0.9) : 1;
        const amt = amount * (i % 2 ? 1 : -1) * (stagger ? offset : 1);
        (GESTURE[accent.gestureMode] ?? GESTURE.swing)(accent, amt);
      });
    }

    function restAccents(char) {
      for (const accent of char.accents) {
        // Rest is not necessarily zero: dead-straight symmetrical arms read as
        // a mannequin, so the figure's hang at a slight, uneven angle.
        accent.rotation = accent.restRotation ?? 0;
        // The seated shrug lifts the forearm child on its own.
        if (accent.forearm) accent.forearm.rotation = accent.forearm.restRotation ?? 0;
        accent.x = snap(accent.restX ?? accent.x);
        accent.scale.set(1, 1);
      }
    }

    // Lighten or darken a mood colour without leaving its hue. Accents that
    // sit ON the body (arms) need to be a different value from it, or they
    // vanish into the mass at stream distance.
    //
    // Renamed from `shade` (KI-051): that name now belongs to the room-light
    // mix defined near `BODY_RIM` (`shade(baseHex, level)`, blends toward
    // `LIGHT.warmth`/`LIGHT.ambient`). This one is unrelated — a flat
    // channel-wise multiply against `factor` — and the two are NOT
    // interchangeable: `accent.shadeFactor` (`1.35`, a multiplier) fed to
    // the room-light `shade()` would clamp to a full-strength mix toward
    // `LIGHT.warmth` instead of a gentle lighten. Kept as its own function
    // under its own name rather than merged, so `applyMood`'s per-accent
    // lighten and `paint`'s per-band light direction stay two independent
    // knobs.
    //
    // It is now applied to a body's own FILL rather than to a tint (Task 8),
    // which is a rename of what it receives and not a change to what it does:
    // the arm is a lighter version of whatever colour the torso is painted,
    // and `paint()` lights both from the same lamp afterwards.
    function lightenTint(tint, factor) {
      const clamp = (v) => Math.max(0, Math.min(255, Math.round(v * factor)));
      return (clamp((tint >> 16) & 0xff) << 16)
           | (clamp((tint >> 8) & 0xff) << 8)
           | clamp(tint & 0xff);
    }

    // `liftDark` used to live here: a luminance floor applied to the TINT.
    // It was the right instinct aimed one step short of the pixel — the tint is
    // multiplied by the base fill before anyone sees it, so a tint comfortably
    // above the floor still rendered a body at 1.19:1. It also used the NTSC
    // luma weights on raw sRGB rather than WCAG relative luminance, so its
    // "floor" was not the quantity anyone measures. Both are now done in
    // world/visuals.py against the rendered colour, and the answer arrives
    // precomputed in BODY_TINT (KI-028).
    //
    // Task 8 finished the same thought one step further along: the rendered
    // colour is no longer something the GPU arrives at per pixel, it is what
    // `moodFill()` computes and `paint()` is handed. Nothing on this page
    // multiplies a mood over a drawn figure any more.

    // Faces are redrawn rather than tweened: a handful of primitives per mood
    // change is far cheaper than keeping a rig, and OBS renders in software.
    function setExpression(char, mood) {
      char.mood = mood;
      const { valence, arousal } = faceFor(mood);
      // Remembered so a blink can redraw the eyes alone and put them back
      // exactly as the mood had them.
      char.face = { valence, arousal };
      char.blinkClosed = false;
      const fill = moodFill(mood);
      char.visor.clear();
      if (char.faceKind === "machine") {
        machineFace(char, valence, arousal, fill);
      } else {
        humanFace(char, valence, arousal);
      }
      applyMood(char, mood);
    }

    // A readout, not an expression: the same two axes, computed. Arousal lights
    // more of the LED; valence steps the mouth. No brows — a brow is a muscle.
    function machineFace(char, valence, arousal, fill) {
      // 0.14, not 0.35: a dark inset of the head's own colour. The original
      // number was picked when the skull was a 0x545862 fill being *tinted*,
      // so it rendered at roughly a third of the mood colour and a 0.35 visor
      // came out the same value as the head — invisible on the real page.
      // KI-028 raised the base fill and this ticket replaced the tint with a
      // paint, so the head now renders at ~0.82 of the mood colour and 0.14
      // sits further below it than it ever did. Kept, deliberately: the visor
      // reads as a recess, and moving it is an art call with no frame behind it.
      char.visor.roundRect(snap(-21), snap(-16), 42, 23, 8).fill(lightenTint(fill, 0.14));
      machineEyes(char, arousal);
      char.browL.clear();
      char.browR.clear();
      // Small (user, 2026-09-24: "make his mouth smaller") — a readout under
      // the eyes, never wider than half the visor. The corners are not
      // snapped: at half-width 6 the CELL grid has only 4 and 8 to offer.
      const w = 6 + arousal * 2;
      const step = valence * 4;
      char.mouth.clear()
        .moveTo(-w, snap(12)).lineTo(-w / 3, snap(12) + step)
        .lineTo(w / 3, snap(12) + step).lineTo(w, snap(12))
        .stroke({ width: 2, color: 0xd1d4dc });
    }

    // Eyes are drawn on their own so a blink can close them without disturbing
    // the mood: `arousal` is passed in rather than read, and a blink passes 0.
    function machineEyes(char, arousal) {
      const lit = 2 + arousal * 5;
      for (const [eye, side] of [[char.eyeL, -1], [char.eyeR, 1]]) {
        eye.clear();
        eye.roundRect(snap(side * 10 - 6), snap(-5 - lit / 2), 12, lit, 1.5).fill(0xd1d4dc);
      }
    }

    function humanEyes(char, arousal) {
      const open = 2.2 + arousal * 4.2;            // eyelid opening
      const eyeY = -4;
      const dx = char.style === "orb" ? 15 : 10;
      for (const [eye, side] of [[char.eyeL, -1], [char.eyeR, 1]]) {
        eye.clear();
        if (arousal < 0.08) {
          // Nearly asleep: a closed lid line reads better than a tiny dot.
          eye.roundRect(snap(side * dx - 5), snap(eyeY), 10, 2, 1).fill(0xd1d4dc);
        } else {
          eye.ellipse(snap(side * dx), snap(eyeY), 3.4, open / 2).fill(0xd1d4dc);
        }
      }
    }

    function drawEyes(char, arousal) {
      if (char.faceKind === "machine") machineEyes(char, arousal);
      else humanEyes(char, arousal);
    }

    function humanFace(char, valence, arousal) {
      const eyeY = -4;
      const dx = char.style === "orb" ? 15 : 10;
      humanEyes(char, arousal);
      for (const [brow, side] of [[char.browL, -1], [char.browR, 1]]) {
        brow.clear();
        // Worry raises the INNER brow (y grows downward). Positive moods stay
        // level — angling them the other way reads as anger, which is how the
        // first pass made every happy face look stern.
        const worry = valence < 0 ? -valence : 0;
        const lift = arousal * 3;
        const inner = eyeY - 9 - lift - worry * 4;
        const outer = eyeY - 9 - lift + worry * 3;
        brow.moveTo(snap(side * (dx - 7)), snap(inner))
            .lineTo(snap(side * (dx + 7)), snap(outer))
            .stroke({ width: 2, color: 0x9aa4b2 });
      }
      char.mouth.clear();
      const curve = valence * 7;
      const width = 9 + arousal * 3;
      char.mouth
        .moveTo(snap(-width), snap(10))
        .quadraticCurveTo(snap(0), snap(10 + curve), snap(width), snap(10))
        .stroke({ width: 2.5, color: 0xd1d4dc });
    }

    // A mood, applied to every volume the figure is made of.
    //
    // This used to be three container-tint assignments — `char.body.tint =
    // tint` and friends. A container tint is a shader multiply over pixels
    // that have ALREADY been drawn, so once `paint()` started mixing the lit
    // band toward `LIGHT.warmth` and the shade band toward `LIGHT.ambient`,
    // the tint multiplied the room's own lamp along with the body. Every mood
    // came out as the same figure under a differently coloured light, which is
    // precisely why the gallery read as "the same figure, but red".
    //
    // Now the mood goes in as `paint()`'s own colour argument — the seam Task 6
    // built and, until this ticket, only ever handed `BODY_FILL`. The lamp stays
    // amber and the room stays cold in every mood; what changes is the colour
    // they fall on. Nothing here writes `.tint` any more, on purpose.
    // Takes the MOOD, not two colours. Both surfaces are derived here, in one
    // place, because a call site that can pass a fill and forget a rim is a
    // call site that eventually does: the first frame of this ticket shipped
    // exactly that — bodies correctly re-lit, every silhouette outlined in the
    // lamp's cream — and it was invisible to a test that drove this function
    // with both colours already in hand. There is no argument left to omit.
    //
    // Both surfaces move together on the accents too: an arm's `shadeFactor`
    // lightened its fill AND its rim before, because the container tint it was
    // handed had already been lightened and was multiplied over both.
    // Lightening only the fill leaves a bright arm inside a rim struck for a
    // darker body.
    function applyMood(char, mood) {
      const fill = moodFill(mood);
      const rim = moodRim(mood);
      repaint(char.body, fill, rim);
      if (char.skull) repaint(char.skull, fill, rim);
      for (const accent of char.accents) {
        const factor = accent.shadeFactor;
        repaint(
          accent,
          factor ? lightenTint(fill, factor) : fill,
          factor ? lightenTint(rim, factor) : rim,
        );
      }
    }

export { character, gesture, restAccents, setExpression, drawEyes };
