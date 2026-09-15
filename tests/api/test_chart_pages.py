import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from scheduler.watchlist import TickerJobSpec, Watchlist

client = TestClient(app)


def test_chart_page_serves_html_with_symbol():
    response = client.get("/chart/aapl")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'const SYMBOL = "AAPL"' in response.text
    assert 'const INTERVAL = "1d"' in response.text
    assert "/stream/bars/" in response.text
    assert "__SYMBOL__" not in response.text
    assert "__INTERVAL__" not in response.text


def test_chart_page_renders_prediction_markers():
    # B11: the model's calls are drawn ON the chart at their bar, colored by
    # outcome — the track record made visible, not just a p= number.
    body = client.get("/chart/BTCUSDT?interval=1m").text
    assert "/signals/" in body        # pulls the model's calls
    assert "setMarkers" in body       # draws them on the candlestick series
    assert "arrowUp" in body and "arrowDown" in body


def _predict_watchlist(symbols=("BTCUSDT", "ETHUSDT")):
    return Watchlist(
        interval_seconds=300,
        tickers=tuple(
            TickerJobSpec(s, "1d", market="crypto", predict=True) for s in symbols
        ),
        events=(),
    )


def test_charts_page_renders_all_predict_symbols_with_markers():
    # B13: the stream charts BTC AND ETH (every predict symbol), each panel with
    # its own prediction markers.
    with patch("api.main.load_watchlist", return_value=_predict_watchlist()):
        body = client.get("/charts?interval=1m").text
    assert "BTCUSDT" in body and "ETHUSDT" in body
    assert "setMarkers" in body
    assert "__SYMBOLS__" not in body and "__INTERVAL__" not in body


def test_charts_page_rejects_bad_interval():
    resp = client.get("/charts?interval=5m")
    assert resp.status_code == 400


def test_chart_page_rejects_injection_attempts():
    assert client.get("/chart/%3Cscript%3E").status_code == 400
    assert client.get("/chart/AAPL%22%3E").status_code == 400


def test_chart_page_rejects_overlong_symbol():
    assert client.get("/chart/" + "A" * 16).status_code == 400


def _watchlist():
    return Watchlist(
        interval_seconds=300,
        tickers=(TickerJobSpec("AAPL", "1d"), TickerJobSpec("MSFT", "1d")),
        events=(),
    )


def test_dashboard_lists_watchlist_symbols_with_closes():
    closes = [
        {"symbol": "AAPL", "timestamp": "2026-07-16T00:00:00+00:00", "close": 231.5}
    ]
    with (
        patch("api.main.load_watchlist", return_value=_watchlist()),
        patch("api.main.get_latest_closes", return_value=closes) as reader,
    ):
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "231.5" in response.text
    assert 'href="/chart/AAPL"' in response.text
    # MSFT has no stored bars yet — still listed, with a placeholder.
    assert 'href="/chart/MSFT"' in response.text
    assert "—" in response.text
    reader.assert_called_once_with(["AAPL", "MSFT"])


def test_dashboard_503_when_postgres_down():
    with (
        patch("api.main.load_watchlist", return_value=_watchlist()),
        patch("api.main.get_latest_closes", side_effect=RuntimeError("down")),
    ):
        assert client.get("/dashboard").status_code == 503


def test_dashboard_skips_symbols_failing_the_whitelist():
    watchlist = Watchlist(
        interval_seconds=300,
        tickers=(
            TickerJobSpec('AAPL"><script>', "1d"),
            TickerJobSpec("AAPL", "1d"),
        ),
        events=(),
    )
    with (
        patch("api.main.load_watchlist", return_value=watchlist),
        patch("api.main.get_latest_closes", return_value=[]) as reader,
    ):
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert '"><' not in response.text
    assert 'href="/chart/AAPL"' in response.text
    reader.assert_called_once_with(["AAPL"])


