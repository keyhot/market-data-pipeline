"""R3-a: short newly listed perps, days 1-30 (vault Docs/r3a-preregistration.md).

Pure, DB-free pieces:
- the event universe from listing METADATA (S3 file names, exchangeInfo
  onboardDate) -- `build_events`, `assign_window`;
- the K3 loader cut: `event_rows` never returns a row whose bar OPEN time is
  >= day 0 + 31 d, for price, funding and BTC-hedge frames alike;
- the K2 tradability share from Kraken PF `openingDate`;
- the A0 synthetic power harness (capped 1x short, t3 residuals, month
  clusters, month-clustered percentile bootstrap, primary AND placebo leg).

Nothing here reads a real return.
"""
from __future__ import annotations

import re
from typing import Mapping

import numpy as np
import pandas as pd

MAX_DAY = 30
QUOTES = ("USDT", "USDC", "BUSD", "USD1")
_MULT = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")
INDEX_TOKENS = {"BTCDOM", "DEFI", "FOOTBALL", "BLUEBIRD"}
MAJORS = {"BTC", "ETH"}
# new ticker -> old ticker of the same token (flagged only if the old one is in the universe)
RENAMES = {"POL": "MATIC", "RENDER": "RNDR", "S": "FTM", "LUNC": "LUNA", "A": "EOS",
           "SKY": "MKR"}

