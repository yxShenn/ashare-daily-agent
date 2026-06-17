"""打分策略:横截面标准化(z-score)后加权求和。实盘与回测共用(DRY)。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .factors import FACTOR_NAMES


def score_cross_section(factor_df: pd.DataFrame, weights: dict) -> pd.Series:
    """对一个横截面(每行一只股,列为各因子原始值)打分。

    每个因子在截面内做 z-score,再按权重加权求和。
    """
    z = pd.DataFrame(index=factor_df.index)
    for c in FACTOR_NAMES:
        col = pd.to_numeric(factor_df.get(c), errors="coerce")
        sd = col.std()
        if sd and not np.isnan(sd):
            z[c] = (col - col.mean()) / sd
        else:
            z[c] = 0.0
    z = z.fillna(0.0)
    w = np.array([float(weights.get(c, 0.0)) for c in FACTOR_NAMES])
    return pd.Series(z[FACTOR_NAMES].values @ w, index=factor_df.index)
