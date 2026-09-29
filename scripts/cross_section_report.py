"""E13 cross-sectional perps ranking — stage A (vault
`Docs/cross-sectional-ranking-experiment.md`).

    poetry run python scripts/cross_section_report.py --fetch      # klines, then funding
    poetry run python scripts/cross_section_report.py --counts     # universe size per year
    poetry run python scripts/cross_section_report.py --control    # leak control + null, 2020-22 only

Everything downloaded lands in gitignored `data/archive/binance_um/` (zips)
and `data/archive/cross_section/` (panels). Nothing after 2025-12-31 is
fetched; stage A reads no return after 2022-12-31 (the test years are
2023-2025 and belong to stage B).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model.backtest import COST_PROFILES  # noqa: E402
from model.cross_section import (  # noqa: E402
    block_bootstrap_ci, daily_features, decile_weights, eligibility,
    forward_funding, forward_returns, funding_by_day, is_candidate,
    portfolio_pnl, random_null_weights, slippage_depth, slippage_hourly,
    slippage_per_side, walk_forward_scores,
)

ROOT = Path(__file__).resolve().parents[1]
ZIPS = ROOT / "data/archive/binance_um"
PANELS = ROOT / "data/archive/cross_section"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION = "https://data.binance.vision/"
LAST_MONTH = "2025-12"          # 2026 is never fetched
DEV_END = pd.Timestamp("2022-12-31", tz="UTC")   # stage A reads no return after this
FIELDS = ("open_time", "open", "high", "low", "close", "volume", "close_time",
          "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume",
          "ignore")
PANEL_FIELDS = ("close", "high", "low", "volume", "quote_volume")
TOP_N, WARMUP, LIQ_WINDOW, MIN_QV = 50, 30, 30, 5e6   # pre-registered


# ------------------------------------------------ Amendment A1 (frozen) --
# Committed 2026-09-28 BEFORE any 2023-2025 return was read (vault note,
# "Amendment A1"). The verdict is judged on PRIMARY only; every other row is
# information and cannot flip it.
A1 = {
    "primary": "daily Abdi-Ranaldo, 30d trailing, half-spread, floor 1 bp (unchanged)",
    "fee_per_side": 0.0005,                 # P1 perp_taker; = venue 10 bps taker RT
    "sens_1h_ar": {"window_bars": 720, "min_bars": 360, "floor": 0.0001,
                   "fallback": "primary (daily AR) where no hourly value"},
    "sens_depth": {"sample": "every Wednesday from 2023-01-04, eligible names only",
                   "order_usd": 100_000,    # per name per trade, fixed before reading
                   "band": 0.01,            # linear book within +-1% of mid
                   "half_spread": 0.0001,   # archive has no touch; 1 bp floor
                   "asof": "backward only", "fallback": "primary (daily AR)"},
    "venue_rows": {"taker_10bps_rt": 0.0005, "maker_4bps_rt": 0.0002},
    "oi": "partial coverage -> neutral rank per section 2, never dropped",
    "last_day": "2025-12-31 has no d+1 row (2026 never fetched): return 0 by rule",
}


def _list(prefix: str) -> tuple[list[str], list[str]]:
    """(sub-prefixes, keys) under an S3 prefix, following pagination."""
    dirs, keys, marker = [], [], ""
    while True:
        for attempt in range(5):
            try:
                text = requests.get(S3, params={"delimiter": "/", "prefix": prefix,
                                                "marker": marker}, timeout=60).text
                break
            except requests.RequestException:
                if attempt == 4:
                    raise
        dirs += re.findall(r"<Prefix>" + re.escape(prefix) + r"([^<]+)/</Prefix>", text)
        keys += re.findall(r"<Key>" + re.escape(prefix) + r"([^<]+)</Key>", text)
        if "<IsTruncated>true" not in text:
            return dirs, keys
        nxt = re.findall(r"<NextMarker>([^<]+)", text)
        marker = nxt[-1] if nxt else prefix + (dirs[-1] + "/" if dirs else keys[-1])


def _get(path: str) -> str:
    local = ZIPS / path.removeprefix("data/futures/um/")
    if not local.exists():
        for _ in range(3):
            try:
                r = requests.get(VISION + path, timeout=60)
            except requests.RequestException:
                continue
            if r.status_code == 404:
                return "missing"
            if r.ok:
                local.parent.mkdir(parents=True, exist_ok=True)
                local.with_suffix(".part").write_bytes(r.content)
                local.with_suffix(".part").rename(local)
                break
        else:
            return "failed"
    return str(local)


def _read(local: str) -> str:
    with zipfile.ZipFile(local) as z:
        return z.read(z.namelist()[0]).decode()


def _month_files(dataset_prefix: str, symbol: str, suffix: str) -> list[str]:
    _, keys = _list(f"{dataset_prefix}/{symbol}/{suffix}")
    months = [k for k in keys if k.endswith(".zip")
              and re.search(r"(\d{4}-\d{2})\.zip$", k).group(1) <= LAST_MONTH]
    return [f"{dataset_prefix}/{symbol}/{suffix}{k}" for k in sorted(months)]


def _parse_klines(text: str) -> pd.DataFrame:
    head = text.split("\n", 1)[0]
    frame = pd.read_csv(io.StringIO(text), header=0 if head.startswith("open_time") else None,
                        names=None if head.startswith("open_time") else FIELDS)
    frame.columns = FIELDS
    frame.index = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    return frame[list(PANEL_FIELDS)].astype(float)


def fetch() -> None:
    PANELS.mkdir(parents=True, exist_ok=True)
    monthly, _ = _list("data/futures/um/monthly/klines/")
    daily, _ = _list("data/futures/um/daily/klines/")
    only_daily = sorted(set(daily) - set(monthly))
    firsts = {}
    for s in only_daily:          # survivorship: none may have traded before 2026
        _, keys = _list(f"data/futures/um/daily/klines/{s}/1d/")
        dates = sorted(re.findall(r"(\d{4}-\d{2}-\d{2})\.zip$", " ".join(keys)))
        firsts[s] = dates[0] if dates else None
    early = {s: d for s, d in firsts.items() if d and d < "2026-01-01"}
    print(f"  symbols: {len(monthly)} monthly, {len(only_daily)} daily-only; "
          f"daily-only listed before 2026: {early or 'none'}", flush=True)
    symbols = sorted(s for s in monthly if is_candidate(s))
    with ThreadPoolExecutor(32) as pool:
        files = [f for fs in pool.map(lambda s: _month_files(
            "data/futures/um/monthly/klines", s, "1d/"), symbols) for f in fs]
        print(f"  {len(symbols)} candidate symbols, {len(files)} monthly 1d zips", flush=True)
        locals_ = list(pool.map(_get, files))
    bad = [f for f, l in zip(files, locals_) if l in ("missing", "failed")]
    print(f"  klines missing/failed: {len(bad)} {bad[:5]}", flush=True)
    per_symbol: dict[str, list[pd.DataFrame]] = {}
    for f, l in zip(files, locals_):
        if l not in ("missing", "failed"):
            per_symbol.setdefault(f.split("/")[5], []).append(_parse_klines(_read(l)))
    days = pd.date_range("2020-01-01", "2025-12-31", freq="D", tz="UTC")
    frames = {s: pd.concat(v).sort_index() for s, v in per_symbol.items()}
    frames = {s: f[~f.index.duplicated(keep="last")] for s, f in frames.items()}
    for field in PANEL_FIELDS:
        panel = pd.DataFrame({s: f[field] for s, f in frames.items()}).reindex(days)
        panel.to_csv(PANELS / f"{field}.csv", index_label="day")
    _fetch_funding(days)


def _load(field: str) -> pd.DataFrame:
    return pd.read_csv(PANELS / f"{field}.csv", index_col="day", parse_dates=["day"])


def _universe() -> pd.DataFrame:
    return eligibility(_load("volume"), _load("quote_volume"), WARMUP, LIQ_WINDOW, TOP_N, MIN_QV)


def _fetch_funding(days: pd.DatetimeIndex) -> None:
    """Funding for every symbol ever in the universe (2020-2025)."""
    ever = [s for s, v in _universe().any().items() if v]
    with ThreadPoolExecutor(12) as pool:
        files = [f for fs in pool.map(lambda s: _month_files(
            "data/futures/um/monthly/fundingRate", s, ""), ever) for f in fs]
        locals_ = list(pool.map(_get, files))
    rows = []
    for f, l in zip(files, locals_):
        if l in ("missing", "failed"):
            continue
        t = pd.read_csv(io.StringIO(_read(l)))
        rows.append(pd.DataFrame({"symbol": f.split("/")[5],
                                  "time": pd.to_datetime(t["calc_time"], unit="ms", utc=True),
                                  "rate": t["last_funding_rate"].astype(float)}))
    events = pd.concat(rows).drop_duplicates(["symbol", "time"])
    funding_by_day(events, days).to_csv(PANELS / "funding_daily.csv", index_label="day")
    print(f"  funding: {len(ever)} symbols ever eligible, {len(files)} zips, "
          f"{len(events):,} events", flush=True)


OI_START = pd.Timestamp("2021-12-01", tz="UTC")   # metrics archive starts here
DEPTH_START = pd.Timestamp("2023-01-01", tz="UTC")  # bookDepth archive starts here


def _needed_oi_pairs(elig: pd.DataFrame) -> dict[str, list[pd.Timestamp]]:
    """(symbol -> days) whose daily metrics file is needed: every eligible
    day d >= OI_START and d - 7 (oi_change_7 reads both closes)."""
    out = {}
    e = elig.loc[OI_START:]
    for sym in e.columns[e.any()]:
        days = e.index[e[sym].to_numpy(dtype=bool)]
        need = days.union(days - pd.Timedelta(days=7))
        out[sym] = sorted(d for d in need if d >= OI_START)
    return out


def _fetch_oi() -> None:
    """OI at each needed day's close (no returns are read). Zips cached."""
    from model.archive import parse_metrics
    from model.cross_section import oi_at_close
    elig = _universe()
    pairs = _needed_oi_pairs(elig)
    jobs = [(s, d) for s, ds in pairs.items() for d in ds]
    print(f"  oi: {len(pairs)} symbols, {len(jobs):,} daily metrics files", flush=True)

    def one(job):
        s, d = job
        day = d.strftime("%Y-%m-%d")
        return _get(f"data/futures/um/daily/metrics/{s}/{s}-metrics-{day}.zip")
    with ThreadPoolExecutor(24) as pool:
        locals_ = list(pool.map(one, jobs))
    got = {}
    for (s, d), l in zip(jobs, locals_):
        if l in ("missing", "failed"):
            continue
        try:
            got.setdefault(s, []).append(parse_metrics(_read(l))["oi"])
        except Exception:   # a malformed file is a missing file
            continue
    panel = pd.DataFrame(np.nan, index=elig.index, columns=elig.columns)
    for s, parts in got.items():
        series = pd.concat(parts)
        series = series[~series.index.duplicated(keep="last")]
        days = pd.DatetimeIndex(pairs[s])
        panel.loc[days, s] = oi_at_close(series, days).to_numpy()
    panel.to_csv(PANELS / "oi_close.csv", index_label="day")
    miss = sum(l in ("missing", "failed") for l in locals_)
    print(f"  oi: {len(jobs) - miss:,} files read, {miss:,} missing/failed", flush=True)


