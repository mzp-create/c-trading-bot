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


def test_db_path_defaults_beside_trades_file(tmp_path):
    config = {"data": {"trades_file": str(tmp_path / "sub" / "trades.csv")},
              "exchange": {"rate_limit": 0.0}}
    eng = ExecutionEngine(config, mode="paper", trade_direction="both")
    assert (tmp_path / "sub" / "trading.db").exists()
