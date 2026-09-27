"""E9: do funding and perp-premium features add ranking skill? (2026-09-27)

Pre-registered in the vault, `Docs/funding-experiment.md` — config, the
alignment check, the positive control and the verdict rule are fixed there,
and this script applies them rather than choosing them. Same harness as
E7/E8: expanding walk-forward on 15m bars, h = 1, test years 2022..2025;
2026 is never read (the caches stop at 2026-01-01, so it cannot be).

    poetry run python scripts/funding_report.py --symbol BTCUSDT --fetch
    poetry run python scripts/funding_report.py --symbol ETHUSDT
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cross_asset_report import _bars, delta_auc_ci, top_quintile_hit  # noqa: E402
from live_gate_report import HORIZON, TEST_YEARS, auc  # noqa: E402
from order_flow_report import top_quintile_gross_bps  # noqa: E402
from model.features import _normalize, build_features  # noqa: E402
from model.funding import (  # noqa: E402
    check_grid,
    funding_series,
    funding_features,
    kline_close,
    leaked_control,
    with_funding_features,
)
from model.train import _NUM_ROUNDS, _PARAMS  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent.parent / "model" / "artifacts"
START = datetime(2020, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 1, tzinfo=timezone.utc)  # E6: 2026 is never fetched
FAPI = "https://fapi.binance.com/fapi/v1"
_KLINE_PAGE = 1500
_FUNDING_PAGE = 1000
CONTROL_MIN_DELTA = 0.05
STAGE2_MIN_BPS = 11.0
E7_ROWS = 140_250
MIN_ROW_SHARE = 0.99


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _get(path: str, params: dict) -> list:
    response = requests.get(f"{FAPI}/{path}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def _page(path: str, params: dict, limit: int, stamp) -> list:
    """Walk a futures endpoint's page limit from START to END. `stamp` reads
    a row's time, so klines and funding rows share one pager."""
    rows: list = []
    cursor, end = _ms(START), _ms(END)
    while cursor < end:
        page = _get(path, {**params, "startTime": cursor, "limit": limit})
        if not page:
            break
        rows.extend(r for r in page if stamp(r) < end)
        next_cursor = stamp(page[-1]) + 1
        if next_cursor <= cursor or len(page) < limit:
            break
        cursor = next_cursor
        time.sleep(0.25)
    return rows


def _cache(kind: str, symbol: str) -> Path:
    return CACHE_DIR / f"{kind}_{symbol}_15m.csv"


