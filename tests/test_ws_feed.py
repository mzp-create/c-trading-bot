import sys
import types
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.ws_feed import WsFeed
from state.market_state import MarketState
from state.account_state import AccountState


def _bfx_ticker():
    return types.SimpleNamespace(bid=99.0, ask=101.0, last_price=100.0)


def _bfx_position(symbol="tBTCUST", amount=-0.3, status="ACTIVE"):
    return types.SimpleNamespace(symbol=symbol, status=status, amount=amount,
                                 base_price=100.0, pl=1.0, leverage=2.0)


def _bfx_wallet():
    return types.SimpleNamespace(wallet_type="margin", currency="UST",
                                 balance=538.0, available_balance=500.0)


def _bfx_trade():
    return types.SimpleNamespace(id=7, symbol="tBTCUST", order_id=111,
                                 exec_amount=0.5, exec_price=110.0, fee=-0.1,
                                 fee_currency="UST", mts_create=1_700_000_000_000)


def _feed():
    ms, acc = MarketState(), AccountState()
    rest = types.SimpleNamespace(get_positions=lambda: [], get_wallets=lambda: [])
    return WsFeed("k", "s", ["BTC/USDT"], ms, acc, rest), ms, acc


def test_ticker_handler_updates_market():
    feed, ms, _ = _feed()
    feed._on_ticker({"symbol": "tBTCUST"}, _bfx_ticker())
    t = ms.get_ticker("BTC/USDT")
    assert (t.bid, t.ask, t.last) == (99.0, 101.0, 100.0)


def test_position_handlers_update_account():
    feed, _, acc = _feed()
    feed._on_position_snapshot([_bfx_position()])
    assert acc.get_position("BTC/USDT").amount == -0.3
    feed._on_position(_bfx_position(amount=0.8))
    assert acc.get_position("BTC/USDT").amount == 0.8
    feed._on_position_close(_bfx_position())
    assert acc.get_position("BTC/USDT") is None


def test_wallet_and_fill_handlers():
    feed, _, acc = _feed()
    feed._on_wallet_snapshot([_bfx_wallet()])
    assert acc.get_wallets()[0].currency == "USDT"        # normalized UST->USDT
    feed._on_fill(_bfx_trade())
    f = acc.last_fill("BTC/USDT")
    assert f.fee == -0.1 and f.fee_currency == "USDT" and f.order_id == 111


def test_status_and_health():
    feed, _, acc = _feed()
    assert feed.is_healthy() is False
    feed._on_open()
    feed._on_authenticated({"userId": 1})
    assert acc.connected and acc.authenticated
    assert feed.is_healthy() is True
    feed._on_disconnected()
    assert feed.is_healthy() is False
