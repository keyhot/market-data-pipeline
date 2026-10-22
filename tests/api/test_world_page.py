"""The canvas itself isn't unit-testable, so these tests pin the things that
silently break a 24/7 browser source: substitution, the same-origin rule
for the renderer (KI-045), the bounded boot retry, and the textContent-only
rule that keeps event payloads from becoming markup."""

import dataclasses
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from world.light import DEFAULT_LIGHT, as_json
from world.plate import load_manifest

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
    assert "const BODY_TINT = WORLD_CONFIG.body_tints;" in body, (
        "the page does not read the tint table"
    )
    assert set(_served_config(body)["body_tints"]) == set(MOOD_COLORS), (
        "the tint table never reached the page"
    )
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


def _without_comments(js: str) -> str:
    """The same JS with its `//` comments removed.

    A string-matching assertion over page source passes on a comment that
    merely MENTIONS what it is looking for - the `d1ad270` shape this file
    keeps re-learning. It bit again in increment 11: the machine's new body
    explains in prose why it no longer pumps, and the old assertion, which
    searched for `"pump"`, read that sentence and went green on the exact
    change it existed to catch.
    """
    return re.sub(r"//[^\n]*", "", js)


def test_a_comment_cannot_satisfy_a_source_assertion():
    """The guard that makes the test below honest, graded rather than trusted.

    `_without_comments` is the fix for a real green-on-prose failure, so it
    gets a test of its own: the machinery a test depends on is production
    code for that test. A line of commented-out JS that mentions the exact
    assignment must not survive the strip, and a real one must.
    """
    stripped = _without_comments(
        '        // was arm.gestureMode = "pump" until increment 11\n'
        '        arm.gestureMode = "swing";\n'
    )
    assert re.findall(r'gestureMode\s*=\s*"(\w+)"', stripped) == ["swing"], stripped


def test_an_accent_gestures_by_kind_rather_than_all_of_them_rotating():
    """Rotating an accent about its own base swings it like a felled tree, and
    the candle cluster came apart on cheer/wave/shrug. Only what hangs from a
    joint swings; a wick pumps and a base shifts its weight.

    The rule is that the choice is MADE, per body, out of the registry - not
    that any one body makes a particular choice. Increment 11 is why that
    distinction now matters: the MODEL stopped being a cluster of candles and
    became a plated machine with two arms, so the body that used to pump now
    correctly swings, and an assertion naming `bars` and `"pump"` would have
    had to be deleted rather than re-read. What survives the redesign is that
    every mode a body asks for is one the dispatcher can serve, and that they
    are not all the same mode.
    """
    body = client.get("/world").text
    assert "const GESTURE = {" in body
    declared = set(re.findall(r"(\w+): \(", _js_block(body, "const GESTURE = {")))
    assert {"swing", "pump", "shift"} <= declared, declared
    asked = re.findall(r'gestureMode\s*=\s*"(\w+)"', _without_comments(body))
    assert asked, "no body chooses a gesture at all; every accent would swing"
    unknown = sorted(set(asked) - declared)
    assert not unknown, (
        f"{unknown} is asked for but `GESTURE` cannot serve it, so `gesture()` "
        "falls back to swinging the accent about its own origin - silently, "
        "and only on the animations that use it"
    )
    assert len(set(asked)) > 1, (
        f"every accent on the page now gestures by {asked[0]!r}. The registry "
        "exists because one motion does not fit every body"
    )


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
    width is a property of the body — the MODEL stands wider than the figure.
    Same registry shape as HEAD_TRAVEL, checked for the same reason: a new
    body silently inheriting someone else's footprint reads as floating.

    The ordering outlived the body that first justified it. It used to be the
    candle cluster's five splayed bars; since increment 11 it is the machine's
    two legs, which plant 48 units apart against the figure's 38.
    """
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
    # KI-084 split the accents either side of the body (`depthOrder`), and the
    # per-figure scale then moved that whole composite onto a `rig` node inside
    # the container, so the claim this test actually makes — the body and its
    # accents hang off the character, and the shadow does NOT — is checked on
    # the parts that survived both rather than on the old literal.
    # The MODEL's badge (2026-09-24) rides between the body and the head.
    assert 'rig.addChild(...depthOrder(accents, "far"), body, ...(badge ? [badge] : []), head' in made
    assert "container.addChild(rig," in made
    assert "container.addChild(shadow" not in made, "the shadow is parented to the body"
    assert "rig.addChild(shadow" not in made, "the shadow scales with the figure"


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
    `signal_resolved` cut at 0.6/1.2/1.8 (bps of realized move since KI-041) — so the room's single most frequent
    event has always rendered at tier 0: minimum amplitude, no swell, no tier
    colour. Exactly the drift a restated reaction registry would cause, and the
    same fix — the scale comes from `world.state`."""
    from world.state import GENERIC_TIER_CUTS, tier_cuts

    for path in ("/world", "/overlay/events"):
        body = client.get(path).text
        assert "__TIER_CUTS_JSON__" not in body, path
        assert "severity >= 8" not in body, f"{path} still keeps its own scale"
        if path == "/world":
            assert "const TIER_CUTS = WORLD_CONFIG.tier_cuts;" in body
            cuts = _served_config(body)["tier_cuts"]
        else:
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
    version drifted from the server. One definition, in `world.state`. (Since
    #41 `/world` carries it as page code reading the config; the overlay is
    still injected, and test_world_config.py runs the page's copy against
    `severity_tier`.)"""
    from world.state import severity_tier, tier_of_js

    for path in ("/world", "/overlay/events"):
        body = client.get(path).text
        assert body.count("function tierOf") == 1, path
        assert "__TIER_OF_JS__" not in body, f"{path} never got the injection"
        if path == "/overlay/events":
            assert tier_of_js() in body, f"{path} carries its own copy"
        else:
            # `/world` carries it as page code that reads the config (#41);
            # test_world_config.py runs THAT function against `severity_tier`.
            assert (
                "const cuts = TIER_CUTS.cuts[eventType] ?? TIER_CUTS.generic;" in body
            )

    # And the copy agrees with the server it mirrors: same lookup, same count.
    cuts = re.search(
        r"const cuts = TIER_CUTS\.cuts\[eventType\] \?\? TIER_CUTS\.generic;",
        tier_of_js(),
    )
    assert cuts, "the injected function stopped reading the injected cuts"
    assert severity_tier("signal_resolved", 30.0) == 2, "the rule itself moved"


def test_room_lighting_comes_from_the_shared_ramp():
    """Four lighting constants loose in a template is how the tier scale went
    wrong (KI-019). The room's calm→dramatic lighting is injected from
    `world.visuals.room_light`, and the page keys it off `ambient.tier`, which
    is the server's own `severity_tier` as it arrives in `/world/state`."""
    from world.visuals import room_light

    body = client.get("/world").text
    assert "__ROOM_LIGHT_JSON__" not in body
    assert "const ROOM_LIGHT = WORLD_CONFIG.room_light;" in body
    injected = _served_config(body)["room_light"]
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


def _served_config(body: str | None = None) -> dict:
    """The page's one JSON config block (#41), parsed - what `const NAME =
    WORLD_CONFIG.key;` resolves to. Tests that used to regex a literal out of
    the served page read the value here instead."""
    body = body if body is not None else client.get("/world").text
    found = re.search(
        r'<script type="application/json" id="world-config">(.*?)</script>',
        body,
        re.S,
    )
    assert found, "the page carries no world-config block"
    return json.loads(found.group(1))


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
    """M1: the painted monitors decide nothing. The numbers come from the
    server in the config block, and the functions that read them read nothing
    else (#41 made those page code; test_monitors.py runs them against
    world/monitors.py) - because injecting only the numbers was the half-fix
    that let KI-019 happen.
    """
    from world.monitors import monitor_rules

    # #41: the numbers ride in the config block; the three functions that read
    # them are page code, run against world/monitors.py by test_monitors.py.
    body = client.get("/world").text
    assert _served_config(body)["monitor_rules"] == monitor_rules()
    assert "const MONITOR_RULES = WORLD_CONFIG.monitor_rules;" in body

    # Whatever the template does with them, it must not ship them unsubstituted.
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

    from api.routers import world as world_router

    class _Spec:
        def __init__(self, symbol):
            self.symbol = symbol
            self.market = "crypto"
            self.predict = True

    class _Watchlist:
        tickers = [_Spec("BTCUSDT"), _Spec("ETHUSDT"), _Spec("SOLUSDT")]

    original = world_router.load_watchlist
    world_router.load_watchlist = lambda: _Watchlist()
    try:
        with caplog.at_level(_logging.WARNING):
            client.get("/world")
    finally:
        world_router.load_watchlist = original

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
    line = match.group(0).strip() + "\n"
    # A const that reads the config is lifted WITH the values it resolves to:
    # the driver has no `WORLD_CONFIG`, and restating the value in the test
    # would test the test.
    if "WORLD_CONFIG." in line:
        config = _served_config(source)
        line = re.sub(
            r"WORLD_CONFIG\.(\w+)", lambda m: json.dumps(config[m.group(1)]), line
        )
    return line


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
        + _js_block(source, "const litPath = ") + "\n"
        + _js_block(source, "function insideConvex(") + "\n"
        + _js_block(source, "function pathBand(") + "\n"
        + _js_const(source, "BODY_RIM_SHADED")
        + _js_block(source, "function rimStyle(") + "\n"
        + _js_block(source, "function paint(") + "\n"
        + _js_block(source, "function rimStroke(") + "\n"
        + _js_block(source, "function rimStrokeCircle(") + "\n"
        # The character redesign: `seatedRig` and the human head paint their
        # volumes in authored colours (hair/skin/sweater/jeans/shoes) rather
        # than in `BODY_FILL`, so the palette is now as much a prerequisite for
        # executing them as `paint` is. Pulled from the page for the same
        # reason as every other entry here — a second copy of the palette in
        # this file would drift and stop proving anything about what ships.
        + _js_block(source, "const HUMAN = {") + "\n"
        # And increment 11 does the same for the other half of the cast:
        # `BODIES.bars` paints the cuts between its plates in `MACHINE.seam`.
        + _js_block(source, "const MACHINE = {") + ";\n"
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


