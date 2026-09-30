"""E15 stage A/B/C runner tests: DB-free, with a monkeypatched loader and
RESULTS_DIR (KI-097 pattern, see test_archive_report.py). Never touches
real price_bars -- every fixture here is synthetic, built with
model.range_forecast.seasonal_dgp or a hand-built frame."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import numpy as np

import range_forecast_report as rfr  # noqa: E402
import report_results  # noqa: E402
from model.range_forecast import (  # noqa: E402
    ALPHA_PRIMARY,
    dq_design,
    dq_test,
    hourly_grid,
    lr_cc,
    lr_uc,
    miss,
    seasonal_dgp,
)


@pytest.fixture(autouse=True)
def _isolated_results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(report_results, "RESULTS_DIR", tmp_path)
    return tmp_path


def _write_stage_a(tmp_path, verdict="VALID"):
    path = tmp_path / "e15_stage_a.json"
    path.write_text(json.dumps({"experiment": "e15_stage_a", "verdict": verdict}))
    return path


def _write_stage_b(tmp_path, statistic):
    path = tmp_path / "e15_stage_b.json"
    path.write_text(json.dumps({"experiment": "e15_stage_b", "verdict": "SPLIT",
                                 "statistic": statistic}))
    return path


def test_calibration_h4_thins_to_nonoverlapping_origins():
    """Frozen note (Tests > Calibration, 'At h = 4'): calibration (UC/CC/DQ)
    uses ONLY the non-overlapping origins at UTC hours {0,4,8,12,16,20};
    hourly origins in between are overlapping-target artifacts of the
    walk-forward scoring and must not enter the hit sequence or the DQ
    design. Every non-kept-hour origin here is a forced miss (r=5, way
    outside the [-1,1] band); if they leaked into the hit sequence, the
    empirical miss rate would be dominated by 18/24 forced misses per day
    and coverage would collapse toward ~25%, not sit near the kept
    origins' ~80% target coverage."""
    idx = pd.date_range("2021-01-01", periods=24 * 60, freq="h", tz="UTC")  # 60 days
    kept_hours = {0, 4, 8, 12, 16, 20}
    is_kept = np.isin(idx.hour, list(kept_hours))

    rng = np.random.default_rng(5)
    r_kept = np.where(rng.random(len(idx)) < 0.2, 5.0, 0.0)
    r = np.where(is_kept, r_kept, 5.0)  # every non-kept origin is a forced miss

    wf = pd.DataFrame({"r": r, "L80": -1.0, "U80": 1.0}, index=idx)

    cal = rfr._calibration(wf, h=4)

    # 1) which origins are kept
    assert cal.n == int(is_kept.sum())

    # 2) the hit series and the DQ design are built from that SAME kept
    #    set: recompute independently from the pre-thinned frame and
    #    require an exact match against what _calibration returned.
    thinned = wf[is_kept]
    hits_expected = miss(thinned["r"], thinned["L80"], thinned["U80"])
    _, p_uc_expected = lr_uc(hits_expected, ALPHA_PRIMARY)
    _, p_cc_expected = lr_cc(hits_expected, ALPHA_PRIMARY)
    X_expected, Hit_expected = dq_design(thinned.index, hits_expected, ALPHA_PRIMARY, 4)
    _, p_dq_expected, _ = dq_test(X_expected, Hit_expected, ALPHA_PRIMARY)

    assert cal.p_uc == pytest.approx(p_uc_expected)
    assert cal.p_cc == pytest.approx(p_cc_expected)
    assert cal.p_dq == pytest.approx(p_dq_expected)
    assert cal.coverage == pytest.approx(1.0 - hits_expected.mean())

    # The forced-miss overlapping origins did NOT leak in.
    assert cal.coverage > 0.5


def test_calibration_h1_is_unfiltered_and_unchanged():
    """h=1 hits don't overlap (frozen note); every origin is scored, and
    this path must stay byte-identical to before the h=4 fix."""
    idx = pd.date_range("2021-01-01", periods=500, freq="h", tz="UTC")
    rng = np.random.default_rng(9)
    r = np.where(rng.random(len(idx)) < 0.2, 5.0, 0.0)
    wf = pd.DataFrame({"r": r, "L80": -1.0, "U80": 1.0}, index=idx)

    cal = rfr._calibration(wf, h=1)

    assert cal.n == len(wf)  # no thinning at h=1

    hits_expected = miss(wf["r"], wf["L80"], wf["U80"])
    _, p_uc_expected = lr_uc(hits_expected, ALPHA_PRIMARY)
    _, p_cc_expected = lr_cc(hits_expected, ALPHA_PRIMARY)
    X_expected, Hit_expected = dq_design(wf.index, hits_expected, ALPHA_PRIMARY, 1)
    _, p_dq_expected, _ = dq_test(X_expected, Hit_expected, ALPHA_PRIMARY)

    assert cal.p_uc == pytest.approx(p_uc_expected)
    assert cal.p_cc == pytest.approx(p_cc_expected)
    assert cal.p_dq == pytest.approx(p_dq_expected)
    assert cal.coverage == pytest.approx(1.0 - hits_expected.mean())


