"""Walk-forward backtest harness (Sprint 9) — the model plane's real product.

Rolling train → predict folds with a purged boundary, always modeling fees
and slippage. Rules from docs/freqai-takeaways.md: never shuffled, purge
`horizon_bars` at each fold boundary, costs are constructor arguments and
never zero by default. Usage:

    python -m model.backtest --symbol BTCUSDT --interval 1m
"""

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from model.benchmark import (
    exposure_matched_return,
    held_coverage,
    return_dispersion,
)
from model.features import DEFAULT_HORIZON_BARS, build_features
from model.train import _NUM_ROUNDS, _PARAMS, TRAIN_BARS_LIMIT
from storage.postgres_store import get_price_bars

RESULTS_DIR = Path(__file__).parent / "artifacts"


@dataclass(frozen=True)
class BacktestConfig:
    horizon_bars: int = DEFAULT_HORIZON_BARS
    train_rows: int = 800
    test_rows: int = 200
    entry_threshold: float = 0.55  # long when P(up) exceeds this; flat otherwise
    fee_per_side: float = 0.001    # Binance taker 0.1%
    slippage: float = 0.0001       # 1 bp per side
    # Bars to stay flat after a position exits before another may open.
    # 0 = off, which is how every published figure was measured; raising it
    # makes exposure a decision the strategy takes rather than a by-product
    # of the horizon (model plan item 2).
    cooldown_bars: int = 0


def find_episodes(mask) -> list[tuple[int, int]]:
    """Runs of consecutive True as inclusive (start, end) index pairs.

    One run is one position: entered once, exited once. Charging the round
    trip per in-market *bar* instead was KI-040 — a ~3x fee overcharge at
    threshold 0.80, where 3,101 positions were billed 10,052 times.
    """
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return []
    starts = np.flatnonzero(mask & ~np.r_[False, mask[:-1]])
    ends = np.flatnonzero(mask & ~np.r_[mask[1:], False])
    return list(zip(starts.tolist(), ends.tolist()))


def merge_overlapping_holds(
    episodes: list[tuple[int, int]],
    horizon_bars: int,
    pending: tuple[int, int] | None = None,
) -> tuple[list[tuple[int, int]], tuple[int, int] | None]:
    """Fold episodes whose holding periods overlap into the single position
    one unit of capital actually held.

    A position is held while the signal is on and realizes `horizon_bars`
    after it drops. If the next episode opens before that, the trader never
    exited — so it is the *same* position continuing, not a second one. This
    is the KI-001 fix: `strategy_total_return` is a product, and a product
    over overlapping holding periods compounds the same capital twice.

    Merging rather than dropping the later episode matters: dropping would
    discard a real entry (and, when a signal spans a whole fold, an entire
    fold's trading). `pending` carries the still-open position across fold
    boundaries, which are adjacent — merging per fold from a clean slate
    re-admits the overlap at every seam.

    Returns the positions that closed, and the one still open (if any).
    """
    closed: list[tuple[int, int]] = []
    for start, end in episodes:
        if pending is None:
            pending = (start, end)
            continue
        open_start, open_end = pending
        if start <= open_end + horizon_bars:
            pending = (open_start, max(open_end, end))
        else:
            closed.append(pending)
            pending = (start, end)
    return closed, pending


def apply_cooldown(
    held: list[tuple[int, int]], horizon_bars: int, cooldown_bars: int
) -> list[tuple[int, int]]:
    """Decline entries that arrive before the strategy has been flat for
    `cooldown_bars` after its previous exit.

    Runs **after** `merge_overlapping_holds`, and deliberately not instead of
    it: merging is not a policy, it is the KI-001 correctness argument that
    capital held continuously is one position. The cooldown is the policy —
    it puts capital genuinely flat, which is the only honest way to lower
    exposure. The clock starts at the *exit* (`end + horizon_bars`), not at
    the last signal bar, because the position is still held in between.

    A declined entry never happened, so it starts no cooldown of its own: the
    clock keeps running from the last position actually taken.
    """
    if cooldown_bars <= 0:
        return list(held)
    kept: list[tuple[int, int]] = []
    free_at: int | None = None
    for start, end in held:
        if free_at is not None and start < free_at:
            continue
        kept.append((start, end))
        free_at = end + horizon_bars + cooldown_bars
    return kept


def position_returns(
    episodes: list[tuple[int, int]], closes, config: "BacktestConfig"
) -> np.ndarray:
    """Net return of each position: entry close to exit close, one round trip.

    `episodes` index into `closes`. A position enters at its first bar and
    exits `horizon_bars` after its last, so `closes` must extend that far —
    positions whose exit falls off the end are dropped rather than priced at
    a bar that does not exist.
    """
    closes = np.asarray(closes, dtype=float)
    cost = 2 * (config.fee_per_side + config.slippage)
    out = []
    for start, end in episodes:
        exit_idx = end + config.horizon_bars
        if exit_idx >= len(closes) or start >= len(closes):
            continue
        out.append(closes[exit_idx] / closes[start] - 1.0 - cost)
    return np.asarray(out, dtype=float)


