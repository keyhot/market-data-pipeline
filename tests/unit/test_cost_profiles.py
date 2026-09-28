"""Named cost profiles and the maker fill rule (P1, vault
`Docs/cost-profiles-p1.md`).

Pure and DB-free. The fill rule is the part that can invent money: a backtest
that assumes every resting limit order fills books the fills adverse
selection would have refused it. So a touch is not a fill, and the bar that
decides is the one AFTER the signal, when the order exists.
"""

import numpy as np
import pandas as pd
import pytest

from model.backtest import (
    COST_PROFILES,
    BacktestConfig,
    apply_fill_rule,
    limit_buy_fills,
    run_backtest,
)


def test_the_default_config_is_the_spot_taker_profile():
    """Every published figure was measured at the default. It must not move."""
    assert BacktestConfig() == BacktestConfig.for_profile("spot_taker")
    assert BacktestConfig().fill_rule == "market"


@pytest.mark.parametrize("name, round_trip_bps", [
    ("spot_taker", 22), ("perp_taker", 12), ("perp_maker", 6),
])
def test_profiles_price_the_round_trip_the_plan_names(name, round_trip_bps):
    config = BacktestConfig.for_profile(name)
    assert 2 * (config.fee_per_side + config.slippage) * 1e4 == pytest.approx(
        round_trip_bps)


def test_every_profile_is_labelled_a_placeholder_until_the_venue_is_chosen():
    for profile in COST_PROFILES.values():
        assert "PLACEHOLDER" in profile.venue


def test_only_the_maker_profile_carries_the_fill_rule():
    assert BacktestConfig.for_profile("perp_maker").fill_rule == "limit_through"
    assert BacktestConfig.for_profile("perp_taker").fill_rule == "market"


def test_a_profile_keeps_the_other_fields_it_is_given():
    config = BacktestConfig.for_profile("perp_taker", horizon_bars=1,
                                        entry_threshold=0.5)
    assert (config.horizon_bars, config.entry_threshold) == (1, 0.5)


def test_an_unknown_profile_is_an_error_not_a_default():
    with pytest.raises(KeyError):
        BacktestConfig.for_profile("free_lunch")


# --- the fill rule ------------------------------------------------------


def test_a_buy_limit_fills_when_price_trades_through_it():
    assert limit_buy_fills(limit=100.0, next_low=99.9)


def test_a_touch_is_not_a_fill():
    """The mutant that fills on `<=` books every order resting at the low."""
    assert not limit_buy_fills(limit=100.0, next_low=100.0)


def test_a_limit_price_never_reached_does_not_fill():
    assert not limit_buy_fills(limit=100.0, next_low=100.5)


def test_unfilled_entries_are_skipped_not_assumed():
    closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    lows = np.array([99.0, 99.5, 101.5, 102.0, 104.0, 104.5])
    # (0, 0): the order rests at 100; bar 1's low 99.5 trades through it.
    # (2, 2): rests at 102; bar 3's low is exactly 102, a touch — skipped.
    # (3, 3): rests at 103; bar 4's low 104 never reaches it — skipped.
    held = [(0, 0), (2, 2), (3, 3)]
    filled, attempted = apply_fill_rule(held, closes, lows, "limit_through")
    assert filled == [(0, 0)]
    assert attempted == 3


def test_the_deciding_bar_is_the_one_after_the_signal():
    """The signal bar's own low happened before the order existed. Reading it
    fills an order that was never working at that price."""
    closes = np.array([100.0, 101.0, 102.0])
    lows = np.array([90.0, 100.5, 101.0])  # bar 0 dipped; bar 1 did not
    filled, _ = apply_fill_rule([(0, 0)], closes, lows, "limit_through")
    assert filled == []


def test_market_orders_always_fill():
    held = [(0, 0), (2, 2)]
    closes = np.arange(1.0, 6.0)
    filled, attempted = apply_fill_rule(held, closes, closes + 1, "market")
    assert filled == held and attempted == 2


def test_run_backtest_reports_the_fill_rate_and_trades_only_filled_positions():
    rng = np.random.default_rng(7)
    n = 1400
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    bars = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="15min",
                                   tz="UTC"),
        "open": close, "high": close * 1.001, "low": close * 0.999,
        "close": close,
        "volume": rng.integers(100, 10_000, n).astype(float),
    })

    def every_third_bar(X, y, train, test):
        # On one bar, off two: gaps wider than h=1, so positions stay apart.
        return np.where(np.arange(test.start, test.stop) % 3 == 0, 0.9, 0.1)

    base = dict(horizon_bars=1, train_rows=400, test_rows=200,
                entry_threshold=0.5)
    market = run_backtest(bars, BacktestConfig.for_profile("perp_taker", **base),
                          predict_fold=every_third_bar)
    maker = run_backtest(bars, BacktestConfig.for_profile("perp_maker", **base),
                         predict_fold=every_third_bar)
    assert market["fill_rate"] == 1.0
    # The next bar's low sits 0.1% under its close: a rise of more than that
    # leaves the order resting, anything less trades through it.
    assert maker["positions_attempted"] == market["total_positions"]
    assert 0 < maker["total_positions"] < maker["positions_attempted"]
    assert maker["fill_rate"] == pytest.approx(
        maker["total_positions"] / maker["positions_attempted"])
