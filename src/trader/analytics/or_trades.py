"""开盘区间交易的快速模拟（决策 0016）：用清洗后的 1 分钟 bar，逐日模拟几种简单规则，
看"在开盘区间边缘反向""等假突破收回再反向""追突破"扣成本后能不能赚钱，按季度分开统计。

    days = load_minutes(tickers, "2024-01-02", "2026-10-15")   # 慢（读 1 秒 bar 清洗），请缓存
    t = simulate(days, Rule("fade", target="mid"))              # 每笔交易一行
    summarize(t, "quarter")                                      # 每季度一行

这是筛选用的近似模拟，不是正式回测（正式回测用 user/strategies + trader.backtest）：
- 只用 1 分钟 bar 的开高低收，同一根 bar 里止损和目标都到了，按先止损算（保守）；
- 入场那根 bar 只检查止损，不算止盈；
- 限价单（边缘反向的入场、止盈）按限价成交，越过限价开盘的按开盘价；
  止损单、市价单（收回后入场、收盘平仓）另加 slip_adr 的滑点；
- 成本按报表的分档（当天实际开盘价）买卖各扣一次；
- 每只证券每天最多一笔，last_entry_min 以后不再开仓，exit_min 时按收盘价平仓；
- VWAP 是开盘起的累计 VWAP。挂单类判断（边缘反向、突破跟随的入场，VWAP 目标）
  用到上一根 bar 为止的 VWAP，收盘入场（假突破收回）用到入场 bar 为止的 VWAP。
  VWAP 条件只检查当天第一个信号，不满足这天就不做。
开盘区间只用第 n 分钟以前的 bar，信号只用已经走完的 bar，没有未来数据。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, cast

import numpy as np
import pandas as pd

from trader.analytics.daily_metrics import MIN_NS, cumulative_vwap
from trader.analytics.frames import col, rows
from trader.analytics.loader import load_symbol
from trader.analytics.params import ReportParams
from trader.data.history import HistoryService

KINDS = {
    "fade": "边缘反向：第一次碰到区间上沿做空 / 下沿做多（限价）",
    "reclaim": "假突破收回：突破以后，第一根收回区间里的 bar 收盘反向入场",
    "breakout": "突破跟随（对照）：第一次突破上沿做多 / 下沿做空（止损单）",
}
TARGETS = {
    "mid": "区间中线",
    "opposite": "区间对边",
    "vwap": "VWAP（每分钟按最新 VWAP 调整的限价单）",
    "close": "不设目标，持有到收盘",
}
VWAP_SIDES = {
    "any": "不看 VWAP",
    "toward": "交易方向朝着 VWAP（做空时价格在 VWAP 上方、做多时在下方）：回归 VWAP",
    "away": "交易方向背离 VWAP（做空时价格已在 VWAP 下方、做多时在上方）：顺着当天的强弱",
}
CACHE_VERSION = 2  # MinuteDay 字段变化时加 1，notebook 的缓存随之失效
REASONS = ("目标", "止损", "收盘")


@dataclass(frozen=True, slots=True)
class MinuteDay:
    """一只证券一天的常规时段 1 分钟 bar（清洗后、复权）。minute 是距开盘的分钟数（bar 起点）。"""

    symbol: str
    date: date
    minute: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    adr: float  # 前一交易日的 ADR
    cost_pct: float  # 单边成本（百分数）
    vwap: np.ndarray | None = None  # 开盘起到这根 bar（含）为止的累计 VWAP；None 时 VWAP 规则不可用


@dataclass(frozen=True, slots=True)
class Rule:
    """kind：fade / reclaim / breakout（KINDS）；n：开盘区间分钟数；target：mid / opposite / close。
    止损：fade 在区间边缘之外 stop_adr；reclaim 在突破以来的极值之外 stop_adr；
    breakout 在区间中线（stop_at="mid"）或对边（"opposite"）。距离都以 ADR 为单位。
    vwap_side：any / toward / away（VWAP_SIDES）；vwap_min_adr：入场价离 VWAP 至少多远（ADR）；
    vwap_slope_min：入场前 30 分钟 VWAP 的变化下限（见 simulate 的 vwap_slope）。"""

    kind: str
    n: int = 60
    target: str = "mid"
    stop_adr: float = 0.15
    stop_at: str = "mid"  # 只用于 breakout
    slip_adr: float = 0.01
    last_entry_min: int = 330  # 15:00 以后不开新仓
    exit_min: int = 385  # 15:55 这根 bar 开始前平仓（用 15:54 那根 bar 的收盘价）
    vwap_side: str = "any"
    vwap_min_adr: float = 0.0
    # 入场前 30 分钟 VWAP 朝交易方向的变化至少这么多（ADR）；None = 不看。
    # 例如 -0.025：VWAP 逆着交易方向移动不超过 0.025 ADR（反向交易时"当天的趋势不太强"）
    vwap_slope_min: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind 只能是 {'/'.join(KINDS)}")
        if self.target not in TARGETS:
            raise ValueError(f"target 只能是 {'/'.join(TARGETS)}")
        if self.vwap_side not in VWAP_SIDES:
            raise ValueError(f"vwap_side 只能是 {'/'.join(VWAP_SIDES)}")
        if self.target == "vwap" and self.vwap_side == "away":
            raise ValueError("背离 VWAP 的交易不能以 VWAP 为目标（目标在亏损的一边）")

    @property
    def label(self) -> str:
        v = ""
        if self.vwap_side != "any":
            v = " 朝VWAP" if self.vwap_side == "toward" else " 背离VWAP"
            if self.vwap_min_adr > 0:
                v += f"≥{self.vwap_min_adr:g}ADR"
        if self.vwap_slope_min is not None:
            v += f" VWAP斜率≥{self.vwap_slope_min:g}"
        if self.kind == "breakout":
            return f"突破跟随 n={self.n} 止损{'中线' if self.stop_at == 'mid' else '对边'}{v}"
        name = "边缘反向" if self.kind == "fade" else "假突破收回"
        tgt = "VWAP" if self.target == "vwap" else TARGETS[self.target]
        return f"{name} n={self.n} 目标{tgt} 止损{self.stop_adr:g}ADR{v}"


def load_minutes(
    tickers: list[str],
    start: str | date,
    end: str | date,
    params: ReportParams | None = None,
    history: HistoryService | None = None,
) -> list[MinuteDay]:
    """读每只证券每天清洗后的 1 分钟 bar（与日内波动报表同样的清洗和复权）。
    跳过提前收盘日和 ADR 数据不足的日子。读 1 秒 bar 清洗比较慢，notebook 里请缓存结果。"""
    from trader.analytics.pipeline import _d, _history, with_bad_windows

    p = replace(with_bad_windows(params or ReportParams()), include_extended_hours=False)
    h = history or _history()
    out: list[MinuteDay] = []
    for sym in dict.fromkeys(t.upper() for t in tickers):
        if not h.coverage(sym):
            continue
        sd = load_symbol(h, sym, _d(start), _d(end), p)
        for day, inp in sorted(sd.inputs.items()):
            if day in sd.early_close or not math.isfinite(inp.adr) or inp.adr <= 0:
                continue
            m = inp.m1
            raw = inp.raw_open if math.isfinite(inp.raw_open) else float(m.open[0])
            out.append(
                MinuteDay(
                    symbol=sym,
                    date=day,
                    minute=((m.ts - inp.day_open) // MIN_NS).astype(np.int16),
                    open=m.open.astype(np.float32),
                    high=m.high.astype(np.float32),
                    low=m.low.astype(np.float32),
                    close=m.close.astype(np.float32),
                    adr=float(inp.adr),
                    cost_pct=p.cost_pct(raw),
                    vwap=cumulative_vwap(m).astype(np.float32),
                )
            )
    return out


def simulate(days: list[MinuteDay], rule: Rule) -> pd.DataFrame:
    """每笔交易一行：symbol、date、side（1 多 / -1 空）、or_width（区间宽度，ADR）、
    entry_min / exit_min（距开盘分钟）、entry / exit（价格）、reason（目标 / 止损 / 收盘）、
    risk_adr（入场到止损，ADR）、ret_pct（扣成本后的收益率，%）、ret_adr（扣成本后，ADR）、
    r（扣成本后的收益 / 入场时的风险）；cost_pct 这笔交易的买卖成本合计（%）；
    vwap_dist 入场时 VWAP 在交易方向上的距离（ADR，正 = 朝着 VWAP，负 = 背离；没有 VWAP 为空）；
    vwap_slope 入场前 30 分钟 VWAP 的变化（ADR，正 = VWAP 正朝交易方向移动）。
    注意：开盘区间边缘的交易，VWAP 几乎总在区间里面，所以反向交易几乎都"朝着 VWAP"、
    突破跟随几乎都"背离 VWAP"，有区分度的是距离 vwap_dist 和斜率 vwap_slope。"""
    found = [t for d in days if (t := _one_day(d, rule)) is not None]
    cols = ["symbol", "date", "side", "or_width", "entry_min", "exit_min", "entry", "exit",
            "reason", "risk_adr", "ret_pct", "ret_adr", "r", "cost_pct",
            "vwap_dist", "vwap_slope"]  # fmt: skip
    return pd.DataFrame(found, columns=cols)


def _one_day(d: MinuteDay, rule: Rule) -> dict[str, Any] | None:
    mi = d.minute
    head = mi < rule.n
    if not head.any() or head.all():
        return None
    hi = float(d.high[head].max())
    lo = float(d.low[head].min())
    o, h, low, c = (x.astype(np.float64) for x in (d.open, d.high, d.low, d.close))
    adr = d.adr
    slip = rule.slip_adr * adr
    mid = (hi + lo) / 2
    start = int(np.argmax(~head))
    last_bar = int(np.searchsorted(mi, rule.exit_min)) - 1  # 平仓用的 bar
    if last_bar <= start:
        return None
    entry = None
    for i in range(start, last_bar):
        if mi[i] >= rule.last_entry_min:
            break
        up, dn = h[i] > hi, low[i] < lo
        if rule.kind == "fade":
            if up and dn:
                return None  # 一根 bar 两边都碰到，分不清先后，这天不做
            if up:
                entry = (i, -1, max(hi, o[i]), hi + rule.stop_adr * adr)
            elif dn:
                entry = (i, 1, min(lo, o[i]), lo - rule.stop_adr * adr)
        elif rule.kind == "breakout":
            if up and dn:
                return None
            opp = rule.stop_at == "opposite"
            if up:
                entry = (i, 1, max(hi, o[i]) + slip, lo if opp else mid)
            elif dn:
                entry = (i, -1, min(lo, o[i]) - slip, hi if opp else mid)
        else:
            entry = _reclaim_entry(i, h, low, c, mi, hi, lo, rule, adr, last_bar)
            if entry == "none":
                return None
        if entry is not None:
            break
    if entry is None or not isinstance(entry, tuple):
        return None
    i, side, px, stop = entry
    uses_vwap = (
        rule.vwap_side != "any"
        or rule.vwap_slope_min is not None
        or (rule.target == "vwap" and rule.kind != "breakout")
    )
    if uses_vwap and d.vwap is None:
        raise ValueError("这批数据没有 VWAP，请用新版 load_minutes 重新读取")
    vw = d.vwap.astype(np.float64) if d.vwap is not None else None
    # 入场时已知的 VWAP：收盘入场用到这根 bar 为止的，挂单入场用到上一根为止的
    k = i if rule.kind == "reclaim" else i - 1
    vw_now = math.nan if vw is None else float(vw[k])
    # VWAP 最近 30 分钟的变化，正 = 朝交易方向移动（做空时 VWAP 在下降）
    vw_slope = math.nan if vw is None else side * (vw_now - float(vw[max(k - 30, 0)])) / adr
    if rule.vwap_side != "any":
        dist = side * (vw_now - px)  # > 0：VWAP 在交易方向上
        if rule.vwap_side == "away":
            dist = -dist
        if dist <= 0 or dist < rule.vwap_min_adr * adr:
            return None
    if rule.vwap_slope_min is not None and not vw_slope >= rule.vwap_slope_min:
        return None
    n_bars = len(mi)
    if rule.kind == "breakout" or rule.target == "close":
        tgt = np.full(n_bars, math.nan)
    elif rule.target == "vwap":
        assert vw is not None
        tgt = np.concatenate([[math.nan], vw[:-1]])  # 每根 bar 开始时按上一根为止的 VWAP 挂单
    else:
        tgt = np.full(n_bars, mid if rule.target == "mid" else (lo if side < 0 else hi))
    target0 = vw_now if rule.target == "vwap" and rule.kind != "breakout" else float(tgt[i])
    if side * (stop - px) >= 0 or (math.isfinite(target0) and side * (target0 - px) <= 0):
        return None  # 入场价已经在止损或目标之外（跳空），不做
    ex, reason, j = _exit(i, side, px, stop, tgt, o, h, low, c, slip, last_bar)
    cost = d.cost_pct / 100 * (px + ex)
    pnl = side * (ex - px) - cost
    risk = abs(px - stop)
    return {
        "symbol": d.symbol,
        "date": d.date,
        "side": side,
        "or_width": (hi - lo) / adr,
        "entry_min": int(mi[i]),
        "exit_min": int(mi[j]) + 1 if reason == "收盘" else int(mi[j]),
        "entry": px,
        "exit": ex,
        "reason": reason,
        "risk_adr": risk / adr,
        "ret_pct": pnl / px * 100,
        "ret_adr": pnl / adr,
        "r": pnl / risk,
        "cost_pct": cost / px * 100,
        "vwap_dist": side * (vw_now - px) / adr if math.isfinite(vw_now) else math.nan,
        "vwap_slope": vw_slope,
    }


def _reclaim_entry(
    i: int, h: np.ndarray, low: np.ndarray, c: np.ndarray, mi: np.ndarray,
    hi: float, lo: float, rule: Rule, adr: float, last_bar: int,
) -> tuple[int, int, float, float] | str | None:  # fmt: skip
    """第 i 根 bar 第一次突破（之前都在区间里）。向后找第一根收盘回到区间里的 bar，
    在它收盘时反向入场；止损放在突破以来的极值之外 stop_adr。
    收回之前碰到另一边、或到了不再开仓的时刻，这天不做。"""
    up, dn = h[i] > hi, low[i] < lo
    if not (up or dn):
        return None
    if up and dn:
        return "none"
    side = -1 if up else 1
    ext = h[i] if up else low[i]
    for j in range(i, last_bar):
        if mi[j] >= rule.last_entry_min:
            return "none"
        if j > i:
            if (up and low[j] < lo) or (dn and h[j] > hi):
                return "none"
            ext = max(ext, h[j]) if up else min(ext, low[j])
        if (up and c[j] < hi) or (dn and c[j] > lo):
            slip = rule.slip_adr * adr
            return (j, side, float(c[j]) - side * slip, ext - side * rule.stop_adr * adr)
    return "none"


def _exit(
    i: int, side: int, px: float, stop: float, tgt: np.ndarray,
    o: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, slip: float, last_bar: int,
) -> tuple[float, str, int]:  # fmt: skip
    """入场后逐根 bar 检查：先止损（同一根 bar 里止损和目标都到了按止损），
    再目标（tgt 是每根 bar 的目标价，nan = 没有目标），最后收盘平仓。"""
    for j in range(i, last_bar + 1):
        adverse = h[j] if side < 0 else low[j]
        if side * (stop - adverse) >= 0:  # 碰到止损
            fill = stop if j == i else (max(stop, o[j]) if side < 0 else min(stop, o[j]))
            return fill - side * slip, "止损", j
        target = float(tgt[j])
        if j > i and math.isfinite(target):
            fav = low[j] if side < 0 else h[j]
            if side * (fav - target) >= 0:
                # 目标已经在不利的一边（VWAP 移过了入场价）：限价单立即按开盘价成交
                fill = target if side * (target - o[j]) >= 0 else float(o[j])
                return fill, "目标", j
    return float(c[last_bar]) - side * slip, "收盘", last_bar


def add_period(trades: pd.DataFrame, freq: str = "Q") -> pd.DataFrame:
    t = trades.copy()
    t["quarter"] = pd.to_datetime(col(t, "date")).dt.to_period(freq).astype(str)
    return t


def summarize(trades: pd.DataFrame, by: str | list[str] | None = "quarter") -> pd.DataFrame:
    """每组一行：trades 笔数；win 盈利笔数占比；avg_pct 平均每笔收益（%，扣成本）；
    gross_pct 不扣成本（仍含滑点）的平均每笔收益（%）；avg_adr 平均每笔收益（ADR）；
    avg_r 平均 R；pf 盈亏比（总盈利 / 总亏损）；
    sum_pct 收益率之和（%，每笔等额本金）；target / stop / close 三种出场的占比；
    hold 平均持仓分钟。"""
    if by == "quarter" and "quarter" not in trades.columns:
        trades = add_period(trades)

    def agg(g: pd.DataFrame) -> pd.Series:
        r = col(g, "ret_pct")
        loss = float(-r[r < 0].sum())
        vc = col(g, "reason").value_counts(normalize=True)
        reason = {str(k): float(v) for k, v in vc.items()}
        return pd.Series(
            {
                "trades": float(len(g)),
                "win": float((r > 0).mean()),
                "avg_pct": float(r.mean()),
                "gross_pct": float((r + col(g, "cost_pct")).mean()),
                "avg_adr": float(col(g, "ret_adr").mean()),
                "avg_r": float(col(g, "r").mean()),
                "pf": float(r[r > 0].sum()) / loss if loss > 0 else math.nan,
                "sum_pct": float(r.sum()),
                "target": float(reason.get("目标", 0.0)),
                "stop": float(reason.get("止损", 0.0)),
                "close": float(reason.get("收盘", 0.0)),
                "hold": float((col(g, "exit_min") - col(g, "entry_min")).mean()),
            }
        )

    if trades.empty:
        return pd.DataFrame()
    total = agg(trades)
    if by is None:
        return total.to_frame("全部").T
    t = pd.DataFrame({k: agg(cast(pd.DataFrame, g)) for k, g in trades.groupby(by)}).T
    if not isinstance(by, str):  # 多层分组不加合计行
        t.index.names = by
        return t
    t = pd.concat([t, total.to_frame("全部").T])
    t.index.name = by
    return t


def compare_rules(days: list[MinuteDay], rules: list[Rule], by: str = "quarter") -> pd.DataFrame:
    """几条规则并排：行是分组（默认季度），列是 (规则, 指标)，指标取 trades、win、avg_pct、pf。"""
    parts = {}
    for r in rules:
        s = summarize(simulate(days, r), by)
        parts[r.label] = s[["trades", "win", "avg_pct", "pf"]]
    return pd.concat(parts, axis=1)


def with_features(trades: pd.DataFrame, feats: pd.DataFrame, names: list[str]) -> pd.DataFrame:
    """把第 n 分钟已知的特征（opening.features 的输出）按 symbol、date 接到每笔交易上。"""
    keep = ["symbol", "date", *[x for x in names if x not in ("symbol", "date")]]
    f = feats[keep].drop(columns=[x for x in names if x in trades.columns], errors="ignore")
    return trades.merge(f, on=["symbol", "date"], how="left")


def fixed_bins(trades: pd.DataFrame, feature: str, train: pd.Series, q: int = 3) -> pd.Series:
    """用训练段（train 为 True 的行）的分位数定边界，再给全部交易分档：
    边界不看要评估的那一段，避免"用将来的分布挑日子"。"""
    x = col(trades, feature)
    vals = cast(pd.Series, x[train]).to_numpy(dtype=float)
    edges = np.unique(np.nanquantile(vals, np.linspace(0, 1, q + 1)))
    edges[0], edges[-1] = -np.inf, np.inf
    return cast(pd.Series, pd.cut(x, edges))


def by_bin(trades: pd.DataFrame, feature: str, bins: pd.Series) -> pd.DataFrame:
    """每个季度 × 每档一行的汇总（行索引为 (季度, 档)），只用有档位的交易。"""
    t = add_period(trades) if "quarter" not in trades.columns else trades.copy()
    t[feature + "_bin"] = bins.astype(str)
    t = rows(t, bins.notna().to_numpy())
    return summarize(t, ["quarter", feature + "_bin"])