def _eval_roles() -> dict:
    """`EVAL_LAYER_ROLES` as data — the one rule both eval gates apply (KI-064)."""
    literal = _js_const(_world_source(), "EVAL_LAYER_ROLES").split("=", 1)[1]
    return json.loads(re.sub(r"(\w+):", r'"\1":', literal.strip().rstrip(";")))


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

    # KI-064: both gates now apply ONE classification (`applyEvalLayers`,
    # graded by `test_both_eval_gates_classify_through_the_one_rule`), so
    # lockstep is the room and the plate holding the same role in it.
    gallery = _js_block(body, "function drawGallery(")
    assert "STYLES.forEach" in gallery, "not actually the gallery body"
    sheet = _js_block(body, "function drawAnimationSheet(")
    assert "sample.loopAnim" in sheet, "not actually the animation-sheet body"
    roles = _eval_roles()
    assert roles["room"] == roles["plate"] == "scenery", (
        "the room and the plate must share the VOID_MODE-gated scenery role"
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
    plate_json = manifest or _manifest()
    return (
        f"const PLATE = {json.dumps(plate_json)};\n"
        f"const PAINTED_CAST = {json.dumps(_painted_cast(plate_json))};\n"
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


def _painted_cast(manifest: dict) -> dict:
    """What `/world` injects for the cast: the server's own decision about
    which entries are people, not a copy of the rule (KI-076). Taken from
    `world.plate` so a change to `characters()` reaches this harness."""
    shipped = load_manifest()
    return dataclasses.replace(shipped, cast=manifest["cast"]).cast_payload()


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
        # A body with no painted furniture beside it has no chair to face and
        # nothing to be measured against, so both degrade rather than being
        # absent — which is why `character()` needs no branch for the standing
        # cast.
        "facing": None,
        "figureScale": 1,
    }
    assert emitted["trader"] == {
        "x": cast["trader"]["x"],
        "baseY": cast["trader"]["base_y"],
        "pose": "seated",
        # The two measurements the painted chair carries: which way its cushion
        # points, and how big a body sitting in it has to be drawn
        # (`tools/measure_chair.py::measure_seat_axes`).
        "facing": cast["trader"]["facing"],
        "figureScale": cast["trader"]["figure_scale"],
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
    assert moved == {
        "x": 111,
        "baseY": 222,
        "pose": "standing",
        "facing": None,
        "figureScale": 1,
    }


@needs_node
def test_a_settings_key_in_cast_is_not_a_character_on_the_page_either():
    """KI-076: Sprint 16 made `cast` a mixed block - `scale` and `rig_height`
    beside the people - and answered "which of these is a character" once, in
    `PlateManifest.characters()`. The page indexed the raw block, so
    `anchorFor("scale")` found the number 1.25, took it for an anchor and
    returned `{x: undefined}`. The guard has to be on the side that renders."""
    emitted = _run_node(
        _layout_driver(
            'console.log(JSON.stringify(anchorFor("scale")));', plate=True
        )
    )

    assert "scale" in _manifest()["cast"], "the manifest no longer poses this risk"
    assert emitted == {
        "x": pytest.approx(1920 * 0.5),
        "baseY": pytest.approx(960 * 0.66),
        "pose": "standing",
        "facing": None,
        "figureScale": 1,
    }, "a settings key resolved to an anchor instead of falling back"


def test_the_cast_payload_reaches_the_page_substituted():
    """An unreplaced `__CAST_JSON__` is a syntax error on the room's own
    boot path - the KI-045 shape, and the reason a placeholder gets its own
    check (a dev server that re-reads templates but not `api/main.py` renders
    exactly this)."""
    body = TestClient(app).get("/world").text
    assert "__CAST_JSON__" not in body, "placeholder left unreplaced"

    assert "const PAINTED_CAST = WORLD_CONFIG.painted_cast;" in body
    payload = _served_config(body)["painted_cast"]
    assert set(payload) == {"characters", "scale"}
    assert payload["scale"] == load_manifest().cast["scale"]
    assert set(payload["characters"]) == {"model", "trader"}, (
        "the page was handed something other than the people"
    )


def test_the_page_never_indexes_the_raw_cast_block():
    """The class, not the instance: the decision is made server-side and
    injected, so a future call site cannot reach a settings key by name.
    Mirrors `MONITOR_RULES`/`__MONITOR_JS__` for the monitors."""
    offenders = [
        f"{number}: {line.strip()[:90]}"
        for number, line in enumerate(_world_source().splitlines(), start=1)
        if "PLATE.cast" in re.sub(r"//.*$", "", line)
    ]
    assert not offenders, (
        "read the injected PAINTED_CAST instead - PLATE.cast carries settings "
        "as well as people:\n" + "\n".join(offenders)
    )


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


def test_the_banner_bar_is_no_taller_than_its_strip():
    """KI-071: `min-height` is set to the painted quiet strip (60px), but with
    the default `content-box` sizing it bounds the CONTENT, and the padding
    (14px top from the placement, 8.8px bottom from the CSS) is added on top —
    an ~83px translucent bar hanging ~23px onto the painted room, the "text
    over painted detail" failure the band exists to prevent. The bar's BORDER
    box has to be the strip. Comments stripped: the prose names the rule."""
    css = re.search(r"#banner\s*\{([^}]*)\}", _world_source()).group(1)
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert re.search(r"box-sizing\s*:\s*border-box", css), (
        "#banner's min-height bounds its content box, so the padding pushes "
        "the bar past the quiet strip it was measured to fill"
    )


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
        + _js_const(source, "GLOW_SPREAD")
        + "let glows = [];\n"
        + "let glowTexture = null;\n"
        + "const monitorGraphics = {};\n"
        + "let floorLine, wall, floorPlane, vignette;\n"
        + """
        class FakeGraphics {
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
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
            this.anchor = { set() {} }; this.position = { set() {} };
          }
        }
        const fakeCtx = {
          createRadialGradient() { return { addColorStop() {} }; },
          createLinearGradient() { return { addColorStop() {} }; },
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
        + _js_block(source, "function glowShapes(")
        + "\n"
        + _js_block(source, "function glowLightTexture(")
        + "\n"
        + _js_block(source, "function buildGlows(")
        + "\n"
        + _js_block(source, "function buildMonitorGraphics(")
        + "\n"
        # `drawPlate` calls this on its success path, so the driver has to
        # carry it. Leaving it out made `drawPlate` die on a ReferenceError
        # before `buildGlows` ever ran, which this test caught as a glow
        # count of 0 — the same "a driver that throws is an absent test"
        # shape as KI-085, found here by the suite instead of by a sprint.
        + _js_block(source, "function drawForeground(")
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


def _pillar_light_driver(
    source: str, mood: str, direction: list | None = None
) -> str:
    """The page's real `drawPillars` over the page's real `paint`/`litRect`
    and `moodFill`, on the shipped plate, with only PIXI faked. The fakes
    record every fill and stroke, so what is graded is what the draw handed
    the GPU — not a reading of its source."""
    return (
        _layout_driver("", plate=True)
        + _js_const(source, "MOOD_COLOR")
        + _js_const(source, "NEUTRAL_TINT")
        + _js_const(source, "BODY_TINT")
        + _js_const(source, "BODY_FILL")
        + _js_const(source, "BODY_RIM")
        + _js_const(source, "LIGHT")
        + (f"LIGHT.direction = {json.dumps(direction)};\n" if direction else "")
        + _js_block(source, "function snap(") + "\n"
        + _js_const(source, "CELL")
        + _shading_prelude(source)
        + _js_block(source, "function mulHex(") + "\n"
        + _js_block(source, "function moodTint(") + "\n"
        + _js_block(source, "function moodFill(") + "\n"
        + _js_block(source, "const litColumn = ") + "\n"
        + _js_const(source, "PILLAR_ALPHA")
        + "let pillarGlassFilter = null;\n"
        + _js_block(source, "function pillarGlass(") + "\n"
        + _js_block(source, "function drawPillars(") + "\n"
        + f"""
        const SYMBOLS = ["BTCUSDT", "ETHUSDT"];
        class FakeGraphics {{
          constructor() {{
            this.fills = []; this.strokes = []; this.alpha = 1;
            this.shapes = []; this.pending = [];
          }}
          rect(x, y, w, h) {{ this.pending.push([x, y, w, h]); return this; }}
          roundRect(x, y, w, h) {{ this.pending.push([x, y, w, h]); return this; }}
          circle() {{ return this; }}
          poly() {{ return this; }}
          ellipse() {{ return this; }}
          fill(f) {{
            this.fills.push(f);
            this.shapes.push(this.pending); this.pending = [];
            return this;
          }}
          stroke(s) {{ this.strokes.push(s); return this; }}
        }}
        class FakeContainer {{
          constructor() {{ this.children = []; this.alpha = 1; this.filters = null; }}
          addChild(...c) {{ this.children.push(...c); return c[0]; }}
        }}
        class FakeAlphaFilter {{ constructor(o) {{ this.alpha = o.alpha; }} }}
        const PIXI = {{ Graphics: FakeGraphics, Container: FakeContainer,
                        AlphaFilter: FakeAlphaFilter }};
        const layers = {{ props: new FakeContainer() }};
        layers.props.removeChildren = function () {{ this.children = []; }};
        const state = {{ symbols: {{
          BTCUSDT: {{ pressure: 0.6, mood: "{mood}" }},
          ETHUSDT: {{ pressure: -0.4, mood: "{mood}" }},
        }} }};
        drawPillars(state);
        const firstFilter = layers.props.children[0].filters;
        drawPillars(state);
        const glass = layers.props.children[0];
        const graphics = glass.children.filter((c) => c instanceof FakeGraphics);
        const colourOf = (f) => (typeof f === "number" ? f : f && f.color);
        const masks = new Set(graphics.map((g) => g.mask).filter(Boolean));
        const drawn = graphics.filter((g) => !masks.has(g));
        const base = moodFill("{mood}");
        const rungs = ["shade", "base", "lit"].map((r) => shade(base, rampLevel(r)));
        console.log(JSON.stringify({{
          propsChildren: layers.props.children.length,
          glassAlpha: glass.alpha,
          filters: (glass.filters || []).map((f) => f.alpha),
          sameFilter: firstFilter && glass.filters
            && firstFilter[0] === glass.filters[0],
          masked: graphics.filter((g) => g.mask).length,
          masksInTree: [...masks].every((m) => glass.children.includes(m)),
          colours: drawn.map((g) => g.fills.map(colourOf)),
          fillAlphas: drawn.flatMap((g) => g.fills)
            .map((f) => (f && typeof f === "object" && "alpha" in f ? f.alpha : 1)),
          strokes: drawn.reduce((n, g) => n + g.strokes.length, 0),
          rungs,
          raw: parseInt(MOOD_COLOR["{mood}"].slice(1), 16),
          neutral: moodFill("__no_such_mood__"),
          litSide: Math.sign(-KEY_AXIS[0]),
          litShare: 1 - BAND_INSET.lit,
          columns: graphics.filter((g) => g.mask).map((g) => ({{
            bands: g.shapes.map((s) => s[0]),
            cells: g.mask.shapes.flat(),
          }})),
        }}));
        """
    )


@needs_node
@pytest.mark.parametrize("mood", ["bullish", "bearish", "calm", "panicked"])
def test_the_pillars_are_lit_like_the_cast(mood):
    """KI-062: the pillars filled with `color(mood)` — the raw identity colour,
    one flat tone at alpha 0.85 — beside a cast lit in three bands and painted
    in the contrast-lifted body colour. Decided 2026-09-26: the furniture joins
    the light model.

    The rule, independent of where a tube stands or how full it is: every
    colour a pillar draws is one of the three rungs `paint()` gives the cast's
    own body colour (`moodFill`) for that mood, the column uses all three, and
    nothing is outlined. The moods are the four `world.state._mood()` hands a
    symbol, so a mood missing from `BODY_TINT` (which would quietly paint the
    pillar the neutral grey) fails here by name.
    """
    emitted = _run_node(_pillar_light_driver(_world_source(), mood))
    rungs = emitted["rungs"]
    assert emitted["neutral"] not in rungs, (
        f"{mood!r} has no BODY_TINT; the pillar would paint neutral grey"
    )
    assert emitted["colours"], "drawPillars drew nothing"
    for colours in emitted["colours"]:
        assert colours and set(colours) <= set(rungs), (
            f"pillar colours {colours} are not the lit rungs {rungs} of "
            f"moodFill({mood!r}) — raw identity colour is "
            f"{emitted['raw']}"
        )
    columns = [c for c in emitted["colours"] if len(c) == 3]
    assert len(columns) == 2, "each tube's column is one three-band volume"
    for colours in columns:
        assert len(set(colours)) == 3, "the column is flat, not lit"
    assert emitted["strokes"] == 0, "the liquid is outlined inside its glass"


@needs_node
@pytest.mark.parametrize("direction", [None, [0.55, 0.84]])
def test_a_pillar_is_lit_as_an_upright_cylinder(direction):
    """A cylinder's surface turns only across its width, so its light does
    too: every band spans the whole column top to bottom, stays inside it,
    and the lit band hugs the lamp's side. `litRect` put a 30%-by-30% lit
    block in the column's upper corner instead — a flat grey slab on the
    first frame (KI-062), which every colour-only assertion passed.

    Run twice, the second time with the lamp mirrored to the other side: the
    lit band must follow it, so a side baked into the drawing fails here.
    """
    emitted = _run_node(
        _pillar_light_driver(_world_source(), "bullish", direction)
    )
    side = emitted["litSide"]
    assert side == (1 if direction is None else -1), "control did not flip"
    assert len(emitted["columns"]) == 2
    for column in emitted["columns"]:
        cells = column["cells"]
        left = min(c[0] for c in cells)
        right = max(c[0] + c[2] for c in cells)
        top = min(c[1] for c in cells)
        bottom = max(c[1] + c[3] for c in cells)
        shade_band, base_band, lit_band = column["bands"]
        for x, y, w, h in column["bands"]:
            assert (y, y + h) == (top, bottom), (
                f"band y {y}..{y + h} does not span the column {top}..{bottom}"
            )
            assert left <= x and x + w <= right, "a band leaves its column"
        assert shade_band[2] > base_band[2] > lit_band[2], "bands do not narrow"
        # A cylinder's normal sweeps every horizontal direction, so the lit
        # share is BAND_INSET's alone — the lamp's lean must not narrow it.
        assert lit_band[2] == pytest.approx(
            (right - left) * emitted["litShare"], abs=1 / 64
        ), "the lit strip's width depends on how far the lamp leans"
        lit_edge = lit_band[0] + lit_band[2] if side > 0 else lit_band[0]
        column_edge = right if side > 0 else left
        assert lit_edge == pytest.approx(column_edge), (
            "the lit band is not on the lamp's side of the column"
        )


@needs_node
def test_the_pillar_translucency_is_one_fade_over_the_whole_group():
    """Three opaque bands at a per-fill 0.85 composite to ~0.997 where they
    overlap: the lit column would go opaque with a seam at each band edge. The
    0.85 is one AlphaFilter over a pillars-only container — the SAME filter
    across draws, since `drawPillars` runs every state update — and the cells
    that cut the column are masks inside that container, so they sway with
    it."""
    emitted = _run_node(_pillar_light_driver(_world_source(), "bullish"))
    assert emitted["propsChildren"] == 1, "pillars go into one container"
    assert emitted["filters"] == [0.85]
    assert emitted["glassAlpha"] == 1, "alpha on the container is per-primitive"
    assert emitted["sameFilter"] is True, "a new AlphaFilter per draw leaks"
    assert set(emitted["fillAlphas"]) == {1}, "a fill carries its own alpha"
    assert emitted["masked"] == 2, "each column is cut into cells by a mask"
    assert emitted["masksInTree"] is True, "a mask outside the tree won't sway"


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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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


@needs_node
def test_depth_order_partitions_accents_without_reordering_them():
    """`depthOrder` is the whole of the KI-084 fix, so it is tested as the
    pure function it is: it splits one accents array into the two groups
    `character()` composites either side of the body, and it must not
    disturb the order WITHIN a group. That second half is not decoration —
    `seatedRig` pushes the far leg before the near one precisely so the near
    one draws over it, and a partition that sorted or reversed would undo
    that silently while every z-order assertion about the body still passed.
    """
    source = _world_source()
    driver = (
        _js_block(source, "function depthOrder(")
        + """
        const accents = [
          { id: "legFar", depth: "far" }, { id: "legNear", depth: "far" },
          { id: "armL", depth: "near" }, { id: "untagged" },
          { id: "armR", depth: "near" },
        ];
        console.log(JSON.stringify({
          far: depthOrder(accents, "far").map((a) => a.id),
          near: depthOrder(accents, "near").map((a) => a.id),
        }));
        """
    )
    emitted = _run_node(driver)
    # An untagged accent is a FAR one: every body style that predates the
    # seated rig tags nothing, and they all used to draw behind the mass.
    # Defaulting the other way would move every wick on the candle in front
    # of it the moment this shipped.
    assert emitted == {
        "far": ["legFar", "legNear", "untagged"],
        "near": ["armL", "armR"],
    }


@needs_node
def test_the_near_limbs_composite_in_front_of_the_torso_and_under_the_labels():
    """KI-084, the wiring half. `character()` used to add every accent before
    the body (`container.addChild(...accents, body, head, ...)`), and the
    seated torso spans ±24 — which is why the seated arms have been
    completely invisible at rest for as long as the seated rig has existed.
    Nothing was ever wrong with the arm animations; there was simply nothing
    on screen to animate.

    This drives the page's REAL `addChild` line rather than re-typing the
    order, because the bug it guards against is precisely a correct
    `depthOrder` that nothing calls. The labels are asserted to stay last: a
    name tag a forearm can cover is the same class of defect as an arm the
    torso covers.
    """
    source = _world_source()
    # Two lines now, because the per-figure scale needs a node of its own: the
    # body composites onto `rig`, and `rig` composites into the container with
    # the labels after it. Both are driven, so "the labels stay last" is still
    # checked against the page rather than against this test's memory of it.
    rig_line = re.search(r"^\s*rig\.addChild\(.*\);$", source, re.M)
    outer_line = re.search(r"^\s*container\.addChild\(rig.*\);$", source, re.M)
    assert rig_line, "the page no longer composites the body on one addChild line"
    assert outer_line, "the page no longer composites the rig into the container"
    driver = (
        _js_block(source, "function depthOrder(")
        + """
        const body = { id: "body" }, head = { id: "head" };
        const nameTag = { id: "nameTag" }, stat = { id: "stat" };
        const moodTag = { id: "moodTag" };
        const accents = [
          { id: "legFar", depth: "far" }, { id: "legNear", depth: "far" },
          { id: "armL", depth: "near" }, { id: "armR", depth: "near" },
        ];
        const order = [], outer = [];
        const rig = {
          id: "rig",
          addChild(...kids) { order.push(...kids.map((k) => k.id)); },
        };
        const container = {
          addChild(...kids) { outer.push(...kids.map((k) => k.id)); },
        };
        """
        + rig_line.group(0).strip()
        + "\n"
        + outer_line.group(0).strip()
        + "\nconsole.log(JSON.stringify({ order, outer }));\n"
    )
    # Once with no badge (the trader), once with the MODEL's, which rides on
    # the body — so under the head and the near arms.
    for badge, order in (
        ("null", ["legFar", "legNear", "body", "head", "armL", "armR"]),
        ('{ id: "badge" }', ["legFar", "legNear", "body", "badge", "head", "armL", "armR"]),
    ):
        assert _run_node(f"const badge = {badge};\n" + driver) == {
            "order": order,
            "outer": ["rig", "nameTag", "stat", "moodTag"],
        }


@needs_node
def test_the_foreground_redraws_the_plate_masked_and_degrades_to_nothing():
    """The painted chair back has to land in FRONT of the trader, and the
    plate is one flat image — so `/world` draws the plate a SECOND time,
    masked to a polygon the manifest measures.

    Two claims, because the failure modes are opposite. With regions: a mask
    is built from the polygon and the second sprite is sized to
    `PLATE.canvas` exactly as `drawPlate` sizes the first, which is what
    makes the two land pixel-for-pixel on each other with no frame offset to
    drift. Without regions: NOTHING is added — an unmasked full-plate sprite
    over the cast would paint the entire room over the characters, which is
    the white-frame class of defect (KI-045/047), not a cosmetic one.
    """
    source = _world_source()
    block = _js_block(source, "function drawForeground(")
    harness = """
        class FakeGraphics {
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          constructor() { this.polys = []; }
          poly(p) { this.polys.push(p); return this; }
          fill() { return this; }
        }
        class FakeSprite { constructor(texture) { this.texture = texture; } }
        const PIXI = { Graphics: FakeGraphics, Sprite: FakeSprite };
        const added = [];
        const layers = { foreground: { addChild(...k) { added.push(...k); } } };
    """
    with_region = _run_node(
        "const PLATE = { canvas: [1920, 960], foreground: ["
        '{ id: "chair", poly: [[1, 2], [3, 4], [5, 6]] },'
        # A region with no usable polygon is skipped, not thrown on: `poly()`
        # on a bad list would throw inside the one function standing between
        # the cast and a blank frame.
        '{ id: "broken" }] };\n'
        + harness + block + """
        drawForeground({ id: "tex" });
        const [mask, sprite] = added;
        console.log(JSON.stringify({
          added: added.length,
          polys: mask.polys,
          maskIsTheGraphics: sprite.mask === mask,
          size: [sprite.width, sprite.height],
          reusesTexture: sprite.texture.id === "tex",
        }));
        """
    )
    assert with_region == {
        "added": 2,
        # Flattened, which is the shape PIXI v8's `poly()` takes.
        "polys": [[1, 2, 3, 4, 5, 6]],
        "maskIsTheGraphics": True,
        "size": [1920, 960],
        "reusesTexture": True,
    }
    empty = _run_node(
        "const PLATE = { canvas: [1920, 960], foreground: [] };\n"
        + harness + block + "drawForeground({});\n"
        "console.log(JSON.stringify({ added: added.length }));"
    )
    assert empty == {"added": 0}, (
        "a plate with no foreground must add NOTHING - an unmasked second "
        "plate sprite over layers.chars paints the room over the cast"
    )


def test_the_foreground_layer_sits_above_the_cast_and_below_the_labels():
    """Depth order is the entire point of the layer, and it is decided on one
    `app.stage.addChild` line. Above `chars` or the chair cannot occlude
    anyone; below the labels, or the chair covers a nameplate.
    """
    source = _world_source()
    line = re.search(r"app\.stage\.addChild\((.*?)\);", source, re.S)
    assert line, "the page no longer composites its layers on one addChild call"
    order = [name.strip() for name in line.group(1).replace("\n", "").split(",")]
    order = [o for o in order if o]
    assert order.index("layers.foreground") > order.index("layers.chars"), (
        "the painted foreground must be added AFTER layers.chars or it "
        "cannot stand in front of the cast"
    )
    assert order.index("layers.foreground") < order.index("layers.nameplates"), (
        "the painted foreground must be added BEFORE layers.nameplates or "
        "the chair back can cover a character's name"
    )


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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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
        + _js_const(source, "EVAL_LAYER_ROLES")
        + "\n"
        + _js_block(source, "function applyEvalLayers(")
        + "\n"
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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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
    cast_scale = emitted["castScale"]

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
                # The seated rig is only built from a painted plate, and
                # always under the keyline filter, whose grid is one unit —
                # so a seated offset is on the grid when it is a whole unit
                # (2026-09-25: the breath dropped from a CELL to one unit).
                # The standing table keeps the CELL grid, graded elsewhere.
                assert float(after[p]).is_integer(), (
                    f"{name}.{p}: {after[p]} is not a whole unit, off the "
                    "keyline grid the seated rig is drawn to"
                )
                assert abs(screen_delta) > 1e-6, f"{name}.{p} moved by ~0 screen px"
    asleep = emitted["results"]["asleep"]
    assert asleep["before"]["headY"] != asleep["after"]["headY"]
    assert 0.3 in emitted["blinkCalls"] or 0 in emitted["blinkCalls"], (
        "asleep never actually called setBlink - eyes never close"
    )


@needs_node
def test_the_neck_holds_its_length_through_a_breath():
    """The neck is painted on `body`; the head is a sibling node. So the
    visible neck — head underside to collar — is fixed only while
    `head.y - body.y` is. The seated breathe used to move `body.y` a whole
    CELL and leave the head where it was, so the neck grew and shrank by a
    CELL every breath (2026-09-25, the user's catch). `cheer` lifts `body.y`
    the same way and is held to the same rule.

    Graded as the rule, not an instance: at every phase of the cycle the
    head-to-body offset is exactly `headBaseY`, AND the body actually moved
    somewhere in the cycle (a breathe that stopped breathing would pass the
    first half on its own).
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + _js_const(source, "CELL")
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + _js_block(source, "const EASE = {")
        + ";\n"
        + _js_const(source, "HEAD_TRAVEL")
        + "\nfunction setBlink() {}\n"
        + _js_range(
            source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.turn"
        )
        + """
        const c = {
          style: "figure", headBaseY: -108,
          body: { rotation: 0, x: 0, y: 0 },
          head: { rotation: 0, x: 0, y: -108 },
        };
        c.armFar = { rotation: 0, restRotation: 0 };
        c.armNear = { rotation: 0, restRotation: 0 };
        const samples = {};
        for (const name of ["breathe", "cheer"]) {
          samples[name] = [];
          for (let i = 0; i < 64; i++) {
            SEATED_ANIMATIONS[name](c, i / 64, 1);
            samples[name].push({ bodyY: c.body.y, headY: c.head.y });
          }
        }
        console.log(JSON.stringify({ samples }));
        """
    )
    for name, samples in _run_node(driver)["samples"].items():
        assert len({s["bodyY"] for s in samples}) > 1, f"{name} no longer moves the body"
        stretched = [s for s in samples if s["headY"] - s["bodyY"] != -108]
        assert not stretched, (
            f"the head does not ride {name}, so the neck changes length: {stretched[:3]}"
        )


@needs_node
def test_a_seated_breath_is_one_pixel_and_only_up():
    """The breath that carried the head (above) still moved the whole bust a
    CELL — 8 screen px — up, back, and DOWN into the cushion: a block on a
    spring (2026-09-25, the user's "still feels unnatural"). A chest lifts on
    the inhale and settles to rest, never below it. Graded over the whole
    cycle: the body visits rest and one unit up, and nothing else."""
    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + _js_const(source, "CELL")
        + _js_block(source, "function snap(")
        + "\n"
        + _js_block(source, "const EASE = {")
        + ";\n"
        + _js_const(source, "HEAD_TRAVEL")
        + "\nfunction setBlink() {}\n"
        + _js_range(
            source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.turn"
        )
        + """
        const c = {
          style: "figure", headBaseY: -100,
          body: { rotation: 0, x: 0, y: 0 },
          head: { rotation: 0, x: 0, y: -100 },
        };
        const ys = [];
        for (let i = 0; i < 64; i++) {
          SEATED_ANIMATIONS.breathe(c, i / 64, 1);
          ys.push(c.body.y + 0);
        }
        console.log(JSON.stringify({ ys }));
        """
    )
    ys = set(_run_node(driver)["ys"])
    assert ys == {0, -1}, (
        f"a seated breath visits body.y {sorted(ys)}; it should rest at 0 and "
        "rise one unit (-1), never sink below rest or jump a CELL"
    )


@needs_node
def test_a_profile_head_stays_on_its_neck_while_it_looks():
    """The neck is painted on `body`, the head is a sibling, and `head`'s
    origin is mid-skull. So for the profile trader: the idle `shift` must
    carry the head with the torso, and a glance or a deliberate look must
    neither slide the head (`head.x`) nor spin it about the skull
    (`head.rotation`) — it nods about the neck, on `aim`, on top of the
    resting look at the charts. Graded as the rule: at every phase, head.x
    equals body.x and head.rotation is 0."""
    source = _world_source()
    driver = (
        "const PLATE = null;\n"
        + _js_const(source, "CELL")
        + _js_block(source, "function snap(")
        + "\nfunction setBlink() {}\n"
        + _js_block(source, "function setGaze(")
        + "\n"
        + _js_block(source, "const IDLE_ACTIONS = {")
        + """;
        const c = {
          container: { pose: "seated", x: 0 },
          profile: { side: -1 }, aimRest: 0.25,
          body: { x: 0, y: 0 }, head: { x: 0, y: -100, rotation: 0 },
          aim: { rotation: 0.25 }, eyeL: { x: 0 }, eyeR: { x: 0 },
        };
        const out = [];
        for (let i = 0; i <= 16; i++) {
          const u = Math.sin((i / 16) * Math.PI);
          for (const dir of [-1, 1]) {
            // Each graded on its own too: idleTick only runs the ACTIVE
            // actions, so a shift must not lean on a glance to re-seat the head.
            IDLE_ACTIONS.shift.run(c, u, dir);
            out.push({ what: "shift", bx: c.body.x, hx: c.head.x, hr: c.head.rotation, ar: c.aim.rotation });
            IDLE_ACTIONS.glance.run(c, u, dir);
            out.push({ what: "glance", bx: c.body.x, hx: c.head.x, hr: c.head.rotation, ar: c.aim.rotation });
            setGaze(c, dir * 4 * u, dir * 0.12 * u, dir * 3 * u);   // watchTick's look
            out.push({ what: "look", bx: c.body.x, hx: c.head.x, hr: c.head.rotation, ar: c.aim.rotation });
          }
        }
        console.log(JSON.stringify({ out }));
        """
    )
    out = _run_node(driver)["out"]
    assert any(o["bx"] != 0 for o in out), "the idle shift no longer moves the torso"
    assert any(abs(o["ar"] - 0.25) > 1e-6 for o in out), "a profile's look no longer nods"
    for o in out:
        assert o["hx"] == o["bx"], f"the head slid off the neck: {o}"
        assert o["hr"] == 0, f"the head spun about its skull, not its neck: {o}"


def test_the_head_rest_is_recorded_once():
    """`positionCharacters` runs on every `draw(state)`, i.e. on every SSE
    update, and by then an animation has usually moved `head.y`. Recording
    `headBaseY = head.y` there banks that frame's offset as the rest pose, so
    the head random-walks off the neck. It must be recorded only when unset.
    Comments stripped first: the prose explains the very assignment.
    """
    block = _js_block(_world_source(), "function positionCharacters(")
    code = "\n".join(line.split("//")[0] for line in block.splitlines())
    assigns = re.findall(r"^.*headBaseY\s*=(?!=).*$", code, re.M)
    assert assigns, "positionCharacters no longer records the head's rest"
    for line in assigns:
        assert re.search(r"if\s*\(\s*char\.headBaseY\s*==\s*null\s*\)", line), (
            f"headBaseY is re-recorded unconditionally on every draw: {line.strip()}"
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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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
def test_the_seated_figure_is_drawn_to_the_chairs_own_proportion():
    """How big the trader is, graded against the furniture he uses.

    This test used to assert the opposite of what it now asserts, and the
    reversal is the finding. It pinned the head *under* the painted backrest
    — and a figure short enough to hide behind an office chair's back is a
    doll, which is exactly what the user saw and said ("his size is relatively
    small comparing to the chair"). The old rule was satisfied by the defect.

    A ratio, so it needs no px/cm and no projection: crown-above-hips against
    backrest-above-seat, both read off the art (`measure_plate_chair`) rather
    than typed here as the literals 462/682 this test used to carry. A person
    in a task chair clears its back by about half again — call it 1.25 to 1.95
    for hand-painted furniture — and the bracket fails from BOTH ends, which a
    'must be under the backrest' rule could never do.

    At the shipped `figure_scale` it reads ~1.7; at the 1.0 that shipped
    through increment 4 it reads 1.08, and that is what this would have caught.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          // `pivot`/`rotation` are real PIXI.Graphics fields and the seated
          // legs are posed with both. A double that omits them does not fail
          // loudly, it throws from inside the code under test — so it is the
          // double that gets fixed, not the page.
          constructor() { this.pivot = { x: 0, y: 0 }; this.rotation = 0; }
          roundRect() { return this; }
          circle() { return this; } poly() { return this; }
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
        + _seated_rig_block(source)
        + "\n"
        + """
        const fakeGfx = { roundRect() { return this; }, circle() { return this; }, poly() { return this; },
                           fill() { return this; }, stroke() { return this; } };
        const accents = [];
        const headY = seatedRig(fakeGfx, accents);
        console.log(JSON.stringify({
          headY, castScale: CAST_SCALE,
          // An arm is an accent that is not a leg bone. It used to be read as
          // `depth === "near"`, and that stopped being true the moment the
          // figure turned: `seatedRig` derives each arm's depth from the yaw
          // now, so at this chair's facing one arm is near and one is far.
          armCount: accents.filter((a) => !a.limb).length,
          armsAreFirst: accents.slice(0, 2).every((a) => !a.limb),
          armDepths: accents.filter((a) => !a.limb).map((a) => a.depth),
        }));
        """
    )
    from tools.measure_chair import measure_plate_chair

    emitted = _run_node(driver)
    trader = _manifest()["cast"]["trader"]
    chair = measure_plate_chair()
    # Crown above the hips: the head sits at `headY` and its hair circle
    # (`litCircle(0, -4, 22)` in `character()`) reaches 26 further up.
    HUMAN_CROWN_ABOVE_HEAD_Y = 26
    figure = (
        (abs(emitted["headY"]) + HUMAN_CROWN_ABOVE_HEAD_Y)
        * emitted["castScale"]
        * trader["figure_scale"]
    )
    # ...against the chair's own back, measured behind HIM rather than at the
    # chair's highest painted pixel: the far side of the backrest sits higher
    # on screen only because it is further away.
    backrest = trader["sit_anchor"]["y"] - chair["crown_behind_trader_y"]
    ratio = figure / backrest

    assert 1.25 <= ratio <= 1.95, (
        f"the seated figure stands {figure:.0f}px above its own hips against "
        f"a backrest {backrest}px above the seat (ratio {ratio:.2f}): a person "
        "in an office chair clears its back by about half again, so this is "
        + ("a doll in a grown-up's chair" if ratio < 1.25 else "a giant in a toy one")
    )
    assert emitted["armCount"] == 2, "seatedRig must build exactly two arms"
    # One arm in front of the torso and one behind it, because the figure is
    # turned: hand-tagging both `near` was right only while he faced the
    # camera, and would now draw his far forearm across his own back.
    assert sorted(emitted["armDepths"]) == ["far", "near"], (
        "both arms composite on the same side of a turned torso: "
        f"{emitted['armDepths']}"
    )
    # ...and they are still `accents[0]`/`accents[1]`, which `character()`
    # aliases as `armNear`/`armFar` BY INDEX. The legs are pushed after the
    # arms for exactly this reason, and nothing else in the suite would notice
    # if a future edit pushed a leg bone first and silently re-aliased an arm
    # animation onto a shin.
    assert emitted["armsAreFirst"], (
        "the two arms must stay accents[0] and accents[1] - character() "
        "aliases armNear/armFar by index"
    )


# The trader's head, as `character()` paints it on the `facesAway` branch this
# chair's yaw selects: since increment 16 a PROFILE turned to the charts,
# built by `profileHead` where node can run it. The chin every neck
# measurement starts from is read off that builder's own return value rather
# than pinned as a literal here, so a redrawn head moves it with the drawing.



def _seated_rig_block(source: str) -> str:
    """`seatedRig` with the hand outline it draws from (increment 18)."""
    return (
        _js_const(source, "HAND_OUTLINE") + "\n"
        + _js_block(source, "function outlineDepthAt(") + "\n"
        + _js_block(source, "function seatedRig(")
    )

def _profile_head_run(side: int = -1, manifest: dict | None = None) -> dict:
    """`profileHead` built for one `side`, plus the gaze helpers run on the
    real manifest's chart screens.

    `paint` and the feature Graphics are recorders: every volume comes back as
    `{kind, geom, fill, opts}` in head-local units BEFORE banding, and every
    flat feature as the rect or polygon it fills. `shade` is the page's own,
    so a feature colour compares against the palette it is shaded from.
    """
    source = _world_source()
    plate = manifest or _manifest()
    return _run_node(
        f"const PLATE = {json.dumps(plate)};\n"
        + f"const PAINTED_CAST = {json.dumps(_painted_cast(plate))};\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + _js_block(source, "const HUMAN = {") + ";\n"
        + _js_block(source, "function mixHex(") + "\n"
        + _js_block(source, "function shade(") + "\n"
        + _js_block(source, "function rampLevel(") + "\n"
        + _js_block(source, "function snap(") + "\n"
        + _js_const(source, "CELL")
        + _js_const(source, "CAST_SCALE")
        + _js_block(source, "const PROFILE_HEAD = {") + ";\n"
        + _js_block(source, "function mirrorRect(") + "\n"
        + _js_block(source, "function profileHead(") + "\n"
        + _js_block(source, "function chartTarget(") + "\n"
        + _js_block(source, "function gazeSide(") + "\n"
        + _js_const(source, "GAZE_MAX")
        + _js_const(source, "GAZE_HEAD_SHARE")
        + _js_block(source, "function gazePitch(") + "\n"
        + _js_block(source, "function aimAtCharts(") + "\n"
        + f"const SIDE = {int(side)};\n"
        + """
        const volumes = [];
        const litRect = (x, y, w, h, r) => ({ kind: "rect", geom: [x, y, w, h, r] });
        const litCircle = (cx, cy, rad) => ({ kind: "circle", geom: [cx, cy, rad] });
        const litPath = (pts) => ({ kind: "path", geom: pts.flat() });
        function paint(g, volume, fill, rim, opts) {
          volumes.push({ kind: volume.kind, geom: volume.geom, fill, opts: opts || {} });
        }
        const features = [];
        let pending = null;
        const recorder = {
          rect(x, y, w, h) { pending = { kind: "rect", geom: [x, y, w, h] }; return this; },
          poly(pts) { pending = { kind: "poly", geom: pts }; return this; },
          ellipse(cx, cy, rx, ry) { pending = { kind: "ellipse", geom: [cx, cy, rx, ry] }; return this; },
          fill(colour) { features.push({ ...pending, fill: colour }); pending = null; return this; },
        };
        const head = profileHead({}, recorder, SIDE);
        const target = chartTarget(PLATE.screens);
        const cast = PLATE.cast.trader;
        const side = gazeSide(target, cast.x, cast.facing);
        console.log(JSON.stringify({
          head, volumes, features, target, side, HUMAN, P: PROFILE_HEAD,
          CAST_SCALE, GAZE_MAX, GAZE_HEAD_SHARE,
          noPlateSide: gazeSide(null, cast.x, cast.facing),
          noPlatePitch: gazePitch({ x: 0, y: 0 }, null, -1),
        }));
        """
    )


def _aim_run(head_y: float, side: int) -> dict:
    """`aimAtCharts` run on a fake trader whose head is `profileHead`'s, at
    the manifest's own hip anchor — the rotation the page parks on `aim`."""
    source = _world_source()
    plate = _manifest()
    trader = plate["cast"]["trader"]
    return _run_node(
        f"const PLATE = {json.dumps(plate)};\n"
        + f"const PAINTED_CAST = {json.dumps(_painted_cast(plate))};\n"
        + _js_const(source, "CAST_SCALE")
        + _js_const(source, "GAZE_MAX")
        + _js_const(source, "GAZE_HEAD_SHARE")
        + _js_block(source, "function chartTarget(") + "\n"
        + _js_block(source, "function gazePitch(") + "\n"
        + _js_block(source, "function aimAtCharts(") + "\n"
        + f"const EYE = {json.dumps(_profile_head_run(side)['head'])};\n"
        + f"""
        const char = {{ profile: {{ ...EYE, side: {int(side)} }}, figureScale: {trader["figure_scale"]},
                        headBaseY: {head_y}, aim: {{ rotation: 0 }} }};
        const anchor = {{ x: {trader["sit_anchor"]["x"]}, baseY: {trader["sit_anchor"]["y"]} }};
        aimAtCharts(char, anchor);
        console.log(JSON.stringify({{ rotation: char.aim.rotation }}));
        """
    )


def _extent(volume: dict) -> tuple[float, float, float, float]:
    """x0, y0, x1, y1 of a recorded head volume or feature, head-local."""
    g = volume["geom"]
    if volume["kind"] == "circle":
        cx, cy, r = g
        return cx - r, cy - r, cx + r, cy + r
    if volume["kind"] in ("poly", "path"):
        xs, ys = g[0::2], g[1::2]
        return min(xs), min(ys), max(xs), max(ys)
    if volume["kind"] == "ellipse":
        cx, cy, rx, ry = g
        return cx - rx, cy - ry, cx + rx, cy + ry
    if volume["kind"] != "rect":
        raise ValueError(f"no extent for a {volume['kind']!r}")
    x, y, w, h = g[:4]
    return x, y, x + w, y + h


def _inside_convex(pts, px, py) -> bool:
    """Is (px, py) inside or on the convex polygon `pts`, either winding?
    The test-side twin of the page's `insideConvex`, written separately so
    the two can disagree."""
    sign = 0
    for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
        cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        if abs(cross) < 1e-9:
            continue
        if sign and (cross > 0) != (sign > 0):
            return False
        sign = cross
    return True


def _convex_span(pts, value, axis=1):
    """Where the line `coord[axis] == value` crosses a convex polygon: the
    (min, max) of the OTHER coordinate, or None if it misses.

    `axis=1` cuts across a bone at a height in its own frame (its width
    there); `axis=0` cuts a room outline at an x (its top and bottom).
    """
    other = 1 - axis
    hits = []
    for a, b in zip(pts, pts[1:] + pts[:1]):
        lo, hi = sorted((a[axis], b[axis]))
        if not lo <= value <= hi:
            continue
        if a[axis] == b[axis]:
            hits += [a[other], b[other]]
        else:
            t = (value - a[axis]) / (b[axis] - a[axis])
            hits.append(a[other] + t * (b[other] - a[other]))
    return (min(hits), max(hits)) if hits else None


def _mirrored(rect: list, side: int) -> list:
    """A `PROFILE_HEAD` rect as `mirrorRect` places it for `side`."""
    x, y, w, h = rect[:4]
    return [-(x + w) if side < 0 else x, y, w, h]


def _ray_hits_quad(origin, angle_up: float, side: int, quad) -> bool:
    """Does a ray from `origin`, pointing `side` in x and `angle_up` radians
    above level (screen y grows DOWN), cross the convex `quad`?"""
    ox, oy = origin
    dx, dy = side * math.cos(angle_up), -math.sin(angle_up)
    n = len(quad)
    for i in range(n):
        (ax, ay), (bx, by) = quad[i], quad[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        denom = dx * ey - dy * ex
        if abs(denom) < 1e-12:
            continue
        t = ((ax - ox) * ey - (ay - oy) * ex) / denom
        u = ((ax - ox) * dy - (ay - oy) * dx) / denom
        if t > 0 and 0 <= u <= 1:
            return True
    return False


@needs_node
def test_the_trader_turns_to_the_charts_side():
    """The profile faces the side the painted CHARTS are on, read off the
    manifest's `role: chart` screens rather than typed as "left" — and the
    nose is what says so: its tip stands proud of the face on that side.
    With no plate, the chair's own facing decides, and it is never 0.
    """
    emitted = _profile_head_run()
    trader = _manifest()["cast"]["trader"]
    assert emitted["target"], "no chart screen in the manifest to look at"
    expected = 1 if emitted["target"]["x"] > trader["x"] else -1
    assert emitted["side"] == expected
    assert emitted["noPlateSide"] == (1 if trader["facing"][0] > 0 else -1)
    assert emitted["noPlatePitch"] == 0
    for side in (-1, 1):
        run = _profile_head_run(side)
        nose = [f for f in run["features"] if f["kind"] == "poly"]
        assert len(nose) == 1, "the profile has no nose wedge"
        skin = [v for v in run["volumes"] if v["fill"] == run["HUMAN"]["skin"]]
        face_front = max(side * e for v in skin for e in _extent(v)[0::2])
        tip = max(side * x for x in nose[0]["geom"][0::2])
        assert tip > face_front, (
            f"facing {side:+d}, the nose tip reaches {tip} and the face "
            f"{face_front}: the nose is not the front of this head"
        )


@needs_node
def test_the_profile_eye_sits_in_the_front_of_the_head_above_the_nose_tip():
    """An eye behind the ear or below the nose is a face drawn on the back of
    a head. Graded for both sides from the builder's own output."""
    for side in (-1, 1):
        run = _profile_head_run(side)
        ex, ey = run["head"]["eye"]
        hair = [v for v in run["volumes"] if v["fill"] == run["HUMAN"]["hair"]]
        x0 = min(_extent(v)[0] for v in hair + run["volumes"])
        x1 = max(_extent(v)[2] for v in run["volumes"])
        centre = (x0 + x1) / 2
        assert side * (ex - centre) > 0, f"facing {side:+d}, the eye is in the back half"
        nose = next(f for f in run["features"] if f["kind"] == "poly")
        pts = list(zip(nose["geom"][0::2], nose["geom"][1::2]))
        tip = max(pts, key=lambda p: side * p[0])
        assert ey < tip[1], f"facing {side:+d}, the eye ({ey}) is not above the nose tip ({tip[1]})"


@needs_node
def test_the_profile_is_one_head_mirrored():
    """Facing left is facing right mirrored — every volume and every feature.
    A per-side hand edit is how the two drift apart."""
    left, right = _profile_head_run(-1), _profile_head_run(1)

    def mirrored(item):
        x0, y0, x1, y1 = _extent(item)
        return (item["kind"], item["fill"], -x1, y0, -x0, y1)

    def plain(item):
        return (item["kind"], item["fill"], *_extent(item))

    for key in ("volumes", "features"):
        assert [plain(v) for v in left[key]] == [mirrored(v) for v in right[key]], key
    assert left["head"]["eye"][0] == -right["head"]["eye"][0]


@needs_node
def test_no_hair_shows_below_the_ear():
    """The profile's hair stops at the ear: below the ear's bottom, whatever
    is painted last at a point is never hair.

    The skull is a circle, and a circle does not end on a line. With the nape
    laid UNDER it, the circle's lower arc hung a tuft of hair below the ear —
    down the jaw and the back of the neck, which reads as a sideburn gone
    wrong (the user's catch). Graded by painting order, both sides: the last
    volume covering each point is the one the viewer sees. "Under the ear" is
    the ear's own column and everything in front of it; the skull may still
    curve down BEHIND the ear, which is the back of a head, not a tuft.
    """
    for side in (-1, 1):
        run = _profile_head_run(side)
        hair = run["HUMAN"]["hair"]
        ear_bottom = run["P"]["ear"][1] + run["P"]["ear"][3]
        ear = _mirrored(run["P"]["ear"], side)
        ear_back = ear[0] if side > 0 else ear[0] + ear[2]

        def covers(v, px, py):
            g = v["geom"]
            if v["kind"] == "circle":
                return math.hypot(px - g[0], py - g[1]) <= g[2]
            if v["kind"] == "path":
                return _inside_convex(list(zip(g[0::2], g[1::2])), px, py)
            assert v["kind"] == "rect", v["kind"]
            return _inside_round_rect(px, py, g[:4], g[4] if len(g) > 4 else 0, slack=0)

        x0 = min(_extent(v)[0] for v in run["volumes"])
        x1 = max(_extent(v)[2] for v in run["volumes"])
        y1 = max(_extent(v)[3] for v in run["volumes"])
        tufts = []
        for i in range(int((x1 - x0) * 2) + 1):
            px = x0 + i / 2
            if side * (px - ear_back) < 0:
                continue   # behind the ear
            for j in range(int((y1 - ear_bottom) * 2) + 1):
                py = ear_bottom + j / 2
                top = next((v for v in reversed(run["volumes"]) if covers(v, px, py)), None)
                if top is not None and top["fill"] == hair:
                    tufts.append((px, py))
        assert not tufts, (
            f"facing {side:+d}, hair shows below the ear (bottom y {ear_bottom}) "
            f"at {len(tufts)} points, e.g. {tufts[:4]}"
        )


@needs_node
def test_the_hair_is_matte_whichever_way_the_head_faces():
    """Every head volume painted in `HUMAN.hair` carries `matte`; skin never.

    The lamp's band on a circle is a concentric disc; on a crown of dark hair
    a tan disc reads as a bald spot (increment 15). The profile is graded by
    RUNNING `profileHead`, both sides; the camera-facing head is still two
    inline calls in `character()`, graded off the source with comments
    stripped.
    """
    for side in (-1, 1):
        run = _profile_head_run(side)
        palette = run["HUMAN"]
        for v in run["volumes"]:
            if v["fill"] == palette["hair"]:
                assert v["opts"].get("matte"), f"a hair volume {v['geom']} is lit"
            elif v["fill"] == palette["skin"]:
                assert not v["opts"].get("matte"), f"a skin volume {v['geom']} is matte"
        assert any(v["fill"] == palette["hair"] for v in run["volumes"])
    body = _without_comments(_js_block(_world_source(), "function character("))
    facing = re.findall(
        r"paint\(skull, litCircle\([^)]*\), HUMAN\.(\w+),[^;]*?matte: (true|false) \}\);",
        body,
    )
    assert sorted(facing) == [("hair", "true"), ("skin", "false")], facing


@needs_node
def test_the_trader_looks_at_a_chart():
    """The line of sight leaves the eye and lands on a painted chart screen.

    The eye's screen position is the manifest's hip anchor plus the head's
    own eye through the layer and figure scale — the same composition
    `aimAtCharts` does — and the look is `gazePitch` above level toward the
    profile's side. It must CROSS a `role: chart` quad. It must also be a
    look the page actually carries: the neck (`aim.rotation`) takes
    `GAZE_HEAD_SHARE` of it and the pupil sits in the eye white's front-upper
    corner for the rest.

    The level ray from the same eye misses every chart — asserted, so this
    test is known to be able to fail (a head that forgot to look up would).
    """
    plate = _manifest()
    trader = plate["cast"]["trader"]
    head_y = _seated_rig_run()["headY"]
    run = _profile_head_run()
    side = run["side"]
    k = run["CAST_SCALE"] * trader["figure_scale"]
    ex, ey = run["head"]["eye"]
    eye = (trader["sit_anchor"]["x"] + ex * k, trader["sit_anchor"]["y"] + (head_y + ey) * k)
    target = run["target"]
    pitch = math.atan2(eye[1] - target["y"], side * (target["x"] - eye[0]))
    pitch = max(0.0, min(run["GAZE_MAX"], pitch))
    charts = [s["quad"] for s in plate["screens"] if s.get("role") == "chart"]

    assert any(_ray_hits_quad(eye, pitch, side, q) for q in charts), (
        f"from the eye at ({eye[0]:.0f}, {eye[1]:.0f}) a look {math.degrees(pitch):.1f} "
        "degrees up misses every chart screen"
    )
    assert not any(_ray_hits_quad(eye, 0.0, side, q) for q in charts), (
        "a LEVEL look already hits a chart - this test cannot tell a head "
        "that looks up from one that does not"
    )
    rotation = _aim_run(head_y, side)["rotation"]
    assert rotation == pytest.approx(-side * pitch * run["GAZE_HEAD_SHARE"], abs=1e-6)
    assert 0 < run["GAZE_HEAD_SHARE"] < 1, "the eyes do none of the looking"
    rects = [f for f in run["features"] if f["kind"] == "rect"]
    pupil = next(f for f in rects if _mirrored(run["P"]["pupil"], side) == f["geom"])
    # The white is an oval inscribed in its box (increment 18), so it is
    # found by its extent rather than by its rect.
    ex0, ey0, ew, eh = _mirrored(run["P"]["eye"], side)
    eye_white = next(
        f for f in run["features"]
        if f["kind"] == "ellipse" and _extent(f) == (ex0, ey0, ex0 + ew, ey0 + eh)
    )
    assert pupil["fill"] != eye_white["fill"], "the pupil is the colour of the white"
    wx0, wy0, wx1, wy1 = _extent(eye_white)
    px0, py0, px1, py1 = _extent(pupil)
    assert side * ((px0 + px1) - (wx0 + wx1)) > 0, "the pupil is not at the front of the eye"
    assert (py0 + py1) < (wy0 + wy1), "the pupil is not raised toward the charts"


def _seated_rig_run() -> dict:
    """`seatedRig` run in node against recording stubs: `{headY, drawn, HUMAN}`."""
    source = _world_source()
    return _run_node(
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + _js_block(source, "const HUMAN = {")
        + ";\n"
        # The far leg mixes its jeans toward ambient; that is the only piece of
        # the shading chain this driver needs, because the stubs below replace
        # `litRect`/`litCircle`/`paint` wholesale.
        + _js_block(source, "function mixHex(")
        + "\n"
        + """
        class FakeGraphics {
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          constructor() { this.pivot = { x: 0, y: 0 }; this.rotation = 0; }
          roundRect() { return this; } circle() { return this; } poly() { return this; }
          fill() { return this; } stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        // The authored volumes, before banding and snapping smear them. A
        // recording `litRect` reports the rect `seatedRig` asked for, which is
        // what this test is about; the real one is graded elsewhere.
        const drawn = [];
        const litRect = (x, y, w, h, r) => ({
          rect: [x, y, w, h], radius: r || 0, band: (g) => g, rim: (g) => g });
        const litCircle = (cx, cy, rad) => ({
          rect: [cx - rad, cy - rad, 2 * rad, 2 * rad], radius: rad,
          band: (g) => g, rim: (g) => g });
        const litPath = (pts, ox = 0, oy = 0) => {
          const [mnx, mny, mxx, mxy] = [Math.min(...pts.map((q) => q[0])), Math.min(...pts.map((q) => q[1])),
                                         Math.max(...pts.map((q) => q[0])), Math.max(...pts.map((q) => q[1]))];
          return { rect: [ox + mnx, oy + mny, mxx - mnx, mxy - mny], radius: 0,
                   pts: pts.map(([x, y]) => [ox + x, oy + y]),
                   band: (g) => g, rim: (g) => g };
        };
        function paint(g, volume, fill, rim, opts) {
          drawn.push({ rect: volume.rect, fill, opts: opts || {},
                       host: g, radius: volume.radius, pts: volume.pts || null });
        }
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + "\n"
        + _seated_rig_block(source)
        + "\n"
        + """
        const accents = [];
        const BODY = new FakeGraphics();
        const headY = seatedRig(BODY, accents);
        // Each leg bone as the joint it starts at, the joint it ends at and
        // its width — `boneGfx` sets these after painting, in rig space.
        const legs = accents.filter((a) => a.limb === "leg").map((a) => ({
          start: [a.x, a.y],
          end: [a.x - Math.sin(a.restRotation) * a.boneLength,
                a.y + Math.cos(a.restRotation) * a.boneLength],
          width: a.boneWidth,
          behindSeat: !!a.behindSeat,
        }));
        // Where each volume went: the torso (`body`, which the seated
        // animations move), or an accent — by the part it plays.
        for (const d of drawn) {
          const g = d.host;
          d.host = g === BODY ? "body"
            : accents.includes(g) ? (g.limb || g.gestureMode || "accent") : "child";
        }
        // The palette rides along rather than being restated in python: a
        // recoloured sweater must not silently empty the filter below.
        console.log(JSON.stringify({ headY, drawn, HUMAN, legs, CELL }));
        """
    )



@needs_node
def test_only_the_far_shin_and_shoe_go_behind_the_seat():
    """The far leg below the knee is tagged to be hidden by the cushion;
    thighs and the near leg are not.

    Increment 22. The camera is behind the chair, so the cushion's front edge
    is its upper-left edge on screen and his far knee lands on it: the far
    shin hangs down BEHIND the seat and was painted over it ("I don't like
    that his right leg goes on top of chair"). A thigh lies ON the cushion
    and must stay in front of it. The near knee sits at the cushion's corner,
    beside it: masked, its shin lost a notch to the corner's tip.

    Structural, not a list of names: the far leg is the one drawn FIRST (the
    page orders legs by depth), and a bone is below its knee when it starts
    where another bone of the same leg ends.
    """
    legs = _seated_rig_run()["legs"]

    def starts_at_end_of(leg, other):
        return math.dist(leg["start"], other["end"]) < 1e-6

    thighs = [leg for leg in legs if not any(starts_at_end_of(leg, o) for o in legs)]
    assert len(thighs) == 2, "two legs, each one thigh"
    chain = [thighs[0]]
    while nxt := [leg for leg in legs if starts_at_end_of(leg, chain[-1])]:
        chain.append(nxt[0])
    far_below_knee = chain[1:]
    assert len(far_below_knee) == 2, "a shin and a shoe"

    assert [leg["behindSeat"] for leg in legs] == [
        any(leg is b for b in far_below_knee) for leg in legs
    ], (
        "only the FAR leg's shin and shoe go behind the seat - never a thigh, "
        "which sits on it, and never the near leg, which is beside its corner"
    )
    # And the tag is acted on - for the ROOM's trader, in `boot()`, and not in
    # `character()`, which also builds the animation sheet's seated samples in
    # grid cells nowhere near the chair. Comments stripped, so the prose
    # above the call cannot pass for it.
    source = _world_source()
    boot = _without_comments(_js_block(source, "async function boot("))
    assert 'occludeBehindSeat("trader", trader.accents);' in boot
    assert "occludeBehindSeat(" not in _without_comments(
        _js_block(source, "function character(")
    )


@needs_node
def test_the_seated_figure_has_a_neck():
    """There is skin between his collar and his jaw, and the trace says how much.

    A head resting straight on a collar is the loudest "this is not a person"
    signal a seated figure can send, and it was invisible to every check this
    suite had: total height, seat fit and the chair-proportion bracket all
    pass with the head sunk to the shoulders, because sinking it barely moves
    the figure's overall span.

    So this measures the one span that does move: the gap between the chin and
    the topmost SWEATER volume. Sweater, not torso — the collar is what would
    actually cover the neck, and a torso-top measurement would happily report
    a neck that a high collar swallows whole.

    The target is not typed here. `measure_chair.head_unit` derives it from the
    trace as `neck`->`head` minus the head RADIUS: the trace puts `head` at the
    head's centre, so that subtraction is what separates a bare neck from a
    neck-plus-half-a-head. It comes out ~0.17 heads, about 7 rig units.

    The bracket fails from both ends, because "more neck" is not the goal and a
    giraffe is as wrong as a mole. It reads about -13 on the figure that
    shipped through increment 5 — the collar stood 13 units ABOVE the chin.
    """
    from tools.measure_chair import head_unit, measure_plate_chair

    emitted = _seated_rig_run()
    drawn = emitted["drawn"]
    palette = emitted["HUMAN"]

    sweater = [d for d in drawn if d["fill"] == palette["sweater"]]
    assert sweater, "seatedRig paints no sweater volume at all"
    sweater_top = min(d["rect"][1] for d in sweater)
    chin = emitted["headY"] + _profile_head_run()["head"]["chin"]
    # Positive means skin shows: the collar stops BELOW the jaw.
    neck = sweater_top - chin

    target = head_unit(measure_plate_chair())["parts"][
        "visible neck (shoulder->chin)"
    ]["should_be_u"]

    assert 0.5 * target <= neck <= 1.8 * target, (
        f"the chin sits at {chin} and the collar tops out at {sweater_top}, "
        f"leaving {neck:.0f} units of neck where the reference trace asks for "
        f"{target:.0f} ({target / 44:.2f} heads): "
        + (
            "his head is sunk into his shoulders"
            if neck < 0.5 * target
            else "that is a giraffe, not a trader"
        )
    )

    # ...and something has to FILL that gap, or the neck is a hole with the
    # room showing through it. A skin volume, narrower than the collar, and
    # reaching from under the jaw down into the sweater.
    skin = [d for d in drawn if d["fill"] == palette["skin"]]
    collar_w = max(
        d["rect"][2] for d in sweater if d["rect"][1] <= sweater_top + 4
    )
    bridging = [
        d
        for d in skin
        if d["rect"][1] <= chin
        and d["rect"][1] + d["rect"][3] >= sweater_top
        and d["rect"][2] < collar_w
    ]
    assert bridging, (
        "nothing skin-coloured bridges the collar and the jaw: the neck gap "
        f"({chin} to {sweater_top}) would show the room through it. Skin "
        f"volumes drawn: {[d['rect'] for d in skin]}"
    )


def _inside_polygon(px, py, pts) -> bool:
    """Even-odd point-in-polygon; a point on an edge counts as inside."""
    inside = False
    for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
        if min(ay, by) <= py <= max(ay, by) and ay != by:
            x = ax + (py - ay) * (bx - ax) / (by - ay)
            if abs(x - px) < 1e-9:
                return True
            if x > px and py != max(ay, by):
                inside = not inside
    return inside


def test_the_neck_joins_the_head_across_its_whole_underside():
    """No row between the jaw and the collar shows the room beside the neck.

    The neck used to be a 12-wide pill with radius 6. It bridged jaw and
    collar, so the test above passed, but directly under the jaw a pill is a
    dome: the room showed on both sides of it, and the silhouette keyline
    wrapped the head's whole underside. On air the head read as a lollipop
    balanced on a knob (the user's catch, 2026-09-25).

    So this grades rows, not a bridge. At every height from the chin line
    down to the sweater, skin must cover the nape's BACK edge (where the back
    of the neck continues the back of the head) and the nod PIVOT under the
    jaw (the head rotates about it, so the throat stays joined at any gaze).
    """
    emitted = _seated_rig_run()
    drawn = emitted["drawn"]
    palette = emitted["HUMAN"]
    head = _profile_head_run()["head"]
    sweater_top = min(d["rect"][1] for d in drawn if d["fill"] == palette["sweater"])
    chin = emitted["headY"] + head["chin"]
    skin = [d for d in drawn if d["fill"] == palette["skin"] and d["host"] == "body"]

    def covered(x, y):
        for d in skin:
            if d["pts"]:
                if _inside_polygon(x, y, [tuple(p) for p in d["pts"]]):
                    return True
            elif _inside_round_rect(x, y, d["rect"], d["radius"], slack=0):
                return True
        return False

    # The source's own numbers: the nape rect's back edge and the pivot's
    # distance in front of the head's origin. The seated trader faces -x.
    nape_x, _, nape_w, _, _ = _run_node(
        _js_block(_world_source(), "const PROFILE_HEAD = {")
        + ";\nconsole.log(JSON.stringify(PROFILE_HEAD.nape));"
    )
    back = nape_x + nape_w - 0.5
    pivot = -abs(head["pivot"][0])
    y = chin
    while y <= sweater_top:
        for x, what in ((back, "back of the neck"), (pivot, "throat under the pivot")):
            assert covered(x, y), (
                f"at y={y} the {what} (x={x}) is bare: the room shows between "
                f"the jaw ({chin}) and the collar ({sweater_top})"
            )
        y += 0.5


def _inside_round_rect(px, py, rect, r, slack=0.5) -> bool:
    """Is (px, py) inside `rect` [x, y, w, h] with corners rounded to `r`?"""
    x, y, w, h = rect
    hw, hh = w / 2, h / 2
    ax, ay = abs(px - (x + hw)), abs(py - (y + hh))
    if ax > hw + slack or ay > hh + slack:
        return False
    dx, dy = max(0.0, ax - (hw - r)), max(0.0, ay - (hh - r))
    return math.hypot(dx, dy) <= r + slack


def _seat_outline(seat) -> list[tuple[float, float]]:
    """A seat volume as the outline it draws, whatever primitive drew it.

    A `litPath` hands over its points. A rounded rect is sampled round its
    four corner arcs, so the rules below grade a SHAPE rather than which
    helper made it — and the rect the seat used to be can be put back and
    watched fail them.
    """
    if seat["pts"]:
        return [tuple(p) for p in seat["pts"]]
    x, y, w, h = seat["rect"]
    r = seat["radius"]
    corners = [(x + w - r, y + h - r, 0), (x + r, y + h - r, 90),
               (x + r, y + r, 180), (x + w - r, y + r, 270)]
    return [
        (cx + r * math.cos(math.radians(a0 + k * 15)),
         cy + r * math.sin(math.radians(a0 + k * 15)))
        for cx, cy, a0 in corners for k in range(7)
    ]


@needs_node
def test_the_seated_figure_sits_on_a_seat_not_on_the_ball_of_a_thigh():
    """His hips are a jeans SEAT under the sweater, the rear thigh's round
    hip end stays inside it, and the seat is a BUTTOCK — flat where it sits,
    round where it leaves the chair back — not a box.

    With no seat, the hips were whatever the thighs' capsule ends made of
    them: the sweater's rounded bottom corners cut the back away, and the far
    thigh's hip cap — a disc half the thigh's width round the hip joint —
    filled the notch as a ball bulging out under the chair back. The user's
    word for it was "fix his butt". Increment 17 fixed it with a r4 rect,
    and the user's next word was "I don't like how the torso connects to the
    ass": below the backrest the rect showed as a plank with a square rear
    corner, a stick of a torso planted on it.

    So this grades rules rather than the one pose, on the seat's OUTLINE
    (`_seat_outline`, so a rect and a path answer the same questions):

    * every thigh whose hip sits BEHIND the body's centre (the deeper one,
      drawn first) has its whole hip cap inside the seat. The near thigh is
      exempt: its cap is the top of the lap, and the lap is supposed to show;
    * the seat is flat underneath across at least 40% of its width, because
      it is sitting on something;
    * the corner at the BACK and the bottom is rounded by at least two
      CELLs on both axes: the rear reaches its furthest point well above the
      cushion, and the bottom's last point stands well in from it. A CELL is
      what `litRect` rounds a slab by; a buttock is rounder than a slab.
      Which side is the back is read off the legs — the knees go forward.
    * and the seat stays with the hips when the torso breathes.
    """
    emitted = _seated_rig_run()
    jeans = emitted["HUMAN"]["jeans"]
    cell = emitted["CELL"]
    seats = [
        d for d in emitted["drawn"]
        if d["fill"] == jeans and d["host"] not in ("leg", "child")
    ]
    assert seats, "nothing but the legs wears jeans: his hips are the thighs' round ends"
    for seat in seats:
        # `breathe` moves `body.y` a whole CELL; the thighs never move. A
        # seat on the torso lifts off them and shows the ball every breath.
        assert seat["host"] == "seat", (
            f"the seat is painted on {seat['host']!r}, which moves while the "
            "thighs under it stay put"
        )
    assert len(seats) == 1, f"expected one seat volume, found {len(seats)}"
    outline = _seat_outline(seats[0])
    xs = [x for x, _ in outline]
    ys = [y for _, y in outline]
    width = max(xs) - min(xs)

    ends = [tuple(bone["end"]) for bone in emitted["legs"]]
    thighs = [
        bone for bone in emitted["legs"]
        if not any(math.dist(bone["start"], e) < 0.01 for e in ends)
    ]
    assert len(thighs) == 2, f"expected two thighs, found {len(thighs)}"
    rear = [t for t in thighs if t["start"][1] < 0]
    assert rear, "no thigh has its hip behind the body's centre to grade"
    for thigh in rear:
        hx, hy = thigh["start"]
        r = thigh["width"] / 2
        rim = [
            (hx + r * math.cos(a), hy + r * math.sin(a))
            for a in (i * math.tau / 72 for i in range(72))
        ]
        outside = [
            (round(px, 1), round(py, 1)) for px, py in rim
            if not _inside_convex(outline, px, py)
        ]
        assert not outside, (
            f"the rear thigh's hip cap (centre {hx:.1f},{hy:.1f}, r {r:.1f}) "
            f"pokes out of the seat at {outside[:4]}: that is the ball under "
            "the chair back"
        )

    bottom = max(ys)
    flat = [x for x, y in outline if abs(y - bottom) < 0.01]
    assert max(flat) - min(flat) >= 0.4 * width, (
        f"the seat is flat for {max(flat) - min(flat):.1f} of {width:.1f}: a "
        "pelvis sitting on a cushion is flat underneath, not a ball"
    )

    # The back is the side the knees are NOT on.
    forward = sum(t["end"][0] - t["start"][0] for t in thighs)
    back = -1 if forward > 0 else 1
    rear_x = max(back * x for x in xs)
    rear_ys = [y for x, y in outline if abs(back * x - rear_x) < 0.01]
    assert bottom - max(rear_ys) >= 2 * cell, (
        f"the seat's rear runs straight down to {max(rear_ys):.1f}, "
        f"{bottom - max(rear_ys):.1f} above the cushion: a box's back, not a "
        "buttock's"
    )
    assert rear_x - max(back * x for x in flat) >= 2 * cell, (
        f"the seat's flat underside runs to within "
        f"{rear_x - max(back * x for x in flat):.1f} of its rear: a square "
        "corner sitting on the cushion"
    )


def _rig_bone_driver(source: str) -> str:
    """`seatedRig`, with every painted volume tagged by the bone it went into.

    The driver above throws `g` away, which is enough to ask "what was
    drawn" and not enough to ask "what was drawn OVER what". A joint is a
    relationship between two bones, so this one keeps the Graphics: each gets
    an id and reports its final transform, because `boneGfx` sets `x`/`y`
    AFTER it paints and a transform read at paint time is all zeroes.
    """
    return (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + _js_block(source, "const HUMAN = {")
        + ";\n"
        + _js_block(source, "function mixHex(")
        + "\n"
        + """
        let nextId = 0;
        class FakeGraphics {
          constructor() {
            this.gid = nextId++;
            this.pivot = { x: 0, y: 0 };
            this.rotation = 0; this.x = 0; this.y = 0;
          }
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          roundRect() { return this; } circle() { return this; } poly() { return this; }
          fill() { return this; } stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const drawn = [];
        // `at` is what the call site authored; `snapped` is where `litRect`
        // actually puts it, because `band()` runs both coordinates through
        // `snap()` and the CELL grid is coarser than a finger is wide. A check
        // written against `at` would pass a hand whose separations authored
        // 3.9 units apart all landed in the same cell.
        const litRect = (x, y, w, h, r) => ({
          shape: "rect", at: [x, y, w, h], snapped: [snap(x), snap(y), w, h],
          // Alongside `at`/`snapped` rather than inside them: both are unpacked
          // positionally throughout this file, so a fifth element would
          // re-point every `x, y, w, h = ...` in it. Gradeable because the
          // corner is what domes a hand's finger end over.
          radius: r === undefined ? 0 : r,
          band: (g) => g, rim: (g) => g });
        const litCircle = (cx, cy, rad) => ({
          shape: "circle", at: [cx, cy, rad], band: (g) => g, rim: (g) => g });
        // A path reports its BOUNDING BOX as `at`/`snapped` (only its origin
        // snaps), and its outline, snapped, as `points`.
        const litPath = (pts, ox = 0, oy = 0) => {
          const [mnx, mny, mxx, mxy] = [Math.min(...pts.map((q) => q[0])), Math.min(...pts.map((q) => q[1])),
                                         Math.max(...pts.map((q) => q[0])), Math.max(...pts.map((q) => q[1]))];
          return {
            shape: "path", at: [ox + mnx, oy + mny, mxx - mnx, mxy - mny],
            snapped: [snap(ox) + mnx, snap(oy) + mny, mxx - mnx, mxy - mny],
            radius: null, points: pts.map(([x, y]) => [snap(ox) + x, snap(oy) + y]),
            band: (g) => g, rim: (g) => g };
        };
        function paint(g, volume, fill, rim, opts) {
          drawn.push({
            gid: g.gid, shape: volume.shape, at: volume.at,
            snapped: volume.snapped || volume.at, points: volume.points,
            radius: volume.radius === undefined ? 0 : volume.radius,
            fill, opts: opts || {},
          });
        }
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + "\n"
        + _seated_rig_block(source)
        + "\n"
        + """
        const accents = [];
        seatedRig(new FakeGraphics(), accents);
        // Only the bones report a width, and only legs are tagged `leg`. The
        // ORDER is the accent order, which is the order they render in.
        const bones = accents
          .filter((g) => g.limb === "leg")
          .map((g, order) => ({
            gid: g.gid, order, x: g.x, y: g.y,
            width: g.boneWidth, length: g.boneLength,
            rotation: g.rotation, depth: g.depth || null,
            // The drawn outline in the bone's own frame (`limbOutline`), or
            // null for a bone still painted as a capsule.
            outline: g.outline || null,
          }));
        // An arm is the accent that carries a forearm. They are not tagged
        // `limb` — `arm.limb` was deliberately removed when the arms came back
        // inside the cushion and answered to the seat-fit tests again — so the
        // child IS the identification.
        const arms = accents
          .filter((g) => g.forearm)
          .map((g) => ({
            gid: g.gid, width: g.boneWidth,
            foreGid: g.forearm.gid, foreWidth: g.forearm.boneWidth,
            // The forearm's rotation is stored RELATIVE to its parent, so the
            // angle that says which way its local axes point on screen is the
            // sum. Carried here rather than recomputed per test: "which side of
            // the hand is downward" is a fact about the rig, not about a caller.
            foreWorldRotation: g.rotation + g.forearm.rotation,
            // Enough to place the arm in the room: the shoulder, the upper
            // arm's angle and drawn length, the forearm's drawn length, and
            // which side of the torso it composites on.
            x: g.x, y: g.y, rotation: g.rotation, length: g.boneLength,
            foreLength: g.forearm.boneLength, depth: g.depth || null,
            outline: g.outline || null, foreOutline: g.forearm.outline || null,
          }));
        // The palette rides along rather than being restated in python: the
        // hand is identified by its skin fill, and a recoloured hand must not
        // silently empty that lookup.
        console.log(JSON.stringify({ bones, arms, drawn, HUMAN }));
        """
    )


def _bone_capsule(drawn: list, gid: int, palette: dict) -> dict | None:
    """The limb volume `boneGfx` builds for a bone: the one painted into that
    Graphics that is not skin or a crease.

    It used to be simply the FIRST volume there. Increment 20 paints the
    forearm's sleeve AFTER the hand, so its cuff laps over the wrist, and
    "first" then quietly returned the hand -- a test comparing the palm with
    the sleeve compared the palm with itself and passed.
    """
    return next(
        (d for d in drawn if d["gid"] == gid
         and d["fill"] not in (palette["skin"], palette["crease"], palette.get("contact"))),
        None,
    )


@needs_node
def test_the_seated_figure_paints_every_volume_in_its_own_colour():
    """Every volume of the trader is `keepFill` — which is now what decides he
    carries no outline, so it is worth grading directly.

    This replaces `test_only_the_ends_of_a_limb_carry_the_outline`, which
    encoded the PREVIOUS policy (thigh rimmed / shin `noRim` / shoe rimmed, and
    the same for the arm). That policy is gone: `paint` gives a `keepFill`
    volume no rim at all, so "the ends keep theirs" is no longer true and the
    old assertion could only fail. See `test_a_person_is_not_outlined` for the
    rule and the measurements behind it.

    What still needs grading here is the half that rule DEPENDS on: the rig
    paints itself in authored colours rather than in the mood's `BODY_FILL`.
    A volume that forgot `keepFill` would silently take a rim back, and it
    would take it back on a LIMB, which is exactly where the die-cut read was
    worst. Both limbs are still read in threes so the check keeps naming the
    part that broke.
    """
    source = _world_source()
    emitted = _run_node(_rig_bone_driver(source))
    bones, arms = emitted["bones"], emitted["arms"]
    drawn, palette = emitted["drawn"], emitted["HUMAN"]

    assert len(bones) == 6, (
        f"expected two legs of three bones, got {len(bones)}: this test reads "
        "them in threes and a change to the leg's bone count invalidates that"
    )
    assert len(arms) == 2, f"expected two arms carrying a forearm, got {len(arms)}"

    chains = []
    for leg, start in enumerate((0, 3)):
        thigh, shin, shoe = bones[start : start + 3]
        chains.append(
            (
                f"leg {leg}",
                ("thigh", _bone_capsule(drawn, thigh["gid"], palette)),
                ("shin", _bone_capsule(drawn, shin["gid"], palette)),
                ("shoe", _bone_capsule(drawn, shoe["gid"], palette)),
            )
        )
    for side, arm in enumerate(arms):
        # The hand is not a bone: it is painted INTO the forearm, after the
        # sleeve, so it is the skin-coloured volume sharing that Graphics.
        hand = next(
            (
                d
                for d in drawn
                if d["gid"] == arm["foreGid"] and d["fill"] == palette["skin"]
            ),
            None,
        )
        chains.append(
            (
                f"arm {side}",
                ("upper arm", _bone_capsule(drawn, arm["gid"], palette)),
                ("forearm", _bone_capsule(drawn, arm["foreGid"], palette)),
                ("hand", hand),
            )
        )

    for limb, (outer_name, outer), (mid_name, mid), (end_name, end) in chains:
        for name, volume in ((outer_name, outer), (mid_name, mid), (end_name, end)):
            assert volume, f"{limb}: no {name} volume is painted at all"

        for name, volume in (
            (outer_name, outer),
            (mid_name, mid),
            (end_name, end),
        ):
            assert volume["opts"].get("keepFill"), (
                f"{limb}: the {name} is not painted `keepFill`, so it takes "
                "the mood as its fill and -- because `paint` keys the outline "
                "off exactly that flag -- takes a cream keyline back with it. "
                "A person is painted in skin, hair, sweater and jeans; only a "
                "market glyph is painted in the mood."
            )


@needs_node
def test_a_person_is_not_outlined():
    """`paint` strokes no rim for a `keepFill` volume, and still strokes one
    for a volume that takes the mood as its fill.

    This is the rim POLICY, graded at the mechanism rather than at the call
    sites -- which is the lesson increment 6 taught when it restated the option
    beside the new arms instead of the rule, and the forearm shipped outlined.

    `keepFill` is not a proxy chosen here; it is the page's own definition of
    "a volume of a person" -- "`keepFill` is the human's exemption, and it
    exists because a person is not one colour". The machine head takes the
    other branch (`paint(skull, litCircle(0, 0, 28), BODY_FILL)`) and keeps its
    rim, as every market glyph does: a glyph is a shape that needs an edge
    against a busy room, and a person is not.

    WHY the human's rim goes, measured on the delivered frame rather than
    argued (vault `Docs/character-redesign.md`, increment 8):

    * The rim was the human's only COLOUR mood channel, and that channel is
      constant in the shipped configuration: `react()` routes a mood to the
      trader only from `trader_*` events, which come only from the opt-in
      freqtrade sidecar, and `draw(state)` re-pins the trader to `"idle"` on
      every state update. Mood still reaches the figure through the other two
      of the brief's three channels -- face and posture -- which this does not
      touch.
    * What the rim actually delivered was the LAMP, not the mood:
      `rimStyle` mixes every rim 30% toward `LIGHT.warmth` (`#f0a848`), so the
      trader's edge measured hue 26 deg / sat 0.16 whatever the mood, against
      authored tints of sat 0.31-1.00.
    * At 1:1 viewer scale the unrimmed figure reads as a painted person and the
      rimmed one as an assembly of outlined tubes -- the die-cut sticker read
      KI-051 exists to remove. The lit/shade bands were always doing the
      separating; the rim competed with them.

    This also dissolves the per-END defect the cap was invented for: with no
    rim on the human there is no keyline to overhang the knee, and no reason to
    paint a disc over the joint (which read as a ball joint and made the trader
    a wooden mannequin).
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0x8899aa;\n"
        + "const BODY_RIM = { width: 1.5, color: 0xffffff, alignment: 1 };\n"
        + _js_const(source, "LIGHT")
        + _js_const(source, "CELL")
        + _js_block(source, "function snap(")
        + "\n"
        + _shading_prelude(source)
        + """
        // A rim is the ONE thing drawn with `stroke`; every band is a `fill`.
        // So counting strokes counts rims, without this test having to know
        // how `rimStroke` builds its path.
        let strokes = [];
        class FakeGraphics {
          roundRect() { return this; } circle() { return this; } poly() { return this; }
          fill() { return this; }
          stroke(style) { strokes.push(style ?? null); return this; }
        }
        const run = (opts) => {
          strokes = [];
          paint(new FakeGraphics(), litRect(0, 0, 20, 40, 10),
                HUMAN.sweater, 0x223344, opts);
          const rect = strokes.length;
          strokes = [];
          paint(new FakeGraphics(), litCircle(0, 0, 20),
                HUMAN.skin, 0x223344, opts);
          return { rect, circle: strokes.length };
        };
        console.log(JSON.stringify({
          person: run({ keepFill: true }),
          glyph: run(undefined),
          // The interior exemption still has to work for the glyph path.
          glyphInner: run({ noRim: true }),
        }));
        """
    )
    out = _run_node(driver)

    for shape in ("rect", "circle"):
        assert out["person"][shape] == 0, (
            f"a keepFill {shape} volume still strokes a rim "
            f"({out['person'][shape]} of them). A person is not outlined: the "
            "figure's edge is carried by the lit/shade bands, and a keyline "
            "over them is the die-cut sticker read."
        )
        assert out["glyph"][shape] == 1, (
            f"a plain {shape} volume has lost its rim. This increment takes "
            "the outline off the HUMAN, not off the market glyphs and the "
            "model -- a glyph needs an edge against a busy room."
        )
        assert out["glyphInner"][shape] == 0, (
            f"`noRim` stopped working for a plain {shape} volume. The "
            "interior exemption is still what the model's own volumes use."
        )


@needs_node
def test_a_matte_volume_takes_no_lamp_band_even_after_a_mood_change():
    """`matte` drops the lit rung, and SURVIVES `repaint`.

    Measured before it was believed (vault `Docs/character-redesign.md`,
    increment 9; re-runnable as `scripts/measure_lit_band.py`). `shade` mixes
    toward `LIGHT.warmth` by an ABSOLUTE 0.3 for the lit rung, so every cool
    volume's lit band lands within 5 degrees of the lamp's own hue (34 deg):
    the sweater drifts 180, the jeans 169, the shoes 164. Predicted and then
    confirmed off a live frame -- `shoes.lit` `#655340` and `skin.shade`
    `#886955`, both exact.

    The shoes are where that stops being a look and becomes a defect: they
    author at hue 227 and land at 31, which is 4 degrees off skin's OWN
    authored hue, so the trader appears to be wearing slippers. The near foot
    measured 327px of warm wash against 150px of authored `#2a2e3c` -- 3.4:1.

    Value lift is NOT the fault and must not be blamed later: the hair lifts
    0.29 -> 0.49, a LARGER gain than the shoes' 0.24 -> 0.40, and reads fine,
    because it drifts 8 degrees rather than 164. The fault is a value lift onto
    a hue the volume never authored. That is also why darkening `HUMAN.shoes`
    is the wrong fix -- it lowers the band and leaves it at hue 31 -- and the
    A/B frame refused it exactly as the table predicted.

    The second half of this test is the one that would rot silently. `repaint`
    rebuilds its options field by field (`{ keepFill, noRim, matte }`), so it
    is a WHITELIST: an option it does not name is dropped the next time a mood
    is applied. `matte` was in fact lost this way when first written -- the A/B
    shot came back pixel-identical to the shipped figure -- because `draw()`
    re-pins the trader to `"idle"` on every update and repaints him. Anything
    added to `paint`'s options from here needs a line in `repaint` and a line
    in this test.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0x8899aa;\n"
        + "const BODY_RIM = { width: 1.5, color: 0xffffff, alignment: 1 };\n"
        + _js_const(source, "LIGHT")
        + _js_const(source, "CELL")
        + _js_block(source, "function snap(")
        + "\n"
        + _shading_prelude(source)
        + _js_block(source, "function repaint(")
        + "\n"
        + """
        // Every band is a `fill`; the rim is the one `stroke`. Recording the
        // fills is therefore recording the bands, in draw order.
        let fills = [];
        class FakeGraphics {
          constructor() { this.children = []; }
          roundRect() { return this; } circle() { return this; } poly() { return this; }
          stroke() { return this; } clear() { return this; }
          fill(c) { fills.push(c && c.color !== undefined ? c.color : c); return this; }
        }
        const run = (opts) => {
          const g = new FakeGraphics();
          fills = [];
          paint(g, litRect(0, 0, 20, 40, 10), HUMAN.shoes, 0x223344, opts);
          const painted = fills.slice();
          // What `applyMood` does on every single state update.
          fills = [];
          repaint(g, 0x8899aa, 0x223344);
          return { painted, repainted: fills.slice() };
        };
        console.log(JSON.stringify({
          matte: run({ keepFill: true, matte: true }),
          plain: run({ keepFill: true }),
          lamp: shade(HUMAN.shoes, rampLevel("lit")),
          authored: HUMAN.shoes,
        }));
        """
    )
    out = _run_node(driver)
    lamp, authored = out["lamp"], out["authored"]

    assert lamp != authored, (
        "the lit rung has stopped moving the shoe colour at all, so this test "
        "can no longer tell a matte volume from a lit one. Check `LIGHT.ramp`."
    )
    for phase in ("painted", "repainted"):
        assert lamp not in out["matte"][phase], (
            f"a matte volume took the lamp's band ({lamp:#08x}) when {phase}. "
            "The shoes author at hue 227 and this band lands at 31 -- 4 "
            "degrees off skin -- which is the whole of the slipper read."
        )
        assert authored in out["matte"][phase], (
            f"a matte volume lost its authored colour when {phase}. Dropping "
            "the lit rung means falling back to the base rung, not skipping "
            "the band: the shoe still needs its brightest face painted."
        )
        assert lamp in out["plain"][phase], (
            f"a plain volume lost the lamp's band when {phase}. This increment "
            "takes the warm band off the SHOES, not off the room -- `shade` is "
            "shared with every glyph, the model robot and world/visuals.py."
        )


@needs_node
def test_everything_the_trader_wears_is_matte_and_only_his_skin_takes_the_lamp():
    """Every volume of the seated rig is matte except skin.

    Increment 9 scoped `matte` to the shoe and let the sweater and jeans keep
    the lamp's band, judging "a warm sheen at roughly their own brightness"
    to read. Increment 15 refused it off the 1:1 frame: the lit rung mixes
    0.45 toward `#f0a848`, which turns grey-blue cloth tan, and the band came
    out as a skin-coloured ellipse on the sleeve, the thigh and the shin --
    knee pads and holes, not light. The reference
    (`Docs/char-redesign-reference/ref-trader.png`) lights every garment within
    its own hue and keeps the warm band for skin.

    Graded as a rule over EVERY volume rather than bone by bone, for the
    reason increment 6 taught: a rule restated per limb is a rule the next
    limb gets wrong. So a new garment that forgets `matte` fails here, and so
    does a hand or a neck that takes it.
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))
    drawn, human = emitted["drawn"], emitted["HUMAN"]

    skin = [v for v in drawn if v["fill"] == human["skin"]]
    worn = [v for v in drawn if v["fill"] != human["skin"]]
    assert skin and len(worn) >= 8, (
        f"found {len(skin)} skin and {len(worn)} other volumes; expected a "
        "neck and a hand of skin and at least torso, collar, two arms, two "
        "thighs, two shins and two shoes of cloth. The driver lost the rig."
    )
    for v in worn:
        assert v["opts"].get("matte") and v["opts"].get("keepFill"), (
            f"a {v['shape']} at {v['at']} in {v['fill']:#08x} takes the lamp's "
            "band: a grey-blue garment's lit rung lands tan and reads as a "
            "patch of skin. Paint it `cloth`/`clothInner`."
        )
    for v in skin:
        assert not v["opts"].get("matte"), (
            f"the skin at {v['at']} went matte. Skin is the one material the "
            "warm band is right for -- a matte hand is a grey glove."
        )


@needs_node
def test_the_forearm_casts_a_contact_shadow_on_the_thigh():
    """Where the forearm lies on the lap, a band of shadow separates them.

    The 1:1 evaluation (2026-09-24): sweater, forearm and thigh read as ONE
    blue-grey mass, and it is not a value problem -- our sweater/jeans gap
    already exceeds the reference's (`#435e71`/`#3b5562`). What the reference
    has is an EDGE where the arm crosses the leg. So the forearm paints its
    own silhouette, shifted toward the FLOOR side, in `HUMAN.contact` BEFORE
    the hand and the sleeve: they cover all of it but a sliver on the thigh
    below them, which is exactly a contact shadow, and it moves with the arm
    because it is the arm's own Graphics.
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))
    drawn, human = emitted["drawn"], emitted["HUMAN"]
    contact = human["contact"]
    assert _relative_luminance(contact) < _relative_luminance(human["jeans"]) * 0.8, (
        f"the contact shadow {contact:#08x} is not darker than the jeans it falls on"
    )
    for arm in emitted["arms"]:
        on_fore = [(i, d) for i, d in enumerate(drawn) if d["gid"] == arm["foreGid"]]
        shadows = [(i, d) for i, d in on_fore if d["fill"] == contact]
        assert shadows, "the forearm casts no contact shadow"
        first_cover = min(i for i, d in on_fore
                          if d["fill"] in (human["skin"], human["sweater"]))
        assert max(i for i, _ in shadows) < first_cover, (
            "the contact shadow is painted over the hand or the sleeve"
        )
        floor = 1 if math.sin(arm["foreWorldRotation"]) > 0 else -1
        outline = arm["foreOutline"]
        sleeve_shadow = [d for _, d in shadows if len(d["points"]) == len(outline)]
        assert sleeve_shadow, "no shadow carries the sleeve's own outline"
        shifts = {(round(q[0] - o[0], 6), round(q[1] - o[1], 6))
                  for q, o in zip(sleeve_shadow[0]["points"], outline)}
        assert len(shifts) == 1, f"the shadow is not the sleeve translated: {shifts}"
        (dx, dy), = shifts
        assert dy == 0 and 2 <= dx * floor <= 6, (
            f"the shadow is shifted ({dx}, {dy}); it must fall 2-6 units toward "
            f"the floor side ({floor:+d} local x), onto the thigh"
        )


@needs_node
def test_the_hand_in_the_lap_sits_in_shadow_but_stays_skin():
    """The hand is `shadowed`: every rung one shade step down, never matte.

    The reference's hand in the lap measures `#8b6d6b`; ours was lit to the
    lamp's rung and was the loudest thing on the body -- the eye went to the
    lap before the face. Skin's own shade rung renders `#886955`, which is
    the reference, so the hand drops a rung rather than changing colour: it
    keeps `HUMAN.skin` (every hand test finds it by that fill) and keeps its
    warmth (`matte` would be the grey glove). `repaint` is a whitelist, so
    the option must survive a mood change too.
    """
    source = _world_source()
    emitted = _run_node(_rig_bone_driver(source))
    drawn, human = emitted["drawn"], emitted["HUMAN"]
    fores = {a["foreGid"] for a in emitted["arms"]}
    hands = [d for d in drawn if d["gid"] in fores and d["fill"] == human["skin"]]
    assert hands and all(d["opts"].get("shadowed") for d in hands), hands
    necks = [d for d in drawn if d["gid"] not in fores and d["fill"] == human["skin"]]
    assert necks and not any(d["opts"].get("shadowed") for d in necks), (
        "the neck went into shadow too: only the hand in the lap is out of the lamp"
    )
    out = _run_node(
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0x8899aa;\n"
        + "const BODY_RIM = { width: 1.5, color: 0xffffff, alignment: 1 };\n"
        + _js_const(source, "LIGHT") + _js_const(source, "CELL")
        + _js_block(source, "function snap(") + "\n"
        + _shading_prelude(source)
        + _js_block(source, "function repaint(") + "\n"
        + """
        let fills = [];
        class FakeGraphics {
          constructor() { this.children = []; }
          roundRect() { return this; } circle() { return this; } poly() { return this; }
          stroke() { return this; } clear() { return this; }
          fill(c) { fills.push(c && c.color !== undefined ? c.color : c); return this; }
        }
        const g = new FakeGraphics();
        paint(g, litRect(0, 0, 20, 40, 10), HUMAN.skin, 0x223344,
              { keepFill: true, shadowed: true });
        const painted = fills.slice();
        fills = [];
        repaint(g, 0x8899aa, 0x223344);
        const s = rampLevel("shade");
        console.log(JSON.stringify({ painted, repainted: fills.slice(),
          want: [shade(HUMAN.skin, 2 * s), shade(HUMAN.skin, s),
                 shade(HUMAN.skin, rampLevel("lit") + s)] }));
        """
    )
    assert out["painted"] == out["want"], out
    assert out["repainted"] == out["want"], (
        "`shadowed` was dropped by `repaint` -- add it to the whitelist"
    )


@needs_node
def test_the_seated_arms_bend_at_an_elbow():
    """Two bones and a joint, and the forearm hangs off the upper arm.

    The comment this replaced asserted the reference "paints an arm as ONE
    tapering sleeve ... with no articulated elbow". The trace the whole
    redesign runs on says otherwise — it carries `elbow_near`/`elbow_far`, and
    the near arm turns a near right angle at one — and a 4x crop of the art
    shows the sleeve making that turn. A straight tube from shoulder to a
    round hand was the single most doll-like thing left on the figure.

    Three claims, and the third is the one that would silently rot:

    1. each arm has exactly one forearm child, and the elbow is a real BEND
       rather than a seam in a straight tube;
    2. both segments carry the trace's length, converted through the one unit
       this redesign uses;
    3. the forearm is attached AT the far end of the upper arm. That is a
       reparenting calculation — PIXI maps a child through the parent's pivot
       AND rotation — and getting it wrong detaches the forearm by a few units
       in a way no screenshot at this scale would show. It is checked by
       composing the recorded transforms two independent ways and requiring
       them to agree, rather than by restating the page's own arithmetic.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + _js_block(source, "const HUMAN = {")
        + ";\n"
        + _js_block(source, "function mixHex(")
        + "\n"
        + """
        class FakeGraphics {
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          constructor() {
            this.calls = []; this.children = [];
            this.x = 0; this.y = 0;
            this.pivot = { x: 0, y: 0 }; this.rotation = 0;
          }
          addChild(c) { this.children.push(c); return c; }
          roundRect() { return this; } circle() { return this; } poly() { return this; }
          fill() { return this; } stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const litRect = (x, y, w, h, r) => ({
          rect: [x, y, w, h], band: (g) => g, rim: (g) => g });
        const litCircle = (cx, cy, rad) => ({
          rect: [cx - rad, cy - rad, 2 * rad, 2 * rad],
          band: (g) => g, rim: (g) => g });
        const litPath = (pts, ox = 0, oy = 0) => {
          const [mnx, mny, mxx, mxy] = [Math.min(...pts.map((q) => q[0])), Math.min(...pts.map((q) => q[1])),
                                         Math.max(...pts.map((q) => q[0])), Math.max(...pts.map((q) => q[1]))];
          return { rect: [ox + mnx, oy + mny, mxx - mnx, mxy - mny],
                   band: (g) => g, rim: (g) => g };
        };
        function paint(g, volume, fill, rim, opts) {
          (g.drawn || (g.drawn = [])).push({ rect: volume.rect, fill });
        }
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + "\n"
        + _seated_rig_block(source)
        + "\n"
        + """
        const accents = [];
        seatedRig(new FakeGraphics(), accents);
        const arms = accents.filter((a) => !a.limb);
        console.log(JSON.stringify(arms.map((a) => ({
          x: a.x, y: a.y, rotation: a.rotation, pivot: a.pivot,
          length: a.boneLength, trueLength: a.boneTrue, width: a.boneWidth,
          children: a.children.map((c) => ({
            x: c.x, y: c.y, rotation: c.rotation, pivot: c.pivot,
            length: c.boneLength, trueLength: c.boneTrue, width: c.boneWidth,
            drawn: c.drawn,
          })),
        }))));
        """
    )
    arms = _run_node(driver)
    assert len(arms) == 2, f"expected two arms, got {len(arms)}"

    # The unit: 59.5 reference px per head, 44 rig units per head. Both arm
    # bones trace at 58.1 px, so both want ~43 units.
    TARGET = 58.1 / 59.5 * 44

    for index, arm in enumerate(arms):
        assert len(arm["children"]) == 1, (
            f"arm {index} carries {len(arm['children'])} child bones; an arm "
            "is an upper arm with exactly one forearm parented to it"
        )
        fore = arm["children"][0]

        for name, seg in (("upper arm", arm), ("forearm", fore)):
            # The TRUE length, before the turn foreshortens it. Both arms are
            # the same bones, so this is the one that may not vary with side.
            assert 0.8 * TARGET <= seg["trueLength"] <= 1.25 * TARGET, (
                f"arm {index}'s {name} is authored {seg['trueLength']:.0f} "
                f"units where the trace asks for {TARGET:.0f}"
            )
            # ...and a projection can only ever SHORTEN it. A drawn bone
            # longer than its own bone means the turn or the elevation is
            # being applied twice.
            assert seg["length"] <= seg["trueLength"] + 1, (
                f"arm {index}'s {name} draws {seg['length']:.0f} units from a "
                f"{seg['trueLength']:.0f}-unit bone: a projection cannot "
                "lengthen anything"
            )

        # A bend, not a seam. The forearm's rotation is stored relative to its
        # parent, so a straight tube reads as ~0 here however the whole arm is
        # swung. The reference's near elbow is near a right angle.
        assert abs(fore["rotation"]) > 0.6, (
            f"arm {index}'s forearm sits {fore['rotation']:.2f} rad off its "
            "upper arm: that is a straight tube with a seam in it, not an elbow"
        )

        # Attachment, two ways. The far end of the upper-arm capsule in the
        # parent's own local frame...
        end_local = (
            arm["pivot"]["x"],
            arm["length"] + arm["pivot"]["y"],
        )
        # ...and where the child was actually parked.
        child_local = (fore["x"], fore["y"])
        gap = math.dist(end_local, child_local)
        assert gap < 1.5, (
            f"arm {index}'s forearm is parked {gap:.1f} units from the end of "
            f"its upper arm (elbow at {end_local}, child at {child_local}): "
            "the reparenting maths dropped the pivot or the rotation"
        )

        # The hand is on the end of the sleeve, not floating past it. It has
        # to be placed at the forearm's DRAWN length, because the sleeve is a
        # projection of a 43-unit bone and can come out at 28 — a hand parked
        # at the authored length hangs in clear air beside the arm.
        sleeve_end = fore["length"] + fore["width"]
        hands = [d for d in fore["drawn"] if d["fill"] == 0xC08F68]
        assert len(hands) == 1, f"arm {index} has {len(hands)} hands"
        hand_top = hands[0]["rect"][1]
        assert hand_top <= sleeve_end, (
            f"arm {index}'s hand starts at {hand_top} but its sleeve ends at "
            f"{sleeve_end:.0f}: the hand is floating off the end of the arm"
        )


# The hand, measured off `ref-trader.png` and converted through the one unit
# this redesign uses (1 head = 59.5 reference px = 44 rig units).
#
# `ref-trader.png` is an exact 3x upscale of the traced frame, so its pixels
# divide by 3 before they convert. Re-measured 2026-09-22 against both hands:
#
#   reference NEAR hand    30 px x 32 px  ->  22.2 u x 23.7 u
#   reference FAR  hand    26 px x 25 px  ->  19.2 u x 18.5 u
#
# THE HAND IS STRAIGHT (2026-09-23, the user's call on the open pose
# question): it carries on the line of the forearm, fingers pointing where the
# forearm points, and lies flat on the thigh. Until then it hung off the wrist
# at a right angle with the fingers aimed at the floor -- a claw over the
# knee, not a hand at rest.
#
# So the two axes swapped roles. ALONG is now heel to fingertip, down the
# forearm's own line, and it carries the palm AND the fingers: 8 of palm and 12
# of finger, the 1:1.5 split increment 12 measured (the reference divides palm
# to finger about 1:1.4). 20 sits between the two reference hands.
HAND_ALONG_U = 20.0

# ACROSS is now the span of four fingers side by side, and it is the number the
# CELL forces: `litRect` snaps both coordinates, only a whole-cell offset
# survives that snap unchanged (`snap(x + 4k) == snap(x) + 4k`), so three
# separations can only sit at +4, +8 and +12 -- and a hand narrower than 16
# leaves the fourth finger a stub. 16 is also inside the reference's range.
HAND_ACROSS_U = 16.0

# Of the ALONG extent, the fingers take more than the palm -- the prong rule,
# graded on its own in `test_the_fingers_reach_further_than_the_palm_they_hang_from`.
FINGER_DEPTH_FRACTION = 0.6

# A separation's own thickness. Four fingers at a 4-unit pitch makes the gap
# and the finger the same order of size, which is what the reference shows --
# its gaps measure 3-7 px against fingers of 1-4 px. 2.0 is as wide as the
# pitch allows while keeping the widest finger inside the evenness bound
# below: the leading finger has no separation beside it, so it is a whole
# 4 units against the other three at 4 - gap.
HAND_GAP_U = 2.0

# A separation is a long thin line running the length of a finger. Anything
# painted in `HUMAN.crease` that is squarer than this -- in EITHER axis -- is
# BLUNT: a volume big enough both ways for `paint`'s three insets to read as a
# dark object rather than as shading, the joint cap this redesign has now hit
# three times. Told apart by shape, not by orientation or draw order: the hand
# turned a right angle on 2026-09-23 and a classifier keyed on "wide in x"
# filed every separation as debris.
SEPARATION_ASPECT = 2.0
# `const CELL = (PLATE && PLATE.cell) || 4`, and the rig runs with no plate in
# these drivers. Restated here rather than parsed because it is the grid the
# ASSERTION is about: if the page's cell ever moves, this check should be read
# again rather than silently follow it.
CELL_U = 4


def _outline_depth_at(pts, x: float) -> float:
    """The lowest (largest-y) crossing of the vertical line through `x` with
    the polygon `pts`. The test-side twin of the page's `outlineDepthAt`."""
    depth = -math.inf
    for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
        if not min(ax, bx) - 1e-9 <= x <= max(ax, bx) + 1e-9:
            continue
        y = max(ay, by) if ax == bx else ay + (by - ay) * (x - ax) / (bx - ax)
        depth = max(depth, y)
    return depth


def _hand_parts(emitted: dict, arm: dict) -> tuple[dict, list[dict], list[dict]]:
    """The one skin volume on a forearm, the separations, and the tip cuts.

    Identified by fill rather than by draw order: order is how `paint` stacks
    them and a test that encoded it would pass a hand whose creases had been
    painted UNDER the slab and were invisible.

    Separations and tip cuts share `HUMAN.crease` — they are the same shadow —
    so they are split by SHAPE: a separation runs the length of a finger and is
    gap-thin, a tip cut is a blunt block that shortens one. Keying that on the
    aspect ratio -- long axis against short, whichever way the hand lies --
    states the difference the eye reads; keying it on the order the loop
    happens to paint them in, or on which axis is long, would not.
    """
    palette = emitted["HUMAN"]
    on_fore = [d for d in emitted["drawn"] if d["gid"] == arm["foreGid"]]
    slabs = [d for d in on_fore if d["fill"] == palette["skin"]]
    creases = [d for d in on_fore if d["fill"] == palette.get("crease")]
    seps = [
        c
        for c in creases
        if max(c["snapped"][2:4]) > SEPARATION_ASPECT * min(c["snapped"][2:4])
    ]
    tips = [c for c in creases if c not in seps]
    return (slabs[0] if len(slabs) == 1 else None), seps, tips


@needs_node
def test_the_hand_is_a_slab_with_three_finger_separations():
    """Four fingers, so three dark lines cut into the hand's far end.

    The reference paints a hand as a broad pale slab divided into fingers by
    near-black shadow (`#262127` sampled between the far hand's fingers,
    against `#a67265` on the lit slab). The shipped hand was once a single
    rounded rect with nothing cut into it, which at 1:1 is a mitten on the end
    of a stick.

    Three separations and not four fingers, deliberately. `paint` draws three
    banded insets per volume, and four finger-sized volumes would each take a
    set — the joint-cap failure that made the elbow read as a bolt, repeated
    four times per hand. One slab keeps the bands at a size that reads as
    shading, and the fingers are the gaps between what is cut out of it.

    The hand is STRAIGHT: the fingers run down the forearm's own local y, so a
    separation is long in y and gap-thin in x, and the four fingers sit side by
    side across x. `test_the_hand_carries_on_the_line_of_the_forearm` grades
    where along the hand they are cut.

    The separations are FILLS, never `stroke` — `paint` gives a `keepFill`
    volume no rim precisely because a person is not outlined
    (`test_a_person_is_not_outlined`), and an outlined hand would undo
    increment 8 on the one volume that most needs internal detail.
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))
    arms = emitted["arms"]
    assert len(arms) == 2, f"expected two arms, got {len(arms)}"

    for index, arm in enumerate(arms):
        slab, creases, _tips = _hand_parts(emitted, arm)
        assert slab, (
            f"arm {index} has no single skin volume on its forearm: the hand "
            "is one slab, and the fingers are cut out of it rather than "
            "painted as separate volumes"
        )
        assert len(creases) == 3, (
            f"arm {index} cuts {len(creases)} separations into its hand; four "
            "fingers need three, and the reference's hand has four"
        )

        _, _, across, along = slab["snapped"]
        assert along == pytest.approx(HAND_ALONG_U, abs=1.0), (
            f"arm {index}'s hand reaches {along:.1f} units past the wrist, "
            f"heel to fingertip, where the reference traces {HAND_ALONG_U}"
        )
        assert across == pytest.approx(HAND_ACROSS_U, abs=1.0), (
            f"arm {index}'s hand is {across:.1f} units across its four "
            f"fingers where the grid and the reference agree on {HAND_ACROSS_U}"
        )

        for crease in creases:
            assert crease["opts"].get("keepFill"), (
                f"arm {index}: a finger separation is not `keepFill`, so it "
                "takes the mood as its fill and -- because `paint` keys the "
                "outline off exactly that flag -- strokes a rim. A crease in "
                "a hand is shadow, and shadow does not change with the mood."
            )
            cx, cy, cw, ch = crease["snapped"]
            assert cw < ch, (
                f"arm {index}: a separation is {cw:.1f} across by {ch:.1f} "
                "along the forearm. The hand is straight, so a finger gap runs "
                "DOWN the hand with the fingers -- a line across it is the "
                "old drooping hand's cut, turned a right angle from its fingers"
            )
            assert ch == pytest.approx(along * FINGER_DEPTH_FRACTION, abs=1.5), (
                f"arm {index}: a separation cuts {ch:.1f} units into a "
                f"{along:.1f}-unit hand. The fingers take a little over half "
                "of the heel-to-tip length; a cut that reaches further turns "
                "the hand into a fork"
            )
            sx, sy, sw, sh = slab["snapped"]
            assert sx <= cx and cx + cw <= sx + sw + 1e-9, (
                f"arm {index}: a separation spans {cx:.1f}..{cx + cw:.1f} "
                f"across a hand spanning {sx:.1f}..{sx + sw:.1f}: it is cut "
                "outside the hand it is meant to divide"
            )
            assert sy <= cy and cy + ch <= sy + sh + 1e-9, (
                f"arm {index}: a separation spans {cy:.1f}..{cy + ch:.1f} "
                f"along a hand spanning {sy:.1f}..{sy + sh:.1f}: it is cut "
                "outside the hand it is meant to divide"
            )

        # The separations are authored a whole CELL apart, which is the RULE
        # rather than its outcome. `snap(v + 4k) == snap(v) + 4k` holds for
        # every v, so a whole-cell offset survives the snap at any pose; an
        # offset of 3.9 survives it at THIS one and collapses two separations
        # into one cell at the next pose someone authors. The evenness check
        # below cannot see that — it measures the hand the page draws today,
        # and today's arm happens to round the right way.
        sx_raw = slab["at"][0]
        for crease in creases:
            offset = crease["at"][0] - sx_raw
            assert offset % CELL_U == pytest.approx(0, abs=1e-9), (
                f"arm {index}: a separation is authored {offset:.2f} units "
                f"across a hand, which is not a whole {CELL_U}-unit CELL. "
                "`litRect` snaps it, and only a whole cell survives that "
                "unchanged -- at some other arm pose two of these land in the "
                "same cell and two fingers silently become one."
            )

        # Four fingers of roughly one width. Measured as the gaps BETWEEN the
        # separations rather than as their positions, so this states the thing
        # the eye reads and stays true if the hand is ever re-placed.
        edges = sorted(c["snapped"][0] for c in creases)
        gap = creases[0]["snapped"][2]
        sx, sw = slab["snapped"][0], slab["snapped"][2]
        fingers = (
            [edges[0] - sx]
            + [b - a - gap for a, b in zip(edges, edges[1:])]
            + [sx + sw - (edges[-1] + gap)]
        )
        assert min(fingers) > 0, (
            f"arm {index}'s fingers measure {[round(f, 1) for f in fingers]}: "
            "two separations overlap, so a finger has no width at all"
        )
        assert max(fingers) <= 2.2 * min(fingers), (
            f"arm {index}'s fingers measure {[round(f, 1) for f in fingers]} "
            "units. The reference's four are within a whisker of each other; "
            "one much wider than the rest reads as a thumb, which a hand seen "
            "from behind lying on a thigh does not show"
        )


@needs_node
def test_the_hand_carries_on_the_line_of_the_forearm():
    """The hand is straight: palm at the wrist, fingers at the far end.

    This replaces `test_the_fingers_hang_from_the_hands_screen_downward_edge`,
    which graded the pose the user overruled on 2026-09-23. The hand used to
    hang off the wrist at a right angle with its fingers aimed at the floor;
    over the knee that read as a claw rather than as a hand at rest.

    Graded in the forearm's OWN frame, which is what makes it pose-free: the
    bone runs down local +y, so a straight hand is one whose long axis is y,
    whose separations run the same way, and whose fingers are cut from the end
    FURTHEST from the wrist. A separation cut from the heel would put the
    fingers inside the sleeve and a palm on the end -- a hand on backwards.
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))

    for index, arm in enumerate(emitted["arms"]):
        slab, seps, _tips = _hand_parts(emitted, arm)
        assert slab and len(seps) == 3, (
            f"arm {index} has no hand to check; see "
            "test_the_hand_is_a_slab_with_three_finger_separations"
        )
        _sx, sy, sw, sh = slab["snapped"]
        assert sh > sw, (
            f"arm {index}'s hand is {sw:.1f} across by {sh:.1f} down the "
            "forearm: it lies ACROSS the bone, so the fingers point off the "
            "side of the arm instead of carrying on its line"
        )
        for sep in seps:
            cx, cy, cw, ch = sep["snapped"]
            # The hand is an OUTLINE since increment 18, its tips an arc at the
            # outer corners, so "where the hand ends" is the outline's own
            # depth across this gap — the shallower of its two edges, or the
            # cut either stops short of the tip or paints past it.
            ends = min(_outline_depth_at(slab["points"], cx),
                       _outline_depth_at(slab["points"], cx + cw))
            assert cy + ch == pytest.approx(ends, abs=1e-6), (
                f"arm {index}: a separation ends at {cy + ch:.2f} where the "
                f"hand ends at {ends:.2f}. A finger gap has to open at the "
                "fingertips, or the fingers are welded together at the end"
            )
            assert cy > sy, (
                f"arm {index}: a separation starts {cy - sy:.1f} units past "
                "the heel. The fingers are at the far end of the hand, with "
                "the palm between them and the wrist"
            )


