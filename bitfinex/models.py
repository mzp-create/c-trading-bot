"""Typed, frozen models returned by BitfinexClient. Callers read attributes.

`status` strings are the bitfinex order-status text (e.g. 'EXECUTED',
'ACTIVE', 'CANCELED', 'REJECTED') or our paper equivalents. There is NO
success sentinel that can default to True.
"""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Order:
    id: Optional[int]
    symbol: str                      # display form, e.g. BTC/USDT
    side: str                        # buy | sell
    order_type: str                  # market | limit
    amount: float                    # absolute requested
    filled: float
    avg_price: Optional[float]
    status: str
    reduce_only: bool
    fee: float
    fee_currency: Optional[str]
    raw: Any = None

    @property
    def is_filled(self) -> bool:
        return self.id is not None and "EXECUTED" in (self.status or "").upper()

    @property
    def is_rejected(self) -> bool:
        s = (self.status or "").upper()
        return self.id is None or "REJECT" in s or "ERROR" in s


@dataclass(frozen=True)
class Position:
    symbol: str                      # display form
    side: str                        # long | short
    amount: float                    # signed
    entry_price: float
    unrealized_pnl: float
    leverage: float
    raw_symbol: str

    @property
    def abs_amount(self) -> float:
        return abs(self.amount)


@dataclass(frozen=True)
class Ticker:
    symbol: str
    bid: float
    ask: float
    last: float


@dataclass(frozen=True)
class Wallet:
    currency: str
    wallet_type: str                 # exchange | margin | funding
    balance: float
    available: float


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str                        # buy | sell
    amount: float                    # absolute
    price: float
    fee: float
    fee_currency: Optional[str]
    order_id: Optional[int]
    trade_id: Optional[int]
    ts: str                          # ISO-8601 UTC
