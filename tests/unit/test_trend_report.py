"""Tests for `scripts/trend_report.py` -- the E16 runner. DB-free: builds
tiny real zip fixtures on disk (the archive format the loaders actually
read) rather than mocking `model.spot_archive`, so a loader that keeps 2026
data is caught the same way it would be caught on the real archive."""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import trend_report as tr  # noqa: E402


def _write_spot_zip(directory: Path, symbol: str, interval: str, month: str, start_ms: int, n_days: int):
    directory.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    ms_per_day = 86_400_000
    for i in range(n_days):
        open_time = start_ms + i * ms_per_day
        close = 100.0 + i
        w.writerow([open_time, close, close, close, close, 1.0,
                    open_time + ms_per_day - 1, 1.0, 1, 0.5, 0.5, 0])
    path = directory / f"{symbol}-{interval}-{month}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{symbol}-{interval}-{month}.csv", buf.getvalue())
    return path


def _write_funding_zip(directory: Path, symbol: str, month: str, start_ms: int, n_events: int):
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["calc_time,funding_interval_hours,last_funding_rate"]
    step_ms = 8 * 3_600_000
    for i in range(n_events):
        lines.append(f"{start_ms + i * step_ms},8,0.0001")
    path = directory / f"{symbol}-fundingRate-{month}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{symbol}-fundingRate-{month}.csv", "\n".join(lines))
    return path


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    dec_2025_ms = int(pd.Timestamp("2025-12-01", tz="UTC").timestamp() * 1000)
    jan_2026_ms = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
    _write_spot_zip(root / "binance_spot" / "klines" / "BTCUSDT" / "1d", "BTCUSDT", "1d",
                    "2025-12", dec_2025_ms, 31)
    _write_spot_zip(root / "binance_spot" / "klines" / "BTCUSDT" / "1d", "BTCUSDT", "1d",
                    "2026-01", jan_2026_ms, 10)
    _write_spot_zip(root / "binance_um" / "monthly" / "klines" / "BTCUSDT" / "1d", "BTCUSDT", "1d",
                    "2025-12", dec_2025_ms, 31)
    _write_spot_zip(root / "binance_um" / "monthly" / "klines" / "BTCUSDT" / "1d", "BTCUSDT", "1d",
                    "2026-01", jan_2026_ms, 10)
    _write_funding_zip(root / "binance_um" / "monthly" / "fundingRate" / "BTCUSDT", "BTCUSDT",
                       "2025-12", dec_2025_ms, 93)
    _write_funding_zip(root / "binance_um" / "monthly" / "fundingRate" / "BTCUSDT", "BTCUSDT",
                       "2026-01", jan_2026_ms, 30)
    return root


def test_load_spot_stage_a_excludes_2026(archive):
    bars = tr.load_spot(archive, "BTCUSDT", "A")
    # the row closing exactly 2026-01-01 (opened 2025-12-31) is legitimately
    # kept -- it is December's own last bar; only bars OPENING in 2026 (here,
    # everything from the 2026-01 fixture beyond the first row) are excluded.
    assert bars.index.max() == pd.Timestamp("2026-01-01", tz="UTC")
    assert (bars.index <= pd.Timestamp("2026-01-01", tz="UTC")).all()


def test_load_spot_stage_c_keeps_2026(archive):
    bars = tr.load_spot(archive, "BTCUSDT", "C")
    assert (bars.index >= pd.Timestamp("2026-01-01", tz="UTC")).any()


def test_load_spot_stage_b_excludes_2026(archive):
    bars = tr.load_spot(archive, "BTCUSDT", "B")
    assert (bars.index <= pd.Timestamp("2026-01-01", tz="UTC")).all()


def test_load_perp_stage_b_excludes_2026(archive):
    bars = tr.load_perp(archive, "BTCUSDT", "B")
    assert (bars.index <= pd.Timestamp("2026-01-01", tz="UTC")).all()


def test_load_funding_events_stage_a_excludes_2026(archive):
    events = tr.load_funding_events(archive, "BTCUSDT", "A")
    assert (events["time"] < pd.Timestamp("2026-01-01", tz="UTC")).all()
    assert len(events) == 93  # only the December file


