"""Volume Profile notebook 的图（规范第 6 节）。每个函数返回 matplotlib Figure。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from trader.analytics.frames import col, rows
from trader.analytics.volume_profile.service import VolumeProfile, as_date
from trader.core import volume_profile as vp
from trader.core.timeutil import from_ns

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

LEVEL_STYLE = {
    "poc": {"color": "#d62728", "lw": 1.4},
    "vah": {"color": "#1f77b4", "lw": 1.0, "ls": "--"},
    "val": {"color": "#1f77b4", "lw": 1.0, "ls": "--"},
}


def _hbars(ax: Any, fp: vp.FineProfile, size: int, color: str, alpha: float = 0.6) -> None:
    b = vp.rebin(fp, size)
    if len(b.vol) == 0:
        return
    idx = np.arange(len(b.vol))
    ax.barh(b.low(idx), b.vol, height=size / 100, align="edge", color=color, alpha=alpha)


def fig_day(v: VolumeProfile, symbol: str, day: str | date) -> Figure:
    """单日图：左边 1 分钟收盘价（盘前 + 常规时段）和当日 VWAP，叠加前一日 POC / VAH / VAL、
    5 日复合价值区、developing POC 轨迹、盘前高低点；右边是当天（截至收盘）和前一日的成交量分布。"""
    sym, t = symbol.upper(), as_date(day)
    td = v.td(t)
    m1 = v.history.bars(sym, "1m", td.pre_open, td.close, "extended").to_pandas()
    m1["time"] = [from_ns(int(x)) for x in m1["ts_start"]]
    rth = rows(m1, m1["ts_start"] >= td.open)
    pv = (col(rth, "vwap").fillna(col(rth, "close")) * col(rth, "volume")).cumsum()
    vwap = pv / col(rth, "volume").cumsum()
    prev = v.get_levels(sym, t, "daily")
    comp = v.get_levels(sym, t, "composite", n=5)
    pre = v.window(sym, t, "premarket").fine.extent()  # 清洗后的盘前高低点（分）
    dev = v.get_developing(sym, t)
    size = round(prev["bin_size"] * 100)

    fig, (ax, axr) = plt.subplots(
        1, 2, figsize=(14, 6), sharey=True, gridspec_kw={"width_ratios": [4, 1]}
    )
    ax.plot(m1["time"], m1["close"], color="#444", lw=0.8, label="1 分钟收盘价")
    ax.plot(rth["time"], vwap, color="#9467bd", lw=1.0, label="VWAP")
    ax.plot(dev["time"], dev["dpoc"], color="#ff7f0e", lw=1.2, label="developing POC")
    ax.axhspan(comp["val"], comp["vah"], color="#2ca02c", alpha=0.08, label="5 日价值区")
    for k, st in LEVEL_STYLE.items():
        ax.axhline(prev[k], **st, label=f"前一日 {k.upper()}")
        axr.axhline(prev[k], **st)
    if pre is not None:
        for px in pre:
            ax.axhline(px / 100, color="#8c564b", lw=0.8, ls=":")
        ax.plot([], [], color="#8c564b", lw=0.8, ls=":", label="盘前高低点")
    ax.axvline(from_ns(td.open), color="#999", lw=0.6)
    ax.legend(fontsize=8, loc="best")
    ax.set_title(f"{sym} {t}（ADR {prev['adr']:.2f}，格子 {prev['bin_size']:.2f}）", fontsize=11)
    today = v.window(sym, t, "developing").fine
    yesterday = v.window(sym, t, "daily").fine
    _hbars(axr, yesterday, size, "#aaa", 0.5)
    _hbars(axr, today, size, "#ff7f0e", 0.6)
    axr.set_title("成交量分布：当天（橙）/ 前一日（灰）", fontsize=9)
    axr.set_xticks([])
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def fig_windows(
    v: VolumeProfile,
    symbol: str,
    day: str | date,
    windows: list[tuple[str, str, dict[str, Any]]] | None = None,
) -> Figure:
    """同一天开盘前可用的几种 Profile 并排，横线为各自的 POC / VAH / VAL，看关键价位是否重合。"""
    sym, t = symbol.upper(), as_date(day)
    windows = windows or [
        ("前一日", "daily", {}),
        ("5 日", "composite", {"n": 5}),
        ("20 日", "composite", {"n": 20}),
        ("衰减 H=3", "decay", {"half_life": 3}),
    ]
    fig, axes = plt.subplots(1, len(windows), figsize=(3.2 * len(windows), 6), sharey=True)
    for ax, (title, kind, kw) in zip(np.atleast_1d(axes), windows, strict=True):
        w = v.window(sym, t, kind, **kw)
        lv = v.levels_of(w)
        _hbars(ax, w.fine, round(lv["bin_size"] * 100), "#1f77b4", 0.5)
        for k, st in LEVEL_STYLE.items():
            ax.axhline(lv[k], **st)
        for px in lv["lvn"]:
            ax.axhline(px, color="#7f7f7f", lw=0.6, ls=":")
        va = f"VA {lv['val']:.2f}–{lv['vah']:.2f}"
        ax.set_title(f"{title}\nPOC {lv['poc']:.2f}  {va}", fontsize=9)
        ax.set_xticks([])
    fig.suptitle(f"{sym} {t} 开盘前（虚线：LVN）", fontsize=11)
    fig.tight_layout()
    return fig


def fig_sensitivity(comparisons: Mapping[str, pd.DataFrame], title: str = "") -> Figure:
    """各种对比（compare 的结果）中 POC / VAH / VAL 差异绝对值（ADR 倍数）的分布。"""
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), sharey=True)
    names = list(comparisons)
    for ax, k in zip(axes, ("poc", "vah", "val"), strict=True):
        data = [comparisons[n][f"d_{k}_adr"].abs().dropna().to_numpy() for n in names]
        ax.boxplot(data, showfliers=False)
        ax.set_xticks(range(1, len(names) + 1), names, fontsize=8)
        ax.set_title(f"|Δ{k.upper()}|（ADR）", fontsize=10)
        ax.grid(axis="y", alpha=0.3)
    if title:
        fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    return fig


def sensitivity_table(comparisons: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """每种对比：差异绝对值的中位数、90 分位、超过 0.1 ADR 的比例。"""
    rows = []
    for name, df in comparisons.items():
        r: dict[str, Any] = {"对比": name, "天数": len(df)}
        for k in ("poc", "vah", "val"):
            x = df[f"d_{k}_adr"].abs()
            r[f"{k} 中位数"] = x.median()
            r[f"{k} 90 分位"] = x.quantile(0.9)
            r[f"{k} >0.1 ADR"] = (x > 0.1).mean()
        rows.append(r)
    return pd.DataFrame(rows).set_index("对比")


def fig_shape(levels: pd.DataFrame) -> Figure:
    """各证券前一日 Profile 的价值区宽度（ADR 倍数）和 POC 位置的分布。"""
    d = rows(levels, levels["profile_type"] == "daily")
    syms = sorted(d["symbol"].unique())
    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    for ax, name, title in zip(
        axes,
        ("va_width_adr", "poc_position", "n_hvn"),
        ("价值区宽度（ADR）", "POC 位置（0 = 最低，1 = 最高）", "HVN 个数"),
        strict=True,
    ):
        data = [d.loc[d["symbol"] == s, name].dropna().to_numpy() for s in syms]
        ax.boxplot(data, showfliers=False)
        ax.set_xticks(range(1, len(syms) + 1), syms, fontsize=8, rotation=45)
        ax.set_title(title, fontsize=10)
        ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    return fig
