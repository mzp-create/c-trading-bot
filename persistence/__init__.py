"""SQLite persistence layer for the trading bot.

One DB file per instance. The TradingRepository is the single interface for
all reads and writes. Writes are isolated so a DB failure never crashes
trading (see repository._safe).
"""

from persistence.models import (
    OrderRecord, FillRecord, PositionRecord,
    TradeRecord, EquitySnapshot, SignalRecord,
)
from persistence.repository import TradingRepository

__all__ = [
    "TradingRepository",
    "OrderRecord", "FillRecord", "PositionRecord",
    "TradeRecord", "EquitySnapshot", "SignalRecord",
]
