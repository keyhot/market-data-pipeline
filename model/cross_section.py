"""Cross-sectional perps ranking (experiment E13, vault
`Docs/cross-sectional-ranking-experiment.md`). Pure, DB-free functions over
daily PANELS: a DataFrame indexed by UTC day (the daily kline's OPEN time)
with one column per symbol.

Timing, the one rule everything obeys:
- Day d's kline closes at d + 1 day, 00:00 UTC. Every FEATURE for day d uses
  only data known by that close.
- The position chosen at d's close is held for day d + 1. Its return is
  close[d+1] / close[d] - 1 (`forward_returns`), and it pays or receives the
  funding events that settle in (d+1 00:00, d+2 00:00] (`forward_funding`).
- A model refit at month start R may train only on days d < R
  (`training_days`): the target of day R - 1 is known at R + 1 day 00:00,
  which is exactly when the first decision it serves (close of R) is made.

The universe is point-in-time by construction (`eligibility`): a symbol is
eligible on d only from ROWS up to d — traded that day, an unbroken traded
history before it, and a trailing liquidity rank. No listing or delisting date
is ever read, so a delisted symbol simply stops being eligible, and a reused
ticker (a gap in trading) restarts its warm-up.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DAY = pd.Timedelta(days=1)
FEATURES = ("ret_1", "ret_7", "ret_30", "vol_30", "funding_7", "oi_change_7")

# Stablecoins, FX and index products are not "coins" a cross-section should
# rank. Pre-registered list; the suffix/underscore filter does the rest.
EXCLUDED = frozenset({
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDPUSDT", "FDUSDUSDT", "DAIUSDT",
    "EURUSDT", "GBPUSDT", "USDEUSDT", "USD1USDT", "RLUSDUSDT", "USDBRLUSDT",
    "BTCDOMUSDT", "DEFIUSDT", "BLUEBIRDUSDT", "FOOTBALLUSDT",
})


def is_candidate(symbol: str) -> bool:
    """USDT-margined perpetuals only: no dated quarterlies (`_`), no
    stablecoin/FX/index products."""
    return (symbol.endswith("USDT") and "_" not in symbol
            and symbol.isascii() and symbol not in EXCLUDED)


# ---------------------------------------------------------------- universe --

def market_wide_gap_days(volume: pd.DataFrame, min_missing: int = 10) -> pd.Series:
    """Days a source-truncated archive drops MANY symbols at once (KI-098:
    Binance's monthly 1d zips are truncated for 48-49 of ~630 symbols on
    2022-02-26..28 and 49 on 2022-04-01..02), as distinct from one symbol's
    own halt, delisting, or not-yet-listed history. A cell counts only if it
    is INTERIOR to that symbol's own row history (both an earlier and a
    later non-null row exist in `volume`) — most days most symbols are
    "missing" simply because they have not listed yet, and that is not a
    gap. A day qualifies once at least `min_missing` symbols are
    interior-missing on it."""
    missing = volume.isna()
    interior = missing & volume.ffill().notna() & volume.bfill().notna()
    return interior.sum(axis=1) >= min_missing


def bridge_market_wide_gaps(volume: pd.DataFrame, quote_volume: pd.DataFrame,
                            gap_days: pd.Series | None = None,
                            min_missing: int = 10
                            ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Forward-fill `volume`/`quote_volume` on cells that fall on a detected
    market-wide archive gap day (`market_wide_gap_days`, auto-detected when
    `gap_days` is None), so the WARM-UP (`traded_run_length`) and the
    LIQUIDITY RANK (its `quote_volume` rolling median) both see the name's
    last known trading state through the source's own outage instead of a
    blackout that masquerades as a per-symbol halt (KI-098). Mirrors the
    scratch re-score in `scripts/e13_diagnostics.py` that the E13 review
    already validated against 2022-2025 (gross + funding 8.07 vs the
    branch's 7.87, null p97.5 18.06 vs 19.23 — verdict unaffected). A real,
    symbol-only halt (not on a gap day) is untouched."""
    gap = (gap_days if gap_days is not None
          else market_wide_gap_days(volume, min_missing)).reindex(
        volume.index, fill_value=False)
    cells = volume.isna() & pd.DataFrame(
        np.repeat(gap.to_numpy(dtype=bool)[:, None], volume.shape[1], axis=1),
        volume.index, volume.columns)
    return (volume.where(~cells, volume.ffill()),
           quote_volume.where(~cells, quote_volume.ffill()))