def test_load_funding_events_stage_c_keeps_2026(archive):
    events = tr.load_funding_events(archive, "BTCUSDT", "C")
    assert len(events) == 93 + 30


def test_load_spot_close_time_shift(archive):
    """Close-time indexing: the row's index is one day AFTER its open."""
    bars = tr.load_spot(archive, "BTCUSDT", "A")
    dec_2025_open = pd.Timestamp("2025-12-01", tz="UTC")
    assert (dec_2025_open + pd.Timedelta(days=1)) in bars.index


def test_stage_b_refuses_without_valid_stage_a(tmp_path):
    (tmp_path / "e16_stage_a.json").write_text(json.dumps({"verdict": "VOID"}))
    with pytest.raises(RuntimeError, match="not VALID"):
        tr.run_stage_b(tmp_path, results_dir=tmp_path)


def test_stage_b_refuses_without_any_stage_a(tmp_path):
    with pytest.raises(RuntimeError, match="missing"):
        tr.run_stage_b(tmp_path, results_dir=tmp_path)


def test_stage_c_refuses_without_stage_b_pass(tmp_path):
    (tmp_path / "e16_stage_b.json").write_text(json.dumps({"verdict": "FAIL"}))
    with pytest.raises(RuntimeError, match="not PASS"):
        tr.run_stage_c(tmp_path, results_dir=tmp_path)


def test_stage_a_allowlist_constant_matches_module_keys():
    assert tr.STAGE_A_ALLOWLIST == {
        "a1_ci_lo_x", "a1_ci_lo_d",
        "a2_ci_hi_x", "a2_ci_hi_d",
        "a3_ok",
        "a4_mismatches",
        "a5_valid_months", "a5_missing_boundary_funding",
    }


def test_hold_index_is_64_months_sept2020_to_dec2025():
    assert len(tr.DECISION_TIMES) == 64
    assert len(tr.HOLD_INDEX) == 64
    assert tr.DECISION_TIMES[0] == pd.Timestamp("2020-09-01", tz="UTC")
    assert tr.DECISION_TIMES[-1] == pd.Timestamp("2025-12-01", tz="UTC")
    assert tr.HOLD_INDEX[0] == pd.Timestamp("2020-10-01", tz="UTC")
    assert tr.HOLD_INDEX[-1] == pd.Timestamp("2026-01-01", tz="UTC")


# --------------------------------------------------------------------------
# Stage B report-only helpers
# --------------------------------------------------------------------------

def test_max_drawdown_hand_computed():
    # curve: 1 -> 1.1 -> 0.99 -> 1.089 -> 0.9801 ; trough vs. the 1.1 peak
    # is 0.9801/1.1 - 1 = -0.109
    r = [0.10, -0.10, 0.10, -0.10]
    got = tr._max_drawdown(r)
    assert got == pytest.approx(-0.109, abs=1e-9)


def test_max_drawdown_all_nonnegative_is_zero():
    assert tr._max_drawdown([0.01, 0.0, 0.02]) == pytest.approx(0.0)


def test_expected_shortfall_90_hand_computed():
    # n=10 -> ceil(0.10*10) = 1 worst month
    r = [0.05, -0.20, 0.01, 0.02, -0.01, 0.03, 0.04, -0.02, 0.0, 0.06]
    assert tr._expected_shortfall_90(r) == pytest.approx(-0.20)


def test_expected_shortfall_90_averages_worst_k():
    # n=20 -> ceil(0.10*20) = 2 worst months averaged
    r = [-0.30, -0.10] + [0.01] * 18
    assert tr._expected_shortfall_90(r) == pytest.approx(-0.20)


# --------------------------------------------------------------------------
# Stage B end-to-end wiring (synthetic universe, monkeypatched loaders and
# a small DECISION_TIMES/HOLD_INDEX so the real bootstrap/HOP machinery runs
# on a fast, fully-controlled dataset rather than mocking the statistics
# themselves).
# --------------------------------------------------------------------------

