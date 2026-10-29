"""KI-094 remainder: E10's verdict code (`horizon_report.py`) had no test that
could fail. There is no `report()` here -- the VOID/PASS/FAIL decision is the
tail of `main()`, so it is driven through `main()` with `_bars`, `run` and
`summarize` replaced by stubs (no model, no database, no archive). `_auc_ci`
is the week-block bootstrap behind the AUC interval.

Every clause of the verdict is pinned on its own with the others satisfied,
on both sides of its boundary, so an `or`/`and` swap, a `>=`/`>` flip or a
re-ordered branch changes a verdict here."""

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import horizon_report as hr  # noqa: E402

# ------------------------------------------------------------------ _auc_ci


def _weekly_oos(weeks=4):
    """`weeks` Monday-to-Sunday weeks of 7 daily rows; p is the row id so a
    stubbed AUC can see exactly which rows a bootstrap draw picked."""
    n = 7 * weeks
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")  # a Monday
    return pd.DataFrame({"y": np.arange(n) % 2, "p": np.arange(n, dtype=float)},
                        index=idx)


@pytest.fixture
def drawn(monkeypatch):
    """Run `_auc_ci` with auc stubbed to the mean of p; return the recorded
    calls (bootstrap draws first, the pooled point last) and the result."""
    monkeypatch.setattr(hr, "BOOTSTRAP", 40)
    calls = []

    def fake_auc(y, p):
        calls.append(np.asarray(p).copy())
        return float(np.mean(p))
    monkeypatch.setattr(hr, "auc", fake_auc)
    result = hr._auc_ci(_weekly_oos())
    return calls, result


def test_auc_ci_draws_the_configured_number_of_resamples(drawn):
    calls, _ = drawn
    assert len(calls) == 40 + 1  # BOOTSTRAP draws, then the pooled point


def test_auc_ci_point_is_the_pooled_auc_not_the_mean_of_draws(drawn):
    calls, (point, _, _) = drawn
    assert point == pytest.approx(13.5)  # mean(0..27), the un-resampled rows
    np.testing.assert_array_equal(calls[-1], np.arange(28.0))


