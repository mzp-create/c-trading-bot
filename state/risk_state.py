"""Per-symbol, bot-owned SL/TP + trailing metadata (NOT exchange state).

The engine sets this on entry and clears it on close; main._check_positions
merges it onto exchange positions for the SL/TP threshold check. Thread-safe.
"""

import threading
from typing import List, Optional


class RiskState:
    def __init__(self):
        self._lock = threading.Lock()
        self._meta: dict[str, dict] = {}

    def set(self, symbol: str, *, stop_loss: float, take_profit: float,
            trailing_stop: bool, trailing_activation: float,
            trailing_distance: float, entry_price: float, side: str) -> None:
        with self._lock:
            self._meta[symbol] = {
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "trailing_stop": trailing_stop,
                "trailing_activation": trailing_activation,
                "trailing_distance": trailing_distance,
                "highest_price": entry_price if side == "buy" else 0.0,
                "lowest_price": entry_price if side == "sell" else float("inf"),
            }

    def get(self, symbol: str) -> Optional[dict]:
        with self._lock:
            m = self._meta.get(symbol)
            return dict(m) if m is not None else None

    def update_trailing(self, symbol: str, *, stop_loss: Optional[float] = None,
                        highest_price: Optional[float] = None,
                        lowest_price: Optional[float] = None) -> None:
        with self._lock:
            m = self._meta.get(symbol)
            if m is None:
                return
            if stop_loss is not None:
                m["stop_loss"] = stop_loss
            if highest_price is not None:
                m["highest_price"] = highest_price
            if lowest_price is not None:
                m["lowest_price"] = lowest_price

    def clear(self, symbol: str) -> None:
        with self._lock:
            self._meta.pop(symbol, None)

    def symbols(self) -> List[str]:
        with self._lock:
            return list(self._meta.keys())
