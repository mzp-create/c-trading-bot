import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.rest import BfxRest, REDUCE_ONLY
from bitfinex.errors import OrderRejected, AckUnparseable


def _order(**kw):
    base = dict(id=111, symbol="tBTCUST", amount=0.0, amount_orig=0.001,
                order_type="MARKET", order_status="EXECUTED @ 100.0",
                price=0.0, price_avg=100.0, flags=0, mts_create=1_700_000_000_000)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _notif(status, data, text=""):
    return types.SimpleNamespace(status=status, text=text, data=data,
                                 mts=1, type="on-req", message_id=None, code=None)


class _FakeAuth:
    def __init__(self):
        self.last_submit = None
    def submit_order(self, *, type, symbol, amount, price, flags=None, **k):
        self.last_submit = dict(type=type, symbol=symbol, amount=amount,
                                price=price, flags=flags)
        return _notif("SUCCESS", _order())
    def cancel_order(self, *, id):
        return _notif("SUCCESS", _order(id=id, order_status="CANCELED"))
    def get_positions(self):
        return [types.SimpleNamespace(symbol="tBTCUST", status="ACTIVE",
                amount=-0.3, base_price=100.0, pl=1.0, leverage=2.0,
                position_id=9, mts_update=1)]
    def get_wallets(self):
        return [types.SimpleNamespace(wallet_type="margin", currency="UST",
                balance=538.0, available_balance=500.0)]
    def get_trades_history(self, *, symbol=None, start=None, end=None,
                           limit=None, sort=None):
        return [types.SimpleNamespace(id=7, symbol="tBTCUST", order_id=111,
                exec_amount=0.001, exec_price=100.0, fee=-0.1, fee_currency="UST",
                mts_create=1_700_000_000_000)]


class _FakePublic:
    def get_t_ticker(self, symbol):
        return types.SimpleNamespace(bid=99.0, ask=101.0, last_price=100.0)


def _fake_client():
    return types.SimpleNamespace(
        rest=types.SimpleNamespace(auth=_FakeAuth(), public=_FakePublic()))


def _rest():
    return BfxRest(api_key="k", api_secret="s", client=_fake_client())


def test_submit_buy_market_signed_and_typed():
    r = _rest()
    order = r.submit_order("BTC/USDT", "buy", 0.001, order_type="market",
                           price=None, reduce_only=False)
    sub = r._client.rest.auth.last_submit
    assert sub["type"] == "MARKET"
    assert sub["symbol"] == "tBTCUST"
    assert float(sub["amount"]) == pytest.approx(0.001)
    assert sub["flags"] in (None, 0)
    assert order.id == 111
    assert order.symbol == "BTC/USDT"
    assert order.side == "buy"
    assert order.avg_price == 100.0
    assert order.is_filled is True


def test_submit_sell_is_negative_amount():
    r = _rest()
    r.submit_order("BTC/USDT", "sell", 0.002, order_type="market", price=None,
                   reduce_only=False)
    assert float(r._client.rest.auth.last_submit["amount"]) == pytest.approx(-0.002)


def test_reduce_only_sets_flag():
    r = _rest()
    r.submit_order("BTC/USDT", "sell", 0.001, order_type="market", price=None,
                   reduce_only=True)
    assert r._client.rest.auth.last_submit["flags"] == REDUCE_ONLY


def test_rejected_notification_raises_order_rejected():
    fc = _fake_client()
    fc.rest.auth.submit_order = lambda **k: _notif("ERROR", None, "balance too low")
    r = BfxRest(api_key="k", api_secret="s", client=fc)
    with pytest.raises(OrderRejected):
        r.submit_order("BTC/USDT", "buy", 1.0, order_type="market", price=None,
                       reduce_only=False)


def test_transport_raise_becomes_ack_unparseable():
    fc = _fake_client()
    def boom(**k):
        raise RuntimeError("connection reset")
    fc.rest.auth.submit_order = boom
    r = BfxRest(api_key="k", api_secret="s", client=fc)
    with pytest.raises(AckUnparseable):
        r.submit_order("BTC/USDT", "buy", 1.0, order_type="market", price=None,
                       reduce_only=False)


def test_get_positions_typed():
    pos = _rest().get_positions()
    assert len(pos) == 1
    assert pos[0].symbol == "BTC/USDT"
    assert pos[0].side == "short"
    assert pos[0].amount == -0.3
    assert pos[0].unrealized_pnl == 1.0


def test_get_wallets_and_ticker_typed():
    r = _rest()
    w = r.get_wallets()[0]
    assert (w.currency, w.wallet_type, w.balance) == ("UST", "margin", 538.0)
    t = r.get_ticker("BTC/USDT")
    assert (t.bid, t.ask, t.last) == (99.0, 101.0, 100.0)


def test_get_trades_typed_fill():
    f = _rest().get_trades("BTC/USDT")[0]
    assert f.symbol == "BTC/USDT"
    assert f.order_id == 111
    assert f.price == 100.0
    assert f.fee == -0.1
