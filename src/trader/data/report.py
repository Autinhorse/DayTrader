"""数据覆盖与校验报告：命令行 `trader data report` 和界面的数据页共用。"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from trader.core.trading_calendar import TradingCalendar
from trader.data.catalog import Catalog
from trader.data.corporate_actions import load_actions


def data_report(data_dir: Path, catalog: Catalog, cal: TradingCalendar, symbols: list[str]) -> str:
    actions = load_actions(data_dir)
    lines: list[str] = []
    out = lines.append
    out(f"数据目录：{data_dir}")
    out(
        f"{'标的':<6} {'起始':<10} {'结束':<10} {'有数据':>6} {'无数据':>6} {'未下载':>6} "
        f"{'总行数':>12} {'有长空档天数':>10}  提示汇总"
    )
    details: list[str] = []
    for sym in symbols:
        parts = catalog.partitions(sym)
        have = [p for p in parts if p.rows > 0]
        if not have:
            out(f"{sym:<6} 没有数据")
            continue
        first, last = have[0].day, have[-1].day
        known = {p.day for p in parts}
        missing = [d.day for d in cal.trading_days(first, last) if d.day not in known]
        empty_inside = [p.day for p in parts if p.rows == 0 and first <= p.day <= last]
        warn: dict[str, int] = {}
        for p in have:
            for k, v in p.warnings.items():
                if k not in ("rth_gaps", "vwap_outside_range"):
                    warn[k] = warn.get(k, 0) + v
        gap_days = [p for p in have if p.gaps]
        minutes = len([p for p in catalog.partitions(sym, "bar_1m") if p.rows > 0])
        out(
            f"{sym:<6} {first!s:<10} {last!s:<10} {len(have):>6} {len(empty_inside):>6} "
            f"{len(missing):>6} {sum(p.rows for p in have):>12,} {len(gap_days):>10}  "
            + (", ".join(f"{k}={v:,}" for k, v in sorted(warn.items())) or "-")
            + f"；官方 1 分钟 bar {minutes} 天"
        )
        if missing:
            details.append(
                f"{sym} 未下载的交易日：{', '.join(map(str, missing[:20]))}"
                + (" ..." if len(missing) > 20 else "")
            )
        if empty_inside:
            details.append(f"{sym} 交易日但没有任何 bar：{', '.join(map(str, empty_inside[:20]))}")
        if gap_days:
            worst = sorted(gap_days, key=lambda p: -sum(e - s for s, e in p.gaps))[:3]
            desc = "; ".join(
                f"{p.day} 共 {sum(e - s for s, e in p.gaps) / 60e9:.0f} 分钟/{len(p.gaps)} 段"
                for p in worst
            )
            details.append(f"{sym} 常规时段长空档最多的日子：{desc}")
        splits = actions.filter((pl.col("symbol") == sym) & (pl.col("kind") == "split"))
        for r in splits.iter_rows(named=True):
            if first <= r["ex_date"] <= last:
                details.append(f"{sym} 区间内拆股：{r['ex_date']} 比例 {r['ratio']:g}")
    if details:
        out("")
        out("明细：")
        lines.extend(f"  {d}" for d in details)
    out("")
    out("说明：校验错误（重复时间、时间倒序、非正价格、负成交量）的日期不会写入，显示为“未下载”。")
    out("      长空档：常规时段内连续 60 秒以上没有 bar。")
    out("      vwap 落在当秒最高最低价之外的 bar 很常见，是 Massive 的正常现象，不在上表汇总。")
    return "\n".join(lines)
