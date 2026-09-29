"""E15 A6b: post-freeze, report-only synthetic strictness diagnostic.

**This NEVER gates E15's verdict.** It is not stage A, B or C, and it does
not change stage A's VALID/VOID verdict or stage B's PASS/SPLIT/FAIL. It
answers the vault's open "UC strictness at this sample size" question with
data: how does the frozen UC/CC/DQ conjunction behave, at stage-B scale, on
a model that is slightly WRONG (a controlled coverage deficit, or a DGP
outside log-HAR)? See `Docs/range-forecast-experiment.md`'s "A6b" section
and `model/range_forecast_a6b.py`.

Reads NO real market data anywhere in this script.

    poetry run python scripts/e15_a6b_report.py [--seeds N] [--calib-seeds N]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from model.range_forecast import (  # noqa: E402
    ALPHA_PRIMARY,
    dq_design,
    dq_test,
    hourly_grid,
    lr_cc,
    lr_ind,
    lr_uc,
    miss,
    seasonal_dgp,
    walk_forward,
)
from model.range_forecast_a6b import (  # noqa: E402
    calibrate_distortion_factor,
    distort_band,
    empirical_coverage,
    garch_t_dgp,
    uc_tolerant_pass,
)
import report_results  # noqa: E402
from report_results import write_result  # noqa: E402

H = 1  # h=4 is out of scope for A6b -- see the runner's docstring note below
ALPHA = ALPHA_PRIMARY
WARMUP_YEARS = 1.0
YEARS_SCORED = 5.0
CALIB_WARMUP_YEARS = 1.0
CALIB_YEARS_SCORED = 5.0
CALIB_SEED_BASE = 1000
TARGETS = (0.795, 0.780)


# --------------------------------------------------------------------------
# Shared walk-forward + scoring helpers
# --------------------------------------------------------------------------

def _wf_for(dgp_fn, seed: int, warmup_years: float, years_scored: float, h: int = H):
    """One seed's stage-B-shaped walk-forward: `warmup_years` of history
    the fold-refits can train on, then `years_scored` of scored origins,
    scored on the seasonal_har candidate (fit unchanged)."""
    bars = dgp_fn(seed=seed, years=warmup_years + years_scored, seasonal=True)
    grid = hourly_grid(bars)
    start = grid.index[0] + pd.Timedelta(hours=int(round(warmup_years * 8760)))
    end = grid.index[-1] + pd.Timedelta(hours=1)
    return walk_forward(grid, h, "seasonal_har", start, end)


def _summarize(wf: pd.DataFrame, k: float, alpha: float = ALPHA, h: int = H) -> dict:
    """One seed's row under distortion factor `k` (k=1 = undistorted):
    coverage, the frozen UC/CC/DQ p-values plus IND (report-only), and the
    strict vs. ±2pp-tolerant UC pass/fail and full-conjunction pass/fail."""
    L, U = distort_band(wf["L80"].to_numpy(), wf["U80"].to_numpy(), k)
    r = wf["r"].to_numpy()
    hits = miss(r, L, U)
    coverage = empirical_coverage(r, L, U)

    tolerant_pass, p_uc = uc_tolerant_pass(hits, alpha)
    strict_uc = bool(p_uc >= 0.05)
    _, p_cc = lr_cc(hits, alpha)
    _, p_ind = lr_ind(hits)
    X, Hit = dq_design(wf.index, hits, alpha, h)
    _, p_dq, _ = dq_test(X, Hit, alpha)
    cc_ok = bool(p_cc >= 0.05)
    dq_ok = bool(p_dq >= 0.05)

    return {
        "n": int(len(hits)),
        "coverage": coverage,
        "p_uc": p_uc, "p_cc": p_cc, "p_dq": p_dq, "p_ind": p_ind,
        "uc_strict": strict_uc, "uc_tolerant": bool(tolerant_pass),
        "cc": cc_ok, "dq": dq_ok,
        "ind_report_only": bool(p_ind >= 0.05),
        "all_strict": bool(strict_uc and cc_ok and dq_ok),
        "all_tolerant": bool(tolerant_pass and cc_ok and dq_ok),
    }


def _aggregate(rows: list[dict]) -> dict:
    n_seeds = len(rows)
    out = {"n_seeds": n_seeds, "n_origins_mean": float(np.mean([r["n"] for r in rows]))}
    out["coverage_mean"] = float(np.mean([r["coverage"] for r in rows]))
    out["coverage_std"] = float(np.std([r["coverage"] for r in rows]))
    for key in ("uc_strict", "uc_tolerant", "cc", "dq", "ind_report_only",
                "all_strict", "all_tolerant"):
        out[f"{key}_pass_rate"] = float(np.mean([bool(r[key]) for r in rows]))
    out["p_uc_mean"] = float(np.mean([r["p_uc"] for r in rows]))
    return out


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_a6b(seeds=range(10), calib_seeds=None,
            warmup_years: float = WARMUP_YEARS, years_scored: float = YEARS_SCORED,
            calib_warmup_years: float = CALIB_WARMUP_YEARS,
            calib_years_scored: float = CALIB_YEARS_SCORED,
            h: int = H, verbose: bool = False) -> dict:
    seeds = list(seeds)
    if calib_seeds is None:
        calib_seeds = range(CALIB_SEED_BASE, CALIB_SEED_BASE + 25)
    calib_seeds = list(calib_seeds)

    # -- 1. Calibrate the distortion factor on a POOLED, SEPARATE-SEED
    # simulation (never the seeds A6b actually scores) so 79.5%/78.0% are
    # achieved coverage on real (if synthetic) data, not assumed by fiat.
    t0 = time.time()
    calib_frames = [
        _wf_for(seasonal_dgp, seed=s, warmup_years=calib_warmup_years,
                years_scored=calib_years_scored, h=h)
        for s in calib_seeds
    ]
    pooled_r = np.concatenate([f["r"].to_numpy() for f in calib_frames])
    pooled_L = np.concatenate([f["L80"].to_numpy() for f in calib_frames])
    pooled_U = np.concatenate([f["U80"].to_numpy() for f in calib_frames])
    pooled_coverage_k1 = empirical_coverage(pooled_r, pooled_L, pooled_U)

    calibration = {
        "calib_seeds": calib_seeds,
        "pooled_n": int(len(pooled_r)),
        "pooled_coverage_k1": pooled_coverage_k1,
    }
    ks = {}
    for target in TARGETS:
        k, achieved = calibrate_distortion_factor(pooled_r, pooled_L, pooled_U, target)
        key = f"target_{int(round(target * 1000))}"
        calibration[key] = {"target": target, "k": k, "achieved_coverage": achieved}
        ks[target] = k
    if verbose:
        print(f"  calibration: pooled_n={calibration['pooled_n']} "
              f"cov_k1={pooled_coverage_k1:.5f} "
              f"k_795={ks[0.795]:.5f} k_780={ks[0.780]:.5f} "
              f"({time.time() - t0:.1f}s)")

    # -- 2. Seasonal (log-HAR, the candidate's own family) seeds: control
    # (k=1), the 79.5% deficit and the 78.0% deficit share ONE walk-forward
    # run per seed (only the reported interval is distorted post-hoc).
    variants = {"control_80": [], "coverage_795": [], "coverage_780": []}
    t1 = time.time()
    for seed in seeds:
        wf = _wf_for(seasonal_dgp, seed=seed, warmup_years=warmup_years,
                     years_scored=years_scored, h=h)
        variants["control_80"].append(_summarize(wf, k=1.0, h=h))
        variants["coverage_795"].append(_summarize(wf, k=ks[0.795], h=h))
        variants["coverage_780"].append(_summarize(wf, k=ks[0.780], h=h))
    if verbose:
        print(f"  seasonal seeds done ({time.time() - t1:.1f}s)")

    # -- 3. Family misspecification: GARCH(1,1)-t(5), candidate fit
    # unchanged, undistorted (k=1) -- report whatever coverage it lands on.
    t2 = time.time()
    variants["misspecified_garch_t5"] = []
    for seed in seeds:
        wf = _wf_for(garch_t_dgp, seed=seed, warmup_years=warmup_years,
                     years_scored=years_scored, h=h)
        variants["misspecified_garch_t5"].append(_summarize(wf, k=1.0, h=h))
    if verbose:
        print(f"  misspecified seeds done ({time.time() - t2:.1f}s)")

    aggregated = {name: _aggregate(rows) for name, rows in variants.items()}

    return {
        "h": h,
        "seeds": seeds,
        "calib_seeds": calib_seeds,
        "calibration": calibration,
        "variants": aggregated,
        "raw_per_seed": variants,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, default=10,
                         help="number of A6b test seeds (0..N-1), default 10 (A6's own count)")
    parser.add_argument("--calib-seeds", type=int, default=25,
                         help="number of pooled-calibration seeds, default 25")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    seeds = range(args.seeds)
    calib_seeds = range(CALIB_SEED_BASE, CALIB_SEED_BASE + args.calib_seeds)

    result = run_a6b(seeds=seeds, calib_seeds=calib_seeds, verbose=args.verbose)

    # The results JSON persists the aggregates; the (larger) per-seed raw
    # rows are useful for debugging a run but are not part of the report.
    statistic = {"calibration": result["calibration"], "variants": result["variants"]}
    path = write_result(
        "e15_a6b",
        inputs={"seeds": list(result["seeds"]), "calib_seeds": list(result["calib_seeds"]),
                "note": "synthetic only; reads no real market data; report-only, never gates"},
        parameters={"alpha_primary": ALPHA, "h": result["h"],
                    "warmup_years": WARMUP_YEARS, "years_scored": YEARS_SCORED,
                    "calib_warmup_years": CALIB_WARMUP_YEARS,
                    "calib_years_scored": CALIB_YEARS_SCORED,
                    "uc_tolerance_pp": 2.0, "targets": list(TARGETS)},
        statistic=statistic,
        ci={},
        verdict="REPORT_ONLY",
        extra={},
    )
    print(f"A6b: REPORT_ONLY -> {path}")
    for name, agg in result["variants"].items():
        g = agg.get
        nan = float("nan")
        print(f"  {name}: coverage={g('coverage_mean', nan):.4f} "
              f"uc_strict={g('uc_strict_pass_rate', nan):.2f} "
              f"uc_tolerant={g('uc_tolerant_pass_rate', nan):.2f} "
              f"cc={g('cc_pass_rate', nan):.2f} dq={g('dq_pass_rate', nan):.2f} "
              f"all_strict={g('all_strict_pass_rate', nan):.2f} "
              f"all_tolerant={g('all_tolerant_pass_rate', nan):.2f}")
    return result


if __name__ == "__main__":
    main()
