"""按证券汇总（规范第 4 节）：中位数为主，另给均值、分位数、标准差、有效天数。"""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pandas as pd

from trader.analytics.frames import included
from trader.analytics.params import ksuffix

# 不参与汇总的列（价格本身没有跨证券比较的意义）
_SKIP = {"open", "high", "low", "close", "adr", "raw_open", "cost_pct", "pm_high", "pm_low"}
_FIXED = {
    "symbol", "date", "benchmark", "early_close", "possible_halt", "adr_ok", "included",
    "anomaly_flag", "anomaly_reason",
}  # fmt: skip


def metric_columns(daily: pd.DataFrame) -> list[str]:
    fixed = _FIXED
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


def means(daily: pd.DataFrame) -> pd.DataFrame:
    d = included(daily)
    return cast(pd.DataFrame, d.groupby("symbol")[metric_columns(d)].mean())


def capturable_days_ratio(daily: pd.DataFrame, k: float) -> pd.Series:
    """swing_capturable_pct（阈值 k）> 0 的天数占比。"""
    d = included(daily)
    col = d[f"swing_capturable_pct_{ksuffix(k)}"]
    hit = (col > 0).astype(float).where(col.notna())
    return hit.groupby(d["symbol"]).mean()


def extremes(daily: pd.DataFrame, k: float, n: int = 3) -> pd.DataFrame:
    """每只证券振幅最大、body_ratio 最高（通常是财报或重大消息日）、未确认的最后一段最长的 n 天。"""
    d = included(daily).copy()
    leg = f"last_leg_adr_{ksuffix(k)}"
    d["abs_last_leg_adr"] = d[leg].abs()
    rows = []
    for sym, g in d.groupby("symbol"):
        for metric in ("range_pct", "body_ratio", "abs_last_leg_adr"):
            for _, r in cast(pd.DataFrame, g).nlargest(n, columns=metric).iterrows():
                rows.append(
                    {
                        "symbol": sym,
                        "by": metric,
                        "date": r["date"],
                        "range_pct": r["range_pct"],
                        "body_ratio": r["body_ratio"],
                        "last_leg_adr": r[leg],
                        "gap_adr": r["gap_adr"],
                    }
                )
    return pd.DataFrame(rows)


def bucket_medians(buckets: pd.DataFrame, value: str = "rv_share") -> pd.DataFrame:
    """行为证券，列为时间桶（距开盘分钟数），值为各交易日的中位数。"""
    if buckets.empty:
        return pd.DataFrame()
    d = included(buckets)
    return d.pivot_table(index="symbol", columns="bucket_minute", values=value, aggfunc="median")


def ratios_vs(
    med: pd.DataFrame, mean: pd.DataFrame, days_ratio: pd.Series, k: float, ref: str = "SPY"
) -> tuple[pd.DataFrame, list[str]]:
    """各证券相对参照证券的比值（规范第 4 节）。body_ratio、range_adr、swing_n 用中位数之比，
    swing_capturable_pct 用均值之比，capturable_days_ratio 直接相除。分母为 0 或缺失时为空，
    并返回原因说明。"""
    s = ksuffix(k)
    tag = ref.lower()
    srcs: list[tuple[str, Any]] = [
        ("body_ratio", med["body_ratio"]),
        ("range_adr", med["range_adr"]),
        (f"swing_n_{s}", med[f"swing_n_{s}"]),
        (f"swing_capturable_pct_{s}_mean", mean[f"swing_capturable_pct_{s}"]),
        ("capturable_days_ratio", days_ratio.reindex(med.index)),
    ]
    out = pd.DataFrame(index=med.index)
    if ref not in med.index:
        for name, _ in srcs:
            out[f"{name}_vs_{tag}"] = np.nan
        return out, [f"本次计算没有 {ref}，*_vs_{tag} 都为空"]
    notes: list[str] = []
    for name, col in srcs:
        base = float(cast(Any, col).get(ref, np.nan)) if ref in col.index else np.nan
        if not np.isfinite(base) or base == 0:
            out[f"{name}_vs_{tag}"] = np.nan
            why = "没有数据" if not np.isfinite(base) else "为 0"
            notes.append(f"{name}_vs_{tag} 为空：{ref} 的{name}{why}，不能作分母")
        else:
            out[f"{name}_vs_{tag}"] = col / base
    return out, notes


PathClose = dict[str, dict[Any, tuple[np.ndarray, np.ndarray]]]  # 证券 → 日期 → (ts, 5 分钟收盘价)


def return_corr(
    a: dict[Any, tuple[np.ndarray, np.ndarray]], b: dict[Any, tuple[np.ndarray, np.ndarray]]
) -> float:
    """两只证券 5 分钟对数收益率的相关系数：每天只用两边都有的 bar，收益率不跨日。"""
    ra, rb = [], []
    for day in sorted(set(a) & set(b)):
        ta, ca = a[day]
        tb, cb = b[day]
        common, ia, ib = np.intersect1d(ta, tb, return_indices=True)
        if len(common) < 3:
            continue
        ra.append(np.diff(np.log(ca[ia])))
        rb.append(np.diff(np.log(cb[ib])))
    if not ra:
        return float("nan")
    x, y = np.concatenate(ra), np.concatenate(rb)
    if x.std() == 0 or y.std() == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def leveraged_table(
    daily: pd.DataFrame,
    med: pd.DataFrame,
    mean: pd.DataFrame,
    path_close: PathClose,
    pairs: dict[str, tuple[str, float]],
    k: float,
) -> tuple[pd.DataFrame, list[str]]:
    """杠杆 ETF 与标的并排对照（规范第 6 节第 9 项）。两只都有数据的对才列出。"""
    s = ksuffix(k)
    rows = []
    notes = []
    d = included(daily)
    for lev, (und, mult) in pairs.items():
        lev, und = lev.upper(), und.upper()
        if lev not in med.index or und not in med.index:
            missing = [x for x in (lev, und) if x not in med.index]
            notes.append(f"杠杆对照 {lev}/{und}：{'、'.join(missing)} 不在本次计算的证券里，未列出")
            continue
        both = d[d["symbol"].isin([lev, und])].pivot_table(
            index="date", columns="symbol", values="range_pct"
        )
        both = both.dropna()
        ratio = (both[lev] / both[und]).median() if len(both) else float("nan")
        row: dict[str, Any] = {"pair": f"{lev} / {und}", "leverage": mult}
        for name, src, col in (
            ("range_pct", med, "range_pct"),
            ("body_ratio", med, "body_ratio"),
            ("swing_n", med, f"swing_n_{s}"),
            ("efficiency", med, "efficiency"),
            ("capturable_mean", mean, f"swing_capturable_pct_{s}"),
            ("dollar_vol", med, "dollar_vol"),
        ):
            row[f"{name}_lev"] = src.at[lev, col]
            row[f"{name}_und"] = src.at[und, col]
        row["range_ratio"] = ratio
        row["corr_5m"] = return_corr(path_close.get(lev, {}), path_close.get(und, {}))
        rows.append(row)
    if not rows:
        return pd.DataFrame(), notes
    return pd.DataFrame(rows).set_index("pair"), notes
