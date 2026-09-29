"""E12: do archive metrics (OI, long/short, taker ratio) and coarse book
depth add ranking skill on 15m perp bars? (P2, 2026-09-28)

Pre-registered in the vault, `Docs/archive-information-experiment.md` —
features, arms, the stamp-convention check, the positive controls and the
verdict rule are fixed there; this script applies them rather than choosing
them. Harness as E7-E9: expanding walk-forward, h = 1, on P1's perp kline
cache. 2026 is never fetched and never read.

    poetry run python scripts/archive_report.py --fetch            # rerunnable
    poetry run python scripts/archive_report.py --symbol BTCUSDT --control-only
    poetry run python scripts/archive_report.py --symbol BTCUSDT   # the arms
"""

import argparse
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from cross_asset_report import delta_auc_ci, top_quintile_hit  # noqa: E402
from live_gate_report import HORIZON, auc  # noqa: E402
from order_flow_report import top_quintile_gross_bps  # noqa: E402
from model.archive import (  # noqa: E402
    DEPTH_FEATURES,
    METRICS_FEATURES,
    check_stamp_convention,
    depth_features,
    leaked_control,
    metrics_features,
    parse_depth,
    parse_metrics,
    with_archive_features,
)
from model.features import _normalize, build_features  # noqa: E402
from model.train import _NUM_ROUNDS, _PARAMS  # noqa: E402
from report_results import write_result  # noqa: E402

SYMBOLS = ("BTCUSDT", "ETHUSDT")
ARCHIVE_DIR = ROOT / "data" / "archive" / "binance_um"
CACHE_DIR = ROOT / "model" / "artifacts"
VISION = ("https://data.binance.vision/data/futures/um/daily/{dataset}/"
          "{symbol}/{symbol}-{dataset}-{day}.zip")
WINDOWS = {  # 2026 is never fetched
    "metrics": pd.date_range("2020-09-01", "2025-12-31", freq="D"),
    "bookDepth": pd.date_range("2023-01-01", "2025-12-31", freq="D"),
}
ARMS = {  # arm -> (features, the feature the control replaces, test years)
    "metrics": (METRICS_FEATURES, "oi_change_16", (2023, 2024, 2025)),  # ETH starts 2021-12
    "depth": (DEPTH_FEATURES, "depth_change_4", (2024, 2025)),
}
CONTROL_MIN_DELTA = 0.05
MIN_GROSS_BPS = 6.0  # half of P1's perp_taker 12 bps round trip
MIN_ROW_SHARE = 0.99


def _zip_path(dataset: str, symbol: str, day: pd.Timestamp) -> Path:
    return (ARCHIVE_DIR / dataset / symbol
            / f"{symbol}-{dataset}-{day:%Y-%m-%d}.zip")


def _download(args) -> str:
    dataset, symbol, day = args
    path = _zip_path(dataset, symbol, day)
    if path.exists():
        return "cached"
    url = VISION.format(dataset=dataset, symbol=symbol, day=f"{day:%Y-%m-%d}")
    for _ in range(3):
        try:
            response = requests.get(url, timeout=60)
        except requests.RequestException:
            continue
        if response.status_code == 404:
            return "missing"
        if response.ok:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(response.content)
            tmp.rename(path)
            return "fetched"
    return "failed"


