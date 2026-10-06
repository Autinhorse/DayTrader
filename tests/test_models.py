from decimal import Decimal
from typing import Any

import pytest

from trader.core.models import Bar, OrderIntent, OrderStatus, Reason
from trader.core.timeutil import NS_PER_MIN

REASON = Reason(code="test")


def intent(**kw):
    base: dict[str, Any] = dict(
        source="s1", symbol="AAPL", side="BUY", qty=10, order_type="MKT", reason=REASON
    )
    base.update(kw)
    return OrderIntent(**base)


def test_bar_ts_end():
    bar = Bar("AAPL", "1m", ts_start=0, open=1, high=2, low=0.5, close=1.5, volume=100)
    assert bar.ts_end == NS_PER_MIN


def test_reason_code_required():
    with pytest.raises(ValueError):
        Reason(code="  ")


@pytest.mark.parametrize(
    "kw",
    [
        dict(order_type="MKT"),
        dict(order_type="LMT", limit_price=Decimal("10.5")),
        dict(order_type="STP", stop_price=Decimal("9")),
        dict(order_type="STP_LMT", limit_price=Decimal("9.1"), stop_price=Decimal("9")),
    ],
)
def test_valid_intents(kw):
    intent(**kw)


@pytest.mark.parametrize(
    "kw",
    [
        dict(qty=0),
        dict(side="HOLD"),
        dict(tif="GTC"),
        dict(order_type="LMT"),  # 缺 limit_price
        dict(order_type="MKT", limit_price=Decimal("10")),  # 多了 limit_price
        dict(order_type="STP_LMT", limit_price=Decimal("9")),  # 缺 stop_price
        dict(order_type="LMT", limit_price=10.5),  # float 不是 Decimal
        dict(order_type="LMT", limit_price=Decimal("-1")),
    ],
)
def test_invalid_intents(kw):
    with pytest.raises(ValueError):
        intent(**kw)


def test_bracket():
    assert not intent().is_bracket
    assert intent(stop_loss=Decimal("9")).is_bracket


def test_terminal_statuses():
    terminal = {s for s in OrderStatus if s.is_terminal}
    assert terminal == {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
