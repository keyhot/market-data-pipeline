"""A shared visual identity so `/world` and the OBS overlays read as one product.

Two invariants carry the "calm that swells" register into pixels:

* ``MOOD_COLORS`` covers *every* mood the reactions registry can emit, so a new
  character mood can never render as an invisible/unstyled blob (the completeness
  test in ``tests/unit/test_visuals.py`` enforces it, mirroring the reactions
  registry invariant).
* ``tier_visuals`` is a **monotonic** calm->dramatic ramp keyed on
  ``world.state.severity_tier`` (0-3): tier 0 is quiet (near-neutral, no glow),
  tier 3 is dramatic (saturated, strong glow). That ramp is the *visual* half of
  the ambient-baseline-that-swells; the director's scene/line rails are the rest.

Procedural only — no external sprite/texture/font assets — to keep the CSP/SRI
policy intact and stay "start minimal, tasteful".
"""

from __future__ import annotations

# Dark ambient base. ``bg`` matches the overlays / TradingView dark theme so the
# chart, the world room, and the overlays sit on one continuous surface.
PALETTE: dict[str, str] = {
    "bg": "#131722",       # page surface (matches overlay_*.html)
    "fg": "#d1d4dc",       # primary text
    "up": "#26a69a",       # gains / positive outcomes
    "down": "#ef5350",     # losses / negative outcomes
    "neutral": "#787b86",  # the mood_color fallback — always renderable
    "muted": "#4a4e57",    # secondary chrome, dividers
    "accent": "#f0b90b",   # sparing highlight (a swell, never the baseline)
}

# Every mood in world.reactions (REACTIONS + _SIGNAL_OUTCOMES + _STREAK_DIRECTIONS)
# maps to a hex inside the palette family. Warm/orange = surprise & stress,
# green/teal = positive outcomes, red = losses, blue/grey/purple = the calm
# contemplative register. The completeness invariant is test-enforced.
MOOD_COLORS: dict[str, str] = {
    # positive / up
    "elated": "#26a69a",
    "relieved": "#4db6ac",
    "proud": "#66bb6a",
    "eager": "#26a69a",
    "decisive": "#42a5f5",
    # calm / contemplative
    "focused": "#5c6bc0",
    "attentive": "#7e8aa2",
    "speaking": "#b0bec5",
    "weighing": "#9575cd",
    "idle": "#546e7a",
    # surprise / alertness (warm)
    "surprised": "#ffca28",
    "startled": "#ffa726",
    "alert": "#ffb74d",
    "anxious": "#ff7043",
    "alarmed": "#ff5252",
    # loss / grief (red-brown)
    "resigned": "#8d6e63",
    "dejected": "#ef5350",
    "grim": "#c62828",
    # pressure moods from world.state._mood() — a *second* vocabulary the canvas
    # colours for symbol pillars and the model body (not in the reactions
    # registry, so not caught by the completeness invariant; kept here so this
    # map is the single source of truth for every mood the room can show).
    "calm": "#4a90d9",
    "bullish": "#26a69a",
    "bearish": "#ef5350",
    "panicked": "#f2a900",
}


def mood_color(mood: str) -> str:
    """Hex for ``mood``, falling back to the always-renderable neutral."""
    return MOOD_COLORS.get(mood, PALETTE["neutral"])


# One accent per speaking character, so a viewer learns "teal = the optimist"
# without reading the name every time (Sprint 14, B5). Each colour is drawn
# from that personality's own emotional register in MOOD_COLORS above, so the
# bubbles can't drift away from the palette the room already uses. Completeness
# against director.personalities is test-enforced, mirroring the reactions
# registry invariant.
CHARACTER_COLORS: dict[str, str] = {
    "optimist": MOOD_COLORS["relieved"],       # teal — the upside voice
    "statistician": MOOD_COLORS["focused"],    # indigo — the cool one
    "anxious": MOOD_COLORS["anxious"],         # warm orange — the worrier
}


def character_color(character: str) -> str:
    """Accent hex for a speaking character; unknown speakers stay renderable."""
    return CHARACTER_COLORS.get(character, PALETTE["fg"])


# Monotonic calm->dramatic ramp, indexed by severity tier 0-3. Every dimension
# rises with the tier so the escalation reads on every axis at once: bigger,
# brighter halo, more opaque, heavier text.
_TIER_RAMP: tuple[dict[str, float | int], ...] = (
    {"scale": 1.00, "glow": 0, "opacity": 0.70, "weight": 400},   # 0 calm
    {"scale": 1.05, "glow": 6, "opacity": 0.85, "weight": 500},   # 1
    {"scale": 1.12, "glow": 14, "opacity": 0.95, "weight": 600},  # 2
    {"scale": 1.22, "glow": 26, "opacity": 1.00, "weight": 700},  # 3 dramatic
)


def tier_visuals(tier: int) -> dict[str, float | int]:
    """Visual weight for a severity ``tier``; clamps out-of-range to [0, 3]."""
    clamped = max(0, min(int(tier), len(_TIER_RAMP) - 1))
    return dict(_TIER_RAMP[clamped])


def css_variables() -> str:
    """Render ``PALETTE`` as a ``:root{ --key: value; }`` block for one-line
    injection into every page (via the ``__THEME_VARS__`` template placeholder),
    so all three pages share one source of truth for colour."""
    lines = "\n".join(f"  --{key}: {value};" for key, value in PALETTE.items())
    return f":root {{\n{lines}\n}}"


def tier_styles_css(prefix: str = "tier") -> str:
    """CSS classes ``.{prefix}-0..3`` encoding the same calm->dramatic ramp as
    ``tier_visuals``, so a live DOM event swells by toggling one class — the
    server-rendered look and the SSE-delivered look come from one ramp. The
    ``transition`` makes escalate/decay *ambient* (a slow swell, never a flash);
    ``transform`` is compositor-only so scaling a feed row never reflows it."""
    rules = [
        f".{prefix} {{ transform-origin: left center; transition: transform "
        "0.6s ease, box-shadow 0.6s ease, opacity 0.6s ease; }"
    ]
    for tier, v in enumerate(_TIER_RAMP):
        glow = int(v["glow"])
        shadow = "none" if glow == 0 else f"0 0 {glow}px rgba(240, 185, 11, 0.5)"
        rules.append(
            f".{prefix}-{tier} {{ transform: scale({v['scale']}); "
            f"box-shadow: {shadow}; opacity: {v['opacity']}; "
            f"font-weight: {int(v['weight'])}; }}"
        )
    return "\n".join(rules)