def _read_zip(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(archive.namelist()[0]).decode()


def _cache(dataset: str, symbol: str) -> Path:
    return CACHE_DIR / f"archive_{dataset}_{symbol}.csv"


def fetch() -> None:
    """Download every daily zip (skipping those on disk), then rebuild the
    reduced per-symbol caches the report reads."""
    jobs = [(d, s, day) for d, days in WINDOWS.items() for s in SYMBOLS
            for day in days]
    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(_download, jobs))
    tally = pd.Series(outcomes).value_counts().to_dict()
    missing = [f"{d}/{s}/{day:%Y-%m-%d}" for (d, s, day), o in zip(jobs, outcomes)
               if o in ("missing", "failed")]
    print(f"  archive: {tally}; missing/failed: {missing[:20]}", flush=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    parse = {"metrics": parse_metrics, "bookDepth": parse_depth}
    for dataset, days in WINDOWS.items():
        for symbol in SYMBOLS:
            paths = [p for p in (_zip_path(dataset, symbol, d) for d in days)
                     if p.exists()]
            frame = pd.concat(parse[dataset](_read_zip(p)) for p in paths)
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            frame.to_csv(_cache(dataset, symbol), index_label="stamp")
            print(f"  cached {len(frame):,} {dataset} rows -> "
                  f"{_cache(dataset, symbol)}", flush=True)


def _load(dataset: str, symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(_cache(dataset, symbol), index_col="stamp")
    frame.index = pd.to_datetime(frame.index, utc=True)
    return frame[frame.index < "2026-01-01"]


def _perp_bars(symbol: str) -> pd.DataFrame:
    bars = pd.read_csv(CACHE_DIR / f"perp_klines_{symbol}_15m.csv")
    bars = bars.drop_duplicates("timestamp").sort_values("timestamp")
    stamps = pd.to_datetime(bars["timestamp"], utc=True)
    return bars[stamps < "2026-01-01"].reset_index(drop=True)


def _walk_forward(arms: dict, y: pd.Series, forward: pd.Series,
                  years: tuple) -> pd.DataFrame:
    rows = y.index
    chunks = []
    for year in years:
        test = np.flatnonzero(rows.year == year)
        train_end = test[0] - HORIZON
        row = {"year": year, "y": y.iloc[test].to_numpy(),
               "ret": forward.iloc[test].to_numpy()}
        for name, X in arms.items():
            booster = lgb.train(
                _PARAMS, lgb.Dataset(X.iloc[:train_end], label=y.iloc[:train_end]),
                num_boost_round=_NUM_ROUNDS)
            row[name] = booster.predict(X.iloc[test])
        chunks.append(pd.DataFrame(row, index=rows[test]))
        print(f"    {year}: {train_end:,} train rows, {len(test):,} test",
              flush=True)
    return pd.concat(chunks)


def collect(symbol: str, arm: str, control_only: bool) -> tuple[pd.DataFrame, dict]:
    bars = _perp_bars(symbol)
    close = _normalize(bars)["close"]
    info: dict = {}
    if arm == "metrics":
        metrics = _load("metrics", symbol)
        info = check_stamp_convention(metrics, close)  # before any AUC
        feats = metrics_features(metrics, close.index, info["delay"])
    else:
        feats = depth_features(_load("bookDepth", symbol), close.index)
    names, control_col, years = ARMS[arm]

    own = build_features(bars, horizon_bars=HORIZON)
    X_arm, y = with_archive_features(own.X, own.y, feats)
    X_ctrl, _ = with_archive_features(
        own.X, own.y, leaked_control(feats, close, control_col))
    rows = X_arm.index.intersection(X_ctrl.index)
    X_arm, X_ctrl, y = X_arm.loc[rows], X_ctrl.loc[rows], y.loc[rows]
    X_base = X_arm[list(own.X.columns)]
    forward = (close.shift(-HORIZON) / close - 1.0).loc[rows]

    base_rows = own.X.index[own.X.index.year.isin(years)]
    info["row_share"] = rows.year.isin(years).sum() / len(base_rows)
    arms = {"base": X_base, "control": X_ctrl}
    if not control_only:
        arms[arm] = X_arm
    return _walk_forward(arms, y, forward, years), info


def report(symbol: str, arm: str, oos: pd.DataFrame, info: dict) -> dict:
    y = oos.y.to_numpy()
    control = auc(y, oos.control.to_numpy()) - auc(y, oos.base.to_numpy())
    out = {"symbol": symbol, "arm": arm, "control": control,
           "row_share": info["row_share"], "predictions": len(oos)}
    print(f"\n{symbol} / {arm}: {len(oos):,} predictions, "
          f"row share {info['row_share']:.4f} (needs >= {MIN_ROW_SHARE})")
    if "delay" in info:
        print(f"  stamp convention: corr(taker, bar return) close-row "
              f"{info['corr_close_row']:+.3f} vs open-row "
              f"{info['corr_open_row']:+.3f} -> delay {info['delay']}")
    print(f"  positive control ΔAUC {control:+.4f} "
          f"(must exceed +{CONTROL_MIN_DELTA})")
    if arm not in oos:
        write_result(
            f"archive_{symbol}_{arm}",
            inputs={"symbol": symbol, "arm": arm, "rows": len(oos),
                   "row_share": info["row_share"]},
            parameters={"control_min_delta": CONTROL_MIN_DELTA},
            statistic={"control_delta_auc": control},
            ci=None,
            verdict="CONTROL_ONLY",
        )
        return out
    point, low, high = delta_auc_ci(oos, arm=arm)
    gross = top_quintile_gross_bps(oos, arm)
    print(f"  pooled AUC base {auc(y, oos.base.to_numpy()):.4f}  "
          f"{arm} {auc(y, oos[arm].to_numpy()):.4f}")
    print(f"  ΔAUC {point:+.4f}  95% CI [{low:+.4f}, {high:+.4f}]")
    for year, g in oos.groupby("year"):
        d = auc(g.y.to_numpy(), g[arm].to_numpy()) - auc(g.y.to_numpy(),
                                                         g.base.to_numpy())
        print(f"  {year}: ΔAUC {d:+.4f}  top-quintile hit base "
              f"{top_quintile_hit(g, 'base'):.4f} {arm} "
              f"{top_quintile_hit(g, arm):.4f}")
    print(f"  top-quintile gross/call: base "
          f"{top_quintile_gross_bps(oos, 'base'):+.2f} bps, {arm} {gross:+.2f} "
          f"bps (needs >= {MIN_GROSS_BPS})")
    valid = control > CONTROL_MIN_DELTA and info["row_share"] >= MIN_ROW_SHARE
    passed = low > 0 and gross >= MIN_GROSS_BPS
    out.update(delta=point, low=low, high=high, gross=gross,
               verdict="VOID" if not valid else ("PASS" if passed else "FAIL"))
    print(f"  symbol verdict: {out['verdict']}")
    write_result(
        f"archive_{symbol}_{arm}",
        inputs={"symbol": symbol, "arm": arm, "rows": len(oos),
               "row_share": info["row_share"]},
        parameters={"control_min_delta": CONTROL_MIN_DELTA,
                    "min_gross_bps": MIN_GROSS_BPS,
                    "min_row_share": MIN_ROW_SHARE},
        statistic={"delta_auc": point, "control_delta_auc": control,
                  "gross_bps": gross},
        ci={"low": low, "high": high},
        verdict=out["verdict"],
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=SYMBOLS)
    parser.add_argument("--arm", choices=tuple(ARMS), action="append")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--control-only", action="store_true",
                        help="score base and the leaked control, never the arm")
    args = parser.parse_args()
    if args.fetch:
        fetch()
    if args.symbol:
        for arm in args.arm or tuple(ARMS):
            report(args.symbol, arm,
                   *collect(args.symbol, arm, args.control_only))


if __name__ == "__main__":
    main()