@dataclass
class FoldResult:
    fold: int
    rows: int
    positions: int
    # None, not NaN: a fold with no positions has no hit rate, and NaN is
    # neither JSON-valid nor equal to itself.
    hit_rate: float | None
    strategy_return: float
    buy_hold_return: float
    wins: int = 0
    # Fraction of this fold's bars spent in the market, and what a strategy
    # with no forecasting skill would have returned at that exposure and
    # turnover. Scored per fold on purpose: over years of bars a single
    # global null sits a point or two from buy-and-hold and discriminates
    # nothing, while a falling fold makes being flat worth something.
    exposure: float = 0.0
    null_return: float = 0.0


def run_backtest(bars: pd.DataFrame, config: BacktestConfig | None = None) -> dict:
    """Walk-forward over the bars; returns the summary dict (also what the
    CLI writes to JSON). Deterministic for fixed data + config."""
    config = config or BacktestConfig()
    features = build_features(bars, horizon_bars=config.horizon_bars)
    X, y = features.X, features.y
    n = len(X)
    fold_span = config.train_rows + config.horizon_bars + config.test_rows
    if n < fold_span:
        raise ValueError(
            f"not enough rows for one fold: have {n}, need {fold_span}"
        )

    # Closes live in bar space, which extends `horizon_bars` past the end of
    # X — a position opened on the last test bar exits at a real price rather
    # than being silently dropped.
    close_all = _close_series(bars)
    close_values = close_all.to_numpy()
    row_of = close_all.index.get_indexer(X.index)

    folds: list[FoldResult] = []
    fold_spans: list[tuple[int, int]] = []
    episodes: list[tuple[int, int]] = []
    entry_label: dict[int, int] = {}
    fold_of_entry: dict[int, int] = {}
    start = 0
    while start + fold_span <= n:
        train_end = start + config.train_rows
        # Purge: the last labels in the train window look up to horizon_bars
        # into the future — skip that many rows so nothing leaks into test.
        test_start = train_end + config.horizon_bars
        test_end = test_start + config.test_rows

        booster = lgb.train(
            _PARAMS,
            lgb.Dataset(X.iloc[start:train_end], label=y.iloc[start:train_end]),
            num_boost_round=_NUM_ROUNDS,
        )
        prob = booster.predict(X.iloc[test_start:test_end])
        y_test = y.iloc[test_start:test_end].to_numpy()

        # Long when confident, flat otherwise. A run of consecutive in-market
        # bars is ONE position — one entry, one exit, one round trip (KI-040)
        # — and a fresh signal arriving before that position realizes is the
        # same position continuing, not a second one to compound (KI-001).
        in_market = prob > config.entry_threshold
        close = _closes_for(bars, X.index[test_start:test_end])

        for local_start, local_end in find_episodes(in_market):
            entry = int(row_of[test_start + local_start])
            exit_bar = int(row_of[test_start + local_end])
            episodes.append((entry, exit_bar))
            entry_label[entry] = int(y_test[local_start])
            fold_of_entry[entry] = len(folds)

        fold_spans.append(
            (int(row_of[test_start]), int(row_of[test_end - 1]))
        )
        folds.append(
            FoldResult(
                fold=len(folds),
                rows=len(y_test),
                positions=0,
                hit_rate=None,
                strategy_return=0.0,
                buy_hold_return=float(close.iloc[-1] / close.iloc[0] - 1),
            )
        )
        start += config.test_rows

    # One merge pass over the whole walk-forward, not one per fold: fold test
    # windows are adjacent, so a position open at a seam must not be closed
    # and re-opened there.
    closed, pending = merge_overlapping_holds(episodes, config.horizon_bars)
    held = closed + ([pending] if pending is not None else [])
    # Merge first, then decline: merging is the KI-001 statement that capital
    # held continuously is one position, and the cooldown is the policy that
    # puts capital genuinely flat. Reversing them would re-book one hold as
    # two, which is the defect that rewrite fixed.
    held = apply_cooldown(held, config.horizon_bars, config.cooldown_bars)
    held = [
        position for position in held
        if position[1] + config.horizon_bars < len(close_values)
    ]

    returns = position_returns(held, close_values, config)
    all_returns = returns.tolist()
    for (entry, _), net in zip(held, returns):
        fold = folds[fold_of_entry[entry]]
        fold.positions += 1
        fold.strategy_return = (1 + fold.strategy_return) * (1 + net) - 1
        fold.wins += entry_label[entry]
    coverage = held_coverage(held, config.horizon_bars, len(close_values))
    cost = 2 * (config.fee_per_side + config.slippage)
    for fold, (lo, hi) in zip(folds, fold_spans):
        fold.hit_rate = fold.wins / fold.positions if fold.positions else None
        fold.exposure = float(coverage[lo : hi + 1].mean())
        fold.null_return = exposure_matched_return(
            fold.exposure, fold.buy_hold_return, fold.positions, cost
        )

    position_std, position_stderr = return_dispersion(all_returns)
    # E4 — the per-fold null above uses each fold's OWN exposure, so it
    # inherits the model's between-fold allocation, which is anti-skilled
    # (it is more exposed in falling folds than rising ones). Holding
    # exposure flat at the run's average isolates that: the pair separates
    # "chose when to be exposed" from merely "was exposed".
    mean_exposure = float(coverage.mean()) if len(coverage) else 0.0
    null_constant = float(np.prod([
        1 + exposure_matched_return(
            mean_exposure, f.buy_hold_return, f.positions, cost
        )
        for f in folds
    ]) - 1)

    equity = float(np.prod([1 + f.strategy_return for f in folds]))
    drawdown = _max_drawdown([f.strategy_return for f in folds])
    return {
        "config": asdict(config),
        "folds": [asdict(f) for f in folds],
        "n_folds": len(folds),
        # Renamed from `total_trades`, which counted in-market BARS. An old
        # artifact and a new one are not comparable, so they do not share a key.
        "total_positions": int(sum(f.positions for f in folds)),
        "fee_charges": int(sum(f.positions for f in folds)),
        "accounting": "position-level",
        "position_policy": "merge-while-open",
        "max_concurrent_positions": _max_concurrent(held, config.horizon_bars),
        # Position-weighted, not a mean of per-fold ratios: with 519 positions
        # over 253 folds, a fold holding one position must not weigh as much
        # as a fold holding forty. Renamed from `overall_hit_rate`, which was
        # that unweighted mean over in-market bars.
        "hit_rate_per_position": (
            float(sum(f.wins for f in folds) / sum(f.positions for f in folds))
            if any(f.positions for f in folds) else None
        ),
        "strategy_total_return": float(equity - 1),
        "buy_hold_total_return": float(
            np.prod([1 + f.buy_hold_return for f in folds]) - 1
        ),
        "max_fold_drawdown": drawdown,
        # Renamed from `avg_trade_return`: a "trade" used to be an in-market
        # bar, so the old key's value is not comparable to this one.
        "avg_position_return": float(np.mean(all_returns)) if all_returns else None,
        # E2 — the mean alone is unreadable. A +3.2 bps average over 1,120
        # positions is an edge or a rounding error depending entirely on how
        # wide the positions were, and the harness used to decline to say.
        "position_return_std": position_std,
        "position_return_stderr": position_stderr,
        # Item 2 of the model plan: the harness could not previously tell a
        # good model from a long one, because it reported neither how much of
        # the time it was in the market nor a benchmark matched to that.
        # Denominator is the full bar series, train region included — the
        # same one the plan's 82.0% was computed against, so the two are
        # directly comparable.
        "time_in_market": float(coverage.mean()) if len(coverage) else 0.0,
        "null_total_return": float(
            np.prod([1 + f.null_return for f in folds]) - 1
        ),
        "excess_vs_null": float(
            equity - np.prod([1 + f.null_return for f in folds])
        ),
        "null_constant_exposure": null_constant,
    }


