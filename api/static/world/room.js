// /world — Room geometry: the painted plate or the procedural room, the foreground,
// cast anchors and seats, tubes and pillars.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import { app, layers } from "./boot.js";
import {
  WORLD_CONFIG, SYMBOLS, PLATE, PAINTED_CAST, PLATE_SRC, TEXT_PLACEMENTS,
} from "./config.js";
import { shade, rampLevel, paint, litColumn, moodFill } from "./shading.js";
import { CELL, snap } from "./tiers.js";
import { GROUND } from "./cast.js";
import { buildGlows, buildAmbientLights } from "./ambient.js";
import { buildMonitorGraphics } from "./monitors.js";

    // B2: the room is built ONCE and only its alpha and tint are animated.
    // OBS renders this in software on a box that also encodes 1080p, and a
    // full-canvas Graphics rebuilt per frame is the one way this ticket could
    // cost the stream frames — it currently drops zero.
    let floorLine, wall, floorPlane, vignette;

    // Scene-wide lighting per tier, from world/visuals.py — a canvas-shaped
    // ramp, deliberately not the DOM one (see `room_light`). Eased toward,
    // never snapped to: escalate and decay are the *ambient* half of
    // calm-that-swells, so the room brightens over seconds, not on a frame.
    const ROOM_LIGHT = WORLD_CONFIG.room_light;

    // The vignette only. PIXI v8 *does* have `FillGradient` (the wall below
    // uses it) — but its radial path cannot express a transparent centre:
    // `buildRadialGradient` pre-fills the whole gradient texture with the LAST
    // colour stop before painting the ramp over it, and our last stop is opaque
    // black, so a source-over ramp of transparent-black changes nothing and the
    // room renders as a solid black rectangle. Measured, not guessed: against
    // the canvas version this came out max delta 255/255, mean 51.5.
    // Faking it with stacked Graphics bands was the other thing tried, and it
    // reads as a wireframe. So: canvas texture, built on layout only (boot +
    // resize), never per frame.
    function gradientSprite(paint, w, h) {
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.ceil(w));
      canvas.height = Math.max(1, Math.ceil(h));
      paint(canvas.getContext("2d"), canvas.width, canvas.height);
      return new PIXI.Sprite(PIXI.Texture.from(canvas));
    }

    let plateReady = false;

    // The plate is a new hard on-air dependency, which is precisely the shape
    // of KI-045 — and KI-047 is the same page going white for 36 hours because
    // nothing noticed a dead renderer. So the failure path is written first and
    // the success path is the special case: no plate, no manifest, or a texture
    // that will not decode all land in the procedural room the stream already
    // ships.
    // A second draw of the plate, masked to the painted furniture that stands
    // between the camera and the cast, parented above `layers.chars`.
    //
    // The plate is ONE flat image, so without this every character is a
    // sticker on the front of the room: measured at the anchor it actually
    // renders at, the trader's torso stopped at screen x 1375 and the painted
    // backrest's left edge starts at 1375 — he was drawn flush against the
    // chair, never into it.
    //
    // It re-draws the WHOLE plate sprite rather than a cropped frame on
    // purpose. Built exactly the way `drawPlate` builds the background one
    // (same texture, same `PLATE.canvas` size, same layer coordinate space),
    // it lands pixel-for-pixel over it BY CONSTRUCTION, with no frame offset
    // to get wrong; the mask is the only thing deciding what shows. A crop
    // would be the same picture plus arithmetic that can drift from the
    // manifest.
    //
    // No `foreground` in the manifest — an older plate, or a repaint that
    // dropped it — means no sprite and no mask, which is the flat room that
    // shipped all sprint. Same degrade rule `text_surfaces` and
    // `ambient_lights` already follow.
    // It carries its OWN try/catch, and that is the most important line in
    // it. It is called from inside `drawPlate`'s try, so without one a
    // malformed polygon would not cost the chair slice — it would cost the
    // glows, the live monitors, the city lights and the plate itself, and
    // drop a painted room to the procedural one. A chair back is cosmetic;
    // the room is not. The suite measured exactly that collapse the first
    // time this was wired without the catch (`test_a_throwing_builder_
    // degrades_to_a_clean_procedural_room` went red on the GLOW count).
    function drawForeground(texture) {
      try {
        const regions = (PLATE && PLATE.foreground) || [];
        if (!layers.foreground || !regions.length) return;
        const mask = new PIXI.Graphics();
        for (const region of regions) {
          // A region with no polygon masks nothing rather than masking
          // everything: an unmasked full-plate sprite over `layers.chars`
          // paints the whole room on top of the cast.
          if (!Array.isArray(region.poly) || region.poly.length < 3) continue;
          mask.poly(region.poly.flat());
          mask.fill(0xffffff);
        }
        const sprite = new PIXI.Sprite(texture);
        sprite.width = PLATE.canvas[0];
        sprite.height = PLATE.canvas[1];
        sprite.mask = mask;
        layers.foreground.addChild(mask, sprite);
      } catch (err) {
        console.error("foreground failed; the room keeps its plate", err);
      }
    }

    async function drawPlate() {
      if (!PLATE_SRC) { drawProceduralRoom(); return; }
      try {
        const texture = await PIXI.Assets.load(PLATE_SRC);
        const sprite = new PIXI.Sprite(texture);
        sprite.width = PLATE.canvas[0];
        sprite.height = PLATE.canvas[1];
        // Nearest-neighbour: the plate is pixel art, and smoothing it is the
        // one thing guaranteed to make it look worse than the room it replaces.
        texture.source.scaleMode = "nearest";
        layers.plate.addChild(sprite);
        // The same texture again, above the cast, masked to the painted
        // furniture that stands in front of them.
        drawForeground(texture);
        // Built once, right here — never inside the per-frame ticker. See
        // buildGlows's own comment for why.
        buildGlows();
        // M2: one Graphics per painted monitor, same "build once" rule —
        // never inside drawCandles or the poll loop. See buildMonitorGraphics
        // below.
        buildMonitorGraphics();
        // Task 14: the painted city's window lights, same "build once" rule
        // — never inside the per-frame ticker. See buildAmbientLights above.
        buildAmbientLights();
        // Claimed only once ALL THREE builders have actually succeeded —
        // not before them. Every other read of `plateReady` (the vignette
        // gate, applyGlow, tickAmbientLights, drawCandles) treats it as "the
        // room is safe to light as painted"; claiming it before the builders
        // ran let a throw inside any of them leave the flag true while the
        // catch below drew the procedural room on top of the still-added
        // plate sprite — plate and procedural room composited together, a
        // darkened double-exposure.
        plateReady = true;
        // The plate reserves a quiet strip at the top for the newcomer line.
        // Bind the banner to the measurement so a repaint moves the text with
        // the art instead of hanging it over a painted detail.
        // The bottom band DOES have an element - `#nowband`, the page's
        // largest text - and it is CSS-positioned by decision (KI-070): a
        // flex bar pinned `bottom: 0`, whose cells are laid out by the
        // browser rather than placed from the manifest. So `bands.bottom`
        // binds nothing here, and the two `band-bottom` placements that
        // described it were deleted rather than left to pass a CI check
        // over a layout the page does not draw. `history` is the one thing
        // actually drawn on that band, and it is placed (see `draw()`).
        const banner = document.getElementById("banner");
        const bandHeight = bannerMinHeight();
        if (banner && bandHeight) {
          banner.style.minHeight = bandHeight + "px";
        }
        // Sprint 16 Task 10: the bar's own height above binds to the full
        // quiet strip (a background choice, not text); the READABLE line
        // inside it binds to `TEXT_PLACEMENTS.banner` directly, so the rule
        // ("no text in the air") covers this DOM element the same way it
        // covers every canvas label — a repaint that moved the placement
        // moves where the sentence actually sits, not just how tall its bar is.
        const bannerText = TEXT_PLACEMENTS.banner;
        if (banner && bannerText) {
          // `dx`/`dy`, not `x`/`y`: this bar is pinned to the top-left of
          // the surface and pads its line INSIDE itself, so it wants the
          // offset within the surface, never the absolute canvas point the
          // canvas labels are drawn at (KI-072). The two are the same
          // numbers only while `band-top` sits at the canvas origin.
          banner.style.paddingLeft = bannerText.dx + "px";
          banner.style.paddingTop = bannerText.dy + "px";
          banner.style.fontSize = bannerText.size + "px";
        }
      } catch (err) {
        // Deliberately NOT retryBoot(): a reload cannot conjure a missing
        // texture, and a reload storm against a 404 helps nobody (the cap in
        // retryBoot exists for exactly that). Degrade and carry on.
        console.warn("plate failed to load", err);
        // `plateReady` is still false here by construction (see the comment
        // above) — but the sprite may already be sitting in `layers.plate`
        // if the throw came from a builder rather than the texture load
        // itself. Strip it before drawing the fallback, or the room reads as
        // plate-and-procedural-room composited rather than a clean degrade.
        layers.plate.removeChildren();
        drawProceduralRoom();
      }
    }

    function drawProceduralRoom() {
      wall = new PIXI.Graphics();
      floorPlane = new PIXI.Graphics();
      floorLine = new PIXI.Graphics();
      vignette = new PIXI.Container();
      layers.room.addChild(wall, floorPlane, floorLine);
      // Above everything, including the characters: a vignette the cast stood
      // in front of would be a grey rectangle, not a darkened room.
      app.stage.addChild(vignette);
      layoutRoom();
    }

    // The renderer follows the window (`resizeTo`), and the characters are
    // re-placed on every draw — so a floor drawn once at boot drifts off them
    // and they end up standing through it. Same expression, same moment.
    function layoutRoom() {
      if (!floorLine) return;
      const w = app.screen.width, h = app.screen.height, ground = h * GROUND;
      floorLine.clear().rect(0, ground, w, 3).fill(0x2a2e39);

      // Depth in three pieces: a back wall darkening toward the ceiling, a
      // floor plane a shade lighter, and a vignette closing the corners.
      // `textureSpace: "global"` takes the same pixel coordinates the canvas-2D
      // version did, so this is a straight port — verified pixel-for-pixel
      // against it (max delta 1/255, and that one is a sub-pixel row the canvas
      // version gained by rounding `ground` up).
      wall.clear().rect(0, 0, w, ground).fill(new PIXI.FillGradient({
        textureSpace: "global",
        start: { x: 0, y: 0 },
        end: { x: 0, y: ground },
        colorStops: [
          { offset: 0, color: "rgba(0,0,0,0.42)" },
          { offset: 1, color: "rgba(0,0,0,0)" },
        ],
      }));

      floorPlane.clear().rect(0, ground, w, h - ground)
        .fill({ color: 0xffffff, alpha: 0.022 });

      vignette.removeChildren();
      vignette.addChild(gradientSprite((ctx, cw, ch) => {
        const g = ctx.createRadialGradient(
          cw / 2, ch / 2, Math.min(cw, ch) * 0.32,
          cw / 2, ch / 2, Math.max(cw, ch) * 0.72,
        );
        g.addColorStop(0, "rgba(0,0,0,0)");
        g.addColorStop(0.6, "rgba(0,0,0,0.45)");
        g.addColorStop(1, "rgba(0,0,0,1)");
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, cw, ch);
      }, w, h));
    }

    // The symbol pillars — and they are pillars now. They were horizontal bars
    // pinned to the top-right at a fixed 140px pitch, which clipped the first
    // one clean off the canvas at 1080p and read as stray UI rather than as
    // anything in the room. Now they stand ON the floor the characters stand
    // on, sized from the canvas, behind the cast (`layers.props`), so the room
    // has a background made of the market itself.
    // --- P5: the layout is read off the plate, not off the canvas ----------
    // With a plate, every position here is a measurement taken from the
    // painted room (world/plate.py, checked against the PNG itself). Without
    // one — a missing manifest, a texture that will not decode, KI-045's shape
    // — they fall back to the canvas fractions the procedural room has always
    // used, so the degraded path stays exactly as watchable as it is today.
    // Both branches are executed in node by tests/api/test_world_page.py;
    // substring checks over the page could not tell them apart.
    const CAST_FRACTIONS = { model: 0.32, trader: 0.62 };

    function anchorFor(name) {
      const painted =
        plateReady && PAINTED_CAST ? PAINTED_CAST.characters[name] : null;
      if (painted) {
        // `pose` is the plate's answer to "is there a chair here" — the trader
        // is seated on this plate. C2 rebuilds the rig around it; it is
        // returned rather than assigned so there is no dead write waiting for
        // that ticket.
        return {
          x: painted.x,
          baseY: painted.base_y,
          pose: painted.pose || "standing",
          // Two more measurements the painted furniture carries, both optional
          // and both degrading to "the figure the page drew before there was a
          // plate": which way the chair points (`seatedRig` turns the whole
          // pose through it) and how big this body is beside it.
          facing: painted.facing || null,
          figureScale: painted.figure_scale || 1,
        };
      }
      return {
        x: app.screen.width * (CAST_FRACTIONS[name] ?? 0.5),
        baseY: app.screen.height * GROUND,
        pose: "standing",
        facing: null,
        figureScale: 1,
      };
    }

    // Where a seated body's HIPS go, which is a different question from where
    // a seated body FITS. `seat` is the fit budget — the cushion's painted
    // span, what the rig's width is checked against. `sit_anchor` is the point
    // the rig's origin is pinned to. Sprint 15 used one number for both, taking
    // the seat rect's centre as the anchor, and the figure still landed beside
    // the chair (KI-054): this plate draws the chair in three-quarter view, so
    // the cushion's centre and the point a sitter's hips occupy are ~7px and
    // ~8px apart in different directions, and the backrest is up-and-right of
    // both. Two measurements now, two fields.
    //
    // Returns null — never a throw and never a guess — when the plate has no
    // `sit_anchor` (an older manifest, a repaint mid-edit). The caller then
    // keeps the standing anchor, which is exactly the placement that shipped
    // before this existed: a degrade to the previous look, not to a blank one.
    function sitAnchorFor(name) {
      const painted =
        plateReady && PAINTED_CAST ? PAINTED_CAST.characters[name] : null;
      const sit = painted ? painted.sit_anchor : null;
      if (!sit || sit.x == null || sit.y == null) return null;
      return { x: sit.x, baseY: sit.y };
    }

    // The painted cushion's silhouette, in plate pixels, or null. Same gate
    // and same degrade as `sitAnchorFor`: an older manifest has no polygon,
    // and no polygon means the legs draw over the seat as they always did.
    function seatOccluderFor(name) {
      const painted =
        plateReady && PAINTED_CAST ? PAINTED_CAST.characters[name] : null;
      const poly = painted ? painted.seat_occluder : null;
      return Array.isArray(poly) && poly.length >= 3 ? poly : null;
    }

    // The seat hides what hangs behind it (increment 22). The camera is behind
    // the chair, so the cushion's front edge is its upper-LEFT edge on screen
    // and his far knee lands on it: the far shin drops through the cushion and
    // was painted over it ("I don't like that his right leg goes on top of
    // chair"). The far shin and shoe each get an INVERSE mask of the measured
    // cushion (`scripts/measure_chair.py::measure_seat_occluder`).
    //
    // The mask is a SPRITE of the polygon, never a Graphics. PIXI 8.19 routes
    // a Graphics mask to its StencilMask, whose `addBounds` clips the masked
    // object's bounds to the mask's rectangle even when the mask is inverse
    // (its AlphaMask checks `inverse`; the stencil one does not). The keyline
    // filter on `rig` sizes its texture from those bounds, so with a Graphics
    // mask both SHOES — wholly outside the cushion — vanished and the shins
    // ended on a flat line at the texture's edge. Shot, then fixed.
    //
    // The masks live on `layers.foreground`, in plate space like the chair
    // back's, so the cut stays on the painted seat whatever the figure's own
    // container is doing — a hop or a pulse moves the shin, never the chair.
    // One sprite per bone (one texture), because one mask shared by several
    // containers is not something PIXI promises. Its own try/catch, for
    // `drawForeground`'s reason: a cosmetic cut must not cost `character()`
    // the trader.
    function occludeBehindSeat(name, accents) {
      try {
        const poly = seatOccluderFor(name);
        const hangs = accents.filter((g) => g.behindSeat);
        if (!poly || !layers.foreground || !hangs.length) return 0;
        const x0 = Math.min(...poly.map((p) => p[0]));
        const y0 = Math.min(...poly.map((p) => p[1]));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(...poly.map((p) => p[0])) - x0;
        canvas.height = Math.max(...poly.map((p) => p[1])) - y0;
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#ffffff";
        ctx.beginPath();
        poly.forEach(([x, y], i) => (i ? ctx.lineTo(x - x0, y - y0) : ctx.moveTo(x - x0, y - y0)));
        ctx.closePath();
        ctx.fill();
        const texture = PIXI.Texture.from(canvas);
        texture.source.scaleMode = "nearest";
        for (const g of hangs) {
          const mask = new PIXI.Sprite(texture);
          mask.x = x0;
          mask.y = y0;
          g.setMask({ mask, inverse: true });
          layers.foreground.addChild(mask);
        }
        return hangs.length;
      } catch (err) {
        console.error("seat occluder failed; the legs draw over the seat", err);
        return 0;
      }
    }

    // How tall the newcomer banner must be to sit on the strip the plate left
    // quiet for it. `null` means "no plate — leave the CSS alone". A value
    // rather than a branch inside drawPlate, because a test can only run a
    // value: asserting the manifest key appears in the function passed
    // identically with the height hardcoded.
    function bannerMinHeight() {
      if (!plateReady || !PLATE || !PLATE.bands || !PLATE.bands.top) return null;
      return PLATE.bands.top;
    }

    function tubeFor(symbol) {
      if (!plateReady || !PLATE || !PLATE.tubes) return null;
      return PLATE.tubes.find((tube) => tube.symbol === symbol) || null;
    }

    // Where one pressure pillar goes. Separated from the drawing so it can be
    // run without PIXI: an off-by-tens against the painted bore is the whole
    // risk of this ticket, and it is the kind of thing only arithmetic can
    // catch before X2 puts it on air.
    function pillarGeometry(symbol, pressure, index) {
      const tube = tubeFor(symbol);
      const ground = app.screen.height * GROUND;
      const tallest = Math.min(ground - 120, app.screen.height * 0.42);
      const baseY = tube ? tube.base_y : ground;
      const width = tube ? tube.width : 54;
      // A painted tube is a housing with a visible end: the bar may fill it,
      // never overrun it. Without a plate the old ceiling still applies.
      const maxHeight = tube ? tube.height : tallest;
      // Tall enough to be architecture. At 70px they read as bins next to a
      // 240px cast; a pillar has to give the room a vertical.
      const height = Math.min(190 + Math.abs(Number(pressure) || 0) * 70, maxHeight);
      // Right of the cast and marching off toward the corner when procedural,
      // so they read as depth rather than as a chart; centred in the bore when
      // the plate painted one.
      const x = tube ? tube.x - width / 2 : app.screen.width * 0.80 + index * 132;
      // KI-055: the tube is a cylinder seen slightly from above, so the fill's
      // top and base are ellipses, not flat caps - `bore_ry` is that ellipse's
      // measured squash (KI-055, Docs/known-issues.md). Read here, not re-derived
      // in drawPillars, for the same reason this whole function exists:
      // off-by-tens against the painted bore is arithmetic, checkable without
      // PIXI. No painted tube means no measured squash either, so this falls
      // back to a fixed fraction of the (also-fallback) width rather than
      // reaching into a null tube - the same degrade-not-throw rule
      // rampLevel/CONTACT use above.
      const boreRy = tube && Number.isFinite(tube.bore_ry) ? tube.bore_ry : width * 0.13;
      return { x, baseY, width, height, boreRy };
    }

    // How much of the painted tube shows through what fills it. Once per
    // column, not per band: the column is three stacked opaque bands now
    // (KI-062), and PixiJS v8 applies a Graphics' or Container's alpha to
    // each primitive separately — three bands at 0.85 composite to ~0.997
    // where they overlap, an opaque column with a faint seam at every band
    // edge. An AlphaFilter flattens the group first and fades the result.
    const PILLAR_ALPHA = 0.85;
    // ONE filter for the life of the page. `drawPillars` runs on every state
    // update and `removeChildren()` destroys nothing, so a filter per draw
    // would pile up for as long as the stream is up. A bundle without
    // `AlphaFilter` gets null, and the caller falls back to plain alpha
    // rather than throwing inside the per-poll draw.
    let pillarGlassFilter = null;
    function pillarGlass() {
      if (!pillarGlassFilter && PIXI.AlphaFilter) {
        pillarGlassFilter = new PIXI.AlphaFilter({ alpha: PILLAR_ALPHA });
      }
      return pillarGlassFilter;
    }

    function drawPillars(state) {
      // M2: the monitor Graphics live in their OWN layer (layers.monitors),
      // never in layers.props — this removeChildren() only ever touches
      // pillars/caps/labels, the things drawPillars/draw() itself put here.
      layers.props.removeChildren();
      const symbols = Object.entries(state.symbols ?? {})
        .filter(([symbol]) => SYMBOLS.includes(symbol));
      if (!symbols.length) return;
      // Pillars only — `layers.props` also carries the history label, which
      // must not fade with the liquid.
      const glass = new PIXI.Container();
      const filter = pillarGlass();
      if (filter) glass.filters = [filter];
      else glass.alpha = PILLAR_ALPHA;
      symbols.forEach(([symbol, data], i) => {
        const geo = pillarGeometry(symbol, data.pressure, i);
        // KI-062: the pillars are in the light model now. They filled with
        // the raw identity colour as one flat tone, beside a cast that is
        // lit in three bands and painted in the contrast-lifted body colour
        // — the KI-051 belonging problem, one object over. The column is
        // ONE `paint()` volume (`litColumn`, a cylinder) in `moodFill`, the
        // cast's own body colour, so the whole column is lit as one form. Banding each cell
        // instead gives every 4px cell its own ~1px lamp-coloured top:
        // scanlines, not a lit object. No rim — the painted glass is the
        // tube's edge, and a keyline would outline the liquid inside it.
        // The tube fill is a column of discrete cells, not a smooth
        // roundRect — decided 2026-08-25 from the reference image, which
        // draws it as stacked units. One cell of gap every two blocks so the
        // column reads as filled units rather than a single bar. The cells
        // are the MASK now: the light falls on the column, the gaps cut it.
        const cells = new PIXI.Graphics();
        const blocks = Math.floor(geo.height / (CELL * 2));
        for (let b = 0; b < blocks; b++) {
          cells.rect(snap(geo.x), snap(geo.baseY - (b + 1) * CELL * 2), geo.width, CELL);
        }
        cells.fill(0xffffff);
        // The lit volume spans exactly the cells it is masked to — both ends
        // are already whole CELLs, so `litRect`'s own snap moves nothing and
        // no cell can hang past the light.
        const top = snap(geo.baseY - blocks * CELL * 2);
        const bottom = snap(geo.baseY - CELL * 2) + CELL;
        const base = moodFill(data.mood);
        const pillar = new PIXI.Graphics();
        if (blocks > 0) {
          paint(pillar, litColumn(geo.x, top, geo.width, bottom - top), base,
                undefined, { noRim: true });
        }
        pillar.mask = cells;
        // KI-055: the tubes are painted cylinders seen slightly from above,
        // so the surface of what fills them is an ellipse. A flat cap
        // contradicts the perspective of the art it sits inside — it reads
        // as a bar chart, which is the model's BODY, not the room's
        // furniture. The base gets the same treatment: it is the tube's own
        // floor, always visible under the fill, never a flat edge either.
        // Shaded from the column's own base, so the surface and the floor
        // agree with the liquid they bound about both colour and light.
        const cap = new PIXI.Graphics();
        // Review round 2, MINOR 6: every other draw in this function snaps
        // its POSITION but not its size (`cells.rect` above snaps `geo.x`,
        // never `geo.width`) — the cap's centre-x is the one coordinate that
        // skipped it, ~2px off the stripes it caps. `snap(geo.x)` is the
        // exact value the stripes themselves are drawn from, so adding half
        // the (unsnapped) width lands the cap dead-centre on the column it
        // caps, not just closer to it. `cy` is deliberately NOT snapped here
        // — review round 2, MINOR 2: the cap already sits a few px above the
        // painted bore's own measured centre (pinned in
        // tests/unit/test_plate_manifest.py), and that gap looked right
        // in-frame; snapping cy would nudge it without anyone having asked
        // to move it.
        const capX = snap(geo.x) + geo.width / 2;
        // Review round 2, finding (d): opaque caps over a translucent fill
        // read as a different, harder material sitting on top of the liquid
        // rather than its own surface. Same translucency as the column it
        // caps — the one `glass` fade over both, not a per-fill alpha.
        cap.ellipse(capX, geo.baseY - geo.height, geo.width / 2, geo.boreRy)
           .fill(shade(base, rampLevel("lit")));
        cap.ellipse(capX, geo.baseY, geo.width / 2, geo.boreRy)
           .fill(shade(base, rampLevel("shade")));
        // Sprint 16 Task 10: no per-tube symbol/mood label any more — both
        // floated at a fixed offset off the tube's own base, with no
        // declared surface (Task 9 deleted `symbol-BTCUSDT`/`symbol-ETHUSDT`
        // from `text` for exactly that reason), and #nowband already prints
        // every symbol's price and mood on every frame.
        // The mask joins the display list beside what it cuts, so the
        // ambient sway of `layers.props` carries the cells and the light
        // together.
        glass.addChild(cells, pillar, cap);
      });
      layers.props.addChild(glass);
    }

export {
  vignette, ROOM_LIGHT, plateReady, drawPlate, layoutRoom, anchorFor,
  sitAnchorFor, occludeBehindSeat, drawPillars, CAST_FRACTIONS, bannerMinHeight,
  tubeFor, pillarGeometry,
  drawForeground, drawProceduralRoom, gradientSprite,
};
