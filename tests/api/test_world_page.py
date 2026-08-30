"""The canvas itself isn't unit-testable, so these tests pin the things that
silently break a 24/7 browser source: substitution, the same-origin rule
for the renderer (KI-045), the bounded boot retry, and the textContent-only
rule that keeps event payloads from becoming markup."""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
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


def test_the_retry_cap_cannot_be_defeated():
    """Two ways the bounded backoff stops being bounded, both real.

    1. Resetting the counter mid-boot: anything throwing *after* the reset but
       before boot() resolves reaches the catch with a fresh budget, so the
       page reload-loops forever. The reset must come after the room is built.
    2. The counter rides in the URL. `Number("abc")` is NaN, NaN fails every
       comparison, so a junk value would slip past the cap and then increment
       to NaN forever.
    """
    body = client.get("/world").text
    # The reset happens once construction is done, not the moment PIXI answers.
    assert body.index("startAmbient();") < body.index("bootSucceeded();")
    assert body.index("bootSucceeded();") < body.index("subscribe()")
    # A non-integer counter counts as exhausted, never as zero.
    assert "Number.isInteger(raw)" in body


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
    # P5 moved the arithmetic into pillarGeometry so it could be run without
    # PIXI; the claim is unchanged and now lives where the numbers are. The
    # behavioural half is test_without_a_plate_the_pillars_march_off_toward_
    # the_corner_as_before, which executes it.
    geometry = _js_block(_world_source(), "function pillarGeometry(")
    assert "app.screen.height" in geometry, "pillar layout ignores the canvas"


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


def test_no_template_loads_a_script_from_another_origin():
    """The rule is "no external origin in a script tag", not "not unpkg".

    KI-045 was unpkg, but cdnjs, jsdelivr or a raw GitHub URL is the identical
    outage with a different domain: a third party in the on-air load path,
    whose failures are undetectable from here and unfixable. Naming two vendors
    in a grep would let the third one through. Scripts specifically — a font or
    an image that fails to load degrades the frame; a script that fails to load
    blanks it.
    """
    templates = Path(__file__).resolve().parents[2] / "api" / "templates"
    external = re.compile(r'<script[^>]+src="https?://', re.IGNORECASE)
    offenders = [
        p.name for p in templates.glob("*.html") if external.search(p.read_text())
    ]
    assert offenders == [], f"templates loading a remote script: {offenders}"


def test_the_page_reports_that_it_is_still_drawing():
    """KI-046. The count must come from the RENDER loop, not from the timer
    that posts it - a page whose timer fires while its renderer is dead is the
    exact failure this detects.

    Two more regressions the assertions above miss on their own: the posted
    body could carry a hardcoded `frames: 1` instead of the counter -
    `framesDrawn++` would still be in the ticker, `/world/heartbeat` would
    still be in the page, and the test above would stay green while shipping
    a beat that has nothing to do with whether the page is drawing - and the
    posting interval could drift off 15s, the same honest counter posted less
    often, which just as quietly breaks STALE_AFTER=45.0's "three missed
    beats" meaning. The isolated heartbeat window below pins both."""
    body = client.get("/world").text
    assert "/world/heartbeat" in body
    assert "framesDrawn++" in body
    ticker = body.split("app.ticker.add(")[1][:800]
    assert "framesDrawn" in ticker

    heartbeat = body.split('"/world/heartbeat"')[1][:300]
    assert re.search(r"frames:\s*framesDrawn\b", heartbeat), (
        "the posted frame count must be the render-loop counter itself - a "
        "hardcoded literal such as `frames: 1` would pass every assertion "
        "above while shipping exactly the bug KI-046 exists to catch"
    )
    assert "15000" in heartbeat, (
        "the posting cadence must be 15s - STALE_AFTER=45.0 only means "
        "'three missed beats' if the page actually posts that often"
    )


def test_the_monitors_rules_are_injected_rather_than_written_into_the_page():
    """M1: the painted monitors decide nothing. Both halves are injected — the
    constants AND the functions that read them — because injecting only the
    numbers is the half-fix that let KI-019 happen. `__TIER_OF_JS__` above is
    the precedent; Task 11's page consumes these two placeholders.

    Asserted against the replacement map rather than the rendered body: the
    template does not carry the placeholders until the candles land, and a
    seam that is only proven once its consumer exists is a seam nobody checked.
    """
    from api.main import _THEME_REPLACEMENTS
    from world.monitors import monitor_rules, rules_js

    assert json.loads(_THEME_REPLACEMENTS["__MONITOR_RULES_JSON__"]) == monitor_rules()
    assert _THEME_REPLACEMENTS["__MONITOR_JS__"] == rules_js()

    # Whatever the template does with them, it must not ship them unsubstituted.
    body = client.get("/world").text
    assert "__MONITOR_RULES_JSON__" not in body
    assert "__MONITOR_JS__" not in body


# --- Track P: the plate's anchors reach the page -------------------------------
# The room stops guessing its layout from canvas fractions and reads the
# measured manifest instead. Absence is a first-class answer: no manifest is a
# procedural room, never a blank one.


def test_the_plate_manifest_reaches_the_page():
    body = _world_source()
    assert "const PLATE =" in body
    assert '"plate": "world-plate-btc-eth.png"' in body or (
        '"plate":"world-plate-btc-eth.png"' in body
    )


