"""pandas 小工具：布尔筛选的结果在类型检查器看来可能是 Series，这里统一标成 DataFrame。"""

from __future__ import annotations

from typing import Any, cast

import pandas as pd


def rows(df: pd.DataFrame, mask: Any) -> pd.DataFrame:
    return cast(pd.DataFrame, df.loc[mask])


def included(daily: pd.DataFrame) -> pd.DataFrame:
    """参与汇总的日子。"""
    return rows(daily, daily["included"])
