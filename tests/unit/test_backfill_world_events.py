"""Backfill tests exercise pure helpers against a fake provider — no network,
no database. The one behavioural claim worth pinning is that the live append
path still tolerates a re-fire once the unique index exists."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from ingestion.binance_provider import BinanceProvider
from scripts.backfill_world_events import (
    apply_cooldown,
    iter_windows,
    stamp_backfilled,
)

BASE = datetime(2026, 5, 20, tzinfo=timezone.utc)
START_MS = int(BASE.timestamp() * 1000)


class FakePagedBinance:
    """Serves 1m klines from a fixed span, honouring startTime and the
    1000-per-request cap — the real endpoint's shape, no network."""

    def __init__(self, total_bars):
        self.bars = [START_MS + i * 60_000 for i in range(total_bars)]
        self.requests = []

    def get_klines(self, ticker_symbol, interval, limit=1000, start_ms=None):
        self.requests.append(start_ms)
        available = [t for t in self.bars if start_ms is None or t >= start_ms]
        return [[t, "100", "101", "99", "100.5", "10"] for t in available[:limit]]


def test_pagination_walks_past_the_1000_candle_cap():
    provider = BinanceProvider()
    fake = FakePagedBinance(2500)
    provider.get_klines = fake.get_klines

    collected = provider.get_klines_paginated(
        "BTCUSDT", "1m", START_MS, START_MS + 2500 * 60_000, sleep_seconds=0
    )
    assert len(collected) == 2500
    assert len(fake.requests) == 3          # 1000 + 1000 + 500
    open_times = [k[0] for k in collected]
    assert open_times == sorted(set(open_times)), "pages overlapped or repeated"


def test_pagination_stops_at_end_ms():
    provider = BinanceProvider()
    provider.get_klines = FakePagedBinance(2500).get_klines
    end_ms = START_MS + 1500 * 60_000

    collected = provider.get_klines_paginated(
        "BTCUSDT", "1m", START_MS, end_ms, sleep_seconds=0
    )
    assert collected and all(k[0] < end_ms for k in collected)


def test_pagination_refuses_to_spin_when_the_api_stops_advancing():
    """A malformed response repeating the same candle must terminate rather
    than hammer Binance forever — 87 requests per symbol is already a lot."""

    class StuckBinance:
        def __init__(self):
            self.calls = 0

        def get_klines(self, ticker_symbol, interval, limit=1000, start_ms=None):
            self.calls += 1
            return [[START_MS, "1", "2", "0.5", "1.5", "10"]] * limit

    provider = BinanceProvider()
    stuck = StuckBinance()
    provider.get_klines = stuck.get_klines

    provider.get_klines_paginated(
        "BTCUSDT", "1m", START_MS, START_MS + 10**9, sleep_seconds=0
    )
    assert stuck.calls < 5, "no-forward-progress guard did not fire"


def _frame(rows):
    index = pd.to_datetime([BASE + timedelta(minutes=i) for i in range(rows)], utc=True)
    return pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.0 + (i % 5) for i in range(rows)],
            "volume": [10.0] * rows,
        },
        index=index,
    )


def test_iter_windows_covers_the_frame_in_lookback_steps():
    frame = _frame(300)
    windows = list(iter_windows(frame, vol_window=60, lookback_bars=10))
    assert windows, "no windows produced"
    # Every window is long enough for detect_events to evaluate anything.
    assert all(len(w) >= 70 for w in windows)
    # The last window ends at the last bar, so recent history isn't dropped.
    assert windows[-1].index[-1] == frame.index[-1]


def test_iter_windows_yields_nothing_for_a_short_frame():
    assert list(iter_windows(_frame(30), vol_window=60, lookback_bars=10)) == []


def test_apply_cooldown_suppresses_same_type_within_the_window():
    events = [
        {"event_type": "big_move", "symbol": "BTCUSDT", "occurred_at": BASE},
        {"event_type": "big_move", "symbol": "BTCUSDT",
         "occurred_at": BASE + timedelta(minutes=5)},
        {"event_type": "big_move", "symbol": "BTCUSDT",
         "occurred_at": BASE + timedelta(minutes=45)},
    ]
    kept = apply_cooldown(events, cooldown_minutes=30)
    assert len(kept) == 2


def test_apply_cooldown_is_per_event_type():
    events = [
        {"event_type": "big_move", "symbol": "BTCUSDT", "occurred_at": BASE},
        {"event_type": "streak", "symbol": "BTCUSDT", "occurred_at": BASE},
    ]
    assert len(apply_cooldown(events, cooldown_minutes=30)) == 2


def test_apply_cooldown_is_per_symbol():
    events = [
        {"event_type": "big_move", "symbol": "BTCUSDT", "occurred_at": BASE},
        {"event_type": "big_move", "symbol": "ETHUSDT", "occurred_at": BASE},
    ]
    assert len(apply_cooldown(events, cooldown_minutes=30)) == 2


def test_stamp_backfilled_flags_every_event():
    events = [{"event_type": "big_move", "payload": {"sigmas": 4.2}}]
    stamped = stamp_backfilled(events)
    assert stamped[0]["payload"]["backfilled"] is True
    assert stamped[0]["payload"]["sigmas"] == 4.2
    assert "backfilled" not in events[0]["payload"], "input must not be mutated"


def test_stamp_backfilled_handles_a_missing_payload():
    assert stamp_backfilled([{"event_type": "streak"}])[0]["payload"] == {
        "backfilled": True
    }


def test_dry_run_reports_candidate_count_without_writing(monkeypatch, capsys):
    """--dry-run must preview the real candidate count and never write. The
    count an operator sanity-checks before a live 60-day backfill has to be true."""
    import scripts.backfill_world_events as bf

    monkeypatch.setattr(
        bf, "backfill_symbol",
        lambda symbol, days, provider, config=None: [
            {"event_type": "big_move", "symbol": symbol,
             "occurred_at": BASE, "severity": 5.0, "payload": {"backfilled": True}},
            {"event_type": "streak", "symbol": symbol,
             "occurred_at": BASE, "severity": 9.0, "payload": {"backfilled": True}},
        ],
    )
    calls = []
    monkeypatch.setattr(
        bf, "append_world_events_backfill",
        lambda events: calls.append(events) or len(events),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["backfill_world_events.py", "--symbols", "BTCUSDT", "--dry-run"],
    )

    rc = bf.main()

    assert rc == 0
    assert calls == [], "dry-run must not write to the store"
    out = capsys.readouterr().out
    assert "would write 2 world events" in out
