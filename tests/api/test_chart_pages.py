from unittest.mock import patch

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
