"""R3-b Stage A0: catching liquidation wicks with resting maker orders on Kraken PF.

Pre-registration: vault ``Docs/r3b-preregistration.md``. Everything here is
outcome-blind: a fill at bar t is decided from bars <= t only (sigma from bars
<= t-1, the fill from bar t's own low/high), and no function reads a price
after a fill bar. The power harness is synthetic.
"""
from __future__ import annotations

from typing import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

ARM_F_START = pd.Timestamp("2026-09-29 03:17", tz="UTC")
SIGMA_WINDOW = 60
HOLD = 30
TICKS = {"PF_XBTUSD": 1.0, "PF_ETHUSD": 0.1}
SIGMA_30 = {"PF_XBTUSD": 186.0, "PF_ETHUSD": 225.0}   # bps, plan assumption
SIGMA_CTRL = {"PF_XBTUSD": 45.0, "PF_ETHUSD": 55.0}   # bps, assumption
RHO_COFILL = 0.85
CONTROL_M = 10
COFILL_MINUTES = 2


# ---- C1 protection and parsing -----------------------------------------

def c1_reserved(idx: pd.DatetimeIndex) -> np.ndarray:
    """C1 arm H (1st of month + 01:00 overhang on the 2nd) and arm F minutes."""
    idx = pd.DatetimeIndex(idx)
    return np.asarray((idx.day == 1) | ((idx.day == 2) & (idx.hour < 1)) | (idx >= ARM_F_START))


def parse_candles(raw: Iterable[dict]) -> pd.DataFrame:
    raw = list(raw)
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close"], dtype=float,
                            index=pd.DatetimeIndex([], tz="UTC"))
    df = pd.DataFrame(raw)
    df.index = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    df = df[["open", "high", "low", "close"]].astype(float)
    df = df[~df.index.duplicated()]
    return df[~c1_reserved(df.index)].sort_index()


