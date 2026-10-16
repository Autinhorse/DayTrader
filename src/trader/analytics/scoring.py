"""打分（规范第 5 节）：在本次输入的证券（含参照证券）中做百分位排名，是相对分数。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pandas as pd

from trader.analytics.frames import included
from trader.analytics.params import ReportParams, ksuffix


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


def consistency(daily: pd.DataFrame, p: ReportParams) -> pd.Series:
    """当日震荡分 = 当天 swing_n 和 1 − body_ratio 在所有证券、所有交易日中的百分位的平均；
    稳定性原值 = 当日震荡分 ≥ 50 的天数占比。"""
    d = included(daily)
    n_col = f"swing_n_{ksuffix(p.score_k)}"
    day_score = (pct_rank(d[n_col]) + pct_rank(1 - d["body_ratio"])) / 2
    hit = (day_score >= 50).astype(float).where(day_score.notna())
    return hit.groupby(d["symbol"]).mean()


def score(med: pd.DataFrame, daily: pd.DataFrame, p: ReportParams) -> pd.DataFrame:
    s = ksuffix(p.score_k)
    activity = weighted({k: med[k] for k in p.activity_weights}, p.activity_weights)
    rot_parts = {
        "swing_n": med[f"swing_n_{s}"],
        "one_minus_body": 1 - med["body_ratio"],
        "vwap_crosses": med["vwap_crosses"],
    }
    rotation = weighted(rot_parts, p.rotation_weights)
    sv_parts = {
        "swing_capturable_pct": med[f"swing_capturable_pct_{s}"],
        "swing_avg_pct": med[f"swing_avg_pct_{s}"],
    }
    swing_value = weighted(sv_parts, p.swing_value_weights)
    cons_raw = consistency(daily, p).reindex(med.index)
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