def test_auc_ci_resamples_whole_calendar_weeks_with_replacement(drawn):
    calls, _ = drawn
    seen_weeks, any_repeat = set(), False
    for draw in calls[:-1]:
        assert len(draw) == 28  # as many weeks as there are, 7 rows each
        weeks = []
        for chunk in draw.reshape(4, 7):
            start = int(chunk[0])
            assert start % 7 == 0, "a block must start on a Monday"
            np.testing.assert_array_equal(chunk, np.arange(start, start + 7.0))
            weeks.append(start // 7)
        seen_weeks.update(weeks)
        any_repeat |= len(set(weeks)) < 4
    assert seen_weeks == {0, 1, 2, 3}, "every week, the last included, is drawable"
    assert any_repeat, "weeks are drawn with replacement"


def test_auc_ci_interval_is_the_2_5_and_97_5_percentiles_of_the_draws(drawn):
    calls, (_, low, high) = drawn
    draws = [float(np.mean(c)) for c in calls[:-1]]
    assert len(set(draws)) > 5, "the draws must actually vary"
    expect_low, expect_high = np.percentile(draws, [2.5, 97.5])
    assert low == pytest.approx(expect_low)
    assert high == pytest.approx(expect_high)
    assert low < high


def test_auc_ci_is_seeded_and_the_first_draw_is_the_seeded_block_sequence(drawn):
    calls, _ = drawn
    want = np.random.default_rng(hr.SEED).integers(0, 4, 4)
    got = [int(chunk[0]) // 7 for chunk in calls[0].reshape(4, 7)]
    assert got == want.tolist()


def test_auc_ci_real_auc_point_and_a_single_week_collapses_the_interval():
    # classic AUC example: positives at p=.35 and .8 beat 3 of the 4 pairs
    idx = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    oos = pd.DataFrame({"y": [0, 1, 0, 1], "p": [0.1, 0.35, 0.4, 0.8]}, index=idx)
    point, low, high = hr._auc_ci(oos)
    assert point == pytest.approx(0.75)
    assert low == pytest.approx(0.75) and high == pytest.approx(0.75)


def test_auc_ci_scores_y_against_p_not_the_reverse():
    # separable score: AUC is 1.0 only with y as the label and p as the score
    idx = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    sep = pd.DataFrame({"y": [0, 0, 1, 1], "p": [0.1, 0.2, 0.8, 0.9]}, index=idx)
    assert hr._auc_ci(sep)[0] == pytest.approx(1.0)
    anti = sep.assign(p=[0.9, 0.8, 0.2, 0.1])
    assert hr._auc_ci(anti)[0] == pytest.approx(0.0)


# ------------------------------------------------------------- main verdict

GOOD_CONTROL = {"auc": 0.80, "skill": 50.0, "lo": 40.0, "rows": 140_250,
                "positive_years": 4}
GOOD_PRIMARY = {"auc": 0.52, "skill": 30.0, "lo": 5.0, "rows": 140_250,
                "positive_years": 4}


def _verdict(monkeypatch, capsys, tmp_path, control=None, primary=None):
    control = {**GOOD_CONTROL, **(control or {})}
    primary = {**GOOD_PRIMARY, **(primary or {})}
    calls = []

    class _Oos:
        def to_csv(self, path):
            Path(path).write_text("stub")

    def fake_run(bars, h, leak=False):
        calls.append((h, leak))
        return {"oos": _Oos(), "leak": leak}

    monkeypatch.setattr(sys, "argv", ["horizon_report.py", "--symbol", "BTCUSDT",
                                      "--primary-only"])
    monkeypatch.setattr(hr, "_bars", lambda symbol: pd.DataFrame({"x": [1, 2]}))
    monkeypatch.setattr(hr, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(hr, "run", fake_run)
    monkeypatch.setattr(hr, "summarize",
                        lambda label, r: control if r["leak"] else primary)
    hr.main()
    out = capsys.readouterr().out
    word = re.search(r"h=16 verdict: (PASS|FAIL|VOID)", out).group(1)
    reasons = re.search(r"verdict: VOID \((.*?)\) \(skill", out)
    return word, (reasons.group(1) if reasons else None), out, calls


def test_preregistered_constants():
    assert hr.PRIMARY == 16 and hr.EXPLORATORY == (4, 32, 96)
    assert hr.COST_BPS == 22.0
    assert hr.CONTROL_MIN_AUC == 0.75 and hr.CONTROL_MIN_SKILL_BPS == 44.0
    assert hr.E7_ROWS == 140_250 and hr.MIN_ROW_SHARE == 0.99


def test_all_clauses_satisfied_is_pass(monkeypatch, capsys, tmp_path):
    word, reasons, out, calls = _verdict(monkeypatch, capsys, tmp_path)
    assert word == "PASS" and reasons is None
    assert "≥ 22: True; CI above 0: True; positive years 4/4" in out


def test_control_run_is_the_leaked_arm_and_goes_first(monkeypatch, capsys, tmp_path):
    _, _, _, calls = _verdict(monkeypatch, capsys, tmp_path)
    assert calls == [(16, True), (16, False)]
    assert (tmp_path / "horizon_oos_BTCUSDT_h16.csv").read_text() == "stub"


def test_the_control_gates_the_verdict_not_the_primary(monkeypatch, capsys, tmp_path):
    # a control that fails and a primary that is fine -> VOID; the reverse
    # (fine control, primary below every bar) is a FAIL, not a VOID
    word, _, _, _ = _verdict(monkeypatch, capsys, tmp_path, control={"auc": 0.5})
    assert word == "VOID"
    word, _, _, _ = _verdict(monkeypatch, capsys, tmp_path,
                             primary={"skill": -50.0, "lo": -60.0, "positive_years": 0})
    assert word == "FAIL"


@pytest.mark.parametrize("control, expect", [
    ({"auc": 0.7499}, "VOID"),       # AUC just under the bar, skill fine
    ({"auc": 0.75}, "PASS"),         # exactly on the bar is lit up (strict <)
    ({"skill": 43.99}, "VOID"),      # skill just under, AUC fine
    ({"skill": 44.0}, "PASS"),       # exactly on the bar (strict <)
    ({"auc": 0.7499, "skill": 43.99}, "VOID"),
])
def test_void_on_either_control_clause_alone(monkeypatch, capsys, tmp_path,
                                             control, expect):
    word, reasons, _, _ = _verdict(monkeypatch, capsys, tmp_path, control=control)
    assert word == expect
    if expect == "VOID":
        assert reasons == "control did not light up"


@pytest.mark.parametrize("rows, expect", [
    (138_847, "VOID"),   # 0.99 * 140,250 = 138,847.5
    (138_848, "PASS"),
    (140_250, "PASS"),
    (0, "VOID"),
])
def test_void_when_the_row_share_is_under_99_percent_of_e7(monkeypatch, capsys,
                                                           tmp_path, rows, expect):
    word, reasons, _, _ = _verdict(monkeypatch, capsys, tmp_path,
                                   primary={"rows": rows})
    assert word == expect
    if expect == "VOID":
        assert reasons == f"rows {rows:,} < 99% of 140,250"


def test_both_void_reasons_are_reported_together(monkeypatch, capsys, tmp_path):
    word, reasons, _, _ = _verdict(monkeypatch, capsys, tmp_path,
                                   control={"auc": 0.5}, primary={"rows": 100})
    assert word == "VOID"
    assert reasons == "control did not light up; rows 100 < 99% of 140,250"


def test_void_beats_a_passing_primary(monkeypatch, capsys, tmp_path):
    word, _, _, _ = _verdict(monkeypatch, capsys, tmp_path,
                             control={"skill": 0.0},
                             primary={"skill": 500.0, "lo": 400.0})
    assert word == "VOID"


@pytest.mark.parametrize("primary, expect", [
    ({"skill": 22.0}, "PASS"),                 # skill == cost: >= passes
    ({"skill": 21.99}, "FAIL"),                # only the skill clause fails
    ({"lo": 0.01}, "PASS"),
    ({"lo": 0.0}, "FAIL"),                     # CI touches 0: strict >
    ({"lo": -1.0}, "FAIL"),                    # only the CI clause fails
    ({"positive_years": 3}, "PASS"),           # >= 3 passes
    ({"positive_years": 2}, "FAIL"),           # only the years clause fails
    ({"skill": 21.99, "lo": 0.0, "positive_years": 2}, "FAIL"),
])
def test_pass_needs_every_one_of_skill_ci_and_years(monkeypatch, capsys, tmp_path,
                                                    primary, expect):
    word, reasons, _, _ = _verdict(monkeypatch, capsys, tmp_path, primary=primary)
    assert word == expect and reasons is None


def test_verdict_line_reports_each_clause_truth(monkeypatch, capsys, tmp_path):
    _, _, out, _ = _verdict(monkeypatch, capsys, tmp_path,
                            primary={"skill": 21.0, "lo": -1.0, "positive_years": 2})
    assert "≥ 22: False; CI above 0: False; positive years 2/4" in out
