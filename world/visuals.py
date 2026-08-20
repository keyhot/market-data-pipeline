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
    "surface": "#1e222d",  # a panel *on* the page: feed rows, cards, dividers
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


# The room needs a ramp of its own. ``_TIER_RAMP`` above is a CSS font weight
# and a box-shadow radius — shaped for ``tier_styles_css`` and the DOM
# overlays, and meaningless to a canvas. Reading ``weight: 700`` as brightness
# would be worse than an honest number, so there are two ramps and they both
# live here: what must never happen is lighting constants loose in a template.
#
# Calm is dim, cool and closed in; dramatic is bright, warm and open. Monotonic
# in all three by test, because a tier-2 event reading calmer than a tier-1 one
# would invert the whole calm-that-swells register.
_ROOM_LIGHT_RAMP: tuple[dict[str, float], ...] = (
    {"vignette": 0.55, "warmth": 0.00, "lift": 0.00},   # 0 calm
    {"vignette": 0.44, "warmth": 0.10, "lift": 0.05},   # 1
    {"vignette": 0.30, "warmth": 0.26, "lift": 0.12},   # 2
    {"vignette": 0.16, "warmth": 0.45, "lift": 0.22},   # 3 dramatic
)


def room_light(tier: int) -> dict[str, float]:
    """Scene-wide lighting for a severity ``tier``.

    ``vignette`` is how closed-in the edges are, ``warmth`` how far the key
    light runs toward the warm end, and ``lift`` how much the whole room
    brightens. Clamps out-of-range to [0, 3], like ``tier_visuals``.
    """
    clamped = max(0, min(int(tier), len(_ROOM_LIGHT_RAMP) - 1))
    return dict(_ROOM_LIGHT_RAMP[clamped])


def max_tier_scale() -> float:
    """The biggest a row ever gets. A surface that scales its rows has to
    reserve room for the largest of them, and `transform` does not reflow —
    what does not fit is simply clipped (KI-031)."""
    return max(float(step["scale"]) for step in _TIER_RAMP)


def css_variables() -> str:
    """Render ``PALETTE`` as a ``:root{ --key: value; }`` block for one-line
    injection into every page (via the ``__THEME_VARS__`` template placeholder),
    so all three pages share one source of truth for colour.

    ``--tier-max-scale`` rides along so a page can size against the swell
    ceiling in plain CSS rather than hard-coding a number that would drift from
    ``_TIER_RAMP`` the first time the ramp is retuned."""
    lines = "\n".join(f"  --{key}: {value};" for key, value in PALETTE.items())
    lines += f"\n  --tier-max-scale: {max_tier_scale()};"
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


# --- The cast has to be visible (KI-028) ----------------------------------
#
# Measured on the live program frame, the silhouettes sat at 1.19:1 (trader) and
# 1.00:1 (model) against their own background, versus WCAG's 3:1 floor for
# non-text graphics. Only the face marks read; the bodies were the same
# luminance as the room. The B2 lighting work pulled the room this dark and the
# characters were never re-tested against it.
#
# The arithmetic says it was never fixable by choosing better mood colours.
# PixiJS tint MULTIPLIES: a body renders at `base x tint / 255`. With the old
# base fill of 0x545862 the brightest body obtainable — a pure white tint —
# is 0x545862 itself, which measures 2.51:1. The ceiling was below the floor.
# So the base fill is the thing that had to move, and it lives here now rather
# than as a literal repeated through five body-drawing functions in a template.
BODY_BASE_FILL = 0xD0D0D0
# The rim is drawn brighter than the fill so the difference SURVIVES tinting
# (both get multiplied by the same tint, so the ratio between them is fixed
# here and nowhere else). A lit edge is also what a 2200 kbps VBR encoder
# preserves best: near-black gradients are what it spends the fewest bits on,
# which is why the cast read worse on air than in a screenshot.
BODY_RIM_FILL = 0xFFFFFF

