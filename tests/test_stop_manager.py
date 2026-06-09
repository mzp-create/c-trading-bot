import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.stop_manager import StopOrderManager


class _Order:
    def __init__(self, oid, symbol="SOL/USDT", side="sell", reduce_only=True):
        self.id = oid
        self.symbol = symbol
        self.side = side
        self.reduce_only = reduce_only


class _Client:
    def __init__(self):
        self.created = []
        self.cancelled = []
        self._next = 500

    def create_order(self, symbol, side, amount, *, order_type, price,
                     reduce_only):
        self.created.append(dict(symbol=symbol, side=side, amount=amount,
                                 order_type=order_type, price=price,
                                 reduce_only=reduce_only))
        self._next += 1
        return _Order(self._next, symbol=symbol, side=side,
                      reduce_only=reduce_only)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def fetch_open_orders(self):
        return []


def test_place_for_long_submits_reduce_only_sell_stop():
    client = _Client()
    mgr = StopOrderManager(client)
    oid = mgr.place("SOL/USDT", "buy", 1.5, 60.0)
    assert oid == 501
    c = client.created[-1]
    assert c["side"] == "sell"            # opposite of a long
    assert c["order_type"] == "stop"
    assert c["price"] == 60.0
    assert c["reduce_only"] is True
    assert c["amount"] == 1.5


def test_place_for_short_submits_buy_stop():
    client = _Client()
    mgr = StopOrderManager(client)
    mgr.place("ETH/USDT", "sell", 0.1, 1800.0)
    assert client.created[-1]["side"] == "buy"


def test_cancel_cancels_tracked_id_and_forgets_it():
    client = _Client()
    mgr = StopOrderManager(client)
    mgr.place("SOL/USDT", "buy", 1.5, 60.0)
    mgr.cancel("SOL/USDT")
    assert client.cancelled == [501]
    mgr.cancel("SOL/USDT")               # second cancel is a no-op
    assert client.cancelled == [501]


def test_place_swallows_client_error_and_returns_none():
    class _Boom(_Client):
        def create_order(self, *a, **k):
            raise RuntimeError("rejected")
    mgr = StopOrderManager(_Boom())
    assert mgr.place("SOL/USDT", "buy", 1.5, 60.0) is None
