// /world — Ambient light: background drift, pillar glows, ambient light sprites.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import { advanceCharacters } from "./scene.js";
import { PLATE } from "./config.js";
import { snap } from "./tiers.js";
import { app, layers } from "./boot.js";
import { vignette, ROOM_LIGHT, plateReady } from "./room.js";
import { rng32 } from "./animation.js";
import { swellTick } from "./swell.js";

    // --- B6: continuous market-reactive ambient ----------------------------
    // A calm market used to mean a near-frozen frame for the best part of an
    // hour: the room only moved when an event fired. This is the *continuous*
    // half of calm-that-swells — every frame carries a little motion, and its
    // amplitude is market data rather than decoration. Pressure sets the
    // background's colour temperature, agitation sets the drift, and both stay
    // deliberately low-amplitude so quiet reads quiet.
    const ambient = {
      pressure: 0, agitation: 0, tier: 0, t: 0, drift: [],
      // Starts at the calm end and eases from there, so a page that opens
      // during a tier-3 moment swells into it rather than booting bright.
      light: { ...ROOM_LIGHT[0] },
    };
    const DRIFT_COUNT = 26;          // cheap enough for OBS's software renderer
    const BASE_BG = [0x13, 0x17, 0x22];
    const WARM_BG = [0x24, 0x18, 0x1c];   // stress: pressure negative
    const COOL_BG = [0x11, 0x1d, 0x28];   // ease: pressure positive

    function readAmbient(state) {
      const symbols = Object.values(state.symbols ?? {});
      if (!symbols.length) return;
      const mean = (pick) =>
        symbols.reduce((sum, s) => sum + (pick(s) ?? 0), 0) / symbols.length;
      ambient.pressure = mean((s) => s.pressure);
      ambient.agitation = mean((s) => s.agitation);
      ambient.tier = Math.max(...symbols.map((s) => s.tier ?? 0), 0);
    }

    function mixChannel(from, to, amount) {
      return Math.round(from + (to - from) * amount);
    }

    function startAmbient() {
      for (let i = 0; i < DRIFT_COUNT; i++) {
        const dot = new PIXI.Graphics().circle(0, 0, 1.6).fill(0x9aa4b2);
        dot.x = Math.random() * app.screen.width;
        dot.y = Math.random() * app.screen.height;
        dot.alpha = 0;
        // Each mote keeps its own speed so the field never pulses in lockstep.
        dot.speed = 4 + Math.random() * 10;
        layers.room.addChild(dot);
        ambient.drift.push(dot);
      }

      app.ticker.add((ticker) => {
        const dt = ticker.deltaMS / 1000;
        ambient.t += dt;
        swellTick(dt);

        // Scene-wide lighting, eased toward the current tier's ramp. `tier`
        // here is the server's own severity_tier, arriving in /world/state —
        // so the room's swell and the director's decision agree by
        // construction rather than by a second scale (KI-019).
        const tier = Math.max(0, Math.min(3, Math.round(ambient.tier)));
        const want = ROOM_LIGHT[tier];
        for (const key of ["vignette", "warmth", "lift"]) {
          // ~4s to cross the ramp: a swell you can watch arrive, not a cut.
          ambient.light[key] += (want[key] - ambient.light[key]) * Math.min(1, dt / 4);
        }
        if (!plateReady) {
          // `vignette` only exists on the procedural path
          // (drawProceduralRoom is the only place that builds one) — the
          // plate is painted, so there is nothing here to dim. B2's
          // tint/alpha room animation, left exactly as it was.
          vignette.alpha = ambient.light.vignette;
        }
        // The plate can't be tinted (Task 9): applyGlow is a no-op of its
        // own accord when there is no plate, so it is safe to call every
        // tick either way. `ambient.light.lift` — the EASED value from the
        // loop above — not `want.lift`/`ROOM_LIGHT[tier].lift`: reading the
        // ramp's raw target here would snap the glow to full brightness in
        // one frame while the vignette/warmth/background dimming beside it
        // keep easing for four more seconds. Two speeds for one swell.
        applyGlow(ambient.light.lift);
        // Task 14: the city's window lights, off the same running clock —
        // a no-op of its own accord when there is no plate (see
        // tickAmbientLights's own guard), so it is safe to call every tick
        // either way, same as applyGlow above.
        tickAmbientLights(ambient.t);

        // Colour temperature: warm as selling pressure builds, cool as it eases.
        // Tier warmth composes on top rather than replacing it — pressure is
        // *which way* the market leans, tier is *how much is happening*.
        const lean = Math.max(-1, Math.min(1, ambient.pressure / 3));
        const target = lean < 0 ? WARM_BG : COOL_BG;
        const amount = Math.min(1, Math.abs(lean) * 0.85 + ambient.light.warmth * 0.4);
        const warmed = lean < 0 || ambient.light.warmth > 0.05 ? target : COOL_BG;
        const lift = 1 + ambient.light.lift;
        const channel = (i) =>
          Math.min(255, Math.round(mixChannel(BASE_BG[i], warmed[i], amount) * lift));
        app.renderer.background.color =
          (channel(0) << 16) + (channel(1) << 8) + channel(2);

        // Room-wide sway: slow, sub-pixel at rest, a visible breath under load.
        const swayAmp = 1.5 + ambient.tier * 2.2;
        layers.chars.x = Math.sin(ambient.t * 0.21) * swayAmp;
        layers.chars.y = Math.cos(ambient.t * 0.17) * swayAmp * 0.5;
        layers.props.x = Math.sin(ambient.t * 0.13) * swayAmp * 0.4;

        // Characters: run any in-flight reaction animation, or breathe.
        advanceCharacters(dt);

        // Drift: density and speed track agitation, so a quiet market has a
        // barely-there field and a busy one visibly stirs.
        const energy = Math.max(0, Math.min(1, ambient.agitation / 6));
        for (const dot of ambient.drift) {
          dot.y -= dot.speed * energy * dt * 3;
          dot.alpha = 0.05 + energy * 0.28;
          if (dot.y < -4) {
            dot.y = app.screen.height + 4;
            dot.x = Math.random() * app.screen.width;
          }
        }
      });
    }

    // Task 9: the plate is painted, so the room can no longer be tinted —
    // multiplying a pixel-art texture destroys the one detail (the amber
    // desk lamp) that makes the room feel lived in. The glow instead ADDS
    // light to the surfaces the plate deliberately left blank: the dark
    // monitors and the tube housings, listed in the manifest as
    // `PLATE.glow`. A fixed, room-local accent — never the ramp's colour
    // temperature — so the lamp's own painted hue can't drift with tier;
    // only how brightly the room lights it does. Pinned equal to
    // world.visuals.PALETTE["accent"] by
    // tests/api/test_world_page.py::test_glow_color_matches_the_shared_accent
    // rather than injected: this value is deliberately room-local (Override
    // 1 — the fewer things this canvas reads out of the shared theme
    // module, the smaller the KI-019 surface), so a test pins the match
    // instead of a new placeholder wiring the two together at runtime.
    const GLOW_COLOR = 0xf0b90b;
    // Room-local scale from the eased `lift` (0 at tier 0, ~0.22 at tier 3)
    // to a visible additive alpha. Deliberately not a copy of the ramp
    // itself — retuning this number changes only this canvas, never the
    // theme vars block or the overlays that read `room_light` directly
    // (KI-019's family).
    const GLOW_LIFT_TO_ALPHA = 3;

    // Built once, the moment the plate becomes ready (drawPlate, above) —
    // never inside the per-frame ticker. `applyGlow` runs ~60x/sec, and a
    // `new PIXI.Graphics()` per glow rect at that rate is exactly the
    // "full-canvas Graphics rebuilt per frame" cost B2's own comment (above,
    // "the room is built ONCE") warns against, on a box that also encodes
    // 1080p. `ambient.drift`'s dots and the vignette already show the right
    // shape for this ticker — build once, mutate a property per tick — so
    // the glow's own Graphics follow the same rule.
    let glows = [];
    let glowTexture = null;

    // KI-089: the glow was one flat rect per tube PAIR, and at a raised tier
    // it read as a UI highlight box — light off glass has no straight edges.
    // Each painted tube (assigned or spare) now gets its own glow, centred on
    // its measured bore and a little wider than the glass, clamped inside
    // the manifest rect the chart-overlap validator cleared. A rect with no
    // tube in it (none today) keeps one soft glow of its own size.
    // How much wider than the glass its glow spreads.
    const GLOW_SPREAD = 1.8;

    function glowShapes(rect, tubes) {
      const inside = tubes.filter((t) => t.x >= rect.x && t.x <= rect.x + rect.w);
      if (!inside.length) {
        return [{ x: rect.x + rect.w / 2, y: rect.y + rect.h / 2, w: rect.w, h: rect.h }];
      }
      return inside.map((t) => {
        const half = Math.min(t.width * GLOW_SPREAD / 2, t.x - rect.x, rect.x + rect.w - t.x);
        const top = Math.max(rect.y, t.base_y - t.height - t.width / 2);
        const bottom = Math.min(rect.y + rect.h, t.base_y + t.width / 2);
        return { x: t.x, y: (top + bottom) / 2, w: 2 * half, h: bottom - top };
      });
    }

    // Built once, like softShadow's. The colour is baked into the texture, so
    // brightness is only ever the sprite's alpha (never a tint of the plate).
    // Across the tube the light peaks on the glass and falls to nothing at
    // both sides; along it, it fades to nothing past both ends.
    function glowLightTexture() {
      if (!glowTexture) {
        const canvas = document.createElement("canvas");
        canvas.width = 64;
        canvas.height = 256;
        const ctx = canvas.getContext("2d");
        const rgb = [GLOW_COLOR >> 16, (GLOW_COLOR >> 8) & 255, GLOW_COLOR & 255].join(",");
        const across = ctx.createLinearGradient(0, 0, canvas.width, 0);
        across.addColorStop(0, `rgba(${rgb},0)`);
        across.addColorStop(0.3, `rgba(${rgb},0.55)`);
        across.addColorStop(0.5, `rgba(${rgb},1)`);
        across.addColorStop(0.7, `rgba(${rgb},0.55)`);
        across.addColorStop(1, `rgba(${rgb},0)`);
        ctx.fillStyle = across;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.globalCompositeOperation = "destination-in";
        const along = ctx.createLinearGradient(0, 0, 0, canvas.height);
        along.addColorStop(0, "rgba(0,0,0,0)");
        along.addColorStop(0.15, "rgba(0,0,0,1)");
        along.addColorStop(0.85, "rgba(0,0,0,1)");
        along.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = along;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        glowTexture = PIXI.Texture.from(canvas);
      }
      return glowTexture;
    }

    function buildGlows() {
      // Defensive, not load-bearing today: the only caller is drawPlate's
      // try block, once. But a half-thrown call (this map throws partway
      // through, e.g. a bad rect) or a future retry path calling it twice
      // would otherwise leave orphaned sprites stacked in layers.glow —
      // this makes a second call start clean instead of accumulating.
      layers.glow.removeChildren();
      const tubes = [...(PLATE.tubes || []), ...(PLATE.spare_tubes || [])];
      glows = PLATE.glow.flatMap((rect) => glowShapes(rect, tubes).map((shape) => {
        const glow = new PIXI.Sprite(glowLightTexture());
        glow.anchor.set(0.5);
        // Not snapped: a falloff has no edge to hold to the pixel grid, and
        // the bore it lights is measured to the pixel, not the CELL.
        glow.position.set(shape.x, shape.y);
        glow.width = shape.w;
        glow.height = shape.h;
        glow.blendMode = "add";
        glow.alpha = 0;              // applyGlow sets the real value per tick
        layers.glow.addChild(glow);
        return glow;
      }));
    }

    // `lift` must be the EASED `ambient.light.lift`, never `ROOM_LIGHT[tier]
    // .lift` directly — see the ticker's own call site for why.
    function applyGlow(lift) {
      if (!plateReady) return;   // no plate: nothing was ever built to light
      const alpha = Math.min(1, lift * GLOW_LIFT_TO_ALPHA);
      for (const glow of glows) glow.alpha = alpha;
    }

    // --- Task 14 (STRETCH): the painted city's window lights blink --------
    // The plate already paints a skyline through the room's own window; this
    // animates what is there rather than inventing anything new. A small
    // list of rects measured over painted glass, each fading on its own
    // phase and period — a shared clock would pulse the whole skyline in
    // unison, which reads as a fault rather than as a city.
    const AMBIENT_LIGHTS = (PLATE && PLATE.ambient_lights) || [];
    // The floor of the fade: dim, not zero. Additive light at alpha 0 IS the
    // plate's own painted light — correctly "off" — but never literal zero,
    // so a light is never left fully unlit for the low point of its cycle
    // either (a light that vanishes entirely reads as a rendering glitch,
    // not a window that is briefly dark).
    const AMBIENT_LIGHT_MIN_ALPHA = 0.12;

    // Built once, exactly like buildGlows/buildMonitorGraphics beside it —
    // never inside the per-frame ticker. Each light gets its own Graphics
    // (mutated only on .alpha per tick) and its own phase, seeded off the
    // light's index with the same rng32 the cast's breathing uses, so a
    // reload is reproducible but no two lights start in lockstep. Additive
    // blend, the same as buildGlows: an opaque fill at alpha near 1 would
    // paint FLAT OVER the plate's own painted detail (several of these
    // signs have an icon drawn inside the red field) the moment a light
    // reached peak brightness — the opposite of animating what is already
    // there. `filter` skips any entry missing a usable colour rather than
    // throwing mid-map — a malformed manifest entry degrades that ONE light
    // to unlit, never the whole room to procedural (buildMonitorGraphics's
    // `if (screen.quad)` guard is the same idea for a different builder).
    let ambientLightSprites = [];

    function buildAmbientLights() {
      ambientLightSprites = AMBIENT_LIGHTS
        .filter((light) => (
          light && /^#[0-9a-fA-F]{6}$/.test(light.colour || "")
          && light.w > 0 && light.h > 0
        ))
        .map((light, i) => {
          // Snap the position, draw the MEASURED size (KI-075, the rule
          // `drawPillars` states): a snapped 6 drew 8 and bled lamp colour
          // off the sign, and a snapped 1 or 2 drew nothing at all.
          const graphics = new PIXI.Graphics()
            .rect(snap(light.x), snap(light.y), light.w, light.h)
            .fill({ color: parseInt(light.colour.slice(1), 16), alpha: 1 });
          graphics.blendMode = "add";
          layers.plate.addChild(graphics);
          return {
            graphics,
            period: light.period > 0 ? light.period : 4,
            phase: rng32(i + 1)(),
          };
        });
    }

    // `t` is `ambient.t`, the same running clock the swell/drift/breathing
    // already read — never a second clock of its own (KI-019's family).
    function tickAmbientLights(t) {
      if (!plateReady) return;   // no plate: nothing was ever built to light
      for (const light of ambientLightSprites) {
        const u = 0.5 + 0.5 * Math.sin((t / light.period + light.phase) * Math.PI * 2);
        light.graphics.alpha = AMBIENT_LIGHT_MIN_ALPHA + (1 - AMBIENT_LIGHT_MIN_ALPHA) * u;
      }
    }

export { ambient, readAmbient, startAmbient, buildGlows, buildAmbientLights, applyGlow, glowLightTexture, glowShapes, tickAmbientLights };