def _closes_for(bars: pd.DataFrame, index: pd.Index) -> pd.Series:
    frame = bars.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index(pd.to_datetime(frame["timestamp"], utc=True))
    frame.columns = [str(c).lower() for c in frame.columns]
    return frame.loc[index, "close"].astype(float)




def _close_series(bars: pd.DataFrame) -> pd.Series:
    """The full close series in bar space, sorted — the price ladder every
    position is entered and exited against."""
    frame = bars.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index(pd.to_datetime(frame["timestamp"], utc=True))
    frame.columns = [str(c).lower() for c in frame.columns]
    return frame["close"].astype(float).sort_index()


def _max_concurrent(held: list[tuple[int, int]], horizon_bars: int) -> int:
    """How many positions were open at once, measured from the positions the
    equity path actually took. `skip-while-open` makes this 1; it is computed
    rather than asserted so that removing the selection shows up as a number."""
    if not held:
        return 0
    edges = []
    for entry, exit_bar in held:
        edges.append((entry, 1))
        edges.append((exit_bar + horizon_bars + 1, -1))
    edges.sort()
    open_now = peak = 0
    for _, delta in edges:
        open_now += delta
        peak = max(peak, open_now)
    return peak


def _max_drawdown(fold_returns: list[float]) -> float:
    equity = np.cumprod([1 + r for r in fold_returns])
    peaks = np.maximum.accumulate(equity)
    return float(((equity - peaks) / peaks).min()) if len(equity) else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="1m")
    args = parser.parse_args()

    bars = pd.DataFrame(get_price_bars(args.symbol, args.interval, TRAIN_BARS_LIMIT))
    results = run_backtest(bars)
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"backtest_{args.symbol.upper()}_{args.interval}.json"
    out.write_text(json.dumps(results, indent=2))
    print(json.dumps({k: v for k, v in results.items() if k != "folds"}, indent=2))
    print(f"\nfull results: {out}")


if __name__ == "__main__":
    main()
