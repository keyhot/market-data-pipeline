"""The canvas itself isn't unit-testable, so these tests pin the things that
silently break a 24/7 browser source: substitution, the SRI pin, and the
textContent-only rule that keeps event payloads from becoming markup."""

import re

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_world_page_renders():
    response = client.get("/world")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_no_unsubstituted_placeholders_remain():
    body = client.get("/world").text
    assert "__SYMBOLS__" not in body
    assert not re.search(r"__[A-Z_]+__", body)


def test_watchlist_symbols_are_embedded():
    body = client.get("/world").text
    assert "BTCUSDT" in body


def test_pixi_is_pinned_with_integrity():
    body = client.get("/world").text
    assert 'pixi.js@8.19.0' in body
    assert 'integrity="sha384-' in body
    assert 'crossorigin="anonymous"' in body


def test_page_uses_textcontent_not_innerhtml():
    body = client.get("/world").text
    assert "innerHTML" not in body


def test_page_consumes_state_and_sse_endpoints():
    body = client.get("/world").text
    assert "/world/state" in body
    assert "/stream/world/events" in body


def test_shared_theme_vars_are_injected():
    body = client.get("/world").text
    assert "--bg: #131722" in body        # css_variables() reached the page
    assert '"calm": "#4a90d9"' in body     # MOOD_COLORS superset (pressure mood)


def test_newcomer_banner_is_present():
    body = client.get("/world").text
    assert "trades live, and the world remembers when it's wrong" in body


def test_live_event_swell_keys_on_tier():
    # The swell must fire on live SSE events, not just the initial paint: react()
    # scales the nudge by the event's tier. Pin the hook so a refactor can't
    # silently flatten the room back to a constant nudge.
    body = client.get("/world").text
    assert "tierOf(event.severity)" in body


# --- B5: on-screen commentary (the stream is silent, so lines must be READ) ---


def test_commentary_renders_as_on_screen_speech():
    """TTS is a deferred ops step, so `commentary_spoken` is currently invisible
    to a viewer — the personalities don't exist on screen at all. The SSE path
    must route those events to a bubble, not just to the room's nudge."""
    body = client.get("/world").text
    assert "commentary_spoken" in body
    assert "speak(" in body or "renderBubble(" in body


def test_speech_bubbles_use_the_shared_character_palette():
    from world.visuals import CHARACTER_COLORS

    body = client.get("/world").text
    assert "__CHARACTER_COLORS_JSON__" not in body     # substituted
    for hex_colour in CHARACTER_COLORS.values():
        assert hex_colour in body


def test_speech_bubble_text_is_set_as_textcontent():
    """Commentary payloads are data and reach the page over SSE; innerHTML
    would make a crafted payload markup on a 24/7 public stream."""
    body = client.get("/world").text
    assert "innerHTML" not in body
    assert "textContent" in body


# --- B9: always-on now-band (read the whole state in ~2s, no event needed) ---


def test_now_band_is_present_and_always_on():
    """A newcomer arriving during a calm stretch sees no events at all. The
    band is the answer: price, the model's record, and a one-word mood, on
    screen permanently rather than only when something fires."""
    body = client.get("/world").text
    assert 'id="nowband"' in body
    assert "renderNowBand(" in body


def test_now_band_reads_prices_and_accuracy_from_state():
    body = client.get("/world").text
    assert "state.prices" in body
    assert "accuracy" in body


def test_now_band_has_no_placeholder_or_markup_leak():
    body = client.get("/world").text
    assert not re.search(r"__[A-Z_]+__", body)
    assert "innerHTML" not in body


# --- B6: continuous ambient (calm must still move) ---


def test_ambient_runs_every_frame_not_only_on_events():
    """A 55-minute calm stretch used to be a near-frozen frame — the room only
    moved when an event fired. The ambient loop rides the PIXI ticker so calm
    reads alive-but-quiet."""
    body = client.get("/world").text
    assert "app.ticker.add(" in body
    assert "startAmbient(" in body


def test_ambient_amplitude_is_driven_by_market_state():
    """Motion has to be *data*, not decoration: pressure sets the colour
    temperature and agitation sets the drift, so what a viewer sees moving is
    the market moving."""
    body = client.get("/world").text
    assert "pressure" in body and "agitation" in body


# --- B1: procedural characters, faces, and the named animations made real ---


