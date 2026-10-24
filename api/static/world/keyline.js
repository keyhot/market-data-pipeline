// /world — The rim keyline: rim from the light, the WebGL keyline filter and the
// probe that proves it renders.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import "./cast.js";
import { LIGHT } from "./config.js";
import { app } from "./boot.js";

    // THE KEYLINE (increment 15): one dark contour round the whole person,
    // at the plate's own pixel pitch — the thing that makes every object in
    // this painted room read as pixel art, and the one thing the trader never
    // had. The reference trader carries it; the chair he sits in carries it.
    //
    // This is NOT the rim increment 8 took off the person, and the difference
    // is the whole design. That rim was a stroke PER VOLUME, so an arm came
    // out as two outlined sausages and a leg as three links of a chain. This
    // is drawn on the ALPHA of the assembled figure, after every limb has
    // composited, so it can only ever trace the silhouette: where two limbs
    // overlap there is no edge to find. `paint` still strokes no rim for a
    // `keepFill` volume, and `test_a_person_is_not_outlined` still holds.
    //
    // Two jobs in one pass, because they share the grid:
    //   * every pixel takes the colour of one texel inside its `block` cell,
    //     snapped in GLOBAL coordinates — so breathing and gestures move the
    //     figure across a fixed grid instead of dragging the grid with it,
    //     which would shimmer. The texture's global origin is
    //     `uOutputFrame.xy + uGlobalFrame.xy`: in Pixi 8.19 `uGlobalFrame.xy`
    //     is the PARENT filter's offset (0 here) and `uOutputFrame.xy` is
    //     this filter's bounds. Using `uGlobalFrame` alone anchored the grid
    //     to the figure's bounds, and a 10-frame capture measured the cell
    //     phase flipping on x and y as he breathed;
    //   * alpha is hard (the room has no soft edges), and an empty cell with
    //     a filled neighbour becomes the keyline.
    //
    // `color` is sampled, not picked: the most common near-black on the
    // chair's own contours in the plate is (20, 24, 36). `block` is the
    // plate's keyline width in frame pixels.
    const KEYLINE = { color: 0x121622, block: 2 };

    // THE RIM (KI-058, Sprint 17 #12): a light edge one cell INSIDE the
    // keyline, all the way round. Measured on the plate, the dark keyline is
    // only 1.4:1 (median) against the dark desk and cabinet the trader sits
    // in front of: it separates him where the room is lamp-lit and nowhere
    // else, and his clothes are below 3:1 even flat. So the pair is
    // light-inside, dark-outside: the filter sees only the figure's alpha, not
    // the room, and the pair covers either background. The rim lightens each
    // edge cell's OWN colour toward white, by `floor` everywhere plus up to
    // `lift` more where the edge faces the key (`-LIGHT.direction`).
    // Chosen off a 4-way A/B (user, 2026-09-26): the lamp's warmth at 0.5
    // read as an orange sticker outline; this reads as edge light in each
    // garment's hue. Against the dark room's p90 it clears 3:1 on the sweater
    // (4.2) and jeans (3.6); shoes and hair stay ~2.7 — the recorded residual.
    function rimFromLight(light) {
      const d = (light && light.direction) || [0, 1];
      const len = Math.hypot(d[0], d[1]) || 1;
      return {
        color: [1, 1, 1],
        toLight: [-d[0] / len, -d[1] / len],
        floor: 0.3,
        lift: 0.15,
      };
    }
    const RIM = rimFromLight(LIGHT);

    // The shader's rim amount, restated for the probe that proves it: `n` is
    // the outward normal of an edge cell (the sum of the directions to its
    // empty neighbours), in global pixel axes, y down.
    function rimAmount(nx, ny) {
      const len = Math.hypot(nx, ny) || 1;
      const facing = Math.max(0, (nx * RIM.toLight[0] + ny * RIM.toLight[1]) / len);
      return RIM.floor + RIM.lift * facing;
    }

    const KEYLINE_FRAGMENT = `in vec2 vTextureCoord;
out vec4 finalColor;
uniform sampler2D uTexture;
uniform highp vec4 uInputSize;
uniform highp vec4 uInputClamp;
uniform highp vec4 uOutputFrame;
uniform highp vec4 uGlobalFrame;
uniform vec3 uKeyline;
uniform float uBlock;
uniform vec3 uRim;
uniform vec2 uToLight;
uniform float uRimFloor;
uniform float uRimLift;

highp vec2 origin() { return uOutputFrame.xy + uGlobalFrame.xy; }

vec4 cell(highp vec2 g) {
  highp vec2 c = floor(g / uBlock) * uBlock + floor(uBlock * 0.5) + 0.5;
  highp vec2 uv = clamp((c - origin()) * uInputSize.zw, uInputClamp.xy, uInputClamp.zw);
  return texture(uTexture, uv);
}

void main() {
  highp vec2 g = vTextureCoord * uInputSize.xy + origin();
  vec4 s = cell(g);
  float r = cell(g + vec2(uBlock, 0.0)).a;
  float l = cell(g - vec2(uBlock, 0.0)).a;
  float d = cell(g + vec2(0.0, uBlock)).a;
  float u = cell(g - vec2(0.0, uBlock)).a;
  if (s.a >= 0.5) {
    vec3 body = s.rgb / s.a;
    vec2 n = vec2(step(r, 0.5) - step(l, 0.5), step(d, 0.5) - step(u, 0.5));
    bool edge = r < 0.5 || l < 0.5 || d < 0.5 || u < 0.5;
    float facing = length(n) > 0.0 ? max(0.0, dot(normalize(n), uToLight)) : 0.0;
    finalColor = vec4(edge ? mix(body, uRim, uRimFloor + uRimLift * facing) : body, 1.0);
    return;
  }
  float n = max(max(r, l), max(d, u));
  finalColor = n >= 0.5 ? vec4(uKeyline, 1.0) : vec4(0.0);
}
`;

    // Built once and PROVEN once, then shared. A filter is the one thing on
    // this page that can fail after it has been attached: a shader that does
    // not compile is not an exception at construction, it is a program that
    // draws nothing at the first frame — the blank-figure failure (KI-050's
    // class) that no node test can see. So before any figure wears it, the
    // filter draws a known square off-screen and the pixels are read back:
    // it must leave the square's colour inside and put the keyline outside.
    // Anything else — no WebGL, a throw, a wrong pixel — and every figure
    // draws exactly as it did before this existed. The outcome is recorded
    // on `window.__keyline` so a frame can be asked which one it got.
    let keylineCache;
    function keylineFilter() {
      if (keylineCache !== undefined) return keylineCache;
      keylineCache = null;
      try {
        if (!app || !app.renderer || app.renderer.type !== PIXI.RendererType.WEBGL) {
          window.__keyline = "off: not webgl";
          return null;
        }
        const k = KEYLINE.color;
        const filter = new PIXI.Filter({
          glProgram: PIXI.GlProgram.from({
            vertex: PIXI.defaultFilterVert, fragment: KEYLINE_FRAGMENT, name: "keyline",
          }),
          resources: {
            keylineUniforms: {
              uKeyline: { value: new Float32Array([((k >> 16) & 255) / 255, ((k >> 8) & 255) / 255, (k & 255) / 255]), type: "vec3<f32>" },
              uBlock: { value: KEYLINE.block, type: "f32" },
              uRim: { value: new Float32Array(RIM.color), type: "vec3<f32>" },
              uToLight: { value: new Float32Array(RIM.toLight), type: "vec2<f32>" },
              uRimFloor: { value: RIM.floor, type: "f32" },
              uRimLift: { value: RIM.lift, type: "f32" },
            },
          },
          padding: KEYLINE.block * 2,
          resolution: 1,
          antialias: "off",
        });
        if (!keylineProves(filter)) {
          window.__keyline = "off: probe failed";
          return null;
        }
        window.__keyline = "on";
        keylineCache = filter;
      } catch (err) {
        window.__keyline = "off: " + (err && err.message);
      }
      return keylineCache;
    }

    // The off-screen proof: a 12x12 mid-grey square at (11, 11) in a 32x32
    // target: an ODD origin, so the filter's own bounds start off the global
    // grid. A grid anchored to the screen puts pixels 10 and 11 in one cell,
    // so column/row 10 must come out as the RIM (an edge cell, inside the
    // figure) and 9 as the keyline; a grid anchored to the bounds (the
    // shimmer bug) paints 10 as the keyline. The square is grey, not white,
    // so a rim is visibly lighter than the body it edges, and the two edges
    // read are the left (facing away from the key, `floor` only) and the top
    // (facing it) — so a shader that outlines but draws no rim, or ignores
    // the light's direction, is refused like one that draws nothing.
    const PROBE_GREY = 0x40;
    function keylineProves(filter) {
      const probe = new PIXI.Container();
      const inner = new PIXI.Container();
      const g = PROBE_GREY;
      const square = new PIXI.Graphics().rect(11, 11, 12, 12).fill((g << 16) | (g << 8) | g);
      inner.addChild(square);
      inner.filters = [filter];
      probe.addChild(inner);
      const target = PIXI.RenderTexture.create({ width: 32, height: 32, resolution: 1 });
      try {
        app.renderer.render({ container: probe, target, clear: true });
        const { pixels, width } = app.renderer.extract.pixels(target);
        const at = (x, y) => Array.from(pixels.slice((y * width + x) * 4, (y * width + x) * 4 + 4));
        const k = KEYLINE.color;
        const near = (a, b) => Math.abs(a - b) <= 8;
        const is = (rgb) => ([r, gg, b, a]) => a > 200
          && near(r, rgb[0]) && near(gg, rgb[1]) && near(b, rgb[2]);
        const rim = (nx, ny) => {
          const m = rimAmount(nx, ny);
          return RIM.color.map((c) => Math.round(g + (c * 255 - g) * m));
        };
        const body = is([g, g, g]);
        const keyed = is([(k >> 16) & 255, (k >> 8) & 255, k & 255]);
        return body(at(16, 16)) && is(rim(-1, 0))(at(10, 16)) && is(rim(0, -1))(at(16, 10))
          && keyed(at(9, 16)) && keyed(at(16, 9));
      } finally {
        inner.filters = null;
        probe.destroy({ children: true });
        target.destroy(true);
      }
    }

export { keylineFilter, keylineProves, rimAmount, rimFromLight };
