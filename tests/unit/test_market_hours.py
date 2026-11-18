import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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


# --- Aware inputs in other zones, and naive inputs -------------------------
#
# Every input above is a UTC-aware datetime, so a mutant that threw the input's
# zone away on the way in (read its wall clock as UTC, or dropped tzinfo and
# fell back to the host zone) answered exactly the same and survived. The rows
# below are picked so that the wall clock as written and the instant it denotes
# give different answers, and the expectations are literals: only the input is
# built with ZoneInfo, never the answer.
#
# 2026: Warsaw is CEST (+2) from Mar 29 to Oct 25 and CET (+1) otherwise; New
# York is EDT (-4) from Mar 8 to Nov 1 and EST (-5) otherwise; Tokyo is +9 all
# year.

# (zone, local wall clock as written, expected, what that is in UTC / New York)
_AWARE_LOCAL = [
    # Mon 2026-07-20: EDT, Warsaw CEST, Tokyo JST.
    ("America/New_York", (2026, 7, 20, 9, 29), False, "09:29 EDT"),
    ("America/New_York", (2026, 7, 20, 9, 30), True, "09:30 EDT open; as UTC 05:30"),
    ("America/New_York", (2026, 7, 20, 16, 0), False, "16:00 EDT close; as UTC 12:00"),
    ("Europe/Warsaw", (2026, 7, 20, 15, 29), False, "13:29Z; as UTC it is 11:29 EDT"),
    ("Europe/Warsaw", (2026, 7, 20, 15, 30), True, "13:30Z = 09:30 EDT open"),
    ("Europe/Warsaw", (2026, 7, 20, 21, 59), True, "19:59Z = 15:59 EDT"),
    ("Europe/Warsaw", (2026, 7, 20, 22, 0), False, "20:00Z = 16:00 EDT close"),
    ("Asia/Tokyo", (2026, 7, 20, 22, 29), False, "13:29Z = 09:29 EDT"),
    ("Asia/Tokyo", (2026, 7, 20, 22, 30), True, "13:30Z = 09:30 EDT; as UTC 18:30"),
    ("Asia/Tokyo", (2026, 7, 21, 4, 59), True, "Tue in Tokyo; Mon 19:59Z = 15:59 EDT"),
    ("Asia/Tokyo", (2026, 7, 21, 5, 0), False, "Mon 20:00Z = 16:00 EDT close"),
    # Mon 2026-01-12: EST, Warsaw CET, Tokyo JST.
    ("America/New_York", (2026, 1, 12, 9, 29), False, "09:29 EST"),
    ("America/New_York", (2026, 1, 12, 9, 30), True, "09:30 EST open; as UTC 04:30"),
    ("America/New_York", (2026, 1, 12, 16, 0), False, "16:00 EST close; as UTC 11:00"),
    ("Europe/Warsaw", (2026, 1, 12, 15, 29), False, "14:29Z; as UTC it is 10:29 EST"),
    ("Europe/Warsaw", (2026, 1, 12, 15, 30), True, "14:30Z = 09:30 EST open"),
    ("Europe/Warsaw", (2026, 1, 12, 21, 59), True, "20:59Z = 15:59 EST"),
    ("Europe/Warsaw", (2026, 1, 12, 22, 0), False, "21:00Z = 16:00 EST close"),
    ("Asia/Tokyo", (2026, 1, 12, 23, 29), False, "14:29Z = 09:29 EST"),
    ("Asia/Tokyo", (2026, 1, 12, 23, 30), True, "14:30Z = 09:30 EST; as UTC 18:30"),
    ("Asia/Tokyo", (2026, 1, 13, 5, 59), True, "Mon 20:59Z = 15:59 EST"),
    ("Asia/Tokyo", (2026, 1, 13, 6, 0), False, "Mon 21:00Z = 16:00 EST close"),
    # Mon 2026-03-16: Europe is still on CET, New York already on EDT, so
    # Warsaw is 5h ahead and the session opens at 14:30 there, not 15:30.
    ("Europe/Warsaw", (2026, 3, 16, 14, 29), False, "13:29Z = 09:29 EDT"),
    ("Europe/Warsaw", (2026, 3, 16, 14, 30), True, "13:30Z = 09:30 EDT open"),
    # The weekday is New York's, not the input's. Sat in Tokyo is still Fri
    # there, and Mon in Tokyo is still Sun there.
    ("Asia/Tokyo", (2026, 7, 25, 2, 0), True, "Sat Tokyo = Fri 17:00Z = 13:00 EDT"),
    ("Asia/Tokyo", (2026, 7, 20, 3, 0), False, "Mon Tokyo = Sun 18:00Z, a Sunday"),
]


