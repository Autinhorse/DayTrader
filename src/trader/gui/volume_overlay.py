"""K 线图上的成交量分布（横向柱）。

数据来自研究模块 trader.analytics.volume_profile（1 秒 bar、清洗、每日缓存），只取常规时段，
关键价位口径与 notebook、`trader profile` 命令相同。回放中一律截止到回放时钟。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

from trader.analytics.volume_profile import VolumeProfile, WindowProfile
from trader.core.timeutil import from_ns
from trader.core.trading_calendar import TradingDay
from trader.gui.services import ny_seconds

# (代码, 下拉框里的名字)；“今日”是图上最后一个交易日（回放中是回放当天，截止到回放时钟）
MODES = [
    ("off", "关"),
    ("today", "今日"),
    ("prev", "前一日"),
    ("c5", "前5日合并"),
    ("c15", "前15日合并"),
    ("visible", "可见范围"),
    ("daily", "每日"),
]
COMPOSITE = {"prev": 1, "c5": 5, "c15": 15}
VISIBLE_MAX_DAYS = 60  # “可见范围”最多合并这么多个交易日


def _md(d: date) -> str:
    return f"{d:%m-%d}"


def _days_text(w: WindowProfile) -> str:
    if not w.days:
        return "无数据"
    a, b = w.days[0], w.days[-1]
    return _md(a) if a == b else f"{_md(a)}~{_md(b)}"


def profile_payload(
    vpf: VolumeProfile,
    symbol: str,
    mode: str,
    days: list[TradingDay],
    until: int | None = None,
    visible: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """图表 setProfile 的数据。days：图上的交易日（最后一个是“今日”）；until：回放时钟（UTC 纳秒），
    之后的数据一律不用；visible：可见范围（UTC 纳秒），mode 为 visible 时需要。

    profiles 每项一个分布：t0 / t1 为空时画在图的右侧，否则画在 t0 ~ t1 这段时间里（每日）。"""
    out: dict[str, Any] = {"mode": mode, "profiles": []}
    if mode == "off" or not days:
        return out
    last = days[-1]
    cut = last.end if until is None else min(until, last.end)
    items: list[tuple[WindowProfile, str, TradingDay | None]] = []
    if mode == "today":
        items.append((vpf.between(symbol, last.start, cut), f"今日 {_md(last.day)}", None))
    elif mode in COMPOSITE:
        n = COMPOSITE[mode]
        w = (
            vpf.window(symbol, last.day, "daily")
            if n == 1
            else vpf.window(symbol, last.day, "composite", n=n)
        )
        name = "前一日" if n == 1 else f"前{n}日合并"
        items.append((w, f"{name} {_days_text(w)}", None))
    elif mode == "visible":
        if visible is None:
            return out
        a, b = visible[0], min(visible[1], cut)
        tds = vpf.store.days_between(symbol, from_ns(a).date(), from_ns(max(a, b - 1)).date())
        note = ""
        if len(tds) > VISIBLE_MAX_DAYS:
            a = max(a, tds[-VISIBLE_MAX_DAYS].start)
            note = f"（只算最后 {VISIBLE_MAX_DAYS} 天）"
        if b > a:
            w = vpf.between(symbol, a, b)
            span = f"{from_ns(a):%m-%d %H:%M} ~ {from_ns(b):%m-%d %H:%M}"
            items.append((w, f"可见范围 {span}{note}", None))
    elif mode == "daily":
        for d in days:
            if d.start < cut:
                items.append((vpf.between(symbol, d.start, min(cut, d.end)), _md(d.day), d))
    else:
        raise ValueError(f"未知的分布模式 {mode!r}")
    for w, label, d in items:
        p = _profile(vpf, w, label)
        if p is not None:
            if d is not None:
                p["t0"], p["t1"] = ny_seconds(d.start), ny_seconds(d.end)
            out["profiles"].append(p)
    return out


def _profile(vpf: VolumeProfile, w: WindowProfile, label: str) -> dict[str, Any] | None:
    r = vpf.binned(w)
    lv = r.levels
    if lv is None or r.bins.empty:
        return None
    vol = r.bins["volume"].to_numpy()
    nz = np.flatnonzero(vol > 0)
    lo_i, hi_i = int(nz[0]), int(nz[-1]) + 1
    vol = vol[lo_i:hi_i]
    low = r.bins["price_low"].to_numpy()[lo_i:hi_i]
    high = r.bins["price_high"].to_numpy()[lo_i:hi_i]
    mid = r.bins["price_mid"].to_numpy()[lo_i:hi_i]
    in_va = np.flatnonzero((low >= lv.val - 1e-9) & (high <= lv.vah + 1e-9))
    return {
        "label": label,
        "t0": None,
        "t1": None,
        "low": float(low[0]),  # 第一个格子的最低价位；格子 i 覆盖 low + i × size 起的 size 美元
        "size": r.bin_size,
        "vol": [round(float(v)) for v in vol],
        "poc_i": int(np.argmin(np.abs(mid - lv.poc))),
        "va": [int(in_va[0]), int(in_va[-1])] if len(in_va) else [0, len(vol) - 1],
        "poc": lv.poc,
        "vah": lv.vah,
        "val": lv.val,
        "total": lv.total_volume,
        "n_days": len(w.days),
    }
