"""#41: `/world` is handed ONE JSON config block, not ~20 text placeholders.

The page used to be a blind `str.replace` target: fifteen-odd tokens, each
spliced into JavaScript as a literal (and two of them as generated CODE). A
value containing `</script>` ended the inline script mid-statement, a stale dev
server that re-read the template but not `api/main.py` served the raw tokens as
a syntax error, and `0x...` hex literals were numbers only because of how
Python happened to print them.

Now the server renders one `<script type="application/json" id="world-config">`
block, escaped for the script context, and the page parses it once. These tests
pin the seam from both ends: what the server emits, what the page reads, and
that the two key sets are the same set.
"""

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as main
import api.routers.world as world_router
from api.main import app
from api.templating import world_page_source
from world import monitors, reactions, visuals
from world.light import as_json as light_as_json
from world.light import light_for
from world.plate import load_manifest
from world.state import GENERIC_TIER_CUTS, severity_tier, tier_cuts
from world.text_layout import as_json as text_as_json

client = TestClient(app)
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="node is not installed on this host"
)

TEMPLATE = Path(main.__file__).resolve().parent / "templates" / "world.html"

# Page const -> config key. The page keeps its own names (hundreds of lines read
# them); the right-hand side of each is the ONLY thing that changed.
PAGE_CONSTS = {
    "SYMBOLS": "symbols",
    "BANNER_PREFIX": "banner_prefix",
    "PLATE": "plate",
    "PAINTED_CAST": "painted_cast",
    "TEXT_PLACEMENTS": "text",
    "LIGHT": "light",
    "MOOD_COLOR": "mood_colors",
    "NEUTRAL": "neutral_color",
    "NEUTRAL_TINT": "neutral_tint",
    "BODY_TINT": "body_tints",
    "BODY_FILL": "body_base_fill",
    "TIER_CUTS": "tier_cuts",
    "ROOM_LIGHT": "room_light",
    "REACTIONS": "reactions",
    "MONITOR_RULES": "monitor_rules",
    "CHARACTER_COLOR": "character_colors",
}