# The floor is set well above WCAG's 3:1 on purpose. This ratio is computed
# against the flat page background, but on air the vignette darkens the cast and
# the room together — and because the WCAG formula adds 0.05 to both terms,
# darkening two colours equally REDUCES their ratio. A body computed at exactly
# 3.0 here measures below 3.0 in the frame. 4.5 is the margin that survives the
# vignette, the wall gradient and the encoder.
SILHOUETTE_MIN_CONTRAST = 4.5


def _linearize(channel: float) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG relative luminance of an sRGB triple."""
    r, g, b = (_linearize(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two sRGB triples. Symmetric."""
    high, low = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _to_rgb(value: str | int) -> tuple[int, int, int]:
    if isinstance(value, str):
        value = int(value.lstrip("#"), 16)
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c))) for c in rgb))


def _rendered(tint: tuple[int, int, int]) -> tuple[int, int, int]:
    """What the canvas actually shows: the base fill multiplied by the tint."""
    base = _to_rgb(BODY_BASE_FILL)
    return tuple(base[i] * tint[i] / 255 for i in range(3))


def _lerp_to_floor(start, end, background, floor):
    """Least blend from ``start`` toward ``end`` whose RENDERED body clears the
    floor. Returns ``end`` if even that cannot (it always can — end is white).

    Bisection rather than a closed form because luminance is non-linear in the
    blend parameter, and 24 steps lands well inside a single 8-bit level.
    """
    def at(t):
        # Quantized to 8 bits INSIDE the search, not after it. Bisecting on
        # continuous colour and rounding afterwards lands a hair under the floor
        # — every mood came out at 4.485-4.497 against a floor of 4.5. What ships
        # is an integer triple, so that is what has to be measured.
        return tuple(
            max(0, min(255, round(start[i] + (end[i] - start[i]) * t)))
            for i in range(3)
        )

    def clears(t):
        return contrast_ratio(_rendered(at(t)), background) >= floor

    if clears(0.0):
        return at(0.0)
    if not clears(1.0):
        return at(1.0)
    low, high = 0.0, 1.0
    for _ in range(24):
        mid = (low + high) / 2
        if clears(mid):
            high = mid
        else:
            low = mid
    return at(high)


def body_tint(mood: str, background: str | None = None) -> str:
    """The tint that renders ``mood``'s body at or above the contrast floor.

    Hue is given up last. First the colour is taken to full chroma (scaled so
    its brightest channel peaks), which costs nothing but darkness; only if that
    still falls short is it mixed toward white, and only as far as it must go. A
    dim mood therefore stays recognisably itself — it just stops being invisible.
    """
    bg = _to_rgb(background or PALETTE["bg"])
    mood_rgb = _to_rgb(mood_color(mood))
    peak = max(mood_rgb) or 1
    full_chroma = tuple(min(255, c * 255 / peak) for c in mood_rgb)
    lifted = _lerp_to_floor(mood_rgb, full_chroma, bg, SILHOUETTE_MIN_CONTRAST)
    if contrast_ratio(_rendered(lifted), bg) < SILHOUETTE_MIN_CONTRAST:
        lifted = _lerp_to_floor(
            full_chroma, (255, 255, 255), bg, SILHOUETTE_MIN_CONTRAST
        )
    return _to_hex(lifted)


def body_tints(background: str | None = None) -> dict[str, str]:
    """Every mood's body tint, precomputed. Rendered into the page as JSON so
    the template does no colour maths of its own — the same reason the tier and
    room-light ramps live here. Completeness over MOOD_COLORS is test-enforced,
    mirroring the reactions-registry invariant."""
    return {mood: body_tint(mood, background) for mood in MOOD_COLORS}


def body_contrast(mood: str, background: str | None = None) -> float:
    """Measured contrast of ``mood``'s rendered body against the room. The
    number the invariant test asserts on, and the number a screenshot is
    compared back to."""
    bg = _to_rgb(background or PALETTE["bg"])
    return contrast_ratio(_rendered(_to_rgb(body_tint(mood, background))), bg)
