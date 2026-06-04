import sys, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
from bitfinex.rest import BfxRest
from bitfinex.errors import OrderRejected, AckUnparseable


def _notif(status, data, text=""):
    return types.SimpleNamespace(status=status, text=text, data=data)


def _client(submit):
    auth = types.SimpleNamespace(submit_order=submit)
    return types.SimpleNamespace(rest=types.SimpleNamespace(auth=auth, public=None))


def test_success_ack_yields_real_id():
    order = types.SimpleNamespace(id=555, symbol="tBTCUST", amount=0.0,
        amount_orig=0.001, order_type="MARKET", order_status="EXECUTED",
        price=0.0, price_avg=100.0)
    r = BfxRest("k", "s", client=_client(lambda **k: _notif("SUCCESS", order)))
    o = r.submit_order("BTC/USDT", "buy", 0.001, order_type="market", price=None,
                       reduce_only=False)
    assert o.id == 555 and o.is_filled


def test_error_ack_raises():
    r = BfxRest("k", "s", client=_client(lambda **k: _notif("ERROR", None, "no")))
    with pytest.raises(OrderRejected):
        r.submit_order("BTC/USDT", "buy", 1.0, order_type="market", price=None,
                       reduce_only=False)


def test_unknown_outcome_never_retries():
    def boom(**k):
        raise RuntimeError("reset")
    r = BfxRest("k", "s", client=_client(boom))
    with pytest.raises(AckUnparseable):  # caller must NOT auto-retry
        r.submit_order("BTC/USDT", "buy", 1.0, order_type="market", price=None,
                       reduce_only=False)
