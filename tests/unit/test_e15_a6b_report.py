"""E15 A6b runner tests: DB-free, tiny synthetic scale (KI-097 pattern, see
test_range_forecast_report.py). Never touches real price_bars -- the
report's stage-B-scale run happens only via the CLI, not in the test
suite."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import e15_a6b_report as a6b  # noqa: E402
import report_results  # noqa: E402

from model.range_forecast import ALPHA_PRIMARY, seasonal_dgp  # noqa: E402
from model.range_forecast_a6b import garch_t_dgp  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(report_results, "RESULTS_DIR", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------
# _wf_for / _summarize plumbing
# --------------------------------------------------------------------------

def test_wf_for_uses_the_given_dgp_and_scale(monkeypatch):
    calls = []
    real_seasonal = seasonal_dgp

    def spy(seed, years, seasonal=True, **kw):
        calls.append((seed, years))
        return real_seasonal(seed=seed, years=years, seasonal=seasonal)

    wf = a6b._wf_for(spy, seed=1, warmup_years=1.0, years_scored=0.3)
    assert calls == [(1, pytest.approx(1.3))]
    assert len(wf) > 0
    assert set(["sigma", "L80", "U80", "r"]).issubset(wf.columns)


def test_summarize_k1_matches_direct_frozen_computation():
    """At k=1 (no distortion), _summarize's UC/CC/DQ numbers must equal
    calling the frozen model.range_forecast test functions directly on the
    undistorted L80/U80 -- otherwise the 'control (c) must reproduce A6'
    check is meaningless."""
    from model.range_forecast import dq_design, dq_test, lr_cc, lr_uc, miss

    wf = a6b._wf_for(seasonal_dgp, seed=2, warmup_years=1.0, years_scored=0.3)
    row = a6b._summarize(wf, k=1.0)

    hits = miss(wf["r"], wf["L80"], wf["U80"])
    _, p_uc = lr_uc(hits, ALPHA_PRIMARY)
    _, p_cc = lr_cc(hits, ALPHA_PRIMARY)
    X, Hit = dq_design(wf.index, hits, ALPHA_PRIMARY, a6b.H)
    _, p_dq, _ = dq_test(X, Hit, ALPHA_PRIMARY)

    assert row["p_uc"] == pytest.approx(p_uc)
    assert row["p_cc"] == pytest.approx(p_cc)
    assert row["p_dq"] == pytest.approx(p_dq)
    assert row["uc_strict"] == (p_uc >= 0.05)
    assert row["cc"] == (p_cc >= 0.05)
    assert row["dq"] == (p_dq >= 0.05)
    assert row["all_strict"] == (row["uc_strict"] and row["cc"] and row["dq"])


def test_summarize_distorted_band_changes_coverage():
    wf = a6b._wf_for(seasonal_dgp, seed=3, warmup_years=1.0, years_scored=0.3)
    row_k1 = a6b._summarize(wf, k=1.0)
    row_narrow = a6b._summarize(wf, k=0.5)
    assert row_narrow["coverage"] < row_k1["coverage"]


# --------------------------------------------------------------------------
# run_a6b orchestration
# --------------------------------------------------------------------------

def test_run_a6b_uses_garch_dgp_for_the_misspecified_variant(monkeypatch):
    """Mutant target: the misspecified variant must be scored on
    garch_t_dgp, not seasonal_dgp."""
    dgp_calls = []
    real_garch = garch_t_dgp

    def spy_garch(seed, years, seasonal=True, **kw):
        dgp_calls.append(seed)
        return real_garch(seed=seed, years=years, seasonal=seasonal)

    monkeypatch.setattr(a6b, "garch_t_dgp", spy_garch)

    result = a6b.run_a6b(
        seeds=range(2), calib_seeds=range(500, 502),
        warmup_years=1.0, years_scored=0.3,
        calib_warmup_years=1.0, calib_years_scored=0.3,
    )
    assert dgp_calls == [0, 1]
    assert "misspecified_garch_t5" in result["variants"]


def test_run_a6b_control_variant_reproduces_a6_style_pass_rate(monkeypatch):
    """At tiny scale, control_80 (k=1) run through run_a6b's aggregation
    must give the same seed-by-seed strict UC/CC/DQ verdicts as scoring
    those same seeds directly with the frozen functions -- the concrete
    form of 'control (c) must reproduce A6's pass rate' at test scale."""
    from model.range_forecast import dq_design, dq_test, lr_cc, lr_uc, miss

    seeds = range(3)
    result = a6b.run_a6b(
        seeds=seeds, calib_seeds=range(600, 602),
        warmup_years=1.0, years_scored=0.3,
        calib_warmup_years=1.0, calib_years_scored=0.3,
    )
    control = result["variants"]["control_80"]

    expected_pass = 0
    for seed in seeds:
        wf = a6b._wf_for(seasonal_dgp, seed=seed, warmup_years=1.0, years_scored=0.3)
        hits = miss(wf["r"], wf["L80"], wf["U80"])
        _, p_uc = lr_uc(hits, ALPHA_PRIMARY)
        _, p_cc = lr_cc(hits, ALPHA_PRIMARY)
        X, Hit = dq_design(wf.index, hits, ALPHA_PRIMARY, a6b.H)
        _, p_dq, _ = dq_test(X, Hit, ALPHA_PRIMARY)
        if p_uc >= 0.05 and p_cc >= 0.05 and p_dq >= 0.05:
            expected_pass += 1

    assert control["n_seeds"] == len(list(seeds))
    assert control["all_strict_pass_rate"] * control["n_seeds"] == pytest.approx(
        expected_pass
    )


def test_run_a6b_calibration_achieves_targets_on_pooled_sample(monkeypatch):
    result = a6b.run_a6b(
        seeds=range(2), calib_seeds=range(700, 704),
        warmup_years=1.0, years_scored=0.3,
        calib_warmup_years=1.0, calib_years_scored=0.3,
    )
    calib = result["calibration"]
    assert calib["pooled_n"] > 0
    assert 0.0 <= calib["pooled_coverage_k1"] <= 1.0
    assert calib["target_795"]["achieved_coverage"] == pytest.approx(0.795, abs=0.05)
    assert calib["target_780"]["achieved_coverage"] == pytest.approx(0.780, abs=0.05)


def test_run_a6b_result_has_no_h4_variants_by_default():
    result = a6b.run_a6b(
        seeds=range(1), calib_seeds=range(800, 802),
        warmup_years=1.0, years_scored=0.3,
        calib_warmup_years=1.0, calib_years_scored=0.3,
    )
    assert result["h"] == 1


# --------------------------------------------------------------------------
# CLI / results-JSON writer wiring
# --------------------------------------------------------------------------

def test_main_writes_results_json(tmp_path, monkeypatch):
    monkeypatch.setattr(a6b, "run_a6b", lambda **kw: {
        "h": 1, "seeds": [0, 1], "calib_seeds": [900, 901],
        "calibration": {"pooled_n": 10, "pooled_coverage_k1": 0.8,
                         "target_795": {"k": 1.0, "achieved_coverage": 0.795},
                         "target_780": {"k": 1.0, "achieved_coverage": 0.780}},
        "variants": {"control_80": {"n_seeds": 2, "all_strict_pass_rate": 1.0}},
    })
    a6b.main(["--seeds", "2", "--calib-seeds", "2"])
    payload = json.loads((tmp_path / "e15_a6b.json").read_text())
    assert payload["experiment"] == "e15_a6b"
    assert payload["verdict"] == "REPORT_ONLY"
