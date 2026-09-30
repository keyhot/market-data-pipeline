"""E15 range forecast report runner. Pre-registered in the vault,
`Docs/range-forecast-experiment.md` -- every window, threshold and formula
is fixed there; this script applies them, never chooses them.

Stage A is plumbing only: it prints and persists NO candidate/control
statistic (no coverage, Winkler or p-value of the candidate, control (i) or
control (ii)) -- only the allowlisted A1-A6 checks. Stage B (real scoring
on 2021-2025) refuses to run without a VALID stage-A result, and stage C
(2026, confirmatory) refuses to run without a stage-B result, running only
the symbol/horizon pairs that passed.

    poetry run python scripts/range_forecast_report.py --stage A
    poetry run python scripts/range_forecast_report.py --stage B
    poetry run python scripts/range_forecast_report.py --stage C
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from model.range_forecast import (  # noqa: E402
    ALPHA_PRIMARY,
    ALPHA_REPORT,
    Calibration,
    dq_design,
    dq_test,
    gate_4h,
    hac_mean_ci,
    hourly_grid,
    lr_cc,
    lr_uc,
    miss,
    overall_verdict,
    primary_pass,
    qlike,
    seasonal_dgp,
    stage_a_valid,
    walk_forward,
    winkler,
)
import report_results  # noqa: E402
from report_results import write_result  # noqa: E402

SYMBOLS = ("BTCUSDT", "ETHUSDT")
HORIZONS = (1, 4)

STAGE_B_START = pd.Timestamp("2021-01-01", tz="UTC")
STAGE_B_LOAD_CUTOFF = pd.Timestamp("2025-12-31 23:45", tz="UTC")
STAGE_B_END = pd.Timestamp("2026-01-01", tz="UTC")
STAGE_C_START = pd.Timestamp("2026-01-01", tz="UTC")
STAGE_C_END = pd.Timestamp("2026-09-27", tz="UTC")
GRID_START = pd.Timestamp("2020-01-01", tz="UTC")

STAGE_A_ALLOWLIST = {
    "a1_ci_lo", "a1_rel_improvement",
    "a2_violations",
    "a3_mismatches",
    "a4_ci_hi",
    "a5_share_by_year",
    "a6_pass_rate_uc", "a6_pass_rate_cc", "a6_pass_rate_dq", "a6_pass_rate_all",
}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load_bars(symbol: str, stage: str) -> pd.DataFrame:
    """15m spot bars for `symbol`, truncated per stage. Stage A and B never
    see a bar opening at or after 2025-12-31 23:45 UTC (frozen note, Test
    window / stage B); stage C loads everything available."""
    from storage.postgres_store import get_price_bars
    bars = pd.DataFrame(get_price_bars(symbol, "15m", 10_000_000))
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
    if stage in ("A", "B"):
        bars = bars[bars["timestamp"] < STAGE_B_LOAD_CUTOFF]
    return bars.reset_index(drop=True)


# --------------------------------------------------------------------------
# Shared scoring helpers (used by stage B/C; stage A calls only A1/A4's
# model pair, never the candidate, and persists no statistic from them)
# --------------------------------------------------------------------------

def _score(grid, h, model, start, end):
    return walk_forward(grid, h, model, start, end)


def _calibration(wf, h, alpha=ALPHA_PRIMARY):
    """UC/CC/DQ on the scored hit sequence. At h=1 the hits don't overlap
    and every origin enters. At h=4, per the frozen note (Tests >
    Calibration, 'At h = 4'), only the non-overlapping origins at UTC
    hours {0,4,8,12,16,20} enter the hit sequence and the DQ design --
    hourly origins in between are overlapping-target scoring artifacts
    (they still carry Winkler, which is scored on all hourly origins
    elsewhere, but not calibration)."""
    if h == 4:
        wf = wf[wf.index.hour.isin((0, 4, 8, 12, 16, 20))]
    hits = miss(wf["r"], wf["L80"], wf["U80"])
    _, p_uc = lr_uc(hits, alpha)
    _, p_cc = lr_cc(hits, alpha)
    X, Hit = dq_design(wf.index, hits, alpha, h)
    _, p_dq, _ = dq_test(X, Hit, alpha)
    coverage = 1.0 - hits.mean()
    return Calibration(p_uc=p_uc, p_cc=p_cc, p_dq=p_dq, coverage=coverage, n=len(wf))


def _winkler_ci(wf_other, wf_candidate, alpha=ALPHA_PRIMARY):
    common = wf_other.index.intersection(wf_candidate.index)
    a = wf_other.loc[common]
    b = wf_candidate.loc[common]
    d = winkler(a["L80"], a["U80"], a["r"], alpha) - winkler(b["L80"], b["U80"], b["r"], alpha)
    mean, lo, hi = hac_mean_ci(d)
    return mean, lo, hi, common


# --------------------------------------------------------------------------
# Stage A
# --------------------------------------------------------------------------

def _a2_alignment(grid, h):
    from model.range_forecast import _realized_return, _target_rv_sum
    r = _realized_return(grid, h)
    rv = _target_rv_sum(grid, h)
    r1_sum = grid["r1"].rolling(h).sum().shift(-(h - 1))
    ok = r.notna() & rv.notna() & r1_sum.notna()
    diff = (r[ok] - r1_sum[ok]).abs()
    cs_bound = np.sqrt(4 * h * rv[ok].to_numpy()) + 1e-12
    v1 = int((diff.to_numpy() > 1e-12).sum())
    v2 = int((r[ok].abs().to_numpy() > cs_bound).sum())
    return v1 + v2


def _a3_truncation(grid, h):
    from model.range_forecast import forecast_variance, seasonal_factor
    rng = np.random.default_rng(7)
    window = grid.loc[(grid.index >= STAGE_B_START) & (grid.index < STAGE_B_END)]
    drawn = list(rng.choice(window.index, size=min(24, len(window)), replace=False))
    for month in ("2021-03", "2022-06", "2023-03", "2024-01", "2025-07", "2025-12"):
        t0 = pd.Timestamp(month, tz="UTC")
        if t0 in grid.index:
            drawn.append(t0)

    mismatches = 0
    for t in drawn:
        T = pd.Timestamp(year=t.year, month=t.month, day=1, tz="UTC")
        truncated = grid.loc[grid.index <= t]
        for model in ("plain_har", "rolling"):
            full_series = forecast_variance(model, grid, T, h)
            trunc_series = forecast_variance(model, truncated, T, h)
            fv = full_series.get(t, np.nan)
            tv = trunc_series.get(t, np.nan)
            if pd.isna(fv) and pd.isna(tv):
                continue
            try:
                np.testing.assert_array_equal(fv, tv)
            except AssertionError:
                mismatches += 1
    return mismatches


def _a4_negative_control(wf_ii, h, alpha=ALPHA_PRIMARY):
    """Permute (ii)'s test-month sigma within each test month (its FHS
    quantiles kept fixed), and compare to unpermuted (ii): CI upper < 0."""
    rng = np.random.default_rng(7)
    permuted = wf_ii.copy()
    for month, idx in wf_ii.groupby(pd.PeriodIndex(wf_ii.index, freq="M")).groups.items():
        idx = list(idx)
        order = rng.permutation(len(idx))
        for col in ("sigma", "L80", "U80"):
            permuted.loc[idx, col] = wf_ii.loc[idx, col].to_numpy()[order]
    d = winkler(wf_ii["L80"], wf_ii["U80"], wf_ii["r"], alpha) - \
        winkler(permuted["L80"], permuted["U80"], permuted["r"], alpha)
    _, _, hi = hac_mean_ci(d)
    return hi


def _a5_validity(grid, h):
    from model.range_forecast import _target_rv_sum
    window = grid.loc[(grid.index >= STAGE_B_START) & (grid.index < STAGE_B_END)]
    target_rv = _target_rv_sum(window, h)
    has_target = target_rv.notna()
    wf = walk_forward(grid, h, "plain_har", STAGE_B_START, STAGE_B_END)
    scored_set = set(wf.index)
    years = window.index.year
    by_year = {}
    for yr in sorted(set(years)):
        yr_mask = years == yr
        n_target = int(has_target.to_numpy()[yr_mask].sum())
        n_scored = sum(1 for t, ok in zip(window.index[yr_mask], has_target.to_numpy()[yr_mask])
                        if ok and t in scored_set)
        by_year[str(yr)] = (n_scored / n_target) if n_target else 1.0
    return by_year


def run_stage_a(symbols=SYMBOLS, results_dir=None):
    statistic = {}
    checks = {}
    inputs = {"symbols": list(symbols), "source": "price_bars 15m spot"}

    for symbol in symbols:
        bars = load_bars(symbol, "A")
        inputs.setdefault("first_bar", {})[symbol] = str(bars["timestamp"].min())
        inputs.setdefault("last_bar", {})[symbol] = str(bars["timestamp"].max())
        grid = hourly_grid(bars)

        for h in HORIZONS:
            key = f"{symbol}_{h}h"
            wf_ii = _score(grid, h, "plain_har", STAGE_B_START, STAGE_B_END)
            wf_oracle = _score(grid, h, "oracle", STAGE_B_START, STAGE_B_END)

            mean, lo, hi, common = _winkler_ci(wf_ii, wf_oracle)
            mean_s_ii = winkler(wf_ii.loc[common, "L80"], wf_ii.loc[common, "U80"],
                                 wf_ii.loc[common, "r"], ALPHA_PRIMARY).mean()
            rel = float(mean / mean_s_ii) if mean_s_ii else float("nan")
            statistic[f"a1_ci_lo_{key}"] = lo
            statistic[f"a1_rel_improvement_{key}"] = rel
            checks[f"a1_{key}"] = bool(lo > 0)

            a2_violations = _a2_alignment(grid, h)
            statistic[f"a2_violations_{key}"] = a2_violations
            checks[f"a2_{key}"] = a2_violations == 0

            a3_mismatches = _a3_truncation(grid, h)
            statistic[f"a3_mismatches_{key}"] = a3_mismatches
            checks[f"a3_{key}"] = a3_mismatches == 0

            a4_ci_hi = _a4_negative_control(wf_ii, h)
            statistic[f"a4_ci_hi_{key}"] = a4_ci_hi
            checks[f"a4_{key}"] = bool(a4_ci_hi < 0)

            a5_by_year = _a5_validity(grid, h)
            statistic[f"a5_share_by_year_{key}"] = a5_by_year
            checks[f"a5_{key}"] = all(v >= 0.99 for v in a5_by_year.values())

    a6 = run_a6()
    for h, rates in a6.items():
        statistic[f"a6_pass_rate_uc_{h}h"] = rates["uc"]
        statistic[f"a6_pass_rate_cc_{h}h"] = rates["cc"]
        statistic[f"a6_pass_rate_dq_{h}h"] = rates["dq"]
        statistic[f"a6_pass_rate_all_{h}h"] = rates["all"]

    valid, failing = stage_a_valid(checks)
    verdict = "VALID" if valid else "VOID"

    path = write_result(
        "e15_stage_a",
        inputs=inputs,
        parameters={
            "alpha_primary": ALPHA_PRIMARY, "alpha_report": ALPHA_REPORT,
            "hac_lag": 168, "rv_floor": 1e-8,
            "factor_window_h": 8736, "roll_window_h": 168,
            "min_valid": {"24": 12, "168": 84}, "min_cell": 40,
            "purge": "h", "refit": "monthly",
        },
        statistic=statistic,
        ci={},
        verdict=verdict,
        extra={"failing_checks": failing},
        results_dir=results_dir,
    )
    print(f"stage A: {verdict} (failing: {failing}) -> {path}")
    return verdict, failing, statistic


def run_a6(seeds=range(10), years_scored=5.0, warmup_years=1.0, horizons=(1,)):
    results = {}
    for h in horizons:
        uc_pass = cc_pass = dq_pass = all_pass = 0
        for seed in seeds:
            bars = seasonal_dgp(seed=seed, years=warmup_years + years_scored, seasonal=True)
            grid = hourly_grid(bars)
            end = grid.index[-1] + pd.Timedelta(hours=1)
            start = grid.index[0] + pd.Timedelta(hours=int(warmup_years * 8760))
            wf = walk_forward(grid, h, "seasonal_har", start, end)
            if len(wf) == 0:
                continue
            hits = miss(wf["r"], wf["L80"], wf["U80"])
            _, p_uc = lr_uc(hits, ALPHA_PRIMARY)
            _, p_cc = lr_cc(hits, ALPHA_PRIMARY)
            X, Hit = dq_design(wf.index, hits, ALPHA_PRIMARY, h)
            _, p_dq, _ = dq_test(X, Hit, ALPHA_PRIMARY)
            ok_uc, ok_cc, ok_dq = p_uc >= 0.05, p_cc >= 0.05, p_dq >= 0.05
            uc_pass += ok_uc
            cc_pass += ok_cc
            dq_pass += ok_dq
            all_pass += (ok_uc and ok_cc and ok_dq)
        n = len(list(seeds))
        results[h] = {
            "uc": uc_pass / n, "cc": cc_pass / n, "dq": dq_pass / n, "all": all_pass / n,
        }
    return results


# --------------------------------------------------------------------------
# Stage B / C
# --------------------------------------------------------------------------

def run_stage_b(symbols=SYMBOLS, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    a_path = rd / "e15_stage_a.json"
    if not a_path.exists():
        raise RuntimeError("stage A result missing; run --stage A first")
    a_result = json.loads(a_path.read_text())
    if a_result.get("verdict") != "VALID":
        raise RuntimeError(f"stage A verdict is {a_result.get('verdict')!r}, not VALID; refusing")

    inputs, statistic, ci, extra = {"symbols": list(symbols)}, {}, {}, {}
    symbol_pass = {}
    fourh_pass = {}
    for symbol in symbols:
        bars = load_bars(symbol, "B")
        grid = hourly_grid(bars)
        for h in HORIZONS:
            wf_cand = _score(grid, h, "seasonal_har", STAGE_B_START, STAGE_B_END)
            wf_ii = _score(grid, h, "plain_har", STAGE_B_START, STAGE_B_END)
            common = wf_cand.index.intersection(wf_ii.index)
            wf_cand_c, wf_ii_c = wf_cand.loc[common], wf_ii.loc[common]
            cal = _calibration(wf_cand_c, h)
            mean, lo, hi, _ = _winkler_ci(wf_ii_c, wf_cand_c)
            key = f"{symbol}_{h}h"
            statistic[key] = {
                "p_uc": cal.p_uc, "p_cc": cal.p_cc, "p_dq": cal.p_dq,
                "coverage": cal.coverage, "n": cal.n, "mean_d": mean,
            }
            ci[key] = [lo, hi]
            if h == 1:
                passed = primary_pass(cal, lo)
                symbol_pass[symbol] = passed
                statistic[key]["verdict"] = "PASS" if passed else "FAIL"
            else:
                gate = gate_4h(symbol_pass.get(symbol, False), cal, lo)
                fourh_pass[symbol] = gate
                statistic[key]["verdict"] = "PASS" if gate else "FAIL"

    overall = overall_verdict(symbol_pass)
    statistic["_4h_gate"] = fourh_pass
    path = write_result(
        "e15_stage_b", inputs=inputs,
        parameters={"alpha_primary": ALPHA_PRIMARY, "p_level": 0.05},
        statistic=statistic, ci=ci, verdict=overall, extra=extra,
        results_dir=results_dir,
    )
    print(f"stage B: {overall} -> {path}")
    return overall, statistic


def run_stage_c(symbols=SYMBOLS, results_dir=None):
    rd = Path(results_dir) if results_dir is not None else report_results.RESULTS_DIR
    b_path = rd / "e15_stage_b.json"
    if not b_path.exists():
        raise RuntimeError("stage B result missing; run --stage B first")
    b_result = json.loads(b_path.read_text())

    statistic = {}
    for symbol in symbols:
        for h in HORIZONS:
            key = f"{symbol}_{h}h"
            entry = b_result.get("statistic", {}).get(key, {})
            if entry.get("verdict") != "PASS":
                continue
            bars = load_bars(symbol, "C")
            grid = hourly_grid(bars)
            wf_cand = _score(grid, h, "seasonal_har", STAGE_C_START, STAGE_C_END)
            if len(wf_cand) == 0:
                statistic[key] = {"verdict": "NOT_CONFIRMED", "reason": "no scored rows"}
                continue
            cal = _calibration(wf_cand, h)
            passed = cal.p_uc >= 0.05 and cal.p_cc >= 0.05 and cal.p_dq >= 0.05
            statistic[key] = {
                "p_uc": cal.p_uc, "p_cc": cal.p_cc, "p_dq": cal.p_dq,
                "coverage": cal.coverage, "n": cal.n,
                "verdict": "CONFIRMED" if passed else "NOT_CONFIRMED",
            }

    verdict = "CONFIRMED" if statistic and all(
        v.get("verdict") == "CONFIRMED" for v in statistic.values()) else "NOT_CONFIRMED"
    path = write_result(
        "e15_stage_c", inputs={"symbols": list(symbols)},
        parameters={"alpha_primary": ALPHA_PRIMARY},
        statistic=statistic, ci={}, verdict=verdict,
        results_dir=results_dir,
    )
    print(f"stage C: {verdict} -> {path}")
    return verdict, statistic


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["A", "B", "C"], required=True)
    args = parser.parse_args(argv)
    if args.stage == "A":
        run_stage_a()
    elif args.stage == "B":
        run_stage_b()
    else:
        run_stage_c()


if __name__ == "__main__":
    main()
