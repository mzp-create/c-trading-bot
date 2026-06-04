import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine


def _engine(tmp_path):
    config = {
        "trading": {"initial_capital": 1000.0,
                    "symbols": [{"name": "BTC/USDT", "enabled": True}]},
        "data": {"db_file": str(tmp_path / "t.db")},
        "risk": {"trailing_stop": False},
        "exchange": {},
    }
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_paper_engine_has_client(tmp_path):
    eng = _engine(tmp_path)
    assert eng._client is not None
    assert not hasattr(eng, "_paper_balance")
    assert not hasattr(eng, "_paper_execute_order")


def test_open_then_position_via_client(tmp_path):
    eng = _engine(tmp_path)
    r = eng.execute_order("BTC/USDT", "buy", 0.01, 100.0, "market",
                          stop_loss_pct=2.0, take_profit_pct=4.0)
    assert r["success"] is True
    pos = [p for p in eng.open_positions if p["symbol"] == "BTC/USDT"]
    assert len(pos) == 1
    assert "stop_loss" in pos[0] and "take_profit" in pos[0]
