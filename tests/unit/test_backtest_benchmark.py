"""Exposure, the no-skill null, and the re-entry cooldown (model plan item 2).

The plan's finding — vault `Docs/model-improvement-plan.md` — is that a null
with *no forecasting skill in it at all* ("hold buy-and-hold's return scaled by
time in market, minus one round trip per position") reproduces the published
headline to 0.0 points. That null was computed in a scratchpad. These tests
pin it as a committed path, because a harness that cannot tell a good model
from a long one cannot score anything measured on the new 6.6 years of bars.

Pure functions only here: no LightGBM, no Postgres, no clock.
"""

import numpy as np
import pandas as pd
import pytest

from model.backtest import (
    BacktestConfig,
    apply_cooldown,
    merge_overlapping_holds,
    run_backtest,
)
from model.benchmark import (
    exposure_matched_return,
    held_coverage,
    time_in_market,
)


# --- time in market -----------------------------------------------------


def test_a_position_occupies_its_signal_bars_and_its_horizon():
    """Entered at bar 2, signal off after 4, exits horizon=3 bars later at 7:
    bars 2..7 inclusive are six held bars out of twenty."""
    assert time_in_market([(2, 4)], horizon_bars=3, n_bars=20) == pytest.approx(0.3)


def test_holding_nothing_is_zero_exposure():
    assert time_in_market([], horizon_bars=15, n_bars=100) == 0.0


def test_a_bar_held_by_two_positions_is_counted_once():
    """Exposure is the fraction of wall-clock the strategy was in the market,
    not a sum of holding periods — otherwise it can exceed 100%."""
    assert time_in_market(
        [(0, 4), (2, 6)], horizon_bars=0, n_bars=10
    ) == pytest.approx(0.7)


def test_a_position_running_past_the_last_bar_cannot_exceed_full_exposure():
    assert time_in_market([(5, 9)], horizon_bars=50, n_bars=10) == pytest.approx(0.5)


def test_being_in_the_market_throughout_is_full_exposure():
    assert time_in_market([(0, 9)], horizon_bars=0, n_bars=10) == 1.0


# --- the no-skill null --------------------------------------------------


def test_the_null_reproduces_the_published_btc_headline():
    """The plan's central measurement, as one assertion. BTCUSDT h=15:
    82.0% exposure of a +18.4% benchmark, 519 round trips at 22 bps, and a
    strategy that returned -63.3%. A null with no skill in it lands on the
    same number — which is why the harness needs to report it."""
    null = exposure_matched_return(
        exposure=0.820, benchmark_return=0.184, positions=519, cost=0.0022
    )
    assert null == pytest.approx(-0.633, abs=0.001)


def test_a_strategy_that_never_traded_has_a_null_of_zero():
    assert exposure_matched_return(0.0, 0.5, positions=0, cost=0.0022) == 0.0


def test_full_exposure_with_no_turnover_is_exactly_buy_and_hold():
    assert exposure_matched_return(1.0, 0.184, positions=0, cost=0.0022) == (
        pytest.approx(0.184)
    )


def test_each_extra_round_trip_costs_the_null_more():
    cheap = exposure_matched_return(0.8, 0.2, positions=10, cost=0.0022)
    churny = exposure_matched_return(0.8, 0.2, positions=100, cost=0.0022)
    assert churny < cheap


def test_the_toll_compounds_rather_than_adding_up():
    """519 round trips at 22 bps is -68.1%, not -114%: fees are charged on
    the capital that survived the previous one."""
    drag = exposure_matched_return(0.0, 0.0, positions=519, cost=0.0022)
    assert drag == pytest.approx(-0.681, abs=0.001)
    assert drag > -1.0


# --- re-entry cooldown --------------------------------------------------


def test_no_cooldown_leaves_the_positions_exactly_as_merged():
    held = [(0, 5), (25, 30), (60, 65)]
    assert apply_cooldown(held, horizon_bars=15, cooldown_bars=0) == held


def test_a_re_entry_inside_the_cooldown_is_not_taken():
    """(0,5) exits at 5+15=20. With a 32-bar cooldown the next entry is not
    allowed before 52, so the signal at 25 is declined."""
    held = [(0, 5), (25, 30)]
    assert apply_cooldown(held, horizon_bars=15, cooldown_bars=32) == [(0, 5)]


def test_the_cooldown_runs_from_the_exit_not_the_last_signal_bar():
    """A position is still held for `horizon_bars` after the signal drops.
    Counting the cooldown from bar 5 rather than bar 20 would re-enter while
    capital is still committed."""
    held = [(0, 5), (18, 20)]
    assert apply_cooldown(held, horizon_bars=15, cooldown_bars=1) == [(0, 5)]