def traded_run_length(volume: pd.DataFrame) -> pd.DataFrame:
    """Consecutive traded days (volume > 0) ending at each day, inclusive.
    A zero-volume or missing day resets the count to 0. Callers that must
    not mistake a market-wide archive gap for a halt (KI-098) bridge with
    `bridge_market_wide_gaps` first — `eligibility` does this."""
    traded = (volume.fillna(0).to_numpy(dtype=float) > 0)
    run = np.zeros(traded.shape, dtype=np.int64)
    current = np.zeros(traded.shape[1], dtype=np.int64)
    for i in range(traded.shape[0]):
        current = np.where(traded[i], current + 1, 0)
        run[i] = current
    return pd.DataFrame(run, index=volume.index, columns=volume.columns)


def eligibility(volume: pd.DataFrame, quote_volume: pd.DataFrame,
                warmup: int = 30, liquidity_window: int = 30,
                top_n: int = 50, min_quote_volume: float = 0.0) -> pd.DataFrame:
    """Boolean panel: eligible on d iff traded on d with >= `warmup` traded
    days before it (unbroken — but a market-wide archive gap day bridges
    rather than breaking it, `bridge_market_wide_gaps`/KI-098, applied to
    both the warm-up and the liquidity rank below), and among the `top_n` by
    trailing `liquidity_window`-day median quote volume ending at d (which
    must also clear `min_quote_volume`). Reads nothing after d."""
    volume, quote_volume = bridge_market_wide_gaps(volume, quote_volume)
    run = traded_run_length(volume)
    history = run >= warmup + 1
    liquidity = quote_volume.rolling(liquidity_window,
                                     min_periods=liquidity_window).median()
    liquidity = liquidity.where(history & (liquidity >= min_quote_volume))
    rank = liquidity.rank(axis=1, ascending=False, method="first")
    return rank <= top_n


# ---------------------------------------------------------------- funding --

