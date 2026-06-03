"""
Bitfinex API Client Module - CCXT-Based Permanent Solution

This module provides:
  - BitfinexClient: Uses CCXT's built-in nonce handling (no more custom nonces)
  - PaperBitfinexClient: Simulated trading against virtual balance

Key Changes from Legacy Version:
- Uses ccxt.bitfinex with proper nonce management via CCXT
- CCXT handles nonces internally - no more "nonce: small" errors
- All authenticated calls go through CCXT unified API
- Margin trading uses CCXT's unified margin API
"""

import ccxt
import pandas as pd
import logging
import time
import random
from typing import Optional, Dict, List, Any
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
#  Real (live) client - CCXT-based with automatic nonce handling
# -----------------------------------------------------------------------------

class BitfinexClient:
    """CCXT-based Bitfinex REST client with automatic nonce handling.
    
    This client uses CCXT's bitfinex implementation which properly manages
    nonces internally. No more custom nonce files or "nonce: small" errors.

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

        # Caches
        self._cached_balance: dict = {}
        self._cached_balance_ts: float = 0.0
        self._cached_positions: List[dict] = []
        self._cached_positions_ts: float = 0.0

        self._log = logging.getLogger(f"{__name__}.BitfinexClient")
        
        # Determine exchange name - use "bitfinex" (CCXT handles v1/v2 internally)
        exchange_id = self.exchange_config.get("name", "bitfinex")
        # Normalize "bitfinex2" to "bitfinex" since CCXT 4.5 uses unified bitfinex
        if exchange_id == "bitfinex2":
            exchange_id = "bitfinex"
            
        self._log.info(
            "Initializing BitfinexClient (mode=%s, exchange=%s)",
            mode, exchange_id
        )

        # Rate limiting
        self._rate_limit = float(self.exchange_config.get("rate_limit", 1.0))
        self._last_call_ts = 0.0

        # ----- build CCXT exchange instance -----
        exchange_class = getattr(ccxt, exchange_id, ccxt.bitfinex)
        
        exchange_kwargs = {
            "rateLimit": self._rate_limit * 1000,  # ccxt expects ms
            "enableRateLimit": True,
            "options": {
                "defaultType": "margin",  # Default to margin trading
            },
        }

        # If live mode with real API keys
        if mode == "live":
            exchange_kwargs["apiKey"] = self.exchange_config.get("api_key", "")
            exchange_kwargs["secret"] = self.exchange_config.get("api_secret", "")
            self._log.info("Live mode — real API keys loaded")
            self._log.info("Nonce handling: CCXT automatic (no manual management)")
        else:
            self._log.info("Paper mode — no API keys needed")

        self._exchange = exchange_class(exchange_kwargs)

        # Capability check
        self._log.info("Exchange capabilities:")
        for cap in ("fetchOHLCV", "fetchTicker", "fetchOrderBook",
                     "createOrder", "cancelOrder", "fetchBalance",
                     "fetchOpenOrders", "fetchPositions", "createMarketBuyOrder",
                     "createMarketSellOrder", "fetchMyTrades"):
            has_cap = self._exchange.has.get(cap, False)
            status = "✓" if has_cap else "✗"
            self._log.info(f"  {status} {cap}")

        # Paper trading virtual state
        self._paper_balance: Dict[str, Dict[str, float]] = {}
        self._paper_orders: List[dict] = []
        self._paper_positions: Dict[str, dict] = {}
        self._init_paper_balance()

        self._log.info("BitfinexClient ready — using %s with CCXT nonce handling", exchange_id)

    # ── properties ──────────────────────────────────────────────────────

    @property
    def exchange_name(self) -> str:
        return self.EXCHANGE_NAME

    @property
    def has_websocket(self) -> bool:
        return self._exchange.has.get("ws", False)

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
                              price: Optional[float] = None,
                              symbol: str = "BTC/USDT") -> dict:
        """Simulate an order fill at latest ticker mid-price ± 0.1% spread."""
        ticker = self.fetch_ticker(symbol)
        mid_price = (ticker["bid"] + ticker["ask"]) / 2.0
        spread = 0.001  # 0.1 %

        fill_price = mid_price * (1.0 - spread) if side == "buy" else mid_price * (1.0 + spread)
        fill_price = price or fill_price
        cost = amount * fill_price
        quote = symbol.split("/")[1] if "/" in symbol else "USDT"

        if side == "buy":
            base_used = amount
            quote_cost = cost
        else:
            base_used = -amount
            quote_cost = -cost

        # Update paper balances
        base_cur = symbol.split("/")[0] if "/" in symbol else "BTC"
        if base_cur not in self._paper_balance:
            self._paper_balance[base_cur] = {"free": 0.0, "used": 0.0, "total": 0.0}

        self._paper_balance[base_cur]["free"] += base_used
        self._paper_balance[base_cur]["total"] += base_used
        self._paper_balance[quote]["free"] -= quote_cost
        self._paper_balance[quote]["total"] -= quote_cost

        order = {
            "id": f"paper_{int(time.time() * 1000)}_{random.randint(1000,9999)}",
            "symbol": symbol,
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
        if symbol not in self._paper_positions:
            self._paper_positions[symbol] = {
                "symbol": symbol,
                "contracts": 0.0,
                "entryPrice": 0.0,
                "unrealizedPnl": 0.0,
                "liquidationPrice": 0.0,
                "leverage": 1.0,
                "marginMode": "isolated",
                "side": "long",
            }
        pos = self._paper_positions[symbol]
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
            "Paper order filled: %s %s %.6f @ %.2f (cost=%.2f)",
            side.upper(), symbol, amount, fill_price, cost,
        )
        return order

    def _symbol_to_ccxt(self, symbol: str) -> str:
        """Convert internal symbol format to CCXT format.
        
        Examples:
            BTC/USDT -> BTC/USDT:USDT (margin)
            tBTCUST -> BTC/USDT:USDT
        """
        if symbol.startswith("t"):
            # Bitfinex format tBTCUST -> BTC/USDT:USDT
            base_quote = symbol[1:]
            if base_quote.endswith("UST"):
                return base_quote.replace("UST", "") + "/USDT:USDT"
            elif base_quote.endswith("USD"):
                return base_quote.replace("USD", "") + "/USD:USD"
            else:
                # Try to split into base/quote
                for quote in ["USD", "UST", "BTC", "ETH"]:
                    if base_quote.endswith(quote):
                        base = base_quote[:-len(quote)]
                        return f"{base}/{quote}:{quote}"
        return symbol

    def _symbol_to_bitfinex(self, symbol: str) -> str:
        """Convert CCXT symbol to Bitfinex t-format."""
        if symbol.startswith("t"):
            return symbol
        # BTC/USDT:USDT -> tBTCUST
        parts = symbol.replace(":USDT", "").replace(":USD", "").split("/")
        if len(parts) == 2:
            base, quote = parts
            quote = quote.replace("USDT", "UST").replace("USD", "USD")
            return f"t{base}{quote}"
        return symbol

    def _parse_bitfinex_order(self, raw_response, symbol: str, amount: float, price: Optional[float]) -> dict:
        """Parse Bitfinex v2 order submit response to standardized format.
        
        Bitfinex v2 returns a notification array: 
        [TYPE, MESSAGE, CODE, [ORDER_ARRAY]]
        or directly: [ID, GID, CID, SYMBOL, MTS_CREATE, MTS_UPDATE, AMOUNT, ...]
        """
        if not raw_response:
            return {"success": False, "error": "Empty response from Bitfinex", "symbol": symbol}
        
        # Check if it's a notification wrapper [TYPE, MESSAGE, CODE, DATA]
        if isinstance(raw_response, list) and len(raw_response) >= 4 and isinstance(raw_response[1], str):
            msg_type = raw_response[0] if len(raw_response) > 0 else None
            message = raw_response[1] if len(raw_response) > 1 else ""
            code = raw_response[2] if len(raw_response) > 2 else None
            data = raw_response[3] if len(raw_response) > 3 else None
            
            # If there's an error message, return it
            if msg_type != "SUCCESS" or not data:
                return {
                    "success": False, 
                    "error": message or "Order submission failed", 
                    "code": code,
                    "symbol": symbol
                }
            
            # Use the inner data array as the order response
            raw_response = data
        
        # Now parse the order array [ID, GID, CID, SYMBOL, MTS_CREATE, MTS_UPDATE, AMOUNT, ...]
        try:
            order_id = raw_response[0] if len(raw_response) > 0 else None
            amount_orig = float(raw_response[7]) if len(raw_response) > 7 else amount
            
            return {
                "id": str(order_id) if order_id else None,
                "symbol": symbol,
                "status": "open" if order_id else "failed",
                "amount": abs(amount_orig),
                "filled": abs(amount_orig),
                "price": price if price else 0.0,
                "side": "buy" if amount_orig > 0 else "sell",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "success": bool(order_id),
            }
        except Exception as exc:
            self._log.error(f"Failed to parse Bitfinex order response: {exc}, response: {raw_response}")
            return {
                "id": None,
                "symbol": symbol,
                "status": "failed",
                "amount": amount,
                "filled": 0,
                "price": price if price else 0.0,
                "error": str(exc),
                "success": False,
            }

    # ── public methods ───────────────────────────────────────────────────

    def fetch_ohlcv(self, symbol: str, timeframe: str = "1h",
                    limit: int = 200, since: Optional[int] = None) -> Optional[pd.DataFrame]:
        """Fetch OHLCV candles and return a DataFrame.

        Parameters
        ----------
        symbol : str
            Trading pair (e.g., "BTC/USDT" or "tBTCUST").
        timeframe : str
            Candle duration.
        limit : int
            Number of candles.
        since : int, optional
            Unix timestamp in milliseconds.

        Returns
        -------
        pd.DataFrame or None
            Columns: timestamp (ms int index), open, high, low, close, volume.
        """
        self._rate_limit_wait()
        
        # Normalize symbol for CCXT
        ccxt_symbol = self._symbol_to_ccxt(symbol)
        
        try:
            if since is not None:
                raw = self._exchange.fetch_ohlcv(
                    ccxt_symbol, timeframe=timeframe, since=since, limit=limit
                )
            else:
                raw = self._exchange.fetch_ohlcv(
                    ccxt_symbol, timeframe=timeframe, limit=limit
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
        ccxt_symbol = self._symbol_to_ccxt(symbol)
        try:
            ticker = self._exchange.fetch_ticker(ccxt_symbol)
        except ccxt.BaseError as exc:
            self._log.error("fetch_ticker(%s) failed: %s", symbol, exc)
            return {"bid": 0.0, "ask": 0.0, "last": 0.0, "symbol": symbol}
        return ticker

    def fetch_orderbook(self, symbol: str, limit: int = 25) -> dict:
        """Return order book with bids and asks."""
        self._rate_limit_wait()
        ccxt_symbol = self._symbol_to_ccxt(symbol)
        try:
            ob = self._exchange.fetch_order_book(ccxt_symbol, limit=limit)
            return ob
        except ccxt.BaseError as exc:
            self._log.error("fetch_orderbook(%s) failed: %s", symbol, exc)
            return {"bids": [], "asks": [], "symbol": symbol}

    @staticmethod
    def _normalize_order_result(order: Any, symbol: str) -> dict:
        """Normalize a raw CCXT order dict into the standard success contract.

        Always returns:
            {success, id, filled, average, status, symbol, error, raw}
        success=True only when the order was accepted (no exception).
        """
        try:
            order = order or {}
            order_id = order.get("id")
            filled = float(order.get("filled", 0) or 0)
            average = order.get("average")
            average = float(average) if average not in (None, "") else None
            status = order.get("status") or ("open" if order_id else "unknown")
            # An order is only "accepted" if the exchange returned an order id.
            # A response with no id (and no fill) means it was NOT placed — do
            # not report success, or callers will treat a phantom order as live.
            accepted = order_id is not None or filled > 0
            return {
                "success": accepted,
                "id": str(order_id) if order_id is not None else None,
                "filled": filled,
                "average": average,
                "status": status,
                "symbol": order.get("symbol", symbol) or symbol,
                "error": None if accepted else "order not accepted (no id/fill returned)",
                "raw": order,
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "success": True,
                "id": None,
                "filled": 0.0,
                "average": None,
                "status": "unknown",
                "symbol": symbol,
                "error": None,
                "raw": order,
                "parse_warning": str(exc),
            }

    @staticmethod
    def _error_order_result(error: str, symbol: str) -> dict:
        """Build a normalized failure result (order was NOT accepted)."""
        return {
            "success": False,
            "id": None,
            "filled": 0.0,
            "average": None,
            "status": "rejected",
            "symbol": symbol,
            "error": str(error),
            "raw": None,
        }

    @staticmethod
    def _parse_onreq(raw: Any) -> Optional[dict]:
        """Parse a Bitfinex ``on-req`` order-submit acknowledgement.

        CCXT 4.5.x cannot parse this notification shape and returns an
        all-None order, so we read it ourselves. Shape:
            [MTS, "on-req", null, null, [[ORDER...]], CODE, STATUS, TEXT]
        where ORDER = [ID, GID, CID, SYMBOL, MTS_C, MTS_U, AMOUNT, AMOUNT_ORIG,
                       TYPE, ..., flags(12), STATUS(13), ..., PRICE(16), PRICE_AVG(17), ...]
        Returns {id, symbol, amount_orig, status, price, error} or None.
        """
        try:
            if not isinstance(raw, list) or len(raw) < 7 or raw[1] != "on-req":
                return None
            status_text = raw[6]
            if status_text == "ERROR":
                return {"id": None, "error": str(raw[7]) if len(raw) > 7 else "order rejected"}
            payload = raw[4]
            if not payload:
                return None
            o = payload[0] if isinstance(payload[0], list) else payload
            price = o[17] if len(o) > 17 and o[17] not in (None, 0) else (
                o[16] if len(o) > 16 and o[16] not in (None, 0) else None)
            return {
                "id": o[0],
                "symbol": o[3],
                "amount_orig": float(o[7]) if o[7] is not None else None,
                "status": o[13] if len(o) > 13 else None,
                "price": float(price) if price is not None else None,
                "error": None,
            }
        except Exception:  # noqa: BLE001 - defensive; recovery is best-effort
            return None

    def _recover_order(self, ccxt_order: dict, raw_json: Any) -> dict:
        """If CCXT returned an order with no id, recover it from the on-req ack.

        Bitfinex MARKET orders fill immediately, so an accepted ACTIVE order is
        treated as filled at the ack price (best-effort) unless a later status
        poll says otherwise. An on-req ERROR yields a rejected marker.
        """
        ccxt_order = dict(ccxt_order or {})
        if ccxt_order.get("id"):
            return ccxt_order
        info = self._parse_onreq(raw_json)
        if not info:
            return ccxt_order
        if info.get("error"):
            # Genuine rejection — leave id None and stash the reason.
            ccxt_order["_onreq_error"] = info["error"]
            return ccxt_order
        ccxt_order["id"] = info["id"]
        ccxt_order["symbol"] = ccxt_order.get("symbol") or info.get("symbol")
        if not ccxt_order.get("filled"):
            ccxt_order["filled"] = info.get("amount_orig") or 0.0
        if not ccxt_order.get("average"):
            ccxt_order["average"] = info.get("price")
        ccxt_order["status"] = ccxt_order.get("status") or info.get("status")
        return ccxt_order

    @staticmethod
    def _bitfinex_to_display(sym: str) -> str:
        """Convert a Bitfinex symbol to the unified display form.

        tBTCUST -> BTC/USDT ; tBTCF0:USTF0 -> BTC/USDT ; tETHUST -> ETH/USDT
        """
        if not sym:
            return sym
        s = sym[1:] if sym.startswith("t") else sym
        s = s.replace("F0:USTF0", "UST").replace("F0:USDF0", "USD")
        for q, disp in (("UST", "USDT"), ("USDT", "USDT"), ("USD", "USD")):
            if s.endswith(q):
                return f"{s[:-len(q)]}/{disp}"
        return sym

    def create_order(self, symbol: str, order_type: str, side: str,
                     amount: float, price: Optional[float] = None,
                     params: Optional[dict] = None) -> dict:
        """Place a limit or market order.

        Uses CCXT's unified API which handles authentication and nonces internally.

        Parameters
        ----------
        symbol : str
            Trading pair (e.g., "BTC/USDT").
        order_type : str
            "market" or "limit".
        side : str
            "buy" or "sell".
        amount : float
            Order amount in base currency.
        price : float, optional
            Price for limit orders.
        params : dict, optional
            Additional exchange-specific parameters. Pass
            ``{"reduceOnly": True}`` to close/reduce a position (CCXT maps this
            to the Bitfinex reduce/close flag so no fresh margin is required).

        Returns
        -------
        dict
            Normalized result:
            {success: bool, id: str|None, filled: float, average: float|None,
             status: str, symbol: str, error: str|None, raw: <ccxt order>}.
            ``success`` is True only when the order was accepted (no exception).
        """
        params = params or {}

        if self.mode == "paper":
            order = self._simulate_order_fill(side, amount, price, symbol)
            return self._normalize_order_result(order, symbol)

        self._rate_limit_wait()

        # Submit directly via Bitfinex's raw /auth/w/order/submit endpoint and
        # parse the on-req acknowledgement ourselves. CCXT 4.5.x cannot parse
        # this notification shape — it returns an all-None order AND clears
        # last_json_response — so its return value cannot tell us whether the
        # order was accepted. A false "failed" makes the caller retry and STACK
        # positions (the 2026-06-03 failure mode). The raw call returns the
        # on-req array directly, which carries the real order id.
        bfx_symbol = self._symbol_to_bitfinex(symbol)  # e.g. BTC/USDT -> tBTCUST
        signed = amount if side.lower() == "buy" else -amount
        payload = {
            "symbol": bfx_symbol,
            "amount": f"{signed:.8f}",
            "type": "MARKET" if order_type.lower() == "market" else "LIMIT",
        }
        if order_type.lower() != "market" and price is not None:
            payload["price"] = f"{price}"
        if params.get("reduceOnly") or params.get("reduce_only"):
            payload["flags"] = 1024  # Bitfinex reduce-only flag

        try:
            self._log.info("Submitting order (raw on-req): %s", payload)
            resp = self._exchange.private_post_auth_w_order_submit(payload)
            info = self._parse_onreq(resp)

            if info is None:
                # Could not parse the ack — treat as unknown, NOT success, and
                # do NOT let the caller retry blindly (it may have executed).
                self._log.error("Unparseable order ack: %s", str(resp)[:300])
                return self._error_order_result(
                    "order submitted but ack unparseable — verify via positions before retry",
                    symbol,
                )
            if info.get("error"):
                self._log.error("Order rejected (on-req): %s", info["error"])
                return self._error_order_result(info["error"], symbol)

            # Accepted: we have a real order id. Bitfinex market orders fill
            # immediately, so report the original amount filled at the ack/ticker
            # price (best-effort; the engine re-reads positions for exact state).
            avg = info.get("price")
            if order_type.lower() == "market" and not avg:
                try:
                    avg = float(self.fetch_ticker(symbol).get("last"))
                except Exception:  # noqa: BLE001
                    avg = price
            order = {
                "id": info["id"],
                "symbol": symbol,
                "filled": info.get("amount_orig") or amount,
                "average": avg,
                "status": "closed" if order_type.lower() == "market" else (info.get("status") or "open"),
            }
            self._log.info("Order accepted: id=%s status=%s filled=%s avg=%s",
                           order["id"], order["status"], order["filled"], order["average"])
            return self._normalize_order_result(order, symbol)

        except ccxt.BaseError as exc:
            self._log.error("create_order failed: %s", exc)
            return self._error_order_result(str(exc), symbol)

    def create_margin_order(self, symbol: str, side: str, amount: float,
                            order_type: str = "market",
                            reduce_only: bool = False) -> dict:
        """Create a margin trading order (long or short).

        For shorts, use side="sell" with negative amount or side="buy" to close.

        Parameters
        ----------
        symbol : str
            Trading pair.
        side : str
            "buy" (long) or "sell" (short).
        amount : float
            Positive amount.
        order_type : str
            "market" or "limit".
        reduce_only : bool
            When True, pass reduceOnly so the order closes/reduces an existing
            position instead of opening an opposing one.

        Returns
        -------
        dict
            Normalized order result (see ``create_order``).
        """
        # Bitfinex uses defaultType="margin" in exchange options.
        # reduceOnly is forwarded so closes never need fresh margin.
        params = {"reduceOnly": True} if reduce_only else None
        return self.create_order(symbol, order_type, side, amount, None, params=params)

    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> dict:
        """Cancel an open order.

        In paper mode this removes the order from the paper order list.
        """
        if self.mode == "paper":
            self._paper_orders = [o for o in self._paper_orders if o["id"] != order_id]
            self._log.info("Paper order %s cancelled", order_id)
            return {"id": order_id, "status": "canceled"}

        self._rate_limit_wait()
        try:
            # For bitfinex2, we need symbol
            if symbol:
                ccxt_symbol = self._symbol_to_ccxt(symbol)
                result = self._exchange.cancel_order(order_id, ccxt_symbol)
            else:
                result = self._exchange.cancel_order(order_id)
            self._log.info("Order %s cancelled", order_id)
            return result
        except ccxt.BaseError as exc:
            self._log.error("cancel_order(%s) failed: %s", order_id, exc)
            return {"error": str(exc), "id": order_id}

    def fetch_balance(self) -> dict:
        """Return balance dict with free / used / total per currency.

        Uses CCXT's unified fetchBalance which handles authentication internally.
        Also parses raw Bitfinex response to extract margin wallet balances.
        """
        if self.mode == "paper":
            return self._paper_balance.copy()

        # Cache balance for 10s
        if time.time() - self._cached_balance_ts < 10.0 and self._cached_balance:
            return self._cached_balance

        self._rate_limit_wait()
        try:
            # CCXT handles nonces internally
            balance = self._exchange.fetch_balance()
            
            # Extract margin balance from raw info (CCXT doesn't parse margin correctly)
            result = {"free": {}, "used": {}, "total": {}}
            
            # Parse raw info for margin wallet
            # Format: [wallet_type, currency, balance, unsettled_interest, available, ...]
            if "info" in balance and isinstance(balance["info"], list):
                for entry in balance["info"]:
                    if isinstance(entry, list) and len(entry) >= 5:
                        wallet_type = entry[0]  # 'exchange', 'margin', 'funding'
                        currency = entry[1]
                        total_amt = float(entry[2]) if entry[2] else 0.0
                        available = float(entry[4]) if entry[4] else 0.0
                        
                        if wallet_type == "margin" and total_amt > 0:
                            result["total"][currency] = total_amt
                            result["free"][currency] = available
                            result["used"][currency] = total_amt - available
            
            # If no margin balance found, fall back to CCXT parsed data
            if not result["total"]:
                for currency in balance.keys():
                    if currency not in ['info', 'free', 'used', 'total']:
                        data = balance[currency]
                        if isinstance(data, dict) and data.get('total', 0) > 0:
                            result["total"][currency] = data['total']
                            result["free"][currency] = data.get('free', 0)
                            result["used"][currency] = data.get('used', 0)
            
            # Alias UST -> USDT (Bitfinex uses UST for Tether)
            if "UST" in result.get("total", {}):
                result["total"]["USDT"] = result["total"]["UST"]
                result["free"]["USDT"] = result["free"].get("UST", 0)
                result["used"]["USDT"] = result["used"].get("UST", 0)
            
            self._cached_balance = result
            self._cached_balance_ts = time.time()
            return result
            
        except ccxt.BaseError as exc:
            self._log.error("fetch_balance failed: %s", exc)
            return {}

    def fetch_open_orders(self, symbol: Optional[str] = None) -> list:
        """Return list of open orders."""
        if self.mode == "paper":
            return [o for o in self._paper_orders if o.get("status") == "open"]

        self._rate_limit_wait()
        try:
            if symbol:
                ccxt_symbol = self._symbol_to_ccxt(symbol)
                return self._exchange.fetch_open_orders(ccxt_symbol)
            else:
                return self._exchange.fetch_open_orders()
        except ccxt.BaseError as exc:
            self._log.error("fetch_open_orders failed: %s", exc)
            return []

    def fetch_position(self, symbol: str) -> dict:
        """Return position info for a specific symbol.
        
        Returns empty position dict if no position exists.
        """
        if self.mode == "paper":
            return self._paper_positions.get(symbol, {
                "symbol": symbol,
                "contracts": 0.0,
                "entryPrice": 0.0,
                "unrealizedPnl": 0.0,
            })
        
        # Check cache first
        if time.time() - self._cached_positions_ts < 5.0 and self._cached_positions:
            for pos in self._cached_positions:
                if pos.get("symbol") == symbol:
                    return pos
            return {"symbol": symbol, "contracts": 0.0, "entryPrice": 0.0, "unrealizedPnl": 0.0}
        
        # Fetch all positions
        positions = self.fetch_positions()
        for pos in positions:
            if pos.get("symbol") == symbol:
                return pos
        
        return {"symbol": symbol, "contracts": 0.0, "entryPrice": 0.0, "unrealizedPnl": 0.0}

    def fetch_positions(self, symbols: Optional[List[str]] = None) -> list:
        """Return list of ALL open positions.
        
        Parameters
        ----------
        symbols : list, optional
            List of symbols to filter by.
            
        Returns
        -------
        list
            List of position dicts with keys:
            - symbol: str
            - contracts: float
            - entryPrice: float
            - unrealizedPnl: float
            - side: str ('long' or 'short')
            - amount: float (signed)
        """
        if self.mode == "paper":
            return [
                {
                    "symbol": s,
                    "contracts": p["contracts"],
                    "entryPrice": p["entryPrice"],
                    "unrealizedPnl": p.get("unrealizedPnl", 0.0),
                    "side": p.get("side", "long"),
                    "amount": p["contracts"] if p.get("side") == "long" else -p["contracts"],
                }
                for s, p in self._paper_positions.items()
                if p.get("contracts", 0) != 0
            ]
        
        # Cache for 5 seconds
        if time.time() - self._cached_positions_ts < 5.0 and self._cached_positions:
            if symbols:
                return [p for p in self._cached_positions if p.get("symbol") in symbols]
            return self._cached_positions
        
        self._rate_limit_wait()

        # Read positions from the raw /auth/r/positions endpoint. CCXT's
        # fetch_positions only returns DERIVATIVE (F0) positions and silently
        # omits spot-MARGIN positions (e.g. tBTCUST) — which left real positions
        # invisible to the close path. The raw endpoint returns BOTH.
        positions = self._fetch_positions_raw()
        if positions is None:
            # Fall back to CCXT if the raw call failed.
            positions = self._fetch_positions_ccxt(symbols)

        self._cached_positions = positions
        self._cached_positions_ts = time.time()

        if symbols:
            return [p for p in positions if p.get("symbol") in symbols]
        return positions

    def _fetch_positions_raw(self) -> Optional[list]:
        """Fetch ALL positions (margin + derivative) via /auth/r/positions.

        Position array: [SYMBOL, STATUS, AMOUNT(signed), BASE_PRICE,
        MARGIN_FUNDING, MARGIN_FUNDING_TYPE, PL, PL_PERC, PRICE_LIQ, LEVERAGE, ...]
        Returns None on failure so the caller can fall back to CCXT.
        """
        try:
            raw = self._exchange.private_post_auth_r_positions()
        except Exception as exc:  # noqa: BLE001
            self._log.error("fetch_positions (raw) failed: %s", exc)
            return None

        positions = []
        for p in raw or []:
            try:
                if len(p) < 4 or p[1] != "ACTIVE":
                    continue
                amount = float(p[2])
                if amount == 0:
                    continue
                positions.append({
                    "symbol": self._bitfinex_to_display(p[0]),
                    "contracts": abs(amount),
                    "entryPrice": float(p[3]) if p[3] is not None else 0.0,
                    "unrealizedPnl": float(p[6]) if len(p) > 6 and p[6] is not None else 0.0,
                    "side": "long" if amount > 0 else "short",
                    "amount": amount,
                    "leverage": float(p[9]) if len(p) > 9 and p[9] is not None else 1.0,
                    "marginMode": "margin",
                    "raw_symbol": p[0],
                })
            except (TypeError, ValueError, IndexError) as exc:
                self._log.warning("skipping unparseable position %r: %s", p, exc)
        return positions

    def _fetch_positions_ccxt(self, symbols: Optional[List[str]] = None) -> list:
        """Fallback: CCXT fetch_positions (derivatives only)."""
        positions = []
        try:
            for pos in self._exchange.fetch_positions(symbols):
                contracts = float(pos.get("contracts", 0) or pos.get("amount", 0))
                if contracts == 0:
                    continue
                side = pos.get("side", "long")
                positions.append({
                    "symbol": self._bitfinex_to_display(pos.get("symbol", "")) or pos.get("symbol", ""),
                    "contracts": contracts,
                    "entryPrice": float(pos.get("entryPrice", 0) or pos.get("entry_price", 0)),
                    "unrealizedPnl": float(pos.get("unrealizedPnl", 0) or pos.get("unrealized_profit", 0)),
                    "side": side,
                    "amount": contracts if side == "long" else -contracts,
                    "leverage": float(pos.get("leverage", 1)),
                    "marginMode": pos.get("marginMode", "isolated"),
                })
        except ccxt.BaseError as exc:
            self._log.error("fetch_positions (ccxt) failed: %s", exc)
        return positions

    def close_position(self, symbol: str) -> dict:
        """Close an open position with a reduceOnly market order.

        Parameters
        ----------
        symbol : str
            Symbol of position to close.

        Returns
        -------
        dict
            Normalized order result (see ``create_order``). On no-op returns a
            success result with id=None.
        """
        if self.mode == "paper":
            pos = self._paper_positions.get(symbol)
            if pos and pos.get("contracts", 0) > 0:
                side = "sell" if pos.get("side") == "long" else "buy"
                order = self._simulate_order_fill(side, pos["contracts"], None, symbol)
                return self._normalize_order_result(order, symbol)
            result = self._normalize_order_result({}, symbol)
            result["status"] = "no_position"
            result["success"] = True  # benign no-op: nothing to close is not a failure
            result["error"] = None
            return result

        # Get current position
        position = self.fetch_position(symbol)
        contracts = position.get("contracts", 0)

        if contracts == 0:
            result = self._normalize_order_result({}, symbol)
            result["status"] = "no_position"
            result["success"] = True  # benign no-op: nothing to close is not a failure
            result["error"] = None
            return result

        # Determine close side (opposite of current position)
        side = "sell" if position.get("side") == "long" else "buy"

        self._log.info(f"Closing {position.get('side')} position: {contracts} {symbol}")

        # reduceOnly market order so the close never needs fresh margin.
        return self.create_order(
            symbol, "market", side, contracts, params={"reduceOnly": True}
        )

    def fetch_my_trades(self, symbol: Optional[str] = None, since: Optional[int] = None,
                        limit: Optional[int] = None) -> list:
        """Fetch user's trade history."""
        if self.mode == "paper":
            return self._paper_orders
        
        self._rate_limit_wait()
        try:
            if symbol:
                ccxt_symbol = self._symbol_to_ccxt(symbol)
                return self._exchange.fetch_my_trades(ccxt_symbol, since, limit)
            else:
                return self._exchange.fetch_my_trades(None, since, limit)
        except ccxt.BaseError as exc:
            self._log.error("fetch_my_trades failed: %s", exc)
            return []

    def get_ws_url(self) -> str:
        """Return the Bitfinex WebSocket URL."""
        return "wss://api-pub.bitfinex.com/ws/2"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} mode={self.mode}>"


