"""KI-094/KI-097: `report()`'s VOID/PASS/FAIL branches
(`funding_report.py:173-176`) had no test and persisted nothing. VOID here
has TWO triggers (`control <= CONTROL_MIN_DELTA or not rows_ok`) and PASS
needs both `low > 0` and `positive_years >= 3` -- each is tested in
isolation so an `and`->`or` or `or`->`and` swap fails loudly."""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import funding_report as fr  # noqa: E402
import report_results  # noqa: E402

_MEDIANS = {-1: 1.0, 0: 1.0, 1: 1.0}


def _make_oos(year_is_positive: list[bool], control_delta: float,
              rows_per_year: int = 4) -> pd.DataFrame:
    years = (2022, 2023, 2024, 2025)
    chunks = []
    for year, positive in zip(years, year_is_positive):
        idx = pd.date_range(f"{year}-06-01", periods=rows_per_year, freq="D",
                            tz="UTC")
        arm, base = (1.0, 0.0) if positive else (0.0, 1.0)
        chunks.append(pd.DataFrame({
            "year": year, "y": [0, 1] * (rows_per_year // 2),
            "ret": 0.0005, "base": base, "funding": arm,
            "control": base + control_delta,
        }, index=idx))
    return pd.concat(chunks)


@pytest.fixture(autouse=True)
def _isolated_results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(report_results, "RESULTS_DIR", tmp_path)


def _patch(monkeypatch, low, high, e7_rows=16):
    monkeypatch.setattr(fr, "auc", lambda y, p: float(np.mean(p)))
    monkeypatch.setattr(fr, "delta_auc_ci", lambda oos, arm="funding": (low, low, high))
    monkeypatch.setattr(fr, "E7_ROWS", e7_rows)


def test_report_void_when_control_at_or_below_threshold(monkeypatch):
    _patch(monkeypatch, low=0.01, high=0.02)
    oos = _make_oos([True, True, True, True], control_delta=fr.CONTROL_MIN_DELTA)
    assert fr.report("BTCUSDT", oos, _MEDIANS, corr=0.1) == "VOID"


def test_report_void_when_rows_not_ok_despite_control_ok(monkeypatch):
    """control is well above threshold, but the row-share check fails on
    its own: VOID must trigger from EITHER clause of the `or`. An `or`->
    `and` mutant would let this slip through to PASS/FAIL."""
    _patch(monkeypatch, low=0.01, high=0.02, e7_rows=10_000)  # MIN_ROW_SHARE=0.99
    oos = _make_oos([True, True, True, True], control_delta=0.5)
    assert fr.report("BTCUSDT", oos, _MEDIANS, corr=0.1) == "VOID"


def test_report_passes_when_control_and_rows_ok_ci_above_zero_three_years(monkeypatch):
    _patch(monkeypatch, low=0.01, high=0.02, e7_rows=16)
    oos = _make_oos([True, True, True, False], control_delta=0.5)
    assert fr.report("BTCUSDT", oos, _MEDIANS, corr=0.1) == "PASS"


def test_report_fails_when_ci_not_above_zero_despite_positive_years(monkeypatch):
    _patch(monkeypatch, low=0.0, high=0.02, e7_rows=16)
    oos = _make_oos([True, True, True, True], control_delta=0.5)
    assert fr.report("BTCUSDT", oos, _MEDIANS, corr=0.1) == "FAIL"


def test_report_fails_when_too_few_positive_years_despite_ci(monkeypatch):
    _patch(monkeypatch, low=0.01, high=0.02, e7_rows=16)
    oos = _make_oos([True, True, False, False], control_delta=0.5)
    assert fr.report("BTCUSDT", oos, _MEDIANS, corr=0.1) == "FAIL"


def test_report_persists_result_json(monkeypatch, tmp_path):
    _patch(monkeypatch, low=0.01, high=0.02, e7_rows=16)
    oos = _make_oos([True, True, True, True], control_delta=0.5)
    fr.report("ETHUSDT", oos, _MEDIANS, corr=0.2)
    data = json.loads((tmp_path / "funding_ETHUSDT.json").read_text())
    assert data["verdict"] == "PASS"
    assert data["statistic"]["rows_ok"] is True
