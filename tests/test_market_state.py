import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitfinex.models import Ticker
from state.market_state import MarketState


def test_update_and_get():
    ms = MarketState()
    assert ms.get_ticker("BTC/USDT") is None
    ms.update_ticker(Ticker(symbol="BTC/USDT", bid=99.0, ask=101.0, last=100.0))
    t = ms.get_ticker("BTC/USDT")
    assert t.last == 100.0
    assert ms.age("BTC/USDT") is not None and ms.age("BTC/USDT") < 5.0
    assert ms.is_fresh("BTC/USDT", max_age=5.0) is True


def test_unknown_symbol_not_fresh():
    ms = MarketState()
    assert ms.age("ETH/USDT") is None
    assert ms.is_fresh("ETH/USDT", max_age=5.0) is False


def test_concurrent_updates_no_crash():
    ms = MarketState()
    def worker(n):
        for i in range(200):
            ms.update_ticker(Ticker(symbol="BTC/USDT", bid=i, ask=i, last=i))
            ms.get_ticker("BTC/USDT")
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert ms.get_ticker("BTC/USDT") is not None
