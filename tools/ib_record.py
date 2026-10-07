"""IBKR 只读行情录制（DESIGN.md 5.7，阶段 1）。不包含任何下单代码。

连接本机 IB Gateway 的**模拟账户**端口（默认 4002），以只读方式订阅：
  - 少数几个标的的逐笔成交（tick-by-tick AllLast，账户的并发额度很少）；
  - 其余全部标的的普通流式行情（定时快照：最新价、买卖价、累计成交量）。
录到 data/live/<纽约日期>/ 下的 tbt.jsonl 和 mkt.jsonl（逐行追加，崩溃也不丢已写内容）。
第二天用 `trader data compare --symbol SPY --date <日期>` 与 Massive 数据对比。

用法（交易日盘前启动，默认录到 20:00 盘后结束，Ctrl+C 可提前结束）：
    uv run python tools/ib_record.py
    uv run python tools/ib_record.py --tick-symbols SPY,QQQ,NVDA,TSLA,AAPL --until 16:05
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_async import IB, Stock, Ticker
from ib_async.ib import StartupFetch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("TRADER_HOME", str(ROOT))

from trader.config import data_dir, load_universe  # noqa: E402

NY = ZoneInfo("America/New_York")
# 只允许模拟账户端口：4002 = Gateway 模拟，7497 = TWS 模拟。实盘端口 4001 / 7496 一律拒绝。
PAPER_PORTS = {4002, 7497}


def num(x):
    """IB 用 nan 表示无值；JSON 里写成 null。"""
    if x is None:
        return None
    try:
        return None if math.isnan(x) else x
    except TypeError:
        return x


def ns(dt: datetime | None) -> int | None:
    return None if dt is None else int(dt.timestamp() * 1e9)


class Recorder:
    def __init__(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.tbt = (out_dir / "tbt.jsonl").open("a", encoding="utf-8")
        self.mkt = (out_dir / "mkt.jsonl").open("a", encoding="utf-8")
        self.symbol_of: dict[int, str] = {}  # id(Ticker) -> 代码
        self.tbt_tickers: set[int] = set()  # 订阅了逐笔的 Ticker
        self.counts = {"tbt": 0, "mkt": 0}

    def hook_tick_by_tick(self, ib: IB) -> None:
        """ib_async 会把逐笔成交的 IB 原始成交时间（秒）换成本机接收时间，
        所以在它处理之前截获原始回调，自己记录。"""
        wrapper = ib.wrapper
        original = wrapper.tickByTickAllLast

        def hooked(reqId, tickType, ib_time, price, size, attrib, exchange, conditions):
            ticker = wrapper.reqId2Ticker.get(reqId)
            sym = self.symbol_of.get(id(ticker)) if ticker is not None else None
            if sym is not None:
                rec = {
                    "sym": sym,
                    "recv": time.time_ns(),
                    "ts": int(ib_time) * 1_000_000_000,  # IB 原始成交时间，只精确到秒
                    "price": price,
                    "size": float(size),
                    "exch": exchange,
                    "cond": conditions,
                    "past_limit": bool(getattr(attrib, "pastLimit", False)),
                    "unreported": bool(getattr(attrib, "unreported", False)),
                }
                self.tbt.write(json.dumps(rec) + "\n")
                self.counts["tbt"] += 1
            original(reqId, tickType, ib_time, price, size, attrib, exchange, conditions)

        wrapper.tickByTickAllLast = hooked

    def on_pending(self, tickers: set[Ticker]) -> None:
        """普通流式行情：每次更新记一条快照。逐笔成交在 hook_tick_by_tick 里记录。"""
        recv = time.time_ns()
        for t in tickers:
            sym = self.symbol_of.get(id(t))
            if sym is None or id(t) in self.tbt_tickers:
                continue
            rec = {
                "sym": sym,
                "recv": recv,
                "ts": ns(t.time),
                "last": num(t.last),
                "last_size": num(t.lastSize),
                "bid": num(t.bid),
                "ask": num(t.ask),
                "bid_size": num(t.bidSize),
                "ask_size": num(t.askSize),
                "volume": num(t.volume),
                "rt_time": ns(t.rtTime) if isinstance(t.rtTime, datetime) else None,
                "rt_volume": num(t.rtVolume),
            }
            self.mkt.write(json.dumps(rec) + "\n")
            self.counts["mkt"] += 1

    def flush(self) -> None:
        self.tbt.flush()
        self.mkt.flush()

    def close(self) -> None:
        self.tbt.close()
        self.mkt.close()


async def main_async(args: argparse.Namespace) -> int:
    if args.port not in PAPER_PORTS:
        print(f"拒绝连接端口 {args.port}：录制脚本只允许模拟账户端口 {sorted(PAPER_PORTS)}。")
        return 2
    symbols = (
        [s.upper() for s in args.symbols.split(",")] if args.symbols else load_universe().names
    )
    tick_syms = [s.strip().upper() for s in args.tick_symbols.split(",") if s.strip()]

    now_ny = datetime.now(NY)
    hh, mm = map(int, args.until.split(":"))
    until = now_ny.replace(hour=hh, minute=mm, second=0, microsecond=0)
    out_dir = data_dir() / "live" / now_ny.date().isoformat()

    ib = IB()
    ib.errorEvent += lambda reqId, code, msg, contract: print(f"  IB 消息 {code}: {msg}")
    print(f"连接 {args.host}:{args.port}（只读）...")
    await ib.connectAsync(
        args.host, args.port, clientId=args.client_id, readonly=True, fetchFields=StartupFetch(0)
    )  # 不拉取持仓、订单等账户信息
    ib.reqMarketDataType(1)  # 实时行情

    rec = Recorder(out_dir)
    rec.hook_tick_by_tick(ib)
    contracts = [Stock(s, "SMART", "USD") for s in symbols]
    qualified = await ib.qualifyContractsAsync(*contracts)
    by_sym = {c.symbol: c for c in qualified if c and c.conId}
    missing = [s for s in symbols if s not in by_sym]
    if missing:
        print(f"无法识别的代码，已跳过：{missing}")

    for s, c in by_sym.items():
        # 233 = RTVolume：最新成交价、量、时间
        if s in tick_syms:
            t = ib.reqTickByTickData(c, "AllLast")
            rec.tbt_tickers.add(id(t))
        else:
            t = ib.reqMktData(c, "233")
        rec.symbol_of[id(t)] = s
    ib.pendingTickersEvent += rec.on_pending

    print(f"已订阅 {len(by_sym)} 个标的（逐笔：{[s for s in tick_syms if s in by_sym]}）")
    print(f"写入 {out_dir}，录到纽约时间 {until:%H:%M}，Ctrl+C 可提前结束")
    try:
        while datetime.now(NY) < until:
            await asyncio.sleep(10)
            rec.flush()
            print(
                f"  {datetime.now(NY):%H:%M:%S} 逐笔 {rec.counts['tbt']:,} 条，"
                f"快照 {rec.counts['mkt']:,} 条",
                flush=True,
            )
    finally:
        rec.flush()
        rec.close()
        ib.disconnect()
    print("录制结束。")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="IBKR 只读行情录制（只连模拟账户端口）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002, help="4002 = IB Gateway 模拟账户")
    p.add_argument("--client-id", type=int, default=91)
    p.add_argument("--symbols", help="默认 universe.yaml 全部")
    p.add_argument(
        "--tick-symbols",
        default="SPY,QQQ,NVDA,TSLA,AAPL",
        help="订阅逐笔成交的标的（账户并发额度为 5 个，超出时 IB 报错 10190）",
    )
    p.add_argument("--until", default="20:00", help="纽约时间 HH:MM")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("已手动结束。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
