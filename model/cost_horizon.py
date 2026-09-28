"""E11: a cost-clearing 3-class label at h = 4 with expanding training
(vault `Docs/cost-horizon-experiment.md`).

E1 tested this label only on 800-row folds and at h = 16, where it is nearly
the sign label. E10 left it open at h = 4: v0's AUC peaks there, a third of
rows are `flat`, and misses run larger than hits, which is exactly what a
magnitude label targets. Positions, the drift null and the CI are E10's
(`model/horizon.py`); only the label, the score and the gate differ.
"""

import numpy as np
import pandas as pd

from model.cost_label import DOWN, UP, cost_class
from model.horizon import GATE_QUANTILE


def three_class_oos(X: pd.DataFrame, ret: pd.Series, cost: float, h: int,
                    years, fit) -> pd.DataFrame:
    """One model per test year on the cost classes of every row before it,
    less an h-row purge. The score is P(up) − P(down); its centre is the
    training rows' up share minus their down share, known at prediction
    time. `expanding_oos` would centre on the mean class code, which is not a
    base rate, so this is a sibling rather than a caller."""
    cls = pd.Series(cost_class(ret, cost), index=ret.index)
    chunks = []
    for year in years:
        test = np.flatnonzero(X.index.year == year)
        train_end = test[0] - h
        train_cls = cls.iloc[:train_end].to_numpy()
        model = fit(X.iloc[:train_end], train_cls)
        proba = np.asarray(model.predict(X.iloc[test]), dtype=float)
        chunks.append(pd.DataFrame({
            "year": year,
            "score": proba[:, UP] - proba[:, DOWN],
            "argmax": proba.argmax(axis=1),
            "y": (ret.iloc[test] > 0).astype(int).to_numpy(),
            "cls": cls.iloc[test].to_numpy(),
            "ret": ret.iloc[test].to_numpy(),
            "centre": float((train_cls == UP).mean() - (train_cls == DOWN).mean()),
        }, index=X.index[test]))
    return pd.concat(chunks)


def gate_each_side(oos: pd.DataFrame) -> pd.Series:
    """Side ∈ {−1, 0, 1}: the top fifth of |score − centre| *within* each
    side's rows, per year. One pooled gate lets a saturated score put every
    call on one side (E10's control went 100% short that way)."""
    offset = oos.score - oos.centre
    side = pd.Series(0, index=oos.index)
    for sign in (1, -1):
        mine = np.sign(offset) == sign
        conf = offset[mine].abs()
        if conf.empty:
            continue
        cutoff = conf.groupby(oos.year[mine]).transform(
            lambda c: c.quantile(GATE_QUANTILE))
        side[conf.index[conf >= cutoff]] = sign
    return side


def mechanism(side: pd.Series, ret: pd.Series, cost: float) -> dict:
    """What a magnitude label is for, read on the calls: the hit rate, how
    much larger misses are than hits (E10: 1.10–1.22 for the sign label), and
    the share of calls whose move clears the round trip in the called
    direction."""
    called = side != 0
    s, r = side[called], ret[called]
    signed = s * r
    right = signed > 0
    return {
        "calls": int(called.sum()),
        "hit": float(right.mean()),
        "miss_hit_ratio": float(r[~right].abs().mean() / r[right].abs().mean()),
        "clearing_hit": float((signed > cost).mean()),
    }
