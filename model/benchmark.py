"""The benchmark the strategy is actually scored against (model plan item 2).

A long-only strategy that is in the market 82-95% of the time and loses to
buy-and-hold has told you almost nothing: it may have no forecasting skill, or
it may simply have been long in a rising market and paid for the privilege.
The harness could not tell those apart, because it reported neither exposure
nor a benchmark matched to it.

These two functions are that benchmark. `exposure_matched_return` is a null
with **no forecasting skill in it at all** — hold the benchmark's return,
scaled by time in market, minus one round trip per position. On the published
BTCUSDT h=15 run it reproduces the -63.3% headline to 0.0 points, which is the
measurement the vault plan is built on. Beating buy-and-hold is not the bar;
beating this is.

Pure and DB-free, in the shape of `model.backtest.find_episodes` — see
vault `Docs/model-improvement-plan.md` §2.
"""

import numpy as np


def held_coverage(
    held: list[tuple[int, int]], horizon_bars: int, n_bars: int
) -> np.ndarray:
    """Per-bar mask of when capital was committed.

    A position is held from its entry bar through `horizon_bars` after its
    last signal bar. This is the ONE definition of "in the market": a fold's
    exposure is a slice of this array and the run's is its mean, so the two
    cannot drift apart.
    """
    covered = np.zeros(max(0, n_bars), dtype=bool)
    for start, end in held:
        lo = max(0, start)
        hi = min(n_bars - 1, end + horizon_bars)
        if hi >= lo:
            covered[lo : hi + 1] = True
    return covered


def time_in_market(
    held: list[tuple[int, int]], horizon_bars: int, n_bars: int
) -> float:
    """Fraction of the bars over which capital was committed.

    Overlapping holds are counted **once** — this is the fraction of
    wall-clock spent in the market, not a sum of holding periods, so it
    cannot exceed 1.0 and a hold running past the last bar is truncated
    rather than credited.
    """
    if n_bars <= 0:
        return 0.0
    return float(held_coverage(held, horizon_bars, n_bars).mean())


def exposure_matched_return(
    exposure: float, benchmark_return: float, positions: int, cost: float
) -> float:
    """What a strategy with **no skill** would have returned at this exposure
    and this turnover: the benchmark's return scaled by time in market, then
    charged one round trip per position.

    The toll compounds rather than accumulating — each round trip is charged
    on the capital the previous one left — so 519 trips at 22 bps is -68.1%,
    not -114%.
    """
    return (1.0 + exposure * benchmark_return) * (1.0 - cost) ** positions - 1.0
