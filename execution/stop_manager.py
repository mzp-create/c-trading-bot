"""Owns the reduce-only exchange-native catastrophe stop for each open
position: place on entry, cancel on close, reconcile on restart.

The reduce_only flag is the safety invariant — a stop with no position to
reduce is rejected by the exchange, so an orphaned stop can never open a new
position. All methods are best-effort: they log and swallow client errors so a
stop-management failure never breaks the trade loop (the bot-side stop check
remains as backstop).
"""

import logging
from typing import Any, Dict, List, Optional


class StopOrderManager:
    def __init__(self, client, log: Optional[logging.Logger] = None):
        self._client = client
        self._log = log or logging.getLogger(__name__)
        self._ids: dict[str, int] = {}   # symbol -> resting stop order id

    @staticmethod
    def _stop_side(position_side: str) -> str:
        ps = position_side.lower()
        if ps == "buy":
            return "sell"
        if ps == "sell":
            return "buy"
        raise ValueError(f"unexpected position_side {position_side!r}")

    def place(self, symbol: str, position_side: str, amount: float,
              stop_price: float) -> Optional[int]:
        if stop_price is None or stop_price <= 0:
            self._log.error("[%s] UNPROTECTED — refusing to place catastrophe "
                            "stop at non-positive trigger %s", symbol, stop_price)
            return None
        if symbol in self._ids:
            self._log.warning("[%s] replacing existing catastrophe stop id=%s",
                              symbol, self._ids[symbol])
            self.cancel(symbol)
        try:
            order = self._client.create_order(
                symbol, self._stop_side(position_side), abs(amount),
                order_type="stop", price=stop_price, reduce_only=True)
            oid = getattr(order, "id", None)
            if oid is not None:
                self._ids[symbol] = oid
                self._log.info("[%s] catastrophe stop placed id=%s @ %.6f",
                               symbol, oid, stop_price)
            return oid
        except Exception as exc:
            self._log.error("[%s] UNPROTECTED — failed to place catastrophe "
                            "stop: %s", symbol, exc)
            return None

    def cancel(self, symbol: str) -> None:
        oid = self._ids.pop(symbol, None)
        if oid is None:
            return
        try:
            self._client.cancel_order(oid)
            self._log.info("[%s] catastrophe stop cancelled id=%s", symbol, oid)
        except Exception as exc:
            self._log.error("[%s] failed to cancel catastrophe stop id=%s: %s",
                            symbol, oid, exc)

    def reconcile(self, positions: List[Dict[str, Any]]) -> None:
        """On restart: adopt an existing reduce-only stop per position, place
        one where missing, and cancel orphan reduce-only stops with no matching
        open position."""
        try:
            open_orders = self._client.fetch_open_orders()
        except Exception as exc:
            self._log.error("stop reconcile: fetch_open_orders failed: %s", exc)
            return

        by_symbol: dict[str, int] = {}
        for o in open_orders:
            # Only adopt exchange STOP orders. This bot places no exchange-native
            # take-profits, so today all reduce_only resting orders are stops —
            # but filter by order_type to stay correct if that ever changes.
            if (getattr(o, "reduce_only", False)
                    and getattr(o, "order_type", "") == "stop"):
                by_symbol.setdefault(o.symbol, o.id)

        held = {p.get("symbol") for p in positions}

        for p in positions:
            symbol = p.get("symbol")
            if symbol in by_symbol:
                self._ids[symbol] = by_symbol[symbol]
                self._log.info("[%s] adopted existing catastrophe stop id=%s",
                               symbol, by_symbol[symbol])
                continue
            stop_loss = p.get("stop_loss")
            if stop_loss is None:
                self._log.error("[%s] UNPROTECTED on reload — no stop_loss, "
                                "cannot place catastrophe stop", symbol)
                continue
            self.place(symbol, p.get("side", "buy"),
                       float(p.get("amount", 0.0)), float(stop_loss))

        for symbol, oid in by_symbol.items():
            if symbol not in held:
                self._ids.pop(symbol, None)
                try:
                    self._client.cancel_order(oid)
                    self._log.info("[%s] cancelled orphan catastrophe stop id=%s",
                                   symbol, oid)
                except Exception as exc:
                    self._log.error("[%s] failed to cancel orphan stop id=%s: %s",
                                    symbol, oid, exc)
