"""KI-094/KI-097: `report()`'s VOID/PASS/FAIL branches
(`archive_report.py:216-219`) had no test and persisted nothing. `valid`
(control ok AND row-share ok) gates VOID, `passed` (CI above 0 AND gross
above the floor) gates PASS/FAIL -- each clause is pinned in isolation so
an `and`->`or` swap on either one fails loudly. The control-only path
(arm absent from `oos`) is covered too."""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import archive_report as ar  # noqa: E402
import report_results  # noqa: E402


def _make_oos(gross_positive: bool, control_value: float, row_share: float) -> tuple[pd.DataFrame, dict]:
    if gross_positive:
        # top quintile (conf tie at 0.4) = last two rows, both long, both +
        arm_col = [0.5] * 6 + [0.9, 0.9]
        ret_col = [0.0] * 6 + [0.001, 0.001]  # -> gross = +10.0 bps
    else:
        arm_col = [0.5] * 6 + [0.9, 0.1]
        ret_col = [0.0] * 6 + [0.002, 0.003]  # -> gross = -5.0 bps
    idx = pd.date_range("2024-01-01", periods=8, freq="D", tz="UTC")
    # base == 0.0 so `mean(control) - mean(base)` is an exact subtraction of
    # 0.0 (no float rounding), letting the control-threshold test land on
    # CONTROL_MIN_DELTA bit-exactly.
    oos = pd.DataFrame({
        "year": 2024, "y": [0, 1] * 4, "ret": ret_col,
        "base": 0.0, "control": control_value, "metrics": arm_col,
    }, index=idx)
    info = {"row_share": row_share}
    return oos, info


@pytest.fixture(autouse=True)
def _isolated_results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(report_results, "RESULTS_DIR", tmp_path)


def _patch(monkeypatch, low, high):
    monkeypatch.setattr(ar, "auc", lambda y, p: float(np.mean(p)))
    monkeypatch.setattr(ar, "delta_auc_ci", lambda oos, arm="metrics": (low, low, high))


def test_report_void_when_control_at_or_below_threshold(monkeypatch):
    _patch(monkeypatch, low=0.01, high=0.02)
    oos, info = _make_oos(gross_positive=True, control_value=ar.CONTROL_MIN_DELTA,
                          row_share=1.0)
    out = ar.report("BTCUSDT", "metrics", oos, info)
    assert out["verdict"] == "VOID"


def test_report_void_when_row_share_below_threshold_despite_control_ok(monkeypatch):
    """control is well above threshold, but row_share alone fails: `valid`
    is `control ok AND row_share ok`. An `and`->`or` mutant would let
    control-ok alone produce a non-VOID verdict."""
    _patch(monkeypatch, low=0.01, high=0.02)
    oos, info = _make_oos(gross_positive=True, control_value=1.0, row_share=0.5)
    out = ar.report("BTCUSDT", "metrics", oos, info)
    assert out["verdict"] == "VOID"


def test_report_passes_when_valid_ci_above_zero_and_gross_above_floor(monkeypatch):
    _patch(monkeypatch, low=0.01, high=0.02)
    oos, info = _make_oos(gross_positive=True, control_value=1.0, row_share=1.0)
    out = ar.report("BTCUSDT", "metrics", oos, info)
    assert out["verdict"] == "PASS"
    assert out["gross"] == pytest.approx(10.0)


def test_report_fails_when_ci_not_above_zero_despite_gross_ok(monkeypatch):
    _patch(monkeypatch, low=0.0, high=0.02)
    oos, info = _make_oos(gross_positive=True, control_value=1.0, row_share=1.0)
    out = ar.report("BTCUSDT", "metrics", oos, info)
    assert out["verdict"] == "FAIL"


def test_report_fails_when_gross_below_floor_despite_ci_above_zero(monkeypatch):
    _patch(monkeypatch, low=0.01, high=0.02)
    oos, info = _make_oos(gross_positive=False, control_value=1.0, row_share=1.0)
    out = ar.report("BTCUSDT", "metrics", oos, info)
    assert out["gross"] == pytest.approx(-5.0)
    assert out["verdict"] == "FAIL"


def test_report_control_only_path_persists_control_only_verdict(monkeypatch, tmp_path):
    """`arm` absent from `oos` (the `--control-only` CLI path) returns
    early with no PASS/FAIL/VOID key -- but must still persist a JSON."""
    _patch(monkeypatch, low=0.01, high=0.02)
    idx = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    oos = pd.DataFrame({"year": 2024, "y": [0, 1, 0, 1], "ret": 0.0,
                        "base": 0.5, "control": 1.5}, index=idx)
    info = {"row_share": 1.0}
    out = ar.report("BTCUSDT", "metrics", oos, info)
    assert "verdict" not in out
    data = json.loads((tmp_path / "archive_BTCUSDT_metrics.json").read_text())
    assert data["verdict"] == "CONTROL_ONLY"


def test_report_persists_result_json(monkeypatch, tmp_path):
    _patch(monkeypatch, low=0.01, high=0.02)
    oos, info = _make_oos(gross_positive=True, control_value=1.0, row_share=1.0)
    ar.report("ETHUSDT", "metrics", oos, info)
    data = json.loads((tmp_path / "archive_ETHUSDT_metrics.json").read_text())
    assert data["verdict"] == "PASS"
    assert data["statistic"]["gross_bps"] == pytest.approx(10.0)
