import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from execution.engine import ExecutionEngine


def _engine(tmp_path):
    config = {"data": {"trades_file": str(tmp_path / "t.csv"),
                       "db_file": str(tmp_path / "t.db")},
              "exchange": {"rate_limit": 0.0},
              "risk": {"stop_loss_pct": 3.0, "take_profit_pct": 6.0}}
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


def test_engine_has_stop_manager(tmp_path):
    eng = _engine(tmp_path)
    assert eng._stop_mgr is not None


def test_entry_places_stop_and_close_cancels_it(tmp_path):
    eng = _engine(tmp_path)
    calls = {"place": [], "cancel": []}
    eng._stop_mgr.place = lambda *a, **k: calls["place"].append((a, k))
    eng._stop_mgr.cancel = lambda symbol: calls["cancel"].append(symbol)

    eng.execute_order(symbol="SOL/USDT", side="buy", amount=1.0, price=60.0,
                      stop_loss_pct=3.0, take_profit_pct=6.0)
    assert len(calls["place"]) == 1
    assert calls["place"][0][0][0] == "SOL/USDT"

    eng.close_position("SOL/USDT", reason="test")
    assert calls["cancel"] == ["SOL/USDT"]


def test_sync_positions_triggers_stop_reconcile(tmp_path):
    eng = _engine(tmp_path)
    seen = {}
    eng._stop_mgr.reconcile = lambda positions: seen.setdefault("called", positions)
    # sync_positions_at_startup returns early in paper mode; override mode and
    # stub fetch_positions so the loop body is skipped cleanly.
    eng.mode = "live"
    eng._client.fetch_positions = lambda: []
    eng.sync_positions_at_startup()
    assert "called" in seen
