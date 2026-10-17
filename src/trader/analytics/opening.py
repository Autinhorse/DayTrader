"""开盘区间与日内高低点（决策 0015）。

回答三个问题：
1. 全天最高价、最低价一般出现在什么时候？
2. 开盘后 n 分钟（30/60/90）的区间形成以后，这一天是留在区间里震荡，还是突破走单边？
3. 到第 n 分钟为止已经知道的信息（区间宽度、跳空、盘前、昨天、最近几天、QQQ）能不能提前判断？

    rep = build_report(tickers, start, end)       # 每日表里有 or_hi_n 等开盘区间字段
    d = included_days(rep)                         # 参与汇总的日子
    extremes_summary(d)                            # 高低点出现在前 n 分钟的比例
    out = day_outcomes(d, 60)                      # 每天的走势分类
    f = features(rep.daily, 60)                    # 第 60 分钟时已知的特征
    conditional(out, f, "or_width")                # 按特征分档看走势分类

特征只用第 n 分钟以前（含昨天、盘前）的数据，不看之后的走势；走势分类只用第 n 分钟以后的数据。
所有幅度以 ADR（过去 21 天常规时段振幅的中位数）为单位。
"""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pandas as pd

from trader.analytics.frames import col, included, rows
from trader.analytics.params import skip_suffix
from trader.analytics.pipeline import VolReport

TYPES = ("区间内", "单边突破", "突破后收回", "双向突破")
TYPE_NOTES = {
    "区间内": "之后一直没有突破开盘区间：全天高低点都在开盘区间里",
    "单边突破": "只突破了一边，而且收盘在突破的那一边之外：趋势日",
    "突破后收回": "只突破了一边，但收盘回到区间里：假突破",
    "双向突破": "两边都突破了：宽幅震荡",
}


def included_days(rep: VolReport, symbols: list[str] | None = None) -> pd.DataFrame:
    """参与汇总的日子；symbols 为空时取用户输入的证券（不含参照证券）。"""
    syms = symbols if symbols is not None else rep.tickers
    d = included(rep.daily)
    return rows(d, col(d, "symbol").isin(syms)).reset_index(drop=True)


def _with_total(t: Any, total: Any) -> pd.DataFrame:
    return pd.concat([cast(pd.DataFrame, t), cast(pd.Series, total).to_frame("全部").T])


def _lookup(dates: pd.Series, by_date: Any) -> pd.Series:
    """按日期取参照证券当天的值。"""
    return cast(pd.Series, dates.map(cast(Any, by_date)))


def extremes_summary(daily: pd.DataFrame, windows: tuple[int, ...] = (30, 60, 90)) -> pd.DataFrame:
    """每只证券（最后一行"全部"）：全天最高价、最低价出现在开盘后 n 分钟以内的天数比例；
    either = 至少一个，both = 两个都在（这一天的高低点在第 n 分钟就已经定了）。"""
    hi, lo = col(daily, "high_time"), col(daily, "low_time")
    parts = {}
    for n in windows:
        parts[f"high_{n}"] = hi < n
        parts[f"low_{n}"] = lo < n
        parts[f"either_{n}"] = (hi < n) | (lo < n)
        parts[f"both_{n}"] = (hi < n) & (lo < n)
    flags = pd.DataFrame(parts)
    t = cast(pd.DataFrame, flags.groupby(daily["symbol"]).mean())
    t.insert(0, "days", cast(pd.Series, daily.groupby("symbol").size()))
    total = flags.mean()
    total["days"] = len(daily)
    return _with_total(t, total)


def extremes_histogram(daily: pd.DataFrame, step: int = 15) -> pd.DataFrame:
    """最高价、最低价出现时刻的分布：每 step 分钟一档，值为天数占比。索引是美东时间（档的起点）。"""
    edges = np.arange(0, 390 + step, step)
    labels = [f"{9 + (30 + m) // 60}:{(30 + m) % 60:02d}" for m in edges[:-1]]
    out = {}
    for name, c in (("最高价", "high_time"), ("最低价", "low_time")):
        b = cast(pd.Series, pd.cut(col(daily, c), edges, right=False, labels=labels))
        out[name] = b.value_counts(normalize=True, sort=False)
    return pd.DataFrame(out)


