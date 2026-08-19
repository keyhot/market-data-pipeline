import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app
from scheduler.watchlist import TickerJobSpec, Watchlist

client = TestClient(app)


def _watchlist(symbols=("BTCUSDT", "ETHUSDT"), predict=True):
    return Watchlist(
        interval_seconds=300,
        tickers=tuple(
            TickerJobSpec(s, "1d", market="crypto", predict=predict)
            for s in symbols
        ),
        events=(),
    )


def test_overlay_signals_renders_predict_symbols():
    with patch("api.main.load_watchlist", return_value=_watchlist()):
        response = client.get("/overlay/signals")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '["BTCUSDT", "ETHUSDT"]' in response.text
    assert "__SYMBOLS__" not in response.text
    assert "signal-based simulation" in response.text


def test_overlay_signals_excludes_non_predict_and_invalid_symbols():
    watchlist = Watchlist(
        interval_seconds=300,
        tickers=(
            TickerJobSpec("BTCUSDT", "1d", market="crypto", predict=True),
            TickerJobSpec("AAPL", "1d", predict=False),
            TickerJobSpec("BAD SYMBOL!", "1d", market="crypto", predict=True),
        ),
        events=(),
    )
    with patch("api.main.load_watchlist", return_value=watchlist):
        response = client.get("/overlay/signals")

    assert '["BTCUSDT"]' in response.text


def test_overlay_events_renders():
    response = client.get("/overlay/events")

    assert response.status_code == 200
    assert "/stream/world/events" in response.text
    assert "textContent" in response.text  # XSS-safe rendering marker


def test_overlay_events_carries_shared_theme_and_tier_swell():
    body = client.get("/overlay/events").text
    assert "--bg: #131722" in body       # shared palette reached the overlay
    assert ".tier-3" in body             # the shared swell ramp is injected
    # The swell fires on the live SSE path, not just initial paint.
    assert "tier-${tierOf(e.event_type, e.severity)}" in body
    assert not re.search(r"__[A-Z_]+__", body)  # every placeholder substituted


def test_every_known_event_type_has_an_overlay_headline():
    """The JS HEADLINES map has a silent fallback, so a new salience rule
    renders as raw `market broadcast_live` on a live 24/7 stream and no test
    notices. Same registry invariant world/reactions.py is held to, on the
    other side of the SSE boundary."""
    from world.salience import KNOWN_EVENT_TYPES

    body = client.get("/overlay/events").text
    missing = sorted(
        etype
        for etype in KNOWN_EVENT_TYPES
        if not re.search(rf"\b{etype}:\s*\(e\)", body)
    )
    assert missing == [], f"event types with no overlay headline: {missing}"


def test_overlay_signals_uses_shared_palette_vars():
    with patch("api.main.load_watchlist", return_value=_watchlist()):
        body = client.get("/overlay/signals").text
    assert "--bg: #131722" in body
    assert "var(--up)" in body and "var(--down)" in body


# --- B3: overlay & typography polish -------------------------------------
#
# Both overlays are mounted at more than one size by `scripts/stream_scene.py`
# (`/overlay/events` at 480x840 and 960x1080; `/overlay/signals` at 1920x120
# and 960x120), and they are read by a newcomer who has never seen the project.
# These pin the parts of "polish" that a screenshot can't grade.


def _css_rules(body: str) -> dict[str, str]:
    """Crude `selector -> declarations` split of a page's <style> block. Enough
    to assert *which* rule owns a property, which is the thing that goes wrong
    here (see KI-019: two surfaces inventing their own copy of a server rule)."""
    style = re.search(r"<style>(.*?)</style>", body, re.S).group(1)
    style = re.sub(r"@keyframes\s+[\w-]+\s*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", style)
    return {
        selector.strip(): declarations
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", style)
    }


def test_event_rows_refresh_their_age_from_one_shared_timer():
    """KI-022. A row's `… ago` was written once and never rewritten, and OBS
    keeps a browser source's page loaded for the life of the process — so the
    rail has been telling viewers on a 24/7 stream that a six-hour-old event
    happened `0s ago`. The timestamp must be kept on the row and re-rendered
    on ONE interval for all rows (one timer, not one per row)."""
    body = client.get("/overlay/events").text
    assert "dataset.occurredAt" in body       # the truth stays on the row
    assert body.count("setInterval(") == 1    # one shared tick, not per row
    assert re.search(r"setInterval\(\s*refreshAges", body)