def _fetch_depth() -> None:
    """A1 sensitivity (b): bookDepth on every Wednesday from 2023-01-04, for
    the symbols eligible that day; kept is the daily median over snapshots
    of mean(bid1, ask1) notional (USD within 1% of mid). Zips are NOT kept."""
    from model.archive import parse_depth
    elig = _universe()
    weds = pd.date_range("2023-01-04", "2025-12-31", freq="7D", tz="UTC")
    jobs = [(s, d) for d in weds for s in elig.columns[elig.loc[d].fillna(False).astype(bool)]]
    print(f"  depth: {len(weds)} Wednesdays, {len(jobs):,} files", flush=True)

    def one(job):
        s, d = job
        url = f"{VISION}data/futures/um/daily/bookDepth/{s}/{s}-bookDepth-{d:%Y-%m-%d}.zip"
        for _ in range(3):
            try:
                r = requests.get(url, timeout=120)
            except requests.RequestException:
                continue
            if r.status_code == 404:
                return np.nan
            if r.ok:
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    wide = parse_depth(z.read(z.namelist()[0]).decode())
                return float(((wide["bid1"] + wide["ask1"]) / 2).median()) if len(wide) else np.nan
        return np.nan
    with ThreadPoolExecutor(16) as pool:
        vals = list(pool.map(one, jobs))
    rows = pd.DataFrame([(d, s, v) for (s, d), v in zip(jobs, vals)], columns=["day", "symbol", "d1"])
    rows.pivot(index="day", columns="symbol", values="d1").to_csv(PANELS / "depth1.csv", index_label="day")
    print(f"  depth: {rows['d1'].notna().sum():,} of {len(rows):,} read", flush=True)


