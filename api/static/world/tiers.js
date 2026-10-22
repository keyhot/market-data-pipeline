// /world — The 0..3 tier scale (cuts from the config) and the pixel grid (CELL/snap).
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

import "./shading.js";
import { WORLD_CONFIG, PLATE } from "./config.js";

    // Shared severity -> tier bands (mirror the overlay + world/state tiers) so
    // the "swell" reads the same on every surface: tier 0 barely stirs, tier 3
    // leaps. This is the calm-that-swells register in motion.
    // The tier scale is the server's, injected — never restated here. Severity
    // is rule-specific: 1.6 is tier 2 for a `signal_resolved` and tier 0 for a
    // `big_move`. An absolute 2/5/8 scale in this file quietly pinned the most
    // frequent event in the world to tier 0, so the room never swelled for it.
    const TIER_CUTS = WORLD_CONFIG.tier_cuts;
    // The rule that reads them is page code, not generated code. It is the
    // same three lines `world.state.severity_tier` computes (the overlay
    // still gets them from `tier_of_js`), and a node test runs THIS function
    // against `severity_tier` for every rule, so the copies cannot drift.
    function tierOf(eventType, severity) {
      const cuts = TIER_CUTS.cuts[eventType] ?? TIER_CUTS.generic;
      return cuts.reduce((tier, cut) => tier + (severity >= cut ? 1 : 0), 0);
    }

    // 4px cells at 1920x960. Every coordinate the cast draws or moves to is
    // quantised here, so motion reads as pixel STEPS rather than smooth
    // slides — the one drawing rule the whole cast obeys. Falls back to a
    // literal 4 when there is no plate (PLATE is null): CELL must not throw
    // on the no-manifest path any more than anchorFor does. Top-level scope,
    // not inside boot(), so playAnimation, the cast helpers, and (Tasks 7/8)
    // drawCandles/applyGlow can all reach it.
    const CELL = (PLATE && PLATE.cell) || 4;
    function snap(v) { return Math.round(v / CELL) * CELL; }

export { TIER_CUTS, tierOf, CELL, snap };
