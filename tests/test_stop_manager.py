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


def test_reconcile_adopts_existing_places_missing_cancels_orphans():
    client = _Client()
    # Exchange already has a reduce-only stop for SOL (adopt), an orphan stop
    # for XRP (no position -> cancel), and none for ETH (place).
    client.fetch_open_orders = lambda: [
        _Order(901, symbol="SOL/USDT", side="sell", reduce_only=True),
        _Order(902, symbol="XRP/USDT", side="sell", reduce_only=True),
    ]
    mgr = StopOrderManager(client)
    positions = [
        {"symbol": "SOL/USDT", "side": "buy", "amount": 1.5, "stop_loss": 60.0},
        {"symbol": "ETH/USDT", "side": "sell", "amount": 0.1, "stop_loss": 1800.0},
    ]
    mgr.reconcile(positions)

    assert mgr._ids["SOL/USDT"] == 901            # adopted
    assert "ETH/USDT" in mgr._ids                 # placed
    assert 902 in client.cancelled                # orphan cancelled
    assert [c["symbol"] for c in client.created] == ["ETH/USDT"]  # only ETH placed


def test_reconcile_skips_position_without_stop_loss():
    client = _Client()
    client.fetch_open_orders = lambda: []
    mgr = StopOrderManager(client)
    mgr.reconcile([{"symbol": "SOL/USDT", "side": "buy", "amount": 1.5}])
    assert client.created == []   # no stop_loss -> cannot place, skip