TEST_START = pd.Timestamp("2023-01-01", tz="UTC")
TEST_END = pd.Timestamp("2025-12-31", tz="UTC")
RESULTS = ROOT / "scripts/results/e13_stage_b.json"


def _fetch_hourly() -> None:
    """A1 sensitivity (a): 1h klines 2022-12..2025-12 for every name eligible
    in the test years; only the per-day hourly-AR slippage panel is kept."""
    elig = _universe()
    days = elig.index[elig.index >= TEST_START]
    names = list(elig.columns[elig.loc[TEST_START:].any()])
    months = pd.period_range("2022-12", LAST_MONTH, freq="M")
    jobs = [(s, str(m)) for s in names for m in months]
    print(f"  1h: {len(names)} names, {len(jobs):,} monthly zips", flush=True)

    def one(job):
        s, m = job
        return _get(f"data/futures/um/monthly/klines/{s}/1h/{s}-1h-{m}.zip")
    with ThreadPoolExecutor(16) as pool:
        locals_ = list(pool.map(one, jobs))
    by_sym = {}
    for (s, _), l in zip(jobs, locals_):
        if l not in ("missing", "failed"):
            by_sym.setdefault(s, []).append(_parse_klines(_read(l)))
    out = pd.DataFrame(np.nan, index=days, columns=elig.columns)
    for s, parts in by_sym.items():
        f = pd.concat(parts).sort_index()
        f = f[~f.index.duplicated(keep="last")]
        one_ = slippage_hourly(f[["high"]].rename(columns={"high": s}),
                               f[["low"]].rename(columns={"low": s}),
                               f[["close"]].rename(columns={"close": s}), days)
        out[s] = one_[s]
    out.to_csv(PANELS / "slip_1h.csv", index_label="day")
    print(f"  1h: {len(by_sym)} names with bars", flush=True)


