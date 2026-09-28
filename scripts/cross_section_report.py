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
    portfolio_pnl, random_null_weights, slippage_per_side, walk_forward_scores,
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
    ap.add_argument("--oi", action="store_true", help="stage B: OI panel (metrics)")
    ap.add_argument("--depth", action="store_true", help="A1 (b): weekly bookDepth sample")
    a = ap.parse_args()
    if a.oi:
        _fetch_oi()
    if a.depth:
        _fetch_depth()
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
