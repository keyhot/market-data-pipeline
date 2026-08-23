"""Position-level trade accounting (KI-001 + KI-040).

These are the pure half of the harness: no LightGBM, no Postgres, no clock —
the same shape as `world.state.project_state` and `director.scenes.tick`, so
the accounting rules can be pinned in milliseconds instead of behind a 5-minute
walk-forward.

Two defects are pinned here:
  KI-040 — a fee per in-market *bar* instead of per position;
  KI-001 — compounding positions whose holding periods overlap.
"""

import numpy as np
import pytest

from model.backtest import (
    BacktestConfig,
    find_episodes,
    merge_overlapping_holds,
    position_returns,
)


# --- KI-040: one position, one round trip -------------------------------


def test_consecutive_in_market_bars_are_one_position():
    mask = np.array([False, True, True, True, False, True, False])
    assert find_episodes(mask) == [(1, 3), (5, 5)]


def test_an_all_flat_mask_opens_no_positions():
    assert find_episodes(np.zeros(5, dtype=bool)) == []


def test_a_mask_that_is_in_market_throughout_is_a_single_position():
    assert find_episodes(np.ones(4, dtype=bool)) == [(0, 3)]


def test_a_position_pays_one_round_trip_however_long_it_is_held():
    """The KI-040 defect in one assertion: a 4-bar run and a 1-bar run at the
    same entry and exit price must pay the same fee, because both are one
    entry and one exit."""
    config = BacktestConfig(fee_per_side=0.001, slippage=0.0, horizon_bars=0)
    closes = np.full(10, 100.0)

    held_long = position_returns([(1, 4)], closes, config)
    held_once = position_returns([(1, 1)], closes, config)

    assert held_long[0] == pytest.approx(held_once[0])
    assert held_long[0] == pytest.approx(-0.002)  # 2 x 10 bps, charged once


def test_position_return_runs_from_entry_bar_to_exit_bar_plus_horizon():
    """Hand-computable: enter at 100, the run ends at index 3, the horizon is
    2 more bars, so the exit price is closes[5] = 110 — +10% gross."""
    config = BacktestConfig(fee_per_side=0.0, slippage=0.0, horizon_bars=2)
    closes = np.array([90.0, 100.0, 101.0, 102.0, 105.0, 110.0, 120.0])

    net = position_returns([(1, 3)], closes, config)

    assert net[0] == pytest.approx(0.10)


def test_a_position_whose_exit_falls_off_the_end_is_dropped_not_guessed():
    config = BacktestConfig(fee_per_side=0.0, slippage=0.0, horizon_bars=3)
    closes = np.array([100.0, 101.0, 102.0])

    assert len(position_returns([(1, 2)], closes, config)) == 0


# --- KI-001: one unit of capital, no overlap ----------------------------


def test_an_entry_while_a_position_is_still_open_continues_that_position():
    """The KI-001 defect in one assertion. The first run ends at bar 2 and
    holds 5 more bars to realize, so it is still open at bar 4 — the trader
    never exited, so this is one position 0->6, not two compounding ones."""
    closed, pending = merge_overlapping_holds([(0, 2), (4, 6), (20, 21)], 5)

    assert closed == [(0, 6)]
    assert pending == (20, 21)


def test_an_entry_after_the_horizon_closes_is_a_separate_position():
    closed, pending = merge_overlapping_holds([(0, 0), (5, 5)], horizon_bars=4)
    assert closed == [(0, 0)]
    assert pending == (5, 5)


def test_an_entry_one_bar_before_the_horizon_closes_is_the_same_position():
    closed, pending = merge_overlapping_holds([(0, 0), (4, 4)], horizon_bars=4)
    assert closed == []
    assert pending == (0, 4)


def test_the_open_position_carries_across_the_fold_boundary():
    """Fold test windows are adjacent, so a position open at the end of one
    fold is still open in the next. Merging per fold from a clean slate would
    re-admit exactly the overlap KI-001 is about."""
    _, pending = merge_overlapping_holds([(90, 95)], horizon_bars=10)

    closed, still_open = merge_overlapping_holds(
        [(100, 101), (120, 120)], horizon_bars=10, pending=pending
    )

    assert closed == [(90, 101)]
    assert still_open == (120, 120)


def test_nothing_is_merged_when_no_holding_periods_touch():
    episodes = [(0, 1), (10, 11), (20, 21)]
    closed, pending = merge_overlapping_holds(episodes, horizon_bars=2)
    assert closed == episodes[:-1]
    assert pending == episodes[-1]


def test_merging_never_drops_an_entry():
    """Dropping the later episode instead of merging would silently discard a
    real entry — and a signal spanning a whole fold would discard the fold."""
    closed, pending = merge_overlapping_holds([(0, 1), (2, 3), (4, 5)], 10)
    assert closed == []
    assert pending == (0, 5)
