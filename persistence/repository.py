"""TradingRepository — the single interface for all database access.

Every write is wrapped by `_safe`: on any sqlite3.Error it logs at ERROR and
returns a sentinel (-1 for id-returning writes, None for updates) so the
trading path never crashes because of persistence.
"""

import logging
import sqlite3
from typing import Callable, List, Optional

from persistence.schema import connect, init_db
from persistence.models import (
    OrderRecord, FillRecord, PositionRecord,
    TradeRecord, EquitySnapshot, SignalRecord,
)

log = logging.getLogger(__name__)


class TradingRepository:
    def __init__(self, db_path: str, instance: str, mode: str):
        self.db_path = db_path
        self.instance = instance
        self.mode = mode
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._conn = connect(db_path)
            init_db(self._conn)
            log.info("TradingRepository ready at %s (instance=%s, mode=%s)",
                     db_path, instance, mode)
        except Exception as exc:
            log.error("Persistence init FAILED at %s: %s — continuing without DB",
                      db_path, exc)
            self._conn = None

    # ── internal ─────────────────────────────────────────────────────────
    def _safe(self, fn: Callable, default):
        if self._conn is None:
            return default
        try:
            return fn()
        except Exception as exc:
            log.error("Persistence write failed: %s", exc)
            return default

    # ── writes ───────────────────────────────────────────────────────────
    def record_order(self, order: OrderRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO orders (ts,instance,symbol,side,order_type,amount,"
                "price,reduce_only,reason,exchange_order_id,status,filled,"
                "avg_price,error,raw,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order.ts, self.instance, order.symbol, order.side,
                 order.order_type, order.amount, order.price,
                 int(order.reduce_only), order.reason, order.exchange_order_id,
                 order.status, order.filled, order.avg_price, order.error,
                 order.raw, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def update_order(self, order_id: int, *, status: str, filled: float,
                     avg_price: Optional[float], error: Optional[str] = None) -> None:
        def _do():
            self._conn.execute(
                "UPDATE orders SET status=?, filled=?, avg_price=?, error=? "
                "WHERE id=?",
                (status, filled, avg_price, error, order_id))
            self._conn.commit()
            return None
        return self._safe(_do, None)

    def record_fill(self, fill: FillRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO fills (ts,instance,symbol,side,amount,price,fee,"
                "fee_currency,order_id,exchange_trade_id,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (fill.ts, self.instance, fill.symbol, fill.side, fill.amount,
                 fill.price, fill.fee, fill.fee_currency, fill.order_id,
                 fill.exchange_trade_id, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def open_position(self, pos: PositionRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO positions (instance,symbol,side,amount,entry_price,"
                "opened_at,closed_at,realized_pnl,status,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.instance, pos.symbol, pos.side, pos.amount, pos.entry_price,
                 pos.opened_at, pos.closed_at, pos.realized_pnl, pos.status,
                 self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def close_position(self, position_id: int, *, closed_at: str,
                       close_price: float, realized_pnl: float) -> None:
        # close_price is intentionally not stored on positions — the trades
        # row carries it. Kept in the signature for caller-API symmetry.
        def _do():
            self._conn.execute(
                "UPDATE positions SET status='closed', closed_at=?, "
                "realized_pnl=? WHERE id=?",
                (closed_at, realized_pnl, position_id))
            self._conn.commit()
            return None
        return self._safe(_do, None)

    def record_trade(self, trade: TradeRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO trades (ts,instance,symbol,side,entry_price,"
                "close_price,amount,pnl,fee,reason,position_id,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade.ts, self.instance, trade.symbol, trade.side,
                 trade.entry_price, trade.close_price, trade.amount, trade.pnl,
                 trade.fee, trade.reason, trade.position_id, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def snapshot_equity(self, snap: EquitySnapshot) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO equity_snapshots (ts,instance,balance,equity,"
                "open_count,daily_pnl,mode) VALUES (?,?,?,?,?,?,?)",
                (snap.ts, self.instance, snap.balance, snap.equity,
                 snap.open_count, snap.daily_pnl, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)

    def record_signal(self, sig: SignalRecord) -> int:
        def _do():
            cur = self._conn.execute(
                "INSERT INTO signals (ts,instance,symbol,decision,confidence,"
                "strategy_breakdown,acted,order_id,mode) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sig.ts, self.instance, sig.symbol, sig.decision, sig.confidence,
                 sig.strategy_breakdown, int(sig.acted), sig.order_id, self.mode))
            self._conn.commit()
            return cur.lastrowid
        return self._safe(_do, -1)
