# World renderer stack decision

**Decision: PixiJS 8.19.0, loaded from a pinned CDN URL with an SRI hash.**

## Why

The world room is 2D: a floor line, two character sprites built from
procedural geometry, per-symbol mood pillars, and text labels carrying the
numbers. PixiJS is a 2D WebGL compositor built for exactly that shape.

| Criterion | PixiJS 8.19.0 | Three.js r17x |
|---|---|---|
| Dimensionality | 2D-native; scene is a display list | 3D scene graph; 2D means an orthographic camera and manual layout |
| Text quality | First-class `Text`/`BitmapText`, crisp at fixed DPR | Needs `TextGeometry` (font loading) or DOM/CSS overlays; both are awkward |
| CDN bundle (minified) | ~780 KB, single UMD `PIXI` global | Comparable, but a usable 2D setup needs extra loaders |
| 24/7 OBS CPU cost | Lower — no lighting, no depth pass, no per-frame matrix work we don't need | Higher for identical output |
| Sprint 14 headroom | Many small interacting sprites is Pixi's core case | Would be fighting the abstraction |
| Licence | MIT | MIT |

Both are MIT, so no attribution footer is required (unlike TradingView on
`chart.html`).

## Alternatives considered

- **Three.js** — rejected above. Revisit only if the world becomes genuinely
  volumetric.
- **Phaser** — ruled out at sprint planning: it is a game framework (physics,
  input, state machines) and we need a renderer.
- **Plain Canvas 2D** — viable for v0 and zero-dependency, but Sprint 14's
  "many small interacting systems" would mean writing a display list, a
  transform stack, and batching by hand. Deferred rather than dismissed: if
  PixiJS ever proves too heavy for the OBS source, this is the fallback.

## Constraints this inherits

- **No build step and no static-asset pipeline.** The repo has no
  `StaticFiles` mount; third-party JS loads from a pinned CDN URL with
  `integrity` + `crossorigin="anonymous"`, matching `chart.html`.
- **Offline behaviour:** if the CDN is unreachable, the page renders its
  static HTML shell and the status line reads "renderer unavailable". The
  stream degrades to a readable text state rather than a blank canvas.
- **Procedural geometry only in v0** — shapes, lines, and text, no external
  sprite or texture assets. Art becomes a later addition behind the same
  projection and reaction layers.

## How it's wired

`GET /world/state` returns the projection; `api/templates/world.html` draws it
and applies deltas from `EventSource('/stream/world/events')`. The renderer
never computes state — it only draws what `world/state.py` and
`world/reactions.py` produced.
