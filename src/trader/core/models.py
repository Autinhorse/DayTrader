"""核心数据模型（DESIGN.md 4.2）。所有模块只通过这些类型交互。

行情价格用 float64，订单价格用 Decimal。时间一律是 UTC 纳秒整数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from trader.core.timeutil import timeframe_ns

Side = Literal["BUY", "SELL"]
OrderType = Literal["MKT", "LMT", "STP", "STP_LMT"]
TimeInForce = Literal["DAY", "IOC"]


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str  # 例如 "AAPL"
    exchange: str = "SMART"
    currency: str = "USD"
    min_tick: Decimal = Decimal("0.01")  # 默认值；实盘版从 IBKR 合约信息读取实际报价规则
    con_id: int | None = None  # IBKR 合约编号，实盘版解析后填入


@dataclass(frozen=True, slots=True)
class MarketMeta:
    """每个行情事件都携带的来源与质量信息。"""

    source: str  # "massive" "ibkr" "synthetic"
    feed_kind: str  # "agg_1s" "trades" "tick_by_tick" "snapshot" "backfill"
    event_time: int  # 市场发生时间
    received_time: int | None  # 本机接收时间；历史数据为 None
    available_time: int  # 策略最早可见时间
    sequence: int
    synthetic: bool = False
    quality_flags: frozenset[str] = frozenset()  # 例如 "degraded" "gap_before" "late_revision"


@dataclass(frozen=True, slots=True)
class Bar:
    """区间 [ts_start, ts_end) 的 OHLCV。没有合格成交的区间没有 bar，不补空 bar。

    bar 不跨时段：时段末尾不足一个周期的 bar 在时段结束时收盘（closes_at），日线在交易日结束时收盘。
    """

    symbol: str
    timeframe: str  # "1s" "5s" "1m" "5m" "15m" "1h" "1d"
    ts_start: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    trades: int | None = None
    session: str | None = None  # "overnight" "pre" "regular" "post"；日线为 None
    closes_at: int | None = None  # 不足一个周期的 bar 和日线的实际收盘时间

    @property
    def ts_end(self) -> int:
        if self.closes_at is not None:
            return self.closes_at
        return self.ts_start + timeframe_ns(self.timeframe)


@dataclass(frozen=True, slots=True)
class Tick:
    """逐笔成交。"""

    symbol: str
    ts: int
    price: float
    size: float
    conditions: tuple[str, ...] = ()  # 成交条件代码，用于聚合时过滤


@dataclass(frozen=True, slots=True)
class Quote:
    """最优买卖价，可选。"""

    symbol: str
    ts: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float


@dataclass(frozen=True, slots=True)
class Reason:
    """下单原因，贯穿订单、成交和回测报告。"""

    code: str  # 机器可统计，例如 "ema_cross_up" "stop_loss" "eod_flatten" "manual"
    text: str = ""  # 人可读说明
    context: dict[str, float] = field(default_factory=dict)  # 下单瞬间的指标快照

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("Reason.code 不能为空")


_PRICES_REQUIRED: dict[str, tuple[bool, bool]] = {
    # order_type: (需要 limit_price, 需要 stop_price)
    "MKT": (False, False),
    "LMT": (True, False),
    "STP": (False, True),
    "STP_LMT": (True, True),
}


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """策略或手动下单发出的订单意图。只校验自身是否完整；报价规则和风控由 oms 负责。"""

    source: str  # 策略实例 id，或 "manual"
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    reason: Reason  # 必填
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: TimeInForce = "DAY"
    outside_rth: bool = False
    take_profit: Decimal | None = None  # 两者任一非空即为括号单
    stop_loss: Decimal | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side 只能是 BUY 或 SELL：{self.side!r}")
        if self.order_type not in _PRICES_REQUIRED:
            raise ValueError(f"无法识别的订单类型：{self.order_type!r}")
        if self.tif not in ("DAY", "IOC"):
            raise ValueError(f"无法识别的 tif：{self.tif!r}")
        if self.qty <= 0:
            raise ValueError(f"qty 必须为正数：{self.qty}")
        need_limit, need_stop = _PRICES_REQUIRED[self.order_type]
        if need_limit != (self.limit_price is not None):
            raise ValueError(
                f"{self.order_type} 订单{'必须' if need_limit else '不能'}带 limit_price"
            )
        if need_stop != (self.stop_price is not None):
            raise ValueError(
                f"{self.order_type} 订单{'必须' if need_stop else '不能'}带 stop_price"
            )
        for name in ("limit_price", "stop_price", "take_profit", "stop_loss"):
            price = getattr(self, name)
            if price is not None and (not isinstance(price, Decimal) or price <= 0):
                raise ValueError(f"{name} 必须是正的 Decimal：{price!r}")

    @property
    def is_bracket(self) -> bool:
        return self.take_profit is not None or self.stop_loss is not None


@dataclass(frozen=True, slots=True)
class Fill:
    """成交事实，按 broker_exec_id 去重。

    corrects 非空表示这是对另一笔成交（其 broker_exec_id）的更正：保留原记录，以新版本为准。
    """

    client_order_id: str
    ts: int
    price: Decimal
    qty: int
    broker_exec_id: str
    corrects: str | None = None


@dataclass(frozen=True, slots=True)
class CommissionUpdate:
    """手续费可能晚于成交到达，单独成事件。"""

    broker_exec_id: str
    commission: Decimal


class OrderStatus(StrEnum):
    NEW = "NEW"  # 已创建，尚未过风控
    AWAITING_CONFIRM = "AWAITING_CONFIRM"  # 实盘半自动方式下等待人工确认
    SUBMITTED = "SUBMITTED"  # 请求已记录并发给执行器，未确认
    UNKNOWN = "UNKNOWN"  # 发送结果不明，等待查询确认
    ACCEPTED = "ACCEPTED"  # 执行器确认挂单
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"  # 撤单请求已发出，期间仍可能成交
    PENDING_REPLACE = "PENDING_REPLACE"  # 改单请求已发出，期间仍可能成交
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)


@dataclass(slots=True)
class Order:
    """可变的运行时订单，由归约函数 trader.oms.orders.reduce 从事件流算出（DESIGN.md 8.1）。

    改单时用新的 intent 替换旧的（数量、限价、止损价）。
    """

    intent: OrderIntent
    client_order_id: str
    status: OrderStatus = OrderStatus.NEW
    # 券商侧标识，实盘版填入
    account: str | None = None
    client_id: int | None = None
    broker_order_id: int | None = None
    perm_id: int | None = None
    filled_qty: int = 0
    avg_fill_price: Decimal | None = None
    commission: Decimal = Decimal(0)
    commission_pending: bool = False  # 有成交的手续费尚未到达时为 True，净盈亏视为未结算
    status_times: dict[OrderStatus, int] = field(default_factory=dict)  # 各状态首次进入的时间
    exec_ids: set[str] = field(default_factory=set)  # 已入账的成交，按 broker_exec_id 去重
    created_at: int = 0
    parent_id: str | None = None  # 括号单的子单指向入场单
    oca_group: str | None = None  # 同组的退出单（止盈、止损）互斥
    role: str = "single"  # single / entry / take_profit / stop_loss
    reject_rule: str | None = None  # 被风控或执行器拒绝的规则
    reject_detail: str = ""
    # 每笔成交的当前版本（键为最初的 broker_exec_id；更正后指向新版本），用于重算成交量和均价
    fills: dict[str, Fill] = field(default_factory=dict)
    commission_execs: set[str] = field(default_factory=set)  # 已收到手续费的成交
    broker_status: str | None = None  # 券商原始状态，原样保留
    pending_intent: OrderIntent | None = None  # 改单请求已发出、尚未确认的新内容
    notes: list[str] = field(
        default_factory=list
    )  # 异常情况（迟到成交、超量成交、无法识别的状态等）

    @property
    def remaining_qty(self) -> int:
        return self.intent.qty - self.filled_qty


@dataclass(slots=True)
class Position:
    """某个所有者在某个标的上的持仓。成本按均价法，与 IBKR 一致。"""

    symbol: str
    qty: int = 0  # 负数为空头
    avg_cost: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: float = 0.0  # 按最新行情价计算，行情是 float