def _stats(pnl: pd.DataFrame) -> dict:
    m, lo, hi = block_bootstrap_ci(pnl["net"])
    sd = pnl["net"].std()
    return {"net_bps": [round(m * 1e4, 1), round(lo * 1e4, 1), round(hi * 1e4, 1)],
            "gross_bps": round(pnl["gross"].mean() * 1e4, 1),
            "funding_bps": round(pnl["funding"].mean() * 1e4, 2),
            "cost_bps": round(pnl["cost"].mean() * 1e4, 1),
            "turnover": round(pnl["turnover"].mean(), 3),
            "sharpe_ann": round(float(pnl["net"].mean() / sd * np.sqrt(365)) if sd > 0 else 0.0, 2)}


def _null(w, elig, fwd, ffund, fee, slip, seeds) -> np.ndarray:
    out = []
    for seed in range(seeds):
        nw = random_null_weights(w, elig, np.random.default_rng(seed))
        p = portfolio_pnl(nw, fwd, ffund, fee, slip).loc[TEST_START:TEST_END]
        out.append(float((p["gross"] + p["funding"]).mean()))
    return np.array(out)


def score(seeds: int = 1000) -> dict:
    """Stage B: the pre-registered rank-ridge, walk-forward from 2021-01,
    reported on 2023-01..2025-12 only. Verdict on the PRIMARY cost (A1)."""
    close, high, low = _load("close"), _load("high"), _load("low")
    elig = _universe()
    fund = _load("funding_daily").reindex(index=close.index, columns=close.columns).fillna(0.0)
    oi_path = PANELS / "oi_close.csv"
    oi = (_load("oi_close").reindex(index=close.index, columns=close.columns)
          if oi_path.exists() else None)
    fwd, ffund = forward_returns(close), forward_funding(fund)
    slip = slippage_per_side(high, low, close)
    fee = A1["fee_per_side"]
    feats = daily_features(close, fund, oi)
    start = pd.Timestamp("2021-01-01", tz="UTC")
    test = slice(TEST_START, TEST_END)
    scores = walk_forward_scores(feats, fwd, elig, start, TEST_END)
    w = decile_weights(scores)
    et = elig.loc[test].fillna(False).astype(bool)
    oi_cov = float(feats["oi_change_7"].where(et).loc[test].notna().sum().sum() / et.sum().sum())

    rows = {}
    rows["primary (daily AR)"] = portfolio_pnl(w, fwd, ffund, fee, slip).loc[test]
    s1h = PANELS / "slip_1h.csv"
    if s1h.exists():
        h = _load("slip_1h").reindex(index=close.index, columns=close.columns)
        rows["(a) 1h AR"] = portfolio_pnl(w, fwd, ffund, fee, h.combine_first(slip)).loc[test]
    dpath = PANELS / "depth1.csv"
    if dpath.exists():
        d1 = _load("depth1")
        dep = slippage_depth(d1, close.index, close.columns, A1["sens_depth"]["order_usd"],
                             A1["sens_depth"]["band"], A1["sens_depth"]["half_spread"])
        rows["(b) bookDepth $100k"] = portfolio_pnl(w, fwd, ffund, fee, dep.combine_first(slip)).loc[test]
    rows["venue taker 10 bps RT (= primary fee)"] = rows["primary (daily AR)"]
    rows["venue maker 4 bps RT"] = portfolio_pnl(w, fwd, ffund, A1["venue_rows"]["maker_4bps_rt"], slip).loc[test]
    rows["flat 1 bp slippage"] = portfolio_pnl(w, fwd, ffund, fee, 0.0001).loc[test]
    res = {"days": len(rows["primary (daily AR)"]), "oi_coverage_test": round(oi_cov, 3),
           "median_book_per_side": float((w > 0).sum(axis=1).loc[test].median()),
           "median_slippage_bps": {"primary": round(float(slip.where(elig).loc[test].stack().median() * 1e4), 1)},
           "rows": {k: _stats(v) for k, v in rows.items()}}
    if s1h.exists():
        res["median_slippage_bps"]["1h"] = round(float(h.where(elig).loc[test].stack().median() * 1e4), 1)
    if dpath.exists():
        res["median_slippage_bps"]["depth"] = round(float(dep.where(elig).loc[test].stack().median() * 1e4), 1)

    prim = rows["primary (daily AR)"]
    gf = float((prim["gross"] + prim["funding"]).mean())
    null = _null(w.loc[test], elig.loc[test], fwd, ffund, fee, slip, seeds)
    res["null_gross_plus_funding_bps"] = {
        "seeds": seeds, "mean": round(null.mean() * 1e4, 2),
        "p2.5": round(np.quantile(null, 0.025) * 1e4, 2),
        "p97.5": round(np.quantile(null, 0.975) * 1e4, 2)}
    res["model_gross_plus_funding_bps"] = round(gf * 1e4, 2)
    res["model_null_percentile"] = round(float((null < gf).mean() * 100), 1)
    c1 = res["rows"]["primary (daily AR)"]["net_bps"][1] > 0
    c2 = gf > np.quantile(null, 0.975)
    res["pass_rule"] = {"1_net_ci_above_0": bool(c1), "2_beats_null_p97.5": bool(c2),
                        "verdict": "PASS" if (c1 and c2) else "FALSIFIED"}
    res["null_sanity_pass"] = bool(abs(res["null_gross_plus_funding_bps"]["mean"]) < 10)

    res["per_year_net_bps"] = {str(y): _stats(g)["net_bps"] for y, g in prim.groupby(prim.index.year)}
    for leg, wl in (("long", w.clip(lower=0)), ("short", w.clip(upper=0))):
        p = portfolio_pnl(wl, fwd, ffund, fee, slip).loc[test]
        res[f"{leg}_leg_bps"] = {"gross": round(p["gross"].mean() * 1e4, 1),
                                 "funding": round(p["funding"].mean() * 1e4, 2),
                                 "cost": round(p["cost"].mean() * 1e4, 1)}
    sc, fr = scores.loc[test].where(et), fwd.loc[test].where(et)
    ic = sc.rank(axis=1).corrwith(fr.rank(axis=1), axis=1).dropna()
    m, lo, hi = block_bootstrap_ci(ic)
    res["rank_ic"] = [round(m, 4), round(lo, 4), round(hi, 4)]
    res["coefficients_last_fit"] = _last_beta(feats, fwd, elig)

    diag = walk_forward_scores({"ret_1": feats["ret_1"]}, fwd, elig, start, TEST_END)
    res["diag_ret_1_alone"] = _stats(portfolio_pnl(decile_weights(diag), fwd, ffund, fee, slip).loc[test])
    leaked = walk_forward_scores(dict(feats, ret_30=fwd), fwd, elig, start, TEST_END)
    res["leaked_control_test_years"] = _stats(portfolio_pnl(decile_weights(leaked), fwd, ffund, fee, slip).loc[test])
    res["dev_control_2021_22"] = control()
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res, indent=1))
    return res


