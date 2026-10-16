"""扩展时段参考指标（规范第 7 节）：跳空拆解、盘前振幅、扩展时段成交占比。不参与任何打分。

盘前 4:00–9:30、盘后 16:00–20:00（提前收盘日按交易日历）。成交量低于 eth_min_bar_volume 的
1 分钟 bar 不参与取价（第一笔、最后一笔、最高、最低），成交量照常累计。夜盘（2026 年 12 月起
可能出现）第一版不计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

import pandas as pd
import polars as pl

NAN = math.nan


@dataclass(slots=True)
class EthDay:
    """一个交易日的扩展时段数据（复权价格）。"""

    pre_first: float = NAN  # 盘前第一笔（第一根满足成交量条件的 bar 的开盘价）
    pm_high: float = NAN
    pm_low: float = NAN
    post_last: float = NAN  # 盘后最后一笔（最后一根满足成交量条件的 bar 的收盘价）
    vol_pre: float = 0.0
    vol_rth: float = 0.0
    vol_post: float = 0.0
    prev_post_last: float = NAN  # 前一交易日盘后最后一笔，由读取方填

    @classmethod
    def from_frame(cls, df: pl.DataFrame, factor: float, min_volume: float) -> EthDay:
        """df：day_bars(..., "1m", day, "extended")，原始价格；factor 为拆股比例。"""
        out = cls()
        if df.is_empty():
            return out
        vol = {
            s: float(v) for s, v in df.group_by("session").agg(pl.col("volume").sum()).iter_rows()
        }
        out.vol_pre = vol.get("pre", 0.0) * factor
        out.vol_rth = vol.get("regular", 0.0) * factor
        out.vol_post = vol.get("post", 0.0) * factor
        ok = df.filter(pl.col("volume") >= min_volume)
        pre = ok.filter(pl.col("session") == "pre")
        if not pre.is_empty():
            out.pre_first = float(pre["open"][0]) / factor
            out.pm_high = float(pre["high"].max()) / factor  # type: ignore[arg-type]
            out.pm_low = float(pre["low"].min()) / factor  # type: ignore[arg-type]
        post = ok.filter(pl.col("session") == "post")
        if not post.is_empty():
            out.post_last = float(post["close"][-1]) / factor
        return out


def eth_metrics(eth: EthDay | None, o: float, pc: float, adr: float) -> dict[str, float]:
    """三段跳空之和等于 gap_adr；任一段缺失时三段都为空。"""
    keys = (
        "gap_post_adr", "gap_overnight_adr", "gap_pre_adr", "pm_range_adr", "pm_high", "pm_low",
        "eth_vol_share",
    )  # fmt: skip
    if eth is None:
        return dict.fromkeys(keys, NAN)
    ok = math.isfinite(adr) and adr > 0
    parts = (eth.prev_post_last, eth.pre_first, pc)
    split_ok = ok and all(math.isfinite(x) for x in parts)
    total = eth.vol_pre + eth.vol_rth + eth.vol_post
    return {
        "gap_post_adr": (eth.prev_post_last - pc) / adr if split_ok else NAN,
        "gap_overnight_adr": (eth.pre_first - eth.prev_post_last) / adr if split_ok else NAN,
        "gap_pre_adr": (o - eth.pre_first) / adr if split_ok else NAN,
        "pm_range_adr": (eth.pm_high - eth.pm_low) / adr if ok else NAN,
        "pm_high": eth.pm_high,
        "pm_low": eth.pm_low,
        "eth_vol_share": (eth.vol_pre + eth.vol_post) / total if total > 0 else NAN,
    }


GAP_PARTS = ("gap_post_adr", "gap_overnight_adr", "gap_pre_adr")


def summarize(daily: pd.DataFrame) -> pd.DataFrame:
    """每只证券：扩展时段字段的中位数，以及三段跳空绝对值的占比（Σ|该段| / Σ|三段|）。
    只用参与汇总的日子。"""
    d = cast(pd.DataFrame, daily[daily["included"]])
    cols = [*GAP_PARTS, "gap_adr", "pm_range_adr", "eth_vol_share"]
    if not set(cols) <= set(d.columns):
        return pd.DataFrame()
    out = cast(pd.DataFrame, d.groupby("symbol")[cols].median()).add_suffix("_median")
    full = cast(pd.DataFrame, d.dropna(subset=list(GAP_PARTS)))
    absd = cast(pd.DataFrame, full[list(GAP_PARTS)]).abs()
    sym = cast(pd.Series, full["symbol"])
    tot = cast(pd.Series, absd.sum(axis=1).groupby(sym).sum())
    for c in GAP_PARTS:
        share = cast(pd.Series, cast(pd.Series, absd[c]).groupby(sym).sum()) / tot
        out[c.replace("_adr", "_share")] = share.where(tot > 0)
    out["gap_days"] = full.groupby("symbol").size()
    return out
