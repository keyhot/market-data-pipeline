// /world — Renderer lifecycle: the app/layers/cast bindings, boot() with backoff
// retry, eval layers, the gallery and animation-sheet still modes, labels.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import { TIER_CUTS } from "./tiers.js";
import { SYMBOLS, fallback } from "./config.js";
import { drawPlate, occludeBehindSeat } from "./room.js";
import { STYLES, CAST, CAST_SCALE, setCharacterVisible } from "./cast.js";
import { character, setExpression } from "./character.js";
import { ANIM, ANIM_DURATION, SEATED_ANIMATIONS } from "./animation.js";
import { cast, positionCharacters } from "./scene.js";
import { startAmbient } from "./ambient.js";
import { BARS_POLL_MS, pollBars } from "./monitors.js";
import { swell } from "./swell.js";
import { react, refresh, subscribe } from "./main.js";

    // Declared here, constructed in boot(): character() needs app.screen and
    // the layer containers, neither of which exist until PIXI has initialised.
    let app, layers = {}, model = null, trader = null;

    // This page is unattended and on air. Every failure below used to latch:
    // the card was painted once and the room stayed blank until a human
    // refreshed the browser source. So a boot failure now reloads on a bounded
    // backoff. The window it covers is small but recurring — a browser source
    // reloading while the API container restarts, i.e. every deploy.
    const RETRY_BACKOFF_MS = [2000, 5000, 15000, 30000, 60000];

    function retryBoot(reason) {
      // The counter rides in the URL, so treat it as untrusted input: anything
      // that is not a clean non-negative integer counts as exhausted rather
      // than as zero. `Number("abc")` is NaN, and NaN fails every comparison
      // below — it would slip past the cap and then increment to NaN forever,
      // which is the reload storm this cap exists to prevent.
      const raw = Number(
        new URLSearchParams(location.search).get("boot_retry") || 0
      );
      const attempt =
        Number.isInteger(raw) && raw >= 0 ? raw : RETRY_BACKOFF_MS.length;
      if (attempt >= RETRY_BACKOFF_MS.length) {
        // Out of retries: say so plainly and stop. A reload storm against an
        // API that is genuinely down helps nobody.
        fallback.textContent = reason + " — the world is still running";
        return;
      }
      fallback.textContent = reason + " — retrying…";
      const next = new URL(location.href);
      next.searchParams.set("boot_retry", String(attempt + 1));
      setTimeout(() => location.replace(next.toString()), RETRY_BACKOFF_MS[attempt]);
    }

    // Called only once the room is fully built. Clearing the counter any
    // earlier defeats the cap: anything that throws *after* the reset but
    // before boot() resolves lands in the catch with a fresh budget, and the
    // page reload-loops forever against the API instead of stopping after
    // five tries. The reset has to be the last thing, not the first.
    function bootSucceeded() {
      if (!new URLSearchParams(location.search).has("boot_retry")) return;
      const clean = new URL(location.href);
      clean.searchParams.delete("boot_retry");
      history.replaceState(null, "", clean.toString());
    }

    // The void is a legitimate question — comparing two silhouettes against
    // each other is easier without a painting behind them — but it is no
    // longer the default. Judging a cast against a background the cast will
    // never stand in is how KI-051 shipped. Top-level, not inside boot():
    // drawGallery and drawAnimationSheet are boot()'s siblings, not its
    // children, and need to read this too.
    const VOID_MODE = new URLSearchParams(location.search).get("void") === "1";

    async function boot() {
      if (typeof PIXI === "undefined") {
        retryBoot("renderer unavailable");
        return;
      }
      app = new PIXI.Application();
      await app.init({ background: 0x131722, resizeTo: window, antialias: false });
      document.getElementById("room").appendChild(app.canvas);
      fallback.textContent = "";

      // KI-046: the room can be dark while every light is green. The count is
      // incremented by the RENDER loop, so a page whose setInterval still fires
      // over a dead renderer reports a frozen frame count rather than health.
      let framesDrawn = 0;
      app.ticker.add(() => { framesDrawn++; });

      // Below the room: the painted backdrop, when there is one.
      layers.plate = new PIXI.Container();
      layers.room = new PIXI.Container();
      // Task 9: additive light (applyGlow) falls ON the room but must never
      // land over the cast's faces, so it sits above layers.room — and,
      // since M2's live chart candles (layers.monitors) must never be washed
      // out by the glow, this sits below that layer too.
      layers.glow = new PIXI.Container();
      // M2: the monitors get their OWN container, a stage sibling exactly
      // like layers.glow — built once (buildMonitorGraphics, called from
      // drawPlate's success path) and never wiped by anyone else. It does
      // NOT share layers.props: that container has a per-cycle
      // wipe-and-rebuild contract (drawPillars calls removeChildren() on
      // every draw()), which is a different lifecycle than "build once,
      // never cleared" — putting both contracts in one container is how a
      // future props consumer forgets to preserve the monitors and the room
      // goes blank on air with no text-reading test able to see it. Placed
      // above layers.glow (so it never washes the candles out) and below props
      // (so pillars/labels still paint over it exactly as before, since they
      // never spatially overlap the painted screens anyway).
      layers.monitors = new PIXI.Container();
      layers.chars = new PIXI.Container();
      layers.props = new PIXI.Container();
      // Sprint 16 Task 10: `name-trader` originally sat on `desk-plate-trader`,
      // real clean desk paint (Task 9 verified it) that the seated rig's own
      // head rendered almost entirely in front of at this cast scale/anchor —
      // a fact nothing had ever rendered the room live enough to see. A
      // surface can be exactly where the plate says and still be unreadable
      // if a layer above it paints over it, which is as much "text where it
      // is not necessary" as hovering in the air was. Fix round: `name-trader`
      // moved to `desk-face-trader` (the desk's front panel, well clear of
      // the rig — see `world/plate.py`'s docstring and
      // `test_the_name_trader_surface_does_not_intersect_the_seated_rig`).
      // `nameplates` stays its own layer, added to `app.stage` LAST (after
      // `layers.chars`) regardless: it is what keeps a label from inheriting
      // a reaction's shake/hop, and it is cheap insurance against the next
      // surface ever landing under a figure again.
      layers.nameplates = new PIXI.Container();
      // Shadows are a *child* of the cast layer, not of any character: they
      // inherit CAST_SCALE and the layer's coordinate space for free, so a
      // shadow's x is just the character's x — but they are siblings of the
      // bodies, so a lean, a squash or a hop moves the body without dragging
      // its shadow off the floor with it. Added first, so they stay behind.
      layers.shadows = new PIXI.Container();
      layers.chars.addChild(layers.shadows);
      // The painted furniture that stands in FRONT of the cast — the chair
      // back the trader sits against. It is a second draw of the plate's own
      // pixels, masked to a polygon the manifest measures, so it lands above
      // `layers.chars` and below the labels. See `drawForeground`.
      layers.foreground = new PIXI.Container();
      app.stage.addChild(
        layers.plate, layers.room, layers.glow, layers.monitors,
        layers.props, layers.chars, layers.foreground, layers.nameplates);
      // Pixel-art rule: no sub-pixel compositing for anything the cast or its
      // furniture draws. The plate and the vignette stay off this list on
      // purpose — they are painted art and a soft light falloff, not part of
      // the grid. The glow layer is additive light too, for the same reason.
      layers.chars.roundPixels = true;
      layers.props.roundPixels = true;
      layers.monitors.roundPixels = true;
      layers.nameplates.roundPixels = true;

      await drawPlate();
      model = character("MODEL");
      trader = character("TRADER");
      // Only the ROOM's trader, never inside `character()`: the animation
      // sheet builds seated TRADER samples too, in grid cells nowhere near
      // the chair, and a plate-space mask would cut them wherever the
      // painted cushion happens to fall on the sheet.
      occludeBehindSeat("trader", trader.accents);
      // `character()`'s nameTag is shared with the gallery/animation-sheet
      // eval surfaces (`?gallery=1`/`?anims=1`), which legitimately float a
      // label beside a diagnostic specimen — the design rule ("no text in
      // the air") is about the ROOM, never those. The room names nobody:
      // the MODEL/TRADER nameplates it used to set on the furniture came off
      // (user, 2026-09-24) — the trader's sat ~190px from him on the desk
      // front — and the MODEL wears its own name now (`MACHINE.badge`).
      // `test_the_room_names_nobody_in_the_air`. The constructor's floating
      // copy is blanked here rather than removed from `character()` itself,
      // which both eval surfaces still need.
      model.nameTag.text = "";
      trader.nameTag.text = "";
      cast.push(model, trader);
      positionCharacters();
      startAmbient();
      bootSucceeded();

      // M2: polled, not streamed (KI-013 — see pollBars's own comment). Runs
      // regardless of which boot branch follows; pollBars is a no-op with no
      // plate, so this is as safe on the preview/gallery paths as it is live.
      pollBars();
      setInterval(pollBars, BARS_POLL_MS);

      // KI-046: posted every 15s regardless of which boot branch runs below —
      // the beat is about whether the renderer is alive, which is equally true
      // of a preview surface as of the on-air room. Keyed by Host server-side,
      // so a developer tab never covers for a dead on-air shard (see
      // world_heartbeat's docstring).
      setInterval(() => {
        fetch("/world/heartbeat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ page: "world", frames: framesDrawn }),
        }).catch(() => {});   // a failed beat is the watchdog's problem, not the room's
      }, 15000);

      // scripts/shoot.py polls this instead of guessing a duration — readiness
      // is polled, not timed. That design only holds if the flag actually
      // means "drawn": set it before any gate's own draw call runs and the
      // poll is decorative, the settle sleep becomes the only real guard, and
      // shoot.py can photograph a page that reports ready before it painted
      // anything. So each gate below sets it itself, immediately after ITS
      // OWN drawing call, never here up front.

      // B1 evaluation gate: `?gallery=1` lays out both styles against every
      // mood so the options can be compared in one screenshot. Aesthetics is
      // the thing no assertion grades, so it needs to be *lookable*.
      if (new URLSearchParams(location.search).get("gallery") === "1") {
        drawGallery();
        window.__worldDrawn = true;
        return;
      }
      // `?anims=1` — the other half of the eval gate, and the half that was
      // missing: sixteen named animations shipped without anyone ever seeing
      // one. Every animation, on both cast bodies, looping. LOCAL ONLY — 32
      // characters ticking is not something to point a browser source at.
      if (new URLSearchParams(location.search).get("anims")) {
        drawAnimationSheet();
        window.__worldDrawn = true;
        return;
      }

      // B8's evaluation gate. The plan said "trigger via a synthetic tier-3
      // world event" — but `world_events` is append-only and its entire worth
      // is that it is true, which is the trade B4 already refused. So the
      // choreography runs here instead, on a LOCAL-ONLY surface the way
      // `?anims=1` does: no row is written, and nothing points a browser source
      // at it. Returns before subscribe(), which is also what makes it
      // screenshottable — headless Chrome hangs on this page's SSE.
      //
      // The severity is taken from the injected cuts rather than typed in, so
      // the preview swells for the same reason the live room does.
      if (new URLSearchParams(location.search).get("swell")) {
        const cuts = TIER_CUTS.cuts["model_losing_streak"] ?? TIER_CUTS.generic;
        const rehearse = () => {
          swell.last = -1e9;        // the floor guards the live room, not this
          react({
            event_type: "model_losing_streak",
            symbol: SYMBOLS[0] ?? "BTCUSDT",
            severity: cuts[cuts.length - 1] + 1,
            payload: {},
          });
        };
        rehearse();
        window.__worldDrawn = true;
        setInterval(rehearse, 12000);
        return;
      }

      // Live path: `draw(world)` happens inside refresh(), which already
      // swallows its own fetch/parse failures to keep the last frame on
      // screen (see refresh's own comment) — but the flag has to be honest
      // even if that ever stops being true. A dropped `/world/state` call is
      // a data problem, not a failure to draw the room (the cast is already
      // positioned above), so the flag is set from `finally`, not only on
      // success — otherwise a throwing refresh would leave `__worldDrawn`
      // false forever and shoot.py would spin its full 30s poll on a page
      // that is actually fine.
      try {
        await refresh();
      } finally {
        window.__worldDrawn = true;
      }
      subscribe();
      setInterval(refresh, 60000);   // projection stays authoritative server-side
    }

    // KI-064: every stage layer, classified for the eval gates (`?gallery=1`,
    // `?anims=1`). They used to hide content one layer at a time by name, and
    // each new layer leaked onto the specimen sheets until a screenshot caught
    // it (monitors, then nameplates). Now a layer is one of:
    //   scenery — the painted room; shown, unless `?void=1` strips it
    //             (`foreground` is the plate's own pixels, so it goes too);
    //   content — live or room-specific drawing that would sit over the
    //             specimens (candles, pillars, name labels); hidden;
    //   stage   — what the eval surface itself draws into; left alone.
    // A layer missing here throws on the eval path and fails
    // `test_every_stage_layer_has_an_eval_role` in CI.
    const EVAL_LAYER_ROLES = { plate: "scenery", room: "scenery", glow: "scenery", foreground: "scenery", monitors: "content", props: "content", nameplates: "content", chars: "stage", shadows: "stage" };

    function applyEvalLayers() {
      for (const [name, layer] of Object.entries(layers)) {
        const role = EVAL_LAYER_ROLES[name];
        if (role === "scenery") layer.visible = !VOID_MODE;
        else if (role === "content") layer.visible = false;
        else if (role !== "stage") throw new Error(`stage layer ${name} has no eval role (KI-064)`);
      }
    }

    function drawGallery() {
      setCharacterVisible(model, false);
      setCharacterVisible(trader, false);
      // Scenery stays (unless ?void=1), content goes — `EVAL_LAYER_ROLES`.
      applyEvalLayers();
      layers.chars.scale.set(1);     // the gallery does its own fitting
      // A representative spread rather than all 23 moods: every style compared
      // across the emotional range they actually have to carry.
      const moods = ["elated", "focused", "anxious", "dejected", "idle",
                     "startled", "bullish", "bearish"];
      // Derive the pitch instead of hard-coding it: at a fixed one, adding the
      // fifth style pushed it off the bottom of the canvas, and an option that
      // can't be seen is an option that never gets evaluated.
      const top = 56;                                    // clear the banner
      const rowH = (app.screen.height - top) / STYLES.length;
      const colW = app.screen.width / moods.length;
      // A sample is ~220 units tall above its feet (the mood tag) and ~60
      // below (name + style), so 320 per row leaves a margin at any height.
      const fit = Math.min(1, rowH / 320, colW / 200);
      STYLES.forEach((style, row) => {
        moods.forEach((mood, col) => {
          const sample = character(mood, style);
          sample.container.scale.set(fit);
          sample.container.x = colW * (col + 0.5);
          sample.container.y = top + rowH * row + 220 * fit;
          sample.baseX = sample.container.x;
          sample.baseY = sample.container.y;
          sample.headBaseY = sample.head.y;
          setExpression(sample, mood);
          sample.moodTag.text = mood;
          sample.stat.text = style;
        });
      });
    }

    // Sixteen animations went live without anyone seeing one: a still frame
    // can't show motion, and three of them (wave, shake, flicker) are at or
    // near zero displacement at any phase you'd think to freeze. So the sheet
    // LOOPS them, through `advanceCharacters` — a sheet with its own player
    // would be showing me an animation nobody ships.
    // C2 review round 1, Finding 2b: this sheet is the ticket's own
    // prescribed check ("check that in every frame of every trader animation
    // the figure stays in the chair") - the real query param is `?anims=`,
    // not the brief's invented `?sheet=1`. Before this fix it was
    // unperformable: `character(style, style)` passes a BODY-STYLE name
    // ("figure") where `anchorFor` wants a CHARACTER name ("trader"), so
    // every sample resolved pose:"standing" and seatedRig never ran here,
    // and `asleep`/`gesture`/`breathe` aren't ANIM keys at all, so they
    // could not even be listed.
    function drawAnimationSheet() {
      setCharacterVisible(model, false);
      setCharacterVisible(trader, false);
      // Scenery stays (unless ?void=1), content goes — `EVAL_LAYER_ROLES`.
      applyEvalLayers();
      // Deliberately not the gallery's scale-1: `slump` moves a head 13.5px,
      // and whether that reads as dejected or as a head in a torso is a
      // question about broadcast size. Tier-3 amplitude, for the same reason.
      layers.chars.scale.set(CAST_SCALE);
      const amp = 0.6 + 3 * 0.25;
      // `?anims=slump` narrows to one animation across every body — iterating
      // on a single pose against 32 looping cells is how you miss things.
      const only = new URLSearchParams(location.search).get("anims");
      // Cells are (character-name, body-style, animation-name) triples, not
      // a name x style cross product: `gesture`/`breathe`/`asleep` only mean
      // anything for the trader, and trying them against the model's bars
      // body would call `SEATED_ANIMATIONS[name] || ANIM[name]` with both
      // sides undefined and throw.
      let cells;
      if (ANIM[only]) {
        // Unchanged from before this fix: one animation surveyed across
        // every candidate body, standing, for comparing body TYPES rather
        // than casting - `style` stays the sample's own name here on purpose.
        cells = STYLES.map((style) => [style, style, only]);
      } else if (SEATED_ANIMATIONS[only] && !ANIM[only]) {
        cells = [["TRADER", CAST.TRADER, only]];
      } else {
        cells = [
          ...Object.keys(ANIM).map((name) => [CAST.MODEL, CAST.MODEL, name]),
          // "TRADER", not CAST.TRADER twice - the one change that makes
          // `anchorFor("trader")` resolve `pose: "seated"` and seatedRig run.
          ...Object.keys(ANIM).map((name) => ["TRADER", CAST.TRADER, name]),
          // The three names ANIM doesn't have at all - otherwise declared
          // and never once seen, the exact defect this ticket is about.
          ...Object.keys(SEATED_ANIMATIONS)
            .filter((name) => !ANIM[name])
            .map((name) => ["TRADER", CAST.TRADER, name]),
        ];
      }
      const total = cells.length;
      const cellW = 250;
      const cols = Math.max(1, Math.floor(app.screen.width / cellW));
      const rows = Math.ceil(total / cols);
      // A cell shorter than this overlaps its neighbour's labels. The sheet is
      // a local page, so it runs off the bottom of a short window rather than
      // shrinking the thing whose size is the question.
      const rowH = Math.max(330, (app.screen.height - 40) / rows);
      cells.forEach(([name, style, anim], idx) => {
        const sample = character(name, style);
        sample.container.x = cellW * ((idx % cols) + 0.5) / CAST_SCALE;
        sample.container.y =
          (40 + rowH * Math.floor(idx / cols) + rowH * 0.86) / CAST_SCALE;
        sample.baseX = sample.container.x;
        sample.baseY = sample.container.y;
        sample.headBaseY = sample.head.y;
        setExpression(sample, "focused");
        sample.moodTag.text = anim;
        sample.stat.text = "";
        sample.loopAnim = anim;
        sample.amp = amp;
        sample.duration = ANIM_DURATION[anim] ?? 0.9;
        sample.phase = (idx * 0.13) % 1;   // stagger, so it isn't a chorus line
        cast.push(sample);
      });
    }

    function label(text, x, y, size, tint) {
      const t = new PIXI.Text({ text, style: {
        fill: tint ?? 0xd1d4dc, fontFamily: "system-ui", fontSize: size,
      }});
      t.x = x; t.y = y;
      return t;
    }

    // Sprint 16 Task 10: the room's own text is never positioned by a
    // literal offset any more (KI-054's sibling failure, one layer up — a
    // label that hovers near a character instead of sitting on the surface
    // the room actually painted for it). `placement` is a resolved
    // `TEXT_PLACEMENTS.*` entry, so the caller always names the const field
    // directly (`TEXT_PLACEMENTS.history`, `TEXT_PLACEMENTS["name-model"]`)
    // rather than routing through a second lookup this function would have
    // to trust. A placement that did not resolve (surface repainted away,
    // manifest missing) is `undefined` here, same contract as `as_json`
    // itself: the label is omitted, never a throw into the render loop.
    function placedLabel(placement, text, tint) {
      if (!placement) return null;
      return label(text, placement.x, placement.y, placement.size, tint);
    }

export { app, layers, model, trader, retryBoot, boot, label, placedLabel, drawAnimationSheet, drawGallery, applyEvalLayers };
