"""Paper-parity GOLDEN characterization tests (pre-refactor baseline).

These tests capture the CURRENT engine's paper behavior as of the
pre-Phase-4 baseline.  They must PASS against today's engine.  When the
engine is refactored, re-running this suite proves paper results did not
drift.

Constants captured here (as of baseline):
  TAKER_FEE = 0.001   (0.1%)
  MAKER_FEE = 0.0
  SLIPPAGE  = 0.0005  (0.05%)

Paper market-buy fill price: price * (1 + SLIPPAGE)
Fee: fill_price * amount * TAKER_FEE
close_position PnL: round((close_price - entry_price) * amount, 2)
  (no fee deducted on close in the paper branch)
get_balance() returns a bare float (the "free" USDT figure).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from execution.engine import ExecutionEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine(tmp_path, capital: float = 1000.0) -> ExecutionEngine:
    config = {
        "trading": {
            "initial_capital": capital,
            "symbols": [{"name": "BTC/USDT", "enabled": True}],
        },
        "data": {
            "trades_file": str(tmp_path / "t.csv"),
            "db_file": str(tmp_path / "t.db"),
        },
        "risk": {"trailing_stop": False},
        "exchange": {"rate_limit": 0.0},
    }
    return ExecutionEngine(config, mode="paper", trade_direction="both",
                           instance="long")


# ---------------------------------------------------------------------------
# Test 1 — open long: fill price, fee, side, position tracking
# ---------------------------------------------------------------------------

def test_golden_open_long(tmp_path):
    """Market buy fills at price*(1+SLIPPAGE); fee = fill*amount*TAKER_FEE."""
    eng = _engine(tmp_path)
    r = eng.execute_order("BTC/USDT", "buy", 0.01, 100.0, "market",
                          stop_loss_pct=2.0, take_profit_pct=4.0)

    # --- core success keys ---
    assert r["success"] is True
    assert r["side"] == "buy"

    # filled_price = round(100.0 * 1.0005, 2) = 100.05
    assert r["filled_price"] == 100.05

    # fee = round(100.05 * 0.01 * 0.001, 8) = 0.0010005
    assert r["fee"] == 0.0010005

    # amount is passed through unmodified
    assert r["amount"] == 0.01

    # order_id and db_order_id are present (values are non-deterministic)
    assert isinstance(r["order_id"], str) and r["order_id"].startswith("paper_")
    assert isinstance(r["db_order_id"], int)

    # --- position tracking ---
    positions = [p for p in eng.open_positions if p["symbol"] == "BTC/USDT"]
    assert len(positions) == 1
    pos = positions[0]
    assert abs(pos["amount"] - 0.01) < 1e-12
    # entry_price in the position dict is fill_price (not rounded to 2 dp)
    assert pos["entry_price"] == 100.05
    assert pos["side"] == "buy"


# ---------------------------------------------------------------------------
# Test 2 — close long: PnL rounding matches round(raw_pnl, 2)
# ---------------------------------------------------------------------------

def test_golden_close_long_pnl(tmp_path):
    """close_position PnL = round((close_price - entry_price)*amount, 2).

    No fee is deducted on the paper close path.
    The engine rounds to 2 decimal places, so the raw value 0.0995 becomes 0.1.
    """
    eng = _engine(tmp_path)
    eng.execute_order("BTC/USDT", "buy", 0.01, 100.0, "market",
                      stop_loss_pct=2.0, take_profit_pct=4.0)

    # Force the close price to 110.0 by monkey-patching _current_price.
    # entry_price = 100.05 (fill_price), so raw PnL = (110-100.05)*0.01 = 0.0995
    # round(0.0995, 2) = 0.1  (rounds up)
    eng._current_price = lambda symbol, fallback: 110.0

    res = eng.close_position("BTC/USDT", reason="take_profit")

    assert res["success"] is True
    assert res["error"] is None
    assert res["price"] == 110.0

    # Characterization: the engine returns round(pnl, 2).
    # raw = (110.0 - 100.05) * 0.01 = 0.0995 → rounds to 0.10
    assert res["pnl"] == 0.10


# ---------------------------------------------------------------------------
# Test 3 — balance after open: get_balance() is a bare float, drops by cost+fee
# ---------------------------------------------------------------------------

def test_golden_balance_after_open(tmp_path):
    """get_balance() returns a float (free USDT).  It decreases by cost + fee.

    cost = fill_price * amount = 100.05 * 0.01 = 1.0005
    fee  = fill_price * amount * TAKER_FEE = 100.05 * 0.01 * 0.001 = 0.0010005
    balance = 1000.0 - 1.0005 - 0.0010005 = 998.9984995
    """
    eng = _engine(tmp_path, capital=1000.0)
    eng.execute_order("BTC/USDT", "buy", 0.01, 100.0, "market",
                      stop_loss_pct=None, take_profit_pct=None)

    bal = eng.get_balance()

    # get_balance() returns a plain float (not a dict)
    assert isinstance(bal, float)

    # Exact expected value:
    #   fill_price = 100.0 * 1.0005 = 100.05
    #   cost       = 100.05 * 0.01  = 1.0005
    #   fee        = 100.05 * 0.01 * 0.001 = 0.0010005
    #   balance    = 1000.0 - 1.0005 - 0.0010005 = 998.9984995
    assert abs(bal - 998.9984995) < 1e-9
