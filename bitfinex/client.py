"""BitfinexClient — public typed API composing rest/paper/ohlcv + keyguard.

- Auth methods (create_order/close_position/cancel/positions/balance/trades)
  use bfxapi in live, PaperBroker in paper.
- Public data (ticker/ohlcv) always use real public sources (mode-agnostic).
- In live mode, WS feed is wired in via _enable_ws; all methods route
  WS-first with REST fallback.
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
                 instance: str = "default", enable_ws: bool = True):
        self.mode = mode
        self.instance = instance
        self._ohlcv = OhlcvFetcher()
        self._ticker_source = None
        self._market = None
        self._account = None
        self._feed = None
        self._ticker_staleness = 15.0

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
            # Account-level guard: refuse a 2nd live instance on the SAME
            # Bitfinex account — two keys on one account net each other's
            # positions per symbol, so long/short need separate sub-accounts.
            # Configurable (exchange.account_guard, default on); fail-open if
            # the account id can't be read.
            if ex.get("account_guard", True):
                account_id = self._auth.get_account_id()
                if account_id is not None:
                    keyguard.register_account(
                        KEY_REGISTRY_PATH, instance=instance,
                        account_id=account_id, pid=os.getpid())
                    atexit.register(keyguard.release_account, KEY_REGISTRY_PATH,
                                    account_id=account_id)
                else:
                    log.warning("Could not read Bitfinex account id — account "
                                "guard skipped for instance %r", instance)
            ws_cfg = ex.get("ws", {})
            if enable_ws and ws_cfg.get("enabled", True):
                self._start_ws(config, api_key, api_secret, ws_cfg)
                atexit.register(self.close)
        else:
            capital = float(config.get("trading", {}).get("initial_capital", 1000.0))
            names = [s.get("name") for s in
                     config.get("trading", {}).get("symbols", []) if s.get("name")]
            self._auth = PaperBroker(initial_capital=capital, base_symbols=names)
            self._ticker_source = _PublicTicker()

    def _start_ws(self, config, api_key, api_secret, ws_cfg):
        from state.market_state import MarketState
        from state.account_state import AccountState
        from bitfinex.ws_feed import WsFeed
        symbols_cfg = config.get("trading", {}).get("symbols", [])
        names = [s.get("name") for s in symbols_cfg if s.get("name")] or ["BTC/USDT"]
        market, account = MarketState(), AccountState()
        feed = WsFeed(
            api_key, api_secret, names, market, account, self._auth,
            ticker_staleness=float(ws_cfg.get("ticker_staleness_seconds", 15)),
            order_confirm_timeout=float(ws_cfg.get("order_confirm_timeout_seconds", 10)),
            reconcile_interval=float(ws_cfg.get("reconcile_interval_seconds", 90)),
            wss_host=ws_cfg.get("wss_host"))
        self._enable_ws(market=market, account=account, feed=feed,
                        rest=self._auth,
                        ticker_staleness=float(ws_cfg.get("ticker_staleness_seconds", 15)))
        feed.start()

    def _enable_ws(self, *, market, account, feed, rest, ticker_staleness):
        """Wire WS state/feed into the client (also the test seam)."""
        self._market = market
        self._account = account
        self._feed = feed
        self._auth = rest          # REST fallback for reads/orders
        self._ticker_source = rest  # ticker REST fallback == BfxRest (live)
        self._ticker_staleness = ticker_staleness

    def _ws_healthy(self) -> bool:
        return self._feed is not None and self._feed.is_healthy()

    # ── orders (auth) ────────────────────────────────────────────────────
    def create_order(self, symbol: str, side: str, amount: float, *,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False) -> Order:
        if self._ws_healthy():
            return self._feed.submit_order_sync(
                symbol, side, amount, order_type=order_type, price=price,
                reduce_only=reduce_only)
        return self._auth.submit_order(symbol, side, amount, order_type=order_type,
                                       price=price, reduce_only=reduce_only)

    def close_position(self, symbol: str) -> Order:
        pos = self.fetch_position(symbol)
        if pos is None:
            raise ValueError(f"no open position for {symbol}")
        close_side = "sell" if pos.side == "long" else "buy"
        price = self.fetch_ticker(symbol).last
        return self.create_order(symbol, close_side, pos.abs_amount,
                                 order_type="market", price=price,
                                 reduce_only=True)

    def cancel_order(self, order_id: int) -> Order:
        return self._auth.cancel_order(order_id)   # REST: rare path, reliable

    def fetch_open_orders(self) -> List[Order]:
        return self._auth.get_open_orders()

    # ── reads (auth) ─────────────────────────────────────────────────────
    def fetch_positions(self) -> List[Position]:
        if self._account is not None and self._account.authenticated:
            return self._account.get_positions()
        return self._auth.get_positions()

    def fetch_position(self, symbol: str) -> Optional[Position]:
        for p in self.fetch_positions():
            if p.symbol == symbol:
                return p
        return None

    def fetch_balance(self) -> List[Wallet]:
        if self._account is not None and self._account.authenticated:
            return self._account.get_wallets()
        return self._auth.get_wallets()

    def fetch_my_trades(self, symbol: Optional[str] = None, since=None,
                        limit=None) -> List[Fill]:
        return self._auth.get_trades(symbol, since, limit)

    # ── public data (mode-agnostic) ──────────────────────────────────────
    def fetch_ticker(self, symbol: str) -> Ticker:
        if (self._ws_healthy() and self._market is not None
                and self._market.is_fresh(symbol, self._ticker_staleness)):
            t = self._market.get_ticker(symbol)
            if t is not None:
                return t
        return self._ticker_source.get_ticker(symbol)

    def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200,
                  since: Optional[int] = None) -> Optional[pd.DataFrame]:
        return self._ohlcv.fetch_ohlcv(symbol, timeframe, limit, since)

    def last_fill(self, symbol: str) -> Optional[Fill]:
        if self._account is not None:
            return self._account.last_fill(symbol)
        return None

    def close(self) -> None:
        if self._feed is not None:
            self._feed.stop()


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
