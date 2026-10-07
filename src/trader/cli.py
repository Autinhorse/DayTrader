"""命令行入口 `trader`（研究版）。

    trader data download --symbols SPY,QQQ --start 2024-01-01 [--end 2026-10-06]
    trader data update [--symbols ...]          把已有标的更新到最新交易日（并补齐中间缺的日期）
    trader data actions [--symbols ...]         刷新拆股与分红表
    trader data report [--symbols ...]          覆盖范围与校验报告
    trader data bars --symbol SPY --date 2026-10-01 [--session extended] [--adjusted]
    trader data compare --date 2026-10-06 [--symbols SPY,QQQ]   与 IBKR 录制数据对比
    trader data check-agg --symbols SPY --date 2026-10-05   1 分钟 bar 与 Massive 官方对比
    trader indicators list                      全部指标（内置 + user/indicators/）
    trader strategies list                      全部策略（user/strategies/）
    trader backtest run config/backtests/ema_cross_spy.yaml
    trader backtest list                        最近的回测
    trader indicators compute --symbol SPY --timeframe 5m --name ema --params period=20 --date D

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
    stores = {k: BarStore(data_dir(), k) for k in ("1s", "1m")}
    end = date.fromisoformat(args.end) if args.end else ny_date(clock.now())
    failed = 0
    try:
        for sym in _symbols(args.symbols):
            if update:
                known = catalog.known_days(sym)
                start = min(known) if known else DEFAULT_START
            else:
                start = date.fromisoformat(args.start)
            changed = False
            for kind in ("1s", "1m"):  # 1 秒 bar 和官方 1 分钟 bar
                res = download_symbol(
                    client,
                    stores[kind],
                    catalog,
                    cal,
                    clock,
                    sym,
                    start,
                    end,
                    redownload=getattr(args, "redownload", False),
                    workers=args.workers,
                    kind=kind,
                )
                failed += len(res.failed) + len(res.rejected)
                changed = changed or bool(res.stored or res.empty)
            if changed:
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
    from trader.data.report import data_report

    catalog = _catalog()
    try:
        text = data_report(data_dir(), catalog, TradingCalendar(), _symbols(args.symbols))
    finally:
        catalog.close()
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
        args.symbol.upper(), args.timeframe, td.start, td.end, args.session, args.adjusted
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
    ).select("time_ny", "session", "open", "high", "low", "close", "volume", "vwap", "trades")
    print(
        f"{args.symbol.upper()} {day}（{'常规时段' if args.session == 'rth' else '含盘前盘后'}，"
        f"{'拆股复权' if args.adjusted else '原始价格'}）共 {df.height:,} 根 {args.timeframe} bar"
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

    syms = _symbols(args.symbols) if args.symbols else None
    return compare_day(date.fromisoformat(args.date), data_dir(), syms)


def cmd_check_agg(args: argparse.Namespace) -> int:
    from trader.data.agg_check import check_day

    cal = TradingCalendar()
    catalog = _catalog()
    hist = HistoryService(data_dir(), cal, catalog)
    client = _client()
    day = date.fromisoformat(args.date)
    print(
        f"{day} 我们由 1 秒 bar 聚合的 1 分钟 bar 与 Massive 官方 1 分钟 bar（全部时段，原始价格）"
    )
    for sym in _symbols(args.symbols):
        r = check_day(client, hist, cal, sym, day)
        print(
            f"{sym:<6} 共同 {r['both']} 分钟，完全相同 {r['identical']}，"
            f"价格不同 {r['price_diff']}，成交量不同 {r['volume_diff']}；"
            f"只在我们 {r['only_ours']}，只在 Massive {r['only_massive']}"
        )
        for e in r["examples"][: args.examples]:  # type: ignore[index]
            print(f"    {e}")
    catalog.close()
    return 0


def _user_indicator_dir():
    return project_dir() / "user" / "indicators"


def cmd_ind_list(args: argparse.Namespace) -> int:
    from trader.indicators.base import list_indicators, load_all

    errors = load_all(_user_indicator_dir())
    for cls in list_indicators():
        origin = "自定义" if cls.__module__.startswith("user_indicators") else "内置"
        params = ", ".join(f"{k}={f.default}" for k, f in cls.Params.model_fields.items())
        outs = ", ".join(f"{o.key}({o.plot}/{o.pane})" for o in cls.outputs)
        print(f"{cls.name:<14} [{origin}] {cls.description}")
        print(f"{'':<14} 参数：{params or '无'}；输出：{outs}；类型：{cls.scope}")
    for f, e in errors.items():
        print(f"加载失败 user/indicators/{f}：{e}")
    return 1 if errors else 0


def cmd_ind_compute(args: argparse.Namespace) -> int:
    from trader.data.indicator_service import IndicatorService
    from trader.indicators.base import load_all

    errors = load_all(_user_indicator_dir())
    for f, e in errors.items():
        print(f"加载失败 user/indicators/{f}：{e}")
    params: dict[str, object] = {}
    for kv in args.params or []:
        k, _, v = kv.partition("=")
        params[k] = v
    cal = TradingCalendar()
    day = date.fromisoformat(args.date)
    td = cal.trading_day(day)
    catalog = _catalog()
    svc = IndicatorService(HistoryService(data_dir(), cal, catalog))
    df = svc.compute(
        args.symbol.upper(), args.timeframe, args.name, params, td.start, td.end, args.session
    )
    catalog.close()
    shown = df.with_columns(
        pl.col("ts_start")
        .map_elements(lambda t: from_ns(t).strftime("%H:%M:%S"), return_dtype=pl.String)
        .alias("time_ny")
    ).drop("ts_start", "ts_end")
    shown = shown.select("time_ny", *[c for c in shown.columns if c != "time_ny"])
    print(
        f"{args.symbol.upper()} {day} {args.timeframe} {args.name} {params or ''}：{df.height} 根"
    )
    with pl.Config(tbl_rows=args.rows * 2 + 1, tbl_width_chars=120):
        print(shown.head(args.rows))
        print("...")
        print(shown.tail(args.rows))
    return 0


def cmd_strategies_list(args: argparse.Namespace) -> int:
    from trader.strategy.base import list_strategies, load_user_strategies

    errors = load_user_strategies(project_dir() / "user" / "strategies")
    for cls in list_strategies():
        params = ", ".join(f"{k}={f.default}" for k, f in cls.Params.model_fields.items())
        print(f"{cls.name:<16} {cls.description}")
        print(f"{'':<16} 参数：{params}")
    for f, e in errors.items():
        print(f"加载失败 user/strategies/{f}：{e}")
    return 1 if errors else 0


def _fmt(x: object, pct: bool = False) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x * 100:.1f}%" if pct else f"{x:,.2f}"
    return str(x)


def cmd_backtest_run(args: argparse.Namespace) -> int:
    from pathlib import Path

    from trader.backtest.config import load_config
    from trader.backtest.runner import run_backtest

    cfg = load_config(Path(args.config))
    if args.start:
        cfg.start = date.fromisoformat(args.start)
    if args.end:
        cfg.end = date.fromisoformat(args.end)
    print(f"回测 {cfg.name or cfg.strategy}：{cfg.start} ~ {cfg.end} ...")
    r = run_backtest(
        cfg, data_dir=data_dir(), project_dir=project_dir(), clock=WallClock(lambda _e: None)
    )
    s = r.summary
    c, pnl, perf, risk = s["counts"], s["pnl"], s["performance"], s["risk"]
    print(
        f"交易 {c['trades']} 笔（多 {c['long']} / 空 {c['short']}），订单 {c['orders']}，"
        f"被拒 {c['rejected']}，部分成交 {c['partially_filled']}"
    )
    print(
        f"净盈亏 {_fmt(pnl['net_pnl'])} = 毛盈亏 {_fmt(pnl['gross_pnl'])} − 手续费 "
        f"{_fmt(pnl['commission'])}；收益率 {pnl['return_pct']:.2f}%"
    )
    print(
        f"  毛盈亏按含成本的成交价计算，其中价差成本 {_fmt(pnl['spread_cost'])}、"
        f"冲击成本 {_fmt(pnl['impact_cost'])}"
    )
    print(
        f"胜率 {_fmt(perf['win_rate'], True)}，盈亏比 {_fmt(perf['payoff_ratio'])}，"
        f"利润因子 {_fmt(perf['profit_factor'])}，期望 {_fmt(perf['expectancy'])} 美元/笔"
    )
    print(
        f"最大回撤 {_fmt(risk['max_drawdown'])}（{_fmt(risk['max_drawdown_pct'], True)}），"
        f"日夏普 {_fmt(risk['sharpe_daily_annualized'])}，"
        f"平均持仓 {_fmt(s['holding']['avg_holding_min'])} 分钟"
    )
    scen = s["cost_sensitivity"]["scenarios"]
    print(
        "成本敏感性（价差与冲击按倍数重算）："
        + "；".join(f"{x['multiplier']:g} 倍 净盈亏 {_fmt(x['net_pnl'])}" for x in scen)
    )
    be = s["cost_sensitivity"]["breakeven_extra_cost_per_share"]
    print(f"盈亏平衡：每股再多 {_fmt(be)} 美元成本时净盈亏归零")
    pa, lq = s["path_ambiguity"], s["liquidity"]
    print(
        f"路径歧义 {pa['trades']} 笔（若按有利结果多 {_fmt(pa['pnl_if_favorable'])}）；"
        f"超出市价单模型适用范围 {lq['out_of_range_trades']} 笔"
    )
    print(f"结果已保存到 {r.out_dir}")
    return 0


def cmd_backtest_list(args: argparse.Namespace) -> int:
    import sqlite3

    idx = project_dir() / "runs" / "index.sqlite"
    if not idx.exists():
        print("还没有回测记录")
        return 0
    with sqlite3.connect(idx) as db:
        rows = db.execute(
            "SELECT run_id, name, start, end, trades, net_pnl FROM runs "
            "ORDER BY created DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
    for run_id, name, start, end, trades, net in rows:
        print(f"{run_id:<40} {name or '':<20} {start}~{end} 交易 {trades:>4} 净盈亏 {net:>10,.2f}")
    return 0


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

    b = data.add_parser("bars", help="查看某天的 bar")
    b.add_argument("--symbol", required=True)
    b.add_argument("--timeframe", default="1s", help="1s 5s 10s 30s 1m 2m 5m 15m 30m 1h 1d")
    b.add_argument("--date", required=True)
    b.add_argument("--session", choices=["rth", "extended"], default="rth")
    b.add_argument("--adjusted", action="store_true", help="按拆股复权")
    b.add_argument("--rows", type=int, default=5, help="头尾各显示几行")
    b.set_defaults(func=cmd_bars)

    c = data.add_parser("compare", help="与 IBKR 录制数据对比")
    c.add_argument("--symbols", help="默认录制到的全部标的")
    c.add_argument("--date", required=True)
    c.set_defaults(func=cmd_compare)

    k = data.add_parser("check-agg", help="我们的 1 分钟 bar 与 Massive 官方 1 分钟 bar 对比")
    k.add_argument("--symbols")
    k.add_argument("--date", required=True)
    k.add_argument("--examples", type=int, default=3, help="每个标的列出几条不一致的分钟")
    k.set_defaults(func=cmd_check_agg)

    ind = sub.add_parser("indicators", help="指标").add_subparsers(dest="cmd", required=True)
    il = ind.add_parser("list", help="列出全部指标")
    il.set_defaults(func=cmd_ind_list)
    ic = ind.add_parser("compute", help="计算某天的指标")
    ic.add_argument("--symbol", required=True)
    ic.add_argument("--timeframe", default="1m")
    ic.add_argument("--name", required=True)
    ic.add_argument("--params", nargs="*", help="参数，例如 period=20 source=close")
    ic.add_argument("--date", required=True)
    ic.add_argument("--session", choices=["rth", "extended"], default="extended")
    ic.add_argument("--rows", type=int, default=5)
    ic.set_defaults(func=cmd_ind_compute)

    st = sub.add_parser("strategies", help="策略").add_subparsers(dest="cmd", required=True)
    st.add_parser("list", help="列出全部策略").set_defaults(func=cmd_strategies_list)

    bt = sub.add_parser("backtest", help="回测").add_subparsers(dest="cmd", required=True)
    br = bt.add_parser("run", help="按配置文件运行回测")
    br.add_argument("config")
    br.add_argument("--start", help="覆盖配置里的开始日期")
    br.add_argument("--end", help="覆盖配置里的结束日期")
    br.set_defaults(func=cmd_backtest_run)
    bl = bt.add_parser("list", help="最近的回测")
    bl.add_argument("--limit", type=int, default=20)
    bl.set_defaults(func=cmd_backtest_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
