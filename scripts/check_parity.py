"""Compare the latest CSV snapshot per symbol against Postgres price_bars.

Run before flipping storage defaults (Sprint 7 cutover): exit 0 means every
bar in the newest CSV snapshot of each symbol exists in Postgres with the
same close and volume.

Usage: python scripts/check_parity.py [--data-root PATH]
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_postgres import (  # noqa: E402
    _parse_name,
    _read_csv,
    _snapshots_oldest_first,
)
from storage import postgres_store  # noqa: E402
from storage.db import ping  # noqa: E402

_CLOSE_TOLERANCE = 1e-4


def compare_bars(symbol: str, csv_df: pd.DataFrame, stored: list[dict]) -> list[str]:
    """Every CSV bar must exist in `stored` with matching close and volume."""
    stored_by_ts = {pd.Timestamp(bar["timestamp"]): bar for bar in stored}
    mismatches = []
    for ts, row in csv_df.iterrows():
        bar = stored_by_ts.get(pd.Timestamp(ts))
        if bar is None:
            mismatches.append(f"{symbol} {ts.date()}: missing from Postgres")
            continue
        if abs(float(row["Close"]) - float(bar["close"])) > _CLOSE_TOLERANCE:
            mismatches.append(
                f"{symbol} {ts.date()}: close CSV={row['Close']} PG={bar['close']}"
            )
        elif int(row["Volume"]) != int(bar["volume"]):
            mismatches.append(
                f"{symbol} {ts.date()}: volume CSV={row['Volume']} PG={bar['volume']}"
            )
    return mismatches


def _latest_snapshot_per_symbol(tickers_dir: Path) -> dict[str, Path]:
    latest: dict[str, Path] = {}
    # oldest-first, so later assignments win = newest snapshot per symbol.
    for path in _snapshots_oldest_first(tickers_dir):
        symbol, _ = _parse_name(path)
        latest[symbol] = path
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "raw",
    )
    args = parser.parse_args()

    if not ping():
        print("Postgres is not reachable — is the container up? (docker compose up -d)")
        return 1

    all_mismatches: list[str] = []
    checked = 0
    for symbol, path in _latest_snapshot_per_symbol(args.data_root / "tickers").items():
        csv_df = _read_csv(path)
        if csv_df is None:
            print(f"skipped (not a valid snapshot): {path}")
            continue
        stored = postgres_store.get_price_bars(symbol, limit=len(csv_df) + 100)
        all_mismatches.extend(compare_bars(symbol, csv_df, stored))
        checked += 1

    for line in all_mismatches:
        print(f"MISMATCH: {line}")
    print(f"checked {checked} symbols, {len(all_mismatches)} mismatches")
    return 1 if all_mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