def test_a_declined_entry_does_not_start_a_cooldown_of_its_own():
    """The trade never happened, so it cannot gate the next one — the clock
    still runs from the last position actually taken."""
    held = [(0, 5), (25, 30), (60, 65)]
    kept = apply_cooldown(held, horizon_bars=15, cooldown_bars=32)
    assert kept == [(0, 5), (60, 65)]


def test_a_cooldown_can_only_reduce_exposure():
    held, pending = merge_overlapping_holds(
        [(0, 2), (10, 12), (20, 22), (30, 32), (40, 42)], horizon_bars=5
    )
    held = held + [pending]
    hot = time_in_market(held, horizon_bars=5, n_bars=60)
    cooled = time_in_market(
        apply_cooldown(held, horizon_bars=5, cooldown_bars=12),
        horizon_bars=5,
        n_bars=60,
    )
    assert cooled < hot


def test_the_cooldown_is_a_config_knob_that_defaults_to_off():
    """Off by default: every published figure was measured without it, and a
    default that changed them would silently reinterpret the artifacts."""
    assert BacktestConfig().cooldown_bars == 0


# --- the equity path the cooldown produces ------------------------------


def test_positions_the_equity_path_declined_are_not_charged_a_fee():
    """The cooldown must drop the position, not merely mark it: a declined
    entry that still paid a round trip would import the KI-040 overcharge by
    another route."""
    held = [(0, 5), (25, 30)]
    kept = apply_cooldown(held, horizon_bars=15, cooldown_bars=32)
    assert len(kept) == 1


def test_coverage_marks_every_bar_a_position_was_held_through():
    """The per-bar array the fold-level exposure is sliced out of — one
    definition, so a fold's exposure and the run's cannot disagree."""
    covered = held_coverage([(1, 2)], horizon_bars=2, n_bars=8)
    assert covered.tolist() == [False, True, True, True, True, False, False, False]


# --- what the harness reports -------------------------------------------


def _bars(n=1200, seed=3, drift=0.0):
    rng = np.random.default_rng(seed)
    index = pd.date_range("2026-05-01", periods=n, freq="1min", tz="UTC")
    close = 100.0 * np.exp(np.cumsum(rng.normal(drift, 0.001, n)))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.001,
            "Low": close * 0.999,
            "Close": close,
            "Volume": rng.integers(100, 10_000, n).astype(float),
        },
        index=index,
    )


_FAST = BacktestConfig(train_rows=400, test_rows=100)


def test_the_summary_reports_exposure_and_the_null_it_is_matched_to():
    results = run_backtest(_bars(), _FAST)

    assert 0.0 <= results["time_in_market"] <= 1.0
    assert results["excess_vs_null"] == pytest.approx(
        results["strategy_total_return"] - results["null_total_return"]
    )


def test_every_fold_carries_its_own_exposure_and_null():
    """Per fold, not just per run: over years of bars a global null sits a
    couple of points from buy-and-hold and cannot discriminate anything,
    while a falling fold gives going flat something to be worth."""
    results = run_backtest(_bars(), _FAST)

    for fold in results["folds"]:
        assert 0.0 <= fold["exposure"] <= 1.0
        if fold["positions"] == 0:
            assert fold["null_return"] == pytest.approx(
                fold["exposure"] * fold["buy_hold_return"]
            )


def test_a_fold_that_held_nothing_is_scored_against_nothing():
    results = run_backtest(_bars(), _FAST)
    idle = [f for f in results["folds"] if f["exposure"] == 0.0]
    for fold in idle:
        assert fold["null_return"] == 0.0
        assert fold["strategy_return"] == 0.0


def test_a_cooldown_lowers_both_the_positions_taken_and_the_exposure():
    """The knob item 2 asks for: exposure becomes something the strategy
    decides, not a by-product of the horizon and the merge rule."""
    hot = run_backtest(_bars(), _FAST)
    cooled = run_backtest(_bars(), BacktestConfig(
        train_rows=400, test_rows=100, cooldown_bars=400))

    assert cooled["total_positions"] < hot["total_positions"]
    assert cooled["time_in_market"] < hot["time_in_market"]
    assert cooled["fee_charges"] == cooled["total_positions"]


def test_the_published_equity_path_is_unchanged_by_the_benchmark_work():
    """A regression pin, not a discovery: these are the numbers the current
    harness produces on this fixture. Exposure and the null are additive
    reporting — if adding them moved the equity path, this fails."""
    results = run_backtest(_bars(n=2600), BacktestConfig(
        train_rows=400, test_rows=100))

    assert results["n_folds"] == 21
    assert results["total_positions"] == 16
    assert results["hit_rate_per_position"] == pytest.approx(0.5)
    assert results["strategy_total_return"] == pytest.approx(-0.011022098334825703)
    assert results["buy_hold_total_return"] == pytest.approx(0.041448730766337816)
    assert results["avg_position_return"] == pytest.approx(-0.0006589124138992458)
