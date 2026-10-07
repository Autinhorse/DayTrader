"""策略接口（DESIGN.md 7.1–7.4）。

策略只有一份代码，用于回测、回放、模拟盘和实盘；差异全部由注入的上下文承担，
策略里不允许出现按运行模式分支的判断（ctx 不提供查询运行模式的方法）。

约束（tests/test_architecture.py 静态检查 user/strategies/）：
- 不直接取系统时间，不读写文件、不联网，不导入 trader.brokers / data / oms 等；
- 每次下单、改单、撤单都要给 Reason；
- 需要跨重启保留的状态放在 ctx.state（只接受可 JSON 序列化的值）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from trader.core.models import Bar, Fill, Order, Position, Reason
from trader.core.trading_calendar import TradingDay


@dataclass(frozen=True)
class FeedRequirements:
    trades_complete: bool = False  # 是否必须完整逐笔成交
    quotes: bool = False
    min_bar_interval: str = "1s"  # 策略用到的最小 bar 周期
    allow_degraded: bool = False  # 是否允许在降级行情下继续运行


class StrategyParams(BaseModel):
    """参数模型基类：未知参数报错。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class IndicatorHandle(Protocol):
    @property
    def value(self) -> float | None: ...  # 最新收盘 bar 上的值（单输出指标）

    def __getitem__(self, i: int) -> float | None: ...  # [-1] 是上一根 bar 的值

    def get(self, key: str, back: int = 0) -> float | None: ...  # 多输出指标按 key 取


Price = float | Decimal | None


class StrategyContext(Protocol):
    # 行情与指标
    def subscribe(self, symbol: str, timeframe: str) -> None: ...
    def indicator(
        self, name: str, symbol: str, timeframe: str, **params: Any
    ) -> IndicatorHandle: ...
    def history(self, symbol: str, timeframe: str, n: int) -> list[Bar]: ...

    # 下单（reason 必填）；返回 client_order_id
    def buy(
        self,
        symbol: str,
        qty: int,
        *,
        reason: Reason,
        order_type: str = "MKT",
        limit_price: Price = None,
        stop_price: Price = None,
        take_profit: Price = None,
        stop_loss: Price = None,
        tif: str = "DAY",
        outside_rth: bool = False,
    ) -> str: ...
    def sell(
        self,
        symbol: str,
        qty: int,
        *,
        reason: Reason,
        order_type: str = "MKT",
        limit_price: Price = None,
        stop_price: Price = None,
        take_profit: Price = None,
        stop_loss: Price = None,
        tif: str = "DAY",
        outside_rth: bool = False,
    ) -> str: ...
    def modify(
        self,
        client_order_id: str,
        *,
        reason: Reason,
        qty: int | None = None,
        limit_price: Price = None,
        stop_price: Price = None,
    ) -> None: ...
    def cancel(self, client_order_id: str, *, reason: Reason) -> None: ...
    def close_position(self, symbol: str, *, reason: Reason) -> str | None: ...

    # 状态
    def position(self, symbol: str) -> Position: ...
    def open_orders(self, symbol: str | None = None) -> list[Order]: ...
    def now(self) -> int: ...
    def set_timer(self, name: str, at: int) -> None: ...
    @property
    def state(self) -> StrategyState: ...
    @property
    def today(self) -> TradingDay | None: ...  # 当前交易日的时段信息

    # 记录
    def log(self, msg: str, **fields: Any) -> None: ...
    def mark(self, symbol: str, text: str, kind: str = "note") -> None: ...


class StrategyState(MutableMapping[str, Any]):
    """ctx.state：只接受可 JSON 序列化的值。实盘版每次回调结束后落盘；回测中只在内存里。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._d: dict[str, Any] = dict(data or {})

    def __getitem__(self, k: str) -> Any:
        return self._d[k]

    def __setitem__(self, k: str, v: Any) -> None:
        try:
            json.dumps(v)
        except (TypeError, ValueError) as e:
            raise TypeError(f"ctx.state[{k!r}] 只能保存可 JSON 序列化的值：{e}") from None
        self._d[k] = v

    def __delitem__(self, k: str) -> None:
        del self._d[k]

    def __iter__(self) -> Iterator[str]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._d))


class Strategy(ABC):
    name: ClassVar[str]
    Params: ClassVar[type[StrategyParams]]
    requires: ClassVar[FeedRequirements] = FeedRequirements()
    description: ClassVar[str] = ""

    def __init__(self, params: StrategyParams, ctx: StrategyContext) -> None:
        self.params = params
        self.ctx = ctx

    def on_start(self) -> None:  # noqa: B027
        """订阅行情、声明指标、读取 ctx.state。"""

    def on_session_open(self) -> None:  # noqa: B027
        """每个交易日开始时调用（盘前开始；有夜盘时为夜盘开始）。"""

    @abstractmethod
    def on_bar(self, bar: Bar) -> None:
        """订阅周期的 bar 收盘并到达可用时间时调用。"""

    def on_order(self, order: Order) -> None:  # noqa: B027
        """订单状态变化（含被风控拒绝）。"""

    def on_fill(self, fill: Fill) -> None:  # noqa: B027
        """成交。"""

    def on_timer(self, name: str) -> None:  # noqa: B027
        """ctx.set_timer 设置的定时器到点。"""

    def on_session_close(self) -> None:  # noqa: B027
        """常规时段收盘时调用。"""

    def on_stop(self) -> None:  # noqa: B027
        """策略停止。"""


# ---------- 注册与自动发现 ----------

_REGISTRY: dict[str, type[Strategy]] = {}
USER_MODULE_PREFIX = "user_strategies"


def register_strategy[S: type[Strategy]](cls: S) -> S:
    for attr in ("name", "Params"):
        if not hasattr(cls, attr):
            raise TypeError(f"策略 {cls.__qualname__} 缺少类属性 {attr}")
    old = _REGISTRY.get(cls.name)
    if old is not None and (old.__module__, old.__qualname__) != (cls.__module__, cls.__qualname__):
        raise ValueError(f"策略名 {cls.name!r} 已被 {old.__module__}.{old.__qualname__} 使用")
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str) -> type[Strategy]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"没有名为 {name!r} 的策略，可用：{', '.join(sorted(_REGISTRY))}") from None


def list_strategies() -> list[type[Strategy]]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def load_user_strategies(directory: Path) -> dict[str, str]:
    """加载 user/strategies/ 下每个 .py 文件，返回 {文件名: 错误}；同名文件再次加载即热加载。"""
    errors: dict[str, str] = {}
    if not directory.exists():
        return errors
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        mod_name = f"{USER_MODULE_PREFIX}.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("无法加载")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception as e:  # 一个文件出错不影响其他策略
            sys.modules.pop(mod_name, None)
            errors[path.name] = f"{type(e).__name__}: {e}"
    return errors
