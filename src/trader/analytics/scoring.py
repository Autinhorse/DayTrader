"""打分（规范第 5 节）：在本次输入的证券（含参照证券）中做百分位排名，是相对分数。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pandas as pd

from trader.analytics.aggregate import capturable_days_ratio, conversion
from trader.analytics.frames import included
from trader.analytics.params import ReportParams, ksuffix, skip_suffix


def pct_rank(x: Any) -> pd.Series:
    """0–100：最小为 0，最大为 100，并列取平均名次；只有一个有效值时为 50；空值仍为空。"""
    s = cast(pd.Series, x)
    valid = s.notna().sum()
    if valid == 0:
        return s * float("nan")
    if valid == 1:
        return s.where(s.isna(), 50.0)
    return (s.rank(method="average") - 1) / (valid - 1) * 100


def weighted(parts: Mapping[str, Any], weights: dict[str, float]) -> pd.Series:
    """各组成先做百分位排名再加权平均；任一组成为空则结果为空。"""
    total = sum(weights.values())
    acc: pd.Series | None = None
    for name, w in weights.items():
        term = pct_rank(parts[name]) * (w / total)
        acc = term if acc is None else acc + term
    assert acc is not None
    return acc


def consistency(daily: pd.DataFrame, k: float | str) -> pd.Series:
    """当日震荡分 = 当天"可获取波动 / ADR%"和 1 − body_ratio 在所有证券、所有交易日中的百分位
    的平均（都是连续值，比摆动次数这种小整数少并列）；稳定性原值 = 当日震荡分 ≥ 50 的天数占比。"""
    d = included(daily)
    cap = d[f"swing_capturable_pct_{ksuffix(k)}"] / d["adr_pct"]
    day_score = (pct_rank(cap) + pct_rank(1 - d["body_ratio"])) / 2
    hit = (day_score >= 50).astype(float).where(day_score.notna())
    return hit.groupby(d["symbol"]).mean()


def score(
    med: pd.DataFrame,
    mean: pd.DataFrame,
    days_ratio: pd.Series,
    daily: pd.DataFrame,
    p: ReportParams,
    k: float | str | None = None,
) -> pd.DataFrame:
    """k：摆动阈值或字段后缀，默认 score_k（阈值敏感性、开盘剔除对照用其他口径重算）。"""
    k = p.score_k if k is None else k
    s = ksuffix(k)
    activity = weighted({c: med[c] for c in p.activity_weights}, p.activity_weights)
    rot_parts = {
        "swing_n": mean[f"swing_n_{s}"],
        "one_minus_body": 1 - med["body_ratio"],
        "vwap_crosses": med["vwap_crosses"],
    }
    rotation = weighted(rot_parts, p.rotation_weights)
    sv_parts = {
        "swing_capturable_pct": mean[f"swing_capturable_pct_{s}"],
        "capturable_days_ratio": days_ratio.reindex(med.index),
        "swing_avg_pct": med[f"swing_avg_pct_{s}"],
    }
    swing_value = weighted(sv_parts, p.swing_value_weights)
    cons_raw = consistency(daily, k).reindex(med.index)
    cons = pct_rank(cons_raw)
    out = pd.DataFrame(
        {
            "score_activity": activity,
            "score_rotation": rotation,
            "score_swing_value": swing_value,
            "score_consistency": cons,
            "consistency_raw": cons_raw,
        },
        index=med.index,
    )
    w = p.score_weights
    total = sum(w.values())
    out.insert(
        0,
        "score_total",
        (
            out["score_activity"] * w["activity"]
            + out["score_rotation"] * w["rotation"]
            + out["score_swing_value"] * w["swing_value"]
            + out["score_consistency"] * w["consistency"]
        )
        / total,
    )
    return out


def sensitivity(
    med: pd.DataFrame,
    mean: pd.DataFrame,
    daily: pd.DataFrame,
    p: ReportParams,
    days_ratio: Mapping[float, pd.Series],
) -> pd.DataFrame:
    """每个摆动阈值下的 swing_n 均值、swing_capturable_pct 均值、capturable_days_ratio、
    综合得分和名次（规范第 6 节第 4 项）。列为 (阈值, 字段) 两级。"""
    parts = {}
    for k in p.swing_thresholds_adr:
        s = ksuffix(k)
        sc = score(med, mean, days_ratio[k], daily, p, k)
        parts[f"{k:g}"] = pd.DataFrame(
            {
                "swing_n_mean": mean[f"swing_n_{s}"],
                "capturable_mean": mean[f"swing_capturable_pct_{s}"],
                "capturable_days_ratio": days_ratio[k].reindex(med.index),
                "score_total": sc["score_total"],
                "rank": sc["score_total"].rank(ascending=False, method="min"),
            }
        )
    return pd.concat(parts, axis=1, names=["k", "field"])


def open_skip_table(
    med: pd.DataFrame, mean: pd.DataFrame, daily: pd.DataFrame, p: ReportParams
) -> pd.DataFrame:
    """开盘剔除对照：分别只统计开始于开盘 m 分钟以后的摆动（m 取 open_skip_compare），
    各算一次综合得分和名次，并列出转化效率、可获取波动均值（绝对值）、摆动次数均值。
    振幅、ADR、body_ratio 等不变。列为 (剔除分钟数, 字段) 两级。"""
    parts = {}
    for m in p.open_skip_compare:
        ss = skip_suffix(p.score_k, m)
        dr = capturable_days_ratio(daily, ss)
        sc = score(med, mean, dr, daily, p, ss)
        parts[f"{m}"] = pd.DataFrame(
            {
                "score_total": sc["score_total"],
                "rank": sc["score_total"].rank(ascending=False, method="min"),
                "conversion": conversion(med, mean, ss),
                "capturable_mean": mean[f"swing_capturable_pct_{ss}"],
                "swing_n_mean": mean[f"swing_n_{ss}"],
            }
        )
    return pd.concat(parts, axis=1, names=["skip_min", "field"])
