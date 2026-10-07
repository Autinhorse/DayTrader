"""IBKR 录制数据与 Massive 1 秒 bar 的对比（DESIGN.md 5.7）。

IBKR 侧有两种来源，分别与 Massive 比较：
  - 逐笔成交（tbt.jsonl）：按成交时间聚合成 1 秒 bar（IB 逐笔时间只精确到秒）；
  - 普通流式行情快照（mkt.jsonl）：按接收时间分秒，价格取最新价，成交量取累计量的差值。
    快照不是完整成交流，最高最低价和成交量天然偏小，相当于设计里的"降级"行情。
系统不抹平差异，只把偏差量化出来，供决定策略使用的 bar 周期和订阅方式。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC, from_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.store import BarStore

OHLCV = ["open", "high", "low", "close", "volume"]


def _bucket(col: str, size: int) -> pl.Expr:
    return (pl.col(col) // size * size).alias("ts_start")


def bars_from_trades(trades: pl.DataFrame, size: int = NS_PER_SEC) -> pl.DataFrame:
    """trades: ts, price, size → 按 ts 分桶的 OHLCV。"""
    if trades.is_empty():
        return pl.DataFrame(schema={"ts_start": pl.Int64, **dict.fromkeys(OHLCV, pl.Float64)})
    return (
        trades.sort("ts", maintain_order=True)
        .group_by(_bucket("ts", size), maintain_order=True)
        .agg(
            pl.col("price").first().alias("open"),
            pl.col("price").max().alias("high"),
            pl.col("price").min().alias("low"),
            pl.col("price").last().alias("close"),
            pl.col("size").sum().cast(pl.Float64).alias("volume"),
        )
        .sort("ts_start")
    )


def bars_from_snapshots(snaps: pl.DataFrame, size: int = NS_PER_SEC) -> pl.DataFrame:
    """snaps: recv, last, volume（累计）→ 按接收时间分桶。

    成交量 = 本桶末累计量 − 上一桶末累计量。
    """
    s = snaps.filter(pl.col("last").is_not_null()).sort("recv", maintain_order=True)
    if s.is_empty():
        return pl.DataFrame(schema={"ts_start": pl.Int64, **dict.fromkeys(OHLCV, pl.Float64)})
    bars = s.group_by(_bucket("recv", size), maintain_order=True).agg(
        pl.col("last").first().alias("open"),
        pl.col("last").max().alias("high"),
        pl.col("last").min().alias("low"),
        pl.col("last").last().alias("close"),
        pl.col("volume").drop_nulls().max().alias("cum"),
    )
    return (
        bars.sort("ts_start")
        .with_columns(pl.col("cum").forward_fill())
        .with_columns(pl.col("cum").diff().fill_null(0).clip(lower_bound=0).alias("volume"))
        .select("ts_start", *OHLCV)
        .cast({"volume": pl.Float64})
    )


def resample(bars: pl.DataFrame, size: int) -> pl.DataFrame:
    return (
        bars.sort("ts_start")
        .group_by(_bucket("ts_start", size), maintain_order=True)
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
        )
        .sort("ts_start")
    )


def diff_stats(ref: pl.DataFrame, other: pl.DataFrame) -> dict[str, float]:
    """ref = Massive，other = IBKR。价格偏差单位是美分。"""
    j = ref.join(other, on="ts_start", how="inner", suffix="_ib")
    out: dict[str, float] = {
        "massive_bars": ref.height,
        "ib_bars": other.height,
        "matched": j.height,
        "only_massive": ref.height - j.height,
        "only_ib": other.height - j.height,
    }
    if j.height:
        for c in ("close", "high", "low"):
            d = ((pl.col(f"{c}_ib") - pl.col(c)).abs() * 100).alias("d")
            col = j.select(d)["d"]
            out[f"{c}_mean_cents"] = float(col.mean() or 0)  # type: ignore[arg-type]
            out[f"{c}_p95_cents"] = float(col.quantile(0.95) or 0)
            out[f"{c}_max_cents"] = float(col.max() or 0)  # type: ignore[arg-type]
            out[f"{c}_exact_pct"] = float((col < 0.005).mean() or 0) * 100  # type: ignore[arg-type]
    vol_ref = float(ref["volume"].sum())
    out["volume_ratio"] = float(other["volume"].sum()) / vol_ref if vol_ref else float("nan")
    return out


def _format(title: str, st: dict[str, float]) -> list[str]:
    lines = [
        f"  {title}",
        f"    bar 数：Massive {st['massive_bars']:,.0f}，IBKR {st['ib_bars']:,.0f}，"
        f"共同 {st['matched']:,.0f}，只在 Massive {st['only_massive']:,.0f}，"
        f"只在 IBKR {st['only_ib']:,.0f}",
    ]
    if st["matched"]:
        for c, name in (("close", "收盘价"), ("high", "最高价"), ("low", "最低价")):
            lines.append(
                f"    {name}偏差（美分）：平均 {st[f'{c}_mean_cents']:.2f}，"
                f"95% 分位 {st[f'{c}_p95_cents']:.2f}，最大 {st[f'{c}_max_cents']:.2f}，"
                f"完全相同 {st[f'{c}_exact_pct']:.1f}%"
            )
    lines.append(f"    成交量比例 IBKR / Massive：{st['volume_ratio']:.3f}")
    return lines


def _read_recording(path: Path) -> pl.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pl.DataFrame()
    return pl.read_ndjson(path, infer_schema_length=None)


def compare_day(
    day: date,
    data_dir: Path,
    symbols: list[str] | None = None,
    out_dir: Path | None = None,
) -> int:
    """对比录制日的全部（或指定）标的，打印汇总表并把明细保存到 runs/compare/。

    只比较常规时段与实际录制时间的重叠部分。
    """
    cal = TradingCalendar()
    if not cal.is_trading_day(day):
        print(f"{day} 不是交易日")
        return 1
    td = cal.trading_day(day)
    live = Path(data_dir) / "live" / day.isoformat()
    tbt_all = _read_recording(live / "tbt.jsonl")
    mkt_all = _read_recording(live / "mkt.jsonl")
    if tbt_all.is_empty() and mkt_all.is_empty():
        print(f"没有找到 {live} 下的录制数据")
        return 1
    recv = pl.concat([df.select("recv") for df in (tbt_all, mkt_all) if not df.is_empty()])
    rec_lo = -(-int(recv["recv"].min()) // NS_PER_MIN) * NS_PER_MIN  # type: ignore[arg-type]
    rec_hi = int(recv["recv"].max()) // NS_PER_MIN * NS_PER_MIN  # type: ignore[arg-type]
    lo, hi = max(td.open, rec_lo), min(td.close, rec_hi)
    if hi <= lo:
        print("录制时间与常规时段没有重叠")
        return 1

    def window(df: pl.DataFrame) -> pl.DataFrame:
        return df.filter((pl.col("ts_start") >= lo) & (pl.col("ts_start") < hi))

    recorded = sorted(
        set(tbt_all["sym"].unique().to_list() if not tbt_all.is_empty() else [])
        | set(mkt_all["sym"].unique().to_list() if not mkt_all.is_empty() else [])
    )
    symbols = [s for s in (symbols or recorded) if s in recorded]
    store = BarStore(data_dir)
    span = f"{from_ns(lo):%H:%M}~{from_ns(hi):%H:%M}"
    header = f"IBKR 与 Massive 对比 {day}，比较时段 {span}（纽约时间，常规时段内）"
    detail = [header]
    table = [
        header,
        "",
        f"{'标的':<6} {'IBKR 来源':<18} {'1秒:IB有bar的比例':>16} {'1秒收盘相同':>10} "
        f"{'1分收盘相同':>10} {'1分收盘平均偏差':>14} {'1分高低相同':>10} {'成交量比':>8}",
    ]
    for sym in symbols:
        massive = window(store.read_day(sym, day).select("ts_start", *OHLCV))
        if massive.is_empty():
            table.append(f"{sym:<6} 本地没有 Massive 数据，先运行 trader data update")
            continue
        sources: list[tuple[str, pl.DataFrame]] = []
        tbt = tbt_all.filter(pl.col("sym") == sym) if not tbt_all.is_empty() else tbt_all
        if not tbt.is_empty():
            trades = tbt.select("ts", "price", "size", "unreported")
            sources.append(("逐笔（全部）", bars_from_trades(trades)))
            sources.append(
                ("逐笔（去掉unreported）", bars_from_trades(trades.filter(~pl.col("unreported"))))
            )
        mkt = mkt_all.filter(pl.col("sym") == sym) if not mkt_all.is_empty() else mkt_all
        if not mkt.is_empty():
            sources.append(
                ("快照（降级）", bars_from_snapshots(mkt.select("recv", "last", "volume")))
            )
        detail += ["", f"===== {sym} ====="]
        for name, ib_bars in sources:
            ib_bars = window(ib_bars)
            s1 = diff_stats(massive, ib_bars)
            m1 = diff_stats(resample(massive, NS_PER_MIN), resample(ib_bars, NS_PER_MIN))
            detail += [f"[{name}]", *_format("1 秒 bar", s1), *_format("1 分钟 bar", m1)]
            cover = s1["matched"] / s1["massive_bars"] * 100 if s1["massive_bars"] else 0
            hl = (m1.get("high_exact_pct", 0) + m1.get("low_exact_pct", 0)) / 2
            table.append(
                f"{sym:<6} {name:<18} {cover:>15.1f}% {s1.get('close_exact_pct', 0):>9.1f}% "
                f"{m1.get('close_exact_pct', 0):>9.1f}% "
                f"{m1.get('close_mean_cents', 0):>11.2f} 美分 "
                f"{hl:>9.1f}% {s1['volume_ratio']:>8.3f}"
            )
    notes = [
        "",
        "说明：",
        "  1秒:IB有bar的比例 = Massive 有成交的秒里，IBKR 也有成交的比例。",
        "  “相同”指价格差小于 0.5 美分；高低相同取最高价和最低价两者的平均。",
        "  IB 逐笔成交的时间只精确到秒；快照按本机接收时间分秒，会比真实成交晚约 0.3~1 秒。",
        "  快照的成交量取 IB 累计成交量的差值。",
    ]
    table += notes
    text = "\n".join(table)
    print(text)
    out_dir = out_dir or Path(data_dir).parent / "runs" / "compare"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{day.isoformat()}_summary.txt").write_text(text + "\n", encoding="utf-8")
    (out_dir / f"{day.isoformat()}_detail.txt").write_text(
        "\n".join(detail) + "\n", encoding="utf-8"
    )
    print(f"\n汇总和明细已保存到 {out_dir}")
    return 0