def test_dashboard_renders_dash_for_null_close():
    closes = [
        {"symbol": "AAPL", "timestamp": "2026-07-16T00:00:00+00:00", "close": None}
    ]
    with (
        patch("api.main.load_watchlist", return_value=_watchlist()),
        patch("api.main.get_latest_closes", return_value=closes),
    ):
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert "—" in response.text
    assert ">None<" not in response.text


def test_charts_page_can_be_narrowed_to_one_symbol():
    """KI-027: `event-focus` needs a single-symbol chart without the app's
    chrome. `/charts` is already the chrome-free broadcast surface, so it grows
    a filter rather than the dashboard page growing a `?chrome=0`."""
    with patch("api.main.load_watchlist", return_value=_predict_watchlist()):
        body = client.get("/charts?interval=1m&symbols=BTCUSDT").text
    assert "BTCUSDT" in body
    assert "ETHUSDT" not in body


def test_charts_page_rejects_a_symbol_that_is_not_on_the_watchlist():
    """The page renders the symbols into JS. Whatever reaches it has to come
    from the watchlist, never straight from the query string."""
    with patch("api.main.load_watchlist", return_value=_predict_watchlist()):
        resp = client.get("/charts?symbols=DOGEUSDT")
    assert resp.status_code == 400


# --- markers stay inside the candles they annotate -----------------------------

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
_CHARTS = Path(__file__).resolve().parents[2] / "api" / "templates" / "charts.html"


def _js_function(source: str, signature: str) -> str:
    """One top-level function of the page, lifted verbatim by brace matching."""
    start = source.index(signature)
    depth = 0
    for i in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            return source[start : i + 1]
    raise AssertionError(f"unterminated {signature}")


def _markers(interval: str, signals: list[dict], first_bar_time) -> list:
    source = _CHARTS.read_text()
    driver = (
        f"const INTERVAL = {json.dumps(interval)};\n"
        'const _OUTCOME_COLOR = { win: "#26a69a", loss: "#ef5350" };\n'
        + _js_function(source, "function barTime(")
        + "\n"
        + _js_function(source, "function signalMarkers(")
        + "\n"
        + f"console.log(JSON.stringify(signalMarkers({json.dumps(signals)}, "
        f"{json.dumps(first_bar_time)})));"
    )
    result = subprocess.run(
        [NODE, "-e", driver], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _signal(ts: str, direction: str = "up", outcome: str | None = "win") -> dict:
    return {"signal_timestamp": ts, "direction": direction,
            "outcome": outcome, "probability": 0.8}


@needs_node
def test_calls_older_than_the_first_candle_are_not_drawn():
    """The page loads 250 1m candles but 200 calls, which reach further back.
    Lightweight Charts pins a marker with no bar to the NEAREST bar, so every
    call older than the window landed on the first candle: event-focus went
    out with a column of thirty stacked `p=` labels down its left edge."""
    markers = _markers(
        "1m",
        [_signal("2026-09-15T01:10:00Z"), _signal("2026-09-15T01:59:00Z"),
         _signal("2026-09-15T02:00:00Z"), _signal("2026-09-15T02:30:00Z", "down")],
        1789437600,  # 2026-09-15T02:00:00Z, barTime() of the first candle
    )
    assert [m["time"] for m in markers] == [1789437600, 1789439400]
    assert markers[1]["shape"] == "arrowDown"


@needs_node
def test_daily_charts_filter_on_the_date_not_the_instant():
    markers = _markers(
        "1d",
        [_signal("2026-09-13T00:00:00Z"), _signal("2026-09-14T00:00:00Z"),
         _signal("2026-09-15T00:00:00Z")],
        "2026-09-14",
    )
    assert [m["time"] for m in markers] == ["2026-09-14", "2026-09-15"]


@needs_node
def test_markers_are_sorted_and_keep_their_outcome_colour():
    markers = _markers(
        "1m",
        [_signal("2026-09-15T02:30:00Z", outcome="loss"),
         _signal("2026-09-15T02:10:00Z", outcome=None)],
        1789437600,
    )
    assert [m["time"] for m in markers] == [1789438200, 1789439400]
    assert [m["color"] for m in markers] == ["#787b86", "#ef5350"]
