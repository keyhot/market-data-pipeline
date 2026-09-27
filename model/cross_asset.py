"""Cross-asset features (experiment E7, vault `Docs/cross-asset-experiment.md`):
the other symbol's v0 features, joined on the bar timestamp. Its bar at t
closes at the same instant as the symbol's own, so nothing past t is read."""

import pandas as pd

from model.features import _compute_features, _normalize


def with_cross_features(
    X: pd.DataFrame, y: pd.Series, other_bars: pd.DataFrame
) -> tuple[pd.DataFrame, pd.Series]:
    """X plus the other symbol's features as `x_*`. Rows where the other
    symbol has no complete feature row are dropped, never filled."""
    other = _compute_features(_normalize(other_bars)).add_prefix("x_")
    joined = X.join(other, how="inner").dropna()
    return joined, y.loc[joined.index]