class _Scripts(HTMLParser):
    """`<script>` elements as (attrs, text). The stdlib parser treats script
    content as CDATA, which is exactly the rule a browser applies - so a block
    that survives it survives the page."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.found = []
        self._open = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._open = (dict(attrs), [])

    def handle_data(self, data):
        if self._open is not None:
            self._open[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._open is not None:
            attrs, parts = self._open
            self.found.append((attrs, "".join(parts)))
            self._open = None


def _json_blocks(body: str) -> list[tuple[dict, str]]:
    parser = _Scripts()
    parser.feed(body)
    return [(a, t) for a, t in parser.found if a.get("type") == "application/json"]


def _config_text(body: str) -> str:
    blocks = _json_blocks(body)
    assert len(blocks) == 1, f"expected exactly one JSON block, found {len(blocks)}"
    attrs, text = blocks[0]
    assert attrs.get("id") == "world-config"
    return text


def _served_config(body: str | None = None) -> dict:
    return json.loads(
        _config_text(
            body
            if body is not None
            else world_page_source(page=client.get("/world").text)
        )
    )


def _function_text(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth = 0
    for i in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            return source[start : i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _node(script: str, stdin: str | None = None):
    result = subprocess.run(
        [NODE, "-e", script], input=stdin, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- the block itself ---------------------------------------------------------


def test_the_page_carries_exactly_one_json_config_block():
    body = world_page_source(page=client.get("/world").text)
    _config_text(body)  # asserts one block, with the right id
    block = body.index('id="world-config"')
    # An inline script runs while the document is still being parsed, so a block
    # placed after it would be `null` to `getElementById`.
    assert block < body.index("<script src="), "the config must precede every script"
    assert block < body.index('<script type="module" src="/static/world/main.js">'), (
        "the config must precede the page's module script"
    )


def test_the_page_parses_the_block_once_and_no_old_token_is_left():
    source = world_page_source()
    assert (
        source.count('JSON.parse(document.getElementById("world-config").textContent)')
        == 1
    )
    assert len(re.findall(r"\bconst WORLD_CONFIG\b", source)) == 1
    tokens = set(re.findall(r"__[A-Z_]+__", source))
    assert tokens == {"__THEME_VARS__", "__WORLD_CONFIG__"}, (
        "the template grew (or kept) a text placeholder; the only two allowed "
        f"are the CSS theme block and the config block's own slot: {sorted(tokens)}"
    )
    body = world_page_source(page=client.get("/world").text)
    assert not re.search(r"__[A-Z_]+__", body), "a placeholder reached the served page"


def test_keys_the_server_emits_are_the_keys_the_page_reads():
    source = world_page_source()
    assert "WORLD_CONFIG[" not in source, "bracket access hides a key from this test"
    read = set(re.findall(r"\bWORLD_CONFIG\.(\w+)", source))
    emitted = set(_served_config())
    assert emitted - read == set(), f"emitted but never read: {sorted(emitted - read)}"
    assert read - emitted == set(), f"read but never emitted: {sorted(read - emitted)}"


def test_every_page_const_reads_its_own_config_key():
    source = world_page_source()
    for const, key in PAGE_CONSTS.items():
        assert re.search(rf"^\s*const {const} = WORLD_CONFIG\.{key};$", source, re.M), (
            f"`const {const}` no longer reads WORLD_CONFIG.{key} on a line of its own"
        )
    assert re.search(
        r"const BODY_RIM = \{ width: 1\.5, color: WORLD_CONFIG\.body_rim_fill, "
        r"alignment: 1 \};",
        source,
    )
    # The second room-light site copies from the first; it does not read the key.
    assert "light: { ...ROOM_LIGHT[0] }" in source


# --- every value the old placeholders carried ----------------------------------


def test_the_config_carries_every_value_the_placeholders_did():
    config = _served_config()
    manifest = load_manifest()

    assert config["symbols"] == [
        s for s in dict.fromkeys(
            spec.symbol.upper()
            for spec in world_router.load_watchlist().tickers
            if spec.market == "crypto"
        )
    ]
    assert config["plate"] == manifest.as_dict()
    assert config["painted_cast"] == manifest.cast_payload()
    assert config["light"] == json.loads(light_as_json(light_for(manifest)))
    assert config["text"] == json.loads(text_as_json(manifest))
    assert config["mood_colors"] == visuals.MOOD_COLORS
    assert config["body_tints"] == visuals.body_tints()
    assert config["room_light"] == [visuals.room_light(t) for t in range(4)]
    assert config["character_colors"] == visuals.CHARACTER_COLORS
    assert config["monitor_rules"] == monitors.monitor_rules()
    assert config["tier_cuts"] == {
        "cuts": {k: list(v) for k, v in tier_cuts().items()},
        "generic": list(GENERIC_TIER_CUTS),
    }

    # These four were JS hex literals - NUMBERS. A "0x..." string would parse,
    # then silently stop being a colour the moment PIXI multiplied it.
    assert config["neutral_color"] == int(visuals.PALETTE["neutral"][1:], 16)
    assert config["neutral_tint"] == int(visuals.neutral_body_tint()[1:], 16)
    assert config["body_base_fill"] == visuals.BODY_BASE_FILL
    assert config["body_rim_fill"] == visuals.BODY_RIM_FILL
    for key in ("neutral_color", "neutral_tint", "body_base_fill", "body_rim_fill"):
        assert type(config[key]) is int, key


def test_the_reaction_registry_reaches_the_page_whole():
    got = _served_config()["reactions"]
    assert set(got) == {"base", "outcomes", "directions", "fallback"}
    assert got["base"] == {
        t: {"mood": m, "animation": a} for t, (m, a) in reactions.REACTIONS.items()
    }
    assert got["outcomes"] == {
        t: {"mood": m, "animation": a}
        for t, (m, a) in reactions._SIGNAL_OUTCOMES.items()
    }
    assert got["directions"] == {
        t: {"mood": m, "animation": a}
        for t, (m, a) in reactions._STREAK_DIRECTIONS.items()
    }
    assert got["fallback"] == {
        "mood": reactions._FALLBACK[0],
        "animation": reactions._FALLBACK[1],
    }


def test_no_plate_still_serves_null_for_the_plate_and_the_cast(monkeypatch):
    """Same fallback as before: the procedural room is a supported way to run,
    so the keys are present and `null` - never absent, never defaulted in JS."""
    monkeypatch.setattr(world_router, "load_manifest", lambda: None)
    config = _served_config()
    assert config["plate"] is None
    assert config["painted_cast"] is None
    assert config["light"] == json.loads(light_as_json(light_for(None)))
    assert config["text"] == json.loads(text_as_json(None))
    assert set(config) == set(PAGE_CONSTS.values()) | {"body_rim_fill"}


# --- escaping --------------------------------------------------------------------

HOSTILE = {
    "close</script><script>alert(1)</script>": "</SCRIPT> <!-- <script> -->",
    'quote"and\'apostrophe\\back\\"slash': "&amp; &lt; &#x27;    ",
    "non-ascii é 日本語 \U0001f4c8": "\u0000\u001f\n\t",
}


def _hostile_body(monkeypatch) -> str:
    monkeypatch.setattr(visuals, "CHARACTER_COLORS", HOSTILE)
    return world_page_source(page=client.get("/world").text)


def test_a_hostile_value_cannot_break_out_of_the_block(monkeypatch):
    clean = world_page_source(page=client.get("/world").text)
    body = _hostile_body(monkeypatch)
    text = _config_text(body)
    # The three characters that matter in a script context are never raw.
    assert not set("<>&") & set(text)
    assert json.loads(text)["character_colors"] == HOSTILE
    # Nothing the value said became markup: same number of script elements.
    assert body.lower().count("</script") == clean.lower().count("</script")
    assert body.lower().count("<script") == clean.lower().count("<script")
    assert "alert(1)" not in body.replace(text, ""), "the payload escaped the block"


def test_a_value_that_looks_like_a_token_survives_substitution(monkeypatch):
    """`_render_template` is a blind whole-document `.replace()`. The config is
    substituted LAST, so a value spelling a real token is data, not a slot."""
    hostile = {"__THEME_VARS__": "__WORLD_CONFIG__", "x": "__TIER_OF_JS__"}
    monkeypatch.setattr(visuals, "CHARACTER_COLORS", hostile)
    config = _served_config(world_page_source(page=client.get("/world").text))
    assert config["character_colors"] == hostile


@needs_node
def test_the_browsers_own_parser_reads_a_hostile_block_back(monkeypatch):
    """Python round-tripping its own output proves little; JSON.parse on the
    element's text is what the page runs."""
    text = _config_text(_hostile_body(monkeypatch))
    got = _node(
        "const t = require('fs').readFileSync(0, 'utf8');"
        "console.log(JSON.stringify(JSON.parse(t).character_colors));",
        stdin=text,
    )
    assert got == HOSTILE


