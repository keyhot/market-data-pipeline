"""E17 stage A0: can a 2026 read of "DVOL fixes the range band" show
anything at all? Design: vault `Docs/e17-preregistration.md` (DRAFT).

  fetch   pull hourly DVOL (BTC, ETH) from Deribit's public endpoint into
          data/dvol/*.csv (idempotent merge); prints counts and first/last
          timestamps only -- never a value.
  a0      kill 1 (DVOL gaps, timestamps only, 2021-03-24 -> 2026-09-27),
          kill 2 (the E15 control's DQ in six 9-month development blocks,
          real 2021-25 data, already read by E15), kill 3 (synthetic power
          of the paired Winkler contrast on a 9-month window) plus the
          harness self-checks (strong plant, exchangeable-null size,
          monotone power). Writes scripts/results/e17_stage_a0.json.

NEVER loads a 2026 bar or DVOL value for scoring: bars come through E15's
`load_bars(symbol, "B")` (cut < 2025-12-31 23:45) and DVOL values through
`truncate_values` (cut < 2026-01-01).
"""

from __future__ import annotations

import argparse
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ingestion import deribit_dvol as dv  # noqa: E402
from model import implied_vol_band as ivb  # noqa: E402
from model.range_forecast import (  # noqa: E402
    ALPHA_PRIMARY, dq_design, dq_test, hourly_grid, miss, walk_forward,
)
from report_results import write_result  # noqa: E402

DVOL_DIR = ROOT / "data" / "dvol"
PAIRS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
CONTROL_START = pd.Timestamp("2021-01-01", tz="UTC")
SYN_START = pd.Timestamp("2024-04-01", tz="UTC")
SYN_IV_START = pd.Timestamp("2024-10-01", tz="UTC")
SYN_YEARS = 2.5
N_CALIB = 30
N_POWER = 300
N_STRONG = 100
PROCS = 3


# --------------------------------------------------------------------------
# DVOL cache
# --------------------------------------------------------------------------

def cache_path(currency: str) -> Path:
    return DVOL_DIR / f"{currency}_1h.csv"


def fetch(currencies=("BTC", "ETH"), get_json=dv.http_get_json) -> dict:
    out = {}
    now_ms = int(time.time() * 1000)
    for cur in currencies:
        new = dv.fetch_dvol(cur, dv.DVOL_START_MS, now_ms, get_json=get_json)
        merged = dv.merge_dvol(dv.load_dvol_csv(cache_path(cur)), new)
        dv.save_dvol_csv(merged, cache_path(cur))
        out[cur] = {"rows": len(merged), "first": str(merged["ts"].min()),
                    "last": str(merged["ts"].max())}
    return out


def load_dvol_for_scoring(currency: str) -> pd.DataFrame:
    """DVOL values, cut strictly before 2026 (the verdict window)."""
    return ivb.truncate_values(dv.load_dvol_csv(cache_path(currency)))


def gap_report() -> dict:
    """Kill 1: counts by timestamp only (2026 included, values never read)."""
    return {cur: ivb.gap_share(dv.load_dvol_csv(cache_path(cur))["ts"],
                               ivb.DVOL_FIRST, ivb.GAP_END)
            for cur in PAIRS.values()}


# --------------------------------------------------------------------------
# Kill 2 (real, pre-2026)
# --------------------------------------------------------------------------

def control_blocks(bars: pd.DataFrame, dvol: pd.DataFrame) -> dict:
    grid = hourly_grid(bars)
    wf = walk_forward(grid, 1, "seasonal_har", CONTROL_START, ivb.SCORING_CUTOFF)
    x = ivb.iv_feature(dvol, wf.index)
    rows = wf[x.notna().to_numpy()]
    blocks = ivb.block_dq(rows, ivb.dev_blocks())
    stub = ivb.block_dq(rows, [ivb.DEV_STUB])[0]
    return {"blocks": blocks, "stub_report_only": stub,
            "fails": int(sum(b["fail"] for b in blocks)),
            "rows": int(len(rows)), "control_rows": int(len(wf))}


def kill2(symbols=tuple(PAIRS)) -> dict:
    from range_forecast_report import load_bars
    out = {}
    for sym in symbols:
        bars = load_bars(sym, "B")
        assert pd.to_datetime(bars["timestamp"], utc=True).max() < ivb.SCORING_CUTOFF
        dvol = load_dvol_for_scoring(PAIRS[sym])
        out[sym] = control_blocks(bars, dvol)
    return out


# --------------------------------------------------------------------------
# Kill 3 (synthetic)
# --------------------------------------------------------------------------

def _verdict_wf(grid, extra=None):
    wf = walk_forward(grid, 1, "seasonal_har", ivb.VERDICT_START, ivb.VERDICT_END, extra=extra)
    return wf[wf.index < ivb.VERDICT_END]


def _dq_stat(wf) -> float:
    hits = miss(wf["r"], wf["L80"], wf["U80"])
    X, Hit = dq_design(wf.index, hits, ALPHA_PRIMARY, 1)
    return dq_test(X, Hit, ALPHA_PRIMARY)[0]


