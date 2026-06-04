import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine


def _engine(tmp_path):
    config = {
        "data": {
            "trades_file": str(tmp_path / "trades.csv"),
            "db_file": str(tmp_path / "trading.db"),
        },
        "exchange": {"rate_limit": 0.0},
    }
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_engine_builds_repo(tmp_path):
    eng = _engine(tmp_path)
    assert eng._repo is not None
    assert eng._repo.instance == "long"
    assert (tmp_path / "trading.db").exists()


def test_record_trade_writes_to_db(tmp_path):
    eng = _engine(tmp_path)
    position = {"symbol": "BTC/USDT", "side": "buy",
                "entry_price": 100.0, "amount": 0.5}
    eng._record_trade(position, close_price=110.0, pnl=5.0, reason="take_profit")

    rows = eng._repo.recent_trades(limit=5)
    assert len(rows) == 1
    assert rows[0].symbol == "BTC/USDT"
    assert rows[0].pnl == 5.0
    assert rows[0].reason == "take_profit"
    # In-memory history still populated (back-compat).
    assert len(eng._trade_history) == 1


def test_db_path_defaults_to_data_dir(tmp_path):
    config = {"data": {"data_dir": str(tmp_path / "sub")},
              "exchange": {}}
    eng = ExecutionEngine(config, mode="paper", trade_direction="both")
    assert (tmp_path / "sub" / "trading.db").exists()


def test_execute_order_records_order(tmp_path):
    eng = _engine(tmp_path)
    result = eng.execute_order(symbol="BTC/USDT", side="buy", amount=0.001,
                               price=100.0, order_type="market")
    assert result.get("success") is True
    assert result.get("db_order_id", -1) > 0
    row = eng._repo._conn.execute(
        "SELECT * FROM orders WHERE id=?", (result["db_order_id"],)).fetchone()
    assert row["symbol"] == "BTC/USDT"
    assert row["side"] == "buy"
    assert row["reduce_only"] == 0
    assert row["status"] == "filled"


def test_close_position_records_position_and_fill(tmp_path):
    eng = _engine(tmp_path)
    eng.execute_order(symbol="BTC/USDT", side="buy", amount=0.001,
                      price=100.0, order_type="market")
    close = eng.close_position("BTC/USDT", reason="take_profit")
    assert close.get("success") is True

    pos = eng._repo._conn.execute(
        "SELECT * FROM positions ORDER BY id DESC LIMIT 1").fetchone()
    assert pos["status"] == "closed"
    assert pos["realized_pnl"] is not None
    close_orders = eng._repo._conn.execute(
        "SELECT COUNT(*) FROM orders WHERE reduce_only=1").fetchone()[0]
    assert close_orders == 1
    fills = eng._repo._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    assert fills == 1
