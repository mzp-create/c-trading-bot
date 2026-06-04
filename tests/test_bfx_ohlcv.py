import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from bitfinex.ohlcv import OhlcvFetcher


class _FakeCcxt:
    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=200):
        # ccxt returns [ts, open, high, low, close, volume] rows
        assert symbol == "BTC/USDT"          # display symbol passed straight through
        return [[1_700_000_000_000, 100.0, 110.0, 90.0, 105.0, 12.0]]


def test_fetch_ohlcv_returns_dataframe():
    f = OhlcvFetcher(exchange=_FakeCcxt())
    df = f.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=1)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[-1] == 105.0


def test_fetch_ohlcv_error_returns_none():
    class _Boom:
        def fetch_ohlcv(self, *a, **k):
            raise RuntimeError("network")
    assert OhlcvFetcher(exchange=_Boom()).fetch_ohlcv("BTC/USDT") is None
