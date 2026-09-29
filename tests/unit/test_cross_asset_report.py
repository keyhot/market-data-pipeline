"""KI-094/KI-097: `delta_auc_ci` is THE shared verdict statistic for
E7/E8/E9/E12 (`cross_asset_report.py:59`) and had no test that could fail;
`report()`'s PASS/FAIL branch had none either, and printed its verdict
without persisting it. Fixtures are hand-computable so a sign swap, an
unpaired bootstrap, a row-level block, or an `and`->`or` in the verdict
rule all fail loudly."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import cross_asset_report as car  # noqa: E402
import report_results  # noqa: E402


# ---------------------------------------------------------------- delta_auc_ci

def _perfect_separation_one_day(n: int = 20) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    y = np.array(([1, 0] * (n // 2))[:n])
    cross = y.astype(float)  # perfectly separates -> auc == 1.0
    base = np.full(n, 0.5)  # ties everywhere -> auc == 0.5
    return pd.DataFrame({"y": y, "base": base, "cross": cross}, index=idx)


def test_delta_auc_ci_point_and_collapsed_ci_on_one_day():
    oos = _perfect_separation_one_day()
    point, low, high = car.delta_auc_ci(oos)
    assert point == pytest.approx(0.5)  # auc(y, cross)=1.0 - auc(y, base)=0.5
    # Every row falls on the same calendar day, so a day-block bootstrap can
    # only ever resample that one block: every draw reproduces the exact
    # point estimate. A mutant that blocks by ROW instead of by day would
    # inject spurious spread and blow this CI open.
    assert low == pytest.approx(0.5)
    assert high == pytest.approx(0.5)


def test_delta_auc_ci_is_paired_not_independent():
    """`cross` and `base` are the identical, imperfect, non-constant column
    on 25 days with both classes every day. A paired bootstrap evaluates
    both arms on the SAME resampled rows every draw, so delta is exactly 0
    every time. An 'unpaired picks' bug -- resampling each arm
    independently -- would inject noise and the CI would not collapse."""
    rng = np.random.default_rng(3)
    days = pd.date_range("2024-01-01", periods=25, freq="D", tz="UTC")
    chunks = []
    for d in days:
        y = np.array([1, 0, 1, 0])
        p = rng.uniform(0.2, 0.8, size=4)
        idx = pd.DatetimeIndex([d + pd.Timedelta(hours=h) for h in range(4)])
        chunks.append(pd.DataFrame({"y": y, "base": p, "cross": p}, index=idx))
    oos = pd.concat(chunks)
    point, low, high = car.delta_auc_ci(oos)
    assert (point, low, high) == (0.0, 0.0, 0.0)


def test_delta_auc_ci_sign_swap_would_flip_point():
    """Sanity check on the other direction: base perfectly separates,
    cross does not -> point must be NEGATIVE. Catches an operand swap in
    `auc(y, cross) - auc(y, base)`."""
    oos = _perfect_separation_one_day()
    oos = oos.rename(columns={"base": "cross", "cross": "base"})
    point, _, _ = car.delta_auc_ci(oos)
    assert point == pytest.approx(-0.5)


# --------------------------------------------------------------------- report

def _make_oos(year_is_positive: list[bool], rows_per_year: int = 4) -> pd.DataFrame:
    """4 years (2022..2025), each with a constant base/cross pair whose sign
    of (mean(cross) - mean(base)) is controlled directly. Paired with a
    monkeypatched `auc = lambda y, p: mean(p)`, this pins each year's ΔAUC
    sign without depending on the real AUC formula."""
    years = (2022, 2023, 2024, 2025)
    chunks = []
    for year, positive in zip(years, year_is_positive):
        idx = pd.date_range(f"{year}-06-01", periods=rows_per_year, freq="D",
                            tz="UTC")
        cross, base = (1.0, 0.0) if positive else (0.0, 1.0)
        chunks.append(pd.DataFrame({
            "year": year,
            "y": [0, 1] * (rows_per_year // 2),
            "base": base, "cross": cross,
        }, index=idx))
    return pd.concat(chunks)


@pytest.fixture(autouse=True)
def _isolated_results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(report_results, "RESULTS_DIR", tmp_path)
    return tmp_path


def _patch_auc_and_ci(monkeypatch, low, high, point=None):
    monkeypatch.setattr(car, "auc", lambda y, p: float(np.mean(p)))
    monkeypatch.setattr(car, "delta_auc_ci",
                        lambda oos, arm="cross": (point if point is not None else low, low, high))


def test_report_passes_ci_above_zero_and_three_positive_years(monkeypatch, tmp_path):
    _patch_auc_and_ci(monkeypatch, low=0.01, high=0.02)
    oos = _make_oos([True, True, True, False])
    assert car.report("BTCUSDT", oos) == "PASS"
    assert (tmp_path / "cross_asset_BTCUSDT.json").exists()


def test_report_fails_when_ci_not_above_zero_despite_all_years_positive(monkeypatch):
    """low <= 0 with 4/4 positive years: an `and`->`or` mutant would PASS
    this (positive_years >= 3 alone satisfies an OR). Real rule needs both."""
    _patch_auc_and_ci(monkeypatch, low=0.0, high=0.02)
    oos = _make_oos([True, True, True, True])
    assert car.report("BTCUSDT", oos) == "FAIL"


def test_report_fails_when_too_few_positive_years_despite_ci_above_zero(monkeypatch):
    """low > 0 with only 2/4 positive years: an `and`->`or` mutant would
    PASS this too (low > 0 alone satisfies an OR)."""
    _patch_auc_and_ci(monkeypatch, low=0.01, high=0.02)
    oos = _make_oos([True, True, False, False])
    assert car.report("BTCUSDT", oos) == "FAIL"


def test_report_persists_result_json_with_verdict(monkeypatch, tmp_path):
    _patch_auc_and_ci(monkeypatch, low=0.01, high=0.02)
    oos = _make_oos([True, True, True, True])
    car.report("ETHUSDT", oos)
    import json
    data = json.loads((tmp_path / "cross_asset_ETHUSDT.json").read_text())
    assert data["verdict"] == "PASS"
    assert data["ci"] == {"low": 0.01, "high": 0.02}
    assert data["inputs"]["symbol"] == "ETHUSDT"
