"""KI-094 remainder: `cost_label_report.fold_verdict` is L1's verdict cell
(`folds_beat_null_z_gt_2`) and had no test that could fail. A fold "beats"
its null when strategy - null > TIE, "loses" when null - strategy > TIE,
and is a tie otherwise; z is the sign-test statistic over the non-tied
folds; the cell is `inconclusive` below MIN_NON_TIED non-tied folds, `pass`
when z > 2, else `fail`. Each boundary is pinned on both sides with literal
numbers so a `>`->`>=`, a swapped sign, a dropped `/ 2` or a re-ordered
branch changes a verdict here."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import cost_label_report as clr  # noqa: E402

TIE = 1e-12


def _folds(beat=0, lose=0, tie=0):
    """`beat` folds ahead of their null by 1 bp, `lose` behind by 1 bp, `tie`
    exactly level."""
    out = [{"strategy_return": 0.0001, "null_return": 0.0}] * beat
    out += [{"strategy_return": 0.0, "null_return": 0.0001}] * lose
    out += [{"strategy_return": 0.0, "null_return": 0.0}] * tie
    return out


def test_constants_are_the_preregistered_values():
    assert clr.TIE == TIE
    assert clr.MIN_NON_TIED == 300


# ------------------------------------------------------------------ counting

def test_beat_lose_tie_counts_and_z_with_all_three_kinds():
    out = clr.fold_verdict(_folds(beat=3, lose=1, tie=2))
    assert (out["beat"], out["lose"], out["tie"]) == (3, 1, 2)
    # m = 4, z = (3 - 2) / (sqrt(4) / 2) = 1.0: positive when beats dominate
    assert out["z"] == pytest.approx(1.0)
    assert out["cell"] == "inconclusive"  # 4 << MIN_NON_TIED


def test_z_is_negative_when_losses_dominate():
    out = clr.fold_verdict(_folds(beat=1, lose=3))
    assert (out["beat"], out["lose"]) == (1, 3)
    assert out["z"] == pytest.approx(-1.0)


def test_z_is_scaled_by_half_sqrt_m_not_sqrt_m():
    # m = 100, beat = 60: z = (60 - 50) / (10 / 2) = 2.0, not 1.0
    out = clr.fold_verdict(_folds(beat=60, lose=40))
    assert out["z"] == pytest.approx(2.0)


def test_tie_is_the_remainder_after_beat_and_lose():
    out = clr.fold_verdict(_folds(beat=5, lose=7, tie=11))
    assert out["tie"] == 11
    assert out["tie"] == 23 - (out["beat"] + out["lose"])


def test_difference_of_exactly_tie_is_a_tie_on_both_sides():
    # strict `> TIE`: strategy - null == 1e-12 exactly is level, not a beat
    ahead = [{"strategy_return": TIE, "null_return": 0.0}]
    behind = [{"strategy_return": 0.0, "null_return": TIE}]
    out = clr.fold_verdict(ahead + behind)
    assert (out["beat"], out["lose"], out["tie"]) == (0, 0, 2)


def test_difference_just_over_tie_counts():
    ahead = [{"strategy_return": 2 * TIE, "null_return": 0.0}]
    behind = [{"strategy_return": 0.0, "null_return": 2 * TIE}]
    out = clr.fold_verdict(ahead + behind)
    assert (out["beat"], out["lose"], out["tie"]) == (1, 1, 0)


def test_beat_is_strategy_minus_null_not_the_reverse():
    # one fold, strategy below null by a wide margin: that is a loss
    out = clr.fold_verdict([{"strategy_return": -0.5, "null_return": 0.5}])
    assert (out["beat"], out["lose"]) == (0, 1)


def test_no_non_tied_fold_gives_nan_z_and_inconclusive():
    out = clr.fold_verdict(_folds(tie=5))
    assert math.isnan(out["z"])
    assert out["cell"] == "inconclusive"
    assert out["tie"] == 5
    empty = clr.fold_verdict([])
    assert math.isnan(empty["z"]) and empty["cell"] == "inconclusive"


# ------------------------------------------------------------- the cell rule

def test_cell_is_inconclusive_one_below_min_non_tied_even_with_huge_z():
    out = clr.fold_verdict(_folds(beat=299))  # z = 17.3, but only 299 non-tied
    assert out["z"] > 2
    assert out["cell"] == "inconclusive"


def test_cell_is_decided_at_exactly_min_non_tied():
    # m = 300, beat = 168: z = 18 / (sqrt(300) / 2) = 2.078 > 2
    out = clr.fold_verdict(_folds(beat=168, lose=132))
    assert out["z"] == pytest.approx(18 / (math.sqrt(300) / 2))
    assert out["cell"] == "pass"


def test_ties_do_not_count_toward_min_non_tied():
    # 299 non-tied + 100 ties is still inconclusive (m, not len(folds))
    out = clr.fold_verdict(_folds(beat=299, tie=100))
    assert out["cell"] == "inconclusive"


def test_z_of_exactly_two_fails_pass_needs_strictly_more():
    # m = 400, beat = 220: z = 20 / (20 / 2) = 2.0 exactly
    out = clr.fold_verdict(_folds(beat=220, lose=180))
    assert out["z"] == 2.0
    assert out["cell"] == "fail"


def test_z_just_over_two_passes():
    out = clr.fold_verdict(_folds(beat=221, lose=179))
    assert out["z"] == pytest.approx(2.1)
    assert out["cell"] == "pass"


def test_a_strongly_negative_z_is_a_fail_not_a_pass():
    # |z| = 3 on the losing side must not pass (rules out abs(z) > 2)
    out = clr.fold_verdict(_folds(beat=170, lose=230))
    assert out["z"] == pytest.approx(-3.0)
    assert out["cell"] == "fail"


def test_inconclusive_wins_over_pass_when_both_would_apply():
    out = clr.fold_verdict(_folds(beat=100, lose=0))  # z = 10, m = 100 < 300
    assert out["z"] == pytest.approx(10.0)
    assert out["cell"] == "inconclusive"


# -------------------------------------------------------- the table row wrap

def test_row_carries_the_verdict_cell_and_scales_bp():
    r = {"avg_position_return": 0.0012, "position_return_stderr": 0.0003,
         "total_positions": 7, "time_in_market": 0.4,
         "strategy_total_return": 0.5, "null_total_return": 0.25,
         "null_constant_exposure": 0.125, "buy_hold_total_return": 0.0625,
         "folds": _folds(beat=221, lose=179)}
    row = clr._row("3class", 64, r)
    assert row["cell"] == "pass" and row["beat"] == 221 and row["lose"] == 179
    assert row["arm"] == "3class" and row["cooldown"] == 64
    assert row["avg_pos_bp"] == pytest.approx(12.0)
    assert row["se_bp"] == pytest.approx(3.0)
    assert (row["strategy"], row["null_fold"], row["null_const"], row["buy_hold"]) == (
        0.5, 0.25, 0.125, 0.0625)


def test_row_keeps_a_zero_average_and_nans_a_missing_one():
    base = {"total_positions": 0, "time_in_market": 0.0,
            "strategy_total_return": 0.0, "null_total_return": 0.0,
            "null_constant_exposure": 0.0, "buy_hold_total_return": 0.0,
            "folds": []}
    zero = clr._row("binary", 0, {**base, "avg_position_return": 0.0,
                                  "position_return_stderr": 0.0})
    assert zero["avg_pos_bp"] == 0.0 and zero["se_bp"] == 0.0
    none = clr._row("binary", 0, {**base, "avg_position_return": None,
                                  "position_return_stderr": None})
    assert math.isnan(none["avg_pos_bp"]) and math.isnan(none["se_bp"])
