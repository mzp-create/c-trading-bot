import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.paper import PaperBroker
from bitfinex.models import Order, Position


def test_paper_open_then_position():
    pb = PaperBroker(initial_capital=1000.0)
    o = pb.submit_order("BTC/USDT", "buy", 0.5, order_type="market",
                        price=100.0, reduce_only=False)
    assert isinstance(o, Order)
    assert o.is_filled is True
    assert o.id is not None
    pos = pb.get_positions()
    assert len(pos) == 1
    assert pos[0].symbol == "BTC/USDT" and pos[0].side == "long"
    assert pos[0].amount == 0.5


def test_paper_reduce_only_close_removes_position():
    pb = PaperBroker(initial_capital=1000.0)
    pb.submit_order("BTC/USDT", "buy", 0.5, order_type="market", price=100.0,
                    reduce_only=False)
    o = pb.submit_order("BTC/USDT", "sell", 0.5, order_type="market",
                        price=110.0, reduce_only=True)
    assert o.is_filled is True
    assert pb.get_positions() == []


def test_paper_wallets():
    pb = PaperBroker(initial_capital=1000.0)
    w = pb.get_wallets()
    assert any(x.currency == "USDT" and x.balance == 1000.0 for x in w)
