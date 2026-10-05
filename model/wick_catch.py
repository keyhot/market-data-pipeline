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
               touch: bool = False) -> pd.DataFrame:
    """Trade-through fills of a bid/ask resting k*sigma_t from close_{t-1}.

    One position per symbol: after a fill at t, no order rests through t+hold.
    A bar that would fill both sides is a fill on neither (counted in attrs).
    """
    g = _grid(bars)
    sig = trailing_sigma(bars["close"]).reindex(g.index)
    pc = g["close"].shift(1)
    bid = floor_tick(pc * np.exp(-k * sig), tick)
    ask = ceil_tick(pc * np.exp(k * sig), tick)
    eps = 1e-9 * np.nan_to_num(pc.to_numpy(), nan=1.0)
    thr = 0.0 if touch else tick
    low, high = g["low"].to_numpy(), g["high"].to_numpy()
    with np.errstate(invalid="ignore"):
        hit_b = low <= bid - thr + eps
        hit_a = high >= ask + thr - eps
    cand = np.flatnonzero(hit_b | hit_a)
    times, sides, through, ambiguous, next_ok = [], [], [], 0, -1
    for i in cand:
        if i < next_ok:
            continue
        if hit_b[i] and hit_a[i]:
            ambiguous += 1
            continue
        if hit_b[i]:
            sides.append("bid"); through.append((bid[i] - low[i]) / tick)
        else:
            sides.append("ask"); through.append((high[i] - ask[i]) / tick)
        times.append(g.index[i])
        next_ok = i + hold + 1
    out = pd.DataFrame({"side": sides, "through_ticks": through},
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
