import pandas as pd

from config.exceptions import (
    BaseAppException,
    InvalidDateError,
    NoDataFoundError,
)
from ingestion.factory import get_default_provider
from ingestion.providers import MarketDataProvider

NEWS_COLUMNS = ["id", "title", "publisher", "url", "published_at", "summary"]


def fetch_news(
    ticker_symbol: str,
    limit: int | None = None,
    since: str | None = None,
    provider: MarketDataProvider | None = None,
) -> pd.DataFrame:
    if provider is None:
        provider = get_default_provider()

    try:
        raw = provider.get_news(ticker_symbol)
    except BaseAppException:
        raise
    except Exception as e:
        raise BaseAppException(f"Failed to fetch news: {e}", status_code=503)

    news = normalize_news(raw)

    if since is not None:
        try:
            since_ts = pd.to_datetime(since)
        except ValueError as e:
            raise InvalidDateError(f"Invalid date: {e}") from e
        if since_ts.tzinfo is None:
            since_ts = since_ts.tz_localize("UTC")
        news = news[news["published_at"] >= since_ts]

    if limit is not None:
        news = news.head(limit)

    if news.empty:
        raise NoDataFoundError("No news found for the given parameters")

    return news.reset_index(drop=True)


def normalize_news(raw: list[dict] | None) -> pd.DataFrame:
    rows = []
    for item in raw or []:
        content = item.get("content") or {}
        rows.append(
            {
                "id": content.get("id") or item.get("id"),
                "title": content.get("title"),
                "publisher": (content.get("provider") or {}).get("displayName"),
                "url": (content.get("canonicalUrl") or {}).get("url"),
                "published_at": content.get("pubDate"),
                "summary": content.get("summary"),
            }
        )

    news = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    if not news.empty:
        news["published_at"] = pd.to_datetime(
            news["published_at"], utc=True, errors="coerce"
        )
        news = news.sort_values("published_at", ascending=False).reset_index(drop=True)
    return news
