"""R3-b Stage A runner: the development year (2024) only, frozen spec.

Rules: vault ``Docs/r3b-preregistration.md`` §Frozen spec. Loads the A0 dev
cache (C1 blocks already dropped at parse) and slices it to 2024 BEFORE any
computation; no other bar is ever passed to a scorer function. k is chosen by
the pre-stated fill-rate rule (outcome-blind); 2024 outcomes are then scored
once, plus the self-checks. Writes scripts/results/r3b_stage_a.json.

    R3B_CACHE=/path/to/.cache/r3b poetry run python scripts/r3b_stage_a.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model import wick_catch as wc  # noqa: E402

CACHE = Path(os.environ.get("R3B_CACHE", ROOT / ".cache" / "r3b"))
OUT = ROOT / "scripts" / "results" / "r3b_stage_a.json"
SYMS = ("PF_XBTUSD", "PF_ETHUSD")
DEV = ("2024-01-01", "2025-01-01")                 # [start, end): the development year
DEV_DAYS = ("2024-01-02", "2024-12-31")            # eligible days (1st of month excluded)
VERDICT_DAYS = ("2025-01-01", "2026-09-27")
K_GRID = [round(2.0 + 0.25 * i, 2) for i in range(41)]
SEED = 20261005
BOOT_REAL, BOOT_SYN, SEEDS = 9999, 999, 300
PLACEBO_DRAWS = 300
MIN_DEV_FILLS = 100


def load(sym):
    # Local cache written by scripts/r3b_a0_report.py (gitignored), never external input.
    b = pd.read_pickle(CACHE / f"{sym}_dev.pkl")
    b = wc.slice_window(b, *DEV)
    assert b.index.min() >= pd.Timestamp(DEV[0], tz="UTC") and b.index.max() < pd.Timestamp(DEV[1], tz="UTC")
    return b


def rate(fills, days):
    return len(fills[fills.index.strftime("%Y-%m-%d").isin(days)]) / max(len(days), 1)


def main():
    bars = {s: load(s) for s in SYMS}
    days = wc._eligible_index(*DEV_DAYS).strftime("%Y-%m-%d")
    res = {"git_hash": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                      capture_output=True, text=True).stdout.strip(),
           "window": list(DEV), "eligible_days": len(days), "symbols": {}}

    # data gates (refuse, not trim)
    gates = {s: wc.data_gate(bars[s], DEV[0], DEV[1]) for s in SYMS}
    sig = {s: wc.trailing_sigma_traded(bars[s]) for s in SYMS}

    cache = {}

    def fills(s, k, touch=False):
        if (s, k, touch) not in cache:
            cache[(s, k, touch)] = wc.wick_fills(bars[s], k=k, tick=wc.TICKS[s], touch=touch,
                                                 sigma=sig[s], require_hold_present=True)
        return cache[(s, k, touch)]

    # k: outcome-blind fill-rate rule
    k = wc.choose_k(lambda kk: max(rate(fills(s, kk), days) for s in SYMS), K_GRID, 1.0)
    res["k"] = k
    res["k_rates"] = {str(kk): {s: rate(fills(s, kk), days) for s in SYMS}
                      for kk in K_GRID if k is not None and abs(kk - k) <= 1.0}
    if k is None:
        res["stage_a"] = {"result": "INVALID", "why": "no k on grid gives <= 1 fill/symbol/day"}
        OUT.write_text(json.dumps(res, indent=1, default=str))
        return

    # K1/K2 re-check on 2024 tuples (pre-trigger info only)
    f = {s: fills(s, k) for s in SYMS}
    tup = wc.day_tuples(f[SYMS[0]].index, f[SYMS[1]].index, *DEV_DAYS)
    nv = wc.eligible_days(*VERDICT_DAYS)
    syn = {"per_day": float(tup[:, :2].sum(1).mean()), "gating_N": float(tup[:, :2].sum(1).mean() * nv),
           "power30": wc.power(30, tup, nv, icc=0.10, n_seeds=SEEDS, n_boot=BOOT_SYN, seed=21),
           "null": wc.power(0, tup, nv, icc=0.10, n_seeds=SEEDS, n_boot=BOOT_SYN, seed=22),
           "plant_200": wc.power(200, tup, nv, icc=0.10, n_seeds=SEEDS, n_boot=BOOT_SYN, seed=23),
           "sign_mutant_-200": wc.power(-200, tup, nv, icc=0.10, n_seeds=SEEDS, n_boot=BOOT_SYN, seed=24),
           "null_icc0.20_clustered": wc.power(0, tup, nv, icc=0.20, n_seeds=SEEDS, n_boot=BOOT_SYN, seed=25),
           "null_icc0.20_iid_mutant": wc.power(0, tup, nv, icc=0.20, n_seeds=SEEDS, n_boot=BOOT_SYN,
                                               seed=25, iid_bootstrap=True)}
    res["synthetic"] = syn

    # score 2024 (the one real read) + controls
    rng = np.random.default_rng(SEED)
    scored, ctl = {}, {}
    for s in SYMS:
        scored[s] = wc.score_fills(bars[s], f[s])
        ctl[s] = wc.control_returns(bars[s], f[s], rng=rng)
        b = bars[s]
        res["symbols"][s] = {
            "n_bars": int(len(b)), "gate": gates[s], "fills": int(len(f[s])),
            "rate_per_day": rate(f[s], days), "bid_share": float((f[s]["side"] == "bid").mean()),
            "one_tick_through_share": float((f[s]["through_ticks"] < 2 - 1e-9).mean()),
            "ambiguous_bars": int(f[s].attrs["ambiguous"]),
            "touch_fills": int(len(fills(s, k, touch=True))),
            "depth_bps_median": float(np.median(np.abs(
                f[s]["limit"] / wc._grid(b)["close"].shift(1).reindex(f[s].index) - 1) * 1e4)),
            "controls_missing": int(np.isnan(ctl[s]).sum())}

    def pooled(sc, cc, rng_, n_boot):
        x = np.concatenate([sc[s]["net_bps"].to_numpy() for s in SYMS])
        c = np.concatenate([cc[s] for s in SYMS])
        d = np.concatenate([sc[s].index.strftime("%Y-%m-%d") for s in SYMS])
        ok = np.isfinite(x) & np.isfinite(c)
        return wc.cluster_bootstrap(x[ok], c[ok], d[ok], n_boot=n_boot, rng=rng_)

    res["dev_result"] = pooled(scored, ctl, np.random.default_rng(SEED + 1), BOOT_REAL)
    res["descriptive"] = {}
    for s in SYMS:
        for side in ("bid", "ask"):
            m = (scored[s]["side"] == side).to_numpy()
            x, c = scored[s]["net_bps"].to_numpy()[m], ctl[s][m]
            res["descriptive"][f"{s}_{side}"] = {"n": int(m.sum()), "mean_net": float(np.nanmean(x)),
                                                 "mean_control": float(np.nanmean(c)),
                                                 "mean_paired": float(np.nanmean(x - c))}
        res["descriptive"][f"{s}_gross_mean"] = float(scored[s]["gross_bps"].mean())

    # S1 plant: +200 bps at close_{t+30}, fills held fixed (prefix-invariant), full scorer after
    pl_sc, pl_ct = {}, {}
    rng_p = np.random.default_rng(SEED)
    for s in SYMS:
        pb = wc.plant_exit(bars[s], f[s], 200.0)
        pl_sc[s] = wc.score_fills(pb, f[s])
        pl_ct[s] = wc.control_returns(pb, f[s], rng=rng_p)
    pr = pooled(pl_sc, pl_ct, np.random.default_rng(SEED + 1), BOOT_REAL)
    d_prim = pr["mean_primary"] - res["dev_result"]["mean_primary"]
    d_pair = pr["mean_paired"] - res["dev_result"]["mean_paired"]
    res["plant"] = {"delta_primary": d_prim, "delta_paired": d_pair, "pass_test": pr["pass"],
                    "ok": bool(180 <= d_prim <= 225 and 180 <= d_pair <= 225 and pr["pass"])}

    # S2 placebo null on real 2024 bars: random same-day same-side market entries as events
    rej_pair = rej_iid = 0
    rng_n = np.random.default_rng(SEED + 2)
    for _ in range(PLACEBO_DRAWS):
        psc, pct = {}, {}
        for s in SYMS:
            pe = wc.placebo_events(bars[s], f[s], rng=rng_n)
            psc[s] = wc.score_fills(bars[s], pe)
            pct[s] = wc.control_returns(bars[s], pe, rng=rng_n)
        r = pooled(psc, pct, rng_n, BOOT_SYN)
        rej_pair += r["lb_paired"] > 0
        x = np.concatenate([psc[s]["net_bps"].to_numpy() for s in SYMS])
        c = np.concatenate([pct[s] for s in SYMS])
        ok = np.isfinite(c)
        ri = wc.cluster_bootstrap(x[ok], c[ok], np.arange(ok.sum()), n_boot=BOOT_SYN, rng=rng_n)
        rej_iid += ri["lb_paired"] > 0
    res["placebo"] = {"draws": PLACEBO_DRAWS, "paired_rejection": rej_pair / PLACEBO_DRAWS,
                      "paired_rejection_iid_mutant": rej_iid / PLACEBO_DRAWS}

    n_fills = sum(res["symbols"][s]["fills"] for s in SYMS)
    checks = {
        "data_gates": all(gates[s]["ok"] for s in SYMS),
        "min_fills": n_fills >= MIN_DEV_FILLS,
        "K1_power30_joint>=0.50": syn["power30"]["joint"] >= 0.50,
        "K2_null_primary<=0.08": syn["null"]["primary"] <= 0.08,
        "K2_plant200_joint>=0.95": syn["plant_200"]["joint"] >= 0.95,
        "syn_null>=0.02": syn["null"]["primary"] >= 0.02,
        "syn_sign_mutant<=0.01": syn["sign_mutant_-200"]["joint"] <= 0.01,
        "syn_iid_mutant_over_rejects": syn["null_icc0.20_iid_mutant"]["primary"]
        > syn["null_icc0.20_clustered"]["primary"],
        "S1_plant": res["plant"]["ok"],
        "S2_placebo_in_[0.02,0.08]": 0.02 <= res["placebo"]["paired_rejection"] <= 0.08,
        "controls_complete": all(res["symbols"][s]["controls_missing"] == 0 for s in SYMS),
    }
    res["stage_a"] = {"checks": checks, "result": "VALID" if all(checks.values()) else "INVALID"}
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({"k": k, "stage_a": res["stage_a"], "dev": res["dev_result"]}, indent=1))


if __name__ == "__main__":
    main()
