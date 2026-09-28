"""E11 sizing, run before the pre-registration (vault
`Docs/cost-horizon-experiment.md`). Reads 2020–2021 only.

Per symbol at h = 4: the cost-class shares, and what perfect foresight earns
per position through E11's own gate, positions and drift null, so the
control's floor is measured rather than guessed. Two oracles:

- `top`: the score is the true forward return, so the gate takes the largest
  fifth of moves on each side, which is the ceiling;
- `class`: the score is the true cost class (+1/0/−1) plus seeded noise, so
  the gate takes a random fifth of the cost-clearing moves, which is roughly
  what a leaked feature does once P(up) − P(down) saturates.

    poetry run python scripts/cost_horizon_sizing.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from model.cost_horizon import gate_each_side  # noqa: E402
from model.cost_label import DOWN, FLAT, UP, cost_class  # noqa: E402
from model.horizon import drift_per_bar, forward_return, positions  # noqa: E402
from storage.postgres_store import get_price_bars  # noqa: E402

H = 4
COST = 0.0022


def main() -> None:
    for symbol in ("BTCUSDT", "ETHUSDT"):
        bars = pd.DataFrame(get_price_bars(symbol, "15m", 10_000_000))
        stamps = pd.to_datetime(bars.timestamp, utc=True)
        keep = stamps < "2022-01-01"
        close = bars[keep].set_index(stamps[keep]).close.astype(float).sort_index()
        ret = forward_return(close, H).dropna()
        cls = pd.Series(cost_class(ret, COST), index=ret.index)
        shares = {name: round(float((cls == k).mean()), 3)
                  for name, k in (("down", DOWN), ("flat", FLAT), ("up", UP))}
        print(f"{symbol} h={H} class shares {shares}  "
              f"E|r| {ret.abs().mean() * 1e4:.1f} bps")
        mu = drift_per_bar(close)
        noise = np.random.default_rng(7).uniform(-0.1, 0.1, len(ret))
        signed_cls = (cls == UP).astype(float) - (cls == DOWN).astype(float)
        for name, score in (("top", ret), ("class", signed_cls + noise)):
            oos = pd.DataFrame({"year": ret.index.year, "score": score,
                                "centre": 0.0}, index=ret.index)
            pos = positions(gate_each_side(oos), close, H, mu)
            by_year = (pos.groupby("year").skill.mean() * 1e4).round(1).to_dict()
            print(f"  oracle {name:5s}: {len(pos):,} positions, skill/pos "
                  f"{pos.skill.mean() * 1e4:+.1f} bps, by year {by_year}")


if __name__ == "__main__":
    main()
