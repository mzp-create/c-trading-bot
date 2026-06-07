"""Regression test for the 2026-06-07 nonce-storm incident.

MarketRegimeDetector.check_correlation fetched per-asset OHLCV by constructing a
brand-new MarketDataCollector (-> live BitfinexClient -> authenticated WsFeed +
reconcile loop) on every call and never closing it. Over hours this leaked
hundreds of authenticated WS connections onto the SAME Bitfinex API key, whose
uncoordinated wall-clock-microsecond nonces then collided -> `10114 nonce:
small`. Correlation lookback is public candle data and must use the keyless
OhlcvFetcher, never a live client.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import bitfinex.ohlcv as ohlcv_mod
import market_data.collector as collector_mod
import risk.regime_detector as rd


def test_correlation_fetch_never_builds_a_live_client(monkeypatch):
    built = {"collector": 0, "fetcher": 0}

    class _SpyCollector:  # building this in live mode is the leak we're guarding against
        def __init__(self, *a, **k):
            built["collector"] += 1

        def get_ohlcv(self, *a, **k):
            return pd.DataFrame({"close": range(40)})

    class _KeylessFetcher:
        def __init__(self, *a, **k):
            built["fetcher"] += 1

        def fetch_ohlcv(self, symbol, timeframe="1h", limit=200, since=None):
            return pd.DataFrame({"close": range(40)})

    monkeypatch.setattr(collector_mod, "MarketDataCollector", _SpyCollector)
    monkeypatch.setattr(ohlcv_mod, "OhlcvFetcher", _KeylessFetcher)

    det = rd.MarketRegimeDetector({"exchange": {"testnet": False}})
    df = det._get_recent_returns("BTC/USDT", lookback=30)

    assert df is not None, "correlation lookback should still return candles"
    assert built["collector"] == 0, (
        "LEAK: built a live MarketDataCollector for a public candle fetch"
    )
    assert built["fetcher"] >= 1, "should fetch via the keyless OhlcvFetcher"
