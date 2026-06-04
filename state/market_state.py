"""Latest market prices, push-maintained by the WS feed (thread-safe)."""

import threading
import time
from typing import Optional

from bitfinex.models import Ticker


class MarketState:
    def __init__(self):
        self._lock = threading.Lock()
        self._tickers: dict[str, Ticker] = {}
        self._ts: dict[str, float] = {}     # display symbol -> monotonic ts

    def update_ticker(self, ticker: Ticker) -> None:
        with self._lock:
            self._tickers[ticker.symbol] = ticker
            self._ts[ticker.symbol] = time.monotonic()

    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        with self._lock:
            return self._tickers.get(symbol)

    def age(self, symbol: str) -> Optional[float]:
        """Seconds since the symbol's last update, or None if never seen."""
        with self._lock:
            ts = self._ts.get(symbol)
        return None if ts is None else (time.monotonic() - ts)

    def is_fresh(self, symbol: str, max_age: float) -> bool:
        age = self.age(symbol)
        return age is not None and age <= max_age
