// /world — The palette and the two figures' anatomy: mood colours, body tint/fill/rim,
// HUMAN and MACHINE, the model badge.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import { WORLD_CONFIG } from "./config.js";

    // The mood -> colour map is injected from world/visuals.MOOD_COLORS (hex
    // strings) so this canvas and the DOM overlays share ONE palette; drift
    // between them was the old failure. It is IDENTITY colour — chrome, the
    // nowband's mood cell. Nothing drawn in the room reads it: bodies and the
    // tube pillars take `moodFill` below (KI-062 retired the pillars' own
    // raw lookup here).
    const MOOD_COLOR = WORLD_CONFIG.mood_colors;
    const NEUTRAL = WORLD_CONFIG.neutral_color;
    // The same grey after `visuals.lift_to_silhouette_floor` — what an
    // un-mooded figure is PAINTED in. Identity colour and body colour are
    // different jobs and only one of them has a contrast floor (KI-060).
    const NEUTRAL_TINT = WORLD_CONFIG.neutral_tint;

    // KI-028: the cast measured 1.19:1 (trader) and 1.00:1 against its own
    // background, versus WCAG's 3:1 floor — the bodies were the same luminance
    // as the room and only the faces read. A body's colour is NOT its mood
    // colour: PixiJS tint multiplies, so what renders is BODY_FILL x tint. The
    // old code floored the luminance of the *tint*, which is not the number a
    // viewer sees, and with the previous fill of 0x545862 the brightest body
    // obtainable — a pure white tint — was still only 2.51:1. The ceiling was
    // below the floor, so no tint could have fixed it; the fill had to move.
    // Both now come from world/visuals.py, which does the contrast maths and
    // is test-asserted over every mood.
    const BODY_TINT = WORLD_CONFIG.body_tints;
    const BODY_FILL = WORLD_CONFIG.body_base_fill;
    // A lit edge, drawn brighter than the fill so the difference survives being
    // tinted by the same multiplier. Inner alignment so the rim costs the
    // silhouette no width — shadow sizes and head placement are tuned to these
    // exact rects. This is also the detail a 2200 kbps VBR encoder keeps best:
    // it spends its fewest bits on the near-black gradients that used to be the
    // only thing separating the cast from the room.
    const BODY_RIM = { width: 1.5, color: WORLD_CONFIG.body_rim_fill, alignment: 1 };

    // The trader's own palette, sampled off the pre-Sprint-16 reference
    // (`Docs/char-redesign-reference/REFERENCE-characters.jpeg` in the vault):
    // brown hair, warm skin, a grey-blue sweater, blue-grey jeans, dark shoes.
    //
    // These are authored colours and are NOT the mood — they are painted with
    // `paint(..., keepFill = true)`, so a mood change re-lights their rim and
    // leaves the person a person. This is the brief's "mood has to move to face
    // + posture (+ a rim light)" as a constant rather than a comment: the day
    // the trader stopped being one flat mood-coloured silhouette is the day it
    // needed a palette of its own.
    //
    // Deliberately NOT in world/visuals.py with the mood palette: nothing here
    // is contrast-lifted per mood (KI-028), because none of it varies with
    // mood. A sweater is grey-blue when the model is elated and when it is
    // panicked; what changes is the light on its edge.
    const HUMAN = {
      hair:    0x33261f,
      skin:    0xc08f68,
      sweater: 0x3f5a70,
      // The sweater's ribbed hem: darker than BOTH garments, because the
      // sweater and jeans are too close in value to draw a waist on their
      // own (increment 21). `test_the_sweater_ends_in_a_hem`.
      hem:     0x2b3d4e,
      // A fold in the sleeve (Sprint 17 #4) is a small keyline of its own:
      // a dark crease with a light lip on the side it rises from, the pair
      // the figure's edge already wears (dark keyline outside, light rim
      // inside, KI-058). The crease sits between the sweater and its hem in
      // value; near-black that wide reads as a barcode (increment 17).
      // `test_the_near_sleeve_carries_folds_inside_its_outline`.
      fold:    0x30455a,
      foldLip: 0x506d85,
      jeans:   0x34495c,
      // Floor < shoe < jeans (2026-09-24, reversing increment 16's "darker
      // than the floor"). The dark `#181a22` sat on the figure's own soft
      // shadow at ratio ~1.0 and the shins simply stopped. The reference
      // lights its shoe ~2.25x its floor and BLUER than it (`#233444`);
      // increment 16's refused `#2a2e3c` was a grey in the floor's own hue,
      // which is what read as a puddle. And not too light: the shoe has to
      // part from the SHIN as well as the floor — `#233246` did the floor
      // and merged into the jeans as a flared trouser leg; the darker, bluer
      // one won the A/B (`inc24-shoe-ab-3x.png`). Graded against the plate's
      // floor and the jeans by
      // `test_both_shoes_sit_between_the_floor_and_the_jeans`.
      shoes:   0x1f2a42,
      // The shadow the forearm casts on the thigh it lies on (2026-09-24):
      // the arm and the leg read as one mass with no edge between them.
      // Darker than the jeans it falls on, in their hue.
      // `test_the_forearm_casts_a_contact_shadow_on_the_thigh`.
      contact: 0x22303f,
      // The shadow between two fingers — the only place on this figure where
      // one volume has to read as two. Sampled off the reference rather than
      // picked: its far hand measures `#262127` between the fingers against
      // `#a67265` on the lit slab above them, a luminance ratio of 0.28. Held
      // against skin's own lit rung (`#ce975e`, L 0.63) that asks for L 0.18,
      // which was `#3a2a22`. Warm, not neutral: a crease in a hand is skin
      // the light did not reach, and a blue-black one reads as a crack.
      //
      // That ratio did not survive the frame. The reference's separations are
      // one or two pixels wide; ours are 2 units x the figure scale, and at
      // that width a near-black line three times over reads as a BARCODE
      // stamped on the hand. Lifted to a deep skin shadow — still clearly
      // darker than the slab, so the four fingers still count (A/B 1:1 and
      // 4x, `data/visual-qa/char-redesign/inc17-*`).
      crease:  0x6e4a34,
    };

    // The MODEL's palette, and it holds exactly ONE colour — which is the
    // point. The trader has skin and hair and denim, so he cannot be
    // recoloured and gave up carrying mood in his body (increment 8). A
    // machine can be recoloured, and the MODEL's mood is the LIVE one:
    // `react()` moods it from real model events, where `draw()` re-pins the
    // trader to "idle" on every update. So every plate stays on `BODY_FILL`
    // and only the cuts between them are pinned to a colour of their own.
    //
    // `seam` is computed, not picked: it is `lightenTint(BODY_FILL, 0.14)`
    // evaluated out (0xd0d0d0 -> 0x1d1d1d), which is the recess
    // `machineFace` already cuts for the visor — this page's own shipped
    // answer to "what does a hollow in this machine look like", rather than
    // a second opinion about it. Neutral rather than warm, deliberately, and
    // this is where the machine departs from `HUMAN.crease`: a crease in a
    // hand is skin the lamp did not reach and has to stay skin-coloured,
    // while a gap between two steel plates is not made of steel.
    //
    // It has to be a LITERAL rather than that call. `repaint` stores a
    // keepFill volume's authored colour and replays it, so a seam derived
    // from the mood at boot would freeze at the first mood's recess while
    // the plates around it went on changing colour.
    const MACHINE = {
      seam: 0x28303a,
      // The MODEL's head (increment 13): an oblique box. `x/y/w/h` is the
      // FRONT face, the one `machineFace`'s visor, eyes and mouth are drawn
      // onto — they were authored against the 56-unit ball this replaces, so
      // the face is 56 across and reaches y +24 to clear the mouth's lowest
      // step (`snap(12 + 6)` = 20, plus its stroke). `depth` is how far the
      // top and the near side show, up and to the LEFT, which is the
      // reference's own direction (`ref-model.png`, same room, same lamp).
      head: { x: -28, y: -28, w: 56, h: 52, depth: 8, radius: 4 },
      // Its own name, stencilled on (user, 2026-09-24) — it replaced the
      // MODEL/TRADER nameplates the room used to float on its furniture.
      // On the PELVIS, the one plate no seam crosses: the chassis is split
      // down the middle at x 0. Centre and glyph size in rig units.
      // `test_the_model_wears_its_version_on_a_clean_plate`.
      // v0.2 (2026-09-27, `0f82ba1`): the 15m model trained on every bar
      // since 2020, rolled to now and confidence-gated, replaced v0.1 (1m).
      badge: { text: "model v0.2", x: 0, y: -58, size: 8 },
    };

    // The badge is a SIBLING of `body` (a Pixi 8 Graphics is a leaf), so it
    // copies the body's transform every frame: breathe, lean and shift all
    // write `body` directly, and a badge parked at rest slides off its plate
    // on the first breath. Dark seam colour, so it reads on every mood fill.
    function modelBadge(body) {
      const b = MACHINE.badge;
      const badge = new PIXI.Container();
      const text = new PIXI.Text({ text: b.text, resolution: 4, style: {
        fill: MACHINE.seam, fontFamily: "monospace", fontSize: b.size, fontWeight: "bold",
      }});
      text.anchor.set(0.5);
      text.x = b.x; text.y = b.y;
      badge.addChild(text);
      badge.onRender = () => {
        badge.x = body.x; badge.y = body.y; badge.rotation = body.rotation;
      };
      return badge;
    }

export {
  MOOD_COLOR, NEUTRAL, NEUTRAL_TINT, BODY_TINT, BODY_FILL, BODY_RIM, HUMAN,
  MACHINE, modelBadge,
};