def _last_beta(feats, fwd, elig) -> dict:
    """The rank-ridge fitted on everything before the last test month."""
    from model.cross_section import cs_rank, fit_ridge
    refit = pd.Timestamp("2025-12-01", tz="UTC")
    ranked = {k: cs_rank(v, elig).loc[:refit - pd.Timedelta(days=1)].stack() for k, v in feats.items()}
    X = pd.concat(ranked, axis=1)
    y = cs_rank(fwd, elig).stack().reindex(X.index)
    beta = fit_ridge(X.to_numpy(), y.to_numpy())
    return {k: round(float(b), 4) for k, b in zip(X.columns, beta)}


def counts() -> dict:
    """Universe size per year from VOLUME ONLY (no return is read)."""
    vol, qv = _load("volume"), _load("quote_volume")
    out = {}
    for floor in (0.0, 5e6, 20e6):
        e = eligibility(vol, qv, WARMUP, LIQ_WINDOW, TOP_N, min_quote_volume=floor)
        n = e.sum(axis=1)
        out[f"floor_{floor:.0e}"] = {
            str(y): {"min": int(g.min()), "median": float(g.median()), "max": int(g.max())}
            for y, g in n.groupby(n.index.year)}
    e = eligibility(vol, qv, WARMUP, LIQ_WINDOW, TOP_N)
    ever = e.any()
    last_traded = (vol > 0)[::-1].idxmax()
    out["ever_eligible"] = int(ever.sum())
    out["ever_eligible_now_delisted"] = int(
        (ever & (last_traded < pd.Timestamp("2025-12-01", tz="UTC"))).sum())
    out["candidate_symbols"] = int(vol.shape[1])
    print(json.dumps(out, indent=1))
    return out


