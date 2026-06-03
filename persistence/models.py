"""Typed records written to / read from the trading database.

`instance` and `mode` are NOT fields here — the repository injects them from
its construction args so callers never repeat them.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderRecord:
    ts: str                              # ISO-8601 UTC
    symbol: str                          # display form, e.g. BTC/USDT
    side: str                            # buy | sell
    order_type: str                      # market | limit
    amount: float                        # requested, absolute
    price: Optional[float] = None        # limit price; None for market
    reduce_only: bool = False
    reason: Optional[str] = None         # entry | stop_loss | take_profit | manual | ...
    exchange_order_id: Optional[str] = None
    status: str = "unknown"              # accepted|rejected|filled|partial|canceled|unknown
    filled: float = 0.0
    avg_price: Optional[float] = None
    error: Optional[str] = None
    raw: Optional[str] = None            # JSON of the exchange ack/result


@dataclass
class FillRecord:
    ts: str
    symbol: str
    side: str
    amount: float                        # absolute
    price: float
    fee: float = 0.0
    fee_currency: Optional[str] = None
    order_id: Optional[int] = None       # FK -> orders.id
    exchange_trade_id: Optional[str] = None


@dataclass
class PositionRecord:
    symbol: str
    side: str                            # buy | sell (long | short)
    amount: float                        # absolute
    entry_price: float
    opened_at: str
    status: str = "open"                 # open | closed
    closed_at: Optional[str] = None
    realized_pnl: Optional[float] = None


@dataclass
class TradeRecord:
    ts: str
    symbol: str
    side: str                            # side of the position being closed
    entry_price: float
    close_price: float
    amount: float                        # absolute
    pnl: float
    fee: float = 0.0
    reason: Optional[str] = None
    position_id: Optional[int] = None    # FK -> positions.id


@dataclass
class EquitySnapshot:
    ts: str
    balance: float                       # wallet balance
    equity: float                        # balance + unrealized
    open_count: int = 0
    daily_pnl: float = 0.0


@dataclass
class SignalRecord:
    ts: str
    symbol: str
    decision: str                        # BUY | SELL | HOLD
    confidence: float
    strategy_breakdown: Optional[str] = None   # JSON / text
    acted: bool = False
    order_id: Optional[int] = None       # FK -> orders.id when acted
