"""B10 — the standby card: what a viewer sees instead of a frozen frame."""

import re

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_standby_renders():
    response = client.get("/standby")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_no_unsubstituted_placeholders():
    assert "__" not in _stripped(client.get("/standby").text)


def test_carries_the_shared_palette():
    """On-palette with the rest of the stream — a standby card in different
    colours reads as a different channel."""
    body = client.get("/standby").text
    assert "--bg" in body and "#131722" in body


def test_theme_block_is_not_nested_inside_another_rule():
    """`visuals.css_variables()` injects a complete `:root { … }` block. Wrap
    it in a second one and you get `:root { :root { … } }` — invalid CSS, so
    every var silently resolves to nothing and the page renders inverted
    (white on white) with no error anywhere. Caught only by looking at it in
    OBS, which is why it is pinned here."""
    css = re.sub(r"\s+", "", client.get("/standby").text)
    assert "{:root{" not in css


def test_states_change_the_copy():
    reconnecting = client.get("/standby?state=reconnecting").text
    starting = client.get("/standby?state=starting").text
    assert "Reconnecting" in reconnecting
    assert "starting" in starting.lower()
    assert reconnecting != starting


def test_unknown_state_falls_back_rather_than_erroring():
    """This page is the fallback surface — it must never be the thing that
    breaks. A bad query is not a reason to show a 500 on stream."""
    response = client.get("/standby?state=<script>")
    assert response.status_code == 200
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text or "Standing by" in response.text


def test_the_card_never_touches_the_network():
    """The whole point is that it works when things are broken. A fetch or an
    EventSource here would (a) burn one of KI-013's six per-origin connections
    and (b) leave the card blank in exactly the outage it exists for."""
    body = client.get("/standby").text
    for forbidden in ("fetch(", "EventSource", "XMLHttpRequest", "WebSocket"):
        assert forbidden not in body, forbidden


def test_no_innerhtml_assignment():
    assert "innerHTML" not in client.get("/standby").text


def _stripped(html: str) -> str:
    """Drop CSS custom properties (`--foo`) and comment rules, so the
    placeholder check only sees real `__NAME__` leftovers."""
    return html.replace("--", "")
