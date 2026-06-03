import sys
import types
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine
from main import TradingBot


def _engine(tmp_path):
    config = {"data": {"trades_file": str(tmp_path / "trades.csv"),
                       "db_file": str(tmp_path / "trading.db")},
              "exchange": {"rate_limit": 0.0}}
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def _stub_bot(eng, *, initial_capital=500.0, daily_pnl=5.0):
    return types.SimpleNamespace(
        executor=eng,
        initial_capital=initial_capital,
        daily_pnl=daily_pnl,
        log=logging.getLogger("stub"))


def test_record_decision_signal_for_hold_and_acted(tmp_path):
    eng = _engine(tmp_path)
    bot = _stub_bot(eng)

    # Seed an orders row so the FK on signals.order_id=7 is satisfiable.
    # In production _db_order_id always comes from ExecutionEngine.execute_order
    # which writes the orders row first; this replicates that invariant.
    conn = eng._repo._conn
    conn.execute(
        "INSERT INTO orders (id,ts,instance,symbol,side,order_type,amount,"
        "status,mode) VALUES (7,'2026-01-01T00:00:00Z','long','ETH/USDT',"
        "'buy','market',0.1,'filled','paper')")
    conn.commit()

    TradingBot._record_decision_signal(bot, "BTC/USDT",
        {"signal": "HOLD", "confidence": 0.0, "reason": "flat"})
    TradingBot._record_decision_signal(bot, "ETH/USDT",
        {"signal": "BUY", "confidence": 0.8, "reason": "trend",
         "_acted": True, "_db_order_id": 7})

    rows = eng._repo._conn.execute(
        "SELECT symbol, decision, acted, order_id FROM signals ORDER BY id"
    ).fetchall()
    assert (rows[0]["symbol"], rows[0]["decision"], rows[0]["acted"],
            rows[0]["order_id"]) == ("BTC/USDT", "HOLD", 0, None)
    assert (rows[1]["symbol"], rows[1]["decision"], rows[1]["acted"],
            rows[1]["order_id"]) == ("ETH/USDT", "BUY", 1, 7)


def test_equity_snapshot_values(tmp_path):
    eng = _engine(tmp_path)
    bot = _stub_bot(eng, initial_capital=500.0, daily_pnl=5.0)
    bal, eq = TradingBot._equity_snapshot_values(bot)
    assert bal == 505.0
    assert eq == 505.0
