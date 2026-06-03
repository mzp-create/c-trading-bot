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
  - Stores trade log in the SQLite database (data/trading.db)

Live mode:
  - Uses BitfinexClient.create_order() for real execution
  - Fetches actual positions and balance from exchange

Both modes:
  - Enforce rate limits
  - Log every order attempt and result
  - Handle errors gracefully
"""

import os
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

from persistence import (
    TradingRepository, OrderRecord, FillRecord, PositionRecord, TradeRecord,
)

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
    trade_direction : str
        One of ``"long"``, ``"short"``, or ``"both"``. Filters order direction.
    """

    def __init__(self, config: dict, mode: str = "paper",
                 trade_direction: str = "both", instance: str = "default"):
        self.config = config
        self.mode = mode
        self.instance = instance
        self.trade_direction = trade_direction.lower()
        self.trading_config = config.get("trading", {})
        self.data_config = config.get("data", {})
        self.risk_config = config.get("risk", {})

        self._log = logging.getLogger(f"{__name__}.ExecutionEngine")
        self._log.info(
            "ExecutionEngine initialised (mode=%s, direction=%s)", mode, self.trade_direction
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

        # Legacy trades.csv path — NO LONGER written by the engine (trades go
        # to SQLite now). Retained only so the one-time CSV importer knows where
        # the historical file lives, and to anchor the default DB path below.
        trades_path = self.data_config.get("trades_file", "data/trades.csv")
        self._trades_csv = Path(trades_path)
        self._trades_csv.parent.mkdir(parents=True, exist_ok=True)

        # SQLite persistence (single source of truth). Default the DB beside
        # the trades file. Construction never raises — see TradingRepository.
        db_path = self.data_config.get("db_file") or str(
            self._trades_csv.parent / "trading.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._repo = TradingRepository(db_path, instance=self.instance,
                                       mode=self.mode)

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
        # Start with exchange-fetched positions
        for pos in self._cached_live_positions:
            symbol = pos.get("symbol", "")
            # Merge with local metadata (SL/TP set at entry)
            meta = self._live_position_meta.get(symbol, {})
            merged_pos = {**pos, **meta}
            merged.append(merged_pos)
        
        # Also include locally tracked positions that aren't in exchange cache
        # (Bitfinex margin shorts don't appear in fetch_positions)
        exchange_symbols = {p.get("symbol") for p in self._cached_live_positions}
        for pos in self._open_positions:
            symbol = pos.get("symbol", "")
            if symbol not in exchange_symbols:
                merged.append(pos)
        
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

    def _get_position_for_direction_check(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get position for a symbol to validate direction filtering.
        
        Returns position dict with 'side' key ('buy' or 'sell') if found,
        None if no position exists.
        """
        for pos in self.open_positions:
            if pos.get("symbol") == symbol:
                # Normalize side to 'buy' or 'sell'
                side = pos.get("side", "")
                if side in ("buy", "long"):
                    return {**pos, "side": "buy"}
                elif side in ("sell", "short"):
                    return {**pos, "side": "sell"}
        return None

    def sync_positions_at_startup(self):
        """Sync positions from exchange at bot startup and init SL/TP tracking.
        
        This ensures that positions opened before the bot started are
        properly tracked with stop-loss and take-profit levels.
        """
        if not self._client or self.mode == "paper":
            return
        
        self._log.info("Syncing positions from exchange at startup...")
        try:
            positions = self._get_live_positions()
            self._cached_live_positions = positions
            
            # Initialize SL/TP metadata for each position from risk config
            sl_pct = self.risk_config.get("stop_loss_pct", 2.0)
            tp_pct = self.risk_config.get("take_profit_pct", 4.0)
            
            synced_count = 0
            for pos in positions:
                symbol = pos.get("symbol", "")
                entry_price = pos.get("entry_price", 0)
                side = pos.get("side", "buy")
                
                if not symbol or entry_price <= 0:
                    continue
                
                # Calculate SL/TP levels
                sl_pct_decimal = sl_pct / 100.0
                tp_pct_decimal = tp_pct / 100.0
                
                if side == "buy":
                    stop_loss = entry_price * (1.0 - sl_pct_decimal)
                    take_profit = entry_price * (1.0 + tp_pct_decimal)
                else:
                    stop_loss = entry_price * (1.0 + sl_pct_decimal)
                    take_profit = entry_price * (1.0 - tp_pct_decimal)
                
                # Store in metadata
                self._live_position_meta[symbol] = {
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "trailing_stop": self.risk_config.get("trailing_stop", True),
                    "trailing_activation": self.risk_config.get("trailing_stop_activation", 2.0),
                    "trailing_distance": self.risk_config.get("trailing_stop_distance", 0.5),
                    "highest_price": entry_price if side == "buy" else 0,
                    "lowest_price": entry_price if side == "sell" else float('inf'),
                }
                
                self._log.info(
                    f"  Synced {symbol} {side.upper()}: entry=${entry_price:.2f}, "
                    f"SL=${stop_loss:.2f}, TP=${take_profit:.2f}"
                )
                synced_count += 1
            
            self._log.info(f"Position sync complete: {synced_count} position(s) loaded")
            
        except Exception as exc:
            self._log.error("Failed to sync positions at startup: %s", exc)

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

        # ── direction validation for dual-instance trading ────────────────
        if self.trade_direction == "long" and side == "sell":
            # Allow sell only if closing existing long position
            existing_pos = self._get_position_for_direction_check(symbol)
            if not existing_pos or existing_pos.get("side") != "buy":
                return {
                    "success": False, 
                    "error": f"SELL blocked in long-only mode (no long position to close for {symbol})"
                }
        
        if self.trade_direction == "short" and side == "buy":
            # Allow buy only if closing existing short position (covering)
            existing_pos = self._get_position_for_direction_check(symbol)
            if not existing_pos or existing_pos.get("side") != "sell":
                return {
                    "success": False,
                    "error": f"BUY blocked in short-only mode (no short position to cover for {symbol})"
                }

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

        # Normalized contract: success=False means the order was rejected.
        if not result.get("success"):
            return {"success": False, "error": result.get("error") or "Order rejected"}

        order_id = result.get("id") or f"live_{int(time.time())}"
        fill_price = float(result.get("average") or price or 0)
        filled = float(result.get("filled", 0) or 0)
        fee_cost = float(((result.get("raw") or {}).get("fee") or {}).get("cost", 0.0) or 0.0)
        
        # For market orders that show 0 filled, assume full fill
        # (the order should fill immediately, polling is done in bitfinex_client)
        if order_type == "market" and filled == 0:
            filled = amount
            self._log.info("Market order assumed filled: %s @ %s", amount, fill_price)

        # Record position locally for tracking
        pos = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "amount": filled,
            "entry_price": fill_price,
            "order_type": order_type,
            "fee": fee_cost,
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
            "fee": fee_cost,
            "side": side,
        }

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """Normalize any Bitfinex symbol representation to the unified display
        form used throughout the bot (e.g. "BTC/USDT").

        Accepts all three formats:
          - t-format:   "tBTCUST"          -> "BTC/USDT"
          - derivative: "BTC/USDT:USDT"    -> "BTC/USDT"
          - unified:    "BTC/USDT"         -> "BTC/USDT"
        """
        if not symbol:
            return ""
        s = symbol.strip()

        # Strip CCXT derivative suffix ":USDT" / ":USD" etc.
        if ":" in s:
            s = s.split(":", 1)[0]

        # Already unified "BASE/QUOTE"
        if "/" in s:
            return s

        # Bitfinex t-format e.g. tBTCUST / tBTCUSD
        if s.startswith("t") and s[1:].isupper() and len(s) > 3:
            body = s[1:]
            if body.endswith("UST"):
                return f"{body[:-3]}/USDT"
            if body.endswith("USD"):
                return f"{body[:-3]}/USD"
            for quote in ("USDT", "BTC", "ETH"):
                if body.endswith(quote):
                    return f"{body[:-len(quote)]}/{quote}"
        return s

    def _get_live_positions(self) -> List[Dict[str, Any]]:
        """Fetch current positions from the exchange for ALL configured symbols (P1-10).
        
        Uses fetch_positions() to get all positions at once from Bitfinex.
        """
        if not self._client:
            return []
        try:
            # Use fetch_positions to get ALL positions at once
            raw_positions = self._client.fetch_positions()
            
            # Build symbol name mapping from config, keyed on the NORMALIZED
            # display form so it matches whatever format fetch_positions returns.
            symbols_config = self.trading_config.get("symbols", [])
            if not symbols_config:
                symbols_config = [{"name": self.trading_config.get("symbol", "BTC/USDT"),
                                   "symbol": "tBTCUST"}]

            # Map every known representation (tBTCUST, BTC/USDT:USDT, BTC/USDT)
            # -> the config display name (e.g. "BTC/USDT").
            symbol_map = {}
            for sym_config in symbols_config:
                exchange_symbol = sym_config.get("symbol", "")
                name = sym_config.get("name", exchange_symbol)
                for key in (exchange_symbol, name):
                    norm = self._normalize_symbol(key)
                    if norm:
                        symbol_map[norm] = name

            # Convert positions to internal format
            positions = []
            for pos in raw_positions:
                exchange_symbol = pos.get("symbol", "")
                contracts = float(pos.get("contracts", 0))
                if contracts == 0:
                    continue

                # Normalize the exchange symbol to the unified display form so
                # downstream SL/TP + close lookups (keyed "BTC/USDT") match.
                norm = self._normalize_symbol(exchange_symbol)
                display_name = symbol_map.get(norm, norm or exchange_symbol)
                side = "buy" if pos.get("side") == "long" else "sell"
                
                positions.append({
                    "symbol": display_name,
                    "side": side,
                    "amount": contracts,
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                })
            
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
        """Record a completed trade to in-memory history and the database."""
        ts = datetime.now(timezone.utc).isoformat()
        record = {
            "timestamp": ts,
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

        # Persist to the database (single source of truth). Never raises.
        self._repo.record_trade(TradeRecord(
            ts=ts,
            symbol=position.get("symbol", ""),
            side=position.get("side", ""),
            entry_price=float(position.get("entry_price", 0) or 0),
            close_price=float(close_price),
            amount=abs(float(position.get("amount", 0) or 0)),
            pnl=round(pnl, 2),
            reason=reason,
        ))

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
            Close result: {success: bool, pnl: float, price: float,
            error: str|None}.
        """
        self._log.info(f"Closing position for {symbol} (reason: {reason})")

        # Find position in our tracking
        position = None
        for pos in self.open_positions:
            if pos.get("symbol") == symbol:
                position = pos
                break

        # Live mode: open_positions reads a per-cycle cache. When close_position
        # is called outside the run loop (Telegram /close, manual) that cache may
        # be stale/empty, so refresh once from the exchange before giving up —
        # otherwise we would fail to close a position that really exists.
        if not position and self.mode != "paper":
            self._update_position_cache()
            for pos in self.open_positions:
                if pos.get("symbol") == symbol:
                    position = pos
                    break

        if not position:
            return {
                "success": False, "pnl": 0.0, "price": 0.0,
                "error": f"No open position for {symbol}",
            }

        side = position.get("side", "buy")
        amount = float(position.get("amount", 0))
        entry_price = float(position.get("entry_price", 0))

        # Determine close side (opposite of entry)
        close_side = "sell" if side == "buy" else "buy"

        try:
            if self.mode == "paper":
                # Paper mode: simulate closing at the current market price.
                current_price = self._current_price(symbol, entry_price)
                if close_side == "sell":
                    pnl = (current_price - entry_price) * amount
                else:
                    pnl = (entry_price - current_price) * amount

                # Record trade
                self._record_trade(position, current_price, pnl, reason)

                # Remove from tracking
                if position in self._open_positions:
                    self._open_positions.remove(position)
                if symbol in self._live_position_meta:
                    del self._live_position_meta[symbol]

                self._log.info(f"Paper position closed: {symbol} PnL=${pnl:.2f}")
                return {"success": True, "pnl": round(pnl, 2),
                        "price": current_price, "error": None}

            else:
                # Live mode: send a reduceOnly market order so the position is
                # CLOSED (not flipped into an opposing one). Pass the unified
                # symbol straight through — the client converts it internally.
                self._enforce_rate_limit()

                result = self._client.create_order(
                    symbol,
                    "market",
                    close_side,
                    amount,
                    None,                       # price (None for market orders)
                    params={"reduceOnly": True},
                )

                if result.get("success"):
                    # Prefer the actual fill price (average), else fall back.
                    close_price = result.get("average")
                    if close_price in (None, 0, 0.0):
                        close_price = self._current_price(symbol, entry_price)
                    close_price = float(close_price)

                    if close_side == "sell":
                        pnl = (close_price - entry_price) * amount
                    else:
                        pnl = (entry_price - close_price) * amount

                    # Journal the trade and clear local SL/TP metadata so the
                    # closed position is no longer tracked.
                    self._record_trade(position, close_price, pnl, reason)
                    if symbol in self._live_position_meta:
                        del self._live_position_meta[symbol]
                    self._open_positions = [
                        p for p in self._open_positions
                        if p.get("symbol") != symbol
                    ]

                    self._log.info(f"Live position closed: {symbol} PnL=${pnl:.2f}")
                    return {"success": True, "pnl": round(pnl, 2),
                            "price": close_price, "error": None}
                else:
                    # Failure: do NOT record a trade and do NOT delete meta.
                    error = result.get("error", "Unknown error")
                    self._log.error(f"Failed to close position: {error}")
                    return {"success": False, "pnl": 0.0, "price": 0.0,
                            "error": error}

        except Exception as exc:
            self._log.error(f"Close position failed: {exc}", exc_info=True)
            return {"success": False, "pnl": 0.0, "price": 0.0,
                    "error": str(exc)}

    def _current_price(self, symbol: str, fallback: float) -> float:
        """Best-effort current market price for a symbol.

        Uses the exchange client ticker when available; otherwise returns the
        provided fallback (typically the entry price).
        """
        if self._client is not None:
            try:
                ticker = self._client.fetch_ticker(symbol)
                last = ticker.get("last") or ticker.get("close")
                if last:
                    return float(last)
                bid = ticker.get("bid")
                ask = ticker.get("ask")
                if bid and ask:
                    return (float(bid) + float(ask)) / 2.0
            except Exception as exc:
                self._log.warning("Could not fetch current price for %s: %s", symbol, exc)
        return float(fallback)

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
