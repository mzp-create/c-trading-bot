import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.paper import PaperBroker, SLIPPAGE, TAKER_FEE


def _broker():
    return PaperBroker(initial_capital=1000.0, quote="USDT",
                       base_symbols=["BTC/USDT"])


def test_market_buy_slippage_and_fee():
    b = _broker()
    o = b.submit_order("BTC/USDT", "buy", 0.01, order_type="market", price=100.0)
    assert o.avg_price == round(100.0 * (1 + SLIPPAGE), 8)
    assert abs(o.fee - (100.0 * (1 + SLIPPAGE) * 0.01 * TAKER_FEE)) < 1e-12
    assert o.is_filled
    pos = b.get_positions()
    assert len(pos) == 1 and pos[0].symbol == "BTC/USDT" and pos[0].amount == 0.01


def test_buy_decrements_quote_increments_base():
    b = _broker()
    b.submit_order("BTC/USDT", "buy", 0.01, order_type="market", price=100.0)
    w = {x.currency: x for x in b.get_wallets()}
    fill = 100.0 * (1 + SLIPPAGE)
    spent = fill * 0.01 + fill * 0.01 * TAKER_FEE
    assert abs(w["USDT"].available - (1000.0 - spent)) < 1e-9
    assert abs(w["BTC"].available - 0.01) < 1e-12


def test_reduce_only_closes_position():
    b = _broker()
    b.submit_order("BTC/USDT", "buy", 0.01, order_type="market", price=100.0)
    o = b.submit_order("BTC/USDT", "sell", 0.01, order_type="market",
                       price=110.0, reduce_only=True)
    assert o.is_filled and o.reduce_only
    assert b.get_positions() == []


def test_market_sell_slippage_down():
    b = _broker()
    b._wallets["BTC"]["available"] = 1.0
    b._wallets["BTC"]["balance"] = 1.0
    o = b.submit_order("BTC/USDT", "sell", 0.01, order_type="market", price=100.0)
    assert o.avg_price == round(100.0 * (1 - SLIPPAGE), 8)


def test_insufficient_quote_rejected():
    import pytest
    from bitfinex.errors import OrderRejected
    b = PaperBroker(initial_capital=0.5, quote="USDT", base_symbols=["BTC/USDT"])
    with pytest.raises(OrderRejected):
        b.submit_order("BTC/USDT", "buy", 0.01, order_type="market", price=100.0)
