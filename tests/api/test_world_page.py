"""The canvas itself isn't unit-testable, so these tests pin the things that
silently break a 24/7 browser source: substitution, the SRI pin, and the
textContent-only rule that keeps event payloads from becoming markup."""

import json
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
    assert "tierOf(event.event_type, event.severity)" in body


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


def test_every_style_declares_which_face_it_wears():
    """A machine and a person shouldn't wear the same face — that's half of
    what makes MODEL and TRADER tell apart. A style missing from the registry
    would silently inherit a face rather than be given one."""
    body = client.get("/world").text
    listed = re.search(r"const STYLES = \[(.*?)\]", body, re.S).group(1)
    styles = re.findall(r'"([a-z]+)"', listed)
    kinds = dict(re.findall(r"(\w+): \"(machine|human)\"", body))
    assert set(styles) == set(kinds), f"styles {styles} vs faces {sorted(kinds)}"
    assert set(kinds.values()) == {"machine", "human"}, kinds


def test_the_cast_is_scaled_by_its_layer_not_by_each_character():
    """Half the named animations set `container.scale` themselves (jolt, hop,
    pulse, sleep, turn), so a per-character base scale gets clobbered the first
    time one fires. The layer carries it; the gallery, which lays itself out,
    puts it back."""
    body = client.get("/world").text
    layout = re.search(
        r"function positionCharacters\(\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "layers.chars.scale" in layout
    gallery = re.search(
        r"function drawGallery\(\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "layers.chars.scale" in gallery, "the gallery inherits the cast scale"


def test_a_dormant_character_does_not_sink_into_the_background():
    """The trader stays dormant until freqtrade events exist, and his dormant
    tint is nearly the background — he disappeared in the wide shot. A
    luminance floor keeps him present without pretending he's active."""
    body = client.get("/world").text
    assert "function liftDark(" in body
    expression = re.search(
        r"function setExpression\(char, mood\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "liftDark(" in expression, "the floor is defined but never applied"


def test_an_accent_gestures_by_kind_rather_than_all_of_them_rotating():
    """Rotating a bar about its own base swings it like a felled tree, and the
    cluster came apart on cheer/wave/shrug. Only what hangs from a joint
    swings; a bar pumps and a base shifts its weight."""
    body = client.get("/world").text
    assert "const GESTURE = {" in body
    for mode in ("swing:", "pump:", "shift:"):
        assert mode in body, mode
    bars = re.search(r"      bars\(body, accents\) \{(.*?)\n      \},", body, re.S)
    assert '"pump"' in bars.group(1), "the bar cluster still swings"


def test_how_far_a_head_may_sink_is_declared_per_body():
    """`slump` drops the head a fixed distance, but how much room there is to
    drop into is a property of the body: the figure's head already overlaps its
    chest at rest, so the shared distance buried a third of it and read blobby
    rather than dejected. Every body has to say."""
    body = client.get("/world").text
    listed = re.search(r"const STYLES = \[(.*?)\]", body, re.S).group(1)
    styles = set(re.findall(r'"([a-z]+)"', listed))
    declared = re.search(r"const HEAD_TRAVEL = \{(.*?)\}", body, re.S).group(1)
    travel = {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", declared)}
    assert set(travel) == styles, f"{sorted(travel)} vs {sorted(styles)}"
    assert travel["figure"] < travel["bars"], "the neckless body sinks furthest"


def test_the_animation_sheet_loops_every_animation_through_the_real_player():
    """Sixteen animations shipped without anyone seeing one — and wave, shake
    and flicker all sit at or near zero displacement at whatever phase you'd
    think to freeze. A sheet with its own player would be showing an animation
    nobody ships, so it drives `advanceCharacters` instead."""
    body = client.get("/world").text
    sheet = re.search(
        r"function drawAnimationSheet\(\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "Object.keys(ANIM)" in sheet, "the sheet keeps its own list of animations"
    assert "loopAnim" in sheet
    advance = re.search(
        r"function advanceCharacters\(dt\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "loopAnim" in advance, "the sheet is not driven by the real player"


def test_the_tier_scale_is_injected_rather_than_reinvented():
    """The canvas and the events rail each decide how big an event reads, and
    both did it on an absolute 2/5/8 scale while the server normalises PER RULE.
    `signal_resolved` cuts at 0.6/1.2/1.8 — so the room's single most frequent
    event has always rendered at tier 0: minimum amplitude, no swell, no tier
    colour. Exactly the drift a restated reaction registry would cause, and the
    same fix — the scale comes from `world.state`."""
    from world.state import GENERIC_TIER_CUTS, tier_cuts

    for path in ("/world", "/overlay/events"):
        body = client.get(path).text
        assert "__TIER_CUTS_JSON__" not in body, path
        assert "severity >= 8" not in body, f"{path} still keeps its own scale"
        found = re.search(r"const TIER_CUTS = (\{.*?\});", body, re.S)
        assert found, f"{path} has no injected tier scale"
        cuts = json.loads(found.group(1))
        assert cuts["cuts"]["signal_resolved"] == list(tier_cuts()["signal_resolved"])
        assert cuts["generic"] == list(GENERIC_TIER_CUTS), path
        assert "tierOf(e.event_type" in body or "tierOf(event.event_type" in body, (
            f"{path} computes a tier without saying which rule it came from"
        )


def test_characters_have_idle_behaviour_between_events():
    """A calm stretch is most of the airtime and it looked like two statues
    sharing a 2px bob. Blink, glance and weight-shift run only on the branch
    where no reaction is playing, so idle life can never fight an animation."""
    body = client.get("/world").text
    assert "function idleTick(" in body
    actions = re.search(r"const IDLE_ACTIONS = \{(.*?)\n    \};", body, re.S).group(1)
    for action in ("blink:", "glance:", "shift:"):
        assert action in actions, action
    advance = re.search(
        r"function advanceCharacters\(dt\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "idleTick(" in advance


def test_idle_timing_is_seeded_rather_than_ad_hoc_randomness():
    """The eval gate for calm is one quiet frame compared against another, and
    unseeded idle drift makes two calm frames incomparable. The director
    already treats RNG as a thing you inject; so does the room."""
    body = client.get("/world").text
    assert "function rng32(" in body
    idle = re.search(
        r"function idleTick\(char, dt\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "Math.random" not in idle, "idle timing must not be ad-hoc random"
    assert "char.rand(" in idle


def test_the_observer_glances_without_claiming_to_have_acted():
    """'Disagreement is the content' — but the trader is a dry-run sidecar that
    stays dormant until its own events exist, so giving it a reaction to an
    event it had no part in would be inventing activity, which is the one thing
    this room must never do. It looks. It does not act."""
    body = client.get("/world").text
    react = re.search(r"function react\(event\) \{(.*?)\n    \}", body, re.S).group(1)
    assert "lookAt(" in react
    look = re.search(
        r"function lookAt\(observer, subject, delay\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "setExpression" not in look, "a glance must not restate the observer's mood"
    assert "playAnimation" not in look, "a glance is attention, not action"
    assert "stat" not in look, "a glance must not touch the observer's numbers"


def test_the_floor_is_laid_out_with_the_characters_not_only_at_boot():
    """The renderer follows the window and `positionCharacters` runs on every
    draw, so a floor drawn once at boot drifts off its inhabitants and they end
    up standing through it — which is exactly what a resize produced."""
    body = client.get("/world").text
    layout = re.search(
        r"function positionCharacters\(\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "layoutRoom(" in layout, "the floor is never re-placed after boot"


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
