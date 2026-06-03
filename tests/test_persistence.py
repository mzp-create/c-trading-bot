import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence.models import (
    OrderRecord, FillRecord, PositionRecord,
    TradeRecord, EquitySnapshot, SignalRecord,
)


def test_order_record_defaults():
    o = OrderRecord(ts="2026-06-03T00:00:00+00:00", symbol="BTC/USDT",
                    side="buy", order_type="market", amount=0.001)
    assert o.price is None
    assert o.reduce_only is False
    assert o.status == "unknown"
    assert o.filled == 0.0


def test_trade_record_fields():
    t = TradeRecord(ts="2026-06-03T00:00:00+00:00", symbol="BTC/USDT",
                    side="buy", entry_price=100.0, close_price=101.0,
                    amount=0.5, pnl=0.5)
    assert t.fee == 0.0
    assert t.reason is None
    assert t.position_id is None
