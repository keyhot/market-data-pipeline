"""R3-a Stage A0: counts, Kraken listing lag, synthetic power. Reads NO returns.

Inputs: data/archive/r3a_meta/ (from scripts/r3a_fetch_metadata.py) -- S3 file
names, Binance exchangeInfo, Kraken instruments + tickers. Kill rules are in
vault Docs/r3a-preregistration.md, written before this ran.

Output: scripts/results/r3a_stage_a0.json
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model import listing_short as ls  # noqa: E402
from scripts.r3a_fetch_metadata import s3_list  # noqa: E402

META = ROOT / "data" / "archive" / "r3a_meta"
OUT = ROOT / "scripts" / "results" / "r3a_stage_a0.json"
EFFECTS = [0.0, 0.025, 0.05, 0.075, 0.10, 0.15, 0.20, 0.30, 0.40]
SIGMAS = [0.30, 0.45, 0.60]
SE_INFL = [1.0, 1.3, 1.6]
GATE = {"effect": 0.10, "sigma": 0.45, "se_inflation": 1.3, "min_joint_power": 0.50}
N_SEEDS, N_BOOT = 300, 999


def daily_first_names(symbols: list[str]) -> dict[str, str]:
    cache = META / "daily_first.json"
    out = json.loads(cache.read_text()) if cache.exists() else {}
    for sym in symbols:
        if sym in out:
            continue
        keys = s3_list(f"data/futures/um/daily/klines/{sym}/1d/")
        dates = sorted(d for k in keys for d in re.findall(r"-1d-(\d{4}-\d{2}-\d{2})\.zip$", k))
        if dates:
            out[sym] = dates[0]
    cache.write_text(json.dumps(out, indent=0, sort_keys=True))
    return out


def placebo_per_month(ev: pd.DataFrame, months: pd.DatetimeIndex) -> float:
    """0.2 x tokens aged 6-18 months at each month start (delistings ignored -> upper-ish)."""
    d = ev["listing_date"]
    counts = [((d <= m - pd.DateOffset(months=6)) & (d > m - pd.DateOffset(months=18))).sum()
              for m in months]
    return max(3.0, float(np.median(counts)) * 0.2)


def window_power(n: int, n_months: int, ppm: float) -> dict:
    res: dict = {"n_events": n, "n_months": n_months, "placebo_per_month": ppm,
                 "curves": {}, "se_inflation_at_gate": {}}
    seed = 0
    for sigma in SIGMAS:
        rows = []
        for e in EFFECTS:
            seed += 1
            p = ls.power(e, n, n_months, ppm, sigma, GATE["se_inflation"], N_SEEDS, N_BOOT, seed)
            rows.append({"effect": e, **p})
        res["curves"][f"sigma_{sigma:.2f}"] = {
            "rows": rows,
            "mde80_joint": ls.mde(EFFECTS, [r["joint"] for r in rows]),
            "mde80_primary": ls.mde(EFFECTS, [r["primary"] for r in rows]),
        }
    for se in SE_INFL:
        seed += 1
        res["se_inflation_at_gate"][f"{se:.1f}"] = ls.power(
            GATE["effect"], n, n_months, ppm, GATE["sigma"], se, N_SEEDS, N_BOOT, seed)
    gate_rows = res["curves"][f"sigma_{GATE['sigma']:.2f}"]["rows"]
    at = {r["effect"]: r for r in gate_rows}
    res["gate_joint_power"] = at[GATE["effect"]]["joint"]
    res["gate_primary_power"] = at[GATE["effect"]]["primary"]
    # self-checks
    sign = ls.power(-0.40, n, n_months, ppm, GATE["sigma"], GATE["se_inflation"], N_SEEDS, N_BOOT, 901)
    iid = ls.power(0.0, n, n_months, ppm, GATE["sigma"], 1.6, N_SEEDS, N_BOOT, 902, iid_bootstrap=True)
    clu = ls.power(0.0, n, n_months, ppm, GATE["sigma"], 1.6, N_SEEDS, N_BOOT, 902)
    res["self_checks"] = {
        "plant40_joint": at[0.40]["joint"], "plant40_ok": at[0.40]["joint"] >= 0.95,
        "null_primary_size": at[0.0]["primary"],
        "null_size_ok": 0.02 <= at[0.0]["primary"] <= 0.08,
        "sign_mutant_joint": sign["joint"], "sign_mutant_ok": sign["joint"] <= 0.01,
        "iid_mutant_size_se1.6": iid["primary"], "clustered_size_se1.6": clu["primary"],
        "iid_mutant_ok": iid["primary"] > clu["primary"],
    }
    sc = res["self_checks"]
    res["void"] = not all(sc[k] for k in sc if k.endswith("_ok"))
    res["k1_fired"] = res["gate_joint_power"] < GATE["min_joint_power"]
    return res


def main() -> int:
    first_month = json.loads((META / "s3_first_month.json").read_text())
    info = json.loads((META / "binance_exchangeinfo.json").read_text())["symbols"]
    live = {x["symbol"] for x in info}
    s3_only_recent = [s for s, m in first_month.items()
                      if m and s not in live and m >= "2024-01" and not ls.excluded_by_name(s)]
    daily_first = daily_first_names(sorted(s3_only_recent))
    ev = ls.build_events(first_month, info, daily_first)
    clean = ev[~ev["censored"] & ~ev["rename"]]

    counts = {
        "s3_fundingrate_dirs": len(first_month),
        "exchangeinfo_symbols": len(info),
        "tokens_total": int(len(ev)),
        "censored": int(ev["censored"].sum()),
        "renames": sorted(ev.loc[ev["rename"], "token"]),
        "by_window": clean["window"].value_counts(dropna=False).to_dict(),
        "by_year": clean["listing_date"].dt.year.value_counts().sort_index().to_dict(),
        "precision": clean["precision"].value_counts().to_dict(),
    }
    counts["by_window"] = {str(k): int(v) for k, v in counts["by_window"].items()}
    counts["by_year"] = {str(k): int(v) for k, v in counts["by_year"].items()}

    # K2: Kraken PF opening dates vs Binance day 0, 2024-25 listings
    kinst = json.loads((META / "kraken_instruments.json").read_text())["instruments"]
    kopen = {}
    for x in kinst:
        tok = ls.kraken_token(x["symbol"])
        if tok and x.get("openingDate"):
            kopen[tok] = pd.Timestamp(x["openingDate"])
    k2_ev = clean[(clean["listing_date"] >= "2024-01-01") & (clean["listing_date"] <= "2025-12-31")]
    k2 = ls.kraken_open_share(k2_ev, kopen)
    lags = np.array(k2.pop("lags_days"))
    k2["lag_days_quantiles"] = ({q: float(np.quantile(lags, q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)}
                                if len(lags) else {})
    k2["matched_lag_le_1d"] = int((lags < 2).sum())
    k2["matched_kraken_before_binance"] = int((lags < 0).sum())
    k2["k2_fired"] = not (k2["share"] >= 0.5)
    k2b_ev = clean[clean["window"] == "b"]
    k2b = ls.kraken_open_share(k2b_ev, kopen)
    k2b.pop("lags_days")

    # spreads on the matched alt PF contracts (metadata)
    tick = json.loads((META / "kraken_tickers.json").read_text())["tickers"]
    alt = set(clean["token"])
    spreads = []
    for t in tick:
        tok = ls.kraken_token(t.get("symbol", "").upper())
        b, a = t.get("bid"), t.get("ask")
        if tok in alt and b and a and a > b > 0:
            spreads.append((a - b) / ((a + b) / 2) * 1e4)
    spreads = np.array(spreads)
    cost = {"taker_bps_per_side": 5.0, "fees_round_trip_bps": 10.0,
            "n_alt_pf_tickers": int(len(spreads)),
            "spread_bps_median": float(np.median(spreads)) if len(spreads) else None,
            "spread_bps_p90": float(np.quantile(spreads, 0.9)) if len(spreads) else None,
            "note": "round trip = fees + 1 full spread (2 x half); funding not read in A0"}

    windows = {}
    for w, (lo, hi) in {"a": ls.WINDOW_A, "b": ls.WINDOW_B}.items():
        months = pd.date_range(lo.replace(day=1), hi, freq="MS")
        n = counts["by_window"].get(w, 0)
        windows[w] = window_power(n, len(months), placebo_per_month(ev, months))

    k3 = {"tests": "tests/unit/test_listing_short.py::test_event_rows_*, ::test_mutant_without_filter_raises",
          "k3_fired": False}
    verdict = {}
    for w in ("a", "b"):
        r = windows[w]
        # pre-reg: K2/K3 kill both windows regardless; VOID is about the K1 harness only
        r["k1_status"] = "VOID" if r["void"] else ("FIRED" if r["k1_fired"] else "PASS")
        if k2["k2_fired"] or k3["k3_fired"]:
            verdict[w] = "KILLED"
        elif r["void"]:
            verdict[w] = "VOID"
        else:
            verdict[w] = "KILLED" if r["k1_fired"] else "PROCEED"
    # robustness of K2: even if EVERY Kraken PF opened 2024-25 were a day-1 match
    k_open_24_25 = sum(1 for ts in kopen.values()
                       if pd.Timestamp("2024-01-01", tz="UTC") <= ts < pd.Timestamp("2026-01-01", tz="UTC"))
    k2["upper_bound_share_if_all_kraken_2024_25_openings_were_day1"] = (
        k_open_24_25 / k2["n_denominator"] if k2["n_denominator"] else None)
    k2["kraken_pf_openings_2024_25"] = k_open_24_25
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True, cwd=ROOT).stdout.strip()
    out = {"experiment": "R3-a stage A0", "git_hash": git, "gate": GATE,
           "n_seeds": N_SEEDS, "n_boot": N_BOOT, "counts": counts,
           "k2_tradability_2024_25": k2, "k2_info_window_b": k2b, "cost": cost,
           "windows": windows, "k3": k3, "verdict_by_window": verdict,
           "reads_returns": False}
    OUT.write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({"counts": counts["by_window"], "k2": k2["share"], "verdict": verdict}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
