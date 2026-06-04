"""Public historical candles via ccxt (the only retained ccxt use).

Keyless ccxt instance — candles need no auth, so this never contends for the
bfxapi nonce. Display symbols in (BTC/USDT); ccxt handles its own form here.
"""

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


class OhlcvFetcher:
    def __init__(self, exchange=None):
        if exchange is None:
            import ccxt
            exchange = ccxt.bitfinex({"enableRateLimit": True})
        self._exchange = exchange

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200,
                    since: Optional[int] = None) -> Optional[pd.DataFrame]:
        try:
            rows = self._exchange.fetch_ohlcv(symbol, timeframe, since=since,
                                              limit=limit)
        except Exception as exc:
            log.error("fetch_ohlcv(%s) failed: %s", symbol, exc)
            return None
        if not rows:
            return None
        df = pd.DataFrame(
            rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.set_index("timestamp")
        return df
