"""Bar 聚合（DESIGN.md 6.1）：把 1 秒 bar 合成任意支持的周期。

对齐规则（回测、实时、图表共用，另有一份向量化实现 trader.data.aggregate，由测试保证两者一致）：
- 日内周期从每个时段的开始时刻起按周期切分；时段都从整点或半点开始，所以 30 分钟及以下的周期
  等同于按自然时间对齐。bar 不跨时段：时段末尾不足一个周期的 bar 在时段结束时收盘。
  例如 1 小时 bar：盘前 04:00…08:00、09:00–09:30（半根），常规 09:30、10:30…15:30–16:00（半根）。
- 日线：每个交易日一根，从第一个纳入的时段开始，到最后一个纳入的时段结束。
- sessions="rth" 只纳入常规时段；"extended" 纳入全部时段。
- 周期内没有任何成交就没有这根 bar。

封口由时钟触发：引擎在 next_close_time() 设定定时器，到点调用 on_time()，
不等下一笔行情，成交稀疏的标的也按时收盘。实时封口宽限和迟到修订在阶段 6 实现。
"""

from __future__ import annotations

from dataclasses import dataclass

from trader.core.models import Bar
from trader.core.timeutil import timeframe_ns
from trader.core.trading_calendar import Session, SessionFilter, TradingCalendar, TradingDay

SUPPORTED_TIMEFRAMES = ("1s", "5s", "10s", "30s", "1m", "2m", "5m", "15m", "30m", "1h", "1d")


def check_timeframe(timeframe: str) -> None:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"不支持的周期 {timeframe!r}，可选：{', '.join(SUPPORTED_TIMEFRAMES)}")


def bucket_of(ts: int, session: Session, tf_ns: int) -> tuple[int, int]:
    """ts 所在 bar 的 [开始, 收盘)。"""
    start = session.start + (ts - session.start) // tf_ns * tf_ns
    return start, min(start + tf_ns, session.end)


def daily_bounds(day: TradingDay, sessions: SessionFilter) -> tuple[int, int]:
    inc = day.included(sessions)
    return inc[0].start, inc[-1].end


@dataclass(slots=True)
class _Building:
    start: int
    end: int
    session: str | None
    open: float
    high: float
    low: float
    close: float
    volume: float
    pv: float  # Σ vwap × volume（只计有 vwap 的输入）
    pv_volume: float
    trades: int | None


class BarAggregator:
    """单个标的、单个周期的增量聚合器。输入按时间顺序的 1 秒 bar。"""

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        cal: TradingCalendar,
        sessions: SessionFilter = "extended",
    ) -> None:
        check_timeframe(timeframe)
        self.symbol = symbol
        self.timeframe = timeframe
        self.sessions: SessionFilter = sessions
        self._cal = cal
        self._daily = timeframe == "1d"
        self._tf_ns = 0 if self._daily else timeframe_ns(timeframe)
        self._day: TradingDay | None = None
        self._cur: _Building | None = None

    def _locate(self, ts: int) -> tuple[TradingDay, Session] | None:
        d = self._day
        if d is None or not (d.start <= ts < d.end):
            loc = self._cal.locate(ts)
            if loc is None:
                return None
            self._day = loc[0]
            return loc
        s = d.session_at(ts)
        return (d, s) if s is not None else None

    def update(self, bar: Bar) -> list[Bar]:
        """并入一根 1 秒 bar，返回因此收盘的 bar（正常情况下已由 on_time 收盘，返回空列表）。"""
        loc = self._locate(bar.ts_start)
        if loc is None:
            return []
        day, session = loc
        if self.sessions == "rth" and session.name != "regular":
            return []
        if self._daily:
            start, end = daily_bounds(day, self.sessions)
            label = None
        else:
            start, end = bucket_of(bar.ts_start, session, self._tf_ns)
            label = session.name
        closed: list[Bar] = []
        cur = self._cur
        if cur is not None and cur.start != start:
            closed.append(self._emit(cur))
            cur = None
        has_vwap = bar.vwap is not None
        if cur is None:
            self._cur = _Building(
                start=start,
                end=end,
                session=label,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                pv=bar.vwap * bar.volume if bar.vwap is not None else 0.0,
                pv_volume=bar.volume if has_vwap else 0.0,
                trades=bar.trades,
            )
        else:
            cur.high = max(cur.high, bar.high)
            cur.low = min(cur.low, bar.low)
            cur.close = bar.close
            cur.volume += bar.volume
            if bar.vwap is not None:
                cur.pv += bar.vwap * bar.volume
                cur.pv_volume += bar.volume
            if cur.trades is not None and bar.trades is not None:
                cur.trades += bar.trades
            else:
                cur.trades = None
        return closed

    def next_close_time(self) -> int | None:
        """正在形成的 bar 的收盘时间；没有正在形成的 bar 时返回 None。"""
        return self._cur.end if self._cur is not None else None

    def on_time(self, now: int) -> Bar | None:
        """时钟到达 now：正在形成的 bar 已到收盘时间就收盘并返回。"""
        cur = self._cur
        if cur is not None and now >= cur.end:
            self._cur = None
            return self._emit(cur)
        return None

    def partial(self) -> Bar | None:
        """正在形成的 bar 的当前状态（closed=False 的中间更新，仅供界面）。"""
        return self._emit(self._cur) if self._cur is not None else None

    def flush(self) -> Bar | None:
        """不管时间，强制收盘正在形成的 bar（只用于批量计算的结尾）。"""
        cur, self._cur = self._cur, None
        return self._emit(cur) if cur is not None else None

    def _emit(self, b: _Building) -> Bar:
        full = (not self._daily) and b.end == b.start + self._tf_ns
        return Bar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            ts_start=b.start,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
            vwap=b.pv / b.pv_volume if b.pv_volume > 0 else None,
            trades=b.trades,
            session=b.session,
            closes_at=None if full else b.end,
        )
