import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from bitfinex.symbols import to_bitfinex, to_display


def test_to_bitfinex():
    assert to_bitfinex("BTC/USDT") == "tBTCUST"
    assert to_bitfinex("ETH/USD") == "tETHUSD"
    assert to_bitfinex("tBTCUST") == "tBTCUST"          # already bitfinex


def test_to_display():
    assert to_display("tBTCUST") == "BTC/USDT"
    assert to_display("tETHUSD") == "ETH/USD"
    assert to_display("BTC/USDT") == "BTC/USDT"          # already display


def test_to_display_tolerates_derivative_form():
    assert to_display("tBTCF0:USTF0") == "BTC/USDT"


def test_round_trip():
    for d in ("BTC/USDT", "ETH/USD"):
        assert to_display(to_bitfinex(d)) == d


def test_unknown_raises():
    with pytest.raises(ValueError):
        to_bitfinex("not-a-symbol")
    with pytest.raises(ValueError):
        to_display("garbage")
