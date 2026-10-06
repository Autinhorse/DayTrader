"""命令行入口 `trader`（研究版）。

    trader data download --symbols SPY,QQQ --start 2024-01-01 [--end 2026-10-06]
    trader data update [--symbols ...]          把已有标的更新到最新交易日（并补齐中间缺的日期）
    trader data actions [--symbols ...]         刷新拆股与分红表
    trader data report [--symbols ...]          覆盖范围与校验报告
    trader data bars --symbol SPY --date 2026-10-01 [--session extended] [--adjusted]
    trader data compare --symbol SPY --date 2026-10-08   与 IBKR 录制数据对比

不带 --symbols 时使用 config/universe.yaml 里的全部标的。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import polars as pl

from trader.config import data_dir, load_universe, project_dir
from trader.core.clock import WallClock
from trader.core.timeutil import from_ns, ny_date
from trader.core.trading_calendar import TradingCalendar
from trader.data.catalog import Catalog
from trader.data.history import HistoryService

DEFAULT_START = date(2024, 1, 1)


def _symbols(arg: str | None) -> list[str]:
    if arg:
        return [s.strip().upper() for s in arg.split(",") if s.strip()]
    return load_universe().names


def _catalog() -> Catalog:
    return Catalog(data_dir() / "catalog.sqlite")


def _client():
    from trader.data.massive import MassiveClient, find_api_key

    key = find_api_key(project_dir())
    if not key:
        sys.exit("找不到 Massive API key：请在 .env 中写 MASSIVE_API_KEY=...")
    return MassiveClient(key)


def cmd_download(args: argparse.Namespace, *, update: bool = False) -> int:
    from trader.data.download import download_symbol, refresh_corporate_actions
    from trader.data.store import BarStore

    cal = TradingCalendar()
    catalog = _catalog()
    client = _client()
    clock = WallClock(lambda _e: None)
    store = BarStore(data_dir())
    end = date.fromisoformat(args.end) if args.end else ny_date(clock.now())
    failed = 0
    try:
        for sym in _symbols(args.symbols):
            if update:
                known = catalog.known_days(sym)
                start = min(known) if known else DEFAULT_START
            else:
                start = date.fromisoformat(args.start)
            res = download_symbol(
                client,
                store,
                catalog,
                cal,
                clock,
                sym,
                start,
                end,
                redownload=getattr(args, "redownload", False),
                workers=args.workers,
            )
            failed += len(res.failed) + len(res.rejected)
            if res.stored or res.empty:
                n = refresh_corporate_actions(client, data_dir(), sym)
                print(f"{sym}: 拆股/分红记录 {n} 条")
    except KeyboardInterrupt:
        client.stop_event.set()
        print("已停止；已写入的日期不会丢失，再次运行会从缺的日期继续。")
        return 1
    finally:
        catalog.close()
    if failed:
        print(f"共有 {failed} 个交易日失败或被校验拒绝，再次运行同一命令会重试这些日期。")
    return 1 if failed else 0


def cmd_actions(args: argparse.Namespace) -> int:
    from trader.data.download import refresh_corporate_actions

    client = _client()
    for sym in _symbols(args.symbols):
        print(f"{sym}: {refresh_corporate_actions(client, data_dir(), sym)} 条")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from trader.data.corporate_actions import load_actions

    cal = TradingCalendar()
    catalog = _catalog()
    actions = load_actions(data_dir())
    lines: list[str] = []
    out = lines.append
    out(f"数据目录：{data_dir()}")
    out(
        f"{'标的':<6} {'起始':<10} {'结束':<10} {'有数据':>6} {'无数据':>6} {'未下载':>6} "
        f"{'总行数':>12} {'有长空档天数':>10}  提示汇总"
    )
    details: list[str] = []
    for sym in _symbols(args.symbols):
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
        out(
            f"{sym:<6} {first!s:<10} {last!s:<10} {len(have):>6} {len(empty_inside):>6} "
            f"{len(missing):>6} {sum(p.rows for p in have):>12,} {len(gap_days):>10}  "
            + (", ".join(f"{k}={v:,}" for k, v in sorted(warn.items())) or "-")
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
        acts = actions.filter(pl.col("symbol") == sym)
        splits = acts.filter(pl.col("kind") == "split")
        for r in splits.iter_rows(named=True):
            if first <= r["ex_date"] <= last:
                details.append(f"{sym} 区间内拆股：{r['ex_date']} 比例 {r['ratio']:g}")
    catalog.close()
    if details:
        out("")
        out("明细：")
        lines.extend(f"  {d}" for d in details)
    out("")
    out("说明：校验错误（重复时间、时间倒序、非正价格、负成交量）的日期不会写入，显示为'未下载'。")
    out(
        "      长空档：常规时段内连续 60 秒以上没有 bar。vwap_outside_range 是 Massive 的正常现象。"
    )
    text = "\n".join(lines)
    print(text)
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n报告已保存到 {args.out}")
    return 0


def cmd_bars(args: argparse.Namespace) -> int:
    cal = TradingCalendar()
    day = date.fromisoformat(args.date)
    if not cal.is_trading_day(day):
        print(f"{day} 不是交易日")
        return 1
    catalog = _catalog()
    hist = HistoryService(data_dir(), cal, catalog)
    td = cal.trading_day(day)
    df = hist.bars(
        args.symbol.upper(), "1s", td.pre_open, td.post_close, args.session, args.adjusted
    )
    fp = catalog.fingerprint(args.symbol.upper(), day)
    catalog.close()
    if df.is_empty():
        print("没有数据")
        return 1
    shown = df.with_columns(
        pl.col("ts_start")
        .map_elements(lambda t: from_ns(t).strftime("%H:%M:%S"), return_dtype=pl.String)
        .alias("time_ny")
    ).select("time_ny", "open", "high", "low", "close", "volume", "vwap", "trades")
    print(
        f"{args.symbol.upper()} {day}（{'常规时段' if args.session == 'rth' else '含盘前盘后'}，"
        f"{'拆股复权' if args.adjusted else '原始价格'}）共 {df.height:,} 根 1 秒 bar"
        + ("，半日市" if td.early_close else "")
    )
    print(f"开盘 {from_ns(td.open):%H:%M}，收盘 {from_ns(td.close):%H:%M}（纽约时间），指纹 {fp}")
    with pl.Config(tbl_rows=args.rows * 2 + 1, tbl_cols=10, tbl_width_chars=120):
        print(shown.head(args.rows))
        print("...")
        print(shown.tail(args.rows))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from trader.data.compare import compare_day

    return compare_day(args.symbol.upper(), date.fromisoformat(args.date), data_dir())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trader", description="交易系统命令行（研究版）")
    sub = p.add_subparsers(dest="group", required=True)
    data = sub.add_parser("data", help="行情数据").add_subparsers(dest="cmd", required=True)

    d = data.add_parser("download", help="下载指定区间（增量，已有日期跳过）")
    d.add_argument("--symbols", help="逗号分隔；默认 universe.yaml 全部")
    d.add_argument("--start", default=DEFAULT_START.isoformat())
    d.add_argument("--end", help="默认今天（未收盘完毕的当天不下载）")
    d.add_argument("--redownload", action="store_true", help="区间内已有日期也重新下载")
    d.add_argument("--workers", type=int, default=4)
    d.set_defaults(func=cmd_download)

    u = data.add_parser("update", help="已有标的更新到最新交易日")
    u.add_argument("--symbols")
    u.add_argument("--end")
    u.add_argument("--workers", type=int, default=4)
    u.set_defaults(func=lambda a: cmd_download(a, update=True))

    a = data.add_parser("actions", help="刷新拆股与分红表")
    a.add_argument("--symbols")
    a.set_defaults(func=cmd_actions)

    r = data.add_parser("report", help="覆盖范围与校验报告")
    r.add_argument("--symbols")
    r.add_argument("--out", help="同时保存到文件")
    r.set_defaults(func=cmd_report)

    b = data.add_parser("bars", help="查看某天的 1 秒 bar")
    b.add_argument("--symbol", required=True)
    b.add_argument("--date", required=True)
    b.add_argument("--session", choices=["rth", "extended"], default="rth")
    b.add_argument("--adjusted", action="store_true", help="按拆股复权")
    b.add_argument("--rows", type=int, default=5, help="头尾各显示几行")
    b.set_defaults(func=cmd_bars)

    c = data.add_parser("compare", help="与 IBKR 录制数据对比")
    c.add_argument("--symbol", required=True)
    c.add_argument("--date", required=True)
    c.set_defaults(func=cmd_compare)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
