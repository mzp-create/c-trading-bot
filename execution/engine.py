"""
Execution Engine — Hermes Crypto Trading Bot.

Manages order placement, position tracking, and balance management in
both **paper** and **live** modes.

Both modes use the same BitfinexClient path:
  - Paper: BitfinexClient wraps PaperBroker (slippage/fee/wallet simulation)
  - Live:  BitfinexClient wraps BfxRest / WsFeed (real exchange)

Common:
  - execute_order / close_position route through client.create_order()
  - get_balance routes through client.fetch_balance()
  - Position dicts carry SL/TP/trailing_stop fields for _check_positions
  - All trades persist to SQLite via TradingRepository
  - Rate limits enforced; errors handled gracefully
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
from bitfinex.errors import OrderRejected, AckUnparseable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

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

        # Build BitfinexClient for both paper and live modes
        from bitfinex import BitfinexClient
        self._client = BitfinexClient(config, mode=mode, instance=self.instance)
        self._log.info("BitfinexClient connected (mode=%s)", mode)

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
        if self.mode == "paper":
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
        if self.mode == "paper":
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

        # Persist the entry order (additive; never blocks trading).
        db_order_id = self._repo.record_order(OrderRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            symbol=symbol, side=side, order_type=order_type,
            amount=abs(amount), price=price, reduce_only=False,
            reason="entry"))

        try:
            result = self._live_execute_order(
                symbol, side, amount, price, order_type,
                stop_loss_pct, take_profit_pct,
            )
        except Exception as exc:
            self._log.error("Order execution failed: %s", exc, exc_info=True)
            self._repo.update_order(db_order_id, status="rejected", filled=0.0,
                                    avg_price=None, error=str(exc))
            return {"success": False, "error": str(exc),
                    "db_order_id": db_order_id}

        # Reflect the outcome on the order row and surface the id to callers.
        if result.get("success"):
            self._repo.update_order(
                db_order_id, status="filled", filled=abs(amount),
                avg_price=result.get("filled_price") or result.get("average")
                or price)
        else:
            self._repo.update_order(
                db_order_id, status="rejected", filled=0.0, avg_price=None,
                error=result.get("error"))
        result["db_order_id"] = db_order_id
        return result

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
                order = self._client.cancel_order(order_id)
                # Typed Order: no exception + CANCELED status means success.
                if (order.status or "").upper() == "CANCELED":
                    return True
                self._log.error("Cancel failed: unexpected status %s", order.status)
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

        Queries the client (PaperBroker in paper mode, exchange in live).
        Returns a bare float (the available balance for `currency`).
        """
        try:
            wallets = self._client.fetch_balance()
            # Typed: list[Wallet]; find the matching currency.
            for w in wallets:
                if w.currency == currency:
                    return float(w.available)
            return 0.0
        except Exception as exc:
            self._log.error("get_balance failed: %s", exc)
            return 0.0

    # ── order execution internals ────────────────────────────────────────

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
            order = self._client.create_order(
                symbol, side, amount, order_type=order_type, price=price)
        except (OrderRejected, AckUnparseable) as exc:
            self._log.error("Live order failed (%s): %s", type(exc).__name__, exc)
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            self._log.error("Live order failed: %s", exc)
            return {"success": False, "error": str(exc)}

        # Map typed Order -> engine's outward dict contract.
        # Paper broker returns small integer ids; live returns large integer ids.
        # Produce a string id prefixed by mode so callers can distinguish.
        _prefix = "paper" if self.mode == "paper" else "live"
        if order.id is not None:
            order_id = f"{_prefix}_{order.id}"
        else:
            order_id = f"{_prefix}_{int(time.time())}"
        fill_price = float(order.avg_price or price or 0)
        filled = float(order.filled or 0)
        fee_cost = float(order.fee or 0.0)

        if not order.is_accepted:
            return {"success": False,
                    "error": order.status or "Order not accepted"}

        # For market orders that show 0 filled, assume full fill
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

        # Store SL/TP metadata for position tracking (both paper and live)
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
            "fee": round(fee_cost, 8),
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
            # fetch_positions() returns list[Position] (typed).
            typed_positions = self._client.fetch_positions()

            # Build symbol name mapping from config so tBTCUST / BTC/USDT:USDT
            # -> the config display name (e.g. "BTC/USDT").
            symbols_config = self.trading_config.get("symbols", [])
            if not symbols_config:
                symbols_config = [{"name": self.trading_config.get("symbol", "BTC/USDT"),
                                   "symbol": "tBTCUST"}]

            symbol_map = {}
            for sym_config in symbols_config:
                exchange_symbol = sym_config.get("symbol", "")
                name = sym_config.get("name", exchange_symbol)
                for key in (exchange_symbol, name):
                    norm = self._normalize_symbol(key)
                    if norm:
                        symbol_map[norm] = name

            # Convert typed Position objects to the engine's internal dict shape.
            positions = []
            for p in typed_positions:
                if p.abs_amount == 0:
                    continue
                # Typed Position already carries display-form symbol; map via
                # config table so the display name matches the config key.
                norm = self._normalize_symbol(p.symbol)
                display_name = symbol_map.get(norm, norm or p.symbol)
                side = "buy" if p.side == "long" else "sell"
                # Dual keys (camelCase + snake_case) during migration: external
                # readers (scripts/reconcile_*, telegram_alerts) still use camelCase.
                # TODO: drop camelCase aliases once those callers are migrated.
                positions.append({
                    "symbol": display_name,
                    "side": side,
                    "contracts": p.abs_amount,
                    "amount": p.abs_amount,
                    "entryPrice": p.entry_price,
                    "entry_price": p.entry_price,
                    "unrealizedPnl": p.unrealized_pnl,
                    "unrealized_pnl": p.unrealized_pnl,
                })

            return positions
        except Exception as exc:
            self._log.error("Failed to fetch live positions: %s", exc)
            return []

    # ── trade history ────────────────────────────────────────────────────

    def _persist_close(self, *, symbol, side, close_side, amount, entry_price,
                       close_price, pnl, reason, opened_at,
                       exchange_order_id=None, fee=0.0, fee_currency=None):
        """Persist a position close: a synthetic open+close position row, a
        reduce-only close order, and its fill. All calls go through the
        repository's _safe guard, so this never raises into the trading path.

        Phase-1 note: the engine does not yet carry a persistent position id
        from entry time, so we record the position as opened-then-closed in one
        shot here. Real entry-to-exit position tracking lands in a later phase.
        """
        now = datetime.now(timezone.utc).isoformat()
        pos_id = self._repo.open_position(PositionRecord(
            symbol=symbol, side=side, amount=abs(amount),
            entry_price=entry_price, opened_at=opened_at or now))
        # NOTE: pos_id is -1 if open_position failed (logged at ERROR by the
        # repo); close_position then no-ops on WHERE id=-1. Trading is unaffected.
        self._repo.close_position(pos_id, closed_at=now,
                                  close_price=close_price,
                                  realized_pnl=round(pnl, 2))
        close_oid = self._repo.record_order(OrderRecord(
            ts=now, symbol=symbol, side=close_side, order_type="market",
            amount=abs(amount), price=close_price, reduce_only=True,
            reason=reason, status="filled", filled=abs(amount),
            avg_price=close_price, exchange_order_id=exchange_order_id))
        self._repo.record_fill(FillRecord(
            ts=now, symbol=symbol, side=close_side, amount=abs(amount),
            price=close_price, order_id=close_oid, fee=fee,
            fee_currency=fee_currency))

    def close(self) -> None:
        """Stop the live WS feed (if any). Safe in paper mode."""
        client = getattr(self, "_client", None)
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception as exc:
                self._log.error("client close failed: %s", exc)

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
            # Unified path for both paper and live: send a reduceOnly market
            # order so the position is CLOSED (not flipped).  PaperBroker
            # simulates slippage/fee on the fill; the live client sends to the
            # exchange.  We pass `price=ref` so PaperBroker can use it as the
            # mid-point for slippage simulation (live bfxapi ignores price on
            # market orders).
            self._enforce_rate_limit()
            ref = self._current_price(symbol, entry_price)

            try:
                order = self._client.create_order(
                    symbol, close_side, amount,
                    order_type="market", price=ref, reduce_only=True)
            except (OrderRejected, AckUnparseable) as exc:
                self._log.error("Failed to close position: %s", exc)
                return {"success": False, "pnl": 0.0, "price": 0.0,
                        "error": str(exc)}

            if order.is_accepted:
                # Prefer the actual fill price (average), else fall back.
                close_price = order.avg_price
                if close_price in (None, 0, 0.0):
                    close_price = ref
                close_price = float(close_price)

                if close_side == "sell":
                    pnl = (close_price - entry_price) * amount
                else:
                    pnl = (entry_price - close_price) * amount

                # Persist the close; additive and never raises.
                fill = None
                if hasattr(self._client, "last_fill"):
                    try:
                        fill = self._client.last_fill(symbol)
                    except Exception:
                        fill = None
                fee = fill.fee if fill is not None else 0.0
                fee_ccy = fill.fee_currency if fill is not None else None
                self._persist_close(
                    symbol=symbol, side=side, close_side=close_side,
                    amount=amount, entry_price=entry_price,
                    close_price=close_price, pnl=pnl, reason=reason,
                    opened_at=position.get("timestamp"),
                    exchange_order_id=str(order.id) if order.id is not None
                    else None, fee=fee, fee_currency=fee_ccy)

                # Journal the trade and clear local SL/TP metadata so the
                # closed position is no longer tracked.
                self._record_trade(position, close_price, pnl, reason)
                if symbol in self._live_position_meta:
                    del self._live_position_meta[symbol]
                self._open_positions = [
                    p for p in self._open_positions
                    if p.get("symbol") != symbol
                ]

                self._log.info(
                    "Position closed: %s PnL=$%.2f (mode=%s)",
                    symbol, pnl, self.mode)
                return {"success": True, "pnl": round(pnl, 2),
                        "price": close_price, "error": None}
            else:
                # Failure: do NOT record a trade and do NOT delete meta.
                error = order.status or "Unknown error"
                self._log.error("Failed to close position: %s", error)
                return {"success": False, "pnl": 0.0, "price": 0.0,
                        "error": error}

        except Exception as exc:
            self._log.error("Close position failed: %s", exc, exc_info=True)
            return {"success": False, "pnl": 0.0, "price": 0.0,
                    "error": str(exc)}

    def _current_price(self, symbol: str, fallback: float) -> float:
        """Best-effort current market price for a symbol.

        Uses the exchange client ticker when available; otherwise returns the
        provided fallback (typically the entry price).
        """
        if self._client is not None:
            try:
                t = self._client.fetch_ticker(symbol)
                # Typed Ticker: access attributes directly.
                if t.last:
                    return float(t.last)
                if t.bid and t.ask:
                    return (t.bid + t.ask) / 2.0
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
