"""E13 review diagnostics (vault `Docs/code-review-e13.md`).

READ-ONLY. Rebuilds the stage B book from the cached panels exactly as
`scripts/cross_section_report.py --score` does, ASSERTS that it reproduces
`scripts/results/e13_stage_b.json` (gross, funding, cost, turnover, rank IC,
last-fit betas), and only then answers the review's questions:

  Q1  is the +0.106 rank IC monetisable, or a skew / market-direction artifact?
  Q2  funding sign and timing, recomputed per held name-day from the RAW
      archive zips by a second code path (not `funding_by_day`)
  Q3  the Abdi-Ranaldo slippage: negatives floored after the window mean (as
      coded) vs per day, BTC / ETH / eligible median, per year, vs bookDepth
  Q4  dead names in the book, drift turnover, the null's match, block length

Holdout: no row on or after 2026-01-01 is read (asserted on every panel and on
every raw funding event). 2025-12-31's return is a rule-imposed 0, so every
return statistic stops at decision day 2025-12-30 (the reproduction block
alone keeps it, to match the report). Dev (2021-22) and test (2023-25) are
reported separately. Nothing is fetched; nothing is written except `--out`.

    OMP_NUM_THREADS=2 poetry run python scripts/e13_diagnostics.py --out /tmp/e13_diag.json
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import cross_section_report as R  # noqa: E402  (main() is guarded)
from model.cross_section import (  # noqa: E402
    block_bootstrap_ci, cs_rank, daily_features, decile_weights, eligibility, forward_funding,
    forward_returns, funding_by_day, portfolio_pnl, random_null_weights,
    slippage_depth, slippage_per_side, walk_forward_scores,
)

BPS = 1e4
HOLDOUT = pd.Timestamp("2026-01-01", tz="UTC")
PERIODS = {"dev_2021_22": ("2021-01-01", "2022-12-31"),
           "test_2023_25": ("2023-01-01", "2025-12-30")}   # 12-31: return 0 by rule
FLOOR = 1e-4
SLACK = pd.Timedelta(seconds=60)


# ------------------------------------------------------------------ helpers --

def vals(df: pd.DataFrame) -> np.ndarray:
    a = df.to_numpy(dtype=float).ravel()
    return a[~np.isnan(a)]


def bp(x, nd: int = 1):
    x = float(x)
    return None if np.isnan(x) else round(x * BPS, nd)


def ci(x, block: int = 10, scale: float = BPS, nd: int = 1) -> list:
    m, lo, hi = block_bootstrap_ci(np.asarray(x, dtype=float), block=block)
    return [round(m * scale, nd), round(lo * scale, nd), round(hi * scale, nd)]


def span(index: pd.DatetimeIndex, period: str) -> pd.DatetimeIndex:
    a, b = (pd.Timestamp(t, tz="UTC") for t in PERIODS[period])
    return index[(index >= a) & (index <= b)]


def buckets(score: pd.DataFrame, q: int) -> pd.DataFrame:
    """1..q per day by score rank among scored names (integer arithmetic)."""
    rank = score.rank(axis=1)
    return np.floor((rank - 1).mul(q).div(score.count(axis=1), axis=0)) + 1


def daily_ic(a: pd.DataFrame, r: pd.DataFrame, spearman: bool) -> pd.Series:
    a, r = a.where(r.notna()), r.where(a.notna())
    if spearman:
        a, r = a.rank(axis=1), r.rank(axis=1)
    return a.corrwith(r, axis=1).dropna()


# ------------------------------------------------------------------ build --

def build() -> dict:
    close, high, low, vol = (R._load(f) for f in ("close", "high", "low", "volume"))
    fund_raw = R._load("funding_daily")
    for name, p in (("close", close), ("high", high), ("low", low),
                    ("volume", vol), ("funding_daily", fund_raw)):
        assert p.index.max() < HOLDOUT, f"{name} panel reaches the 2026 holdout"
    elig = R._universe()
    fund = fund_raw.reindex(index=close.index, columns=close.columns).fillna(0.0)
    oi = R._load("oi_close").reindex(index=close.index, columns=close.columns)
    fwd, ffund = forward_returns(close), forward_funding(fund)
    slip = slippage_per_side(high, low, close)
    feats = daily_features(close, fund, oi)
    start = pd.Timestamp("2021-01-01", tz="UTC")
    scores = walk_forward_scores(feats, fwd, elig, start, R.TEST_END)
    w = decile_weights(scores)
    return dict(close=close, high=high, low=low, vol=vol, elig=elig, fund=fund,
                fwd=fwd, ffund=ffund, slip=slip, feats=feats, scores=scores, w=w,
                start=start, fee=R.A1["fee_per_side"])


def reproduce(c: dict) -> dict:
    """Stop unless this rebuild IS the stage B book."""
    ref = json.loads(R.RESULTS.read_text())
    test = slice(R.TEST_START, R.TEST_END)
    p = portfolio_pnl(c["w"], c["fwd"], c["ffund"], c["fee"], c["slip"]).loc[test]
    got = {"gross_bps": round(p["gross"].mean() * BPS, 1),
           "funding_bps": round(p["funding"].mean() * BPS, 2),
           "cost_bps": round(p["cost"].mean() * BPS, 1),
           "turnover": round(p["turnover"].mean(), 3)}
    want = {k: ref["rows"]["primary (daily AR)"][k] for k in got}
    et = c["elig"].loc[test].fillna(False).astype(bool)
    sc, fr = c["scores"].loc[test].where(et), c["fwd"].loc[test].where(et)
    ic = sc.rank(axis=1).corrwith(fr.rank(axis=1), axis=1).dropna()
    got["rank_ic"] = [round(x, 4) for x in block_bootstrap_ci(ic)]
    want["rank_ic"] = ref["rank_ic"]
    got["beta"] = R._last_beta(c["feats"], c["fwd"], c["elig"])
    want["beta"] = ref["coefficients_last_fit"]
    assert got == want, f"rebuild does not reproduce stage B:\n got {got}\nwant {want}"
    return got


# ------------------------------------------------------------------ Q2 --

def pinned_funding_checks() -> dict:
    """The sign and the (t, t+1] timing, through the SAME functions the
    report uses (funding_by_day -> forward_funding -> portfolio_pnl)."""
    days = pd.date_range("2024-03-01", periods=4, freq="D", tz="UTC")
    d = days[0]
    ev = pd.DataFrame({"symbol": "L", "rate": [0.0100, 0.0010, 0.0020, 0.0400],
                       "time": [d + pd.Timedelta(days=1, milliseconds=3),   # at d's close -> NOT d's hold
                                d + pd.Timedelta(days=1, hours=8),          # inside (d+1, d+2]
                                d + pd.Timedelta(days=2),                   # d+2 00:00 -> d's hold
                                d + pd.Timedelta(days=2, hours=8)]})        # next hold
    f = forward_funding(funding_by_day(ev, days))
    held_at_d = float(f.loc[d, "L"])
    assert np.isclose(held_at_d, 0.0010 + 0.0020), held_at_d
    one = pd.DataFrame({"L": [1.0], "S": [-1.0]}, index=days[:1])
    zero = pd.DataFrame(0.0, index=one.index, columns=one.columns)
    long_pays = portfolio_pnl(one, zero, pd.DataFrame({"L": [0.001], "S": [0.0]}, index=one.index), 0, 0)
    short_recv = portfolio_pnl(one, zero, pd.DataFrame({"L": [0.0], "S": [0.001]}, index=one.index), 0, 0)
    assert np.isclose(long_pays["funding"].iloc[0], -0.001) and np.isclose(short_recv["funding"].iloc[0], 0.001)
    return {"held_over_(d+1,d+2]_sums": held_at_d, "long_on_+10bp": -0.001, "short_on_+10bp": 0.001}


def raw_funding(symbols) -> pd.DataFrame:
    rows = []
    for s in symbols:
        for p in sorted((R.ZIPS / "monthly/fundingRate" / s).glob("*.zip")):
            t = pd.read_csv(io.StringIO(R._read(str(p))))
            rows.append(pd.DataFrame({
                "symbol": s, "time": pd.to_datetime(t["calc_time"], unit="ms", utc=True),
                "rate": t["last_funding_rate"].astype(float),
                "interval_h": t.get("funding_interval_hours", pd.Series(np.nan, index=t.index))}))
    ev = pd.concat(rows).drop_duplicates(["symbol", "time"]).sort_values(["symbol", "time"])
    assert ev["time"].max() < HOLDOUT, "a raw funding event reaches the 2026 holdout"
    return ev.reset_index(drop=True)


def independent_forward_funding(ev: pd.DataFrame, held: pd.Series) -> pd.Series:
    """Per held (day d, symbol): sum of raw events with stamp in
    (d+1 00:00, d+2 00:00], 60 s of stamp jitter allowed — by searchsorted
    on the raw stamps, not by the floor/pivot/shift of the report."""
    out = pd.Series(0.0, index=held.index)
    for s, g in ev.groupby("symbol"):
        idx = held.index[held.index.get_level_values(1) == s]
        if len(idx) == 0:
            continue
        ns = "datetime64[ns]"
        t = g["time"].to_numpy(dtype=ns)
        cs = np.concatenate([[0.0], np.cumsum(g["rate"].to_numpy())])
        d = idx.get_level_values(0)
        lo = np.searchsorted(t, (d + pd.Timedelta(days=1) + SLACK).to_numpy(dtype=ns), side="right")
        hi = np.searchsorted(t, (d + pd.Timedelta(days=2) + SLACK).to_numpy(dtype=ns), side="right")
        out.loc[idx] = cs[hi] - cs[lo]
    return out


def q2(c: dict) -> dict:
    w, ffund, elig, feats = c["w"], c["ffund"], c["elig"], c["feats"]
    held = w.stack()
    held = held[held != 0]
    syms = sorted(set(held.index.get_level_values(1)) | {"BTCUSDT", "SRMUSDT", "FTTUSDT"})
    ev = raw_funding(syms)
    ev21 = ev[(ev["time"] >= "2021-01-01") & (ev["time"] < HOLDOUT)]
    off = (ev21["time"] - ev21["time"].dt.round("h")).dt.total_seconds().abs()
    btc = ev21.loc[ev21["symbol"] == "BTCUSDT", "rate"].round(8)
    srm = ev.loc[(ev["symbol"] == "SRMUSDT") & (ev["time"] >= "2022-12-05"), "rate"]
    out = {"pinned": pinned_funding_checks(),
           "archive_convention": {
               "btc_modal_rate": float(btc.mode().iloc[0]),
               "btc_modal_share": round(float((btc == btc.mode().iloc[0]).mean()), 3),
               "btc_share_positive": round(float((btc > 0).mean()), 3),
               "srm_post_delisting_rates": sorted(set(srm.round(8).tolist()))[:5]},
           "stamp_offset_s": {"max": float(off.max()), "n_over_60s": int((off > 60).sum()),
                              "n_events_2021_25": int(len(off))}}
    fpanel = ffund.stack().reindex(held.index).fillna(0.0)
    findep = independent_forward_funding(ev, held)
    out["independent_recompute_max_abs_diff_per_name_day"] = float((fpanel - findep).abs().max())
    for per in PERIODS:
        days = span(w.index, per)
        h = held[held.index.get_level_values(0).isin(days)]
        n = len(days)
        fp, fi = fpanel.reindex(h.index), findep.reindex(h.index)
        contrib = -h * fp
        short = contrib[h < 0]
        worst = short.sort_values()
        top1 = worst.iloc[:max(1, len(worst) // 100)]
        e = elig.loc[days].fillna(False).astype(bool)
        f = ffund.loc[days].where(e)
        by = {}
        for feat, sign in (("vol_30", 1), ("ret_7", 1)):
            bk = buckets(feats[feat].loc[days].where(e), 5)
            by[feat] = {int(b): bp(np.nanmean(vals(f.where(bk == b))), 2) for b in range(1, 6)}
        out[per] = {
            "book_funding_bps_panel": bp(contrib.sum() / n, 2),
            "book_funding_bps_raw_zips": bp((-h * fi).sum() / n, 2),
            "long_leg_bps": bp(contrib[h > 0].sum() / n, 2),
            "short_leg_bps": bp(short.sum() / n, 2),
            "short_share_from_worst_1pct_name_days": round(float(top1.sum() / short.sum()), 3),
            "short_name_days_with_negative_funding": round(float((fp[h < 0] < 0).mean()), 3),
            "short_median_fwd_funding_bps_day": bp(fp[h < 0].median(), 2),
            "long_median_fwd_funding_bps_day": bp(fp[h > 0].median(), 2),
            "short_top_symbols_bps": {s: bp(v / n, 2) for s, v in
                                      short.groupby(level=1).sum().nsmallest(5).items()},
            "mean_fwd_funding_bps_day_by_quintile": by,
        }
        if per.startswith("test"):
            d, s = worst.index[0]
            win = ev[(ev["symbol"] == s) & (ev["time"] > d + pd.Timedelta(days=1) - pd.Timedelta(hours=1))
                     & (ev["time"] <= d + pd.Timedelta(days=2) + pd.Timedelta(hours=1))]
            out["hand_check"] = {
                "symbol": s, "decision_day": str(d.date()), "weight": float(h.loc[(d, s)]),
                "raw_events_near_(d+1,d+2]": [[str(t), r, iv] for t, r, iv in
                                               win[["time", "rate", "interval_h"]].itertuples(index=False)],
                "panel_forward_funding": float(fp.loc[(d, s)]),
                "raw_sum": float(fi.loc[(d, s)]),
                "pnl_contribution_bps": bp(contrib.loc[(d, s)], 2),
                "close_d_d+1": [float(c["close"].loc[d, s]), float(c["close"].loc[d + pd.Timedelta(days=1), s])],
                "vol_30_rank": round(float(cs_rank(feats["vol_30"], elig).loc[d, s]), 3),
                "ret_7_rank": round(float(cs_rank(feats["ret_7"], elig).loc[d, s]), 3)}
    p = portfolio_pnl(w, c["fwd"], ffund, 0, 0).loc[R.TEST_START:R.TEST_END]
    out["if_sign_were_flipped_gross_plus_funding_bps"] = bp(p["gross"].mean() - p["funding"].mean(), 2)
    return out


# ------------------------------------------------------------------ Q1 --

def quantile_table(score: pd.DataFrame, fwd: pd.DataFrame, days, q: int) -> dict:
    s = score.loc[days]
    r = fwd.loc[days].where(s.notna())
    bk = buckets(s, q)
    xs_mean, xs_med = r.mean(axis=1), r.median(axis=1)
    rows = {}
    for b in range(1, q + 1):
        rb = r.where(bk == b)
        rows[b] = {"mean_demeaned": bp(np.mean(vals(rb.sub(xs_mean, axis=0)))),
                   "median_demedianed": bp((rb.median(axis=1) - xs_med).mean()),
                   "raw_mean": bp(np.mean(vals(rb))), "raw_median": bp(np.median(vals(rb)))}
    top, bot = r.where(bk == q), r.where(bk == 1)
    return {"by_bucket_bps": rows,
            "top_minus_bottom_mean_ci": ci(top.mean(axis=1) - bot.mean(axis=1)),
            "top_minus_bottom_median_ci": ci(top.median(axis=1) - bot.median(axis=1))}


def vol_matched_null(w: pd.DataFrame, vq: pd.DataFrame, elig: pd.DataFrame, rng) -> pd.DataFrame:
    """Descriptive only: each model name swapped for a random eligible name
    from the SAME vol_30 quintile that day (keeps the vol tilt, randomises
    selection within it)."""
    W, Q = w.to_numpy(), vq.to_numpy()
    E = elig.fillna(False).to_numpy(dtype=bool)
    out = np.zeros_like(W)
    for i in range(W.shape[0]):
        taken: set = set()
        for j in np.flatnonzero(W[i] != 0):
            pool = [x for x in np.flatnonzero(E[i] & (Q[i] == Q[i, j])) if x not in taken]
            pick = pool[rng.integers(len(pool))]
            taken.add(pick)
            out[i, pick] = W[i, j]
    return pd.DataFrame(out, index=w.index, columns=w.columns)


def q1(c: dict, seeds: int) -> dict:
    fwd, elig, feats, scores, w = c["fwd"], c["elig"], c["feats"], c["scores"], c["w"]
    no_vol = walk_forward_scores({k: v for k, v in feats.items() if k != "vol_30"},
                                 fwd, elig, c["start"], R.TEST_END)
    vrank = cs_rank(feats["vol_30"], elig)
    x = vrank.where(scores.notna())
    xm = x.sub(x.mean(axis=1), axis=0)
    ym = scores.sub(scores.mean(axis=1), axis=0)
    beta = (xm * ym).sum(axis=1) / (xm * xm).sum(axis=1)
    partial = ym - xm.mul(beta, axis=0)
    neg_vol = (-feats["vol_30"]).where(scores.notna())
    out = {}
    for per in PERIODS:
        days = span(fwd.index, per)
        r = fwd.loc[days].where(scores.loc[days].notna())
        xs = r.mean(axis=1)
        up = xs > 0
        res = {"days": len(days), "share_up_days": round(float(up.mean()), 3)}
        for name, sc in (("score", scores), ("neg_vol_30", neg_vol),
                         ("score_refit_without_vol", no_vol), ("score_vol_partialled", partial)):
            sp = daily_ic(sc.loc[days], r, True)
            pe = daily_ic(sc.loc[days], r, False)
            res[f"ic_{name}"] = {
                "spearman_b10": ci(sp, 10, 1, 4), "spearman_b30": ci(sp, 30, 1, 4),
                "spearman_b60": ci(sp, 60, 1, 4), "pearson_b10": ci(pe, 10, 1, 4),
                "spearman_up_days": round(float(sp[up.reindex(sp.index)].mean()), 4),
                "spearman_down_days": round(float(sp[~up.reindex(sp.index)].mean()), 4),
                "share_days_ic_positive": round(float((sp > 0).mean()), 3)}
        res["quintiles_score"] = quantile_table(scores, fwd, days, 5)
        res["deciles_score"] = quantile_table(scores, fwd, days, 10)
        res["quintiles_neg_vol_30"] = quantile_table(neg_vol, fwd, days, 5)
        res["quintiles_score_refit_without_vol"] = quantile_table(no_vol, fwd, days, 5)
        bk = buckets(scores.loc[days], 5)
        spread = r.where(bk == 5).mean(axis=1) - r.where(bk == 1).mean(axis=1)
        res["q5_minus_q1_mean_by_market_direction_bps"] = {
            "up_days": bp(spread[up].mean()), "down_days": bp(spread[~up].mean())}
        wl, ws = (w.loc[days] > 0), (w.loc[days] < 0)
        res["legs_mean_fwd_bps"] = {
            "long_names": bp(fwd.loc[days].where(wl).mean(axis=1).mean()),
            "short_names": bp(fwd.loc[days].where(ws).mean(axis=1).mean()),
            "xs_equal_weight_eligible": bp(xs.mean()),
            "BTCUSDT": bp(fwd.loc[days, "BTCUSDT"].mean()),
            "ETHUSDT": bp(fwd.loc[days, "ETHUSDT"].mean())}
        res["vol_rank_of_legs"] = {"long": round(float(np.mean(vals(vrank.loc[days].where(wl)))), 3),
                                   "short": round(float(np.mean(vals(vrank.loc[days].where(ws)))), 3)}
        out[per] = res
    # vol-matched null on the test years, same slice as the report's null
    test = slice(R.TEST_START, R.TEST_END)
    vq = buckets(feats["vol_30"].where(elig.fillna(False).astype(bool)), 5)
    zero = 0.0
    model = portfolio_pnl(w, fwd, c["ffund"], zero, zero).loc[test]
    gf_model = float((model["gross"] + model["funding"]).mean())
    null = []
    for seed in range(seeds):
        nw = vol_matched_null(w.loc[test], vq.loc[test], elig.loc[test], np.random.default_rng(seed))
        p = portfolio_pnl(nw, fwd, c["ffund"], zero, zero).loc[test]
        null.append(float((p["gross"] + p["funding"]).mean()))
    null = np.array(null)
    out["vol_matched_null_test"] = {
        "seeds": seeds, "model_gross_plus_funding_bps": bp(gf_model, 2),
        "null_mean_bps": bp(null.mean(), 2), "null_p2.5": bp(np.quantile(null, 0.025), 2),
        "null_p97.5": bp(np.quantile(null, 0.975), 2),
        "model_percentile": round(float((null < gf_model).mean() * 100), 1)}
    return out


# ------------------------------------------------------------------ Q3 --

def q3(c: dict) -> dict:
    close, high, low, elig, w = c["close"], c["high"], c["low"], c["elig"], c["w"]
    eta = (np.log(high) + np.log(low)) / 2
    lc = np.log(close)
    term = 4 * (lc.shift(1) - eta.shift(1)) * (lc.shift(1) - eta)
    A = c["slip"]                                                     # as coded
    B = (np.sqrt(term.clip(lower=0)) / 2).rolling(30, min_periods=15).mean().clip(lower=FLOOR)
    C = (np.sqrt(term.clip(lower=0).rolling(30, min_periods=15).mean()) / 2).clip(lower=FLOOR)
    h = R._load("slip_1h").reindex(index=close.index, columns=close.columns)
    dep = slippage_depth(R._load("depth1"), close.index, close.columns,
                         R.A1["sens_depth"]["order_usd"], R.A1["sens_depth"]["band"],
                         R.A1["sens_depth"]["half_spread"])
    r1 = lc.diff()
    negcov = -(r1 * r1.shift(1))
    mt = term.rolling(30, min_periods=15).mean()
    mn = negcov.rolling(30, min_periods=15).mean()
    traded = (w - w.shift(1).fillna(0.0)).abs()
    panels = {"A_floor_after_mean(as coded)": A, "B_floor_per_day": B, "C_clip_terms_then_mean": C,
              "1h_AR": h, "depth_100k": dep}
    out = {}
    for y in range(2020, 2026):
        yr = str(y)
        e = elig.loc[yr].fillna(False).astype(bool)
        row = {"eligible_median_bps": {}, "BTC_mean_bps": {}, "ETH_mean_bps": {},
               "traded_weighted_bps": {}}
        for name, p in panels.items():
            if name in ("1h_AR", "depth_100k") and y < 2023:
                continue
            pe = p.loc[yr].where(e)
            row["eligible_median_bps"][name] = bp(np.median(vals(pe)))
            row["BTC_mean_bps"][name] = bp(p.loc[yr, "BTCUSDT"].mean())
            row["ETH_mean_bps"][name] = bp(p.loc[yr, "ETHUSDT"].mean())
            t = traded.loc[yr]
            if y >= 2021 and t.to_numpy().sum() > 0:
                pf = p.loc[yr].combine_first(A.loc[yr])
                row["traded_weighted_bps"][name] = bp((t * pf).sum().sum() / t.sum().sum())
        row["share_eligible_at_floor_A"] = round(float((vals(A.loc[yr].where(e)) <= FLOOR * 1.0001).mean()), 3)
        row["share_30d_mean_term_negative"] = round(float((vals(mt.loc[yr].where(e)) < 0).mean()), 3)
        row["mean_term_x1e4"] = round(float(np.mean(vals(term.loc[yr].where(e)))) * BPS, 3)
        row["mean_neg_autocov_x1e4"] = round(float(np.mean(vals(negcov.loc[yr].where(e)))) * BPS, 3)
        a, b = vals(mt.loc[yr].where(e & mn.loc[yr].notna())), vals(mn.loc[yr].where(e & mt.loc[yr].notna()))
        row["corr_30d_term_vs_neg_autocov"] = round(float(np.corrcoef(a, b)[0, 1]), 3)
        ra, rb = r1.loc[yr].where(e), r1.shift(1).loc[yr].where(e)
        both = ra.notna() & rb.notna()
        row["lag1_autocorr_daily_logret"] = round(float(np.corrcoef(vals(ra.where(both)), vals(rb.where(both)))[0, 1]), 4)
        row["median_abs_daily_logret_bps"] = bp(np.median(np.abs(vals(ra))))
        out[yr] = row
    for per in PERIODS:
        days = span(close.index, per)
        e = elig.loc[days].fillna(False).astype(bool)
        out[f"eligible_median_A_bps_{per}"] = bp(np.median(vals(A.loc[days].where(e))))
    e = elig.loc[:"2022-12-31"].fillna(False).astype(bool)
    out["eligible_median_A_bps_all_dev_rows_as_control()"] = bp(np.median(vals(A.loc[:"2022-12-31"].where(e))))
    return out


# ------------------------------------------------------------------ Q4 --

def q4(c: dict) -> dict:
    w, fwd, ffund, close, vol, elig = c["w"], c["fwd"], c["ffund"], c["close"], c["vol"], c["elig"]
    held = w != 0
    nxt_zero = (vol.shift(-1) == 0) & close.shift(-1).notna()
    nxt_nan = close.shift(-1).isna() & close.notna()
    leaves = ~elig.shift(-1).fillna(False).astype(bool)
    out = {}
    wl, ws = w.clip(lower=0), w.clip(upper=0)
    r_prev = fwd.shift(1)
    dl = wl.shift(1) * (1 + r_prev)
    dl = dl.div(dl.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    ds = ws.shift(1) * (1 + r_prev)
    ds = -ds.div(ds.sum(axis=1).replace(0, np.nan), axis=0).abs().fillna(0.0)
    drift_turn = (wl - dl).abs().sum(axis=1) + (ws - ds).abs().sum(axis=1)
    coded_turn = (w - w.shift(1).fillna(0.0)).abs().sum(axis=1)
    for per in PERIODS:
        days = span(w.index, per)
        n = len(days)
        hd = held.loc[days]
        out[per] = {
            "held_name_days": int(hd.to_numpy().sum()),
            "held_next_row_zero_volume": int((hd & nxt_zero.loc[days]).to_numpy().sum()),
            "their_gross_bps_per_day": bp((w * fwd).where(hd & nxt_zero).loc[days].sum().sum() / n, 2),
            "their_funding_bps_per_day": bp((-w * ffund).where(hd & nxt_zero).loc[days].sum().sum() / n, 2),
            "held_next_close_missing": int((hd & nxt_nan.loc[days]).to_numpy().sum()),
            "held_then_leave_universe": int((hd & leaves.loc[days]).to_numpy().sum()),
            "turnover_coded": round(float(coded_turn.loc[days].mean()), 3),
            "turnover_with_drift_rebalance": round(float(drift_turn.loc[days].mean()), 3)}
        if per.startswith("test"):
            examples = (hd & nxt_zero.loc[days]).stack()
            out[per]["examples_next_row_zero_volume"] = [
                [str(d.date()), s, float(w.loc[d, s]), bp(fwd.loc[d, s])]
                for d, s in examples[examples].index[:8]]
    # Archive gaps: a missing row resets the warm-up (by rule), so a day the
    # ARCHIVE lacks for most symbols drops them from the universe for a month.
    gap = close.isna() & close.ffill().notna() & close.bfill().notna()
    wide = gap.sum(axis=1) >= 10
    cells = gap & pd.DataFrame(np.repeat(wide.to_numpy()[:, None], gap.shape[1], 1),
                               gap.index, gap.columns)
    qv = R._load("quote_volume")
    bridged = eligibility(vol.where(~cells, vol.ffill()), qv.where(~cells, qv.ffill()),
                          R.WARMUP, R.LIQ_WINDOW, R.TOP_N, R.MIN_QV).fillna(False).astype(bool)
    e0 = elig.fillna(False).astype(bool)
    changed = bridged != e0
    out["archive_gaps"] = {
        "interior_missing_symbol_days_by_year": {
            int(y): int(v) for y, v in gap.groupby(gap.index.year).sum().sum(axis=1).items()},
        "market_wide_gap_days": [str(d.date()) for d in wide[wide].index],
        "eligible_name_days_changed_if_bridged_by_year": {
            int(y): int(v) for y, v in changed.groupby(changed.index.year).sum().sum(axis=1).items()},
        "names_losing_most_days": {s: int(v) for s, v in (bridged & ~e0).sum().nlargest(10).items()}}
    test = slice(R.TEST_START, R.TEST_END)
    prim = portfolio_pnl(w, fwd, ffund, c["fee"], c["slip"]).loc[test]
    x = prim["net"].to_numpy()
    out["net_acf_lags_1_10"] = [round(float(pd.Series(x).autocorr(k)), 3) for k in range(1, 11)]
    out["net_ci_by_block"] = {b: ci(prim["net"], b) for b in (1, 10, 30, 60)}
    gf = prim["gross"] + prim["funding"]
    out["gross_plus_funding_ci_by_block"] = {b: ci(gf, b) for b in (10, 30, 60)}
    wt, et = w.loc[test], elig.loc[test]
    zero = pd.DataFrame(0.0, index=wt.index, columns=wt.columns)
    mt = portfolio_pnl(wt, zero, zero, 0, 0)["turnover"]
    vr = cs_rank(c["feats"]["vol_30"], elig).loc[test]
    match = []
    for seed in range(5):
        nw = random_null_weights(wt, et, np.random.default_rng(seed))
        nt = portfolio_pnl(nw, zero, zero, 0, 0)["turnover"]
        match.append({"seed": seed, "turnover_null": round(float(nt.iloc[1:].mean()), 3),
                      "turnover_model": round(float(mt.iloc[1:].mean()), 3),
                      "share_days_exact": round(float(np.isclose(nt.iloc[1:], mt.iloc[1:]).mean()), 3),
                      "vol_rank_long": round(float(np.mean(vals(vr.where(nw > 0)))), 3),
                      "vol_rank_short": round(float(np.mean(vals(vr.where(nw < 0)))), 3)})
    out["null_match_test"] = match
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--seeds", type=int, default=100, help="vol-matched null seeds (descriptive)")
    a = ap.parse_args()
    c = build()
    res = {"reproduced_stage_b": reproduce(c)}
    res["Q2_funding"] = q2(c)
    res["Q1_ic"] = q1(c, a.seeds)
    res["Q3_slippage"] = q3(c)
    res["Q4_correctness"] = q4(c)
    text = json.dumps(res, indent=1, default=str)
    if a.out:
        a.out.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
