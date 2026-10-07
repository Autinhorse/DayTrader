"""回测输出与汇总统计（DESIGN.md 10.3、10.4）。

- orders / fills / trades / equity 表；
- trades：一次开仓到平仓为一笔（持仓从 0 到非 0 再回到 0；一笔成交穿越零点时拆成平仓和新开仓），
  带入场/出场原因、毛盈亏、手续费、净盈亏、持仓时长、持仓期间最大浮盈（MFE）和最大浮亏（MAE）；
- 汇总统计：盈亏与成本分项、胜率、盈亏比、利润因子、回撤、夏普、分组统计、成本敏感性、
  路径歧义、流动性提示、数据质量。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import polars as pl

from trader.brokers.sim.matcher import FillDetail
from trader.core.models import Order
from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC, from_ns
from trader.core.trading_calendar import TradingCalendar
from trader.data.history import HistoryService
from trader.engine.engine import EngineResult


def orders_frame(orders: list[Order]) -> pl.DataFrame:
    rows = []
    for o in orders:
        it = o.intent
        rows.append(
            {
                "client_order_id": o.client_order_id,
                "created_at": o.created_at,
                "symbol": it.symbol,
                "side": it.side,
                "qty": it.qty,
                "order_type": it.order_type,
                "limit_price": float(it.limit_price) if it.limit_price is not None else None,
                "stop_price": float(it.stop_price) if it.stop_price is not None else None,
                "tif": it.tif,
                "outside_rth": it.outside_rth,
                "role": o.role,
                "parent_id": o.parent_id,
                "status": o.status.value,
                "filled_qty": o.filled_qty,
                "avg_fill_price": float(o.avg_fill_price) if o.avg_fill_price else None,
                "commission": float(o.commission),
                "reason_code": it.reason.code,
                "reason_text": it.reason.text,
                "reason_context": json.dumps(it.reason.context, ensure_ascii=False),
                "reject_rule": o.reject_rule,
                "reject_detail": o.reject_detail,
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None)


@dataclass
class _Leg:
    ts: int
    qty: int  # 本次成交中归入这笔交易的数量（正数）
    price: float
    commission: float
    spread: float
    impact: float
    reason: str
    ambiguous: bool
    out_of_range: bool
    alt_price: float | None  # 路径歧义时有利结果（止盈）的价格


@dataclass
class _Trade:
    symbol: str
    direction: int  # 1 多 -1 空
    entries: list[_Leg] = field(default_factory=list)
    exits: list[_Leg] = field(default_factory=list)


def build_trades(result: EngineResult, history: HistoryService, session: str) -> pl.DataFrame:
    orders = {o.client_order_id: o for o in result.orders}
    details: dict[str, FillDetail] = result.fill_details
    pos: dict[str, int] = {}
    open_trades: dict[str, _Trade] = {}
    done: list[_Trade] = []
    for f in sorted(result.fills, key=lambda f: (f.ts, f.broker_exec_id)):
        o = orders[f.client_order_id]
        sym = o.intent.symbol
        d = details.get(f.broker_exec_id)
        signed = f.qty if o.intent.side == "BUY" else -f.qty
        comm = float(result.exec_commission.get(f.broker_exec_id, Decimal(0)))
        alt = None
        if d is not None and d.ambiguous and o.parent_id:
            tp = [
                x for x in orders.values() if x.parent_id == o.parent_id and x.role == "take_profit"
            ]
            if tp and tp[0].intent.limit_price is not None:
                alt = float(tp[0].intent.limit_price)
        cur = pos.get(sym, 0)
        remaining = signed
        while remaining:
            if cur == 0:
                t = open_trades[sym] = _Trade(sym, 1 if remaining > 0 else -1)
                part = remaining
            elif (cur > 0) == (remaining > 0):
                t = open_trades[sym]
                part = remaining
            else:
                t = open_trades[sym]
                part = -cur if abs(remaining) > abs(cur) else remaining
            share = abs(part) / f.qty
            leg = _Leg(
                ts=f.ts,
                qty=abs(part),
                price=float(f.price),
                commission=comm * share,
                spread=(d.spread_cost if d else 0.0) * share,
                impact=(d.impact_cost if d else 0.0) * share,
                reason=o.intent.reason.code,
                ambiguous=bool(d and d.ambiguous),
                out_of_range=bool(d and d.out_of_range),
                alt_price=alt,
            )
            if (part > 0) == (t.direction > 0):
                t.entries.append(leg)
            else:
                t.exits.append(leg)
            cur += part
            remaining -= part
            if cur == 0:
                done.append(open_trades.pop(sym))
        pos[sym] = cur
    done += list(open_trades.values())  # 未平仓的（正常情况下不应有）
    return _trades_frame(done, history, session)


def _avg(legs: list[_Leg]) -> float:
    q = sum(x.qty for x in legs)
    return sum(x.price * x.qty for x in legs) / q if q else math.nan


def _trades_frame(trades: list[_Trade], history: HistoryService, session: str) -> pl.DataFrame:
    rows = []
    for t in trades:
        qty = sum(x.qty for x in t.entries)
        exit_qty = sum(x.qty for x in t.exits)
        entry_px, exit_px = _avg(t.entries), _avg(t.exits)
        entry_ts = t.entries[0].ts
        exit_ts = t.exits[-1].ts if t.exits else None
        gross = (exit_px - entry_px) * exit_qty * t.direction if exit_qty else 0.0
        legs = t.entries + t.exits
        commission = sum(x.commission for x in legs)
        mfe = mae = None
        if exit_ts is not None:
            bars = history.bars(t.symbol, "1s", entry_ts, exit_ts + NS_PER_SEC, session)  # type: ignore[arg-type]
            if bars.height:
                hi, lo = float(bars["high"].max()), float(bars["low"].min())  # type: ignore[arg-type]
                fav, adv = (hi, lo) if t.direction > 0 else (lo, hi)
                mfe = max((fav - entry_px) * t.direction, 0.0) * qty
                mae = min((adv - entry_px) * t.direction, 0.0) * qty
        alt_gain = 0.0
        for x in t.exits:
            if x.ambiguous and x.alt_price is not None:
                alt_gain += (x.alt_price - x.price) * x.qty * t.direction
        rows.append(
            {
                "symbol": t.symbol,
                "direction": "long" if t.direction > 0 else "short",
                "qty": qty,
                "entry_time": entry_ts,
                "entry_price": entry_px,
                "entry_reason": t.entries[0].reason,
                "exit_time": exit_ts,
                "exit_price": exit_px if t.exits else None,
                "exit_reason": t.exits[-1].reason if t.exits else None,
                "gross_pnl": gross,
                "commission": commission,
                "net_pnl": gross - commission,
                "spread_cost": sum(x.spread for x in legs),
                "impact_cost": sum(x.impact for x in legs),
                "holding_s": (exit_ts - entry_ts) / NS_PER_SEC if exit_ts is not None else None,
                "mfe": mfe,
                "mae": mae,
                "shares_traded": qty + exit_qty,
                "ambiguous": any(x.ambiguous for x in t.exits),
                "ambiguous_alt_gain": alt_gain,
                "out_of_range": any(x.out_of_range for x in legs),
                "closed": exit_qty == qty,
            }
        )
    schema = {
        "symbol": pl.String,
        "direction": pl.String,
        "qty": pl.Int64,
        "entry_time": pl.Int64,
        "entry_price": pl.Float64,
        "entry_reason": pl.String,
        "exit_time": pl.Int64,
        "exit_price": pl.Float64,
        "exit_reason": pl.String,
        "gross_pnl": pl.Float64,
        "commission": pl.Float64,
        "net_pnl": pl.Float64,
        "spread_cost": pl.Float64,
        "impact_cost": pl.Float64,
        "holding_s": pl.Float64,
        "mfe": pl.Float64,
        "mae": pl.Float64,
        "shares_traded": pl.Int64,
        "ambiguous": pl.Boolean,
        "ambiguous_alt_gain": pl.Float64,
        "out_of_range": pl.Boolean,
        "closed": pl.Boolean,
    }
    return pl.DataFrame(rows, schema=schema)


def fills_frame(result: EngineResult) -> pl.DataFrame:
    orders = {o.client_order_id: o for o in result.orders}
    rows = []
    for f in result.fills:
        o = orders[f.client_order_id]
        d: FillDetail | None = result.fill_details.get(f.broker_exec_id)
        rows.append(
            {
                "exec_id": f.broker_exec_id,
                "client_order_id": f.client_order_id,
                "ts": f.ts,
                "symbol": o.intent.symbol,
                "side": o.intent.side,
                "qty": f.qty,
                "price": float(f.price),
                "base_price": d.base_price if d else None,
                "commission": float(result.exec_commission.get(f.broker_exec_id, 0)),
                "spread_cost": d.spread_cost if d else 0.0,
                "impact_cost": d.impact_cost if d else 0.0,
                "kind": d.kind if d else None,
                "ambiguous": bool(d and d.ambiguous),
                "out_of_range": bool(d and d.out_of_range),
                "reason_code": o.intent.reason.code,
                "role": o.role,
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None)


def equity_frame(result: EngineResult) -> pl.DataFrame:
    return pl.DataFrame(
        result.equity,
        schema={
            "ts": pl.Int64,
            "equity": pl.Float64,
            "cash": pl.Float64,
            "market_value": pl.Float64,
        },
        orient="row",
    )


# ---------- 汇总统计 ----------


def _pnl_stats(net: list[float]) -> dict[str, float | int | None]:
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    n = len(net)
    return {
        "trades": n,
        "net_pnl": sum(net),
        "win_rate": len(wins) / n if n else None,
        "avg_win": gross_win / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "payoff_ratio": (gross_win / len(wins)) / (gross_loss / len(losses))
        if wins and losses and gross_loss
        else None,
        "expectancy": sum(net) / n if n else None,
        "profit_factor": gross_win / gross_loss if gross_loss else None,
    }


def _drawdown(equity: pl.DataFrame) -> dict[str, float | None]:
    if equity.is_empty():
        return {"max_drawdown": 0.0, "max_drawdown_pct": 0.0, "longest_drawdown_min": 0.0}
    eq = equity["equity"].to_list()
    ts = equity["ts"].to_list()
    peak, peak_ts = eq[0], ts[0]
    mdd = mdd_pct = 0.0
    longest = 0
    for t, v in zip(ts, eq, strict=True):
        if v >= peak:
            peak, peak_ts = v, t
        else:
            mdd = max(mdd, peak - v)
            mdd_pct = max(mdd_pct, (peak - v) / peak if peak else 0)
            longest = max(longest, t - peak_ts)
    return {
        "max_drawdown": mdd,
        "max_drawdown_pct": mdd_pct,
        "longest_drawdown_min": longest / NS_PER_MIN,
    }


def _daily(equity: pl.DataFrame, cal: TradingCalendar, initial: float) -> list[tuple[str, float]]:
    out: dict[str, float] = {}
    for t, v in equity.select("ts", "equity").iter_rows():
        d = cal.trading_date_of(t)
        if d is not None:
            out[d.isoformat()] = v
    days = sorted(out)
    rets, prev = [], initial
    for d in days:
        rets.append((d, out[d] - prev))
        prev = out[d]
    return rets


def _group(trades: pl.DataFrame, by: str | pl.Expr, name: str) -> list[dict[str, Any]]:
    if trades.is_empty():
        return []
    g = (
        trades.group_by(by)
        .agg(
            pl.len().alias("trades"),
            pl.col("net_pnl").sum().alias("net_pnl"),
            (pl.col("net_pnl") > 0).mean().alias("win_rate"),
        )
        .sort(name if isinstance(by, pl.Expr) else by)
    )
    return g.to_dicts()


def summarize(
    result: EngineResult,
    trades: pl.DataFrame,
    orders: pl.DataFrame,
    equity: pl.DataFrame,
    cal: TradingCalendar,
    initial_cash: float,
    cost_scenarios: list[float],
    data_quality: dict[str, Any],
) -> dict[str, Any]:
    closed = trades.filter(pl.col("closed"))
    net = closed["net_pnl"].to_list()
    s: dict[str, Any] = {}
    s["counts"] = {
        "trades": closed.height,
        "long": closed.filter(pl.col("direction") == "long").height,
        "short": closed.filter(pl.col("direction") == "short").height,
        "open_at_end": trades.height - closed.height,
        "orders": orders.height,
        "rejected": orders.filter(pl.col("status") == "REJECTED").height if orders.height else 0,
        "partially_filled": orders.filter(
            (pl.col("filled_qty") > 0) & (pl.col("filled_qty") < pl.col("qty"))
        ).height
        if orders.height
        else 0,
    }
    s["pnl"] = {
        "gross_pnl": float(closed["gross_pnl"].sum()),
        "commission": float(closed["commission"].sum()),
        "spread_cost": float(closed["spread_cost"].sum()),
        "impact_cost": float(closed["impact_cost"].sum()),
        "net_pnl": float(closed["net_pnl"].sum()),
        "final_equity": equity["equity"].to_list()[-1] if equity.height else initial_cash,
        "return_pct": (equity["equity"].to_list()[-1] / initial_cash - 1) * 100
        if equity.height
        else 0.0,
    }
    s["performance"] = _pnl_stats(net)
    s["risk"] = _drawdown(equity)
    daily = _daily(equity, cal, initial_cash)
    rets = [r for _, r in daily]
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        s["risk"]["sharpe_daily_annualized"] = mean / sd * math.sqrt(252) if sd else None
    else:
        s["risk"]["sharpe_daily_annualized"] = None
    hold = closed["holding_s"].drop_nulls()
    total_secs = sum(
        (d.close - d.open) / NS_PER_SEC for d in (cal.trading_day(x) for x in result.days)
    )
    s["holding"] = {
        "avg_holding_min": float(hold.mean()) / 60 if hold.len() else None,  # type: ignore[arg-type]
        "time_in_market_pct": float(hold.sum()) / total_secs * 100 if total_secs else None,
    }
    entry_slot = pl.col("entry_time").map_elements(_half_hour, return_dtype=pl.String).alias("slot")
    entry_day = (
        pl.col("entry_time")
        .map_elements(lambda t: from_ns(t).date().isoformat(), return_dtype=pl.String)
        .alias("day")
    )
    s["groups"] = {
        "by_entry_reason": _group(closed, "entry_reason", "entry_reason"),
        "by_exit_reason": _group(closed, "exit_reason", "exit_reason"),
        "by_direction": _group(closed, "direction", "direction"),
        "by_day": _group(closed, entry_day, "day"),
        "by_half_hour": _group(closed, entry_slot, "slot"),
    }
    # 成本敏感性：价差和冲击成本按倍数重新计价（事后重算，假设决策不随成本变化）
    scen = []
    for m in cost_scenarios:
        adj = [
            r["net_pnl"] - (m - 1) * (r["spread_cost"] + r["impact_cost"])
            for r in closed.iter_rows(named=True)
        ]
        st = _pnl_stats(adj)
        scen.append(
            {
                "multiplier": m,
                "net_pnl": st["net_pnl"],
                "win_rate": st["win_rate"],
                "profit_factor": st["profit_factor"],
            }
        )
    shares = int(closed["shares_traded"].sum()) if closed.height else 0
    s["cost_sensitivity"] = {
        "scenarios": scen,
        "shares_traded": shares,
        # 每股再多付多少成本，净盈亏归零（负数表示已经亏损）
        "breakeven_extra_cost_per_share": float(closed["net_pnl"].sum()) / shares
        if shares
        else None,
    }
    amb = closed.filter(pl.col("ambiguous"))
    s["path_ambiguity"] = {
        "trades": amb.height,
        "pnl_if_favorable": float(amb["ambiguous_alt_gain"].sum()) if amb.height else 0.0,
        "note": "同一根 1 秒 bar 上止盈止损都可触及时按止损处理；差额大说明结论依赖秒内顺序",
    }
    oor = closed.filter(pl.col("out_of_range"))
    s["liquidity"] = {
        "out_of_range_trades": oor.height,
        "out_of_range_pct": oor.height / closed.height * 100 if closed.height else 0.0,
        "note": "市价单数量超过前 60 秒成交量的参与率上限，或成交量过低，成交假设不可靠",
    }
    s["data_quality"] = data_quality
    s["daily_pnl"] = [{"day": d, "pnl": r} for d, r in daily]
    return s


def _half_hour(ts: int) -> str:
    """入场时间所在的半小时段，例如 "09:30"。"""
    t = from_ns(ts)
    return f"{t.hour:02d}:{'00' if t.minute < 30 else '30'}"