def test_the_plate_asset_is_actually_served():
    response = client.get("/static/world-plate-btc-eth.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_the_plate_is_addressed_same_origin_like_every_other_asset():
    """KI-045: a third party in the on-air load path white-framed the stream for
    two hours, and KI-047 white-framed it for 36 hours when the renderer died.
    The plate is a new hard on-air dependency of exactly that shape, so the
    manifest may only name a bare filename — never a scheme, a host, or a path
    that could climb out of `/static/`.
    """
    from world.plate import load_manifest

    manifest = load_manifest()
    assert manifest is not None
    assert "/" not in manifest.plate
    assert ":" not in manifest.plate
    body = _world_source()
    assert '"/static/" + PLATE.plate' in body


def test_a_watchlist_symbol_with_no_painted_tube_warns_at_startup(caplog):
    """The ticket's whole claim: a disagreement is a WARNING, never a silent
    mis-render. The plate paints tube bases at fixed positions, so a symbol the
    manifest does not name has nowhere to stand — it would simply not be drawn,
    and nothing on air would say so.
    """
    import logging as _logging

    from api import main as api_main

    class _Spec:
        def __init__(self, symbol):
            self.symbol = symbol
            self.market = "crypto"
            self.predict = True

    class _Watchlist:
        tickers = [_Spec("BTCUSDT"), _Spec("ETHUSDT"), _Spec("SOLUSDT")]

    original = api_main.load_watchlist
    api_main.load_watchlist = lambda: _Watchlist()
    try:
        with caplog.at_level(_logging.WARNING):
            client.get("/world")
    finally:
        api_main.load_watchlist = original

    warnings = " ".join(r.getMessage() for r in caplog.records)
    assert "SOLUSDT" in warnings, (
        "a watchlist symbol with no painted tube must be reported, not dropped"
    )


def _js_const(source: str, name: str) -> str:
    """One `const NAME = ...;` line, lifted from the page.

    The node drivers below must run against the page's own constants. Restating
    `GROUND = 0.66` or the cast fractions in the test would make the fallback
    assertions test the test — the page could drift to any value and stay green.
    """
    match = re.search(rf"^\s*const {name} = .*;$", source, re.M)
    assert match, f"the page no longer declares a one-line const {name}"
    return match.group(0).strip() + "\n"


def _js_block(source: str, opening: str) -> str:
    """The brace-matched source of one JS construct, from `opening` to its
    closing brace.

    The plan's own Task 4 checks were substring-presence over the whole page:
    `assert "plateReady" in body` passes on a comment, and asserting a colour
    literal passes because `visuals.css_variables()` already injects `#131722`
    into every page — it was green before a line of plate code existed. That is
    the `d1ad270` shape, the third time this sprint. Slicing the actual function
    is what makes "the failure path calls the fallback" a claim that can fail.
    """
    start = source.index(opening)
    depth = 0
    for j in range(source.index("{", start), len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[start : j + 1]
    raise AssertionError(f"unbalanced braces after {opening!r}")


def test_a_plate_that_fails_to_load_degrades_to_the_procedural_room():
    """KI-045's lesson applied to the asset that repeats its shape, and KI-047's
    applied to the page that already went white for 36 hours: the room must
    never be a blank canvas. A texture that will not decode lands in the
    procedural room the stream already ships.
    """
    body = _world_source()
    assert "function drawProceduralRoom()" in body
    draw_plate = _js_block(body, "async function drawPlate(")
    assert "catch (" in draw_plate
    failure_path = _js_block(draw_plate[draw_plate.index("catch (") :], "catch (")
    assert "drawProceduralRoom()" in failure_path, (
        "the catch block must draw the fallback room, not merely log"
    )


def test_the_missing_plate_path_also_draws_the_room():
    """No manifest at all is a supported way to run, not a degraded one."""
    draw_plate = _js_block(_world_source(), "async function drawPlate(")
    guard = draw_plate[: draw_plate.index("try")]
    assert "!PLATE_SRC" in guard and "drawProceduralRoom()" in guard


def test_plate_ready_is_claimed_only_on_the_success_path():
    draw_plate = _js_block(_world_source(), "async function drawPlate(")
    assert "plateReady = true" in draw_plate
    failure_path = _js_block(draw_plate[draw_plate.index("catch (") :], "catch (")
    assert "plateReady = true" not in failure_path


def test_the_room_is_drawn_once_and_the_plate_decides_which():
    """The plan is explicit that `drawRoom()` must be *deleted* from boot, not
    renamed there: leaving the unconditional call draws the procedural room
    underneath the plate on the success path and twice on the failure path,
    and the fallback test can no longer tell the two apart.
    """
    boot = _js_block(_world_source(), "async function boot()")
    assert "await drawPlate()" in boot
    assert "drawProceduralRoom()" not in boot
    assert "drawRoom()" not in boot


def test_the_canvas_has_a_background_before_the_plate_can_fail():
    """The third rung: no plate, no procedural room, still not a white frame.
    Asserted as an ORDERING inside boot, because the colour literal alone is
    already in every page via the injected theme vars and proves nothing.
    """
    boot = _js_block(_world_source(), "async function boot()")
    assert boot.index("background: 0x131722") < boot.index("drawPlate()")


def test_the_heartbeat_is_installed_only_after_the_plate_settles():
    """KI-046, and the reason the ordering is not free: `probe_renderer` treats
    an absent beat as the literal blank-renderer signature, because the page
    registers its heartbeat *after* the renderer boots. Installing the interval
    above `await drawPlate()` would let a load that HANGS beat `healthy: true`
    over an advancing ticker and an empty stage — the one failure the guard
    exists to catch, made invisible by its own heartbeat.
    """
    boot = _js_block(_world_source(), "async function boot()")
    assert boot.index("await drawPlate()") < boot.index("/world/heartbeat")


def test_preview_modes_hide_the_plate_not_just_the_procedural_room():
    """`?swell=1` (drawGallery) and the cast sheet (drawAnimationSheet) both set
    layers.room.visible = false. With a plate they must hide layers.plate too,
    or the character sheet renders over a full pixel-art control room and is
    unreadable as evidence.

    A page-wide `body.count(...) >= 2` passes if both copies live in the SAME
    function and the other preview mode hides nothing — this sprint's
    can't-fail shape (`d1ad270`) applied to a count instead of a substring.
    `_js_block` slices each function separately so the two assertions are
    provably about two different regions, and each pins the plate hidden
    ALONGSIDE the room (not instead of it) — the point of a specimen sheet is
    a plain background, not a different piece of scenery.
    """
    body = _world_source()

    gallery = _js_block(body, "function drawGallery(")
    assert "STYLES.forEach" in gallery, "not actually the gallery body"
    assert "layers.room.visible = false" in gallery
    assert "layers.plate.visible = false" in gallery, (
        "drawGallery must hide the plate, not just the procedural room"
    )

    sheet = _js_block(body, "function drawAnimationSheet(")
    assert "sample.loopAnim" in sheet, "not actually the animation-sheet body"
    assert "layers.room.visible = false" in sheet
    assert "layers.plate.visible = false" in sheet, (
        "drawAnimationSheet must hide the plate, not just the procedural room"
    )


# --- P5: layout comes from the measurements, not from canvas fractions -------
#
# The plan's Step 1 for this ticket was four `assert "function anchorFor(" in
# body` checks, which pass on a comment — the same defect as `d1ad270` and the
# Task 4 checks above. These run the emitted layout helpers in node instead, so
# an off-by-tens in the manifest or a fallback that never fires is a failure.

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

CANVAS = {"width": 1920, "height": 960}


def _layout_driver(body: str, *, plate: bool, manifest: dict | None = None) -> str:
    """The page's own layout helpers, lifted out and run against stubs.

    Only `app`, `GROUND` and the manifest are stubbed — the arithmetic under
    test is the page's, character for character.
    """
    source = _world_source()
    return (
        f"const PLATE = {json.dumps(manifest or _manifest())};\n"
        f"const plateReady = {str(plate).lower()};\n"
        f"const app = {{ screen: {json.dumps(CANVAS)} }};\n"
        + _js_const(source, "GROUND")
        + _js_const(source, "CAST_FRACTIONS")
        + _js_block(source, "function anchorFor(")
        + "\n"
        + _js_block(source, "function tubeFor(")
        + "\n"
        + _js_block(source, "function pillarGeometry(")
        + "\n"
        + _js_block(source, "function bannerMinHeight(")
        + "\n"
        + body
    )


def _run_node(driver: str):
    result = subprocess.run(
        [NODE, "-e", driver], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _manifest() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "api"
        / "static"
        / "world-plate-btc-eth.json"
    )
    return json.loads(path.read_text())


@needs_node
def test_the_cast_stands_where_the_plate_painted_it():
    """GROUND = 0.66 and width * 0.32 put the cast where the canvas said. With
    a plate, the measurements win — the model on the painted floor lane, the
    trader on the seat of the painted chair."""
    emitted = _run_node(
        _layout_driver(
            'console.log(JSON.stringify({model: anchorFor("model"),'
            ' trader: anchorFor("trader")}));',
            plate=True,
        )
    )
    cast = _manifest()["cast"]

    assert emitted["model"] == {
        "x": cast["model"]["x"],
        "baseY": cast["model"]["base_y"],
        "pose": "standing",
    }
    assert emitted["trader"] == {
        "x": cast["trader"]["x"],
        "baseY": cast["trader"]["base_y"],
        "pose": "seated",
    }

    # And it is the manifest being read, not this plate's numbers being known:
    # a repaint that moves the floor lane moves the model with it.
    repainted = _manifest()
    repainted["cast"] = dict(
        repainted["cast"], model={"x": 111, "base_y": 222, "pose": "standing"}
    )
    moved = _run_node(
        _layout_driver(
            'console.log(JSON.stringify(anchorFor("model")));',
            plate=True,
            manifest=repainted,
        )
    )
    assert moved == {"x": 111, "baseY": 222, "pose": "standing"}


@needs_node
def test_without_a_plate_the_cast_keeps_the_canvas_fractions_it_has_today():
    """The fallback is not decoration: `drawPlate` degrades to the procedural
    room on any texture failure (KI-045/KI-047), and a room whose cast has no
    floor to stand on is the blank frame by another route."""
    emitted = _run_node(
        _layout_driver(
            'console.log(JSON.stringify({model: anchorFor("model"),'
            ' trader: anchorFor("trader")}));',
            plate=False,
        )
    )

    assert emitted["model"]["x"] == pytest.approx(1920 * 0.32)
    assert emitted["trader"]["x"] == pytest.approx(1920 * 0.62)
    assert emitted["model"]["baseY"] == pytest.approx(960 * 0.66)
    assert emitted["model"]["pose"] == "standing"
    assert emitted["trader"]["pose"] == "standing"


@needs_node
def test_a_symbol_the_plate_never_painted_has_no_tube():
    """`watchlist_disagreements` warns about it at startup; the renderer must
    also not invent a bore for it."""
    emitted = _run_node(
        _layout_driver(
            'console.log(JSON.stringify({btc: tubeFor("BTCUSDT"),'
            ' sol: tubeFor("SOLUSDT")}));',
            plate=True,
        )
    )

    assert emitted["sol"] is None
    assert emitted["btc"]["base_y"] == _manifest()["tubes"][0]["base_y"]


@needs_node
def test_a_pillar_is_drawn_inside_the_tube_the_plate_painted():
    """The ticket's headline claim, as arithmetic: the bar's foot sits on the
    painted base, it is the painted width, it is centred in the painted bore,
    and a maximum-pressure pillar cannot outgrow the housing."""
    emitted = _run_node(
        _layout_driver(
            'console.log(JSON.stringify(['
            'pillarGeometry("BTCUSDT", 0.1, 0),'
            'pillarGeometry("BTCUSDT", 99, 0),'
            'pillarGeometry("ETHUSDT", 0.1, 1)]));',
            plate=True,
        )
    )
    btc, btc_full, eth = emitted
    tubes = {t["symbol"]: t for t in _manifest()["tubes"]}

    assert btc["baseY"] == tubes["BTCUSDT"]["base_y"]
    assert btc["width"] == tubes["BTCUSDT"]["width"]
    assert btc["x"] == tubes["BTCUSDT"]["x"] - tubes["BTCUSDT"]["width"] / 2
    assert btc_full["height"] == tubes["BTCUSDT"]["height"], "a full tube overflows"
    assert eth["baseY"] == tubes["ETHUSDT"]["base_y"]


@needs_node
def test_without_a_plate_the_pillars_march_off_toward_the_corner_as_before():
    emitted = _run_node(
        _layout_driver(
            'console.log(JSON.stringify(['
            'pillarGeometry("BTCUSDT", 0.1, 0),'
            'pillarGeometry("ETHUSDT", 0.1, 1)]));',
            plate=False,
        )
    )
    first, second = emitted

    assert first["x"] == pytest.approx(1920 * 0.80)
    assert second["x"] == pytest.approx(1920 * 0.80 + 132)
    assert first["width"] == 54
    assert first["baseY"] == pytest.approx(960 * 0.66)


def test_the_cast_is_placed_from_its_anchor_every_frame():
    """`character()` sets a y once; `positionCharacters()` overwrites x AND y
    on every draw and is therefore the only placement that survives. The plan
    pointed this ticket at `character()`, where the write is dead."""
    block = _js_block(_world_source(), "function positionCharacters()")
    assert 'anchorFor("model")' in block
    assert 'anchorFor("trader")' in block
    assert "app.screen.width * 0.32" not in block, (
        "the fraction must live in anchorFor's fallback, not beside it"
    )


@needs_node
def test_the_banner_sits_on_the_quiet_strip_the_plate_left_for_it():
    """The plate reserves a top band. Bind the banner to the measurement, so a
    repaint that moves the strip moves the text with it rather than hanging it
    over a painted detail.

    The first version of this test asserted `"PLATE.bands.top" in drawPlate`
    and survived hardcoding the height to 60px — the key still appeared in the
    guard. That is the fourth test this sprint whose name outran its assertion,
    so the height is a value now and the test runs it.
    """
    emitted = _run_node(
        _layout_driver("console.log(JSON.stringify(bannerMinHeight()));", plate=True)
    )
    assert emitted == _manifest()["bands"]["top"]

    without = _run_node(
        _layout_driver("console.log(JSON.stringify(bannerMinHeight()));", plate=False)
    )
    assert without is None, "no plate, no painted strip to sit on"

    # The shipped band happens to be 60, so the assertion above cannot tell
    # `PLATE.bands.top` from a literal 60. A repainted manifest can.
    repainted = _manifest()
    repainted["bands"] = dict(repainted["bands"], top=88)
    assert (
        _run_node(
            _layout_driver(
                "console.log(JSON.stringify(bannerMinHeight()));",
                plate=True,
                manifest=repainted,
            )
        )
        == 88
    )


def test_the_painted_band_is_what_the_banner_is_actually_set_to():
    """The value above is only worth having if drawPlate applies it."""
    block = _js_block(_world_source(), "async function drawPlate(")
    assert "bannerMinHeight()" in block
    assert 'banner.style.minHeight = bandHeight + "px"' in block


@needs_node
def test_the_rendered_page_is_valid_javascript():
    """Nothing else here would notice a broken brace.

    Every other check in this file reads the page as TEXT — brace-matched
    blocks, substring rules, functions lifted out and run in isolation. All of
    them stay green on a page that a browser refuses to parse, and a page that
    will not parse is a white frame: KI-045 and KI-047 by a third route, this
    time self-inflicted. `drawPlate`'s fallback cannot save it either, because
    a syntax error means no code runs at all.
    """
    scripts = re.findall(r"<script>(.*?)</script>", _world_source(), re.S)
    assert scripts, "the page has no inline script to check"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "world.js"
        path.write_text("\n".join(scripts))
        result = subprocess.run(
            [NODE, "--check", str(path)], capture_output=True, text=True, timeout=30
        )
    assert result.returncode == 0, result.stderr


# --- Task 6: the pixel grid — one drawing rule the whole cast obeys ---------


def test_the_cast_is_drawn_on_a_pixel_grid():
    """The pixel look comes from drawing rules, not from sprite sheets - the
    tested animation layer (reactions, breathing, the B7 glance, bubbles)
    survives precisely because we did not replace it with PNGs."""
    body = client.get("/world").text
    assert "function snap(" in body
    assert "roundPixels" in body
    assert "scaleMode" in body


def test_animation_displacement_is_snapped_too():
    """Unsnapped motion over snapped art reads as sub-pixel crawl - the exact
    tell that gives away 'pixel art' that is really a smooth render.

    Review round 1, Finding 3: `const ANIM = {` (where every displacement
    write actually lives) precedes `function playAnimation(` in the file, so
    a forward-only window from `playAnimation` never reaches it - reverting
    a single ANIM entry left the old version of this test green, and only
    reverting all three of restCharacter's snaps (textually the one thing
    inside the old window) turned it red. Sliced on the ANIM table itself
    instead, which is what the name promises.

    A bare `"snap(" in anim` is not enough, though, and mutation-checking it
    caught why: `jolt` calls `EASE.snap(p)` - a pre-existing easing curve
    named `snap`, unrelated to this ticket's grid quantiser - and that alone
    makes the substring true with every quantiser snap() stripped out. The
    negative lookbehind is what keeps this test from being exactly the kind
    of test-that-cannot-fail this ticket exists to replace.
    """
    anim = _js_block(_world_source(), "const ANIM = {")
    assert re.search(r"(?<!EASE\.)snap\(", anim), (
        "no quantiser snap() in the ANIM table - EASE.snap(p) (an easing "
        "curve, not this ticket's grid quantiser) does not count"
    )


def test_cell_derives_from_the_plate_and_falls_back_to_a_literal():
    """`snap`/`CELL` are the interface Tasks 7, 8 and 12 build on top of
    (`drawCandles`, `applySwell`, the seated trader). `world/monitors.py`
    already owns its own `CELL` for a different surface, so this one must
    read the plate's measurement rather than mint a third number, and must
    not throw when there is no manifest (`PLATE` is `null`). Declared at
    top-level script scope, not inside `boot()`, so every cast helper can
    reach it.
    """
    body = client.get("/world").text
    cell_line = _js_const(body, "CELL")
    assert "PLATE.cell" in cell_line
    assert "|| 4" in cell_line
    boot = _js_block(body, "async function boot()")
    assert "const CELL" not in boot
    assert "function snap(" not in boot


def test_the_renderer_disables_antialiasing_globally():
    """Step 3: `antialias` is a single global PixiJS renderer setting, so
    turning it off also changes the procedural fallback room - the on-air
    surface whenever the plate fails to load (KI-045/KI-047). Deliberate,
    not a side effect: pinned here so a revert reads as a revert.
    """
    boot = _js_block(_world_source(), "async function boot()")
    assert "antialias: false" in boot
    assert "antialias: true" not in boot


def test_chars_and_props_layers_get_round_pixels():
    """`roundPixels` is what stops PixiJS compositing the cast and the props
    layer at sub-pixel offsets - the plate and the vignette stay off this
    list on purpose, they are painted art and a soft falloff, not the grid.
    """
    boot = _js_block(_world_source(), "async function boot()")
    assert "layers.chars.roundPixels = true;" in boot
    assert "layers.props.roundPixels = true;" in boot


def test_the_pillar_fill_is_a_stack_of_blocks_not_a_smooth_rect():
    """Step 3b, decided 2026-08-25 from the reference image: the tube fill is
    a column of discrete cells, not a single smooth `roundRect`."""
    draw_pillars = _js_block(_world_source(), "function drawPillars(")
    assert (
        "roundRect(geo.x, geo.baseY - geo.height, geo.width, geo.height"
        not in draw_pillars
    ), "the tube fill is still one smooth rounded rect"
    assert re.search(r"for\s*\(let b = 0", draw_pillars), "the fill is not a block loop"
    assert "CELL" in draw_pillars and "snap(" in draw_pillars


@needs_node
def test_animation_displacement_actually_quantises_to_the_grid():
    """`test_animation_displacement_is_snapped_too` only proves the text
    `snap(` sits inside the ANIM table - not that the pixel a viewer's screen
    receives is on any grid at all. This runs the real `ANIM` table (the code
    `playAnimation` hands off to, via `advanceCharacters`) against a character
    whose rest position is deliberately fractional (583.37, not a 4px
    multiple), and checks the WRITTEN container/head offset in SCREEN space.

    Review round 1, Finding 1: `layers.chars.scale.set(CAST_SCALE)` sits
    between every `snap()` call in this table and the screen, so a local value
    landing on a 4px multiple proves nothing about what a viewer sees -
    that was this test's bug before the fix, and it stayed green through it.
    The rendered cell is `CELL * CAST_SCALE`; it must itself be a whole number
    of screen pixels (the ruling: CAST_SCALE = 1.5 makes it exactly 6) or
    `roundPixels` rounds each frame to a DIFFERENT nearby integer depending on
    phase - a jittering step, worse than a fixed off-grid one. Every snapped
    local coordinate, multiplied by CAST_SCALE, must land on an exact
    multiple of that rendered cell.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\n"
        + _js_const(source, "CELL")
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + _js_block(source, "const EASE = {")
        + ";\n"
        + _js_const(source, "HEAD_TRAVEL")
        + _js_block(source, "function gesture(")
        + "\n"
        + _js_block(source, "const ANIM = {")
        + ";\n"
        + """
        function run(name, p, a) {
          const c = {
            baseY: 583.37, baseX: 917.53, headBaseY: -138.25, style: "bars",
            accents: [],
            container: { y: 0, x: 0, rotation: 0, alpha: 1, scale: { set() {} } },
            head: { y: 0, rotation: 0 },
            mouth: { scale: { y: 1 } },
          };
          ANIM[name](c, p, a);
          return { y: c.container.y, x: c.container.x, headY: c.head.y };
        }
        console.log(JSON.stringify({
          cell: CELL,
          castScale: CAST_SCALE,
          idle: run("idle", 0.37, 1),
          jolt: run("jolt", 0.05, 2.3),
          shake: run("shake", 0.6, 1.7),
          hop: run("hop", 0.1, 1.4),
          slump: run("slump", 0.8, 2.1),
          cheer: run("cheer", 0.25, 1.9),
          step: run("step", 0.5, 1.3),
          pan: run("pan", 0.33, 1.1),
        }));
        """
    )
    emitted = _run_node(driver)
    cell = emitted["cell"]
    cast_scale = emitted["castScale"]
    assert cell == 4, "no plate in this driver, so CELL must fall back to the literal"

    # The rendered cell has to be a whole number of screen pixels, or the
    # step PixiJS actually draws jitters between two integers by phase - the
    # exact failure a CAST_SCALE of 1.35 (5.4px) produces.
    rendered_cell = cell * cast_scale
    assert rendered_cell == int(rendered_cell), (
        f"CELL * CAST_SCALE = {rendered_cell} is not a whole number of screen "
        "pixels - the rendered step size will jitter under roundPixels"
    )

    def on_grid(screen_value: float) -> bool:
        remainder = screen_value % rendered_cell
        return remainder < 1e-6 or rendered_cell - remainder < 1e-6

    # baseY/baseX are deliberately not multiples of 4, so a LOCAL value that
    # lands on the grid anyway proves snap() ran on the final assignment. But
    # the claim this ticket makes is about the screen, not the layer: multiply
    # by CAST_SCALE (what layers.chars.scale actually does before anything is
    # composited) before checking it lands on the rendered cell.
    checked = {
        "idle": "y", "jolt": "y", "shake": "x", "hop": "y",
        "slump": "y", "cheer": "y", "step": "x", "pan": "x",
    }
    for name, axis in checked.items():
        local_value = emitted[name][axis]
        screen_value = local_value * cast_scale
        assert on_grid(screen_value), (
            f"{name}.{axis}: local {local_value} * CAST_SCALE {cast_scale} = "
            f"{screen_value}, not a multiple of the {rendered_cell}px rendered "
            "cell - snapped in the wrong coordinate space"
        )
    head_screen = emitted["slump"]["headY"] * cast_scale
    assert on_grid(head_screen), "slump's head offset is unsnapped in screen space"


@needs_node
def test_the_idle_glance_is_snapped_too():
    """Review round 1, Finding 2: `setGaze` (fed by IDLE_ACTIONS.glance and by
    `watchTick`'s deliberate look) writes a continuous per-frame `dx`/
    `headShift` through a sin() ease - unsnapped, that is the exact sub-pixel
    crawl this ticket exists to remove, and `watchTick`'s amplitude (4) is
    exactly one CELL. Runs the real `setGaze` against fractional inputs and
    checks the WRITTEN eye/head offset in SCREEN space (local value *
    CAST_SCALE), for the same reason test 7 had to stop checking local space
    only: `layers.chars.scale` sits between this function and the screen.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\n"
        + _js_const(source, "CELL")
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + _js_block(source, "function setGaze(")
        + "\n"
        + """
        function run(dx, rot, headShift) {
          const c = {
            eyeL: { x: 0 }, eyeR: { x: 0 }, head: { rotation: 0, x: 0 },
          };
          setGaze(c, dx, rot, headShift);
          return { eyeX: c.eyeL.x, headX: c.head.x };
        }
        console.log(JSON.stringify({
          cell: CELL,
          castScale: CAST_SCALE,
          // Fractional and not a multiple of 4, the way a live sin() ease
          // actually lands - the same reason 583.37 was picked in test 7.
          glance: run(2.5 * 0.6173, 0.05 * 0.6173, 0),
          watch: run(4 * 0.8412, 0.12 * 0.8412, 3 * 0.8412),
        }));
        """
    )
    emitted = _run_node(driver)
    cell = emitted["cell"]
    cast_scale = emitted["castScale"]
    rendered_cell = cell * cast_scale
    assert rendered_cell == int(rendered_cell), (
        f"CELL * CAST_SCALE = {rendered_cell} is not a whole number of screen "
        "pixels - the rendered step size will jitter under roundPixels"
    )

    def on_grid(screen_value: float) -> bool:
        remainder = screen_value % rendered_cell
        return remainder < 1e-6 or rendered_cell - remainder < 1e-6

    for name in ("glance", "watch"):
        for axis in ("eyeX", "headX"):
            local_value = emitted[name][axis]
            screen_value = local_value * cast_scale
            assert on_grid(screen_value), (
                f"{name}.{axis}: local {local_value} * CAST_SCALE {cast_scale} "
                f"= {screen_value}, not a multiple of the {rendered_cell}px "
                "rendered cell - the idle glance is still sub-pixel"
            )


# --- Task 7: TRADER, seated -------------------------------------------------


def _js_range(source: str, start: str, end_marker: str) -> str:
    """Source from `start` through the end of the line containing
    `end_marker` (inclusive).

    `_js_block` brace-matches one construct and stops at its closing brace —
    it cannot reach the two `SEATED_ANIMATIONS.x = SEATED_ANIMATIONS.y;`
    statements that follow the object literal, since those are separate
    top-level statements, not part of it. Reads them from the page rather
    than restating the names in the test, for the same reason `_js_const`
    does: a drifted alias should fail here, not stay silently untested.
    """
    begin = source.index(start)
    end = source.index(end_marker, begin)
    end = source.index("\n", end)
    return source[begin:end] + "\n"


def test_the_trader_has_a_seated_rig():
    body = client.get("/world").text
    assert "function seatedRig(" in body
    assert '"seated"' in body


@needs_node
def test_the_arm_aliases_are_wired_from_the_real_accents_array():
    """`SEATED_ANIMATIONS.gesture` reaches for `c.armFar` - a name that only
    exists on the trader because `character()` aliases it onto `accents[1]`,
    the real Graphics `seatedRig` pushed and already parented to
    `container`. Every other node test here builds a hand-crafted fake with
    `armFar` already present (deliberately - Override 1 is about the
    animation table, not this wiring), which never exercises this specific
    assignment. Caught by hand-mutation: deleting it left every other test
    in this file green (`character()` is never run end-to-end without a real
    PIXI/app), so it gets its own.
    """
    source = _world_source()
    driver = (
        "const container = { pose: \"seated\" };\n"
        "const accents = [{ id: 'near' }, { id: 'far' }];\n"
        "const char = {};\n"
        + _js_block(source, 'if (container.pose === "seated") {')
        + "\n"
        + """
        console.log(JSON.stringify({
          armNear: char.armNear && char.armNear.id,
          armFar: char.armFar && char.armFar.id,
        }));
        """
    )
    emitted = _run_node(driver)
    assert emitted == {"armNear": "near", "armFar": "far"}


def test_every_trader_animation_has_a_seated_variant():
    """A standing 'slump' played by a seated figure lifts it out of the chair.
    The animation vocabulary is registry-invariant elsewhere; it has to stay
    total here too.

    Review round 1, Finding 3: the brief's original bare `animation in
    seated` substring check over a 4000-char window following `function
    seatedRig(` is vacuous for more than the asleep/gesture pair first
    reported - `lean` renamed to `tilt` also stays green, because the
    pre-existing standing-figure comment "...isn't already mid-lean" sits
    inside that same window. Sliced instead to the SEATED_ANIMATIONS object
    literal itself (brace-matched, not a guessed character count) and
    matched as an actual object KEY (`name:` followed by the arrow function -
    the same pattern `test_every_registry_animation_is_implemented_by_the_
    renderer` already uses for the standing table), so a nearby comment or
    docstring mentioning the word cannot satisfy it.
    """
    body = client.get("/world").text
    seated = _js_block(body, "const SEATED_ANIMATIONS = {")
    for animation in ("slump", "lean", "gesture", "breathe", "asleep"):
        assert re.search(rf"\b{animation}:\s*\(c", seated), (
            f"{animation} has no seated variant"
        )


def test_seated_dispatch_replaces_the_standing_table_not_just_names_it():
    """`playAnimation` only records a string (`char.anim = animation`) — the
    per-frame dispatch that actually calls a function lives in
    `advanceCharacters` (`ANIM[char.anim](...)`, and the hardcoded
    `ANIM.idle(...)` for the at-rest/breathing branch). Branching only inside
    `playAnimation`/`restCharacter`, the way the brief's Step 3 describes it,
    would leave every seated animation declared and never once called — the
    same defect class Override 1 warns about, one level up."""
    block = _js_block(_world_source(), "function advanceCharacters(")
    assert re.search(r'pose\s*===\s*"seated"', block), (
        "advanceCharacters never asks which table a character is animating in"
    )
    assert "SEATED_ANIMATIONS.breathe" in block, (
        "the idle/breathing branch never switches tables"
    )
    assert re.search(r"SEATED_ANIMATIONS\[char\.anim\]", block), (
        "the active-animation branch never switches tables"
    )


def test_the_two_registry_animations_most_likely_to_hit_the_trader_are_aliased_seated():
    """`world/reactions.py` dispatches `sleep` (stream_stopped,
    broadcast_ended) and `shrug` (signal_resolved) — never the literal
    `asleep`/`gesture` this ticket was asked to build. Standing `shrug` also
    writes `container.y`, exactly the hip-lift the seated table exists to
    stop. Without the alias both fall back to the whole-container standing
    table for a character sitting in a chair, and 'the trader sits down' is
    true for exactly the two names (`slump`, `lean`) that happen to already
    match a registry animation."""
    body = _world_source()
    assert "SEATED_ANIMATIONS.sleep = SEATED_ANIMATIONS.asleep;" in body
    assert "SEATED_ANIMATIONS.shrug = SEATED_ANIMATIONS.gesture;" in body


@needs_node
def test_the_sheets_loop_dispatch_is_pose_aware_too():
    """Review round 1, Finding 2a: the main per-frame dispatch
    (`ANIM[char.anim]`) was made pose-aware by this ticket, but
    `advanceCharacters`' OTHER dispatch - the `char.loopAnim` branch the
    evaluation sheet uses - was left pointed at `ANIM` only. `asleep` and
    `gesture` are not `ANIM` keys at all, so a seated sample looping either
    one would call `undefined` and throw; `slump`/`lean` would silently run
    the whole-container standing animation on a seated body instead of the
    re-authored one. Runs the REAL `advanceCharacters` (not a restated
    slice) against a fake seated character whose `loopAnim` is "asleep" - a
    name that exists only in SEATED_ANIMATIONS, never in ANIM, so a stray
    `ANIM[char.loopAnim]` fallback would throw rather than silently pass -
    and checks it does not throw and actually moves the rig, the same
    standard Override 1 set for the main dispatch.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\n"
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + "\n"
        + _js_block(source, "const EASE = {")
        + ";\n"
        + _js_const(source, "HEAD_TRAVEL")
        + "\n"
        + "function setBlink() {}\n"
        + _js_range(
            source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.shrug"
        )
        + """
        // Deliberately empty: "asleep" must resolve via SEATED_ANIMATIONS
        // alone, or this test is exercising the fallback, not the fix.
        const ANIM = {};
        function idleTick() {}
        function watchTick() {}
        function restCharacter() {}
        const ambient = { t: 0 };
        """
        + _js_block(source, "function shadowTick(")
        + "\n"
        + _js_block(source, "function advanceCharacters(")
        + "\n"
        + """
        const char = {
          loopAnim: "asleep", phase: 0, duration: 0.9, amp: 1,
          container: { pose: "seated" },
          headBaseY: -108, head: { rotation: 0, y: 0 },
          body: { rotation: 0, x: 0, y: 0 },
        };
        const cast = [char];
        advanceCharacters(0.5);
        console.log(JSON.stringify({
          headY: char.head.y, headRotation: char.head.rotation,
        }));
        """
    )
    emitted = _run_node(driver)
    assert emitted["headY"] != 0 or emitted["headRotation"] != 0, (
        "the loopAnim branch did not run SEATED_ANIMATIONS.asleep - nothing moved"
    )


@needs_node
def test_the_sheet_can_actually_reach_seatedrig():
    """Review round 1, Finding 2b: `character(style, style)` passes a
    BODY-STYLE name ("figure") where `anchorFor` wants a CHARACTER name
    ("trader") - `PLATE.cast` only has "model"/"trader", so every sample the
    sheet ever built resolved `pose: "standing"` and `seatedRig` never ran on
    this page at all. That is the surface the brief's Step 4 tells a human
    to look at to confirm the seated trader stays in the chair through every
    frame of every animation - so the one prescribed check for this ticket's
    headline claim had never once been performable. Runs the real cell-
    building logic (stubbed `character()` recorder, not a live PIXI render)
    and checks it actually calls `character("TRADER", ...)` - the one change
    that makes `anchorFor("trader")` resolve seated - and that the three
    seated-only names only ever pair with the trader, never with a body
    `ANIM` has no key for (which `advanceCharacters` would throw on)."""
    source = _world_source()
    driver = (
        _js_block(source, "const ANIM = {")
        + ";\n"
        + _js_range(
            source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.shrug"
        )
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + """
        const STYLES = ["bars", "figure", "candle", "monolith", "orb"];
        const CAST = { MODEL: "bars", TRADER: "figure" };
        const ANIM_DURATION = {};
        const app = { screen: { width: 1920, height: 960 } };
        const cast = [];
        const calls = [];
        function character(name, style) {
          calls.push([name, style]);
          const sample = {
            container: {}, head: { y: 0 }, moodTag: {}, stat: {},
          };
          return sample;
        }
        function setCharacterVisible() {}
        function setExpression() {}
        const layers = { room: {}, plate: {}, chars: { scale: { set() {} } } };
        const location = { search: "" };
        const model = {}, trader = {};
        """
        + _js_block(source, "function drawAnimationSheet(")
        + "\n"
        + """
        drawAnimationSheet();
        console.log(JSON.stringify({
          calls, animsInOrder: cast.map((s) => s.loopAnim),
        }));
        """
    )
    emitted = _run_node(driver)
    calls = emitted["calls"]
    anims = emitted["animsInOrder"]
    assert ["TRADER", "figure"] in calls, (
        "drawAnimationSheet never builds a sample named \"TRADER\" - "
        "anchorFor can never resolve pose: seated on this page"
    )
    seated_only = {"gesture", "breathe", "asleep"}
    for (name, style), anim in zip(calls, anims):
        if anim in seated_only:
            assert name == "TRADER", (
                f"{anim} is dispatched against a body ({name}, {style}) "
                "that has no ANIM key for it - advanceCharacters would throw"
            )


def test_seated_idle_shift_moves_the_torso_not_the_hips():
    """Deferred finding carried into this ticket (Task 7's Override 3):
    `IDLE_ACTIONS.shift` writes `container.x` unsnapped, left alone for
    standing because its amplitude (3) is under one CELL — a visual call,
    not a mechanical gap. For a seated character `container` IS the chair
    (`positionCharacters` anchors it to the plate's seat), so the seated
    trader gets the same ruling this whole ticket makes for every other
    animation: don't write the hips. Silence was not an acceptable answer
    here (Override 3); this is the answer."""
    block = _js_block(_world_source(), "const IDLE_ACTIONS = {")
    shift = block[block.index("shift:") : block.index("shift:") + 700]
    assert re.search(r'pose\s*===\s*"seated"', shift), (
        "the seated trader's idle shift still writes container.x - the hips"
    )
    assert "c.body.x" in shift


@needs_node
def test_seated_animations_actually_move_real_parts():
    """Override 1's whole risk, made concrete: the brief's Step 3 snippet
    used `c.torso`/`c.armFar`/`c.eyes` as stand-ins for parts that don't
    exist on the real rig (`character()` hangs `body`, `head`, `eyeL`,
    `eyeR`, `mouth`, `accents` off the container — no `torso`, no combined
    `eyes`). A SEATED_ANIMATIONS table written against the wrong names either
    throws (undefined has no `.rotation`) or silently no-ops onto a stray
    property nothing reads — and the Step 1 substring test in the brief
    passes either way, because it only checks that the names are declared.

    This builds a fake character from the names `character()`/`seatedRig`
    actually use — nothing invented — and runs every SEATED_ANIMATIONS entry
    against it via `_run_node`. An invented name throws inside the driver;
    there is no try/except here on purpose, because `_run_node`'s
    `result.returncode == 0` assertion already is the failure mode a wrong
    name produces.

    Presence isn't enough on its own (that's the test above), so each
    animation also has to move something: run at phase 0 and again at a
    non-trivial phase/amplitude, and assert a NONZERO delta on the property
    it is supposed to touch, in SCREEN space (local * CAST_SCALE) for the
    same reason `test_animation_displacement_actually_quantises_to_the_grid`
    had to stop checking local space only - `layers.chars.scale` sits between
    every one of these writes and the screen. `asleep`'s eye-closing claim is
    checked the same way: `setBlink` is real, `drawEyes` (its only outside
    dependency, and not what this ticket touches) is swapped for a recording
    stub so the call becomes an observable fact instead of an assumption.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\n"
        + _js_const(source, "CELL")
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + _js_block(source, "const EASE = {")
        + ";\n"
        + _js_const(source, "HEAD_TRAVEL")
        + "\n"
        + """
        const blinkCalls = [];
        function drawEyes(char, arousal) { blinkCalls.push(arousal); }
        """
        + _js_block(source, "function setBlink(")
        + "\n"
        + _js_range(
            source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.shrug"
        )
        + """
        // The real names character()/seatedRig hang off the rig - no
        // c.torso, no combined c.eyes. face/blinkClosed are what the real
        // setBlink actually reads before it calls drawEyes.
        function fakeChar() {
          return {
            style: "figure", headBaseY: -108,
            face: { arousal: 0.3 }, blinkClosed: false,
            body: { rotation: 0, x: 0, y: 0 },
            head: { rotation: 0, x: 0, y: 0 },
            mouth: { scale: { y: 1 } },
            armFar: { rotation: 0.5, restRotation: 0.5 },
            armNear: { rotation: -0.55, restRotation: -0.55 },
            eyeL: { x: 0 }, eyeR: { x: 0 },
          };
        }
        function snapshot(c) {
          return {
            bodyRotation: c.body.rotation, bodyX: c.body.x, bodyY: c.body.y,
            headRotation: c.head.rotation, headX: c.head.x, headY: c.head.y,
            armFarRotation: c.armFar.rotation,
          };
        }
        const results = {};
        for (const name of ["slump", "lean", "gesture", "breathe", "asleep"]) {
          const c = fakeChar();
          const before = snapshot(c);
          SEATED_ANIMATIONS[name](c, 0.7, 1.4);
          results[name] = { before, after: snapshot(c) };
        }
        console.log(JSON.stringify({
          results, blinkCalls, cell: CELL, castScale: CAST_SCALE,
        }));
        """
    )
    emitted = _run_node(driver)
    cell, cast_scale = emitted["cell"], emitted["castScale"]
    rendered_cell = cell * cast_scale

    def on_grid(v: float) -> bool:
        r = v % rendered_cell
        return r < 1e-6 or rendered_cell - r < 1e-6

    # Every animation must move at least one property - a phase/amplitude
    # change that leaves the whole rig bit-for-bit where it started is an
    # animation implemented against the wrong name.
    props = [
        "bodyRotation", "bodyX", "bodyY", "headRotation", "headX", "headY",
        "armFarRotation",
    ]
    position_props = {"bodyX", "bodyY", "headX", "headY"}
    for name in ("slump", "lean", "gesture", "breathe", "asleep"):
        before = emitted["results"][name]["before"]
        after = emitted["results"][name]["after"]
        moved = [p for p in props if before[p] != after[p]]
        assert moved, f"{name} changed nothing on the rig at p=0.7, a=1.4"
        for p in moved:
            if p in position_props:
                screen_delta = (after[p] - before[p]) * cast_scale
                assert on_grid(after[p] * cast_scale), (
                    f"{name}.{p}: {after[p]} * CAST_SCALE {cast_scale} is not "
                    f"on the {rendered_cell}px rendered cell"
                )
                assert abs(screen_delta) > 1e-6, f"{name}.{p} moved by ~0 screen px"
    asleep = emitted["results"]["asleep"]
    assert asleep["before"]["headY"] != asleep["after"]["headY"]
    assert 0.3 in emitted["blinkCalls"] or 0 in emitted["blinkCalls"], (
        "asleep never actually called setBlink - eyes never close"
    )


@needs_node
def test_seated_rest_does_not_touch_the_chair():
    """`restCharacter`'s standing branch resets `container.y/x/rotation/
    scale` - for a seated character those ARE the chair `positionCharacters`
    anchored the trader to, not something an animation should be putting
    back. The seated branch must reset everything SEATED_ANIMATIONS can
    touch (`body`, `head`, the arms, blink) and leave `container` alone."""
    source = _world_source()
    driver = (
        "const PLATE = null;\n"
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_block(source, "function restAccents(")
        + "\n"
        + """
        const blinkCalls = [];
        function setBlink(c, closed) {
          blinkCalls.push(closed); c.blinkClosed = closed;
        }
        function setGaze(c, dx, rot, headShift) {
          c.eyeL.x = c.eyeR.x = dx; c.head.rotation = rot; c.head.x = headShift;
        }
        """
        + _js_block(source, "function restCharacter(")
        + "\n"
        + _js_block(source, "function restSeatedCharacter(")
        + "\n"
        + """
        const c = {
          container: { pose: "seated", x: 41, y: 682, rotation: 0.3, alpha: 0.4,
                       scale: { set() {} } },
          body: { rotation: 0.16, x: 4, y: 2 },
          head: { rotation: 0.1, y: -104, x: 7 },
          mouth: { scale: { y: 2.6 } },
          accents: [
            { rotation: 0.9, restRotation: -0.55, x: 3, restX: 0, scale: { set() {} } },
            { rotation: -0.2, restRotation: 0.5, x: -2, restX: 0, scale: { set() {} } },
          ],
          headBaseY: -108,
          eyeL: {}, eyeR: {},
        };
        restCharacter(c);
        console.log(JSON.stringify({
          containerX: c.container.x, containerY: c.container.y,
          containerRotation: c.container.rotation, containerAlpha: c.container.alpha,
          bodyRotation: c.body.rotation, bodyX: c.body.x, bodyY: c.body.y,
          headY: c.head.y, headRotation: c.head.rotation, headX: c.head.x,
          armRestored: c.accents[0].rotation, armRestored2: c.accents[1].rotation,
          blinkCalls,
        }));
        """
    )
    emitted = _run_node(driver)
    # The chair: untouched. If restCharacter ran the standing branch instead
    # of the seated one, these would come back snapped/zeroed.
    assert emitted["containerX"] == 41
    assert emitted["containerY"] == 682
    assert emitted["containerRotation"] == 0.3
    # Everything SEATED_ANIMATIONS can move: put back.
    assert emitted["bodyRotation"] == 0
    assert emitted["bodyX"] == 0
    assert emitted["bodyY"] == 0
    assert emitted["headY"] == -108
    assert emitted["headRotation"] == 0
    assert emitted["headX"] == 0
    assert emitted["armRestored"] == -0.55
    assert emitted["armRestored2"] == 0.5
    assert False in emitted["blinkCalls"]


@needs_node
def test_the_seated_head_stays_under_the_painted_backrest():
    """CAST_SCALE went to 1.5 for the grid, not for the chair (Task 7's
    Override 2) - the seated rig was measured against the plate by hand, not
    derived from it, so this pins the arithmetic rather than re-deriving it
    from the PNG on every run. Backrest crown measured off
    world-plate-btc-eth.png at plate y=462 (screen space, since app.stage
    scale is 1 at rest); the seat anchor is plate y=682
    (PLATE.cast.trader.base_y). The skull radius is 28 LOCAL units - 42
    screen px at CAST_SCALE, not 14 - Sprint 15 review round 1 Finding 1
    applied to a body part instead of a grid cell; getting that factor wrong
    is exactly what would let the head silently poke out over the chair.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\n"
        # seatedRig's roundRect calls end in `.fill(BODY_FILL).stroke(BODY_RIM)`
        # - the fake Graphics below never reads either argument, but JS still
        # evaluates the expression, so the identifiers have to exist. The
        # forearm loop builds its own `new PIXI.Graphics()` rather than
        # reusing the `body` argument, so PIXI needs the same stub.
        + "const BODY_FILL = 0xffffff, BODY_RIM = {};\n"
        + """
        class FakeGraphics {
          roundRect() { return this; }
          fill() { return this; }
          stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + "\n"
        + _js_block(source, "function seatedRig(")
        + "\n"
        + """
        const fakeGfx = { roundRect() { return this; }, fill() { return this; },
                           stroke() { return this; } };
        const accents = [];
        const headY = seatedRig(fakeGfx, accents);
        console.log(JSON.stringify({
          headY, castScale: CAST_SCALE, armCount: accents.length,
        }));
        """
    )
    emitted = _run_node(driver)
    HEAD_RADIUS_LOCAL = 28
    SEAT_SCREEN_Y = 682
    BACKREST_CROWN_SCREEN_Y = 462
    crown_screen = (
        SEAT_SCREEN_Y
        - abs(emitted["headY"]) * emitted["castScale"]
        - HEAD_RADIUS_LOCAL * emitted["castScale"]
    )
    assert crown_screen >= BACKREST_CROWN_SCREEN_Y, (
        f"the seated head's crown lands at screen y={crown_screen}, above "
        f"the painted backrest's crown at y={BACKREST_CROWN_SCREEN_Y} - the "
        "trader pokes out over the top of the chair"
    )
    assert emitted["armCount"] == 2, "seatedRig must build exactly two arms"


@needs_node
def test_the_seated_rig_fits_inside_the_painted_seat_not_just_the_backrest():
    """Review round 1, Finding 1: the chair overflow was never CAST_SCALE's
    fault. The reviewer's own arithmetic - chest width 58 units was 87px at
    1.5 AND 78.3px at the pre-ticket 1.35 (break-even is 75/58=1.293, under
    both) - proves the rig's own local width was the defect, not the scale
    review round 1 of Task 6 chose. So this asserts the rig's real geometry,
    not a screenshot: every `roundRect` call `seatedRig` makes (thighs/waist/
    chest onto the fake `body`, and each arm's own call onto its own fake
    `PIXI.Graphics`, offset by the `arm.x` the real function sets) is
    recorded, and the LEFT and RIGHT extent across all of them - computed,
    not eyeballed - is what gets checked.

    Review round 2, Finding 4: an aggregate width check (`rig width <= seat
    width`) is position-blind - it passed while the rig, composited on the
    unchanged anchor `cast.trader.x`, sat measurably off the seat the ticket
    had just measured (left edge ~5px past the cushion's real left edge,
    ~12px of slack on the right - the reviewer's own trace). This composites
    the rig at the manifest's own anchor (`positionCharacters` places
    `container.x` there) and checks BOTH edges against the seat rect, not
    just their difference.

    The seat itself is read from `PLATE.cast.trader.seat` (the manifest,
    `api/static/world-plate-btc-eth.json`) - review round 1's correction:
    the reviewer's first instruction named that field before it existed, so
    it was added there rather than left as a literal here. This test's own
    JS driver still runs with `PLATE = null` (it is exercising the
    CELL/CAST_SCALE fallback literals, unrelated to the seat), so the
    manifest is read on the Python side, the same way `_manifest()` already
    reads `cast`/`bands`/`tubes` for every other plate-derived test in this
    file. `tests/unit/test_plate_manifest.py::test_the_seated_rig_fits_
    inside_the_manifest_seat` runs the identical check from the manifest's
    own test file, mutation-checked there against a deliberately narrowed
    seat; this one is the same claim from the rendering side.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = {};\n"
        + """
        class FakeGraphics {
          constructor() { this.calls = []; this.x = 0; }
          roundRect(x, y, w, h, r) { this.calls.push([x, w]); return this; }
          fill() { return this; }
          stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const bodyCalls = [];
        const fakeGfx = {
          roundRect(x, y, w, h, r) { bodyCalls.push([x, w]); return this; },
          fill() { return this; },
          stroke() { return this; },
        };
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + "\n"
        + _js_block(source, "function seatedRig(")
        + "\n"
        + """
        const accents = [];
        seatedRig(fakeGfx, accents);
        function range(calls, offset) {
          let lo = Infinity, hi = -Infinity;
          for (const [x, w] of calls) {
            lo = Math.min(lo, offset + x);
            hi = Math.max(hi, offset + x + w);
          }
          return [lo, hi];
        }
        let [lo, hi] = range(bodyCalls, 0);
        for (const accent of accents) {
          const [alo, ahi] = range(accent.calls, accent.x);
          lo = Math.min(lo, alo);
          hi = Math.max(hi, ahi);
        }
        console.log(JSON.stringify({ lo, hi, castScale: CAST_SCALE }));
        """
    )
    emitted = _run_node(driver)
    manifest = _manifest()
    anchor_x = manifest["cast"]["trader"]["x"]
    seat = manifest["cast"]["trader"]["seat"]
    # `positionCharacters` places `container.x` at the anchor; every local
    # coordinate `seatedRig` draws is relative to that, scaled by CAST_SCALE.
    left_screen = anchor_x + emitted["lo"] * emitted["castScale"]
    right_screen = anchor_x + emitted["hi"] * emitted["castScale"]
    assert left_screen >= seat["x"], (
        f"the seated rig's left edge is at screen x={left_screen} - "
        f"{seat['x'] - left_screen}px past the seat's left edge ({seat['x']})"
    )
    assert right_screen <= seat["x"] + seat["width"], (
        f"the seated rig's right edge is at screen x={right_screen} - "
        f"{right_screen - seat['x'] - seat['width']}px past the seat's "
        f"right edge ({seat['x'] + seat['width']})"
    )


# --- Task 9: the swell goes additive ----------------------------------------
#
# Tinting a painted plate multiplies every pixel by the tint colour, so the
# hand-painted amber desk lamp comes out teal (the ticket's own framing). The
# fix has to live in the room's own additive layer, not in world/visuals.py's
# shared ramp — retuning that would silently retune both OBS overlays too
# (KI-019's family; see tests/unit/test_visuals_ramp_is_shared.py).


def test_the_swell_is_additive_and_never_tints_the_plate():
    """Tinting a pixel-art texture turns the amber desk lamp teal, and the
    lamp is exactly the detail that makes the room feel inhabited.

    The plan's own draft of this test asserted `"ADD" in swell or "add" in
    swell` — the word "additive" alone (in a comment, with no blend mode ever
    set) satisfies that. Strengthened to the actual PIXI blend-mode
    assignment, and to the plate itself never being tinted anywhere on the
    page, not merely inside this one slice.
    """
    body = client.get("/world").text
    assert "function applySwell(" in body
    swell = body.split("function applySwell(")[1][:1500]
    assert re.search(r'\.blendMode\s*=\s*["\']add["\']', swell), (
        "applySwell must set PIXI's additive blend mode, not just mention "
        "the word 'additive' — a bare substring check can't fail here"
    )
    assert ".tint" not in swell, "the swell must brighten with alpha, not tint"
    assert "layers.plate.tint" not in body


def test_the_swell_layer_sits_above_the_room_and_below_props_and_chars():
    """Additive light must land on the room but never over the cast's faces
    — so `layers.swell` sits above `layers.room` and below `layers.chars` —
    and, because a later ticket (M2) draws live chart candles into
    `layers.props`, it must sit below `layers.props` too, or the glow would
    wash the candles out before anyone draws them.
    """
    boot = _js_block(_world_source(), "async function boot()")
    assert "layers.swell = new PIXI.Container();" in boot
    match = re.search(r"app\.stage\.addChild\(([^)]*)\);", boot)
    assert match, "boot() no longer builds the stage in one addChild call"
    order = [name.strip() for name in match.group(1).split(",")]
    assert order == ["layers.plate", "layers.room", "layers.swell",
                      "layers.props", "layers.chars"], order


def test_the_ambient_vignette_is_guarded_for_the_plate_path():
    """`vignette` (the Container) only exists on the procedural path —
    `drawProceduralRoom` is the only place that builds one; `drawPlate`'s
    success path never does. Setting `vignette.alpha` unconditionally inside
    the per-frame ticker throws the moment a plate loads, since the ticker
    runs every frame regardless of which room got drawn — this was the
    crash hiding at the brief's stale `world.html:357`. Pinned as an
    ordering/guard check, not a node run: the ticker closure captures too
    much per-frame state (dt, ambient, drift, mixChannel) to usefully
    re-run standalone.
    """
    ambient = _js_block(_world_source(), "function startAmbient()")
    assert "if (!plateReady)" in ambient
    guard = _js_block(
        ambient[ambient.index("if (!plateReady)"):], "if (!plateReady)"
    )
    assert "vignette.alpha" in guard, (
        "vignette.alpha must be set only inside the !plateReady guard"
    )
    assert "vignette.alpha" not in ambient.replace(guard, "", 1), (
        "vignette.alpha is also set OUTSIDE the guard — still crashes on the "
        "plate path"
    )
    assert ambient.index("if (!plateReady)") < ambient.index("applySwell(tier)")


def _swell_driver(*, plate_ready: bool) -> str:
    """The page's own `applySwell`, run against the real manifest's `glow`
    rects and the real `room_light` ramp — only `PIXI.Graphics` and
    `layers.swell` are stubbed, and the stub records what was actually
    drawn rather than asserting on source text.
    """
    from world.visuals import room_light

    source = _world_source()
    glow = _manifest()["glow"]
    return (
        f"const PLATE = {json.dumps({'glow': glow})};\n"
        f"const plateReady = {str(plate_ready).lower()};\n"
        f"const ROOM_LIGHT = {json.dumps([room_light(t) for t in range(4)])};\n"
        "class FakeGraphics {\n"
        "  constructor() { this.rects = []; this.fills = []; this.blendMode = null; }\n"
        "  rect(x, y, w, h) { this.rects.push([x, y, w, h]); return this; }\n"
        "  fill(opts) { this.fills.push(opts); return this; }\n"
        "}\n"
        "const PIXI = { Graphics: FakeGraphics };\n"
        "const layers = { swell: { children: [],\n"
        "  removeChildren() { this.children = []; },\n"
        "  addChild(c) { this.children.push(c); } } };\n"
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "SWELL_COLOR")
        + "\n"
        + _js_const(source, "SWELL_LIFT_TO_ALPHA")
        + "\n"
        + _js_block(source, "function applySwell(")
        + "\n"
    )


@needs_node
def test_the_swell_brightens_monotonically_across_all_four_tiers():
    """Override 4's acceptance question: a real tier-3 event still has to
    read as a moment even inside a richer, painted room. Runs the real
    `applySwell` against the real manifest's `glow` rects for every tier and
    checks what it actually painted — not a claim about the source text.
    """
    glow = _manifest()["glow"]
    assert glow, "no glow rects in the manifest — this test would be vacuous"

    driver = _swell_driver(plate_ready=True) + (
        "const out = [];\n"
        "for (let tier = 0; tier < 4; tier++) {\n"
        "  applySwell(tier);\n"
        "  out.push(layers.swell.children.map((c) => ({\n"
        "    fill: c.fills[c.fills.length - 1], blend: c.blendMode,\n"
        "  })));\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    emitted = _run_node(driver)
    assert len(emitted) == 4
    for per_tier in emitted:
        assert len(per_tier) == len(glow), (
            "applySwell must draw exactly one glow per manifest glow rect"
        )
        for rect in per_tier:
            assert rect["blend"] == "add"

    # Monotonic per rect: the alpha painted over the SAME glow rect must rise
    # with tier, never fall or flatten — the ramp's own monotonicity
    # (test_visuals_ramp_is_shared.py) is necessary but not sufficient; this
    # is the proof that applySwell actually passes it through.
    for i in range(len(glow)):
        alphas = [emitted[tier][i]["fill"]["alpha"] for tier in range(4)]
        assert alphas == sorted(alphas), (i, alphas)
        assert len(set(alphas)) > 1, f"glow rect {i} never brightens at all"

    # Colour must not drift with tier: brightening has to come from alpha,
    # not hue, or the "desk lamp stays amber at every tier" claim is false.
    for i in range(len(glow)):
        colors = {emitted[tier][i]["fill"]["color"] for tier in range(4)}
        assert len(colors) == 1, f"glow rect {i} changes colour across tiers: {colors}"


@needs_node
def test_the_no_plate_path_never_populates_the_swell_layer():
    """Override 2: with no plate, `applySwell` must be a no-op — the
    procedural room keeps the ambient tint/alpha animation it already has,
    left exactly as it is by this ticket."""
    driver = _swell_driver(plate_ready=False) + (
        "const out = [];\n"
        "for (let tier = 0; tier < 4; tier++) {\n"
        "  applySwell(tier);\n"
        "  out.push(layers.swell.children.length);\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    emitted = _run_node(driver)
    assert emitted == [0, 0, 0, 0]
