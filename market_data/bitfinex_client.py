"""
Bitfinex API Client Module - Hermes Crypto Trading Bot.

Provides:
  - BitfinexClient: wraps ccxt.bitfinex / ccxt.bitfinex2 for REST + WS
  - PaperBitfinexClient: simulated trading against a virtual balance
"""

import ccxt
import pandas as pd
import logging
import time
import random
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Real (live) client
# ---------------------------------------------------------------------------

class BitfinexClient:
    """ccxt-based Bitfinex REST + WebSocket client.

    Parameters
    ----------
    config : dict
        Full bot config (expects ``config["exchange"]`` + optional
        ``config["trading"]`` sections).
    mode : str
        One of ``"paper"``, ``"live"``.  Default ``"paper"``.
    """

    EXCHANGE_NAME = "bitfinex"

    def __init__(self, config: dict, mode: str = "paper"):
        self.config = config
        self.mode = mode
        self.exchange_config = config.get("exchange", {})
        self.trading_config = config.get("trading", {})

        self._log = logging.getLogger(f"{__name__}.BitfinexClient")
        self._log.info(
            "Initializing BitfinexClient (mode=%s, exchange=%s)",
            mode,
            self.exchange_config.get("name", "bitfinex"),
        )

        # Rate limiting
        self._rate_limit = float(self.exchange_config.get("rate_limit", 1.0))
        self._last_call_ts = 0.0

        # ----- build exchange instance -----
        exchange_id = self.exchange_config.get("name", "bitfinex")
        exchange_class = getattr(ccxt, exchange_id, ccxt.bitfinex)
        exchange_kwargs = {
            "rateLimit": self._rate_limit * 1000,  # ccxt expects ms
            "enableRateLimit": True,
            "options": {
                "defaultType": self.exchange_config.get("default_type", "spot"),
            },
        }

        # If live mode with real API keys…
        if mode == "live":
            exchange_kwargs["apiKey"] = self.exchange_config.get("api_key", "")
            exchange_kwargs["secret"] = self.exchange_config.get("api_secret", "")
            self._log.info("Live mode — real API keys loaded")
        else:
            self._log.info("Paper mode — no API keys needed")

        self._exchange: ccxt.Exchange = exchange_class(exchange_kwargs)

        # Capability check (soft warning)
        for cap in ("fetchOHLCV", "fetchTicker", "fetchOrderBook",
                     "createOrder", "cancelOrder", "fetchBalance",
                     "fetchOpenOrders"):
            if not self._exchange.has.get(cap):
                self._log.warning("Exchange missing capability: %s", cap)

        # Paper‑trading virtual state (also used when mode=="paper")
        self._paper_balance: Dict[str, Dict[str, float]] = {}
        self._paper_orders: List[dict] = []
        self._paper_positions: Dict[str, dict] = {}
        self._init_paper_balance()

        self._log.info("BitfinexClient ready — using %s", exchange_id)

    # ── properties ──────────────────────────────────────────────────────

    @property
    def exchange_name(self) -> str:
        return self.EXCHANGE_NAME

    @property
    def has_websocket(self) -> bool:
        return self._exchange.has.get("hasWebSocket", False)

    # ── internal helpers ─────────────────────────────────────────────────

    def _rate_limit_wait(self):
        """Sleep if necessary to respect the configured rate limit."""
        elapsed = time.time() - self._last_call_ts
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)
        self._last_call_ts = time.time()

    def _init_paper_balance(self):
        """Seed paper balance from config."""
        initial_capital = float(self.trading_config.get("initial_capital", 100.0))
        quote = "USDT"
        self._paper_balance = {
            quote: {"free": initial_capital, "used": 0.0, "total": initial_capital},
        }
        self._paper_orders = []
        self._paper_positions = {}
        self._log.info(
            "Paper balance initialised: %s = %.2f", quote, initial_capital
        )

    def _simulate_order_fill(self, side: str, amount: float,
                              price: Optional[float] = None) -> dict:
        """Simulate an order fill at latest ticker mid‑price ± 0.1% spread."""
        ticker = self.fetch_ticker("BTC/USDT")
        mid_price = (ticker["bid"] + ticker["ask"]) / 2.0
        spread = 0.001  # 0.1 %

        fill_price = mid_price * (1.0 - spread) if side == "buy" else mid_price * (1.0 + spread)
        fill_price = price or fill_price
        cost = amount * fill_price
        quote = "USDT"

        if side == "buy":
            base_used = amount
            quote_cost = cost
        else:
            base_used = -amount
            quote_cost = -cost

        # Update paper balances
        base_cur = "BTC"
        if base_cur not in self._paper_balance:
            self._paper_balance[base_cur] = {"free": 0.0, "used": 0.0, "total": 0.0}

        self._paper_balance[base_cur]["free"] += base_used
        self._paper_balance[base_cur]["total"] += base_used
        self._paper_balance[quote]["free"] -= quote_cost
        self._paper_balance[quote]["total"] -= quote_cost

        order = {
            "id": f"paper_{int(time.time() * 1000)}_{random.randint(1000,9999)}",
            "symbol": "BTC/USDT",
            "side": side,
            "amount": amount,
            "price": fill_price,
            "cost": cost,
            "filled": amount,
            "status": "closed",
            "timestamp": int(time.time() * 1000),
            "datetime": datetime.now(timezone.utc).isoformat(),
        }
        self._paper_orders.append(order)

        # Track position for margin-style simulation
        if "BTC/USDT" not in self._paper_positions:
            self._paper_positions["BTC/USDT"] = {
                "symbol": "BTC/USDT",
                "contracts": 0.0,
                "entryPrice": 0.0,
                "unrealizedPnl": 0.0,
                "liquidationPrice": 0.0,
                "leverage": 1.0,
                "marginMode": "isolated",
                "side": "long",
            }
        pos = self._paper_positions["BTC/USDT"]
        if side == "buy":
            new_qty = pos["contracts"] + amount
            pos["entryPrice"] = (
                (pos["entryPrice"] * pos["contracts"] + fill_price * amount) / new_qty
                if new_qty > 0 else fill_price
            )
            pos["contracts"] = new_qty
            pos["side"] = "long"
        else:
            new_qty = pos["contracts"] - amount
            if new_qty <= 0:
                pos["contracts"] = 0.0
                pos["entryPrice"] = 0.0
            else:
                pos["contracts"] = new_qty
            pos["side"] = "long" if pos["contracts"] > 0 else "short"

        self._log.info(
            "Paper order filled: %s %.6f @ %.2f (cost=%.2f)",
            side.upper(), amount, fill_price, cost,
        )
        return order

    # ── public methods ───────────────────────────────────────────────────

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h",
                    limit: int = 200, since: Optional[int] = None) -> Optional[pd.DataFrame]:
        """Fetch OHLCV candles and return a DataFrame.

        Parameters
        ----------
        symbol : str
            Trading pair.
        timeframe : str
            Candle duration.
        limit : int
            Number of candles.
        since : int, optional
            Unix timestamp in milliseconds. When provided (and supported by
            the exchange), returns candles starting at or after this time.

        Returns
        -------
        pd.DataFrame or None
            Columns: timestamp (ms int index), open, high, low, close, volume.
        """
        self._rate_limit_wait()
        params = {}
        try:
            if since is not None:
                raw = self._exchange.fetch_ohlcv(
                    symbol, timeframe=timeframe, since=since, limit=limit
                )
            else:
                raw = self._exchange.fetch_ohlcv(
                    symbol, timeframe=timeframe, limit=limit
                )
        except ccxt.BaseError as exc:
            self._log.error("fetch_ohlcv(%s) failed: %s", symbol, exc)
            return None

        if not raw:
            self._log.warning("fetch_ohlcv(%s) returned empty data", symbol)
            return None

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = df["timestamp"].astype("int64")
        df.set_index("timestamp", inplace=True)
        return df

    def fetch_ticker(self, symbol: str) -> dict:
        """Return current ticker info (bid, ask, last, etc.)."""
        self._rate_limit_wait()
        try:
            ticker = self._exchange.fetch_ticker(symbol)
        except ccxt.BaseError as exc:
            self._log.error("fetch_ticker(%s) failed: %s", symbol, exc)
            return {"bid": 0.0, "ask": 0.0, "last": 0.0, "symbol": symbol}
        return ticker

    def fetch_orderbook(self, symbol: str, limit: int = 25) -> dict:
        """Return order book with bids and asks."""
        self._rate_limit_wait()
        try:
            ob = self._exchange.fetch_order_book(symbol, limit=limit)
            return ob
        except ccxt.BaseError as exc:
            self._log.error("fetch_orderbook(%s) failed: %s", symbol, exc)
            return {"bids": [], "asks": [], "symbol": symbol}

    def create_order(self, symbol: str, type: str, side: str,
                     amount: float, price: Optional[float] = None) -> dict:
        """Place a limit or market order.

        In paper mode the order is simulated against virtual balance.
        """
        if self.mode == "paper":
            return self._simulate_order_fill(side, amount, price)

        self._rate_limit_wait()
        try:
            order = self._exchange.create_order(symbol, type, side, amount, price)
            self._log.info("Order created: %s %s %.6f @ %s", side, symbol, amount, price or "market")
            return order
        except ccxt.BaseError as exc:
            self._log.error("create_order failed: %s", exc)
            return {"success": False, "error": str(exc)}

    def cancel_order(self, order_id: str):
        """Cancel an open order.

        In paper mode this removes the order from the paper order list.
        """
        if self.mode == "paper":
            self._paper_orders = [o for o in self._paper_orders if o["id"] != order_id]
            self._log.info("Paper order %s cancelled", order_id)
            return {"id": order_id, "status": "canceled"}

        self._rate_limit_wait()
        try:
            result = self._exchange.cancel_order(order_id)
            self._log.info("Order %s cancelled", order_id)
            return result
        except ccxt.BaseError as exc:
            self._log.error("cancel_order(%s) failed: %s", order_id, exc)
            return {"error": str(exc)}

    def fetch_balance(self) -> dict:
        """Return balance dict with free / used / total per currency.

        In paper mode returns the virtual balance.
        On margin, Bitfinex uses 'UST' instead of 'USDT' — we alias it.
        Also checks raw API 'info' for currencies ccxt doesn't parse.
        """
        if self.mode == "paper":
            bal = self._paper_balance.copy()
            return bal

        self._rate_limit_wait()
        try:
            bal = self._exchange.fetch_balance()
        except ccxt.BaseError as exc:
            self._log.error("fetch_balance failed: %s", exc)
            return {}

        # Ensure total/free/used dicts exist
        if "total" not in bal:
            bal["total"] = {}
        if "free" not in bal:
            bal["free"] = {}
        if "used" not in bal:
            bal["used"] = {}

        # Check raw info for wallets ccxt missed (e.g. margin UST on Bitfinex)
        info = bal.get("info", [])
        if isinstance(info, list):
            for wallet in info:
                if isinstance(wallet, list) and len(wallet) >= 5:
                    wtype = wallet[0]
                    currency = str(wallet[1])
                    total_str = wallet[2]
                    available_str = wallet[4]
                    try:
                        total_val = float(total_str)
                        avail_val = float(available_str)
                        if total_val > 0:
                            if currency not in bal["total"]:
                                bal["total"][currency] = total_val
                            if currency not in bal["free"]:
                                bal["free"][currency] = avail_val
                            if currency not in bal["used"]:
                                bal["used"][currency] = total_val - avail_val
                    except (ValueError, TypeError):
                        pass

        # Alias UST -> USDT so the bot works with BTC/USDT
        if "UST" in bal.get("total", {}) and "USDT" not in bal.get("total", {}):
            ust_total = bal["total"]["UST"]
            ust_free = bal["free"].get("UST", 0)
            ust_used = bal["used"].get("UST", 0)
            bal["total"]["USDT"] = ust_total
            bal["free"]["USDT"] = ust_free
            bal["used"]["USDT"] = ust_used

        return bal

    def fetch_open_orders(self, symbol: Optional[str] = None) -> list:
        """Return list of open orders.

        In paper mode this returns unfilled paper orders (all paper orders
        fill instantly, so the list is almost always empty).
        """
        if self.mode == "paper":
            return [o for o in self._paper_orders if o.get("status") == "open"]

        self._rate_limit_wait()
        try:
            return self._exchange.fetch_open_orders(symbol)
        except ccxt.BaseError as exc:
            self._log.error("fetch_open_orders failed: %s", exc)
            return []

    def fetch_position(self, symbol: str) -> dict:
        """Return position info for a symbol (margin)."""
        self._rate_limit_wait()
        try:
            positions = self._exchange.fetch_positions([symbol])
            if positions:
                return positions[0]
        except ccxt.BaseError as exc:
            self._log.error("fetch_position(%s) failed: %s", symbol, exc)

        # Fallback for paper
        return self._paper_positions.get(symbol, {
            "symbol": symbol,
            "contracts": 0.0,
            "entryPrice": 0.0,
            "unrealizedPnl": 0.0,
        })

    def get_ws_url(self) -> str:
        """Return the Bitfinex WebSocket URL."""
        return "wss://api-pub.bitfinex.com/ws/2"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} mode={self.mode}>"


