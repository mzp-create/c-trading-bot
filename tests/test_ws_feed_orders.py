import sys
import types
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.ws_feed import WsFeed
from bitfinex.errors import OrderRejected, AckUnparseable
from state.market_state import MarketState
from state.account_state import AccountState


def _feed(**kw):
    rest = types.SimpleNamespace(
        get_positions=lambda: [], get_wallets=lambda: [])
    return WsFeed("k", "s", ["BTC/USDT"], MarketState(), AccountState(), rest,
                  order_confirm_timeout=kw.get("timeout", 1.0))


def _bfx_order(cid, status="EXECUTED @ 100.0"):
    return types.SimpleNamespace(id=555, cid=cid, symbol="tBTCUST",
                                 amount_orig=0.001, order_type="MARKET",
                                 order_status=status, price_avg=100.0)


def _notif(status, data, text=""):
    return types.SimpleNamespace(status=status, text=text, data=data)


def test_submit_resolves_on_success_notification():
    feed = _feed()
    def fake_send(symbol, side, amount, order_type, price, reduce_only, cid):
        # simulate the exchange replying asynchronously
        threading.Timer(0.02, lambda: feed._on_req_notification(
            _notif("SUCCESS", _bfx_order(cid)))).start()
    feed._send_submit = fake_send
    order = feed.submit_order_sync("BTC/USDT", "buy", 0.001, price=100.0)
    assert order.id == 555 and order.is_accepted and order.symbol == "BTC/USDT"


def test_submit_error_raises_order_rejected():
    feed = _feed()
    def fake_send(symbol, side, amount, order_type, price, reduce_only, cid):
        threading.Timer(0.02, lambda: feed._on_req_notification(
            _notif("ERROR", types.SimpleNamespace(cid=cid), "balance too low"))).start()
    feed._send_submit = fake_send
    with pytest.raises(OrderRejected):
        feed.submit_order_sync("BTC/USDT", "buy", 1.0, price=100.0)


def test_submit_timeout_raises_ack_unparseable():
    feed = _feed(timeout=0.2)
    feed._send_submit = lambda *a: None        # no reply ever
    with pytest.raises(AckUnparseable):
        feed.submit_order_sync("BTC/USDT", "buy", 1.0, price=100.0)
