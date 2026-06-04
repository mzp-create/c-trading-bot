"""BitfinexClient — public typed API composing rest/paper/ohlcv + keyguard.

- Auth methods (create_order/close_position/cancel/positions/balance/trades)
  use bfxapi in live, PaperBroker in paper.
- Public data (ticker/ohlcv) always use real public sources (mode-agnostic).
"""

import atexit
import logging
import os
from typing import List, Optional

import pandas as pd

from bitfinex import symbols
from bitfinex.models import Order, Position, Ticker, Wallet, Fill
from bitfinex.ohlcv import OhlcvFetcher
from bitfinex.paper import PaperBroker

log = logging.getLogger(__name__)

KEY_REGISTRY_PATH = "data/.bfx_key_registry.json"


class BitfinexClient:
    def __init__(self, config: dict, mode: str = "paper",
                 instance: str = "default"):
        self.mode = mode
        self.instance = instance
        self._ohlcv = OhlcvFetcher()
        self._ticker_source = None  # set to rest (live) or a public source

        if mode == "live":
            from bitfinex.rest import BfxRest
            from bitfinex import keyguard
            ex = config.get("exchange", {})
            api_key = ex.get("api_key", "")
            api_secret = ex.get("api_secret", "")
            keyguard.register_key(KEY_REGISTRY_PATH, instance=instance,
                                  api_key=api_key, pid=os.getpid())
            atexit.register(keyguard.release_key, KEY_REGISTRY_PATH,
                            api_key=api_key)
            self._auth = BfxRest(api_key=api_key, api_secret=api_secret)
            self._ticker_source = self._auth
        else:
            capital = float(config.get("trading", {}).get("initial_capital", 1000.0))
            self._auth = PaperBroker(initial_capital=capital)
            # Paper still wants REAL public tickers; build a keyless rest public.
            self._ticker_source = _PublicTicker()

    # ── orders (auth) ────────────────────────────────────────────────────
    def create_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        return self._auth.submit_order(symbol, side, amount, order_type=order_type,
                                       price=price, reduce_only=reduce_only)

    def close_position(self, symbol: str) -> Order:
        pos = self.fetch_position(symbol)
        if pos is None:
            raise ValueError(f"no open position for {symbol}")
        close_side = "sell" if pos.side == "long" else "buy"
        price = self.fetch_ticker(symbol).last
        return self._auth.submit_order(symbol, close_side, pos.abs_amount,
                                       order_type="market", price=price,
                                       reduce_only=True)

    def cancel_order(self, order_id: int) -> Order:
        return self._auth.cancel_order(order_id)

    # ── reads (auth) ─────────────────────────────────────────────────────
    def fetch_positions(self) -> List[Position]:
        return self._auth.get_positions()

    def fetch_position(self, symbol: str) -> Optional[Position]:
        for p in self.fetch_positions():
            if p.symbol == symbol:
                return p
        return None

    def fetch_balance(self) -> List[Wallet]:
        return self._auth.get_wallets()

    def fetch_my_trades(self, symbol: Optional[str] = None, since=None,
                        limit=None) -> List[Fill]:
        return self._auth.get_trades(symbol, since, limit)

    # ── public data (mode-agnostic) ──────────────────────────────────────
    def fetch_ticker(self, symbol: str) -> Ticker:
        return self._ticker_source.get_ticker(symbol)

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200,
                  since: Optional[int] = None) -> Optional[pd.DataFrame]:
        return self._ohlcv.fetch_ohlcv(symbol, timeframe, limit, since)


class _PublicTicker:
    """Keyless public ticker via bfxapi public REST (used in paper mode)."""

    def __init__(self):
        self._pub = None

    def get_ticker(self, symbol: str) -> Ticker:
        if self._pub is None:
            from bfxapi import Client, REST_HOST
            self._pub = Client(rest_host=REST_HOST).rest.public
        t = self._pub.get_t_ticker(symbols.to_bitfinex(symbol))
        return Ticker(symbol=symbol, bid=float(t.bid), ask=float(t.ask),
                      last=float(t.last_price))
