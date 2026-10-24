// /world — Lighting and shading: hex mixing, the light ramp, the key axis, contact
// light, banded volumes (paint/rim), the lit* shape builders, mood tint/fill/rim.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import { NEUTRAL_TINT, BODY_TINT, BODY_FILL, BODY_RIM } from "./palette.js";
import { LIGHT } from "./config.js";
import { snap } from "./tiers.js";

    // --- KI-051: light the cast with the room's own lamp -------------------
    // Every volume used to be one flat fill and one hard rim — the figures
    // read as stickers laid over the painting rather than objects standing
    // in it. `shade`/`paint` below replace the fill rule only: the shapes,
    // positions and animations they are called from are untouched.
    //
    // `function` declarations (not `const … = () =>`), deliberately: they
    // are hoisted, so `BODY_RIM_SHADED` below can call `shade()` regardless
    // of source order, and no later reordering of this block can reintroduce
    // a temporal-dead-zone `ReferenceError` — the failure mode that a
    // `boot().catch()` swallows into a silent reload loop (KI seen in Task 2
    // of this sprint), invisible to every test that only inspects page
    // source rather than running it.

    // Two colours, blended toward `b` by `t` in [0, 1]. Channel-wise, not a
    // library: this file mixes hex nowhere else.
    //
    // Guards its OWN parsed inputs, not its output: `<<`/`|` coerce a NaN
    // operand to 0 (`NaN << 16` is `0`, not `NaN`), so a bad colour does NOT
    // make this return a non-finite number — it silently returns a wrong but
    // perfectly finite one (black, in the simplest case). A `Number.isFinite`
    // check on the RESULT can never see that; it has to happen here, before
    // the bitwise math ever runs. A colour that fails to parse degrades to
    // the OTHER colour, unmixed — "no light contribution from the broken
    // side" rather than a wrong colour nobody asked for.
    function mixHex(a, b, t) {
      const pa = typeof a === "number" ? a : parseInt(String(a).slice(1), 16);
      const pb = typeof b === "number" ? b : parseInt(String(b).slice(1), 16);
      if (!Number.isFinite(pa)) return pb;
      if (!Number.isFinite(pb)) return pa;
      const ch = (v, sh) => (v >> sh) & 0xff;
      const lerp = (x, y) => Math.round(x + (y - x) * t);
      return (lerp(ch(pa, 16), ch(pb, 16)) << 16)
           | (lerp(ch(pa, 8), ch(pb, 8)) << 8)
           |  lerp(ch(pa, 0), ch(pb, 0));
    }

    // PixiJS's container tint, as arithmetic instead of a shader stage.
    //
    // The page used to let the GPU do this: `char.body.tint = tint` multiplied
    // the mood over every pixel the container had already drawn — which, once
    // `paint()` existed, meant it multiplied the mood over the room's LIGHT as
    // well as over the body. The lit band mixes toward `LIGHT.warmth` and the
    // shade band toward `LIGHT.ambient`, so a red mood did not put a red object
    // under the amber lamp; it turned the lamp red. Doing the multiply HERE, on
    // one colour, before any light is applied, is the whole of the change: the
    // product is the body's own colour, and `shade()` then lights it with the
    // room's lamp exactly as it lights every other volume.
    //
    // Guards its own inputs for the same reason `mixHex` does, and to the same
    // rule: `<<`/`|` coerce NaN to 0, so an unparseable colour would come back
    // finite and wrong (black) rather than detectably broken. A side that fails
    // to parse degrades to the other side unmultiplied — "no contribution from
    // the broken side", not a body nobody asked for.
    function mulHex(a, b) {
      const pa = typeof a === "number" ? a : parseInt(String(a).slice(1), 16);
      const pb = typeof b === "number" ? b : parseInt(String(b).slice(1), 16);
      if (!Number.isFinite(pa)) return pb;
      if (!Number.isFinite(pb)) return pa;
      const ch = (v, sh) => (v >> sh) & 0xff;
      const mul = (x, y) => Math.round((x * y) / 255);
      return (mul(ch(pa, 16), ch(pb, 16)) << 16)
           | (mul(ch(pa, 8), ch(pb, 8)) << 8)
           |  mul(ch(pa, 0), ch(pb, 0));
    }

    // A volume's colour at a given light level. `level` is the ramp value for
    // the band being drawn: positive faces the lamp, negative faces away.
    //
    // Lit mixes toward the lamp's OWN warmth and shade toward the room's
    // ambient, rather than toward white and black — a figure that is lit BY
    // THIS ROOM puts amber on one side and the room's cold ambient on the
    // other, the same thing every painted object on the plate already does.
    // `LIGHT` always reaches this page as a complete, validated model
    // (`light_for` never lets a malformed manifest through — see the comment
    // above `const LIGHT`), so this needs no input guard of its own; a
    // non-finite `level` (the one field a caller could still pass wrong) is
    // caught below before it reaches `mixHex`, and a non-finite `LIGHT.warmth`
    // /`LIGHT.ambient` is caught inside `mixHex` itself. Either way the
    // volume draws in its old flat, unshaded colour rather than `g.fill(...)`
    // throwing mid-`character()` and taking the rest of the cast with it —
    // the one path this sprint's "never a blank canvas" rule reaches inside a
    // single figure.
    function shade(baseHex, level) {
      if (!Number.isFinite(level)) return baseHex;
      const toward = level >= 0 ? LIGHT.warmth : LIGHT.ambient;
      return mixHex(baseHex, toward, Math.min(1, Math.abs(level)));
    }

    // The ONE way this file reads the ramp. `LIGHT.ramp.lit` written inline is
    // a `TypeError` the moment `LIGHT` arrives without a `ramp` — and one of
    // those inline reads sits at TOP LEVEL (`BODY_RIM_SHADED` below), which
    // throws before `boot()` is ever called and leaves the page blank with
    // every page-source test still green (the Task 2 failure mode). The inner
    // reads inside `paint()` are the softer version of the same class, so
    // both go through here rather than only the loud one: a missing or
    // non-numeric rung degrades to 0, which `shade()` turns back into the
    // volume's own flat colour — the pre-KI-051 look, not a broken page.
    function rampLevel(rung) {
      const ramp = LIGHT && LIGHT.ramp;
      const level = ramp ? ramp[rung] : undefined;
      return Number.isFinite(level) ? level : 0;
    }

    // The measured direction, normalised, and safe to divide by. Not read raw
    // from `LIGHT.direction` at each draw call, for two reasons: `litCircle`'s
    // no-overshoot property holds EXACTLY only for a unit vector (this plate
    // measures |d| = 1.004, which walks each band's key-facing point outward
    // by 0.4% of its own inset), and a `[0, 0]` direction — which `light_for`
    // accepts, since it validates the ramp and not the vector — would put NaN
    // into every roundRect on the page. Anything unusable falls back to
    // DEFAULT_LIGHT's straight-down axis rather than throwing.
    function keyAxis() {
      const raw = (LIGHT && LIGHT.direction) || [];
      const dx = Number(raw[0]), dy = Number(raw[1]);
      const len = Math.hypot(dx, dy);
      return Number.isFinite(len) && len > 0 ? [dx / len, dy / len] : [0, 1];
    }
    const KEY_AXIS = keyAxis();

    // The measured contact patch — how dark a shadow this room casts, how wide
    // it sits under a body, and how far the lamp's height throws it. Guarded
    // for the same reason `rampLevel` and `keyAxis` above are: `LIGHT.contact
    // .opacity` written inline is a TypeError the moment `LIGHT` arrives
    // without a `contact`, and THIS one is read at top level (`CONTACT`
    // below), which is the flavour that throws before `boot()` is ever called
    // and leaves a blank page every page-source test still passes.
    //
    // The fallback numbers here are deliberately NOT a copy of
    // `DEFAULT_LIGHT`'s: `light_for` always supplies a complete `contact`
    // block, including for a missing or malformed manifest, so what an
    // unmeasured room's shadow looks like is decided in `world/light.py` and
    // nowhere else. What is left here is the structural degrade — no throw,
    // no NaN reaching a sprite — for a state that contract cannot produce.
    function contactLight() {
      const c = (LIGHT && LIGHT.contact) || {};
      const num = (v, dflt) => (Number.isFinite(Number(v)) ? Number(v) : dflt);
      return {
        opacity: Math.min(1, Math.max(0, num(c.opacity, 0))),
        widthScale: Math.max(0, num(c.widthScale, 1)),
        throwScale: Math.max(0, num(c.throwScale, 0)),
      };
    }
    const CONTACT = contactLight();

    // How far this room lays its shadows down, in plate pixels:
    //
    //     throw_px = CONTACT.throwScale * SHADOW_THROW
    //
    // `throwScale` is the plate's measured share, dimensionless and beside
    // `widthScale` for that reason; this constant carries the pixels. It was
    // called `height` until KI-063, which was wrong twice over — not a length,
    // and backwards as physics, since RAISING a lamp shortens a shadow. It is
    // the one number in the shadow that is an art
    // call rather than a measurement, and kept out of the manifest for that
    // reason — a repaint re-measures the lamp, it does not re-decide how far
    // a shadow should read as being thrown. Chosen from frames, not from
    // arithmetic: on a three-point sweep measured off real pixels — each
    // frame's shadow differenced against the painted plate — `1.6` moved the
    // model's centroid only 6px across a 78px patch and still read as a
    // lopsided puddle, `4.5` ran the far tail 41px past the body and detached
    // the mass into a smear, and `3.0` is unmistakably directional with the
    // core still under the feet.
    const SHADOW_THROW = 3.0;

    // How much of a volume each band eats, as a FRACTION of that volume's own
    // extent — never as a fixed number of units.
    //
    // Review round 1's ruling, measured rather than argued: the first version
    // used absolute insets of 0/2/5 against volumes spanning 12 to 66 local
    // units, so a 15-unit bar got a ~1px shade band and a ~4px base band and
    // the remaining ~85% of EVERY figure came out one flat colour. That is a
    // tint, not light — the exact thing KI-051 names as the defect. Raising
    // the absolute numbers cannot fix it either: what shades a torso would
    // collapse or invert a wick.
    //
    // Measured at these values over every volume the page draws — the 66x54
    // chest, the 12x96 bar, the 7x30 wick, the r28 skull, the r46 orb — each
    // one comes out 25-27% lit, 39-43% mid, 30-35% shade by area. One
    // three-tone form shade, the same on the biggest volume and the smallest,
    // which is the whole point of expressing the offsets as fractions. The
    // VALUES are an art call made off a frame, not a derivation; the SHAPE of
    // the rule (proportional, clamped) is the load-bearing part.
    const BAND_INSET = { shade: 0, base: 0.25, lit: 0.70 };
    // No band may eat more than this share of its volume, whatever the
    // fractions above or a future manifest's direction say. A `roundRect` of
    // negative width is a silent geometry failure, not an exception — nothing
    // throws, the volume just stops being drawn — so the clamp is what keeps a
    // retune from quietly deleting the smallest bodies.
    const BAND_MAX = 0.85;

    // The same band, for a circle. A rect loses one linear fraction per axis;
    // a circle shrinks on BOTH axes at once, so the identical fraction costs
    // it far more of itself — at `lit` a rect keeps ~25% of its area and a
    // circle only 9%. Left uncorrected that is a skull visibly darker than the
    // chest under it: head and body disagreeing about the light, which is the
    // the same defect class as the sign error review round 1 shipped. So:
    // solve for the linear inset leaving a circle the AREA share a rect keeps
    // — rect keeps `(1 - band·|dx|)(1 - band·|dy|)`, circle keeps `(1 - f)²`.
    // Derived from the same measured axis rather than hardcoded, so a repaint
    // that moves the lamp keeps the head and the body agreeing.
    function circleBand(band) {
      if (!Number.isFinite(band) || band <= 0) return 0;
      const kept = (1 - band * Math.abs(KEY_AXIS[0]))
                 * (1 - band * Math.abs(KEY_AXIS[1]));
      return 1 - Math.sqrt(Math.max(0, kept));
    }

    // A rim colour shaded toward the lamp's own warmth rather than a fixed
    // hue. It reads as a bright contour where it sits against the volume's
    // own shaded (away-from-key) band — the KI-028 contrast the plain
    // BODY_RIM existed for, now needed most on the side `paint` leaves
    // darkest.
    const BODY_RIM_SHADED = { width: BODY_RIM.width, color: shade(BODY_RIM.color, rampLevel("lit")), alignment: BODY_RIM.alignment };

    // Draw one volume in three bands, lit toward the key. A `volume` is what
    // `litRect`/`litCircle` return: `band(g, inset)` draws the SAME silhouette
    // shrunk to a given band, `rim(g)` strokes its true (un-inset) outline.
    // `paint` supplies the insets and, via `shade`, the colour of each band,
    // so the shape a caller passes in is unchanged — this adds light to the
    // existing rig, it does not redraw it.
    //
    // The brief asks for a rim that "survives only away from the key", and the
    // mechanism for it — draw the rim, then let the lit band overpaint it on
    // the lamp side — does not work here, measured rather than assumed:
    // `BODY_RIM.alignment` is 1, which in PixiJS v8 strokes OUTSIDE the shape
    // (scanned off the delivered frame: on 96 rows through the trader, a rim
    // pixel sat outside the outermost band pixel 192 times and strictly
    // inside it 0). Nothing drawn inside the silhouette can occlude a stroke
    // drawn outside it, and the alternative — moving the rim to an inner
    // alignment — shrinks the very silhouette KI-028's contrast fix was
    // measured against. So the rim stays whole and goes on last, and the
    // measurement lives in the task report instead of an aesthetic claim.
    //
    // Each volume is REMEMBERED on the Graphics it was drawn into, so the same
    // rig can be re-lit in another colour without the call site having to hand
    // its rects over a second time. That is what makes mood a colour `paint()`
    // receives rather than a multiply laid over its output (`repaint` below) —
    // and it is why the nineteen `BODIES.*`/`seatedRig`/`character()` call
    // sites are untouched by this ticket: they still describe the shape once,
    // in `BODY_FILL`, and the mood arrives afterwards.
    // `rim` is the volume's EDGE colour, and it is a second base colour rather
    // than a decoration derived from the first. `world/visuals.py` states the
    // invariant this ticket has to carry: "the rim is drawn brighter than the
    // fill so the difference SURVIVES tinting (both get multiplied by the same
    // tint, so the ratio between them is fixed here and nowhere else)". The
    // container tint used to be what kept that ratio; with the tint gone the
    // mood has to reach the rim's own albedo the same way it reaches the
    // body's, or every figure is outlined in the lamp's cream regardless of
    // what colour it is. Measured on the first frame of this ticket: 11,491
    // pixels of flat `#fbe5c8` keyline, and a 14-unit arm at 1.5px of rim lost
    // its colour almost entirely — a die-cut sticker outline, which is the
    // KI-051 read this whole sprint exists to remove.
    //
    // Omitted, it falls back to `BODY_RIM_SHADED`: the un-mooded edge, which
    // is what the nineteen construction-time call sites want and what a
    // volume drawn outside a character gets.
    // `keepFill` is the human's exemption, and it exists because a person is
    // not one colour. Every volume drawn before the character redesign took
    // the mood as its fill, which is correct for a market glyph and impossible
    // for skin, hair and denim — recolouring those wholesale stops the figure
    // being a person at all. A volume painted with `keepFill` records its own
    // authored colour and keeps it through every `repaint`; what still moves
    // with the mood is its RIM, the lit edge. That is the brief's resolution
    // ("mood has to move to face + posture + a rim light") landing on the seam
    // `paint` already had rather than a second painting path beside it.
    //
    // Default false, so all nineteen pre-existing call sites and every market
    // glyph keep the old behaviour exactly.
    // `opts.noRim` is the second half of the same lesson, and it is the one the
    // first frame of the redesign taught. A rim belongs to a SILHOUETTE, not to
    // every volume that happens to be drawn. While a body was four rectangles
    // that was the same statement; at twenty it stops being one — each interior
    // part came back die-cut in the lamp's cream, which is precisely the
    // "die-cut sticker outline" this file's own `paint` comment names as the
    // KI-051 read to remove. Detail made the figure WORSE, monotonically, until
    // interior volumes could opt out of the edge.
    //
    // So: volumes on the outline keep their rim; volumes that sit inside
    // another (a face inside its hair, a shin between a thigh and a shoe) pass
    // `noRim` and read as shading instead of as separate objects.
    //
    // ...and then the rim came off the PERSON entirely, which is the end of
    // that road rather than a break with it. `noRim` is per-VOLUME, and the
    // defect it could never reach was per-END: the thigh is 4.4 units wider
    // than the shin, so its rim overhung the knee whatever the shin did. The
    // only per-volume cover for that was a disc over the joint, and `paint`
    // bands a circle concentrically, so the disc read as a BALL joint and the
    // trader became a wooden mannequin.
    //
    // A `keepFill` volume gets no rim at all. That is not a new predicate:
    // `keepFill` is this file's own definition of a volume of a PERSON (see
    // above — "a person is not one colour"), and the rule is that a person is
    // not outlined. The machine heads take the other branch (the ball,
    // `paint(skull, litCircle(0, 0, 28), BODY_FILL)`, and the MODEL's box,
    // `boxHead`) and keep their rim, as
    // every market glyph does: a glyph is a shape that needs an edge against a
    // busy room; a face is not.
    //
    // Measured before it was believed (vault `Docs/character-redesign.md`,
    // increment 8). The rim was the human's only COLOUR mood channel, and that
    // channel is CONSTANT in the shipped configuration — `react()` sends a
    // mood to the trader only from `trader_*` events, which exist only when
    // the opt-in freqtrade sidecar runs, and `draw(state)` re-pins him to
    // "idle" every update. What the rim delivered was the LAMP and not the
    // mood: `rimStyle` mixes every rim 30% toward `LIGHT.warmth`, so the
    // trader's edge measured hue 26° / sat 0.16 regardless, against authored
    // tints of sat 0.31–1.00. Mood still reaches him through the other two of
    // the brief's three channels, face and posture.
    //
    // At 1:1 viewer scale the unrimmed figure reads as a painted person and
    // the rimmed one as an assembly of outlined tubes. The lit/shade bands
    // were always doing the separating; the rim competed with them.
    // `test_a_person_is_not_outlined` grades this at the mechanism, because
    // the one thing increment 6 proved is that a rule restated at a call site
    // is a rule the next limb will get wrong.
    function paint(g, volume, fill, rim, opts) {
      const keepFill = opts && opts.keepFill;
      const noRim = !!(opts && opts.noRim);
      // A matte volume takes the BASE rung where the lit one would go, rather
      // than skipping the band: the shoe still needs its brightest face
      // painted, it just must not be painted in the lamp's hue. See `shod`.
      const matte = !!(opts && opts.matte);
      // Anything added here needs a line in `repaint` too — it rebuilds these
      // options field by field, so it is a WHITELIST, and an option it does
      // not name is dropped the moment a mood is applied. `matte` was lost
      // that way when first written: the A/B shot came back pixel-identical
      // to the shipped figure, because `draw()` re-pins the trader to "idle"
      // on every update and repaints him before anything is photographed.
      //
      // `matte` is only meaningful ALONGSIDE `keepFill`, which is how `shod`
      // carries it. `repaint` recovers a volume's authored colour from the
      // stored `fill`, and that is stored only for a `keepFill` volume — so a
      // matte-only volume would keep the rung and take the MOOD as its fill on
      // the next repaint. Nothing draws one; this is the note for whoever adds
      // the next option, not a live defect.
      // A `shadowed` volume sits out of the lamp: every rung drops one shade
      // step, so it keeps its colour and its warmth but loses its brightness.
      // The hand in the lap (2026-09-24) — skin's shade rung IS the
      // reference's hand, and a matte hand is a grey glove.
      const shadowed = !!(opts && opts.shadowed);
      const down = shadowed ? rampLevel("shade") : 0;
      (g.volumes || (g.volumes = [])).push(
        keepFill || noRim || matte || shadowed
          ? { volume, fill: keepFill ? fill : undefined, noRim, matte, shadowed }
          : volume,
      );
      volume.band(g, BAND_INSET.shade);  g.fill(shade(fill, rampLevel("shade") + down));
      volume.band(g, BAND_INSET.base);   g.fill(shade(fill, rampLevel("base") + down));
      volume.band(g, BAND_INSET.lit);
      g.fill(shade(fill, (matte ? rampLevel("base") : rampLevel("lit")) + down));
      if (!noRim && !keepFill) volume.rim(g, rimStyle(rim));
    }

    // A rim's stroke style at a given base colour, lit like the volume's own
    // lamp-facing band — the rim IS the lit edge, so it takes `rampLevel("lit")`
    // and nothing else. `BODY_RIM_SHADED` (the same computation over the
    // un-mooded `BODY_RIM.color`) is what a caller with no colour to offer
    // gets, so the constant stays the single definition of the default edge.
    function rimStyle(rim) {
      if (!Number.isFinite(rim)) return BODY_RIM_SHADED;
      return {
        width: BODY_RIM.width,
        color: shade(rim, rampLevel("lit")),
        alignment: BODY_RIM.alignment,
      };
    }

    // The same volumes, lit again in a new colour.
    //
    // `g.clear()` first, so the bands REPLACE the old ones rather than stacking
    // a second opaque set on top of them — and the recorded list is emptied
    // before the re-paint, because `paint` re-records as it goes and an
    // unreset list would double on every mood change for as long as the stream
    // is up. Nothing here touches the Graphics' transform, which is where
    // every animation's state lives (`scale`, `rotation`, `x`/`y`): a mood
    // change mid-gesture re-colours the figure without interrupting it.
    //
    // A Graphics with nothing recorded is left exactly as it is. That is not
    // only defensive: faces (eyes, brows, mouth, visor) are drawn by
    // `setExpression`'s own primitives, never through `paint`, and clearing
    // one of those here would wipe a face and draw nothing back.
    // A `keepFill` volume was recorded as `{ volume, fill }` rather than bare,
    // so it re-paints in the colour it was authored in and takes only the
    // mood's rim. Everything else is a bare volume and re-paints in the mood,
    // exactly as before.
    function repaint(g, fill, rim) {
      // A limb can be a TREE now — the seated forearm is a child of its own
      // upper arm, so that a shoulder swing carries it. `applyMood` walks
      // `char.accents`, which holds the upper arm only, so without this
      // descent a mood change would re-light the sleeve above the elbow and
      // leave the one below it in the previous mood's colour. Silent, and
      // visible only on a mood change, which is exactly the kind of seam this
      // redesign keeps finding.
      for (const child of g.children || []) repaint(child, fill, rim);
      const volumes = g.volumes;
      if (!volumes || !volumes.length) return;
      g.clear();
      g.volumes = [];
      for (const entry of volumes) {
        if (entry.volume) {
          const authored = entry.fill !== undefined;
          paint(g, entry.volume, authored ? entry.fill : fill, rim, {
            keepFill: authored, noRim: entry.noRim, matte: entry.matte,
            shadowed: entry.shadowed,
          });
        } else {
          paint(g, entry, fill, rim);
        }
      }
    }

    // KI-073 — the rule `repaint`'s early return implies, stated where it can
    // be checked: a Graphics in an accent's tree that has DRAWN something is
    // mood-coloured only if it drew through `paint()`. One drawn the old way
    // (`new PIXI.Graphics().roundRect(...).fill(BODY_FILL)`) is animated like
    // any accent and stays `BODY_FILL` grey through every mood, silently —
    // the container tint used to colour it, `repaint` never will. Returns the
    // offenders as paths (`"accents[2].children[0]"`) so the message names
    // which part to fix. Faces are not accents, so they never reach here.
    function moodBlindParts(accents) {
      const blind = [];
      const walk = (g, where) => {
        const drew = g.context && g.context.instructions
          && g.context.instructions.length > 0;
        if (drew && !(g.volumes && g.volumes.length)) blind.push(where);
        (g.children || []).forEach((c, i) => walk(c, `${where}.children[${i}]`));
      };
      accents.forEach((g, i) => walk(g, `accents[${i}]`));
      return blind;
    }

    // How much of a volume's own extent a band keeps on one axis. `band` is
    // the fraction from `BAND_INSET`; how many UNITS that costs is a property
    // of this volume and this axis, so it is computed where the size is known
    // rather than handed down as a fixed number of pixels. Weighting by the
    // key's own component on the axis is what makes an overhead lamp band a
    // shape across its height and leave its width alone, and a side lamp do
    // the reverse — a light straight down the y axis produces full-width
    // horizontal bands, which is the correct picture and also exactly what the
    // neutral DEFAULT_LIGHT degrades to.
    function bandExtent(size, axisComponent, band) {
      const fraction = Number.isFinite(band) ? Math.max(0, band) : 0;
      return size * (1 - Math.min(fraction * Math.abs(axisComponent), BAND_MAX));
    }

    // Can a `bw × bh` band with corner radius `rb`, sitting `cx, cy` from the
    // volume's own centre, be drawn without any part of it leaving a `w × h`
    // rounded rect of radius `r`?
    //
    // Exact, not a margin heuristic — and the check the FIRST version of this
    // rule was missing. Bands placed flush against the volume's key-facing
    // edges nest correctly in the BOUNDING BOX and still leak out through the
    // rounded corner, because a small band's corner is squarer than the
    // silhouette's: rasterised against the real call sites, the seated chest
    // (48×40, r20 — a stadium) spilled 15.3% of its lit band outside its own
    // outline, which is a figure whose shape changes when the lamp moves.
    //
    // The statement below is the whole of it: a rounded rect contains a
    // rounded rect exactly when each of the inner shape's four corner DISCS
    // lies inside the outer shape eroded by that disc's radius — and the
    // erosion of a rounded rect by a disc is the same rounded rect with both
    // dimensions and the radius reduced by it.
    function bandInside(cx, cy, w, h, r, bw, bh, rb) {
      if (Math.abs(cx) > (w - bw) / 2) return false;
      if (Math.abs(cy) > (h - bh) / 2) return false;
      const room = r - rb;
      if (room <= 0) return true;
      const ax = bw / 2 - rb, ay = bh / 2 - rb;   // the band's own arc centres
      const hx = w / 2 - r, hy = h / 2 - r;       // the volume's arc centres
      for (const sx of [-1, 1]) {
        for (const sy of [-1, 1]) {
          const dx = Math.max(0, Math.abs(cx + sx * ax) - hx);
          const dy = Math.max(0, Math.abs(cy + sy * ay) - hy);
          if (dx * dx + dy * dy > room * room) return false;
        }
      }
      return true;
    }

    // Where a band sits: as far toward the lamp as this volume allows, as an
    // offset from the volume's own centre.
    //
    // The direction is the key axis scaled by how much ROOM each axis has
    // (half the difference between the volume and its band). Walking the raw
    // key vector instead is wrong for anything long and thin, and wrong in a
    // way the frame shows immediately: a 12x96 bar has 2.3 units of
    // horizontal room and 28 of vertical, so a straight diagonal walk runs out
    // of width after 4 units and the band ends up vertically CENTRED — a bar
    // lit from above with its bright part in the middle. Scaling by the room
    // turns that into an almost-vertical walk, while a SQUARE volume (which
    // every circle is, 2R x 2R) still walks exactly along the key axis — which
    // is what keeps `litCircle`'s closed form below the same rule and not a
    // second one.
    //
    // The magnitude is bisected rather than solved: the feasible set is convex
    // (an erosion of one convex shape by another) and contains the centre, so
    // the feasible offsets along a ray are an interval from 0 and bisection
    // lands on its edge — which leaves `bandInside` as the only geometry that
    // has to be right. The centred band always fits, because `rb` is capped at
    // `min(r, bw/2, bh/2)`, which is exactly the condition that makes the
    // centred case pass; the search always has that floor.
    //
    // This is also the ONE place "toward the lamp" is expressed for a rect.
    // Review round 1 shipped a figure lit from two directions at once because
    // that phrase had two separate expressions — a per-axis sign branch for
    // rects and `-LIGHT.direction` for circles — and only one of them was
    // right. There is no sign branch left to get backwards.
    function slideToKey(w, h, r, bw, bh, rb) {
      const ux = -KEY_AXIS[0], uy = -KEY_AXIS[1];
      const scale = Math.max(Math.abs(ux), Math.abs(uy)) || 1;
      const tx = ux * ((w - bw) / 2) / scale;
      const ty = uy * ((h - bh) / 2) / scale;
      let lo = 0, hi = 1;
      for (let i = 0; i < 24; i++) {
        const mid = (lo + hi) / 2;
        if (bandInside(mid * tx, mid * ty, w, h, r, bw, bh, rb)) lo = mid;
        else hi = mid;
      }
      return [lo * tx, lo * ty];
    }

    // Band edges land on a 1/64 unit — a power of two, so they stay exact in
    // binary floating point against the whole (CELL-snapped) coordinates every
    // call site passes, and the final `Math.max`/`Math.min` below can then
    // pin a band inside its volume's box EXACTLY rather than to within 1e-15.
    // Task 5 measured the seated rig clearing the painted seat by 9px and the
    // backrest by 50px; those checks are worth being able to state without a
    // tolerance.
    const quant = (v) => Math.round(v * 64) / 64;
    // The floor is one quantum, not zero: `band()` must always leave a path
    // for the `g.fill()` that follows it, and a zero-area `roundRect` is a
    // silent no-op rather than an error.
    const MIN_BAND = 1 / 64;

    // A volume for `paint`: the same rounded rect shrunk to each band and slid
    // toward the lamp, plus the rim at its true silhouette. One definition
    // rather than restating the arithmetic at every one of the nineteen call
    // sites.
    const litRect = (x, y, w, h, r) => {
      return {
        band: (g, band) => {
          const x0 = snap(x), y0 = snap(y);
          const bw = bandExtent(w, KEY_AXIS[0], band);
          const bh = bandExtent(h, KEY_AXIS[1], band);
          // A corner cannot stay absolute while the rect shrinks: a radius
          // wider than the band it rounds is where a renderer starts guessing.
          const rb = Math.max(0, Math.min(r, bw / 2, bh / 2));
          const [ox, oy] = slideToKey(w, h, r, bw, bh, rb);
          const bx = Math.min(Math.max(x0, quant(x0 + (w - bw) / 2 + ox)), x0 + w);
          const by = Math.min(Math.max(y0, quant(y0 + (h - bh) / 2 + oy)), y0 + h);
          const bwq = Math.max(MIN_BAND, Math.min(quant(bw), x0 + w - bx));
          const bhq = Math.max(MIN_BAND, Math.min(quant(bh), y0 + h - by));
          return g.roundRect(bx, by, bwq, bhq, Math.min(rb, bwq / 2, bhq / 2));
        },
        rim: (g, style) => rimStroke(g, x, y, w, h, r, style),
      };
    };
    // A circle is the closed-form case of the same rule, not a second one:
    // `slideToKey(2R, 2R, R, 2rb, 2rb, rb)` is exactly `(R - rb)·-KEY_AXIS`,
    // the offset that leaves the band internally tangent to the original
    // circle at its key-facing point — which is why a square volume walking
    // along the key axis matters above. `tests/api/test_world_page.py` asserts
    // that identity rather than leaving it as a claim in a comment. Solving it
    // here keeps the skull exact and saves 24 bisection steps per band.
    const litCircle = (cx, cy, radius) => {
      return {
        band: (g, band) => {
          const kept = radius * (1 - Math.min(circleBand(band), BAND_MAX));
          const rb = Math.max(MIN_BAND, quant(kept));
          const slide = radius - rb;
          return g.circle(
            snap(cx) + slide * -KEY_AXIS[0],
            snap(cy) + slide * -KEY_AXIS[1],
            rb,
          );
        },
        rim: (g, style) => rimStrokeCircle(g, cx, cy, radius, style),
      };
    };

    // An upright CYLINDER — what fills the painted tubes (KI-062). `litRect`
    // is right for a torso and wrong here, measured on the first frame: on a
    // 54x340 column its lit band is a 30%-by-30% block slid toward the lamp,
    // which put a flat grey slab over the upper third of each tube. A
    // cylinder's surface turns only ACROSS its width, so its light varies only
    // across its width: every band is full height, shrunk horizontally by the
    // same `BAND_INSET` fraction and pushed to the lamp's side. The lamp's
    // horizontal component picks the side and nothing else — the normal of a
    // cylinder sweeps every horizontal direction, so how far the key leans
    // does not narrow the lit strip. A lamp straight overhead lights it
    // centred.
    const litColumn = (x, y, w, h) => {
      const side = Math.sign(-KEY_AXIS[0]);
      return {
        band: (g, band) => {
          const x0 = snap(x), y0 = snap(y);
          const bw = Math.max(MIN_BAND, quant(bandExtent(w, 1, band)));
          const bx = quant(x0 + (w - bw) / 2 + side * (w - bw) / 2);
          return g.rect(bx, y0, bw, h);
        },
        rim: (g, style) => rimStroke(g, x, y, w, h, 0, style),
      };
    };

    // An OUTLINE, for the parts of a person a rect cannot draw without
    // reading as a block: the face, the swept fringe, the hand (increment
    // 18 — "make him look natural without blocks and squares").
    //
    // The same rule as the other two, not a third one: each band is the
    // outline shrunk about its centroid by `circleBand`'s fraction and slid
    // toward the lamp as far as it stays inside. On a CONVEX outline that is
    // the whole story, because "every vertex inside" is the whole containment
    // check. A concave band would poke out through the concavity it was
    // shrunk past (the corner leak `bandInside` exists to stop on a rect), so
    // a CONCAVE outline (the splayed hand, Sprint 17 #7) is banded on its
    // convex hull and each band is then cut back to the outline
    // (`clipToConvex`): inside it by construction, notches included.
    // The concave details of a profile (nose, lips, the ear's C) are still
    // flat features, never paths.
    //
    // `points` are relative to an origin that snaps like `litRect`'s, so a
    // path on a limb keeps the whole-CELL registration of the rects cut into
    // it; the outline itself is unsnapped, which is what lets it be round.
    const litPath = (points, ox = 0, oy = 0) => {
      const x0 = snap(ox), y0 = snap(oy);
      const pts = points.map(([px, py]) => [x0 + px, y0 + py]);
      // Convex when every turn that is not collinear goes the same way. A
      // concave outline was briefly painted FLAT here (base rung, lit band
      // empty), which cost the splayed hand its light patch by the cuff; it
      // now takes its hull's bands, cut back to its own outline.
      const turns = pts.map((_, i) => {
        const [ax, ay] = pts[i], [bx, by] = pts[(i + 1) % pts.length];
        const [cx, cy] = pts[(i + 2) % pts.length];
        return (bx - ax) * (cy - by) - (by - ay) * (cx - bx);
      }).filter((t) => Math.abs(t) >= 1e-9);
      const convex = turns.every((t) => Math.sign(t) === Math.sign(turns[0]));
      const hull = convex ? pts : convexHull(pts);
      return {
        band: (g, band) => g.poly((convex ? pathBand(pts, band)
          : clipToConvex(pts, pathBand(hull, band))).flat()),
        rim: (g, style) => g.poly(pts.flat()).stroke(style || BODY_RIM_SHADED),
      };
    };

    // Is (px, py) inside (or on) the convex polygon `pts`, either winding?
    function insideConvex(pts, px, py) {
      let sign = 0;
      for (let i = 0; i < pts.length; i++) {
        const [ax, ay] = pts[i], [bx, by] = pts[(i + 1) % pts.length];
        const cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax);
        if (Math.abs(cross) < 1e-9) continue;
        const s = Math.sign(cross);
        if (sign && s !== sign) return false;
        sign = s;
      }
      return true;
    }

    function pathBand(pts, band) {
      const keep = 1 - Math.min(circleBand(band), BAND_MAX);
      const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length;
      const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
      const shrunk = pts.map(([px, py]) => [cx + (px - cx) * keep, cy + (py - cy) * keep]);
      const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
      const reachMax = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
      const fits = (t) => shrunk.every(([px, py]) =>
        insideConvex(pts, px - KEY_AXIS[0] * t, py - KEY_AXIS[1] * t));
      let lo = 0, hi = reachMax;
      for (let i = 0; i < 24; i++) {
        const mid = (lo + hi) / 2;
        if (fits(mid)) lo = mid; else hi = mid;
      }
      return shrunk.map(([px, py]) => [quant(px - KEY_AXIS[0] * lo), quant(py - KEY_AXIS[1] * lo)]);
    }

    // The convex hull of `pts` (Andrew's monotone chain). Its winding does
    // not matter: `pathBand` and `clipToConvex` take either.
    function convexHull(pts) {
      const sorted = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
      const chain = (list) => {
        const h = [];
        for (const p of list) {
          while (h.length >= 2 && cross(h[h.length - 2], h[h.length - 1], p) <= 0) h.pop();
          h.push(p);
        }
        h.pop();
        return h;
      };
      return [...chain(sorted), ...chain([...sorted].reverse())];
    }

    // `subject` cut down to the CONVEX polygon `clip`, either winding
    // (Sutherland-Hodgman). The subject may be concave, and the result never
    // leaves it, so a band built on a concave outline's hull and cut back by
    // this cannot paint a notch. Where the cut leaves the subject in pieces,
    // they come back joined by zero-width seams along the clip's edge,
    // which fill nothing. Quantised like every other band vertex.
    function clipToConvex(subject, clip) {
      const area = clip.reduce((a, [ax, ay], i) => {
        const [bx, by] = clip[(i + 1) % clip.length];
        return a + ax * by - bx * ay;
      }, 0);
      const side = Math.sign(area) || 1;
      let out = subject;
      for (let i = 0; i < clip.length && out.length; i++) {
        const [ax, ay] = clip[i], [bx, by] = clip[(i + 1) % clip.length];
        if (Math.abs(bx - ax) + Math.abs(by - ay) < 1e-9) continue;
        const at = ([px, py]) => side * ((bx - ax) * (py - ay) - (by - ay) * (px - ax));
        const input = out;
        out = [];
        for (let j = 0; j < input.length; j++) {
          const p = input[j], q = input[(j + 1) % input.length];
          const dp = at(p), dq = at(q);
          if (dp >= -1e-9) out.push(p);
          if ((dp >= -1e-9) !== (dq >= -1e-9)) {
            const t = dp / (dp - dq);
            out.push([p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t]);
          }
        }
      }
      // Nothing left (a clip off the subject): the empty band the flat
      // rule used, one point three times.
      if (out.length < 3) return [subject[0], subject[0], subject[0]];
      return out.map(([px, py]) => [quant(px), quant(py)]);
    }

    // The rim, kept at the volume's true (un-inset) silhouette so it still
    // outlines the shape `paint` was called with, not one of its bands.
    // Reached only through a volume's own `rim(g)`, never from a body method:
    // a rim is not something a call site can forget, mis-order, or hand the
    // wrong rectangle — `paint` owns the whole fill rule for a volume, which
    // is the one thing this ticket was allowed to change.
    function rimStroke(g, x, y, w, h, r, style) {
      g.roundRect(snap(x), snap(y), w, h, r).stroke(style || BODY_RIM_SHADED);
    }
    function rimStrokeCircle(g, cx, cy, radius, style) {
      g.circle(snap(cx), snap(cy), radius).stroke(style || BODY_RIM_SHADED);
    }
    // -------------------------------------------------------------------

    // The colour a body of this mood is PAINTED in — the thing `paint()` then
    // lights, not a multiplier laid over the result.
    //
    // `BODY_TINT` is the mood palette lifted to `SILHOUETTE_MIN_CONTRAST`
    // (KI-028), and it is still a *tint*: the contrast floor was measured in
    // `world/visuals.py` on `_rendered()`, "what the canvas actually shows: the
    // base fill multiplied by the tint". So the colour that clears the floor is
    // the PRODUCT, and that is what gets painted. Handing `paint()` the raw
    // `BODY_TINT` entry instead would ship every body 1/0.816 = 1.23x brighter
    // than the number `body_contrast()` asserts and a screenshot is compared
    // back to — a measured quantity moved silently, which is exactly the shape
    // of the defect KI-028 was.
    //
    // `MOOD_COLOR` is deliberately NOT what this reads. That map is the
    // identity palette for chrome — labels, the gallery's mood swatch — and it
    // is the pre-lift colour; reaching for it here is KI-028 reverted, which
    // sinks fourteen of the twenty-two moods back under the floor.
    //
    // Unknown moods fall back to NEUTRAL_TINT *inside* the multiply. `neutral`
    // is the live case, not a hypothetical: `character()` builds every figure
    // at mood "neutral", which `visuals.MOOD_COLORS` does not claim, and
    // returning the bare tint there would make un-mooded figures the brightest
    // thing in the room. NEUTRAL_TINT, not NEUTRAL: the identity grey is the
    // pre-lift colour, and painting a body in it lands at 3.05:1 — the only
    // colour in the room under KI-028's floor, which is what KI-060 was.
    function moodTint(mood) {
      const hex = BODY_TINT[mood];
      return hex ? parseInt(hex.slice(1), 16) : NEUTRAL_TINT;
    }
    function moodFill(mood) {
      return mulHex(BODY_FILL, moodTint(mood));
    }
    // The same mood on the RIM's own albedo — not a lighter `moodFill`, but the
    // second base colour the container tint used to multiply alongside the
    // first. `BODY_RIM_FILL` is white, so this comes out as the lifted tint
    // itself, i.e. `BODY_FILL`'s 0xd0 stretched to 0xff: the fixed
    // rim-over-fill ratio `world/visuals.py` says lives "here and nowhere
    // else", preserved by construction instead of by a number restated here.
    function moodRim(mood) {
      return mulHex(BODY_RIM.color, moodTint(mood));
    }

export {
  mixHex, shade, rampLevel, KEY_AXIS, BAND_INSET, CONTACT, SHADOW_THROW, paint,
  repaint,
  moodBlindParts, litRect, litCircle, litColumn, litPath, moodFill, moodRim,
};
