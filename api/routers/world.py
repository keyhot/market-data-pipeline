"""The Living World's own surfaces: its JSON (`/world/events`, `/world/state`)
and its pages (`/world`, the OBS overlays, `/standby`) (#44, moved from
`api/main.py`)."""

import html
import json
import re

import pandas as pd
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from api.shared import _SYMBOL_PATTERN, _validated_symbol, logger
from api.templating import _TEMPLATES_DIR, _render_template
from config.exceptions import BaseAppException, NoDataFoundError
from scheduler.watchlist import load_watchlist
from schemas.responses import ApiResponse
from storage.postgres_store import (
    get_model_accuracy,
    get_price_bars,
    get_world_events,
)
from world import light, monitors, reactions, text_layout, visuals
from world.plate import glow_chart_overlaps, load_manifest, watchlist_disagreements
from world.reactions import attach_reactions
from world.salience import KNOWN_EVENT_TYPES
from world.state import GENERIC_TIER_CUTS, project_state, tier_cuts

router = APIRouter()

# KI-053 (#25): the banner names the markets in words a stranger already
# knows. "BTC · ETH" read as code; "Bitcoin & Ethereum" is the noun the
# headline needs. A symbol with no entry falls back to its bare ticker, so a
# new watchlist symbol is still named, just less kindly.
_SYMBOL_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum"}


def _banner_prefix(symbols: list[str]) -> str:
    """The banner's lead-in (`#banner-syms`): the markets in play, by name,
    and that they are live. ONE definition: the page's config key and the
    glyph-fit check in `_plate_warnings` both read this, never a restatement."""
    names = [
        _SYMBOL_NAMES.get(s.removesuffix("USDT"), s.removesuffix("USDT"))
        for s in symbols
    ]
    if not names:
        return "Live"
    if len(names) == 1:
        return f"{names[0]}, live"
    return ", ".join(names[:-1]) + " & " + names[-1] + ", live"


def _world_config(symbols: list[str], manifest) -> dict:
    """Everything `/world`'s script reads, as ONE dict (#41).

    It replaces about twenty text placeholders, each spliced into JavaScript as
    a literal (two of them as generated code). Values that were JS hex literals
    — `0xd0d0d0` — are numbers here, so `JSON.parse` hands the page the same
    thing the literal did. A missing plate stays `null`, never absent: the page
    carries no defaults of its own, so the procedural room is chosen HERE.
    """
    return {
        "symbols": symbols,
        "banner_prefix": _banner_prefix(symbols),
        "plate": manifest.as_dict() if manifest else None,
        # The cast, already sorted into people and settings (`cast_payload`).
        "painted_cast": manifest.cast_payload() if manifest else None,
        "light": json.loads(light.as_json(light.light_for(manifest))),
        "text": json.loads(text_layout.as_json(manifest)),
        # The mood -> colour map the canvas reads.
        "mood_colors": visuals.MOOD_COLORS,
        # KI-028: the cast measured 1.19:1 and 1.00:1 against its own background.
        # Body colour is not the mood colour — PixiJS tint MULTIPLIES the base
        # fill — so the page cannot work it out from `mood_colors` alone.
        # Precomputed per mood here, where the contrast maths and the base fill
        # live together.
        "body_tints": visuals.body_tints(),
        # KI-060: `character()` builds every figure at mood "neutral", which
        # MOOD_COLORS deliberately does not claim. Both neutrals come from here:
        # the identity grey for chrome, and the same grey lifted for anything
        # that gets painted as a body.
        "neutral_color": int(visuals.PALETTE["neutral"][1:], 16),
        "neutral_tint": int(visuals.neutral_body_tint()[1:], 16),
        "body_base_fill": visuals.BODY_BASE_FILL,
        "body_rim_fill": visuals.BODY_RIM_FILL,
        # B2: scene-wide lighting per tier. The canvas gets a ramp shaped for a
        # canvas — see `room_light`.
        "room_light": [visuals.room_light(tier) for tier in range(4)],
        "tier_cuts": {
            "cuts": {k: list(v) for k, v in tier_cuts().items()},
            "generic": list(GENERIC_TIER_CUTS),
        },
        # B5: one accent per speaking character, so a speech bubble can never
        # drift from the room's colours.
        "character_colors": visuals.CHARACTER_COLORS,
        # B1: the reaction registry itself reaches the canvas, so "what
        # happened" maps to a face and a named animation from ONE source of
        # truth. The renderer must not re-invent this mapping.
        "reactions": {
            "base": {
                event_type: {"mood": mood, "animation": animation}
                for event_type, (mood, animation) in reactions.REACTIONS.items()
            },
            "outcomes": {
                outcome: {"mood": mood, "animation": animation}
                for outcome, (mood, animation) in reactions._SIGNAL_OUTCOMES.items()
            },
            "directions": {
                direction: {"mood": mood, "animation": animation}
                for direction, (mood, animation) in reactions._STREAK_DIRECTIONS.items()
            },
            "fallback": {
                "mood": reactions._FALLBACK[0],
                "animation": reactions._FALLBACK[1],
            },
        },
        # M1: the monitors' decisions are made in Python and the numbers
        # injected, like every other threshold the room reads. The rule
        # functions that read them are page code, run against `world.monitors`
        # in node by `test_monitors.py`, so the page and the server cannot
        # disagree about how many candles fit or when a screen is too old.
        "monitor_rules": monitors.monitor_rules(),
    }


