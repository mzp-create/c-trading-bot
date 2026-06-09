"""Owns the reduce-only exchange-native catastrophe stop for each open
position: place on entry, cancel on close, reconcile on restart.

The reduce_only flag is the safety invariant — a stop with no position to
reduce is rejected by the exchange, so an orphaned stop can never open a new
position. All methods are best-effort: they log and swallow client errors so a
stop-management failure never breaks the trade loop (the bot-side stop check
remains as backstop).
"""

import logging
from typing import Optional


class StopOrderManager:
    def __init__(self, client, log: Optional[logging.Logger] = None):
        self._client = client
        self._log = log or logging.getLogger(__name__)
        self._ids: dict[str, int] = {}   # symbol -> resting stop order id

    @staticmethod
    def _stop_side(position_side: str) -> str:
        return "sell" if position_side.lower() == "buy" else "buy"

    def place(self, symbol: str, position_side: str, amount: float,
              stop_price: float) -> Optional[int]:
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
            self._log.error("[%s] failed to place catastrophe stop: %s",
                            symbol, exc)
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
