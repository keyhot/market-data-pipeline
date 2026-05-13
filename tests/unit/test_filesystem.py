from pathlib import Path

import pandas as pd

from storage.filesystem import RETENTION_COUNT, save_csv


def make_csv(directory: Path, name: str) -> Path:
    path = directory / name
    save_csv(path, pd.DataFrame({"v": [1]}), keep=RETENTION_COUNT)
    return path


def test_files_below_retention_are_kept(tmp_path):
    for i in range(RETENTION_COUNT):
        make_csv(tmp_path, f"AAPL_1d_2024-01-0{i + 1}T00-00-00Z.csv")

    assert len(list(tmp_path.glob("AAPL_1d_*.csv"))) == RETENTION_COUNT


def test_oldest_file_pruned_when_limit_exceeded(tmp_path):
    for i in range(RETENTION_COUNT):
        make_csv(tmp_path, f"AAPL_1d_2024-01-0{i + 1}T00-00-00Z.csv")

    oldest = tmp_path / "AAPL_1d_2024-01-01T00-00-00Z.csv"
    newest = tmp_path / f"AAPL_1d_2024-01-0{RETENTION_COUNT + 1}T00-00-00Z.csv"
    make_csv(tmp_path, newest.name)

    assert not oldest.exists()
    assert newest.exists()
    assert len(list(tmp_path.glob("AAPL_1d_*.csv"))) == RETENTION_COUNT


def test_pruning_does_not_affect_other_keys(tmp_path):
    for i in range(RETENTION_COUNT + 1):
        make_csv(tmp_path, f"AAPL_1d_2024-01-0{i + 1}T00-00-00Z.csv")
        make_csv(tmp_path, f"AAPL_1mo_2024-01-0{i + 1}T00-00-00Z.csv")

    assert len(list(tmp_path.glob("AAPL_1d_*.csv"))) == RETENTION_COUNT
    assert len(list(tmp_path.glob("AAPL_1mo_*.csv"))) == RETENTION_COUNT


def test_keep_zero_deletes_all(tmp_path):
    for i in range(3):
        path = tmp_path / f"AAPL_1d_2024-01-0{i + 1}T00-00-00Z.csv"
        save_csv(path, pd.DataFrame({"v": [1]}), keep=0)

    assert len(list(tmp_path.glob("AAPL_1d_*.csv"))) == 0