def day_outcomes(daily: pd.DataFrame, n: int) -> pd.DataFrame:
    """每天一行：开盘区间宽度、走势分类（TYPES）、高低点是否已在区间里、区间之外又走了多远、
    之后的可获取波动。or_width 区间宽度；close_adr 收盘相对开盘；
    extension 区间之外的额外振幅（两边之和）；capturable_after 第 n 分钟以后开始的摆动的
    可获取波动 / ADR%（日内波动报表口径，没有时为空）；first_dir 第一次突破的方向
    （1 上、-1 下、0 没突破）；first_min 第一次突破的时刻（距开盘分钟）。"""
    s = str(n)
    hi, lo = col(daily, f"or_hi_{s}"), col(daily, f"or_lo_{s}")
    phi, plo = col(daily, f"post_hi_{s}"), col(daily, f"post_lo_{s}")
    close_adr = (col(daily, "close") - col(daily, "open")) / col(daily, "adr")
    up, dn = phi > hi, plo > lo
    kind = np.select(
        [~up & ~dn, up & dn, up & (close_adr > hi), dn & (-close_adr > lo)],
        ["区间内", "双向突破", "单边突破", "单边突破"],
        default="突破后收回",
    )
    bu, bd = col(daily, f"brk_up_{s}"), col(daily, f"brk_dn_{s}")
    first_dir = np.where(bu.notna() & (bd.isna() | (bu <= bd)), 1, np.where(bd.notna(), -1, 0))
    cap_col = f"swing_capturable_pct_{skip_suffix(0.25, n)}"
    cap = (
        col(daily, cap_col) / col(daily, "adr_pct")
        if cap_col in daily.columns
        else pd.Series(np.nan, index=daily.index)
    )
    out = pd.DataFrame(
        {
            "symbol": daily["symbol"],
            "date": daily["date"],
            "or_width": hi + lo,
            "type": kind,
            "high_set": ~up,
            "low_set": ~dn,
            "close_adr": close_adr,
            "extension": np.maximum(phi - hi, 0) + np.maximum(plo - lo, 0),
            "capturable_after": cap,
            "first_dir": first_dir,
            "first_min": np.fmin(bu, bd),
        }
    )
    return rows(out, hi.notna().to_numpy()).reset_index(drop=True)


def type_shares(out: pd.DataFrame, by: str | None = "symbol") -> pd.DataFrame:
    """各类走势的天数占比，以及高点 / 低点已在区间里的比例、平均额外振幅、之后的可获取波动。"""

    def agg(g: pd.DataFrame) -> pd.Series:
        sh = g["type"].value_counts(normalize=True).reindex(list(TYPES), fill_value=0.0)
        return pd.concat(
            [
                pd.Series({"days": float(len(g))}),
                sh,
                pd.Series(
                    {
                        "高点已出现": g["high_set"].mean(),
                        "低点已出现": g["low_set"].mean(),
                        "额外振幅": g["extension"].mean(),
                        "之后可获取": g["capturable_after"].mean(),
                    }
                ),
            ]
        )

    total = agg(out)
    if by is None:
        return total.to_frame("全部").T
    t = pd.DataFrame({k: agg(cast(pd.DataFrame, g)) for k, g in out.groupby(by)}).T
    return _with_total(t, total)


