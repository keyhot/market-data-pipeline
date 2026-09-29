"""E8: do order-flow features add ranking skill? (2026-09-27)

Pre-registered in the vault, `Docs/order-flow-experiment.md` — config, the
positive control and the verdict rule are fixed there, and this script
applies them rather than choosing them. Same harness as E7: expanding
walk-forward on 15m bars, h = 1, test years 2022..2025; 2026 is never read
(the flow cache stops at 2026-01-01, so it cannot be).

    poetry run python scripts/order_flow_report.py --symbol BTCUSDT --fetch
    poetry run python scripts/order_flow_report.py --symbol ETHUSDT
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cross_asset_report import _bars, delta_auc_ci, top_quintile_hit  # noqa: E402
from live_gate_report import HORIZON, TEST_YEARS, auc  # noqa: E402
from ingestion.binance_provider import BinanceProvider  # noqa: E402
from model.features import build_features  # noqa: E402
from model.order_flow import (  # noqa: E402
    align_flow,
    flow_features,
    flow_frame,
    leaked_control,
    with_flow_features,
)
from model.train import _NUM_ROUNDS, _PARAMS  # noqa: E402
from report_results import write_result  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"
START = datetime(2020, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, tzinfo=timezone.utc)  # E6: 2026 is never fetched
CONTROL_MIN_DELTA = 0.05
STAGE2_MIN_BPS = 11.0
E7_ROWS = 140_250


def _cache(symbol: str) -> Path:
    return CACHE_DIR / f"flow_{symbol}_15m.csv"


def fetch(symbol: str) -> None:
    raw = BinanceProvider().get_klines_paginated(
        symbol, "15m", int(START.timestamp() * 1000), int(END.timestamp() * 1000)
    )
    flow_frame(raw)  # validate the field map before anything is written
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _cache(symbol).open("w", newline="") as fh:
        csv.writer(fh).writerows(raw)
    print(f"  cached {len(raw):,} klines -> {_cache(symbol)}", flush=True)


def _load_raw(symbol: str) -> list[list]:
    with _cache(symbol).open() as fh:
        return [[int(r[0]), *r[1:]] for r in csv.reader(fh)]


def collect(symbol: str) -> tuple[pd.DataFrame, float]:
    bars = _bars(symbol)
    flow = align_flow(flow_frame(_load_raw(symbol)), bars)
    close = pd.Series(bars["close"].astype(float).to_numpy(),
                      index=pd.to_datetime(bars["timestamp"], utc=True))
    feats = flow_features(flow, close)
    corr = float(feats["flow_imbalance_1"].corr(np.log(close / close.shift(1))))

    own = build_features(bars, horizon_bars=HORIZON)
    X_flow, y = with_flow_features(own.X, own.y, feats)
    X_ctrl, _ = with_flow_features(own.X, own.y, leaked_control(feats))
    rows = X_flow.index.intersection(X_ctrl.index)  # same rows in every arm
    X_flow, X_ctrl, y = X_flow.loc[rows], X_ctrl.loc[rows], y.loc[rows]
    X_base = X_flow[list(own.X.columns)]
    forward = (close.shift(-HORIZON) / close - 1.0).loc[rows]

    years = rows.year
    chunks = []
    for year in TEST_YEARS:
        test = np.flatnonzero(years == year)
        train_end = test[0] - HORIZON
        row = {"year": year, "y": y.iloc[test].to_numpy(),
               "ret": forward.iloc[test].to_numpy()}
        for arm, X in (("base", X_base), ("flow", X_flow), ("control", X_ctrl)):
            booster = lgb.train(
                _PARAMS, lgb.Dataset(X.iloc[:train_end], label=y.iloc[:train_end]),
                num_boost_round=_NUM_ROUNDS,
            )
            row[arm] = booster.predict(X.iloc[test])
        chunks.append(pd.DataFrame(row, index=rows[test]))
        print(f"  {year}: {train_end:,} train rows, {len(test):,} test", flush=True)
    return pd.concat(chunks), corr


def top_quintile_gross_bps(oos: pd.DataFrame, arm: str) -> float:
    """Signed gross per call in the top confidence fifth, pooled — the
    pre-registered money proxy and stage-2 trigger."""
    conf = (oos[arm] - 0.5).abs()
    top = oos[conf >= conf.quantile(0.8)]
    return float((np.sign(top[arm] - 0.5) * top.ret).mean() * 1e4)


def report(symbol: str, oos: pd.DataFrame, corr: float) -> str:
    y = oos.y.to_numpy()
    point, low, high = delta_auc_ci(oos, arm="flow")
    control = auc(y, oos.control.to_numpy()) - auc(y, oos.base.to_numpy())
    print(f"\n{symbol} + order flow, {len(oos):,} predictions (E7: {E7_ROWS:,})")
    print(f"  corr(flow_imbalance_1, log_return_1) = {corr:+.3f}")
    print(f"  positive control ΔAUC {control:+.4f} "
          f"(must exceed +{CONTROL_MIN_DELTA})")
    print(f"  pooled AUC base {auc(y, oos.base.to_numpy()):.4f}"
          f"  flow {auc(y, oos.flow.to_numpy()):.4f}")
    print(f"  ΔAUC {point:+.4f}  95% CI [{low:+.4f}, {high:+.4f}]")
    positive_years = 0
    for year, g in oos.groupby("year"):
        d = auc(g.y.to_numpy(), g.flow.to_numpy()) - auc(g.y.to_numpy(),
                                                         g.base.to_numpy())
        positive_years += d > 0
        print(f"  {year}: ΔAUC {d:+.4f}   top-quintile hit base "
              f"{top_quintile_hit(g, 'base'):.4f} flow "
              f"{top_quintile_hit(g, 'flow'):.4f}")
    gross_base = top_quintile_gross_bps(oos, "base")
    gross_flow = top_quintile_gross_bps(oos, "flow")
    print(f"  top-quintile signed gross/call: base {gross_base:+.2f} bps, "
          f"flow {gross_flow:+.2f} bps (stage 2 needs >= {STAGE2_MIN_BPS})")
    if control <= CONTROL_MIN_DELTA:
        verdict = "VOID"
    else:
        verdict = "PASS" if low > 0 and positive_years >= 3 else "FAIL"
    print(f"  verdict: {verdict} (CI above 0: {low > 0}; "
          f"positive years {positive_years}/4; control ok: "
          f"{control > CONTROL_MIN_DELTA}; stage-2 trigger: "
          f"{verdict == 'PASS' and gross_flow >= STAGE2_MIN_BPS})")
    write_result(
        f"order_flow_{symbol}",
        inputs={"symbol": symbol, "rows": len(oos), "e7_rows": E7_ROWS,
               "corr_flow_imbalance_1": corr},
        parameters={"control_min_delta": CONTROL_MIN_DELTA,
                    "stage2_min_bps": STAGE2_MIN_BPS},
        statistic={"delta_auc": point, "control_delta_auc": control,
                  "positive_years": positive_years, "gross_base_bps": gross_base,
                  "gross_flow_bps": gross_flow},
        ci={"low": low, "high": high},
        verdict=verdict,
        extra={"stage2_trigger": verdict == "PASS" and gross_flow >= STAGE2_MIN_BPS},
    )
    return verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, choices=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--fetch", action="store_true",
                        help="(re)build the flow cache from Binance first")
    args = parser.parse_args()
    if args.fetch or not _cache(args.symbol).exists():
        fetch(args.symbol)
    report(args.symbol, *collect(args.symbol))


if __name__ == "__main__":
    main()
