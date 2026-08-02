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


def test_canvas_preserves_its_drawing_buffer_for_obs_capture():
    """KI-013. Pixi 8 is WebGL-only and defaults to preserveDrawingBuffer:false,
    which discards the drawing buffer once the frame is composited. The page
    still looks right in a browser tab (the compositor holds the layer), but
    anything reading the canvas outside the render frame — which is what OBS's
    off-screen browser source does — gets an unreadable buffer, so the room
    freezes on the stream at whatever frame OBS captured first.

    Measured A/B in headless Chrome under software GL (2026-08-02): default →
    readback is opaque black (0,0,0,255) everywhere; with the flag → the exact
    background #131722 and the drawn shape colours. Corroboration: the charts
    (Canvas2D, always readable) and the DOM overlays never froze on stream —
    the room was the only WebGL surface.
    """
    body = client.get("/world").text
    assert "preserveDrawingBuffer: true" in body


def test_live_event_swell_keys_on_tier():
    # The swell must fire on live SSE events, not just the initial paint: react()
    # scales the nudge by the event's tier. Pin the hook so a refactor can't
    # silently flatten the room back to a constant nudge.
    body = client.get("/world").text
    assert "tierOf(event.severity)" in body
