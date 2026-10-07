"""持仓与盈亏（DESIGN.md 8.2）：均价法，与 IBKR 一致。

支持做空，以及一笔成交穿越零点（平仓后反向开仓）。
"""

from __future__ import annotations

from decimal import Decimal

from trader.core.models import Position, Side


class Portfolio:
    def __init__(self, cash: Decimal) -> None:
        self.cash = cash
        self.positions: dict[str, Position] = {}
        self.commission = Decimal(0)
        self.last_price: dict[str, float] = {}

    def position(self, symbol: str) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = self.positions[symbol] = Position(symbol)
        return pos

    def apply_fill(self, symbol: str, side: Side, qty: int, price: Decimal) -> Decimal:
        """入账一笔成交，返回这笔成交实现的盈亏（不含手续费）。"""
        pos = self.position(symbol)
        signed = qty if side == "BUY" else -qty
        self.cash -= price * signed
        realized = Decimal(0)
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            total = abs(pos.qty) + qty
            pos.avg_cost = (pos.avg_cost * abs(pos.qty) + price * qty) / total
            pos.qty += signed
        else:
            closing = min(qty, abs(pos.qty))
            direction = 1 if pos.qty > 0 else -1
            realized = (price - pos.avg_cost) * closing * direction
            pos.realized_pnl += realized
            pos.qty += signed
            if pos.qty == 0:
                pos.avg_cost = Decimal(0)
            elif (pos.qty > 0) != (direction > 0):  # 穿越零点：剩余部分按成交价开新仓
                pos.avg_cost = price
        self.update_mark(symbol, float(price))
        return realized

    def apply_commission(self, amount: Decimal) -> None:
        self.cash -= amount
        self.commission += amount

    def update_mark(self, symbol: str, price: float) -> None:
        self.last_price[symbol] = price
        pos = self.positions.get(symbol)
        if pos is not None and pos.qty:
            pos.unrealized_pnl = (price - float(pos.avg_cost)) * pos.qty

    def market_value(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        if pos is None or not pos.qty:
            return 0.0
        return pos.qty * self.last_price.get(symbol, float(pos.avg_cost))

    def gross_exposure(self) -> float:
        return sum(abs(self.market_value(s)) for s in self.positions)

    def equity(self) -> float:
        return float(self.cash) + sum(self.market_value(s) for s in self.positions)

    def realized_pnl(self) -> Decimal:
        return sum((p.realized_pnl for p in self.positions.values()), Decimal(0))

    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values() if p.qty)
