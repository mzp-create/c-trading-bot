import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.client import BitfinexClient
from bitfinex.models import Order, Position, Ticker


def _cfg(mode_capital=1000.0):
    return {"trading": {"initial_capital": mode_capital},
            "exchange": {"api_key": "", "api_secret": ""}}


def test_paper_client_orders_and_positions():
    c = BitfinexClient(_cfg(), mode="paper", instance="long")
    c._ticker_source = types.SimpleNamespace(
        get_ticker=lambda s: Ticker(symbol=s, bid=100.0, ask=100.0, last=100.0))
    o = c.create_order("BTC/USDT", "buy", 0.5, order_type="market", price=100.0)
    assert isinstance(o, Order) and o.is_filled
    assert isinstance(c.fetch_positions()[0], Position)
    closed = c.close_position("BTC/USDT")
    assert closed.reduce_only is True
    assert c.fetch_positions() == []


def test_paper_client_ticker_uses_public_fetcher():
    c = BitfinexClient(_cfg(), mode="paper", instance="long")
    # Inject a fake public ticker source through the ticker seam.
    c._ticker_source = types.SimpleNamespace(
        get_ticker=lambda s: Ticker(symbol=s, bid=99.0, ask=101.0, last=100.0))
    t = c.fetch_ticker("BTC/USDT")
    assert t.last == 100.0