WINDOW_A = (pd.Timestamp("2020-02-01"), pd.Timestamp("2025-12-01"))
WINDOW_B = (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-09-04"))
STRADDLE = (pd.Timestamp("2025-12-02"), pd.Timestamp("2025-12-31"))


class LookaheadRowError(AssertionError):
    """A scored frame contains a row outside [day 0, day 0 + 31 d)."""


# --- universe -----------------------------------------------------------------------

def _strip_multiplier(base: str) -> str:
    return _MULT.sub("", base)


def token_of(symbol: str) -> str:
    base = symbol.split("_")[0]
    for q in QUOTES:
        if base.endswith(q) and len(base) > len(q):
            base = base[: -len(q)]
            break
    return _strip_multiplier(base)


def excluded_by_name(symbol: str) -> bool:
    if "_" in symbol or "INDEX" in symbol:
        return True
    return token_of(symbol) in INDEX_TOKENS


def _month_start(m: str) -> pd.Timestamp:
    return pd.Timestamp(m + "-01")


def build_events(first_month: Mapping[str, str | None], info: list[dict],
                 daily_first: Mapping[str, str]) -> pd.DataFrame:
    """One row per token: its earliest Binance UM perp listing (metadata only)."""
    by_sym = {x["symbol"]: x for x in info}
    months = [m for m in first_month.values() if m]
    archive_start = _month_start(min(months)) if months else pd.Timestamp("2020-01-01")
    rows = []
    for sym in sorted(set(first_month) | set(by_sym)):
        if excluded_by_name(sym):
            continue
        meta = by_sym.get(sym)
        if meta is not None and (meta.get("contractType") != "PERPETUAL"
                                 or meta.get("underlyingType", "COIN") != "COIN"):
            continue
        fm = first_month.get(sym)
        fm_start = _month_start(fm) if fm else None
        if meta is not None and meta.get("onboardDate"):
            date = pd.Timestamp(int(meta["onboardDate"]), unit="ms").normalize()
            precision = "day"
            if fm_start is not None and fm_start < date.replace(day=1):
                date, precision = fm_start, "month"          # relisted: S3 saw it earlier
        elif sym in daily_first:
            date, precision = pd.Timestamp(daily_first[sym]), "day"
        elif fm_start is not None:
            date, precision = fm_start, "month"
        else:
            continue
        rows.append({"symbol": sym, "token": token_of(sym), "listing_date": date,
                     "precision": precision})
    ev = pd.DataFrame(rows, columns=["symbol", "token", "listing_date", "precision"])
    ev = (ev.sort_values(["token", "listing_date", "precision"])
            .drop_duplicates("token", keep="first").reset_index(drop=True))
    ev = ev[~ev["token"].isin(MAJORS)].reset_index(drop=True)
    ev["censored"] = ev["listing_date"] < archive_start + pd.offsets.MonthBegin(1)
    tokens = set(ev["token"])
    ev["rename"] = ev["token"].map(lambda t: t in RENAMES and RENAMES[t] in tokens)
    ev["window"] = ev["listing_date"].map(assign_window)
    return ev


def assign_window(date: pd.Timestamp) -> str | None:
    if WINDOW_A[0] <= date <= WINDOW_A[1]:
        return "a"
    if STRADDLE[0] <= date <= STRADDLE[1]:
        return "straddle"
    if WINDOW_B[0] <= date <= WINDOW_B[1]:
        return "b"
    return None


# --- K3: the loader cut -------------------------------------------------------------

FRAME_KINDS = ("price", "funding", "btc_hedge")


def _bounds(frame: pd.DataFrame, listing_date: pd.Timestamp, col: str):
    t = pd.to_datetime(frame[col])
    day0 = pd.Timestamp(listing_date).normalize()
    if getattr(t.dt, "tz", None) is not None and day0.tzinfo is None:
        day0 = day0.tz_localize("UTC")
    return t, day0, day0 + pd.Timedelta(days=MAX_DAY + 1)


def assert_no_row_beyond(frame: pd.DataFrame, listing_date: pd.Timestamp,
                         col: str = "open_time") -> None:
    t, lo, hi = _bounds(frame, listing_date, col)
    bad = (t >= hi) | (t < lo)
    if bad.any():
        raise LookaheadRowError(
            f"{int(bad.sum())} row(s) outside [day 0, day {MAX_DAY + 1}) by open time")


def event_rows(frame: pd.DataFrame, listing_date: pd.Timestamp, kind: str,
               col: str = "open_time") -> pd.DataFrame:
    """The ONLY sanctioned way to load an event's rows (price/funding/BTC hedge)."""
    if kind not in FRAME_KINDS:
        raise ValueError(f"unknown frame kind {kind!r}; expected one of {FRAME_KINDS}")
    t, lo, hi = _bounds(frame, listing_date, col)
    out = frame.loc[((t >= lo) & (t < hi)).to_numpy()].copy()
    assert_no_row_beyond(out, listing_date, col)
    return out


# --- K2: tradability ----------------------------------------------------------------

def kraken_token(symbol: str) -> str | None:
    if not symbol.startswith("PF_") or not symbol.endswith("USD"):
        return None
    base = symbol[3:-3]
    base = "BTC" if base == "XBT" else base
    return _strip_multiplier(base)


def kraken_open_share(events: pd.DataFrame,
                      kraken_open: Mapping[str, pd.Timestamp]) -> dict:
    day = events[events["precision"] == "day"]
    lags, n_ok = [], 0
    for tok, d in zip(day["token"], day["listing_date"]):
        ts = kraken_open.get(tok)
        if ts is None:
            continue
        ts = pd.Timestamp(ts)
        ts = ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts
        lag = (ts - pd.Timestamp(d)) / pd.Timedelta(days=1)
        lags.append(float(lag))
        n_ok += ts < pd.Timestamp(d) + pd.Timedelta(days=2)
    n = len(day)
    return {"n_denominator": n, "n_open_by_day1": int(n_ok),
            "share": (n_ok / n) if n else float("nan"),
            "n_matched_on_kraken": len(lags), "lags_days": lags,
            "n_month_precision_excluded": int(len(events) - n)}


# --- A0 synthetic harness -------------------------------------------------------------

def t3(rng: np.random.Generator, size) -> np.ndarray:
    """Student-t(3) scaled to unit variance."""
    return rng.standard_t(3, size) / np.sqrt(3.0)


def short_return(z: np.ndarray) -> np.ndarray:
    """1x short P&L for a 30-day log move z; margin exhaustion caps the loss at -100%."""
    return np.maximum(1.0 - np.exp(z), -1.0)


def cluster_rho(se_inflation: float, mean_cluster_size: float) -> float:
    if mean_cluster_size <= 1 or se_inflation <= 1:
        return 0.0
    return (se_inflation ** 2 - 1.0) / (mean_cluster_size - 1.0)


def _draw(rng, n, sigma, rho, month, n_months):
    c = rng.standard_normal(n_months) * sigma * np.sqrt(rho)
    return c[month] + sigma * np.sqrt(1.0 - rho) * t3(rng, n)


def calibrate_drift(target: float, sigma: float, rng: np.random.Generator,
                    rho: float = 0.0, n: int = 400_000) -> float:
    """Log drift m with E[short_return(m + noise)] = target (net short mean)."""
    noise = np.sqrt(rho) * sigma * rng.standard_normal(n) + sigma * np.sqrt(1 - rho) * t3(rng, n)
    lo, hi = -5.0, 5.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if short_return(mid + noise).mean() > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _cluster_sums(x, month, n_months):
    return (np.bincount(month, weights=x, minlength=n_months),
            np.bincount(month, minlength=n_months).astype(float))


def cluster_lower_bound(x, month, rng, n_boot: int = 999, alpha: float = 0.05) -> float:
    month = np.asarray(month)
    g = int(month.max()) + 1
    s, c = _cluster_sums(np.asarray(x, float), month, g)
    idx = rng.integers(0, g, (n_boot, g))
    stat = s[idx].sum(1) / np.maximum(c[idx].sum(1), 1.0)
    return float(np.quantile(stat, alpha))


def _diff_lower_bound(xe, me, xp, mp, g, rng, n_boot, alpha=0.05):
    se, ce = _cluster_sums(xe, me, g)
    sp, cp = _cluster_sums(xp, mp, g)
    idx = rng.integers(0, g, (n_boot, g))
    stat = (se[idx].sum(1) / np.maximum(ce[idx].sum(1), 1.0)
            - sp[idx].sum(1) / np.maximum(cp[idx].sum(1), 1.0))
    return float(np.quantile(stat, alpha))


def power(effect: float, n_events: int, n_months: int, placebo_per_month: float,
          sigma: float, se_inflation: float, n_seeds: int = 300, n_boot: int = 999,
          seed: int = 0, iid_bootstrap: bool = False) -> dict:
    """Pass rates of the stage-B test on synthetic windows.

    Events: net short mean = `effect`; placebo: net short mean = 0 with its OWN
    month effects (no common-shock cancellation -- conservative for the diff leg).
    """
    rng = np.random.default_rng(seed)
    k = n_events / n_months
    rho = cluster_rho(se_inflation, k)
    m_ev = calibrate_drift(effect, sigma, rng, rho)
    m_pl = calibrate_drift(0.0, sigma, rng, cluster_rho(se_inflation, placebo_per_month))
    n_pl = int(round(placebo_per_month * n_months))
    rho_pl = cluster_rho(se_inflation, placebo_per_month)
    prim = plac = joint = 0
    for _ in range(n_seeds):
        me = rng.integers(0, n_months, n_events)
        mp = np.repeat(np.arange(n_months), int(round(placebo_per_month)))[:n_pl]
        mp = mp if len(mp) == n_pl else rng.integers(0, n_months, n_pl)
        xe = short_return(m_ev + _draw(rng, n_events, sigma, rho, me, n_months))
        xp = short_return(m_pl + _draw(rng, n_pl, sigma, rho_pl, mp, n_months))
        if iid_bootstrap:
            lb1 = cluster_lower_bound(xe, np.arange(n_events), rng, n_boot)
            ok2 = True
        else:
            lb1 = cluster_lower_bound(xe, me, rng, n_boot)
            ok2 = _diff_lower_bound(xe, me, xp, mp, n_months, rng, n_boot) > 0
        prim += lb1 > 0
        plac += ok2
        joint += (lb1 > 0) and ok2
    return {"primary": prim / n_seeds, "placebo_leg": plac / n_seeds,
            "joint": joint / n_seeds, "rho": rho}


def mde(effects, powers, target: float = 0.8) -> float | None:
    """Smallest effect on the grid (linearly interpolated) with power >= target."""
    e, p = np.asarray(effects, float), np.asarray(powers, float)
    for i in range(len(e)):
        if p[i] >= target:
            if i == 0:
                return float(e[0])
            return float(e[i - 1] + (target - p[i - 1]) * (e[i] - e[i - 1]) / (p[i] - p[i - 1]))
    return None
