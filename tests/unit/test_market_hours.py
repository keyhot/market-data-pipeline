from datetime import datetime, timezone

import pytest

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


# --- Winter and the two US DST transitions ---------------------------------
#
# Every test above is in July, where America/New_York is UTC-4, so a fixed
# UTC-4 offset passes all of them. It would gate equities an hour off from
# November to March: the 15:00-16:00 ET hour skipped, and 08:30-09:30 ET
# fetched. The expectations below are literal UTC instants, never computed
# through ZoneInfo, so they cannot agree with the code by construction.
#
# 2026 transitions: US spring-forward Sun Mar 8 and fall-back Sun Nov 1; EU
# Mar 29 and Oct 25. market_hours reads no EU rule and no host timezone, so
# the EU/US gap weeks (Mar 9-27, Oct 26-31) are not special to it; they are
# covered only because a US session there sits 5h, not 6h, from Berlin, which
# is where an offset borrowed from a European zone would be wrong.


def _z(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


# (UTC instant, expected, what it is in New York)
_WINTER_AND_TRANSITIONS = [
    # Mon 2026-01-12, EST (UTC-5): the session is 14:30Z-21:00Z.
    (_z(1, 12, 13, 30), False, "Jan 08:30 EST; a fixed UTC-4 would say open"),
    (_z(1, 12, 14, 29), False, "Jan 09:29 EST"),
    (_z(1, 12, 14, 30), True, "Jan 09:30 EST open boundary"),
    (_z(1, 12, 20, 0), True, "Jan 15:00 EST; a fixed UTC-4 would say closed"),
    (_z(1, 12, 20, 59), True, "Jan 15:59 EST"),
    (_z(1, 12, 21, 0), False, "Jan 16:00 EST close boundary"),
    # Spring-forward week. Fri Mar 6 is still EST, Mon Mar 9 is already EDT.
    (_z(3, 6, 13, 30), False, "Fri Mar 6 08:30 EST"),
    (_z(3, 6, 14, 30), True, "Fri Mar 6 09:30 EST open"),
    (_z(3, 6, 20, 0), True, "Fri Mar 6 15:00 EST"),
    (_z(3, 6, 21, 0), False, "Fri Mar 6 16:00 EST close"),
    (_z(3, 9, 13, 29), False, "Mon Mar 9 09:29 EDT"),
    (_z(3, 9, 13, 30), True, "Mon Mar 9 09:30 EDT open"),
    (_z(3, 9, 19, 59), True, "Mon Mar 9 15:59 EDT"),
    (_z(3, 9, 20, 0), False, "Mon Mar 9 16:00 EDT close"),
    # The change itself, and the weekend around it, is closed all day.
    (_z(3, 7, 15, 0), False, "Sat Mar 7"),
    (_z(3, 8, 15, 0), False, "Sun Mar 8, the day the clocks go forward"),
    # Fall-back week. Fri Oct 30 is still EDT, Mon Nov 2 is already EST.
    (_z(10, 30, 13, 29), False, "Fri Oct 30 09:29 EDT"),
    (_z(10, 30, 13, 30), True, "Fri Oct 30 09:30 EDT open"),
    (_z(10, 30, 19, 59), True, "Fri Oct 30 15:59 EDT"),
    (_z(10, 30, 20, 0), False, "Fri Oct 30 16:00 EDT close"),
    (_z(11, 1, 15, 0), False, "Sun Nov 1, the day the clocks go back"),
    (_z(11, 2, 13, 30), False, "Mon Nov 2 08:30 EST"),
    (_z(11, 2, 14, 29), False, "Mon Nov 2 09:29 EST"),
    (_z(11, 2, 14, 30), True, "Mon Nov 2 09:30 EST open"),
    (_z(11, 2, 20, 30), True, "Mon Nov 2 15:30 EST"),
    (_z(11, 2, 21, 0), False, "Mon Nov 2 16:00 EST close"),
    # Gap weeks: Europe is on standard time while the US is already on (Mar
    # 16) or still on (Oct 26) daylight time, so New York is only 5h behind
    # Berlin and the session opens at 13:30Z, not 14:30Z.
    (_z(3, 16, 13, 29), False, "Mon Mar 16 09:29 EDT, EU still on CET"),
    (_z(3, 16, 13, 30), True, "Mon Mar 16 09:30 EDT, EU still on CET"),
    (_z(10, 26, 19, 59), True, "Mon Oct 26 15:59 EDT, EU back on CET"),
    (_z(10, 26, 20, 0), False, "Mon Oct 26 16:00 EDT, EU back on CET"),
]


@pytest.mark.parametrize(
    "instant, expected, ny",
    _WINTER_AND_TRANSITIONS,
    ids=[f"{i.isoformat()[:16]}Z-{n}" for i, _, n in _WINTER_AND_TRANSITIONS],
)
def test_session_follows_new_york_dst_not_a_fixed_offset(instant, expected, ny):
    assert is_equity_market_open(instant) is expected, ny


def test_the_session_is_an_hour_later_in_utc_in_winter_than_in_summer():
    """The same New York wall clock is a different UTC instant either side of
    the clocks changing, and both instants must be open. This is the whole
    behaviour in two lines, for a reader who skips the table."""
    assert is_equity_market_open(_z(7, 20, 13, 30)) is True  # 09:30 EDT
    assert is_equity_market_open(_z(1, 12, 13, 30)) is False  # 08:30 EST
    assert is_equity_market_open(_z(1, 12, 14, 30)) is True  # 09:30 EST