def test_age_styling_never_touches_the_tier_ramp_properties():
    """`visuals.tier_styles_css` owns transform / opacity / box-shadow /
    font-weight — that ramp IS the swell. A second rule setting any of them
    silently fights the server's severity signal, which is KI-019 one surface
    over, so age must fade on a disjoint property (colour)."""
    tier_owned = ("transform", "opacity", "box-shadow", "font-weight")
    rules = _css_rules(client.get("/overlay/events").text)
    aging = {sel: decl for sel, decl in rules.items() if "stale" in sel}
    assert aging, "no age-fade rule found"
    for selector, declarations in aging.items():
        for prop in tier_owned:
            assert prop not in declarations, f"{selector} fights the tier ramp"


def test_the_entry_animation_does_not_fight_the_tier_scale():
    """A keyframe on the row itself overrides the tier `transform: scale()`
    for its whole duration (animation beats transition), so the newest and
    most dramatic row is the one rendered at the wrong size. The motion goes
    on an inner element instead, where it composes."""
    rules = _css_rules(client.get("/overlay/events").text)
    fresh = {sel: decl for sel, decl in rules.items() if ".fresh" in sel}
    assert fresh, "no entry animation found"
    for selector, declarations in fresh.items():
        if "animation" in declarations:
            assert selector.strip() != ".event.fresh", (
                "the entry animation is on the row that carries the tier scale"
            )


def test_the_feed_fills_whatever_source_it_is_mounted_in():
    """Mounted at 480 wide on chart-focus and 960 wide on event-focus. A fixed
    `max-width: 480px` throws away half of the event-focus source."""
    rules = _css_rules(client.get("/overlay/events").text)
    feed = rules["#feed"]
    assert "max-width: 480px" not in feed
    assert "width: 100%" in feed


def test_the_rail_trims_rows_it_cannot_show_rather_than_clipping_them():
    """Mounted 480x840 and 960x1080: headlines wrap at the narrow one and don't
    at the wide one, so a fixed row count is wrong at one of them — and wrong
    here means `overflow: hidden` slicing a row through the middle of its text
    with nothing saying it did. The count is measured against the source."""
    body = client.get("/overlay/events").text
    # Bounding rects, not scrollHeight: the tier ramp scales the row past its
    # layout box, and a swelling bottom row is exactly the one worth measuring.
    assert "getBoundingClientRect().bottom > feed.getBoundingClientRect()" in body
    assert "feed.children.length > 1" in body     # never empties the rail


def test_overlay_events_uses_the_shared_surface_var():
    """The row background was a literal sitting beside vars for every other
    colour — the palette is only one source of truth if the page uses it."""
    body = client.get("/overlay/events").text
    assert "var(--surface)" in body
    page = re.sub(r":root\s*\{[^}]*\}", "", body, count=2)   # minus the palette
    assert "#1e222d" not in page   # the literal the var replaced


def test_both_overlays_say_what_they_are_showing():
    """Newcomer legibility beyond the /world banner: a viewer arriving mid-
    stream sees a rail of rows and a strip of dots with nothing naming either."""
    events = client.get("/overlay/events").text
    assert "Live events" in events
    with patch("api.main.load_watchlist", return_value=_watchlist()):
        signals = client.get("/overlay/signals").text
    assert "Model calls" in signals
    assert "last 10 resolved" in signals   # the dots, named


def test_signal_cells_share_the_strip_width():
    """1920 wide on two scenes, 960 on a third, and a third predict symbol
    would silently overflow the fixed 260px cells."""
    with patch("api.main.load_watchlist", return_value=_watchlist()):
        rules = _css_rules(client.get("/overlay/signals").text)
    assert "flex: 1" in rules[".cell"]
    assert "min-width: 260px" not in rules[".cell"]


def test_neither_overlay_nests_the_theme_block_in_another_rule():
    """`visuals.css_variables()` already emits a complete `:root { … }`. B10
    wrapped it in a second one and the standby card rendered white-on-white
    with a green suite. Same pin, both stream surfaces."""
    with patch("api.main.load_watchlist", return_value=_watchlist()):
        pages = [client.get("/overlay/events").text,
                 client.get("/overlay/signals").text]
    for body in pages:
        assert "{:root{" not in re.sub(r"\s+", "", body)


def test_the_strip_drops_the_caption_before_the_number():
    """At the 960-wide mount the row ran out of room and ellipsised its END —
    losing `hit 44% of 50`, the honest record this strip exists to show, while
    keeping the caption that explains the dots. Priority is explicit now."""
    with patch("api.main.load_watchlist", return_value=_watchlist()):
        body = client.get("/overlay/signals").text
    narrow = re.search(r"@container[^{]*\{(.*?)\n    \}", body, re.S).group(1)
    assert ".dotlabel { display: none; }" in narrow
    assert "display: none" not in narrow.split(".stats")[-1]
