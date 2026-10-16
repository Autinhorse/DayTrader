"""pandas 小工具：布尔筛选的结果在类型检查器看来可能是 Series，这里统一标成 DataFrame。"""

from __future__ import annotations

from typing import Any, cast

import pandas as pd


def rows(df: pd.DataFrame, mask: Any) -> pd.DataFrame:
    return cast(pd.DataFrame, df.loc[mask])


def included(daily: pd.DataFrame) -> pd.DataFrame:
    """参与汇总的日子。"""
    return rows(daily, daily["included"])


def col(df: pd.DataFrame, name: str) -> pd.Series:
    return cast(pd.Series, df[name])


def corr(a: Any, b: Any, rank: bool = False) -> float:
    """两列的相关系数；rank=True 时先转名次（Spearman）。不需要 scipy。"""
    x, y = cast(pd.Series, a), cast(pd.Series, b)
    if rank:
        x, y = x.rank(), y.rank()
    return float(cast(Any, x.corr(y)))