def _synthetic_stage_b_universe(seed=1, warmup_months=48, hold_months=24,
                                perp_extra_drift=0.0, sigma_m=0.20, rho=0.5):
    """Two-symbol (BTCUSDT, ETHUSDT) synthetic universe built the same way
    as test_trend.py's `_synthetic_run`, with a trend-following-favorable
    DGP (mu_t = 0.3 * sigma_m * sign(trailing 12m return)) so the candidate
    has real timing skill and the wiring can be checked against a
    non-trivial verdict. `perp_extra_drift` adds a deterministic extra daily
    return to the PERP series only (spot is untouched) -- used to catch a
    mutant that scores the long/flat secondary on perp instead of spot."""
    import numpy as np
    rng = np.random.default_rng(seed)
    symbols = ("BTCUSDT", "ETHUSDT")
    n_months = warmup_months + hold_months
    price = {s: 1.0 for s in symbols}
    perp_price = {s: 1.0 for s in symbols}
    hist = {s: [] for s in symbols}
    dates, prices = [], {s: [] for s in symbols}
    perp_prices = {s: [] for s in symbols}
    cur = pd.Timestamp("2016-01-01", tz="UTC")
    for _m in range(n_months):
        mu = {}
        for s in symbols:
            trailing = (np.prod([1 + x for x in hist[s][-12:]]) - 1) if len(hist[s]) >= 12 else 0.0
            mu[s] = 0.3 * sigma_m * np.sign(trailing) if trailing != 0 else 0.0
        z1 = rng.normal(0, 1, 30)
        z2 = rng.normal(0, 1, 30)
        d = {}
        d[symbols[0]] = mu[symbols[0]] / 30 + (sigma_m / np.sqrt(30)) * z1
        d[symbols[1]] = mu[symbols[1]] / 30 + (sigma_m / np.sqrt(30)) * (rho * z1 + np.sqrt(1 - rho ** 2) * z2)
        for i in range(30):
            for s in symbols:
                price[s] *= (1 + d[s][i])
                perp_price[s] *= (1 + d[s][i] + perp_extra_drift)
                prices[s].append(price[s])
                perp_prices[s].append(perp_price[s])
            dates.append(cur)
            cur += pd.Timedelta(days=1)
        for s in symbols:
            hist[s].append(np.prod(1 + d[s]) - 1)

    idx = pd.DatetimeIndex(dates)
    spot_daily = pd.DataFrame(prices, index=idx)[list(symbols)]
    perp_daily = pd.DataFrame(perp_prices, index=idx)[list(symbols)]

    month_grid = pd.date_range(idx[0].normalize(), idx[-1].normalize(), freq="MS", tz="UTC")
    decision_times = month_grid[warmup_months:warmup_months + hold_months]
    hold_index = decision_times + pd.DateOffset(months=1)

    perp_month_closes = pd.DataFrame(
        {s: tr.month_end_closes(perp_daily[s]) for s in symbols}
    )[list(symbols)]
    price_ret = tr.monthly_returns(perp_month_closes)
    funding_daily = pd.DataFrame(0.0, index=idx, columns=list(symbols))
    r_tilde = price_ret  # no funding in this synthetic world
    events_by_symbol = {s: pd.DataFrame(columns=["symbol", "time", "rate"]) for s in symbols}

    return (spot_daily, perp_daily, perp_month_closes, events_by_symbol,
            funding_daily, price_ret, r_tilde, decision_times, hold_index)


def _run_synthetic_stage_b(monkeypatch, tmp_path, **kwargs):
    (spot_daily, perp_daily, perp_month_closes, events_by_symbol,
     funding_daily, price_ret, r_tilde, decision_times, hold_index) = \
        _synthetic_stage_b_universe(**kwargs)

    monkeypatch.setattr(tr, "DECISION_TIMES", decision_times)
    monkeypatch.setattr(tr, "HOLD_INDEX", hold_index)
    monkeypatch.setattr(
        tr, "_load_all",
        lambda root, stage: (spot_daily, perp_daily, perp_month_closes,
                              events_by_symbol, funding_daily, price_ret, r_tilde),
    )
    (tmp_path / "e16_stage_a.json").write_text(json.dumps({"verdict": "VALID"}))
    verdict, statistic, path = tr.run_stage_b(tmp_path, results_dir=tmp_path)
    result = json.loads(Path(path).read_text())
    return verdict, statistic, result


