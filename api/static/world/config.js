// /world — The server's ONE config block (#41), parsed once, and the constants read
// straight off it; the banner's symbol prefix.
// Split out of api/templates/world.html's inline script (#42); declarations
// unchanged. Imports its predecessor first to keep the original evaluation
// order (see main.js).

    // Everything the server decides reaches this page in ONE config block
    // (the JSON element above, rendered by api/main.py and escaped for a
    // script context), parsed once. Each const below reads one key of it and
    // carries no default of its own: a missing plate arrives as null, and
    // a missing key is a test failure, not a quiet fallback.
    const WORLD_CONFIG = JSON.parse(document.getElementById("world-config").textContent);
    const SYMBOLS = WORLD_CONFIG.symbols;
    // The plate's measured anchors, or null when there is no plate. Every
    // layout number below reads from here rather than from canvas fractions.
    // Same-origin by construction (KI-045/KI-047): the manifest names a bare
    // filename and the page prefixes /static/ itself, so no manifest edit can
    // put a third party into the on-air load path.
    const PLATE = WORLD_CONFIG.plate;
    // The cast, already sorted into people and settings by world/plate.py's
    // `cast_payload()` — `{ characters: {...}, scale }`, or null with no plate.
    // Indexing `PLATE.cast` here instead would find `scale` for a name and
    // build an anchor out of `undefined` (KI-076); pinned by
    // test_the_page_never_indexes_the_raw_cast_block.
    const PAINTED_CAST = WORLD_CONFIG.painted_cast;
    const PLATE_SRC = PLATE ? "/static/" + PLATE.plate : null;
    // Sprint 16 Task 10: where text is allowed to sit, already resolved to
    // absolute canvas coordinates by `world/text_layout.py::as_json` — read
    // THIS, never `PLATE.text`/`PLATE.text_surfaces` (Task 9's own surface
    // union is inside `PLATE` too, but re-deriving band-top/band-bottom from
    // it here would restate `_band_surfaces`' arithmetic in a second
    // language). A placement whose surface could not be resolved is already
    // dropped by `as_json` before this page ever sees it, so a missing key
    // here just means "nothing is drawn there" — never a throw.
    const TEXT_PLACEMENTS = WORLD_CONFIG.text;
    // The painted key light (the amber desk lamp): direction, warmth and
    // ramp measured off the plate, or the neutral default when unmeasured.
    // Distinct from ROOM_LIGHT below, which is the severity-tier ambient
    // swell — this one shapes the cast, via shade()/paint() below.
    //
    // AUTHORITATIVE OVER `PLATE.light`. `PLATE` (above) is the raw manifest,
    // substituted verbatim as `manifest.as_dict()`, so `PLATE.light` is
    // whatever the JSON file says, unvalidated. `LIGHT` is
    // `light_for(manifest)`'s output — the same measurement, but only after
    // it has passed the shape check, the monotonic-ramp check, and the
    // missing-manifest/missing-block degrade, all of which land on
    // `DEFAULT_LIGHT` rather than a bad number reaching a draw call
    // (`world/light.py`). Reaching for `PLATE.light` instead of `LIGHT`
    // silently steps around that whole contract. Every draw call below reads
    // `LIGHT`; nothing in this file should ever read `PLATE.light`.
    const LIGHT = WORLD_CONFIG.light;
    const fallback = document.getElementById("fallback");

    // Name the room for a newcomer: the symbols in play + the one-line premise.
    document.getElementById("banner-syms").textContent =
      SYMBOLS.map((s) => s.replace(/USDT$/, "")).join(" · ") + " — ";

export {
  WORLD_CONFIG, SYMBOLS, PLATE, PAINTED_CAST, PLATE_SRC, TEXT_PLACEMENTS,
  LIGHT, fallback,
};
