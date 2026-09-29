"""Downloads Binance SPOT kline monthly archives (data.binance.vision) for
BTCUSDT and ETHUSDT, intervals 1d and 1h, from each symbol's spot listing
month (2017-08) through the latest complete UTC calendar month. Every zip
is verified against its sibling `.CHECKSUM` before being kept; an on-disk
zip is re-verified on rerun rather than trusted to still be good, and only
re-fetched if it fails. Falls back to that month's daily zips (rare — every
month through the latest complete one exists as of 2026-09) if a monthly
zip 404s.

Data prerequisite for the E16 trend and E15b vol-managed experiments (not
yet pre-registered). This script only moves archive bytes into
data/archive/binance_spot/klines/<SYMBOL>/<interval>/ — it computes no
return, volatility, trend signal or other performance statistic.

    poetry run python scripts/fetch_spot_klines.py
    poetry run python scripts/fetch_spot_klines.py --dest /path/to/klines --symbols BTCUSDT
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from model.spot_archive import coverage, load_spot_klines, verify_checksum  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = ROOT / "data" / "archive" / "binance_spot" / "klines"
LISTING = {"BTCUSDT": "2017-08", "ETHUSDT": "2017-08"}
INTERVALS = ("1d", "1h")
MONTHLY = ("https://data.binance.vision/data/spot/monthly/klines/"
           "{symbol}/{interval}/{symbol}-{interval}-{month}.zip")
DAILY = ("https://data.binance.vision/data/spot/daily/klines/"
         "{symbol}/{interval}/{symbol}-{interval}-{day}.zip")


def last_complete_month(now: pd.Timestamp | None = None) -> pd.Period:
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    return now.to_period("M") - 1


def _get(url: str) -> requests.Response | None:
    """The response, or None on a real 404. Retries transient errors; raises
    if every attempt fails for a non-404 reason."""
    last_exc = None
    for _ in range(3):
        try:
            response = requests.get(url, timeout=60)
        except requests.RequestException as exc:
            last_exc = exc
            continue
        if response.status_code == 404:
            return None
        if response.ok:
            return response
        last_exc = RuntimeError(f"{response.status_code} for {url}")
    raise RuntimeError(f"failed after 3 attempts: {url}") from last_exc


def _fetch_and_verify(url: str, dest: Path, name: str) -> str:
    """Downloads `url` + `.CHECKSUM`, verifies, and writes both files (zip
    via a .part-then-rename so a partial download is never mistaken for a
    complete one) only once verification passes. Returns cached / fetched /
    missing / checksum-failed."""
    zip_path = dest / name
    checksum_path = dest / f"{name}.CHECKSUM"
    if zip_path.exists() and checksum_path.exists():
        if verify_checksum(zip_path.read_bytes(), checksum_path.read_text(), name):
            return "cached"
        zip_path.unlink()
        checksum_path.unlink(missing_ok=True)
    response = _get(url)
    if response is None:
        return "missing"
    checksum_response = _get(url + ".CHECKSUM")
    if checksum_response is None:
        return "missing"  # zip exists but no checksum to verify against — skip
    if not verify_checksum(response.content, checksum_response.text, name):
        return "checksum-failed"
    dest.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_suffix(".part")
    tmp.write_bytes(response.content)
    tmp.rename(zip_path)
    checksum_path.write_text(checksum_response.text)
    return "fetched"


def _fetch_month(symbol: str, interval: str, month: pd.Period, dest: Path) -> str:
    name = f"{symbol}-{interval}-{month}.zip"
    outcome = _fetch_and_verify(
        MONTHLY.format(symbol=symbol, interval=interval, month=month), dest, name)
    if outcome != "missing":
        return outcome
    # Fall back to daily zips for every day in this month.
    days = pd.period_range(month.start_time, month.end_time, freq="D")
    outcomes = [_fetch_day(symbol, interval, day, dest) for day in days]
    if all(o in ("fetched", "cached") for o in outcomes):
        return "fetched-daily"
    return "missing"


def _fetch_day(symbol: str, interval: str, day: pd.Period, dest: Path) -> str:
    day_str = day.strftime("%Y-%m-%d")
    name = f"{symbol}-{interval}-{day_str}.zip"
    return _fetch_and_verify(
        DAILY.format(symbol=symbol, interval=interval, day=day_str), dest, name)


def fetch(dest: Path, symbols: tuple[str, ...], intervals: tuple[str, ...]) -> None:
    end = last_complete_month()
    jobs = []
    for symbol in symbols:
        for interval in intervals:
            months = pd.period_range(LISTING[symbol], end, freq="M")
            for month in months:
                jobs.append((symbol, interval, month))
    print(f"  {len(jobs)} monthly targets, through {end}", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(
            lambda j: _fetch_month(j[0], j[1], j[2], dest / j[0] / j[1]), jobs))
    tally = pd.Series(outcomes).value_counts().to_dict()
    failed = [f"{s}/{i}/{m}" for (s, i, m), o in zip(jobs, outcomes) if o == "missing"]
    print(f"  outcomes: {tally}")
    if failed:
        print(f"  MISSING (no monthly or daily zip verified): {failed}")


def report(dest: Path, symbols: tuple[str, ...], intervals: tuple[str, ...]) -> None:
    for symbol in symbols:
        for interval in intervals:
            frame = load_spot_klines(dest, symbol, interval)
            if frame.empty:
                print(f"  {symbol} {interval}: no data on disk")
                continue
            end = last_complete_month().end_time
            if interval == "1h":
                end = end.floor("h")
            else:
                end = end.floor("D")
            cov = coverage(frame, interval, frame.index[0], end)
            print(f"\n{symbol} {interval}: {len(frame):,} rows, "
                  f"{frame.index[0]} .. {frame.index[-1]}")
            print(cov.to_string())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--symbols", nargs="+", default=list(LISTING),
                        choices=list(LISTING))
    parser.add_argument("--intervals", nargs="+", default=list(INTERVALS),
                        choices=list(INTERVALS))
    parser.add_argument("--report-only", action="store_true",
                        help="skip fetching, just print coverage for what's on disk")
    args = parser.parse_args()
    symbols, intervals = tuple(args.symbols), tuple(args.intervals)
    if not args.report_only:
        fetch(args.dest, symbols, intervals)
    report(args.dest, symbols, intervals)


if __name__ == "__main__":
    main()