def test_run_stage_b_end_to_end_wiring(monkeypatch, tmp_path):
    verdict, statistic, result = _run_synthetic_stage_b(monkeypatch, tmp_path)

    assert result["verdict"] in ("PASS", "FAIL")
    assert set(["mean_x", "mean_d", "ir_x", "ir_d", "median_x", "median_d",
                "pass_95", "secondary_verdict", "secondary_mean_x", "secondary_mean_d",
                "e_bar", "mean_gross_exposure", "capped_months",
                "turnover_per_year", "total_funding_paid"]) <= set(result["statistic"])
    assert set(["x", "d", "median_x", "median_d", "secondary_x", "secondary_d"]) <= set(result["ci"])
    assert set(["per_symbol_x", "per_symbol_d", "net_sharpe", "drawdown_and_es_90",
                "total_return_null_form", "per_year_x", "per_year_d",
                "surprise_rule_triggered", "one_sided_lower_bounds"]) <= set(result["extra"])


def test_run_stage_b_one_sided_bounds_present_amendment1(monkeypatch, tmp_path):
    """Amendment 1 (2026-09-29): 'The report must print the one-sided 80%,
    90% and 95% bounds next to the verdict, as report-only fields.'"""
    _v, statistic, result = _run_synthetic_stage_b(monkeypatch, tmp_path)
    bounds = result["extra"]["one_sided_lower_bounds"]
    assert set(bounds) == {"x", "d"}
    for leg in ("x", "d"):
        assert set(bounds[leg]) == {"80", "90", "95"}
        # tighter confidence -> lower (or equal) bound, monotonic
        assert bounds[leg]["80"] >= bounds[leg]["90"] >= bounds[leg]["95"]

    monthly_path = tr.ARTIFACTS_DIR / "e16_monthly.csv"
    assert monthly_path.exists()
    monthly = pd.read_csv(monthly_path)
    assert 20 <= len(monthly) <= 24


def test_run_stage_b_verdict_matches_primary_verdict_function(monkeypatch, tmp_path):
    verdict, statistic, result = _run_synthetic_stage_b(monkeypatch, tmp_path)
    assert result["verdict"] == tr.trend.primary_verdict(
        result["statistic"]["mean_x"], result["statistic"]["mean_d"])


def test_run_stage_b_secondary_verdict_matches_function(monkeypatch, tmp_path):
    verdict, statistic, result = _run_synthetic_stage_b(monkeypatch, tmp_path)
    assert result["statistic"]["secondary_verdict"] == tr.trend.primary_verdict(
        result["statistic"]["secondary_mean_x"], result["statistic"]["secondary_mean_d"])


def test_run_stage_b_pass_95_matches_function(monkeypatch, tmp_path):
    verdict, statistic, result = _run_synthetic_stage_b(monkeypatch, tmp_path)
    assert result["statistic"]["pass_95"] == tr.trend.pass_95(
        result["ci"]["x"][0], result["ci"]["d"][0])
    assert result["statistic"]["secondary_pass_95"] == tr.trend.pass_95(
        result["ci"]["secondary_x"][0], result["ci"]["secondary_d"][0])


def test_run_stage_b_pass_95_called_with_ci_lower_bounds(monkeypatch, tmp_path):
    """pass_95 is defined over the CI LOWER bounds (lo_x, lo_d) -- a mutant
    that feeds the upper bounds instead can leave the boolean unchanged
    (both bounds can share a sign), so this spies on the exact arguments
    rather than trusting the resulting flag."""
    calls = []
    real = tr.pass_95

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(tr, "pass_95", spy)
    _v, statistic, result = _run_synthetic_stage_b(monkeypatch, tmp_path)
    assert len(calls) >= 1
    primary_call = calls[0]
    assert primary_call[0] == pytest.approx(result["ci"]["x"][0])
    assert primary_call[1] == pytest.approx(result["ci"]["d"][0])


