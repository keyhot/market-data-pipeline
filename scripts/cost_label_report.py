"""E1 — does a cost-aware 3-class label find direction v0's sign label can't?

Pre-registered 2026-09-27 in the vault (`Docs/cost-label-experiment.md`),
before any number was seen:
  data      15m bars, 2020-01-01 -> 2025-12-31 (2026 held out, plan E6)
  arms      binary (v0 as-is, same span), 3class (argmax = up beyond the
            22 bps round trip), oracle (the true class — must differ)
  scoring   run_backtest at horizon 16, cooldown 0 and 64; each arm's
            predictions are computed once and replayed for both cooldowns
  verdict   folds beating their own per-fold null, ties apart, sign-test z
            over non-tied folds; pass = z > 2 in all four cells

Usage:
    OMP_NUM_THREADS=4 poetry run python scripts/cost_label_report.py --symbol BTCUSDT
"""

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from model.backtest import BacktestConfig, _lightgbm_fold, run_backtest  # noqa: E402
from model.cost_label import (  # noqa: E402
    DOWN,
    FLAT,
    UP,
    cost_class,
    cost_label_fold,
    forward_returns,
    oracle_fold,
    round_trip_cost,
)
from model.features import build_features  # noqa: E402
from model.zero_shot import cached_predictor  # noqa: E402
from storage.postgres_store import get_price_bars  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"
INTERVAL = "15m"
HORIZON = 16
SELECTION_END = pd.Timestamp("2025-12-31 23:45", tz="UTC")
COOLDOWNS = (0, 64)
TIE = 1e-12
MIN_NON_TIED = 300


def recording(inner):
    """Wrap a predictor so each test bar's output is kept for replay."""
    store: dict = {}

    def predict_fold(X, y, train, test):
        out = np.asarray(inner(X, y, train, test), dtype=float)
        store.update(zip(X.index[test], out))
        return out
    predict_fold.store = store
    return predict_fold


def fold_verdict(folds: list[dict]) -> dict:
    beat = sum(f["strategy_return"] - f["null_return"] > TIE for f in folds)
    lose = sum(f["null_return"] - f["strategy_return"] > TIE for f in folds)
    m = beat + lose
    z = (beat - m / 2) / (math.sqrt(m) / 2) if m else float("nan")
    return {"beat": beat, "lose": lose, "tie": len(folds) - m, "z": z}


def _row(arm: str, cooldown: int, r: dict) -> dict:
    avg, se = r["avg_position_return"], r["position_return_stderr"]
    return {
        "arm": arm, "cooldown": cooldown,
        "positions": r["total_positions"],
        "in_market": r["time_in_market"],
        "avg_pos_bp": avg * 1e4 if avg is not None else float("nan"),
        "se_bp": se * 1e4 if se is not None else float("nan"),
        "strategy": r["strategy_total_return"],
        "null_fold": r["null_total_return"],
        "null_const": r["null_constant_exposure"],
        "buy_hold": r["buy_hold_total_return"],
        **fold_verdict(r["folds"]),
    }


def run(symbol: str) -> tuple[pd.DataFrame, dict]:
    bars = pd.DataFrame(get_price_bars(symbol, INTERVAL, 10_000_000))
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    bars = bars[stamps <= SELECTION_END].reset_index(drop=True)
    base = BacktestConfig(horizon_bars=HORIZON)
    cost = round_trip_cost(base)
    X = build_features(bars, horizon_bars=HORIZON).X
    fwd = forward_returns(bars, HORIZON, X.index)

    classes = cost_class(fwd, cost)
    plumbing = {
        "rows": len(X), "first": str(X.index[0]), "last": str(X.index[-1]),
        "cost": cost,
        "class_share": {name: float((classes == c).mean())
                        for name, c in (("down", DOWN), ("flat", FLAT), ("up", UP))},
    }

    arms = {
        "oracle": oracle_fold(fwd, cost),
        "binary": _lightgbm_fold,
        "3class": cost_label_fold(fwd, cost),
    }
    rows = []
    for arm, inner in arms.items():
        rec = recording(inner)
        first = run_backtest(bars, BacktestConfig(horizon_bars=HORIZON,
                                                  cooldown_bars=COOLDOWNS[0]),
                             predict_fold=rec)
        rows.append(_row(arm, COOLDOWNS[0], first))
        replay = cached_predictor(pd.Series(rec.store))
        for cooldown in COOLDOWNS[1:]:
            r = run_backtest(bars, BacktestConfig(horizon_bars=HORIZON,
                                                  cooldown_bars=cooldown),
                             predict_fold=replay)
            rows.append(_row(arm, cooldown, r))
        p = np.fromiter(rec.store.values(), dtype=float)
        plumbing[f"fire_rate_{arm}"] = float((p > base.entry_threshold).mean())
        print(f"  {arm} done", flush=True)
    return pd.DataFrame(rows), plumbing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    symbol = args.symbol.upper()
    table, plumbing = run(symbol)
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"cost_label_{symbol}_{INTERVAL}.json"
    out.write_text(json.dumps({"plumbing": plumbing,
                               "rows": table.to_dict(orient="records")}, indent=2))
    print(f"\n{symbol} {INTERVAL} h={HORIZON}, through {SELECTION_END:%Y-%m-%d}")
    print(json.dumps(plumbing, indent=2))
    print(table.to_string(index=False, float_format=lambda v: f"{v:8.3f}"))
    print(f"\nfull results: {out}")


if __name__ == "__main__":
    main()
