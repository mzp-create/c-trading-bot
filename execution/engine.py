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
  - Transport-layer throttling delegated to the client; errors handled gracefully
"""

import os
import time
import logging
import random
from pathlib import Path
from collections import deque
from datetime import datetime, timezone
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
        self._order_history: deque = deque(maxlen=1000)  # bounded to prevent memory leak (P2-16)
        self._trade_history: deque = deque(maxlen=1000)  # bounded to prevent memory leak (P2-16)
        self._next_order_id: int = 1

        # RiskState: per-symbol SL/TP + trailing metadata (replaces _live_position_meta)
        from state.risk_state import RiskState
        self._risk_state = RiskState()
        # Thin entry records: symbol -> {symbol, side, amount, entry_price, unrealized_pnl}
        # Used as safety-net fallback for symbols not visible via fetch_positions
        self._risk_entry: dict[str, dict] = {}
        # When True, trust exchange-reported positions exclusively (skip union safety net)
        self._trust_positions = bool(
            config.get("exchange", {}).get("trust_exchange_positions", False)
        )

        # SQLite persistence (single source of truth). Construction never raises
        # — see TradingRepository.
        db_path = self.data_config.get("db_file") or str(
            Path(self.data_config.get("data_dir", "data")) / "trading.db")
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

        Builds position list from client.fetch_positions() and merges in
        RiskState SL/TP metadata.  When trust_exchange_positions is False
        (default), also surfaces any RiskState-known symbols not reported
        by the exchange — the safety net for Bitfinex margin shorts that
        vanish from fetch_positions.
        """
        out = []
        seen: set = set()
        for p in self._client.fetch_positions():
            meta = self._risk_state.get(p.symbol) or {}
            out.append({
                "symbol": p.symbol,
                "side": "buy" if p.side == "long" else "sell",
                "amount": p.abs_amount,
                "entry_price": p.entry_price,
                "unrealized_pnl": p.unrealized_pnl,
                **meta,
            })
            seen.add(p.symbol)
        if not self._trust_positions:
            for sym, e in self._risk_entry.items():
                if sym not in seen:
                    meta = self._risk_state.get(sym) or {}
                    out.append({**e, **meta})
        return out

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
        """Sync positions from exchange at bot startup and seed RiskState.

        For each exchange position, computes SL/TP from risk config and
        stores them in RiskState and risk_entry so open_positions returns
        them with proper SL/TP metadata from the first cycle.
        """
        if self.mode == "paper":
            return

        self._log.info("Syncing positions from exchange at startup...")
        try:
            sl_pct = self.risk_config.get("stop_loss_pct", 2.0)
            tp_pct = self.risk_config.get("take_profit_pct", 4.0)
            sl_pct_decimal = sl_pct / 100.0
            tp_pct_decimal = tp_pct / 100.0

            synced_count = 0
            for p in self._client.fetch_positions():
                if p.abs_amount == 0:
                    continue
                symbol = p.symbol
                entry_price = float(p.entry_price or 0)
                side = "buy" if p.side == "long" else "sell"

                if not symbol or entry_price <= 0:
                    continue

                if side == "buy":
                    stop_loss = entry_price * (1.0 - sl_pct_decimal)
                    take_profit = entry_price * (1.0 + tp_pct_decimal)
                else:
                    stop_loss = entry_price * (1.0 + sl_pct_decimal)
                    take_profit = entry_price * (1.0 - tp_pct_decimal)

                self._risk_state.set(
                    symbol,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    trailing_stop=bool(self.risk_config.get("trailing_stop", True)),
                    trailing_activation=float(
                        self.risk_config.get("trailing_stop_activation", 2.0)),
                    trailing_distance=float(
                        self.risk_config.get("trailing_stop_distance", 0.5)),
                    entry_price=entry_price,
                    side=side,
                )
                self._risk_entry[symbol] = {
                    "symbol": symbol,
                    "side": side,
                    "amount": float(p.abs_amount),
                    "entry_price": entry_price,
                    "unrealized_pnl": float(p.unrealized_pnl or 0.0),
                }

                self._log.info(
                    "  Synced %s %s: entry=$%.2f, SL=$%.2f, TP=$%.2f",
                    symbol, side.upper(), entry_price, stop_loss, take_profit,
                )
                synced_count += 1

            self._log.info("Position sync complete: %d position(s) loaded",
                           synced_count)

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
            # Find and mark cancelled by checking _risk_entry for matching order_id
            for sym, entry in list(self._risk_entry.items()):
                if entry.get("order_id") == order_id:
                    self._risk_state.clear(sym)
                    self._risk_entry.pop(sym, None)
                    self._log.info("Paper order %s cancelled", order_id)
                    return True
            self._log.warning("Order %s not found in open positions", order_id)
            return False

        # Live
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
        for pos in self.open_positions:
            if pos.get("symbol") == symbol:
                return deepcopy(pos)
        return {
            "symbol": symbol,
            "amount": 0.0,
            "entry_price": 0.0,
            "side": None,
        }

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
            sl_price = (
                fill_price * (1.0 - sl_pct)
                if side == "buy"
                else fill_price * (1.0 + sl_pct)
            )
        else:
            sl_price = 0.0

        if take_profit_pct is not None:
            tp_pct = abs(float(take_profit_pct)) / 100.0
            tp_price = (
                fill_price * (1.0 + tp_pct)
                if side == "buy"
                else fill_price * (1.0 - tp_pct)
            )
        else:
            tp_price = 0.0

        # Store SL/TP metadata in RiskState (single source of truth)
        self._risk_state.set(
            symbol,
            stop_loss=sl_price,
            take_profit=tp_price,
            trailing_stop=bool(self.risk_config.get("trailing_stop", False)),
            trailing_activation=float(
                self.risk_config.get("trailing_stop_activation", 2.0)),
            trailing_distance=float(
                self.risk_config.get("trailing_stop_distance", 0.5)),
            entry_price=fill_price,
            side=side,
        )
        # Safety-net entry record: used by open_positions union when the exchange
        # doesn't report the position (e.g. Bitfinex margin shorts)
        self._risk_entry[symbol] = {
            "symbol": symbol,
            "side": side,
            "amount": filled,
            "entry_price": fill_price,
            "unrealized_pnl": 0.0,
        }

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

        # Live mode: if position not found via open_positions (which already
        # includes the RiskState union), also check _risk_entry directly in
        # case the position is RiskState-known but open_positions returned stale
        # data (e.g. called outside the main loop).
        if not position and self.mode != "paper":
            entry = self._risk_entry.get(symbol)
            if entry:
                meta = self._risk_state.get(symbol) or {}
                position = {**entry, **meta}

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

                # Journal the trade and clear RiskState so the closed
                # position is no longer tracked.
                self._record_trade(position, close_price, pnl, reason)
                self._risk_state.clear(symbol)
                self._risk_entry.pop(symbol, None)

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
        """Best-effort current market price for a symbol."""
        try:
            t = self._client.fetch_ticker(symbol)   # client already does WS/REST
            if t.last:
                return float(t.last)
            if t.bid and t.ask:
                return (t.bid + t.ask) / 2.0
        except Exception as exc:
            self._log.warning("price fetch failed for %s: %s", symbol, exc)
        return float(fallback)

    # ── utility ──────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"<ExecutionEngine mode={self.mode} "
            f"open_positions={len(self._risk_entry)}>"
        )
