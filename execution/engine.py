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
from datetime import datetime, timezone
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
        self._order_history: List[Dict[str, Any]] = []
        self._trade_history: List[Dict[str, Any]] = []
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
        """List of currently open positions."""
        if self.mode == "paper":
            return self._open_positions
        # Live: fetch from exchange
        if self._client:
            try:
                positions = self._get_live_positions()
                return positions
            except Exception as exc:
                self._log.error("Failed to fetch live positions: %s", exc)
        return []

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
            needed_quote = cost
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
        """Fetch current positions from the exchange."""
        if not self._client:
            return []
        try:
            symbol = self.trading_config.get("symbol", "BTC/USDT")
            pos = self._client.fetch_position(symbol)
            contracts = float(pos.get("contracts", 0))
            if contracts == 0:
                return []
            side = "buy" if contracts > 0 else "sell"
            return [{
                "symbol": symbol,
                "side": side,
                "amount": abs(contracts),
                "entry_price": float(pos.get("entryPrice", 0)),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
            }]
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