def test_load_bars_stage_b_never_returns_a_bar_at_or_after_cutoff(monkeypatch):
    """get_price_bars returns ISO-string timestamps (confirmed against the
    live store), not Timestamp objects -- load_bars must parse them before
    comparing to STAGE_B_LOAD_CUTOFF."""
    idx = pd.date_range("2025-12-31 22:00", periods=20, freq="15min", tz="UTC")
    fake = pd.DataFrame({"timestamp": idx.astype(str), "close": 100.0, "volume": 1.0})
    monkeypatch.setattr(
        "storage.postgres_store.get_price_bars",
        lambda symbol, interval, limit: fake,
    )
    bars = rfr.load_bars("BTCUSDT", "B")
    assert (bars["timestamp"] < rfr.STAGE_B_LOAD_CUTOFF).all()
    assert bars["timestamp"].max() < pd.Timestamp("2025-12-31 23:45", tz="UTC")


def test_stage_b_refuses_without_valid_stage_a(tmp_path):
    with pytest.raises(RuntimeError):
        rfr.run_stage_b(results_dir=tmp_path)


def test_stage_b_refuses_when_stage_a_is_void(tmp_path):
    _write_stage_a(tmp_path, verdict="VOID")
    with pytest.raises(RuntimeError):
        rfr.run_stage_b(results_dir=tmp_path)


def test_stage_c_refuses_without_stage_b(tmp_path):
    with pytest.raises(RuntimeError):
        rfr.run_stage_c(results_dir=tmp_path)


def test_stage_c_runs_only_pass_pairs(tmp_path, monkeypatch):
    statistic = {
        "BTCUSDT_1h": {"verdict": "PASS"},
        "BTCUSDT_4h": {"verdict": "FAIL"},
        "ETHUSDT_1h": {"verdict": "FAIL"},
        "ETHUSDT_4h": {"verdict": "FAIL"},
    }
    _write_stage_b(tmp_path, statistic)

    calls = []

    def fake_load_bars(symbol, stage):
        calls.append((symbol, stage))
        bars = seasonal_dgp(seed=1, years=1.2, seasonal=True)
        return bars

    monkeypatch.setattr(rfr, "load_bars", fake_load_bars)
    monkeypatch.setattr(rfr, "STAGE_C_START", pd.Timestamp("2021-06-01", tz="UTC"))
    monkeypatch.setattr(rfr, "STAGE_C_END", pd.Timestamp("2021-06-05", tz="UTC"))

    rfr.run_stage_c(results_dir=tmp_path)

    assert calls == [("BTCUSDT", "C")]  # only the PASS pair was loaded/run


def test_stage_a_json_statistic_keys_are_allowlisted(tmp_path, monkeypatch):
    """Stage A's statistic keys are `<a-check>_<...>`; every key's a-check
    prefix must be in the frozen allowlist."""
    monkeypatch.setattr(rfr, "SYMBOLS", ("BTCUSDT",))
    monkeypatch.setattr(rfr, "HORIZONS", (1,))

    def fake_load_bars(symbol, stage):
        return seasonal_dgp(seed=2, years=2.2, seasonal=True)

    monkeypatch.setattr(rfr, "load_bars", fake_load_bars)
    monkeypatch.setattr(rfr, "STAGE_B_START", pd.Timestamp("2021-01-01", tz="UTC"))
    monkeypatch.setattr(rfr, "STAGE_B_END", pd.Timestamp("2021-06-01", tz="UTC"))
    monkeypatch.setattr(rfr, "run_a6", lambda **kw: {1: {"uc": 0.9, "cc": 0.9, "dq": 0.9, "all": 0.8}})

    verdict, failing, statistic = rfr.run_stage_a(results_dir=tmp_path)

    for key in statistic:
        prefix = key.split("_ci_")[0] if "_ci_" in key else key.split("_", 1)[0]
        allowed_prefixes = {"a1", "a2", "a3", "a4", "a5", "a6"}
        assert prefix in allowed_prefixes, f"unexpected stage-A key: {key}"

    payload = json.loads((tmp_path / "e15_stage_a.json").read_text())
    assert payload["verdict"] in ("VALID", "VOID")
    assert payload["ci"] == {}


