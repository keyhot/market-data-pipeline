"""The canvas itself isn't unit-testable, so these tests pin the things that
silently break a 24/7 browser source: substitution, the same-origin rule
for the renderer (KI-045), the bounded boot retry, and the textContent-only
rule that keeps event payloads from becoming markup."""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from world.light import DEFAULT_LIGHT, as_json

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
    rest, which is 99% of the airtime.

    Sprint 16 (KI-051): `shade` was renamed to `lightenTint` for this specific
    multiplicative lighten - `shade` now names the light-mix function
    `paint()` uses, a different colour rule entirely (mixes toward
    `LIGHT.warmth`/`LIGHT.ambient` rather than scaling channels by a
    factor). `"function shade(" in body` is true regardless, since that
    function still exists under its new job - it proves nothing about THIS
    feature any more, which is why it was replaced rather than kept as a
    second assertion alongside the real one.
    """
    body = client.get("/world").text
    assert "function lightenTint(" in body
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
    # Task 8 renamed `bodyTint` to `moodFill`, because what it returns stopped
    # being a tint: the mood is now the colour `paint()` lights (the product
    # `visuals._rendered()` models) rather than a multiplier laid over an
    # already-shaded figure. That the product is the RIGHT one is graded in
    # node by `test_a_mood_resolves_to_the_colour_the_room_lights_not_to_a_
    # multiplier`; this line only holds the wiring.
    assert "moodFill(" in expression, "the table is injected but never applied"
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
    # {0,13}: the branch's own `window.__worldDrawn = true;` (Task 2, set
    # after THIS gate's own draw call, not before it) is one more line inside
    # this early-returning branch than the window used to allow — widen the
    # bound, not the intent: still a *bounded* window, still failing the
    # instant an EventSource sneaks in before the return.
    preview = re.search(
        r'get\("swell"\).*?\n(?:.*\n){0,13}?\s*return;', body
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


def _shading_prelude(source: str) -> str:
    """Everything `seatedRig`/`BODIES.*` need to draw since KI-051 (Task 6):
    they now call `paint`, which calls `shade`/`mixHex`, and draw their rim
    via `rimStyle`/`rimStroke`/`BODY_RIM_SHADED` (Task 8 put `rimStyle` between
    `paint` and the stroke, so the mood can reach the rim's own albedo).
    Pulled from the real page rather than re-typed, the same reason every
    driver in this file pulls `snap` from the page instead of restating it - a
    second definition drifts from the first and stops proving anything about
    the page that actually ships.
    """
    return (
        _js_block(source, "function mixHex(") + "\n"
        + _js_block(source, "function shade(") + "\n"
        + _js_block(source, "function rampLevel(") + "\n"
        + _js_block(source, "function keyAxis(") + "\n"
        + _js_const(source, "KEY_AXIS")
        + _js_const(source, "BAND_INSET")
        + _js_const(source, "BAND_MAX")
        + _js_block(source, "function circleBand(") + "\n"
        + _js_block(source, "function bandExtent(") + "\n"
        + _js_block(source, "function bandInside(") + "\n"
        + _js_block(source, "function slideToKey(") + "\n"
        + _js_const(source, "quant")
        + _js_const(source, "MIN_BAND")
        + _js_block(source, "const litRect = ") + "\n"
        + _js_block(source, "const litCircle = ") + "\n"
        + _js_const(source, "BODY_RIM_SHADED")
        + _js_block(source, "function rimStyle(") + "\n"
        + _js_block(source, "function paint(") + "\n"
        + _js_block(source, "function rimStroke(") + "\n"
        + _js_block(source, "function rimStrokeCircle(") + "\n"
    )


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


def test_preview_modes_keep_the_room_and_plate_in_lockstep():
    """`?gallery=1` (drawGallery) and the cast sheet (drawAnimationSheet) both
    toggle layers.room and layers.plate together, on the same VOID_MODE flag —
    KI-051: judging the cast against a background it never stands in. Task 2
    flipped the default (room+plate visible unless `?void=1`), but the two
    layers still have to move together, or a preview mode could hide the
    procedural room while leaving a full pixel-art control room behind the
    specimens (or the reverse).

    A page-wide `body.count(...) >= 2` passes if both copies live in the SAME
    function and the other preview mode hides nothing — this sprint's
    can't-fail shape (`d1ad270`) applied to a count instead of a substring.
    `_js_block` slices each function separately so the two assertions are
    provably about two different regions.
    """
    body = _world_source()

    gallery = _js_block(body, "function drawGallery(")
    assert "STYLES.forEach" in gallery, "not actually the gallery body"
    assert "layers.room.visible = VOID_MODE ? false : true;" in gallery
    assert "layers.plate.visible = VOID_MODE ? false : true;" in gallery, (
        "drawGallery must gate the plate on VOID_MODE alongside the room"
    )

    sheet = _js_block(body, "function drawAnimationSheet(")
    assert "sample.loopAnim" in sheet, "not actually the animation-sheet body"
    assert "layers.room.visible = VOID_MODE ? false : true;" in sheet
    assert "layers.plate.visible = VOID_MODE ? false : true;" in sheet, (
        "drawAnimationSheet must gate the plate on VOID_MODE alongside the room"
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
    # KI-055: the cap's squash is the painted bore's own, not a guess -
    # pillarGeometry carries it so drawPillars never has to re-derive a tube.
    assert btc["boreRy"] == tubes["BTCUSDT"]["bore_ry"]
    assert eth["boreRy"] == tubes["ETHUSDT"]["bore_ry"]


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
    # No painted tube means no measured squash either - this must degrade to
    # a finite fraction of the (also-fallback) width, never NaN/undefined
    # reaching g.ellipse(), the same "no plate, no throw" rule CONTACT/
    # rampLevel hold above.
    assert first["boreRy"] == pytest.approx(54 * 0.13)


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
def test_a_throwing_builder_degrades_to_a_clean_procedural_room():
    """Review round 4: `plateReady = true` used to sit ABOVE both builders, so
    a throw inside either left it true while the catch drew the procedural
    room underneath the plate sprite already added — plate and procedural
    room composited, a darkened double-exposure. `buildGlows` is made to
    fully SUCCEED here and `buildMonitorGraphics` is what throws (PLATE.screens
    set to a non-iterable) — the shape the review actually found, where the
    first builder can complete before the second fails, so this is a claim
    about the whole try block having run, not just about one line's position
    in the text. Runs the real `drawPlate`/`drawProceduralRoom` against fakes
    that RECORD what was actually built and torn down — not a substring check
    on source order, which a moved-but-still-early assignment would still
    satisfy.
    """
    source = _world_source()
    driver = (
        "const PLATE = {glow: [{x: 10, y: 20, w: 30, h: 40}], screens: 5,\n"
        "  canvas: [1920, 960]};\n"
        'const PLATE_SRC = "/static/plate.png";\n'
        "let plateReady = false;\n"
        + _js_const(source, "GROUND")
        + _js_const(source, "CELL")
        + _js_const(source, "GLOW_COLOR")
        + _js_block(source, "function snap(")
        + "\n"
        + "let glows = [];\n"
        + "const monitorGraphics = {};\n"
        + "let floorLine, wall, floorPlane, vignette;\n"
        + """
        class FakeGraphics {
          clear() { return this; }
          rect() { return this; }
          fill() { return this; }
        }
        class FakeContainer {
          constructor() { this.children = []; }
          addChild(...kids) { this.children.push(...kids); return kids[0]; }
          removeChildren() { this.children = []; }
        }
        class FakeTexture { constructor() { this.source = {}; } }
        class FakeSprite {
          constructor(texture) {
            this.texture = texture; this.width = 0; this.height = 0;
          }
        }
        const fakeCtx = {
          createRadialGradient() { return { addColorStop() {} }; },
          fillRect() {},
        };
        const fakeCanvas = { getContext() { return fakeCtx; } };
        const document = {
          createElement() { return fakeCanvas; },
          getElementById() { return null; },
        };
        const PIXI = {
          Graphics: FakeGraphics,
          Container: FakeContainer,
          FillGradient: class { constructor(opts) { this.opts = opts; } },
          Sprite: FakeSprite,
          Texture: { from() { return new FakeTexture(); } },
          Assets: { load: async () => new FakeTexture() },
        };
        const layers = {
          plate: new FakeContainer(),
          room: new FakeContainer(),
          glow: new FakeContainer(),
          monitors: new FakeContainer(),
        };
        const app = {
          stage: new FakeContainer(), screen: { width: 1920, height: 960 },
        };
        """
        + _js_block(source, "function gradientSprite(")
        + "\n"
        + _js_block(source, "function layoutRoom(")
        + "\n"
        + _js_block(source, "function drawProceduralRoom(")
        + "\n"
        + _js_block(source, "function buildGlows(")
        + "\n"
        + _js_block(source, "function buildMonitorGraphics(")
        + "\n"
        + _js_block(source, "async function drawPlate(")
        + "\n"
        + """
        drawPlate().then(() => {
          console.log(JSON.stringify({
            plateReady,
            plateChildren: layers.plate.children.length,
            roomChildren: layers.room.children.length,
            stageChildren: app.stage.children.length,
            glowChildrenBuilt: layers.glow.children.length,
          }));
        }).catch((err) => { console.error(err.stack || err); process.exit(1); });
        """
    )
    emitted = _run_node(driver)
    assert emitted["plateReady"] is False, (
        "buildGlows succeeded before buildMonitorGraphics threw — "
        "plateReady must still end up false"
    )
    assert emitted["plateChildren"] == 0, (
        "the plate sprite added before the throw must be stripped in the "
        "catch, or the procedural room draws underneath it"
    )
    assert emitted["roomChildren"] == 3, "drawProceduralRoom must actually run"
    assert emitted["stageChildren"] == 1, "the vignette must be added to the stage"
    # buildGlows fully succeeded before the second builder threw, so its
    # one glow is still sitting in layers.glow — harmless only because
    # applyGlow's own plateReady guard (tested separately) keeps it unlit,
    # not because this catch block cleared it too.
    assert emitted["glowChildrenBuilt"] == 1


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
    (`drawCandles`, `applyGlow`, the seated trader). `world/monitors.py`
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


def test_the_tube_fill_is_capped_with_an_ellipse():
    """KI-055: the tubes are painted cylinders seen slightly from above, so
    the surface of whatever fills them is an ellipse - a `roundRect` cap is
    why they read as progress bars. `boreRy` (pillarGeometry's own measured
    field, see test_a_pillar_is_drawn_inside_the_tube_the_plate_painted) is
    what stands in for the guessed constant a comment-only check could not
    tell apart from the real fix."""
    draw_pillars = _js_block(_world_source(), "function drawPillars(")
    assert "roundRect(geo.x - 4" not in draw_pillars, (
        "the cap is still a flat roundRect"
    )
    assert "ellipse(" in draw_pillars
    assert "geo.boreRy" in draw_pillars, "the cap's squash must be the measured one"


def test_the_tube_fill_has_a_base_ellipse_too():
    """Review round 2, mutation M-B: the brief's Step 4 required the tube's
    BASE - its own floor, always visible under the fill - to get the same
    elliptical treatment as the cap, but nothing pinned it: deleting the
    base ellipse and keeping only the cap left 15 tests green.
    `test_the_tube_fill_is_capped_with_an_ellipse` above is satisfied by
    either ellipse alone, so it cannot tell one from two. `drawPillars` must
    draw exactly two ellipses per tube."""
    draw_pillars = _js_block(_world_source(), "function drawPillars(")
    assert draw_pillars.count("ellipse(") == 2, (
        "drawPillars must draw exactly two ellipses per tube - the cap AND "
        "the base"
    )


def test_drawPillars_reads_the_ramp_through_rampLevel_not_raw():
    """Review round 2, mutation M-C: swapping `rampLevel("lit")` for the
    banned raw `LIGHT.ramp.lit` inside `drawPillars` left 20 tests green.
    `rampLevel()` exists so a missing/malformed ramp degrades to 0 (the
    volume's own flat colour, via `shade()`) instead of throwing
    (world.html:249's rule) - a raw `LIGHT.ramp.*` read here is exactly the
    class of inline read that rule exists to prevent, just inside a
    per-poll draw cycle instead of at top level. Scoped to `drawPillars`'s
    own source slice, not the whole file, because the rule's comment block
    legitimately mentions `LIGHT.ramp.lit` in prose."""
    draw_pillars = _js_block(_world_source(), "function drawPillars(")
    assert "LIGHT.ramp" not in draw_pillars, (
        "drawPillars reads LIGHT.ramp directly instead of through rampLevel()"
    )
    assert draw_pillars.count("rampLevel(") == 2, (
        "drawPillars must read both the lit and shaded rungs through "
        "rampLevel()"
    )


def test_the_stacked_cell_fill_survives():
    # Deliberate from Sprint 15 (C1 Step 3b): the cells are the texture that
    # makes the fill read as volume. This ticket is the cap only.
    assert "CELL" in _js_block(_world_source(), "function drawPillars(")


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

    Final review pass: the five originally named were never the whole claim.
    `react()` routes every `trader_*` event to the seated trader and nothing
    else, so `world/reactions.REACTIONS`' own `trader_opened`/`trader_closed`/
    `trader_milestone` entries (`step`/`turn`/`cheer`) are read from the
    registry itself — not restated as a third hardcoded list that could drift
    from the first two — and required to resolve too, either as a direct
    object key or as one of the `SEATED_ANIMATIONS.x = SEATED_ANIMATIONS.y`
    aliases that follow the literal. `step`/`turn` land there; `cheer` is a
    direct key.
    """
    from world.reactions import REACTIONS

    source = _world_source()
    seated = _js_block(source, "const SEATED_ANIMATIONS = {")
    with_aliases = _js_range(
        source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.turn"
    )

    def has_seated_variant(animation: str) -> bool:
        if re.search(rf"\b{animation}:\s*\(c", seated):
            return True
        return bool(re.search(
            rf"SEATED_ANIMATIONS\.{animation} = SEATED_ANIMATIONS\.", with_aliases
        ))

    trader_vocabulary = sorted({
        animation
        for event_type, (_mood, animation) in REACTIONS.items()
        if event_type.startswith("trader_")
    })
    assert trader_vocabulary, (
        "no trader_* reactions in the registry — this test would be vacuous"
    )

    named = ("slump", "lean", "gesture", "breathe", "asleep", *trader_vocabulary)
    for animation in named:
        assert has_seated_variant(animation), f"{animation} has no seated variant"


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
            source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.turn"
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
        # `CAST_SCALE` reads the manifest now, so the identifier has to exist
        # in this driver even though the sheet does not care what it is: null
        # is the honest value here, the same as every other plate-free driver.
        "const PLATE = null;\n"
        + _js_block(source, "const ANIM = {")
        + ";\n"
        + _js_range(
            source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.turn"
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
        const layers = {
          room: {}, plate: {}, monitors: {}, nameplates: {},
          chars: { scale: { set() {} } },
        };
        const location = { search: "" };
        const model = {}, trader = {};
        const VOID_MODE = false;
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
            source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.turn"
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
            armFarRotation: c.armFar.rotation, armNearRotation: c.armNear.rotation,
          };
        }
        const results = {};
        for (const name of ["slump", "lean", "gesture", "breathe", "asleep", "cheer"]) {
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
        "armFarRotation", "armNearRotation",
    ]
    position_props = {"bodyX", "bodyY", "headX", "headY"}
    for name in ("slump", "lean", "gesture", "breathe", "asleep", "cheer"):
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
        # seatedRig's roundRect calls now go through `paint`/`rimStroke`
        # (KI-051, Task 6), which read `BODY_FILL`/`BODY_RIM` and the whole
        # shading chain below - the fake Graphics never reads a fill/stroke
        # argument, but JS still evaluates the expressions building them, so
        # every identifier they touch has to exist. The forearm loop builds
        # its own `new PIXI.Graphics()` rather than reusing the `body`
        # argument, so PIXI needs the same stub.
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + """
        class FakeGraphics {
          roundRect() { return this; }
          circle() { return this; }
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
        + _shading_prelude(source)
        + _js_block(source, "function seatedRig(")
        + "\n"
        + """
        const fakeGfx = { roundRect() { return this; }, circle() { return this; },
                           fill() { return this; }, stroke() { return this; } };
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
        # KI-051 (Task 6): `BODY_RIM` needs a real `.color` - it feeds
        # `BODY_RIM_SHADED` via `shade()`. `LIGHT` is the real page's, since
        # it does not depend on `PLATE` (`__LIGHT_JSON__` is substituted at
        # render time from the manifest, independent of this driver's own
        # `const PLATE = null`).
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + """
        class FakeGraphics {
          constructor() { this.calls = []; this.x = 0; }
          roundRect(x, y, w, h, r) { this.calls.push([x, w]); return this; }
          circle(x, y, r) { this.calls.push([x - r, 2 * r]); return this; }
          fill() { return this; }
          stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const bodyCalls = [];
        const fakeGfx = {
          roundRect(x, y, w, h, r) { bodyCalls.push([x, w]); return this; },
          circle(x, y, r) { bodyCalls.push([x - r, 2 * r]); return this; },
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
        + _shading_prelude(source)
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


def _seated_rig_bounds():
    """The trader's full rendered bounding box, in screen pixels: every real
    `roundRect`/`circle` call `seatedRig` makes (torso, thighs, each arm at
    its own `arm.x` offset — the sibling test above's own recorder, extended
    from an X-only range to a full 2D box) UNIONED with the head circle
    `character()` paints separately (`litCircle(0, 0, 28)`, offset by
    `seatedRig`'s own returned `headY` the same way `head.y = headY` does)
    — then composited at the manifest's real `sit_anchor` through the real
    `CAST_SCALE`, exactly as `positionCharacters` does for a seated pose.
    Nothing here is a magic number: every input is read out of the page or
    the manifest, the same discipline `test_the_seated_rig_fits_inside_the_
    painted_seat_not_just_the_backrest` above already established for the
    X-only, seat-only version of this same claim.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + """
        function makeRecorder(calls) {
          return {
            roundRect(x, y, w, h, r) { calls.push([x, y, w, h]); return this; },
            circle(x, y, r) { calls.push([x - r, y - r, 2 * r, 2 * r]); return this; },
            fill() { return this; },
            stroke() { return this; },
          };
        }
        // `new PIXI.Graphics()` (each arm) needs the same interface as the
        // plain recorder above, on an instance with its own `.calls`.
        class FakeGraphics {
          constructor() {
            this.calls = []; this.x = 0; this.y = 0;
            const r = makeRecorder(this.calls);
            this.roundRect = r.roundRect; this.circle = r.circle;
            this.fill = r.fill; this.stroke = r.stroke;
          }
        }
        const PIXI = { Graphics: FakeGraphics };
        const bodyCalls = [];
        const fakeGfx = makeRecorder(bodyCalls);
        const skullCalls = [];
        const fakeSkull = makeRecorder(skullCalls);
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + "\n"
        + _shading_prelude(source)
        + _js_block(source, "function seatedRig(")
        + "\n"
        + """
        const accents = [];
        const headY = seatedRig(fakeGfx, accents);
        // `character()`'s own line, verbatim: the skull is painted once per
        // non-orb style, at local (0,0,28) within `head`, which is then
        // shifted by `head.y = headY`.
        paint(fakeSkull, litCircle(0, 0, 28), BODY_FILL);
        function box(calls, offX, offY) {
          let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
          for (const [x, y, w, h] of calls) {
            x0 = Math.min(x0, offX + x); y0 = Math.min(y0, offY + y);
            x1 = Math.max(x1, offX + x + w); y1 = Math.max(y1, offY + y + h);
          }
          return [x0, y0, x1, y1];
        }
        let [x0, y0, x1, y1] = box(bodyCalls, 0, 0);
        for (const accent of accents) {
          const [ax0, ay0, ax1, ay1] = box(accent.calls, accent.x, accent.y);
          x0 = Math.min(x0, ax0); y0 = Math.min(y0, ay0);
          x1 = Math.max(x1, ax1); y1 = Math.max(y1, ay1);
        }
        const [hx0, hy0, hx1, hy1] = box(skullCalls, 0, headY);
        x0 = Math.min(x0, hx0); y0 = Math.min(y0, hy0);
        x1 = Math.max(x1, hx1); y1 = Math.max(y1, hy1);
        console.log(JSON.stringify({ x0, y0, x1, y1, castScale: CAST_SCALE }));
        """
    )
    emitted = _run_node(driver)
    manifest = _manifest()
    sit_anchor = manifest["cast"]["trader"]["sit_anchor"]
    scale = emitted["castScale"]
    return {
        "x": sit_anchor["x"] + emitted["x0"] * scale,
        "y": sit_anchor["y"] + emitted["y0"] * scale,
        "w": (emitted["x1"] - emitted["x0"]) * scale,
        "h": (emitted["y1"] - emitted["y0"]) * scale,
    }


def _rects_intersect(a, b):
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


@needs_node
def test_the_name_trader_surface_does_not_intersect_the_seated_rig():
    """Review round 2's own criterion, made measurable: the surface
    `name-trader` sits on must not intersect the trader's rendered bounds.
    `desk-plate-trader` failed this (the rig's real geometry, composited at
    the manifest's own anchor, lands squarely inside it) - that is why
    `name-trader` now names `desk-face-trader` instead. Checked here from
    the rendering side, with the real seated-rig geometry and CAST_SCALE
    (`_seated_rig_bounds` above), not eyeballed from a screenshot.
    """
    rig = _seated_rig_bounds()
    manifest = _manifest()
    surface = next(
        s for s in manifest["text_surfaces"] if s["id"] == "desk-face-trader"
    )
    assert not _rects_intersect(rig, surface), (
        f"the seated rig's rendered bounds {rig} intersect desk-face-trader "
        f"{surface} — the fix this ticket exists to make did not land"
    )

    # Mutation check: the SAME rendered rig, against the surface this fix
    # replaced, must be caught as intersecting — proving this check actually
    # distinguishes a bad surface from a good one, not just returning False
    # for everything.
    old_surface = next(
        s for s in manifest["text_surfaces"] if s["id"] == "desk-plate-trader"
    )
    assert _rects_intersect(rig, old_surface), (
        "desk-plate-trader no longer reads as intersecting the rig — either "
        "the plate or the rig changed, and this mutation check is no longer "
        "anchored to a real, known-bad surface"
    )


# --- Task 9: the glow goes additive -----------------------------------------
#
# Tinting a painted plate multiplies every pixel by the tint colour, so the
# hand-painted amber desk lamp comes out teal (the ticket's own framing). The
# fix has to live in the room's own additive layer, not in world/visuals.py's
# shared ramp — retuning that would silently retune both OBS overlays too
# (KI-019's family; see tests/unit/test_visuals_ramp_is_shared.py).
#
# Fix round 1 (coordinator review): `applyGlow` now takes the EASED
# `ambient.light.lift`, not a raw tier (Finding 1), and the glow `Graphics`
# are built once, when the plate becomes ready, rather than every tick
# (Finding 2) — see `buildGlows`/`applyGlow` in api/templates/world.html.


def test_the_glow_is_additive_and_never_tints_the_plate():
    """Tinting a pixel-art texture turns the amber desk lamp teal, and the
    lamp is exactly the detail that makes the room feel inhabited.

    The plan's own draft of this test asserted `"ADD" in swell or "add" in
    swell` — the word "additive" alone (in a comment, with no blend mode ever
    set) satisfies that. Strengthened to the actual PIXI blend-mode
    assignment. Fix round 1 moved the allocation (and so the `blendMode`
    assignment) out of `applyGlow` into `buildGlows` (Finding 2), so
    this checks the function that actually sets it now, not the one that
    merely mentions "additive" in a comment.
    """
    body = client.get("/world").text
    assert "function buildGlows(" in body
    build = _js_block(body, "function buildGlows(")
    assert re.search(r'\.blendMode\s*=\s*["\']add["\']', build), (
        "buildGlows must set PIXI's additive blend mode, not just "
        "mention the word 'additive' — a bare substring check can't fail here"
    )
    assert ".tint" not in build, "the glow must brighten with alpha, not tint"
    apply_glow = _js_block(body, "function applyGlow(")
    assert ".tint" not in apply_glow
    assert "layers.plate.tint" not in body


def test_the_glow_layer_sits_above_the_room_and_below_monitors_props_and_chars():
    """Additive light must land on the room but never over the cast's faces
    — so `layers.glow` sits above `layers.room` and below `layers.chars` —
    and, because M2's live chart candles live in their OWN layer
    (`layers.monitors`, a stage sibling with a build-once/never-wiped
    contract — deliberately not sharing `layers.props`, whose contract is
    per-cycle wipe-and-rebuild), the glow layer must sit below that layer
    too, or it would wash the candles out. `layers.nameplates` (Sprint 16
    Task 10) sits LAST — a name tag hidden behind the very figure it names
    (the trader's seated head covered all but ~15px of `desk-plate-trader`
    at this cast scale, found by actually rendering the room — `name-trader`
    has since moved to `desk-face-trader`, review round 2, but the layer
    stays last regardless: it is what keeps a label from inheriting a
    reaction's shake/hop) is as much "text where it is not necessary" as
    hovering in mid-air was.
    """
    boot = _js_block(_world_source(), "async function boot()")
    assert "layers.glow = new PIXI.Container();" in boot
    assert "layers.monitors = new PIXI.Container();" in boot
    match = re.search(r"app\.stage\.addChild\(([^)]*)\);", boot)
    assert match, "boot() no longer builds the stage in one addChild call"
    order = [name.strip() for name in match.group(1).split(",")]
    assert order == ["layers.plate", "layers.room", "layers.glow",
                      "layers.monitors", "layers.props", "layers.chars",
                      "layers.nameplates"], order


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
    assert ambient.index("if (!plateReady)") < ambient.index(
        "applyGlow(ambient.light.lift)"
    )


def test_the_glow_builds_its_graphics_once_not_inside_the_ticker():
    """Finding 2 (coordinator, fix round 1): the old `applyGlow` allocated
    `new PIXI.Graphics()` * len(PLATE.glow) every tick — rebuilding geometry
    ~60x/sec, on a box that also encodes 1080p, with no `destroy()` anywhere
    in the file. `ambient.drift`'s dots and the vignette already show the
    right shape for this ticker (build once, mutate a property per tick);
    the glow's own `Graphics` now follow it: allocated once in `drawPlate`'s
    success path (`buildGlows`), mutated (only `.alpha`) inside the ticker.
    """
    body = _world_source()
    draw_plate = _js_block(body, "async function drawPlate(")
    assert "buildGlows();" in draw_plate, (
        "buildGlows must be called once, when the plate becomes ready"
    )
    ambient_fn = _js_block(body, "function startAmbient()")
    ticker = _js_block(ambient_fn, "app.ticker.add(")
    assert "new PIXI.Graphics()" not in ticker, (
        "a Graphics allocation inside the per-frame ticker rebuilds "
        "geometry ~60x/sec"
    )
    assert "buildGlows(" not in ticker, (
        "buildGlows must not be called from inside the ticker"
    )
    apply_glow = _js_block(body, "function applyGlow(")
    assert "new PIXI.Graphics()" not in apply_glow, (
        "applyGlow must only mutate .alpha on the pre-built glows"
    )
    assert "buildGlows(" not in apply_glow, (
        "applyGlow must not call buildGlows() itself either — that "
        "would rebuild the geometry every tick just as surely as inlining "
        "the allocation would"
    )


def test_glow_color_matches_the_shared_accent():
    """Finding 3 (coordinator, fix round 1): `GLOW_COLOR` is a fixed,
    room-local accent — deliberately NOT injected from `world/visuals.py`
    (Override 1: the fewer things this canvas reads out of the shared theme
    module, the smaller the KI-019 surface it can retune by accident). But
    it was chosen to match `PALETTE["accent"]` by eye, and nothing enforced
    that beyond a comment — exactly the "loose lighting constant in a
    template" shape `world/visuals.py`'s own module docstring warns about.

    Decision: pin the equality with a test rather than inject the palette
    value, because injecting it would add a new placeholder to
    `api/main.py`'s `_THEME_REPLACEMENTS` (the shared injection surface
    Override 1 says to keep changes out of) for a value that is deliberately
    room-local — the glow's own comment is explicit that it must NOT track
    `light.warmth`'s colour temperature, i.e. it is meant to be independent
    of the shared theme at runtime. A test pin gets the same drift
    protection (a retune of either constant without the other fails here,
    loudly, in the file a developer is already looking at) without adding a
    third page/overlay dependency on a value nothing else needs.
    """
    from world.visuals import PALETTE

    source = _world_source()
    match = re.search(r"const GLOW_COLOR = (0x[0-9a-fA-F]+);", source)
    assert match, "GLOW_COLOR is no longer a one-line hex const"
    glow_color = int(match.group(1), 16)
    accent = int(PALETTE["accent"].lstrip("#"), 16)
    assert glow_color == accent, (
        f"GLOW_COLOR 0x{glow_color:06x} no longer matches "
        f"PALETTE['accent'] {PALETTE['accent']}"
    )


def test_no_placeholder_shaped_token_hides_in_a_comment():
    """Finding 4 (coordinator, fix round 1): `_render_template` (api/main.py)
    is a blind whole-document `.replace()` — every occurrence of a
    placeholder's literal string is substituted, not just its own declared
    site. Task 9 shipped exactly this defect once already: a comment reading
    "...never __THEME_VARS__ or..." reused a REAL key's exact string and was
    silently rewritten mid-sentence with the injected CSS block — caught only
    because the result happened to break JS syntax
    (`test_the_rendered_page_is_valid_javascript`).

    `test_no_unsubstituted_placeholders_remain` (this file) checks the
    RENDERED page for `__[A-Z_]+__`-shaped leftovers, but a token that
    collides with a REAL key doesn't leave one — the collision IS what gets
    substituted away, so nothing placeholder-shaped survives to be found
    there. Empirically confirmed against this exact bug before fixing it:
    the buggy revision passed `test_no_unsubstituted_placeholders_remain`.
    The only check that can catch a collision, by construction, has to run
    on the RAW template before substitution — and since several placeholders
    legitimately appear more than once in this page (`__ROOM_LIGHT_JSON__`
    seeds both the `const ROOM_LIGHT` declaration and `ambient.light`'s
    initial value), "exactly once" isn't a safe invariant either. What IS
    always true: a substitution site is code (an assignment, a JSON
    injection point) — never English prose inside a comment.

    Final review pass: `_render_template` (api/main.py) is not `world.html`'s
    alone — `overlay_signals.html`, `overlay_events.html`, `dashboard.html`,
    `chart.html`, `charts.html` and `standby.html` all go through the same
    blind whole-document `.replace()`, so the collision this test exists to
    catch is exactly as live in any of them. And these templates lean on CSS
    `/* ... */` block comments at least as much as `//` line comments (every
    one of them has at least one `/* */`, several have five or more) — a
    scan that only ever looked at text after `//` was blind to a placeholder-
    shaped token sitting inside a `<style>` block. Both gaps are closed here:
    every `api/templates/*.html` file, and both comment syntaxes.
    """
    templates_dir = Path(__file__).resolve().parents[2] / "api" / "templates"
    paths = sorted(templates_dir.glob("*.html"))
    assert paths, "no templates found under api/templates/ — this test would be vacuous"

    token = re.compile(r"__[A-Z_]+__")

    for path in paths:
        source = path.read_text()

        # Block comments span lines and are not anchored to line starts the
        # way the // scan below is, so they're checked as their own pass.
        for block in re.finditer(r"/\*.*?\*/", source, re.S):
            found = token.search(block.group(0))
            assert not found, (
                f"{path.name}: block comment contains a placeholder-shaped "
                f"token {found.group(0)!r} — _render_template's blind "
                "whole-document .replace() will silently rewrite it if it "
                "matches a real placeholder key"
            )

        for lineno, line in enumerate(source.splitlines(), start=1):
            idx = line.find("//")
            if idx == -1:
                continue
            found = token.search(line[idx:])
            assert not found, (
                f"{path.name}:{lineno}: comment contains a placeholder-shaped "
                f"token {found.group(0)!r} — _render_template's blind "
                "whole-document .replace() will silently rewrite it if it "
                "matches a real placeholder key"
            )


def _glow_driver(*, plate_ready: bool) -> str:
    """The page's own `buildGlows`/`applyGlow`, run against the real
    manifest's `glow` rects — only `PIXI.Graphics` and `layers.glow` are
    stubbed, and the stub records what was actually drawn/mutated rather
    than asserting on source text.
    """
    source = _world_source()
    glow = _manifest()["glow"]
    return (
        f"const PLATE = {json.dumps({'glow': glow})};\n"
        f"const plateReady = {str(plate_ready).lower()};\n"
        "class FakeGraphics {\n"
        "  constructor() {\n"
        "    this.rects = []; this.fills = []; this.blendMode = null; this.alpha = 1;\n"
        "  }\n"
        "  rect(x, y, w, h) { this.rects.push([x, y, w, h]); return this; }\n"
        "  fill(opts) { this.fills.push(opts); return this; }\n"
        "}\n"
        "const PIXI = { Graphics: FakeGraphics };\n"
        "const layers = { glow: { children: [],\n"
        "  removeChildren() { this.children = []; },\n"
        "  addChild(c) { this.children.push(c); } } };\n"
        "let glows = [];\n"
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "GLOW_COLOR")
        + "\n"
        + _js_const(source, "GLOW_LIFT_TO_ALPHA")
        + "\n"
        + _js_block(source, "function buildGlows(")
        + "\n"
        + _js_block(source, "function applyGlow(")
        + "\n"
    )


@needs_node
def test_the_glow_brightens_monotonically_across_all_four_tiers():
    """Override 4's acceptance question: a real tier-3 event still has to
    read as a moment even inside a richer, painted room. Once the room has
    fully settled at a tier (the EASE itself is tested separately, in
    `test_the_glow_eases_in_step_with_the_rest_of_the_room_not_a_frame`),
    the final brightness at each tier must be strictly higher than the
    last. Feeds `applyGlow` each tier's own settled `lift` (from the real
    `room_light` ramp) and checks what it actually painted — not a claim
    about the source text.
    """
    from world.visuals import room_light

    glow = _manifest()["glow"]
    assert glow, "no glow rects in the manifest — this test would be vacuous"

    driver = _glow_driver(plate_ready=True) + (
        "buildGlows();\n"
        f"const lifts = {json.dumps([room_light(t)['lift'] for t in range(4)])};\n"
        "const out = [];\n"
        "for (const lift of lifts) {\n"
        "  applyGlow(lift);\n"
        "  out.push(layers.glow.children.map((c) => ({\n"
        "    alpha: c.alpha, fill: c.fills[c.fills.length - 1], blend: c.blendMode,\n"
        "  })));\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    emitted = _run_node(driver)
    assert len(emitted) == 4
    for per_tier in emitted:
        assert len(per_tier) == len(glow), (
            "buildGlows must build exactly one glow per manifest glow rect"
        )
        for rect in per_tier:
            assert rect["blend"] == "add"

    # Monotonic per rect: the SETTLED alpha painted over the SAME glow rect
    # must rise with tier, never fall or flatten — the ramp's own
    # monotonicity (test_visuals_ramp_is_shared.py) is necessary but not
    # sufficient; this is the proof that applyGlow actually passes it
    # through.
    for i in range(len(glow)):
        alphas = [emitted[tier][i]["alpha"] for tier in range(4)]
        assert alphas == sorted(alphas), (i, alphas)
        assert len(set(alphas)) > 1, f"glow rect {i} never brightens at all"

    # Colour must not drift with tier: brightening has to come from alpha,
    # not hue, or the "desk lamp stays amber at every tier" claim is false.
    for i in range(len(glow)):
        colors = {emitted[tier][i]["fill"]["color"] for tier in range(4)}
        assert len(colors) == 1, f"glow rect {i} changes colour across tiers: {colors}"


@needs_node
def test_build_glows_is_idempotent():
    """Only one caller today (drawPlate's try block, once) — but the review
    that moved `plateReady` below the builders also asked for this: a second
    call, whether from a future retry path or from a throw that unwinds and
    gets attempted again, must REPLACE the glows in `layers.glow`, not stack
    a second set behind the first at double the intended brightness."""
    glow = _manifest()["glow"]
    driver = _glow_driver(plate_ready=True) + (
        "buildGlows();\n"
        "buildGlows();\n"
        "console.log(JSON.stringify(layers.glow.children.length));\n"
    )
    emitted = _run_node(driver)
    assert emitted == len(glow), (
        "a second buildGlows() call left the first call's glows behind "
        "instead of clearing layers.glow first"
    )


@needs_node
def test_the_no_plate_path_never_populates_the_glow_layer():
    """Override 2: with no plate, `buildGlows` is never invoked at all
    (`drawPlate`'s `!PLATE_SRC`/catch branches call `drawProceduralRoom`
    instead, and never reach the `plateReady = true; buildGlows();`
    pair), so `layers.glow` never gets a child to light in the first place.

    Final review pass: this test's own driver does NOT establish that
    `applyGlow` is a no-op regardless of what it's fed — with `glows` empty
    throughout (buildGlows was never called), the `for (const glow of
    glows)` loop is a no-op whether or not the `if (!plateReady) return;`
    guard even exists. That broader claim is the separate
    `test_apply_glow_stays_dark_when_the_plate_is_not_ready_even_if_built`
    below, which builds glows UNCONDITIONALLY first so there is something
    for the guard to actually stop. This one just pins the realistic call
    pattern: with no plate, the layer stays empty."""
    driver = _glow_driver(plate_ready=False) + (
        "const out = [];\n"
        "for (const lift of [0, 0.05, 0.12, 0.22]) {\n"
        "  applyGlow(lift);\n"
        "  out.push(layers.glow.children.length);\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    emitted = _run_node(driver)
    assert emitted == [0, 0, 0, 0]


@needs_node
def test_apply_glow_stays_dark_when_the_plate_is_not_ready_even_if_built():
    """The realistic no-plate path never calls `buildGlows` at all
    (see the test above), so on its own that leaves `applyGlow`'s
    `if (!plateReady) return;` guard unreachable — a `glows` array
    that already had children in it (say, from a future edit that calls
    `buildGlows` somewhere it shouldn't) would otherwise get lit up by
    `applyGlow` regardless of `plateReady`, since the guard is the only
    thing standing between "no plate" and "light it anyway". Builds the
    glows UNCONDITIONALLY here — deliberately bypassing the real call
    pattern — so the guard itself, not just today's caller discipline, is
    what this test is pinning.
    """
    driver = _glow_driver(plate_ready=False) + (
        "buildGlows();\n"
        "const before = layers.glow.children.map((c) => c.alpha);\n"
        "applyGlow(0.22);\n"
        "const after = layers.glow.children.map((c) => c.alpha);\n"
        "console.log(JSON.stringify({ before, after }));\n"
    )
    result = _run_node(driver)
    assert result["before"], "buildGlows built nothing to test the guard against"
    assert result["before"] == result["after"], (
        "applyGlow must leave the glows exactly as buildGlows left "
        "them when plateReady is false — it changed them instead"
    )


def _tick_snippet(source: str) -> str:
    """The ticker's own contiguous tier/ease/`applyGlow` call — sliced
    verbatim between two anchors rather than restated, so a mutated CALL
    SITE (not just a mutated `applyGlow`) shows up here. Not brace-delimited
    (it's a `const`, a `for` block, a dead `if` block, then a bare call), so
    plain string slicing between the real first and last lines does the job
    `_js_block` does for a single construct.
    """
    start = source.index(
        "const tier = Math.max(0, Math.min(3, Math.round(ambient.tier)));"
    )
    end_marker = "applyGlow(ambient.light.lift);"
    end = source.index(end_marker, start) + len(end_marker)
    return source[start:end]


@needs_node
def test_the_glow_eases_in_step_with_the_rest_of_the_room_not_a_frame():
    """Finding 1 (coordinator, fix round 1): `applyGlow` used to read
    `ROOM_LIGHT[tier].lift` directly — the ramp's raw TARGET — while the
    very same ticker block eases `ambient.light.lift` toward that target
    over ~4s ("a swell you can watch arrive, not a cut"). `ambient.tier`
    jumps discretely (`readAmbient` takes the symbols' current max each
    poll), so the old code snapped the glow to full tier-3 alpha in ONE
    FRAME while the vignette/warmth/background dimming beside it kept
    easing for four more seconds — two speeds for one swell, which the
    sprint's acceptance criterion (a tier-3 event reads as a MOMENT, not a
    flash) can't survive as a pop.

    Runs the REAL ticker call site verbatim (`_tick_snippet` — the tier
    lookup, the ease loop, and the actual `applyGlow(ambient.light.lift)`
    call, not a restatement of any of it) beside the real
    `applyGlow`/`buildGlows`, simulating a hard tier-0-to-tier-3 jump
    one 1/60s frame at a time and checking what got written each frame. A
    regression at the CALL SITE (reverting to `applyGlow(want.lift)`, say)
    would be invisible to a test that only re-ran the ease arithmetic
    itself; this runs the line that actually calls it.
    """
    from world.visuals import room_light

    source = _world_source()
    match = re.search(r"const GLOW_LIFT_TO_ALPHA = ([\d.]+);", source)
    assert match, "GLOW_LIFT_TO_ALPHA is no longer a one-line numeric const"
    glow_lift_to_alpha = float(match.group(1))

    driver = (
        _glow_driver(plate_ready=True)
        + f"const ROOM_LIGHT = {json.dumps([room_light(t) for t in range(4)])};\n"
        + "buildGlows();\n"
        # `ambient.tier = 3` forces the tick snippet's own tier/want lookup
        # to a hard jump straight to tier 3; `.light` starts at the calm end,
        # same as the real `ambient` object's own initializer
        # (`light: { ...__ROOM_LIGHT_JSON__[0] }`).
        + "const ambient = { tier: 3, light: { ...ROOM_LIGHT[0] } };\n"
        + "const dt = 1 / 60;\n"
        + "const alphas = [];\n"
        + "for (let frame = 0; frame < 1800; frame++) {\n"
        + _tick_snippet(source)
        + "\n"
        + "  alphas.push(layers.glow.children[0].alpha);\n"
        + "}\n"
        + "console.log(JSON.stringify({\n"
        + "  first: alphas[0], early: alphas[5], mid: alphas[15], last: alphas[1799],\n"
        + "}));\n"
    )
    result = _run_node(driver)
    settled = min(1, room_light(3)["lift"] * glow_lift_to_alpha)

    # Frame 0 must not already be near the settled brightness — a one-frame
    # jump to (near) `settled` IS the pop this test exists to catch.
    assert result["first"] < settled * 0.5, (
        f"frame 0 alpha {result['first']} is already within striking "
        f"distance of the settled value {settled} — the glow is popping, "
        "not easing"
    )
    # Still climbing well into the window — not flat, not already arrived.
    assert result["first"] < result["early"] < result["mid"], result
    # And it has to actually GET somewhere: settle near the real target
    # within the simulated window (30s of frames at 60fps is many time
    # constants past the ~4s ease).
    assert abs(result["last"] - settled) < 0.02, (result["last"], settled)


# --- M2: live candles in the painted monitors --------------------------------
# world/monitors.py already decided every rule (bars_that_fit/is_drawable/
# stale_alpha_for) under test; this ticket only wires the injected constants
# and functions into drawCandles()/pollBars() and never re-derives them.


def test_candles_are_polled_not_streamed():
    """KI-013: eight browser sources share one Chromium network stack with a
    6-connections-per-origin limit. A second SSE stream on this page is how the
    room went blank for a week."""
    body = client.get("/world").text
    assert "/bars/" in body
    candles = body.split("function pollBars(")[1][:1500]
    assert "EventSource" not in candles


def test_an_empty_or_undrawable_screen_renders_dark_glass():
    """Never an empty axis, never a flat line at zero: the plate's own painted
    dark glass is the honest rendering of 'no data'."""
    body = client.get("/world").text
    draw = body.split("function drawCandles(")[1][:2000]
    assert "isDrawable(screen)" in draw
    # The rule is injected, not restated: one definition, per KI-019.
    assert "function isDrawable(" in body
    assert "function barsThatFit(" in body


def test_stale_bars_dim_instead_of_posing_as_the_present():
    """AMENDED 2026-08-26 (M1). The two substring asserts this test used to
    carry both ship inside `__MONITOR_RULES_JSON__` whether or not the page
    calls the rule, so they passed on a page that restated the threshold
    inline - the second copy of a rule that KI-019 is named after. Pin the
    CALL, and pin that the page does not re-derive it."""
    body = client.get("/world").text
    draw = body.split("function drawCandles(")[1][:2000]
    assert "staleAlpha(" in draw
    assert "function staleAlpha(" in body
    assert "stale_after_seconds" not in draw, "the threshold is read, not restated"


def test_the_monitor_graphics_are_built_once_not_per_poll_or_per_tick():
    """B2's own lesson, restated for this ticket: a `new PIXI.Graphics()`
    inside the per-poll draw function or the poll loop itself is the exact
    cost B2 had to walk back on a box that also encodes 1080p. It must be
    built exactly once, from the plate-success path, mirroring
    `buildGlows()`."""
    body = _world_source()
    draw = _js_block(body, "function drawCandles(")
    assert "new PIXI.Graphics()" not in draw, (
        "drawCandles must reuse a Graphics keyed by screen.id, not build one"
    )
    assert "addChild" not in draw, (
        "KI-052: drawCandles runs once per screen per poll, forever - "
        "anything it adds to a layer (e.g. a quad mask) leaks unboundedly. "
        "The mask belongs in buildMonitorGraphics, built once, like `g` "
        "itself"
    )
    poll = _js_block(body, "async function pollBars(")
    assert "new PIXI.Graphics()" not in poll

    build = _js_block(body, "function buildMonitorGraphics(")
    assert "new PIXI.Graphics()" in build
    assert "layers.monitors.addChild(" in build, (
        "monitor graphics must land in their OWN layer (layers.monitors), "
        "above layers.glow so it does not wash out the candles, and NOT in "
        "layers.props - that container is wiped and rebuilt every draw() "
        "cycle (drawPillars), a different lifecycle than build-once"
    )
    assert "layers.props" not in build, (
        "buildMonitorGraphics must not touch layers.props at all"
    )

    # Built alongside buildGlows(), on the plate SUCCESS path only.
    draw_plate = _js_block(body, "async function drawPlate(")
    assert "buildMonitorGraphics()" in draw_plate
    failure_path = _js_block(draw_plate[draw_plate.index("catch (") :], "catch (")
    assert "buildMonitorGraphics()" not in failure_path


def _monitor_driver(*, plate_ready: bool) -> str:
    """The page's own `buildMonitorGraphics`/`drawCandles`/`pollBars`, run
    against the real manifest's `screens`, with only `PIXI.Graphics`,
    `layers.monitors` and `fetch` stubbed. The stub records what was actually
    built/drawn/fetched rather than asserting on source text — the
    `_glow_driver` pattern applied to the monitors. `layers.monitors` is the
    monitors' OWN container (Finding 1, review round 1) — not `layers.props`,
    which has a different, per-cycle wipe-and-rebuild contract.
    """
    source = _world_source()
    screens = _manifest()["screens"]
    return (
        f"const PLATE = {json.dumps({'screens': screens})};\n"
        f"const plateReady = {str(plate_ready).lower()};\n"
        "let graphicsBuilt = 0;\n"
        "class FakeGraphics {\n"
        "  constructor() {\n"
        "    graphicsBuilt++;\n"
        "    this.rects = []; this.fills = []; this.cleared = 0; this.alpha = 1;\n"
        "  }\n"
        "  clear() { this.cleared++; this.rects = []; this.fills = []; return this; }\n"
        "  rect(x, y, w, h) { this.rects.push([x, y, w, h]); return this; }\n"
        "  poly(points) { this.polyPoints = points; return this; }\n"
        "  fill(color) { this.fills.push(color); return this; }\n"
        "}\n"
        "const PIXI = { Graphics: FakeGraphics };\n"
        "const layers = { monitors: { children: [],\n"
        "  removeChildren() { this.children = []; },\n"
        "  addChild(...items) { this.children.push(...items); } } };\n"
        "const monitorGraphics = {};\n"
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "MONITOR_RULES")
        + "\n"
        + _js_block(source, "function barsThatFit(")
        + "\n"
        + _js_block(source, "function isDrawable(")
        + "\n"
        + _js_block(source, "function staleAlpha(")
        + "\n"
        + _js_block(source, "function buildMonitorGraphics(")
        + "\n"
        + _js_block(source, "function drawCandles(")
        + "\n"
    )


FRESH_TS = "2099-01-01T00:00:00+00:00"
STALE_TS = "2020-01-01T00:00:05+00:00"


def _sample_bars(*, ts_iso: str, flat: bool = False) -> list:
    """A short, drawable run of 1m bars, oldest first — the shape
    `get_price_bars` actually returns."""
    if flat:
        return [
            {"timestamp": ts_iso, "open": 100, "high": 100, "low": 100, "close": 100}
            for _ in range(20)
        ]
    bars = []
    price = 100.0
    for i in range(20):
        o, c = price, price + (1 if i % 2 == 0 else -1)
        bars.append({
            "timestamp": ts_iso if i == 19 else "2020-01-01T00:00:00+00:00",
            "open": o, "high": max(o, c) + 1, "low": min(o, c) - 1, "close": c,
        })
        price = c
    return bars


@needs_node
def test_drawCandles_executed_stays_dark_glass_for_too_small_no_data_and_flat_span():
    """Three honest-state paths, run for real rather than grepped: a screen
    below the legibility floor, a drawable screen with no bars, and a
    drawable screen whose bars have zero price span. All three must draw
    nothing — never an empty axis, never a flat line at zero."""
    driver = _monitor_driver(plate_ready=True) + (
        "buildMonitorGraphics();\n"
        "const small = { id: 'small', x: 0, y: 0, w: 40, h: 30, role: 'chart',"
        " symbol: 'BTCUSDT' };\n"
        "const drawable = PLATE.screens[0];\n"
        f"const freshBars = {json.dumps(_sample_bars(ts_iso=FRESH_TS))};\n"
        f"const flatBars = {json.dumps(_sample_bars(ts_iso=FRESH_TS, flat=True))};\n"
        "monitorGraphics.small = new PIXI.Graphics();\n"
        # Snapshot rects/cleared as plain numbers IMMEDIATELY after each call.
        # `drawable` shares ONE Graphics across the noData/flatSpan cases
        # (buildMonitorGraphics keys by screen.id, and both reuse
        # PLATE.screens[0]) - holding onto the *object* instead and reading it
        # only at the end would report the state after the LAST draw for
        # every earlier case too, since it is the same reference throughout.
        "drawCandles(small, freshBars);\n"
        "const tooSmall = { rects: monitorGraphics.small.rects.length,"
        " cleared: monitorGraphics.small.cleared };\n"
        "drawCandles(drawable, []);\n"
        "const noData = { rects: monitorGraphics[drawable.id].rects.length,"
        " cleared: monitorGraphics[drawable.id].cleared };\n"
        "drawCandles(drawable, flatBars);\n"
        "const flatSpan = { rects: monitorGraphics[drawable.id].rects.length,"
        " cleared: monitorGraphics[drawable.id].cleared };\n"
        "console.log(JSON.stringify({ tooSmall, noData, flatSpan }));\n"
    )
    emitted = _run_node(driver)
    assert emitted["tooSmall"]["rects"] == 0, "too small a screen still drew candles"
    assert emitted["tooSmall"]["cleared"] >= 1, "dark glass still clears stale geometry"
    assert emitted["noData"]["rects"] == 0, "no bars still drew candles"
    assert emitted["flatSpan"]["rects"] == 0, "a zero price span still drew candles"


@needs_node
def test_drawCandles_executed_draws_fresh_candles_and_dims_stale_ones():
    """The success path, run for real: a drawable screen with real bars draws
    at least one candle at full opacity when the newest bar is fresh, and the
    injected `staleAlpha` — not a second copy of the threshold — dims it when
    the newest bar is old."""
    from world.monitors import STALE_ALPHA

    driver = _monitor_driver(plate_ready=True) + (
        "buildMonitorGraphics();\n"
        "const drawable = PLATE.screens[0];\n"
        f"const freshBars = {json.dumps(_sample_bars(ts_iso=FRESH_TS))};\n"
        f"const staleBars = {json.dumps(_sample_bars(ts_iso=STALE_TS))};\n"
        "drawCandles(drawable, freshBars);\n"
        "const fresh = { rects: monitorGraphics[drawable.id].rects.length,"
        " fills: monitorGraphics[drawable.id].fills.length,"
        " alpha: monitorGraphics[drawable.id].alpha };\n"
        "drawCandles(drawable, staleBars);\n"
        "const stale = { rects: monitorGraphics[drawable.id].rects.length,"
        " alpha: monitorGraphics[drawable.id].alpha };\n"
        "console.log(JSON.stringify({ fresh, stale }));\n"
    )
    emitted = _run_node(driver)
    assert emitted["fresh"]["rects"] > 0, (
        "a drawable screen with fresh bars drew nothing"
    )
    assert emitted["fresh"]["fills"] > 0
    assert emitted["fresh"]["alpha"] == 1, "fresh data must render at full strength"
    assert emitted["stale"]["rects"] > 0, "stale data must still be drawn, only dimmed"
    assert emitted["stale"]["alpha"] == STALE_ALPHA, (
        "stale alpha did not match world.monitors.STALE_ALPHA - the page and "
        "the server disagree about the injected rule"
    )


@needs_node
def test_buildMonitorGraphics_keys_by_screen_id_and_lands_in_monitors():
    """Exactly one candle Graphics per painted screen, keyed by `screen.id`,
    and parented under its OWN layer, `layers.monitors` — never `layers.glow`
    (additive glow would wash the candles out), never `layers.chars`, and
    (Finding 1, review round 1) never `layers.props` either, since that
    container is wiped and rebuilt every draw() cycle.

    KI-052: each screen also gets a quad mask, built once here alongside its
    candle Graphics (never inside drawCandles, which runs every poll forever
    - see test_the_monitor_graphics_are_built_once_not_per_poll_or_per_tick).
    That doubles the Graphics/children counts below; the mask itself is
    checked against the manifest's own `quad`, not just counted.
    """
    driver = _monitor_driver(plate_ready=True) + (
        "buildMonitorGraphics();\n"
        "console.log(JSON.stringify({\n"
        "  built: graphicsBuilt,\n"
        "  ids: Object.keys(monitorGraphics).sort(),\n"
        "  inMonitors: layers.monitors.children.length,\n"
        "  sameRef: monitorGraphics[PLATE.screens[0].id]"
        " === layers.monitors.children[0],\n"
        "  masks: Object.fromEntries(Object.entries(monitorGraphics).map(\n"
        "    ([id, g]) => [id, g.mask ? g.mask.polyPoints : null])),\n"
        "}));\n"
    )
    emitted = _run_node(driver)
    manifest_screens = {s["id"]: s for s in _manifest()["screens"]}
    manifest_ids = sorted(manifest_screens)
    assert emitted["ids"] == manifest_ids
    # One candle Graphics per screen, plus one quad mask per screen THAT
    # CARRIES A QUAD - not every screen, per review round 1's own
    # test_a_screen_with_no_quad_still_gets_a_graphics_but_no_mask, which
    # blesses a quad-less screen as legitimate. `2 * len(manifest_ids)`
    # silently assumed every screen has a quad; it and that test contradict
    # each other the moment a real manifest ships a quad-less screen.
    masked_ids = [sid for sid in manifest_ids if manifest_screens[sid].get("quad")]
    assert masked_ids, "no manifest screens carry a quad to check for a mask"
    assert emitted["built"] == len(manifest_ids) + len(masked_ids)
    assert emitted["inMonitors"] == len(manifest_ids) + len(masked_ids)
    assert emitted["sameRef"] is True
    for screen_id in masked_ids:
        quad = manifest_screens[screen_id]["quad"]
        assert emitted["masks"][screen_id] == [c for point in quad for c in point], (
            f"{screen_id}: candle Graphics mask does not match its own quad"
        )


@needs_node
def test_a_screen_with_no_quad_still_gets_a_graphics_but_no_mask():
    """KI-052 review round 1, MINOR 4: `screen.quad` is optional in the
    manifest schema (nothing forbids a future screen without one, and
    `screen.quad.flat()` on `undefined` would throw), so
    `buildMonitorGraphics` guards on it with `if (screen.quad)`. Every other
    degrade path on this page (`rampLevel`, `CONTACT`, the no-plate
    fallbacks) has a test, because the failure path is the one that runs
    unattended at 3am - this drives the real `buildMonitorGraphics` against
    a manifest with one quad-less screen and one normal one, and checks both
    still get their candle Graphics (nothing throws, nothing is skipped)
    while only the quad-less one is left unmasked."""
    source = _world_source()
    driver = (
        "const PLATE = { screens: [\n"
        "  { id: 'no-quad', x: 0, y: 0, w: 200, h: 100, role: 'chart' },\n"
        "  { id: 'has-quad', x: 0, y: 0, w: 200, h: 100, role: 'chart',\n"
        "    quad: [[0, 0], [200, 0], [200, 100], [0, 100]] },\n"
        "] };\n"
        "class FakeGraphics {\n"
        "  poly(points) { this.polyPoints = points; return this; }\n"
        "  fill() { return this; }\n"
        "}\n"
        "const PIXI = { Graphics: FakeGraphics };\n"
        "const layers = { monitors: { children: [],\n"
        "  addChild(...items) { this.children.push(...items); } } };\n"
        "const monitorGraphics = {};\n"
        + _js_block(source, "function buildMonitorGraphics(")
        + "\n"
        + "buildMonitorGraphics();\n"
        + "console.log(JSON.stringify({\n"
        + "  inMonitors: layers.monitors.children.length,\n"
        + "  ids: Object.keys(monitorGraphics).sort(),\n"
        + "  noQuadMask: monitorGraphics['no-quad'].mask || null,\n"
        + "  hasQuadMask: !!monitorGraphics['has-quad'].mask,\n"
        + "}));\n"
    )
    emitted = _run_node(driver)
    assert emitted["ids"] == ["has-quad", "no-quad"], (
        "both screens must still get a candle Graphics, guard or not"
    )
    # One candle Graphics for the quad-less screen, plus a candle Graphics
    # AND a mask for the other - the guard skips only the mask, not the
    # screen.
    assert emitted["inMonitors"] == 3
    assert emitted["noQuadMask"] is None, "a quad-less screen must not get a mask"
    assert emitted["hasQuadMask"] is True


@needs_node
def test_drawPillars_never_touches_the_monitors_layer():
    """Review round 1, Finding 1: the monitor Graphics moved into their OWN
    container (`layers.monitors`), a stage sibling built once by
    `buildMonitorGraphics()` and never wiped by anyone else — the same
    contract `layers.glow` already has via `buildGlows()`. `drawPillars`
    only ever owns `layers.props` (its per-cycle wipe-and-rebuild container
    for pillars/caps/labels/the history line); it must never reach into
    `layers.monitors` at all. This passes because nothing wipes the monitors,
    not because something puts them back - the earlier version of this test
    (a re-append patch over the wipe) is exactly the shape the reviewer flagged:
    it worked, but nothing structurally stopped a future `layers.props`
    consumer from wiping and forgetting to re-append, with a blank monitor on
    air as the failure mode.
    """
    source = _world_source()
    probe = "{ id: 'probe' }"
    driver = (
        "const SYMBOLS = [];\n"
        "const CELL = 4;\n"
        "function snap(v) { return v; }\n"
        "let monitorsWiped = false;\n"
        f"const probe = {probe};\n"
        "const layers = {\n"
        "  props: { children: [],\n"
        "    removeChildren() { this.children = []; },\n"
        "    addChild(...items) { this.children.push(...items); } },\n"
        "  monitors: { children: [probe],\n"
        "    removeChildren() { monitorsWiped = true; this.children = []; },\n"
        "    addChild(...items) { this.children.push(...items); } },\n"
        "};\n"
        "function color() { return 0; }\n"
        "function label() { return {}; }\n"
        "function pillarGeometry() {\n"
        "  return { x: 0, baseY: 0, width: 1, height: 1 };\n"
        "}\n"
        "class FakeGraphics { rect() { return this; } fill() { return this; } }\n"
        "const PIXI = { Graphics: FakeGraphics };\n"
        + _js_block(source, "function drawPillars(")
        + "\n"
        "drawPillars({ symbols: {} });\n"
        "console.log(JSON.stringify({\n"
        "  stillThere: layers.monitors.children.includes(probe),\n"
        "  monitorsCount: layers.monitors.children.length,\n"
        "  monitorsWiped,\n"
        "}));\n"
    )
    emitted = _run_node(driver)
    assert emitted["stillThere"] is True, (
        "drawPillars() must never disturb layers.monitors' children"
    )
    # Not just "the probe is still there somewhere" - exactly the one child it
    # started with, so a stray non-destructive addChild() onto the wrong layer
    # (adding without removing) is caught too, not only a wipe.
    assert emitted["monitorsCount"] == 1, (
        "drawPillars() added something to layers.monitors - it does not own "
        "that container"
    )
    assert emitted["monitorsWiped"] is False, (
        "drawPillars() must never call removeChildren() on layers.monitors "
        "at all - it does not own that container"
    )


@needs_node
def test_pollBars_never_fetches_when_there_is_no_plate():
    """Override 4: PLATE may be null and plateReady may be false. `pollBars`
    must be a clean no-op then, not a per-screen exception every 20s."""
    driver = (
        "const PLATE = null;\n"
        "const plateReady = false;\n"
        "let fetchCalls = 0;\n"
        "function fetch() { fetchCalls++; throw new Error('must not fetch'); }\n"
        "function drawCandles() { throw new Error('must not draw'); }\n"
        + _js_const(_world_source(), "BARS_POLL_MS")
        + "\n"
        + _js_block(_world_source(), "async function pollBars(")
        + "\n"
        "pollBars().then(() => console.log(JSON.stringify({ fetchCalls })));\n"
    )
    emitted = _run_node(driver)
    assert emitted["fetchCalls"] == 0


@needs_node
def test_pollBars_only_polls_chart_screens_with_a_symbol_and_degrades_on_failure():
    """Three screens: one not a chart, one a chart with no symbol assigned yet,
    one real. Only the real one is fetched, using the exact `/bars/{symbol}
    ?interval=1m&limit=120` path this ticket is pinned to. A non-OK response
    and a thrown fetch both degrade to dark glass (`drawCandles(screen, [])`),
    never a stale claim."""
    source = _world_source()
    driver = (
        "const PLATE = { screens: [\n"
        "  { id: 'tube', role: 'gauge', symbol: 'BTCUSDT' },\n"
        "  { id: 'blank', role: 'chart', symbol: null },\n"
        "  { id: 'live', role: 'chart', symbol: 'BTCUSDT' },\n"
        "  { id: 'bad', role: 'chart', symbol: 'DOGEUSDT' },\n"
        "  { id: 'down', role: 'chart', symbol: 'ETHUSDT' },\n"
        "] };\n"
        "const plateReady = true;\n"
        "const fetchUrls = [];\n"
        "const drawnWith = {};\n"
        "function drawCandles(screen, bars) { drawnWith[screen.id] = bars; }\n"
        "function fetch(url) {\n"
        "  fetchUrls.push(url);\n"
        # A real 4xx/5xx Response still carries a `.json()` method - a fetch
        # stub that omits it would make an unguarded `!res.ok` mutant fail via
        # the generic catch instead of via the guard actually missing, which
        # proves nothing about the guard itself. This body is shaped like a
        # SUCCESS payload on purpose, so skipping the check would draw it as
        # if it were real data instead of dark glass.
        "  if (url.includes('DOGEUSDT')) return Promise.resolve({ ok: false,"
        " json: () => Promise.resolve({ data: { bars: [{ open: 9, high: 9,"
        " low: 9, close: 9, timestamp: '2001-01-01T00:00:00+00:00' }] } }) });\n"
        "  if (url.includes('ETHUSDT'))"
        " return Promise.reject(new Error('network down'));\n"
        "  return Promise.resolve({ ok: true, json: () => Promise.resolve(\n"
        "    { data: { bars: [{ open: 1, high: 2, low: 0, close: 1,"
        " timestamp: '2099-01-01T00:00:00+00:00' }] } }) });\n"
        "}\n"
        + _js_const(source, "BARS_POLL_MS")
        + "\n"
        + _js_block(source, "async function pollBars(")
        + "\n"
        "pollBars().then(() =>"
        " console.log(JSON.stringify({ fetchUrls, drawnWith })));\n"
    )
    emitted = _run_node(driver)
    assert emitted["fetchUrls"] == [
        "/bars/BTCUSDT?interval=1m&limit=120",
        "/bars/DOGEUSDT?interval=1m&limit=120",
        "/bars/ETHUSDT?interval=1m&limit=120",
    ], "must skip the non-chart and symbol-less screens, and poll the rest in order"
    assert emitted["drawnWith"]["live"][0]["close"] == 1
    assert emitted["drawnWith"]["bad"] == [], "a non-OK response must draw dark glass"
    assert emitted["drawnWith"]["down"] == [], "a thrown fetch must draw dark glass"
    assert "tube" not in emitted["drawnWith"]
    assert "blank" not in emitted["drawnWith"]


def test_the_gallery_keeps_the_room_it_is_judged_against():
    page = client.get("/world?gallery=1").text
    body = _js_block(page, "function drawGallery(")
    # The specimens must stand in the room by default. This is KI-051's root
    # cause: the tool used to judge the cast deleted the picture.
    assert "layers.plate.visible = false" not in body
    assert "VOID_MODE" in body


def test_the_gallery_hides_the_live_monitors():
    # Observed 2026-09-01: live candles drew straight over the specimens.
    body = _js_block(client.get("/world?gallery=1").text, "function drawGallery(")
    assert "layers.monitors.visible = false" in body


def test_the_gallery_hides_the_rooms_own_nameplates():
    # Observed 2026-09-07 (this ticket's own screenshot): `layers.nameplates`
    # is built once at fixed canvas coordinates, independent of the room
    # characters `setCharacterVisible` hides here — left visible, "MODEL"/
    # "TRADER" drew straight over whichever grid cell happened to land on
    # that spot. Same failure shape, same fix, as the monitors sibling above.
    body = _js_block(client.get("/world?gallery=1").text, "function drawGallery(")
    assert "layers.nameplates.visible = false" in body


def test_the_animation_sheet_keeps_the_room_too():
    body = _js_block(
        client.get("/world?anims=1").text, "function drawAnimationSheet("
    )
    assert "layers.plate.visible = false" not in body
    assert "VOID_MODE" in body


def test_the_animation_sheet_hides_the_rooms_own_nameplates():
    # Sibling of the gallery's own check above - the same static-content leak
    # observed on this ticket's `?anims=1` screenshot.
    body = _js_block(
        client.get("/world?anims=1").text, "function drawAnimationSheet("
    )
    assert "layers.nameplates.visible = false" in body


def test_the_page_reports_that_it_has_drawn():
    # scripts/shoot.py polls this instead of guessing a duration.
    assert "window.__worldDrawn = true" in client.get("/world").text


def test_void_mode_is_reachable_from_drawgallery_and_drawanimationsheet():
    """A `const VOID_MODE` declared inside boot() parses fine and passes every
    text-substring test above — VOID_MODE still appears, in the source,
    inside drawGallery's own body — but throws ReferenceError the instant
    drawGallery runs: it is boot()'s sibling, not its child, and can't see a
    boot()-local const. boot()'s own `.catch()` turns that into a silent
    reload loop, and shoot.py photographs an empty page with no other signal
    that anything is wrong. That took three rounds of debugging-by-screenshot
    to catch by hand; this pins it at the source level so the next edit that
    moves the declaration "closer to the other query-flag reads" (i.e. into
    boot(), where most of them live) can't reintroduce it silently.
    """
    source = _world_source()
    boot_body = _js_block(source, "async function boot(")
    assert "VOID_MODE" not in boot_body, (
        "VOID_MODE must not be declared (or read) inside boot() — "
        "drawGallery and drawAnimationSheet are boot()'s siblings, not its "
        "children, and a boot()-local const is invisible to them"
    )
    assert re.search(r"^    const VOID_MODE = ", source, re.M), (
        "VOID_MODE must be a top-level (4-space-indent) const, a sibling of "
        "drawGallery and drawAnimationSheet, not nested inside another "
        "function"
    )


def test_the_page_only_claims_drawn_after_each_gates_own_draw_call():
    """`window.__worldDrawn` used to be set once, above all four gates, right
    after the heartbeat interval — true before drawGallery, drawAnimationSheet,
    the swell rehearsal, or the live refresh had drawn anything. shoot.py polls
    that flag INSTEAD OF a fixed sleep specifically so a page that has not
    painted can't report ready; setting it early makes the poll decorative and
    turns `--settle` into the only real guard — the exact failure Task 1's CDP
    screenshotter exists to catch.

    Pinned as an ORDERING claim per gate (the same idiom as
    test_the_heartbeat_is_installed_only_after_the_plate_settles above),
    because the flag string appearing anywhere in the page proves nothing
    about when it goes true — this must fail if the assignment moves back
    above the gate branches, which a plain substring test cannot detect.
    """
    boot = _js_block(_world_source(), "async function boot()")

    gallery_gate = 'get("gallery") === "1") {'
    anims_gate = 'get("anims")) {'
    swell_gate = 'get("swell")) {'
    live_start = "try {\n        await refresh();"

    gallery = boot[boot.index(gallery_gate) : boot.index(anims_gate)]
    assert gallery.index("drawGallery();") < gallery.index(
        "window.__worldDrawn = true;"
    ), "drawGallery must run before the gallery gate claims it has drawn"

    anims = boot[boot.index(anims_gate) : boot.index(swell_gate)]
    assert anims.index("drawAnimationSheet();") < anims.index(
        "window.__worldDrawn = true;"
    ), "drawAnimationSheet must run before the anims gate claims it has drawn"

    swell = boot[boot.index(swell_gate) : boot.index(live_start)]
    # The FIRST rehearse() call — the synchronous invocation, not the
    # `const rehearse = () => {...}` definition or the later recurring
    # setInterval(rehearse, ...) re-invocation.
    first_call = swell.index("rehearse();", swell.index("const rehearse ="))
    assert first_call < swell.index("window.__worldDrawn = true;"), (
        "the first rehearse() must run before the swell gate claims it has drawn"
    )

    live = boot[boot.index(live_start) :]
    assert live.index("await refresh();") < live.index(
        "window.__worldDrawn = true;"
    ), "the live path must await refresh() before claiming it has drawn"


@needs_node
def test_worlddrawn_is_still_set_when_refresh_throws():
    """A failed `/world/state` fetch is a data problem, not a failure to draw
    the room — the cast is already positioned before this fragment runs — so
    the flag must go true even if refresh ever stops swallowing its own
    errors (it currently does, via its own try/catch, but that is refresh's
    contract to keep, not this one's to assume). Runs the real live-path
    fragment — `try { await refresh(); } finally { window.__worldDrawn =
    true; }` — against a `refresh` stub that actually throws, so a future
    edit that swaps the `finally` for a `.then()` (which would silently skip
    the flag on a throw and leave shoot.py spinning its full 30s poll on a
    page that is actually fine) fails here.
    """
    boot = _js_block(_world_source(), "async function boot()")
    live_start = "try {\n        await refresh();"
    live = boot[boot.index(live_start) : boot.index("subscribe();")]
    driver = (
        "let worldDrawn = false;\n"
        "const window = {\n"
        "  get __worldDrawn() { return worldDrawn; },\n"
        "  set __worldDrawn(v) { worldDrawn = v; },\n"
        "};\n"
        "async function refresh() { throw new Error('network down'); }\n"
        "(async () => {\n"
        "  try {\n"
        + live
        + "\n"
        "  } catch (e) { /* refresh's own throw must not escape uncaught */ }\n"
        "  console.log(JSON.stringify({ worldDrawn }));\n"
        "})();\n"
    )
    emitted = _run_node(driver)
    assert emitted["worldDrawn"] is True, (
        "window.__worldDrawn must be set from a finally (or equivalent), "
        "not skipped when refresh() throws"
    )


# --- Task 6: the cast is lit by the room's lamp (KI-051) --------------------
#
# Every volume used to be `roundRect(...).fill(BODY_FILL).stroke(BODY_RIM)` -
# one flat fill, one hard rim, no light direction. `shade`/`paint` replace the
# fill rule only; shapes, positions and animations are untouched (pinned by
# `test_the_animation_layer_is_untouched` below and the SHA check in the task
# report).


@needs_node
def test_the_cast_is_shaded_rather_than_flat_filled():
    """KI-051: a figure lit by this room mixes toward the lamp's own warmth
    when its lit band faces the key, and toward the room's ambient when its
    shaded band faces away - not toward flat white/black, which would make a
    figure merely "brighter here" rather than lit by this specific amber lamp
    in this specific cold room. Every painted object on the plate already
    does the same amber-highlight / cold-shadow mix; this is what makes the
    cast belong to it.

    Exercised for real against `LIGHT.warmth`/`LIGHT.ambient` as the page
    actually declares them, not by grepping the source for the identifier
    `LIGHT.warmth`: that string can sit in a comment or a dead branch and
    still satisfy a substring check. This drives the real `shade()` and
    compares its output to a colour computed independently in Python from
    the same measured `LIGHT`, then mutation-checks that the comparison is
    load-bearing by swapping which side of the ramp mixes toward which
    colour - the one bug `light_for`'s own monotonic-ramp check exists to
    keep off the Python side, and the one that would light every figure from
    the wrong side of the room if it ever reached the page.
    """
    source = _world_source()
    light_match = re.search(r"const LIGHT = (\{.*\});", source)
    assert light_match, "the page no longer declares a one-line const LIGHT"
    light = json.loads(light_match.group(1))

    def mix_hex(a, b, t):
        pa = a if isinstance(a, int) else int(str(a).lstrip("#"), 16)
        pb = b if isinstance(b, int) else int(str(b).lstrip("#"), 16)
        ch = lambda v, sh: (v >> sh) & 0xFF  # noqa: E731
        lerp = lambda x, y: round(x + (y - x) * t)  # noqa: E731
        return (
            (lerp(ch(pa, 16), ch(pb, 16)) << 16)
            | (lerp(ch(pa, 8), ch(pb, 8)) << 8)
            | lerp(ch(pa, 0), ch(pb, 0))
        )

    base = 0x808080
    expected_lit = mix_hex(base, light["warmth"], min(1, abs(light["ramp"]["lit"])))
    expected_shaded = mix_hex(
        base, light["ambient"], min(1, abs(light["ramp"]["shade"]))
    )

    shade_block = _js_block(source, "function shade(")

    def run(block):
        driver = (
            _js_const(source, "LIGHT")
            + _js_block(source, "function mixHex(")
            + "\n"
            + block
            + "\n"
            + f"""
            console.log(JSON.stringify({{
              lit: shade({base}, LIGHT.ramp.lit),
              shaded: shade({base}, LIGHT.ramp.shade),
              neutral: shade({base}, LIGHT.ramp.base),
            }}));
            """
        )
        return _run_node(driver)

    emitted = run(shade_block)
    assert emitted["lit"] == expected_lit, (
        f"shade(base, LIGHT.ramp.lit) = {emitted['lit']:#x}, expected "
        f"{expected_lit:#x} - the lit band must mix toward LIGHT.warmth"
    )
    assert emitted["shaded"] == expected_shaded, (
        f"shade(base, LIGHT.ramp.shade) = {emitted['shaded']:#x}, expected "
        f"{expected_shaded:#x} - the shaded band must mix toward LIGHT.ambient"
    )
    assert emitted["neutral"] == base, (
        "LIGHT.ramp.base (0) must return the tint unchanged"
    )
    assert emitted["lit"] != emitted["shaded"], (
        "the lit and shaded bands rendered the same colour"
    )

    # Mutation check: swap which colour each side of the ramp mixes toward.
    mutated = shade_block.replace(
        "level >= 0 ? LIGHT.warmth : LIGHT.ambient",
        "level >= 0 ? LIGHT.ambient : LIGHT.warmth",
    )
    assert mutated != shade_block, "the mutation did not change the source"
    broken = run(mutated)
    assert broken["lit"] != expected_lit or broken["shaded"] != expected_shaded, (
        "swapping which colour each ramp side mixes toward still produced "
        "the correct output - these assertions would not catch a "
        "wrong-side-of-the-room lighting bug"
    )


def test_no_body_volume_is_painted_with_the_bare_flat_fill_any_more():
    """The exact construct KI-051 is about. `paint(...)` replaces every
    `roundRect(...).fill(BODY_FILL).stroke(BODY_RIM)` two-tone stamp with
    three shaded bands and a shaded rim (`BODY_RIM_SHADED`) - so a bare
    `BODY_FILL`/`BODY_RIM` fill/stroke pair must not survive anywhere a body
    volume is drawn.

    Covers all three places a volume is drawn, not just the two the brief
    named: the five `BODIES.*` methods, `seatedRig` (the trader's lower
    body), AND `character()`, where the skull is built separately for every
    style but the orb. The skull is the largest single surface carrying mood
    (`setExpression` paints eyes/brows/mouth onto it) and the easiest to miss
    a shading pass on precisely because it lives in neither of the other two
    constructs - a shaded body with a flat sticker head is a half-finished
    result no assertion scoped to `BODIES`/`seatedRig` alone would catch.
    """
    source = _world_source()
    rig = (
        _js_block(source, "const BODIES = {")
        + _js_block(source, "function seatedRig(")
        + _js_block(source, "function character(")
    )
    assert ".fill(BODY_FILL)" not in rig, (
        "a body volume is still using the bare flat fill KI-051 is about"
    )
    assert re.search(r"\.stroke\(BODY_RIM\)", rig) is None, (
        "a body volume is still stroked with the un-shaded BODY_RIM - "
        "BODY_RIM_SHADED is the KI-051 rim, this is the sticker outline"
    )
    assert rig.count("paint(") >= 15, (
        "fewer paint() calls than the volumes this rig draws - a body was "
        "converted to something else, or not converted at all"
    )


# The sprint's Global Constraint (plan line 47): `ANIM`, `SEATED_ANIMATIONS`
# and `advanceCharacters` must be byte-unchanged by Sprint 16. Digests taken
# from the rendered page at `29c3a96`, this branch's tip before Task 6, and
# verified equal to `git show 29c3a96:api/templates/world.html`'s own blocks.
# If one of these fails, either the shading pass reached into the animation
# layer - the thing the constraint exists to catch - or the constraint is
# being deliberately lifted, which is a decision, not a test edit.
PROTECTED_BLOCKS = {
    "const ANIM = {": (
        "716d3fb9df6668032c3ee2da1f9eccd2cf5d2580968bc12b09e545b35687d58d", 3783
    ),
    "const SEATED_ANIMATIONS = {": (
        "1977ba1c188c5a701d71422678d66eec929defcab43736d25da76986e5c26673", 2540
    ),
    "function advanceCharacters(": (
        "a4f17050f3d1ca7fbc7790bd192fbeb089353d7d73f556bb73acafb9ec4b5e9e", 2275
    ),
}


def test_the_animation_layer_is_untouched():
    """The constraint that makes this redesign safe: the animation layer
    survives byte-for-byte because only the fill rule changed.

    Review round 1, Important 4: the previous version of this test said
    "byte-for-byte" in its docstring and then asserted that two substrings
    were present - nothing in it would have failed if the bodies of those
    blocks had been rewritten, and it cited the task report as if a report
    were a test. This grades the actual bytes: each block is brace-matched
    out of the rendered page and digested, so a single character moved inside
    `ANIM` fails here rather than in a screenshot three tasks later.
    """
    page = client.get("/world").text
    for opening, (digest, size) in PROTECTED_BLOCKS.items():
        block = _js_block(page, opening)
        actual = hashlib.sha256(block.encode()).hexdigest()
        assert (actual, len(block)) == (digest, size), (
            f"{opening!r} is no longer byte-identical to its Sprint 16 "
            f"baseline ({len(block)}B/{actual[:16]} vs {size}B/{digest[:16]}) "
            "- this sprint's Global Constraint says the animation layer does "
            "not move"
        )


# --- KI-051, review round 1: the light has to land on the lamp's side -------
#
# Critical 1 was a sign error that every string-matching test in this file was
# blind to: `insetSpan`'s two branches were swapped, so rects were lit from the
# left while the skull - a circle, which never went through that function - was
# lit from the right. Head and body lit from opposite sides of one figure, and
# the code comment described the correct behaviour, which is why self-review
# missed it. The fix deleted the branch rather than correcting it: "toward the
# key" is now expressed once, as `-KEY_AXIS`, for rects and circles alike.
#
# These drive the page's own geometry in node. They are the tests that can see
# a sign.


def _geometry_driver(source: str, body: str) -> str:
    """The page's real shading geometry, plus a shape recorder."""
    return (
        "const PLATE = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _shading_prelude(source)
        + """
        const shapes = [];
        const rec = {
          roundRect(x, y, w, h, r) {
            shapes.push({ kind: "rect", x, y, w, h, r }); return this;
          },
          circle(x, y, r) { shapes.push({ kind: "circle", x, y, r }); return this; },
          fill() { return this; },
          stroke() { return this; },
        };
        """
        + body
    )


@needs_node
def test_every_band_is_drawn_on_the_lamp_side_of_its_volume():
    """The measured key sits up and to the right of the cast (`direction =
    [-0.55, 0.84]`, so `-direction` points at the painted lamp at plate
    `[1339, 455]`). Every band a volume draws must therefore have its centre
    displaced from the volume's own centre in that direction - a rect and a
    circle alike, which is the half of the claim round 1 got wrong.
    """
    source = _world_source()
    driver = _geometry_driver(
        source,
        """
        const vols = {
          "chest 66x54 r22": litRect(-33, -128, 66, 54, 22),
          "wick 7x30 r3.5":  litRect(-3.5, -130, 7, 30, 3.5),
          "bar 12x54 r4":    litRect(-6, -54, 12, 54, 4),
          "seat chest 48x40 r20": litRect(-24, -80, 48, 40, 20),
          "skull r28":       litCircle(0, 0, 28),
          "orb r46":         litCircle(0, -60, 46),
        };
        const out = {};
        for (const [name, vol] of Object.entries(vols)) {
          out[name] = [];
          for (const band of [BAND_INSET.shade, BAND_INSET.base, BAND_INSET.lit]) {
            shapes.length = 0;
            vol.band(rec, band);
            const s = shapes[0];
            const cx = s.kind === "rect" ? s.x + s.w / 2 : s.x;
            const cy = s.kind === "rect" ? s.y + s.h / 2 : s.y;
            out[name].push({ cx, cy, s });
          }
        }
        console.log(JSON.stringify({ out, axis: KEY_AXIS }));
        """,
    )
    emitted = _run_node(driver)
    ux, uy = -emitted["axis"][0], -emitted["axis"][1]
    assert ux > 0 and uy < 0, (
        f"the key axis no longer points up and to the right: ({ux}, {uy}) - "
        "this test's premise is the painted lamp's measured position"
    )
    for name, bands in emitted["out"].items():
        origin = bands[0]  # the inset-0 band IS the volume's own silhouette
        for label, band in zip(("base", "lit"), bands[1:]):
            dx = band["cx"] - origin["cx"]
            dy = band["cy"] - origin["cy"]
            mag = (dx * dx + dy * dy) ** 0.5
            assert mag > 0.5, (
                f"{name}: the {label} band sits on the volume's own centre "
                f"({dx:+.3f}, {dy:+.3f}) - it carries no light direction"
            )
            # Asserted PER AXIS, because an axis is where a sign error lives.
            # A band cannot always travel the full key direction: a long thin
            # volume runs out of room across its short axis long before its
            # long one, and forcing the diagonal anyway is what left a 12x96
            # bar with its bright part in the middle. So what is pinned is
            # that NEITHER axis moves away from the lamp, and that the result
            # still points broadly at it.
            assert dx * ux >= 0 and dy * uy >= 0, (
                f"{name}: the {label} band is displaced ({dx:+.2f}, {dy:+.2f}), "
                f"which moves it AWAY from the lamp on one axis (the key is at "
                f"({ux:+.2f}, {uy:+.2f})) - a sign is inverted"
            )
            cos = (dx * ux + dy * uy) / mag
            assert cos > 0.7, (
                f"{name}: the {label} band is displaced ({dx:+.2f}, {dy:+.2f}), "
                f"which agrees with the key direction by cos={cos:+.4f} - the "
                "band is not on the lamp's side of the volume"
            )


@needs_node
def test_no_band_is_ever_drawn_outside_the_volume_it_belongs_to():
    """The property `insetSpan` was written to protect, made checkable.

    The first version of this rule kept every band inside the volume's
    BOUNDING BOX and still leaked out through its rounded corner, because a
    small band's corner is squarer than the silhouette's: rasterised against
    the real call sites, the seated chest (48x40, r20 - a stadium) spilled
    15.3% of its lit band outside its own outline. A figure whose silhouette
    grows when the lamp is measured is exactly what the seat- and
    backrest-fit budgets (Task 5: 9px and 50px) cannot afford.

    Checked by sampling the band's own area on a fine grid and testing each
    point against the volume's real rounded rect - every volume the page
    actually draws, read out of the page source rather than re-typed.
    """
    source = _world_source()
    rig = (
        _js_block(source, "const BODIES = {")
        + _js_block(source, "function seatedRig(")
        + _js_block(source, "function character(")
    )
    rects = re.findall(
        r"litRect\(([-\d.]+), ([-\d.]+), (\w+|[\d.]+), (\w+|[\d.]+), ([\d.]+)\)", rig
    )
    circles = re.findall(r"litCircle\(([-\d.]+), ([-\d.]+), ([\d.]+)\)", rig)
    assert len(rects) + len(circles) >= 15, (
        f"only found {len(rects)} rect and {len(circles)} circle volumes in the "
        "rig - the call-site shapes changed and this test stopped covering them"
    )
    # `bars` builds its rects from a loop variable, so its heights have to be
    # substituted - read out of the page, never restated here. A literal list
    # would keep this test green while it checked geometry the page had stopped
    # drawing, which is the shape of test this sprint exists to stop writing.
    declared = re.search(r"const heights = \[([\d,\s]+)\];", rig)
    assert declared, (
        "BODIES.bars no longer declares `const heights = [...]` - this test "
        "can no longer see what the bar cluster actually draws"
    )
    bar_heights = [float(v) for v in declared.group(1).split(",")]
    assert len(bar_heights) >= 3, f"only {len(bar_heights)} bar heights found"
    volumes = []
    for x, y, w, h, r in rects:
        for hh in (bar_heights if h == "h" else [h]):
            yy = -float(hh) if y == "-h" or h == "h" else float(y)
            volumes.append(["rect", float(x), yy, float(w), float(hh), float(r)])
    for cx, cy, rad in circles:
        volumes.append(["circle", float(cx), float(cy), float(rad)])

    driver = _geometry_driver(
        source,
        "const VOLUMES = " + json.dumps(volumes) + ";\n"
        + """
        function insideRR(px, py, hw, hh, r) {
          const ax = Math.abs(px), ay = Math.abs(py);
          if (ax > hw + 1e-9 || ay > hh + 1e-9) return false;
          const dx = Math.max(0, ax - (hw - r)), dy = Math.max(0, ay - (hh - r));
          return dx * dx + dy * dy <= r * r + 1e-9;
        }
        const worst = [];
        VOLUMES.forEach((spec, i) => {
          // The volume's TRUE silhouette, in the same snapped space the page
          // draws it in: litRect snaps its origin, litCircle its centre.
          let vol, sx, sy, w, h, r;
          if (spec[0] === "rect") {
            w = spec[3]; h = spec[4]; r = spec[5];
            vol = litRect(spec[1], spec[2], w, h, r);
            sx = snap(spec[1]); sy = snap(spec[2]);
          } else {
            r = spec[3]; w = h = 2 * r;
            vol = litCircle(spec[1], spec[2], r);
            sx = snap(spec[1]) - r; sy = snap(spec[2]) - r;
          }
          for (const band of [BAND_INSET.shade, BAND_INSET.base, BAND_INSET.lit]) {
            shapes.length = 0;
            vol.band(rec, band);
            const s = shapes[0];
            const br = s.r;
            const bw = s.kind === "rect" ? s.w : 2 * s.r;
            const bh = s.kind === "rect" ? s.h : 2 * s.r;
            const bx = s.kind === "rect" ? s.x : s.x - s.r;
            const by = s.kind === "rect" ? s.y : s.y - s.r;
            if (bw <= 0 || bh <= 0 || br < 0) {
              worst.push({ i, band, leak: 1, bw, bh, br, note: "non-positive band" });
              continue;
            }
            let out = 0, tot = 0;
            const step = Math.min(bw, bh) / 40;
            for (let py = by + step / 2; py < by + bh; py += step) {
              for (let px = bx + step / 2; px < bx + bw; px += step) {
                const inBand = insideRR(
                  px - bx - bw / 2, py - by - bh / 2, bw / 2, bh / 2, br);
                if (!inBand) continue;
                tot += 1;
                const inVol = insideRR(
                  px - sx - w / 2, py - sy - h / 2, w / 2, h / 2, r);
                if (!inVol) out += 1;
              }
            }
            worst.push(
              { i, kind: spec[0], band, leak: tot ? out / tot : 0, bw, bh, br });
          }
        });
        worst.sort((a, b) => b.leak - a.leak);
        console.log(JSON.stringify(worst.slice(0, 4)));
        """,
    )
    emitted = _run_node(driver)
    top = emitted[0]
    assert top["leak"] == 0, (
        f"volume {top['i']} band {top['band']} draws {100 * top['leak']:.2f}% of "
        f"its own area outside the silhouette it belongs to ({top}) - the "
        "figure's shape changes with the light instead of only its colour"
    )
    for row in emitted:
        assert row["bw"] > 0 and row["bh"] > 0 and row["br"] >= 0, (
            f"a band came out non-positive: {row} - a roundRect of negative "
            "width is a silent geometry failure, not an exception"
        )


@needs_node
def test_the_band_offsets_scale_with_the_volume_rather_than_being_fixed_px():
    """Review round 1's ruling. The shipped rule used absolute insets of
    0/2/5 against volumes spanning 12 to 66 local units, so a bar got a ~1px
    shade band and 85% of every figure came out one flat colour - a tint,
    which is the defect KI-051 names, not light. Two volumes five times apart
    in size must get band insets five times apart, and a band that ate a
    constant number of units would fail this.
    """
    source = _world_source()
    driver = _geometry_driver(
        source,
        """
        const out = {};
        for (const [name, w] of [["small", 12], ["large", 60]]) {
          shapes.length = 0;
          litRect(-w / 2, -100, w, 100, 2).band(rec, BAND_INSET.lit);
          out[name] = shapes[0].w;
        }
        // and the clamp: an absurd band must not invert or vanish a volume
        shapes.length = 0;
        litRect(-6, -54, 12, 54, 4).band(rec, 5.0);
        out.absurdRect = shapes[0];
        shapes.length = 0;
        litCircle(0, 0, 28).band(rec, 5.0);
        out.absurdCircle = shapes[0];
        console.log(JSON.stringify(out));
        """,
    )
    e = _run_node(driver)
    small_eaten, large_eaten = 12 - e["small"], 60 - e["large"]
    ratio = large_eaten / small_eaten
    assert 4.5 < ratio < 5.5, (
        f"a 60-unit volume's lit band eats {large_eaten:.2f} units and a "
        f"12-unit one eats {small_eaten:.2f} - a ratio of {ratio:.2f}, not the "
        "5.0 that makes the offset a fraction of the volume's own extent"
    )
    for label in ("absurdRect", "absurdCircle"):
        s = e[label]
        dims = [s["r"]] + ([s["w"], s["h"]] if s["kind"] == "rect" else [])
        assert all(d > 0 for d in dims), (
            f"a band fraction of 5.0 produced {s} - BAND_MAX must clamp every "
            "band to a positive width, height and radius"
        )


@needs_node
def test_a_head_and_the_body_under_it_get_the_same_share_of_light():
    """A rect loses one linear fraction per axis; a circle shrinks on both at
    once, so the SAME fraction costs a circle far more of itself - at `lit`,
    uncorrected, a rect keeps ~25% of its area and a circle 9%. That is a
    skull visibly darker than the chest under it: head and body disagreeing
    about the light, which is the shape Critical 1 took. `circleBand` solves
    for the inset that leaves a circle the area share the rect keeps, so this
    pins the outcome rather than the formula.

    Caught by mutation: with `circleBand` reduced to `return band`, every
    other test in this file still passed.
    """
    source = _world_source()
    driver = _geometry_driver(
        source,
        """
        const area = (w, h, r) => w * h - (4 - Math.PI) * r * r;
        function shares(vol, circle) {
          const a = [];
          for (const band of [BAND_INSET.shade, BAND_INSET.base, BAND_INSET.lit]) {
            shapes.length = 0;
            vol.band(rec, band);
            const s = shapes[0];
            a.push(circle ? Math.PI * s.r * s.r : area(s.w, s.h, s.r));
          }
          return [a[2] / a[0], (a[1] - a[2]) / a[0], (a[0] - a[1]) / a[0]];
        }
        console.log(JSON.stringify({
          skull: shares(litCircle(0, 0, 28), true),
          orb: shares(litCircle(0, -60, 46), true),
          chest: shares(litRect(-33, -128, 66, 54, 22), false),
          seatChest: shares(litRect(-24, -80, 48, 40, 20), false),
          bar: shares(litRect(-6, -96, 12, 96, 4), false),
          wick: shares(litRect(-3.5, -130, 7, 30, 3.5), false),
        }));
        """,
    )
    e = _run_node(driver)
    lit = {k: v[0] for k, v in e.items()}
    lo, hi = min(lit.values()), max(lit.values())
    assert hi - lo < 0.06, (
        "the lit band covers a different share of a circle than of a rect: "
        + ", ".join(f"{k} {100 * v:.1f}%" for k, v in sorted(lit.items()))
        + " - a head shaded on a different rule from the body under it"
    )
    for name, (hi_, mid_, lo_) in e.items():
        assert 0.15 < hi_ < 0.40 and 0.30 < mid_ < 0.55 and 0.20 < lo_ < 0.45, (
            f"{name} came out {100 * hi_:.0f}/{100 * mid_:.0f}/{100 * lo_:.0f} "
            "lit/mid/shade - that is not a three-tone form shade any more"
        )


@needs_node
def test_a_circle_is_the_closed_form_of_the_same_slide_rule():
    """One rule, two shapes. `litCircle` solves its slide in closed form
    (`radius - rb`, the tangency point) instead of bisecting, and that is only
    legitimate if it is the same answer `slideToKey` gives for a square volume
    with a half-extent radius. Pinning the identity is what stops the head and
    the body drifting into two different lighting rules again - the shape
    Critical 1 took.
    """
    source = _world_source()
    driver = _geometry_driver(
        source,
        """
        const out = [];
        for (const [R, rb] of [[28, 8], [46, 20], [46, 45.5], [10, 1]]) {
          const got = slideToKey(2 * R, 2 * R, R, 2 * rb, 2 * rb, rb);
          const want = [(R - rb) * -KEY_AXIS[0], (R - rb) * -KEY_AXIS[1]];
          out.push([R, rb, got, want]);
        }
        console.log(JSON.stringify(out));
        """,
    )
    for R, rb, got, want in _run_node(driver):
        gap = max(abs(a - b) for a, b in zip(got, want))
        assert gap < 1e-3, (
            f"slideToKey for a circle of radius {R} with band radius {rb} "
            f"returned {got}, but the tangency offset is {want} (off by "
            f"{gap:.4f}) - litCircle's closed form and litRect's bisected "
            "placement are no longer the same rule"
        )


@needs_node
def test_a_light_without_a_ramp_degrades_instead_of_blanking_the_page():
    """Review round 1, Important 5. `BODY_RIM_SHADED` is a TOP-LEVEL const
    that reads the ramp, so a `LIGHT` without one threw a TypeError before
    `boot()` was ever called - a blank page that every page-source test in
    this file still passes (Task 2's failure mode), and strictly worse than
    the pre-ticket code, where the same dereference lived inside `paint()`
    and could only spoil one figure.

    `rampLevel()` is the one guarded accessor, used by the top-level const
    AND by `paint()` - guarding only the loud one leaves the inner path open.
    A missing rung degrades to 0, which `shade()` turns back into the volume's
    own flat colour: the pre-KI-051 look, not a broken room.
    """
    source = _world_source()
    stripped = re.sub(
        r'^(\s*const LIGHT = )\{.*\};$',
        r'\1{ "key": [0, 0], "direction": [-0.55, 0.84], '
        r'"warmth": "#f0a848", "ambient": "#1a2030" };',
        _js_const(source, "LIGHT").rstrip("\n"),
        flags=re.M,
    )
    assert '"ramp"' not in stripped and "ramp" not in stripped, (
        "the stand-in LIGHT still carries a ramp - this test would prove nothing"
    )
    driver = (
        "const PLATE = null;\n"
        + "const BODY_FILL = 0xd0d0d0, BODY_RIM = "
        + "{ width: 1.5, color: 0xffffff, alignment: 1 };\n"
        + stripped
        + "\n"
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _shading_prelude(source)
        + """
        const shapes = [];
        const fills = [];
        const rec = {
          roundRect(x, y, w, h, r) { shapes.push([x, y, w, h, r]); return this; },
          circle(x, y, r) { shapes.push([x, y, r]); return this; },
          fill(c) { fills.push(c); return this; },
          stroke() { return this; },
        };
        paint(rec, litRect(-24, -80, 48, 40, 20), BODY_FILL);
        console.log(JSON.stringify(
          { rim: BODY_RIM_SHADED.color, fills, shapes: shapes.length }));
        """
    )
    e = _run_node(driver)
    assert e["rim"] == 0xFFFFFF, (
        f"BODY_RIM_SHADED came out {e['rim']:#x} with no ramp to read - it "
        "must degrade to the plain rim colour, not to a mixed one"
    )
    assert e["fills"] == [0xD0D0D0, 0xD0D0D0, 0xD0D0D0], (
        f"a ramp-less LIGHT painted {[hex(f) for f in e['fills']]} - every "
        "band must fall back to the volume's own flat colour"
    )
    assert e["shapes"] == 4, "paint must still draw three bands and a rim"


# --- P7: the shadow is cast, not pooled ------------------------------------
#
# The brief's three checks for this ticket were `"LIGHT.direction" in body`,
# `"LIGHT.contact" in body` and `"lift" in body` over the whole `shadowTick`
# block — each satisfied by a comment or a dead branch, which is the fifth
# such test this sprint has found and rewritten. These drive the real
# `shadowTick` in node against a fake `char` and compare the numbers it
# writes to values computed here from the page's own `LIGHT`, then
# mutation-check that the comparison is load-bearing.


def _shadow_driver(source: str, body: str, *, light: dict | None = None,
                   tick: str | None = None) -> str:
    """The page's real contact-shadow arithmetic, lifted out and run in node.

    `light` substitutes a stand-in `LIGHT` (an unmeasured room, or a lamp
    twice as high) in place of the page's own; `tick` substitutes a mutated
    `shadowTick` body, which is how these assertions prove they can fail.
    """
    return (
        (f"const LIGHT = {json.dumps(light)};\n" if light is not None
         else _js_const(source, "LIGHT"))
        + _js_block(source, "function keyAxis(") + "\n"
        + _js_const(source, "KEY_AXIS")
        + _js_block(source, "function contactLight(") + "\n"
        + _js_const(source, "CONTACT")
        + _js_const(source, "SHADOW_ASPECT")
        + _js_const(source, "SHADOW_THROW")
        + (tick if tick is not None else _js_block(source, "function shadowTick("))
        + "\n"
        + """
        // A body's whole contact with this function: where it stands, how far
        // off the floor it is, and how wide its own patch is. Nothing else in
        // `character()` is involved, so nothing else is stubbed.
        function fakeChar(lift) {
          return {
            baseY: 400, shadowW: 34,
            container: { x: 200, y: 400 - lift },
            shadow: { x: 0, y: 0, width: 0, height: 0, alpha: 0 },
          };
        }
        function run(lift) {
          const c = fakeChar(lift || 0);
          shadowTick(c);
          return {
            dx: c.shadow.x - c.container.x,
            dy: c.shadow.y - (c.baseY + 3),
            width: c.shadow.width, height: c.shadow.height,
            alpha: c.shadow.alpha,
            // The edge nearest the lamp: the one thing a cast shadow must
            // not move, or it stops touching the foot it belongs to.
            lampEdgeX: c.shadow.x + c.shadow.width / 2,
            lampEdgeY: c.shadow.y - c.shadow.height / 2,
          };
        }
        """
        + body
    )


def _page_light(source: str) -> dict:
    match = re.search(r"const LIGHT = (\{.*\});", source)
    assert match, "the page no longer declares a one-line const LIGHT"
    return json.loads(match.group(1))


@needs_node
def test_the_contact_shadow_is_thrown_away_from_the_lamp():
    """A shadow centred under a figure the room lights hard from one side is a
    puddle, and a puddle is the loudest "pasted on" cue in the before-frame.

    `-KEY_AXIS` is where the lamp is — it is the direction every lit band
    slides in (`litCircle`'s `slide * -KEY_AXIS[i]`), and
    `test_every_band_is_drawn_on_the_lamp_side_of_its_volume` pins that it
    points up and to the right. So a shadow goes the OTHER way, `+KEY_AXIS`.
    The plan's own snippet had this backwards (it subtracted
    `LIGHT.direction`, which throws the shadow back under the lamp), which is
    why this is asserted on the real displacement rather than on the presence
    of the string `LIGHT.direction`.
    """
    source = _world_source()
    emitted = _run_node(_shadow_driver(
        source, "console.log(JSON.stringify({ rest: run(0), axis: KEY_AXIS }));"
    ))
    ux, uy = -emitted["axis"][0], -emitted["axis"][1]
    assert ux > 0 and uy < 0, (
        f"the key axis no longer points up and to the right: ({ux}, {uy}) - "
        "this test's premise is the painted lamp's measured position"
    )
    rest = emitted["rest"]
    dx, dy = rest["dx"], rest["dy"]
    assert (dx * dx + dy * dy) ** 0.5 > 1.0, (
        f"the shadow is displaced ({dx:+.3f}, {dy:+.3f}) - it still sits on "
        "the figure's own centre, which is the puddle this ticket is about"
    )
    assert dx * ux < 0 and dy * uy < 0, (
        f"the shadow is displaced ({dx:+.2f}, {dy:+.2f}), which moves it "
        f"TOWARD the lamp at ({ux:+.2f}, {uy:+.2f}) on at least one axis - "
        "the sign is inverted and every figure is lit from behind its own "
        "shadow"
    )

    # Mutation: throw it toward the lamp instead. If these assertions still
    # pass, they are not reading the sign.
    block = _js_block(source, "function shadowTick(")
    mutated = block.replace("KEY_AXIS[0] *", "-KEY_AXIS[0] *").replace(
        "KEY_AXIS[1] *", "-KEY_AXIS[1] *"
    )
    assert mutated != block, "the mutation did not change the source"
    broken = _run_node(_shadow_driver(
        source, "console.log(JSON.stringify({ rest: run(0) }));", tick=mutated
    ))["rest"]
    assert not (broken["dx"] * ux < 0 and broken["dy"] * uy < 0), (
        "throwing the shadow toward the lamp still satisfied the away-from-"
        "the-lamp assertion - it would not catch an inverted sign"
    )


@needs_node
def test_the_throw_is_the_measured_lamp_height_and_nothing_else():
    """`LIGHT.contact.height` is the plate's own measurement of how far this
    room throws a shadow, so doubling it must double the throw and zeroing it
    must give the centred patch back — an unmeasured room gets no invented
    direction, which is the same rule `DEFAULT_LIGHT` states in Python.

    Driven against the page's own `LIGHT` and two edits of it, so the
    measured values stay under test instead of being restated here.
    """
    source = _world_source()
    light = _page_light(source)
    assert light["contact"]["height"] > 0, (
        "the shipped plate measures no contact height - this test's premise "
        "is that it does"
    )

    def throw(**contact):
        edited = json.loads(json.dumps(light))
        edited["contact"].update(contact)
        rest = _run_node(_shadow_driver(
            source, "console.log(JSON.stringify({ rest: run(0) }));", light=edited
        ))["rest"]
        return (rest["dx"] ** 2 + rest["dy"] ** 2) ** 0.5

    base = throw()
    doubled = throw(height=light["contact"]["height"] * 2)
    assert 1.98 < doubled / base < 2.02, (
        f"doubling the measured lamp height scaled the throw by "
        f"{doubled / base:.3f}x, not 2x - the offset is not the measurement"
    )
    assert throw(height=0) < 1e-9, (
        "a plate that measures no throw still displaced its shadow - the "
        "offset carries a constant of its own"
    )


@needs_node
def test_the_shadow_still_touches_the_foot_that_casts_it():
    """The throw moves the shadow AND grows it, so its lamp-side edge stays
    exactly where the foot is and only the far edge travels. Offsetting alone
    would walk the whole ellipse off the feet, and a shadow that no longer
    touches the body reads as a second figure rather than a grounded one -
    the failure this ticket would most easily trade for the one it fixes.
    """
    source = _world_source()
    light = _page_light(source)
    no_throw = json.loads(json.dumps(light))
    no_throw["contact"]["height"] = 0

    thrown = _run_node(_shadow_driver(
        source, "console.log(JSON.stringify({ rest: run(0) }));"
    ))["rest"]
    centred = _run_node(_shadow_driver(
        source, "console.log(JSON.stringify({ rest: run(0) }));", light=no_throw
    ))["rest"]

    grew = (thrown["width"] > centred["width"]
            and thrown["height"] > centred["height"])
    assert grew, (
        "the thrown shadow is no bigger than the centred one - it was moved "
        "without being stretched, so it has left the feet behind"
    )
    assert abs(thrown["lampEdgeX"] - centred["lampEdgeX"]) < 1e-9, (
        f"the lamp-side edge moved from {centred['lampEdgeX']:.4f} to "
        f"{thrown['lampEdgeX']:.4f} - the shadow no longer starts at the foot"
    )
    assert abs(thrown["lampEdgeY"] - centred["lampEdgeY"]) < 1e-9, (
        f"the lamp-side edge moved from {centred['lampEdgeY']:.4f} to "
        f"{thrown['lampEdgeY']:.4f} - the shadow no longer starts at the foot"
    )

    # Mutation: move without growing. The edge check is the only assertion
    # here that can see it.
    block = _js_block(source, "function shadowTick(")
    mutated = re.sub(r" \+ 2 \* Math\.abs\(throw[XY]\)", "", block)
    assert mutated != block, "the mutation did not change the source"
    broken = _run_node(_shadow_driver(
        source, "console.log(JSON.stringify({ rest: run(0) }));", tick=mutated
    ))["rest"]
    assert abs(broken["lampEdgeX"] - centred["lampEdgeX"]) > 1e-6, (
        "a shadow that slid without stretching still kept its lamp-side edge "
        "- this assertion cannot see the failure it exists for"
    )


@needs_node
def test_the_shadow_is_as_dark_and_as_wide_as_the_plate_measured():
    """0.55 was tuned against the old dark canvas; this floor is painted.
    Opacity and width both come from `LIGHT.contact` now, compared against
    the manifest's own numbers rather than against a copy of them.
    """
    source = _world_source()
    light = _page_light(source)
    block = _js_block(source, "function shadowTick(")
    assert "0.55" not in block, (
        "shadowTick still carries the hardcoded alpha tuned for the old "
        "dark canvas"
    )
    rest = _run_node(_shadow_driver(
        source, "console.log(JSON.stringify({ rest: run(0) }));"
    ))["rest"]
    assert rest["alpha"] == pytest.approx(light["contact"]["opacity"]), (
        f"a resting shadow drew at alpha {rest['alpha']} - the plate measures "
        f"{light['contact']['opacity']}"
    )
    # 34 is `SHADOW_WIDTH.bars`, which `fakeChar` hands in; the width scale is
    # the manifest's. The throw widens it further, so this is a floor.
    assert rest["width"] >= 34 * 2 * light["contact"]["widthScale"] - 1e-9, (
        f"a resting shadow drew {rest['width']:.2f} wide - narrower than the "
        f"measured widthScale {light['contact']['widthScale']} allows"
    )
    assert light["contact"]["widthScale"] != 1.0, (
        "the plate measures widthScale 1.0, so the assertion above proves "
        "nothing about whether the page reads it"
    )


@needs_node
def test_the_shadow_still_spreads_and_fades_as_a_figure_leaves_the_floor():
    """The one behaviour of the old `shadowTick` that was already right, and
    the one a rewrite would silently drop: height off the floor spreads a
    shadow and thins it. Without it a jump is a body sliding up the screen.
    """
    source = _world_source()
    emitted = _run_node(_shadow_driver(
        source,
        "console.log(JSON.stringify({ down: run(0), up: run(100) }));",
    ))
    down, up = emitted["down"], emitted["up"]
    assert up["width"] > down["width"] and up["height"] > down["height"], (
        f"a figure 100 units off the floor cast a {up['width']:.1f}-wide "
        f"shadow against {down['width']:.1f} on the floor - it does not spread"
    )
    assert up["alpha"] < down["alpha"], (
        f"a lifted figure's shadow drew at alpha {up['alpha']:.3f} against "
        f"{down['alpha']:.3f} on the floor - it does not fade"
    )

    block = _js_block(source, "function shadowTick(")
    mutated = block.replace(
        "const lift = Math.max(0, char.baseY - char.container.y);",
        "const lift = 0;",
    )
    assert mutated != block, "the mutation did not change the source"
    broken = _run_node(_shadow_driver(
        source,
        "console.log(JSON.stringify({ down: run(0), up: run(100) }));",
        tick=mutated,
    ))
    assert broken["up"] == broken["down"], (
        "a shadowTick that ignores lift still passed - these assertions do "
        "not read the lift response"
    )


@needs_node
def test_an_unmeasured_room_still_puts_a_shadow_under_its_cast():
    """The procedural fallback room has no plate and therefore no measured
    lamp, so `light_for` hands the page `DEFAULT_LIGHT`. That default must
    still ground the cast: a shadow is the only thing telling a viewer where
    the floor is, and the room that has lost its painting is the last one
    that can afford figures floating in front of nothing (KI-045/KI-047).

    What the neutral default withholds is the DIRECTION, not the shadow -
    with `height: 0` the throw is zero, so the fallback gets the centred
    patch it had before this ticket rather than a made-up cast direction.
    `DEFAULT_LIGHT` is imported rather than retyped, so this cannot drift
    from what `world/light.py` actually serves.
    """
    source = _world_source()
    neutral = json.loads(as_json(DEFAULT_LIGHT))
    rest = _run_node(_shadow_driver(
        source, "console.log(JSON.stringify({ rest: run(0) }));", light=neutral
    ))["rest"]
    assert rest["alpha"] > 0, (
        "a room with no measured light drew its cast with alpha 0 - every "
        "figure in the fallback room floats"
    )
    assert rest["width"] > 0 and rest["height"] > 0, (
        "a room with no measured light drew a zero-sized shadow"
    )
    assert abs(rest["dx"]) < 1e-9 and abs(rest["dy"]) < 1e-9, (
        f"a room with no measured light threw its shadow ({rest['dx']:+.3f}, "
        f"{rest['dy']:+.3f}) - it invented a lamp direction"
    )


@needs_node
def test_a_light_with_no_contact_block_degrades_instead_of_blanking_the_page():
    """`CONTACT` is read at TOP LEVEL, which is the flavour of TypeError that
    throws before `boot()` is ever called and leaves a blank page that every
    page-source test still passes (the Task 2 failure mode). A `LIGHT` with
    no `contact` — which `light_for` cannot produce, but a hand-edited page
    or a future injection bug could — must degrade, not throw.
    """
    source = _world_source()
    light = _page_light(source)
    stripped = {k: v for k, v in light.items() if k != "contact"}
    assert "contact" not in stripped
    rest = _run_node(_shadow_driver(
        source, "console.log(JSON.stringify({ rest: run(0) }));", light=stripped
    ))["rest"]
    assert rest["width"] > 0 and rest["height"] > 0, (
        "a contact-less LIGHT drew a zero-sized shadow rather than degrading "
        "to the body's own patch"
    )
    assert abs(rest["dx"]) < 1e-9 and abs(rest["dy"]) < 1e-9, (
        "a contact-less LIGHT still threw a shadow somewhere"
    )


# --- Task 8: mood shifts the light, not the paint ---------------------------
#
# Until this ticket a mood was a PixiJS container tint set on an ALREADY-shaded
# figure (`char.body.tint = tint`). A container tint is a shader multiply over
# every pixel the container drew, so it did not only recolour the body: it
# recoloured the light. `paint()`'s lit band mixes toward `LIGHT.warmth` and its
# shade band toward `LIGHT.ambient`, and both of those came out multiplied by
# the mood — an amber desk lamp that turned red when the market fell. That is
# what made every mood read as "the same figure, but red".
#
# The seam Task 6 built for this is `paint(g, volume, tint)`'s third argument,
# which until now only ever received `BODY_FILL`. The mood now goes in THERE, so
# the room's lamp stays the room's lamp and what changes is the colour it falls
# on.
#
# `BODY_TINT` (not `MOOD_COLOR`) stays the source: `world.visuals.body_tints()`
# is the identity palette lifted to `SILHOUETTE_MIN_CONTRAST`, which is KI-028's
# measured fix. But it is a *tint*, and the floor was measured on
# `visuals._rendered()` — "what the canvas actually shows: the base fill
# multiplied by the tint". So the colour a body is now painted with is that
# product, computed once as a colour instead of per-pixel by the GPU. Handing
# `paint()` the raw tint instead would ship every body 1/0.816 = 1.23x brighter
# than the number `body_contrast()` asserts, i.e. silently move a measured
# quantity on a ticket that must not.
#
# The RIM is the second half, and it is here because a frame caught it and these
# tests did not. Dropping the container multiply also dropped it from the rim,
# which `BODY_RIM_SHADED` computes once from the lamp over a white base — so the
# first frame of this ticket outlined every figure in the lamp's cream #fbe5c8
# (11,491 pixels, and most of a 14-unit arm), a die-cut sticker keyline, which
# is the KI-051 read this sprint exists to remove. `world/visuals.py` names the
# invariant that broke: the rim is brighter than the fill and "the ratio between
# them is fixed here and nowhere else". The container tint was what held it. So
# the mood now reaches each material's OWN albedo before the light —
# `moodFill = BODY_FILL x tint`, `moodRim = BODY_RIM.color x tint` — and, because
# the rim's base is white, `moodRim` comes out as the lifted tint itself: the
# same ratio, by construction rather than by a number restated on the page.
#
# The tests missed it because `applyMood` used to take two colours and these
# drivers supplied both, so the seam that could forget one was `setExpression`,
# which no driver ran. `applyMood(char, mood)` now derives both itself; there is
# no argument left to omit, and the mutation that reproduces the shipped bug
# fails here.


def _half_up(value: float) -> int:
    """`Math.round`, for the non-negative channel values here.

    Python's `round` is banker's (`round(0.5) == 0`) and JS's is half-up
    (`Math.round(0.5) === 1`). Channel products land exactly on .5 often
    enough that reusing `round` here would produce a one-mood mismatch that
    looks like a flake and is really a rounding-mode bug in the test.
    """
    return int(value + 0.5)


def _mul_channels(a: tuple, b: tuple) -> tuple:
    """`base x tint / 255` — `visuals._rendered`, quantised to what ships."""
    return tuple(_half_up(a[i] * b[i] / 255) for i in range(3))


def _mix_channels(a: tuple, b: tuple, t: float) -> tuple:
    """`mixHex` in Python. Independent of the page, by design."""
    return tuple(_half_up(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _rgb(value) -> tuple:
    if isinstance(value, str):
        value = int(value.lstrip("#"), 16)
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def _int(rgb: tuple) -> int:
    return (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]


def _page_face_moods(source: str) -> set:
    """Every mood the cast can actually be put into — the keys of the page's
    own `FACE` table, which is what `setExpression` is called with."""
    face = _js_block(source, "const FACE = {")
    return set(re.findall(r"^\s*(\w+):\s*\{", face, re.M))


def _mood_fill_driver(source: str, mood_fill: str, emit: str) -> str:
    return (
        _js_const(source, "MOOD_COLOR")
        + _js_const(source, "NEUTRAL")
        + _js_const(source, "BODY_TINT")
        + _js_const(source, "BODY_FILL")
        + _js_const(source, "BODY_RIM")
        + _js_block(source, "function mulHex(")
        + "\n"
        + _js_block(source, "function moodTint(")
        + "\n"
        + mood_fill
        + "\n"
        + _js_block(source, "function moodRim(")
        + "\n"
        + emit
    )


@needs_node
def test_a_mood_resolves_to_the_colour_the_room_lights_not_to_a_multiplier():
    """The mood's body colour is `BODY_FILL x BODY_TINT[mood]` — the product
    `visuals._rendered()` names as "what the canvas actually shows", and the
    exact quantity `SILHOUETTE_MIN_CONTRAST` was measured on (KI-028).

    Driven for real in node over every mood in the page's own table and
    compared against a product computed here from `visuals.BODY_BASE_FILL`
    and `visuals.body_tint()`, not against a copy of the page's arithmetic.
    Two mutations follow, and each one is a bug that has a name.
    """
    from world.visuals import (
        BODY_BASE_FILL,
        BODY_RIM_FILL,
        MOOD_COLORS,
        body_tint,
    )

    source = _world_source()
    mood_fill = _js_block(source, "function moodFill(")
    emit = (
        "const out = {};\n"
        "for (const mood of Object.keys(BODY_TINT)) {\n"
        "  out[mood] = { fill: moodFill(mood), rim: moodRim(mood) };\n"
        "}\n"
        'const u = "no-such-mood-was-ever-emitted";\n'
        'out["__unknown__"] = { fill: moodFill(u), rim: moodRim(u) };\n'
        "console.log(JSON.stringify(out));\n"
    )
    emitted = _run_node(_mood_fill_driver(source, mood_fill, emit))

    base = _rgb(BODY_BASE_FILL)
    rim_base = _rgb(BODY_RIM_FILL)
    expected = {
        mood: _int(_mul_channels(base, _rgb(body_tint(mood))))
        for mood in MOOD_COLORS
    }
    expected_rim = {
        mood: _int(_mul_channels(rim_base, _rgb(body_tint(mood))))
        for mood in MOOD_COLORS
    }
    for mood, want in expected.items():
        assert emitted[mood]["fill"] == want, (
            f"moodFill({mood!r}) = {emitted[mood]['fill']:#08x}, expected "
            f"{want:#08x} — the page is not shipping BODY_FILL x BODY_TINT[mood]"
        )
        # The rim is a SECOND base colour carrying the same mood, not a
        # decoration: `visuals` says the rim-over-fill ratio "is fixed here and
        # nowhere else", and the container tint used to be what held it. Losing
        # it outlines every figure in the lamp's cream — measured at 11,491
        # keyline pixels on this ticket's first frame.
        assert emitted[mood]["rim"] == expected_rim[mood], (
            f"moodRim({mood!r}) = {emitted[mood]['rim']:#08x}, expected "
            f"{expected_rim[mood]:#08x} — the mood is not reaching the rim's "
            "own albedo"
        )
    # An unknown mood must still resolve THROUGH the multiply on BOTH surfaces,
    # or every un-mooded figure in the room gets brighter than every mooded one.
    neutral_tint = int(
        re.search(r"const NEUTRAL = (0x[0-9a-fA-F]+);", source).group(1), 16
    )
    assert emitted["__unknown__"]["fill"] == _int(
        _mul_channels(base, _rgb(neutral_tint))
    ), (
        "an unknown mood skipped the multiply — the neutral fallback must be "
        "the neutral FILL, not the neutral tint"
    )
    assert emitted["__unknown__"]["rim"] == _int(
        _mul_channels(rim_base, _rgb(neutral_tint))
    ), "an unknown mood's rim skipped the multiply"

    # Mutation 1: hand `paint()` the raw tint (the shape the brief's own text
    # suggests). Every mood ships ~1.23x brighter than the measured number.
    raw = mood_fill.replace("mulHex(BODY_FILL, moodTint(mood))", "moodTint(mood)")
    assert raw != mood_fill, "the multiply mutation did not change the source"
    broken = _run_node(_mood_fill_driver(source, raw, emit))
    assert any(broken[m]["fill"] != expected[m] for m in expected), (
        "dropping the BODY_FILL multiply still produced the measured colours — "
        "this assertion would not notice a body 1.23x brighter than KI-028's "
        "own arithmetic"
    )

    # Mutation 2: read the raw identity palette instead of the lifted one.
    # This is KI-028 itself, reverted: `MOOD_COLORS` is the palette BEFORE the
    # contrast lift, so bodies sink back into the room.
    mood_tint = _js_block(source, "function moodTint(")
    reverted = mood_tint.replace("BODY_TINT[mood]", "MOOD_COLOR[mood]")
    assert reverted != mood_tint, "the palette mutation did not change the source"
    sunk = _run_node(
        _mood_fill_driver(source, mood_fill, emit).replace(mood_tint, reverted)
    )
    assert any(sunk[m]["fill"] != expected[m] for m in expected), (
        "swapping BODY_TINT for the un-lifted MOOD_COLOR changed nothing — "
        "the page is not reading the contrast-lifted table at all"
    )
    from world.visuals import PALETTE, SILHOUETTE_MIN_CONTRAST, contrast_ratio

    room = _rgb(PALETTE["bg"])
    below = [
        m for m in expected
        if contrast_ratio(_rgb(sunk[m]["fill"]), room) < SILHOUETTE_MIN_CONTRAST
    ]
    assert below, (
        "reverting to MOOD_COLOR left every mood above the contrast floor, so "
        "this test cannot tell the lifted table from the raw one"
    )


@needs_node
def test_every_mood_the_cast_can_wear_resolves_to_a_body_colour():
    """The registry invariant, in the shape the reaction registries use: a mood
    the world can emit but the cast cannot paint is a silent grey figure.

    Graded against `BODY_TINT`'s own key set — deliberately NOT `MOOD_COLOR`'s.
    They hold the same moods, but only one of them is the contrast-lifted table
    KI-028 resolved, and pointing this invariant at the other one is how that
    fix gets quietly reverted by a test.

    `neutral` is the one mood in `FACE` with no entry, and that is on purpose:
    it is `character()`'s pre-mood default and `visuals.MOOD_COLORS` does not
    claim it. It has to resolve to the neutral FILL rather than to `undefined`,
    which is checked by running it.
    """
    from world.visuals import MOOD_COLORS

    source = _world_source()
    table = json.loads(re.search(r"const BODY_TINT = (\{.*?\});", source).group(1))
    assert set(table) == set(MOOD_COLORS), (
        "the page's body-colour table and world.visuals.MOOD_COLORS disagree: "
        f"page-only {sorted(set(table) - set(MOOD_COLORS))}, "
        f"module-only {sorted(set(MOOD_COLORS) - set(table))}"
    )

    face_moods = _page_face_moods(source)
    assert face_moods, "the page no longer declares a FACE table"
    assert face_moods - set(table) == {"neutral"}, (
        "a mood the cast can be put into has no body colour of its own: "
        f"{sorted(face_moods - set(table) - {'neutral'})}"
    )

    mood_fill = _js_block(source, "function moodFill(")
    emit = (
        f"const moods = {json.dumps(sorted(face_moods))};\n"
        "const out = {};\n"
        "for (const mood of moods) {\n"
        "  out[mood] = { fill: moodFill(mood), rim: moodRim(mood) };\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    emitted = _run_node(_mood_fill_driver(source, mood_fill, emit))
    for mood in sorted(face_moods):
        for surface in ("fill", "rim"):
            value = emitted[mood][surface]
            assert isinstance(value, int) and 0 <= value <= 0xFFFFFF, (
                f"moodFill/{surface} for {mood!r} returned {value!r}, not a "
                "colour — this mood renders as whatever PixiJS does with a "
                "non-colour"
            )

    # Mutation: remove the fallback. `neutral` has no table entry, so it is the
    # mood that proves the fallback is real rather than decorative.
    mood_tint = _js_block(source, "function moodTint(")
    unguarded = mood_tint.replace(": NEUTRAL", ": NaN")
    assert unguarded != mood_tint, "the fallback mutation did not change the source"
    broken = _run_node(
        _mood_fill_driver(source, mood_fill, emit).replace(mood_tint, unguarded)
    )
    assert broken["neutral"] != emitted["neutral"], (
        "breaking the unknown-mood fallback left `neutral` unchanged — nothing "
        "here would notice an un-mooded figure losing its colour"
    )


def _mood_paint_driver(source: str, apply_block: str, repaint_block: str) -> str:
    """The real shading rig, plus Graphics recorders that also trap `tint`.

    The trap is the point: the defect this ticket removes is invisible to a
    fill recorder (the fills never change, the GPU does the recolouring), so
    the test has to be able to see a container tint being written.
    """
    return (
        "const PLATE = null;\n"
        + _js_const(source, "MOOD_COLOR")
        + _js_const(source, "NEUTRAL")
        + _js_const(source, "BODY_TINT")
        + _js_const(source, "BODY_FILL")
        + _js_const(source, "BODY_RIM")
        + _js_const(source, "LIGHT")
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + _shading_prelude(source)
        + _js_block(source, "function mulHex(")
        + "\n"
        + _js_block(source, "function moodTint(")
        + "\n"
        + _js_block(source, "function moodFill(")
        + "\n"
        + _js_block(source, "function moodRim(")
        + "\n"
        + _js_block(source, "function lightenTint(")
        + "\n"
        + repaint_block
        + "\n"
        + apply_block
        + "\n"
        + """
        function makeG() {
          const g = { fills: [], strokes: [], cleared: 0, tintWrites: [] };
          g.roundRect = () => g;
          g.circle = () => g;
          g.fill = (c) => { g.fills.push(c); return g; };
          g.stroke = (s) => { g.strokes.push(s); return g; };
          g.clear = () => { g.cleared++; g.fills = []; g.strokes = []; return g; };
          Object.defineProperty(g, "tint", {
            get() { return 0xffffff; },
            set(v) { g.tintWrites.push(v); },
          });
          return g;
        }
        const body = makeG(), skull = makeG(), arm = makeG();
        arm.shadeFactor = 1.35;
        // The figure's chest, the shared skull, and one of its arms - three
        // real call sites, copied from BODIES.figure/character().
        paint(body, litRect(-33, -128, 66, 54, 22), BODY_FILL);
        paint(skull, litCircle(0, 0, 28), BODY_FILL);
        paint(arm, litRect(-7, -5, 14, 66, 7), BODY_FILL);
        const rims = (g) => g.strokes.map((s) => (s && s.color) ?? null);
        const snapshot = () => ({
          body: body.fills.slice(), bodyRim: rims(body),
          skull: skull.fills.slice(), skullRim: rims(skull),
          arm: arm.fills.slice(), armRim: rims(arm),
        });
        const built = snapshot();
        const char = { body, skull, accents: [arm] };
        applyMood(char, "dejected");
        const dejected = snapshot();
        applyMood(char, "elated");
        const elated = snapshot();
        console.log(JSON.stringify({
          built, dejected, elated,
          tintWrites: body.tintWrites.concat(skull.tintWrites, arm.tintWrites),
          cleared: body.cleared,
        }));
        """
    )


@needs_node
def test_a_mood_change_repaints_the_bands_instead_of_tinting_the_figure():
    """The call-site change, graded on the colours that actually reach
    `g.fill()` and `g.stroke()`.

    Runs the page's real `applyMood` over the page's real `paint`, and
    checks the three bands of a body against band colours computed here from
    `visuals` and the plate's measured `LIGHT` — so the lit band has to be the
    mood mixed toward the LAMP's amber and the shade band the mood mixed
    toward the ROOM's cold ambient. Under the old container tint those two
    were the lamp and the room multiplied by the mood, and no colour a fill
    recorder saw ever changed at all.
    """
    from world.visuals import BODY_BASE_FILL, BODY_RIM_FILL, body_tint

    source = _world_source()
    light = json.loads(re.search(r"const LIGHT = (\{.*\});", source).group(1))
    apply_block = _js_block(source, "function applyMood(")
    repaint_block = _js_block(source, "function repaint(")
    emitted = _run_node(_mood_paint_driver(source, apply_block, repaint_block))

    base = _rgb(BODY_BASE_FILL)
    warmth, ambient = _rgb(light["warmth"]), _rgb(light["ambient"])
    ramp = light["ramp"]

    def bands(fill: tuple) -> list:
        """`paint()`'s three fills, in the order it draws them."""
        out = []
        for rung in ("shade", "base", "lit"):
            level = ramp[rung]
            toward = warmth if level >= 0 else ambient
            out.append(_int(_mix_channels(fill, toward, min(1, abs(level)))))
        return out

    def lighten(fill: tuple, factor: float) -> tuple:
        return tuple(max(0, min(255, _half_up(c * factor))) for c in fill)

    def rim_of(fill: tuple) -> int:
        """A rim is the lit edge: the same base colour at `rampLevel("lit")`."""
        return _int(_mix_channels(fill, warmth, min(1, abs(ramp["lit"]))))

    for mood in ("dejected", "elated"):
        tint = _rgb(body_tint(mood))
        fill = _mul_channels(base, tint)
        rim = _mul_channels(_rgb(BODY_RIM_FILL), tint)
        assert emitted[mood]["bodyRim"] == [rim_of(rim)], (
            f"the {mood} body's rim is {emitted[mood]['bodyRim']}, expected "
            f"[{rim_of(rim):#08x}] — the mood must reach the rim's own albedo, "
            "or every figure is outlined in the lamp's cream"
        )
        assert emitted[mood]["armRim"] == [rim_of(lighten(rim, 1.35))], (
            f"the {mood} arm's rim did not follow its own lightened fill"
        )
        assert emitted[mood]["body"] == bands(fill), (
            f"the {mood} body's bands are {[hex(c) for c in emitted[mood]['body']]}, "
            f"expected {[hex(c) for c in bands(fill)]} — the mood is not what "
            "paint() is lighting"
        )
        assert emitted[mood]["skull"] == bands(fill), (
            f"the {mood} skull did not follow its body — a flat sticker head "
            "on a lit figure is the KI-051 defect, one volume up"
        )
        assert emitted[mood]["arm"] == bands(lighten(fill, 1.35)), (
            f"the {mood} arm lost its shadeFactor lighten — the figure's arms "
            "read as part of the torso at rest again"
        )

    # No container tint anywhere. The one assertion the old code fails.
    assert emitted["tintWrites"] == [], (
        f"a mood was still written as a container tint ({emitted['tintWrites']}) "
        "— that multiplies the room's own lamp by the mood"
    )
    # The mood must actually have moved the paint, and the repaint must
    # REPLACE the construction fills rather than stack a second set on top.
    assert emitted["built"]["body"] != emitted["dejected"]["body"], (
        "the mood change left the body painted in the un-mooded BODY_FILL"
    )
    assert emitted["dejected"]["body"] != emitted["elated"]["body"], (
        "two different moods painted the body the same colour"
    )
    assert len(emitted["elated"]["body"]) == len(emitted["built"]["body"]), (
        "a repaint left more fills on the body than paint() drew — the bands "
        "are stacking rather than being redrawn"
    )
    assert emitted["cleared"] >= 2, "the body was repainted without being cleared"
    assert emitted["dejected"]["bodyRim"] != emitted["elated"]["bodyRim"], (
        "two different moods struck the same rim colour — this is the cream "
        "keyline this ticket's first frame shipped, before it was looked at"
    )

    # Mutation 1: the bug this ticket actually shipped and a frame caught.
    # Strike the rim from its own un-mooded constant instead of from the mood,
    # and every figure is outlined in the lamp's cream regardless of colour.
    keyline = apply_block.replace("const rim = moodRim(mood);",
                                  "const rim = BODY_RIM.color;")
    assert keyline != apply_block, "the keyline mutation did not apply"
    outlined = _run_node(_mood_paint_driver(source, keyline, repaint_block))
    assert outlined["dejected"]["bodyRim"] == outlined["elated"]["bodyRim"], (
        "dropping the mood from the rim did not collapse the rim colours — "
        "the keyline assertion above is not load-bearing"
    )

    # Mutation 2: put the container tint back. This is the pre-ticket code.
    tinted = apply_block.replace(
        "repaint(char.body, fill, rim);", "char.body.tint = fill;"
    )
    assert tinted != apply_block, "the container-tint mutation did not apply"
    broken = _run_node(_mood_paint_driver(source, tinted, repaint_block))
    assert broken["tintWrites"] != [] and (
        broken["dejected"]["body"] == broken["built"]["body"]
    ), (
        "restoring `char.body.tint = fill` produced the same result as "
        "repainting — these assertions cannot see the defect this ticket "
        "exists to remove"
    )

    # Mutation 3: repaint without clearing. Bands stack instead of replacing.
    stacking = repaint_block.replace("g.clear();", "")
    assert stacking != repaint_block, "the clear mutation did not apply"
    stacked = _run_node(_mood_paint_driver(source, apply_block, stacking))
    assert len(stacked["elated"]["body"]) != len(emitted["elated"]["body"]), (
        "dropping g.clear() from repaint changed nothing observable — the "
        "stacking check is not load-bearing"
    )


# --- Sprint 16 Task 10: the page moves onto the surfaces Task 9 declared ---
#
# Task 9 made text placement DATA and proved the data is internally
# consistent (`tests/unit/test_text_layout.py`): every placement lands
# inside the surface it names. It could not prove two things only the PAGE
# can answer, which is what this section is for:
#
#   1. That the page actually READS that data instead of a redefinition
#      that happens to mention `TEXT_PLACEMENTS` somewhere nearby. "Reading
#      a placement does not prove landing inside a surface" - the
#      dispatcher's own ruling against this ticket's brief.
#   2. That the GLYPHS the page draws into a placement's box actually fit
#      it. A placement can be a perfectly valid rectangle, fully inside a
#      perfectly real surface, and still have no room for its own text -
#      Task 9's first-pass `tube-plinth-*` sizing (9-10px surfaces for an
#      8px line) is the shipped example of exactly that failure.
#
# Scope, stated plainly: `banner`/`prices`/`record` are DOM (`#banner`,
# `#nowband`), not single-line canvas text - `banner`'s position is checked
# below (bound via inline style), but none of the three get the glyph-fit
# check, because CSS wraps them and the single-line glyph estimate does not
# apply to wrapped text. `history`, `name-trader` and `name-model` are the
# room's only canvas-drawn labels left after this ticket, and get both
# checks in full.


def _text_placements():
    """The resolved `__TEXT_JSON__` this page was actually served - read
    from the same function `api/main.py` calls, not re-parsed out of the
    HTML, matching `_manifest()`'s own convention above."""
    from world.plate import load_manifest
    from world.text_layout import as_json

    return json.loads(as_json(load_manifest()))


def _text_driver(body: str) -> str:
    """`label`/`placedLabel`, run for real against a stub `PIXI.Text` that
    records what it was given rather than rendering anything - the same
    "real arithmetic, stubbed renderer" shape `_layout_driver` already uses
    for `anchorFor`/`pillarGeometry` above."""
    return (
        "class FakeText {\n"
        "  constructor(opts) {\n"
        "    this.text = opts.text; this._size = opts.style.fontSize;\n"
        "  }\n"
        "}\n"
        "const PIXI = { Text: FakeText };\n"
        + _js_block(body, "function label(")
        + "\n"
        + _js_block(body, "function placedLabel(")
        + "\n"
    )


def test_the_room_no_longer_floats_a_symbol_or_mood_label_at_a_fixed_offset():
    """The room's own floating labels, observed 2026-09-01, by call site:
    two per painted tube (symbol, mood) and three writes into the cast's
    `moodTag` (the model's live mood, the trader's live mood, a reaction's
    live mood). `moodTag` itself is NOT deleted - ruling: it is shared with
    the gallery and animation-sheet eval surfaces (`?gallery=1`/`?anims=1`),
    where labelling a specimen by mood is legitimate diagnostic use and
    never goes on air - only the ROOM's own writes into it are gone."""
    body = client.get("/world").text
    assert "label(data.mood" not in body
    assert "label(symbol," not in body
    assert "model.moodTag.text =" not in body
    assert "trader.moodTag.text =" not in body
    assert "target.moodTag.text =" not in body
    # Still shared machinery for the gallery/sheet - not deleted wholesale.
    assert "sample.moodTag.text = mood;" in body
    assert "sample.moodTag.text = anim;" in body
    assert "TEXT_PLACEMENTS" in body


def test_the_history_line_sits_on_the_bottom_band():
    body = client.get("/world").text
    draw_fn = _js_block(body, "function draw(")
    assert "TEXT_PLACEMENTS.history" in draw_fn
    # Was `app.screen.height - 86`, which is why it ran across the tubes.
    # A page-wide ban on `app.screen.height` would be over-broad - other
    # functions legitimately use it for non-text layout - so this is scoped
    # to `draw()` itself, which after this change has no remaining reason to
    # read it at all.
    assert "app.screen.height" not in draw_fn


def test_the_banner_is_bound_to_its_placement_not_only_its_bar_height():
    """`bannerMinHeight()` already sized the bar's own background from
    `PLATE.bands.top` (Task 4) - that is not text, so it is untouched. This
    is the new half: the READABLE line inside that bar binds to
    `TEXT_PLACEMENTS.banner` directly, the same rule every canvas label now
    follows."""
    body = client.get("/world").text
    assert "TEXT_PLACEMENTS.banner" in body
    assert "banner.style.paddingLeft" in body
    assert "banner.style.paddingTop" in body


@needs_node
def test_placedLabel_positions_and_sizes_a_label_from_its_placement_alone():
    body = client.get("/world").text
    driver = _text_driver(body) + """
    const drawn = placedLabel({ x: 12, y: 34, size: 16 }, "HELLO", 0);
    const missing = placedLabel(null, "HELLO", 0);
    console.log(JSON.stringify({
      x: drawn.x, y: drawn.y, size: drawn._size, text: drawn.text,
      missingIsNull: missing === null,
    }));
    """
    emitted = _run_node(driver)
    assert emitted == {
        "x": 12, "y": 34, "size": 16, "text": "HELLO", "missingIsNull": True,
    }


@needs_node
def test_placedLabel_trusts_its_placement_so_the_manifest_check_is_the_real_guard():
    """`placedLabel` must not silently clamp a bad position - if it did, a
    regression in `floating_text` (the manifest-level guard) could break
    with nothing on the page able to show it. Corrupts a real placement
    exactly the way `test_a_label_nudged_off_its_surface_is_caught`
    (`tests/unit/test_text_layout.py`) does - `name-trader` nudged off
    `desk-plate-trader` - resolves it exactly as `as_json` would, and
    confirms the page's own function reproduces the escaped position
    verbatim. That is the proof this ticket's brief was missing: not that
    the page mentions `TEXT_PLACEMENTS`, but that a bad value in it reaches
    the screen unmodified, which is what makes the manifest-level check the
    thing actually protecting the room rather than a redundant assertion
    nothing depends on.
    """
    import dataclasses

    from world.plate import load_manifest
    from world.text_layout import as_json, floating_text

    manifest = load_manifest()
    placements = [dict(p) for p in manifest.text]
    idx = next(i for i, p in enumerate(placements) if p["id"] == "name-trader")
    placements[idx] = dict(placements[idx], y=placements[idx]["y"] - 400)
    poisoned = dataclasses.replace(manifest, text=tuple(placements))

    # Sanity: the manifest-level guard does catch it (established already by
    # test_text_layout.py; re-asserted here only to anchor the claim below).
    assert floating_text(poisoned) == ["name-trader"]

    resolved = json.loads(as_json(poisoned))["name-trader"]
    body = client.get("/world").text
    driver = _text_driver(body) + f"""
    const drawn = placedLabel({json.dumps(resolved)}, "TRADER", 0);
    console.log(JSON.stringify({{ x: drawn.x, y: drawn.y }}));
    """
    emitted = _run_node(driver)
    assert emitted == {"x": resolved["x"], "y": resolved["y"]}, (
        "placedLabel did not draw exactly where the (deliberately bad) "
        "resolved placement said - if it clamped or ignored the escaped "
        "value, the manifest-level check above would not be load-bearing"
    )


@needs_node
def test_no_room_label_the_page_actually_draws_overflows_its_own_box():
    """The gap Task 9 could not close: a placement can be a valid box,
    fully inside a real surface, and still have no room for the text it is
    asked to hold. Runs the page's own `historyLine` for a busy-but-
    realistic world (not an adversarial string - see task-10-report.md for
    why an adversarial one was rejected), and checks every canvas label the
    room still draws against `glyph_overflow`
    (`tests/unit/test_text_layout.py` covers the estimate itself)."""
    from world.text_layout import glyph_overflow

    placements = _text_placements()
    body = client.get("/world").text
    driver = _js_block(body, "function historyLine(") + """
    const h = {
      total_events: 12345,
      longest_streak: { bars: 9999 },
      worst_loss: { realized_return: -0.9999 },
      outages: 99,
    };
    console.log(JSON.stringify({ history: historyLine(h) }));
    """
    emitted = _run_node(driver)

    drawn = {
        "history": emitted["history"],
        "name-model": "MODEL",
        "name-trader": "TRADER",
    }
    for label_id, text in drawn.items():
        p = placements[label_id]
        assert not glyph_overflow(text, p["size"], p), (
            f"{label_id}: {text!r} at size {p['size']} does not fit its own "
            f"{p['w']}x{p['h']} box"
        )


def test_a_label_whose_text_would_overflow_its_box_is_caught():
    # Mutation check for the check above: the same real, shipped box, with
    # text no reasonable margin would ever fit.
    from world.text_layout import glyph_overflow

    placements = _text_placements()
    p = placements["name-trader"]
    assert glyph_overflow("A NAME NO REASONABLE DESK PLATE COULD HOLD", p["size"], p)


@needs_node
def test_a_built_characters_nametag_is_reachable_from_outside_character():
    """Real bug this ticket shipped once, caught only by `scripts/shoot.py`'s
    screenshot, not by any Node-block test above: `character()` builds
    `nameTag` and adds it to the container, but the returned tracking object
    (`const char = {...}`) never listed it - `model.nameTag.text = ""`
    (this ticket's own new code, run right after `character("MODEL")`)
    threw `Cannot set properties of undefined` in a real browser, because
    every other test here reads fields OFF the extracted function's own
    source rather than off a value the function actually returns.

    Runs the real `const char = {...}` object literal with every free
    variable it references pre-stubbed, and asserts the built object
    actually carries `nameTag` - the same field `model.nameTag.text = ""`
    depends on.
    """
    source = _world_source()
    char_literal = _js_block(source, "const char = {")
    driver = f"""
    const container = {{ y: 5 }}, body = {{}}, head = {{}}, skull = {{}},
      eyeL = {{}}, eyeR = {{}}, mouth = {{}}, browL = {{}}, browR = {{}},
      visor = {{}}, accents = [], style = "bars", stat = {{}}, moodTag = {{}},
      nameTag = {{ text: "TRADER" }}, seed = 1, shadow = {{}};
    const FACE_KIND = {{ bars: "circle" }};
    const SHADOW_WIDTH = {{ bars: 10 }};
    function rng32() {{ return () => 0; }}
    {char_literal};
    // The exact call site this ticket added, against the RETURNED object -
    // not the free `nameTag` variable, which would trivially succeed either
    // way and prove nothing about what character() actually hands back.
    char.nameTag.text = "";
    console.log(JSON.stringify({{ hasNameTag: "nameTag" in char }}));
    """
    emitted = _run_node(driver)
    assert emitted["hasNameTag"], (
        "character()'s returned object no longer exposes nameTag - "
        "model.nameTag.text = \"\" / trader.nameTag.text = \"\" would throw"
    )


# --- Task 14 (STRETCH): the painted city's window lights blink -------------
#
# The plate already paints a skyline of window lights outside the room's own
# window; this animates what is there rather than inventing anything new.
# Ruling 2 (controller pre-flight): the brief's own two tests were substring
# checks over the whole page source - `"phase" in body` passes on an English
# sentence, and the `plateReady` check passes on a comment describing a
# MISSING guard. These run the real `buildAmbientLights`/`tickAmbientLights`
# in node, the same way `_glow_driver`'s tests run the real
# `buildGlows`/`applyGlow` above - against the manifest's own `ambient_lights`
# rects, with only `PIXI.Graphics` and `layers.plate` stubbed.


def _ambient_lights_driver(*, plate_ready: bool, lights: list | None = None) -> str:
    """The page's own `buildAmbientLights`/`tickAmbientLights`, run against
    the real manifest's `ambient_lights` rects - only `PIXI.Graphics` and
    `layers.plate` are stubbed, and the stub records what was actually
    drawn/mutated rather than asserting on source text. Mirrors
    `_glow_driver` above: additive light laid ON TOP of the plate's own
    painted lights, not a substitute for them - an opaque fill at alpha 1
    would flatten the painted sign's own detail into a solid block the
    moment a light reached peak brightness, which is the opposite of what
    "the city already has the lights, animate them" asks for.
    """
    source = _world_source()
    ambient_lights = lights if lights is not None else _manifest()["ambient_lights"]
    return (
        f"const PLATE = {json.dumps({'ambient_lights': ambient_lights})};\n"
        f"const plateReady = {str(plate_ready).lower()};\n"
        "class FakeGraphics {\n"
        "  constructor() {\n"
        "    this.rects = []; this.fills = []; this.blendMode = null; this.alpha = 1;\n"
        "  }\n"
        "  rect(x, y, w, h) { this.rects.push([x, y, w, h]); return this; }\n"
        "  fill(opts) { this.fills.push(opts); return this; }\n"
        "}\n"
        "const PIXI = { Graphics: FakeGraphics };\n"
        "const layers = { plate: { children: [],\n"
        "  addChild(c) { this.children.push(c); } } };\n"
        "let ambientLightSprites = [];\n"
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_block(source, "function rng32(")
        + "\n"
        + _js_const(source, "AMBIENT_LIGHTS")
        + "\n"
        + _js_const(source, "AMBIENT_LIGHT_MIN_ALPHA")
        + "\n"
        + _js_block(source, "function buildAmbientLights(")
        + "\n"
        + _js_block(source, "function tickAmbientLights(")
        + "\n"
    )


@needs_node
def test_ambient_lights_use_additive_blend_so_the_paint_survives():
    """An opaque fill at high alpha replaces the pixels underneath rather
    than lighting them - the exact failure a sighted check caught: light G
    (the 39x25 sign at 1659,402) paints a small arrow/icon inside its red
    field, and an opaque rect at alpha~1 flattens that into a featureless
    block, which is the opposite of "the city already has the lights,
    animate them". `buildGlows` (just above this ticket's own code) already
    solves the identical problem the identical way - `glow.blendMode =
    "add"` - so each light must match it.
    """
    driver = _ambient_lights_driver(plate_ready=True) + (
        "buildAmbientLights();\n"
        "console.log(JSON.stringify("
        "ambientLightSprites.map((l) => l.graphics.blendMode)));\n"
    )
    blends = _run_node(driver)
    lights = _manifest()["ambient_lights"]
    assert len(blends) == len(lights)
    assert all(b == "add" for b in blends), (
        "every ambient light must blend additively, or it paints over the "
        "plate's own detail instead of lighting it"
    )


@needs_node
def test_the_window_lights_blink_on_independent_cycles():
    """A shared phase makes the whole skyline pulse in unison, which reads
    as a fault rather than as a city - the same "vary AND offset" rule
    `char.phaseOffset`/`char.breath` already follow for the cast, just above
    this ticket's own code. Runs the real builder/ticker and checks what got
    painted at three different instants, not a claim about the source text.
    """
    lights = _manifest()["ambient_lights"]
    assert len(lights) >= 2, "not enough lights in the manifest to prove independence"

    driver = _ambient_lights_driver(plate_ready=True) + (
        "buildAmbientLights();\n"
        "const phases = ambientLightSprites.map((l) => l.phase);\n"
        "const snapshots = [0, 1.3, 4.7].map((t) => {\n"
        "  tickAmbientLights(t);\n"
        "  return ambientLightSprites.map((l) => l.graphics.alpha);\n"
        "});\n"
        "console.log(JSON.stringify({ phases, snapshots }));\n"
    )
    emitted = _run_node(driver)

    assert len(emitted["phases"]) == len(lights)
    assert len(set(emitted["phases"])) == len(emitted["phases"]), (
        "every light must carry its own phase, not a shared clock"
    )

    # At any single instant the lights must not all read the same
    # brightness - that is what "independent phase" has to mean in practice.
    for snapshot in emitted["snapshots"]:
        assert len(set(snapshot)) > 1, (
            "every light reported the same alpha at one instant - "
            "the skyline pulsed in unison"
        )

    # And each light must actually move between two different instants - a
    # phase that never advances is a fixed dim window, not a blink.
    for i in range(len(lights)):
        trace = [snapshot[i] for snapshot in emitted["snapshots"]]
        assert len(set(trace)) > 1, f"light {i} never changes brightness over time"


@needs_node
def test_ambient_life_is_guarded_for_the_no_plate_path():
    """The KI-050 shape, exactly: an ambient ticker that writes to something
    only the plate path creates kills the renderer on every load. Two claims,
    both run rather than pattern-matched: (1) the realistic no-plate call
    pattern never builds anything to tick in the first place, and (2) even if
    something HAD been built (a future edit, a stale call), `tickAmbientLights`
    must still refuse to touch it while `plateReady` is false - so the
    guard's own behaviour is pinned, not just its presence in a comment.
    """
    # Realistic pattern: buildAmbientLights is never called without a plate
    # (see the drawPlate wiring test below), so there is nothing to tick.
    realistic = _run_node(
        _ambient_lights_driver(plate_ready=False)
        + (
            "tickAmbientLights(3.0);\n"
            "console.log(JSON.stringify(ambientLightSprites.length));\n"
        )
    )
    assert realistic == 0

    # Adversarial: build unconditionally, then prove the GUARD - not just
    # caller discipline - is what stops tickAmbientLights from touching it.
    result = _run_node(
        _ambient_lights_driver(plate_ready=False)
        + (
            "buildAmbientLights();\n"
            "const before = ambientLightSprites.map((l) => l.graphics.alpha);\n"
            "tickAmbientLights(3.0);\n"
            "const after = ambientLightSprites.map((l) => l.graphics.alpha);\n"
            "console.log(JSON.stringify({ before, after }));\n"
        )
    )
    assert result["before"], "buildAmbientLights built nothing to test the guard against"
    assert result["before"] == result["after"], (
        "tickAmbientLights must leave the lights exactly as buildAmbientLights "
        "left them when plateReady is false - it changed them instead"
    )


def test_ambient_lights_build_their_graphics_once_not_inside_the_ticker():
    """RULING 3 (controller pre-flight): this file already carries the scar -
    `buildMonitorGraphics`'s own comment records that rebuilding `Graphics`
    every tick "is exactly what B2 warned would cost the stream frames on a
    box that also encodes 1080p", and Task 12's central ruling was a
    per-poll `Graphics` leak caught before it shipped. Ambient lights tick
    far more often than either. Built once (`buildAmbientLights`, called from
    `drawPlate`'s success path alongside `buildGlows`/`buildMonitorGraphics`);
    the ticker (`tickAmbientLights`, called from `startAmbient`'s
    `app.ticker.add`) only mutates `.alpha`.
    """
    body = _world_source()
    draw_plate = _js_block(body, "async function drawPlate(")
    assert "buildAmbientLights();" in draw_plate, (
        "buildAmbientLights must be called once, when the plate becomes ready"
    )

    ambient_fn = _js_block(body, "function startAmbient()")
    ticker = _js_block(ambient_fn, "app.ticker.add(")
    assert "tickAmbientLights(" in ticker, (
        "the per-frame ticker must actually drive the lights"
    )
    assert "new PIXI.Graphics()" not in ticker, (
        "a Graphics allocation inside the per-frame ticker rebuilds "
        "geometry ~60x/sec"
    )
    assert "buildAmbientLights(" not in ticker, (
        "buildAmbientLights must not be called from inside the ticker"
    )

    tick_fn = _js_block(body, "function tickAmbientLights(")
    assert "new PIXI.Graphics()" not in tick_fn, (
        "tickAmbientLights must only mutate .alpha on the pre-built lights"
    )
    assert "buildAmbientLights(" not in tick_fn, (
        "tickAmbientLights must not call buildAmbientLights() itself either - "
        "that would rebuild the geometry every tick just as surely as "
        "inlining the allocation would"
    )


def test_the_manifest_carries_a_small_list_of_ambient_lights():
    """Schema-level pin: `ambient_lights` is a small list of rects measured
    over painted windows, each with its own colour and blink period - the
    shape `buildAmbientLights`/the node drivers above assume."""
    lights = _manifest()["ambient_lights"]
    assert 2 <= len(lights) <= 12, "expected a SMALL list, not a new effect"
    canvas_w, canvas_h = _manifest()["canvas"]
    for light in lights:
        for key in ("x", "y", "w", "h", "colour", "period"):
            assert key in light, f"ambient light missing {key!r}: {light}"
        assert 0 <= light["x"] <= canvas_w
        assert 0 <= light["y"] <= canvas_h
        assert light["w"] > 0 and light["h"] > 0
        assert re.match(r"^#[0-9a-fA-F]{6}$", light["colour"])
        assert light["period"] > 0


@needs_node
def test_a_malformed_ambient_light_degrades_that_one_light_not_the_room():
    """Every degrade path on this page has a test, because it is what runs
    unattended at 3am. `buildAmbientLights` runs inside `drawPlate`'s try
    block, so an uncaught throw here would degrade the ENTIRE room to
    procedural over one bad manifest entry - the same shape
    `buildMonitorGraphics`'s `if (screen.quad)` guard exists to prevent for
    a missing quad. A hand-edited manifest with no `colour` on one entry
    must lose only that light, not crash the builder.
    """
    lights = [
        {"x": 100, "y": 100, "w": 8, "h": 8, "period": 3.0},   # no colour
        {"x": 200, "y": 200, "w": 8, "h": 8, "colour": "#ff3b30", "period": 3.0},
    ]
    emitted = _run_node(
        _ambient_lights_driver(plate_ready=True, lights=lights)
        + (
            "buildAmbientLights();\n"
            "console.log(JSON.stringify(ambientLightSprites.length));\n"
        )
    )
    assert emitted == 1, (
        "the malformed entry must be skipped, and the well-formed one "
        "still built"
    )