@needs_node
def test_the_fingers_reach_further_than_the_palm_they_hang_from():
    """A prong, not a notch — the rule increment 10 got backwards.

    Measured 2026-09-22 at matched scale against both reference hands: the
    reference spends MORE of the hand's depth on fingers than on palm (about
    1:1.4), so the digits get free of the palm's silhouette and read as four
    separate things. The shipped hand spent 8 units of palm against 6 of
    finger, and at 1:1 that reads as three dark slots scratched into a rounded
    blob — a mitten with stitching.

    This is graded as an inequality rather than as today's 12-against-8,
    because the numbers are art and the rule is not: whatever depth the hand
    ends up with, the cut has to go deeper than what it leaves behind, or
    there are no fingers on this trader no matter how the rest is tuned.
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))

    for index, arm in enumerate(emitted["arms"]):
        slab, seps, _tips = _hand_parts(emitted, arm)
        assert slab and seps, (
            f"arm {index} has no hand to check; see "
            "test_the_hand_is_a_slab_with_three_finger_separations"
        )
        along = slab["snapped"][3]
        for sep in seps:
            finger = sep["snapped"][3]
            palm = along - finger
            assert finger > palm, (
                f"arm {index}: a {along:.1f}-unit hand keeps {palm:.1f} units "
                f"of palm and cuts only {finger:.1f} units of finger. The "
                "fingers are shallower than the palm they hang off, so they "
                "are notches in a dome rather than prongs -- which is exactly "
                "the mitten this hand was rebuilt to stop being."
            )


@needs_node
def test_the_hands_corner_does_not_dome_over_its_outer_fingers():
    """The corner radius is the other half of the mitten, and it is subtle.

    `litRect`'s radius rounds all four corners, including the two at the
    FINGERTIP end. The arc withdraws the silhouette by up to `r` right where
    the outermost fingers are, so a radius larger than a separation quietly
    removes more of an outer finger than a whole gap does -- and it removes it
    from the silhouette, where the eye reads the digit count. The shipped hand
    carried `CELL` (4) against a 1.5-unit gap, and its outer two fingers
    rounded away into the dome.

    Stated against the gap rather than as a literal: both are art, but a
    corner that eats more than the shadow between two fingers is the
    condition under which the count stops being readable.
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))

    for index, arm in enumerate(emitted["arms"]):
        slab, seps, _tips = _hand_parts(emitted, arm)
        assert slab and seps, (
            f"arm {index} has no hand to check; see "
            "test_the_hand_is_a_slab_with_three_finger_separations"
        )
        gap = min(min(s["snapped"][2:4]) for s in seps)
        # Since increment 18 the hand is an outline, and the rule is stated on
        # the thing the radius stood for: how much shorter the OUTER fingers
        # come out than the longest one, measured at each outer finger's
        # middle. A little is a hand's natural arc; more than a gap is a dome.
        edges = sorted(s["snapped"][0] for s in seps)
        sx, sw = slab["snapped"][0], slab["snapped"][2]
        outer = [(sx + edges[0]) / 2, (edges[-1] + gap + sx + sw) / 2]
        pts = slab["points"]
        longest = max(y for _, y in pts)
        for mid in outer:
            short = longest - _outline_depth_at(pts, mid)
            assert short <= gap + 1e-9, (
                f"arm {index}: the outer finger at {mid:.1f} ends {short:.1f} "
                f"units short of the longest against a {gap:.1f}-unit "
                "separation. The tips round away more of the outer fingers "
                "than the shadow between two fingers does, so the hand ends "
                "in a dome and the count is lost."
            )