def features(daily: pd.DataFrame, n: int, ref: str = "QQQ") -> pd.DataFrame:
    """每天一行，第 n 分钟时已知的特征（以 ADR 为单位）：
    or_width 开盘区间宽度；or_pos 第 n 分钟的价格在区间里的位置（0 = 区间最低，1 = 最高）；
    or_move 开盘到第 n 分钟的涨跌；gap 跳空（开盘相对昨收）；pm_range 盘前振幅；
    open_vs_prev 开盘价在昨天常规时段区间的位置（<0 在昨天最低价以下，>1 在昨天最高价以上）；
    prev_range、prev_body 昨天的振幅（ADR 倍数）和实体比例（接近 1 = 昨天是单边日）；
    vol5 最近 5 天振幅的平均（ADR 倍数，> 1 表示最近波动在放大）；trend5 最近 5 天的涨跌；
    ref_* 参照证券（默认 QQQ）同一天的同样特征；same_dir 开盘到第 n 分钟个股和参照证券同涨同跌。
    用全部日子（含不参与汇总的）计算昨天和最近几天，避免提前收盘日造成断档。"""
    s = str(n)
    d = daily.sort_values(["symbol", "date"]).copy()
    g = d.groupby("symbol")
    adr = col(d, "adr")
    prev_h, prev_l = g["high"].shift(1), g["low"].shift(1)
    hi, lo = col(d, f"or_hi_{s}"), col(d, f"or_lo_{s}")
    width = hi + lo
    f = pd.DataFrame(
        {
            "symbol": d["symbol"],
            "date": d["date"],
            "or_width": width,
            "or_pos": (col(d, f"or_px_{s}") + lo) / width.where(width > 0),
            "or_move": col(d, f"or_px_{s}"),
            "gap": col(d, "gap_adr"),
            "pm_range": d["pm_range_adr"] if "pm_range_adr" in d.columns else np.nan,
            "open_vs_prev": (col(d, "open") - prev_l) / (prev_h - prev_l),
            "prev_range": g["range_adr"].shift(1),
            "prev_body": g["body_ratio"].shift(1),
            "vol5": g["range_adr"].transform(lambda x: x.shift(1).rolling(5, min_periods=4).mean()),
            "trend5": (g["close"].shift(1) - g["close"].shift(6)) / adr,
        }
    )
    r = f[f["symbol"] == ref].set_index("date")
    for c in ("or_width", "or_pos", "or_move", "gap"):
        f[f"ref_{c}"] = _lookup(col(f, "date"), r[c])
    ref_move = col(f, "ref_or_move")
    same = (np.sign(col(f, "or_move")) == np.sign(ref_move)).astype(float)
    f["same_dir"] = same.where(ref_move.notna())  # 1 = 同向，0 = 反向，空 = 参照证券没有数据
    return f.reset_index(drop=True)


def conditional(
    out: pd.DataFrame, feats: pd.DataFrame, feature: str, bins: int | list[float] = 3
) -> pd.DataFrame:
    """按某个特征分档（默认三等分：低、中、高），每档的走势分类占比等（同 type_shares）。
    bins 为列表时按给定的边界分档。"""
    base = out.drop(columns=[feature], errors="ignore")
    m = base.merge(feats[["symbol", "date", feature]], on=["symbol", "date"], how="inner")
    x = col(m, feature)
    if set(x.dropna().unique()) <= {0, 1}:  # 是 / 否
        m["bin"] = x.map({1: "是", 0: "否"})
    elif isinstance(bins, int):
        m["bin"] = pd.qcut(x, bins, duplicates="drop")
    else:
        m["bin"] = pd.cut(x, bins)
    t = type_shares(rows(m, col(m, "bin").notna()), "bin")
    t.index = [str(i) for i in t.index]
    t.index.name = feature
    return t


