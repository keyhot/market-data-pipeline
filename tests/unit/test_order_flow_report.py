"""KI-094/KI-097: `top_quintile_gross_bps` is E12's pass leg and E8/E9's
stage-2 trigger (`order_flow_report.py:100`) and had no test; `report()`'s
VOID/PASS/FAIL branches had none either and persisted nothing. Fixtures are
hand-computable so a dropped `np.sign`, a `>=`->`>`, a `<=`->`<` on the
control gate, or an `and`->`or` in the verdict rule all fail loudly."""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import order_flow_report as ofr  # noqa: E402
import report_results  # noqa: E402

# ------------------------------------------------------------ top_quintile_gross_bps

def test_top_quintile_gross_bps_signed_and_dropped_sign_would_flip_it():
    """One confident SHORT (flow=0.1, conf=0.4) in the top fifth with a
    POSITIVE forward return: the signed proxy must be negative. Dropping
    `np.sign` would report the raw (positive) return instead."""
    oos = pd.DataFrame({"flow": [0.5, 0.5, 0.5, 0.5, 0.1],
                        "ret": [0.0, 0.0, 0.0, 0.0, 0.001]})
    assert ofr.top_quintile_gross_bps(oos, "flow") == pytest.approx(-10.0)


def test_top_quintile_gross_bps_ge_boundary_not_strict_gt():
    """conf == quantile(0.8) exactly for the top two (tied) rows: `>=` must
    include both. A `>=`->`>` mutant selects an empty top set (mean of
    nothing) and this stops matching."""
    oos = pd.DataFrame({"flow": [0.5, 0.5, 0.5, 0.9, 0.1],
                        "ret": [0.0, 0.0, 0.0, 0.002, 0.003]})
    # conf = [0, 0, 0, 0.4, 0.4] -> quantile(0.8) == 0.4 exactly
    assert ofr.top_quintile_gross_bps(oos, "flow") == pytest.approx(-5.0)


# --------------------------------------------------------------------- report

def _make_oos(year_is_positive: list[bool], control_delta: float,
              rows_per_year: int = 4) -> pd.DataFrame:
    years = (2022, 2023, 2024, 2025)
    chunks = []
    for year, positive in zip(years, year_is_positive):
        idx = pd.date_range(f"{year}-06-01", periods=rows_per_year, freq="D",
                            tz="UTC")
        flow, base = (1.0, 0.0) if positive else (0.0, 1.0)
        chunks.append(pd.DataFrame({
            "year": year, "y": [0, 1] * (rows_per_year // 2),
            "ret": 0.0005, "base": base, "flow": flow,
            "control": base + control_delta,
        }, index=idx))
    return pd.concat(chunks)


@pytest.fixture(autouse=True)
def _isolated_results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(report_results, "RESULTS_DIR", tmp_path)


def _patch(monkeypatch, low, high):
    monkeypatch.setattr(ofr, "auc", lambda y, p: float(np.mean(p)))
    monkeypatch.setattr(ofr, "delta_auc_ci", lambda oos, arm="flow": (low, low, high))


def test_report_void_when_control_at_or_below_threshold(monkeypatch):
    """control == CONTROL_MIN_DELTA exactly must VOID: the gate is
    `control <= CONTROL_MIN_DELTA`, and a `<=`->`<` mutant would let this
    slip through to PASS/FAIL instead."""
    _patch(monkeypatch, low=0.01, high=0.02)
    oos = _make_oos([True, True, True, True], control_delta=ofr.CONTROL_MIN_DELTA)
    assert ofr.report("BTCUSDT", oos, corr=0.1) == "VOID"


def test_report_passes_when_control_ok_ci_above_zero_three_years(monkeypatch):
    _patch(monkeypatch, low=0.01, high=0.02)
    oos = _make_oos([True, True, True, False], control_delta=0.5)
    verdict = ofr.report("BTCUSDT", oos, corr=0.1)
    assert verdict == "PASS"


def test_report_fails_when_ci_not_above_zero_despite_positive_years(monkeypatch):
    _patch(monkeypatch, low=0.0, high=0.02)
    oos = _make_oos([True, True, True, True], control_delta=0.5)
    assert ofr.report("BTCUSDT", oos, corr=0.1) == "FAIL"


def test_report_fails_when_too_few_positive_years_despite_ci(monkeypatch):
    _patch(monkeypatch, low=0.01, high=0.02)
    oos = _make_oos([True, True, False, False], control_delta=0.5)
    assert ofr.report("BTCUSDT", oos, corr=0.1) == "FAIL"


def test_report_persists_result_json(monkeypatch, tmp_path):
    _patch(monkeypatch, low=0.01, high=0.02)
    oos = _make_oos([True, True, True, True], control_delta=0.5)
    ofr.report("ETHUSDT", oos, corr=0.2)
    data = json.loads((tmp_path / "order_flow_ETHUSDT.json").read_text())
    assert data["verdict"] == "PASS"
    assert data["inputs"]["symbol"] == "ETHUSDT"
    assert data["ci"] == {"low": 0.01, "high": 0.02}
