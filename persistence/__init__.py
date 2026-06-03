"""SQLite persistence layer for the trading bot.

One DB file per instance. The TradingRepository (added in Task 3) is the single
interface for all reads and writes. Writes are isolated so a DB failure never
crashes trading (see repository._safe).
"""

from persistence.models import (
    OrderRecord, FillRecord, PositionRecord,
    TradeRecord, EquitySnapshot, SignalRecord,
)

__all__ = [
    "OrderRecord", "FillRecord", "PositionRecord",
    "TradeRecord", "EquitySnapshot", "SignalRecord",
]