def ref_alignment(out: pd.DataFrame, daily: pd.DataFrame, n: int, ref: str = "QQQ") -> pd.DataFrame:
    """个股第一次突破开盘区间的那一刻，参照证券（QQQ）的状态，以及这次突破的结果：
    状态分"已同向突破"、"已反向突破"、"还没突破"（参照证券的突破时刻 ≤ 个股突破时刻才算"已"）。
    结果：收盘在突破方向的区间之外的比例（突破成功）、之后反向也突破的比例、突破方向上走出的额外幅度。"""
    s = str(n)
    ref_d = rows(daily, col(daily, "symbol") == ref).set_index("date")
    m = rows(out, col(out, "first_dir") != 0).copy()
    d = daily.set_index(["symbol", "date"])
    keys = pd.MultiIndex.from_frame(cast(pd.DataFrame, m[["symbol", "date"]]))
    hi, lo, phi, plo = (
        col(d, f"{k}_{s}").reindex(keys).to_numpy(dtype=float)
        for k in ("or_hi", "or_lo", "post_hi", "post_lo")
    )
    up = col(m, "first_dir").to_numpy() == 1
    m["success"] = np.where(up, m["close_adr"] > hi, -m["close_adr"] > lo)
    m["reversed"] = np.where(up, plo > lo, phi > hi)
    m["follow"] = np.where(up, phi - hi, plo - lo)
    rbu = _lookup(col(m, "date"), ref_d[f"brk_up_{s}"]).to_numpy(dtype=float)
    rbd = _lookup(col(m, "date"), ref_d[f"brk_dn_{s}"]).to_numpy(dtype=float)
    t = col(m, "first_min").to_numpy(dtype=float)
    same = np.where(up, rbu <= t, rbd <= t)
    opp = np.where(up, rbd <= t, rbu <= t)
    m["ref_state"] = np.select([same, opp], ["已同向突破", "已反向突破"], default="还没突破")
    m = rows(m, col(m, "date").isin(list(ref_d.index)))
    g = m.groupby("ref_state")
    res = pd.DataFrame(
        {
            "次数": g.size(),
            "占比": g.size() / len(m),
            "突破成功": g["success"].mean(),
            "反向也突破": g["reversed"].mean(),
            "突破方向额外幅度": g["follow"].mean(),
        }
    )
    total = pd.Series(
        {"次数": len(m), "占比": 1.0, "突破成功": m["success"].mean(),
         "反向也突破": m["reversed"].mean(), "突破方向额外幅度": m["follow"].mean()}
    )  # fmt: skip
    return _with_total(res.reindex(["已同向突破", "还没突破", "已反向突破"]), total)


def ref_sync(daily: pd.DataFrame, ref: str = "QQQ", tol_min: int = 15) -> pd.DataFrame:
    """每只证券和参照证券的同步程度：全天同涨同跌的比例；
    最高价（最低价）出现时刻相差不超过 tol_min 分钟的比例。"""
    r = rows(daily, col(daily, "symbol") == ref).set_index("date")
    d = rows(daily, col(daily, "symbol") != ref)
    dd = col(d, "date")
    same = np.sign(d["close"] - d["open"]) == np.sign(_lookup(dd, r["close"] - r["open"]))
    hi = (d["high_time"] - _lookup(dd, r["high_time"])).abs() <= tol_min
    lo = (d["low_time"] - _lookup(dd, r["low_time"])).abs() <= tol_min
    flags = pd.DataFrame({"同涨同跌": same, "高点同时": hi, "低点同时": lo})
    t = flags.groupby(d["symbol"]).mean()
    return _with_total(t, flags.mean())


FEATURE_NOTES = {
    "or_width": "开盘区间宽度",
    "or_pos": "第 n 分钟价格在区间里的位置",
    "gap": "跳空",
    "pm_range": "盘前振幅",
    "open_vs_prev": "开盘在昨天区间的位置",
    "prev_range": "昨天振幅",
    "prev_body": "昨天实体比例（单边程度）",
    "vol5": "最近 5 天平均振幅",
    "trend5": "最近 5 天涨跌",
    "ref_or_width": "QQQ 开盘区间宽度",
    "same_dir": "开盘以来和 QQQ 同向",
}


def factor_summary(
    out: pd.DataFrame, feats: pd.DataFrame, names: list[str] | None = None, bins: int = 3
) -> pd.DataFrame:
    """每个特征一行：按三等分分档后，各项指标"最高一档 − 最低一档"的差（百分点或 ADR）。
    差越大，这个特征越能区分；接近 0 说明对这一项没有参考价值。"""
    names = names or list(FEATURE_NOTES)
    cols = ["区间内", "单边突破", "突破后收回", "双向突破", "高点已出现", "之后可获取"]
    res = {}
    for name in names:
        t = conditional(out, feats, name, bins).drop(index="全部")
        spread = t[cols].max() - t[cols].min()
        res[f"{name}（{FEATURE_NOTES.get(name, name)}）"] = spread
    return pd.DataFrame(res).T
