"""L1: does the on-air model's gated call make money at 15m? (2026-09-27)

Pre-registered in the vault, `Docs/pnl-15m-experiment.md` — config and
verdict rule are fixed there, and this script applies them rather than
choosing them. The harness is `run_backtest`, unchanged; expanding training
and the live gate come from `model.expanding.ExpandingGatedFold`. Test span
2022..2025; 2026 is never read.

    poetry run python scripts/pnl_15m_report.py --symbol BTCUSDT
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost_label_report import fold_verdict  # noqa: E402
from model.backtest import BacktestConfig, _close_series, run_backtest  # noqa: E402
from model.expanding import ExpandingGatedFold, signed_gross_returns  # noqa: E402
from model.features import build_features  # noqa: E402
from storage.postgres_store import get_price_bars  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"
HORIZON = 1
FIRST_TEST = pd.Timestamp("2022-01-01", tz="UTC")
REFIT = "Q"


def _bars(symbol: str) -> pd.DataFrame:
    bars = pd.DataFrame(get_price_bars(symbol, "15m", 10_000_000))
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    return bars[stamps < "2026-01-01"]  # 2026 is never read


def _config(bars: pd.DataFrame) -> BacktestConfig:
    X = build_features(bars, horizon_bars=HORIZON).X
    first = int(np.searchsorted(X.index, FIRST_TEST))
    # run_backtest's first test row is train_rows + horizon_bars.
    return BacktestConfig(horizon_bars=HORIZON, train_rows=first - HORIZON,
                          test_rows=200, entry_threshold=0.5)


def _mean_se(values: list[float]) -> tuple[float, float, int]:
    a = np.asarray(values, dtype=float)
    if len(a) < 2:
        return float("nan"), float("nan"), len(a)
    return float(a.mean()), float(a.std(ddof=1) / np.sqrt(len(a))), len(a)


def run(symbol: str) -> dict:
    bars = _bars(symbol)
    config = _config(bars)
    cost = 2 * (config.fee_per_side + config.slippage)
    out: dict = {"symbol": symbol, "cost_round_trip": cost}
    for arm, quantile in (("gated", None), ("ungated", 0.0)):
        kwargs = {} if quantile is None else {"gate_quantile": quantile}
        predictor = ExpandingGatedFold(period=REFIT, horizon_bars=HORIZON,
                                       **kwargs)
        result = run_backtest(bars, config, predict_fold=predictor)
        signed = signed_gross_returns(predictor.calls, _close_series(bars),
                                      HORIZON)
        out[arm] = {
            "backtest": {k: v for k, v in result.items() if k != "folds"},
            "folds_vs_null": fold_verdict(result["folds"]),
            "signed_gross": {side: _mean_se(v) for side, v in
                             (*signed.items(),
                              ("both", signed["long"] + signed["short"]))},
            "calls": int(len(predictor.calls)),
            "gated_calls": int(predictor.calls.gated.sum()),
        }
        print(f"  {symbol} {arm}: {result['n_folds']} folds, "
              f"{result['total_positions']:,} positions", flush=True)

    g = out["gated"]
    b, fv = g["backtest"], g["folds_vs_null"]
    lower = b["avg_position_return"] - 1.96 * b["position_return_stderr"]
    checks = {
        "net_avg_lower_bound_above_0": lower > 0,
        "beats_per_fold_null": b["strategy_total_return"] > b["null_total_return"],
        "beats_constant_null": (b["strategy_total_return"]
                                > b["null_constant_exposure"]),
        "folds_beat_null_z_gt_2": fv["z"] > 2,
    }
    out["checks"] = checks
    out["verdict"] = "PASS" if all(checks.values()) else "FAIL"
    return out


def report(r: dict) -> None:
    bps = 1e4
    print(f"\n=== {r['symbol']} — L1 verdict: {r['verdict']} ===")
    for arm in ("gated", "ungated"):
        a = r[arm]
        b, fv = a["backtest"], a["folds_vs_null"]
        tag = "" if arm == "gated" else "  (reference, no verdict)"
        print(f"\n[{arm}]{tag}  calls {a['calls']:,}, gated {a['gated_calls']:,}")
        print(f"  positions {b['total_positions']:,}  time in market "
              f"{b['time_in_market']:.1%}  hit/position "
              f"{b['hit_rate_per_position'] or float('nan'):.4f}")
        print(f"  net per position {b['avg_position_return'] * bps:+.2f} ± "
              f"{1.96 * b['position_return_stderr'] * bps:.2f} bps (95%)")
        print(f"  strategy {b['strategy_total_return']:+.2%}  "
              f"null/fold {b['null_total_return']:+.2%}  "
              f"null/const {b['null_constant_exposure']:+.2%}  "
              f"B&H {b['buy_hold_total_return']:+.2%}")
        print(f"  folds beating null {fv['beat']}/{fv['beat'] + fv['lose']} "
              f"(ties {fv['tie']}), z {fv['z']:+.2f}")
        for side, (mean, se, n) in a["signed_gross"].items():
            print(f"  signed gross {side:5s}: {mean * bps:+.2f} ± "
                  f"{1.96 * se * bps:.2f} bps over {n:,} positions "
                  f"(round trip {r['cost_round_trip'] * bps:.0f})")
    print("\n  checks: " + ", ".join(f"{k}={v}" for k, v in r["checks"].items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    args = parser.parse_args()
    result = run(args.symbol.upper())
    report(result)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"pnl_15m_{result['symbol']}.json"
    path.write_text(json.dumps(result, indent=2, default=float))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
