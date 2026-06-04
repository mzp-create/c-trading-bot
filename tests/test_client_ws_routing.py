import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.client import BitfinexClient
from bitfinex.models import Order, Position, Ticker, Wallet, Fill


class _FakeFeed:
    def __init__(self, healthy=True):
        self._healthy = healthy
        self.submitted = None
    def is_healthy(self):
        return self._healthy
    def submit_order_sync(self, symbol, side, amount, *, order_type="market",
                          price=None, reduce_only=False):
        self.submitted = (symbol, side, amount, reduce_only)
        return Order(id=1, symbol=symbol, side=side, order_type=order_type,
                     amount=amount, filled=amount, avg_price=100.0,
                     status="EXECUTED", reduce_only=reduce_only, fee=0.0,
                     fee_currency=None, raw=None)
    def start(self): pass
    def stop(self): pass


def _live_client(healthy=True, fresh=True):
    c = BitfinexClient({"trading": {"initial_capital": 1000.0}},
                       mode="paper", instance="long")
    from state.market_state import MarketState
    from state.account_state import AccountState
    ms, acc = MarketState(), AccountState()
    if fresh:
        ms.update_ticker(Ticker(symbol="BTC/USDT", bid=99, ask=101, last=100.0))
    acc.set_status(connected=healthy, authenticated=healthy)
    if healthy:
        acc.apply_position_snapshot([Position(symbol="BTC/USDT", side="long",
            amount=0.5, entry_price=100.0, unrealized_pnl=1.0, leverage=2.0,
            raw_symbol="tBTCUST")])
        acc.apply_wallet_snapshot([Wallet(currency="USDT", wallet_type="margin",
            balance=538.0, available=500.0)])
    rest = types.SimpleNamespace(
        get_ticker=lambda s: Ticker(symbol=s, bid=1, ask=1, last=1.0),
        get_positions=lambda: [],
        get_wallets=lambda: [Wallet(currency="USDT", wallet_type="margin",
                                    balance=1.0, available=1.0)],
        submit_order=lambda *a, **k: Order(id=9, symbol="BTC/USDT", side="buy",
            order_type="market", amount=1.0, filled=1.0, avg_price=1.0,
            status="EXECUTED", reduce_only=k.get("reduce_only", False),
            fee=0.0, fee_currency=None, raw=None),
        get_trades=lambda *a, **k: [])
    feed = _FakeFeed(healthy=healthy)
    c._enable_ws(market=ms, account=acc, feed=feed, rest=rest,
                 ticker_staleness=15.0)
    return c, feed, rest


def test_fresh_ticker_from_state():
    c, _, _ = _live_client(fresh=True)
    assert c.fetch_ticker("BTC/USDT").last == 100.0     # from MarketState


def test_stale_ticker_falls_back_to_rest():
    c, _, _ = _live_client(healthy=True, fresh=False)
    assert c.fetch_ticker("BTC/USDT").last == 1.0       # from REST fallback


def test_positions_from_state_when_authenticated():
    c, _, _ = _live_client(healthy=True)
    assert c.fetch_positions()[0].symbol == "BTC/USDT"


def test_positions_fall_back_to_rest_when_unauthenticated():
    c, _, _ = _live_client(healthy=False)
    assert c.fetch_positions() == []                    # REST returns []


def test_order_routes_to_ws_when_healthy():
    c, feed, _ = _live_client(healthy=True)
    o = c.create_order("BTC/USDT", "buy", 0.001, price=100.0)
    assert feed.submitted == ("BTC/USDT", "buy", 0.001, False)
    assert o.id == 1


def test_order_routes_to_rest_when_unhealthy():
    c, feed, _ = _live_client(healthy=False)
    o = c.create_order("BTC/USDT", "buy", 0.001, price=100.0)
    assert feed.submitted is None and o.id == 9         # REST path
