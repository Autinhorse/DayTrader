"""引擎事件（DESIGN.md 4.3）与调度阶段（DESIGN.md 2.3）。

每个事件都有 available_time：它最早可以进入引擎的时间。调度器按
(available_time, phase, 序号) 排序，同一时刻的阶段顺序固定为：
撮合 → 交付成交与订单更新 → 发布收盘 bar → 交易时段事件 → 定时器。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from trader.core.models import Bar, CommissionUpdate, Fill, MarketMeta, Order, Quote, Tick


class Phase(IntEnum):
    """同一时刻内的处理顺序，数值小的先处理。"""

    MATCH = 1  # 用刚结束的市场区间撮合此前已生效的挂单
    DELIVER = 2  # 成交、手续费、订单更新 → on_fill / on_order
    MARKET = 3  # 收盘 bar、tick、报价 → 聚合、指标、on_bar
    SESSION = 4  # 交易时段事件
    TIMER = 5  # 定时器
    SYSTEM = 6  # 系统事件：放在同一时刻的最后，不打断前面阶段的处理


@dataclass(frozen=True, slots=True)
class BarEvent:
    bar: Bar
    meta: MarketMeta
    closed: bool = True  # False 是未收盘 bar 的中间更新，只发给界面，不发给策略

    phase = Phase.MARKET

    @property
    def available_time(self) -> int:
        return self.meta.available_time


@dataclass(frozen=True, slots=True)
class TickEvent:
    tick: Tick
    meta: MarketMeta

    phase = Phase.MARKET

    @property
    def available_time(self) -> int:
        return self.meta.available_time


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    quote: Quote
    meta: MarketMeta

    phase = Phase.MARKET

    @property
    def available_time(self) -> int:
        return self.meta.available_time


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """订单更新：券商原始状态（模拟撮合时为 None）和归约后的订单快照。

    order 必须是快照（发布者负责复制），接收方不得修改它。
    """

    ts: int
    order: Order
    broker_status: str | None = None

    phase = Phase.DELIVER

    @property
    def available_time(self) -> int:
        return self.ts


@dataclass(frozen=True, slots=True)
class FillEvent:
    fill: Fill

    phase = Phase.DELIVER

    @property
    def available_time(self) -> int:
        return self.fill.ts


@dataclass(frozen=True, slots=True)
class CommissionEvent:
    ts: int  # 手续费到达时间，可能晚于成交
    update: CommissionUpdate

    phase = Phase.DELIVER

    @property
    def available_time(self) -> int:
        return self.ts


@dataclass(frozen=True, slots=True)
class TimerEvent:
    name: str
    at: int  # 设定的触发时间

    phase = Phase.TIMER

    @property
    def available_time(self) -> int:
        return self.at


class SessionKind(StrEnum):
    PRE_OPEN = "PRE_OPEN"  # 盘前开始 04:00
    OPEN = "OPEN"  # 常规时段开盘
    CLOSE = "CLOSE"  # 常规时段收盘（半日市为 13:00）
    POST_CLOSE = "POST_CLOSE"  # 盘后结束


@dataclass(frozen=True, slots=True)
class SessionEvent:
    kind: SessionKind
    ts: int

    phase = Phase.SESSION

    @property
    def available_time(self) -> int:
        return self.ts


class SystemKind(StrEnum):
    CONNECTION_LOST = "CONNECTION_LOST"
    CONNECTION_RESTORED = "CONNECTION_RESTORED"
    FEED_INTERRUPTED = "FEED_INTERRUPTED"
    FEED_DEGRADED = "FEED_DEGRADED"
    FEED_RESTORED = "FEED_RESTORED"
    RISK_TRIGGERED = "RISK_TRIGGERED"
    RECONCILE_MISMATCH = "RECONCILE_MISMATCH"


@dataclass(frozen=True, slots=True)
class SystemEvent:
    kind: SystemKind
    ts: int
    detail: str = ""
    symbol: str | None = None

    phase = Phase.SYSTEM

    @property
    def available_time(self) -> int:
        return self.ts


Event = (
    BarEvent
    | TickEvent
    | QuoteEvent
    | OrderEvent
    | FillEvent
    | CommissionEvent
    | TimerEvent
    | SessionEvent
    | SystemEvent
)
