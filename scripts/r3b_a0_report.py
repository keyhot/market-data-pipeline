"""R3-b Stage A0 runner: Kraken PF 1m dev-window fetch, trigger counts, synthetic power.

Reads NO outcome: only dev-window bars (C1 blocks dropped at parse), each fill
decided from bars <= t. No verdict-window candle is fetched. Rules: vault
``Docs/r3b-preregistration.md``. Writes scripts/results/r3b_stage_a0.json.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model import wick_catch as wc  # noqa: E402

CACHE = ROOT / ".cache" / "r3b"
OUT = ROOT / "scripts" / "results" / "r3b_stage_a0.json"
SYMS = ("PF_XBTUSD", "PF_ETHUSD")
DEV = (pd.Timestamp("2022-03-23", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC"))
DEV_DAYS = ("2022-03-24", "2024-12-31")          # first full day after the 10:03 launch
VERDICT_DAYS = ("2025-01-01", "2026-09-27")
K_GRID = [round(2.0 + 0.25 * i, 2) for i in range(41)]
EFFECTS = [0, 10, 20, 30, 40, 50, 60, 80, 100]
SEEDS, BOOT = 300, 999


def kraken_getter(sym):
    s = requests.Session()

    def get(frm, to):
        for attempt in range(5):
            try:
                r = s.get(f"https://futures.kraken.com/api/charts/v1/trade/{sym}/1m",
                          params={"from": frm, "to": to}, timeout=30)
                if r.status_code == 200:
                    time.sleep(0.25)
                    return r.json().get("candles", [])
            except requests.RequestException:
                pass
            time.sleep(2 ** attempt)
        raise RuntimeError(f"fetch failed {sym} {frm}")
    return get


def load(sym):
    CACHE.mkdir(parents=True, exist_ok=True)
    # Local cache this script wrote itself (gitignored .cache/), never external input.
    p = CACHE / f"{sym}_dev.pkl"
    if p.exists():
        return pd.read_pickle(p)
    df = wc.fetch_range(kraken_getter(sym), *DEV)
    df.to_pickle(p)
    return df


def per_day_rate(fills, days):
    return len(fills[fills.index.normalize().strftime("%Y-%m-%d").isin(days)]) / max(len(days), 1)


def main():
    bars = {s: load(s) for s in SYMS}
    dev_days = wc._eligible_index(*DEV_DAYS).strftime("%Y-%m-%d")
    res = {"git_hash": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                      capture_output=True, text=True).stdout.strip(),
           "dev_window": [str(DEV[0]), str(DEV[1])], "symbols": {}}

    # K3 gaps, overall and per year
    for s in SYMS:
        b = bars[s]
        g = wc.gap_share(b.index, b.index.min(), DEV[1])
        yrs = {}
        for y in (2022, 2023, 2024):
            a = max(b.index.min(), pd.Timestamp(f"{y}-01-01", tz="UTC"))
            yrs[y] = wc.gap_share(b.index, a, pd.Timestamp(f"{y + 1}-01-01", tz="UTC"))["share"]
        res["symbols"][s] = {"first_bar": str(b.index.min()), "last_bar": str(b.index.max()),
                             "n_bars": int(len(b)), "gaps": g, "gap_share_by_year": yrs}

    # k selection on dev (max of the two symbols' rates <= 1/day)
    cache = {}

    def fills(s, k, touch=False):
        key = (s, k, touch)
        if key not in cache:
            cache[key] = wc.wick_fills(bars[s], k=k, tick=wc.TICKS[s], touch=touch)
        return cache[key]

    rate = lambda k: max(per_day_rate(fills(s, k), dev_days) for s in SYMS)
    k_day = wc.choose_k(rate, K_GRID, 1.0)
    k_week = wc.choose_k(rate, K_GRID, 1.0 / 7)
    res["k_rates"] = {str(k): {s: per_day_rate(fills(s, k), dev_days) for s in SYMS} for k in K_GRID}
    res["k_day"], res["k_week"] = k_day, k_week
    n_verdict_days = wc.eligible_days(*VERDICT_DAYS)
    res["eligible_days"] = {"dev": len(dev_days), "verdict": n_verdict_days}

    for label, k in (("day", k_day), ("week", k_week)):
        if k is None:
            continue
        f = {s: fills(s, k) for s in SYMS}
        tup = wc.day_tuples(f[SYMS[0]].index, f[SYMS[1]].index, *DEV_DAYS)
        per_day = tup[:, :2].sum(1).mean()
        n_gate = per_day * n_verdict_days
        info = {"k": k, "dev_fills": {s: int(len(f[s])) for s in SYMS},
                "dev_rate_per_symbol_day": {s: per_day_rate(f[s], dev_days) for s in SYMS},
                "rate_by_year": {s: {y: per_day_rate(f[s], [d for d in dev_days if d.startswith(str(y))])
                                     for y in (2022, 2023, 2024)} for s in SYMS},
                "side_share_bid": {s: float((f[s]["side"] == "bid").mean()) for s in SYMS},
                "one_tick_through_share": {s: float((f[s]["through_ticks"] < 2 - 1e-9).mean()) for s in SYMS},
                "ambiguous_bars": {s: int(f[s].attrs["ambiguous"]) for s in SYMS},
                "touch_fills": {s: int(len(fills(s, k, touch=True))) for s in SYMS},
                "cofills": int(tup[:, 2].sum()),
                "days_with_fill_share": float((tup[:, :2].sum(1) > 0).mean()),
                "max_fills_one_day": int(tup[:, :2].sum(1).max()),
                "gating_N_verdict": float(n_gate)}
        curve = {e: wc.power(e, tup, n_verdict_days, icc=0.10, n_seeds=SEEDS, n_boot=BOOT, seed=100 + e)
                 for e in EFFECTS}
        info["power_curve_icc0.10"] = {str(e): v for e, v in curve.items()}
        info["mde80_joint"] = wc.mde(EFFECTS, [curve[e]["joint"] for e in EFFECTS])
        info["mde80_primary"] = wc.mde(EFFECTS, [curve[e]["primary"] for e in EFFECTS])
        info["power30_by_icc"] = {str(i): wc.power(30, tup, n_verdict_days, icc=i, n_seeds=SEEDS,
                                                   n_boot=BOOT, seed=7) for i in (0.0, 0.05, 0.10, 0.20)}
        r24 = sum(per_day_rate(f[s], [d for d in dev_days if d.startswith("2024")]) for s in SYMS)
        info["sens_2024_rate"] = {"per_day": r24, "N": r24 * n_verdict_days,
                                  "power30": wc.power(30, tup, n_verdict_days, icc=0.10, per_day=r24,
                                                      n_seeds=SEEDS, n_boot=BOOT, seed=8)}
        if label == "day":
            info["self_checks"] = {
                "null_icc0.10": wc.power(0, tup, n_verdict_days, icc=0.10, n_seeds=SEEDS, n_boot=BOOT, seed=11),
                "plant_200": wc.power(200, tup, n_verdict_days, icc=0.10, n_seeds=SEEDS, n_boot=BOOT, seed=12),
                "sign_mutant_-200": wc.power(-200, tup, n_verdict_days, icc=0.10, n_seeds=SEEDS, n_boot=BOOT, seed=13),
                "null_icc0.20_clustered": wc.power(0, tup, n_verdict_days, icc=0.20, n_seeds=SEEDS, n_boot=BOOT, seed=14),
                "null_icc0.20_iid_mutant": wc.power(0, tup, n_verdict_days, icc=0.20, n_seeds=SEEDS, n_boot=BOOT,
                                                    seed=14, iid_bootstrap=True)}
        res[f"trigger_{label}"] = info

    # verdict mapping
    d = res.get("trigger_day")
    k3 = any(res["symbols"][s]["gaps"]["share"] > 0.02 for s in SYMS)
    if d is None:
        res["verdict"] = {"KILLED": True, "why": "no k on grid gives <= 1 fill/day"}
    else:
        sc = d["self_checks"]
        k1 = d["power30_by_icc"]["0.1"]["joint"] < 0.50
        k2 = sc["null_icc0.10"]["primary"] > 0.08 or sc["plant_200"]["joint"] < 0.95
        void = (sc["null_icc0.10"]["primary"] < 0.02 or sc["sign_mutant_-200"]["joint"] > 0.01
                or sc["null_icc0.20_iid_mutant"]["primary"] <= sc["null_icc0.20_clustered"]["primary"])
        res["verdict"] = {"K1": k1, "K2": k2, "K3": k3, "VOID": void,
                          "result": "VOID" if void else ("KILLED" if (k1 or k2 or k3) else "PROCEED")}
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res["verdict"]))


if __name__ == "__main__":
    main()
