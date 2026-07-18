from datetime import datetime, timezone

from scheduler.market_hours import is_equity_market_open

# 2026-07-20 is a Monday; US Eastern is UTC-4 in July.


def _utc(hour, minute=0, day=20):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def test_open_during_regular_session():
    assert is_equity_market_open(_utc(14, 0)) is True  # 10:00 ET


def test_closed_before_open_and_after_close():
    assert is_equity_market_open(_utc(13, 29)) is False  # 09:29 ET
    assert is_equity_market_open(_utc(13, 30)) is True  # 09:30 ET boundary
    assert is_equity_market_open(_utc(20, 0)) is False  # 16:00 ET boundary


def test_closed_on_weekend():
    assert is_equity_market_open(_utc(14, 0, day=18)) is False  # Saturday
    assert is_equity_market_open(_utc(14, 0, day=19)) is False  # Sunday
