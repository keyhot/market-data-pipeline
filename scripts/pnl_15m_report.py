"""L1: does the on-air model's gated call make money at 15m? (2026-09-27)

Pre-registered in the vault, `Docs/pnl-15m-experiment.md` — config and
verdict rule are fixed there, and this script applies them rather than
choosing them. The harness is `run_backtest`, unchanged; expanding training
and the live gate come from `model.expanding.ExpandingGatedFold`. Test span
2022..2025; 2026 is never read.

    poetry run python scripts/pnl_15m_report.py --symbol BTCUSDT

P1 (2026-09-28, vault `Docs/cost-profiles-p1.md`): `--market perp` reruns the
gated arm on Binance USDⓈ-M perp klines — the model trains on the price it
trades — and scores that one set of calls under every named cost profile.
`--fetch` caches the klines from data.binance.vision first (2020-01..2025-12,
the spot span; 2026 is never fetched).

    poetry run python scripts/pnl_15m_report.py --symbol BTCUSDT --market perp --fetch
"""

import argparse
import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

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
VISION = ("https://data.binance.vision/data/futures/um/monthly/klines/"
          "{symbol}/15m/{symbol}-15m-{month}.zip")
MONTHS = pd.period_range("2020-01", "2025-12", freq="M")  # 2026 never fetched
PERP_PROFILES = ("perp_taker", "perp_maker", "spot_taker")


def _bars(symbol: str) -> pd.DataFrame:
    bars = pd.DataFrame(get_price_bars(symbol, "15m", 10_000_000))
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    return bars[stamps < "2026-01-01"]  # 2026 is never read


def _perp_cache(symbol: str) -> Path:
    return RESULTS_DIR / f"perp_klines_{symbol}_15m.csv"


def parse_vision_klines(text: str) -> list[list]:
    """[timestamp, open, high, low, close, volume] rows from one monthly kline
    CSV. Newer files carry a header row and older ones do not; open times are
    ms, or µs in any file that moved to µs."""
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if not rec or not rec[0].strip().isdigit():
            continue  # the header row
        open_time = int(rec[0])
        if open_time > 10**14:
            open_time //= 1000
        stamp = pd.Timestamp(open_time, unit="ms", tz="UTC").isoformat()
        rows.append([stamp, *rec[1:6]])
    return rows


def fetch_perp(symbol: str) -> None:
    rows: list[list] = []
    for month in MONTHS:
        url = VISION.format(symbol=symbol, month=month.strftime("%Y-%m"))
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            rows += parse_vision_klines(
                archive.read(archive.namelist()[0]).decode())
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with _perp_cache(symbol).open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        writer.writerows(rows)
    print(f"  cached {len(rows):,} perp 15m bars -> {_perp_cache(symbol)}",
          flush=True)


def _perp_bars(symbol: str) -> pd.DataFrame:
    bars = pd.read_csv(_perp_cache(symbol))
    bars = bars.drop_duplicates("timestamp").sort_values("timestamp")
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    return bars[stamps < "2026-01-01"].reset_index(drop=True)


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


class Replay:
    """Record a predictor's per-fold output once, then replay it. The calls
    do not depend on cost, so every profile scores the same calls — and the
    16-refit walk-forward is paid once, not once per profile."""

    def __init__(self, predictor):
        self._predictor = predictor
        self._seen: dict[int, np.ndarray] = {}

    def __call__(self, X, y, train: slice, test: slice) -> np.ndarray:
        if test.start not in self._seen:
            self._seen[test.start] = np.asarray(
                self._predictor(X, y, train, test))
        return self._seen[test.start]


def run_perp(symbol: str) -> dict:
    bars = _perp_bars(symbol)
    base = _config(bars)
    predictor = ExpandingGatedFold(period=REFIT, horizon_bars=HORIZON)
    replay = Replay(predictor)
    out: dict = {"symbol": symbol, "market": "perp", "bars": len(bars)}
    for name in PERP_PROFILES:
        config = BacktestConfig.for_profile(
            name, horizon_bars=base.horizon_bars, train_rows=base.train_rows,
            test_rows=base.test_rows, entry_threshold=base.entry_threshold)
        result = run_backtest(bars, config, predict_fold=replay)
        out[name] = {
            "cost_round_trip": 2 * (config.fee_per_side + config.slippage),
            "backtest": {k: v for k, v in result.items() if k != "folds"},
            "folds_vs_null": fold_verdict(result["folds"]),
        }
        print(f"  {symbol} perp {name}: {result['total_positions']:,} of "
              f"{result['positions_attempted']:,} positions", flush=True)
    signed = signed_gross_returns(predictor.calls, _close_series(bars),
                                  HORIZON)
    out["signed_gross"] = {side: _mean_se(v) for side, v in
                           (*signed.items(),
                            ("both", signed["long"] + signed["short"]))}
    out["calls"] = int(len(predictor.calls))
    out["gated_calls"] = int(predictor.calls.gated.sum())
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


def report_perp(r: dict) -> None:
    bps = 1e4
    print(f"\n=== {r['symbol']} perp — {r['bars']:,} bars, "
          f"{r['calls']:,} test calls, gated {r['gated_calls']:,} ===")
    for side, (mean, se, n) in r["signed_gross"].items():
        print(f"  signed gross {side:5s}: {mean * bps:+.2f} ± "
              f"{1.96 * se * bps:.2f} bps over {n:,} positions")
    for name in PERP_PROFILES:
        b = r[name]["backtest"]
        cost = r[name]["cost_round_trip"]
        # Gross of the positions actually taken — for the maker profile,
        # the filled subset, which selection can move off the unconditional.
        gross = b["avg_position_return"] + cost
        print(f"  [{name} {cost * bps:.0f} bps] positions "
              f"{b['total_positions']:,}/{b['positions_attempted']:,} "
              f"(fill {b['fill_rate']:.1%})  long gross {gross * bps:+.2f}  "
              f"net {b['avg_position_return'] * bps:+.2f} ± "
              f"{1.96 * b['position_return_stderr'] * bps:.2f} bps (95%)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--market", choices=("spot", "perp"), default="spot")
    parser.add_argument("--fetch", action="store_true",
                        help="perp: cache klines from data.binance.vision")
    args = parser.parse_args()
    symbol = args.symbol.upper()
    if args.market == "perp":
        if args.fetch:
            fetch_perp(symbol)
        result = run_perp(symbol)
        report_perp(result)
        suffix = "_perp"
    else:
        result = run(symbol)
        report(result)
        suffix = ""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"pnl_15m_{result['symbol']}{suffix}.json"
    path.write_text(json.dumps(result, indent=2, default=float))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