def control() -> dict:
    """Leak control + random null on the DEV years only (scored 2021-2022)."""
    close, high, low = _load("close"), _load("high"), _load("low")
    dev = close.index <= DEV_END + pd.Timedelta(days=1)   # d+1 close of the last dev day
    close, high, low = close[dev], high[dev], low[dev]
    elig = _universe()[dev]
    fund = _load("funding_daily").reindex(index=close.index, columns=close.columns).fillna(0.0)
    fwd, ffund = forward_returns(close), forward_funding(fund)
    slip = slippage_per_side(high, low, close)
    fee = COST_PROFILES["perp_taker"].fee_per_side
    start, end = pd.Timestamp("2021-01-01", tz="UTC"), DEV_END
    feats = daily_features(close, fund)
    leaked = dict(feats, ret_30=fwd)
    scores = walk_forward_scores(leaked, fwd, elig, start, end, min_train_days=180)
    w = decile_weights(scores)
    pnl = portfolio_pnl(w, fwd, ffund, fee, slip).loc[start:end]
    res = {"days": len(pnl), "median_book_per_side": float((w > 0).sum(axis=1).loc[start:end].median()),
           "median_slippage_bps": float(slip.where(elig).stack().median() * 1e4),
           "mean_turnover": float(pnl["turnover"].mean())}
    for col in ("gross", "net"):
        m, lo, hi = block_bootstrap_ci(pnl[col])
        res[f"leaked_{col}_bps"] = [round(m * 1e4, 1), round(lo * 1e4, 1), round(hi * 1e4, 1)]
    null = []
    for seed in range(200):
        nw = random_null_weights(w, elig, np.random.default_rng(seed))
        npnl = portfolio_pnl(nw, fwd, ffund, fee, slip).loc[start:end]
        null.append(float((npnl["gross"] + npnl["funding"]).mean()))
    res["null_gross_plus_funding_bps"] = {
        "mean": round(np.mean(null) * 1e4, 2),
        "p2.5": round(np.quantile(null, 0.025) * 1e4, 2),
        "p97.5": round(np.quantile(null, 0.975) * 1e4, 2)}
    lo_leak = res["leaked_net_bps"][1]
    res["control_pass"] = bool(lo_leak > 100 and lo_leak > res["null_gross_plus_funding_bps"]["p97.5"]
                               and abs(res["null_gross_plus_funding_bps"]["mean"]) < 10)
    print(json.dumps(res, indent=1))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--funding", action="store_true", help="funding only (klines cached)")
    ap.add_argument("--counts", action="store_true")
    ap.add_argument("--control", action="store_true")
    ap.add_argument("--hourly", action="store_true", help="A1 (a): 1h-AR slippage panel")
    ap.add_argument("--score", action="store_true", help="stage B: test years 2023-2025")
    ap.add_argument("--seeds", type=int, default=1000)
    ap.add_argument("--oi", action="store_true", help="stage B: OI panel (metrics)")
    ap.add_argument("--depth", action="store_true", help="A1 (b): weekly bookDepth sample")
    a = ap.parse_args()
    if a.oi:
        _fetch_oi()
    if a.depth:
        _fetch_depth()
    if a.hourly:
        _fetch_hourly()
    if a.score:
        score(a.seeds)
    if a.fetch:
        fetch()
    if a.funding:
        _fetch_funding(pd.date_range("2020-01-01", "2025-12-31", freq="D", tz="UTC"))
    if a.counts:
        counts()
    if a.control:
        control()


if __name__ == "__main__":
    main()
