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
    assert "tier-${tierOf(e.severity)}" in body
    assert not re.search(r"__[A-Z_]+__", body)  # every placeholder substituted


def test_overlay_signals_uses_shared_palette_vars():
    with patch("api.main.load_watchlist", return_value=_watchlist()):
        body = client.get("/overlay/signals").text
    assert "--bg: #131722" in body
    assert "var(--up)" in body and "var(--down)" in body