# --- the page's JS reads the config -----------------------------------------------


@needs_node
@pytest.mark.parametrize("hostile", [False, True])
def test_the_pages_const_lines_resolve_to_the_config_through_json_parse(
    monkeypatch, hostile
):
    """Runs the page's own `const X = WORLD_CONFIG.y;` lines against the served
    block through a `document` stub, so the whole path - element text,
    JSON.parse, the const - is exercised rather than read off the source."""
    body = (
        _hostile_body(monkeypatch)
        if hostile
        else world_page_source(page=client.get("/world").text)
    )
    lines = [
        line.strip()
        for line in body.splitlines()
        if re.match(r"^\s*const (\w+) = WORLD_CONFIG\.\w+;$", line)
        and re.match(r"^\s*const (\w+)", line).group(1) in PAGE_CONSTS
    ]
    assert len(lines) == len(PAGE_CONSTS), "a const line moved or was duplicated"
    prologue = next(
        line.strip() for line in body.splitlines() if "const WORLD_CONFIG =" in line
    )
    rim = next(
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith("const BODY_RIM = ")
    )
    names = [*PAGE_CONSTS, "BODY_RIM"]
    script = (
        "const document = { getElementById: (id) => id === 'world-config' ? "
        "({ textContent: require('fs').readFileSync(0, 'utf8') }) : null };\n"
        + prologue
        + "\n"
        + "\n".join([*lines, rim])
        + f"\nconsole.log(JSON.stringify({{ {', '.join(names)} }}));"
    )
    got = _node(script, stdin=_config_text(body))
    config = json.loads(_config_text(body))
    for const, key in PAGE_CONSTS.items():
        assert got[const] == config[key], const
    assert got["BODY_RIM"] == {
        "width": 1.5,
        "color": config["body_rim_fill"],
        "alignment": 1,
    }


# --- the tier function: page code, not generated code ------------------------------


def test_world_has_one_tier_function_and_it_is_page_code():
    body = world_page_source(page=client.get("/world").text)
    assert body.count("function tierOf") == 1
    assert "const TIER_CUTS = WORLD_CONFIG.tier_cuts;" in body
    overlay = client.get("/overlay/events").text
    assert overlay.count("function tierOf") == 1


@needs_node
def test_the_pages_tier_function_agrees_with_the_server_rule():
    body = world_page_source(page=client.get("/world").text)
    config = _served_config(body)
    probes = []
    for rule in [*tier_cuts(), "an_unlisted_rule"]:
        cuts = tier_cuts().get(rule, GENERIC_TIER_CUTS)
        for cut in cuts:
            for severity in (cut - 1e-9, cut, cut + 1e-9):
                probes.append((rule, severity))
        probes += [(rule, 0), (rule, 1e6)]
    script = (
        f"const TIER_CUTS = {json.dumps(config['tier_cuts'])};\n"
        + _function_text(body, "tierOf")
        + f"\nconst probes = {json.dumps(probes)};\n"
        "console.log(JSON.stringify(probes.map(([r, s]) => tierOf(r, s))));"
    )
    assert _node(script) == [severity_tier(r, s) for r, s in probes]
