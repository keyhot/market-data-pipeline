"""A shared visual identity so /world and the overlays read as one product.
The mood-colour registry invariant keeps every reaction mood renderable (never
an invisible blob); the tier ramp encodes the calm->dramatic 'swell'."""

from world.reactions import _SIGNAL_OUTCOMES, _STREAK_DIRECTIONS, REACTIONS
from world.visuals import (
    MOOD_COLORS,
    PALETTE,
    css_variables,
    mood_color,
    tier_styles_css,
    tier_visuals,
)


def _all_moods():
    moods = {mood for mood, _anim in REACTIONS.values()}
    moods |= {m for m, _ in _SIGNAL_OUTCOMES.values()}
    moods |= {m for m, _ in _STREAK_DIRECTIONS.values()}
    return moods


def test_every_reaction_mood_has_a_colour():
    missing = sorted(_all_moods() - set(MOOD_COLORS))
    assert missing == [], f"moods with no colour: {missing}"


def test_mood_color_falls_back_for_unknown():
    assert mood_color("no-such-mood") == PALETTE["neutral"]
    assert mood_color(next(iter(MOOD_COLORS))).startswith("#")


def test_tier_visuals_escalate_monotonically():
    glows = [tier_visuals(t)["glow"] for t in range(4)]
    scales = [tier_visuals(t)["scale"] for t in range(4)]
    assert glows == sorted(glows) and glows[0] < glows[3]  # calm -> dramatic
    assert scales[0] < scales[3]


def test_tier_visuals_clamps_out_of_range():
    assert tier_visuals(99) == tier_visuals(3)
    assert tier_visuals(-1) == tier_visuals(0)


def test_palette_has_required_keys():
    for key in ("bg", "fg", "up", "down", "neutral", "muted"):
        assert PALETTE[key].startswith("#")


def test_css_variables_render_every_palette_key():
    css = css_variables()
    for key, value in PALETTE.items():
        assert f"--{key}: {value}" in css


def test_mood_colors_covers_state_pressure_moods():
    # world.state._mood() emits a *second* vocabulary the canvas colours for
    # symbol pillars / the model body; it isn't in the reactions registry, so
    # only this test stops the canvas map regressing them to grey.
    for mood in ("calm", "bullish", "bearish", "panicked"):
        assert MOOD_COLORS[mood].startswith("#")


def test_tier_styles_css_defines_a_class_per_tier_with_a_transition():
    css = tier_styles_css()
    for tier in range(4):
        assert f".tier-{tier}" in css
    assert "transition:" in css  # the swell must ease, never flash


def test_every_personality_has_a_character_colour():
    """B5: a speaking character with no accent renders in the default text
    colour and becomes indistinguishable from the others on stream. Registry
    invariant, same as the reactions one — adding a personality without a
    colour fails here rather than showing up as a grey bubble at 3am."""
    from director.personalities import PERSONALITIES
    from world.visuals import CHARACTER_COLORS

    speakers = {p.phrase_character for p in PERSONALITIES}
    missing = sorted(speakers - set(CHARACTER_COLORS))
    assert missing == [], f"personalities with no character colour: {missing}"


def test_character_colours_are_distinguishable_from_each_other():
    from world.visuals import CHARACTER_COLORS

    assert len(set(CHARACTER_COLORS.values())) == len(CHARACTER_COLORS)


def test_character_color_falls_back_for_unknown_speaker():
    from world.visuals import character_color

    assert character_color("nobody").startswith("#")


# --- B2: scene-wide room lighting (a canvas ramp, not the DOM one) -----------


def test_room_light_ramps_monotonically_from_calm_to_dramatic():
    """Calm is dim, closed-in and cool; dramatic is bright, open and warm. The
    ramp has to be monotonic in all three or a tier-2 event would read *calmer*
    than a tier-1 one — the swell has to go one way."""
    from world.visuals import room_light

    steps = [room_light(tier) for tier in range(4)]
    vignettes = [s["vignette"] for s in steps]
    assert vignettes == sorted(vignettes, reverse=True), vignettes
    for key in ("warmth", "lift"):
        values = [s[key] for s in steps]
        assert values == sorted(values), (key, values)
        assert len(set(values)) == len(values), f"{key} has a flat step"


def test_room_light_clamps_out_of_range_tiers():
    from world.visuals import room_light

    assert room_light(-3) == room_light(0)
    assert room_light(99) == room_light(3)


def test_room_light_is_its_own_ramp_not_the_dom_one():
    """`tier_visuals` is {scale, glow, opacity, weight} — a CSS font weight and
    a box-shadow radius, shaped for `tier_styles_css`. PIXI has no use for
    those, and reading `weight: 700` as brightness would be worse than an
    honest number. One ramp per surface, both of them here."""
    from world.visuals import room_light, tier_visuals

    assert set(room_light(0)) & set(tier_visuals(0)) == set()
