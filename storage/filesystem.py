import pandas as pd
from pathlib import Path

def save_csv(path: Path, df: pd.DataFrame) -> None:
    ensure_dir(path.parent)
    df.to_csv(path)

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)