def fetch_range(getter: Callable[[int, int], list], start: pd.Timestamp,
                end: pd.Timestamp) -> pd.DataFrame:
    """Page ``getter(from_s, to_s)`` forward from start until end (exclusive)."""
    frm, end_s, parts = int(start.timestamp()), int(end.timestamp()), []
    while frm < end_s:
        page = getter(frm, end_s)
        if not page:
            break
        last = max(int(c["time"]) for c in page) // 1000
        parts.append(parse_candles(c for c in page if int(c["time"]) // 1000 < end_s))
        if last + 60 <= frm:
            break
        frm = last + 60
    if not parts:
        return parse_candles([])
    df = pd.concat(parts)
    return df[~df.index.duplicated()].sort_index()


# ---- sigma and fills ------------------------------------------------------

def _grid(s: pd.Series | pd.DataFrame):
    full = pd.date_range(s.index.min(), s.index.max(), freq="1min", tz="UTC")
    return s.reindex(full)


def trailing_sigma(close: pd.Series, window: int = SIGMA_WINDOW) -> pd.Series:
    """SD of 1m log returns over bars t-window..t-1; needs every close t-window-1..t-1."""
    g = _grid(close)
    r = np.log(g).diff()
    sig = r.shift(1).rolling(window, min_periods=window).std()
    return sig.reindex(close.index)


def floor_tick(x, tick):
    return np.floor(np.asarray(x) / tick + 1e-9) * tick


def ceil_tick(x, tick):
    return np.ceil(np.asarray(x) / tick - 1e-9) * tick


def wick_fills(bars: pd.DataFrame, k: float, tick: float, hold: int = HOLD,
               touch: bool = False, sigma: pd.Series | None = None,
               require_hold_present: bool = False) -> pd.DataFrame:
    """Trade-through fills of a bid/ask resting k*sigma_t from close_{t-1}.

    One position per symbol: after a fill at t, no order rests through t+hold.
    A bar that would fill both sides is a fill on neither (counted in attrs).
    """
    g = _grid(bars)
    sig = (trailing_sigma(bars["close"]) if sigma is None else sigma).reindex(g.index)
    pc = g["close"].shift(1)
    bid = floor_tick(pc * np.exp(-k * sig), tick)
    ask = ceil_tick(pc * np.exp(k * sig), tick)
    eps = 1e-9 * np.nan_to_num(pc.to_numpy(), nan=1.0)
    thr = 0.0 if touch else tick
    low, high = g["low"].to_numpy(), g["high"].to_numpy()
    with np.errstate(invalid="ignore"):
        hit_b = low <= bid - thr + eps
        hit_a = high >= ask + thr - eps
    if require_hold_present:
        ok = hold_present(g, hold)
        hit_b, hit_a = hit_b & ok, hit_a & ok
    cand = np.flatnonzero(hit_b | hit_a)
    times, sides, through, limits, ambiguous, next_ok = [], [], [], [], 0, -1
    for i in cand:
        if i < next_ok:
            continue
        if hit_b[i] and hit_a[i]:
            ambiguous += 1
            continue
        if hit_b[i]:
            sides.append("bid"); through.append((bid[i] - low[i]) / tick); limits.append(bid[i])
        else:
            sides.append("ask"); through.append((high[i] - ask[i]) / tick); limits.append(ask[i])
        times.append(g.index[i])
        next_ok = i + hold + 1
    out = pd.DataFrame({"side": sides, "through_ticks": through, "limit": limits},
                       index=pd.DatetimeIndex(times, tz="UTC") if times else pd.DatetimeIndex([], tz="UTC"))
    out.attrs["ambiguous"] = ambiguous
    return out


# ---- gaps, k, days ---------------------------------------------------------

def gap_share(present: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    full = pd.date_range(start, end, freq="1min", tz="UTC", inclusive="left")
    exp = full[~c1_reserved(full)]
    have = exp.isin(present).sum()
    return {"expected": int(len(exp)), "missing": int(len(exp) - have),
            "share": float((len(exp) - have) / max(len(exp), 1))}


def choose_k(rate_fn: Callable[[float], float], grid: Sequence[float], target: float = 1.0):
    for k in grid:
        if rate_fn(k) <= target:
            return k
    return None


def _eligible_index(start, end) -> pd.DatetimeIndex:
    d = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    return d[d.day != 1]


def eligible_days(start, end) -> int:
    return int(len(_eligible_index(start, end)))


def day_tuples(fx: pd.DatetimeIndex, fe: pd.DatetimeIndex, start, end,
               cofill_minutes: int = COFILL_MINUTES) -> np.ndarray:
    """Per eligible day: (n XBT fills, n ETH fills, n XBT fills with an ETH fill <= cofill_minutes away)."""
    days = _eligible_index(start, end)
    key = days.strftime("%Y-%m-%d")
    pos = {d: i for i, d in enumerate(key)}
    out = np.zeros((len(days), 3), dtype=int)
    fe_ns = np.sort(pd.DatetimeIndex(fe).asi8)
    tol = cofill_minutes * 60 * 10**9
    for col, idx in ((0, fx), (1, fe)):
        for d in pd.DatetimeIndex(idx).strftime("%Y-%m-%d"):
            if d in pos:
                out[pos[d], col] += 1
    used = np.zeros(len(fe_ns), bool)
    for t in pd.DatetimeIndex(fx):
        d = t.strftime("%Y-%m-%d")
        if d not in pos:
            continue
        j = np.flatnonzero((np.abs(fe_ns - t.value) <= tol) & ~used)
        if len(j):
            used[j[0]] = True
            out[pos[d], 2] += 1
    return out


# ---- synthetic power -----------------------------------------------------

def _t3(rng, size):
    return rng.standard_t(3, size) / np.sqrt(3.0)


def scale_tuples(tuples: np.ndarray, per_day: float, rng: np.random.Generator,
                 n_days: int) -> np.ndarray:
    """Resample whole dev days; stochastically rescale counts to mean per_day fills/day."""
    d = tuples[rng.integers(0, len(tuples), n_days)].astype(float)
    base = tuples[:, :2].sum(1).mean()
    f = per_day / base if base > 0 else 0.0
    x = d * f
    x = np.floor(x) + (rng.random(x.shape) < (x - np.floor(x)))
    x[:, 2] = np.minimum(x[:, 2], x[:, :2].min(1))
    return x.astype(int)


def _lower_bounds(x, c, day, g, rng, n_boot, alpha=0.05):
    s = np.bincount(day, weights=x, minlength=g)
    sd = np.bincount(day, weights=x - c, minlength=g)
    n = np.bincount(day, minlength=g).astype(float)
    idx = rng.integers(0, g, (n_boot, g))
    cnt = np.maximum(n[idx].sum(1), 1.0)
    return (float(np.quantile(s[idx].sum(1) / cnt, alpha)),
            float(np.quantile(sd[idx].sum(1) / cnt, alpha)))


def _draw_window(effect, days, icc, rng, symbols=("PF_XBTUSD", "PF_ETHUSD")):
    sx, se = (SIGMA_30[s] for s in symbols)
    cx, ce = (SIGMA_CTRL[s] for s in symbols)
    g = len(days)
    nx, ne, nc = days[:, 0], days[:, 1], days[:, 2]
    dev = rng.standard_normal(g)
    dctl = rng.standard_normal(g)
    a, b = np.sqrt(icc), np.sqrt(1 - icc)
    dx = np.repeat(np.arange(g), nx)
    de = np.repeat(np.arange(g), ne)
    ex = _t3(rng, len(dx))
    ee = _t3(rng, len(de))
    # co-fills: the first nc ETH fills of a day pair with the first nc XBT fills
    ox = np.concatenate([[0], np.cumsum(nx)[:-1]])
    oe = np.concatenate([[0], np.cumsum(ne)[:-1]])
    for day in np.flatnonzero(nc):
        for j in range(nc[day]):
            ee[oe[day] + j] = (RHO_COFILL * ex[ox[day] + j]
                               + np.sqrt(1 - RHO_COFILL ** 2) * _t3(rng, 1)[0])
    x = np.concatenate([effect + sx * (a * dev[dx] + b * ex),
                        effect + se * (a * dev[de] + b * ee)])
    ctl_sd = np.concatenate([np.full(len(dx), cx), np.full(len(de), ce)])
    day = np.concatenate([dx, de])
    c = ctl_sd * (a * dctl[day] + b * rng.standard_normal(len(day)) / np.sqrt(CONTROL_M))
    return x, c, day


def power(effect: float, tuples: np.ndarray, n_days: int, icc: float = 0.10,
          per_day: float | None = None, n_seeds: int = 300, n_boot: int = 999,
          seed: int = 0, iid_bootstrap: bool = False) -> dict:
    """Pass rates of the stage-B test (effect in bps, net) on synthetic windows."""
    rng = np.random.default_rng(seed)
    per_day = tuples[:, :2].sum(1).mean() if per_day is None else per_day
    prim = pair = joint = 0
    n_ev = 0
    for _ in range(n_seeds):
        days = scale_tuples(tuples, per_day, rng, n_days)
        x, c, day = _draw_window(effect, days, icc, rng)
        n_ev += len(x)
        if iid_bootstrap:
            day = np.arange(len(x))
        g = len(x) if iid_bootstrap else n_days
        lb1, lb2 = _lower_bounds(x, c, day, max(g, 1), rng, n_boot)
        prim += lb1 > 0
        pair += lb2 > 0
        joint += (lb1 > 0) and (lb2 > 0)
    return {"primary": prim / n_seeds, "paired": pair / n_seeds, "joint": joint / n_seeds,
            "mean_events": n_ev / n_seeds}


def mde(effects, powers, target: float = 0.8):
    e, p = np.asarray(effects, float), np.asarray(powers, float)
    for i in range(len(e)):
        if p[i] >= target:
            if i == 0:
                return float(e[0])
            return float(e[i - 1] + (target - p[i - 1]) * (e[i] - e[i - 1]) / (p[i] - p[i - 1]))
    return None


# ---- Stage A/B scorer (frozen spec: vault Docs/r3b-preregistration.md) -----

COST_BPS = 7.5          # maker entry 2 + taker exit 5 + exit half-spread 0.5
MIN_TRADED = 30         # traded (non-flat) minutes needed in t-60..t-1
MAX_GAP = 0.02
MAX_FLAT = 0.10


def slice_window(bars: pd.DataFrame, start, end) -> pd.DataFrame:
    """Bars opening in [start, end) only; the stage's hard data boundary."""
    a, b = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    return bars[(bars.index >= a) & (bars.index < b)]


def flat_minutes(bars: pd.DataFrame) -> pd.Series:
    """Flat = high == low == previous minute's close (A0's definition), on the full grid."""
    g = _grid(bars)
    return (g["high"] == g["low"]) & (g["high"] == g["close"].shift(1))


def trailing_sigma_traded(bars: pd.DataFrame, window: int = SIGMA_WINDOW,
                          min_traded: int = MIN_TRADED) -> pd.Series:
    """SD of the 1m log returns of the NON-flat minutes among t-window..t-1.

    Undefined unless all closes t-window-1..t-1 exist and >= min_traded of the
    window's minutes are non-flat. Bar t is never used. Indexed on the grid.
    """
    g = _grid(bars)
    r = np.log(g["close"]).diff().where(~flat_minutes(bars))
    sig = r.shift(1).rolling(window, min_periods=min_traded).std()
    full = g["close"].notna().astype(float).shift(1).rolling(window + 1).sum() == window + 1
    return sig.where(full)


def hold_present(g: pd.DataFrame, hold: int = HOLD) -> np.ndarray:
    """True at t iff closes t..t+hold all exist on the grid (so the exit exists)."""
    pres = g["close"].notna().astype(float)
    fwd = pres[::-1].rolling(hold + 1).sum()[::-1]
    return (fwd == hold + 1).to_numpy()


def _ret_bps(side_sign, entry, exit_):
    return side_sign * (exit_ - entry) / entry * 1e4


def score_fills(bars: pd.DataFrame, fills: pd.DataFrame, hold: int = HOLD,
                cost_bps: float = COST_BPS) -> pd.DataFrame:
    """Entry at the limit inside bar t, taker exit at close of bar t+hold; net of cost."""
    g = _grid(bars)["close"]
    ex = g.reindex(fills.index + pd.Timedelta(minutes=hold)).to_numpy()
    sign = np.where(fills["side"].to_numpy() == "bid", 1.0, -1.0)
    out = fills.copy()
    out["exit"] = ex
    out["gross_bps"] = _ret_bps(sign, fills["limit"].to_numpy(float), ex)
    out["net_bps"] = out["gross_bps"] - cost_bps
    return out


def _same_day_candidates(g: pd.DataFrame, ok: np.ndarray, t: pd.Timestamp, hold: int,
                         exclude_hold: bool = True) -> pd.DatetimeIndex:
    """Grid minutes on t's UTC day where an entry's hold exists; optionally outside [t, t+hold]."""
    idx = g.index                                   # full 1-minute grid: position == minute
    d0 = t.normalize()
    a, b = idx.searchsorted(d0), idx.searchsorted(d0 + pd.Timedelta(days=1))
    pos = np.arange(a, b)[ok[a:b]]
    if exclude_hold:
        ti = idx.searchsorted(t)
        pos = pos[(pos + hold < ti) | (pos > ti + hold)]
    return idx[pos]


def control_returns(bars: pd.DataFrame, fills: pd.DataFrame, m: int = CONTROL_M,
                    hold: int = HOLD, cost_bps: float = COST_BPS,
                    rng: np.random.Generator | None = None, return_draws: bool = False):
    """Per fill: mean net return of m random same-day market entries (at close_u,
    exit close_{u+hold}) in the fill's direction, outside the fill's hold, same cost."""
    rng = np.random.default_rng(0) if rng is None else rng
    g = _grid(bars)
    ok = hold_present(g, hold)
    close = g["close"]
    means, draws = [], []
    for t, side in zip(fills.index, fills["side"]):
        cand = _same_day_candidates(g, ok, t, hold)
        if len(cand) == 0:
            means.append(np.nan); draws.append(pd.DatetimeIndex([])); continue
        u = cand[rng.choice(len(cand), size=m, replace=len(cand) < m)]
        e, x = close.reindex(u).to_numpy(), close.reindex(u + pd.Timedelta(minutes=hold)).to_numpy()
        sign = 1.0 if side == "bid" else -1.0
        means.append(float(np.mean(_ret_bps(sign, e, x)) - cost_bps))
        draws.append(u)
    means = np.asarray(means)
    return (means, draws) if return_draws else means


def placebo_events(bars: pd.DataFrame, fills: pd.DataFrame, hold: int = HOLD,
                   rng: np.random.Generator | None = None) -> pd.DataFrame:
    """One random same-day market entry per fill, same side; 'limit' = close_u."""
    rng = np.random.default_rng(0) if rng is None else rng
    g = _grid(bars)
    ok = hold_present(g, hold)
    times, sides, lim = [], [], []
    for t, side in zip(fills.index, fills["side"]):
        cand = _same_day_candidates(g, ok, t, hold, exclude_hold=False)
        if len(cand) == 0:
            continue
        u = cand[rng.integers(len(cand))]
        times.append(u); sides.append(side); lim.append(float(g["close"][u]))
    return pd.DataFrame({"side": sides, "limit": lim, "through_ticks": np.nan},
                        index=pd.DatetimeIndex(times, tz="UTC") if times else pd.DatetimeIndex([], tz="UTC"))


def plant_exit(bars: pd.DataFrame, fills: pd.DataFrame, bps: float, hold: int = HOLD) -> pd.DataFrame:
    """Self-check: move close_{t+hold} by `bps` in each fill's favour (log scale)."""
    b = bars.copy()
    for t, side in zip(fills.index, fills["side"]):
        e = t + pd.Timedelta(minutes=hold)
        if e in b.index:
            b.loc[e, "close"] *= np.exp((1 if side == "bid" else -1) * bps / 1e4)
    return b


def cluster_bootstrap(x, c, day, n_boot: int = 9999, rng: np.random.Generator | None = None,
                      alpha: float = 0.05) -> dict:
    """Day-clustered percentile bootstrap of the pooled mean (x) and paired mean (x - c)."""
    rng = np.random.default_rng(0) if rng is None else rng
    x, c = np.asarray(x, float), np.asarray(c, float)
    _, day = np.unique(np.asarray(day), return_inverse=True)
    g = int(day.max()) + 1 if len(day) else 1
    s = np.bincount(day, weights=x, minlength=g)
    sd = np.bincount(day, weights=x - c, minlength=g)
    n = np.bincount(day, minlength=g).astype(float)
    idx = rng.integers(0, g, (n_boot, g))
    cnt = np.maximum(n[idx].sum(1), 1.0)
    bp, bd = s[idx].sum(1) / cnt, sd[idx].sum(1) / cnt
    r = {"n": int(len(x)), "days": g, "mean_primary": float(x.mean()), "mean_paired": float((x - c).mean()),
         "mean_control": float(c.mean()),
         "lb_primary": float(np.quantile(bp, alpha)), "lb_paired": float(np.quantile(bd, alpha)),
         "ci95_primary": [float(np.quantile(bp, 0.025)), float(np.quantile(bp, 0.975))],
         "ci95_paired": [float(np.quantile(bd, 0.025)), float(np.quantile(bd, 0.975))]}
    r["pass"] = bool(r["lb_primary"] > 0 and r["lb_paired"] > 0)
    return r


def data_gate(bars: pd.DataFrame, start, end, max_gap: float = MAX_GAP,
              max_flat: float = MAX_FLAT) -> dict:
    """Refuse-to-score gate: missing-minute share and flat-minute share over the window."""
    gap = gap_share(bars.index, pd.Timestamp(start), pd.Timestamp(end))
    flat = float(flat_minutes(bars).reindex(bars.index).mean())
    return {"gap_share": gap["share"], "flat_share": flat,
            "ok": bool(gap["share"] <= max_gap and flat <= max_flat)}