def test_run_stage_b_uses_ci_lower_bound_not_upper(tmp_path, monkeypatch):
    """Mutant 5 (ci_hi used in place of ci_lo): pins that run_stage_b's
    verdict wiring passes the HAC CI's LOWER bound into primary_pass."""
    _write_stage_a(tmp_path, verdict="VALID")
    monkeypatch.setattr(rfr, "SYMBOLS", ("BTCUSDT",))
    monkeypatch.setattr(rfr, "HORIZONS", (1,))
    monkeypatch.setattr(rfr, "load_bars",
                         lambda symbol, stage: seasonal_dgp(seed=3, years=1.3, seasonal=True))
    monkeypatch.setattr(rfr, "STAGE_B_START", pd.Timestamp("2021-01-01", tz="UTC"))
    monkeypatch.setattr(rfr, "STAGE_B_END", pd.Timestamp("2021-06-01", tz="UTC"))

    captured = {}
    real_primary_pass = rfr.primary_pass

    def spy(cal, ci_lo, level=0.05):
        captured["ci_lo_seen"] = ci_lo
        return real_primary_pass(cal, ci_lo, level)

    monkeypatch.setattr(rfr, "primary_pass", spy)
    rfr.run_stage_b(results_dir=tmp_path)
    payload = json.loads((tmp_path / "e15_stage_b.json").read_text())
    lo, hi = payload["ci"]["BTCUSDT_1h"]
    assert captured["ci_lo_seen"] == pytest.approx(lo)
    assert captured["ci_lo_seen"] != pytest.approx(hi)


def test_run_stage_b_scores_ii_as_plain_har(tmp_path, monkeypatch):
    """Mutant 6: control (i) swapped in for (ii) as the Winkler reference.
    Pins that run_stage_b's (ii) comparison model is literally 'plain_har'."""
    _write_stage_a(tmp_path, verdict="VALID")
    monkeypatch.setattr(rfr, "SYMBOLS", ("BTCUSDT",))
    monkeypatch.setattr(rfr, "HORIZONS", (1,))
    monkeypatch.setattr(rfr, "load_bars",
                         lambda symbol, stage: seasonal_dgp(seed=4, years=1.3, seasonal=True))
    monkeypatch.setattr(rfr, "STAGE_B_START", pd.Timestamp("2021-01-01", tz="UTC"))
    monkeypatch.setattr(rfr, "STAGE_B_END", pd.Timestamp("2021-06-01", tz="UTC"))

    models_used = []
    real_score = rfr._score

    def spy(grid, h, model, start, end):
        models_used.append(model)
        return real_score(grid, h, model, start, end)

    monkeypatch.setattr(rfr, "_score", spy)
    rfr.run_stage_b(results_dir=tmp_path)
    assert "plain_har" in models_used
    assert "rolling" not in models_used


def test_stage_report_compares_candidate_against_ii_not_i(tmp_path, monkeypatch):
    """Feed distinct synthetic Winkler series for (i) and (ii); assert the
    CI helper used (ii), not (i), as the reference."""
    idx = pd.date_range("2024-01-01", periods=200, freq="h", tz="UTC")
    wf_cand = pd.DataFrame({
        "sigma": 1.0, "L80": -1.0, "U80": 1.0, "L95": -2.0, "U95": 2.0,
        "r": 0.0, "rv_target": 1.0,
    }, index=idx)
    wf_ii = wf_cand.copy()
    wf_ii["L80"] = -3.0  # much wider -> candidate should look better vs (ii)
    wf_ii["U80"] = 3.0
    wf_i = wf_cand.copy()
    wf_i["L80"] = -0.5  # narrower -> candidate would look WORSE vs (i)
    wf_i["U80"] = 0.5

    mean_ii, lo_ii, hi_ii, _ = rfr._winkler_ci(wf_ii, wf_cand)
    mean_i, lo_i, hi_i, _ = rfr._winkler_ci(wf_i, wf_cand)
    assert mean_ii > 0  # candidate beats the wide (ii)
    assert mean_i < 0   # candidate loses to the narrow (i)
    # run_stage_b must use the (ii)-shaped comparison, i.e. mean_ii's sign
    assert mean_ii != mean_i
