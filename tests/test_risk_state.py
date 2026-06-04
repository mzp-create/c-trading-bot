import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from state.risk_state import RiskState


def test_set_get_clear():
    rs = RiskState()
    assert rs.get("BTC/USDT") is None
    rs.set("BTC/USDT", stop_loss=98.0, take_profit=104.0, trailing_stop=True,
           trailing_activation=2.0, trailing_distance=0.5, entry_price=100.0,
           side="buy")
    m = rs.get("BTC/USDT")
    assert m["stop_loss"] == 98.0 and m["take_profit"] == 104.0
    assert m["highest_price"] == 100.0 and m["lowest_price"] == float("inf")
    assert rs.symbols() == ["BTC/USDT"]
    rs.clear("BTC/USDT")
    assert rs.get("BTC/USDT") is None and rs.symbols() == []


def test_update_trailing():
    rs = RiskState()
    rs.set("BTC/USDT", stop_loss=98.0, take_profit=104.0, trailing_stop=True,
           trailing_activation=2.0, trailing_distance=0.5, entry_price=100.0,
           side="buy")
    rs.update_trailing("BTC/USDT", stop_loss=101.0, highest_price=103.0)
    m = rs.get("BTC/USDT")
    assert m["stop_loss"] == 101.0 and m["highest_price"] == 103.0


def test_update_trailing_missing_symbol_noop():
    rs = RiskState()
    rs.update_trailing("ETH/USDT", stop_loss=1.0)   # must not raise
    assert rs.get("ETH/USDT") is None


def test_get_returns_copy():
    rs = RiskState()
    rs.set("BTC/USDT", stop_loss=98.0, take_profit=104.0, trailing_stop=False,
           trailing_activation=2.0, trailing_distance=0.5, entry_price=100.0,
           side="sell")
    m = rs.get("BTC/USDT")
    m["stop_loss"] = 0.0                 # mutating the copy must not affect state
    assert rs.get("BTC/USDT")["stop_loss"] == 98.0
    # sell side seeds lowest_price=entry, highest_price=0.0
    assert rs.get("BTC/USDT")["lowest_price"] == 100.0
    assert rs.get("BTC/USDT")["highest_price"] == 0.0
