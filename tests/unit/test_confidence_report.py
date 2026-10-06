import math

import numpy as np
import pandas as pd
import pytest

from model.backtest import BacktestConfig
from scripts.confidence_report import (
    LN2,
    auc,
    bucket_table,
    find_episodes,
    log_loss_report,
    log_loss_score,
    position_table,
    round_trip_cost,
    selection_value,
    threshold_table,
    within_fold_lift,
)

_CONFIG = BacktestConfig(horizon_bars=2)


def _oos(p, y=None, fwd=None, fold=0, start="2026-01-01"):
    p = list(p)
    return pd.DataFrame({
        "ts": pd.date_range(start, periods=len(p), freq="1min", tz="UTC"),
        "fold": fold,
        "p": p,
        "y": y if y is not None else [1] * len(p),
        "fwd": fwd if fwd is not None else [0.0] * len(p),
    })


def test_round_trip_cost_is_both_sides_of_fee_and_slippage():
    assert round_trip_cost(BacktestConfig()) == 0.0022


def test_find_episodes_groups_consecutive_bars_into_one_position():
    mask = np.array([False, True, True, True, False, True, False])
    assert find_episodes(mask) == [(1, 3), (5, 5)]


def test_find_episodes_handles_runs_touching_both_ends():
    assert find_episodes(np.array([True, True, False, True])) == [(0, 1), (3, 3)]
    assert find_episodes(np.array([False, False])) == []


def test_episodes_expose_the_per_bar_fee_overcharge():
    """KI-040: the backtest charges a round trip per in-market bar, so a
    single 4-bar position is billed four times."""
    mask = np.array([True, True, True, True])
    assert mask.sum() == 4
    assert len(find_episodes(mask)) == 1


def test_auc_is_one_for_perfect_ranking_and_half_for_no_ranking():
    assert auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert auc(np.array([1, 1, 0, 0]), np.array([0.1, 0.2, 0.8, 0.9])) == 0.0
    assert auc(np.array([0, 1, 0, 1]), np.array([0.5, 0.5, 0.5, 0.5])) == 0.5


def test_auc_is_nan_when_one_class_is_missing():
    assert np.isnan(auc(np.array([1, 1]), np.array([0.2, 0.9])))


def test_bucket_table_reports_realized_frequency_per_bucket():
    oos = _oos(
        p=[0.1, 0.1, 0.95, 0.95],
        y=[0, 0, 1, 1],
        fwd=[-0.01, -0.01, 0.01, 0.01],
    )
    table = bucket_table(oos, _CONFIG).set_index("bucket")
    assert table.loc["0.00-0.30", "realized"] == 0.0
    assert table.loc["0.90-1.00", "realized"] == 1.0
    assert table.loc["0.90-1.00", "n"] == 2


def test_bucket_table_net_is_gross_minus_the_round_trip():
    oos = _oos(p=[0.95, 0.95], y=[1, 1], fwd=[0.01, 0.01])
    row = bucket_table(oos, _CONFIG).iloc[0]
    assert row["gross_bp"] == 100.0
    assert row["net_bp"] == pytest.approx(100.0 - round_trip_cost(_CONFIG) * 1e4)


def test_threshold_table_separates_billed_bars_from_real_positions():
    oos = _oos(p=[0.9, 0.9, 0.9, 0.1], fwd=[0.001] * 4)
    row = threshold_table(oos, _CONFIG).set_index("thr").loc[0.50]
    assert row["in_market_bars"] == 3
    assert row["positions"] == 1
    assert row["fee_overcharge"] == 3.0