_WORLD_CONFIG_SLOT = "__WORLD_CONFIG__"


def _json_for_script(value) -> str:
    """JSON that is safe as the text of a `<script>` element.

    Script text is not entity-decoded, so `html.escape` would corrupt it; what
    ends a script element (or opens a comment inside one) is `<`, so `<`, `>`
    and `&` become JSON `\\u` escapes, which `JSON.parse` reads back as the
    same characters. `ensure_ascii` already writes everything non-ASCII,
    U+2028/2029 included, as an escape. `allow_nan=False` fails here, at the
    server, rather than as a `JSON.parse` error on a page that already
    returned 200.
    """
    return (
        json.dumps(value, allow_nan=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _render_world_page(config: dict) -> str:
    html = _render_template("world.html", {})
    if html.count(_WORLD_CONFIG_SLOT) != 1:
        raise RuntimeError("world.html has no single slot for the JSON config block")
    # Substituted LAST and in one `replace`, so a value that happens to spell a
    # placeholder is data, not a slot: nothing re-scans what was just inserted.
    return html.replace(_WORLD_CONFIG_SLOT, _json_for_script(config))


@router.get("/world/events", response_model=ApiResponse)
def world_events(
    limit: int = Query(50, ge=1, le=500),
    event_type: str | None = None,
    symbol: str | None = None,
    since: str | None = None,
):
    if event_type is not None and event_type not in KNOWN_EVENT_TYPES:
        raise BaseAppException(
            f"Unknown event type: {event_type!r}", status_code=400
        )
    validated_symbol = _validated_symbol(symbol) if symbol else None
    since_dt = None
    if since is not None:
        try:
            since_dt = pd.Timestamp(since).to_pydatetime()
        except ValueError:
            raise BaseAppException(f"Invalid since: {since!r}", status_code=400)
    try:
        events = get_world_events(
            limit=limit,
            event_type=event_type,
            symbol=validated_symbol,
            since=since_dt,
        )
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)
    if not events:
        raise NoDataFoundError("No world events for the given parameters")
    return ApiResponse(
        status=200, data={"count": len(events), "events": events}
    )


@router.get("/world/state", response_model=ApiResponse)
def world_state(limit: int = Query(500, ge=1, le=2000)):
    """The world_events log folded into what the room currently shows.
    The renderer computes nothing — it draws this."""
    try:
        events = get_world_events(limit=limit)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)
    if not events:
        raise NoDataFoundError("No world events recorded yet")
    state = attach_reactions(project_state(events))
    # KI-012: the model's on-screen accuracy comes from the SAME last-N-resolved
    # basis as the overlay strips (not the event-window fold), so the room and the
    # strips agree. Enrichment — never let it break the state.
    predict_symbols: list[str] = []
    try:
        predict_symbols = [
            spec.symbol.upper()
            for spec in load_watchlist().tickers
            if spec.predict and _SYMBOL_PATTERN.fullmatch(spec.symbol.upper())
        ]
        state["model"]["accuracy"] = get_model_accuracy(predict_symbols)
    except Exception:
        logger.warning("Could not attach model accuracy to /world/state")
    # B9: the always-on now-band needs a price. It rides along here rather than
    # the page fetching /bars per symbol — every extra request is another
    # long-lived connection in a browser process that already exhausted its
    # per-origin limit once (KI-013). Enrichment: a failure costs one line in
    # the band, never the room.
    prices: dict[str, float] = {}
    try:
        for symbol in predict_symbols:
            bars = get_price_bars(symbol, interval="1m", limit=1)
            if bars:
                prices[symbol] = bars[-1]["close"]
    except Exception:
        logger.warning("Could not attach prices to /world/state")
    state["prices"] = prices
    return ApiResponse(status=200, data=state)


