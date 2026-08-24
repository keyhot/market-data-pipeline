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


def test_renderer_is_served_same_origin_and_still_version_pinned():
    """The renderer must not depend on a third party at page load.

    A transient unpkg response without `Access-Control-Allow-Origin` left
    `PIXI` undefined and put the "renderer unavailable" card on air for two
    hours. The version stays pinned — it moved from a URL into a filename.
    """
    body = client.get("/world").text
    assert "/static/pixi-8.19.0.min.js" in body
    assert "unpkg.com" not in body
    # SRI is for third-party bytes; same-origin it can only white-frame the
    # stream with no CDN to blame.
    assert "integrity=" not in body


def test_the_vendored_renderer_is_actually_served():
    """A rewired tag proves nothing if the file is missing from the image."""
    response = client.get("/static/pixi-8.19.0.min.js")
    assert response.status_code == 200
    assert len(response.content) > 100_000


def test_a_failed_boot_retries_instead_of_latching():
    """The fallback used to be terminal: painted once, blank until a human
    refreshed the browser source. Unattended surfaces must self-heal."""
    body = client.get("/world").text
    assert "retryBoot" in body
    assert "boot_retry" in body


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


def test_a_character_does_not_sink_into_the_background():
    """KI-028. This test used to assert that a function named `liftDark`
    existed and was called — and it passed, green, while the cast measured
    1.19:1 and 1.00:1 against its own background. Asserting the mechanism is
    what let the defect through, so it now asserts the *quantity*: the page
    must carry a body tint for every mood, and those tints must come from the
    module that does the contrast maths.
    """
    from world.visuals import MOOD_COLORS, SILHOUETTE_MIN_CONTRAST, body_contrast

    body = client.get("/world").text
    assert "const BODY_TINT = {" in body, "the tint table never reached the page"
    assert "__BODY_TINTS_JSON__" not in body, "placeholder left unreplaced"
    expression = re.search(
        r"function setExpression\(char, mood\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "bodyTint(" in expression, "the table is injected but never applied"
    # The floor is the point, and it holds for every mood the room can show —
    # not just the two that happened to be on screen when it was measured.
    for mood in MOOD_COLORS:
        assert body_contrast(mood) >= SILHOUETTE_MIN_CONTRAST, mood


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


def test_every_body_declares_its_own_contact_patch():
    """A shadow is the only thing telling a viewer where the floor is, and its
    width is a property of the body — the bar cluster stands wider than the
    figure. Same registry shape as HEAD_TRAVEL, checked for the same reason:
    a new body silently inheriting someone else's footprint reads as floating."""
    body = client.get("/world").text
    listed = re.search(r"const STYLES = \[(.*?)\]", body, re.S).group(1)
    styles = set(re.findall(r'"([a-z]+)"', listed))
    declared = re.search(r"const SHADOW_WIDTH = \{(.*?)\}", body, re.S).group(1)
    widths = {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", declared)}
    assert set(widths) == styles, f"{sorted(widths)} vs {sorted(styles)}"
    assert widths["bars"] > widths["figure"], "the bar cluster stands wider"


def test_a_shadow_is_a_sibling_of_its_body_not_a_child_of_it():
    """Parented to the character it would inherit every lean, squash and hop —
    a leaning body would drag its shadow off the floor, which is the exact
    opposite of grounding. It lives on its own layer *inside* `layers.chars`,
    so it still gets CAST_SCALE and the same coordinate space for free."""
    body = client.get("/world").text
    assert "layers.chars.addChild(layers.shadows)" in body, (
        "shadows are not inside the cast layer, so they lose CAST_SCALE"
    )
    assert "layers.shadows.addChild(shadow)" in body
    made = re.search(
        r"function character\(name, style\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "container.addChild(...accents, body, head" in made
    assert "container.addChild(shadow" not in made, "the shadow is parented to the body"


def test_hiding_a_character_also_hides_its_shadow():
    """The shadow is on its own layer, so `container.visible = false` leaves it
    behind — the animation sheet rendered two of them hanging in mid-air where
    MODEL and TRADER had been. That is the cost of the sibling layout, and the
    fix is that visibility is a property of the character, not the container."""
    body = client.get("/world").text
    helper = re.search(
        r"function setCharacterVisible\(char, visible\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "char.container.visible = visible" in helper
    assert "char.shadow.visible = visible" in helper
    direct = re.findall(r"(?:model|trader)\.container\.visible", body)
    assert not direct, f"an eval surface hides a body past the helper: {direct}"


def test_the_cast_does_not_breathe_on_one_clock():
    """Two characters sharing a breathing phase read as one animation played on
    two puppets — the same failure the blink seed already avoids. Both the
    offset and the period come off the per-character RNG, so they never drift
    into step either."""
    body = client.get("/world").text
    advance = re.search(
        r"function advanceCharacters\(dt\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    idle_call = re.search(r"ANIM\.idle\(char, ([^;]+)\);", advance).group(1)
    assert "char.breath" in idle_call and "char.phaseOffset" in idle_call, (
        f"the idle clock is shared across the cast: {idle_call}"
    )
    made = re.search(
        r"function character\(name, style\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "char.phaseOffset = char.rand()" in made
    assert "char.breath" in made


def test_only_named_animations_get_the_out_of_range_curves():
    """`anticipate` and `snap` return negative values on purpose — that dip is
    the crouch and the recoil. But several animations feed the curve straight
    into a scale, and a big enough negative drives it through zero, which is
    the bug `turn` already shipped once. So they are applied per animation,
    never as a blanket substitution for the symmetric bump."""
    body = client.get("/world").text
    anim = re.search(r"\n    const ANIM = \{(.*?)\n    \};", body, re.S).group(1)
    users = set(re.findall(r"(\w+):\s*\([^)]*\) => \{[^\n]*EASE\.(\w+)", anim))
    assert {("jolt", "snap"), ("hop", "anticipate"), ("slump", "settle")} == users, (
        f"the curve assignment changed without review: {sorted(users)}"
    )
    # `turn` and `pulse` drive scale directly off the raw curve.
    assert "EASE." not in re.search(r"turn:(.*?)\n      pan:", anim, re.S).group(1)


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


def test_registry_lookups_carry_no_default_to_hide_a_gap_behind():
    """Five lookups used to end in `?? something`. Every one was unreachable —
    the coverage is closed from both ends: `test_reactions` proves the registry
    can only emit moods and animations it declares, and the tests above prove
    the page implements all of both and every `STYLES` entry. So the defaults
    were never safety nets; they were the branch that would swallow a broken
    invariant and render a wrong-but-plausible character instead of failing.
    That is precisely the shape of the `HEADLINES` bug and of KI-019."""
    body = client.get("/world").text
    guarded = {
        "FACE[mood]": r"return FACE\[mood\]\s*(\?\?|\|\|)",
        "FACE_KIND[style]": r"FACE_KIND\[style\]\s*(\?\?|\|\|)",
        "SHADOW_WIDTH[style]": r"SHADOW_WIDTH\[style\]\s*(\?\?|\|\|)",
        "HEAD_TRAVEL[c.style]": r"HEAD_TRAVEL\[c\.style\]\s*(\?\?|\|\|)",
    }
    for name, pattern in guarded.items():
        assert not re.search(pattern, body), (
            f"{name} grew a default again — if the registry can now miss a key, "
            "fix the registry or the invariant test, don't paper over it here"
        )
    assert "if (!ANIM[animation])" not in body, (
        "playAnimation silently substitutes idle for an unknown animation again"
    )


def test_the_tier_function_is_injected_not_written_out_per_page():
    """Injecting the *cuts* was only half of KI-019. The three lines that read
    them were then written out twice, byte-identical, in `world.html` and
    `overlay_events.html` — and two copies of a rule is exactly how the first
    version drifted from the server. One definition, in `world.state`."""
    from world.state import severity_tier, tier_of_js

    for path in ("/world", "/overlay/events"):
        body = client.get(path).text
        assert body.count("function tierOf") == 1, path
        assert "__TIER_OF_JS__" not in body, f"{path} never got the injection"
        assert tier_of_js() in body, f"{path} carries its own copy"

    # And the copy agrees with the server it mirrors: same lookup, same count.
    cuts = re.search(
        r"const cuts = TIER_CUTS\.cuts\[eventType\] \?\? TIER_CUTS\.generic;",
        tier_of_js(),
    )
    assert cuts, "the injected function stopped reading the injected cuts"
    assert severity_tier("signal_resolved", 1.3) == 2, "the rule itself moved"


def test_room_lighting_comes_from_the_shared_ramp():
    """Four lighting constants loose in a template is how the tier scale went
    wrong (KI-019). The room's calm→dramatic lighting is injected from
    `world.visuals.room_light`, and the page keys it off `ambient.tier`, which
    is the server's own `severity_tier` as it arrives in `/world/state`."""
    from world.visuals import room_light

    body = client.get("/world").text
    assert "__ROOM_LIGHT_JSON__" not in body
    found = re.search(r"const ROOM_LIGHT = (\[.*?\]);", body, re.S)
    injected = json.loads(found.group(1))
    assert injected == [room_light(tier) for tier in range(4)]
    assert "ROOM_LIGHT[" in body, "the ramp is injected but never read"


def test_symbol_pillars_are_laid_out_from_the_canvas_not_a_fixed_pitch():
    """At a fixed 140px pitch from y=24 the top pillar was clipped by the canvas
    edge on the real 1920×1080 stream — furniture running off the top of the
    room the ticket is supposed to define."""
    body = client.get("/world").text
    pillars = re.search(
        r"function drawPillars\(state\) \{(.*?)\n    \}", body, re.S
    ).group(1)
    assert "app.screen.height" in pillars, "pillar layout ignores the canvas"


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


def test_the_wall_is_a_native_gradient_but_the_vignette_cannot_be():
    """Half a migration, on purpose. `FillGradient`'s *radial* path pre-fills
    its whole texture with the LAST colour stop and then paints the ramp over
    it — so the vignette's opaque outer stop erases its own transparent centre
    and the room renders as a solid black rectangle (measured against the
    canvas version: max delta 255/255, mean 51.5). The linear wall has no such
    pre-fill and ported pixel-for-pixel (max delta 1/255). Someone will
    reasonably try to finish the job; this is the test that stops them."""
    body = client.get("/world").text
    layout = re.search(r"function layoutRoom\(\) \{(.*?)\n    \}", body, re.S).group(1)
    assert "PIXI.FillGradient" in layout, "the wall stopped using the native gradient"
    assert 'type: "radial"' not in layout, (
        "a radial FillGradient pre-fills with its last colour stop, which erases "
        "any transparent centre — the vignette would render as solid black"
    )
    assert layout.count("gradientSprite(") == 1, (
        "the vignette must stay a canvas texture for the reason above"
    )


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


# --- B8: the tier-3 swell sequence -----------------------------------------
#
# The money moment, and the Shorts raw material: camera push, character spike,
# a bold callout, a cue for the audio bed, decay. The page source is what these
# can assert — the choreography itself is graded by looking at it, which is
# what `?swell=1` exists for.

def _world_source():
    return client.get("/world").text


def test_a_tier_three_event_starts_the_swell_sequence():
    body = _world_source()
    assert "function startSwell(" in body
    assert re.search(r"tier\s*>=\s*SWELL_TIER", body), (
        "the swell must key on the tier the server computed, not on a second "
        "severity scale of its own (KI-019)"
    )


def test_the_camera_push_returns_the_stage_exactly_to_rest():
    """A transform that does not land back on exactly 1.0 leaves the room a
    little more zoomed after every swell — the same failure as `hop`'s
    inverted squash and `turn` collapsing through zero, on the whole stage."""
    body = _world_source()
    assert "function restCamera(" in body
    assert re.search(r"app\.stage\.scale\.set\(1\)", body)
    assert re.search(r"app\.stage\.position\.set\(0,\s*0\)", body)


def test_a_burst_of_tier_three_events_pushes_the_camera_once():
    """`app.stage.scale` is global: a second push starting mid-decay compounds
    against a base that is not 1.0, and the room ends up permanently zoomed."""
    body = _world_source()
    assert re.search(r"if\s*\(swell\.active\)\s*return", body), (
        "no guard against a second swell starting inside the first"
    )
    assert "SWELL_FLOOR_S" in body, "no floor between consecutive swells"


def test_the_swell_cue_is_a_seam_and_not_a_pretend_sound():
    """There is no audio bed yet — it is Sprint 11's remaining human step. A
    stub that looks like it plays music is a lie the next reader has to find;
    an event with no listener is a seam."""
    body = _world_source()
    assert "function emitSwellCue(" in body
    assert 'CustomEvent("world:swell"' in body
    assert "new Audio(" not in body and ".play()" not in body


def test_the_callout_is_constrained_by_layout_not_by_transform():
    """KI-031: the tier swell clipped the very headlines it existed to
    emphasise, because `transform: scale` does not reflow and the overflow met
    the frame edge. The callout is sized by the box, and its text wraps."""
    body = _world_source()
    callout = re.search(r"#callout\s*\{[^}]*\}", body)
    assert callout, "no #callout rule"
    assert "max-width" in callout.group(0)
    assert re.search(r"#callout[^{]*\{[^}]*overflow-wrap", body)


def test_the_callout_sets_text_as_textcontent():
    body = _world_source()
    assert re.search(r"callout\w*\.textContent\s*=", body)


def test_the_swell_preview_surface_opens_no_event_stream():
    """`?swell=1` is the eval gate the sprint note ruled: a local-only surface,
    the way B1's `?anims=1` is — never a synthetic tier-3 row in an append-only
    log. It must return before subscribe(), which is also what makes it
    screenshottable at all: headless Chrome hangs on the page's SSE."""
    body = _world_source()
    preview = re.search(
        r'get\("swell"\).*?\n(?:.*\n){0,12}?\s*return;', body
    )
    assert preview, "no early-returning ?swell=1 branch"
    assert "EventSource" not in preview.group(0)