@needs_node
def test_the_palm_does_not_swell_out_of_the_back_of_the_wrist():
    """The hand is flush with the BACK of the wrist. It is not centred on it.

    A hand wider than the sleeve has to put its extra somewhere, and it goes
    on the floor side. Centring a 16-unit hand on a 12-unit forearm puts two
    units of palm above the wrist's back edge -- and a hand that swells out of
    the top of the arm it hangs from reads as a dislocation, not as a hand.
    Seen from behind on a thigh, the extra on the floor side is the palm's edge
    lying on the leg, which is where a resting hand spreads.

    This test exists because re-centring the hand SURVIVED every other check
    in this file. The anchoring was stated in a comment and graded by
    nothing, which is the shape of defect this redesign keeps finding -- see
    the notes on mutations that survive.

    Graded against the forearm's own painted capsule rather than against
    `ARM.width`, and which local side is the back is read off the forearm's
    rotation in the room rather than assumed, so the rule survives a pose
    that flips the arm over. (It used to read the back off the side the
    fingers hung from; a straight hand's fingers are on neither side.)
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))

    for index, arm in enumerate(emitted["arms"]):
        slab, seps, _blunt = _hand_parts(emitted, arm)
        capsule = _bone_capsule(emitted["drawn"], arm["foreGid"], emitted["HUMAN"])
        assert slab and seps and capsule, (
            f"arm {index} has no hand to check; see "
            "test_the_hand_is_a_slab_with_three_finger_separations"
        )
        sx, _sy, sw, _sh = slab["snapped"]
        cx, _cy, cw, _ch = capsule["snapped"]
        # Local +x rotated into the room; screen y grows downward, so a
        # positive sine means local +x faces the floor and the back is -x.
        floor_on_plus_x = math.sin(arm["foreWorldRotation"]) > 0

        if floor_on_plus_x:
            back, sleeve_back, side = sx, cx, "-x"
            overhangs = back < sleeve_back - 1e-9
        else:
            back, sleeve_back, side = sx + sw, cx + cw, "+x"
            overhangs = back > sleeve_back + 1e-9

        assert not overhangs, (
            f"arm {index}: the floor is on its forearm's "
            f"{'+x' if floor_on_plus_x else '-x'} side, but the hand's {side} "
            f"edge is at {back:.1f} where the sleeve's is at "
            f"{sleeve_back:.1f}. The palm swells out of the back of the wrist "
            "-- the hand is centred on the bone instead of spreading toward "
            "the floor, which reads as a dislocated joint."
        )


def _bone_point(origin, rotation, width, local):
    """A point in a bone's local frame, carried into the room.

    `boneGfx` parks the Graphics at the joint with its pivot at `(w/2, w/2)`,
    and PIXI maps a local point by `origin + R(rotation) * (local - pivot)`.
    """
    lx, ly = local[0] - width / 2, local[1] - width / 2
    c, s = math.cos(rotation), math.sin(rotation)
    return (origin[0] + lx * c - ly * s, origin[1] + lx * s + ly * c)


@needs_node
def test_the_seated_shrug_lifts_the_forearm_at_the_elbow():
    """Sprint 17 #3 (the user's pick, 2026-09-26): `gesture` — dispatched as
    `shrug` and `turn` — used to swing the whole near arm about the SHOULDER
    by `-0.6u`, which carried the hand BACK along the lap (fingertips
    (-54,-11) -> (-18,+4)) while the comment said "one forearm lifts".

    Graded as the rule, not an instance, over the whole phase: the upper arm
    never moves (no shoulder swing — the desk is in the way); at the peak the
    fingertip is at least a CELL higher than at rest; at every phase it has
    risen more than it has come back toward the body (the hand leaves the
    lap, it does not slide along it); and `restAccents` returns the forearm
    to its rest.
    """
    source = _world_source()
    base = _rig_bone_driver(source)
    driver = (
        base[: base.index("const accents = [];")]
        + _js_block(source, "const EASE = {")
        + ";\n"
        + _js_const(source, "HEAD_TRAVEL")
        + "\nfunction setBlink() {}\n"
        + _js_range(source, "const SEATED_ANIMATIONS = {", "SEATED_ANIMATIONS.turn")
        + "\n"
        + _js_block(source, "function restAccents(")
        + """
        const accents = [];
        seatedRig(new FakeGraphics(), accents);
        for (const g of accents) g.scale = { set() {} };
        // The page's own aliasing (positionCharacters).
        const c = { accents, armNear: accents[0], armFar: accents[1],
                    body: { rotation: 0, x: 0, y: 0 }, head: { rotation: 0, x: 0, y: 0 } };
        const arm = c.armFar, fore = arm.forearm;
        // A point past the wrist on the forearm's axis — where the hand's
        // fingertips are — mapped out through both transforms (PIXI:
        // world = pos + R(rot) * (local - pivot)).
        const map = (g, [x, y]) => {
          const lx = x - g.pivot.x, ly = y - g.pivot.y;
          const cs = Math.cos(g.rotation), sn = Math.sin(g.rotation);
          return [g.x + lx * cs - ly * sn, g.y + lx * sn + ly * cs];
        };
        const tip = () => map(arm, map(fore, [fore.pivot.x, fore.pivot.y + fore.boneLength + 12]));
        const rest = { tip: tip(), upper: arm.rotation };
        const phases = [];
        for (let i = 0; i <= 32; i++) {
          SEATED_ANIMATIONS.shrug(c, i / 32, 1);
          phases.push({ p: i / 32, tip: tip(), upper: arm.rotation });
        }
        // From the PEAK: the cycle ends back at rest on its own (sin(pi) is
        // ~0), so a rest taken there would pass with no reset at all.
        SEATED_ANIMATIONS.shrug(c, 0.5, 1);
        restAccents(c);
        console.log(JSON.stringify({
          rest, phases, depth: arm.depth,
          restored: fore.rotation === fore.restRotation,
        }));
        """
    )
    out = _run_node(driver)
    assert out["depth"] == "near", "the shrugging arm must be the one the viewer sees"
    rest_x, rest_y = out["rest"]["tip"]
    moved_upper = [ph["p"] for ph in out["phases"] if ph["upper"] != out["rest"]["upper"]]
    assert not moved_upper, f"the shrug swings the upper arm at p={moved_upper[:3]}"
    peak = min(ph["tip"][1] for ph in out["phases"])
    assert rest_y - peak >= 4, f"the fingertip rises only {rest_y - peak:.1f} units"
    # An arc about the elbow carries the tip a little back near the top, off
    # the lap; what the old swing did was slide it back ALONG the lap (x +36
    # while y went DOWN 15). So at every phase the hand must have risen more
    # than it has come back.
    slid = [
        ph for ph in out["phases"]
        if ph["tip"][0] - rest_x > 0 and ph["tip"][0] - rest_x >= rest_y - ph["tip"][1]
    ]
    assert not slid, f"the hand slides back along the lap: {slid[:2]}"
    assert out["restored"], "restAccents leaves the forearm lifted after a shrug"


@needs_node
def test_the_near_forearm_rests_on_the_lap():
    """The forearm lies ON the near thigh, and the hand lies there with it.

    The user's call on 2026-09-23, closing the pose question increment 12 left
    open: the arm rests on the lap. Until then the forearm went "forward along
    the armrest" -- an armrest this plate does not paint on the near side --
    and hung six units of clear room above the thigh, with the hand clawing
    down at the knee.

    A BRACKET, so it can fail from both ends. The floor-side edge of the
    forearm at the wrist, and of the hand at its fingertips, must each sit
    between the near thigh's top edge and its centreline: above the top edge
    is the old float, below the centreline is an arm sunk THROUGH the leg.
    And the fingertips may not run off the end of the knee -- a hand past the
    kneecap is hanging off the lap, not resting on it.

    The ORDER is graded too, because it is what makes "on" readable: the
    near arm composites in front of the torso (`depthOrder`) and the legs do
    not, so a forearm lying on the thigh draws over it rather than into it.
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))
    bones = emitted["bones"]
    near = [a for a in emitted["arms"] if a["depth"] == "near"]
    assert len(near) == 1, f"expected one near arm, got {len(near)}"
    arm = near[0]
    # Legs are built far side first, three bones each, so the near thigh is
    # the first bone of the second chain.
    thigh = bones[3]
    assert thigh["depth"] != "near", (
        "the near thigh composites in front of the torso group, so it would "
        "paint OVER a forearm resting on it"
    )

    def end(bone_origin, rotation, length):
        return (
            bone_origin[0] - math.sin(rotation) * length,
            bone_origin[1] + math.cos(rotation) * length,
        )

    hip = (thigh["x"], thigh["y"])
    knee = end(hip, thigh["rotation"], thigh["length"])
    half = thigh["width"] / 2
    # The thigh's TOP is read off the outline it is drawn with, never off
    # `centre - width/2`: a limb tapers toward the knee and bows up at the
    # quad (increment 20), so a constant half-width passes a hand hovering
    # over the real edge near the kneecap.
    assert thigh["outline"] and arm["foreOutline"], (
        "the near thigh and forearm carry no drawn outline to grade the lap "
        "against; see test_a_limb_is_an_outline_that_tapers_into_the_next_one"
    )
    thigh_room = [
        _bone_point(hip, thigh["rotation"], thigh["width"], p)
        for p in thigh["outline"]
    ]

    def thigh_top(x):
        span = _convex_span(thigh_room, x, axis=0)
        assert span, f"x={x:.1f} is off the end of the near thigh"
        return span[0]

    def along_thigh(x):
        return (x - hip[0]) / (knee[0] - hip[0])

    def thigh_centre(x):
        return hip[1] + along_thigh(x) * (knee[1] - hip[1])

    elbow = end((arm["x"], arm["y"]), arm["rotation"], arm["length"])
    r = arm["foreWorldRotation"]
    fw = arm["foreWidth"]
    # The forearm's floor-side edge at the wrist, off its drawn outline too:
    # it narrows toward the wrist, so the capsule's edge is no longer there.
    wrist_y = arm["foreLength"] + fw / 2
    across = _convex_span(arm["foreOutline"], wrist_y)
    floor_x = across[1] if math.sin(r) > 0 else across[0]
    wrist_under = _bone_point(elbow, r, fw, (floor_x, wrist_y))

    slab, _seps, _tips = _hand_parts(emitted, arm)
    assert slab, "the near arm has no hand; see test_the_hand_is_a_slab..."
    sx, sy, sw, sh = slab["snapped"]
    hand_floor_x = sx + sw if math.sin(r) > 0 else sx
    tip_under = _bone_point(elbow, r, fw, (hand_floor_x, sy + sh))

    for name, (px, py) in (("wrist", wrist_under), ("fingertips", tip_under)):
        top, centre = thigh_top(px), thigh_centre(px)
        assert py >= top - 1e-9, (
            f"the underside of the near {name} is at y={py:.1f}, above the "
            f"thigh's top edge at {top:.1f}: the arm hovers "
            f"{top - py:.1f} units over the lap instead of resting on it"
        )
        assert py <= centre + 1e-9, (
            f"the underside of the near {name} is at y={py:.1f}, below the "
            f"thigh's centreline at {centre:.1f}: the arm has sunk through "
            "the leg rather than lying on it"
        )

    reach = along_thigh(tip_under[0])
    assert 0 < reach <= 1 + half / math.dist(hip, knee), (
        f"the near fingertips land {reach:.2f} of the way from hip to knee: "
        "past the end of the kneecap the hand is hanging off the lap"
    )