def fetch(symbol: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    funding = _page("fundingRate", {"symbol": symbol}, _FUNDING_PAGE,
                    lambda r: int(r["fundingTime"]))
    series = {
        "funding": [[r["fundingTime"], r["fundingRate"]] for r in funding],
        "premium": _page("premiumIndexKlines",
                         {"symbol": symbol, "interval": "15m"}, _KLINE_PAGE,
                         lambda k: int(k[0])),
        "index": _page("indexPriceKlines", {"pair": symbol, "interval": "15m"},
                       _KLINE_PAGE, lambda k: int(k[0])),
    }
    for kind, rows in series.items():
        with _cache(kind, symbol).open("w", newline="") as fh:
            csv.writer(fh).writerows(rows)
        print(f"  cached {len(rows):,} {kind} rows -> {_cache(kind, symbol)}",
              flush=True)


def _load(kind: str, symbol: str) -> list[list]:
    with _cache(kind, symbol).open() as fh:
        return list(csv.reader(fh))


def collect(symbol: str) -> tuple[pd.DataFrame, dict, float]:
    bars = _bars(symbol)
    close = _normalize(bars)["close"]
    index_close = kline_close(_load("index", symbol))
    medians = check_grid(index_close, close)  # raises before any AUC exists
    feats = funding_features(funding_series(_load("funding", symbol)),
                             kline_close(_load("premium", symbol)), close.index)
    corr = float(feats["premium_1"].corr(np.log(close / close.shift(1))))

    own = build_features(bars, horizon_bars=HORIZON)
    X_arm, y = with_funding_features(own.X, own.y, feats)
    X_ctrl, _ = with_funding_features(own.X, own.y,
                                      leaked_control(feats, index_close))
    rows = X_arm.index.intersection(X_ctrl.index)  # same rows in every arm
    X_arm, X_ctrl, y = X_arm.loc[rows], X_ctrl.loc[rows], y.loc[rows]
    X_base = X_arm[list(own.X.columns)]
    forward = (close.shift(-HORIZON) / close - 1.0).loc[rows]

    years = rows.year
    chunks = []
    for year in TEST_YEARS:
        test = np.flatnonzero(years == year)
        train_end = test[0] - HORIZON
        row = {"year": year, "y": y.iloc[test].to_numpy(),
               "ret": forward.iloc[test].to_numpy()}
        for arm, X in (("base", X_base), ("funding", X_arm),
                       ("control", X_ctrl)):
            booster = lgb.train(
                _PARAMS, lgb.Dataset(X.iloc[:train_end], label=y.iloc[:train_end]),
                num_boost_round=_NUM_ROUNDS,
            )
            row[arm] = booster.predict(X.iloc[test])
        chunks.append(pd.DataFrame(row, index=rows[test]))
        print(f"  {year}: {train_end:,} train rows, {len(test):,} test", flush=True)
    return pd.concat(chunks), medians, corr


def report(symbol: str, oos: pd.DataFrame, medians: dict, corr: float) -> str:
    y = oos.y.to_numpy()
    point, low, high = delta_auc_ci(oos, arm="funding")
    control = auc(y, oos.control.to_numpy()) - auc(y, oos.base.to_numpy())
    rows_ok = len(oos) >= MIN_ROW_SHARE * E7_ROWS
    print(f"\n{symbol} + funding, {len(oos):,} predictions "
          f"(E7: {E7_ROWS:,}; valid: {rows_ok})")
    print(f"  grid |index/spot-1| median bps: lag-1 {medians[-1]:.2f}  "
          f"lag0 {medians[0]:.2f}  lag+1 {medians[1]:.2f}")
    print(f"  corr(premium_1, log_return_1) = {corr:+.3f}")
    print(f"  positive control ΔAUC {control:+.4f} "
          f"(must exceed +{CONTROL_MIN_DELTA})")
    print(f"  pooled AUC base {auc(y, oos.base.to_numpy()):.4f}"
          f"  funding {auc(y, oos.funding.to_numpy()):.4f}")
    print(f"  ΔAUC {point:+.4f}  95% CI [{low:+.4f}, {high:+.4f}]")
    positive_years = 0
    for year, g in oos.groupby("year"):
        d = auc(g.y.to_numpy(), g.funding.to_numpy()) - auc(g.y.to_numpy(),
                                                            g.base.to_numpy())
        positive_years += d > 0
        print(f"  {year}: ΔAUC {d:+.4f}   top-quintile hit base "
              f"{top_quintile_hit(g, 'base'):.4f} funding "
              f"{top_quintile_hit(g, 'funding'):.4f}")
    gross_base = top_quintile_gross_bps(oos, "base")
    gross_arm = top_quintile_gross_bps(oos, "funding")
    print(f"  top-quintile signed gross/call: base {gross_base:+.2f} bps, "
          f"funding {gross_arm:+.2f} bps (stage 2 needs >= {STAGE2_MIN_BPS})")
    if control <= CONTROL_MIN_DELTA or not rows_ok:
        verdict = "VOID"
    else:
        verdict = "PASS" if low > 0 and positive_years >= 3 else "FAIL"
    print(f"  verdict: {verdict} (CI above 0: {low > 0}; "
          f"positive years {positive_years}/4; control ok: "
          f"{control > CONTROL_MIN_DELTA}; rows ok: {rows_ok}; stage-2 trigger: "
          f"{verdict == 'PASS' and gross_arm >= STAGE2_MIN_BPS})")
    return verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, choices=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--fetch", action="store_true",
                        help="(re)build the funding/premium/index caches first")
    args = parser.parse_args()
    if args.fetch or not _cache("funding", args.symbol).exists():
        fetch(args.symbol)
    report(args.symbol, *collect(args.symbol))


if __name__ == "__main__":
    main()
