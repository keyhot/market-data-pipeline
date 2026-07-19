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


@dataclass
class FoldResult:
    fold: int
    rows: int
    trades: int
    hit_rate: float
    strategy_return: float
    buy_hold_return: float


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

    folds: list[FoldResult] = []
    equity = 1.0
    all_returns: list[float] = []
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

        # Long when confident, flat otherwise; each entry+exit pays fees and
        # slippage on both sides.
        in_market = prob > config.entry_threshold
        close = _closes_for(bars, X.index[test_start:test_end])
        fwd_return = _forward_returns(bars, X.index[test_start:test_end],
                                      config.horizon_bars)
        cost = 2 * (config.fee_per_side + config.slippage)
        trade_returns = np.where(in_market, fwd_return - cost, 0.0)

        trades = int(in_market.sum())
        wins = int(((prob > config.entry_threshold) & (y_test == 1)).sum())
        fold_return = float(np.prod(1 + trade_returns) - 1)
        buy_hold = float(close.iloc[-1] / close.iloc[0] - 1)
        folds.append(
            FoldResult(
                fold=len(folds),
                rows=len(y_test),
                trades=trades,
                hit_rate=wins / trades if trades else float("nan"),
                strategy_return=fold_return,
                buy_hold_return=buy_hold,
            )
        )
        equity *= 1 + fold_return
        all_returns.extend(trade_returns[in_market].tolist())
        start += config.test_rows

    drawdown = _max_drawdown([f.strategy_return for f in folds])
    hit_rates = [f.hit_rate for f in folds if not np.isnan(f.hit_rate)]
    return {
        "config": asdict(config),
        "folds": [asdict(f) for f in folds],
        "n_folds": len(folds),
        "total_trades": int(sum(f.trades for f in folds)),
        "overall_hit_rate": float(np.mean(hit_rates)) if hit_rates else None,
        "strategy_total_return": float(equity - 1),
        "buy_hold_total_return": float(
            np.prod([1 + f.buy_hold_return for f in folds]) - 1
        ),
        "max_fold_drawdown": drawdown,
        "avg_trade_return": float(np.mean(all_returns)) if all_returns else None,
    }


def _closes_for(bars: pd.DataFrame, index: pd.Index) -> pd.Series:
    frame = bars.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index(pd.to_datetime(frame["timestamp"], utc=True))
    frame.columns = [str(c).lower() for c in frame.columns]
    return frame.loc[index, "close"].astype(float)


def _forward_returns(
    bars: pd.DataFrame, index: pd.Index, horizon_bars: int
) -> np.ndarray:
    frame = bars.copy()
    if "timestamp" in frame.columns:
        frame = frame.set_index(pd.to_datetime(frame["timestamp"], utc=True))
    frame.columns = [str(c).lower() for c in frame.columns]
    close = frame["close"].astype(float).sort_index()
    fwd = close.shift(-horizon_bars) / close - 1.0
    return fwd.loc[index].to_numpy()


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
