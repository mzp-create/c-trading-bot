import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_errors_importable():
    from bitfinex.errors import (
        BitfinexError, OrderRejected, AckUnparseable, KeyConflictError,
    )
    assert issubclass(OrderRejected, BitfinexError)
    assert issubclass(AckUnparseable, BitfinexError)
    assert issubclass(KeyConflictError, BitfinexError)


from bitfinex.models import Order, Position, Ticker, Wallet, Fill


def test_order_status_helpers():
    filled = Order(id=1, symbol="BTC/USDT", side="buy", order_type="market",
                   amount=0.5, filled=0.5, avg_price=100.0, status="EXECUTED",
                   reduce_only=False, fee=0.1, fee_currency="USDT", raw=None)
    assert filled.is_filled is True
    assert filled.is_rejected is False

    rejected = Order(id=None, symbol="BTC/USDT", side="buy", order_type="market",
                     amount=0.5, filled=0.0, avg_price=None, status="REJECTED",
                     reduce_only=False, fee=0.0, fee_currency=None, raw=None)
    assert rejected.is_filled is False
    assert rejected.is_rejected is True


def test_position_abs_amount():
    short = Position(symbol="BTC/USDT", side="short", amount=-0.3,
                     entry_price=100.0, unrealized_pnl=1.0, leverage=2.0,
                     raw_symbol="tBTCUST")
    assert short.abs_amount == 0.3


def test_order_is_accepted():
    from bitfinex.models import Order
    # Market order with a real id but a non-EXECUTED transient status:
    active = Order(id=99, symbol="BTC/USDT", side="buy", order_type="market",
                   amount=0.001, filled=0.0, avg_price=None, status="ACTIVE",
                   reduce_only=False, fee=0.0, fee_currency=None, raw=None)
    assert active.is_accepted is True      # has id, not rejected
    assert active.is_filled is False       # but not fully executed

    partial = Order(id=99, symbol="BTC/USDT", side="buy", order_type="market",
                    amount=0.001, filled=0.0005, avg_price=100.0,
                    status="PARTIALLY FILLED @ 100.0", reduce_only=False,
                    fee=0.0, fee_currency=None, raw=None)
    assert partial.is_accepted is True

    rejected = Order(id=None, symbol="BTC/USDT", side="buy", order_type="market",
                     amount=0.001, filled=0.0, avg_price=None, status="REJECTED",
                     reduce_only=False, fee=0.0, fee_currency=None, raw=None)
    assert rejected.is_accepted is False    # no id / rejected
