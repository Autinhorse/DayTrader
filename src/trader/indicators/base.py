"""指标接口与注册（DESIGN.md 6.2、6.3）。

- update(bar) 是唯一的计算入口：批量计算就是对历史 bar 逐根调用，不另写向量化实现。
- 指标只能依赖传入的 bar 和自身状态，不能访问未来数据、时钟或外部资源。
- 连续指标（continuous）跨交易日延续；会话指标（session）在每个交易日开始时由框架调用
  on_session_open(day) 重新开始。day 提供该交易日各时段的起止时间（例如常规时段开盘时刻）。
- 没有通用的 reset：需要从头计算时，框架新建实例并重新预热。
- 细粒度输入（决策 0018）：input_timeframe 不为空的指标（例如 "1s"），框架先用 ingest(bar)
  按时间顺序喂入该周期的 bar，再在每根显示周期的 bar 收盘时调用 update(bar) 取值；同一时刻
  先喂入细 bar。于是 5 分钟图上的值也是用 1 秒 bar 算的，且只含该 bar 收盘之前的数据。
  显示周期等于输入周期时两者都调用。
  批量计算历史时框架改用 ingest_batch（一段 bar 的数组，没有 session、trades），默认逐根转给
  ingest；指标可以覆盖它加速，但结果必须与逐根 ingest 相同（引擎逐根喂入）。
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from trader.core.models import Bar
from trader.core.trading_calendar import TradingDay


@dataclass(frozen=True)
class WarmupSpec:
    """输出有效值前需要的历史数据；三项取最早的起点。"""

    bars: int = 0  # 固定根数
    from_session_start: bool = False  # 需要当前交易日从开始起的全部 bar（日内 VWAP、开盘区间）
    prev_sessions: int = 0  # 需要之前 N 个完整交易日（前一日高低收）


@dataclass(frozen=True)
class OutputSpec:
    key: str  # 例如 "value" "upper" "signal"
    plot: Literal["line", "histogram", "band", "marker", "hline"]
    pane: Literal["price", "separate"]  # 叠加在主图，或单独窗格
    color: str | None = None


@dataclass(frozen=True, slots=True)
class BarBatch:
    """按时间排序的一段 bar（细粒度输入的批量喂入用），各列为等长 numpy 数组。"""

    symbol: str
    timeframe: str
    ts_start: np.ndarray
    ts_end: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    vwap: np.ndarray  # 缺失为 nan

    def __len__(self) -> int:
        return len(self.ts_start)

    def slice(self, i: int, j: int) -> BarBatch:
        return BarBatch(
            self.symbol, self.timeframe, self.ts_start[i:j], self.ts_end[i:j], self.open[i:j],
            self.high[i:j], self.low[i:j], self.close[i:j], self.volume[i:j], self.vwap[i:j],
        )  # fmt: skip

    def bars(self) -> Iterator[Bar]:
        for k in range(len(self)):
            vw = float(self.vwap[k])
            yield Bar(
                self.symbol, self.timeframe, int(self.ts_start[k]), float(self.open[k]),
                float(self.high[k]), float(self.low[k]), float(self.close[k]),
                float(self.volume[k]), None if vw != vw else vw, None, None, int(self.ts_end[k]),
            )  # fmt: skip


class IndicatorParams(BaseModel):
    """参数模型基类：未知参数报错。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Indicator(ABC):
    name: ClassVar[str]  # 注册名，例如 "ema"
    Params: ClassVar[type[IndicatorParams]]  # 参数模型，前端据此生成参数表单
    outputs: ClassVar[list[OutputSpec]]  # 每条输出序列的名称与绘图方式
    scope: ClassVar[Literal["continuous", "session"]] = "continuous"
    description: ClassVar[str] = ""
    input_timeframe: ClassVar[str | None] = None  # 细粒度输入周期，见模块说明

    def __init__(self, params: IndicatorParams) -> None:
        self.params = params

    def warmup(self) -> WarmupSpec:
        return WarmupSpec()

    @abstractmethod
    def update(self, bar: Bar) -> Mapping[str, float | None]:
        """每根收盘 bar 调用一次，返回 outputs 中每个 key 的当前值（数据不足时为 None）。"""

    def on_session_open(self, day: TradingDay) -> None:  # noqa: B027  会话指标按需覆盖
        """仅会话指标：每个交易日开始时由框架调用。"""

    def ingest(self, bar: Bar) -> None:  # noqa: B027  细粒度输入的指标覆盖
        """仅 input_timeframe 不为空的指标：每根输入周期的收盘 bar 调用一次，只更新状态。"""

    def ingest_batch(self, batch: BarBatch) -> None:
        """批量喂入（历史计算用）。默认逐根调用 ingest；覆盖时结果必须相同。"""
        for bar in batch.bars():
            self.ingest(bar)


# ---------- 注册 ----------

_REGISTRY: dict[str, type[Indicator]] = {}

USER_MODULE_PREFIX = "user_indicators"


def register_indicator[I: type[Indicator]](cls: I) -> I:
    """类装饰器。重名时报错；同一个文件重新加载（热加载）时覆盖旧的。"""
    for attr in ("name", "Params", "outputs"):
        if not hasattr(cls, attr):
            raise TypeError(f"指标 {cls.__qualname__} 缺少类属性 {attr}")
    old = _REGISTRY.get(cls.name)
    if old is not None and (old.__module__, old.__qualname__) != (cls.__module__, cls.__qualname__):
        raise ValueError(f"指标名 {cls.name!r} 已被 {old.__module__}.{old.__qualname__} 使用")
    _REGISTRY[cls.name] = cls
    return cls


def get_indicator(name: str) -> type[Indicator]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"没有名为 {name!r} 的指标，可用：{', '.join(sorted(_REGISTRY))}") from None


def list_indicators() -> list[type[Indicator]]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def create(name: str, **params: object) -> Indicator:
    cls = get_indicator(name)
    return cls(cls.Params(**params))


def load_builtin() -> None:
    import trader.indicators.builtin as pkg

    for m in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg.__name__}.{m.name}")


def load_user(directory: Path) -> dict[str, str]:
    """加载 directory 下每个 .py 文件（不含 _ 开头的），返回 {文件名: 错误信息}；成功的不在结果里。

    同名文件再次加载会重新执行（热加载）。
    """
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
        except Exception as e:  # 一个文件出错不影响其他指标
            sys.modules.pop(mod_name, None)
            errors[path.name] = f"{type(e).__name__}: {e}"
    return errors


def load_all(user_dir: Path | None) -> dict[str, str]:
    load_builtin()
    return load_user(user_dir) if user_dir is not None else {}
