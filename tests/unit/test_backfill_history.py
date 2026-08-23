"""Long-span kline backfill — the prerequisite for every model experiment.

Everything the model has ever been measured on is one 36-day window that rose
monotonically, so "beats buy-and-hold" has never meant more than "was long
during an uptrend". These tests cover the pieces that decide whether the fetched
history is trustworthy: the span arithmetic, and the refusal to include a candle
that has not closed yet.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import backfill_history as bh  # noqa: E402


class TestIntervalToMs:
    def test_minutes(self):
        assert bh.interval_to_ms("15m") == 15 * 60 * 1000

    def test_hours(self):
        assert bh.interval_to_ms("1h") == 60 * 60 * 1000

    def test_days(self):
        assert bh.interval_to_ms("1d") == 24 * 60 * 60 * 1000

    def test_unknown_interval_is_refused(self):
        # Silently guessing a bar size would mislabel every row it wrote.
        with pytest.raises(ValueError, match="fortnight"):
            bh.interval_to_ms("1fortnight")


class TestClosedEndMs:
    def test_mid_candle_excludes_the_forming_one(self):
        ivl = bh.interval_to_ms("15m")
        now = 1_700_000_000_000
        end = bh.closed_end_ms(now, ivl)
        assert end <= now
        assert now - end < ivl
        assert end % ivl == 0

    def test_exactly_on_a_boundary_excludes_the_candle_just_opened(self):
        ivl = bh.interval_to_ms("1h")
        boundary = (1_700_000_000_000 // ivl) * ivl
        # The candle opening exactly now has zero bars of data in it.
        assert bh.closed_end_ms(boundary, ivl) == boundary


class TestIterSpans:
    def test_covers_the_whole_range_without_gaps_or_overlap(self):
        spans = list(bh.iter_spans(0, 250, 100))
        assert spans == [(0, 100), (100, 200), (200, 250)]

    def test_an_exact_multiple_produces_no_empty_tail(self):
        assert list(bh.iter_spans(0, 200, 100)) == [(0, 100), (100, 200)]

    def test_an_empty_range_produces_nothing(self):
        assert list(bh.iter_spans(100, 100, 50)) == []


def _kline(open_ms: int, close_price: float) -> list:
    return [open_ms, "1", "2", "0.5", str(close_price), "10",
            open_ms + 1, "0", 1, "0", "0", "0"]


class TestBackfillSymbol:
    def test_writes_every_span_under_the_requested_interval(self):
        written = []
        ivl = bh.interval_to_ms("1h")

        def fetch(symbol, interval, start_ms, end_ms):
            return [_kline(t, 100.0) for t in range(start_ms, end_ms, ivl)]

        def write(symbol, frame, interval):
            written.append((symbol, interval, len(frame)))

        total = bh.backfill_symbol(
            "BTCUSDT", "1h", 0, 3 * ivl, span_ms=ivl,
            fetch=fetch, write=write,
        )

        assert total == 3
        assert written == [("BTCUSDT", "1h", 1)] * 3

    def test_an_empty_span_writes_nothing_but_does_not_stop_the_run(self):
        # Binance returns nothing for a span before the pair was listed;
        # that must not truncate the rest of the backfill.
        written = []
        ivl = bh.interval_to_ms("1h")

        def fetch(symbol, interval, start_ms, end_ms):
            if start_ms == 0:
                return []
            return [_kline(start_ms, 100.0)]

        total = bh.backfill_symbol(
            "BTCUSDT", "1h", 0, 2 * ivl, span_ms=ivl,
            fetch=fetch,
            write=lambda s, f, i: written.append(len(f)),
        )

        assert total == 1
        assert written == [1]

    def test_rows_outside_the_span_are_not_written(self):
        # get_klines_paginated pages forward from start_ms and can overshoot;
        # a row past end_ms would smuggle in the forming candle.
        seen = []
        ivl = bh.interval_to_ms("1h")

        def fetch(symbol, interval, start_ms, end_ms):
            return [_kline(t, 100.0) for t in (start_ms, end_ms, end_ms + ivl)]

        bh.backfill_symbol(
            "BTCUSDT", "1h", 0, ivl, span_ms=ivl,
            fetch=fetch,
            write=lambda s, f, i: seen.append(f),
        )

        assert len(seen) == 1
        assert list(seen[0].index) == [
            pd.Timestamp(0, unit="ms", tz="UTC")
        ]
