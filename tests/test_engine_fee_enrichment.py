import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine


def _engine(tmp_path):
    config = {"data": {"trades_file": str(tmp_path / "trades.csv"),
                       "db_file": str(tmp_path / "trading.db")},
              "exchange": {"rate_limit": 0.0}}
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_persist_close_records_fee_when_provided(tmp_path):
    eng = _engine(tmp_path)
    eng._persist_close(symbol="BTC/USDT", side="buy", close_side="sell",
                       amount=0.5, entry_price=100.0, close_price=110.0,
                       pnl=5.0, reason="take_profit", opened_at=None,
                       fee=0.25, fee_currency="USDT")
    row = eng._repo._conn.execute(
        "SELECT fee, fee_currency FROM fills ORDER BY id DESC LIMIT 1").fetchone()
    assert row["fee"] == 0.25 and row["fee_currency"] == "USDT"


def test_engine_close_noop_safe(tmp_path):
    eng = _engine(tmp_path)
    eng.close()        # paper: no client / no-op, must not raise
