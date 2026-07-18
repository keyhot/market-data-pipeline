"""US equity market hours; crypto trades 24/7 and never consults this."""

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def is_equity_market_open(now: datetime | None = None) -> bool:
    """Regular NYSE/Nasdaq session: Mon-Fri 9:30-16:00 America/New_York.
    Exchange holidays are not modeled — a closed-day fetch is harmless
    (idempotent upsert of the last session's bar)."""
    if now is None:
        now = datetime.now(timezone.utc)
    local = now.astimezone(MARKET_TZ)
    if local.weekday() >= 5:
        return False
    return MARKET_OPEN <= local.time() < MARKET_CLOSE