def seed_trial(args) -> dict:
    """One synthetic path. kappas=() -> control DQ only (calibration)."""
    seed, sd_u, kappas, half_life, with_null = args
    bars, x_iv0, u = ivb.iv_dgp(seed, sd_u, 0.0, SYN_START, SYN_YEARS, SYN_IV_START,
                                half_life=half_life)
    grid = hourly_grid(bars)
    ctrl = _verdict_wf(grid)
    out = {"seed": seed, "n": int(len(ctrl)), "control_dq": float(_dq_stat(ctrl)), "kappa": {}}
    for k in kappas:
        _, x_iv, _ = ivb.iv_dgp(seed, sd_u, k, SYN_START, SYN_YEARS, SYN_IV_START,
                                half_life=half_life) if k != 0.0 else (None, x_iv0, None)
        cand = _verdict_wf(grid, extra=x_iv)
        mean, lo, hi, n = ivb.paired_contrast(ctrl, cand)
        out["kappa"][k] = {"mean": mean, "lo": lo, "hi": hi, "n": n,
                           "pass": ivb.primary_pass(lo)}
        if k == 0.0 and with_null:
            twin = _verdict_wf(grid, extra=ivb.noise_column(seed, x_iv0.index, SYN_IV_START))
            m2, lo2, hi2, _ = ivb.paired_contrast(twin, cand)
            out["null"] = {"mean": m2, "lo": lo2, "hi": hi2,
                           "reject2": ivb.two_sided_reject(lo2, hi2)}
    return out


def _run(jobs):
    with Pool(PROCS) as pool:
        return pool.map(seed_trial, jobs)


def kill3(target_dq: float) -> dict:
    calib = {}
    for sd in ivb.SD_U_GRID:
        res = _run([(s, sd, (), ivb.HALF_LIFE_H, False) for s in range(N_CALIB)])
        calib[sd] = float(np.median([r["control_dq"] for r in res]))
        if calib[sd] >= target_dq:
            break
    sd_u = ivb.choose_sd_u(calib, target_dq)
    sd_used = sd_u if sd_u is not None else max(ivb.SD_U_GRID)
    res = _run([(1000 + s, sd_used, ivb.KAPPAS, ivb.HALF_LIFE_H, True) for s in range(N_POWER)])
    power = {k: float(np.mean([r["kappa"][k]["pass"] for r in res])) for k in ivb.KAPPAS}
    mean_d = {k: float(np.mean([r["kappa"][k]["mean"] for r in res])) for k in ivb.KAPPAS}
    size2 = float(np.mean([r["null"]["reject2"] for r in res]))
    mono_ok, dips = ivb.is_monotone([power[k] for k in ivb.KAPPAS], N_POWER)
    strong = _run([(5000 + s, ivb.STRONG_SD_U, (1.0,), ivb.STRONG_HALF_LIFE_H, False)
                   for s in range(N_STRONG)])
    strong_power = float(np.mean([r["kappa"][1.0]["pass"] for r in strong]))
    return {
        "calibration_median_control_dq": calib, "target_dq": target_dq,
        "sd_u": sd_u, "sd_u_used": sd_used,
        "sd_u_capped": sd_u is None,
        "power_by_kappa": power, "mean_d_bps_by_kappa": mean_d,
        "power_at_gate": power[ivb.POWER_KAPPA],
        "size_two_sided_exchangeable": size2,
        "one_sided_pass_rate_kappa0": power[0.0],
        "monotone": mono_ok, "dips": dips,
        "strong_power": strong_power,
        "median_verdict_rows": float(np.median([r["n"] for r in res])),
        "synthetic_control_dq_median_at_sd_u": float(np.median([r["control_dq"] for r in res])),
    }


def self_checks(k3: dict) -> dict:
    lo, hi = ivb.SIZE_BAND
    return {"strong_power_ok": k3["strong_power"] >= ivb.STRONG_POWER_MIN,
            "size_ok": lo <= k3["size_two_sided_exchangeable"] <= hi,
            "monotone_ok": bool(k3["monotone"])}


def run_a0(results_dir=None) -> tuple[str, dict]:
    gaps = gap_report()
    k2 = kill2()
    target = float(np.median([b["stat"] for s in k2.values() for b in s["blocks"]]))
    k3 = kill3(target)
    checks = self_checks(k3)
    fired = ivb.a0_kills({c: g["share"] for c, g in gaps.items()},
                         {s: v["fails"] for s, v in k2.items()}, k3["power_at_gate"])
    verdict = "VOID" if not all(checks.values()) else ("KILLED" if fired else "PASS")
    stat = {"kills_fired": fired, "self_checks": checks,
            "block_fails": {s: v["fails"] for s, v in k2.items()},
            "gap_share": {c: g["share"] for c, g in gaps.items()},
            "power_at_gate": k3["power_at_gate"],
            "size_two_sided_exchangeable": k3["size_two_sided_exchangeable"],
            "strong_power": k3["strong_power"]}
    write_result("e17_stage_a0",
                 inputs={"bars": "price_bars 15m via load_bars(stage='B'), < 2025-12-31 23:45",
                         "dvol_values": "< 2026-01-01 only; 2026 counted by timestamp",
                         "synthetic": f"iv_dgp, bars from {SYN_START.date()}, IV from {SYN_IV_START.date()}"},
                 parameters={"gap_max": ivb.GAP_MAX, "block_fail_min": ivb.BLOCK_FAIL_MIN,
                             "power_min": ivb.POWER_MIN, "power_kappa": ivb.POWER_KAPPA,
                             "kappas": ivb.KAPPAS, "sd_u_grid": ivb.SD_U_GRID,
                             "half_life_h": ivb.HALF_LIFE_H, "n_calib": N_CALIB,
                             "n_power": N_POWER, "n_strong": N_STRONG,
                             "strong": [ivb.STRONG_SD_U, ivb.STRONG_HALF_LIFE_H],
                             "size_band": ivb.SIZE_BAND},
                 statistic=stat, ci={}, verdict=verdict,
                 extra={"gaps": gaps, "kill2": k2, "kill3": k3},
                 results_dir=results_dir)
    return verdict, stat


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "a0"])
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        for cur, info in fetch().items():
            print(cur, info)  # counts and timestamps only
    else:
        verdict, stat = run_a0()
        print("E17 A0:", verdict, stat)


if __name__ == "__main__":
    main()