@needs_node
def test_a_limb_is_an_outline_that_tapers_into_the_next_one():
    """A thigh, a shin, an upper arm and a forearm are LIMBS, not capsules.

    Increment 20, the user's catch: "the knee, where the wider thigh meets
    the shin in a square step; the forearm and cuff, which are still a slab;
    and the flat top of the thigh". A capsule is one width end to end, so a
    21-unit thigh ended on a 16-unit shin in a notch, and every long edge was
    a ruler line. Three rules, all pose-free, so a re-measured chair or a new
    arm angle still answers to them:

    1. At a joint INSIDE a garment (knee, elbow) the parent's width at its far
       end is the child's width at its start -- a curve, not a step. (The
       ankle is a hem over a shoe, where a step is the picture.)
    2. No slab: each of these bones is at least 1.5 units narrower at one end.
    3. The thigh's up-facing edge bows proud of the straight line between its
       two ends -- the quad -- while its underside, on the cushion, does not.

    And every outline is convex with no zero turn, which is what keeps
    `litPath`'s bands inside it (`test_no_path_band_leaves_its_outline`).
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))
    bones, arms = emitted["bones"], emitted["arms"]
    chains = []
    for start in (0, 3):
        thigh, shin = bones[start], bones[start + 1]
        chains.append(("knee", thigh, shin))
    for arm in arms:
        upper = {"outline": arm["outline"], "width": arm["width"],
                 "length": arm["length"]}
        fore = {"outline": arm["foreOutline"], "width": arm["foreWidth"],
                "length": arm["foreLength"]}
        chains.append(("elbow", upper, fore))

    def width_at(bone, y):
        span = _convex_span(bone["outline"], y)
        assert span, f"no outline at y={y}"
        return span[1] - span[0]

    def ends(bone):
        c = bone["width"] / 2
        return width_at(bone, c), width_at(bone, c + bone["length"])

    for joint, parent, child in chains:
        for name, bone in ((f"{joint} parent", parent), (f"{joint} child", child)):
            pts = bone["outline"]
            assert pts, f"the {name} is still a capsule, not a drawn outline"
            n = len(pts)
            turns = [
                (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
                for a, b, c in ((pts[i], pts[(i + 1) % n], pts[(i + 2) % n])
                                for i in range(n))
            ]
            assert all(t > 1e-9 for t in turns) or all(t < -1e-9 for t in turns), (
                f"the {name} outline is not strictly convex; its bands would "
                "leak through the concavity"
            )
        parent_end = ends(parent)[1]
        child_start = ends(child)[0]
        # Half a unit, under one screen pixel at CAST_SCALE 1.5: the hull's
        # tangent meets the smaller disc a little behind its centre, so the
        # width measured AT the joint runs ~0.4 over the disc's. The step this
        # replaced was 4.4 units.
        assert abs(parent_end - child_start) <= 0.5, (
            f"at the {joint} the parent ends {parent_end:.2f} wide and the child "
            f"starts {child_start:.2f} wide: a {abs(parent_end - child_start):.2f}"
            "-unit square step where a joint should be one curve"
        )

    slabs = [("thigh", bones[i]) for i in (0, 3)] + [("shin", bones[i]) for i in (1, 4)]
    slabs += [("forearm", {"outline": a["foreOutline"], "width": a["foreWidth"],
                           "length": a["foreLength"]}) for a in arms]
    for name, bone in slabs:
        a, b = ends(bone)
        assert abs(a - b) >= 1.5, (
            f"a {name} is {a:.2f} wide at one end and {b:.2f} at the other: "
            "a constant-width slab, not a limb"
        )

    for thigh in (bones[0], bones[3]):
        c, length = thigh["width"] / 2, thigh["length"]
        (lo0, hi0), (lo1, hi1) = (_convex_span(thigh["outline"], y) for y in (c, c + length))
        # Which local side is UP: local +x lands on screen at (cos r, sin r).
        up_is_plus = math.sin(thigh["rotation"]) < 0
        proud = {True: 0.0, False: 0.0}
        for k in range(1, 20):
            t = k / 20
            lo, hi = _convex_span(thigh["outline"], c + length * t)
            proud[True] = max(proud[True], hi - (hi0 + t * (hi1 - hi0)))
            proud[False] = max(proud[False], (lo0 + t * (lo1 - lo0)) - lo)
        up, down = proud[up_is_plus], proud[not up_is_plus]
        assert up >= 2, (
            f"the thigh's top stands only {up:.2f} proud of the line between "
            "its ends: a flat top, not a quad"
        )
        assert down <= 0.5, (
            f"the thigh's underside bows {down:.2f} out of line: it is sitting "
            "on the cushion and should run flat along it"
        )


@needs_node
def test_the_sleeve_laps_over_the_wrist_as_a_cuff():
    """The sweater's cuff lies OVER the heel of the hand, and only the heel.

    Increment 20 ("the forearm and cuff are still a slab"): the hand was
    painted over the sleeve, so the join was the palm's own straight heel
    edge laid across a constant-width slab. Now the sleeve is painted after
    the hand and its flattened end laps the palm in a curve.

    A bracket, both ends: the sleeve must reach past the heel (or the hand is
    pasted on again), and must stop well short of the finger separations (or
    it swallows the palm and the hand becomes a fist of fingers).
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))
    drawn, palette = emitted["drawn"], emitted["HUMAN"]
    fingers = json.loads(_js_const(_world_source(), "HAND_OUTLINE").split("=", 1)[1]
                         .strip().rstrip(";"))
    along = max(y for _, y in fingers)
    for index, arm in enumerate(emitted["arms"]):
        on_fore = [d for d in drawn if d["gid"] == arm["foreGid"]]
        order = [d["fill"] for d in on_fore]
        hand = next(d for d in on_fore if d["fill"] == palette["skin"])
        sleeve = _bone_capsule(drawn, arm["foreGid"], palette)
        seps = [d for d in on_fore if d["fill"] == palette["crease"]]
        assert order.index(palette["sweater"]) > order.index(palette["skin"]), (
            f"arm {index}: the sleeve is painted before the hand, so the hand "
            "lies over it and the wrist is the palm's straight heel edge"
        )
        heel = min(y for _, y in hand["points"])
        cuff = max(y for _, y in sleeve["points"])
        first_finger = min(d["snapped"][1] for d in seps)
        overlap = cuff - heel
        assert overlap >= 1, (
            f"arm {index}: the cuff ends {overlap:.2f} units past the heel -- "
            "it does not reach over the hand"
        )
        # And the end is a CURVE: it stands proud of the wrist's own line
        # (the end disc's centre) by at least 1.5 units. A cuff cut flat
        # across the wrist is the straight slab edge this replaced.
        wrist_line = arm["foreWidth"] / 2 + arm["foreLength"]
        assert cuff - wrist_line >= 1.5, (
            f"arm {index}: the cuff bows only {cuff - wrist_line:.2f} past the "
            "wrist -- a flat end, the slab edge again"
        )
        # Half a CELL of palm must show between the cuff and the first finger
        # separation. (The near hand's origin snaps 1.5 units back up the
        # bone, so its cuff covers 6 of 8 palm units -- shot, and it reads as
        # a hand leaving a sleeve. The line that matters is the fingers.)
        assert cuff <= first_finger - 2, (
            f"arm {index}: the cuff reaches y={cuff:.2f}, within 2 units of the "
            f"finger separations at {first_finger:.2f} (heel {heel:.2f}); the "
            f"hand ({along} long) is swallowed by its sleeve"
        )


@needs_node
def test_nothing_blunt_is_painted_at_the_fingertips():
    """The fingertips end on one flat line, and that is CLOSED, not pending.

    The reference staggers its four tips by 1-3 px and this grid cannot: a
    cut's x snaps to the CELL like every coordinate here, so the smallest
    step available is 4 units -- about two finger widths at the size this is
    seen. The stagger was built anyway and shot, because the cost of being
    wrong about art is a screenshot.

    It reproduced the JOINT CAP, for the third time in this redesign. A 4x4
    crease volume takes `paint`'s three banded insets and comes out a dark
    square with internal structure, so the two outer fingers read as stubs
    wearing caps and the count drops from four to "three and some debris".
    Worse, it was legible only against the chair and invisible against the
    dark room -- a silhouette that depends on its backdrop.

    So this grades the ABSENCE, which is the only half worth keeping: every
    crease on this hand is a long thin separation, and nothing blunt is
    painted at the tips. Written as an aspect rule rather than as a count, so
    it fails for any future square blob at the fingertips and not just for
    the two this test was born from.
    """
    emitted = _run_node(_rig_bone_driver(_world_source()))

    for index, arm in enumerate(emitted["arms"]):
        slab, seps, blunt = _hand_parts(emitted, arm)
        assert slab and seps, (
            f"arm {index} has no hand to check; see "
            "test_the_hand_is_a_slab_with_three_finger_separations"
        )
        assert not blunt, (
            f"arm {index} paints {len(blunt)} blunt crease volume(s) on its "
            f"hand: {[b['snapped'] for b in blunt]}. Every crease here is a "
            "finger separation -- long across the hand and gap-thin. A volume "
            "square enough to take `paint`'s insets as a dark object is the "
            "joint cap again, and at the fingertips it costs the digit count."
        )


# --- The MODEL: increment 11, the machine stops being a cluster of candles --
#
# The trader's ten increments all landed on `seatedRig`. This is the other
# half of the same user ask (2026-09-11): *"the model needs to be a robot"*,
# decided the same day as **a plated machine: panelled chassis, visible
# joints, antenna, glowing eyes — no candle glyphs**. That decision overrides
# the reference art, which builds the robot out of teal candlesticks.
#
# `BODIES.bars` is the body every frame of the stream has drawn for MODEL
# (`CAST.MODEL`). Unlike the trader's rig it is a plain function in a plain
# registry, so it runs in node exactly as the page defines it — which is why
# the body comes before the head: it can be GRADED, where the skull fork in
# `character()` is only reachable through the page.