@router.get("/overlay/signals", response_class=HTMLResponse)
def overlay_signals():
    """OBS Browser Source strip (~1920x120): live signals + track record."""
    symbols = [
        spec.symbol.upper()
        for spec in load_watchlist().tickers
        if spec.predict and _SYMBOL_PATTERN.fullmatch(spec.symbol.upper())
    ]
    deduped = list(dict.fromkeys(symbols))
    return HTMLResponse(
        _render_template("overlay_signals.html", {"__SYMBOLS__": json.dumps(deduped)})
    )


@router.get("/overlay/events", response_class=HTMLResponse)
def overlay_events():
    """OBS Browser Source feed (~480x1080): latest salient world events."""
    return HTMLResponse(_render_template("overlay_events.html", {}))


# B10. What a viewer sees instead of a frozen or black frame while the watchdog
# brings the stream back. Copy only — the card is otherwise identical in every
# state, so a switch between them never reads as a different channel.
_STANDBY_COPY = {
    "reconnecting": (
        "Reconnecting",
        "The stream dropped and is coming back on its own",
    ),
    "starting": ("Starting soon", "Warming up the market feeds"),
    "shortly": ("Back shortly", "Taking a moment to sort something out"),
}
_STANDBY_DEFAULT = "reconnecting"


@router.get("/standby", response_class=HTMLResponse)
def standby_page(state: str = _STANDBY_DEFAULT):
    """Procedural standby card for the `standby` OBS scene (B10).

    Unknown states fall back rather than erroring: this page is the surface
    shown *because* something already went wrong, so it must never be the thing
    that breaks. The state is escaped, never trusted — it reaches the DOM as
    text through a template substitution, the same rule as every other page.
    """
    headline, subline = _STANDBY_COPY.get(state, _STANDBY_COPY[_STANDBY_DEFAULT])
    if state not in _STANDBY_COPY:
        logger.warning("Unknown standby state requested", extra={"state": state})
    return HTMLResponse(
        _render_template(
            "standby.html",
            {"__HEADLINE__": html.escape(headline),
             "__SUBLINE__": html.escape(subline)},
        )
    )


def _banner_copy(symbols: list[str]) -> str:
    """The line `#banner` draws: the page's own symbol prefix (`banner-syms`,
    filled in world.html) before the template's own premise sentence — read
    from the template, never restated here where it could drift."""
    html = (_TEMPLATES_DIR / "world.html").read_text()
    match = re.search(r'<div id="banner">.*?<span class="tag">(.*?)</span>', html, re.S)
    premise = " ".join(match.group(1).split()) if match else ""
    return f"{_banner_prefix(symbols)} — {premise}"


def _plate_warnings(manifest, symbols: list[str]) -> list[str]:
    """Every problem the manifest validators find, for `/world` to log (KI-074).

    CI runs these over the shipped manifest; this runs them over the one
    being served, because editing that file in a deployment is the intended
    workflow and a bad edit mis-renders on air with no other log line.
    """
    problems = [
        f"plate manifest disagrees with watchlist: {p}"
        for p in watchlist_disagreements(manifest, symbols)
    ]
    problems += [
        (
            f"plate glow {g} overlaps a live chart screen; "
            "the candles turn olive at high tier (KI-056)"
        )
        for g in glow_chart_overlaps(manifest)
    ]
    problems += [
        f"text placement {t} escapes its surface (or names none) and will not be drawn"
        for t in text_layout.floating_text(manifest)
    ]
    for placement in (getattr(manifest, "text", ()) or ()):
        if placement.get("id") == "banner" and "w" in placement and "h" in placement:
            copy = _banner_copy(symbols)
            if text_layout.glyph_overflow(copy, placement.get("size", 14), placement):
                problems.append(
                    f"text placement banner: {copy!r} does not fit its "
                    f"{placement['w']}x{placement['h']} box"
                )
    return problems


@router.get("/world", response_class=HTMLResponse)
def world_page():
    """The Living World room — a Browser Source for the world-focus scene."""
    symbols = [
        spec.symbol.upper()
        for spec in load_watchlist().tickers
        if spec.market == "crypto" and _SYMBOL_PATTERN.fullmatch(spec.symbol.upper())
    ]
    deduped = list(dict.fromkeys(symbols))
    manifest = load_manifest()
    # A manifest that disagrees with the watchlist is a warning, never a silent
    # mis-render: the plate paints tube bases at fixed positions, so a symbol
    # with no painted base has nowhere to stand and would simply not be drawn.
    # Absence of the manifest itself is NOT a disagreement — it is the
    # procedural room, which is a supported way to run (Docs/world-room-plate).
    # KI-074: the other manifest validators run here too, for the same reason.
    for problem in _plate_warnings(manifest, deduped):
        logger.warning("%s", problem)
    return HTMLResponse(_render_world_page(_world_config(deduped, manifest)))