# -----------------------------------------------------------------------------
#  Paper-trading client (convenience alias)
# -----------------------------------------------------------------------------

class PaperBitfinexClient(BitfinexClient):
    """Bitfinex client forced into paper mode with full simulation support."""

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
        """Fetch candles and run stop-loss / take-profit checks."""
        df = super().fetch_ohlcv(symbol, timeframe, limit, since=since)
        if df is not None:
            self._check_sl_tp(symbol, df)
        return df

    def _check_sl_tp(self, symbol: str, df: pd.DataFrame):
        """Check stop-loss and take-profit triggers."""
        pos = self._paper_positions.get(symbol)
        if pos is None or pos.get("contracts", 0.0) <= 0:
            return

        latest_close = df["close"].iloc[-1]
        entry = pos.get("entryPrice", latest_close)
        if entry <= 0:
            return

        pnl_pct = (latest_close - entry) / entry * 100.0

        if pnl_pct <= -self._stop_loss_pct:
            self._log.warning(
                "🔴 Paper SL triggered: %.4f%% — closing position",
                pnl_pct,
            )
            self._close_paper_position(symbol, latest_close, "stop_loss")
        elif pnl_pct >= self._take_profit_pct:
            self._log.info(
                "🟢 Paper TP triggered: %.4f%% — closing position",
                pnl_pct,
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

        base_cur = symbol.split("/")[0] if "/" in symbol else "BTC"
        quote_cur = symbol.split("/")[1] if "/" in symbol else "USDT"

        if base_cur in self._paper_balance:
            self._paper_balance[base_cur]["free"] -= contracts
            self._paper_balance[base_cur]["total"] -= contracts
        self._paper_balance[quote_cur]["free"] += close_price * contracts
        self._paper_balance[quote_cur]["total"] += close_price * contracts

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


# -----------------------------------------------------------------------------
#  Factory helper
# -----------------------------------------------------------------------------

def create_bitfinex_client(config: dict,
                           mode: str = "paper") -> BitfinexClient:
    """Factory that returns the appropriate client for *mode*."""
    if mode == "paper":
        return PaperBitfinexClient(config)
    return BitfinexClient(config, mode=mode)
