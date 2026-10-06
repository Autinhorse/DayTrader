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

from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC
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
    """snaps: recv, last, volume（累计）→ 按接收时间分桶。成交量 = 本桶末累计量 − 上一桶末累计量。"""
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


def _read_jsonl(path: Path, symbol: str) -> pl.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pl.DataFrame()
    return pl.read_ndjson(path, infer_schema_length=None).filter(pl.col("sym") == symbol)


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


def compare_day(symbol: str, day: date, data_dir: Path, out_dir: Path | None = None) -> int:
    cal = TradingCalendar()
    if not cal.is_trading_day(day):
        print(f"{day} 不是交易日")
        return 1
    td = cal.trading_day(day)
    massive = BarStore(data_dir).read_day(symbol, day).select("ts_start", *OHLCV)
    live = Path(data_dir) / "live" / day.isoformat()
    tbt = _read_jsonl(live / "tbt.jsonl", symbol)
    mkt = _read_jsonl(live / "mkt.jsonl", symbol)
    if massive.is_empty():
        print(f"本地没有 {symbol} {day} 的 Massive 数据，先运行 trader data update")
        return 1
    if tbt.is_empty() and mkt.is_empty():
        print(f"没有找到 {symbol} 在 {live} 的录制数据")
        return 1

    lines = [f"{symbol} {day} IBKR 与 Massive 对比（只比较常规时段 {td.open}~{td.close} 内的 bar）"]

    def rth(df: pl.DataFrame) -> pl.DataFrame:
        return df.filter((pl.col("ts_start") >= td.open) & (pl.col("ts_start") < td.close))

    sources = []
    if not tbt.is_empty():
        sources.append(("逐笔成交", bars_from_trades(tbt.select("ts", "price", "size"))))
    if not mkt.is_empty():
        sources.append(("流式快照（降级）", bars_from_snapshots(mkt.select("recv", "last", "volume"))))
    for name, ib_bars in sources:
        lines.append("")
        lines.append(f"[{name}]")
        lines += _format("1 秒 bar", diff_stats(rth(massive), rth(ib_bars)))
        lines += _format(
            "1 分钟 bar",
            diff_stats(resample(rth(massive), NS_PER_MIN), resample(rth(ib_bars), NS_PER_MIN)),
        )
    lines.append("")
    lines.append("说明：只在 Massive 有、IBKR 没有的秒，多数是该秒成交很少、IBKR 没有推送；")
    lines.append("      快照的成交量取累计量差值，IB 的累计成交量单位可能是 100 股，比例接近 0.01 时属于这种情况。")
    text = "\n".join(lines)
    print(text)
    out_dir = out_dir or Path(data_dir).parent / "runs" / "compare"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day.isoformat()}_{symbol}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    print(f"\n已保存到 {path}")
    return 0
