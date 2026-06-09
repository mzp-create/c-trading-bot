import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from bitfinex.errors import OrderRejected
from bitfinex.paper import PaperBroker
from bitfinex.rest import BfxRest, REDUCE_ONLY


class _FakeOrder:
    def __init__(self, **kw):
        self.id = kw.get("id", 111)
        self.symbol = kw.get("symbol", "tSOLUST")
        self.order_status = kw.get("order_status", "ACTIVE")
        self.amount_orig = kw.get("amount_orig", -1.5)
        self.amount = kw.get("amount", -1.5)
        self.price_avg = kw.get("price_avg", None)


class _FakeNotif:
    def __init__(self, data):
        self.status = "SUCCESS"
        self.text = "ok"
        self.data = data


class _FakeAuth:
    def __init__(self):
        self.calls = []

    def submit_order(self, **kw):
        self.calls.append(kw)
        return _FakeNotif(_FakeOrder())


class _FakeClient:
    def __init__(self):
        self.rest = type("R", (), {"auth": _FakeAuth(), "public": None})()


def test_submit_order_maps_stop_to_STOP_with_trigger_and_reduce_only():
    client = _FakeClient()
    r = BfxRest("k", "s", client=client)
    order = r.submit_order("SOL/USDT", "sell", 1.5, order_type="stop",
                           price=33.14, reduce_only=True)
    call = client.rest.auth.calls[-1]
    assert call["type"] == "STOP"
    assert call["price"] == "33.14"        # trigger goes in price
    assert call["flags"] == REDUCE_ONLY
    assert call["amount"] == "-1.50000000"  # sell -> negative
    assert order.order_type == "stop"


def test_ws_send_submit_maps_stop_to_STOP(monkeypatch):
    import bitfinex.ws_feed as wf

    captured = {}

    class _Inputs:
        def submit_order(self, **kw):
            captured.update(kw)
            return None  # coroutine stand-in; not awaited in this test

    feed = object.__new__(wf.WsFeed)
    feed._bfx = type("B", (), {"wss": type("W", (), {"inputs": _Inputs()})()})()
    feed._loop = None

    monkeypatch.setattr(wf.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: None)

    feed._send_submit("SOL/USDT", "sell", 1.5, "stop", 33.14, True, cid=7)
    assert captured["type"] == "STOP"
    assert captured["price"] == "33.14"
    assert captured["flags"] == wf.REDUCE_ONLY
    assert captured["amount"] == "-1.50000000"
    assert captured["cid"] == 7


def test_paper_stop_rests_without_closing_position_and_cancels():
    broker = PaperBroker(initial_capital=1000.0)
    # open a long so there is a position to (not) touch
    broker.submit_order("SOL/USDT", "buy", 1.0, order_type="market", price=60.0)
    assert len(broker.get_positions()) == 1

    stop = broker.submit_order("SOL/USDT", "sell", 1.0, order_type="stop",
                               price=30.0, reduce_only=True)
    assert stop.order_type == "stop"
    assert stop.reduce_only is True
    assert stop.amount == 1.0
    assert stop.status == "ACTIVE"
    assert stop.id is not None
    # position is untouched by placing the stop
    assert len(broker.get_positions()) == 1
    # the resting stop is listed
    assert any(o.id == stop.id for o in broker.get_open_orders())

    broker.cancel_order(stop.id)
    assert all(o.id != stop.id for o in broker.get_open_orders())


def test_paper_stop_without_trigger_price_is_rejected():
    broker = PaperBroker(initial_capital=1000.0)
    with pytest.raises(OrderRejected):
        broker.submit_order("SOL/USDT", "sell", 1.0, order_type="stop")
