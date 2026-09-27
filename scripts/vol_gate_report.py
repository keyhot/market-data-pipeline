"""Is v0 worth listening to when a move could pay for itself? Vol-gate experiment.

Pre-registered 2026-09-27, before any gated number was seen:
  gate      `volatility_60` (a v0 feature) in the top 30/20/10% of each fold's
            TRAIN window — the cutoff never sees the test bars
  arms      v0 (the cached walk-forward p, identical rows to
            confidence_report.py) behind each gate, and the no-model control
            `always_long` behind the same gates, so "the gate helps" and "v0
            helps inside the gate" are separate answers
  scoring   `run_backtest` at the default entry threshold, against the
            exposure-matched null — the only bar that counts

Usage:
    poetry run python scripts/vol_gate_report.py --symbol BTCUSDT
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from model.backtest import BacktestConfig, run_backtest  # noqa: E402
from model.gates import always_long, volatility_gate  # noqa: E402
from model.zero_shot import cached_predictor  # noqa: E402
from storage.postgres_store import get_price_bars  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"
GATE_COLUMN = "volatility_60"
FRACTIONS = (1.0, 0.3, 0.2, 0.1)  # 1.0 = no gate


def _covering_bars(symbol: str, interval: str, oos: pd.DataFrame,
                   config: BacktestConfig) -> pd.DataFrame:
    bars = pd.DataFrame(get_price_bars(symbol, interval, 10_000_000))
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    last = oos.ts.max() + pd.Timedelta(minutes=config.horizon_bars)
    return bars[stamps <= last].reset_index(drop=True)


def run(symbol: str, interval: str) -> pd.DataFrame:
    config = BacktestConfig()
    oos = pd.read_csv(CACHE_DIR / f"oos_{symbol}_{interval}.csv", parse_dates=["ts"])
    bars = _covering_bars(symbol, interval, oos, config)
    v0 = cached_predictor(oos.set_index("ts")["p"])
    rows = []
    for name, inner in (("v0", v0), ("always_long", always_long)):
        for fraction in FRACTIONS:
            r = run_backtest(bars, config, predict_fold=volatility_gate(
                inner, GATE_COLUMN, fraction))
            avg = r["avg_position_return"]
            rows.append({
                "arm": name,
                "gate": "none" if fraction >= 1.0 else f"top {fraction:.0%}",
                "positions": r["total_positions"],
                "in_market": r["time_in_market"],
                "hit": r["hit_rate_per_position"],
                "avg_pos_bp": avg * 1e4 if avg is not None else float("nan"),
                "strategy": r["strategy_total_return"],
                "null": r["null_total_return"],
                "excess": r["excess_vs_null"],
            })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="1m")
    args = parser.parse_args()
    table = run(args.symbol.upper(), args.interval)
    print(f"\n{args.symbol.upper()} {args.interval} — vol gate on {GATE_COLUMN}, "
          f"entry p > {BacktestConfig().entry_threshold}, 22 bps round trip")
    print(table.to_string(index=False, float_format=lambda v: f"{v:8.3f}"))


if __name__ == "__main__":
    main()
