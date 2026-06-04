"""Push-maintained account state (positions/wallets/fills), thread-safe.

Authoritative when the feed is authenticated; otherwise callers fall back to
REST. Snapshot events replace; incremental events upsert/remove.
"""

import threading
import time
from typing import List, Optional

from bitfinex.models import Position, Wallet, Fill


class AccountState:
    def __init__(self):
        self._lock = threading.Lock()
        self._positions: dict[str, Position] = {}   # display symbol -> Position
        self._wallets: dict[str, Wallet] = {}       # currency -> Wallet
        self._fills: dict[str, Fill] = {}           # display symbol -> latest Fill
        self.connected = False
        self.authenticated = False
        self.last_reconcile: Optional[float] = None

    # ── status ───────────────────────────────────────────────────────────
    def set_status(self, *, connected: bool, authenticated: bool) -> None:
        with self._lock:
            self.connected = connected
            self.authenticated = authenticated

    def mark_reconciled(self) -> None:
        with self._lock:
            self.last_reconcile = time.monotonic()

    # ── positions ────────────────────────────────────────────────────────
    def apply_position_snapshot(self, positions: List[Position]) -> None:
        with self._lock:
            self._positions = {p.symbol: p for p in positions if p.amount != 0}

    def apply_position(self, position: Position) -> None:
        with self._lock:
            if position.amount == 0:
                self._positions.pop(position.symbol, None)
            else:
                self._positions[position.symbol] = position

    def remove_position(self, symbol: str) -> None:
        with self._lock:
            self._positions.pop(symbol, None)

    def get_positions(self) -> List[Position]:
        with self._lock:
            return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[Position]:
        with self._lock:
            return self._positions.get(symbol)

    # ── wallets ──────────────────────────────────────────────────────────
    def apply_wallet_snapshot(self, wallets: List[Wallet]) -> None:
        with self._lock:
            self._wallets = {w.currency: w for w in wallets}

    def apply_wallet(self, wallet: Wallet) -> None:
        with self._lock:
            self._wallets[wallet.currency] = wallet

    def get_wallets(self) -> List[Wallet]:
        with self._lock:
            return list(self._wallets.values())

    # ── fills ────────────────────────────────────────────────────────────
    def apply_fill(self, fill: Fill) -> None:
        with self._lock:
            self._fills[fill.symbol] = fill

    def last_fill(self, symbol: str) -> Optional[Fill]:
        with self._lock:
            return self._fills.get(symbol)
