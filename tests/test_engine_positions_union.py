import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine


def _engine(tmp_path, trust=False):
    config = {"trading": {"initial_capital": 1000.0,
                          "symbols": [{"name": "BTC/USDT", "enabled": True}]},
              "data": {"db_file": str(tmp_path / "t.db")},
              "risk": {}, "exchange": {"trust_exchange_positions": trust}}
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_union_surfaces_riskstate_known_position(tmp_path):
    eng = _engine(tmp_path, trust=False)
    eng._client.fetch_positions = lambda: []          # client reports nothing
    eng._risk_state.set("BTC/USDT", stop_loss=0, take_profit=0,
                        trailing_stop=False, trailing_activation=0,
                        trailing_distance=0, entry_price=100.0, side="sell")
    eng._risk_entry["BTC/USDT"] = {"symbol": "BTC/USDT", "side": "sell",
                                   "amount": 0.01, "entry_price": 100.0}
    syms = [p["symbol"] for p in eng.open_positions]
    assert "BTC/USDT" in syms


def test_trust_true_skips_union(tmp_path):
    eng = _engine(tmp_path, trust=True)
    eng._client.fetch_positions = lambda: []
    eng._risk_state.set("BTC/USDT", stop_loss=0, take_profit=0,
                        trailing_stop=False, trailing_activation=0,
                        trailing_distance=0, entry_price=100.0, side="sell")
    eng._risk_entry["BTC/USDT"] = {"symbol": "BTC/USDT", "side": "sell",
                                   "amount": 0.01, "entry_price": 100.0}
    assert eng.open_positions == []
