"""Chart pages and the dashboard (#44, moved from `api/main.py`)."""

import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from api.shared import _SYMBOL_PATTERN, _validated_symbol, logger
from api.templating import _render_template
from config.exceptions import BaseAppException
from scheduler.watchlist import load_watchlist
from storage.postgres_store import BAR_INTERVAL, get_latest_closes

router = APIRouter()


_ALLOWED_CHART_INTERVALS = {"1d", "1m"}


@router.get("/chart/{ticker_symbol}", response_class=HTMLResponse)
def chart(ticker_symbol: str, interval: str = BAR_INTERVAL):
    symbol = _validated_symbol(ticker_symbol)
    if interval not in _ALLOWED_CHART_INTERVALS:
        raise BaseAppException(f"Invalid interval: {interval!r}", status_code=400)
    return HTMLResponse(
        _render_template(
            "chart.html", {"__SYMBOL__": symbol, "__INTERVAL__": interval}
        )
    )


@router.get("/charts", response_class=HTMLResponse)
def charts(interval: str = BAR_INTERVAL, symbols: str | None = None):
    """Multi-symbol chart page (chart-focus scene): one candlestick panel per
    watchlist `predict` symbol — BTC *and* ETH — each with prediction markers.

    `?symbols=BTCUSDT` narrows it to a subset. That exists so a scene needing a
    single-symbol chart uses this chrome-free broadcast surface instead of
    `/chart/{symbol}`, whose nav link, status line and footer are meant for a
    human with a browser and went out on air until KI-027."""
    if interval not in _ALLOWED_CHART_INTERVALS:
        raise BaseAppException(f"Invalid interval: {interval!r}", status_code=400)
    available = [
        spec.symbol.upper()
        for spec in load_watchlist().tickers
        if spec.predict and _SYMBOL_PATTERN.fullmatch(spec.symbol.upper())
    ]
    deduped = list(dict.fromkeys(available))
    if symbols is not None:
        # Filtered against the watchlist, never taken from the query string:
        # these are rendered into the page's JS.
        wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in deduped]
        if not wanted or unknown:
            raise BaseAppException(
                f"Unknown chart symbol(s): {', '.join(unknown) or symbols!r}",
                status_code=400,
            )
        deduped = [s for s in deduped if s in wanted]
    return HTMLResponse(
        _render_template(
            "charts.html",
            {"__SYMBOLS__": json.dumps(deduped), "__INTERVAL__": interval},
        )
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    deduped = dict.fromkeys(spec.symbol.upper() for spec in load_watchlist().tickers)
    symbols = []
    for raw in deduped:
        if _SYMBOL_PATTERN.fullmatch(raw):
            symbols.append(raw)
        else:
            logger.warning("Skipping invalid watchlist symbol", extra={"symbol": raw})
    try:
        closes = {row["symbol"]: row for row in get_latest_closes(symbols)}
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Postgres unavailable: {e}", status_code=503)

    rows = []
    for symbol in symbols:
        row = closes.get(symbol)
        close = f"{row['close']:.2f}" if row and row["close"] is not None else "—"
        as_of = row["timestamp"][:10] if row else "—"
        rows.append(
            f'      <tr><td><a href="/chart/{symbol}">{symbol}</a></td>'
            f'<td class="num">{close}</td><td>{as_of}</td></tr>'
        )
    return HTMLResponse(
        _render_template("dashboard.html", {"__ROWS__": "\n".join(rows)})
    )
