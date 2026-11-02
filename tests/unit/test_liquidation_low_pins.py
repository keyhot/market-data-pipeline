"""Low-severity pins from audit 5 (liquidation boundaries): F3-F7 and F9.

Each section below names its finding, the mutant it kills, and why the
existing tests could not. These are new tests in a new file on purpose: the
two existing liquidation test files are rewrapped on `ci-green-lint`, so
editing them in place would conflict.

No database, no sockets: the recorder gets a fake clock and a list-collecting
writer, and everything else is pure.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from ingestion.liquidation_recorder import LiquidationRecorder
from ingestion.liquidations import parse_okx, parse_okx_data
from tests.unit.test_liquidations import LINEAR, OKX_FRAME

UTC = timezone.utc

# --- F3: the OKX event_ts is never asserted -------------------------------------
#
# `parse_okx_data` reads `ts = _ms(detail["ts"])` (ingestion/liquidations.py:195).
# Binance and Bybit assert their `event_ts`; OKX's tests compared REST to WS
# (both through the same line) or looked at the other fields, so a naive
# `datetime.fromtimestamp(int(ts) / 1000)`, a +-60 000 ms shift, or the wrong
# field all stayed green. The expected values below are literals, not `_ms`.

OKX_FRAME_TS = '"ts":"1790650761954"'


def test_okx_ws_event_ts_is_the_detail_ts_as_aware_utc():
    assert OKX_FRAME_TS in OKX_FRAME  # the replacement below can find it
    _, (liq,) = parse_okx(OKX_FRAME, LINEAR)
    assert liq.event_ts == datetime(2026, 9, 29, 2, 59, 21, 954000, tzinfo=UTC)
    # An aware datetime equals the same instant in any zone, so pin the zone:
    # a naive stamp would be read by Postgres in the session TimeZone.
    assert liq.event_ts.utcoffset() == timedelta(0)


def test_okx_rest_event_ts_is_the_detail_ts_as_aware_utc():
    data = json.loads(OKX_FRAME)["data"]
    (liq,) = parse_okx_data(data, LINEAR, "rest")
    assert liq.source == "rest"
    assert liq.event_ts == datetime(2026, 9, 29, 2, 59, 21, 954000, tzinfo=UTC)
    assert liq.event_ts.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "ts_ms,expected",
    [
        # Milliseconds survive (not truncated to the second, not read as seconds).
        ("1790650799999", datetime(2026, 9, 29, 2, 59, 59, 999000, tzinfo=UTC)),
        ("1790650800000", datetime(2026, 9, 29, 3, 0, 0, tzinfo=UTC)),
        ("1790650800001", datetime(2026, 9, 29, 3, 0, 0, 1000, tzinfo=UTC)),
    ],
)
def test_okx_event_ts_keeps_milliseconds_across_a_minute_boundary(ts_ms, expected):
    frame = OKX_FRAME.replace(OKX_FRAME_TS, f'"ts":"{ts_ms}"')
    assert frame != OKX_FRAME
    _, (ws,) = parse_okx(frame, LINEAR)
    (rest,) = parse_okx_data(json.loads(frame)["data"], LINEAR, "rest")
    assert ws.event_ts == expected
    assert rest.event_ts == expected
    assert ws.event_ts.utcoffset() == timedelta(0)
    assert rest.event_ts.utcoffset() == timedelta(0)


def test_okx_event_ts_survives_the_recorder_buffer_unchanged():
    # R:148 only checks the venue and the coverage count of the same frame.
    recorder = LiquidationRecorder(lambda *a, **k: None, clock=lambda: 1790650762.25)
    assert recorder.on_okx_frame(OKX_FRAME) == "data"
    (liq,) = recorder.buffer.liquidations
    assert liq.event_ts == datetime(2026, 9, 29, 2, 59, 21, 954000, tzinfo=UTC)
    assert liq.event_ts.utcoffset() == timedelta(0)
