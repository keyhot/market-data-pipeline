// /world — The chart monitors: candle drawing and the bars poll.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import "./ambient.js";
import { WORLD_CONFIG, PLATE } from "./config.js";
import { CELL, snap } from "./tiers.js";
import { layers } from "./boot.js";
import { plateReady } from "./room.js";

    // --- M2: live candles in the painted monitors -------------------------
    // The plate paints two central monitors as flat dark glass. The rules for
    // whether/how much of that glass is worth drawing into — how many candles
    // fit an irregular painted frame, whether a frame is legible at all, and
    // when data is too old to be shown as the present — are decided in
    // world/monitors.py and reach this page as the numbers in the config:
    // injecting only the numbers was the half-fix that let KI-019 happen (the
    // tier-cuts block above is the same lesson applied to tiers), so the
    // three functions that read them are below, and read NOTHING else.
    const MONITOR_RULES = WORLD_CONFIG.monitor_rules;
    // The rule-reading functions themselves - barsThatFit, isDrawable and
    // staleAlpha. `test_monitors.py` runs these three, as served, in node and
    // compares every answer with world/monitors.py, so the page and the server
    // cannot drift on how many candles fit or when a screen is too old to be
    // believed. `barsThatFit` and `isDrawable` work at the rules' own cell
    // (the cast's 4px grid), not at the page's `CELL`.
    function barsThatFit(widthPx) {
      const usable = widthPx - 2 * MONITOR_RULES.pad_cells * MONITOR_RULES.cell;
      const pitch = (MONITOR_RULES.body_cells + MONITOR_RULES.gap_cells) * MONITOR_RULES.cell;
      return Math.max(0, Math.floor(usable / pitch));
    }
    function isDrawable(rect) {
      return rect.w >= MONITOR_RULES.min_width && rect.h >= MONITOR_RULES.min_height
        && barsThatFit(rect.w) >= MONITOR_RULES.min_bars;
    }
    function staleAlpha(ageSeconds) {
      return ageSeconds > MONITOR_RULES.stale_after_seconds ? MONITOR_RULES.stale_alpha : 1;
    }

    // One Graphics per painted screen, created ONCE — on the plate-success
    // path, right after buildGlows() — and only cleared/redrawn per
    // poll. Rebuilding Graphics every tick or every poll is exactly what B2
    // warned would cost the stream frames on a box that also encodes 1080p.
    const monitorGraphics = {};

    function buildMonitorGraphics() {
      for (const screen of PLATE.screens || []) {
        const g = new PIXI.Graphics();
        // layers.monitors: its own container, built once here and never
        // wiped by anyone else (see the layer's own comment in boot()).
        layers.monitors.addChild(g);
        monitorGraphics[screen.id] = g;
        // KI-052: clip candles to the painted glass's own quad, not the
        // bounding rect drawCandles lays them out in - the rect can (and,
        // before this fix, did) reach past the quad's slanted bottom edge
        // at the bottom-right. Built once here, exactly like `g` itself:
        // drawCandles runs once per screen per poll, forever, so anything
        // it added to a layer would leak without bound (see that
        // function's own comment, and
        // test_the_monitor_graphics_are_built_once_not_per_poll_or_per_tick).
        // Guarded on `screen.quad` so a manifest entry with no quad (never
        // shipped today, but not schema-forbidden) fails closed to an
        // unmasked screen rather than throwing inside boot().
        if (screen.quad) {
          const glass = new PIXI.Graphics().poly(screen.quad.flat()).fill(0xffffff);
          g.mask = glass;
          layers.monitors.addChild(glass);
        }
      }
    }

    // Procedural pixel candles, on the same 4px grid as the cast. Not
    // Lightweight Charts: a smooth anti-aliased chart inside a pixel-art
    // monitor reads as a screenshot pasted into a painting.
    function drawCandles(screen, bars) {
      const g = monitorGraphics[screen.id];
      // A screen absent from monitorGraphics (never built, or a stale
      // manifest reference) fails closed rather than throwing inside a
      // render loop the whole page depends on.
      if (!g) return;
      g.clear();
      // barsThatFit / isDrawable are INJECTED from world/monitors.py — the
      // page must not re-derive them. A screen too small for a legible
      // candle stays dark glass: the plate's painted state is the honest
      // rendering of "nothing to say here".
      const fits = barsThatFit(screen.w);
      if (!isDrawable(screen)) return;
      if (!bars || !bars.length) return;

      const shown = bars.slice(-fits);
      const highs = shown.map((b) => b.high), lows = shown.map((b) => b.low);
      const top = Math.max(...highs), bottom = Math.min(...lows);
      const span = top - bottom;
      if (!(span > 0)) return;      // a flat span would divide by zero and draw a lie

      const newest = Date.parse(shown[shown.length - 1].timestamp);
      const ageSeconds = (Date.now() - newest) / 1000;
      g.alpha = staleAlpha(ageSeconds);   // the rule's own numbers, from the config - see above

      const pad = MONITOR_RULES.pad_cells * CELL;
      const pitch = (MONITOR_RULES.body_cells + MONITOR_RULES.gap_cells) * CELL;
      const plotH = screen.h - 2 * pad;
      const yOf = (price) => snap(screen.y + pad + (top - price) / span * plotH);

      shown.forEach((bar, i) => {
        const x = snap(screen.x + pad + i * pitch);
        const up = bar.close >= bar.open;
        const clr = up ? 0x26a69a : 0xef5350;
        const bodyTop = yOf(Math.max(bar.open, bar.close));
        const bodyBottom = yOf(Math.min(bar.open, bar.close));
        g.rect(x + CELL, yOf(bar.high), CELL, yOf(bar.low) - yOf(bar.high)).fill(clr);
        g.rect(x, bodyTop, MONITOR_RULES.body_cells * CELL,
               Math.max(CELL, bodyBottom - bodyTop)).fill(clr);
      });
    }

    // Polling, not SSE (KI-013): subscribe() below already spends one of
    // Chromium's six same-origin connections on this page's live event
    // stream, and OBS's browser sources all share that one network stack. A
    // second EventSource here is how the room went blank for a week.
    // /music/now-playing set this precedent. 20s is well inside a 1m bar and
    // costs one short-lived fetch per screen per tick.
    const BARS_POLL_MS = 20000;
    async function pollBars() {
      if (!plateReady || !PLATE.screens) return;
      for (const screen of PLATE.screens) {
        if (screen.role !== "chart" || !screen.symbol) continue;
        try {
          const res = await fetch(`/bars/${screen.symbol}?interval=1m&limit=120`);
          if (!res.ok) { drawCandles(screen, []); continue; }
          const body = await res.json();
          drawCandles(screen, (body.data && body.data.bars) || []);
        } catch (err) {
          drawCandles(screen, []);   // dark glass, never a stale claim
        }
      }
    }

export { buildMonitorGraphics, BARS_POLL_MS, pollBars, barsThatFit, drawCandles, isDrawable, staleAlpha };
