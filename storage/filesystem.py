from pathlib import Path

import pandas as pd

RETENTION_COUNT = 5
# Timestamp suffix appended to filenames: "_" + "2024-01-01T12-00-00Z" = 21 chars
_TIMESTAMP_SUFFIX_LEN = 21


def save_csv(path: Path, df: pd.DataFrame, keep: int = RETENTION_COUNT) -> None:
    ensure_dir(path.parent)
    df.to_csv(path)
    _prune_files(path, keep)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _prune_files(path: Path, keep: int) -> None:
    prefix = path.stem[:-_TIMESTAMP_SUFFIX_LEN]
    matches = sorted(path.parent.glob(f"{prefix}_*.csv"))
    for old in matches[: max(0, len(matches) - keep)]:
        old.unlink()
