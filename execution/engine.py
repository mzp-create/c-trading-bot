"""
Execution Engine — Hermes Crypto Trading Bot.

Manages order placement, position tracking, and balance management in
both **paper** and **live** modes.

Paper mode:
  - Tracks virtual balance
  - Simulates slippage (0.05% for market orders)
  - Applies exchange fees (0.1% taker, 0.0% maker per Bitfinex schedule)
  - Tracks PnL per position
  - Maintains simulated order book & trade history
  - Stores trade log in data/trades.csv

Live mode:
  - Uses BitfinexClient.create_order() for real execution
  - Fetches actual positions and balance from exchange

Both modes:
  - Enforce rate limits
  - Log every order attempt and result
  - Handle errors gracefully
"""

import os
import csv
import time
import math
import logging
import random
from pathlib import Path
from collections import deque, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Optional, List, Dict, Any
from copy import deepcopy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

TAKER_FEE = 0.001   # 0.1%
MAKER_FEE = 0.0     # 0.0%  (Bitfinex fee schedule)
SLIPPAGE  = 0.0005  # 0.05% slippage for market orders
DEFAULT_QUOTE = "USDT"

# ---------------------------------------------------------------------------
#  Execution Engine
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """Order execution and position management.

    Parameters
    ----------
    config : dict
        Full bot configuration.
    mode : str
        One of ``"paper"`` or ``"live"``.  Default ``"paper"``.
    """

    def __init__(self, config: dict, mode: str = "paper"):
        self.config = config
        self.mode = mode
        self.trading_config = config.get("trading", {})
        self.data_config = config.get("data", {})
        self.risk_config = config.get("risk", {})

        self._log = logging.getLogger(f"{__name__}.ExecutionEngine")
        self._log.info(
            "ExecutionEngine initialised (mode=%s)", mode
        )

        # --- Internal state ---
        self._open_positions: List[Dict[str, Any]] = []
        self._cached_live_positions: List[Dict[str, Any]] = []  # refreshed once per cycle
        self._live_position_meta: Dict[str, Dict[str, Any]] = {}  # SL/TP tracking for live positions
        self._order_history: deque = deque(maxlen=1000)  # bounded to prevent memory leak (P2-16)
        self._trade_history: deque = deque(maxlen=1000)  # bounded to prevent memory leak (P2-16)
        self._next_order_id: int = 1

        # Paper-only state
        self._paper_balance: Dict[str, Dict[str, float]] = {}
        self._init_paper_balance()

        # Rate limiting
        self._rate_limit = float(
            config.get("exchange", {}).get("rate_limit", 1.0)
        )
        self._last_api_call: float = 0.0

        # Trade history CSV path
        trades_path = self.data_config.get("trades_file", "data/trades.csv")
        self._trades_csv = Path(trades_path)
        self._trades_csv.parent.mkdir(parents=True, exist_ok=True)

        # Wire up BitfinexClient for live mode
        self._client = None
        if mode == "live":
            from market_data.bitfinex_client import BitfinexClient
            self._client = BitfinexClient(config, mode="live")
            self._log.info("Live mode: BitfinexClient connected")

        self._log.info("ExecutionEngine ready")

    # ── properties ────────────────────────────────────────────────────────

    @property
    def open_positions(self) -> List[Dict[str, Any]]:
        """List of currently open positions.

        Uses a cached result updated once per bot cycle to avoid hammering
        the exchange API on every property access (Telegram commands, cycle
        summaries, position checks all read this).
        """
        if self.mode == "paper":
            return self._open_positions
        # Live: return cached positions merged with local SL/TP metadata
        return self._merge_live_positions_with_meta()

    def _merge_live_positions_with_meta(self) -> List[Dict[str, Any]]:
        """Merge exchange-fetched positions with local SL/TP tracking data."""
        merged = []
        for pos in self._cached_live_positions:
            symbol = pos.get("symbol", "")
            # Merge with local metadata (SL/TP set at entry)
            meta = self._live_position_meta.get(symbol, {})
            merged_pos = {**pos, **meta}
            merged.append(merged_pos)
        return merged

    def _update_position_cache(self):
        """Force-refresh the cached live positions from the exchange.

        Called once per bot cycle (inside execute_trade_cycle) so every
        consumer of open_positions reads fresh-but-not-excessive data.
        """
        if not self._client or self.mode == "paper":
            return
        try:
            positions = self._get_live_positions()
            self._cached_live_positions = positions
        except Exception as exc:
            self._log.error("Failed to update position cache: %s", exc)

    # ── order execution ───────────────────────────────────────────────────

    def execute_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        order_type: str = "market",
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Place and track an order.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. ``"BTC/USDT"``.
        side : str
            ``"buy"`` or ``"sell"``.
        amount : float
            Base currency amount (e.g. BTC).
        price : float
            Reference price for the order.
        order_type : str
            ``"market"`` (default) or ``"limit"``.
        stop_loss_pct : float, optional
            Stop-loss percentage from entry.
        take_profit_pct : float, optional
            Take-profit percentage from entry.

        Returns
        -------
        dict
            Order result with keys ``success``, ``order_id``, ``filled_price``,
            ``amount``, ``fee``, and optionally ``error``.
        """
        side = side.lower()
        if side not in ("buy", "sell"):
            return {"success": False, "error": f"Invalid side: {side}"}

        if amount <= 0 or price <= 0:
            return {"success": False, "error": "Invalid amount or price"}

        self._log.info(
            "Executing %s %s %.6f @ %.2f (%s)",
            side.upper(), symbol, amount, price, order_type,
        )

        try:
            if self.mode == "paper":
                return self._paper_execute_order(
                    symbol, side, amount, price, order_type,
                    stop_loss_pct, take_profit_pct,
                )
            else:
                return self._live_execute_order(
                    symbol, side, amount, price, order_type,
                    stop_loss_pct, take_profit_pct,
                )
        except Exception as exc:
            self._log.error("Order execution failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID.

        Returns True if successful, False otherwise.
        """
        self._log.info("Cancelling order %s", order_id)

        if self.mode == "paper":
            # Find and mark cancelled
            for pos in self._open_positions:
                if pos.get("order_id") == order_id:
                    self._open_positions.remove(pos)
                    self._log.info("Paper order %s cancelled", order_id)
                    return True
            self._log.warning("Order %s not found in open positions", order_id)
            return False

        # Live
        if self._client:
            try:
                result = self._client.cancel_order(order_id)
                if result.get("status") == "canceled" or "error" not in result:
                    return True
                self._log.error("Cancel failed: %s", result.get("error"))
            except Exception as exc:
                self._log.error("Cancel error: %s", exc)
        return False

    def get_position(self, symbol: str) -> Dict[str, Any]:
        """Return current position info for a symbol."""
        if self.mode == "paper":
            for pos in self._open_positions:
                if pos.get("symbol") == symbol:
                    return deepcopy(pos)
            return {
                "symbol": symbol,
                "amount": 0.0,
                "entry_price": 0.0,
                "side": None,
            }

        if self._client:
            try:
                return self._client.fetch_position(symbol)
            except Exception as exc:
                self._log.error("Failed to fetch position: %s", exc)
        return {"symbol": symbol, "amount": 0.0}

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Return all open positions."""
        return self.open_positions  # delegates to property

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """Close a specific position by symbol.

        Returns result dict with keys ``success``, ``pnl``, ``reason``.
        """
        self._log.info("Closing position for %s", symbol)

        if self.mode == "paper":
            for i, pos in enumerate(self._open_positions):
                if pos.get("symbol") == symbol:
                    # Simulate closing at current price
                    entry = float(pos.get("entry_price", 0))
                    amount = float(pos.get("amount", 0))
                    side = pos.get("side", "buy")

                    # Use a simulated close price (entry ± small random move)
                    close_price = entry * (1.0 + random.uniform(-0.005, 0.005))
                    if side == "buy":
                        pnl = (close_price - entry) * amount
                    else:
                        pnl = (entry - close_price) * amount

                    # Subtract fees
                    fee = close_price * amount * TAKER_FEE
                    pnl -= fee

                    # Update balance
                    self._update_paper_balance_on_close(symbol, side, amount, close_price, fee)

                    closed = self._open_positions.pop(i)
                    self._record_trade(closed, close_price, pnl, "manual_close")

                    self._log.info(
                        "Position closed: symbol=%s pnl=%.2f", symbol, pnl
                    )
                    return {
                        "success": True,
                        "pnl": round(pnl, 2),
                        "reason": "Manual close",
                        "close_price": close_price,
                    }

            return {"success": False, "reason": f"No open position for {symbol}"}

        # Live
        if self._client:
            try:
                pos = self._client.fetch_position(symbol)
                if pos.get("contracts", 0) <= 0:
                    return {"success": False, "reason": "No position"}
                side = "sell" if pos.get("side") == "long" else "buy"
                amount = abs(pos.get("contracts", 0))
                result = self._client.create_order(
                    symbol, "market", side, amount
                )
                if result.get("success", True) and "error" not in result:
                    return {"success": True, "reason": "Live close"}
                return {"success": False, "error": result.get("error")}
            except Exception as exc:
                self._log.error("Failed to close live position: %s", exc)
                return {"success": False, "error": str(exc)}

        return {"success": False, "reason": "No exchange client"}

    def close_all_positions(self):
        """Close all open positions."""
        self._log.info("Closing all positions...")
        positions = list(self.open_positions)
        if not positions:
            self._log.info("No open positions to close")
            return

        for pos in positions:
            symbol = pos.get("symbol")
            if symbol:
                self.close_position(symbol)

        self._log.info("All positions closed")

    def get_balance(self, currency: str = DEFAULT_QUOTE) -> float:
        """Return available balance for a currency.

        In paper mode returns virtual free balance.
        In live mode queries the exchange.
        """
        if self.mode == "paper":
            bal = self._paper_balance.get(currency, {})
            return bal.get("free", 0.0)

        if self._client:
            try:
                bal = self._client.fetch_balance()
                if isinstance(bal, dict):
                    curr = bal.get(currency, {})
                    if isinstance(curr, dict):
                        return curr.get("free", 0.0)
                    return float(curr) if curr else 0.0
            except Exception as exc:
                self._log.error("Failed to fetch balance: %s", exc)
        return 0.0

    # ── paper trading internals ──────────────────────────────────────────

    def _init_paper_balance(self):
        """Seed paper balance from config — supports multi-pair allocation."""
        initial_capital = float(
            self.trading_config.get("initial_capital", 100.0)
        )

        # Start with all capital in USDT
        self._paper_balance = {
            DEFAULT_QUOTE: {
                "free": initial_capital,
                "used": 0.0,
                "total": initial_capital,
            },
        }

        # Initialize base currency balances for each configured symbol
        raw_symbols = self.trading_config.get("symbols", [])
        if not raw_symbols:
            # Legacy single-symbol fallback
            legacy_symbol = self.trading_config.get("symbol", "BTC/USDT")
            base = legacy_symbol.split("/")[0]
            self._paper_balance[base] = {"free": 0.0, "used": 0.0, "total": 0.0}
        else:
            for s in raw_symbols:
                if not s.get("enabled", True):
                    continue
                base = s["name"].split("/")[0]
                if base not in self._paper_balance:
                    self._paper_balance[base] = {"free": 0.0, "used": 0.0, "total": 0.0}

        self._open_positions = []
        self._order_history = []

    def _paper_execute_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        order_type: str,
        stop_loss_pct: Optional[float],
        take_profit_pct: Optional[float],
    ) -> Dict[str, Any]:
        """Simulate order execution in paper mode.

        Handles position-aware logic:
          - Buying adds to long positions (or reduces a short).
          - Selling adds to short positions (or reduces a long).
        """
        # --- Rate limit ---
        self._enforce_rate_limit()

        base, quote = symbol.split("/")

        # Compute fill price with slippage
        if order_type == "market":
            slippage_mult = 1.0 + (SLIPPAGE if side == "buy" else -SLIPPAGE)
            fill_price = price * slippage_mult
        else:
            fill_price = price

        fee_rate = TAKER_FEE if order_type == "market" else MAKER_FEE
        fee = fill_price * amount * fee_rate
        cost = fill_price * amount
        order_id = f"paper_{int(time.time() * 1000)}_{self._next_order_id:04d}"
        self._next_order_id += 1

        # Find existing opposing position to net against
        existing_pos = None
        for pos in self._open_positions:
            if pos.get("symbol") == symbol and pos.get("side") != side:
                existing_pos = pos
                break

        if existing_pos:
            # We have an opposing position — net against it
            existing_side = existing_pos["side"]
            existing_amt = float(existing_pos["amount"])
            existing_entry = float(existing_pos["entry_price"])

            net_amount = existing_amt - amount
            closed_amt = min(existing_amt, amount)

            # PnL on the closed portion
            if existing_side == "buy":
                pnl = (fill_price - existing_entry) * closed_amt
            else:
                pnl = (existing_entry - fill_price) * closed_amt

            # Subtract fee
            pnl -= fee

            if net_amount <= 0:
                # Position fully closed (or reversed)
                remaining = abs(net_amount)
                self._open_positions.remove(existing_pos)
                self._record_trade(
                    existing_pos, fill_price, pnl,
                    f"closed_{side}",
                )
                self._log.info(
                    "Paper position closed: %s %.6f @ %.2f (pnl=%.2f)",
                    symbol, closed_amt, fill_price, pnl,
                )

                if remaining > 0 and side == "buy":
                    # Remaining is a new buy
                    self._paper_balance[quote]["free"] -= cost
                    self._paper_balance[quote]["total"] -= cost
                    self._paper_balance[quote]["free"] -= fee
                    self._paper_balance[quote]["total"] -= fee

                    if base not in self._paper_balance:
                        self._paper_balance[base] = {"free": 0.0, "used": 0.0, "total": 0.0}
                    self._paper_balance[base]["free"] += amount
                    self._paper_balance[base]["total"] += amount

                    return self._create_paper_position(
                        symbol, side, remaining, fill_price, order_type,
                        fee, order_id, stop_loss_pct, take_profit_pct,
                    )

                elif remaining > 0 and side == "sell":
                    # Remaining is a new sell
                    self._paper_balance[base]["free"] -= amount
                    self._paper_balance[base]["total"] -= amount
                    self._paper_balance[quote]["free"] += cost - fee
                    self._paper_balance[quote]["total"] += cost - fee

                    return self._create_paper_position(
                        symbol, side, remaining, fill_price, order_type,
                        fee, order_id, stop_loss_pct, take_profit_pct,
                    )
                else:
                    # Exactly closed (net_amount == 0)
                    return {
                        "success": True,
                        "order_id": order_id,
                        "filled_price": round(fill_price, 2),
                        "amount": amount,
                        "fee": round(fee, 8),
                        "side": side,
                        "pnl": round(pnl, 2),
                        "position_closed": True,
                    }

            else:
                # Reduced existing position
                existing_pos["amount"] = net_amount
                # Update balance
                if existing_side == "buy":
                    # Was long, sold some
                    self._paper_balance[base]["free"] -= amount
                    self._paper_balance[base]["total"] -= amount
                    self._paper_balance[quote]["free"] += cost - fee
                    self._paper_balance[quote]["total"] += cost - fee
                else:
                    # Was short, bought some
                    self._paper_balance[quote]["free"] -= cost
                    self._paper_balance[quote]["total"] -= cost
                    self._paper_balance[quote]["free"] -= fee
                    self._paper_balance[quote]["total"] -= fee
                    if base not in self._paper_balance:
                        self._paper_balance[base] = {"free": 0.0, "used": 0.0, "total": 0.0}
                    self._paper_balance[base]["free"] += amount
                    self._paper_balance[base]["total"] += amount

                return {
                    "success": True,
                    "order_id": order_id,
                    "filled_price": round(fill_price, 2),
                    "amount": amount,
                    "fee": round(fee, 8),
                    "side": side,
                    "pnl": round(pnl, 2),
                    "position_partial_close": True,
                }

        # --- No opposing position: check balance ---
        if side == "buy":
            needed_quote = cost + fee  # Include fee in availability check (P1-7)
            available = self._paper_balance.get(quote, {}).get("free", 0.0)
            if needed_quote > available:
                return {
                    "success": False,
                    "error": (
                        f"Insufficient {quote}: need {needed_quote:.2f}, "
                        f"have {available:.2f}"
                    ),
                }
        else:  # sell
            available = self._paper_balance.get(base, {}).get("free", 0.0)
            if amount > available:
                return {
                    "success": False,
                    "error": (
                        f"Insufficient {base}: need {amount:.6f}, "
                        f"have {available:.6f}"
                    ),
                }

        # --- Update balances ---
        if side == "buy":
            self._paper_balance[quote]["free"] -= cost
            self._paper_balance[quote]["total"] -= cost
            self._paper_balance[quote]["free"] -= fee
            self._paper_balance[quote]["total"] -= fee
            if base not in self._paper_balance:
                self._paper_balance[base] = {"free": 0.0, "used": 0.0, "total": 0.0}
            self._paper_balance[base]["free"] += amount
            self._paper_balance[base]["total"] += amount

        else:  # sell
            self._paper_balance[base]["free"] -= amount
            self._paper_balance[base]["total"] -= amount
            self._paper_balance[quote]["free"] += cost - fee
            self._paper_balance[quote]["total"] += cost - fee

        # --- Create new position record ---
        return self._create_paper_position(
            symbol, side, amount, fill_price, order_type,
            fee, order_id, stop_loss_pct, take_profit_pct,
        )

    def _create_paper_position(
        self,
        symbol: str,
        side: str,
        amount: float,
        fill_price: float,
        order_type: str,
        fee: float,
        order_id: str,
        stop_loss_pct: Optional[float],
        take_profit_pct: Optional[float],
    ) -> Dict[str, Any]:
        """Create a new position record and add to open positions list."""
        base, quote = symbol.split("/")

        order = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "entry_price": fill_price,
            "order_type": order_type,
            "fee": round(fee, 8),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if stop_loss_pct is not None:
            sl_pct = abs(float(stop_loss_pct)) / 100.0
            order["stop_loss"] = (
                fill_price * (1.0 - sl_pct) if side == "buy"
                else fill_price * (1.0 + sl_pct)
            )
        else:
            order["stop_loss"] = 0.0

        if take_profit_pct is not None:
            tp_pct = abs(float(take_profit_pct)) / 100.0
            order["take_profit"] = (
                fill_price * (1.0 + tp_pct) if side == "buy"
                else fill_price * (1.0 - tp_pct)
            )
        else:
            order["take_profit"] = 0.0

        order["trailing_stop"] = self.risk_config.get("trailing_stop", False)
        order["highest_price"] = fill_price if side == "buy" else 0.0
        order["lowest_price"] = fill_price if side == "sell" else float("inf")

        self._open_positions.append(order)
        self._order_history.append(order)

        self._log.info(
            "Paper order: id=%s %s %.6f @ %.2f (fee=%.6f %s)",
            order_id, side.upper(), amount, fill_price, fee, quote,
        )

        return {
            "success": True,
            "order_id": order_id,
            "filled_price": round(fill_price, 2),
            "amount": amount,
            "fee": round(fee, 8),
            "side": side,
        }

    def _update_paper_balance_on_close(
        self,
        symbol: str,
        side: str,
        amount: float,
        close_price: float,
        fee: float,
    ):
        """Update paper balances after a position is closed."""
        base, quote = symbol.split("/")
        if side == "buy":
            # We had base from the buy, now selling it
            if base in self._paper_balance:
                self._paper_balance[base]["free"] -= amount
                self._paper_balance[base]["total"] -= amount
            self._paper_balance[quote]["free"] += close_price * amount - fee
            self._paper_balance[quote]["total"] += close_price * amount - fee
        else:
            # We had quote from the sell, now buying back
            self._paper_balance[quote]["free"] += close_price * amount - fee
            self._paper_balance[quote]["total"] += close_price * amount - fee
            if base not in self._paper_balance:
                self._paper_balance[base] = {"free": 0.0, "used": 0.0, "total": 0.0}
            self._paper_balance[base]["free"] += amount
            self._paper_balance[base]["total"] += amount

    # ── live trading internals ───────────────────────────────────────────

    def _live_execute_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        order_type: str,
        stop_loss_pct: Optional[float],
        take_profit_pct: Optional[float],
    ) -> Dict[str, Any]:
        """Execute order via the exchange client."""
        if self._client is None:
            return {"success": False, "error": "No exchange client"}

        self._enforce_rate_limit()

        try:
            result = self._client.create_order(
                symbol, order_type, side, amount, price
            )
        except Exception as exc:
            self._log.error("Live order failed: %s", exc)
            return {"success": False, "error": str(exc)}

        if "error" in result and result.get("error"):
            return {"success": False, "error": result["error"]}

        order_id = result.get("id", f"live_{int(time.time())}")
        fill_price = float(result.get("price", price))
        filled = float(result.get("filled", amount))

        # Record position locally for tracking
        pos = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "amount": filled,
            "entry_price": fill_price,
            "order_type": order_type,
            "fee": float(result.get("fee", {}).get("cost", 0.0)),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if stop_loss_pct is not None:
            sl_pct = abs(float(stop_loss_pct)) / 100.0
            pos["stop_loss"] = (
                fill_price * (1.0 - sl_pct)
                if side == "buy"
                else fill_price * (1.0 + sl_pct)
            )
        else:
            pos["stop_loss"] = 0.0

        if take_profit_pct is not None:
            tp_pct = abs(float(take_profit_pct)) / 100.0
            pos["take_profit"] = (
                fill_price * (1.0 + tp_pct)
                if side == "buy"
                else fill_price * (1.0 - tp_pct)
            )
        else:
            pos["take_profit"] = 0.0

        pos["trailing_stop"] = self.risk_config.get("trailing_stop", False)
        pos["highest_price"] = fill_price if side == "buy" else 0.0
        pos["lowest_price"] = fill_price if side == "sell" else float("inf")

        # Store SL/TP metadata for live position tracking
        if self.mode == "live":
            self._live_position_meta[symbol] = {
                "stop_loss": pos.get("stop_loss", 0.0),
                "take_profit": pos.get("take_profit", 0.0),
                "trailing_stop": pos.get("trailing_stop", False),
                "highest_price": pos.get("highest_price", 0.0),
                "lowest_price": pos.get("lowest_price", float("inf")),
                "order_id": order_id,
            }

        self._open_positions.append(pos)
        self._order_history.append(pos)

        self._log.info(
            "Live order executed: id=%s %s %.6f @ %.2f",
            order_id, side.upper(), filled, fill_price,
        )

        return {
            "success": True,
            "order_id": order_id,
            "filled_price": round(fill_price, 2),
            "amount": filled,
            "fee": float(result.get("fee", {}).get("cost", 0.0)),
            "side": side,
        }

    def _get_live_positions(self) -> List[Dict[str, Any]]:
        """Fetch current positions from the exchange for ALL configured symbols (P1-10)."""
        if not self._client:
            return []
        try:
            symbols_config = self.trading_config.get("symbols", [])
            if not symbols_config:
                symbols_config = [{"name": self.trading_config.get("symbol", "BTC/USDT"),
                                   "symbol": "tBTCUST"}]

            positions = []
            for sym_config in symbols_config:
                exchange_symbol = sym_config.get("symbol", "")
                name = sym_config.get("name", exchange_symbol)
                if not exchange_symbol:
                    continue
                try:
                    pos = self._client.fetch_position(exchange_symbol)
                    contracts = float(pos.get("contracts", 0))
                    if contracts == 0:
                        continue
                    side = "buy" if contracts > 0 else "sell"
                    positions.append({
                        "symbol": name,
                        "side": side,
                        "amount": abs(contracts),
                        "entry_price": float(pos.get("entryPrice", 0)),
                        "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    })
                except Exception as sym_exc:
                    self._log.warning("Failed to fetch position for %s: %s", exchange_symbol, sym_exc)
            return positions
        except Exception as exc:
            self._log.error("Failed to fetch live positions: %s", exc)
            return []

    # ── trade history ────────────────────────────────────────────────────

    def _record_trade(
        self,
        position: dict,
        close_price: float,
        pnl: float,
        reason: str,
    ):
        """Record a completed trade to internal history and CSV."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": position.get("symbol", ""),
            "side": position.get("side", ""),
            "entry_price": position.get("entry_price", 0),
            "close_price": close_price,
            "amount": position.get("amount", 0),
            "pnl": round(pnl, 2),
            "reason": reason,
            "mode": self.mode,
        }
        self._trade_history.append(record)

        # Append to CSV
        try:
            file_exists = self._trades_csv.exists()
            with open(self._trades_csv, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=record.keys())
                if not file_exists:
                    writer.writeheader()
                writer.writerow(record)
        except Exception as exc:
            self._log.error("Failed to write trade to CSV: %s", exc)

    def close_position(self, symbol: str, reason: str = "manual") -> Dict[str, Any]:
        """Close an open position for a symbol.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. "BTC/USDT".
        reason : str
            Reason for closing (for logging).

        Returns
        -------
        dict
            Close result with keys 'success', 'pnl', 'order_id', 'error'.
        """
        self._log.info(f"Closing position for {symbol} (reason: {reason})")

        # Find position in our tracking
        position = None
        for pos in self.open_positions:
            if pos.get("symbol") == symbol:
                position = pos
                break

        if not position:
            return {"success": False, "error": f"No open position for {symbol}"}

        side = position.get("side", "buy")
        amount = float(position.get("amount", 0))
        entry_price = float(position.get("entry_price", 0))

        # Determine close side (opposite of entry)
        close_side = "sell" if side == "buy" else "buy"

        try:
            if self.mode == "paper":
                # Paper mode: simulate closing
                current_price = self._paper_balance.get(symbol, {}).get("price", entry_price)
                if close_side == "sell":
                    pnl = (current_price - entry_price) * amount
                else:
                    pnl = (entry_price - current_price) * amount

                # Record trade
                self._record_trade(position, current_price, pnl, reason)

                # Remove from tracking
                if position in self._open_positions:
                    self._open_positions.remove(position)

                self._log.info(f"Paper position closed: {symbol} PnL=${pnl:.2f}")
                return {"success": True, "pnl": pnl, "price": current_price}

            else:
                # Live mode: execute close order on exchange
                self._enforce_rate_limit()
                result = self._client.create_order(
                    symbol=symbol.replace("/", ""),
                    type="market",
                    side=close_side,
                    amount=amount,
                )

                if result.get("success"):
                    close_price = float(result.get("price", entry_price))
                    if close_side == "sell":
                        pnl = (close_price - entry_price) * amount
                    else:
                        pnl = (entry_price - close_price) * amount

                    # Record trade
                    self._record_trade(position, close_price, pnl, reason)

                    # Clean up metadata
                    if symbol in self._live_position_meta:
                        del self._live_position_meta[symbol]

                    self._log.info(f"Live position closed: {symbol} PnL=${pnl:.2f}")
                    return {
                        "success": True,
                        "pnl": pnl,
                        "price": close_price,
                        "order_id": result.get("order_id"),
                    }
                else:
                    error = result.get("error", "Unknown error")
                    self._log.error(f"Failed to close position: {error}")
                    return {"success": False, "error": error}

        except Exception as exc:
            self._log.error(f"Close position failed: {exc}", exc_info=True)
            return {"success": False, "error": str(exc)}

    # ── rate limiting ────────────────────────────────────────────────────

    def _enforce_rate_limit(self):
        """Sleep if needed to respect the configured rate limit."""
        elapsed = time.time() - self._last_api_call
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)
        self._last_api_call = time.time()

    # ── utility ──────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"<ExecutionEngine mode={self.mode} "
            f"open_positions={len(self._open_positions)}>"
        )
