"""报表的表格样式和图（规范第 6 节）。

每个函数返回 matplotlib Figure 或 pandas Styler，notebook 里直接显示。
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any, cast

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from trader.analytics.cleaning import clean_day
from trader.analytics.daily_metrics import Bars, cumulative_vwap
from trader.analytics.frames import corr, included, rows
from trader.analytics.loader import to_bars
from trader.analytics.params import ksuffix
from trader.analytics.pipeline import NY, VolReport
from trader.data.corporate_actions import load_actions, split_factor

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

BENCH_COLOR = "#8a8a8a"
MAIN_COLOR = "#2f6db5"


def _k(rep: VolReport) -> str:
    return ksuffix(rep.params.score_k)


def param_summary(rep: VolReport) -> pd.DataFrame:
    p = rep.params
    d = rep.daily
    used = d[d["included"]]
    tiers = "；".join(
        f"{'< $' + format(cap, 'g') if cap is not None else '其余'}：{bps:g}bp"
        for cap, bps in p.cost_tiers_bps
    )
    rows = [
        ("证券", "、".join(rep.tickers) or "（无）"),
        ("参照证券", "、".join(rep.benchmarks) or "（无）"),
        ("日期范围", f"{rep.start} ~ {rep.end}"),
        (
            "有效交易日数（各证券最多）",
            int(used.groupby("symbol").size().to_numpy().max()) if len(used) else 0,
        ),
        ("本地无数据的证券", "、".join(rep.missing_symbols) or "无"),
        ("未参与汇总的日子", len(rep.excluded)),
        ("疑似停牌（只标记，不剔除）", int(d["possible_halt"].to_numpy().sum())),
        (
            "数据异常日（已清洗；" + ("并剔除" if p.exclude_data_issue_days else "不剔除") + "）",
            int(d["data_issue"].to_numpy().sum()),
        ),
        ("行为特殊日（只标记）", int(d["special_day"].to_numpy().sum())),
        (
            "数据清洗",
            f"离群成交阈值 {p.print_filter_adr:g} × ADR；坏数据时段 {len(p.bad_windows or ())} 个"
            "（config/bad_data_windows.yaml）",
        ),
        ("1 秒数据也分不出先后的分钟", int(d["ambiguous_minutes"].to_numpy().sum())),
        (
            "区间内拆股",
            "、".join(
                f"{sym} {pd.Timestamp(ex).date()}（{ratio:g} 比 1）"
                for sym, ex, ratio in zip(
                    rep.splits["symbol"], rep.splits["ex_date"], rep.splits["ratio"], strict=True
                )
            )
            or "无",
        ),
        ("价格口径", f"拆股复权到 {rep.end}（成本分档按当天实际价格）"),
        ("摆动阈值（ADR 倍数）", ", ".join(f"{k:g}" for k in p.swing_thresholds_adr)),
        ("打分用阈值", f"{p.score_k:g} × ADR"),
        ("ADR", f"{p.adr_days} 日常规时段振幅的中位数（清洗后，不含提前收盘日），用前一交易日的值"),
        ("单边成本假设", tiers),
        ("扩展时段参考", "计算（不参与打分）" if p.include_extended_hours else "不计算"),
    ]
    return pd.DataFrame(rows, columns=["项目", "值"]).set_index("项目")


COST_NOTE = (
    "当前数据不含报价，交易成本是按开盘价分档的假设值（见参数摘要），不是实测价差。"
    "`swing_capturable_pct` 扣除了确认转折时损失的 2θ 和买卖两次成本，但没有计入反应延迟和滑点，"
    "**是用来在证券之间比较的相对指数，不是能实际赚到的收益**；阈值越小高估越多。"
)


def styled_ranking(rep: VolReport):  # noqa: ANN201
    """排名总表：分数用颜色梯度，参照证券灰底，流动性不足标红字。"""
    r = rep.ranking.copy()
    score_cols = [c for c in r.columns if c.startswith("score_")]
    fmt: dict[Any, Any] = {c: "{:.0f}" for c in score_cols}
    for c in r.columns:
        if c in score_cols or r[c].dtype == bool:
            continue
        if c == "dollar_vol":
            fmt[c] = "{:,.0f}"
        elif c == "days" or (c.startswith("swing_n_") and "_vs_" not in c) or c == "vwap_crosses":
            fmt[c] = "{:.1f}"
        elif pd.api.types.is_numeric_dtype(r[c]):
            fmt[c] = "{:.2f}"

    def row_style(row: pd.Series) -> list[str]:
        if bool(row.at["benchmark"]):
            return ["background-color: #e8e8e8"] * len(row)
        if bool(row.at["low_liquidity"]):
            return ["color: #b00020"] * len(row)
        return [""] * len(row)

    styler: Any = r.style
    return (
        styler.format(fmt, na_rep="—")
        .apply(row_style, axis=1)
        .background_gradient(cmap="YlGn", subset=score_cols, vmin=0, vmax=100)
    )


def fig_quadrant(rep: VolReport) -> Figure:
    r = rep.ranking
    fig, ax = plt.subplots(figsize=(8, 6.5))
    dv = r["dollar_vol"].fillna(0)
    size = 40 + 400 * np.sqrt(dv / dv.max()) if dv.max() > 0 else 80
    colors = [BENCH_COLOR if b else MAIN_COLOR for b in r["benchmark"]]
    ax.scatter(r["score_activity"], r["score_rotation"], s=size, c=colors, alpha=0.6)
    for sym, x, y in zip(r.index, r["score_activity"], r["score_rotation"], strict=True):
        ax.annotate(sym, (x, y), textcoords="offset points", xytext=(6, 4), fontsize=9)
    ax.axvline(50, color="#bbb", lw=1, ls="--")
    ax.axhline(50, color="#bbb", lw=1, ls="--")
    ax.set_xlim(-5, 105)
    ax.set_ylim(-5, 105)
    ax.set_xlabel("活跃度 score_activity")
    ax.set_ylabel("震荡度 score_rotation")
    ax.set_title("右上：波动大且来回震荡（点越大成交额越大，灰色为参照证券）")
    fig.tight_layout()
    return fig


def fig_boxplots(rep: VolReport, top: int = 10) -> Figure:
    r = rep.ranking
    syms = [str(s) for s in rows(r, ~r["benchmark"]).index[:top]] + rep.benchmarks
    d = rows(rep.daily, rep.daily["included"] & rep.daily["symbol"].isin(syms))
    metrics = [
        ("range_pct", "振幅 %"),
        ("body_ratio", "body_ratio"),
        (f"swing_n_{_k(rep)}", "摆动次数"),
    ]
    fig, axes = plt.subplots(len(metrics), 1, figsize=(max(8, 0.7 * len(syms) + 2), 9), sharex=True)
    for ax, (m, label) in zip(axes, metrics, strict=True):
        data = [d.loc[d["symbol"] == s, m].dropna().to_numpy() for s in syms]
        bp = ax.boxplot(data, patch_artist=True, showfliers=True, flierprops={"markersize": 3})
        for patch, s in zip(bp["boxes"], syms, strict=True):
            patch.set_facecolor(BENCH_COLOR if s in rep.benchmarks else "#9ec3ea")
        ax.set_ylabel(label)
        ax.grid(axis="y", alpha=0.3)
    axes[-1].set_xticks(range(1, len(syms) + 1), syms, rotation=45)
    axes[0].set_title(f"前 {top} 名和参照证券：每天的差异")
    fig.tight_layout()
    return fig


def daily_ratio_vs(rep: VolReport, metric: str, ref: str = "SPY") -> pd.Series:
    """每只证券每天的指标 / 同一天参照证券的指标。"""
    d = included(rep.daily)
    base = cast(pd.Series, rows(d, d["symbol"] == ref).set_index("date")[metric])
    other = rows(d, d["symbol"] != ref)
    lookup = base.to_dict()
    b = pd.Series([lookup.get(x, np.nan) for x in other["date"]], index=other.index)
    return (other[metric] / b.where(b > 0)).dropna()


def fig_distributions(rep: VolReport) -> Figure:
    """全部证券、全部交易日的分布，用来以后定分类阈值。"""
    d = included(rep.daily)
    n_col = f"swing_n_{_k(rep)}"
    panels: list[tuple[str, Any]] = [
        ("body_ratio", d["body_ratio"]),
        (n_col, d[n_col]),
        ("efficiency（5 分钟）", d["efficiency"]),
        ("vr_3", d["vr_3"]),
        ("body_ratio / SPY 同日", daily_ratio_vs(rep, "body_ratio")),
        (f"{n_col} / SPY 同日", daily_ratio_vs(rep, n_col)),
    ]
    fig, axes = plt.subplots(2, 3, figsize=(13, 7))
    for ax, (title, s) in zip(axes.flat, panels, strict=True):
        s = s.replace([np.inf, -np.inf], np.nan).dropna()
        if len(s):
            hi = s.quantile(0.99)
            ax.hist(s.clip(upper=hi), bins=40, color=MAIN_COLOR, alpha=0.75)
            ax.axvline(s.median(), color="#d62728", lw=1.2, label=f"中位数 {s.median():.3f}")
            ax.legend(fontsize=8)
        ax.set_title(title, fontsize=10)
    fig.suptitle("指标分布（全部证券、全部交易日）")
    fig.tight_layout()
    return fig


def fig_heatmap(rep: VolReport) -> Figure:
    hm = rep.bucket_rv_share
    fig, ax = plt.subplots(figsize=(11, 0.45 * len(hm) + 1.8))
    if hm.empty:
        return fig
    im = ax.imshow(hm.to_numpy(), aspect="auto", cmap="OrRd")
    labels = []
    for m in hm.columns:
        h, mm = divmod(int(m) + 9 * 60 + 30, 60)
        labels.append(f"{h}:{mm:02d}")
    ax.set_xticks(range(len(hm.columns)), labels, rotation=45)
    ax.set_yticks(range(len(hm.index)), hm.index)
    for i in range(hm.shape[0]):
        for j in range(hm.shape[1]):
            v = hm.iat[i, j]
            if v == v:
                ax.text(j, i, f"{v * 100:.0f}", ha="center", va="center", fontsize=7)
    fig.colorbar(im, ax=ax, label="该时段波动占全天比例")
    ax.set_title("日内波动时间分布（格内数字为百分比，按综合得分排序；美东时间，时段起点）")
    fig.tight_layout()
    return fig


def fig_detail_timeseries(rep: VolReport, symbol: str) -> Figure:
    d = rows(rep.daily, (rep.daily["symbol"] == symbol) & rep.daily["included"]).set_index("date")
    n_col = f"swing_n_{_k(rep)}"
    fig, axes = plt.subplots(3, 1, figsize=(11, 7), sharex=True)
    for ax, (m, label) in zip(
        axes,
        [("range_pct", "振幅 %"), ("body_ratio", "body_ratio"), (n_col, "摆动次数")],
        strict=True,
    ):
        s = d[m]
        ax.plot(pd.to_datetime(s.index), s.to_numpy(), marker=".", lw=1, color=MAIN_COLOR)
        ax.plot(
            pd.to_datetime(s.index),
            s.rolling(10, min_periods=3).median().to_numpy(),
            color="#d62728",
            lw=1,
        )
        ax.set_ylabel(label)
        ax.grid(alpha=0.3)
    axes[0].set_title(f"{symbol} 每日指标（红线为 10 日滚动中位数）")
    fig.tight_layout()
    return fig


def typical_day(rep: VolReport, symbol: str) -> date | None:
    """振幅和摆动次数都最接近该证券中位数的一天。"""
    d = rows(rep.daily, (rep.daily["symbol"] == symbol) & rep.daily["included"])
    if d.empty:
        return None
    n_col = f"swing_n_{_k(rep)}"
    dist = ((d["range_pct"] - d["range_pct"].median()) / (d["range_pct"].std() or 1)).abs() + (
        (d[n_col] - d[n_col].median()) / (d[n_col].std() or 1)
    ).abs()
    return d.loc[dist.idxmin(), "date"]


def _day_bars(rep: VolReport, symbol: str, day: date) -> tuple[Bars, Bars]:
    """一天的 1 分钟 bar（原始、清洗后），复权口径与报表相同。清洗按当天记录的阈值重做一遍。"""
    assert rep.history is not None
    h = rep.history
    td = h.cal.trading_day(day)
    f = split_factor(load_actions(h.data_dir), symbol, day, rep.end)
    raw = to_bars(h.day_bars(symbol, "1m", td, "rth"), f)
    fine_df = h.day_bars(symbol, "1s", td, "rth")
    if fine_df.is_empty():
        return raw, raw
    row = rows(rep.daily, (rep.daily["symbol"] == symbol) & (rep.daily["date"] == day))
    limit = float(row["filter_limit"].iloc[0]) if len(row) else math.nan
    win = [w.ns_range() for w in rep.params.bad_windows or () if w.applies(symbol, day)]
    return raw, clean_day(raw, to_bars(fine_df, f), limit, win).m1


def plot_day(
    ax: Any, rep: VolReport, symbol: str, day: date, compact: bool = False, note: str = ""
) -> None:
    """在 ax 上画一天的 1 分钟价格、VWAP、已确认的摆动（实线）和未确认的最后一段（虚线）。
    清洗改动过的地方，原始的高低价用灰色画在下面。"""
    raw, b = _day_bars(rep, symbol, day)
    t = pd.to_datetime(b.ts, unit="ns", utc=True).tz_convert(NY)
    j = np.searchsorted(b.ts, raw.ts).clip(0, max(len(b) - 1, 0))
    same = (b.ts[j] == raw.ts) & np.isclose(b.high[j], raw.high) & np.isclose(b.low[j], raw.low)
    if len(b) and not same.all():
        diff = ~same
        tr = pd.to_datetime(raw.ts[diff], unit="ns", utc=True).tz_convert(NY)
        ax.vlines(tr, raw.low[diff], raw.high[diff], color="#999999", lw=1.5,
                  label="清洗前的高低（被改动的分钟）")  # fmt: skip
    ax.fill_between(t, b.low, b.high, color="#9ec3ea", alpha=0.5, lw=0, label="1 分钟高低")
    ax.plot(t, b.close, color=MAIN_COLOR, lw=0.8, label="收盘价")
    ax.plot(t, cumulative_vwap(b), color="#ff7f0e", lw=1.2, label="VWAP")
    sw = rep.swings
    k = rep.params.score_k
    sel = (
        rows(sw, (sw["symbol"] == symbol) & (sw["date"] == day) & (sw["k"] == k)) if len(sw) else sw
    )
    done = rows(sel, sel["confirmed"]) if len(sel) else sel
    leg = rows(sel, ~sel["confirmed"]) if len(sel) else sel
    if len(done):
        xs = [done["start"].iloc[0], *done["end"]]
        ys = [done["start_price"].iloc[0], *done["end_price"]]
        ax.plot(xs, ys, color="#d62728", lw=1.5, marker="o", ms=3 if compact else 5,
                label=f"摆动（{k:g} × ADR）")  # fmt: skip
    if len(leg):
        r0 = leg.iloc[0]
        ax.plot([r0["start"], r0["end"]], [r0["start_price"], r0["end_price"]], color="#d62728",
                lw=1.5, ls="--", label="未确认的最后一段")  # fmt: skip
    row = rows(rep.daily, (rep.daily["symbol"] == symbol) & (rep.daily["date"] == day)).iloc[0]
    adr = row["adr"]
    if compact:
        title = f"{symbol} {day}：{note}"
    elif math.isfinite(adr):
        title = (
            f"{symbol} {day}  ADR {adr:.2f}，阈值 θ = {k * adr:.2f}，摆动 {len(done)} 次，"
            f"振幅 {row['range_pct']:.2f}%，body_ratio {row['body_ratio']:.2f}，"
            f"最后一段 {row[f'last_leg_adr_{_k(rep)}']:+.2f} ADR"
        )
    else:
        title = f"{symbol} {day}"
    ax.set_title(title, fontsize=9 if compact else 11)
    ax.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%H:%M", tz=NY))  # type: ignore[attr-defined]
    if not compact:
        ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.3)


def fig_detail_day(rep: VolReport, symbol: str, day: date | None = None) -> Figure:
    """一天的 1 分钟收盘价，叠加 VWAP 和识别出的摆动转折点，用于人工检查摆动算法。"""
    day = day or typical_day(rep, symbol)
    fig, ax = plt.subplots(figsize=(12, 5))
    if day is None or rep.history is None:
        return fig
    plot_day(ax, rep, symbol, day)
    fig.tight_layout()
    return fig


def figs_review_days(rep: VolReport, kind: str, per_fig: int = 8) -> list[Figure]:
    """核对图：kind="data" 数据异常日（按清洗改动的分钟数从多到少），kind="special" 行为特殊日。
    各最多 review_plot_max 张，每张大图 per_fig 个。"""
    if kind == "data":
        t = rep.data_issues.sort_values(["changed_minutes", "fixed_prints"], ascending=False)
        notes = t["data_reason"]
    else:
        t = rep.special_days
        notes = t["special_reason"]
    t = t.head(rep.params.review_plot_max)
    out: list[Figure] = []
    if rep.history is None:
        return out
    for i in range(0, len(t), per_fig):
        part = t.iloc[i : i + per_fig]
        n = len(part)
        nrows = (n + 1) // 2
        fig, axes = plt.subplots(nrows, 2, figsize=(14, 3.2 * nrows), squeeze=False)
        for ax, (idx, r) in zip(axes.flat, part.iterrows(), strict=False):
            plot_day(ax, rep, str(r["symbol"]), r["date"], compact=True, note=str(notes.at[idx]))
        for ax in list(axes.flat)[n:]:
            ax.axis("off")
        fig.tight_layout()
        out.append(fig)
    return out


def styled_sensitivity(rep: VolReport):  # noqa: ANN201
    """摆动阈值敏感性表：名次随阈值变化越小，排名越不依赖阈值的选择。"""
    t = rep.sensitivity
    styler: Any = t.style
    fmt = {c: ("{:.0f}" if c[1] in ("rank", "score_total") else "{:.2f}") for c in t.columns}
    ranks = [c for c in t.columns if c[1] == "rank"]
    return styler.format(fmt, na_rep="—").background_gradient(cmap="YlGn_r", subset=ranks)


def styled_open_skip(rep: VolReport):  # noqa: ANN201
    """开盘剔除对照表：名次越绿越靠前。"""
    t = rep.open_skip
    styler: Any = t.style
    fmt = {c: ("{:.0f}" if c[1] in ("rank", "score_total") else "{:.2f}") for c in t.columns}
    ranks = [c for c in t.columns if c[1] == "rank"]
    return styler.format(fmt, na_rep="—").background_gradient(cmap="YlGn_r", subset=ranks)


LEVERAGE_NOTE = (
    "杠杆 ETF 的震荡形态与标的基本相同，二者应视为同一机会，根据资金规模和实际价差二选一。"
    "range_ratio 是每天振幅之比的中位数，可以和名义杠杆倍数对比；"
    "corr_5m 是两者 5 分钟收益率的相关系数。"
)

ETH_NOTE = (
    "**仅供参考，不参与打分。** 跳空拆成三段：前一日盘后（gap_post）、无交易时段（gap_overnight）、"
    "当日盘前（gap_pre），单位是 ADR；三段之和等于 gap_adr。*_share 是各段绝对值占三段合计的比例。"
)


def fig_eth_scatter(rep: VolReport) -> tuple[Figure, float, float]:
    """当日盘前振幅 pm_range_adr 与常规时段振幅 range_adr 的散点图（全部证券、全部交易日）。
    返回 (图, Pearson 相关, Spearman 相关)。"""
    d = included(rep.daily)
    fig, ax = plt.subplots(figsize=(7, 6))
    if "pm_range_adr" not in d.columns:
        return fig, math.nan, math.nan
    x = d[["pm_range_adr", "range_adr"]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(x) < 3:
        return fig, math.nan, math.nan
    pear = corr(x["pm_range_adr"], x["range_adr"])
    spear = corr(x["pm_range_adr"], x["range_adr"], rank=True)
    colors = [BENCH_COLOR if s in rep.benchmarks else MAIN_COLOR for s in d.loc[x.index, "symbol"]]
    ax.scatter(x["pm_range_adr"], x["range_adr"], s=8, alpha=0.4, c=colors)
    ax.set_xlabel("盘前振幅 pm_range_adr")
    ax.set_ylabel("常规时段振幅 range_adr")
    ax.set_title(f"盘前活跃度与当天振幅（Pearson {pear:.2f}，Spearman {spear:.2f}）")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig, pear, spear


# 指标和分数的说明（Markdown），notebook 第 9 部分显示
METRIC_NOTES = Path(__file__).with_name("metric_notes.md").read_text(encoding="utf-8")
