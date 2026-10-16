"""异常日检测（规范 3.7）：找出错误成交（离群价格）造成虚假摆动的日子。只标记，默认不剔除。"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import numpy as np
import pandas as pd

from trader.analytics.params import ReportParams, ksuffix

if TYPE_CHECKING:
    from trader.analytics.daily_metrics import Bars

NEIGHBORS = 5  # 前后各取几根 1 分钟 bar 的收盘价


def outlier_bars(m1: Bars, limit: float, skip_before: int = 0) -> int:
    """最高价或最低价偏离前后各 5 根 bar 收盘价中位数超过 limit（价格单位）的 1 分钟 bar 个数。
    开始时间早于 skip_before（UTC 纳秒）的 bar 不判断（开盘头几分钟本来就波动剧烈，而且只有一侧
    有邻居）；前后合计不足 3 根的 bar 也不判断。"""
    n = len(m1)
    if n < 4 or not np.isfinite(limit) or limit <= 0:
        return 0
    count = 0
    for i in range(n):
        if m1.ts[i] < skip_before:
            continue
        near = np.concatenate(
            [m1.close[max(0, i - NEIGHBORS) : i], m1.close[i + 1 : i + 1 + NEIGHBORS]]
        )
        if len(near) < 3:
            continue
        med = float(np.median(near))
        if m1.high[i] - med > limit or med - m1.low[i] > limit:
            count += 1
    return count


def flag_anomalies(daily: pd.DataFrame, p: ReportParams) -> pd.DataFrame:
    """标记两类日子（规范 3.7）：

    - 数据异常 data_issue：落在坏数据时段，或清洗时改动了 1 分钟 bar。已经在计算前处理掉，
      列出来供核对清洗是否合理。
    - 行为特殊日 special_day：摆动密度过高、摆动次数远超平常（安静来回震荡的日子、财报日），
      或清洗后仍有 1 分钟价格尖刺（持续一分钟以上，多半是消息行情）。数据没问题，只标记。

    摆动次数的中位数只用参与汇总的日子（提前收盘、ADR 不足的日子不算）。"""
    n_col = "swing_n_all" if "swing_n_all" in daily.columns else f"swing_n_{ksuffix(p.score_k)}"
    base = daily[daily["included"]].groupby("symbol")[n_col].median()
    med = cast(pd.Series, daily["symbol"]).map(cast(dict, base.to_dict()))
    win = daily["window_minutes"] > 0
    chg = daily["changed_minutes"] > 0
    outl = daily["outlier_bars"] > 0
    dens = daily["swing_density"] > p.anomaly_swing_density
    many = (daily[n_col] > med * p.anomaly_swing_multiple) & (daily[n_col] >= p.anomaly_min_swings)
    data_r, beh_r = [], []
    for i in range(len(daily)):
        r = []
        if win.iat[i]:
            r.append(f"坏数据时段删除 {daily['window_minutes'].iat[i]:.0f} 分钟")
        if chg.iat[i]:
            r.append(
                f"修正离群成交 {daily['fixed_prints'].iat[i]:.0f} 笔"
                f"（影响 {daily['changed_minutes'].iat[i]:.0f} 分钟）"
            )
        data_r.append("；".join(r))
        b = []
        if dens.iat[i]:
            b.append(f"摆动密度 {daily['swing_density'].iat[i]:.1f}")
        if many.iat[i]:
            b.append(f"摆动次数 {daily[n_col].iat[i]:.0f}（该证券中位数 {med.iat[i]:.0f}）")
        if outl.iat[i]:
            b.append(f"1 分钟价格尖刺 {daily['outlier_bars'].iat[i]:.0f} 根")
        beh_r.append("；".join(b))
    out = daily.copy()
    out["data_issue"] = win | chg
    out["data_reason"] = data_r
    out["special_day"] = dens | many | outl
    out["special_reason"] = beh_r
    return out
