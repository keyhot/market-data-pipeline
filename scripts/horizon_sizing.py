"""E10 sizing, run before the pre-registration (vault `Docs/horizon-experiment.md`):
per horizon, mean |h-bar move|, the top-fifth hit that breaks even on a 22 bps
round trip, the SE of per-call gross, and the share of rows a cost-clearing
3-class label would call `flat` (|move| < 22 bps). Reads 2020–2021 only.

    poetry run python scripts/horizon_sizing.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.postgres_store import get_price_bars  # noqa: E402

COST_BPS = 22.0
TEST_DAYS = 4 * 365  # the four test years, for the independent-call count


def main() -> None:
    for symbol in ("BTCUSDT", "ETHUSDT"):
        bars = pd.DataFrame(get_price_bars(symbol, "15m", 10_000_000))
        stamps = pd.to_datetime(bars.timestamp, utc=True)
        keep = stamps < "2022-01-01"
        close = bars[keep].set_index(stamps[keep]).close.astype(float).sort_index()
        for h in (4, 16, 32, 96):
            r = (close.shift(-h) / close - 1).dropna() * 1e4
            e_abs = r.abs().mean()
            top = TEST_DAYS * 96 / h / 5
            print(f"{symbol} h={h:3d} E|r|={e_abs:6.1f}bp sd={r.std():6.1f} "
                  f"up={(r > 0).mean():.3f} breakeven_hit="
                  f"{0.5 + COST_BPS / 2 / e_abs:.3f} indep_top5th={top:7.0f} "
                  f"se_gross={r.std() / np.sqrt(top):5.1f}bp "
                  f"flat={(r.abs() < COST_BPS).mean():.3f}")


if __name__ == "__main__":
    main()
