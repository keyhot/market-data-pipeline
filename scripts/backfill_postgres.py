"""Replay data/raw/*.csv snapshots into Postgres through the upsert writers.

Rerunnable: every write is an idempotent upsert, so a second run changes no
row counts. Snapshots are replayed oldest-first so the latest fetch wins for
overlapping bars.

Usage: python scripts/backfill_postgres.py [--data-root PATH]
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import postgres_store  # noqa: E402
from storage.db import ping  # noqa: E402

# Filename format (storage/naming.py): {SYMBOL}_{kind}_{timestamp}.csv where
# SYMBOL may itself contain underscores (sanitized BRK.B -> BRK_B).
_TIMESTAMP_PARTS = 2

_EVENT_KINDS = postgres_store.EVENT_TYPES + ("actions",)


_REJECTIONS: dict[Path, str] = {}


def _reject(path: Path, reason: str) -> None:
    _REJECTIONS[path] = reason


def backfill(data_root: Path) -> dict[str, int]:
    written = {"price_bars": 0, "corporate_events": 0, "news_items": 0}
    _REJECTIONS.clear()
    skipped = []

    for path in _snapshots_oldest_first(data_root / "tickers"):
        symbol, _ = _parse_name(path)
        df = _read_csv(path)
        if df is None or not {"Open", "Close"}.issubset(df.columns):
            if df is not None:
                _reject(path, "no Open/Close columns")
            skipped.append(path)
            continue
        written["price_bars"] += postgres_store.upsert_price_bars(
            symbol, postgres_store.BAR_INTERVAL, df
        )

    for path in _snapshots_oldest_first(data_root / "events"):
        symbol, event_type = _parse_name(path)
        df = _read_csv(path)
        if df is None or event_type not in _EVENT_KINDS:
            if df is not None:
                _reject(path, f"unknown event kind {event_type!r}")
            skipped.append(path)
            continue
        written["corporate_events"] += postgres_store.upsert_events_snapshot(
            symbol, event_type, df
        )

    for path in _snapshots_oldest_first(data_root / "news"):
        symbol, _ = _parse_name(path)
        try:
            df = pd.read_csv(path, index_col=0)
            df["published_at"] = pd.to_datetime(
                df["published_at"], utc=True, errors="coerce"
            )
        except Exception as e:
            _reject(path, f"unreadable: {type(e).__name__}: {e}")
            skipped.append(path)
            continue
        written["news_items"] += postgres_store.upsert_news(symbol, df)

    for path in skipped:
        reason = _REJECTIONS.get(path, "not a valid snapshot")
        print(f"skipped: {path} — {reason}")
    if skipped:
        print(f"skipped {len(skipped)} file(s); totals above exclude them")
    return written


def _snapshots_oldest_first(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.csv"), key=lambda p: p.stem.split("_")[-1])


def _parse_name(path: Path) -> tuple[str, str]:
    parts = path.stem.split("_")
    symbol = "_".join(parts[: -_TIMESTAMP_PARTS])
    kind = parts[-_TIMESTAMP_PARTS]
    return symbol, kind


def _read_csv(path: Path) -> pd.DataFrame | None:
    """Parse a snapshot, or record why it could not be and return None.

    Three different rejections used to collapse into one bare `None`, so the
    run reported "not a valid snapshot" for a file that might have been
    unreadable, wrongly indexed, or full of undateable rows. A backfill that
    quietly skips input is a backfill you cannot trust the totals of.
    """
    try:
        df = pd.read_csv(path, index_col=0)
    except Exception as e:
        _reject(path, f"unreadable: {type(e).__name__}: {e}")
        return None
    # A real snapshot has an ISO-date string index; anything else (e.g. the
    # default RangeIndex of a malformed file) is not ingestible. utc=True
    # collapses mixed DST offsets, which parse_dates chokes on.
    if df.index.dtype != object:
        _reject(path, f"index is {df.index.dtype}, not ISO-date strings")
        return None
    index = pd.to_datetime(df.index, utc=True, errors="coerce")
    if index.isna().any():
        bad = int(index.isna().sum())
        _reject(path, f"{bad} of {len(index)} timestamps unparseable")
        return None
    df.index = index
    return df


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

    written = backfill(args.data_root)
    for table, count in written.items():
        print(f"{table}: {count} rows upserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