def test_position_table_charges_one_round_trip_per_position():
    """A 3-bar entry run is one position: entered once, exited once, and held
    until the last entry's horizon runs out."""
    closes = pd.Series(
        [100.0, 100.0, 100.0, 100.0, 110.0, 110.0, 110.0, 110.0],
        index=pd.date_range("2026-01-01", periods=8, freq="1min", tz="UTC"),
    )
    oos = _oos(p=[0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    row = position_table(oos, closes, _CONFIG).set_index("thr").loc[0.50]

    assert row["positions"] == 1
    assert row["entry_bars"] == 3               # three bars above the threshold
    assert row["avg_hold_bars"] == 5            # + the last entry's 2-bar horizon
    assert row["gross_bp"] == pytest.approx(1000.0)   # 100 -> 110
    assert row["net_bp"] == pytest.approx(978.0)     # one round trip, not three


def test_position_table_skips_positions_whose_horizon_runs_past_the_data():
    closes = pd.Series(
        [100.0, 101.0, 102.0],
        index=pd.date_range("2026-01-01", periods=3, freq="1min", tz="UTC"),
    )
    oos = _oos(p=[0.1, 0.1, 0.9])
    assert position_table(oos, closes, _CONFIG).empty


def test_selection_value_is_the_edge_over_being_long_every_bar():
    oos = _oos(p=[0.9, 0.9, 0.1, 0.1], fwd=[0.002, 0.002, 0.0, 0.0])
    # gated bars average 20 bps, all bars average 10 bps
    assert selection_value(oos, 0.80) == 10.0


def test_selection_value_is_zero_when_confidence_selects_nothing_special():
    oos = _oos(p=[0.9, 0.9, 0.1, 0.1], fwd=[0.001] * 4)
    assert selection_value(oos, 0.80) == 0.0


def test_log_loss_score_matches_hand_computation():
    # y=1,p=0.8 -> -ln(0.8); y=0,p=0.2 -> -ln(1-0.2) = -ln(0.8); mean of both.
    ll = log_loss_score(np.array([1.0, 0.0]), np.array([0.8, 0.2]))
    assert ll == pytest.approx(-math.log(0.8))


def test_log_loss_score_clips_extreme_probabilities_instead_of_blowing_up():
    """KI: an unclipped log loss gives -inf/inf at p=0 or p=1 (log(0)). This is
    the mutation check — removing the clip must fail this test."""
    ll = log_loss_score(np.array([0.0, 1.0]), np.array([1.0, 0.0]))
    assert math.isfinite(ll)
    # clipped to ~1e-15, so the per-row term is ~-ln(1e-15); 1-(1-1e-15) loses
    # a little precision in float64, so allow a wider relative tolerance
    assert ll == pytest.approx(-math.log(1e-15), rel=1e-4)


def test_coin_flip_log_loss_is_ln2():
    assert LN2 == pytest.approx(0.6931471805599453)


def test_log_loss_report_perfectly_calibrated_constant_predictor_scores_zero_skill():
    y = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])  # mean 0.1
    p = np.full_like(y, 0.1)
    report = log_loss_report(y, p)
    assert report["log_loss_skill"] == pytest.approx(0.0, abs=1e-12)
    assert report["log_loss"] == pytest.approx(report["base_rate_log_loss"])
    assert report["base_rate_source"] == "eval"
    assert report["base_rate"] == pytest.approx(0.1)
    assert report["coin_flip_log_loss"] == pytest.approx(LN2)


def test_log_loss_report_uses_the_training_base_rate_when_given():
    y = np.array([1.0, 0.0, 1.0, 1.0])
    p = np.array([0.9, 0.1, 0.9, 0.9])
    report = log_loss_report(y, p, base_rate=0.3)
    assert report["base_rate"] == pytest.approx(0.3)
    assert report["base_rate_source"] == "train"


def test_log_loss_report_skill_is_positive_when_model_beats_the_base_rate():
    y = np.array([1.0, 0.0, 1.0, 0.0, 1.0, 0.0])  # base rate 0.5
    p = np.array([0.9, 0.1, 0.9, 0.1, 0.9, 0.1])  # confident and correct
    report = log_loss_report(y, p)
    assert report["log_loss_skill"] > 0
    assert report["log_loss"] < report["base_rate_log_loss"]


def test_within_fold_lift_finds_ranking_a_global_threshold_would_miss():
    """Both folds rank correctly, but on incomparable p-scales: fold 1's
    losers score higher than fold 0's winners."""
    fold_0 = _oos(
        p=[0.1] * 9 + [0.4],
        fwd=[0.0] * 9 + [0.01],
        fold=0,
    )
    fold_1 = _oos(
        p=[0.6] * 9 + [0.95],
        fwd=[0.0] * 9 + [0.01],
        fold=1,
        start="2026-01-02",
    )
    result = within_fold_lift(pd.concat([fold_0, fold_1], ignore_index=True))

    assert result["folds"] == 2
    assert result["lift_bp"] > 0
    assert np.isnan(result["sign_test_z"])   # no fold has both classes here
    # a global 0.5 cutoff takes all of fold 1 and none of fold 0 instead
    assert selection_value(pd.concat([fold_0, fold_1], ignore_index=True), 0.5) < \
        result["lift_bp"]
