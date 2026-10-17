"""排名稳定性（决策 0014）：把一份长区间报表按季度（或月、年）切开，每段单独打分排名，
看哪些证券一直靠前、哪些只是某一段突出。

    rep = build_report(tickers, "2024-01-02", "2026-10-15")
    pr = period_rankings(rep)            # 每段每只证券一行
    stability_summary(pr)                # 每只证券一行：中位名次、进前 N 的段数占比……
    adjacent_rank_corr(pr)               # 相邻两段的名次相关系数

每段都用同一套打分（scoring.score），只用该段里参与汇总的日子；ADR、摆动等每日指标在全区间里连续计算，
不受分段影响。分数是段内的相对分数（段内证券数量可能不同），名次也只在段内比较。
"""

from __future__ import annotations

from typing import cast

import pandas as pd

from trader.analytics import aggregate, scoring
from trader.analytics.frames import col, corr, included, rows
from trader.analytics.params import ksuffix
from trader.analytics.pipeline import VolReport

FREQS = {"Q": "季度", "M": "月", "Y": "年"}


def period_labels(dates: pd.Series, freq: str = "Q") -> pd.Series:
    """日期 → 分段标签，例如 2024Q1、2024-01、2024。"""
    if freq not in FREQS:
        raise ValueError(f"freq 只能是 {'/'.join(FREQS)}")
    return pd.to_datetime(dates).dt.to_period(freq).astype(str)


def period_rankings(rep: VolReport, freq: str = "Q", min_days: int = 30) -> pd.DataFrame:
    """每段、每只证券一行：参与汇总的天数、可交易性评分、段内名次、三个评分组成的原始值和 ADR%。
    某只证券在某段里参与汇总的日子少于 min_days（上市不久、区间开头 ADR 数据不足、区间末尾不满一段）
    就不参加这一段的排名。参照证券（SPY、QQQ）照常参加，和报表的排名总表一致。"""
    p = rep.params
    k = ksuffix(p.score_k)
    d = included(rep.daily).copy()
    d["period"] = period_labels(col(d, "date"), freq)
    out = []
    for per, g in d.groupby("period", sort=True):
        days = cast(pd.Series, g.groupby("symbol").size())
        keep = [s for s, n in days.items() if n >= min_days]
        g = rows(g, col(g, "symbol").isin(keep))
        if g["symbol"].nunique() < 2:
            continue
        med, mean = aggregate.medians(g), aggregate.means(g)
        dr = aggregate.capturable_days_ratio(g, p.score_k)
        sc = scoring.score(med, mean, dr, g, p)
        t = pd.DataFrame(
            {
                "period": per,
                "days": days.reindex(med.index),
                "n_symbols": len(med),
                "score_tradable": sc["score_tradable"],
                "rank": sc["score_tradable"].rank(ascending=False, method="min"),
                "capturable_mean": mean[f"swing_capturable_pct_{k}"],
                "conversion": aggregate.conversion(med, mean, p.score_k),
                "daily_conversion_median": sc["consistency_raw"],
                "adr_pct": med["adr_pct"],
            }
        )
        t["benchmark"] = [s in rep.benchmarks and s not in rep.tickers for s in t.index]
        out.append(t.rename_axis("symbol").reset_index())
    if not out:
        raise ValueError("没有一段的数据足够排名（区间太短或 min_days 太大）")
    return pd.concat(out, ignore_index=True)


def pivot(pr: pd.DataFrame, value: str = "rank") -> pd.DataFrame:
    """证券 × 分段 的宽表（缺的段为空）。"""
    return pr.pivot(index="symbol", columns="period", values=value)