# ---------------------------------------------------------------------------
#  Paper‑trading client (convenience alias)
# ---------------------------------------------------------------------------

class PaperBitfinexClient(BitfinexClient):
    """Bitfinex client forced into paper mode with full simulation support.

    All order methods simulate fills against virtual balance at current
    market price with a 0.1 % spread.  Stop‑loss and take‑profit checks
    are automatically applied on every ``fetch_ohlcv`` call.
    """

    def __init__(self, config: dict):
        super().__init__(config, mode="paper")
        self._log = logging.getLogger(f"{__name__}.PaperBitfinexClient")
        self._stop_loss_pct = float(
            config.get("risk", {}).get("stop_loss_pct", 2.0)
        )
        self._take_profit_pct = float(
            config.get("risk", {}).get("take_profit_pct", 4.0)
        )

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h",
                    limit: int = 200, since: Optional[int] = None) -> Optional[pd.DataFrame]:
        """Fetch candles and then run stop‑loss / take‑profit checks."""
        df = super().fetch_ohlcv(symbol, timeframe, limit, since=since)
        if df is not None:
            self._check_sl_tp(symbol, df)
        return df

    # ── stop‑loss / take‑profit logic ────────────────────────────────────

    def _check_sl_tp(self, symbol: str, df: pd.DataFrame):
        """Scan latest candle close against open position entry prices.

        If the current (most recent) close breaches a stop‑loss or
        take‑profit threshold the position is automatically closed.
        """
        pos = self._paper_positions.get(symbol)
        if pos is None or pos.get("contracts", 0.0) <= 0:
            return

        latest_close = df["close"].iloc[-1]
        entry = pos.get("entryPrice", latest_close)
        if entry <= 0:
            return

        pnl_pct = (latest_close - entry) / entry * 100.0

        # Stop‑loss check
        if pnl_pct <= -self._stop_loss_pct:
            self._log.warning(
                "🔴 Paper SL triggered: %.4f%% (threshold: %.1f%%) — closing position",
                pnl_pct, self._stop_loss_pct,
            )
            self._close_paper_position(symbol, latest_close, "stop_loss")

        # Take‑profit check
        elif pnl_pct >= self._take_profit_pct:
            self._log.info(
                "🟢 Paper TP triggered: %.4f%% (threshold: %.1f%%) — closing position",
                pnl_pct, self._take_profit_pct,
            )
            self._close_paper_position(symbol, latest_close, "take_profit")

    def _close_paper_position(self, symbol: str, close_price: float,
                               reason: str):
        """Close a paper position and update virtual balance."""
        pos = self._paper_positions.get(symbol)
        if pos is None or pos.get("contracts", 0.0) <= 0:
            return

        contracts = pos["contracts"]
        entry = pos["entryPrice"]
        pnl = (close_price - entry) * contracts

        base_cur = symbol.split("/")[0]  # e.g. "BTC"
        quote_cur = symbol.split("/")[1]  # e.g. "USDT"

        # Sell the base back into the market
        if base_cur in self._paper_balance:
            self._paper_balance[base_cur]["free"] -= contracts
            self._paper_balance[base_cur]["total"] -= contracts
        self._paper_balance[quote_cur]["free"] += close_price * contracts
        self._paper_balance[quote_cur]["total"] += close_price * contracts

        # Reset position
        pos["contracts"] = 0.0
        pos["entryPrice"] = 0.0

        self._log.info(
            "Paper position closed: reason=%s pnl=%.2f close=%.2f",
            reason, pnl, close_price,
        )

    def __repr__(self) -> str:
        return (
            f"<PaperBitfinexClient sl={self._stop_loss_pct}% "
            f"tp={self._take_profit_pct}%>"
        )


# ---------------------------------------------------------------------------
#  Factory helper
# ---------------------------------------------------------------------------

def create_bitfinex_client(config: dict,
                           mode: str = "paper") -> BitfinexClient:
    """Factory that returns the appropriate client for *mode*."""
    if mode == "paper":
        return PaperBitfinexClient(config)
    return BitfinexClient(config, mode=mode)
