"""按证券汇总（规范第 4 节）：中位数为主，另给均值、分位数、标准差、有效天数。"""

from __future__ import annotations

from typing import cast

import pandas as pd

from trader.analytics.frames import included
from trader.analytics.params import ReportParams, ksuffix

# 不参与汇总的列（价格本身没有跨证券比较的意义）
_SKIP = {"open", "high", "low", "close", "atr", "cost_pct"}


def metric_columns(daily: pd.DataFrame) -> list[str]:
    fixed = {"symbol", "date", "benchmark", "early_close", "possible_halt", "atr_ok", "included"}
    return [
        c
        for c in daily.columns
        if c not in fixed and c not in _SKIP and pd.api.types.is_numeric_dtype(daily[c])
    ]


def stats(daily: pd.DataFrame) -> pd.DataFrame:
    """长表：索引 (symbol, metric)，列 median mean p25 p75 std days。只用 included 的日子。"""
    d = included(daily)
    cols = metric_columns(d)
    g = d.groupby("symbol")[cols]
    parts = {
        "median": g.median(),
        "mean": g.mean(),
        "p25": g.quantile(0.25),
        "p75": g.quantile(0.75),
        "std": g.std(),
        "days": g.count(),
    }
    out = pd.concat({k: v.stack(future_stack=True) for k, v in parts.items()}, axis=1)
    out.index.names = ["symbol", "metric"]
    return out


def medians(daily: pd.DataFrame) -> pd.DataFrame:
    d = included(daily)
    return cast(pd.DataFrame, d.groupby("symbol")[metric_columns(d)].median())


def extremes(daily: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """每只证券振幅最大的 n 天和 body_ratio 最高的 n 天（通常是财报或重大消息日）。"""
    d = included(daily)
    rows = []
    for sym, g in d.groupby("symbol"):
        for metric in ("range_pct", "body_ratio"):
            for _, r in cast(pd.DataFrame, g).nlargest(n, columns=metric).iterrows():
                rows.append(
                    {
                        "symbol": sym,
                        "by": metric,
                        "date": r["date"],
                        "range_pct": r["range_pct"],
                        "body_ratio": r["body_ratio"],
                        "gap_atr": r["gap_atr"],
                    }
                )
    return pd.DataFrame(rows)


def bucket_medians(buckets: pd.DataFrame, value: str = "rv_share") -> pd.DataFrame:
    """行为证券，列为时间桶（距开盘分钟数），值为各交易日的中位数。"""
    if buckets.empty:
        return pd.DataFrame()
    d = included(buckets)
    return d.pivot_table(index="symbol", columns="bucket_minute", values=value, aggfunc="median")


def ratio_columns(p: ReportParams) -> list[str]:
    s = ksuffix(p.score_k)
    return ["body_ratio", f"swing_n_{s}", f"swing_capturable_pct_{s}", "range_atr"]


def ratios_vs(med: pd.DataFrame, cols: list[str], ref: str = "SPY") -> pd.DataFrame:
    """各证券中位数 / 参照证券中位数。"""
    out = pd.DataFrame(index=med.index)
    for c in cols:
        base = med.at[ref, c] if ref in med.index and c in med.columns else float("nan")
        out[f"{c}_vs_{ref.lower()}"] = med[c] / base if base and base == base else float("nan")
    return out