def _machine_body_driver(source: str, run: bool = True) -> str:
    """`BODIES.bars` executed, with every volume and every accent recorded.

    The same shape as `_rig_bone_driver` pointed at the other half of the
    cast: `paint`/`litRect`/`litCircle` are stubs that record rather than
    draw, and `snap` is the page's own, so a volume reports both what the
    call site authored (`at`) and where the CELL grid actually puts it
    (`snapped`). Increment 10 is why both are kept — a separation authored
    3.9 units from its neighbour snaps onto it, and only the snapped rect
    says so.

    The accents come back carrying the fields the animation layer reads off
    them (`gestureMode`, `restRotation`, `shadeFactor`), because for a
    limb-less body those fields ARE the design: a chassis with nothing on
    `accents` is inert, and `gesture()` would have nothing to move.
    """
    return (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + _js_block(source, "const MACHINE = {")
        + ";\n"
        + _js_block(source, "function mixHex(")
        + "\n"
        + """
        let nextId = 0;
        class FakeGraphics {
          constructor() {
            this.gid = nextId++;
            this.pivot = { x: 0, y: 0 };
            this.rotation = 0; this.x = 0; this.y = 0;
            this.scale = { set() {} };
          }
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          roundRect() { return this; } circle() { return this; } poly() { return this; }
          fill() { return this; } stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const drawn = [];
        const litRect = (x, y, w, h, r) => ({
          shape: "rect", at: [x, y, w, h], snapped: [snap(x), snap(y), w, h],
          // Alongside `at`/`snapped` rather than inside them: both are unpacked
          // positionally throughout this file, so a fifth element would
          // re-point every `x, y, w, h = ...` in it. Gradeable because the
          // corner is what domes a hand's finger end over.
          radius: r === undefined ? 0 : r,
          band: (g) => g, rim: (g) => g });
        const litCircle = (cx, cy, rad) => ({
          shape: "circle", at: [cx - rad, cy - rad, 2 * rad, 2 * rad],
          snapped: [snap(cx) - rad, snap(cy) - rad, 2 * rad, 2 * rad],
          band: (g) => g, rim: (g) => g });
        const litPath = (pts, ox = 0, oy = 0) => {
          const [mnx, mny, mxx, mxy] = [Math.min(...pts.map((q) => q[0])), Math.min(...pts.map((q) => q[1])),
                                         Math.max(...pts.map((q) => q[0])), Math.max(...pts.map((q) => q[1]))];
          return {
            shape: "path", at: [ox + mnx, oy + mny, mxx - mnx, mxy - mny],
            snapped: [snap(ox) + mnx, snap(oy) + mny, mxx - mnx, mxy - mny],
            radius: null, points: pts.map(([x, y]) => [snap(ox) + x, snap(oy) + y]),
            band: (g) => g, rim: (g) => g };
        };
        function paint(g, volume, fill, rim, opts) {
          drawn.push({
            gid: g.gid, shape: volume.shape, at: volume.at,
            snapped: volume.snapped, fill, opts: opts || {},
            points: volume.points || null,
          });
        }
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_block(source, "const BODIES = {")
        + ";\n"
        + ("\n" if not run else """
        const accents = [];
        const trunk = new FakeGraphics();
        const headY = BODIES.bars(trunk, accents);
        const limbs = accents.map((g, order) => ({
          gid: g.gid, order, x: g.x, y: g.y,
          // Rest is not zero, and `gesture` falls back to `swing` for an
          // untagged accent, so both are reported as the page RESOLVES them
          // rather than as the page happens to spell them.
          restRotation: g.restRotation ?? 0,
          gestureMode: g.gestureMode ?? "swing",
          shadeFactor: g.shadeFactor ?? 1,
        }));
        console.log(JSON.stringify({
          trunkGid: trunk.gid, headY, limbs, drawn, MACHINE, BODY_FILL,
        }));
        """)
    )


def _touching(a: list, b: list) -> bool:
    """Do two snapped rects share any area, or any edge?

    Abutting counts: two plates flush against each other render as one mass.
    A GAP does not — that is the picket fence.
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax <= bx + bw and bx <= ax + aw and ay <= by + bh and by <= ay + ah


def _inside(inner: list, outer: list) -> bool:
    """Is `inner` wholly within `outer`? Both are snapped `x, y, w, h`."""
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return (
        ix >= ox - 1e-9
        and iy >= oy - 1e-9
        and ix + iw <= ox + ow + 1e-9
        and iy + ih <= oy + oh + 1e-9
    )


def _components(rects: list) -> list:
    """The rects grouped into connected masses, by `_touching`."""
    groups: list[list[int]] = []
    for i in range(len(rects)):
        joined = [g for g in groups if any(_touching(rects[i], rects[j]) for j in g)]
        merged = [i]
        for group in joined:
            merged.extend(group)
            groups.remove(group)
        groups.append(merged)
    return groups


@needs_node
def test_the_machine_is_one_connected_chassis_not_a_cluster_of_bars():
    """The MODEL's body is a single mass, not five free-standing columns.

    Shipped, `BODIES.bars` paints five candlestick rects 18 units apart with
    6 units of clear room between them, plus a stem. At 1:1 that reads as a
    lollipop over a picket fence — a head on a stick above a row of bars that
    touch neither each other nor it. No amount of shading fixes a silhouette
    that is not connected.

    The rule is connectivity of the SNAPPED rects, not a count of volumes or
    a list of part names: a chassis can be authored out of any number of
    plates, and what makes it one machine is that every plate meets another.
    That is pose-independent, and it is what the defect actually is. Abutting
    counts — two plates flush against each other render as one mass — so this
    fails only on a real gap.
    """
    emitted = _run_node(_machine_body_driver(_world_source()))
    trunk = [
        d["snapped"] for d in emitted["drawn"] if d["gid"] == emitted["trunkGid"]
    ]
    assert len(trunk) >= 4, (
        f"the machine's body paints {len(trunk)} volumes on its trunk; a "
        "chassis with a pelvis and two legs is at least four"
    )
    groups = _components(trunk)
    assert len(groups) == 1, (
        f"the machine's trunk falls into {len(groups)} disconnected masses "
        f"{[sorted(g) for g in groups]} — a body whose parts do not touch is "
        "the candle cluster this increment replaces, not a machine"
    )


@needs_node
def test_the_machines_arms_hang_from_shoulders_and_swing():
    """Two arms on `accents`, swinging from a joint near the top of the body.

    This is the constraint that can invalidate the whole redesign, and it is
    not a matter of looks. `gesture()` only ever moves what is on `accents`;
    the candle cluster put five bars there and let the ripple BE the gesture
    (`gestureMode: "pump"`). A plated chassis has no ripple, so if nothing
    replaces those five entries the robot goes inert for every one of `ANIM`'s
    animations — the exact failure the redesign's "detail vs. animation"
    constraint exists to prevent.

    So the arms are the gesture, and they have to be real arms: hung from the
    top of the body rather than rooted at its feet (`GESTURE.swing` rotates an
    accent about its own origin — a bar rooted at the floor swings like a
    felled tree, which is why the cluster pumped instead), mirrored, and not
    dead-symmetrical.
    """
    emitted = _run_node(_machine_body_driver(_world_source()))
    limbs = emitted["limbs"]
    assert len(limbs) == 2, (
        f"the machine puts {len(limbs)} accents on the gesture layer; a "
        "plated robot gestures with two arms"
    )
    trunk = [
        d["snapped"] for d in emitted["drawn"] if d["gid"] == emitted["trunkGid"]
    ]
    top = min(r[1] for r in trunk)
    bottom = max(r[1] + r[3] for r in trunk)
    shoulder_floor = top + (bottom - top) / 2

    for limb in limbs:
        assert limb["gestureMode"] == "swing", (
            f"arm {limb['order']} gestures by {limb['gestureMode']!r}. What "
            "hangs from a joint swings; pump and shift are for bodies that "
            "have no joint to hang from"
        )
        assert limb["y"] < shoulder_floor, (
            f"arm {limb['order']} pivots at y {limb['y']:.0f}, in the lower "
            f"half of a body spanning {top:.0f}..{bottom:.0f}. An accent "
            "rotates about its own origin, so an arm rooted low swings the "
            "whole limb like a felled tree"
        )
        assert limb["shadeFactor"] > 1, (
            f"arm {limb['order']} is painted at the chassis's own value, so a "
            "resting robot reads as one slab and its arms appear only "
            "mid-animation"
        )
        assert any(d["gid"] == limb["gid"] for d in emitted["drawn"]), (
            f"arm {limb['order']} is on the gesture layer with nothing "
            "painted into it"
        )

    left, right = sorted(limbs, key=lambda limb: limb["x"])
    assert left["x"] < 0 < right["x"], (
        f"both arms hang on the same side of the body ({left['x']}, "
        f"{right['x']})"
    )
    assert abs(abs(left["x"]) - abs(right["x"])) < 1e-9, (
        "the shoulders are not mirrored, which reads as a body wearing one "
        "arm further out than the other"
    )
    assert left["restRotation"] != right["restRotation"], (
        "both arms rest at the same angle. Dead-symmetrical limbs read as a "
        "mannequin, and the asymmetry costs two numbers"
    )


@needs_node
def test_the_machines_plates_are_divided_by_cuts_that_keep_their_own_colour():
    """"Visible joints" and "panelled chassis" are CUTS, never small volumes.

    The joint cap is the standing precedent, and it is why this is a rule
    rather than a preference: `paint` lays three banded insets into every
    volume, and on a small shape those come out as a dark ring round a pale
    core — a bolt. Four finger volumes would have repeated it eight times per
    figure (increment 10), and a robot authored as a stack of little plates
    and pins would repeat it everywhere. One slab with the divisions cut into
    it keeps the bands reading as shading.

    A cut is `{ keepFill, matte }` for the reason `shod` and the hand's
    creases are: `shade` mixes an absolute 0.3 toward `LIGHT.warmth` on the
    lit rung, and a seam lifted toward the lamp reads as a scratch ON the
    plate rather than a gap BETWEEN two.

    And the complement, which is the one place the machine parts company with
    the trader: **the chassis itself stays on the mood fill.** A human cannot
    be recoloured and so stopped carrying mood in his body (increment 8), but
    a machine can, and the MODEL's mood is the live one — `react()` moods it
    from real model events, where `draw()` re-pins the trader to `idle` every
    update. So only the cuts are authored; every plate takes `BODY_FILL` and
    lets `repaint` carry the mood onto it.
    """
    emitted = _run_node(_machine_body_driver(_world_source()))
    seam_fill = emitted["MACHINE"]["seam"]
    cuts = [d for d in emitted["drawn"] if d["fill"] == seam_fill]
    plates = [d for d in emitted["drawn"] if d["fill"] != seam_fill]

    assert len(cuts) >= 4, (
        f"the machine cuts {len(cuts)} seams into itself. A panelled chassis "
        "plus a visible joint on each arm is at least four"
    )
    by_gid: dict[int, int] = {}
    for cut in cuts:
        by_gid[cut["gid"]] = by_gid.get(cut["gid"], 0) + 1
    assert emitted["trunkGid"] in by_gid, (
        "every seam is on a limb and none on the chassis, which is the volume "
        "the word 'panelled' is about"
    )
    for limb in emitted["limbs"]:
        assert by_gid.get(limb["gid"]), (
            f"arm {limb['order']} carries no joint cut, so it reads as one "
            "smooth tube — the mitten, one limb up"
        )

    for plate in plates:
        assert plate["fill"] == emitted["BODY_FILL"], (
            "a plate is painted in an authored colour. The machine's mood IS "
            "its body colour and that mood is live, so a plate takes "
            "BODY_FILL and lets `repaint` carry the mood onto it"
        )
        assert not plate["opts"].get("keepFill"), (
            "a plate is `keepFill`, which pins it to one colour for the life "
            "of the stream and takes its rim away. That is the TRADER's rule "
            "— a person is not outlined and cannot be recoloured — and a "
            "machine is neither"
        )

    for cut in cuts:
        assert cut["opts"].get("keepFill") and cut["opts"].get("matte"), (
            f"a seam at {cut['at']} is not `{{ keepFill, matte }}`: without "
            "keepFill it takes the mood and is outlined, and without matte "
            "its lit rung is mixed 0.3 toward the lamp's hue, which reads as "
            "a scratch on the plate instead of a gap between two"
        )
        host = next(
            (
                p
                for p in plates
                if p["gid"] == cut["gid"] and _inside(cut["snapped"], p["snapped"])
            ),
            None,
        )
        assert host, (
            f"a seam at {cut['snapped']} is not contained in any plate on its "
            "own Graphics: it is cut out of thin air rather than out of "
            "something"
        )
        cut_w, cut_h = cut["snapped"][2], cut["snapped"][3]
        host_w, host_h = host["snapped"][2], host["snapped"][3]
        assert min(cut_w, cut_h) < min(host_w, host_h), (
            f"a seam is {cut_w:.0f}x{cut_h:.0f} inside a "
            f"{host_w:.0f}x{host_h:.0f} plate. A seam is a LINE across a "
            "plate; one as thick as its host is a second plate wearing a "
            "seam's colour"
        )
        for axis, along in ((0, "across"), (1, "down")):
            offset = cut["at"][axis] - host["at"][axis]
            assert offset % CELL_U == pytest.approx(0, abs=1e-6), (
                f"a seam sits {offset:.2f} units {along} its plate, which is "
                f"not a whole {CELL_U}-unit CELL. `litRect` snaps both "
                "coordinates, so only a whole cell survives — an offset that "
                "happens to round correctly at this pose collapses two seams "
                "into one at the next, and nothing in the drawn output says so"
            )


# --- The MODEL's head (increment 13) ---------------------------------------
#
# The body went first because it could be graded and the head could not: the
# skull was built inline in `character()`. The box head is a builder in a
# style-keyed registry (`MACHINE_HEADS`, the shape of `BODIES`) precisely so it
# runs in node here, and its geometry lives in `MACHINE.head`, which both
# preludes already carry.
#
# The hazard the head was deferred over is WIRING, not looks: `machineFace`'s
# visor and `machineEyes` are separate Graphics authored against a 56-unit
# circle and added after the skull. A head of a different shape mis-registers
# them silently — nothing errors, the eyes just sit on the side of the box.


def _machine_head_driver(source: str) -> str:
    """The box head built, and the machine face swept over every mood.

    `machineFace` is run for valence -1..1 and arousal 0..1 — arousal 0 is
    also what a blink passes to `machineEyes` — with recorder doubles for the
    feature Graphics, so every rect and every mouth vertex the face can ever
    draw comes back in head-local units alongside the head's own volumes.
    """
    return (
        _machine_body_driver(source, run=False)
        + _js_block(source, "function lightenTint(") + "\n"
        + _js_block(source, "function machineFace(") + "\n"
        + _js_block(source, "function machineEyes(") + "\n"
        + _js_block(source, "function boxHead(") + "\n"
        + _js_block(source, "const MACHINE_HEADS = {") + ";\n"
        + """
        function recorder() {
          const calls = [];
          return {
            calls,
            clear() { calls.length = 0; return this; },
            roundRect(x, y, w, h) { calls.push({ box: [x, y, w, h] }); return this; },
            poly(f) { const xs = f.filter((_, i) => i % 2 === 0), ys = f.filter((_, i) => i % 2 === 1); return this.roundRect(Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0); },
            moveTo(x, y) { calls.push({ pt: [x, y] }); return this; },
            lineTo(x, y) { calls.push({ pt: [x, y] }); return this; },
            fill() { return this; },
            stroke(s) { calls.push({ stroke: s.width }); return this; },
          };
        }
        const skull = new FakeGraphics();
        const beacon = recorder();
        const crown = MACHINE_HEADS.bars(skull, beacon);
        const features = [];
        function collect(g, part) {
          const width = (g.calls.find((c) => c.stroke) || { stroke: 0 }).stroke;
          for (const c of g.calls) {
            if (c.box) features.push({ part, box: c.box });
            if (c.pt) features.push({ part, box: [
              c.pt[0] - width / 2, c.pt[1] - width / 2, width, width] });
          }
        }
        for (const valence of [-1, -0.5, 0, 0.5, 1]) {
          for (const arousal of [0, 0.25, 0.5, 0.75, 1]) {
            const char = { style: "bars" };
            for (const k of ["visor", "eyeL", "eyeR", "mouth", "browL", "browR"]) {
              char[k] = recorder();
            }
            machineFace(char, valence, arousal, 0x4db6ac);
            for (const k of ["visor", "eyeL", "eyeR", "mouth", "browL", "browR"]) {
              collect(char[k], k);
            }
            machineEyes(char, 0);
            collect(char.eyeL, "eyeL"); collect(char.eyeR, "eyeR");
          }
        }
        const trunk = new FakeGraphics();
        BODIES.bars(trunk, []);
        console.log(JSON.stringify({
          skullGid: skull.gid, trunkGid: trunk.gid, crown, drawn, features,
          beacon: beacon.calls.filter((c) => c.box).map((c) => c.box),
          MACHINE, BODY_FILL, headStyles: Object.keys(MACHINE_HEADS),
        }));
        """
    )


def _snapped_rect(rect: list) -> list:
    x, y, w, h = rect
    return [round(x / CELL_U) * CELL_U, round(y / CELL_U) * CELL_U, w, h]


def _head_parts(emitted: dict) -> tuple[list, list]:
    """The head's plates and its seams, as drawn on the skull."""
    seam = emitted["MACHINE"]["seam"]
    mine = [d for d in emitted["drawn"] if d["gid"] == emitted["skullGid"]]
    return (
        [d for d in mine if d["fill"] != seam],
        [d for d in mine if d["fill"] == seam],
    )


@needs_node
def test_the_machines_head_is_a_box_not_the_shared_ball():
    """The MODEL's skull is plates, not `litCircle(0, 0, 28)`.

    Until increment 13 every machine style wore the same ball, which is what
    made the MODEL read as a figure with a bauble on it rather than as a robot.
    The reference's head is an extruded box; so is this one.
    """
    emitted = _run_node(_machine_head_driver(_world_source()))
    plates, _ = _head_parts(emitted)
    assert plates, "the box head paints nothing onto the skull"
    shapes = {d["shape"] for d in plates}
    assert "circle" not in shapes, (
        f"the MODEL's head is built from {sorted(shapes)} — a machine head is "
        "plates, and a circle among them is the shared ball back again"
    )


@needs_node
def test_every_face_feature_lands_on_the_front_face():
    """The visor, the eyes and the mouth stay on the FACE of the box.

    The silhouette is the wrong thing to grade: a box with visible depth has
    a side and a top, and an eye that lands on the side strip is exactly the
    mis-registration this head was deferred over. So the features are graded
    against the front plate — `MACHINE.head`'s own rect, which must also be a
    plate the builder actually paints — at every mood the face can take,
    including the blink, and with the mouth's stroke width counted.
    """
    emitted = _run_node(_machine_head_driver(_world_source()))
    head = emitted["MACHINE"]["head"]
    front = _snapped_rect([head["x"], head["y"], head["w"], head["h"]])
    plates, _ = _head_parts(emitted)
    assert any(d["snapped"] == front for d in plates), (
        f"`MACHINE.head` describes a front face at {front} that the builder "
        "never paints — the constant the face is graded against is not the "
        "head on screen"
    )
    stray = [
        f for f in emitted["features"] if not _inside(f["box"], front)
    ]
    assert not stray, (
        f"{len(stray)} face features fall off the front face {front}, e.g. "
        f"{stray[0]} — an eye or a visor on the side of the box"
    )


@needs_node
def test_the_head_shows_one_depth_on_its_top_and_one_side():
    """The box is extruded, and extruded ONE way.

    An oblique box shows its top and one side, by the same depth — two
    different depths is two boxes, and a back plate centred behind the front
    is a frame, not a solid. The reference's head shows its side face on the
    viewer's LEFT (`ref-model.png`, the same room and lamp), so the depth
    grows up and to the left.
    """
    emitted = _run_node(_machine_head_driver(_world_source()))
    head = emitted["MACHINE"]["head"]
    front = _snapped_rect([head["x"], head["y"], head["w"], head["h"]])
    plates, _ = _head_parts(emitted)
    # The box is every plate but the antenna, which stands wholly above the
    # top face's back edge.
    rects = [
        d["snapped"] for d in plates
        if d["snapped"][1] + d["snapped"][3] > front[1] - head["depth"]
    ]
    assert len(rects) >= 2, (
        "the head paints no plate behind its face, so it has no depth at all"
    )
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    left, top = front[0] - x0, front[1] - y0
    right, bottom = x1 - (front[0] + front[2]), y1 - (front[1] + front[3])
    assert left > 0 and top > 0, (
        f"the head shows depth left {left} / top {top}: a box with no visible "
        "side is a flat plate, the ball traded for a tile"
    )
    assert left == top, (
        f"the head's side is {left} deep and its top {top}. One box has one "
        "depth"
    )
    assert right == 0 and bottom == 0, (
        f"the head also overhangs its face on the right ({right}) / bottom "
        f"({bottom}): a plate centred behind the face is a picture frame"
    )


@needs_node
def test_the_heads_depth_meets_its_face_at_the_corners():
    """The top and the side recede FROM the face's corners, on a diagonal.

    Increment 13 drew the depth as one rounded plate the face's size, offset
    up-left. That shows a top strip ending square a whole depth short of the
    face's top-right corner, and a side strip ending short of its bottom-left:
    two cards laid askew, not one box (the 2026-09-24 presentability pass).
    An oblique box's receding edges run from the face's own corners, so the
    top face must reach the face's top-RIGHT corner and the side face its
    bottom-LEFT one — which only a slanted face can do.
    """
    emitted = _run_node(_machine_head_driver(_world_source()))
    head = emitted["MACHINE"]["head"]
    fx, fy, fw, fh = _snapped_rect([head["x"], head["y"], head["w"], head["h"]])
    d = head["depth"]
    plates, _ = _head_parts(emitted)
    faces = [p for p in plates if p["shape"] == "path" and p["points"]]
    corners = {tuple(pt) for p in faces for pt in p["points"]}
    for name, corner in {
        "top-right": (fx + fw, fy),
        "bottom-left": (fx, fy + fh),
        "top-right, receded": (fx + fw - d, fy - d),
        "bottom-left, receded": (fx - d, fy + fh - d),
    }.items():
        assert corner in corners, (
            f"no depth face has a vertex at the face's {name} corner {corner}: "
            "the extrusion steps instead of receding on a diagonal"
        )


@needs_node
def test_the_machines_mouth_is_small_on_its_visor():
    """The mouth is a small readout under the eyes, not a grin across the
    visor (user, 2026-09-24: "make his mouth smaller"). At its widest — full
    arousal, stroke counted — it spans at most half the visor, at every mood.
    """
    emitted = _run_node(_machine_head_driver(_world_source()))
    visor = [f["box"] for f in emitted["features"] if f["part"] == "visor"][0]
    mouth = [f["box"] for f in emitted["features"] if f["part"] == "mouth"]
    assert mouth, "the machine face draws no mouth"
    span = max(b[0] + b[2] for b in mouth) - min(b[0] for b in mouth)
    assert span <= visor[2] / 2, (
        f"the mouth spans {span} units on a {visor[2]}-unit visor"
    )


@needs_node
def test_the_machine_stands_on_feet():
    """Each leg ends in a foot wider than the leg, flat on the floor.

    A leg that ends as a bare stick reads as a figure on stilts, not as a
    machine standing in the room. A foot is a trunk plate whose bottom is
    the floor (y 0), which overlaps a leg and is wider than it.
    """
    emitted = _run_node(_machine_body_driver(_world_source()))
    trunk = [
        d["snapped"] for d in emitted["drawn"]
        if d["gid"] == emitted["trunkGid"] and d["fill"] == emitted["BODY_FILL"]
    ]
    on_floor = [r for r in trunk if abs(r[1] + r[3]) < 1e-9]
    legs = [r for r in on_floor if r[3] > r[2]]
    assert len(legs) == 2, f"expected two legs on the floor, found {legs}"
    for leg in legs:
        feet = [
            r for r in on_floor
            if r[2] > leg[2] and r[3] < r[2] and _touching(r, leg)
            and r[0] < leg[0] + leg[2] and leg[0] < r[0] + r[2]
        ]
        assert feet, f"the leg at {leg} ends on the floor with no foot"


@needs_node
def test_the_heads_face_is_cut_from_its_depth():
    """A seam separates the face from the side and top it stands in front of.

    Both are `BODY_FILL`, and `paint` bands every volume toward the one lamp,
    so without a cut the side face comes out the same value as the front and
    the extrusion reads as a notched silhouette. The reference draws a dark
    gap there. Cuts follow the body's rule — `{ keepFill, matte }` in
    `MACHINE.seam` — and plates stay on the mood fill, because the MODEL's mood
    is the live one.
    """
    emitted = _run_node(_machine_head_driver(_world_source()))
    head = emitted["MACHINE"]["head"]
    fx, fy, fw, fh = _snapped_rect([head["x"], head["y"], head["w"], head["h"]])
    plates, cuts = _head_parts(emitted)
    for plate in plates:
        assert plate["fill"] == emitted["BODY_FILL"] and not plate["opts"].get(
            "keepFill"
        ), f"a head plate at {plate['at']} is not on the mood fill"
    for cut in cuts:
        assert cut["opts"].get("keepFill") and cut["opts"].get("matte"), (
            f"a head seam at {cut['at']} is not `{{ keepFill, matte }}`"
        )
    edges = {"left": False, "top": False}
    for cut in cuts:
        cx, cy, cw, ch = cut["snapped"]
        if cx == fx and cw < ch and cy <= fy and cy + ch >= fy + fh:
            edges["left"] = True
        if cy == fy and ch < cw and cx <= fx and cx + cw >= fx + fw:
            edges["top"] = True
    assert all(edges.values()), (
        f"the face is not cut from its depth along {edges}: a seam must run "
        "the full length of the face's edge on each side the depth shows"
    )


@needs_node
def test_the_antenna_stands_on_the_head_and_the_beacon_on_the_antenna():
    """"Antenna" is in the user's own spec (2026-09-11), and it must be
    attached: the body's connectivity rule, one storey up. A mast that floats
    a cell above the crown is a second object, and a beacon off the mast's tip
    is a star behind the robot.

    The beacon is drawn on its own Graphics, not the skull, because `repaint`
    clears the skull on every mood change and replays only what `paint`
    recorded — a plain lit fill on the skull would vanish the first time the
    MODEL changed mood.
    """
    emitted = _run_node(_machine_head_driver(_world_source()))
    plates, _ = _head_parts(emitted)
    beacon = [_snapped_rect(b) for b in emitted["beacon"]]
    assert beacon, "the head carries no beacon"
    rects = [d["snapped"] for d in plates] + beacon
    groups = _components(rects)
    assert len(groups) == 1, (
        f"the head, its antenna and its beacon fall into {len(groups)} "
        "separate masses"
    )
    crown = min(r[1] for r in rects)
    body = [d["snapped"] for d in plates]
    assert crown < min(r[1] for r in body[:2]), (
        "nothing stands above the box — the antenna is missing"
    )
    head = emitted["MACHINE"]["head"]
    top_mid = head["x"] - head["depth"] + head["w"] / 2
    above = [r for r in rects if r[1] + r[3] <= head["y"] - head["depth"]]
    for r in above:
        mid = r[0] + r[2] / 2
        assert abs(mid - top_mid) <= CELL_U / 2, (
            f"the antenna stands at x {mid} on a top face centred at "
            f"{top_mid} — off to one edge it reads as a handle, not a mast"
        )
    assert emitted["crown"] == crown, (
        f"the builder reports its crown at {emitted['crown']} but draws up to "
        f"{crown}; the mood tag is placed from that number"
    )


@needs_node
def test_the_machines_head_is_no_wider_than_its_chassis():
    """Width budget: the head's silhouette fits the chassis under it.

    The human rule from increment 1 ("a head wider than the shoulders is the
    loudest not-a-person cue"), turned for a machine: here the chassis, not
    the shoulders, is the budget, because the shoulders exist only to hang the
    arms from. Depth counts — the side face is part of the silhouette.
    """
    emitted = _run_node(_machine_head_driver(_world_source()))
    plates, _ = _head_parts(emitted)
    head = [d["snapped"] for d in plates]
    head_w = max(r[0] + r[2] for r in head) - min(r[0] for r in head)
    trunk = [
        d["snapped"]
        for d in emitted["drawn"]
        if d["gid"] == emitted["trunkGid"] and d["fill"] == emitted["BODY_FILL"]
    ]
    chassis_w = max(r[2] for r in trunk)
    assert head_w <= chassis_w, (
        f"the MODEL's head is {head_w} units across on a {chassis_w}-unit "
        "chassis"
    )


def _badge_box(badge: dict) -> list:
    """The rect a `MACHINE.badge` covers, in rig units: centred on its
    `x`/`y`, one `size` tall, and as wide as a monospace line at 0.62 of a
    glyph's size per character — the page draws it in `monospace`, which
    every stack in use advances at 0.60-0.62 em."""
    w = len(badge["text"]) * badge["size"] * 0.62
    return [badge["x"] - w / 2, badge["y"] - badge["size"] / 2, w, badge["size"]]


@needs_node
def test_the_model_wears_its_version_on_a_clean_plate():
    """"model v0.2" is stencilled on the MODEL, on ONE plate, crossing no seam.

    The user's call (2026-09-24), made with the floating MODEL/TRADER names
    coming off the room. Graded against the plates `BODIES.bars` actually
    draws, not a remembered pelvis rect: the lettering must sit wholly inside
    one body-coloured plate and touch no seam, so a chassis re-cut that runs
    a seam through it fails here rather than on the stream.
    """
    emitted = _run_node(_machine_body_driver(_world_source()))
    badge = emitted["MACHINE"]["badge"]
    assert badge["text"] == "model v0.2"
    box = _badge_box(badge)
    plates = [d["snapped"] for d in emitted["drawn"]
              if d["gid"] == emitted["trunkGid"] and d["fill"] == emitted["BODY_FILL"]]
    seams = [d["snapped"] for d in emitted["drawn"]
             if d["fill"] == emitted["MACHINE"]["seam"]]
    assert any(_inside(box, p) for p in plates), (
        f"the badge {box} overhangs every plate {plates}"
    )
    as_rect = lambda r: dict(zip("xywh", r))  # noqa: E731
    crossed = [s for s in seams if _rects_intersect(as_rect(box), as_rect(s))]
    assert not crossed, f"a seam {crossed} runs through the badge {box}"


@needs_node
def test_the_badge_moves_with_the_body_it_is_painted_on():
    """The badge is a sibling of `body`, not a child (a Pixi 8 Graphics is a
    leaf), so it must COPY the body's transform every frame. Several writers
    move `body` (breathe, lean, shift, slump); a badge parked at rest would
    slide off its plate on the first breath."""
    source = _world_source()
    emitted = _run_node(
        _js_block(source, "const MACHINE = {") + ";\n"
        + """
        class Node {
          constructor() { this.x = 0; this.y = 0; this.rotation = 0;
                          this.children = []; this.anchor = { set() {} }; }
          addChild(...c) { this.children.push(...c); return c[0]; }
        }
        const PIXI = { Container: Node, Text: class extends Node {
          constructor(o) { super(); this.text = o.text; this.style = o.style; } } };
        """
        + _js_block(source, "function modelBadge(") + "\n"
        + """
        const body = new Node();
        const badge = modelBadge(body);
        body.x = 4; body.y = -4; body.rotation = 0.16;
        badge.onRender();
        const text = badge.children[0];
        console.log(JSON.stringify({ x: badge.x, y: badge.y, r: badge.rotation,
                                     text: text.text, tx: text.x, ty: text.y }));
        """
    )
    assert (emitted["x"], emitted["y"], emitted["r"]) == (4, -4, 0.16)
    assert emitted["text"] == "model v0.2"
    body = _without_comments(_js_block(source, "function character("))
    assert re.search(r'style === "bars" \? modelBadge\(body\)', body), (
        "`character()` never stencils the MODEL's badge"
    )
    assert re.search(r"rig\.addChild\([^;]*\bbadge\b", body), (
        "the badge is built but never added to the rig"
    )


def test_the_room_names_nobody_in_the_air():
    """The MODEL/TRADER nameplates are gone (user, 2026-09-24): the trader's
    sat ~190px from him on the desk front, and the MODEL now wears its own
    name. Neither the manifest nor the page may bring one back."""
    ids = [t["id"] for t in _manifest()["text"]]
    assert not [i for i in ids if i.startswith("name-")], ids
    boot = _without_comments(_js_block(_world_source(), "async function boot("))
    assert "name-trader" not in boot and "name-model" not in boot


@needs_node
def test_only_the_model_wears_the_box_head():
    """`MACHINE_HEADS` forks the MODEL alone, and `character()` reads it.

    `FACE_KIND` puts candle and monolith on the machine face too, and they are
    gallery bodies with their own proportions; they keep the ball. The fork
    itself lives in `character()`, which node cannot run, so its call is
    pinned in the source — with comments stripped, because the comments
    around it name the registry too (increment 11's green-on-prose trap).
    """
    emitted = _run_node(_machine_head_driver(_world_source()))
    assert emitted["headStyles"] == ["bars"], (
        f"the box head is registered for {emitted['headStyles']}"
    )
    body = _without_comments(_js_block(_world_source(), "function character("))
    assert re.search(r"MACHINE_HEADS\[style\]\(skull, beacon\)", body), (
        "`character()` never builds a registered machine head"
    )
    assert "litCircle(0, 0, 28)" in body, (
        "the ball the other machine bodies wear is gone from `character()`"
    )


@needs_node
def test_the_two_thighs_read_as_two_legs_not_one_mass():
    """Both thighs draw at a comparable length, and neither is a lump.

    The trace settles this and it is scale-free: the reference's two thighs
    project to 77.8 and 77.7 px — within a pixel of each other, because the
    man's legs are nearly PARALLEL (8 degrees apart), both extended forward
    with the knees close together.

    The rig splayed its thighs 47 degrees apart (lateral +/-0.40). Turned
    132 degrees, that pointed one of them into the screen, and it came out
    26.6 units long against its twin's 54.2 — and only 1.3x its own width, so
    it drew as a square blob at the hip rather than as a leg. That is most of
    what "the legs read as one mass going left" was.

    Two claims, because either alone has a hole: a ratio between the twins
    (catches the splay) and an aspect ratio per bone (catches a thigh that is
    short because it is SHORT, which the ratio alone would pass if both were).
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + _js_block(source, "const HUMAN = {")
        + ";\n"
        + _js_block(source, "function mixHex(")
        + "\n"
        + """
        class FakeGraphics {
          constructor() {
            this.calls = []; this.children = [];
            this.x = 0; this.y = 0;
            this.pivot = { x: 0, y: 0 }; this.rotation = 0;
          }
          addChild(c) { this.children.push(c); return c; }
          roundRect() { return this; } circle() { return this; } poly() { return this; }
          fill() { return this; } stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const litRect = (x, y, w, h, r) => ({ band: (g) => g, rim: (g) => g });
        const litCircle = (cx, cy, rad) => ({ band: (g) => g, rim: (g) => g });
        const litPath = () => ({ band: (g) => g, rim: (g) => g });
        function paint(g, volume, fill, rim, opts) {}
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + "\n"
        + _seated_rig_block(source)
        + "\n"
        + """
        const accents = [];
        seatedRig(new FakeGraphics(), accents);
        // Bones are pushed thigh, shin, foot per leg, far leg first.
        const legs = accents.filter((a) => a.limb === "leg");
        console.log(JSON.stringify(legs.map((b) => ({
          length: b.boneLength, width: b.boneWidth,
        }))));
        """
    )
    bones = _run_node(driver)
    assert len(bones) == 6, f"expected six leg bones, got {len(bones)}"
    thighs = [bones[0], bones[3]]

    shorter = min(t["length"] for t in thighs)
    longer = max(t["length"] for t in thighs)
    assert shorter / longer >= 0.6, (
        f"one thigh draws {shorter:.0f} units and the other {longer:.0f} "
        f"({shorter / longer:.2f}): the reference's two thighs project within "
        "a pixel of each other because its legs are nearly parallel, so this "
        "much disparity means they are splayed apart and one is pointing at "
        "the camera"
    )
    for index, thigh in enumerate(thighs):
        aspect = thigh["length"] / thigh["width"]
        assert aspect >= 1.5, (
            f"thigh {index} draws {thigh['length']:.0f} long by "
            f"{thigh['width']:.0f} wide ({aspect:.1f}:1): that is a lump at "
            "the hip, not a leg"
        )


@needs_node
def test_a_mood_change_reaches_a_limb_parented_to_another_limb():
    """The forearm is a child, and `applyMood` only walks `char.accents`.

    `repaint` used to return early on any Graphics with no `volumes` of its
    own and never looked at its children, so the moment a limb grew a child
    limb, half a sleeve would keep the previous mood's colour. It shows up
    only on a mood change, which is the kind of seam that ships.
    """
    source = _world_source()
    driver = (
        _js_block(source, "function mixHex(")
        + "\n"
        + """
        const painted = [];
        function paint(g, volume, fill, rim, opts) {
          (g.volumes || (g.volumes = [])).push(
            opts && opts.keepFill ? { volume, fill } : volume);
          painted.push({ g: g.name, fill });
        }
        """
        + _js_block(source, "function repaint(")
        + "\n"
        + """
        const parent = { name: "upper", clear() {}, children: [] };
        const child = { name: "fore", clear() {}, children: [] };
        parent.children.push(child);
        paint(parent, {}, 0x111111);
        paint(child, {}, 0x222222);
        painted.length = 0;
        repaint(parent, 0x00ff00, 0x00ff00);
        console.log(JSON.stringify(painted.map((p) => p.g)));
        """
    )
    touched = _run_node(driver)
    assert "fore" in touched, (
        "repaint did not descend into the child limb: a mood change would "
        f"re-light the upper arm and leave the forearm behind (touched: {touched})"
    )


@needs_node
def test_the_seated_figure_points_where_the_painted_cushion_points():
    """He faces the way the chair does, and the chair's own edges say which way.

    The direction is NOT eyeballed off the picture, and the picture is exactly
    what misleads here: the backrest is painted up-and-right of the cushion,
    which reads as "the back is at the back" and would point the chair
    down-left, out into the room. `measure_seat_axes` settles it off the
    backrest's crown line — parallel to the cushion's upper-left edge, so the
    slab stands on the edge parallel to THAT, the lower-right one it hides
    itself — and the chair faces up-left, toward the desk.

    What is asserted is the rig's own drawn geometry, not the constant:

    * the HIP LINE is perpendicular to the facing. Both hips sit at the same
      height, so their screen offset un-foreshortens exactly (no `up` term to
      contaminate it) and this is the one clean read of the body's yaw.
    * the KNEES lie on the facing side of the hips, by most of a thigh. That
      is what fails if the hips turn and the legs do not — a figure sitting
      square in a chair with its shoulders twisted.

    Both are measured in the flat-ground metric (`y / SIN_PHI`), which is the
    only space in which "perpendicular" means anything on a plate drawn in
    three-quarter view.
    """
    from tools.measure_chair import SIN_PHI

    source = _world_source()
    facing = _manifest()["cast"]["trader"]["facing"]
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + """
        class FakeGraphics {
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          constructor() {
            this.calls = []; this.x = 0; this.y = 0;
            this.pivot = { x: 0, y: 0 }; this.rotation = 0;
          }
          roundRect(x, y, w, h, r) { this.calls.push([x, y, w, h]); return this; }
          circle() { return this; } poly() { return this; }
          fill() { return this; }
          stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const fakeGfx = { roundRect() { return this; }, circle() { return this; }, poly() { return this; },
                           fill() { return this; }, stroke() { return this; } };
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _js_const(source, "CAST_SCALE")
        + "\n"
        + _shading_prelude(source)
        + _seated_rig_block(source)
        + "\n"
        + """
        const accents = [];
        seatedRig(fakeGfx, accents);
        // Every third leg bone is a thigh: `bone()` is called hip->knee,
        // knee->ankle, ankle->toe per leg, and pushes each onto `accents`.
        const legs = accents.filter((a) => a.limb === "leg");
        const thighs = legs.filter((_, i) => i % 3 === 0).map((t) => {
          // A bone's pose IS its rotation, and it is drawn along its own +y
          // from the pivot, so the knee is the far end of that axis.
          // `boneLength` is the joint-to-joint length `boneGfx` drew it at;
          // it used to be recovered from the capsule's roundRect, and a limb
          // is a convex outline now (increment 20).
          const len = t.boneLength;
          return {
            hip: { x: t.x, y: t.y },
            knee: {
              x: t.x - Math.sin(t.restRotation) * len,
              y: t.y + Math.cos(t.restRotation) * len,
            },
            len,
          };
        });
        console.log(JSON.stringify({ thighs }));
        """
    )
    emitted = _run_node(driver)
    thighs = emitted["thighs"]
    assert len(thighs) == 2, f"expected two thighs, got {len(thighs)}"

    def ground(dx: float, dy: float) -> tuple[float, float]:
        return (dx, dy / SIN_PHI)

    def angle_between(a, b) -> float:
        dot = a[0] * b[0] + a[1] * b[1]
        return math.degrees(
            math.acos(max(-1.0, min(1.0, dot / (math.hypot(*a) * math.hypot(*b)))))
        )

    face = ground(*facing)
    hips = ground(
        thighs[0]["hip"]["x"] - thighs[1]["hip"]["x"],
        thighs[0]["hip"]["y"] - thighs[1]["hip"]["y"],
    )
    assert abs(angle_between(hips, face) - 90) <= 6, (
        f"the hip line is {angle_between(hips, face):.0f} degrees from the "
        "chair's facing, not the 90 a seated body's hips make with it — the "
        "figure is not turned with the chair"
    )

    unit = (face[0] / math.hypot(*face), face[1] / math.hypot(*face))
    for thigh in thighs:
        reach = ground(
            thigh["knee"]["x"] - thigh["hip"]["x"],
            thigh["knee"]["y"] - thigh["hip"]["y"],
        )
        forward = reach[0] * unit[0] + reach[1] * unit[1]
        assert forward >= 0.6 * thigh["len"], (
            f"a thigh reaches {forward:.0f} units along the chair's facing out "
            f"of {thigh['len']:.0f}: the hips turned and the legs did not"
        )


def test_the_pages_fallback_facing_is_the_measured_one():
    """A fallback that disagrees with the manifest is a figure only the tests
    can see.

    `seatedRig` takes the facing as an argument and falls back to a literal
    when there is no plate — the `CAST_SCALE` pattern — and every node driver
    in this file runs with `PLATE = null`, so that literal is the pose the
    whole suite grades. If it drifts from the manifest, the suite measures one
    figure and the stream shows another. (`test_the_scale_is_manifest_data_
    not_a_page_constant` makes the same check for the cast scale.)
    """
    source = _world_source()
    literal = re.search(r"const FACE_DIR = facing \|\| \[([-\d., ]+)\];", source)
    assert literal, "seatedRig no longer carries a measured facing fallback"
    assert [float(n) for n in literal.group(1).split(",")] == [
        float(n) for n in _manifest()["cast"]["trader"]["facing"]
    ], "the page's fallback facing is not the one the manifest measures"


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

    **The character redesign split this invariant, which is what
    `Docs/character-redesign.md` meant by "needs splitting, not satisfying".**
    The seat is a budget for what SITS on it. A seated person's legs overhang
    the front of the cushion, so holding the whole rig to the cushion's width
    is precisely what kept the trader at doll proportions — 3.59 heads against
    the reference's 5.47, with the entire shortfall in the legs. Accents
    tagged `limb` are therefore skipped, and the legs answer to
    `test_the_seated_figure_puts_its_feet_on_the_painted_floor` instead, which
    brackets them against the floor the chair's own castors paint.

    Arms are exempt too, and that one is forced rather than chosen: see the
    proof in `seatedRig` beside `arm.limb`. It is a consequence of KI-084
    (arms draw behind the torso), and it comes back the moment KI-084 lands.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        # KI-051 (Task 6): `BODY_RIM` needs a real `.color` - it feeds
        # `BODY_RIM_SHADED` via `shade()`. `LIGHT` is the real page's, since
        # it does not depend on `PLATE` (`__LIGHT_JSON__` is substituted at
        # render time from the manifest, independent of this driver's own
        # `const PLATE = null`).
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + """
        class FakeGraphics {
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          constructor() {
            this.calls = []; this.x = 0;
            this.pivot = { x: 0, y: 0 }; this.rotation = 0;
          }
          roundRect(x, y, w, h, r) { this.calls.push([x, w]); return this; }
          poly(f) { const xs = f.filter((_, i) => i % 2 === 0), ys = f.filter((_, i) => i % 2 === 1); return this.roundRect(Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0); }
          circle(x, y, r) { this.calls.push([x - r, 2 * r]); return this; }
          fill() { return this; }
          stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const bodyCalls = [];
        const fakeGfx = {
          roundRect(x, y, w, h, r) { bodyCalls.push([x, w]); return this; },
          poly(f) { const xs = f.filter((_, i) => i % 2 === 0), ys = f.filter((_, i) => i % 2 === 1); return this.roundRect(Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0); },
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
        + _seated_rig_block(source)
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
          // The seat is a budget for what SITS on it. A seated person's legs
          // overhang the front of the cushion -- that is what legs do -- so a
          // limb is not a seat-fit violation, and holding the rig to the
          // cushion's width including them would force the doll proportions
          // this redesign exists to remove. The torso and the arms still
          // answer to it. See the sibling floor-band bracket, which is the
          // rail the legs DO answer to.
          if (accent.limb) continue;
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


@needs_node
def test_the_seated_figure_puts_its_feet_on_the_painted_floor():
    """The trader's feet land on the floor the plate actually paints.

    The redesign's decisive finding is a RATIO and needs no scale: measured
    against its own head the shipped figure was 3.59 heads crown-to-ankle and
    the reference is 5.47, with the entire shortfall in the legs (thigh 16
    units where the trace says 57, shin 18 where it says 62). That is why it
    read as a doll -- dolls are short-legged -- and a figure that short leaves
    its feet hanging in the air above a chair whose base is painted in full.

    This is the rail for that, and it is a BRACKET rather than a derived
    landing point, deliberately. The floor is a plane in three-quarter view, so
    a seat-to-floor distance needs the projection; nominating a different
    castor as "the floor under the chair" swings the answer by 2x, which is the
    same under-determination that made anthropometry's px/cm range 2.7-4.4 and
    got that whole method abandoned (`tools/measure_chair.py`). A bracket
    asks the question the art CAN answer: the feet cannot be above the furthest
    painted contact point, nor below the nearest one.

    The band is imported, never restated -- `measure_floor_band()` reads it off
    the chair's castors with the same pixel predicate that measured the rake,
    so it keeps tracking the art if the plate is repainted.

    Anchored at `sit_anchor`, not `cast.trader.x/base_y`: `sitAnchorFor()` is
    what actually pins the seated rig's origin, and the two differ by 8px --
    which is a tenth of this bracket's width and exactly the sort of quiet
    substitution that makes a passing test describe a figure nobody drew.
    """
    from tools.measure_chair import measure_floor_band

    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + """
        class FakeGraphics {
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          constructor() {
            this.calls = []; this.x = 0; this.y = 0;
            this.pivot = { x: 0, y: 0 }; this.rotation = 0;
          }
          roundRect(x, y, w, h, r) { this.calls.push([x, y, w, h]); return this; }
          poly(f) { const xs = f.filter((_, i) => i % 2 === 0), ys = f.filter((_, i) => i % 2 === 1); return this.roundRect(Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0); }
          circle(x, y, r) {
            this.calls.push([x - r, y - r, 2 * r, 2 * r]); return this;
          }
          fill() { return this; }
          stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const bodyCalls = [];
        const fakeGfx = {
          roundRect(x, y, w, h, r) { bodyCalls.push([x, y, w, h]); return this; },
          poly(f) { const xs = f.filter((_, i) => i % 2 === 0), ys = f.filter((_, i) => i % 2 === 1); return this.roundRect(Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0); },
          circle(x, y, r) {
            bodyCalls.push([x - r, y - r, 2 * r, 2 * r]); return this;
          },
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
        + _seated_rig_block(source)
        + "\n"
        + """
        const accents = [];
        seatedRig(fakeGfx, accents);
        // The lowest point the rig draws, in its own units. Calls onto the
        // body are untransformed. An accent carries a position AND -- for the
        // leg bones, whose entire pose IS their rotation -- a pivot and an
        // angle, so each corner is transformed properly instead of being
        // offset by `x` alone. Offsetting by x is what an aggregate check
        // would do, and it would report a rotated leg as a short vertical one.
        let bottom = -Infinity;
        for (const [, y, , h] of bodyCalls) bottom = Math.max(bottom, y + h);
        for (const a of accents) {
          const s = Math.sin(a.rotation || 0), c = Math.cos(a.rotation || 0);
          const pvx = a.pivot ? a.pivot.x : 0, pvy = a.pivot ? a.pivot.y : 0;
          for (const [x, y, w, h] of a.calls) {
            const corners = [[x, y], [x + w, y], [x, y + h], [x + w, y + h]];
            for (const [cx, cy] of corners) {
              bottom = Math.max(bottom, (a.y || 0) + (cx - pvx) * s + (cy - pvy) * c);
            }
          }
        }
        console.log(JSON.stringify({ bottom, castScale: CAST_SCALE }));
        """
    )
    emitted = _run_node(driver)
    trader = _manifest()["cast"]["trader"]
    sit = trader["sit_anchor"]
    # The rig is drawn `figure_scale` bigger than its own units before the
    # layer's CAST_SCALE touches it, so both factors are here or this measures
    # a figure nobody draws. It is also the direction this bracket BINDS from:
    # the chair asks for a bigger body than the page drew, and the floor is
    # what stops that getting bigger still — every tenth of scale walks the
    # ankle a further 8px down a band that is 77px wide.
    foot_screen = (
        sit["y"]
        + emitted["bottom"] * emitted["castScale"] * trader["figure_scale"]
    )
    far, near = measure_floor_band()
    assert far <= foot_screen <= near, (
        f"the seated figure's lowest point is at screen y={foot_screen:.0f}, "
        f"outside the painted floor band {far}..{near}: "
        + (
            f"it floats {far - foot_screen:.0f}px above the furthest contact "
            "point the chair's own castors establish"
            if foot_screen < far
            else f"it sinks {foot_screen - near:.0f}px below the nearest one"
        )
    )


@needs_node
def test_every_seated_leg_bone_carries_its_own_angle_and_opts_out_of_gestures():
    """The two annotations that hold the leg pose up, pinned.

    A posed bone's entire geometry IS its rotation, and two consumers will
    take it away:

    1. `restAccents` rewrites EVERY accent's rotation as `restRotation ?? 0`
       on each tick. A bone that sets only `.rotation` renders correctly in a
       screenshot and then snaps bolt upright on frame 2.
    2. `gesture()` applies `GESTURE[accent.gestureMode] ?? GESTURE.swing` to
       every accent. Swinging a thigh, a shin and a shoe independently about
       their own joints does not animate a leg, it dismantles one — the same
       "felled tree" failure the GESTURE table already names for limb-less
       bodies.

    Neither is reachable from a still frame, and the `anims` gallery cannot
    cover it either: every sample there resolves `pose: "standing"`, so
    `seatedRig` never runs in it (see the comment at the gallery's own
    `pose` resolution). Without this test a refactor that drops either
    annotation leaves every existing assertion green, the screenshot perfect,
    and the room broken one frame later.

    Asserts the invariant over every leg bone rather than a count, so bones
    added later are covered by construction.
    """
    source = _world_source()
    driver = (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + """
        class FakeGraphics {
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          constructor() {
            this.calls = []; this.x = 0; this.y = 0;
            this.pivot = { x: 0, y: 0 }; this.rotation = 0;
          }
          roundRect() { return this; }
          circle() { return this; } poly() { return this; }
          fill() { return this; }
          stroke() { return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const fakeGfx = {
          roundRect() { return this; }, circle() { return this; }, poly() { return this; },
          fill() { return this; }, stroke() { return this; },
        };
        """
        + _js_block(source, "function snap(")
        + "\n"
        + _js_const(source, "CELL")
        + "\n"
        + _shading_prelude(source)
        + _seated_rig_block(source)
        + "\n"
        + """
        const accents = [];
        seatedRig(fakeGfx, accents);
        const legs = accents.filter((a) => a.limb === "leg");
        console.log(JSON.stringify({
          legCount: legs.length,
          // `restRotation` is what survives `restAccents`; `.rotation` is only
          // what the first frame happens to show.
          restRotations: legs.map((a) => a.restRotation),
          rotationsMatchRest: legs.every((a) => a.rotation === a.restRotation),
          gestureModes: legs.map((a) => a.gestureMode),
        }));
        """
    )
    emitted = _run_node(driver)
    assert emitted["legCount"] == 6, (
        "expected six leg bones (thigh/shin/foot, near and far); got "
        f"{emitted['legCount']}"
    )
    assert emitted["rotationsMatchRest"], (
        "a leg bone's `.rotation` disagrees with its `restRotation` - "
        "`restAccents` will overwrite the pose on the first tick"
    )
    assert all(r is not None for r in emitted["restRotations"]), (
        "a leg bone has no `restRotation`: `restAccents` resolves that to 0 "
        "and the leg snaps upright one frame after it is drawn"
    )
    # Not every bone is at a distinct angle, but a leg whose bones are ALL at
    # zero is a leg drawn straight down - the shape this increment replaced.
    assert any(r != 0 for r in emitted["restRotations"]), (
        "every leg bone sits at rotation 0 - the legs are not posed at all"
    )
    assert emitted["gestureModes"] == ["still"] * emitted["legCount"], (
        "a leg bone would be swung by `gesture()`, which takes the leg apart: "
        f"{emitted['gestureModes']}"
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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + """
        function makeRecorder(calls) {
          return {
            roundRect(x, y, w, h, r) { calls.push([x, y, w, h]); return this; },
            poly(f) { const xs = f.filter((_, i) => i % 2 === 0), ys = f.filter((_, i) => i % 2 === 1); return this.roundRect(Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0); },
            circle(x, y, r) { calls.push([x - r, y - r, 2 * r, 2 * r]); return this; },
            fill() { return this; },
            stroke() { return this; },
          };
        }
        // `new PIXI.Graphics()` (each arm) needs the same interface as the
        // plain recorder above, on an instance with its own `.calls`.
        class FakeGraphics {
          addChild(c) { (this.children || (this.children = [])).push(c); return c; }
          constructor() {
            this.calls = []; this.x = 0; this.y = 0;
            // Real PIXI.Graphics fields. The seated legs pose themselves with
            // both, so a double without them throws from inside `seatedRig`.
            this.pivot = { x: 0, y: 0 }; this.rotation = 0;
            const r = makeRecorder(this.calls);
            this.roundRect = r.roundRect; this.circle = r.circle; this.poly = r.poly;
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
        + _seated_rig_block(source)
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
        // An accent carries a position and, for the six posed leg bones, a
        // pivot and a rotation. Offsetting by `accent.x` alone -- which is all
        // the X-only sibling above needs, since nothing it measures is turned
        // -- would report a thigh raked across the floor as a short upright
        // rect, and this box is the one the desk furniture is checked against.
        for (const accent of accents) {
          const s = Math.sin(accent.rotation || 0);
          const c = Math.cos(accent.rotation || 0);
          const pvx = accent.pivot ? accent.pivot.x : 0;
          const pvy = accent.pivot ? accent.pivot.y : 0;
          for (const [x, y, w, h] of accent.calls) {
            for (const [cx, cy] of [[x, y], [x + w, y], [x, y + h], [x + w, y + h]]) {
              const lx = cx - pvx, ly = cy - pvy;
              const px = (accent.x || 0) + lx * c - ly * s;
              const py = (accent.y || 0) + lx * s + ly * c;
              x0 = Math.min(x0, px); y0 = Math.min(y0, py);
              x1 = Math.max(x1, px); y1 = Math.max(y1, py);
            }
          }
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
    # BOTH scales, or this measures a figure nobody draws: the rig node is
    # drawn `figure_scale` bigger than its own units before the cast layer's
    # CAST_SCALE touches it. Missing the second one is how a label-clearance
    # check keeps passing while the figure it clears grows by 1.6x — the same
    # quiet substitution the floor-band test's own docstring warns about.
    scale = emitted["castScale"] * manifest["cast"]["trader"]["figure_scale"]
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
    # `layers.foreground` is the painted furniture that stands in FRONT of
    # the cast (the trader's chair back) — so it belongs between `chars` and
    # `nameplates`, and its own test above asserts those two relations by
    # name rather than by this list's index.
    assert order == ["layers.plate", "layers.room", "layers.glow",
                      "layers.monitors", "layers.props", "layers.chars",
                      "layers.foreground", "layers.nameplates"], order


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
    assert "new PIXI.Sprite(" not in ticker, (
        "a glow Sprite allocated inside the per-frame ticker"
    )
    apply_glow = _js_block(body, "function applyGlow(")
    assert "new PIXI.Sprite(" not in apply_glow
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
    `api/templating.py`'s `_THEME_REPLACEMENTS` (the shared injection surface
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
    """Finding 4 (coordinator, fix round 1): `_render_template` (api/templating.py)
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

    Final review pass: `_render_template` (api/templating.py) is not `world.html`'s
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
    manifest = _manifest()
    plate = {k: manifest.get(k, []) for k in ("glow", "tubes", "spare_tubes")}
    return (
        f"const PLATE = {json.dumps(plate)};\n"
        f"const plateReady = {str(plate_ready).lower()};\n"
        # KI-089: the glow is a Sprite over a canvas-drawn falloff texture.
        # The fake canvas records every gradient's colour stops, so the
        # texture's edges can be read as numbers rather than trusted.
        "const gradients = [];\n"
        "const document = { createElement() { return { width: 0, height: 0,\n"
        "  getContext() { return { fillRect() {}, globalCompositeOperation: '',\n"
        "    createLinearGradient(x0, y0, x1, y1) {\n"
        "      const g = { axis: [x1 - x0, y1 - y0], stops: [],\n"
        "        addColorStop(o, c) { this.stops.push([o, c]); } };\n"
        "      gradients.push(g); return g; } }; } }; } };\n"
        "let textures = 0;\n"
        "class FakeSprite {\n"
        "  constructor(texture) {\n"
        "    this.texture = texture; this.blendMode = null; this.alpha = 1;\n"
        "    this.width = 0; this.height = 0; this.x = 0; this.y = 0;\n"
        "    this.anchor = { set: (a) => { this.anchorAt = a; } };\n"
        "    this.position = { set: (x, y) => { this.x = x; this.y = y; } };\n"
        "  }\n"
        "}\n"
        "const PIXI = { Sprite: FakeSprite,\n"
        "  Texture: { from: (canvas) => ({ id: ++textures, canvas }) } };\n"
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
        + _js_const(source, "GLOW_SPREAD")
        + "\n"
        + "let glowTexture = null;\n"
        + _js_block(source, "function glowLightTexture(")
        + "\n"
        + _js_block(source, "function glowShapes(")
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
        "    alpha: c.alpha, texture: c.texture.id, blend: c.blendMode,\n"
        "  })));\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    emitted = _run_node(driver)
    assert len(emitted) == 4
    for per_tier in emitted:
        assert per_tier, "buildGlows built nothing to light"
        for rect in per_tier:
            assert rect["blend"] == "add"

    # Monotonic per rect: the SETTLED alpha painted over the SAME glow rect
    # must rise with tier, never fall or flatten — the ramp's own
    # monotonicity (test_visuals_ramp_is_shared.py) is necessary but not
    # sufficient; this is the proof that applyGlow actually passes it
    # through.
    for i in range(len(emitted[0])):
        alphas = [emitted[tier][i]["alpha"] for tier in range(4)]
        assert alphas == sorted(alphas), (i, alphas)
        assert len(set(alphas)) > 1, f"glow rect {i} never brightens at all"

    # Colour must not drift with tier: brightening has to come from alpha,
    # not hue, or the "desk lamp stays amber at every tier" claim is false.
    # The colour is baked into ONE texture (KI-089), so "never changes
    # colour" is "never changes texture".
    for i in range(len(emitted[0])):
        textures = {emitted[tier][i]["texture"] for tier in range(4)}
        assert len(textures) == 1, f"glow {i} changes texture across tiers: {textures}"


@needs_node
def test_build_glows_is_idempotent():
    """Only one caller today (drawPlate's try block, once) — but the review
    that moved `plateReady` below the builders also asked for this: a second
    call, whether from a future retry path or from a throw that unwinds and
    gets attempted again, must REPLACE the glows in `layers.glow`, not stack
    a second set behind the first at double the intended brightness."""
    driver = _glow_driver(plate_ready=True) + (
        "buildGlows();\n"
        "const once = layers.glow.children.length;\n"
        "buildGlows();\n"
        "console.log(JSON.stringify([once, layers.glow.children.length]));\n"
    )
    once, twice = _run_node(driver)
    assert once > 0
    assert twice == once, (
        "a second buildGlows() call left the first call's glows behind "
        "instead of clearing layers.glow first"
    )


@needs_node
def test_each_painted_tube_glows_on_its_own_bore():
    """KI-089: one flat amber rect per tube PAIR read as a UI highlight box
    on air. Light off a glass tube lives on the tube. Every painted tube —
    assigned or spare — gets its own glow centred on its measured bore, and
    no glow leaves the manifest rect the chart-overlap validator
    (`glow_chart_overlaps`) cleared it for."""
    manifest = _manifest()
    tubes = manifest["tubes"] + manifest["spare_tubes"]
    driver = _glow_driver(plate_ready=True) + (
        "buildGlows();\n"
        "console.log(JSON.stringify(layers.glow.children.map((c) => (\n"
        "  { x: c.x, y: c.y, w: c.width, h: c.height, a: c.anchorAt }))));\n"
    )
    glows = _run_node(driver)
    assert sorted(g["x"] for g in glows) == sorted(t["x"] for t in tubes)
    slack = 0.5
    for g in glows:
        assert g["a"] == 0.5
        left, right = g["x"] - g["w"] / 2, g["x"] + g["w"] / 2
        top, bottom = g["y"] - g["h"] / 2, g["y"] + g["h"] / 2
        assert any(
            left >= r["x"] - slack and right <= r["x"] + r["w"] + slack
            and top >= r["y"] - slack and bottom <= r["y"] + r["h"] + slack
            for r in manifest["glow"]
        ), g
        tube = next(t for t in tubes if t["x"] == g["x"])
        # Fitted to the tube, not the pair: narrower than the rect, wider
        # than the glass, and spanning the tube's own height.
        assert tube["width"] < g["w"] < 2.5 * tube["width"], (g, tube)
        assert top <= tube["base_y"] - tube["height"] + slack
        assert bottom >= tube["base_y"] - slack


@needs_node
def test_the_glow_has_no_straight_edges():
    """KI-089, the rule rather than the instance: whatever the glow's size,
    its light falls to NOTHING at every edge of what it draws, on both axes.
    A texture that stays lit at its border is a rectangle again, just a
    smaller one."""
    driver = _glow_driver(plate_ready=True) + (
        "buildGlows();\n"
        "console.log(JSON.stringify(gradients));\n"
    )
    gradients = _run_node(driver)
    axes = {("x" if g["axis"][1] == 0 else "y") for g in gradients}
    assert axes == {"x", "y"}, gradients

    def alpha(css):
        return float(css.rsplit(",", 1)[1].rstrip(")"))

    for g in gradients:
        stops = sorted(g["stops"])
        assert stops[0][0] == 0 and stops[-1][0] == 1, g
        assert alpha(stops[0][1]) == 0 and alpha(stops[-1][1]) == 0, g
        assert max(alpha(c) for _, c in stops) == 1, g


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
        # (`light: { ...ROOM_LIGHT[0] }`).
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
        "  addChild(c) { (this.children || (this.children = [])).push(c); return c; }\n"
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
        "  addChild(c) { (this.children || (this.children = [])).push(c); return c; }\n"
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
        "function label() { return {}; }\n"
        "function pillarGeometry() {\n"
        "  return { x: 0, baseY: 0, width: 1, height: 1 };\n"
        "}\n"
        "class FakeGraphics {\n"
        "  addChild(c) { (this.children || (this.children = [])).push(c);"
        " return c; }\n"
        "  rect() { return this; }\n"
        "  fill() { return this; }\n"
        "}\n"
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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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
    assert "applyEvalLayers()" in body and _eval_roles()["plate"] == "scenery"


def test_the_gallery_hides_the_live_monitors():
    # Observed 2026-09-01: live candles drew straight over the specimens.
    body = _js_block(client.get("/world?gallery=1").text, "function drawGallery(")
    assert "applyEvalLayers()" in body and _eval_roles()["monitors"] == "content"


def test_the_gallery_hides_the_rooms_own_nameplates():
    # Observed 2026-09-07 (this ticket's own screenshot): `layers.nameplates`
    # is built once at fixed canvas coordinates, independent of the room
    # characters `setCharacterVisible` hides here — left visible, "MODEL"/
    # "TRADER" drew straight over whichever grid cell happened to land on
    # that spot. Same failure shape, same fix, as the monitors sibling above.
    body = _js_block(client.get("/world?gallery=1").text, "function drawGallery(")
    assert "applyEvalLayers()" in body and _eval_roles()["nameplates"] == "content"


def test_the_animation_sheet_keeps_the_room_too():
    body = _js_block(
        client.get("/world?anims=1").text, "function drawAnimationSheet("
    )
    assert "layers.plate.visible = false" not in body
    assert "applyEvalLayers()" in body and _eval_roles()["plate"] == "scenery"


def test_the_animation_sheet_hides_the_rooms_own_nameplates():
    # Sibling of the gallery's own check above - the same static-content leak
    # observed on this ticket's `?anims=1` screenshot.
    body = _js_block(
        client.get("/world?anims=1").text, "function drawAnimationSheet("
    )
    assert "applyEvalLayers()" in body and _eval_roles()["nameplates"] == "content"


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
    assert "const LIGHT = WORLD_CONFIG.light;" in source
    light = _served_config(source)["light"]

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
        + _seated_rig_block(source)
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
    # Re-baselined 2026-09-25, deliberately: the user asked for the seated
    # breathe (and cheer) to carry the head with the body (the neck stretched a CELL
    # every breath). `test_the_neck_holds_its_length_through_a_breath`.
    # And again the same day: the breath is one unit and only up, since a CELL
    # bob of the whole bust still read as unnatural.
    # `test_a_seated_breath_is_one_pixel_and_only_up`.
    # And 2026-09-26 (Sprint 17 #3, the user's pick): the shrug lifts the
    # forearm at the elbow instead of swinging the arm back along the lap.
    # `test_the_seated_shrug_lifts_the_forearm_at_the_elbow`.
    "const SEATED_ANIMATIONS = {": (
        "0c9aef488a35203c5f331e92a5063b36726e8dbf9b76d39cf0ee011ade775db9", 4460
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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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
          poly(flat) { shapes.push({ kind: "poly", flat }); return this; },
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
    # Comments stripped: a volume QUOTED in a comment is not a volume drawn,
    # and counting them let the floor below track prose (increment 16).
    rig = _without_comments(
        _js_block(source, "const BODIES = {")
        + _seated_rig_block(source)
        + _js_block(source, "function character(")
    )
    rects = re.findall(
        r"litRect\(([-\d.]+), ([-\d.]+), (\w+|[\d.]+), (\w+|[\d.]+), ([\d.]+)\)", rig
    )
    circles = re.findall(r"litCircle\(([-\d.]+), ([-\d.]+), ([\d.]+)\)", rig)
    # A size may be written as a named constant rather than as a literal -
    # `bars` spells its seam thickness `cut` so the number lives in one place.
    # The value is read out of the page, never restated here: a literal copy
    # would keep this test green while it graded geometry the page had stopped
    # drawing, which is the shape of test this sprint exists to stop writing.
    # (This replaced a `const heights = [...]` substitution written for the
    # candle cluster, which increment 11 deleted along with the cluster.)
    scalars = {
        name: float(value)
        for name, value in re.findall(r"const (\w+) = ([-\d.]+);", rig)
    }

    def _size(token: str) -> float:
        return scalars[token] if token in scalars else float(token)

    # The floor is the count this extraction ACTUALLY reaches, not a round
    # number below it. Six of the rig's `litRect` calls size themselves from
    # expressions the regex cannot read (`H.across`, `HALF_W * 2`, the
    # forearm's own `width`), so the gap between what is drawn and what is
    # graded is real and is recorded here rather than papered over: a volume
    # that silently drops out of the extraction has to fail, or this test
    # quietly stops covering whatever was removed.
    # The profile head (increment 16) authors its volumes as DATA in
    # `PROFILE_HEAD` and mirrors them, so they are read from there: the one
    # rect and the one circle `profileHead` paints. The face and fringe are
    # convex PATHS since increment 18 and are graded by
    # `test_no_path_band_leaves_its_outline`.
    profile = dict(
        re.findall(r"^\s+(\w+): \[([-\d, ]+)\],", _js_block(source, "const PROFILE_HEAD = {"), re.M)
    )
    for key in ("nape",):
        rects.append(tuple(v.strip() for v in profile[key].split(",")))
    circles.append(tuple(v.strip() for v in profile["skull"].split(",")))
    # 24, not 25: the neck left this extraction on 2026-09-25 when it became
    # a column path; `test_no_path_band_leaves_its_outline` grades it now.
    assert (len(rects), len(circles)) == (24, 5), (
        f"the extraction now reaches {len(rects)} rect and {len(circles)} "
        "circle volumes, not 24 and 5. Either a volume was added (raise these "
        "numbers, having checked the new one is graded) or one stopped being "
        "readable - and an unreadable volume is an ungraded volume"
    )
    volumes = []
    for x, y, w, h, r in rects:
        volumes.append(
            ["rect", float(x), float(y), _size(w), _size(h), float(r)]
        )
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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
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
          poly(f) { const xs = f.filter((_, i) => i % 2 === 0), ys = f.filter((_, i) => i % 2 === 1); return this.roundRect(Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 0); },
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
        + f"const PAINTED_CAST = {json.dumps(_painted_cast(_manifest()))};\n"
        + _js_const(source, "CAST_SCALE")
        + _js_block(source, "function throwAxis(") + "\n"
        + (tick if tick is not None else _js_block(source, "function shadowTick("))
        + "\n"
        + """
        // A body's whole contact with this function: where it stands, how far
        // off the floor it is, and how wide its own patch is. Nothing else in
        // `character()` is involved, so nothing else is stubbed. It stands on
        // the model's painted mark by default — below and left of the lamp —
        // because since KI-061 WHERE a figure stands decides its throw.
        // `x`/`baseY` are PLATE pixels; the figure is placed as
        // `positionCharacters` places it, divided into the scaled cast layer.
        function fakeChar(lift, px, py) {
          const x = px / CAST_SCALE, baseY = py / CAST_SCALE;
          return {
            baseY, shadowW: 34,
            container: { x, y: baseY - lift },
            shadow: { x: 0, y: 0, width: 0, height: 0, alpha: 0 },
          };
        }
        function run(lift, x = 360, baseY = 830) {
          const c = fakeChar(lift || 0, x, baseY);
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
    assert "const LIGHT = WORLD_CONFIG.light;" in source
    return _served_config(source)["light"]


@needs_node
def test_the_contact_shadow_is_thrown_away_from_the_lamp():
    """A shadow centred under a figure the room lights hard from one side is a
    puddle, and a puddle is the loudest "pasted on" cue in the before-frame.

    KI-061: the lamp is painted as a POINT (`LIGHT.key`), so "away from the
    lamp" is a different direction for each figure — from the key, through
    the foot. Graded at both painted cast marks, read from the manifest: the
    model, far to the lamp's left, throws left; the trader, under it, throws
    straight down with no sideways component. The single room-wide
    `KEY_AXIS` this used to throw along put the trader's shadow down-and-left
    as if the lamp stood beside him — kept below as the mutation.
    """
    source = _world_source()
    light = _page_light(source)
    kx, ky = light["key"]
    aspect = float(re.search(r"const SHADOW_ASPECT = ([\d.]+);", source).group(1))
    cast = _manifest()["cast"]
    # Where `positionCharacters` actually puts each figure: a seated one at
    # its `sit_anchor` (the hips), not at its cast x.
    def mark(name):
        sit = cast[name].get("sit_anchor")
        return (sit["x"], sit["y"]) if sit else (cast[name]["x"], cast[name]["base_y"])
    marks = {name: mark(name) for name in ("model", "trader")}
    probe = "console.log(JSON.stringify({%s}));" % ", ".join(
        f"{name}: run(0, {x}, {y})" for name, (x, y) in marks.items()
    )

    def away_from_key(rest, x, y):
        """The throw, undone of the floor's foreshortening, is parallel to
        foot - key: positive dot product, zero cross product."""
        dx, dy = rest["dx"], rest["dy"] / (aspect / 2)
        fx, fy = x - kx, y - ky
        return dx * fx + dy * fy, dx * fy - dy * fx

    emitted = _run_node(_shadow_driver(source, probe))
    for name, (x, y) in marks.items():
        rest = emitted[name]
        assert (rest["dx"] ** 2 + rest["dy"] ** 2) ** 0.5 > 1.0, (
            f"the {name}'s shadow is not displaced - the puddle is back"
        )
        dot, cross = away_from_key(rest, x, y)
        assert dot > 0, (
            f"the {name}'s shadow moved ({rest['dx']:+.2f}, {rest['dy']:+.2f}), "
            f"TOWARD the lamp at {light['key']} - an inverted sign"
        )
        assert abs(cross) < 1e-6, (
            f"the {name}'s shadow is not on the line from the lamp through its "
            f"foot (cross {cross:+.4f}) - it is thrown along a room-wide axis"
        )
    assert marks["trader"][0] > kx, (
        "the trader's hips no longer sit right of the lamp - re-read KI-061 "
        "before trusting the next assertion"
    )
    assert emitted["trader"]["dx"] > 0 and emitted["trader"]["dy"] > 0, (
        f"the trader sits just right of the lamp and his shadow moved "
        f"({emitted['trader']['dx']:+.2f}, {emitted['trader']['dy']:+.2f}) - "
        "down-and-LEFT is the KI-061 frame"
    )

    # Mutation 0: compare the scaled cast layer with plate pixels, which the
    # first cut of this fix shipped. The trader's throw goes back left.
    block = _js_block(source, "function shadowTick(")
    unscaled = block.replace(
        "throwAxis(char.container.x * CAST_SCALE, char.baseY * CAST_SCALE)",
        "throwAxis(char.container.x, char.baseY)",
    )
    assert unscaled != block, "the unscaled mutation did not apply"
    broken = _run_node(_shadow_driver(source, probe, tick=unscaled))
    assert broken["trader"]["dx"] < 0, (
        "mixing cast-layer and plate coordinates still threw the trader's "
        "shadow right - the scale assertion cannot see it"
    )

    # Mutation 1: the pre-KI-061 room-wide axis. The trader's cross product
    # must see it.
    global_axis = block.replace(
        "const axis = throwAxis(char.container.x * CAST_SCALE, char.baseY * CAST_SCALE);",
        "const axis = KEY_AXIS;",
    )
    assert global_axis != block, "the global-axis mutation did not apply"
    broken = _run_node(_shadow_driver(source, probe, tick=global_axis))
    assert abs(away_from_key(broken["trader"], *marks["trader"])[1]) > 1e-3, (
        "a shadow thrown along the room-wide axis still passed - the "
        "per-figure assertion cannot see KI-061"
    )

    # Mutation 2: throw it toward the lamp instead.
    toward = block.replace("const throwX = axis[0] *", "const throwX = -axis[0] *"
                           ).replace("axis[1] * CONTACT", "-axis[1] * CONTACT")
    assert toward != block, "the toward-the-lamp mutation did not apply"
    broken = _run_node(_shadow_driver(source, probe, tick=toward))
    assert away_from_key(broken["model"], *marks["model"])[0] < 0, (
        "throwing the shadow toward the lamp still satisfied the away-from-"
        "the-lamp assertion - it would not catch an inverted sign"
    )


@needs_node
def test_the_throw_is_the_measured_scale_and_nothing_else():
    """`LIGHT.contact.throwScale` is the plate's own measurement of how far
    this room throws a shadow, so doubling it must double the throw and zeroing it
    must give the centred patch back — an unmeasured room gets no invented
    direction, which is the same rule `DEFAULT_LIGHT` states in Python.

    Driven against the page's own `LIGHT` and two edits of it, so the
    measured values stay under test instead of being restated here.
    """
    source = _world_source()
    light = _page_light(source)
    assert light["contact"]["throwScale"] > 0, (
        "the shipped plate measures no throw scale - this test's premise "
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
    doubled = throw(throwScale=light["contact"]["throwScale"] * 2)
    assert 1.98 < doubled / base < 2.02, (
        f"doubling the measured throw scale scaled the throw by "
        f"{doubled / base:.3f}x, not 2x - the offset is not the measurement"
    )
    assert throw(throwScale=0) < 1e-9, (
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
    no_throw["contact"]["throwScale"] = 0

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
        + _js_const(source, "NEUTRAL_TINT")
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
    # NEUTRAL_TINT, not NEUTRAL: an un-mooded BODY is painted in the lifted
    # grey, because the identity grey renders at 3.05:1 against the room and
    # the floor KI-028 set is 4.5 (KI-060).
    assert "const NEUTRAL_TINT = WORLD_CONFIG.neutral_tint;" in source
    neutral_tint = _served_config(source)["neutral_tint"]
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
    assert "const BODY_TINT = WORLD_CONFIG.body_tints;" in source
    table = _served_config(source)["body_tints"]
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
    unguarded = mood_tint.replace(": NEUTRAL_TINT", ": NaN")
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
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + _js_const(source, "MOOD_COLOR")
        + _js_const(source, "NEUTRAL")
        + _js_const(source, "NEUTRAL_TINT")
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
    light = _served_config(source)["light"]
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
# apply to wrapped text. `history` is the room's only canvas-drawn label
# left (the `name-*` nameplates came off on 2026-09-24), and gets both
# checks in full.


def _text_placements():
    """The resolved `__TEXT_JSON__` this page was actually served - read
    from the same function `api/routers/world.py` calls, not re-parsed out of the
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


def _nowband_driver(source: str, state: str) -> str:
    """`renderNowBand` against a stub DOM - the page's own strip builder, run.

    Only `document`, `SYMBOLS` and `MOOD_COLOR` are stubbed; the cells and
    their text are the page's, character for character."""
    return (
        """
        class El {
          constructor(tag) {
            this.tag = tag; this.className = ""; this.style = {};
            this.children = []; this._text = "";
          }
          set textContent(value) { this._text = value; this.children = []; }
          get textContent() {
            return this.children.length
              ? this.children.map((c) => c.textContent).join("")
              : this._text;
          }
          appendChild(child) { this.children.push(child); return child; }
        }
        const band = new El("div");
        const document = {
          getElementById: () => band,
          createElement: (tag) => new El(tag),
          createTextNode: (text) => {
            const node = new El("#text");
            node.textContent = text;
            return node;
          },
        };
        const SYMBOLS = ["BTCUSDT", "ETHUSDT"];
        const MOOD_COLOR = {};
        """
        + _js_block(source, "function formatPrice(")
        + "\n"
        + _js_block(source, "function bandCell(")
        + "\n"
        + 'const nowband = document.getElementById("nowband");\n'
        + _js_block(source, "function renderNowBand(")
        + f"\nrenderNowBand({state});\n"
        + "console.log(JSON.stringify({ text: band.textContent }));"
    )


@needs_node
def test_the_now_band_publishes_the_traders_open_trades_and_pnl():
    """KI-065: Task 10 deleted the trader's `open_trades` / `profit_pct`
    readout - correctly, it floated beside the figure with no surface under
    it - and nothing else in the room or the overlays carries it. The show's
    stated premise is that an AI trades crypto live, loses money and
    publishes every loss forever; this is the loss. It goes on `#nowband`,
    beside the model's record, which is where a newcomer already looks."""
    emitted = _run_node(
        _nowband_driver(
            _world_source(),
            '{ prices: { BTCUSDT: 118432.5 }, symbols: {},'
            ' model: { accuracy: { wins: 3, losses: 5, hit_rate: 0.375,'
            ' window: 8 } },'
            ' trader: { open_trades: 2, profit_pct: -1.3712, mood: "weighing" } }',
        )
    )

    assert "2 open" in emitted["text"], emitted["text"]
    assert "-1.37%" in emitted["text"], emitted["text"]
    assert "trader" in emitted["text"]
    # The model's record is untouched beside it.
    assert "3W 5L" in emitted["text"]


@needs_node
def test_the_now_band_says_dash_when_no_sidecar_is_running():
    """`state.trader` is null until a `trader_*` event arrives, and those come
    from the freqtrade sidecar - an opt-in compose profile that is not running
    on this stack. The cell renders anyway, the way the model's record does
    when its accuracy is unknown: a named slot with an em dash is honest, an
    absent slot silently un-publishes the premise again."""
    emitted = _run_node(
        _nowband_driver(
            _world_source(),
            "{ prices: {}, symbols: {}, model: {}, trader: null }",
        )
    )

    assert "trader —" in emitted["text"], emitted["text"]


def test_every_declared_text_placement_is_read_by_the_page():
    """KI-070: `floating_text` and `glyph_overflow` validate every placement
    in `manifest.text`, so a placement the page never consults is worse than
    no placement at all - CI reports the "no text in the air" rule satisfied
    over data nothing renders, and a repaint that moves it moves nothing.

    `#nowband` is the case that made this real: the manifest declared
    `prices` and `record` on `band-bottom` while the strip has always been
    CSS-positioned, so the rule was enforced everywhere except where the
    text is largest. Either a placement is read, or it is not declared."""
    code = "\n".join(
        re.sub(r"//.*$", "", line) for line in _world_source().splitlines()
    )
    unread = [
        placement["id"]
        for placement in load_manifest().text
        if f"TEXT_PLACEMENTS.{placement['id']}" not in code
        and f'TEXT_PLACEMENTS["{placement["id"]}"]' not in code
    ]
    assert not unread, (
        "declared but never read, so CI validates a layout the page does not "
        f"draw - bind them or delete them: {unread}"
    )


@needs_node
def test_the_banner_is_padded_from_inside_its_surface_not_from_the_canvas():
    """KI-072: `#banner` is pinned `top: 0; left: 0` and positions its line
    with padding, so it needs the offset INSIDE its surface. `as_json` hands
    out absolute canvas coordinates - correctly; that is what every canvas
    label needs - and the two agree today only because `band-top` starts at
    (0, 0). Move the placement to the desk face and the old code pads a
    top-pinned bar by 1133/669px instead of drawing the line there.

    Runs the page's own binding, with `document` and the placement stubbed."""
    body = client.get("/world").text
    block = re.search(
        r'const banner = document\.getElementById\("banner"\);'
        r".*?banner\.style\.fontSize[^\n]*\n\s*\}",
        body,
        re.S,
    )
    assert block, "the page no longer binds #banner in one readable block"

    driver = (
        "const style = {};\n"
        "const document = { getElementById: () => ({ style }) };\n"
        "function bannerMinHeight() { return 60; }\n"
        # banner moved to `desk-face-trader` (1109, 655): absolute 1133/669,
        # 24/14 inside its own surface.
        "const TEXT_PLACEMENTS = { banner: "
        '{ x: 1133, y: 669, dx: 24, dy: 14, w: 100, h: 30, size: 15 } };\n'
        + block.group(0)
        + "\nconsole.log(JSON.stringify(style));"
    )
    style = _run_node(driver)

    assert style["paddingLeft"] == "24px", (
        "the banner is padded by an absolute canvas coordinate"
    )
    assert style["paddingTop"] == "14px"
    assert style["fontSize"] == "15px"
    assert style["minHeight"] == "60px"


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
    (`tests/unit/test_text_layout.py`) does - a label nudged off its
    surface (here `history`, the room's last canvas label) - resolves it exactly as `as_json` would, and
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
    idx = next(i for i, p in enumerate(placements) if p["id"] == "history")
    placements[idx] = dict(placements[idx], y=placements[idx]["y"] - 400)
    poisoned = dataclasses.replace(manifest, text=tuple(placements))

    # Sanity: the manifest-level guard does catch it (established already by
    # test_text_layout.py; re-asserted here only to anchor the claim below).
    assert floating_text(poisoned) == ["history"]

    resolved = json.loads(as_json(poisoned))["history"]
    body = client.get("/world").text
    driver = _text_driver(body) + f"""
    const drawn = placedLabel({json.dumps(resolved)}, "HISTORY", 0);
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
    realistic world (not an adversarial string: a 6-digit event count plus
    3-digit outages pushes even this deliberately conservative estimate to
    782 against a 780px box while real rendering there is still 642-709px, so
    an adversarial case would flag a non-issue), and checks every canvas label the
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
    p = placements["history"]
    assert glyph_overflow("A LINE NO REASONABLE BAND COULD HOLD " * 4, p["size"], p)


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
      nameTag = {{ text: "TRADER" }}, seed = 1, shadow = {{}},
      figureScale = 1, aim = {{}}, profile = null;
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
        "  addChild(c) { (this.children || (this.children = [])).push(c); return c; }\n"
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
def test_ambient_lights_snap_position_but_draw_the_measured_size():
    """KI-075: `snap()` on the SIZE turned the manifest's measured 6x5 into
    8x4 (+33% / -20%), bleeding additive lamp colour off a 6px sign onto the
    wall — and a measured 1 or 2 snapped to 0, an invisible light that still
    passed the builder's `w > 0` filter. The rule `drawPillars` states: snap
    the POSITION, never the size. Graded on every manifest light plus two
    small ones the manifest may one day hold."""
    lights = _manifest()["ambient_lights"] + [
        {"x": 101, "y": 203, "w": 1, "h": 2, "colour": "#ff0000", "period": 3},
        {"x": 7, "y": 9, "w": 6, "h": 5, "colour": "#00ff00", "period": 3},
    ]
    driver = _ambient_lights_driver(plate_ready=True, lights=lights) + (
        "buildAmbientLights();\n"
        "console.log(JSON.stringify("
        "ambientLightSprites.map((l) => l.graphics.rects[0])));\n"
    )
    drawn = _run_node(driver)
    assert len(drawn) == len(lights)
    for light, (x, y, w, h) in zip(lights, drawn):
        assert (w, h) == (light["w"], light["h"]), (
            f"the {light['w']}x{light['h']} light at ({light['x']}, "
            f"{light['y']}) was drawn {w}x{h}"
        )
        assert x % 4 == 0 and y % 4 == 0, f"the light's position ({x}, {y}) is off the CELL grid"


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
    assert result["before"], (
        "buildAmbientLights built nothing to test the guard against"
    )
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


@needs_node
def test_a_zero_or_negative_period_falls_back_to_the_default_floor():
    """`buildAmbientLights`'s `period: light.period > 0 ? light.period : 4`
    is a defensive floor against a hand-edited manifest entry with a
    `period` of `0` or negative - either would otherwise reach
    `tickAmbientLights`'s `t / light.period`, producing a division by zero
    (a light that is either always mid-fade or, for `0`, NaN-locked) or a
    sign flip that runs the sine backwards. The malformed-entry test above
    (which checks `colour`) does not exercise this guard at all.

    A fourth entry omits `period` entirely - covered rather than declared
    out of scope, because `undefined > 0` is also `false`: a missing or
    non-numeric period takes the exact same floor branch as `0` and `-2.5`,
    so proving it costs nothing beyond one more list entry.

    Runs the real builder and reads the `period` it actually stored, not
    the source text of the ternary - then ticks once so the `period: 0`
    light's `alpha` is checked for the actual 3am symptom (an un-floored
    zero divides `t` by zero, `Math.sin(Infinity)` is `NaN`, and
    `JSON.stringify` serializes `NaN` as `null`), not just the stored
    number.
    """
    lights = [
        {"x": 100, "y": 100, "w": 8, "h": 8, "colour": "#ff3b30", "period": 0},
        {"x": 200, "y": 200, "w": 8, "h": 8, "colour": "#ff3b30", "period": -2.5},
        {"x": 300, "y": 300, "w": 8, "h": 8, "colour": "#ff3b30", "period": 6},
        {"x": 400, "y": 400, "w": 8, "h": 8, "colour": "#ff3b30"},  # no period key
    ]
    result = _run_node(
        _ambient_lights_driver(plate_ready=True, lights=lights)
        + (
            "buildAmbientLights();\n"
            "const periods = ambientLightSprites.map((l) => l.period);\n"
            "tickAmbientLights(1.0);\n"
            "const alphas = ambientLightSprites.map((l) => l.graphics.alpha);\n"
            "console.log(JSON.stringify({ periods, alphas }));\n"
        )
    )
    assert result["periods"] == [4, 4, 6, 4], (
        "a period of 0, a negative number, or a missing/non-numeric period "
        "must all fall back to the default floor of 4; a positive period "
        "must pass through unchanged"
    )
    assert all(alpha is not None for alpha in result["alphas"]), (
        "an un-floored period of 0 reaches tickAmbientLights's t/period "
        "division, producing Infinity -> NaN alpha (NaN serializes as JSON "
        "null) - the exact failure the floor exists to prevent"
    )


TEMPLATE = Path(__file__).resolve().parents[2] / "api" / "templates" / "world.html"


def test_the_body_tint_fallback_clears_the_silhouette_floor():
    """KI-060: the one colour that never passed KI-028's contrast check was the
    fallback. Every *named* mood reaches the page through `visuals.body_tints()`,
    which lifts it to `SILHOUETTE_MIN_CONTRAST` — that lift IS the KI-028 fix.
    The page's own `NEUTRAL` literal never went through it, and measured 3.05:1
    against the room against a 4.5 floor (and WCAG's 3:1 for non-text graphics).

    So a figure whose mood the world emits but the cast cannot name rendered
    *below the floor the fix exists to hold* — precisely the case the fallback
    is there to protect. `character()` builds every figure at mood "neutral",
    which `MOOD_COLORS` does not claim, so this is the live path, not a
    hypothetical.
    """
    from world import visuals

    body = client.get("/world").text
    assert "const NEUTRAL_TINT = WORLD_CONFIG.neutral_tint;" in body
    assert _served_config(body)["neutral_tint"] == int(
        visuals.neutral_body_tint()[1:], 16
    ), (
        "the page's neutral body tint is not visuals.neutral_body_tint()"
    )
    assert visuals.neutral_body_contrast() >= visuals.SILHOUETTE_MIN_CONTRAST


def test_the_template_states_no_colour_of_its_own():
    """The other half, and the half a rendered-page test cannot see: an injected
    value and a hand-typed one that happen to agree look identical in the
    output. KI-060 was exactly that — `PALETTE['neutral']` copied into the page
    as `0x787b86`, correct as an identity colour and wrong as a body tint,
    with nothing to keep the copy honest."""
    source = TEMPLATE.read_text()
    assert "const NEUTRAL_TINT = WORLD_CONFIG.neutral_tint;" in source
    assert "const NEUTRAL = WORLD_CONFIG.neutral_color;" in source
    assert "0x787b86" not in source, (
        "world.html hand-copies PALETTE['neutral'] again — inject it"
    )


# --- The keyline (increment 15) -------------------------------------------


def _keyline_fragment(source: str) -> str:
    match = re.search(r"const KEYLINE_FRAGMENT = `(.*?)`;", source, re.S)
    assert match, "the page no longer declares KEYLINE_FRAGMENT as a template literal"
    return match.group(1)


def test_the_keyline_shader_declares_shared_uniforms_at_the_vertex_precision():
    """The bug the keyline's probe caught on its first frame, pinned.

    PixiJS's default filter vertex declares `uInputSize` (and its siblings)
    with no qualifier, so they are `highp`; a fragment shader defaults to
    `mediump`. GLSL refuses to LINK a program whose shared uniform differs in
    precision between stages -- "Precisions of uniform 'uInputSize' differ" --
    and Pixi reports that as a console error and draws nothing. The probe
    turned that into an unoutlined trader instead of an invisible one, but
    the keyline was silently off. Every uniform the vendored vertex declares
    must be `highp` if the fragment declares it too.
    """
    pixi = (Path(__file__).resolve().parents[2] / "api" / "static" / "pixi-8.19.0.min.js").read_text()
    vertex_at = pixi.index("in vec2 aPosition;")
    vertex = pixi[vertex_at : pixi.index("`", vertex_at)]
    shared = set(re.findall(r"uniform\s+vec\d\s+(\w+);", vertex))
    assert "uInputSize" in shared, "could not read the vendored filter vertex"

    fragment = _keyline_fragment(_world_source())
    declared = re.findall(r"uniform\s+(highp\s+|mediump\s+|lowp\s+)?\w+\s+(\w+);", fragment)
    checked = [name for _, name in declared if name in shared]
    assert checked, "the fragment reads no uniform the vertex shares; this test graded nothing"
    for qualifier, name in declared:
        if name in shared:
            assert qualifier.strip() == "highp", (
                f"`{name}` is declared {qualifier.strip() or 'without a precision'} "
                "in the keyline fragment and highp in Pixi's filter vertex: the "
                "program will not link and the trader loses his outline."
            )


def _keyline_driver(source: str, scenario: str) -> str:
    """`keylineFilter` against a stub renderer, one scenario per process
    (the page caches the outcome in a module-level `let`)."""
    return (
        "const LIGHT = { warmth: '#f0a848', direction: [-0.55, 0.84] };\n"
        + _js_block(source, "const KEYLINE = {") + ";\n"
        + _js_block(source, "function rimFromLight(") + "\n"
        + "const RIM = rimFromLight(LIGHT);\n"
        + _js_block(source, "function rimAmount(") + "\n"
        + "const PROBE_GREY = 0x40;\n"
        + "const KEYLINE_FRAGMENT = '';\n"
        + "let keylineCache;\n"
        + _js_block(source, "function keylineFilter(") + "\n"
        + _js_block(source, "function keylineProves(") + "\n"
        + """
        const window = {};
        const scenario = %s;
        // A 32x32 target the stub "renders" into. The page draws a grey
        // square at 11..22; a working shader snaps it onto the global 2px grid
        // (10..23), rims its outer cell (toward the lamp's warmth, more on the
        // side facing the key) and puts the keyline one cell outside (8..25).
        // `passthrough` returns the square untouched; `bounds` is the shimmer
        // bug -- a grid anchored at the odd bounds origin, so the square lands
        // on 11..24 with its keyline at 9..10 and column 10 is keyline, not
        // rim. `norim` outlines on the right grid and draws no rim (KI-058's
        // failure); `undirected` rims every edge by `floor` alone;
        // `facingonly` rims only the edges that face the key.
        function frame(works) {
          const px = new Uint8Array(32 * 32 * 4);
          const k = KEYLINE.color;
          const g = PROBE_GREY;
          const rimmed = works || ["bounds", "undirected", "facingonly"].includes(scenario);
          const outlined = rimmed || scenario === "norim";
          const [lo, hi, rlo, rhi] = scenario === "bounds" ? [11, 25, 9, 27]
            : scenario === "passthrough" ? [11, 23, 11, 23] : [10, 24, 8, 26];
          for (let y = 0; y < 32; y++) for (let x = 0; x < 32; x++) {
            const i = (y * 32 + x) * 4;
            const inside = x >= lo && x < hi && y >= lo && y < hi;
            const ring = x >= rlo && x < rhi && y >= rlo && y < rhi;
            if (inside && (outlined || scenario === "passthrough")) {
              const nx = (x >= hi - 2 ? 1 : 0) - (x < lo + 2 ? 1 : 0);
              const ny = (y >= hi - 2 ? 1 : 0) - (y < lo + 2 ? 1 : 0);
              const edge = rimmed && (nx || ny);
              const facingOnly = scenario === "facingonly" && rimAmount(nx, ny) === RIM.floor;
              const m = !edge || facingOnly ? 0
                : scenario === "undirected" ? RIM.floor : rimAmount(nx, ny);
              px.set([...RIM.color.map((c) => Math.round(g + (c * 255 - g) * m)), 255], i);
            } else if (outlined && ring) px.set([(k >> 16) & 255, (k >> 8) & 255, k & 255, 255], i);
          }
          return px;
        }
        class Filter { constructor(o) {
          if (scenario === "throws") throw new Error("no program");
          this.opts = o; } }
        const PIXI = {
          RendererType: { WEBGL: 1, WEBGPU: 2 },
          Filter, defaultFilterVert: "",
          GlProgram: { from: (o) => o },
          Container: class { addChild() {} destroy() {} },
          Graphics: class { rect() { return this; } fill() { return this; } },
          RenderTexture: { create: () => ({ destroy() {} }) },
        };
        const app = { renderer: {
          type: scenario === "webgpu" ? 2 : 1,
          render() {},
          extract: { pixels: () => ({ pixels: frame(scenario === "works"), width: 32 }) },
        } };
        const got = keylineFilter();
        console.log(JSON.stringify({ attached: !!got, state: window.__keyline,
                                     cachedTwice: keylineFilter() === got }));
        """ % json.dumps(scenario)
    )


@needs_node
@pytest.mark.parametrize(
    "scenario",
    ["webgpu", "throws", "blank", "passthrough", "bounds", "norim", "undirected", "facingonly"],
)
def test_a_keyline_that_cannot_be_proven_is_not_worn(scenario):
    """No WebGL, a throw, or a shader that draws nothing: the trader goes on
    air unoutlined -- never invisible. The failure path, written first.

    `blank` is the one that matters: a shader that fails to compile does NOT
    throw in PixiJS, it logs and draws transparent pixels, which is how the
    precision bug above presented. Only reading the pixels back catches it.
    `passthrough` keeps the square and draws no ring -- a probe that checked
    only the inside would wear a filter that pixelates and never outlines.
    `bounds` outlines correctly but on a grid anchored to the filter's own
    bounds, which shimmers as the figure breathes (measured, increment 15).
    `norim` outlines on the right grid but draws no light edge -- the KI-058
    trader, whose dark keyline is 1.4:1 on the dark room -- and `undirected`
    draws a rim that ignores where the lamp is. `facingonly` lights just the
    edges that face the key: his shoes and shins face the floor, and those are
    the edges below 3:1, so a rim that skips them fixes nothing.
    """
    out = _run_node(_keyline_driver(_world_source(), scenario))
    assert not out["attached"], f"the keyline was attached in the {scenario!r} case"
    assert out["state"].startswith("off"), out["state"]
    assert out["cachedTwice"], "the outcome is not cached; every figure would re-probe"


@needs_node
def test_a_keyline_that_draws_the_proof_is_worn():
    """The same probe passes a shader that does its job, so the three refusals
    above are the probe discriminating, not the probe always saying no."""
    out = _run_node(_keyline_driver(_world_source(), "works"))
    assert out["attached"] and out["state"] == "on", out


def test_only_the_seated_rig_wears_the_keyline_and_never_its_labels():
    """On `rig`, inside the seated branch; never on `container`.

    `container` also holds the name tag, the stat line and the mood tag, and
    a keyline round type is a sticker. The machine keeps its own rim -- it is
    a glyph -- so the filter is the person's alone.
    """
    body = _without_comments(_js_block(_world_source(), "function character("))
    guard = re.search(
        r'const keyline = container\.pose === "seated" \? keylineFilter\(\) : null;'
        r"\s*if \(keyline\) rig\.filters = \[keyline\];",
        body,
    )
    assert guard, "the keyline is no longer attached to the rig under the seated guard"
    assert "container.filters" not in body, "a filter on `container` outlines the labels"
    assert body.count("keylineFilter(") == 1


def _relative_luminance(colour: int) -> float:
    def lin(v: float) -> float:
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = ((colour >> s) & 255 for s in (16, 8, 0))
    return 0.2126 * lin(r / 255) + 0.7152 * lin(g / 255) + 0.0722 * lin(b / 255)


@needs_node
def test_both_shoes_sit_between_the_floor_and_the_jeans():
    """Floor < shoe < jeans: a shoe LIGHTER than the floor, darker than the leg.

    Reverses increment 16's "darker than the floor" (user, 2026-09-24,
    "try to lighten up shoes"). Darker did not read either: the plate floor
    under the feet is `#1e222e`, the figure's own soft shadow takes it to
    ~`#191c27` round the shoes, and a `#181a22` shoe on that is ratio ~1.0 --
    the shins simply stopped. The reference lights its shoe at ~2.25x its
    floor (`#233444` on `#1a202c`) and keeps it bluer than the floor, so it
    reads as a shoe rather than a patch of the same floor; increment 16's
    refused `#2a2e3c` was a GREY the floor's own hue, which is what read as a
    puddle. And the shoe stays the darkest thing on the leg, below the jeans.

    Every drawn volume nearest `HUMAN.shoes` in the palette is a shoe, near
    and far alike; each one's base rung, rendered through the page's own
    `shade`, is held against the median plate floor in the band
    `measure_floor_band` measures where the feet land, and against the jeans.
    """
    from PIL import Image

    from tools.measure_chair import PLATE_PNG, measure_floor_band

    emitted = _seated_rig_run()
    palette = emitted["HUMAN"]

    def rgb(c: int) -> tuple:
        return ((c >> 16) & 255, (c >> 8) & 255, c & 255)

    def nearest(c: int) -> str:
        return min(
            # `hem` and `contact` are near the new, lighter shoe in value.
            ("sweater", "jeans", "skin", "shoes", "crease", "hem", "contact"),
            key=lambda k: sum((a - b) ** 2 for a, b in zip(rgb(c), rgb(palette[k]))),
        )

    shoes = sorted({d["fill"] for d in emitted["drawn"] if nearest(d["fill"]) == "shoes"})
    assert len(shoes) == 2, f"expected a near and a far shoe colour, found {shoes}"
    source = _world_source()
    rendered = _run_node(
        "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + _js_block(source, "function mixHex(") + "\n"
        + _js_block(source, "function shade(") + "\n"
        + _js_block(source, "function rampLevel(") + "\n"
        + f"console.log(JSON.stringify({json.dumps(shoes)}.map((c) => shade(c, rampLevel('base')))));"
    )
    far, near = measure_floor_band()
    hips = _manifest()["cast"]["trader"]["sit_anchor"]
    plate = Image.open(PLATE_PNG).convert("RGB")
    floor = sorted(
        _relative_luminance((p[0] << 16) | (p[1] << 8) | p[2])
        for x in range(hips["x"] - 200, hips["x"] - 60)
        for y in range(far, near)
        for p in [plate.getpixel((x, y))]
    )
    floor = floor[len(floor) // 2]
    # How blue the floor is, as the median of blue-minus-red over the same
    # pixels: the shoe has to be bluer than THAT, not than grey.
    blues = sorted(
        p[2] - p[0]
        for x in range(hips["x"] - 200, hips["x"] - 60)
        for y in range(far, near)
        for p in [plate.getpixel((x, y))]
    )
    floor_blue = blues[len(blues) // 2]
    jeans = _relative_luminance(palette["jeans"])
    for authored, shaded in zip(shoes, rendered):
        ratio = _relative_luminance(shaded) / floor
        # 1.3, not the reference's 2.25: the shoe must part from the shin
        # too, and `#233246` (1.9x) merged into the jeans on the frame.
        assert ratio >= 1.3, (
            f"a shoe authored {authored:#08x} renders {shaded:#08x}, "
            f"{ratio:.2f}x the floor's luminance: dark on dark, the shin just stops"
        )
        assert _relative_luminance(shaded) < jeans, (
            f"a shoe renders {shaded:#08x}, no darker than the jeans: the foot "
            "reads as more trouser leg"
        )
        r, g, b = rgb(shaded)
        assert b - r >= floor_blue + 8, (
            f"a shoe renders {shaded:#08x} (blue-red {b - r}) against a floor "
            f"at {floor_blue}: a shoe in the floor's own hue is the puddle "
            "increment 16 refused (`#2a2e3c`, 18); the reference's is 33"
        )


@needs_node
def test_no_path_band_leaves_its_outline():
    """Every `litPath` outline the page paints is CONVEX, and every band
    `paint` draws inside it stays inside it.

    Increment 18 drew the face and fringe as outlines rather than rects
    ("natural, without blocks"). A band is the outline shrunk about its
    centroid and slid toward the lamp; that is contained only while the
    outline is convex — shrink a concave one and the band pokes out through
    the notch, the rounded-corner leak `bandInside` was written to stop. So
    both halves are graded: the shape, and the page's own banding of it,
    for both sides the profile can face.
    """
    source = _world_source()
    P = _profile_head_run()["P"]
    hand = json.loads(_js_const(source, "HAND_OUTLINE").split("=", 1)[1].strip().rstrip(";"))
    across = max(x for x, _ in hand)
    # Each outline with the two ways the page places it: the head mirrors
    # about x = 0 by `side`, the hand about its own width by which side of
    # the forearm faces the floor (`seatedRig`).
    placements = {
        "face": [[[s * x, y] for x, y in P["face"]] for s in (-1, 1)],
        "fringe": [[[s * x, y] for x, y in P["fringe"]] for s in (-1, 1)],
        "hand": [[[x, y] for x, y in hand], [[across - x, y] for x, y in hand]],
        # The torso (increment 19) mirrors with the chair's facing.
        "torso": [[[s * x, y] for x, y in _torso_outline()] for s in (-1, 1)],
        # The neck (2026-09-25) is a path now, not a rounded rect, and it
        # mirrors with the torso.
        "neck": [[[s * x, y] for x, y in _neck_outline()] for s in (-1, 1)],
    }
    # The limbs (increment 20) are built at runtime from the pose, so they
    # come off the rig itself: one outline per side, both chains.
    rig = _run_node(_rig_bone_driver(source))
    for name, index in (("thigh", 0), ("shin", 1)):
        placements[name] = [rig["bones"][index]["outline"], rig["bones"][index + 3]["outline"]]
    placements["upper arm"] = [a["outline"] for a in rig["arms"]]
    placements["forearm"] = [a["foreOutline"] for a in rig["arms"]]
    for name, both in placements.items():
        for side, mirrored in zip((-1, 1), both):
            n = len(mirrored)
            turns = [
                (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
                for a, b, c in (
                    (mirrored[i], mirrored[(i + 1) % n], mirrored[(i + 2) % n])
                    for i in range(n)
                )
            ]
            assert all(t > 0 for t in turns) or all(t < 0 for t in turns), (
                f"the {name} outline is not convex (turns {turns}); its bands "
                "would leak through the concavity"
            )
            driver = _geometry_driver(
                source,
                f"const PTS = {json.dumps(mirrored)};\n"
                + """
                const bands = [];
                const g = { poly(flat) { bands.push(flat); return this; },
                            fill() { return this; }, stroke() { return this; } };
                const vol = litPath(PTS);
                for (const band of Object.values(BAND_INSET)) vol.band(g, band);
                console.log(JSON.stringify({ bands, inset: BAND_INSET }));
                """,
            )
            emitted = _run_node(driver)
            assert len(emitted["bands"]) == len(emitted["inset"])
            outline = [tuple(p) for p in mirrored]
            for flat in emitted["bands"]:
                for px, py in zip(flat[0::2], flat[1::2]):
                    # One quantum of slack: band vertices land on 1/64.
                    inside = _inside_convex(outline, px, py) or any(
                        _inside_convex(outline, px + dx, py + dy)
                        for dx in (-1 / 64, 1 / 64) for dy in (-1 / 64, 1 / 64)
                    )
                    assert inside, (
                        f"facing {side:+d}, a band of the {name} leaves its "
                        f"outline at ({px}, {py})"
                    )


@needs_node
def test_the_profile_face_is_in_thirds():
    """Hairline to brow, brow to nose base, nose base to chin: three roughly
    equal thirds, and the eye within a few units of halfway down the head.

    The user's catch (increment 18) was "his forehead is nonexistent": the
    fringe came down to -12 and the brow sat at -12, so the top third was
    zero and the nose alone took 14 of the head's 46 units. Pose-free and
    art-free — it reads the builder's data, so any redraw answers to it.
    """
    run = _profile_head_run(1)
    P = run["P"]
    hairline = max(y for _, y in P["fringe"])
    brow = P["brow"][1]
    nose_base = max(y for _, y in P["nose"])
    chin = run["head"]["chin"]
    thirds = [brow - hairline, nose_base - brow, chin - nose_base]
    assert min(thirds) > 0, f"the thirds {thirds} are out of order"
    assert max(thirds) <= 1.35 * min(thirds), (
        f"hairline→brow, brow→nose, nose→chin measure {thirds}: one third "
        "dwarfs another (the old head's forehead measured 0)"
    )
    nose_len = nose_base - min(y for _, y in P["nose"])
    assert nose_len <= (chin - hairline) / 3, (
        f"the nose is {nose_len} units long on a {chin - hairline}-unit face; "
        "a nose longer than the middle third starts above the eyes"
    )
    crown = run["head"]["crown"]
    eye_y = run["head"]["eye"][1]
    assert abs(eye_y - (crown + chin) / 2) <= 4, (
        f"the eye sits at {eye_y} on a head from {crown} to {chin}; an eye is "
        "about halfway down a head, not up in the hairline"
    )


@needs_node
def test_only_a_sideburn_of_hair_shows_in_front_of_the_ear():
    """In front of the ear and below the temple, the cheek is SKIN: the one
    hair allowed there is the sideburn, at most 2 units wide and ending
    above mid-ear.

    The complement of `test_no_hair_shows_below_the_ear`. The old face was a
    rect whose back edge stopped 8 units short of the ear, so a column of
    hair ran from the fringe to the jaw in front of it — "too much hair on
    the left of the ear" (the user's catch, increment 18). Graded by painting
    order over the volumes, both sides; the sideburn is a flat feature and is
    graded on its own numbers.
    """
    for side in (-1, 1):
        run = _profile_head_run(side)
        P, hair = run["P"], run["HUMAN"]["hair"]
        ex, ey, ew, eh = P["ear"]
        # The ear's front edge, facing +x, and the band of cheek in front of
        # it from ear top to ear bottom: in front means up to the nose.
        front = ex + ew
        tufts = []
        for i in range(0, 17):
            px = side * (front + i / 2)
            for j in range(0, eh * 2 + 1):
                py = ey + j / 2
                top = None
                for v in reversed(run["volumes"]):
                    g = v["geom"]
                    if v["kind"] == "circle":
                        hit = math.hypot(px - g[0], py - g[1]) <= g[2]
                    elif v["kind"] == "path":
                        hit = _inside_convex(list(zip(g[0::2], g[1::2])), px, py)
                    else:
                        hit = _inside_round_rect(px, py, g[:4], g[4] if len(g) > 4 else 0, slack=0)
                    if hit:
                        top = v
                        break
                if top is not None and top["fill"] == hair:
                    tufts.append((px, py))
        assert not tufts, (
            f"facing {side:+d}, hair shows on the cheek in front of the ear at "
            f"{len(tufts)} points, e.g. {tufts[:4]}"
        )
        sx, sy, sw, sh = P["sideburn"]
        assert sw <= 2, f"the sideburn is {sw} wide; a taper leaves a line, not a column"
        assert sy + sh <= ey + eh / 2, "the sideburn runs past mid-ear toward the jaw"
        assert abs(sx + sw - front) <= 4, "the sideburn is not in front of the ear"


def _neck_outline() -> list[list[float]]:
    """The skin column `seatedRig` paints between jaw and collar, rig-local."""
    run = _seated_rig_run()
    neck = [
        d for d in run["drawn"]
        if d["host"] == "body" and d["fill"] == run["HUMAN"]["skin"] and d["pts"]
    ]
    assert len(neck) == 1, f"expected the neck as ONE skin path on the body, found {len(neck)}"
    return neck[0]["pts"]


def _torso_outline() -> list[list[float]]:
    """The sweater outline `seatedRig` paints onto `body`, rig-local."""
    run = _seated_rig_run()
    torso = [
        d for d in run["drawn"]
        if d["host"] == "body" and d["fill"] == run["HUMAN"]["sweater"] and d["pts"]
    ]
    assert len(torso) == 1, (
        f"expected the torso as ONE outline on the body, found {len(torso)} "
        "sweater paths — a rounded rect is the slab increment 19 replaced"
    )
    return torso[0]["pts"]


@needs_node
def test_the_torso_is_an_outline_not_a_slab():
    """The torso is a shaped outline — shoulders that fall away from a flat
    neckline, a chest wider than the hips — not a rounded rect.

    The user's brief, again: "natural, without blocks". A rounded slab has
    its full width at its top corners; a person's shoulders slope off the
    neck. Graded as rules, not as today's points: the neckline is narrower
    than the chest, the widest span sits BELOW the neckline, and the hips
    still carry most of that width so the thighs have somewhere to hang
    from (the r18 slab hung them off a point). The three measured budgets —
    top at -72 for the neck, ±24 for the seat — hold too.
    """
    pts = _torso_outline()
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    width = max(xs) - min(xs)
    top = min(ys)
    assert top == -72, f"the torso tops out at {top}, the neck test's -72 moved"
    assert max(abs(x) for x in xs) <= 24, "the torso overran the seat budget"

    neckline = [x for x, y in pts if y == top]
    assert max(neckline) - min(neckline) <= 0.6 * width, (
        f"the neckline spans {max(neckline) - min(neckline)} of {width}: "
        "the shoulders do not fall away, it is a slab"
    )
    for extreme in (min(xs), max(xs)):
        y_at = min(y for x, y in pts if x == extreme)
        assert y_at >= top + 8, (
            f"the torso is widest at y {y_at}, within 8 of the neckline: "
            "square shoulders"
        )
    bottom = max(ys)
    hips = [x for x, y in pts if y == bottom]
    assert max(hips) - min(hips) >= 0.6 * width, (
        "the hips narrowed to a point — the legs hang off nothing"
    )


def _luma(rgb: int) -> float:
    r, g, b = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@needs_node
def test_the_sweater_ends_in_a_hem():
    """Where the sweater meets the jeans there is a HEM: a band along the
    torso's bottom edge, darker than both garments, so the waist reads as a
    line rather than as one blue-grey mass running from the shoulder into
    the lap (increment 21 — "I don't like how the torso connects to the
    ass"). Sweater `#3f5a70` against jeans `#34495c` is too close in value
    to draw that line by itself.

    Graded as rules on the drawn volumes: a volume on `body` (so it breathes
    with the torso rather than sliding over it) whose bottom IS the torso's
    bottom, that lies inside the torso outline, runs at least 80% of the
    torso's width there, is no taller than two CELLs (a band, not a second
    garment), and is darker than both the sweater and the jeans.
    """
    run = _seated_rig_run()
    human, cell = run["HUMAN"], run["CELL"]
    torso = _torso_outline()
    bottom = max(y for _, y in torso)
    base = [x for x, y in torso if y == bottom]
    base_w = max(base) - min(base)
    hems = []
    for d in run["drawn"]:
        if d["host"] != "body" or d["fill"] in (human["sweater"], human["skin"]):
            continue
        pts = d["pts"] or []
        if not pts or max(y for _, y in pts) != bottom:
            continue
        hems.append(d)
    assert hems, "nothing on the torso sits along its bottom edge: no hem"
    hem = hems[0]
    pts = hem["pts"]
    for px, py in pts:
        assert _inside_convex(torso, px, py), f"the hem pokes out of the torso at {px},{py}"
    at_bottom = [x for x, y in pts if y == bottom]
    assert max(at_bottom) - min(at_bottom) >= 0.8 * base_w, (
        "the hem is a patch, not a band round the waist"
    )
    height = bottom - min(y for _, y in pts)
    assert 0 < height <= 2 * cell, f"the hem is {height} tall: a second garment"
    for name in ("sweater", "jeans"):
        assert _luma(hem["fill"]) < _luma(human[name]), (
            f"the hem is no darker than the {name}: no line at the waist"
        )


@needs_node
def test_the_profile_eye_is_tired():
    """A slight shadow under the eye (the user's ask, increment 19): below
    the eye box, across it, inside the face, and lighter than the mouth's
    shade so it reads as tired rather than bruised. Both mirrors."""
    for side in (-1, 1):
        run = _profile_head_run(side)
        P = run["P"]
        ex0, ey0, ew, eh = P["eye"]
        if side < 0:
            ex0 = -(ex0 + ew)
        white = [f for f in run["features"] if f["kind"] == "ellipse"
                 and f["geom"][:2] == [ex0 + ew / 2, ey0 + eh / 2]]
        assert white, "the eye white moved off `P.eye`"
        order = run["features"].index(white[0])
        below = [f for f in run["features"][:order] if f["kind"] == "ellipse"]
        bags = [f for f in below
                if f["geom"][1] + f["geom"][3] >= ey0 + eh + 2
                and abs(f["geom"][0] - (ex0 + ew / 2)) <= ew / 2]
        assert bags, f"facing {side:+d}: no shadow drawn under (and before) the eye"
        cx, cy, rx, ry = bags[0]["geom"]
        assert cy - ry <= ey0 + eh, "the shadow does not meet the eye — a smudge"
        face = [[side * x, y] for x, y in P["face"]]
        for px, py in ((cx - rx, cy), (cx + rx, cy), (cx, cy + ry)):
            assert _inside_convex(face, px, py), (
                f"facing {side:+d}: the shadow leaves the face at ({px}, {py})"
            )
        mouth = [f for f in run["features"] if f["kind"] == "rect"
                 and f["geom"][1] == P["mouth"][1]]
        assert mouth, "the mouth moved off `P.mouth`"
        # The nose is filled with the skin's own base value.
        nose = [f for f in run["features"] if f["kind"] == "poly"]
        assert nose, "the nose is no longer a poly"
        lum = lambda c: _relative_luminance(c)
        assert lum(mouth[0]["fill"]) < lum(bags[0]["fill"]) < lum(nose[0]["fill"]), (
            "the under-eye shadow must sit between the mouth's shade and the "
            "skin — at the mouth's value it is a bruise"
        )


# --- KI-064: every stage layer is classified for the eval gates -------------


def _code(text: str) -> str:
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def test_every_stage_layer_has_an_eval_role():
    """KI-064: the eval gates (`?gallery=1`, `?anims=1`) hid content layers
    one at a time by name, so each new layer leaked onto the specimen sheets
    until someone noticed (monitors, then nameplates — three times in one
    sprint). Every layer the page builds must now be classified, and every
    classification must name a real layer; an unclassified one fails here."""
    source = _world_source()
    built = set(re.findall(r"layers\.(\w+)\s*=\s*new PIXI\.Container\(", _code(source)))
    roles = json.loads(
        re.sub(r"(\w+):", r'"\1":', _js_const(source, "EVAL_LAYER_ROLES")
               .split("=", 1)[1].strip().rstrip(";"))
    )
    assert built, "no stage layers found — the scan is broken"
    assert built - roles.keys() == set(), f"unclassified layers: {built - roles.keys()}"
    assert roles.keys() - built == set(), f"roles for layers that don't exist: {roles.keys() - built}"
    assert set(roles.values()) <= {"scenery", "content", "stage"}


def test_both_eval_gates_classify_through_the_one_rule():
    source = _world_source()
    for gate in ("function drawGallery(", "function drawAnimationSheet("):
        body = _code(_js_block(source, gate))
        assert "applyEvalLayers()" in body, f"{gate} does not apply the layer roles"
        assert not re.search(r"layers\.\w+\.visible\s*=", body), (
            f"{gate} still sets a layer's visibility by hand, beside the rule"
        )


@needs_node
def test_the_eval_rule_hides_content_and_refuses_an_unclassified_layer():
    source = _world_source()

    def run(void: bool, extra: bool):
        driver = (
            f"const VOID_MODE = {str(void).lower()};\n"
            + _js_const(source, "EVAL_LAYER_ROLES")
            + "\nconst layers = {};\n"
            + "for (const k of Object.keys(EVAL_LAYER_ROLES)) layers[k] = { visible: true };\n"
            + ("layers.brandNew = { visible: true };\n" if extra else "")
            + _js_block(source, "function applyEvalLayers(")
            + """
            let error = null;
            try { applyEvalLayers(); } catch (e) { error = String(e); }
            const visible = {};
            for (const [k, v] of Object.entries(layers)) visible[k] = v.visible;
            console.log(JSON.stringify({ error, visible, roles: EVAL_LAYER_ROLES }));
            """
        )
        return _run_node(driver)

    for void in (False, True):
        out = run(void, extra=False)
        assert out["error"] is None
        for name, role in out["roles"].items():
            expected = {"scenery": not void, "content": False, "stage": True}[role]
            assert out["visible"][name] is expected, f"void={void}: {name} ({role})"
    assert "brandNew" in (run(False, extra=True)["error"] or ""), (
        "an unclassified layer reached the eval surface without complaint"
    )


# --- KI-073: an accent is mood-coloured only if it paints ---------------


def _mood_blind_driver(source: str, bodies_block: str) -> str:
    """Every body builder the page has — `BODIES.*` and `seatedRig` — run
    against Graphics that remember what they drew, then graded by the page's
    own `moodBlindParts`.

    `paint` here does what the real one does to its target that matters for
    the rule: it records the volume on `g.volumes` AND draws. A direct
    `.fill()` only draws. So an accent built the pre-Sprint-16 way shows up
    as drawn-but-unrecorded, which is exactly what `repaint` skips.
    """
    return (
        "const PLATE = null;\nconst PAINTED_CAST = null;\n"
        + "const BODY_FILL = 0xffffff, BODY_RIM = { color: 0xffffff };\n"
        + _js_const(source, "LIGHT")
        + _js_block(source, "const HUMAN = {") + ";\n"
        + _js_block(source, "const MACHINE = {") + ";\n"
        + _js_block(source, "function mixHex(") + "\n"
        + """
        class FakeGraphics {
          constructor() {
            this.pivot = { x: 0, y: 0, set() {} };
            this.rotation = 0; this.x = 0; this.y = 0;
            this.scale = { x: 1, y: 1, set() {} };
            this.context = { instructions: [] };
          }
          addChild(...cs) { (this.children || (this.children = [])).push(...cs); return cs[0]; }
          roundRect() { return this; } circle() { return this; } poly() { return this; }
          rect() { return this; } ellipse() { return this; }
          moveTo() { return this; } lineTo() { return this; } closePath() { return this; }
          fill() { this.context.instructions.push("fill"); return this; }
          stroke() { this.context.instructions.push("stroke"); return this; }
        }
        const PIXI = { Graphics: FakeGraphics };
        const volume = (shape) => ({ shape, band: (g) => g, rim: (g) => g });
        const litRect = () => volume("rect");
        const litCircle = () => volume("circle");
        const litPath = () => volume("path");
        function paint(g, v, fill, rim, opts) {
          (g.volumes || (g.volumes = [])).push(v);
          g.context.instructions.push("fill");
        }
        """
        + _js_block(source, "function snap(") + "\n"
        + _js_const(source, "CELL") + "\n"
        + _js_const(source, "CAST_SCALE") + "\n"
        + bodies_block + ";\n"
        + _seated_rig_block(source) + "\n"
        + _js_block(source, "function moodBlindParts(") + "\n"
        + """
        const out = {};
        for (const style of Object.keys(BODIES)) {
          const accents = [];
          BODIES[style](new FakeGraphics(), accents);
          out[style] = { accents: accents.length, blind: moodBlindParts(accents) };
        }
        const accents = [];
        seatedRig(new FakeGraphics(), accents, -1);
        out.seated = { accents: accents.length, blind: moodBlindParts(accents) };
        console.log(JSON.stringify(out));
        """
    )


@needs_node
def test_every_accent_the_page_builds_is_painted_and_so_takes_a_mood():
    """KI-073. `repaint` returns early from a Graphics with no recorded
    volumes — deliberately, for faces — so an accent drawn with a bare
    `.fill()` animates like every other limb and stays `BODY_FILL` grey
    through every mood, with nothing throwing. The container tint that used
    to colour it is gone. Every builder is run here, so the next accent
    written the old way fails the suite instead of reaching a frame."""
    source = _world_source()
    emitted = _run_node(
        _mood_blind_driver(source, _js_block(source, "const BODIES = {"))
    )

    assert set(emitted) >= {"candle", "bars", "monolith", "figure", "seated"}
    # Load-bearing: a builder that pushed nothing would pass vacuously.
    assert sum(b["accents"] for b in emitted.values()) >= 6, emitted
    for style, built in emitted.items():
        assert built["blind"] == [], (
            f"{style} draws {built['blind']} without paint() — "
            "repaint() skips it, so it never takes a mood"
        )


@needs_node
def test_an_accent_drawn_the_old_way_is_caught():
    """The mutation, kept: add one accent to `BODIES.candle` exactly as the
    pre-Sprint-16 code drew them, and the check above must name it."""
    source = _world_source()
    bodies = _js_block(source, "const BODIES = {")
    anchor = "accents.push(wickTop, wickLow);"
    assert anchor in bodies, "the candle's accent push moved; re-aim the mutation"
    mutated = bodies.replace(
        anchor,
        anchor + "\n        accents.push("
        "new PIXI.Graphics().roundRect(0, 0, 4, 4, 0).fill(BODY_FILL));",
    )
    emitted = _run_node(_mood_blind_driver(source, mutated))
    assert emitted["candle"]["blind"] == ["accents[2]"], emitted["candle"]


@needs_node
def test_a_mood_blind_part_is_found_below_its_accent():
    """`repaint` descends an accent's children (the seated forearm is a child
    of its upper arm), so the check must too — and must pass over a
    Graphics that drew nothing, which is a joint, not a part."""
    source = _world_source()
    driver = _js_block(source, "function moodBlindParts(") + """
        const g = (drew, volumes, children) => ({
          context: { instructions: drew ? ["fill"] : [] },
          volumes, children,
        });
        const arm = g(true, [{}], [g(true, undefined, []), g(false, undefined, [])]);
        const joint = g(false, undefined, [g(true, [{}], [])]);
        console.log(JSON.stringify(moodBlindParts([arm, joint])));
    """
    assert _run_node(driver) == ["accents[0].children[0]"]