def test_every_registry_animation_is_implemented_by_the_renderer():
    """reactions.py has named animations since Sprint 12 (jolt/shake/hop/…),
    but they were only strings — the room nudged everything identically. Each
    name now has a behaviour, and this is the invariant that keeps it true: a
    new reaction whose animation isn't implemented would stand still on a
    stream nobody is watching at 3am."""
    from world.reactions import ANIMATIONS

    body = client.get("/world").text
    missing = sorted(a for a in ANIMATIONS if not re.search(rf"\b{a}:\s*\(c", body))
    assert missing == [], f"animations with no renderer implementation: {missing}"


def test_every_registry_mood_has_a_face():
    """Same invariant one layer over: a mood with no expression is a blank
    stare, and the fallback should be a deliberate choice, not an accident."""
    from world.reactions import MOODS

    body = client.get("/world").text
    missing = sorted(m for m in MOODS if not re.search(rf"\b{m}:\s*\{{", body))
    assert missing == [], f"moods with no face: {missing}"


def test_reaction_registry_is_injected_rather_than_reinvented():
    """The canvas must not keep its own copy of what an event means — drift
    between the room and the overlays is exactly what world/visuals.py exists
    to prevent."""
    body = client.get("/world").text
    assert "__REACTIONS_JSON__" not in body
    assert '"signal_resolved"' in body and '"model_losing_streak"' in body
    assert "reactionFor(" in body


def test_characters_have_a_face_not_just_a_tinted_circle():
    body = client.get("/world").text
    for part in ("eyeL", "eyeR", "mouth", "browL", "setExpression("):
        assert part in body, part


def test_every_offered_style_has_a_body_builder():
    """The styles list is what the gallery renders and what ?style= accepts —
    an entry with no builder would silently fall back and quietly misrepresent
    the option being evaluated."""
    body = client.get("/world").text
    styles = re.search(r"const STYLES = \[(.*?)\]", body, re.S).group(1)
    names = re.findall(r'"([a-z]+)"', styles)
    assert len(names) >= 3, names
    for name in names:
        assert re.search(rf"\b{name}\(body", body), f"{name} has no body builder"


def test_limbless_bodies_still_have_a_gesture():
    """These bodies are market glyphs, not anatomy, so the arm-driven
    animations (shrug/wave/cheer) need something else to move — otherwise
    three of the sixteen would render as nothing at all."""
    body = client.get("/world").text
    assert "function gesture(" in body
    assert "accents" in body


def test_prototype_gallery_is_available_for_evaluation():
    """B1 ships options, not a final look — the gallery is the human-eval gate."""
    body = client.get("/world").text
    assert "drawGallery(" in body
    assert "gallery" in body and "STYLES" in body


def test_model_and_trader_are_cast_with_different_bodies():
    """The B1 pick: the model is a machine built from the market's own glyphs,
    the trader is a person. Casting them both from one style would throw that
    contrast away — and it is the contrast that lets a newcomer tell which of
    the two just reacted, before reading a single label."""
    body = client.get("/world").text
    cast = re.search(r"const CAST = \{(.*?)\}", body, re.S).group(1)
    assigned = dict(re.findall(r'(\w+):\s*"(\w+)"', cast))
    assert assigned == {"MODEL": "bars", "TRADER": "figure"}, assigned
    assert len(set(assigned.values())) == len(assigned), "the cast shares a body"
    for style in assigned.values():
        assert re.search(rf"\b{style}\(body", body), f"{style} has no body builder"


def test_the_figures_arms_read_apart_from_its_torso():
    """v1's figure was dropped partly because the arms were the same tint as the
    body: they only existed once shrug/wave/cheer fired, and a resting trader
    read as a capsule. Shading them apart is what makes the arms visible at
    rest, which is 99% of the airtime."""
    body = client.get("/world").text
    assert "function shade(" in body
    assert "shadeFactor" in body


def test_gallery_fits_every_style_on_one_screen():
    """At a fixed row pitch the fifth style lands below the canvas — and an
    option nobody can see is an option nobody evaluates, which is the whole job
    of the gate."""
    body = client.get("/world").text
    gallery = re.search(
        r"function drawGallery\(\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "STYLES.length" in gallery, "row pitch ignores how many styles there are"
    assert "app.screen.height" in gallery, "row pitch ignores the canvas height"