def stability_summary(pr: pd.DataFrame, top_n: int = 6) -> pd.DataFrame:
    """每只证券一行，按中位分数从高到低：
    periods 参加排名的段数；score_median / score_mean / score_min 段内评分的中位数、均值、最低值；
    rank_median、rank_best、rank_worst 段内名次；top_share 进入前 top_n 的段数占比；
    bottom_half_share 落到后一半的段数占比；last_rank 最近一段的名次；
    capturable_mean、conversion 各段原始值的中位数（不受段内相对打分影响，可跨段比较）。"""
    g = pr.groupby("symbol")
    half = pr["rank"] > pr["n_symbols"] / 2
    last = pr["period"].max()
    out = pd.DataFrame(
        {
            "periods": g.size(),
            "score_median": g["score_tradable"].median(),
            "score_mean": g["score_tradable"].mean(),
            "score_min": g["score_tradable"].min(),
            "rank_median": g["rank"].median(),
            "rank_best": g["rank"].min(),
            "rank_worst": g["rank"].max(),
            "top_share": (pr["rank"] <= top_n).groupby(pr["symbol"]).mean(),
            "bottom_half_share": half.groupby(pr["symbol"]).mean(),
            "last_rank": pr[pr["period"] == last].set_index("symbol")["rank"],
            "capturable_mean": g["capturable_mean"].median(),
            "conversion": g["conversion"].median(),
            "benchmark": g["benchmark"].first(),
        }
    )
    return out.sort_values(["score_median", "score_mean"], ascending=False)


def adjacent_rank_corr(pr: pd.DataFrame) -> pd.Series:
    """相邻两段（上一段 → 这一段）的评分名次 Spearman 相关系数，只算两段都有的证券。
    接近 1 = 排名几乎不变；接近 0 = 上一段的排名对下一段没有参考价值。"""
    w = pivot(pr, "score_tradable")
    cols = list(w.columns)
    out = {}
    for a, b in zip(cols, cols[1:], strict=False):
        both = w[[a, b]].dropna()
        out[f"{a}→{b}"] = corr(both[a], both[b], rank=True) if len(both) >= 3 else float("nan")
    return pd.Series(out, name="spearman", dtype=float)


def lagged_rank_corr(pr: pd.DataFrame, max_lag: int = 4) -> pd.Series:
    """相隔 1..max_lag 段的名次相关系数的平均值：排名的"记忆"能维持几段。"""
    w = pivot(pr, "score_tradable")
    cols = list(w.columns)
    out = {}
    for lag in range(1, max_lag + 1):
        vals = []
        for a, b in zip(cols, cols[lag:], strict=False):
            both = w[[a, b]].dropna()
            if len(both) >= 3:
                vals.append(corr(both[a], both[b], rank=True))
        out[lag] = sum(vals) / len(vals) if vals else float("nan")
    return pd.Series(out, name="spearman_mean", dtype=float).rename_axis("lag")


def selection_check(pr: pd.DataFrame, lookback: int = 1, top_n: int = 6) -> pd.DataFrame:
    """按过去 lookback 段的平均评分选出前 top_n 只（不含参照证券），看它们在下一段的表现：
    每段一行，picked 选中的证券；next_score / next_rank 它们下一段评分和名次的平均；
    all_score 下一段全部（非参照）证券的平均评分；hit 选中的证券里下一段仍在前 top_n 的只数。
    回答"用最近的排名选股票池，下一段还管不管用"。"""
    q = rows(pr, ~col(pr, "benchmark"))
    w = pivot(q, "score_tradable")
    rk = pivot(q, "rank")
    cap = pivot(q, "capturable_mean")
    cols = list(w.columns)
    found = []
    for i in range(lookback, len(cols)):
        past, nxt = cols[i - lookback : i], cols[i]
        hist = cast(pd.Series, w[past].dropna(how="any").mean(axis=1))  # 过去每段都有数据的证券
        hist = cast(pd.Series, hist[col(w, nxt).notna().reindex(hist.index, fill_value=False)])
        if len(hist) < top_n:
            continue
        picked = [str(x) for x in list(hist.sort_values(ascending=False).index)[:top_n]]
        nrank = rk[nxt].dropna()
        nxt_sorted = col(w, nxt).dropna().sort_values(ascending=False)
        top_next = {str(x) for x in list(nxt_sorted.index)[:top_n]}
        found.append(
            {
                "period": nxt,
                "picked": " ".join(picked),
                "next_score": w.loc[picked, nxt].mean(),
                "next_rank": nrank.loc[picked].mean(),
                "all_score": w[nxt].mean(),
                "next_capturable": cap.loc[picked, nxt].mean(),
                "all_capturable": cap[nxt].mean(),
                "hit": len(top_next & set(picked)),
            }
        )
    return pd.DataFrame(found).set_index("period") if found else pd.DataFrame()
