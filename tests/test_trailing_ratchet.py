"""Regression test for the trailing-stop ratchet fix: a trailing stop must only
move toward the position (up for a long, down for a short), never loosen on a
price retrace."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from risk.manager import RiskManager

CFG = {
    "trading": {"initial_capital": 1000, "max_risk_per_trade": 0.02},
    "risk": {"leverage": 1.0, "stop_loss_pct": 2.0,
             "trailing_stop": True, "trailing_stop_activation": 2.0,
             "trailing_stop_distance": 0.5,
             "max_daily_loss": 1000, "max_drawdown_pct": 100},
}


def test_trailing_stop_does_not_loosen_on_retrace_long():
    rm = RiskManager(CFG)
    pos = {"entry_price": 100.0, "amount": 1.0, "side": "buy",
           "stop_loss": 98.0, "take_profit": 0, "trailing_stop": True}

    # Price rises to 110 -> stop ratchets up to ~109.45 (110 * (1 - 0.5%)).
    rm.update_position(pos, 110.0)
    s1 = pos["stop_loss"]
    assert s1 > 98.0                      # activated and rose

    # Small retrace to 109.5 (still above the trailed stop, so no SL hit).
    # The OLD code recomputed stop = 109.5*0.995 = 108.95 (LOOSENED). The fix
    # ratchets, so the stop must NOT drop.
    rm.update_position(pos, 109.5)
    assert pos["stop_loss"] == s1, f"trailing stop loosened: {s1} -> {pos['stop_loss']}"


def test_trailing_stop_does_not_loosen_on_retrace_short():
    rm = RiskManager(CFG)
    pos = {"entry_price": 100.0, "amount": 1.0, "side": "sell",
           "stop_loss": 102.0, "take_profit": 0, "trailing_stop": True}

    # Price falls to 90 -> stop ratchets down to ~90.45 (90 * (1 + 0.5%)).
    rm.update_position(pos, 90.0)
    s1 = pos["stop_loss"]
    assert s1 < 102.0

    # Small retrace up to 90.5 (still below the trailed stop). Must not loosen.
    rm.update_position(pos, 90.5)
    assert pos["stop_loss"] == s1, f"short trailing stop loosened: {s1} -> {pos['stop_loss']}"