def test_run_stage_b_secondary_scored_on_spot_not_perp(monkeypatch, tmp_path):
    """The long/flat secondary must be scored on SPOT returns with spot
    costs -- not on the perp series the primary condition uses. Giving the
    perp series a large extra drift the spot series never sees must not
    move the secondary's numbers."""
    _v1, _s1, baseline = _run_synthetic_stage_b(monkeypatch, tmp_path, perp_extra_drift=0.0)
    _v2, _s2, shifted = _run_synthetic_stage_b(monkeypatch, tmp_path, perp_extra_drift=0.01)

    assert shifted["statistic"]["secondary_mean_x"] == pytest.approx(
        baseline["statistic"]["secondary_mean_x"], abs=1e-12)
    assert shifted["statistic"]["secondary_mean_d"] == pytest.approx(
        baseline["statistic"]["secondary_mean_d"], abs=1e-12)
    # the primary DOES see the perp drift -- otherwise this test would be
    # vacuous (both conditions insensitive to perp_extra_drift).
    assert shifted["statistic"]["mean_d"] != pytest.approx(
        baseline["statistic"]["mean_d"], abs=1e-9)


def test_run_stage_b_x_uses_null_reference_d_uses_control_reference(monkeypatch, tmp_path):
    """KI-094: x's reference must be the candidate's own signed-exposure
    null N, d's must be the +1 control R+. `primary_verdict` is symmetric
    in its two arguments, so a mutant that swaps `conditions()`'s N_c/R_plus
    arguments does NOT flip PASS/FAIL -- it must still be caught, via the
    saved monthly series rather than the verdict."""
    _v, statistic, _r = _run_synthetic_stage_b(monkeypatch, tmp_path)
    monthly = pd.read_csv(tr.ARTIFACTS_DIR / "e16_monthly.csv")
    assert monthly["x"].to_numpy() == pytest.approx(
        (monthly["R_candidate"] - monthly["N_null"]).to_numpy(), abs=1e-12)
    assert monthly["d"].to_numpy() == pytest.approx(
        (monthly["R_candidate"] - monthly["R_control"]).to_numpy(), abs=1e-12)
    assert statistic["mean_x"] == pytest.approx(float(monthly["x"].mean()))
    assert statistic["mean_d"] == pytest.approx(float(monthly["d"].mean()))


def test_run_stage_b_verdict_uses_mean_not_median(monkeypatch, tmp_path):
    """The D3 staged bar is the point-estimate MEAN > 0 (Amendment 1), not
    the report-only median check. A mutant that verdicts off median_x/
    median_d must be caught even in a scenario where both happen to FAIL
    either way -- so this spies on the exact arguments `primary_verdict`
    is called with, rather than trusting the resulting PASS/FAIL label."""
    calls = []
    real = tr.primary_verdict

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(tr, "primary_verdict", spy)
    _v, statistic, _r = _run_synthetic_stage_b(monkeypatch, tmp_path)
    assert calls, "primary_verdict was never called"
    primary_call = calls[0]
    assert primary_call[0] == pytest.approx(statistic["mean_x"])
    assert primary_call[1] == pytest.approx(statistic["mean_d"])


def test_run_stage_b_secondary_exposure_never_negative(monkeypatch, tmp_path):
    """e^LF = max(e, 0), never rescaled -- a dropped `.clip(lower=0.0)`
    would let it go negative whenever the candidate book is net short."""
    _run_synthetic_stage_b(monkeypatch, tmp_path)
    monthly = pd.read_csv(tr.ARTIFACTS_DIR / "e16_monthly.csv")
    for s in ("BTCUSDT", "ETHUSDT"):
        assert (monthly[f"e_secondary_{s}"] >= 0.0).all()
        assert (monthly[f"e_secondary_{s}"] == monthly[f"e_{s}"].clip(lower=0.0)).all()
