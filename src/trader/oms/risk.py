"""交易前风控（DESIGN.md 8.3）：纯函数 check(intent, view, limits) -> None | Reject。

所有运行组合共用这份实现，阈值来自各自的配置。约定：
- 在途订单计入敞口：持仓上限、总敞口、做空开关按最坏情况算
  （持仓 + 未终结订单中会增加敞口的未成交量）。
- 退出不受开仓限制：减仓单（含止盈止损子单、收盘前平仓）只检查白名单、归属、
  数量不超过可平持仓和连接状态。
- 计数类规则（频率、重复）每张订单只计一次：由调用方只在创建时传入，复检时 count_once=False。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from trader.core.models import OrderIntent
from trader.core.timeutil import NS_PER_MIN, NS_PER_SEC


class RiskLimits(BaseModel):
    """风控阈值。默认值按 2026-10-07 确认的规模：单笔 5,000～20,000 美元，初始资金 10 万美元。"""

    model_config = ConfigDict(extra="forbid")

    max_order_qty: int = Field(default=5000, ge=1, description="单笔最大股数")
    max_order_usd: float = Field(default=20_000, gt=0, description="单笔最大金额")
    max_position_usd: float = Field(default=20_000, gt=0, description="单标的持仓上限（含在途）")
    max_gross_exposure_usd: float = Field(
        default=100_000, gt=0, description="全部持仓市值绝对值之和上限"
    )
    max_daily_loss_usd: float = Field(default=2_000, gt=0, description="当日亏损达到后只允许减仓")
    max_orders_per_min: int = Field(default=10, ge=1, description="每分钟新订单数上限")
    price_protection_pct: float = Field(
        default=3.0, gt=0, description="限价偏离最新价超过该百分比则拒绝"
    )
    allow_short: bool = True
    no_open_first_min: int = Field(default=0, ge=0, description="常规时段开盘后 N 分钟内禁止新开仓")
    no_open_before_flatten_min: int = Field(
        default=10, ge=0, description="收盘前平仓前 N 分钟起禁止新开仓"
    )
    duplicate_window_s: int = Field(
        default=2, ge=0, description="同一来源完全相同的订单视为重复的时间窗"
    )
    max_data_age_s: int = Field(default=120, ge=1, description="行情超过这么久未更新则拒绝新开仓")
    flatten_before_close_min: int = Field(default=5, ge=1, description="收盘前 N 分钟强制平仓")


@dataclass(frozen=True, slots=True)
class Reject:
    rule: str
    detail: str


@dataclass(slots=True)
class RiskView:
    """风控检查时刻的状态快照，由订单管理模块构造。"""

    now: int
    whitelist: frozenset[str]
    owners: dict[str, str]
    positions: dict[str, int]
    # 其他未终结订单的未成交量（不含被检查的这张）：(买入量, 卖出量)
    pending: dict[str, tuple[int, int]]
    last_price: dict[str, float]
    last_bar_time: dict[str, int]
    gross_exposure: float
    daily_pnl: float
    session: str | None  # 当前时段名；不在交易时段为 None
    allowed_sessions: frozenset[str]  # 允许新开仓的时段
    regular_open: int | None
    flatten_at: int | None  # 当日收盘前平仓时刻
    flattening: bool  # 已开始收盘前平仓
    connected: bool = True
    recent_orders: list[int] = field(default_factory=list)  # 同一来源已创建订单的时间
    recent_signatures: list[tuple[int, tuple]] = field(default_factory=list)


def signature(intent: OrderIntent) -> tuple:
    return (
        intent.source,
        intent.symbol,
        intent.side,
        intent.qty,
        intent.order_type,
        intent.limit_price,
        intent.stop_price,
    )


def closable_qty(intent: OrderIntent, view: RiskView) -> int:
    """这张订单方向上可以平掉的持仓（扣除其他同方向在途订单）。"""
    pos = view.positions.get(intent.symbol, 0)
    buy, sell = view.pending.get(intent.symbol, (0, 0))
    if intent.side == "SELL" and pos > 0:
        return max(pos - sell, 0)
    if intent.side == "BUY" and pos < 0:
        return max(-pos - buy, 0)
    return 0


def is_exit(intent: OrderIntent, view: RiskView) -> bool:
    return 0 < intent.qty <= closable_qty(intent, view)


def _ref_price(intent: OrderIntent, view: RiskView) -> float | None:
    px = intent.limit_price or intent.stop_price
    if px is not None:
        return float(px)
    return view.last_price.get(intent.symbol)


def check(
    intent: OrderIntent,
    view: RiskView,
    limits: RiskLimits,
    *,
    exit_order: bool | None = None,
    count_once: bool = True,
) -> Reject | None:
    """exit_order 为 None 时按持仓自动判断是否减仓单；括号单子单和收盘前平仓由调用方传 True。"""
    sym = intent.symbol
    if sym not in view.whitelist:
        return Reject("whitelist", f"{sym} 不在标的清单中")
    owner = view.owners.get(sym)
    if owner is not None and owner != intent.source:
        return Reject("ownership", f"{sym} 属于 {owner}")
    if not view.connected:
        return Reject("connection", "与执行器断开")

    exiting = is_exit(intent, view) if exit_order is None else exit_order
    if exiting:
        if intent.qty > closable_qty(intent, view) and exit_order is None:
            return Reject("exit_qty", "数量超过可平持仓")
        return None

    # ---------- 以下只对开仓（或增加敞口）的订单 ----------
    if view.flattening:
        return Reject("time_window", "已开始收盘前平仓，禁止新开仓")
    if view.session not in view.allowed_sessions:
        return Reject("time_window", f"当前时段 {view.session} 不允许开仓")
    if (
        view.regular_open is not None
        and view.session == "regular"
        and view.now < view.regular_open + limits.no_open_first_min * NS_PER_MIN
    ):
        return Reject("time_window", f"开盘后 {limits.no_open_first_min} 分钟内禁止新开仓")
    if view.flatten_at is not None and view.now >= (
        view.flatten_at - limits.no_open_before_flatten_min * NS_PER_MIN
    ):
        return Reject(
            "time_window", f"收盘前平仓前 {limits.no_open_before_flatten_min} 分钟禁止新开仓"
        )

    last_bar = view.last_bar_time.get(sym)
    if last_bar is None or view.now - last_bar > limits.max_data_age_s * NS_PER_SEC:
        return Reject("data_freshness", f"{sym} 行情超过 {limits.max_data_age_s} 秒未更新")

    px = _ref_price(intent, view)
    if px is None:
        return Reject("price_protection", f"{sym} 没有最新价，无法评估")
    if intent.qty > limits.max_order_qty:
        return Reject("order_qty", f"{intent.qty} 股超过单笔上限 {limits.max_order_qty}")
    notional = intent.qty * px
    if notional > limits.max_order_usd:
        return Reject("order_usd", f"单笔 {notional:,.0f} 美元超过上限 {limits.max_order_usd:,.0f}")

    last = view.last_price.get(sym)
    if intent.limit_price is not None and last:
        dev = abs(float(intent.limit_price) - last) / last * 100
        if dev > limits.price_protection_pct:
            return Reject("price_protection", f"限价偏离最新价 {dev:.1f}%")

    pos = view.positions.get(sym, 0)
    buy, sell = view.pending.get(sym, (0, 0))
    signed = intent.qty if intent.side == "BUY" else -intent.qty
    worst_long = pos + buy + max(signed, 0)
    worst_short = pos - sell + min(signed, 0)
    if not limits.allow_short and worst_short < 0:
        return Reject("short", "配置不允许做空")
    worst = max(abs(worst_long), abs(worst_short)) * px
    if worst > limits.max_position_usd:
        return Reject("position_usd", f"最坏持仓 {worst:,.0f} 美元超过上限")
    increase = max(abs(pos + signed) - abs(pos), 0) * px
    if view.gross_exposure + increase > limits.max_gross_exposure_usd:
        return Reject("gross_exposure", "总敞口超过上限")
    if view.daily_pnl <= -limits.max_daily_loss_usd:
        return Reject("daily_loss", f"当日亏损 {-view.daily_pnl:,.0f} 美元已达上限")

    if count_once:
        recent = [t for t in view.recent_orders if view.now - t < NS_PER_MIN]
        if len(recent) >= limits.max_orders_per_min:
            return Reject("order_rate", f"每分钟最多 {limits.max_orders_per_min} 张新订单")
        sig = signature(intent)
        window = limits.duplicate_window_s * NS_PER_SEC
        for t, s in view.recent_signatures:
            if s == sig and view.now - t < window:
                return Reject("duplicate", "短时间内重复的相同订单")
    return None