def funding_by_day(events: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Sum of funding rates per holding day H: events settling in
    (H 00:00, H+1 00:00]. Counts every event, whatever the symbol's funding
    interval (8h, 4h or 1h). `events` has columns symbol, time (UTC), rate.
    Settlement stamps carry a few ms of jitter, hence the 1-minute slack."""
    holding = (events["time"] - pd.Timedelta(minutes=1)).dt.floor("D")
    frame = (events.assign(day=holding)
             .pivot_table(index="day", columns="symbol", values="rate",
                          aggfunc="sum"))
    return frame.reindex(days).fillna(0.0)


def forward_funding(funding_daily: pd.DataFrame) -> pd.DataFrame:
    """Funding paid over the holding day of a position chosen at d's close."""
    return funding_daily.shift(-1)


# ---------------------------------------------------------------- returns --

def forward_returns(close: pd.DataFrame) -> pd.DataFrame:
    """close[d+1]/close[d] - 1. A zero-volume row after d still has a close
    (Binance's post-delisting settlement mark), so a held delisting realises
    its move to settlement. A MISSING next row gives 0 (nothing to hold)."""
    nxt = close.shift(-1)
    return (nxt / close - 1).where(nxt.notna() & close.notna(), 0.0)


def daily_features(close: pd.DataFrame, funding_daily: pd.DataFrame,
                   oi_close: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Feature panels at day d's close. Log returns over 1/7/30 days,
    30-day volatility of daily log returns, 7-day summed funding, and 7-day
    log change in open interest (NaN where OI is unknown)."""
    logc = np.log(close)
    r1 = logc.diff()
    oi = (oi_close if oi_close is not None
          else pd.DataFrame(np.nan, index=close.index, columns=close.columns))
    logoi = np.log(oi.where(oi > 0))
    return {
        "ret_1": r1,
        "ret_7": logc - logc.shift(7),
        "ret_30": logc - logc.shift(30),
        "vol_30": r1.rolling(30, min_periods=30).std(),
        "funding_7": funding_daily.reindex_like(close).rolling(7, min_periods=7).sum(),
        "oi_change_7": logoi - logoi.shift(7),
    }


def oi_at_close(oi: pd.Series, days: pd.DatetimeIndex,
                delay: pd.Timedelta = pd.Timedelta(minutes=5),
                max_age: pd.Timedelta = pd.Timedelta(hours=1)) -> pd.Series:
    """Open interest known at each day's close (d + 1 day): the latest
    positive row with create_time + `delay` <= the close, NaN if none or
    staler than `max_age`. `delay` is E12's conservative 5 minutes."""
    oi = oi[oi > 0].sort_index()
    available = oi.index + delay
    closes = days + DAY
    pos = available.searchsorted(closes, side="right") - 1
    ok = pos >= 0
    safe = np.maximum(pos, 0)
    values = oi.to_numpy(dtype=float)[safe] if len(oi) else np.full(len(days), np.nan)
    age = closes - (available[safe] if len(oi) else closes)
    out = np.where(ok & (age <= max_age), values, np.nan) if len(oi) else values
    return pd.Series(out, index=days)


# ---------------------------------------------------------------- costs --

def slippage_per_side(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
                      window: int = 30, floor: float = 0.0001) -> pd.DataFrame:
    """Per-symbol per-side slippage at d: half the Abdi-Ranaldo (2017)
    close-high-low spread, averaged over the trailing `window` days that end
    at d (its eta[t+1] term means the last pair is (d-1, d)), floored at P1's
    1 bp. Daily bars overstate it (volatility leaks in), so it errs costly."""
    eta = (np.log(high) + np.log(low)) / 2
    c = np.log(close)
    term = 4 * (c.shift(1) - eta.shift(1)) * (c.shift(1) - eta)
    mean = term.rolling(window, min_periods=window // 2).mean().clip(lower=0)
    return (np.sqrt(mean) / 2).clip(lower=floor)


def slippage_hourly(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
                    days: pd.DatetimeIndex, window: int = 720, min_bars: int = 360,
                    floor: float = 0.0001) -> pd.DataFrame:
    """Amendment A1 (a): the Abdi-Ranaldo half-spread on HOURLY bars, over
    the trailing `window` bars that close by d's close (open < d + 1 day),
    in the same order as the daily version: mean, clip >= 0, sqrt, halve,
    floor. Indexed by `days`; NaN where fewer than `min_bars` terms exist."""
    eta = (np.log(high) + np.log(low)) / 2
    c = np.log(close)
    term = 4 * (c.shift(1) - eta.shift(1)) * (c.shift(1) - eta)
    mean = term.rolling(window, min_periods=min_bars).mean().clip(lower=0)
    half = (np.sqrt(mean) / 2).clip(lower=floor)
    # The last bar that closes by d's close opens at d + 23h: as-of it.
    last_bar = days + DAY - pd.Timedelta(hours=1)
    out = half.reindex(half.index.union(last_bar)).ffill().reindex(last_bar)
    out.index = days
    return out


def slippage_depth(depth1: pd.DataFrame, days: pd.DatetimeIndex, columns,
                   order_usd: float, band: float = 0.01,
                   half_spread: float = 0.0001) -> pd.DataFrame:
    """Amendment A1 (b): per-side cost of an `order_usd` trade on a linear
    book holding `depth1` USD within `band` of mid: half_spread + 1/2 * band
    * order / depth. Samples (one row per sampled day, known at that day's
    close) join as-of BACKWARD; NaN before a name's first sample, so the
    caller supplies the fallback."""
    cost = half_spread + 0.5 * band * order_usd / depth1.where(depth1 > 0)
    cost = cost.reindex(columns=list(columns)).sort_index()
    return cost.reindex(cost.index.union(days)).ffill().reindex(days)


# ---------------------------------------------------------------- model --

def cs_rank(panel: pd.DataFrame, eligible: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional percentile rank among eligible names, centred to
    [-0.5, 0.5]. Missing values on an eligible name are neutral (0)."""
    masked = panel.where(eligible)
    ranked = (masked.rank(axis=1) - 0.5).div(masked.count(axis=1), axis=0) - 0.5
    return ranked.where(eligible).fillna(0.0).where(eligible)


def training_days(days: pd.DatetimeIndex, refit: pd.Timestamp) -> pd.DatetimeIndex:
    """Days whose target may train a model refit at month start `refit`:
    strictly before it (see the module docstring)."""
    return days[days < refit]


def fit_ridge(X: np.ndarray, y: np.ndarray, lam: float = 1.0) -> np.ndarray:
    """Ridge coefficients (no intercept: features and target are centred ranks)."""
    p = X.shape[1]
    return np.linalg.solve(X.T @ X + lam * np.eye(p), X.T @ y)


def walk_forward_scores(features: dict[str, pd.DataFrame], target: pd.DataFrame,
                        eligible: pd.DataFrame, start: pd.Timestamp,
                        end: pd.Timestamp, lam: float = 1.0,
                        min_train_days: int = 180) -> pd.DataFrame:
    """Expanding walk-forward: refit a rank-ridge at each month start in
    [start, end], score eligible names through the month. Features and target
    are cross-sectional centred ranks, so the model is scale-free."""
    ranked = {k: cs_rank(v, eligible) for k, v in features.items()}
    tgt = cs_rank(target, eligible)
    names = list(ranked)
    stacked = pd.concat({k: ranked[k].stack() for k in names}, axis=1)
    y_all = tgt.stack().reindex(stacked.index)
    days_level = stacked.index.get_level_values(0)
    scores = pd.DataFrame(np.nan, index=eligible.index, columns=eligible.columns)
    refits = pd.date_range(start, end, freq="MS", tz=eligible.index.tz)
    if len(refits) == 0 or refits[0] != start:
        refits = refits.insert(0, start)
    for i, refit in enumerate(refits):
        train_days = training_days(eligible.index, refit)
        if len(train_days) < min_train_days:
            continue
        mask = days_level < refit
        beta = fit_ridge(stacked[mask].to_numpy(), y_all[mask].to_numpy(), lam)
        stop = refits[i + 1] if i + 1 < len(refits) else end + DAY
        span = (days_level >= refit) & (days_level < stop) & (days_level <= end)
        block = stacked[span]
        scores_block = pd.Series(block.to_numpy() @ beta, index=block.index)
        scores.update(scores_block.unstack())
    return scores.where(eligible)


# ---------------------------------------------------------------- portfolio --

def book_size(n_eligible: int, frac: float = 0.1, min_per_side: int = 3) -> int:
    return max(min_per_side, int(np.floor(n_eligible * frac)))


def decile_weights(scores: pd.DataFrame, frac: float = 0.1,
                   min_per_side: int = 3) -> pd.DataFrame:
    """+1/k on the top-k scores, -1/k on the bottom-k (k = decile, at least
    `min_per_side`); a day with fewer than 2k scored names stays flat. Ties
    break by column order, deterministically."""
    w = np.zeros(scores.shape)
    values = scores.to_numpy(dtype=float)
    for i, row in enumerate(values):
        idx = np.flatnonzero(~np.isnan(row))
        k = book_size(len(idx), frac, min_per_side)
        if len(idx) < 2 * k:
            continue
        order = idx[np.argsort(row[idx], kind="stable")]
        w[i, order[-k:]] = 1.0 / k
        w[i, order[:k]] = -1.0 / k
    return pd.DataFrame(w, index=scores.index, columns=scores.columns)


def portfolio_pnl(weights: pd.DataFrame, fwd_ret: pd.DataFrame,
                  fwd_funding: pd.DataFrame, fee_per_side: float,
                  slippage: pd.DataFrame | float) -> pd.DataFrame:
    """Per-rebalance P&L of a weight panel (fractions of one unit per leg).
    gross = sum w * r; funding = -sum w * f (a long pays positive funding, a
    short receives it); cost = sum |w_d - w_{d-1}| * (fee + slippage[d]) —
    only what TRADES pays. net = gross + funding - cost."""
    w = weights.fillna(0.0)
    r = fwd_ret.reindex_like(w).fillna(0.0)
    f = fwd_funding.reindex_like(w).fillna(0.0)
    slip = (slippage.reindex_like(w).fillna(0.0) if isinstance(slippage, pd.DataFrame)
            else pd.DataFrame(slippage, index=w.index, columns=w.columns))
    traded = (w - w.shift(1).fillna(0.0)).abs()
    out = pd.DataFrame({
        "gross": (w * r).sum(axis=1),
        "funding": -(w * f).sum(axis=1),
        "cost": (traded * (fee_per_side + slip)).sum(axis=1),
        "turnover": traded.sum(axis=1),
    })
    out["net"] = out["gross"] + out["funding"] - out["cost"]
    return out


def random_null_weights(weights: pd.DataFrame, eligible: pd.DataFrame,
                        rng: np.random.Generator) -> pd.DataFrame:
    """A random ranking with the model's exposure and turnover: each day the
    same k per side at the same +-1/k, keeping as many of yesterday's (null)
    names per side as the model kept of its own, the rest drawn at random
    from eligible names not on the other side."""
    W = weights.to_numpy()
    E = eligible.fillna(False).to_numpy(dtype=bool)
    out = np.zeros_like(W)
    prev_long, prev_short = np.array([], int), np.array([], int)
    prev_ml, prev_ms = set(), set()
    for i in range(W.shape[0]):
        ml, ms = set(np.flatnonzero(W[i] > 0)), set(np.flatnonzero(W[i] < 0))
        k = len(ml)
        if k == 0:
            prev_long, prev_short, prev_ml, prev_ms = np.array([], int), np.array([], int), ml, ms
            continue
        pool = np.flatnonzero(E[i])
        keep_l = min(len(ml & prev_ml), k)
        keep_s = min(len(ms & prev_ms), k)
        still_l = [j for j in prev_long if E[i, j]]
        still_s = [j for j in prev_short if E[i, j]]
        longs = list(rng.permutation(still_l)[:keep_l]) if still_l else []
        shorts = list(rng.permutation(still_s)[:keep_s]) if still_s else []
        taken = set(longs) | set(shorts)
        # Fresh names first: re-using a name the null just dropped (or moving
        # it across sides) would cost it less (or more) turnover than the model.
        stale = set(prev_long) | set(prev_short)
        fresh = [j for j in pool if j not in taken and j not in stale]
        spare = [j for j in pool if j not in taken and j in stale]
        rest = np.concatenate([rng.permutation(fresh), rng.permutation(spare)]).astype(int)
        need_l, need_s = k - len(longs), k - len(shorts)
        longs += list(rest[:need_l])
        shorts += list(rest[need_l:need_l + need_s])
        out[i, longs] = 1.0 / k
        out[i, shorts] = -1.0 / k
        prev_long, prev_short, prev_ml, prev_ms = np.array(longs, int), np.array(shorts, int), ml, ms
    return pd.DataFrame(out, index=weights.index, columns=weights.columns)


# ---------------------------------------------------------------- stats --

def block_bootstrap_ci(x: np.ndarray | pd.Series, block: int = 10,
                       n: int = 1000, seed: int = 7,
                       alpha: float = 0.05) -> tuple[float, float, float]:
    """Mean and moving-block bootstrap (1 - alpha) CI of a daily series."""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    rng = np.random.default_rng(seed)
    starts_max = len(x) - block + 1
    n_blocks = int(np.ceil(len(x) / block))
    means = np.empty(n)
    for b in range(n):
        starts = rng.integers(0, starts_max, n_blocks)
        sample = np.concatenate([x[s:s + block] for s in starts])[:len(x)]
        means[b] = sample.mean()
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(x.mean()), float(lo), float(hi)
