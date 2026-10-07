"""模拟撮合（DESIGN.md 9.2）：用 1 秒 bar 撮合，规则确定、保守、可配置。

- 订单只参与起点不早于生效时间的完整区间（ts_start >= effective_time）。
- 市价单：生效后第一根有成交的 bar 开盘价 + 不利方向成本（半个价差 + 冲击项），一次全部成交。
- 限价单：开盘价严格优于限价按开盘价成交；否则最低（最高）价严格穿过限价按限价成交；只触及不算。
  可成交量 = bar 成交量 × 参与率，同一标的同一根 bar 上的全部限价单按生效先后共享。
- 止损单：触及止损价即触发，按止损价与开盘价中更差者 + 市价成本成交（跳空按不利价格）。
- 止损限价单：先按止损规则触发，从下一根 bar 起按限价单撮合。
- IOC：只在生效后第一根 bar 上撮合一次，余量撤销。
- 同一根 bar 上同组止盈和止损都能成交：按不利结果（止损），标记路径歧义。
- 常规时段外：只有 outside_rth 的限价单撮合；outside_rth 的其他类型在提交时拒绝。
- 手续费按 IBKR 固定费率：每股 0.005 美元，单张订单最低 1 美元（订单结束时补足一次），
  最高为成交金额的 1%。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

from trader.core.models import Bar, CommissionUpdate, Fill, Order
from trader.core.timeutil import NS_PER_SEC
from trader.oms.orders import Accepted, BrokerUpdate, Cancelled, Expired, Rejected

PRICE_Q = Decimal("0.0001")
CENT = Decimal("0.01")


class SimConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_delay_ms: int = Field(default=0, ge=0, description="决策到订单生效的额外延迟")
    bar_grace_ms: int = Field(default=300, ge=0, description="bar 封口宽限，与实时一致")
    half_spread_bps: dict[str, float] = Field(
        default_factory=lambda: {"default": 2.0}, description="半个价差（基点），按标的"
    )
    impact_k: float = Field(default=10.0, ge=0, description="冲击项系数")
    impact_min_volume: float = Field(
        default=1000, gt=0, description="冲击项使用的 60 秒成交量下限（股）"
    )
    market_max_participation: float = Field(
        default=0.02, gt=0, description="市价单简化模型的适用上限"
    )
    limit_participation: float = Field(default=0.10, gt=0, le=1, description="限价单参与率")
    commission_per_share: float = 0.005
    commission_min: float = 1.0
    commission_max_pct: float = 1.0
    cost_scenarios: list[float] = Field(default_factory=lambda: [1.0, 2.0, 3.0])

    def half_spread(self, symbol: str) -> float:
        return self.half_spread_bps.get(symbol, self.half_spread_bps.get("default", 2.0))


@dataclass(slots=True)
class FillDetail:
    """成交的撮合细节，供报告和成本敏感性计算。"""

    exec_id: str
    client_order_id: str
    symbol: str
    side: str
    ts: int
    price: Decimal
    qty: int
    base_price: float  # 未加成本前的价格（开盘价、限价或止损触发价）
    spread_cost: float  # 美元
    impact_cost: float  # 美元
    kind: str  # market / limit / stop
    ambiguous: bool = False  # 同一根 bar 上止盈止损都可触及，按止损处理
    out_of_range: bool = False  # 超出市价单简化模型的适用范围


@dataclass(slots=True)
class _Working:
    order: Order
    effective: int
    seq: int
    triggered_at: int | None = None  # 止损限价单的触发 bar
    commission: Decimal = Decimal(0)
    last_exec: str | None = None


@dataclass(slots=True)
class _Candidate:
    w: _Working
    qty: int
    price: Decimal
    detail: FillDetail


@dataclass
class SimBroker:
    config: SimConfig
    details: dict[str, FillDetail] = field(default_factory=dict)
    _working: dict[str, _Working] = field(default_factory=dict)
    _vol: dict[str, deque[tuple[int, float]]] = field(default_factory=dict)
    _seq: int = 0
    _exec_seq: int = 0

    # ---------- 订单操作 ----------

    def submit(self, order: Order, effective: int, now: int) -> list[BrokerUpdate]:
        it = order.intent
        if it.outside_rth and it.order_type != "LMT":
            return [Rejected(order.client_order_id, now, "sim", "盘前盘后只接受限价单")]
        self._seq += 1
        self._working[order.client_order_id] = _Working(order, effective, self._seq)
        return [Accepted(order.client_order_id, now)]

    def cancel(self, client_order_id: str, now: int) -> list[BrokerUpdate]:
        w = self._working.pop(client_order_id, None)
        if w is None:
            return []
        return [Cancelled(client_order_id, now), *self._final_commission(w)]

    def modify(self, order: Order, effective: int) -> None:
        """改单：订单对象已带新的 intent；生效时间重新计算，排队顺序排到最后。"""
        w = self._working.get(order.client_order_id)
        if w is not None:
            self._seq += 1
            w.order, w.effective, w.seq, w.triggered_at = order, effective, self._seq, None

    def expire(self, now: int, outside_rth_too: bool) -> list[BrokerUpdate]:
        """DAY 订单过期：常规收盘时过期普通订单，盘后结束时也过期 outside_rth 订单。"""
        out: list[BrokerUpdate] = []
        for coid, w in list(self._working.items()):
            if w.order.intent.outside_rth and not outside_rth_too:
                continue
            del self._working[coid]
            out += [Expired(coid, now), *self._final_commission(w)]
        return out

    def working_ids(self) -> list[str]:
        return list(self._working)

    # ---------- 撮合 ----------

    def _vol60(self, symbol: str, effective: int) -> float:
        dq = self._vol.get(symbol)
        if not dq:
            return 0.0
        lo = effective - 60 * NS_PER_SEC
        return sum(v for ts, v in dq if lo <= ts and ts + NS_PER_SEC <= effective)

    def _record_volume(self, bar: Bar) -> None:
        dq = self._vol.setdefault(bar.symbol, deque())
        dq.append((bar.ts_start, bar.volume))
        while dq and dq[0][0] < bar.ts_start - 120 * NS_PER_SEC:
            dq.popleft()

    def on_bar(self, bar: Bar) -> list[BrokerUpdate]:
        """一根 1 秒 bar 结束后撮合该标的的挂单，返回成交、手续费和撤销回报。"""
        updates: list[BrokerUpdate] = []
        ws = sorted(
            (w for w in self._working.values() if w.order.intent.symbol == bar.symbol),
            key=lambda w: (w.effective, w.seq),
        )
        if ws:
            updates = self._match(bar, ws)
        self._record_volume(bar)
        return updates

    def _match(self, bar: Bar, ws: list[_Working]) -> list[BrokerUpdate]:
        regular = bar.session in (None, "regular")
        budget = int(bar.volume * self.config.limit_participation)
        cands: list[_Candidate] = []
        ioc_done: list[_Working] = []
        for w in ws:
            it = w.order.intent
            if w.effective > bar.ts_start:
                continue
            if not regular and not it.outside_rth:
                continue
            if it.tif == "IOC":
                ioc_done.append(w)
            c: _Candidate | None = None
            if it.order_type == "MKT":
                c = self._market(w, bar, float(bar.open), "market")
            elif it.order_type == "STP":
                c = self._stop(w, bar)
            elif it.order_type == "LMT" or (
                it.order_type == "STP_LMT"
                and w.triggered_at is not None
                and w.triggered_at < bar.ts_start
            ):
                c, budget = self._limit(w, bar, budget)
            elif it.order_type == "STP_LMT" and w.triggered_at is None and self._touched(w, bar):
                w.triggered_at = bar.ts_start  # 触发不等于成交，下一根 bar 起按限价撮合
            if c is not None:
                cands.append(c)
        cands = self._resolve_oca(cands)
        updates: list[BrokerUpdate] = []
        for c in cands:
            updates += self._apply(c, bar)
        for w in ioc_done:  # IOC 只撮合一次
            if w.order.client_order_id in self._working:
                del self._working[w.order.client_order_id]
                updates += [
                    Cancelled(w.order.client_order_id, bar.ts_end, "IOC 未成交部分撤销"),
                    *self._final_commission(w),
                ]
        return updates

    def _touched(self, w: _Working, bar: Bar) -> bool:
        stop = float(w.order.intent.stop_price or 0)
        return bar.high >= stop if w.order.intent.side == "BUY" else bar.low <= stop

    def _stop(self, w: _Working, bar: Bar) -> _Candidate | None:
        if not self._touched(w, bar):
            return None
        stop = float(w.order.intent.stop_price or 0)
        base = max(stop, bar.open) if w.order.intent.side == "BUY" else min(stop, bar.open)
        return self._market(w, bar, base, "stop")

    def _market(self, w: _Working, bar: Bar, base: float, kind: str) -> _Candidate:
        it = w.order.intent
        qty = w.order.remaining_qty
        vol60 = self._vol60(it.symbol, w.effective)
        cfg = self.config
        out_of_range = vol60 < cfg.impact_min_volume or qty > cfg.market_max_participation * vol60
        impact_bps = cfg.impact_k * qty / max(vol60, cfg.impact_min_volume)
        spread_bps = cfg.half_spread(it.symbol)
        sign = 1 if it.side == "BUY" else -1
        price = Decimal(str(base * (1 + sign * (spread_bps + impact_bps) / 1e4))).quantize(
            PRICE_Q, ROUND_HALF_UP
        )
        detail = FillDetail(
            exec_id="",
            client_order_id=w.order.client_order_id,
            symbol=it.symbol,
            side=it.side,
            ts=bar.ts_start,
            price=price,
            qty=qty,
            base_price=base,
            spread_cost=base * spread_bps / 1e4 * qty,
            impact_cost=base * impact_bps / 1e4 * qty,
            kind=kind,
            out_of_range=out_of_range,
        )
        return _Candidate(w, qty, price, detail)

    def _limit(self, w: _Working, bar: Bar, budget: int) -> tuple[_Candidate | None, int]:
        it = w.order.intent
        limit = float(it.limit_price or 0)
        if it.side == "BUY":
            base = bar.open if bar.open < limit else (limit if bar.low < limit else None)
        else:
            base = bar.open if bar.open > limit else (limit if bar.high > limit else None)
        if base is None or budget <= 0:
            return None, budget
        qty = min(w.order.remaining_qty, budget)
        price = Decimal(str(base)).quantize(PRICE_Q, ROUND_HALF_UP)
        detail = FillDetail(
            exec_id="",
            client_order_id=w.order.client_order_id,
            symbol=it.symbol,
            side=it.side,
            ts=bar.ts_start,
            price=price,
            qty=qty,
            base_price=base,
            spread_cost=0.0,
            impact_cost=0.0,
            kind="limit",
        )
        return _Candidate(w, qty, price, detail), budget - qty

    @staticmethod
    def _resolve_oca(cands: list[_Candidate]) -> list[_Candidate]:
        """同组止盈止损在同一根 bar 上都能成交：只保留止损，标记路径歧义。"""
        groups: dict[str, list[_Candidate]] = {}
        for c in cands:
            g = c.w.order.oca_group
            if g:
                groups.setdefault(g, []).append(c)
        drop: set[int] = set()
        for members in groups.values():
            if len(members) > 1:
                stops = [c for c in members if c.w.order.role == "stop_loss"]
                keep = stops[0] if stops else members[0]
                keep.detail.ambiguous = True
                drop |= {id(c) for c in members if c is not keep}
        return [c for c in cands if id(c) not in drop]

    def _apply(self, c: _Candidate, bar: Bar) -> list[BrokerUpdate]:
        self._exec_seq += 1
        exec_id = f"sim-{self._exec_seq:08d}"
        c.detail.exec_id = exec_id
        self.details[exec_id] = c.detail
        coid = c.w.order.client_order_id
        fill = Fill(coid, bar.ts_start, c.price, c.qty, exec_id)
        cfg = self.config
        commission = Decimal(str(cfg.commission_per_share)) * c.qty
        cap = c.price * c.qty * Decimal(str(cfg.commission_max_pct)) / 100
        commission = min(commission, cap).quantize(PRICE_Q)
        c.w.commission += commission
        c.w.last_exec = exec_id
        out: list[BrokerUpdate] = [fill]
        comm = CommissionUpdate(exec_id, commission)
        # 订单完全成交：从挂单中移除，补足最低收费
        if c.w.order.remaining_qty - c.qty <= 0:
            self._working.pop(coid, None)
            extra = self._min_topup(c.w)
            comm = CommissionUpdate(exec_id, commission + extra)
        return [*out, comm]  # type: ignore[list-item]

    def _min_topup(self, w: _Working) -> Decimal:
        if w.last_exec is None:
            return Decimal(0)
        minimum = Decimal(str(self.config.commission_min))
        extra = max(minimum - w.commission, Decimal(0))
        w.commission += extra
        return extra

    def _final_commission(self, w: _Working) -> list[BrokerUpdate]:
        """部分成交后被撤销或过期：补足这张订单的最低收费。"""
        extra = self._min_topup(w)
        if extra > 0 and w.last_exec is not None:
            return [CommissionUpdate(w.last_exec, extra)]  # type: ignore[list-item]
        return []