@pytest.mark.parametrize(
    "zone, wall, expected, what",
    _AWARE_LOCAL,
    ids=[f"{z}-{datetime(*w).isoformat()[:16]}" for z, w, _, _ in _AWARE_LOCAL],
)
def test_an_aware_datetime_in_any_zone_is_judged_by_the_instant_it_denotes(
    zone, wall, expected, what
):
    now = datetime(*wall, tzinfo=ZoneInfo(zone))
    assert is_equity_market_open(now) is expected, what


@pytest.mark.parametrize(
    "instant, expected, ny",
    _WINTER_AND_TRANSITIONS,
    ids=[f"{i.isoformat()[:16]}Z-{n}" for i, _, n in _WINTER_AND_TRANSITIONS],
)
def test_one_instant_gets_one_answer_whichever_zone_it_is_written_in(
    instant, expected, ny
):
    """The winter and transition table again, with each instant restated in
    Warsaw, New York and Tokyo wall clock. Same instant, same answer."""
    for zone in ("Europe/Warsaw", "America/New_York", "Asia/Tokyo"):
        restated = instant.astimezone(ZoneInfo(zone))
        assert is_equity_market_open(restated) is expected, f"{zone}: {ny}"


@pytest.fixture
def host_tz():
    """Set the process timezone as the C library sees it, for one test.

    A naive datetime is read through the OS (`.astimezone()` with no argument),
    so the only way to vary what it means is the `TZ` variable plus
    `time.tzset()`. The explicit tzset after the context closes is what puts
    the zone back (monkeypatch restores the variable but never re-runs tzset),
    and the final assert makes a leak fail here rather than in a later test.
    Same shape as the fixture in test_soak_report.py.
    """
    before = (os.environ.get("TZ"), time.tzname)
    with pytest.MonkeyPatch.context() as mp:

        def use(name):
            mp.setenv("TZ", name)
            time.tzset()
            # An OS without that zone's data falls back to UTC silently,
            # which would turn every case below back into the tautology.
            july = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
            assert july.astimezone().utcoffset() == (
                july.astimezone(ZoneInfo(name)).utcoffset()
            ), f"TZ={name} did not take effect"

        yield use
    time.tzset()
    assert (os.environ.get("TZ"), time.tzname) == before


# (host zone, naive wall clock, expected, what that is in UTC / New York)
_NAIVE_HOST_LOCAL = [
    ("Europe/Warsaw", (2026, 7, 20, 15, 29), False, "13:29Z, 09:29 EDT; as UTC 11:29"),
    ("Europe/Warsaw", (2026, 7, 20, 21, 59), True, "19:59Z, 15:59 EDT; as UTC 17:59"),
    ("Europe/Warsaw", (2026, 1, 12, 15, 29), False, "14:29Z, 09:29 EST; as UTC 10:29"),
    ("Europe/Warsaw", (2026, 1, 12, 21, 59), True, "20:59Z, 15:59 EST; as UTC 16:59"),
    ("Asia/Tokyo", (2026, 7, 20, 22, 29), False, "13:29Z, 09:29 EDT"),
    ("Asia/Tokyo", (2026, 7, 20, 22, 30), True, "13:30Z, 09:30 EDT; as UTC 18:30"),
    ("Asia/Tokyo", (2026, 7, 25, 2, 0), True, "Sat in Tokyo; Fri 13:00 EDT"),
    ("America/New_York", (2026, 7, 20, 9, 30), True, "09:30 EDT; as UTC 05:30"),
    ("America/New_York", (2026, 7, 20, 16, 0), False, "16:00 EDT; as UTC 12:00"),
]


@pytest.mark.parametrize(
    "zone, wall, expected, what",
    _NAIVE_HOST_LOCAL,
    ids=[f"{z}-{datetime(*w).isoformat()[:16]}" for z, w, _, _ in _NAIVE_HOST_LOCAL],
)
def test_a_naive_datetime_is_the_host_wall_clock_not_utc(
    host_tz, zone, wall, expected, what
):
    """Pins today's behaviour, not a documented contract: the docstring of
    `is_equity_market_open` says nothing about naive input and production calls
    it with no argument. A naive value goes through `.astimezone()`, which means
    the host's wall clock. Reading it as UTC would slide the session by the
    host's offset (and the weekday with it), and on a UTC runner that is
    invisible unless the host zone is set here. If naive input should be
    rejected instead, this is the test to change."""
    host_tz(zone)
    assert is_equity_market_open(datetime(*wall)) is expected, what
